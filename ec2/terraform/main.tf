# One GPU EC2 instance running the pinned vLLM container on 127.0.0.1:8000.
# Clients reach it only through an SSM port-forward. Turning it off is
# `destroy` on this root.

module "conventions" {
  source = "../../shared/modules/conventions"

  project = var.project
  region  = var.region
}

# Owned by the shared stack; looked up by name so this root reads no other state.
data "aws_s3_bucket" "models" {
  bucket = module.conventions.bucket_name
}

data "aws_ssm_parameter" "gpu_ami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

locals {
  # The API key and the optional token are the only parameters the role may read.
  hf_token_parameter_arns = var.hf_token_parameter == "" ? [] : [
    "arn:${module.conventions.partition}:ssm:${var.region}:${module.conventions.account_id}:parameter${var.hf_token_parameter}",
  ]
  readable_parameter_arns = concat([module.conventions.api_key_parameter_arn], local.hf_token_parameter_arns)
}

resource "aws_iam_role" "server" {
  name = "${var.project}-model-server"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:${module.conventions.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "server" {
  name = "read-models-and-secrets"
  role = aws_iam_role.server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = data.aws_s3_bucket.models.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${data.aws_s3_bucket.models.arn}/models/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.readable_parameter_arns
      },
      # AmazonSSMManagedInstanceCore grants ssm:GetParameter on "*". Without this
      # Deny the role could read every SSM parameter in the account, not just the
      # ones named above. Deny wins over the managed Allow.
      {
        Effect      = "Deny"
        Action      = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        NotResource = local.readable_parameter_arns
      },
    ]
  })
}

resource "aws_iam_instance_profile" "server" {
  name = "${var.project}-model-server"
  role = aws_iam_role.server.name
}

# No ingress: clients reach the server only through an SSM port-forward tunnel.
resource "aws_security_group" "server" {
  name        = "${var.project}-model-server"
  description = "Egress only; access is through SSM"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "server" {
  ami                         = data.aws_ssm_parameter.gpu_ami.value
  instance_type               = var.instance_type
  iam_instance_profile        = aws_iam_instance_profile.server.name
  vpc_security_group_ids      = [aws_security_group.server.id]
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required"
    # Hop limit 1 keeps the bridge-networked vLLM container from reaching IMDS and
    # the instance role credentials. The model process never needs AWS access.
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region               = var.region
    bucket               = data.aws_s3_bucket.models.id
    api_key_parameter    = module.conventions.api_key_parameter
    hf_token_parameter   = var.hf_token_parameter
    vllm_image           = module.conventions.vllm_image
    model_source         = var.model_source
    model_from_s3        = var.model_from_s3
    served_model_name    = var.served_model_name
    tensor_parallel_size = var.tensor_parallel_size
    max_model_len        = var.max_model_len
    vllm_extra_args      = join(" ", var.vllm_extra_args)
  })

  # A new AMI release must not silently replace a running server.
  lifecycle {
    ignore_changes = [ami]
  }

  tags = {
    Name = "${var.project}-model-server"
  }
}
