# Contribute to drip-ai

Thanks for helping make self-hosted model deployment easier to operate.
For a bug report, include the command, expected result, operating system,
tool versions, and a small redacted reproduction. For a larger change,
open an issue first so maintainers can agree on the scope.

## Work on a change

1. Fork the repository and create a branch for your change.
2. Read [the architecture](docs/architecture.md) and [the contributor rules](AGENTS.md).
3. Keep the change focused and add regression coverage for changed behavior.
4. Run the repository check from the root:

   ```bash
   bin/drip-ai check
   ```

5. Open a pull request explaining the problem, resulting behavior, and checks run.

The check needs Terraform, Bash, and Python. It initializes providers in
throwaway directories and uses mocked AWS calls; it needs no AWS credentials
and creates no cloud resources. Provider downloads require internet access.
A live serving test requires a separately approved deployment and tunnel.

## Keep deployment details private

Use fictional identifiers in tests and examples. Keep weights, model choices,
Terraform variables and state, credentials, client settings, and deployment
logs out of commits, issues, and pull requests. An ignored file can still be
added with `git add -f`, so inspect the staged diff before submitting.

The architecture catalog lists general upstream compatibility. It is not a
place to record deployment choices. Model-specific configuration belongs in
your private working copy; do not submit curated profiles from a deployment.
The repository check requires the public profile map to remain `{}`. Restore
that empty map before checking or submitting the public source.

Report sensitive security issues through [the security policy](SECURITY.md).
Contributions are submitted under the [MIT License](LICENSE); preserve
applicable [third-party notices](NOTICE).
