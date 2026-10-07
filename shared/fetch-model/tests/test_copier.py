"""Tests for shared/fetch-model/copier.py. Hugging Face is a fake server on 127.0.0.1; S3 is in memory."""

import contextlib
import hashlib
import io
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
COPIER_PATH = REPO_ROOT / "shared" / "fetch-model" / "copier.py"


def load_copier():
    import importlib.util

    spec = importlib.util.spec_from_file_location("fetch_model", COPIER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


fetch = load_copier()

REPO = "acme/coder"
SHA = "0123456789abcdef0123456789abcdef01234567"
CANARY_TOKEN = "hf_canaryTOKEN7f3a9c"
PART = 1024  # patched part size so multipart runs on small fixtures
BIG = bytes(range(256)) * 10  # 2560 bytes: three parts
FILES = {
    "config.json": b'{"model_type":"coder"}',
    "model-00001.safetensors": BIG,
    "nested/tokenizer.json": b'{"vocab":{}}',
}
LFS = {"model-00001.safetensors"}


class FakeHub:
    """Hugging Face metadata and resolve routes, plus an optional second origin acting as the CDN."""

    def __init__(self, files=FILES, lfs=LFS):
        self.files = dict(files)
        self.lfs = set(lfs)
        self.served = {}  # path -> bytes served instead of the published content
        self.queued = {}  # path -> [(status, headers)] answered before the real content
        self.metadata_status = 200
        self.siblings = None  # raw siblings override for hostile metadata
        self.redirect = False
        self.stall = None  # Event: serve half of each body, then wait
        self.api = self._serve()
        self.cdn = self._serve()
        self.endpoint = self.base(self.api)

    def _serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), HubHandler)
        server.daemon_threads = True
        server.hub, server.requests = self, []
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        return server

    @staticmethod
    def base(server):
        return f"http://127.0.0.1:{server.server_address[1]}"

    def close(self):
        if self.stall:
            self.stall.set()
        for server in (self.api, self.cdn):
            server.shutdown()
            server.server_close()

    def sibling(self, path):
        body = self.files[path]
        entry = {"rfilename": path, "size": len(body)}
        entry["blobId"] = hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()
        if path in self.lfs:
            entry["lfs"] = {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
        return entry

    def metadata(self):
        siblings = self.siblings or [self.sibling(path) for path in self.files]
        return {"id": REPO, "sha": SHA, "gated": False, "siblings": siblings}

    def get_paths(self):
        return [path for path, _ in self.api.requests + self.cdn.requests if "/api/" not in path]


class HubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        hub = self.server.hub
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        url = urllib.parse.urlsplit(self.path)
        if url.path == f"/api/models/{REPO}/revision/main" and url.query == "blobs=true":
            if hub.metadata_status != 200:
                return self.reply(hub.metadata_status, b"denied")
            return self.reply(200, json.dumps(hub.metadata()).encode())
        match = re.fullmatch(rf"/{REPO}/resolve/{SHA}/(.+)|/cdn/(.+)", url.path)
        if not match:
            return self.reply(404, b"no route")
        path = urllib.parse.unquote(match.group(1) or match.group(2))
        queued = hub.queued.get(path)
        if queued:
            status, headers = queued.pop(0)
            return self.reply(status, b"busy", headers)
        if hub.redirect and match.group(1):
            location = f"{hub.base(hub.cdn)}/cdn/{urllib.parse.quote(path)}?X-Amz-Signature=sig"
            return self.reply(302, b"", {"Location": location})
        body = hub.served.get(path, hub.files[path])
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # copier hung up early
            if hub.stall:
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                hub.stall.wait()
                body = body[len(body) // 2 :]
            self.wfile.write(body)

    def reply(self, status, body, headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeS3Error(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    """The S3 calls the copier makes, with keyword names checked the way botocore checks them."""

    def __init__(self):
        self.objects = {}  # key -> (body, metadata)
        self.uploads = {}
        self.calls = []
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.mpu_started = threading.Event()
        self.aborted = threading.Event()

    def _call(self, operation, bucket, key):
        assert bucket == "weights"
        with self.lock:
            self.calls.append((operation, key))

    def writes(self):
        reads = {"list_objects_v2", "head_object", "get_object"}
        return [call for call in self.calls if call[0] not in reads]

    def _create_only(self, key, if_none_match):
        if if_none_match is not None:
            assert if_none_match == "*"
            if key in self.objects:
                raise FakeS3Error("PreconditionFailed", 412)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, Delimiter=None):
        self._call("list_objects_v2", Bucket, Prefix)
        assert Delimiter in (None, "/")
        contents = [
            {"Key": key, "Size": len(body)}
            for key, (body, _) in sorted(self.objects.items())
            if key.startswith(Prefix) and not (Delimiter and "/" in key[len(Prefix) :])
        ]
        return {"Contents": contents, "IsTruncated": False}

    def head_object(self, Bucket, Key):
        self._call("head_object", Bucket, Key)
        if Key not in self.objects:
            raise FakeS3Error("404", 404)
        body, metadata = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": dict(metadata)}

    def get_object(self, Bucket, Key):
        self._call("get_object", Bucket, Key)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def put_object(self, Bucket, Key, Body, Metadata=None, ContentType=None, IfNoneMatch=None):
        self._call("put_object", Bucket, Key)
        self._create_only(Key, IfNoneMatch)
        self.objects[Key] = (bytes(Body), dict(Metadata or {}))

    def create_multipart_upload(self, Bucket, Key, Metadata):
        self._call("create_multipart_upload", Bucket, Key)
        upload_id = f"upload-{next(self.ids)}"
        self.uploads[upload_id] = {"key": Key, "metadata": dict(Metadata), "parts": {}}
        self.mpu_started.set()
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self._call("upload_part", Bucket, Key)
        if UploadId not in self.uploads:
            raise FakeS3Error("NoSuchUpload", 404)
        self.uploads[UploadId]["parts"][PartNumber] = bytes(Body)
        return {"ETag": f'"etag-{PartNumber}"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch=None):
        self._call("complete_multipart_upload", Bucket, Key)
        upload = self.uploads[UploadId]
        self._create_only(Key, IfNoneMatch)
        numbers = [part["PartNumber"] for part in MultipartUpload["Parts"]]
        assert numbers == sorted(upload["parts"]), numbers
        body = b"".join(upload["parts"][number] for number in numbers)
        self.objects[Key] = (body, upload["metadata"])
        del self.uploads[UploadId]

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self._call("abort_multipart_upload", Bucket, Key)
        self.uploads.pop(UploadId, None)
        self.aborted.set()


class ProcessExit(BaseException):
    pass


class CopierTest(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.addCleanup(self.hub.close)
        self.s3 = FakeS3()
        self.waits = []
        for name, value in (("PART_SIZE", PART), ("READ_CHUNK_BYTES", 256)):
            patcher = mock.patch.object(fetch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(fetch, "wait_before_retry", self.record_wait)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record_wait(self, stop, seconds):
        self.waits.append(seconds)

    def run_main(self, *args, token=None, exit_process=None, conditional=True):
        environment = {"HF_ENDPOINT": self.hub.endpoint}
        if token is not None:
            environment["HF_TOKEN"] = token
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(stdout):
            with contextlib.redirect_stderr(stderr):
                code = fetch.main(
                    list(args),
                    make_client=lambda region, workers: (self.s3, conditional),
                    exit_process=exit_process or self.fail_exit,
                )
                self.token_left = os.environ.get("HF_TOKEN")
        return code, stdout.getvalue(), stderr.getvalue()

    def fail_exit(self, code):
        raise AssertionError(f"unexpected process exit {code}")

    def copy(self, *extra, **kwargs):
        args = ["copy", "--repo", REPO, "--revision", "main", "--name", "coder"]
        args += ["--bucket", "weights", "--region", "us-east-1", *extra]
        return self.run_main(*args, **kwargs)

    def lines(self, output, kind):
        prefix = kind + " "
        return [json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)]

    def stored(self, path):
        return self.s3.objects[f"models/coder/{path}"]

    def test_copies_selected_files_under_prefix_with_hash_metadata(self):
        code, out, _ = self.copy()
        self.assertEqual(code, 0, out)
        expected = {f"models/coder/{path}" for path in FILES} | {"models/coder/drip-ai-fetch.json"}
        self.assertEqual(set(self.s3.objects), expected)
        body, metadata = self.stored("model-00001.safetensors")
        self.assertEqual(body, BIG)
        self.assertEqual(metadata["sha256"], hashlib.sha256(BIG).hexdigest())
        self.assertEqual(metadata["revision"], SHA)
        self.assertEqual(self.s3.calls.count(("upload_part", "models/coder/model-00001.safetensors")), 3)
        self.assertEqual(self.lines(out, "DONE"), [{"files": 3, "bytes": sum(map(len, FILES.values()))}])
        self.assertEqual(self.s3.uploads, {})

    def test_sha256_mismatch_aborts_upload_and_exits_4(self):
        self.hub.served["model-00001.safetensors"] = bytes(len(BIG))
        code, out, _ = self.copy()
        self.assertEqual(code, 4)
        self.assertRegex(out, r"(?m)^ERROR 4 model-00001.safetensors: sha256 mismatch after 4 attempts$")
        key = "models/coder/model-00001.safetensors"
        self.assertNotIn(key, self.s3.objects)
        self.assertEqual(self.s3.calls.count(("abort_multipart_upload", key)), 4)
        self.assertNotIn(("complete_multipart_upload", key), self.s3.calls)
        self.assertEqual(self.s3.uploads, {})

    def test_size_mismatch_aborts_upload(self):
        self.hub.served["model-00001.safetensors"] = BIG + b"x"
        code, out, _ = self.copy()
        self.assertEqual(code, 4)
        self.assertIn("more bytes than the published size", out)
        key = "models/coder/model-00001.safetensors"
        self.assertNotIn(key, self.s3.objects)
        self.assertEqual(self.s3.calls.count(("abort_multipart_upload", key)), 4)
        self.hub.served["model-00001.safetensors"] = BIG[:-1]
        code, out, _ = self.copy()
        self.assertEqual(code, 4)
        self.assertIn("size mismatch", out)
        self.assertNotIn(key, self.s3.objects)

    def test_git_sha1_verifies_non_lfs_files(self):
        self.hub.served["config.json"] = b'{"model_type":"evil!"}'  # same length, other bytes
        code, out, _ = self.copy()
        self.assertEqual(code, 4)
        self.assertIn("ERROR 4 config.json: git sha1 mismatch", out)
        self.assertNotIn("models/coder/config.json", self.s3.objects)
        self.hub.served.clear()
        self.s3 = FakeS3()
        code, out, _ = self.copy()
        self.assertEqual(code, 0, out)
        body, metadata = self.stored("config.json")
        self.assertEqual(metadata["git-sha1"], self.hub.sibling("config.json")["blobId"])
        self.assertNotIn("sha256", metadata)

    def test_rerun_skips_verified_objects_without_http_get(self):
        self.assertEqual(self.copy()[0], 0)
        self.hub.api.requests.clear()
        code, out, _ = self.copy()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hub.get_paths(), [])
        self.assertEqual(len(self.lines(out, "FILE_SKIP")), 3)
        self.assertEqual(self.lines(out, "FILE_DONE"), [])

    def test_different_revision_status_object_exits_3_without_writes(self):
        status = {"repo": REPO, "revision": "f" * 40, "status": "complete"}
        self.s3.objects["models/coder/drip-ai-fetch.json"] = (json.dumps(status).encode(), {})
        code, out, _ = self.copy()
        self.assertEqual(code, 3)
        self.assertIn("ERROR 3 ", out)
        self.assertEqual(self.s3.writes(), [])
        self.assertEqual(self.hub.get_paths(), [])

    def test_unknown_object_in_prefix_exits_3(self):
        self.s3.objects["models/coder/stray.bin"] = (b"old", {})
        code, out, _ = self.copy()
        self.assertEqual(code, 3)
        self.assertIn("outside this revision", out)
        self.assertEqual(self.s3.writes(), [])
        self.s3.objects = {"models/coder/config.json": (FILES["config.json"], {})}
        code, out, _ = self.copy()
        self.assertEqual(code, 3, out)
        self.assertEqual(self.s3.writes(), [])

    def assert_refused_without_writes(self, name, expected):
        code, out, _ = self.copy("--name", name)
        self.assertEqual(code, 3, out)
        self.assertRegex(out, rf"(?m)^ERROR 3 .*{re.escape(expected)}.*--name")
        self.assertEqual(self.s3.writes(), [])
        self.assertEqual(self.hub.get_paths(), [])

    def test_nested_names_conflict_only_with_another_model(self):
        refusals = [
            *[(f"models/coder/{marker}", "coder/sub", "model coder is stored at models/coder/")
              for marker in ("config.json", "drip-ai-fetch.json", "README.md")],
            *[(f"models/coder/sub/{marker}", "coder", "model coder/sub is nested in models/coder/")
              for marker in ("config.json", "drip-ai-fetch.json")],
        ]
        for existing, name, expected in refusals:
            with self.subTest(existing=existing, name=name):
                self.s3 = FakeS3()
                self.s3.objects[existing] = (b"{}", {})
                self.assert_refused_without_writes(name, expected)
        self.s3 = FakeS3()
        self.s3.objects["models/coder/other/config.json"] = (b"{}", {})
        code, out, _ = self.copy("--name", "coder/sub")
        self.assertEqual(code, 0, out)
        self.assertIn("models/coder/sub/config.json", self.s3.objects)
        self.hub.files["sub/config.json"] = b'{"model_type":"vision"}'
        self.s3 = FakeS3()
        for run in ("first", "rerun"):
            with self.subTest(run=run):
                code, out, _ = self.copy()
                self.assertEqual(code, 0, out)
        self.assertEqual(len(self.lines(out, "FILE_SKIP")), 4)

    def test_name_rule_matches_common_sh(self):
        common = (REPO_ROOT / "shared" / "lib" / "common.sh").read_text()
        shell = re.search(r"^model_name_pattern='\^(.+)\$'$", common, re.M).group(1)
        self.assertEqual(fetch.NAME_PATTERN.pattern, shell)

    def test_head_fallback_treats_404_as_absent_and_403_as_missing_list_bucket(self):
        code, out, _ = self.copy(conditional=False)
        self.assertEqual(code, 0, out)
        stored = set(self.s3.objects) - {"models/coder/drip-ai-fetch.json"}
        self.assertEqual(stored, {f"models/coder/{path}" for path in FILES})
        self.s3 = FakeS3()

        def forbidden(Bucket, Key):
            raise FakeS3Error("403", 403)

        self.s3.head_object = forbidden
        code, out, _ = self.copy(conditional=False)
        self.assertEqual(code, 1, out)
        self.assertRegex(
            out, r"(?m)^ERROR 1 S3 403 on HeadObject .*task role lacks s3:ListBucket; "
            r"HeadObject on a missing key returns 403$"
        )
        self.assertNotIn("models/coder/config.json", self.s3.objects)

    def test_hostile_filenames_rejected_before_any_write(self):
        hostile = [
            "/etc/passwd",
            "../escape",
            "a/../../b",
            "a//b",
            "./a",
            "dir/",
            "nul\0byte",
            "esc\x1b[2J",
            "x" * 256,
            "drip-ai-fetch.json",
            "/".join(["d" * 200] * 6),
        ]
        for name in hostile + ["clash"]:
            with self.subTest(name=name):
                good = self.hub.sibling("config.json")
                bad = dict(good, rfilename=name)
                extra = [dict(good, rfilename="clash/inner")] if name == "clash" else []
                self.hub.siblings = [good, bad] + extra
                self.s3 = FakeS3()
                code, out, _ = self.copy()
                self.assertEqual(code, 2, out)
                self.assertEqual(self.s3.calls, [])
                self.assertEqual(self.hub.get_paths(), [])
                self.assertNotIn("\x1b", out)

    def test_file_count_size_and_total_caps(self):
        base = self.hub.sibling("config.json")
        self.hub.siblings = [dict(base, rfilename=f"f{index}.json") for index in range(5001)]
        code, out, _ = self.copy()
        self.assertEqual(code, 2)
        self.assertIn("5000", out)
        self.hub.siblings.pop()
        self.assertEqual(self.run_main("plan", "--repo", REPO)[0], 0)
        huge = dict(base, rfilename="huge.json", size=64 * 1024**3 + 1)
        self.hub.siblings = [huge]
        code, out, _ = self.copy()
        self.assertEqual(code, 2)
        self.assertIn("64 GiB", out)
        self.hub.siblings = [dict(huge, size=64 * 1024**3)]
        self.assertEqual(self.run_main("plan", "--repo", REPO)[0], 0)
        self.hub.siblings = [dict(base, rfilename=f"g{index}.json", size=10**9) for index in range(3)]
        code, out, _ = self.copy("--max-total-gb", "2")
        self.assertEqual(code, 2)
        self.assertIn("--max-total-gb", out)
        self.assertEqual(self.s3.calls, [])

    def test_token_not_sent_across_redirect_host(self):
        self.hub.redirect = True
        code, out, _ = self.copy(token=CANARY_TOKEN)
        self.assertEqual(code, 0, out)
        api_auth = {auth for _, auth in self.hub.api.requests}
        self.assertEqual(api_auth, {f"Bearer {CANARY_TOKEN}"})
        self.assertEqual(len(self.hub.cdn.requests), len(FILES))
        self.assertEqual({auth for _, auth in self.hub.cdn.requests}, {None})

    def test_canary_token_absent_from_logs(self):
        self.hub.redirect = True
        self.hub.queued["config.json"] = [(503, {"Retry-After": "1"})]
        self.hub.served["model-00001.safetensors"] = bytes(len(BIG))
        with mock.patch.object(fetch, "PROGRESS_SECONDS", 0):
            code, out, err = self.copy(token=CANARY_TOKEN)
        self.assertEqual(code, 4)
        self.assertIsNone(self.token_left)
        for text in (out, err, repr(self.s3.objects), repr(self.s3.calls)):
            self.assertNotIn(CANARY_TOKEN, text)
            self.assertNotIn("canary", text.lower())

    def test_none_placeholder_means_anonymous(self):
        for placeholder in ("none", ""):
            with self.subTest(placeholder=placeholder):
                self.hub.api.requests.clear()
                self.s3 = FakeS3()
                code, out, _ = self.copy(token=placeholder)
                self.assertEqual(code, 0, out)
                self.assertEqual({auth for _, auth in self.hub.api.requests}, {None})
                self.assertIsNone(self.token_left)

    def test_401_403_exit_5_with_gated_message(self):
        for status in (401, 403):
            with self.subTest(status=status, mode="copy"):
                self.hub.metadata_status = 200
                self.hub.queued["config.json"] = [(status, {})]
                self.s3 = FakeS3()
                code, out, _ = self.copy()
                self.assertEqual(code, 5)
                self.assertRegex(out, r"ERROR 5 .*gated or private.*accept the license.*token")
                self.assertNotIn("models/coder/config.json", self.s3.objects)
            with self.subTest(status=status, mode="plan"):
                self.hub.metadata_status = status
                code, out, err = self.run_main("plan", "--repo", REPO)
                self.assertEqual(code, 5)
                self.assertEqual(out, "")
                self.assertRegex(err, r"ERROR 5 .*gated or private")

    def test_429_retry_after_honored(self):
        self.hub.queued["config.json"] = [(429, {"Retry-After": "7"}), (429, {"Retry-After": "9999"})]
        self.hub.queued["nested/tokenizer.json"] = [(503, {"Retry-After": "3"})]
        code, out, _ = self.copy("--files", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.waits, [7, 300, 3])
        self.assertEqual(self.stored("config.json")[0], FILES["config.json"])

    def test_deadline_watchdog_exits_124(self):
        self.hub.stall = threading.Event()
        exited = []
        fired = threading.Event()

        def record_exit(code):
            exited.append(code)
            fired.set()

        result = []
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            worker = threading.Thread(
                target=lambda: result.append(
                    self.copy("--deadline-seconds", "1", exit_process=record_exit)
                )
            )
            worker.start()
            self.assertTrue(fired.wait(15))
            self.hub.stall.set()
            worker.join(30)
        self.assertEqual(exited, [124])
        self.assertIn("ERROR 124 ", stdout.getvalue() + result[0][1])

    def test_sigterm_aborts_in_flight_upload_and_exits_143(self):
        self.hub.stall = threading.Event()

        def raise_exit(code):
            raise ProcessExit(code)

        aborted_while_stalled = []

        def terminate():
            self.s3.mpu_started.wait(15)
            os.kill(os.getpid(), signal.SIGTERM)
            aborted_while_stalled.append(self.s3.aborted.wait(10))
            self.hub.stall.set()

        previous = signal.getsignal(signal.SIGTERM)
        sender = threading.Thread(target=terminate)
        sender.start()
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(ProcessExit) as caught:
                    with mock.patch.dict(os.environ, {"HF_ENDPOINT": self.hub.endpoint}):
                        fetch.main(
                            ["copy", "--repo", REPO, "--revision", "main", "--name", "coder"]
                            + ["--bucket", "weights", "--region", "us-east-1"],
                            make_client=lambda region, workers: (self.s3, True),
                            exit_process=raise_exit,
                        )
        finally:
            sender.join()
        self.assertEqual(caught.exception.args, (143,))
        self.assertEqual(aborted_while_stalled, [True])  # the handler aborted, not worker cleanup
        self.assertIn("ERROR 143 ", stdout.getvalue())
        key = "models/coder/model-00001.safetensors"
        self.assertIn(("abort_multipart_upload", key), self.s3.calls)
        self.assertNotIn(key, self.s3.objects)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_default_excludes_only_with_safetensors(self):
        files = {
            "model.safetensors": b"s",
            "pytorch_model.bin": b"b",
            "original/consolidated.pth": b"p",
            "sub/weights.gguf": b"g",
            "config.json": b"c",
        }
        self.hub.files, self.hub.lfs = files, set()
        code, out, _ = self.run_main("plan", "--repo", REPO)
        self.assertEqual(code, 0)
        planned = [entry["path"] for entry in json.loads(out)["files"]]
        self.assertEqual(sorted(planned), ["config.json", "model.safetensors"])
        code, out, _ = self.run_main("plan", "--repo", REPO, "--all-files")
        self.assertEqual(len(json.loads(out)["files"]), 5)
        del files["model.safetensors"]
        code, out, _ = self.run_main("plan", "--repo", REPO)
        self.assertEqual(len(json.loads(out)["files"]), 4)

    def test_status_object_written_first_and_marked_complete_last(self):
        code, out, _ = self.copy()
        self.assertEqual(code, 0, out)
        status_key = "models/coder/drip-ai-fetch.json"
        writes = self.s3.writes()
        self.assertEqual(writes[0], ("put_object", status_key))
        self.assertEqual(writes[-1], ("put_object", status_key))
        self.assertEqual(writes.count(("put_object", status_key)), 2)
        status = json.loads(self.s3.objects[status_key][0])
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["revision"], SHA)
        self.assertEqual(status["repo"], REPO)
        self.assertEqual(sorted(entry["path"] for entry in status["files"]), sorted(FILES))
        self.assertEqual(status["total_bytes"], sum(map(len, FILES.values())))
        self.assertTrue(status["started_at"] <= status["completed_at"])

    def test_progress_lines_parseable_and_free_of_urls(self):
        self.hub.redirect = True
        with mock.patch.object(fetch, "PROGRESS_SECONDS", 0):
            code, out, _ = self.copy()
        self.assertEqual(code, 0, out)
        progress = self.lines(out, "PROGRESS")
        self.assertTrue(progress)
        for entry in progress:
            self.assertEqual(set(entry), {"path", "done", "size"})
            self.assertLessEqual(entry["done"], entry["size"])
        for line in out.splitlines():
            self.assertRegex(line, r"^(PLAN|PROGRESS|FILE_DONE|FILE_SKIP|DONE) \{")
            self.assertNotRegex(line, r"https?:|127\.0\.0\.1|Signature|resolve/|Bearer")

    def test_plan_mode_matches_copy_selection(self):
        self.hub.files["pytorch_model.bin"] = b"duplicate"
        code, out, _ = self.run_main("plan", "--repo", REPO)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        plan = json.loads(out)
        self.assertEqual(plan["revision_sha"], SHA)
        self.assertIs(plan["gated"], False)
        self.assertEqual({auth for _, auth in self.hub.api.requests}, {None})
        code, copy_out, _ = self.copy(token=CANARY_TOKEN)
        self.assertEqual(code, 0, copy_out)
        self.assertEqual(self.lines(copy_out, "PLAN"), [plan])
        copied = {key[len("models/coder/") :] for key in self.s3.objects} - {"drip-ai-fetch.json"}
        self.assertEqual(copied, {entry["path"] for entry in plan["files"]})
        self.assertNotIn("pytorch_model.bin", copied)

    def test_source_under_32k(self):
        self.assertLess(COPIER_PATH.stat().st_size, 32768)
        completed = subprocess.run(
            [sys.executable, "-I", "-c", COPIER_PATH.read_text(), "copy", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--deadline-seconds", completed.stdout)


if __name__ == "__main__":
    unittest.main()
