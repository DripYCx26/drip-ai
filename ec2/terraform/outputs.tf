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
