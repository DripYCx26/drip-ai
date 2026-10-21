output "instance_id" {
  value = aws_instance.server.id
}

output "served_model_name" {
  value = var.served_model_name
}

output "max_model_len" {
  value = var.max_model_len
}

output "region" {
  value = var.region
}

# bin/drip-ai ec2 reconfigure rebuilds the container argv from these and the new flags.
output "model_arg" {
  value = local.model_arg
}

output "tensor_parallel_size" {
  value = var.tensor_parallel_size
}
