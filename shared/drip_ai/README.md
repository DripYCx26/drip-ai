# drip_ai

The control core: stdlib-only Python 3.9 or newer, run with `python3 -I`, reaching AWS only through the AWS CLI. Entry scripts append this package's parent folder to `sys.path`. Facts it reads are JSON in [../catalog](../catalog) and [../config](../config); how it fits the rest is in [docs/architecture.md](../../docs/architecture.md).

| Path | What it is |
| --- | --- |
| `__init__.py` | Package paths: the catalog loader and the private state folder `${XDG_STATE_HOME:-~/.local/state}/drip-ai/` |
| `conventions.py` | Names and image pins shared with `shared/modules/conventions`: region suffixes, the models bucket, the execution role, the `drip-ai-` name rule, log groups; the pins are read from the module's `main.tf` |
| `config.py` | Layered config with provenance, the schema from `defaults.json`, the user file, the `DRIP_AI_*` table, `config set`, `import-tfvars`, and model profiles as a layer |
| `modelinfo.py` | Bounded reading of `config.json` into the facts sizing uses, and weight bytes from a listing |
| `capabilities.py` | Whether an image supports a flag or engine path: supported, rejected, or unknown, and the substrate policy that keeps or drops flags |
| `sizing.py` | The defaults engine: window, DCP, KV dtype, MTP, and performance flags with a reason each; the flag rules and the only `SM_VLLM_*` mapper |
| `learn.py` | The learning store: append under a lock, validate on read, decay, summarize, forget, export |
| `awscli.py` | Every AWS call: deadline, error classifier, operation allowlist, and the `read_only()` guard |
| `logsignals.py` | Bounded harvest of a vLLM log and the signals in it (version, KV pool, failure kind) |
| `procs.py` | The tunnel registry and the port listeners `lsof` sees; stops only a registered process with a matching start time |
| `serve.py` | Port and registry policy for tunnels (`prepare`), and `list`, `stop`, `holder`, `region`, `detach` |
| `doctor.py` | `bin/drip-ai doctor`: inventory, findings R1 to R13, and the allowlisted fixes |
| `probe.py` | `bin/drip-ai runtime probe`: parse `vllm serve --help` into per-image flag evidence; `--docker` first checks `disk.min_free_gb` |
| `cli.py` | Routes `plan`, `config`, `learn`, `doctor`, `runtime`, `logs`, and `vllm-args` for `bin/drip-ai` |

## Tests

The repo gate runs everything: `bin/drip-ai check`. These modules' tests live in [../tests](../tests):

```bash
python3 -I -m unittest discover -s shared/tests
```
