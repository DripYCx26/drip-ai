"""Pins shared/drip_ai/conventions.py to the Terraform conventions module it mirrors, and the name
rule every API-created resource passes."""

import re
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drip_ai import conventions  # noqa: E402

MODULE = Path(__file__).resolve().parents[1] / "modules" / "conventions"


class ConventionsTest(unittest.TestCase):
    def test_the_region_suffix_rule_matches_the_terraform_module(self):
        outputs = (MODULE / "outputs.tf").read_text()
        rule = 'var.region == "us-east-1" ? "" : "-${var.region}"'
        self.assertIn(f'output "global_name_suffix" {{\n  value = {rule}\n}}', outputs)
        self.assertIn(f'output "bucket_name" {{\n  value = "${{var.project}}-models-${{local.account_id}}${{{rule}}}"',
                      outputs)
        self.assertEqual(conventions.global_name_suffix("us-east-1"), "")
        self.assertEqual(conventions.global_name_suffix("us-east-2"), "-us-east-2")
        self.assertEqual(conventions.models_bucket("111122223333", "us-east-2"), "drip-ai-models-111122223333-us-east-2")
        self.assertEqual(conventions.models_bucket("111122223333", "us-east-1"), "drip-ai-models-111122223333")
        self.assertEqual(conventions.execution_role_name("eu-west-1"), "drip-ai-sagemaker-execution-eu-west-1")
        roles = (Path(__file__).resolve().parents[2] / "sagemaker" / "terraform" / "main.tf").read_text()
        self.assertIn('name = "${var.project}-sagemaker-execution${module.conventions.global_name_suffix}"', roles)

    def test_the_image_pins_come_from_the_terraform_module(self):
        text = (MODULE / "main.tf").read_text()
        pins = conventions.image_pins()
        self.assertIn(f'sagemaker_image_digest     = "{pins.sagemaker_digest}"', text)
        self.assertRegex(pins.sagemaker_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(pins.sagemaker_image("us-east-2"),
                         f"763104351884.dkr.ecr.us-east-2.amazonaws.com/vllm@{pins.sagemaker_digest}")
        self.assertRegex(pins.vllm_digest, r"^sha256:[0-9a-f]{64}$")

    def test_every_created_name_starts_with_drip_ai_and_fits_63_characters(self):
        for good in ("drip-ai-h200-us-east-2-1", "drip-ai-m-0a1b2c3d", "drip-ai-c-0a1b2c3d-12"):
            self.assertEqual(conventions.check_name(good), good)
        for bad in ("dripai-x", "drip-ai-", "drip-ai-Upper", "drip-ai--double", "drip-ai-" + "a" * 56, "other-1"):
            with self.subTest(bad), self.assertRaises(conventions.BadName):
                conventions.check_name(bad)
        self.assertEqual(conventions.log_group("drip-ai-h200-us-east-1-1"),
                         "/aws/sagemaker/Endpoints/drip-ai-h200-us-east-1-1")
        self.assertTrue(re.fullmatch(r"arn:aws:logs:us-east-2:111122223333:log-group:/aws/sagemaker/Endpoints/drip-ai-.+",
                                     conventions.log_group_arn("111122223333", "us-east-2", "drip-ai-x-1")))


if __name__ == "__main__":
    unittest.main()
