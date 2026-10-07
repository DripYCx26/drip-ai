# Security

## Report a vulnerability

Use the repository's **Security → Advisories → Report a vulnerability** form
for private reports. Include affected versions, a minimal reproduction,
impact, and suggested mitigation. If the private reporting button is absent,
avoid posting sensitive details in a public issue; contact a maintainer
through a private channel listed on their GitHub profile.

Replace credentials, account identifiers, model names, and customer data
with fictional values. Do not attach Terraform state, environment files,
raw deployment logs, or client configuration.

## Understand the boundaries

The latest default-branch version is the maintenance target. This project is
an operator tool for an AWS account, not a public inference gateway. Read
[setup and permissions](docs/setup.md) before sharing an account.

Clients connect through a local tunnel. Keep model servers off public ports:
vLLM's API key protects `/v1` routes, not every route. SageMaker's local proxy
binds to loopback. AWS permissions and access to state, SSM, S3, and logs remain
part of the trust boundary.

A neutral served-model alias reduces client-visible identifiers. It does not
hide the model from authorized AWS operators, state, storage, or logs, and it
does not change the model's license or attribution requirements.

If a credential is exposed, revoke or rotate it at its issuer and remove it
from affected artifacts and history. Deleting a file in a later commit does
not remove the original secret from Git history.
