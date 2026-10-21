# Connect clients

A tunnel serves the model on `http://127.0.0.1:<port>`, port `ports.base` (default 8000): `bin/drip-ai sagemaker tunnel` for a SageMaker endpoint, or `bin/drip-ai ec2 tunnel` for the EC2 server. `DRIP_AI_PORT` overrides the port. The tunnel links only the clients in `clients.link`: opencode by default, and Codex or Claude Code only when you list them. Once the tunnel reports a successful link, a fresh session of each linked client uses the served model. When it closes, each one goes back to its own settings.

Turn linking on once, for the clients you want:

```bash
bin/drip-ai config set clients.link=codex,claude,opencode
```

`bin/drip-ai smoke` checks every API on the open tunnel. It needs a real text answer from chat completions, `/v1/messages`, and `/v1/responses`, and one parsed tool call.

## What the tunnel does

The tunnel registers itself in `~/.local/state/drip-ai/procs/` with its pid, port, endpoint, and region, and removes the record when it exits. Before it starts, it checks the port:

- A registered tunnel already serving the same endpoint: the new one says `A drip-ai tunnel already serves <endpoint> (pid N on 127.0.0.1:P); nothing changed.` and exits.
- Another process: the tunnel refuses and names its pid and command, unless `ports.auto` is true, which takes the next free port up to `ports.base + ports.range - 1`. It never stops a process it did not start.

`bin/drip-ai serve [--region R] [--endpoint NAME]` runs the same tunnel in the background with its output in `~/.local/state/drip-ai/procs/`; `bin/drip-ai serve list` shows the running ones and `serve stop --port P` stops one, which unlinks its clients.

Once the model answers, the tunnel links each named client that is installed and prints one line per client:

```text
Codex: linked (restart Codex)
Claude Code: linked (new session)
opencode: linked (restart opencode)
```

A client that is not installed is skipped without a line. If one client fails to link, it prints `not linked` and the error, and the others and the tunnel carry on.

The tunnel restores each client it linked when it exits, on Ctrl-C, TERM, or HUP. `bin/drip-ai sagemaker stop`, `ec2 stop`, and `ec2 destroy` restore every client too, because the links would point at a server that is gone.

Restoring is exact. If you did not touch the config, the original bytes come back. If you or the app changed it, only the values that still equal what the link wrote are put back, and the output says `kept your change to <key>` for the rest.

Each client gets the served model name and the server's context window (`max_model_len`), which the tunnel prints as `Context window: <n> tokens`. Beside each config the link keeps a record of what it changed (`drip-ai-link.json`, mode 0600) and a lock naming the tunnel that owns it, so a second tunnel cannot take over a live link.

The table shows when each client counts as installed and which file the tunnel edits.

| Client | Installed when | File the tunnel edits | Pick up the change |
| --- | --- | --- | --- |
| Codex | `~/.codex` exists (`$CODEX_HOME` when absolute) | `config.toml` | Restart Codex |
| Claude Code CLI | `~/.claude` exists (`$CLAUDE_CONFIG_DIR` when set) | `settings.json` | Start a new session |
| opencode | `~/.config/opencode` exists (under `$XDG_CONFIG_HOME` when set), or `opencode` is on `PATH` | `opencode.json` | Restart opencode |

The Claude desktop app is not supported.

## Codex

The link writes these keys to `config.toml`, shown for the SageMaker defaults:

```toml
model = "drip-coder"
model_provider = "drip-ai"
model_context_window = 131072
model_auto_compact_token_limit = 117964

[model_providers.drip-ai]
name = "drip-ai"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"

[model_providers.drip-ai.auth]
command = "/usr/local/bin/aws"
args = ["ssm", "get-parameter", "--region", "us-east-1", "--name", "/drip-ai/api-key", "--with-decryption", "--query", "Parameter.Value", "--output", "text"]
timeout_ms = 15000
refresh_interval_ms = 300000
```

- The key is never in the file. Codex runs the auth command, which reads the key from SSM into memory.
- `model_auto_compact_token_limit` is 90% of the window, the share at which Codex compacts by default. Codex caps the window of a model it does not know at 272,000 tokens.
- The link also keeps a copy of the previous config in `config.toml.drip-ai.bak`.
- It refuses when `config.toml` already defines a `model_providers.drip-ai` it did not write. Delete that table, or use another provider id for your own setup.

## Claude Code

The link writes these keys to `settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_MODEL": "drip-coder",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "drip-coder",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "drip-coder",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "drip-coder",
    "CLAUDE_CODE_SUBAGENT_MODEL": "drip-coder",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "131072",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "117964"
  },
  "apiKeyHelper": "/usr/local/bin/aws ssm get-parameter --region us-east-1 --name /drip-ai/api-key --with-decryption --query Parameter.Value --output text"
}
```

- `apiKeyHelper` runs the same SSM read as Codex, so the key stays off disk. Claude Code sends its output as both `x-api-key` and `Authorization: Bearer`, and both the SageMaker proxy and vLLM accept that.
- Every alias, subagents, and background tasks use the served model.
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` tells Claude Code the real window. It applies to a model name that does not start with `claude-`.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` compacts at 90% of the window. Claude Code accepts 100,000 tokens or more here, so the link leaves it out for a smaller window.
- `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is 16384, or a quarter of a smaller window. That is a chosen cap, not a measured one.
- `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` keeps Anthropic's pre-release request fields away from vLLM.
- Anthropic supports non-Claude models behind a gateway on a best-effort basis only.

The names come from the Claude Code [environment variables](https://code.claude.com/docs/en/env-vars) and [settings](https://code.claude.com/docs/en/settings) references.

## opencode

The link writes these keys to `opencode.json`, and creates the file when it is missing. In this illustrative JSON, `<absolute-config-directory>` stands for the resolved `$XDG_CONFIG_HOME` or `~/.config` directory; the link writes its actual absolute path:

```json
{
  "provider": {
    "drip-ai": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "drip-ai",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "{file:<absolute-config-directory>/drip-ai/opencode-key}"
      },
      "models": {
        "drip-coder": {
          "name": "drip-coder",
          "tool_call": true,
          "reasoning": true,
          "limit": { "context": 131072, "output": 16384 }
        }
      }
    }
  },
  "model": "drip-ai/drip-coder"
}
```

opencode has no hook that runs a command for the key, so the link writes the key to `~/.config/drip-ai/opencode-key` (mode 0600, in a 0700 folder) and deletes it when it unlinks. The key sits on your disk only while the tunnel is open. If the tunnel is killed with SIGKILL, the file stays until the next link, `bin/drip-ai unlink`, or `bin/drip-ai doctor --fix R13 --yes`.

These settings change what the opencode link writes:

| Setting | Effect |
| --- | --- |
| A second tunnel while one runs | Its provider is `drip-ai-<region>` (with its own link record, lock, and key file), so both endpoints show in opencode |
| `clients.opencode.set_default_model` | `true` also sets the top-level `model` to this provider; by default the link leaves your model alone |

The link never writes opencode's `compaction`. opencode applies it to every model of every provider, not per provider, so a buffer sized for a million-token window would make every smaller model compact far too early. If you set it in your own `opencode.json`, it applies to all your models.

It refuses when `opencode.json` already defines the provider it writes without having written it, or when `opencode.jsonc` sits beside it, because opencode would read that file last.

## Opt out

You can keep the tunnel from touching some or all clients:

| Setting | Effect |
| --- | --- |
| `clients.link` in your config | The clients a tunnel links; default opencode only, `[]` for none |
| `DRIP_AI_LINK_CLIENTS=codex,claude` | Overrides `clients.link` for one run: `codex`, `claude`, `opencode`, `all`, or `none` |
| `bin/drip-ai sagemaker tunnel --no-link` or `bin/drip-ai ec2 tunnel --no-link` | Links nothing |
| `DRIP_AI_CODEX_LINK=0` or `--no-codex-link` | Skips Codex only |

## Commands

These commands work on a stuck link or link by hand. `CLIENT` is `codex`, `claude`, or `opencode`; without it, each command covers every client.

| Command | What it does |
| --- | --- |
| `bin/drip-ai status [CLIENT]` | Prints `linked` or `unlinked`, the owning process, and the current model |
| `bin/drip-ai unlink [CLIENT]` | Restores what the link changed |
| `bin/drip-ai unlink [CLIENT] --force` | Also drops a damaged link record or a stuck lock; for Codex without a record, removes drip-ai's tables |
| `bin/drip-ai link [CLIENT] --model NAME --context-window TOKENS` | Links by hand to `127.0.0.1:<port>` |
| `bin/drip-ai codex link\|unlink\|status ...` | Codex's own link program with all its flags (`python3 -I shared/lib/codex_link.py --help`) |

## Troubleshooting

| What you see | What to do |
| --- | --- |
| A client still uses its old model | Restart Codex or opencode, or start a new Claude Code session |
| `is not plain JSON` | The config has comments or trailing commas. The error prints the keys to add; paste them by hand, or remove the comments and reopen the tunnel |
| `the API key command failed with only HOME and PATH=/usr/bin:/bin set` | Apps start the SSM read without your shell's environment. Use a named profile or SSO from `~/.aws/config` (`export AWS_PROFILE=<name>`) and reopen the tunnel. opencode still links |
| `already linked by pid <n>` | Another tunnel owns that client's link. Close it, or run `bin/drip-ai unlink CLIENT` |
| `port <n> is held by pid <m> (...)` | Another process listens there. Stop it if it is yours, set `DRIP_AI_PORT`, or `bin/drip-ai config set ports.auto=true` |
| `settings.json env sets ANTHROPIC_AUTH_TOKEN` | Claude Code sends that token instead of the drip-ai key. Remove it while the tunnel is open |
| A project ignores the link | A project's `.claude/settings.json` or `opencode.json` overrides your user config. Remove the model and base URL keys there |

## Other clients

For another client that supports the model's OpenAI-compatible APIs, configure it by hand: set it to `http://127.0.0.1:8000/v1`, use the served model name, and put the key in your shell first:

```bash
export DRIP_AI_API_KEY="$(bin/drip-ai key)"
```

`bin/drip-ai key` refuses to print to a terminal, so the key stays out of your scrollback. Each region's shared stack holds its own key; `key` and `smoke` read the key of the region whose registered tunnel serves the port, else the home region's. The ChatGPT app can't use a self-hosted endpoint.

## On a SageMaker endpoint

`bin/drip-ai sagemaker tunnel` runs a local proxy on the same address, which checks the same key and serves the same model name. The proxy answers `GET /v1/models` itself. Limits that EC2 does not have:

- A request body can be at most 6 MiB.
- SageMaker's invocation time limit can cut a long streaming response. If that happens before any output, the proxy returns a 502 with code `ModelStreamError`; after output has started, the stream simply ends early without a final event. Long non-streaming calls also travel as a stream internally, so the same limit applies, and the proxy gives up on any request after 15 minutes.
- The proxy signs with your AWS credentials, so they must allow `sagemaker:InvokeEndpoint` on `endpoint/drip-ai-*`; that IAM action also authorizes streaming invocation. It needs AWS CLI v2 at 2.9 or later.
