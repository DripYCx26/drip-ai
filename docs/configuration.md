# Configure drip-ai

drip-ai reads its settings in layers and can say where each value came from. Every key, its type, and its built-in value live in [shared/config/defaults.json](../shared/config/defaults.json); a key that file does not list is refused.

## Layers

Each layer overrides the ones before it:

| Layer | Where it comes from |
| --- | --- |
| `builtin` | `shared/config/defaults.json` |
| `derived` | The sizing engine and the learning store, only for keys whose built-in value is `null` (the context window, DCP, KV precision, and the performance flags) |
| `profile` | The model's entry in `shared/model-profiles/profiles.json`: its `serve` keys and `weights` |
| `user` | Your config file: `$DRIP_AI_CONFIG`, else `${XDG_CONFIG_HOME:-~/.config}/drip-ai/config.json` |
| `env` | `DRIP_AI_LINK_CLIENTS`, `DRIP_AI_PORT`, and `DRIP_AI_LEARN` |
| `cli` | Flags such as `--hours`, and `--set key=value` on `plan` |

An explicit value, from a profile or anything above it, always beats a derived one. Learning only fills derived values and orders choices; it never overrides what you set.

## See and change settings

These commands only read:

```bash
bin/drip-ai config show --explain        # every key, its value, its layer, and why
bin/drip-ai plan <name> --explain        # the serving plan for a model in S3, with every decision's reason
```

`plan` prints lines such as `serve.dcp = 8  derived  (smallest DCP with pool >= 2x1048576x1.10 (4,129,544 modeled tokens))`. Each number names its class: modeled, measured, or reported.

Write your file with `config set`, which keeps it 0600 and refuses unknown keys and wrong types:

```bash
bin/drip-ai config set regions=us-east-2,us-east-1 race.max_lanes=4 clients.link=opencode
bin/drip-ai config set race.max_lanes=            # an empty value removes the key
```

A synthetic example of the file itself:

```json
{"version": 1, "regions": ["us-east-2", "us-east-1"],
 "race": {"max_lanes": 4, "max_spend_per_hour_usd": 400, "hours": 12},
 "clients": {"link": ["opencode"], "opencode": {"set_default_model": true}},
 "ports": {"base": 8000, "range": 10, "auto": true},
 "notify": {"argv": ["osascript", "-e"]},
 "serve": {"target_sequences": 2, "min_context": 262144}}
```

The file must be plain JSON of at most 256 KB, and refused when other users can write it. `notify.argv` runs with one message appended when a race needs you, for example when its credentials expire.

## Move old Terraform tuning

Tuning used to live in `sagemaker/terraform/terraform.tfvars`. `sagemaker start` refuses while that file still sets `instance_types`, `max_model_len`, `vllm_extra_args`, or another old key, so a setting is never ignored silently. Move it once:

```bash
bin/drip-ai config import-tfvars
```

It writes each key to your config file, keeps the original as `terraform.tfvars.pre-import`, and leaves only `region` in the tfvars. `lanes_per_type` has no new key; `race.max_lanes` caps lanes across every region instead.

## The keys you change most

| Key | What it does |
| --- | --- |
| `regions` | Regions a race may use, in order; default the home region |
| `race.classes`, `race.instance_types` | Limit the GPU classes (`h200`, `b300`, `b200`, `h100`, `l40s-*`) or exact types raced; default every class whose GPUs hold the model |
| `race.max_lanes`, `race.max_spend_per_hour_usd`, `race.hours` | How many endpoints may bill at once, the ceiling on their summed list price, and the race cap |
| `serve.max_model_len` | Fixes the context window; the plan refuses when it does not fit |
| `serve.target_sequences`, `serve.min_context` | How many full-length sequences the KV cache should hold, and the smallest derived window accepted |
| `serve.extra_args` | vLLM flags added last; they win over the profile and the plan |
| `serve.repetition_penalty` | The server's default repetition penalty, 1.0, passed as `--override-generation-config` so a checkpoint's `generation_config.json` (some ship 1.1, which degrades long reasoning) does not apply it; `null` keeps the checkpoint's |
| `clients.link` | Clients a tunnel links; default `["opencode"]`, Codex and Claude Code only when listed, `[]` for none |
| `ports.base`, `ports.range`, `ports.auto` | The local port, and whether a taken one moves to the next free port |
| `lifecycle.protect` | Names `doctor` never touches |
| `learn.enabled` | Turns the learning store off (also `DRIP_AI_LEARN=off`) |
| `disk.min_free_gb` | Free space this machine keeps before a docker pull, image build, or large download, such as `runtime probe --docker`; default 30, `0` skips the check |

## The learning store

Races record what they saw in `${XDG_STATE_HOME:-~/.local/state}/drip-ai/observations.jsonl` (0600): where lanes found capacity and how long it took, which flags an image accepted or rejected, and the KV pool vLLM logged. The next plan orders regions and pools by placement chance (a Beta(1, 1) estimate whose evidence halves every 7 days), and a measured KV pool replaces the modeled one. Inspect and prune it:

| Command | What it does |
| --- | --- |
| `bin/drip-ai learn show` | Counts per kind, and how many malformed lines were skipped |
| `bin/drip-ai learn explain` | Placement chances, flag evidence, and KV measurements |
| `bin/drip-ai learn forget --kind K --before 2026-10-01` | Removes matching observations |
| `bin/drip-ai learn export --redact` | Every observation, with account and run ids removed |
| `bin/drip-ai learn import --log-group G --region R ...` | Records the KV pool from an endpoint's startup log |

A line that looks like a secret is refused. The store never leaves this machine.
