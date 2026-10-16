# A long-lived CPU runner for the build-lane container. The image owns queue
# claiming, worktrees, adapter builds, and its existing verification gates.
# This root only supplies the host, ranked queue read, and endpoint settings.

data "aws_ssm_parameter" "runner_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  default = true
}

# The runner has no inbound listener. It needs outbound HTTPS for its model
# endpoint, queue reads, and the image registry; operators use SSM for access.
resource "aws_security_group" "runner" {
  name        = "${var.project}-lane-runner"
  description = "Egress only; lane runner access is through SSM"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "runner" {
  ami                         = data.aws_ssm_parameter.runner_ami.value
  instance_type               = var.runner_instance_type
  iam_instance_profile        = var.runner_instance_profile_arn
  subnet_id                   = var.runner_subnet_id == "" ? null : var.runner_subnet_id
  vpc_security_group_ids      = [aws_security_group.runner.id]
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required"
    # The trusted runner container reads the exact queue object through the
    # instance role, so it needs IMDSv2 credentials through its network stack.
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size = var.runner_root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region                          = var.region
    runner_image                    = var.runner_image
    ranked_queue_uri                = var.ranked_queue_uri
    lane_runner_enabled             = var.lane_runner_enabled
    lane_count                      = var.lane_count
    model_endpoint_url              = var.model_endpoint_url
    model_name                      = var.model_name
    model_endpoint_secret_parameter = var.model_endpoint_secret_parameter_arn
    git_push_secret_parameter       = var.git_push_secret_parameter_arn
  })

  lifecycle {
    ignore_changes = [ami]
  }

  tags = {
    Name = "${var.project}-lane-runner"
  }
}
