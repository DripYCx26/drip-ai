output "instance_id" {
  value = aws_instance.runner.id
}

output "instance_type" {
  value = var.runner_instance_type
}

output "lane_count" {
  value = var.lane_count
}

output "ranked_queue_uri" {
  value = var.ranked_queue_uri
}

output "region" {
  value = var.region
}
