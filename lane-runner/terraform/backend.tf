# The bucket and region come from the caller at init (-backend-config).
terraform {
  backend "s3" {
    key          = "lane-runner.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}
