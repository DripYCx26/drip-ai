# bucket and region come from the caller's account at init (-backend-config).
terraform {
  backend "s3" {
    key          = "fetch-model.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}
