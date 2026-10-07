# bucket and region come from the caller's account at init (-backend-config).
terraform {
  backend "s3" {
    key          = "shared.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}
