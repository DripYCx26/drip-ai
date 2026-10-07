# bootstrap

Terraform root that creates the S3 bucket holding every other root's state. It keeps its own state on local disk (`bootstrap/terraform.tfstate`, gitignored), because it creates the bucket the other roots use.

| Path | What it is |
| --- | --- |
| `main.tf` | The bucket `drip-ai-tfstate-<account>-<region>`: versioned, AES256 encrypted, public access blocked, ACLs off, TLS-only policy, 90-day expiry for old state versions, `prevent_destroy` |
| `variables.tf` | `project` (default `drip-ai`) and `region` (default `us-east-1`) |
| `outputs.tf` | `state_bucket` and `region` |
| `versions.tf` | Terraform and provider pins; the provider tags every resource `Project`, `ManagedBy=terraform`, `Stack=bootstrap` |
| `tests/bootstrap.tftest.hcl` | Offline `terraform test` with a mocked AWS provider |

## Commands

`bin/drip-ai setup` applies this root only when `aws s3api head-bucket` reports the bucket missing; [docs/setup.md](../docs/setup.md#create-the-shared-stack) covers the run. The bucket name comes from your account id and Region, so the repo holds no account id. `DRIP_AI_STATE_BUCKET_SUFFIX` reaches this root as `state_bucket_suffix` and makes the name `drip-ai-tfstate-<account>-<region>-<suffix>`.

If `bootstrap/terraform.tfstate` is lost, the bucket still exists: import it with the `tf` helper (`bin/drip-ai tf bootstrap import aws_s3_bucket.state <bucket>`) and read the next plan for the settings that still need importing.

## Tests

The repo gate runs this root with the others: `bin/drip-ai check`. To run it alone:

```bash
terraform -chdir=bootstrap init -backend=false -lockfile=readonly -input=false && terraform -chdir=bootstrap test
```
