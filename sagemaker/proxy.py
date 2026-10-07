"""Serve the vLLM HTTP API on 127.0.0.1 by forwarding to a SageMaker real-time endpoint.

Clients send the drip-ai API key. The proxy signs each call with the operator's AWS credentials,
sends it through InvokeEndpointWithResponseStream, and relays the payload bytes unchanged.
The API key arrives as line 1 on stdin. It stays one stdlib-only file so `python3 -I` runs it with
nothing to install.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.client
import json
import re
import selectors
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NoReturn, Optional, TextIO

LISTEN_HOST = "127.0.0.1"
MAX_IN_FLIGHT = 16
MAX_BODY_BYTES = 6 * 1024 * 1024  # SageMaker's documented request payload limit
MODELS_PATH = "/v1/models"
POST_ROUTES = frozenset(
    {
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/responses",
        "/v1/messages",
        "/v1/messages/count_tokens",
    }
)
UNAUTHORIZED_BODY = b'{"error":"Unauthorized"}'  # vLLM's exact 401 body

CONNECT_TIMEOUT_SECONDS = 10
UPSTREAM_IDLE_TIMEOUT_SECONDS = 120
REQUEST_DEADLINE_SECONDS = 15 * 60
CLIENT_IDLE_TIMEOUT_SECONDS = 120
WATCH_INTERVAL_SECONDS = 0.5
READ_CHUNK_BYTES = 64 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
# ASSUMPTION: no non-streaming vLLM response comes near this; it only bounds buffering memory.
MAX_BUFFERED_RESPONSE_BYTES = 64 * 1024 * 1024

CREDENTIAL_REFRESH_MARGIN = timedelta(minutes=5)
AWS_CLI_MIN_VERSION = (2, 9)  # first release with `aws configure export-credentials`
AWS_CLI_TIMEOUT_SECONDS = 60
EXPORT_CREDENTIALS_COMMAND = ("aws", "configure", "export-credentials", "--format", "process")

# Both values are interpolated into the upstream URL, so only AWS's own shapes are accepted.
ENDPOINT_NAME_PATTERN = re.compile(r"[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}")
REGION_PATTERN = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")
SAFE_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
CONTENT_LENGTH_PATTERN = re.compile(r"[0-9]{1,16}")

LOGIN_REFRESH_HINT = "refresh your AWS login (aws login, aws sso login, or your profile's method)"
CREDENTIALS_REJECTED_MESSAGE = f"AWS credentials rejected or expired; {LOGIN_REFRESH_HINT}"
CREDENTIALS_UNAVAILABLE_MESSAGE = f"AWS credentials unavailable; {LOGIN_REFRESH_HINT}"
CREDENTIAL_ERROR_CODES = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "IncompleteSignature",
        "InvalidClientTokenId",
        "InvalidSignatureException",
        "MissingAuthenticationToken",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)
# Upstream rejections relayed with their own status; every other upstream failure becomes 502.
RELAYED_STATUS_BY_CODE = {
    "ValidationError": 400,
    "ThrottlingException": 429,
    "ServiceUnavailable": 503,
}
RELAYED_STATUSES = frozenset(RELAYED_STATUS_BY_CODE.values())
UPSTREAM_ERROR_MESSAGE_BY_STATUS = {
    400: "SageMaker rejected the request",
    429: "SageMaker is throttling requests",
    502: "SageMaker returned an error",
    503: "SageMaker endpoint is unavailable",
}
FAILURE_STATUS_BY_CODE = {"UpstreamTimeout": 504, "DeadlineExceeded": 504}
FAILURE_MESSAGE_BY_CODE = {
    "UpstreamTimeout": f"SageMaker sent nothing for {UPSTREAM_IDLE_TIMEOUT_SECONDS} seconds",
    "DeadlineExceeded": f"The request ran past {REQUEST_DEADLINE_SECONDS // 60} minutes",
    "ResponseTooLarge": "The SageMaker response is too large to buffer; request streaming instead",
}

EVENT_PRELUDE_BYTES = 12
EVENT_CRC_BYTES = 4
MAX_EVENT_HEADERS_BYTES = 128 * 1024  # botocore's limit
MAX_EVENT_PAYLOAD_BYTES = 24 * 1024 * 1024  # botocore's limit
# Value widths of fixed-size event-stream header types. Types 6 (bytes) and 7 (string) carry a
# 2-byte length instead.
FIXED_HEADER_VALUE_BYTES = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
BYTES_HEADER_TYPE = 6
STRING_HEADER_TYPE = 7


class StartupError(Exception):
    """The proxy cannot start; the message is one line for stderr."""


class CredentialsError(Exception):
    """AWS credentials could not be obtained. The message never contains credential values."""


class EventStreamError(Exception):
    """The upstream event stream is malformed or failed a CRC check."""


class UpstreamStreamError(Exception):
    """SageMaker sent an exception or error frame inside the response stream."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ClientDisconnectedError(Exception):
    """A write to the client socket failed."""


UPSTREAM_READ_ERRORS = (OSError, http.client.HTTPException, EventStreamError, UpstreamStreamError)


def log(line: str) -> None:
    # One write per line so request threads never interleave partial lines.
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def safe_error_code(raw: Optional[str], fallback: str) -> str:
    """Returns `raw` when it is a plain identifier fit for logs and JSON, else `fallback`."""
    return raw if raw and SAFE_ERROR_CODE_PATTERN.fullmatch(raw) else fallback


def proxy_error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"type": "proxy_error", "code": code, "message": message}}).encode()


# Credentials


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: Optional[str] = field(default=None, repr=False)
    expiration: Optional[datetime] = None


def parse_process_credentials(output: bytes) -> AwsCredentials:
    """Parses the credential_process JSON that `aws configure export-credentials` prints."""
    try:
        document = json.loads(output)
    except ValueError:
        raise CredentialsError("export-credentials printed invalid JSON") from None
    if not isinstance(document, dict) or document.get("Version") != 1:
        raise CredentialsError("export-credentials printed an unsupported format")
    access_key_id = document.get("AccessKeyId")
    secret_access_key = document.get("SecretAccessKey")
    session_token = document.get("SessionToken")
    expiration = document.get("Expiration")
    if not (_is_nonempty_str(access_key_id) and _is_nonempty_str(secret_access_key)):
        raise CredentialsError("export-credentials printed no access key")
    if session_token is not None and not _is_nonempty_str(session_token):
        raise CredentialsError("export-credentials printed an invalid session token")
    return AwsCredentials(
        access_key_id,
        secret_access_key,
        session_token,
        None if expiration is None else _parse_expiration(expiration),
    )


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _parse_expiration(value: object) -> datetime:
    if not isinstance(value, str):
        raise CredentialsError("export-credentials printed an invalid Expiration")
    try:
        # Python 3.9's fromisoformat does not accept a trailing Z.
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise CredentialsError("export-credentials printed an invalid Expiration") from None
    if parsed.tzinfo is None:
        raise CredentialsError("export-credentials printed an Expiration without a time zone")
    return parsed


def export_cli_credentials() -> AwsCredentials:
    """Resolves credentials the way the AWS CLI does, so AWS_PROFILE and SSO sessions apply."""
    try:
        completed = subprocess.run(
            EXPORT_CREDENTIALS_COMMAND,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=AWS_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CredentialsError("aws configure export-credentials did not finish") from None
    if completed.returncode != 0:
        raise CredentialsError(f"aws configure export-credentials exited {completed.returncode}")
    return parse_process_credentials(completed.stdout)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CredentialCache:
    """Shares one credential set across request threads and refetches it near its expiration."""

    def __init__(
        self,
        fetch: Callable[[], AwsCredentials] = export_cli_credentials,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._fetch = fetch
        self._now = now
        self._lock = threading.Lock()
        self._current: Optional[AwsCredentials] = None

    def get(self) -> AwsCredentials:
        # Held during the fetch so concurrent requests wait for one `aws` process, not one each.
        with self._lock:
            if self._current is None or self._is_near_expiration(self._current):
                self._current = self._fetch()
            return self._current

    def invalidate(self) -> None:
        with self._lock:
            self._current = None

    def _is_near_expiration(self, credentials: AwsCredentials) -> bool:
        if credentials.expiration is None:
            return False
        return credentials.expiration - self._now() <= CREDENTIAL_REFRESH_MARGIN


# Signature Version 4


def sigv4_authorization(
    method: str,
    path: str,
    headers: dict[str, str],
    payload_sha256: str,
    credentials: AwsCredentials,
    region: str,
    service: str,
    amz_date: str,
) -> str:
    """Returns the Authorization value that signs exactly `headers` for a request without a query.

    `headers` must include host and x-amz-date; `path` is the URI path as sent on the wire.
    """
    canonical_headers = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    signed_headers = ";".join(sorted(canonical_headers))
    # Every service except S3 signs the wire path URI-encoded a second time.
    canonical_uri = urllib.parse.quote(path, safe="/~")
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            "",
            "".join(f"{name}:{canonical_headers[name]}\n" for name in sorted(canonical_headers)),
            signed_headers,
            payload_sha256,
        ]
    )
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + credentials.secret_access_key).encode()
    for scope_part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, scope_part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


# AWS event stream


@dataclass(frozen=True)
class EventMessage:
    headers: dict[str, str]  # string-typed headers only; other types are skipped
    payload: bytes


class EventStreamDecoder:
    """Decodes AWS event-stream frames from bytes split at arbitrary points."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[EventMessage]:
        self._buffer += data
        messages = []
        while len(self._buffer) >= EVENT_PRELUDE_BYTES:
            total_length, headers_length = self._read_prelude()
            if len(self._buffer) < total_length:
                break
            frame = bytes(self._buffer[:total_length])
            del self._buffer[:total_length]
            messages.append(_decode_frame(frame, headers_length))
        return messages

    def finish(self) -> None:
        if self._buffer:
            raise EventStreamError("stream ended inside a frame")

    def _read_prelude(self) -> tuple[int, int]:
        total_length, headers_length, prelude_crc = struct.unpack_from(">III", self._buffer)
        if zlib.crc32(self._buffer[:8]) != prelude_crc:
            raise EventStreamError("prelude CRC mismatch")
        payload_length = total_length - headers_length - EVENT_PRELUDE_BYTES - EVENT_CRC_BYTES
        if (
            payload_length < 0
            or headers_length > MAX_EVENT_HEADERS_BYTES
            or payload_length > MAX_EVENT_PAYLOAD_BYTES
        ):
            raise EventStreamError("frame lengths out of range")
        return total_length, headers_length


def _decode_frame(frame: bytes, headers_length: int) -> EventMessage:
    (message_crc,) = struct.unpack_from(">I", frame, len(frame) - EVENT_CRC_BYTES)
    if zlib.crc32(frame[:-EVENT_CRC_BYTES]) != message_crc:
        raise EventStreamError("message CRC mismatch")
    headers_end = EVENT_PRELUDE_BYTES + headers_length
    return EventMessage(
        _decode_headers(frame[EVENT_PRELUDE_BYTES:headers_end]), frame[headers_end:-EVENT_CRC_BYTES]
    )


def _decode_headers(block: bytes) -> dict[str, str]:
    headers = {}
    offset = 0
    while offset < len(block):
        name_length = block[offset]
        name = _take(block, offset + 1, name_length)
        offset += 1 + name_length
        value_type = _take(block, offset, 1)[0]
        offset += 1
        if value_type in FIXED_HEADER_VALUE_BYTES:
            _take(block, offset, FIXED_HEADER_VALUE_BYTES[value_type])
            offset += FIXED_HEADER_VALUE_BYTES[value_type]
            continue
        if value_type not in (BYTES_HEADER_TYPE, STRING_HEADER_TYPE):
            raise EventStreamError("unknown header value type")
        (value_length,) = struct.unpack(">H", _take(block, offset, 2))
        value = _take(block, offset + 2, value_length)
        offset += 2 + value_length
        if value_type == STRING_HEADER_TYPE:
            headers[_utf8(name)] = _utf8(value)
    return headers


def _take(block: bytes, offset: int, length: int) -> bytes:
    if offset + length > len(block):
        raise EventStreamError("header runs past the header block")
    return block[offset : offset + length]


def _utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EventStreamError("header is not UTF-8") from None


def payload_part(message: EventMessage) -> bytes:
    """Returns PayloadPart bytes or b"" for other events; raises UpstreamStreamError on failures."""
    message_type = message.headers.get(":message-type")
    if message_type == "event":
        # Unknown event types are skipped, as the AWS SDKs do.
        return message.payload if message.headers.get(":event-type") == "PayloadPart" else b""
    if message_type == "exception":
        raise UpstreamStreamError(
            safe_error_code(message.headers.get(":exception-type"), "UnknownException")
        )
    if message_type == "error":
        raise UpstreamStreamError(
            safe_error_code(message.headers.get(":error-code"), "UnknownError")
        )
    raise UpstreamStreamError("UnknownMessageType")


def upstream_payload_parts(response: http.client.HTTPResponse) -> Iterator[bytes]:
    """Yields PayloadPart bytes from an InvokeEndpointWithResponseStream body in arrival order."""
    decoder = EventStreamDecoder()
    while True:
        data = response.read1(READ_CHUNK_BYTES)
        if not data:
            decoder.finish()
            return
        for message in decoder.feed(data):
            part = payload_part(message)
            if part:
                yield part


# SageMaker runtime


class SageMakerUpstream:
    """Signs and opens InvokeEndpointWithResponseStream calls for one endpoint."""

    def __init__(
        self,
        region: str,
        endpoint: str,
        credentials: CredentialCache,
        base_url: Optional[str] = None,
    ) -> None:
        # base_url exists for tests; production always uses the regional runtime host over TLS.
        url = urllib.parse.urlsplit(base_url or f"https://runtime.sagemaker.{region}.amazonaws.com")
        self.region = region
        self.credentials = credentials
        self.path = (
            f"/endpoints/{urllib.parse.quote(endpoint, safe='')}/invocations-response-stream"
        )
        self._host = url.netloc
        self._tls = ssl.create_default_context() if url.scheme == "https" else None

    def signed_headers(self, route: str, body: bytes) -> dict[str, str]:
        """Returns the headers that send `body` to vLLM's `route`; raises CredentialsError."""
        credentials = self.credentials.get()
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload_sha256 = hashlib.sha256(body).hexdigest()
        headers = {
            "host": self._host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_sha256,
            "x-amzn-sagemaker-custom-attributes": f"route={route}",
        }
        if credentials.session_token:
            headers["x-amz-security-token"] = credentials.session_token
        authorization = sigv4_authorization(
            "POST",
            self.path,
            headers,
            payload_sha256,
            credentials,
            self.region,
            "sagemaker",
            amz_date,
        )
        # The response is an event stream, so the call itself accepts any type.
        return {
            **headers,
            "authorization": authorization,
            "content-type": "application/json",
            "accept": "*/*",
        }

    def connect(self, read_timeout: float) -> http.client.HTTPConnection:
        """Opens the connection; `read_timeout` bounds each later socket read and write."""
        if self._tls is None:
            connection = http.client.HTTPConnection(self._host, timeout=CONNECT_TIMEOUT_SECONDS)
        else:
            connection = http.client.HTTPSConnection(
                self._host, timeout=CONNECT_TIMEOUT_SECONDS, context=self._tls
            )
        connection.connect()
        connection.sock.settimeout(read_timeout)
        return connection


@dataclass(frozen=True)
class UpstreamErrorReply:
    status: int
    content_type: str
    body: bytes
    code: str
    credentials_rejected: bool = False


def upstream_error_reply(status: int, error_type: Optional[str], body: bytes) -> UpstreamErrorReply:
    """Maps a non-200 SageMaker response to what the client receives."""
    document = _json_object(body)
    code = aws_error_code(error_type, document)
    if code == "ModelError" or status == 424:
        return _model_error_reply(document)
    if code in CREDENTIAL_ERROR_CODES or (status == 403 and code != "AccessDeniedException"):
        body = proxy_error_body(code, CREDENTIALS_REJECTED_MESSAGE)
        return UpstreamErrorReply(502, "application/json", body, code, credentials_rejected=True)
    if code == "AccessDeniedException":
        message = "These AWS credentials may not invoke this endpoint"
        return UpstreamErrorReply(502, "application/json", proxy_error_body(code, message), code)
    client_status = RELAYED_STATUS_BY_CODE.get(code, status if status in RELAYED_STATUSES else 502)
    message = UPSTREAM_ERROR_MESSAGE_BY_STATUS[client_status]
    return UpstreamErrorReply(
        client_status, "application/json", proxy_error_body(code, message), code
    )


def aws_error_code(error_type: Optional[str], document: dict) -> str:
    """Reads the AWS error code from x-amzn-ErrorType or the JSON body, without namespace or URI."""
    raw = str(error_type or document.get("__type") or document.get("code") or "")
    return safe_error_code(raw.split(":", 1)[0].rsplit("#", 1)[-1], "UpstreamError")


def _model_error_reply(document: dict) -> UpstreamErrorReply:
    original_status = document.get("OriginalStatusCode")
    original_message = document.get("OriginalMessage")
    status = (
        original_status
        if isinstance(original_status, int) and 400 <= original_status <= 599
        else 502
    )
    if not isinstance(original_message, str):
        body = proxy_error_body("ModelError", "The model container returned an error")
        return UpstreamErrorReply(status, "application/json", body, "ModelError")
    content_type = (
        "application/json"
        if _json_object(original_message.encode())
        else "text/plain; charset=utf-8"
    )
    return UpstreamErrorReply(status, content_type, original_message.encode(), "ModelError")


def _json_object(body: bytes) -> dict:
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    return document if isinstance(document, dict) else {}


class UpstreamWatchdog:
    """Shuts the upstream socket when the client hangs up or the request outlives its deadline.

    Shutting the socket ends the request thread's blocked read at once, and the closed connection
    tells SageMaker to stop generating.
    """

    def __init__(self, client: socket.socket, upstream: socket.socket, deadline: float) -> None:
        self.reason: Optional[str] = None
        self._client = client
        self._upstream = upstream
        self._deadline = deadline
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def __enter__(self) -> UpstreamWatchdog:
        threading.Thread(target=self._watch, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Not joined: the thread notices within one interval, and the lock stops any late abort.
        with self._lock:
            self._stopped.set()

    def _watch(self) -> None:
        # OSError or ValueError means the client socket closed under us: nothing is left to watch.
        with contextlib.suppress(OSError, ValueError), selectors.DefaultSelector() as selector:
            selector.register(self._client, selectors.EVENT_READ)
            is_watching_client = True
            while not self._stopped.is_set():
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    self._abort("DeadlineExceeded")
                    return
                interval = min(remaining, WATCH_INTERVAL_SECONDS)
                if not is_watching_client:
                    self._stopped.wait(interval)
                elif selector.select(interval):
                    if _peer_closed(self._client):
                        self._abort("ClientClosed")
                        return
                    # Pipelined bytes keep the socket readable; keep only the deadline.
                    is_watching_client = False

    def _abort(self, reason: str) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            self.reason = reason
            # The base-class call skips SSLSocket.shutdown, which unwraps TLS under the reader.
            with contextlib.suppress(OSError):
                socket.socket.shutdown(self._upstream, socket.SHUT_RDWR)


def _peer_closed(client: socket.socket) -> bool:
    try:
        return client.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, socket.timeout):
        return False
    except OSError:
        return True


# Local HTTP server


class ProxyRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "drip-ai-sagemaker-proxy"
    sys_version = ""
    timeout = CLIENT_IDLE_TIMEOUT_SECONDS
    server: SageMakerProxyServer

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    # Other methods get the same checks and a JSON 404. HEAD keeps http.server's bodiless 501.
    do_DELETE = do_OPTIONS = do_PATCH = do_PUT = do_GET

    def log_message(self, format: str, *args: object) -> None:
        """Silences http.server's log, which prints request lines; _handle logs metadata only."""

    def _handle(self) -> None:
        started = time.monotonic()
        self._status: Optional[int] = None
        self._bytes_out = 0
        self._body_read = False
        self._failure: Optional[str] = None
        path = self.path.split("?", 1)[0]
        try:
            self._dispatch(path)
        except ClientDisconnectedError:
            self.close_connection = True
            self._failure = self._failure or "ClientClosed"
        except Exception as error:  # last resort: a generic reply and a metadata-only log line
            self.close_connection = True
            self._failure = f"{type(error).__name__}@line{_error_line(error)}"
            if self._status is None:
                with contextlib.suppress(ClientDisconnectedError):
                    self._send_proxy_error(500, "InternalError", "The proxy failed; see its log")
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            failure = f" error={self._failure}" if self._failure else ""
            status = self._status or "-"
            log(
                f"{self.command} {_loggable_path(path)} {status} {elapsed_ms}ms "
                f"{self._bytes_out}B{failure}"
            )

    def _dispatch(self, path: str) -> None:
        if not self._is_local_request():
            self._send_proxy_error(
                403, "Forbidden", "Only local clients without an Origin header may call this proxy"
            )
            return
        if not self._has_api_key():
            self._send_body(401, "application/json", UNAUTHORIZED_BODY)
            return
        if not self.server.in_flight.acquire(blocking=False):
            self._send_proxy_error(
                429, "TooManyRequests", f"More than {MAX_IN_FLIGHT} requests are in flight"
            )
            return
        try:
            if self.command == "GET" and path == MODELS_PATH:
                self._send_body(200, "application/json", self.server.models_body)
            elif self.command == "POST" and path in POST_ROUTES:
                self._forward(path)
            else:
                self._send_proxy_error(404, "NotFound", "No such route")
        finally:
            self.server.in_flight.release()

    def _is_local_request(self) -> bool:
        # DNS-rebinding and browser defence: local CLI clients send a loopback Host and no Origin.
        hosts = self.headers.get_all("Host") or []
        return (
            "Origin" not in self.headers
            and len(hosts) == 1
            and hosts[0].lower() in self.server.allowed_hosts
        )

    def _has_api_key(self) -> bool:
        presented = [self.headers.get("x-api-key", "")]
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            presented.append(token.strip())
        # http.server decodes header bytes as Latin-1, so encoding back recovers the sent bytes.
        return any(
            hmac.compare_digest(key.encode("latin-1"), self.server.api_key)
            for key in presented
            if key
        )

    def _forward(self, route: str) -> None:
        body = self._read_body()
        if body is None:
            return
        try:
            document = json.loads(body)
        except (ValueError, RecursionError):
            self._send_proxy_error(400, "InvalidJSON", "Request body is not valid JSON")
            return
        wants_stream = isinstance(document, dict) and document.get("stream") is True
        deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
        upstream = self.server.upstream
        try:
            headers = upstream.signed_headers(route, body)
        except CredentialsError:
            self._send_proxy_error(502, "CredentialsUnavailable", CREDENTIALS_UNAVAILABLE_MESSAGE)
            return
        # SageMaker sends nothing for a non-streaming call until vLLM finishes the whole generation,
        # so only streaming reads are held to the idle timeout. The watchdog enforces the deadline.
        read_timeout = (
            UPSTREAM_IDLE_TIMEOUT_SECONDS
            if wants_stream
            else max(deadline - time.monotonic(), WATCH_INTERVAL_SECONDS)
        )
        try:
            connection = upstream.connect(read_timeout)
        except OSError:
            self._send_proxy_error(502, "UpstreamUnreachable", "Could not connect to SageMaker")
            return
        try:
            with UpstreamWatchdog(self.connection, connection.sock, deadline) as watchdog:
                self._exchange(connection, upstream.path, body, headers, wants_stream, watchdog)
        finally:
            connection.close()

    def _read_body(self) -> Optional[bytes]:
        lengths = self.headers.get_all("Content-Length") or []
        if "Transfer-Encoding" in self.headers or len(lengths) != 1:
            self._send_proxy_error(
                411, "LengthRequired", "Send the body with one Content-Length header"
            )
            return None
        if not CONTENT_LENGTH_PATTERN.fullmatch(lengths[0].strip()):
            self._send_proxy_error(
                400, "InvalidContentLength", "Content-Length is not a byte count"
            )
            return None
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            self._send_proxy_error(
                413, "PayloadTooLarge", f"Request body exceeds {MAX_BODY_BYTES} bytes"
            )
            return None
        try:
            body = self.rfile.read(length)
        except OSError:
            raise ClientDisconnectedError from None
        if len(body) != length:
            raise ClientDisconnectedError
        self._body_read = True
        return body

    def _exchange(
        self,
        connection: http.client.HTTPConnection,
        path: str,
        body: bytes,
        headers: dict[str, str],
        wants_stream: bool,
        watchdog: UpstreamWatchdog,
    ) -> None:
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
        except UPSTREAM_READ_ERRORS as error:
            self._end_after_failure(_exchange_failure_code(error, wants_stream, watchdog))
            return
        if response.status != 200:
            self._relay_upstream_error(response)
            return
        parts = upstream_payload_parts(response)
        try:
            if wants_stream:
                self._stream(parts)
            else:
                self._buffer(parts)
        except UPSTREAM_READ_ERRORS as error:
            self._end_after_failure(_exchange_failure_code(error, wants_stream, watchdog))

    def _stream(self, parts: Iterator[bytes]) -> None:
        # Headers wait for the first part, so a failure before any output still gets a JSON status.
        for part in parts:
            if self._status is None:
                self._start_event_stream()
            self._write(b"%x\r\n%s\r\n" % (len(part), part))
            self._bytes_out += len(part)
        if self._status is None:
            self._start_event_stream()
        self._write(b"0\r\n\r\n")

    def _buffer(self, parts: Iterator[bytes]) -> None:
        body = bytearray()
        for part in parts:
            body += part
            if len(body) > MAX_BUFFERED_RESPONSE_BYTES:
                self._end_after_failure("ResponseTooLarge")
                return
        self._send_body(200, "application/json", bytes(body))

    def _relay_upstream_error(self, response: http.client.HTTPResponse) -> None:
        try:
            body = response.read(MAX_ERROR_BODY_BYTES)
        except (OSError, http.client.HTTPException):
            body = b""  # the status and x-amzn-ErrorType still classify the error
        reply = upstream_error_reply(response.status, response.getheader("x-amzn-ErrorType"), body)
        if reply.credentials_rejected:
            self.server.upstream.credentials.invalidate()
        self._failure = reply.code
        self._send_body(reply.status, reply.content_type, reply.body)

    def _end_after_failure(self, code: str) -> None:
        self._failure = code
        if code == "ClientClosed" or self._status is not None:
            # No terminating chunk: the client sees a truncated stream, never a clean end.
            self.close_connection = True
            return
        message = FAILURE_MESSAGE_BY_CODE.get(code, "The SageMaker response stream failed")
        self._send_proxy_error(FAILURE_STATUS_BY_CODE.get(code, 502), code, message)

    def _start_event_stream(self) -> None:
        self._send_head(
            200,
            [
                ("Content-Type", "text/event-stream"),
                ("Cache-Control", "no-cache"),
                ("Transfer-Encoding", "chunked"),
            ],
        )

    def _send_proxy_error(self, status: int, code: str, message: str) -> None:
        self._send_body(status, "application/json", proxy_error_body(code, message))

    def _send_body(self, status: int, content_type: str, body: bytes) -> None:
        self._send_head(
            status, [("Content-Type", content_type), ("Content-Length", str(len(body)))]
        )
        self._write(body)
        self._bytes_out += len(body)

    def _send_head(self, status: int, headers: list[tuple[str, str]]) -> None:
        self._status = status
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if self._has_unread_body():
            # Unread body bytes would be parsed as the next request.
            self.send_header("Connection", "close")
        try:
            self.end_headers()
        except OSError:
            raise ClientDisconnectedError from None

    def _has_unread_body(self) -> bool:
        # Every Content-Length counts: with duplicates, the first alone may claim an empty body.
        lengths = self.headers.get_all("Content-Length") or []
        declares_body = (
            "Transfer-Encoding" in self.headers
            or len(lengths) > 1
            or any(length.strip() != "0" for length in lengths)
        )
        return declares_body and not self._body_read

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except OSError:
            raise ClientDisconnectedError from None


def _exchange_failure_code(
    error: BaseException, wants_stream: bool, watchdog: UpstreamWatchdog
) -> str:
    if watchdog.reason:
        return watchdog.reason
    code = _read_failure_code(error)
    # A non-streaming read times out only at the deadline, so report it as the deadline.
    return "DeadlineExceeded" if code == "UpstreamTimeout" and not wants_stream else code


def _read_failure_code(error: BaseException) -> str:
    if isinstance(error, socket.timeout):
        return "UpstreamTimeout"
    if isinstance(error, EventStreamError):
        return "EventStreamCorrupt"
    if isinstance(error, UpstreamStreamError):
        return error.code
    return "UpstreamReadError"


def _loggable_path(path: str) -> str:
    if path in POST_ROUTES or path == MODELS_PATH:
        return path
    return path[:120].encode("unicode_escape").decode("ascii")


def _error_line(error: BaseException) -> int:
    frames = traceback.extract_tb(error.__traceback__)
    return frames[-1].lineno if frames else 0


class SageMakerProxyServer(ThreadingHTTPServer):
    """Serves the vLLM routes on 127.0.0.1 only and forwards them to one SageMaker endpoint."""

    # Listen backlog. With socketserver's default of 5, macOS reset 4 of 16 simultaneous
    # connects (measured).
    request_queue_size = 4 * MAX_IN_FLIGHT

    def __init__(self, port: int, api_key: str, model: str, upstream: SageMakerUpstream) -> None:
        self.api_key = api_key.encode("utf-8")
        self.upstream = upstream
        self.in_flight = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self.models_body = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "drip-ai",
                    }
                ],
            }
        ).encode()
        super().__init__((LISTEN_HOST, port), ProxyRequestHandler)
        bound_port = self.server_address[1]
        self.allowed_hosts = frozenset(
            {"localhost", LISTEN_HOST, f"localhost:{bound_port}", f"{LISTEN_HOST}:{bound_port}"}
        )

    def handle_error(self, request: object, client_address: object) -> None:
        # The default prints a traceback; keep stderr to metadata.
        log(f"connection error {type(sys.exc_info()[1]).__name__}")


# Command line


class OneLineArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: error: {message}\n")


def _region(value: str) -> str:
    if not REGION_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not an AWS region: {value!r}")
    return value


def _endpoint_name(value: str) -> str:
    if not ENDPOINT_NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a SageMaker endpoint name: {value!r}")
    return value


def _model_name(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("model name is empty")
    return value


def _port(value: str) -> int:
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError(f"not a TCP port: {value!r}")
    return int(value)


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = OneLineArgumentParser(
        description="Serve the vLLM API on 127.0.0.1 and forward it to a SageMaker endpoint. "
        "Reads the client API key from line 1 of stdin.",
    )
    parser.add_argument("--region", required=True, type=_region, help="AWS region of the endpoint")
    parser.add_argument(
        "--endpoint", required=True, type=_endpoint_name, help="SageMaker endpoint name"
    )
    parser.add_argument(
        "--model", required=True, type=_model_name, help="model id that GET /v1/models lists"
    )
    parser.add_argument("--port", required=True, type=_port, help="local port on 127.0.0.1")
    return parser.parse_args(argv)


def read_api_key(stdin: TextIO) -> str:
    key = stdin.readline().strip()
    if not key:
        raise StartupError("the API key must arrive as line 1 on stdin")
    return key


def aws_cli_version(version_output: str) -> Optional[tuple[int, int]]:
    match = re.search(r"aws-cli/(\d+)\.(\d+)", version_output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def check_aws_cli() -> None:
    required = "AWS CLI v{}.{} or newer is required".format(*AWS_CLI_MIN_VERSION)
    if shutil.which("aws") is None:
        raise StartupError(f"{required}; `aws` is not on PATH")
    try:
        completed = subprocess.run(
            ("aws", "--version"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=AWS_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StartupError(f"{required}; `aws --version` did not finish") from None
    version = aws_cli_version(completed.stdout + completed.stderr)
    if version is None or version < AWS_CLI_MIN_VERSION:
        found = "an unrecognized version" if version is None else "{}.{}".format(*version)
        raise StartupError(f"{required}; found {found}")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        api_key = read_api_key(sys.stdin)
        check_aws_cli()
    except StartupError as error:
        print(f"sagemaker-proxy: {error}", file=sys.stderr)
        return 1
    upstream = SageMakerUpstream(args.region, args.endpoint, CredentialCache())
    try:
        server = SageMakerProxyServer(args.port, api_key, args.model, upstream)
    except OSError as error:
        print(
            f"sagemaker-proxy: cannot listen on {LISTEN_HOST}:{args.port}: {error.strerror}",
            file=sys.stderr,
        )
        return 1
    listen_url = f"http://{LISTEN_HOST}:{args.port}"
    log(f"sagemaker-proxy: {listen_url} -> endpoint {args.endpoint} in {args.region}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("sagemaker-proxy: stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
