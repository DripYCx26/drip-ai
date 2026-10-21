"""Pins shared/drip_ai/awscli.py: the one error classifier, the operation allowlist and its
read-only guard, deadlines, and the base principal a resumed race compares."""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drip_ai import awscli  # noqa: E402

ENDPOINT_NOT_FOUND = ('An error occurred (ValidationException) when calling the DescribeEndpoint operation: Could not'
                      ' find endpoint "arn:aws:sagemaker:us-east-2:111122223333:endpoint/drip-ai-x".')
NOT_FOUND = {
    ENDPOINT_NOT_FOUND: "sagemaker describe-endpoint",
    "An error occurred (ValidationException) when calling the DescribeModel operation: Could not find model \"x\".":
        "sagemaker describe-model",
    "An error occurred (ValidationException) when calling the DescribeEndpointConfig operation: Could not find"
    ' endpoint configuration "drip-ai-c-1".': "sagemaker describe-endpoint-config",
    "An error occurred (NoSuchEntity) when calling the GetRole operation: The role with name x cannot be found.":
        "iam get-role",
}
SAMPLES = {
    # Seen in a race log 63 times, from `aws login` credentials.
    "aws: [ERROR]: Your session has expired. Please reauthenticate using 'aws login'.": "credentials",
    "Your session has expired. Please reauthenticate using aws login.": "credentials",
    "aws: [ERROR]: An error occurred (ExpiredTokenException) when calling the DescribeEndpoint operation:"
    " The security token included in the request is expired": "credentials",
    "Unable to locate credentials. You can configure credentials by running \"aws configure\".": "credentials",
    "Error loading SSO Token: Token for alice does not exist": "credentials",
    "An error occurred (UnrecognizedClientException) when calling the ListEndpoints operation: The security token"
    " included in the request is invalid.": "credentials",
    "An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation": "credentials",
    "Unable to provision requested ML compute capacity due to InsufficientInstanceCapacity error. Please retry using a"
    " different ML instance type or after some time.": "capacity",
    "An error occurred (ResourceLimitExceeded) when calling the CreateEndpoint operation: The account-level service"
    " limit 'ml.p5en.48xlarge for endpoint usage' is 2 Instances": "quota",
    "An error occurred (ThrottlingException) when calling the DescribeEndpoint operation (reached max retries: 4):"
    " Rate exceeded": "throttle",
    'An error occurred (ValidationException) when calling the DeleteEndpoint operation: Cannot update in-progress'
    ' endpoint "arn:aws:sagemaker:us-east-1:111122223333:endpoint/drip-ai-x".': "busy",
    'An error occurred (ValidationException) when calling the CreateEndpoint operation: Cannot create already existing'
    ' endpoint "arn:aws:sagemaker:us-east-1:111122223333:endpoint/drip-ai-x".': "conflict",
    'Read timeout on endpoint URL: "https://api.sagemaker.us-east-1.amazonaws.com/"': "transient",
    "Could not connect to the endpoint URL: \"https://api.sagemaker.us-east-2.amazonaws.com/\"": "transient",
    "An error occurred (InternalFailure) when calling the CreateModel operation (reached max retries: 2)": "transient",
    "An error occurred (AccessDeniedException) when calling the CreateEndpoint operation: User: x is not authorized"
    " to perform: sagemaker:CreateEndpoint": "denied",
    "An error occurred (ValidationException) when calling the CreateEndpointConfig operation: 1 validation error"
    " detected": "invalid",
    "An error occurred (ResourceNotFound) when calling the DescribeSomething operation: nope": "unknown",
    "": "unknown",
}


class ClassifierTest(unittest.TestCase):
    def test_every_sample_maps_to_its_class(self):
        for text, expected in SAMPLES.items():
            with self.subTest(text[:60]):
                self.assertEqual(awscli.classify(text, "sagemaker describe-endpoint"), expected)
        for text, operation in NOT_FOUND.items():
            with self.subTest(text[:60]):
                self.assertEqual(awscli.classify(text, operation), "absent")

    def test_only_a_not_found_message_is_absent(self):
        for text in SAMPLES:
            with self.subTest(text[:60]):
                self.assertNotEqual(awscli.classify(text, "sagemaker describe-endpoint"), "absent")

    def test_only_could_not_find_endpoint_makes_an_endpoint_absent(self):
        for text, operation in NOT_FOUND.items():
            with self.subTest(text[:60]):
                self.assertEqual(awscli.classify(text, "sagemaker describe-endpoint") == "absent",
                                 text == ENDPOINT_NOT_FOUND)
                self.assertNotEqual(awscli.classify(text), "absent")  # no operation, never absent
        self.assertEqual(awscli.classify(ENDPOINT_NOT_FOUND, "sagemaker describe-endpoint-config"), "invalid")


class ShellTest(unittest.TestCase):
    def test_no_shell_script_greps_a_piped_aws_call(self):
        # Under pipefail a pipeline's status is its last failing command's, so `aws ... | grep -q`
        # tests the aws exit code, not the match; one kept a waiter polling forever. Read stderr
        # from a file and grep that, as sagemaker/endpoint.sh does.
        root = Path(__file__).resolve().parents[2]
        scripts = [root / "bin" / "drip-ai", root / "drip-ai.command",
                   *[p for p in root.glob("**/*.sh") if ".terraform" not in p.parts],
                   *root.glob("**/*.tftpl")]
        aws_pipe = re.compile(r"(^|[\s;(&|`]|\$\()aws\s[^#\n]*?\|&?\s*[ef]?grep\b")
        found = []
        for path in scripts:
            joined = path.read_text().replace("\\\n", " ")
            found += [f"{path.relative_to(root)}: {line.strip()}" for line in joined.splitlines()
                      if not line.lstrip().startswith("#") and aws_pipe.search(line)]
        self.assertEqual(found, [])


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        fake = self.bin / "aws"
        fake.write_text('#!/bin/sh\ncase "$*" in *slow*) exec sleep 5 ;; esac\necho "{}"\n')
        fake.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_only_refuses_every_mutating_operation_and_allows_reads(self):
        with awscli.read_only():
            with self.assertRaises(awscli.MutationRefused):
                awscli.call("sagemaker", "create-endpoint", "--endpoint-name", "drip-ai-x-1", region="us-east-1")
            with self.assertRaises(awscli.MutationRefused):
                awscli.call("ec2", "purchase-capacity-block", region="us-east-1")
            self.assertTrue(awscli.call("sagemaker", "describe-endpoint", region="us-east-1").ok)
        self.assertTrue(awscli.call("sagemaker", "create-endpoint", region="us-east-1").ok)

    def test_an_operation_off_the_allowlist_is_refused_before_it_runs(self):
        with self.assertRaisesRegex(ValueError, "not on drip-ai's operation allowlist"):
            awscli.call("iam", "delete-role", region="us-east-1")

    def test_a_call_past_its_deadline_is_transient(self):
        result = awscli.call("sts", "get-caller-identity", "slow", timeout=0.5)
        self.assertEqual((result.ok, result.error_class), (False, "transient"))
        self.assertIn("gave no answer", result.stderr)

    def test_the_base_principal_drops_only_a_session_name(self):
        self.assertEqual(awscli.base_principal("arn:aws:sts::111122223333:assumed-role/Dev/alice-1"),
                         "arn:aws:sts::111122223333:assumed-role/Dev")
        self.assertEqual(awscli.base_principal("arn:aws:iam::111122223333:user/alice"),
                         "arn:aws:iam::111122223333:user/alice")


if __name__ == "__main__":
    unittest.main()
