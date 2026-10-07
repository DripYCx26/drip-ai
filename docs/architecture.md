# Architecture

drip-ai is a command-line tool, `bin/drip-ai`, over a few Terraform stacks in one AWS account. Shared parts hold the weights and the key; a backend turns them into a running vLLM server; a tunnel brings that server to `localhost` on your machine.

## Components

Each component has one job:

| Component | Where | What it does |
| --- | --- | --- |
| CLI | `bin/drip-ai` | Routes every command to the script that owns it |
| State bucket | `bootstrap/` | Holds every stack's Terraform state, one bucket per account and Region |
| Shared stack | `shared/terraform/` | The models bucket `drip-ai-models-<account>`, the API key `/drip-ai/api-key` in SSM, and the `drip-ai` resource group |
| Fetch job | `shared/fetch-model/` | A Fargate task that copies a Hugging Face repo into `models/<name>/` in the models bucket and writes the `drip-ai-fetch.json` manifest |
| Model profiles | `shared/model-profiles/` | vLLM parser flags per `model_type`, and the architectures the pinned vLLM serves |
| Conventions | `shared/modules/conventions/` | Resource names and container image pins every stack reads |
| Tunnel and client link | `shared/lib/common.sh`, `shared/lib/codex_link.py` | Runs a backend's local proxy or port-forward, waits for the model, links the coding tools, and unlinks them on exit |
| Smoke test | `shared/clients.sh` | Calls every client API on the open tunnel |
| Status snapshot | `shared/status.py` | `bin/drip-ai status --json`: what runs and bills, as one JSON object, read-only |
| SageMaker backend | `sagemaker/` | Races endpoints, runs AWS's vLLM image, and signs calls through a local proxy |
| EC2 backend | `ec2/` | One GPU instance running the pinned vLLM container, reached through SSM |

## Request path

A client request travels the same way on both backends up to the tunnel:

```text
coding tool -> http://localhost:8000/v1/...
  SageMaker: proxy.py (checks the API key, signs with your AWS credentials)
             -> InvokeEndpointWithResponseStream -> endpoint drip-ai-<type> -> vLLM
  EC2:       SSM port-forward -> instance 127.0.0.1:8000 -> vLLM (checks the API key)
```

vLLM serves the three client APIs itself: `/v1/chat/completions` for opencode, `/v1/responses` for Codex, and `/v1/messages` for the Claude Code CLI. Nothing listens on a public port.

## Backend seam

A backend is a folder that turns a model in the models bucket into a vLLM server on `127.0.0.1:$DRIP_AI_PORT`. SageMaker and EC2 each provide the same parts:

- A Terraform root at `<backend>/terraform/` with state key `<backend>.tfstate`, which reads the models bucket and the API key from the shared stack and the image pins from the conventions module.
- An operator script behind `bin/drip-ai <backend> ...` with at least `status`, `logs`, `tunnel`, and a start and stop pair.
- A tunnel command that calls `tunnel_with_link MODEL CONTEXT_WINDOW READY_FUNCTION PROXY_COMMAND` in `shared/lib/common.sh`. The backend supplies the served model name, the context window, a readiness check, and the command that serves the port; the shared code waits for the model and links the coding tools.
- A `tests/` folder of unit tests.

A backend is registered in three files: `<backend>_command`, `state_roots`, and the usage text in `bin/drip-ai`; `tf_root_dir` in `shared/lib/common.sh`; and the root, test, and script lists in `shared/check.sh`. Model profiles are read only by the SageMaker backend today.

## Where an app plugs in

An integration can use the same surfaces as the CLI workflow:

- The HTTP API at `http://localhost:8000/v1` while a tunnel runs, with the API key as a Bearer token.
- The CLI. Infrastructure and copy commands normally show a plan and request confirmation; `--yes` answers that confirmation for an approved run. `ec2 start` resumes immediately without prompting, so its cost must be approved before invocation. `--yes` never types the endpoint name that `--take-over` and `--force` ask for, nor the Hugging Face token, so `fetch --yes` refuses a gated model. `up` and the tunnels hold their process until it is stopped. `status --json`, `status`, `list`, `models list`, `smoke`, and `check` ask nothing.
- AWS state: the `StartedBy` and `Project` tags on each endpoint, the models in the models bucket with their `drip-ai-fetch.json` manifests, and the API key in SSM.

### Status snapshot

`bin/drip-ai status --json` prints one JSON object an app can poll. It makes only read-only AWS calls, reads the sections in parallel, runs no Terraform, and never reads or prints the API key or a token. It exits 0 whenever it prints the object:

The following values are synthetic examples:

```json
{
  "region": "us-east-1",
  "endpoints": [{"name": "drip-ai-p5en-48xlarge", "status": "InService", "instance_type": "ml.p5en.48xlarge",
                 "started_by": "arn:aws:iam::111122223333:user/alice", "created": "2026-01-01T10:00:00+00:00",
                 "served_model": "example-model", "max_model_len": 131072}],
  "ec2": [{"instance_id": "i-0123456789abcdef0", "instance_type": "g6e.12xlarge", "state": "stopped"}],
  "fetches": [{"task_id": "0123456789abcdef", "model": "ExampleOrg/ExampleModel", "status": "RUNNING",
               "started_by": "arn:aws:iam::111122223333:user/alice", "created": "2026-01-01T09:00:00+00:00"}],
  "models": [{"name": "ExampleOrg/ExampleModel", "source": "fetched", "fetch": "copying"},
             {"name": "my-upload", "source": "uploaded", "fetch": null}],
  "clients": {"codex": "linked", "claude": "unlinked", "opencode": "unlinked"},
  "errors": {}
}
```

These are the keys:

| Key | Value |
| --- | --- |
| `region` | The Region read, chosen as for every command |
| `endpoints` | One object per `drip-ai` endpoint. `started_by` is its `StartedBy` tag, null when missing; `created` is ISO 8601; `served_model` and `max_model_len` come from the endpoint's model |
| `ec2` | One object per instance tagged `Project=drip-ai` and `Stack=ec2` that is not terminated; an empty list when there is none |
| `fetches` | One object per running fetch task; `model` is the folder it copies to; an empty list when the fetch stack was never applied |
| `models` | One object per folder under `models/` holding `drip-ai-fetch.json` (`source` `fetched`, `fetch` its status such as `copying` or `complete`) or `config.json` (`source` `uploaded`, `fetch` null) |
| `clients` | `codex`, `claude`, and `opencode`, each `linked`, `unlinked`, or `damaged link record` |
| `errors` | Section name to the reason a read failed. The whole section is null when a read it depends on failed; otherwise the section is shown, a field that could not be read is null, and the first such failure is named |

A null value is unknown, never zero or none; an empty list means the read worked and found nothing.
