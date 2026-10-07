# Names and image pins every stack shares. No resources: each root reads these
# so a name or a pin is defined once, and no root reads another root's state.

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  api_key_parameter  = "/${var.project}/api-key"
  hf_token_parameter = "/${var.project}/hf-token"

  # AWS's vLLM SageMaker image. The endpoint and the model-fetch task both run
  # this digest; the tag is resolved only to prove it still points here. Change
  # the pin only with a dated docs/DECISIONS.md entry.
  # When the pin changes, rerun shared/model-profiles/regenerate_architectures.py with the new vLLM tag.
  sagemaker_image_registry   = "763104351884"
  sagemaker_image_repository = "vllm"
  sagemaker_image_tag        = "server-sagemaker-cuda-v2.6.1"
  sagemaker_image_digest     = "sha256:64fa565e864edb53c376b754bd3813935a78298edc1b9ed940d052bda7c63c4f"

  # EC2 path vLLM OpenAI-compatible server image, pinned by tag and
  # manifest-list digest. Docker pulls by the digest; the tag is for humans.
  # Change only with a dated docs/DECISIONS.md entry.
  vllm_image = "vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90"
}
