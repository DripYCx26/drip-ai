"""Names and image pins shared with Terraform's shared/modules/conventions, defined once there.

The image pins are read from the module's main.tf at run time, so a pin change in Terraform reaches
the API-created SageMaker models with no second copy. The name rules below mirror the module's
outputs; shared/tests/test_conventions.py fails if either side changes alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from drip_ai import REPO_ROOT

PROJECT = "drip-ai"
CONVENTIONS_TF = REPO_ROOT / "shared" / "modules" / "conventions" / "main.tf"
HOME_REGION_WITHOUT_SUFFIX = "us-east-1"
REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")
ACCOUNT = re.compile(r"[0-9]{12}")
# SageMaker names: letters, digits, and inner hyphens, at most 63. Every name drip-ai creates also
# starts with drip-ai-, which the execution role's log permission and every cleanup check rely on.
NAME = re.compile(r"drip-ai-[a-z0-9]+(-[a-z0-9]+)*")
NAME_MAX = 63
LOG_GROUP_PREFIX = "/aws/sagemaker/Endpoints/"
_PIN = re.compile(r'^\s*(sagemaker_image_registry|sagemaker_image_repository|sagemaker_image_tag|'
                  r'sagemaker_image_digest|vllm_image)\s*=\s*"([^"]+)"\s*$', re.M)


class BadName(ValueError):
    """A name drip-ai would create breaks the drip-ai- prefix or the 63-character rule."""


def check_name(name: str) -> str:
    if len(name) > NAME_MAX or not NAME.fullmatch(name):
        raise BadName(f"{name!r} is not a drip-ai- name of at most {NAME_MAX} lowercase letters, digits,"
                      " and inner hyphens")
    return name


def check_region(region: str) -> str:
    if not REGION.fullmatch(region or ""):
        raise ValueError(f"{region!r} is not an AWS region name")
    return region


def global_name_suffix(region: str) -> str:
    """IAM and S3 names are global, so every region but us-east-1 appends itself."""
    return "" if check_region(region) == HOME_REGION_WITHOUT_SUFFIX else f"-{region}"


def models_bucket(account: str, region: str) -> str:
    if not ACCOUNT.fullmatch(account or ""):
        raise ValueError("an AWS account id is 12 digits")
    return f"{PROJECT}-models-{account}{global_name_suffix(region)}"


def execution_role_name(region: str) -> str:
    return f"{PROJECT}-sagemaker-execution{global_name_suffix(region)}"


def execution_role_arn(account: str, region: str, partition: str = "aws") -> str:
    return f"arn:{partition}:iam::{account}:role/{execution_role_name(region)}"


def log_group(endpoint: str) -> str:
    return LOG_GROUP_PREFIX + check_name(endpoint)


def log_group_arn(account: str, region: str, endpoint: str, partition: str = "aws") -> str:
    return f"arn:{partition}:logs:{region}:{account}:log-group:{log_group(endpoint)}"


@dataclass(frozen=True)
class ImagePins:
    sagemaker_registry: str
    sagemaker_repository: str
    sagemaker_tag: str
    sagemaker_digest: str
    vllm_image: str

    def sagemaker_image(self, region: str) -> str:
        return (f"{self.sagemaker_registry}.dkr.ecr.{check_region(region)}.amazonaws.com/"
                f"{self.sagemaker_repository}@{self.sagemaker_digest}")

    @property
    def vllm_digest(self) -> str:
        return self.vllm_image.rpartition("@")[2]


def image_pins() -> ImagePins:
    """The pins in shared/modules/conventions/main.tf; raises when one is missing."""
    found = dict(_PIN.findall(CONVENTIONS_TF.read_text()))
    try:
        return ImagePins(found["sagemaker_image_registry"], found["sagemaker_image_repository"],
                         found["sagemaker_image_tag"], found["sagemaker_image_digest"], found["vllm_image"])
    except KeyError as missing:
        raise ValueError(f"shared/modules/conventions/main.tf has no {missing.args[0]} pin") from None
