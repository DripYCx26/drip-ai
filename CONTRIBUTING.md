# Contributing

- For bug reports, include the command, expected result, operating system, tool versions, and a small redacted reproduction.
- Open an issue before a larger change so maintainers can agree on its scope.

## Make a change

- Fork the repository and create a branch.
- Read the [architecture guide](docs/architecture.md). For agent-assisted setup changes, also read the [installation guide](AGENTS.md).
- Keep changes focused and add regression coverage for changed behavior.
- Run the repository check from the root:

  ```bash
  bin/drip-ai check
  ```

  The check needs Terraform, Bash, and Python. It initializes providers in temporary directories and uses mocked AWS calls; it needs no AWS credentials and creates no cloud resources. Provider downloads need internet access. A live serving test requires a separately approved deployment and tunnel.

- Open a pull request that explains the problem, resulting behavior, and checks run.

## Keep deployment details private

- Use fictional identifiers in tests and examples.
- Keep weights, model choices, Terraform variables and state, credentials, client settings, and deployment logs out of commits, issues, and pull requests. Inspect the staged diff before submitting; ignored files can still be added with `git add -f`.
- Keep the public architecture catalog general. Put model-specific configuration in your private working copy, and do not submit deployment profiles. The repository check requires the public profile map to remain `{}`; restore it before checking or submitting.
- Report sensitive security issues through the [security policy](SECURITY.md).
- Contributions are submitted under the [MIT License](LICENSE). Preserve applicable [third-party notices](NOTICE).
