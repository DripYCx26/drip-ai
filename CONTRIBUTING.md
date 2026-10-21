# Contributing

Bug reports, fixes and docs improvements are welcome. For a bug, include the command you ran, what you expected, your operating system and tool versions, and a small redacted reproduction. For anything bigger than a fix, open an issue first so we can agree on the scope.

## Making a change

1. Fork the repository and branch from `main`.
2. Skim [the architecture](docs/architecture.md) so the change lands in the component that owns it.
3. Keep the change focused, and add a test for any behavior you change.
4. Run the checks from the repository root:

   ```bash
   bin/drip-ai check --public
   ```

5. Open a pull request that says what the problem was, what changed, and which checks you ran.

The checks need Terraform, Bash and Python 3.9 or newer. They run Terraform in throwaway directories against mocked AWS calls, so they need no credentials and create nothing in AWS. Provider downloads need internet access, and the full run takes several minutes. `--public` also confirms the tree names no model: see below.

Nothing in the test suite proves a model serves. That takes a real deployment, a tunnel and `bin/drip-ai smoke`, which spends money and stays out of CI.

## Keep deployments out of the repository

Use made-up identifiers in tests, examples and docs, such as account `111122223333` and models like `Org/Name`. Keep weights, model choices, Terraform variables and state, credentials, client settings and logs out of commits, issues and pull requests. An ignored file can still be added with `git add -f`, so read the staged diff before you push.

The repository ships `shared/model-profiles/profiles.json` as `{}` and no catalog or default file names a `model_type` or a Hugging Face repo id. Put your own profiles in your working copy, restore the empty map before you commit, and let `bin/drip-ai check --public` confirm it.

## Style

Follow the code around you. Shell scripts target Bash and must pass `bash -n`; Terraform must pass `terraform fmt`. Put facts such as prices, hardware and error patterns in `shared/catalog/*.json` and settings with their defaults in `shared/config/defaults.json`, not in code. Record changes to serving, networking, tagging or state in [docs/DECISIONS.md](docs/DECISIONS.md), newest first.

## Security and license

Report vulnerabilities as described in [SECURITY.md](SECURITY.md), not in a public issue. Contributions are submitted under the [MIT License](LICENSE); keep the [third-party notices](NOTICE) accurate.
