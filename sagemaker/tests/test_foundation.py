"""Pins the boundary between Terraform and the race: the sagemaker root holds only the per-region
foundation, and every per-model resource is the race's."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "terraform"
sys.path.append(str(ROOT.parent))
import race  # noqa: E402  (sagemaker/race.py, which appends shared/ for drip_ai)
PER_RUN = ("aws_sagemaker_model", "aws_sagemaker_endpoint_configuration", "aws_sagemaker_endpoint",
           "aws_cloudwatch_log_group")


class FoundationTest(unittest.TestCase):
    def test_the_sagemaker_root_declares_no_model_configuration_endpoint_or_log_group(self):
        declared = set()
        for path in ROOT.glob("*.tf"):
            declared |= set(re.findall(r'^resource\s+"([a-z0-9_]+)"', path.read_text(), re.M))
        self.assertEqual(declared, {"aws_iam_role", "aws_iam_role_policy"})
        self.assertFalse(declared & set(PER_RUN))

    def test_the_root_takes_no_model_or_tuning_variable(self):
        names = set(re.findall(r'^variable\s+"([a-z0-9_]+)"', (ROOT / "variables.tf").read_text(), re.M))
        self.assertEqual(names, {"project", "region"})


class LogGrantTest(unittest.TestCase):
    """The execution role writes logs only under /aws/sagemaker/Endpoints/drip-ai-*, so every name
    the race creates must stay under it."""

    @staticmethod
    def plan(name="drip-ai-h200-us-east-2-1", model="drip-ai-m-0a1b2c3d", config="drip-ai-c-0a1b2c3d-1",
             log_group=None):
        return {"lanes": [{"name": name, "model": {"name": model}, "config": {"name": config},
                           "log_group": log_group or f"/aws/sagemaker/Endpoints/{name}"}]}

    def test_the_grant_is_the_project_prefix_and_the_project_is_pinned_to_drip_ai(self):
        main = (ROOT / "main.tf").read_text()
        self.assertRegex(main, r'log-group:/aws/sagemaker/Endpoints/\$\{var\.project\}-\*"')
        self.assertIn('condition     = var.project == "drip-ai"', (ROOT / "variables.tf").read_text())
        self.assertEqual(race.conventions.PROJECT, "drip-ai")

    def test_the_race_refuses_a_name_the_grant_would_not_cover(self):
        race.check_names(self.plan())
        cases = {"another prefix": self.plan(name="other-h200-us-east-2-1"),
                 "64 characters": self.plan(config="drip-ai-c-" + "a" * 54),
                 "a model outside the prefix": self.plan(model="model-0a1b2c3d"),
                 "another endpoint's log group": self.plan(log_group="/aws/sagemaker/Endpoints/drip-ai-x-1")}
        for label, plan in cases.items():
            with self.subTest(label), self.assertRaisesRegex(SystemExit, "nothing was created"):
                race.check_names(plan)


if __name__ == "__main__":
    unittest.main()
