"""Tests for sagemaker/proxy.py. Network use stays on 127.0.0.1; AWS is never called."""

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import random
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROXY_PATH = Path(__file__).resolve().parent.parent / "proxy.py"


def load_proxy():
    spec = importlib.util.spec_from_file_location("sagemaker_proxy", PROXY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


proxy = load_proxy()

ENDPOINT = "drip-ai"
MODEL = "drip-coder"
CANARY_API_KEY = "sk-canary-api-key-7f3a9c"
CANARY_ACCESS_KEY = "AKIDCANARY7F3A9C"
CANARY_SECRET_KEY = "canary-secret-key-0b8e41"
CANARY_SESSION_TOKEN = "canary-session-token-55d2"
CANARY_CREDENTIALS = proxy.AwsCredentials(
    CANARY_ACCESS_KEY, CANARY_SECRET_KEY, CANARY_SESSION_TOKEN
)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# The empty_message vector from AWS's event-stream test suite (base64 AAAAEAAAAAAFwkjrfZjI/w==).
AWS_EMPTY_MESSAGE = bytes.fromhex("000000100000000005c248eb7d98c8ff")


# Event-stream frames built from the spec, independent of the decoder under test.


def string_header(name, value):
    encoded_name, encoded_value = name.encode(), value.encode()
    return (
        bytes([len(encoded_name)])
        + encoded_name
        + b"\x07"
        + struct.pack(">H", len(encoded_value))
        + encoded_value
    )


# One header of each non-string type: bool true, bool false, byte, short, int, long, bytes,
# timestamp, uuid.
NON_STRING_HEADERS = (
    b"\x04flag\x00"
    + b"\x05flag2\x01"
    + b"\x04byte\x02\x7f"
    + b"\x05short\x03\x00\x01"
    + b"\x03int\x04\x00\x00\x00\x01"
    + b"\x04long\x05"
    + bytes(8)
    + b"\x05bytes\x06\x00\x03abc"
    + b"\x02ts\x08"
    + bytes(8)
    + b"\x04uuid\x09"
    + b"\x11" * 16
)


def frame(headers, payload):
    prelude = struct.pack(">II", 16 + len(headers) + len(payload), len(headers))
    message = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + payload
    return message + struct.pack(">I", zlib.crc32(message))


def payload_part_frame(payload, extra_headers=b""):
    headers = (
        extra_headers
        + string_header(":message-type", "event")
        + string_header(":event-type", "PayloadPart")
    )
    return frame(headers, payload)


def exception_frame(code):
    headers = string_header(":message-type", "exception") + string_header(":exception-type", code)
    return frame(headers, b'{"Message":"model failed"}')


def decode_in_pieces(stream, sizes):
    decoder = proxy.EventStreamDecoder()
    messages, offset = [], 0
    for size in sizes:
        messages += decoder.feed(stream[offset : offset + size])
        offset += size
    messages += decoder.feed(stream[offset:])
    decoder.finish()
    return messages


class EventStreamDecoderTest(unittest.TestCase):
    def test_decodes_identically_across_arbitrary_byte_splits(self):
        stream = (
            AWS_EMPTY_MESSAGE
            + payload_part_frame(b'data: {"a":1}\n\n', NON_STRING_HEADERS)
            + payload_part_frame(b"data: [DONE]\n\n")
            + exception_frame("ModelStreamError")
        )
        whole = decode_in_pieces(stream, [])
        self.assertEqual(
            [message.payload for message in whole],
            [b"", b'data: {"a":1}\n\n', b"data: [DONE]\n\n", b'{"Message":"model failed"}'],
        )
        self.assertEqual(whole[0].headers, {})
        self.assertEqual(whole[1].headers, {":message-type": "event", ":event-type": "PayloadPart"})
        self.assertEqual(decode_in_pieces(stream, [1] * len(stream)), whole)
        rng = random.Random(7)
        for _ in range(50):
            self.assertEqual(
                decode_in_pieces(stream, [rng.randint(1, 40) for _ in range(20)]), whole
            )

    def test_rejects_a_corrupted_length_by_prelude_crc_before_waiting_for_it(self):
        corrupted = bytearray(payload_part_frame(b"data"))
        corrupted[1] ^= 0x01  # total length grows by 64 KiB, still within the size limits
        with self.assertRaises(proxy.EventStreamError):
            proxy.EventStreamDecoder().feed(bytes(corrupted))

    def test_rejects_a_corrupted_message_crc(self):
        corrupted = bytearray(payload_part_frame(b"data"))
        corrupted[-6] ^= 0x01
        with self.assertRaises(proxy.EventStreamError):
            proxy.EventStreamDecoder().feed(bytes(corrupted))

    def test_rejects_a_stream_that_ends_inside_a_frame(self):
        decoder = proxy.EventStreamDecoder()
        decoder.feed(payload_part_frame(b"data")[:-1])
        with self.assertRaises(proxy.EventStreamError):
            decoder.finish()

    def test_exception_frame_surfaces_its_code(self):
        (message,) = proxy.EventStreamDecoder().feed(exception_frame("ModelStreamError"))
        with self.assertRaises(proxy.UpstreamStreamError) as raised:
            proxy.payload_part(message)
        self.assertEqual(raised.exception.code, "ModelStreamError")


SUITE_CREDENTIALS = proxy.AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
SUITE_HEADERS = {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}
SUITE_CREDENTIAL_SCOPE = "AKIDEXAMPLE/20150830/us-east-1/service/aws4_request"
SUITE_STS_TOKEN = (
    "AQoDYXdzEPT//////////wEXAMPLEtc764bNrC9SAPBSM22wDOk4x4HIZ8j4FZTwdQWLWsKWHGBuFqwAeMicRXmx"
    "fpSPfIeoIYRqTflfKD8YUuwthAx7mSEI/qkPpKPi/kMcGdQrmGdeehM4IC1NtBmUpp2wUE8phUZampKsburEDy0"
    "KPkyQDYwT7WZ0wq5VSXDvp75YU9HFvlRd8Tx6q6fE8YQcHNVXAkiY9q6d+xo0rKwT38xVqr7ZD0u0iPPkUL64lI"
    "ZbqBAz+scqKmlzm8FDrypNC9Yjc8fPOLn9FX9KSYvKTr4rvx3iSIlTJabIQwj2ICCR/oLxBA=="
)


class SigV4Test(unittest.TestCase):
    """Published vectors: the AWS SigV4 test suite and the S3 GET-object example."""

    def test_matches_published_vectors(self):
        sts_credentials = proxy.AwsCredentials(
            "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", SUITE_STS_TOKEN
        )
        s3_credentials = proxy.AwsCredentials(
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )
        s3_headers = {
            "Host": "examplebucket.s3.amazonaws.com",
            "Range": "bytes=0-9",
            "x-amz-content-sha256": EMPTY_SHA256,
            "x-amz-date": "20130524T000000Z",
        }
        suite = (SUITE_CREDENTIALS, "us-east-1", "service", "20150830T123600Z")
        suite_scope = f"Credential={SUITE_CREDENTIAL_SCOPE}"
        # (method, path, headers, (credentials, region, service, date), credential, signed headers, signature)
        vectors = {
            "get-vanilla": (
                "GET", "/", SUITE_HEADERS, suite, suite_scope, "host;x-amz-date",
                "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
            ),
            "post-vanilla": (
                "POST", "/", SUITE_HEADERS, suite, suite_scope, "host;x-amz-date",
                "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b",
            ),
            "post-sts-header-before": (
                "POST", "/", {**SUITE_HEADERS, "X-Amz-Security-Token": SUITE_STS_TOKEN},
                (sts_credentials, *suite[1:]), suite_scope, "host;x-amz-date;x-amz-security-token",
                "85d96828115b5dc0cfc3bd16ad9e210dd772bbebba041836c64533a82be05ead",
            ),
            "s3-get-object with x-amz-content-sha256": (
                "GET", "/test.txt", s3_headers, (s3_credentials, "us-east-1", "s3", "20130524T000000Z"),
                "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request",
                "host;range;x-amz-content-sha256;x-amz-date",
                "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
            ),
        }
        for label, (method, path, headers, signing, credential, signed, signature) in vectors.items():
            credentials, region, service, date = signing
            with self.subTest(label):
                self.assertEqual(
                    proxy.sigv4_authorization(
                        method, path, headers, EMPTY_SHA256, credentials, region, service, date
                    ),
                    f"AWS4-HMAC-SHA256 {credential}, SignedHeaders={signed}, Signature={signature}",
                )


class CredentialCacheTest(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def cache_returning(self, *credential_sets):
        fetched = []

        def fetch():
            fetched.append(None)
            return credential_sets[min(len(fetched), len(credential_sets)) - 1]

        return proxy.CredentialCache(fetch, now=lambda: self.now), fetched

    def test_refetches_within_five_minutes_of_expiration(self):
        expiring = proxy.AwsCredentials("AKIDOLD", "old", "token", self.NOW + timedelta(minutes=10))
        fresh = proxy.AwsCredentials("AKIDNEW", "new", "token", self.NOW + timedelta(hours=1))
        cache, fetched = self.cache_returning(expiring, fresh)
        self.now = self.NOW
        self.assertEqual(cache.get().access_key_id, "AKIDOLD")
        self.now = self.NOW + timedelta(minutes=4)
        self.assertEqual(cache.get().access_key_id, "AKIDOLD")
        self.now = self.NOW + timedelta(minutes=5)
        self.assertEqual(cache.get().access_key_id, "AKIDNEW")
        self.assertEqual(len(fetched), 2)

    def test_keeps_credentials_without_expiration(self):
        cache, fetched = self.cache_returning(proxy.AwsCredentials("AKIDLONG", "secret"))
        self.now = self.NOW
        cache.get()
        self.now = self.NOW + timedelta(days=365)
        cache.get()
        self.assertEqual(len(fetched), 1)

    def test_parses_cli_output_and_rejects_malformed_output(self):
        output = json.dumps(
            {
                "Version": 1,
                "AccessKeyId": CANARY_ACCESS_KEY,
                "SecretAccessKey": CANARY_SECRET_KEY,
                "SessionToken": CANARY_SESSION_TOKEN,
                "Expiration": "2026-10-05T13:00:00Z",
            }
        ).encode()
        credentials = proxy.parse_process_credentials(output)
        self.assertEqual(credentials.expiration, datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc))
        self.assertNotIn(CANARY_SECRET_KEY, repr(credentials))
        self.assertNotIn(CANARY_SESSION_TOKEN, repr(credentials))
        for bad in (b"not json", b'{"Version": 2}', b'{"Version": 1, "AccessKeyId": "AKID"}'):
            with self.assertRaises(proxy.CredentialsError):
                proxy.parse_process_credentials(bad)


# A fake SageMaker runtime on 127.0.0.1 that records each call and answers with `respond`.


class FakeSageMakerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with self.server.lock:
            self.server.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )
        self.server.respond(self)
        self.close_connection = True

    def log_message(self, format, *args):
        pass


class FakeSageMaker(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeSageMakerHandler)
        self.lock = threading.Lock()
        self.requests = []
        self.respond = event_stream(b"{}")
        threading.Thread(target=self.serve_forever, args=(0.05,), daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self):
        self.shutdown()
        self.server_close()


def start_event_stream(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/vnd.amazon.eventstream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()


def write_chunk(handler, data):
    handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))


def finish_event_stream(handler, payloads):
    frames = b"".join(payload_part_frame(payload) for payload in payloads)
    for offset in range(0, len(frames), 7):  # 7-byte chunks split frames at arbitrary points
        write_chunk(handler, frames[offset : offset + 7])
    handler.wfile.write(b"0\r\n\r\n")


def event_stream(*payloads):
    def respond(handler):
        start_event_stream(handler)
        finish_event_stream(handler, payloads)

    return respond


def aws_error(status, error_type, document):
    def respond(handler):
        body = json.dumps(document).encode()
        handler.send_response(status)
        handler.send_header("x-amzn-ErrorType", error_type)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return respond


def respond_after(delay_seconds, respond):
    def delayed(handler):
        time.sleep(delay_seconds)
        with contextlib.suppress(OSError):  # the proxy may have hung up already
            respond(handler)

    return delayed


def raw_status(reply):
    return int(reply.split(b" ", 2)[1])


def read_until(response, size):
    received = b""
    while len(received) < size:
        piece = response.read1(size - len(received))
        if not piece:
            break
        received += piece
    return received


class ProxyServerTest(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.fake = FakeSageMaker()
        self.addCleanup(self.fake.close)
        self.credential_fetches = 0
        self.credentials_unavailable = False
        upstream = proxy.SageMakerUpstream(
            "us-east-1",
            ENDPOINT,
            proxy.CredentialCache(self.fetch_credentials),
            base_url=self.fake.url,
        )
        self.server = proxy.SageMakerProxyServer(0, CANARY_API_KEY, MODEL, upstream)
        self.server.daemon_threads = (
            False  # join handler threads on close so every log line is captured
        )
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.is_proxy_stopped = False
        self.addCleanup(self.stop_proxy)

    def fetch_credentials(self):
        self.credential_fetches += 1
        if self.credentials_unavailable:
            raise proxy.CredentialsError("export-credentials failed")
        return CANARY_CREDENTIALS

    def stop_proxy(self):
        if not self.is_proxy_stopped:
            self.is_proxy_stopped = True
            self.server.shutdown()
            self.server.server_close()

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)

    def request(self, method, path, body=None, headers=None, key=CANARY_API_KEY):
        all_headers = {"Authorization": f"Bearer {key}"} if key else {}
        all_headers.update(headers or {})
        connection = self.connect()
        try:
            connection.request(method, path, body=body, headers=all_headers)
            response = connection.getresponse()
            return response.status, response, response.read()
        finally:
            connection.close()

    def post_json(self, path, document, **kwargs):
        return self.request("POST", path, body=json.dumps(document).encode(), **kwargs)

    def test_streaming_chat_relays_each_part_in_order_as_it_arrives(self):
        parts = [b'data: {"a":1}\n\n', b'data: {"b"', b":2}\n\ndata: [DONE]\n\n"]
        release = threading.Event()
        released_by_client = []

        def respond(handler):
            start_event_stream(handler)
            write_chunk(handler, payload_part_frame(parts[0]))
            released_by_client.append(release.wait(5))
            finish_event_stream(handler, parts[1:])

        self.fake.respond = respond
        connection = self.connect()
        self.addCleanup(connection.close)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"model": MODEL, "stream": True, "messages": []}),
            headers={"Authorization": f"Bearer {CANARY_API_KEY}"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(response.getheader("Cache-Control"), "no-cache")
        self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
        self.assertEqual(read_until(response, len(parts[0])), parts[0])
        release.set()
        self.assertEqual(response.read(), b"".join(parts[1:]))
        self.assertEqual(released_by_client, [True])

    def test_non_streaming_request_returns_the_parts_as_one_body(self):
        self.fake.respond = event_stream(b'{"id":"x",', b'"choices":[]}')
        status, response, body = self.post_json(
            "/v1/chat/completions", {"model": MODEL, "messages": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"id":"x","choices":[]}')
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        self.assertEqual(response.getheader("Content-Length"), str(len(body)))

    def test_signs_the_upstream_call_and_routes_by_path_without_query(self):
        body = json.dumps({"model": MODEL, "max_tokens": 1, "messages": []}).encode()
        status, _, _ = self.request("POST", "/v1/messages?beta=true", body=body)
        self.assertEqual(status, 200)
        (sent,) = self.fake.requests
        headers = sent["headers"]
        self.assertEqual(sent["path"], f"/endpoints/{ENDPOINT}/invocations-response-stream")
        self.assertEqual(sent["body"], body)
        self.assertEqual(headers["x-amzn-sagemaker-custom-attributes"], "route=/v1/messages")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(headers["x-amz-security-token"], CANARY_SESSION_TOKEN)
        signed_names = (
            "host;x-amz-content-sha256;x-amz-date;x-amz-security-token;"
            "x-amzn-sagemaker-custom-attributes"
        )
        signed = {name: headers[name] for name in signed_names.split(";")}
        expected = proxy.sigv4_authorization(
            "POST",
            sent["path"],
            signed,
            hashlib.sha256(body).hexdigest(),
            CANARY_CREDENTIALS,
            "us-east-1",
            "sagemaker",
            headers["x-amz-date"],
        )
        self.assertEqual(headers["authorization"], expected)
        self.assertIn(
            f"/us-east-1/sagemaker/aws4_request, SignedHeaders={signed_names}, ", expected
        )

    def test_rejects_a_missing_or_wrong_key_with_vllms_401_body(self):
        for key in (None, "sk-wrong"):
            status, response, body = self.request("GET", "/v1/models", key=key)
            self.assertEqual((status, body), (401, b'{"error":"Unauthorized"}'))
            self.assertEqual(response.getheader("Content-Type"), "application/json")
        status, _, _ = self.post_json("/v1/chat/completions", {"messages": []}, key="sk-wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.requests, [])

    def test_accepts_the_key_in_x_api_key(self):
        status, _, _ = self.request(
            "GET", "/v1/models", key=None, headers={"x-api-key": CANARY_API_KEY}
        )
        self.assertEqual(status, 200)

    def test_rejects_an_origin_header_or_a_foreign_host_with_403(self):
        port = self.server.server_address[1]
        self.assertEqual(
            self.request("GET", "/v1/models", headers={"Origin": "http://localhost"})[0], 403
        )
        self.assertEqual(
            self.request("GET", "/v1/models", headers={"Host": f"evil.example:{port}"})[0], 403
        )
        self.assertEqual(
            self.request("GET", "/v1/models", headers={"Host": f"localhost:{port}"})[0], 200
        )

    def test_unknown_route_returns_404_json(self):
        for method, path in (
            ("GET", "/v1/unknown"),
            ("GET", "/v1/chat/completions"),
            ("POST", "/v1/models"),
            ("DELETE", "/v1/chat/completions"),
        ):
            status, _, body = self.request(method, path, body=b"{}" if method == "POST" else None)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"]["code"], "NotFound")
        self.assertEqual(self.fake.requests, [])

    def test_body_limit_is_6_mib_inclusive(self):
        connection = self.connect()
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/v1/completions")
        connection.putheader("Authorization", f"Bearer {CANARY_API_KEY}")
        connection.putheader("Content-Length", "6291457")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)
        prefix, suffix = b'{"pad":"', b'"}'
        at_limit = prefix + b"a" * (6291456 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(self.request("POST", "/v1/completions", body=at_limit)[0], 200)
        self.assertEqual([len(sent["body"]) for sent in self.fake.requests], [6291456])

    def test_invalid_json_returns_400_without_calling_sagemaker(self):
        status, _, body = self.request("POST", "/v1/chat/completions", body=b'{"messages": [')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "InvalidJSON")
        self.assertEqual(self.fake.requests, [])

    def test_model_error_relays_the_original_status_and_body(self):
        original = '{"object":"error","message":"max_tokens is too large","code":400}'
        self.fake.respond = aws_error(
            424,
            "ModelError:http://internal.amazon.com/coral/com.amazonaws.sagemaker/",
            {
                "ErrorCode": "CLIENT_ERROR_FROM_MODEL",
                "OriginalStatusCode": 400,
                "OriginalMessage": original,
                "message": "Received client error (400) from primary",
            },
        )
        status, response, body = self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual((status, body), (400, original.encode()))
        self.assertEqual(response.getheader("Content-Type"), "application/json")

    def test_unavailable_aws_credentials_return_502_with_login_hint(self):
        self.credentials_unavailable = True
        status, _, body = self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual(status, 502)
        self.assertEqual(
            json.loads(body),
            {
                "error": {
                    "type": "proxy_error",
                    "code": "CredentialsUnavailable",
                    "message": (
                        "AWS credentials unavailable; refresh your AWS login"
                        " (aws login, aws sso login, or your profile's method)"
                    ),
                }
            },
        )
        self.assertEqual(self.fake.requests, [])

    def test_rejected_aws_credentials_return_502_and_are_refetched(self):
        self.fake.respond = aws_error(
            403, "ExpiredTokenException", {"message": "The security token expired"}
        )
        status, _, body = self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual(status, 502)
        self.assertEqual(
            json.loads(body),
            {
                "error": {
                    "type": "proxy_error",
                    "code": "ExpiredTokenException",
                    "message": (
                        "AWS credentials rejected or expired; refresh your AWS login"
                        " (aws login, aws sso login, or your profile's method)"
                    ),
                }
            },
        )
        self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual(self.credential_fetches, 2)

    def test_seventeenth_concurrent_request_gets_429(self):
        arrived, release = threading.Semaphore(0), threading.Event()

        def hold(handler):
            arrived.release()
            release.wait(10)
            event_stream(b"{}")(handler)

        self.fake.respond = hold
        statuses = []
        clients = [
            threading.Thread(
                target=lambda: statuses.append(self.post_json("/v1/completions", {})[0])
            )
            for _ in range(16)
        ]
        for client in clients:
            client.start()
        for _ in clients:
            self.assertTrue(arrived.acquire(timeout=10))
        status, _, body = self.post_json("/v1/completions", {})
        release.set()
        for client in clients:
            client.join(10)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "TooManyRequests")
        self.assertEqual(statuses, [200] * 16)

    def test_models_route_lists_only_the_served_model(self):
        status, _, body = self.request("GET", "/v1/models?limit=10")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["object"], "list")
        (model,) = document["data"]
        self.assertEqual(
            {k: model[k] for k in ("id", "object", "owned_by")},
            {"id": MODEL, "object": "model", "owned_by": "drip-ai"},
        )
        self.assertIsInstance(model["created"], int)

    def test_server_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_client_disconnect_closes_the_upstream_connection(self):
        upstream_closed = threading.Event()

        def hold_open(handler):
            start_event_stream(handler)
            write_chunk(handler, payload_part_frame(b"data: first\n\n"))
            handler.connection.settimeout(5)
            try:
                if handler.connection.recv(1) == b"":
                    upstream_closed.set()
            except socket.timeout:
                pass
            except OSError:
                upstream_closed.set()

        self.fake.respond = hold_open
        connection = self.connect()
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"stream": True}),
            headers={"Authorization": f"Bearer {CANARY_API_KEY}"},
        )
        response = connection.getresponse()
        self.assertEqual(read_until(response, 13), b"data: first\n\n")
        connection.close()
        self.assertTrue(upstream_closed.wait(3))

    def test_request_past_its_deadline_returns_504(self):
        self.fake.respond = lambda handler: handler.connection.recv(1)
        with mock.patch.object(proxy, "REQUEST_DEADLINE_SECONDS", 1):
            status, _, body = self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual((status, json.loads(body)["error"]["code"]), (504, "DeadlineExceeded"))

    def test_streaming_request_past_its_deadline_is_truncated(self):
        # Parts keep arriving well inside the idle timeout, so only the deadline can end the stream.
        def trickle(handler):
            start_event_stream(handler)
            with contextlib.suppress(OSError):  # the proxy hangs up at the deadline
                for _ in range(30):  # about 9 seconds, far past the 1-second deadline
                    write_chunk(handler, payload_part_frame(b"data: tick\n\n"))
                    handler.wfile.flush()
                    time.sleep(0.3)
                handler.wfile.write(b"0\r\n\r\n")

        self.fake.respond = trickle
        connection = self.connect()
        self.addCleanup(connection.close)
        with mock.patch.object(proxy, "REQUEST_DEADLINE_SECONDS", 1), mock.patch.object(
            proxy, "UPSTREAM_IDLE_TIMEOUT_SECONDS", 30
        ):
            started = time.monotonic()
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"stream": True, "messages": []}),
                headers={"Authorization": f"Bearer {CANARY_API_KEY}"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            with self.assertRaises(http.client.IncompleteRead):  # no terminating 0-length chunk
                response.read()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.stop_proxy()
        self.assertRegex(
            self.stderr.getvalue(),
            r"(?m)^POST /v1/chat/completions 200 \d+ms \d+B error=DeadlineExceeded$",
        )

    def test_idle_streaming_upstream_returns_504(self):
        self.fake.respond = respond_after(1.5, event_stream(b"data: late\n\n"))
        with mock.patch.object(proxy, "UPSTREAM_IDLE_TIMEOUT_SECONDS", 0.5):
            status, _, body = self.post_json(
                "/v1/chat/completions", {"stream": True, "messages": []}
            )
        self.assertEqual((status, json.loads(body)["error"]["code"]), (504, "UpstreamTimeout"))

    def test_non_streaming_upstream_may_stay_silent_past_the_idle_timeout(self):
        # SageMaker sends nothing for a non-streaming call until the whole generation is done.
        self.fake.respond = respond_after(1.5, event_stream(b'{"id":"late"}'))
        with mock.patch.object(proxy, "UPSTREAM_IDLE_TIMEOUT_SECONDS", 0.5):
            status, _, body = self.post_json("/v1/chat/completions", {"messages": []})
        self.assertEqual((status, body), (200, b'{"id":"late"}'))

    def test_chunked_or_ambiguous_request_length_is_refused_without_calling_sagemaker(self):
        cases = (
            (b"Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n", {411}),
            (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}", {400, 411}),
        )
        for length_headers, body, allowed in cases:
            with self.subTest(length_headers=length_headers):
                reply = self.raw_exchange(
                    b"POST /v1/chat/completions HTTP/1.1\r\n"
                    + self.raw_host_and_key()
                    + length_headers
                    + b"\r\n"
                    + body
                )
                self.assertIn(raw_status(reply), allowed)
        self.assertEqual(self.fake.requests, [])

    def test_non_canonical_request_targets_return_404(self):
        port = self.server.server_address[1]
        for target in (
            f"http://localhost:{port}/v1/models",
            "/v1/%6dodels",
            "/v1/x/../models",
        ):
            with self.subTest(target=target):
                reply = self.raw_exchange(
                    b"GET %s HTTP/1.1\r\n" % target.encode()
                    + self.raw_host_and_key()
                    + b"Connection: close\r\n\r\n"
                )
                self.assertEqual(raw_status(reply), 404)
                self.assertIn(b'"code": "NotFound"', reply)

    def test_early_length_error_closes_a_keep_alive_connection(self):
        # Each body is a whole request: were it parsed, a second response would follow.
        smuggled = b"GET /v1/models HTTP/1.1\r\n" + self.raw_host_and_key() + b"\r\n"
        for length_headers in (
            b"Transfer-Encoding: chunked\r\n",
            b"Content-Length: 0\r\nContent-Length: %d\r\n" % len(smuggled),
        ):
            with self.subTest(length_headers=length_headers):
                reply = self.raw_exchange(
                    b"POST /v1/chat/completions HTTP/1.1\r\n"
                    + self.raw_host_and_key()
                    + b"Connection: keep-alive\r\n"
                    + length_headers
                    + b"\r\n"
                    + smuggled
                )
                self.assertEqual(raw_status(reply), 411)
                self.assertRegex(reply, rb"(?i)\r\nConnection: close\r\n")
                self.assertEqual(reply.count(b"HTTP/1.1 "), 1)
        self.assertEqual(self.fake.requests, [])

    def raw_host_and_key(self):
        port = self.server.server_address[1]
        return b"Host: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n" % (
            port,
            CANARY_API_KEY.encode(),
        )

    def raw_exchange(self, request):
        """Sends raw bytes and returns everything received until the proxy closes the socket."""
        with socket.create_connection(("127.0.0.1", self.server.server_address[1])) as client:
            client.settimeout(3)
            client.sendall(request)
            received = b""
            while True:
                try:
                    piece = client.recv(65536)
                except socket.timeout:
                    self.fail(f"the proxy kept the connection open after {received[:200]!r}")
                if not piece:
                    return received
                received += piece

    def test_log_has_one_metadata_line_per_request_and_no_secrets(self):
        self.fake.respond = event_stream(b'{"ok":true}')
        self.post_json("/v1/chat/completions?beta=true", {"messages": []})
        self.post_json(
            "/v1/chat/completions",
            {"stream": True},
            key=None,
            headers={"x-api-key": CANARY_API_KEY},
        )
        self.request("GET", "/v1/models", key="wrong-" + CANARY_API_KEY)
        self.fake.respond = aws_error(
            403, "UnrecognizedClientException", {"message": "invalid token"}
        )
        self.post_json("/v1/chat/completions", {"messages": []})
        self.stop_proxy()
        lines = self.stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[0], r"^POST /v1/chat/completions 200 \d+ms 11B$")
        self.assertRegex(lines[2], r"^GET /v1/models 401 \d+ms 24B$")
        self.assertRegex(
            lines[3],
            r"^POST /v1/chat/completions 502 \d+ms \d+B error=UnrecognizedClientException$",
        )
        upstream_traffic = repr(self.fake.requests)
        for canary in (CANARY_API_KEY, CANARY_SECRET_KEY, CANARY_SESSION_TOKEN):
            self.assertNotIn(canary, self.stderr.getvalue())
        self.assertNotIn(CANARY_API_KEY, upstream_traffic)
        self.assertNotIn(CANARY_SECRET_KEY, upstream_traffic)


class CommandLineTest(unittest.TestCase):
    ARGS = ("--region", "us-east-1", "--endpoint", ENDPOINT, "--model", MODEL, "--port", "18999")

    def run_proxy(self, args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-I", str(PROXY_PATH), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )

    def assert_fails_with_one_line(self, completed, pattern):
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(completed.stderr.strip().splitlines()), 1, completed.stderr)
        self.assertRegex(completed.stderr, pattern)

    def test_each_startup_failure_is_one_line(self):
        cases = []
        for flag, value in (
            ("--endpoint", "bad/name"),
            ("--region", "us-east-1.evil.example"),
            ("--port", "0"),
        ):
            args = list(self.ARGS)
            args[args.index(flag) + 1] = value
            cases.append((f"bad {flag}", args, "key\n", None, re.escape(flag)))
        cases.append(("empty key", self.ARGS, "\n", None, "API key"))
        with tempfile.TemporaryDirectory() as empty_dir:
            cases.append(("no aws on PATH", self.ARGS, "key\n", {"PATH": empty_dir},
                          r"AWS CLI v2\.9 or newer is required; `aws` is not on PATH"))
            for label, args, stdin, env, pattern in cases:
                with self.subTest(label):
                    self.assert_fails_with_one_line(self.run_proxy(args, stdin=stdin, env=env), pattern)

    def test_reads_the_aws_cli_version(self):
        self.assertEqual(
            proxy.aws_cli_version("aws-cli/2.37.9 Python/3.14.6 Darwin/27.0.0 exe/arm64"), (2, 37)
        )
        self.assertEqual(proxy.aws_cli_version("aws-cli/1.18.69 Python/2.7.18"), (1, 18))
        self.assertIsNone(proxy.aws_cli_version("command not found"))


if __name__ == "__main__":
    unittest.main()
