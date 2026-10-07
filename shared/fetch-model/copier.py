"""Copy one Hugging Face model revision into s3://<bucket>/models/<name>/, verifying every file.

`copy` runs inside the drip-ai ECS fetch task as `python3 -I -c <this source> copy ...`; `plan`
previews a repo anonymously. Stdlib only; botocore is imported in copy mode alone. The task
definition embeds this source, so it stays one file under 32 KiB.

Exit codes: 0 ok, 2 bad input or over a cap, 3 prefix conflict, 4 integrity or download failure
after retries, 5 Hugging Face refused access, 124 deadline, 143 SIGTERM, 1 S3 or other failure.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.parse
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

DEFAULT_ENDPOINT = "https://huggingface.co"
STATUS_NAME = "drip-ai-fetch.json"
SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
NAME_PATTERN = re.compile(rf"{SEGMENT}(/{SEGMENT})?")
REPO_PATTERN = re.compile(rf"{SEGMENT}/{SEGMENT}")
REVISION_PATTERN = re.compile(r"(?!.*\.\.)[A-Za-z0-9._/-]{1,128}")
REGION_PATTERN = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
TOKEN_PATTERN = re.compile(r"[\x21-\x7e]{1,1024}")
SHA1_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
DIGITS_PATTERN = re.compile(r"[0-9]{1,9}")
# Formats that duplicate a safetensors checkpoint; skipped only when the revision has one.
DUPLICATE_WEIGHT_PATTERNS = (
    "original/*", "*.bin", "*.pth", "*.pt", "*.gguf", "*.onnx", "*.h5", "*.msgpack", "*.ot"
)

MAX_FILES = 5000
MAX_FILE_BYTES = 64 * 1024**3
MAX_SEGMENT_BYTES = 255
MAX_KEY_BYTES = 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
GB = 1000**3
PART_SIZE = 64 * 1024 * 1024
MAX_PARTS = 9000
BUFFERED_PARTS = 24
READ_CHUNK_BYTES = 1024 * 1024
SOCKET_TIMEOUT_SECONDS = 60
ATTEMPTS = 4
MAX_RETRY_AFTER_SECONDS = 300
MAX_REDIRECTS = 5
PROGRESS_SECONDS = 30
POLL_SECONDS = 1.0  # bounds how long a SIGTERM waits for the main thread to wake
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
PRECONDITION_CODES = frozenset({"412", "PreconditionFailed"})
FORBIDDEN_CODES = frozenset({"403", "AccessDenied", "Forbidden"})
MODEL_MARKERS = (STATUS_NAME, "config.json")
GATED_MESSAGE = (
    "Hugging Face refused access: the repo is gated or private; accept the license on "
    "Hugging Face and supply a token"
)
MALFORMED_MESSAGE = "Hugging Face metadata has an unexpected shape"
TLS = ssl.create_default_context()
OUTPUT_LOCK = threading.RLock()  # reentrant: the SIGTERM handler may interrupt a print


class Fail(Exception):
    """Ends the run with an exit code; the message never holds a URL, header, or token."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class Stopped(Fail):
    """Another file failed first; this one stops quietly."""

    def __init__(self) -> None:
        super().__init__(1, "stopped")


class HttpError(Exception):
    def __init__(self, status: int, retry_after: Optional[int] = None) -> None:
        super().__init__(f"HTTP {status}" if status else "bad or too many redirects")
        self.status = status
        self.retry_after = retry_after
        self.retryable = status in (403, 408, 429) or status >= 500


class IntegrityError(Exception):
    """The bytes did not match the size or hash Hugging Face published."""


@dataclass(frozen=True)
class RepoFile:
    path: str
    size: int
    sha256: Optional[str]  # LFS files
    git_sha1: Optional[str]  # other files: sha1 of "blob <size>\0" + content


@dataclass(frozen=True)
class Revision:
    sha: str
    files: list[RepoFile]
    gated: bool


@dataclass(frozen=True)
class Endpoint:
    base: str
    origin: tuple[str, str, int]


def log_line(text: str, stream: Any = None) -> None:
    with OUTPUT_LOCK:
        print(text, file=stream or sys.stdout, flush=True)


def emit(kind: str, payload: dict[str, Any]) -> None:
    log_line(f"{kind} {json.dumps(payload, separators=(',', ':'))}")


def quoted(path: str) -> str:
    return json.dumps(path)[:120]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def url_origin(parts: urllib.parse.SplitResult) -> Optional[tuple[str, str, int]]:
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return ("https", host, port or 443)
    if parts.scheme == "http" and host == "127.0.0.1":  # the test server only
        return ("http", host, port or 80)
    return None


def parse_endpoint(value: str) -> Endpoint:
    parts = urllib.parse.urlsplit(value.rstrip("/"))
    origin = url_origin(parts)
    if origin is None or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        raise Fail(2, "HF_ENDPOINT must be https://host[:port]")
    return Endpoint(f"{parts.scheme}://{parts.netloc}", origin)


def read_token(raw: str) -> Optional[str]:
    if raw in ("", "none"):
        return None
    if not TOKEN_PATTERN.fullmatch(raw):
        raise Fail(2, "HF_TOKEN holds characters a token cannot have")
    return raw


def http_get(url: str, endpoint: Endpoint, token: Optional[str]) -> http.client.HTTPResponse:
    """GET following redirects by hand; the token goes only to the endpoint's exact origin."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        origin = url_origin(parts)
        if origin is None:
            raise HttpError(0)
        scheme, host, port = origin
        if scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=SOCKET_TIMEOUT_SECONDS, context=TLS
            )
        else:
            connection = http.client.HTTPConnection(host, port, timeout=SOCKET_TIMEOUT_SECONDS)
        headers = {"Accept-Encoding": "identity", "Connection": "close", "User-Agent": "drip-ai"}
        if token and origin == endpoint.origin:
            headers["Authorization"] = f"Bearer {token}"
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        if response.status == 200:
            return response  # Connection: close hands the socket to the response
        location = response.getheader("Location")
        retry_after = response.getheader("Retry-After") or ""
        response.close()
        connection.close()
        if response.status in REDIRECT_STATUSES and location:
            url = urllib.parse.urljoin(url, location)
            continue
        if response.status in (401, 403) and origin == endpoint.origin:
            raise Fail(5, GATED_MESSAGE)
        delay = int(retry_after) if DIGITS_PATTERN.fullmatch(retry_after) else None
        raise HttpError(response.status, delay)
    raise HttpError(0)


def wait_before_retry(stop: threading.Event, seconds: float) -> None:
    stop.wait(seconds)


def retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, HttpError) and error.status in (429, 503) and error.retry_after is not None:
        return min(error.retry_after, MAX_RETRY_AFTER_SECONDS)
    return 2.0**attempt


def s3_error_code(error: BaseException) -> Optional[str]:
    """The S3 error code of a botocore ClientError-shaped exception, else None."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict) or not isinstance(response.get("Error"), dict):
        return None
    return str(response["Error"].get("Code", "Unknown"))


def with_retries(action: Callable[[], Any], stop: threading.Event, label: str, refused: int) -> Any:
    """Run action up to ATTEMPTS times; S3 errors are not retried here (botocore already did)."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return action()
        except Fail:
            raise
        except Exception as error:
            code = s3_error_code(error)
            if code is not None:
                raise Fail(1, f"S3 {code[:64]} on {label}") from error
            if isinstance(error, HttpError) and not error.retryable:
                raise Fail(refused, f"{label}: {error}") from error
            if attempt == ATTEMPTS:
                reason = error if isinstance(error, (HttpError, IntegrityError)) else type(error).__name__
                raise Fail(4, f"{label}: {reason} after {ATTEMPTS} attempts") from error
            wait_before_retry(stop, retry_delay(error, attempt))
            if stop.is_set():
                raise Stopped()
    raise AssertionError("unreachable")


def parse_sibling(entry: Any) -> RepoFile:
    if not isinstance(entry, dict):
        raise Fail(4, MALFORMED_MESSAGE)
    path, size, lfs, blob = (entry.get(k) for k in ("rfilename", "size", "lfs", "blobId"))
    if not isinstance(path, str) or type(size) is not int or size < 0:
        raise Fail(4, MALFORMED_MESSAGE)
    if lfs is not None:
        digest = lfs.get("sha256") if isinstance(lfs, dict) else None
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest) or lfs.get("size") != size:
            raise Fail(4, MALFORMED_MESSAGE)
        return RepoFile(path, size, digest, None)
    if not isinstance(blob, str) or not SHA1_PATTERN.fullmatch(blob):
        raise Fail(4, MALFORMED_MESSAGE)
    return RepoFile(path, size, None, blob)


def fetch_revision(
    endpoint: Endpoint, repo: str, revision: str, token: Optional[str], stop: threading.Event
) -> Revision:
    rev = urllib.parse.quote(revision, safe="")
    url = f"{endpoint.base}/api/models/{repo}/revision/{rev}?blobs=true"

    def read() -> bytes:
        with http_get(url, endpoint, token) as response:
            return response.read(MAX_JSON_BYTES + 1)

    body = with_retries(read, stop, "Hugging Face metadata", 2)
    try:
        data = json.loads(body) if len(body) <= MAX_JSON_BYTES else None
    except ValueError:
        data = None
    sha = data.get("sha") if isinstance(data, dict) else None
    siblings = data.get("siblings") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not SHA1_PATTERN.fullmatch(sha) or not isinstance(siblings, list):
        raise Fail(4, MALFORMED_MESSAGE)
    files = [parse_sibling(entry) for entry in siblings]
    return Revision(sha, files, data.get("gated") not in (None, False))


def check_names(files: list[RepoFile], prefix: str) -> None:
    """Reject names that could escape the prefix, break S3 keys, or clash as file and directory."""
    paths = {f.path for f in files}
    if len(paths) != len(files):
        raise Fail(2, "the repo lists a file twice")
    for path in paths:
        segments = path.split("/")
        if (
            not path.isprintable()
            or path == STATUS_NAME
            or len((prefix + path).encode()) >= MAX_KEY_BYTES
            or any(s in ("", ".", "..") or len(s.encode()) > MAX_SEGMENT_BYTES for s in segments)
        ):
            raise Fail(2, f"unsafe file name in the repo: {quoted(path)}")
        for depth in range(1, len(segments)):
            if "/".join(segments[:depth]) in paths:
                raise Fail(2, f"a file and a directory share a name: {quoted(path)}")


def select_files(files: list[RepoFile], all_files: bool) -> list[RepoFile]:
    if all_files or not any(f.path.endswith(".safetensors") for f in files):
        return list(files)
    return [
        f for f in files if not any(fnmatch.fnmatchcase(f.path, p) for p in DUPLICATE_WEIGHT_PATTERNS)
    ]


def check_caps(files: list[RepoFile], max_total_bytes: Optional[int]) -> None:
    if len(files) > MAX_FILES:
        raise Fail(2, f"{len(files)} files exceed the cap of {MAX_FILES}")
    for f in files:
        if f.size > MAX_FILE_BYTES:
            raise Fail(2, f"{quoted(f.path)} is larger than 64 GiB")
    total = sum(f.size for f in files)
    if max_total_bytes is not None and total > max_total_bytes:
        raise Fail(2, f"{total} bytes exceed --max-total-gb")


def resolve_plan(
    args: argparse.Namespace,
    endpoint: Endpoint,
    token: Optional[str],
    stop: threading.Event,
    prefix: str,
    max_total_bytes: Optional[int],
) -> tuple[Revision, list[RepoFile], dict[str, Any]]:
    """The one selection path that plan and copy share."""
    revision = fetch_revision(endpoint, args.repo, args.revision, token, stop)
    check_names(revision.files, prefix)
    files = select_files(revision.files, args.all_files)
    check_caps(files, max_total_bytes)
    plan = {
        "repo": args.repo,
        "revision_sha": revision.sha,
        "files": [{"path": f.path, "size": f.size} for f in files],
        "total_bytes": sum(f.size for f in files),
        "gated": revision.gated,
    }
    return revision, files, plan


def abort_upload(s3: Any, bucket: str, key: str, upload_id: str) -> None:
    try:
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    except Exception:  # best effort: the bucket lifecycle rule aborts leftovers after one day
        pass


class VerifyingReader:
    """Reads one download in chunks: hashes it, refuses bytes past the published size, logs progress."""

    def __init__(self, response: http.client.HTTPResponse, f: RepoFile, stop: threading.Event):
        self.response, self.file, self.stop = response, f, stop
        self.hasher = hashlib.sha256() if f.sha256 else hashlib.sha1(b"blob %d\0" % f.size)
        self.done = 0
        self.last_progress = time.monotonic()

    def read(self, limit: int) -> bytes:
        chunks = []
        remaining = limit
        while remaining > 0:
            if self.stop.is_set():
                raise Stopped()
            chunk = self.response.read(min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            self.done += len(chunk)
            if self.done > self.file.size:
                raise IntegrityError("more bytes than the published size")
            self.hasher.update(chunk)
            chunks.append(chunk)
            now = time.monotonic()
            if now - self.last_progress >= PROGRESS_SECONDS:
                self.last_progress = now
                emit("PROGRESS", {"path": self.file.path, "done": self.done, "size": self.file.size})
        return b"".join(chunks)

    def verify(self) -> None:
        if self.done != self.file.size:
            raise IntegrityError("size mismatch")
        if self.hasher.hexdigest() != (self.file.sha256 or self.file.git_sha1):
            raise IntegrityError("sha256 mismatch" if self.file.sha256 else "git sha1 mismatch")


class Copier:
    """Copies the files of one revision into one prefix with create-only, verified writes."""

    def __init__(
        self, s3: Any, args: argparse.Namespace, endpoint: Endpoint, token: Optional[str],
        sha: str, conditional: bool, in_flight: dict[str, str], part_pool: ThreadPoolExecutor,
    ) -> None:
        self.s3, self.bucket, self.repo = s3, args.bucket, args.repo
        self.prefix = f"models/{args.name}/"
        self.endpoint, self.token, self.sha = endpoint, token, sha
        self.conditional = conditional
        self.in_flight = in_flight  # upload id -> key, read by the SIGTERM handler
        self.part_pool = part_pool
        self.permits = threading.BoundedSemaphore(BUFFERED_PARTS)
        self.stop = threading.Event()

    def metadata(self, f: RepoFile) -> dict[str, str]:
        digest = {"sha256": f.sha256} if f.sha256 else {"git-sha1": str(f.git_sha1)}
        return {"revision": self.sha, **digest}

    def stored_state(self, f: RepoFile) -> Optional[bool]:
        """None when absent; whether the stored object has this file's size, hash, and revision."""
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=self.prefix + f.path)
        except Exception as error:
            code = s3_error_code(error)
            if code in NOT_FOUND_CODES:
                return None
            if code in FORBIDDEN_CODES:
                raise Fail(1, f"S3 403 on HeadObject {quoted(f.path)}: task role lacks "
                           "s3:ListBucket; HeadObject on a missing key returns 403") from error
            raise
        stored = head.get("Metadata") or {}
        return head.get("ContentLength") == f.size and all(
            stored.get(k) == v for k, v in self.metadata(f).items()
        )

    def copy_file(self, f: RepoFile, verified: bool) -> None:
        skipped = verified or not with_retries(lambda: self.copy_once(f), self.stop, f.path, 4)
        emit("FILE_SKIP" if skipped else "FILE_DONE", {"path": f.path, "size": f.size})

    def copy_once(self, f: RepoFile) -> bool:
        """Download, verify, and store one file; False when a matching object already exists."""
        url = f"{self.endpoint.base}/{self.repo}/resolve/{self.sha}/{urllib.parse.quote(f.path)}"
        part_size = max(PART_SIZE, -(-f.size // MAX_PARTS))
        key = self.prefix + f.path
        with http_get(url, self.endpoint, self.token) as response:
            reader = VerifyingReader(response, f, self.stop)
            if f.size >= part_size:
                return self.multipart(f, reader, part_size)
            with self.permits:
                body = reader.read(f.size + 1)
                reader.verify()
                return self.create_only(f, lambda condition: self.s3.put_object(
                    Bucket=self.bucket, Key=key, Body=body, Metadata=self.metadata(f), **condition
                ))

    def multipart(self, f: RepoFile, reader: VerifyingReader, part_size: int) -> bool:
        key = self.prefix + f.path
        upload = self.s3.create_multipart_upload(
            Bucket=self.bucket, Key=key, Metadata=self.metadata(f)
        )
        upload_id = upload["UploadId"]
        self.in_flight[upload_id] = key
        futures: list[Any] = []
        stored = False
        try:
            while True:
                self.permits.acquire()
                try:
                    body = reader.read(part_size)
                except BaseException:
                    self.permits.release()
                    raise
                if not body:
                    self.permits.release()
                    break
                number = len(futures) + 1
                futures.append(self.part_pool.submit(self.upload_part, key, upload_id, number, body))
                if len(body) < part_size:
                    break
            parts = [future.result() for future in futures]
            reader.verify()
            stored = self.create_only(f, lambda condition: self.s3.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts}, **condition,
            ))
            return stored
        finally:
            wait(futures)
            if not stored:
                abort_upload(self.s3, self.bucket, key, upload_id)
            self.in_flight.pop(upload_id, None)

    def upload_part(self, key: str, upload_id: str, number: int, body: bytes) -> dict[str, Any]:
        try:
            response = self.s3.upload_part(
                Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=body
            )
            return {"ETag": response["ETag"], "PartNumber": number}
        finally:
            self.permits.release()

    def create_only(self, f: RepoFile, write: Callable[[dict[str, str]], Any]) -> bool:
        """Write only if the key is absent; accept an existing object only when it matches."""
        if self.conditional:
            try:
                write({"IfNoneMatch": "*"})
                return True
            except Exception as error:
                if s3_error_code(error) not in PRECONDITION_CODES:
                    raise
            existing = self.stored_state(f)
        else:
            existing = self.stored_state(f)
            if existing is None:  # racy against a concurrent writer; see make_s3_client
                write({})
                return True
        if existing:
            return False
        raise Fail(3, f"{f.path}: another writer stored different content")


def list_prefix(s3: Any, bucket: str, prefix: str, **options: str) -> dict[str, int]:
    listed: dict[str, int] = {}
    page_token: dict[str, str] = {}
    while True:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, **options, **page_token)
        listed.update((item["Key"], item["Size"]) for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return listed
        page_token = {"ContinuationToken": page["NextContinuationToken"]}


def check_prefix(copier: Copier, files: list[RepoFile]) -> set[str]:
    """Exit 3 before any write unless the prefix holds only this revision's verified files and
    no model nests inside another (models/A/ holding files directly and also models/A/B/).

    Returns the paths already stored and verified."""
    s3, bucket, prefix = copier.s3, copier.bucket, copier.prefix
    name = prefix[len("models/"):-1]
    parent, _, child = name.partition("/")
    if child and list_prefix(s3, bucket, f"models/{parent}/", Delimiter="/"):
        raise Fail(3, f"model {parent} is stored at models/{parent}/ and {name} cannot nest "
                   f"inside it; choose another --name, e.g. {parent}-{child}")
    listed = list_prefix(s3, bucket, prefix)
    status_key = prefix + STATUS_NAME
    if listed.pop(status_key, None) is not None:
        body = s3.get_object(Bucket=bucket, Key=status_key)["Body"].read(MAX_JSON_BYTES)
        try:
            status = json.loads(body)
        except ValueError:
            status = None
        if not isinstance(status, dict) or (status.get("repo"), status.get("revision")) != (
            copier.repo, copier.sha
        ):
            raise Fail(3, f"{prefix} holds another repo or revision; choose another --name")
    wanted = {prefix + f.path: f for f in files}
    outside = [key for key in listed if key not in wanted]
    for key in [] if child else outside:  # a one-segment name: another model may sit below
        nested, _, base = key[len(prefix):].partition("/")
        if base in MODEL_MARKERS:
            raise Fail(3, f"model {name}/{nested} is nested in {prefix}; choose another --name")
    if outside:
        sample = quoted(outside[0][len(prefix):])
        raise Fail(3, f"{prefix} holds {len(outside)} objects outside this revision, e.g. {sample}")
    for key, size in listed.items():
        f = wanted[key]
        if size != f.size or not copier.stored_state(f):
            raise Fail(3, f"{quoted(f.path)} exists without this revision's size and hash")
    return {key[len(prefix):] for key in listed}


def write_status(copier: Copier, status: dict[str, Any]) -> None:
    copier.s3.put_object(
        Bucket=copier.bucket, Key=copier.prefix + STATUS_NAME,
        Body=json.dumps(status).encode(), ContentType="application/json",
    )


def copy_all(copier: Copier, files: list[RepoFile], verified: set[str], workers: int) -> None:
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(copier.copy_file, f, f.path in verified) for f in files]
        try:
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=POLL_SECONDS, return_when=FIRST_EXCEPTION)
                for future in done:
                    future.result()
        except BaseException:
            copier.stop.set()
            for future in futures:
                future.cancel()
            raise


@contextlib.contextmanager
def sigterm_aborts(
    s3: Any, bucket: str, in_flight: dict[str, str], exit_process: Callable[[int], Any]
) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle(signum: int, frame: Any) -> None:
        log_line("ERROR 143 stopped by SIGTERM; aborting in-flight uploads")
        for upload_id, key in list(in_flight.items()):
            abort_upload(s3, bucket, key, upload_id)
        exit_process(143)

    previous = signal.signal(signal.SIGTERM, handle)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def s3_config(config_class: Callable[..., Any], workers: int) -> Any:
    """Checksums only when S3 requires them: botocore 1.36+ would add CRCs to upload_part that
    create_multipart_upload never declared, and the copier verifies sha256 itself."""
    options: dict[str, Any] = dict(
        signature_version="s3v4",
        connect_timeout=SOCKET_TIMEOUT_SECONDS,
        read_timeout=SOCKET_TIMEOUT_SECONDS,
        max_pool_connections=workers * 3 + 2,
        retries={"mode": "standard", "max_attempts": 5},
    )
    try:
        return config_class(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            **options,
        )
    except TypeError:  # botocore older than 1.36 has neither option nor the default CRCs
        return config_class(**options)


def make_s3_client(region: str, workers: int) -> tuple[Any, bool]:
    """S3 client on the default credential chain, and whether it can send IfNoneMatch."""
    import botocore.session
    from botocore.config import Config

    config = s3_config(Config, workers)
    client = botocore.session.get_session().create_client("s3", region_name=region, config=config)
    model = client.meta.service_model
    conditional = all(
        "IfNoneMatch" in model.operation_model(op).input_shape.members
        for op in ("PutObject", "CompleteMultipartUpload")
    )
    if not conditional:
        log_line("WARN botocore lacks IfNoneMatch; checking with HEAD before each write")
    return client, conditional


def run_copy(
    args: argparse.Namespace, token: Optional[str], endpoint: Endpoint,
    make_client: Callable[[str, int], tuple[Any, bool]], exit_process: Callable[[int], Any],
) -> int:
    def expire() -> None:
        log_line(f"ERROR 124 deadline of {args.deadline_seconds} seconds reached")
        exit_process(124)

    watchdog = threading.Timer(args.deadline_seconds, expire)
    watchdog.daemon = True
    watchdog.start()
    stop = threading.Event()
    try:
        s3, conditional = make_client(args.region, args.files)
        prefix = f"models/{args.name}/"
        revision, files, plan = resolve_plan(
            args, endpoint, token, stop, prefix, args.max_total_gb * GB
        )
        emit("PLAN", plan)
        in_flight: dict[str, str] = {}
        with ThreadPoolExecutor(args.files * 2) as part_pool, sigterm_aborts(
            s3, args.bucket, in_flight, exit_process
        ):
            copier = Copier(s3, args, endpoint, token, revision.sha, conditional, in_flight, part_pool)
            verified = check_prefix(copier, files)
            status = {
                "repo": args.repo, "revision": revision.sha, "status": "copying",
                "files": plan["files"], "total_bytes": plan["total_bytes"],
                "started_at": utc_now(), "completed_at": None,
            }
            write_status(copier, status)
            copy_all(copier, files, verified, args.files)
            write_status(copier, dict(status, status="complete", completed_at=utc_now()))
        emit("DONE", {"files": len(files), "bytes": plan["total_bytes"]})
        return 0
    finally:
        stop.set()
        watchdog.cancel()


def matching(pattern: re.Pattern, what: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError(f"not a valid {what}")
        return value

    return check


def int_in(low: int, high: int) -> Callable[[str], int]:
    def check(value: str) -> int:
        if not DIGITS_PATTERN.fullmatch(value) or not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f"must be an integer from {low} to {high}")
        return int(value)

    return check


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fetch-model", description=(__doc__ or "").split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="print the selected files of a revision as JSON")
    copy = commands.add_parser("copy", help="copy a revision into S3, verified")
    for command in (plan, copy):
        command.add_argument("--repo", required=True, type=matching(REPO_PATTERN, "ORG/NAME"))
        command.add_argument(
            "--revision", type=matching(REVISION_PATTERN, "revision"),
            required=command is copy, default="main",
        )
        command.add_argument("--all-files", action="store_true", help="keep duplicate formats")
    copy.add_argument("--name", required=True, type=matching(NAME_PATTERN, "model name"))
    copy.add_argument("--bucket", required=True, type=matching(BUCKET_PATTERN, "bucket"))
    copy.add_argument("--region", required=True, type=matching(REGION_PATTERN, "region"))
    copy.add_argument("--files", type=int_in(1, 64), default=8, help="files copied at once")
    copy.add_argument("--deadline-seconds", type=int_in(1, 7 * 86400), default=21600)
    copy.add_argument("--max-total-gb", type=int_in(1, 100000), default=1200)
    return parser.parse_args(argv)


def main(
    argv: Optional[list[str]] = None,
    make_client: Callable[[str, int], tuple[Any, bool]] = make_s3_client,
    exit_process: Callable[[int], Any] = os._exit,
) -> int:
    raw_token = os.environ.pop("HF_TOKEN", "")
    args = parse_args(argv)
    stream = sys.stderr if args.command == "plan" else sys.stdout
    try:
        endpoint = parse_endpoint(os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT)
        if args.command == "plan":
            plan = resolve_plan(args, endpoint, None, threading.Event(), "models/", None)[2]
            log_line(json.dumps(plan))
            return 0
        return run_copy(args, read_token(raw_token), endpoint, make_client, exit_process)
    except Fail as error:
        log_line(f"ERROR {error.code} {error}", stream)
        return error.code
    except Exception as error:
        log_line(f"ERROR 1 unexpected {type(error).__name__}", stream)
        return 1


if __name__ == "__main__":
    sys.exit(main())
