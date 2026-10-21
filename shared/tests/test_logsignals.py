"""Pins the vLLM log reader (shared/drip_ai/logsignals.py): values, failure classes, and bounds."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drip_ai import logsignals  # noqa: E402

STARTED = """INFO 10-09 15:20:01 [api_server.py:1880] vLLM API server version 0.11.2
INFO 10-09 15:31:40 [gpu_worker.py:298] Available KV cache memory: 31.87 GiB
INFO 10-09 15:31:41 [kv_cache_utils.py:1087] GPU KV cache size: 4,129,536 tokens
"""


class ParseTest(unittest.TestCase):
    def test_a_started_server_shows_its_version_and_kv_pool(self):
        found = logsignals.parse(STARTED)
        self.assertEqual((found.vllm_version, found.kv_pool_tokens, found.available_kv_gib, found.failure),
                         ("0.11.2", 4129536, 31.87, None))

    def test_each_failure_is_classified_in_order(self):
        cases = {
            "vllm serve: error: unrecognized arguments: --async-scheduling": ("unknown_flag", "--async-scheduling"),
            "ValueError: To serve at least one request with the models's max seq len (1048576), (60.32 GiB KV cache is"
            " needed, which is larger than the available KV cache memory (30.00 GiB). Based on the available memory,"
            " the estimated maximum model length is 520000.": ("kv_too_small", None),
            "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB": ("oom", None),
            "Error downloading model files: connection reset": ("download", None),
            "The primary container for production variant main did not pass the ping health check.": ("health", None),
        }
        for text, (outcome, flag) in cases.items():
            with self.subTest(outcome):
                found = logsignals.parse(text)
                self.assertEqual((found.failure, found.unknown_flag), (outcome, flag))
        self.assertEqual(logsignals.parse(list(cases)[1]).estimated_max_model_len, 520000)

    def test_only_the_last_megabyte_is_read(self):
        text = "vLLM API server version 0.1.0\n" + "x" * (logsignals.BYTE_LIMIT + 10) + "\n"
        self.assertIsNone(logsignals.parse(text).vllm_version)


class HarvestTest(unittest.TestCase):
    def test_the_newest_stream_is_read_with_bounds(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        log = folder / "calls"
        events = {"events": [{"message": line} for line in STARTED.splitlines()]}
        streams = {"logStreams": [{"logStreamName": "main/i-0abc/AllTraffic"}]}
        fake = folder / "aws"
        fake.write_text(f"""#!/bin/sh
echo "$*" >> {log}
case "$2" in
  describe-log-streams) echo '{json.dumps(streams)}' ;;
  get-log-events) echo '{json.dumps(events)}' ;;
esac
""")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{folder}:{os.environ.get('PATH', '/usr/bin:/bin')}"}):
            text = logsignals.harvest("us-east-2", "/aws/sagemaker/Endpoints/drip-ai-h200-us-east-2-1")
        self.assertEqual(logsignals.parse(text).kv_pool_tokens, 4129536)
        calls = log.read_text().splitlines()
        self.assertIn("--order-by LastEventTime --descending --max-items 1", calls[0])
        self.assertIn(f"--limit {logsignals.EVENT_LIMIT} --no-start-from-head", calls[1])


if __name__ == "__main__":
    unittest.main()
