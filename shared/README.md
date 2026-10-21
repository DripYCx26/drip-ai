# shared

Code every backend uses: the Python control core and its catalogs, the models bucket and API key, the Hugging Face fetch job, model profiles, the shell library, the client commands, and the coding-tool links. [docs/architecture.md](../docs/architecture.md) shows how these fit with the backends.

| Path | What it is |
| --- | --- |
| `drip_ai/` | The control core, stdlib-only Python run with `python3 -I`; see [its README](drip_ai/README.md) |
| `catalog/` | Facts as JSON the core reads: `instances.json` (GPUs, memory, classes), `attention.json` (KV layouts per architecture family), `runtimes.json` (vLLM version and flag evidence per image digest), `vllm_flags.json` (flag first releases and types), `prices.json`, `aws_errors.json` (error classes), `log_signals.json` (vLLM log patterns) |
| `config/defaults.json` | Every drip-ai config key with its type and built-in value; also the schema, so an unlisted key is refused |
| `terraform/` | Terraform root `shared` (state key `shared.tfstate`): the models bucket, the API key in SSM, and the `drip-ai` resource group |
| `fetch-model/` | The Hugging Face to S3 job: script, copier, Terraform root, tests; see [its README](fetch-model/README.md) |
| `model-profiles/` | Per-family vLLM flags keyed by `model_type`, and the architectures the pinned image's vLLM serves; `sagemaker start` checks both before it applies; see [its README](model-profiles/README.md) |
| `modules/conventions/` | Resource-free module: names, ARNs, and image pins that every root reads, so each is defined once |
| `lib/common.sh` | Shell library: Region, caller identity, account guard, `tf_init`, `tf`, `tf_output`, `tf_approved` (adds `-auto-approve` for a run given `--yes`), S3 leases, `drip_config`, `serve_prepare`, `tunnel_with_link` |
| `lib/codex_link.py` | Links and unlinks Codex's `config.toml` |
| `lib/json_link.py` | Links and unlinks the Claude Code CLI's `settings.json` and opencode's `opencode.json` (`--client claude\|opencode`; opencode takes `--provider` and `--set-default-model`) |
| `lib/link_files.py` | Owner lock, compare-and-swap write, and private files shared by both linkers; all three run with `python3 -I` and the standard library only |
| `clients.sh` | `key`, `smoke`, `link\|unlink\|status [client]`, and `codex link\|unlink\|status` behind `bin/drip-ai` |
| `models.sh` | `upload <folder>` and `list` behind `bin/drip-ai models` |
| `status.py` | `bin/drip-ai status --json`: one read-only JSON snapshot of endpoints, EC2, fetches, models, client links, races, tunnels, and capacity blocks for an app to poll; the shape is in [docs/architecture.md](../docs/architecture.md#where-an-app-plugs-in) |
| `check.sh` | The repo gate behind `bin/drip-ai check`; `--dry-run` prints the steps, `--public` first checks the tree carries no model choice |
| `testing/cli_stubs.py` | Test harness that puts stub `aws`, `terraform`, and `curl` first on `PATH` and logs every call; `skip_sleeps()` makes `sleep` log its seconds and return at once |
| `tests/` | Unit tests for the library, dispatch (including `up`), clients, models, model profiles, `status --json`, the client links, and each control-core module (`test_config.py`, `test_sizing.py`, `test_learn.py`, `test_awscli.py`, `test_logsignals.py`, `test_procs.py`, `test_doctor.py`, `test_probe.py`, `test_conventions.py`); `fixtures/` holds synthetic model configs and help text |

## Terraform root

| Item | Value |
| --- | --- |
| Variables | `project`, `region`, `api_key_version` (raise it by one to write a new key) |
| Outputs | `models_bucket`, `api_key_parameter`, `region` |
| API key | `/drip-ai/api-key`, a write-only SecureString from an ephemeral random password, so state never holds the key |
| Models bucket | `drip-ai-models-<account>` in us-east-1, `drip-ai-models-<account>-<region>` elsewhere; weights live under `models/<name>/`; a lifecycle rule aborts incomplete multipart uploads after one day |

`bin/drip-ai setup` applies it after it creates the state bucket and initializes every root, so a new member's setup leaves them ready to run `bin/drip-ai fetch`. Never use a bare `terraform` call: the `tf` helper supplies the per-account backend.

## Library behavior

- Region: `DRIP_AI_REGION`, else `AWS_REGION`, else `AWS_DEFAULT_REGION`, else `us-east-1`.
- Account guard: the state bucket name is derived from your caller identity and Region. `tf` refuses a root that was initialised for another account or Region; `bin/drip-ai init --reconfigure` switches deliberately.
- Lock errors: when Terraform reports a held state lock, `tf` prints who holds it.
- Leases: `lease_acquire NAME TTL_SECONDS` writes `s3://<state bucket>/leases/NAME.json` with a create-only S3 write. It takes over an expired lease or one whose holding process on this machine has exited, refreshes your own (same ARN, host, and process), refuses anyone else's live lease or another live drip-ai process on this machine, and caps the TTL at 24 hours. Every state-bucket and lease call passes `--expected-bucket-owner`, and every `terraform init` uses `-lockfile=readonly`. `lease_release NAME` deletes only a lease that still names you.
- Tunnels: `serve_prepare` asks `drip_ai/serve.py` for the port and the opencode provider and registers the shell as the tunnel. `tunnel_with_link` refuses a local port something already answers on, naming its pid, then runs the proxy or port-forward as a child, waits for the model, links each installed coding tool that `clients.link` names (opencode only by default), and unlinks exactly what it linked on exit, Ctrl-C, TERM, or HUP, removing its registry record. With `tunnel_smoke=yes` (`sagemaker tunnel --smoke`) it runs the smoke test once after linking.

## Environment variables

| Variable | Effect |
| --- | --- |
| `DRIP_AI_REGION` | AWS Region for every command; see the order above |
| `DRIP_AI_PORT` | Local port for tunnels, `smoke`, and the dashboard; overrides `ports.base` (default `8000`) |
| `DRIP_AI_STATE_BUCKET_SUFFIX` | Optional suffix (up to 12 of `a-z`, `0-9`, `-`) appended to the state bucket name, for when another account already owns `drip-ai-tfstate-<account>-<region>`; everyone sharing the account must set the same value |
| `DRIP_AI_LINK_CLIENTS` | Which tools a tunnel links, overriding `clients.link` (default opencode): a comma list of `codex`, `claude`, `opencode`, or `all` or `none`; `--no-link` on a tunnel means `none` |
| `DRIP_AI_LEARN` | `off` turns the learning store off for the run (`learn.enabled`) |
| `DRIP_AI_CONFIG` | Absolute path of the drip-ai config file; default `${XDG_CONFIG_HOME:-~/.config}/drip-ai/config.json` |
| `XDG_STATE_HOME` | Root of the private state folder `drip-ai/` (learning, race journals, tunnel records); default `~/.local/state` |
| `DRIP_AI_CODEX_LINK` | `0` drops Codex only; `--no-codex-link` does the same |
| `DRIP_AI_MODEL` | Model name for `smoke` when `/v1/models` lists several |
| `DRIP_AI_WATCH_SECONDS` | Refresh seconds for the SageMaker dashboard (default 15) and the fetch view (default 10); minimum 5 |
| `CODEX_HOME` | Codex config folder when it is an absolute path; default `~/.codex` |
| `CLAUDE_CONFIG_DIR` | Claude Code config folder; default `~/.claude` |
| `XDG_CONFIG_HOME` | opencode config root; default `~/.config` |

Test-only hooks (`DRIP_AI_TTY`, `DRIP_AI_NOW_EPOCH`, `DRIP_AI_WATCH_ITERATIONS`, `DRIP_AI_TUNNEL_POLL_SECONDS`, `DRIP_AI_TUNNEL_WAIT_SECONDS`, `DRIP_AI_STOP_POLL_SECONDS`, `DRIP_AI_STOP_WAIT_SECONDS`) are described in the script headers.

## Tests

The repo gate runs everything: `bin/drip-ai check`. The shared unit tests alone:

```bash
python3 -I -m unittest discover -s shared/tests
```
