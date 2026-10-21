"""Pins `shared/clients.sh key`, `smoke`, `link`, `unlink`, `status`, and `codex` against stub CLIs."""

import json
import os
import pty
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))

from cli_stubs import StubRepo  # noqa: E402

SCRIPT = "shared/clients.sh"
REGION = "eu-west-1"
ENV = {"DRIP_AI_REGION": REGION}
KEY = "sk-CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyz"
CURL_CONFIG = f'header = "Authorization: Bearer {KEY}"\n'
MODEL = "drip-coder"
GET_KEY = ["ssm", "get-parameter", "--region", REGION, "--name", "/drip-ai/api-key",
           "--with-decryption", "--query", "Parameter.Value", "--output", "text"]
MODELS_BODY = json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
CHAT_BODY = json.dumps({"id": "c1", "choices": [{"message": {"content": "ok"}}]})
TOOL_BODY = json.dumps({"id": "c2", "choices": [{"message": {"content": None, "tool_calls": [
    {"id": "t1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]}}]})
STREAM_BODY = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
MESSAGES_BODY = json.dumps({"id": "m1", "type": "message", "content": [{"type": "text", "text": "ok"}]})
RESPONSES_BODY = json.dumps({"id": "r1", "object": "response", "output": [
    {"type": "reasoning", "summary": []},
    {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})


def make_repo():
    repo = StubRepo()
    repo.stub("aws", "ssm", "get-parameter", stdout=KEY + "\n")
    return repo


def script_apis(repo, **overrides):
    """Answers each API the smoke test calls; `overrides` maps a rule name to (stdout, exit)."""
    answers = {
        "models": ("/v1/models", None, MODELS_BODY, None),
        "stream": ("/v1/chat/completions", "-fsSN", STREAM_BODY, None),
        "chat": ("/v1/chat/completions", None, CHAT_BODY, 1),  # the plain answer comes first
        "tool": ("/v1/chat/completions", None, TOOL_BODY, None),
        "messages": ("/v1/messages", None, MESSAGES_BODY, None),
        "responses": ("/v1/responses", None, RESPONSES_BODY, None),
    }
    for name, (path, flag, body, times) in answers.items():
        stdout, exit_code = overrides.get(name, (body, 0))
        tokens = [f"http://localhost:8000{path}", *([flag] if flag else [])]
        repo.stub("curl", *tokens, stdin=CURL_CONFIG, stdout=stdout, exit=exit_code, times=times)


class KeyTest(unittest.TestCase):
    def test_key_reads_ssm_by_convention_without_terraform(self):
        with make_repo() as repo:
            result = repo.run(SCRIPT, "key", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, KEY + "\n")
            self.assertEqual([(c.tool, c.argv) for c in repo.calls()], [("aws", GET_KEY)])

    def test_key_and_smoke_follow_the_registered_tunnel_on_the_port(self):
        with make_repo() as repo:
            sys.path.append(str(Path(__file__).resolve().parents[1]))
            from drip_ai import procs

            proc = subprocess.Popen(["sleep", "60"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            procs.register({"HOME": str(repo.home)}, proc.pid, ["tunnel"], port=8002, endpoint="drip-ai-h200-us-east-2-1",
                           region="us-east-2", provider="drip-ai-us-east-2")
            result = repo.run(SCRIPT, "key", env={"DRIP_AI_PORT": "8002"})
            self.assertEqual(result.returncode, 0, result.stderr)
            [call] = repo.calls()
            self.assertEqual(call.argv[call.argv.index("--region") + 1], "us-east-2")
            config = repo.home / ".config" / "drip-ai" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"ports": {"base": 8002}}))
            config.chmod(0o600)
            self.assertEqual(repo.run(SCRIPT, "key").returncode, 0)
            self.assertEqual(repo.calls()[-1].argv[repo.calls()[-1].argv.index("--region") + 1], "us-east-2")

    def test_key_refuses_on_terminal(self):
        with make_repo() as repo:
            primary, secondary = pty.openpty()
            try:
                proc = repo.popen(SCRIPT, "key", env=ENV, stdout=secondary, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, text=True)
                os.close(secondary)
                secondary = None
                _, err = proc.communicate(timeout=30)
                os.set_blocking(primary, False)
                try:
                    shown = os.read(primary, 4096).decode()
                except (BlockingIOError, OSError):
                    shown = ""
            finally:
                os.close(primary)
                if secondary is not None:
                    os.close(secondary)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('export DRIP_AI_API_KEY="$(bin/drip-ai key)"', err)
            self.assertNotIn(KEY, shown + err)
            self.assertEqual(repo.calls(), [])

    def test_key_fails_when_ssm_fails(self):
        with StubRepo() as repo:
            repo.stub("aws", "ssm", "get-parameter", exit=255, stderr="Unable to locate credentials\n")
            result = repo.run(SCRIPT, "key", env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("could not read /drip-ai/api-key", result.stderr)

    def test_key_refuses_a_key_that_would_break_the_curl_config(self):
        for bad in ('sk-"quoted', "sk-back\\slash", "sk with space", "sk-two\nlines", "sk-bell\x07", ""):
            with self.subTest(bad=bad), StubRepo() as repo:
                repo.stub("aws", "ssm", "get-parameter", stdout=bad + "\n")
                result = repo.run(SCRIPT, "key", env=ENV)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")


class SmokeTest(unittest.TestCase):
    def test_smoke_checks_each_api(self):
        with make_repo() as repo:
            script_apis(repo)
            result = repo.run(SCRIPT, "smoke", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            for header in ("== /v1/models", "== /v1/chat/completions (opencode)",
                           "== /v1/chat/completions tool call", "== /v1/chat/completions stream",
                           "stream ok", "== /v1/messages (Claude Code)", "== /v1/responses (Codex)"):
                self.assertIn(header, result.stdout)
            curls = [c.argv for c in repo.calls() if c.tool == "curl"]
            urls = [next(a for a in argv if a.startswith("http://")) for argv in curls]
            self.assertEqual(urls, [f"http://localhost:8000{p}" for p in
                                    ("/v1/models", "/v1/chat/completions", "/v1/chat/completions",
                                     "/v1/chat/completions", "/v1/messages", "/v1/responses")])
            bodies = [json.loads(argv[argv.index("-d") + 1]) for argv in curls[1:]]
            self.assertTrue(all(body["model"] == MODEL for body in bodies))
            answer, tool, stream, messages, responses = bodies
            # Room for a reasoning model to think and still answer, on every API that must answer.
            self.assertGreaterEqual(answer["max_tokens"], 512)
            self.assertEqual(messages["max_tokens"], answer["max_tokens"])
            self.assertEqual(responses["max_output_tokens"], answer["max_tokens"])
            # "auto" goes through the tool-call parser; a named choice would bypass it.
            self.assertEqual(tool["tool_choice"], "auto")
            self.assertEqual(tool["tools"][0]["function"]["name"], "get_weather")
            self.assertIn("get_weather", tool["messages"][0]["content"])
            self.assertTrue(stream["stream"])
            self.assertEqual(responses["input"], "Say ok")

    def test_smoke_key_never_in_argv(self):
        with make_repo() as repo:
            script_apis(repo)
            result = repo.run(SCRIPT, "smoke", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            curls = [c for c in repo.calls() if c.tool == "curl"]
            self.assertEqual(len(curls), 6)
            for call in curls:
                self.assertEqual(call.stdin, CURL_CONFIG)
                self.assertIn("--config", call.argv)
                self.assertEqual(call.argv[call.argv.index("--config") + 1], "-")
            for call in repo.calls():
                self.assertFalse(any(KEY in arg for arg in call.argv), call.argv)
                self.assertFalse(any(KEY in value for value in call.env.values()), call.tool)
            self.assertNotIn(KEY, result.stdout + result.stderr)

    def test_smoke_fails_on_each_wrong_answer(self):
        cases = {
            "models": ((json.dumps({"data": [{"id": "other"}]}), 0), {"DRIP_AI_MODEL": MODEL},
                       f"smoke: /v1/models does not list {MODEL}\n"),
            "chat": (('{"error": "x"}', 0), {}, 'smoke: /v1/chat/completions has no "choices"\n'),
            "tool": ((json.dumps({"choices": [{"message": {"content": "<tool_call>get_weather</tool_call>"}}]}), 0),
                     {}, "smoke: /v1/chat/completions did not return a parsed get_weather tool call;"
                     " check the model profile's tool-call parser\n"),
            "stream": (('data: {"x":1}\n\n', 0), {},
                       "smoke: streaming /v1/chat/completions did not end with data: [DONE]\n"),
            "messages": (('{"type": "error"}', 0), {}, 'smoke: /v1/messages has no "type":"message"\n'),
            "responses": (("", 22), {}, "smoke: /v1/responses request failed\n"),
        }
        for name, (override, extra_env, message) in cases.items():
            with self.subTest(name), make_repo() as repo:
                script_apis(repo, **{name: override})
                result = repo.run(SCRIPT, "smoke", env={**ENV, **extra_env})
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, message)

    def test_smoke_refuses_an_answer_a_parser_did_not_clean(self):
        answers = {
            "empty": "",
            "only whitespace": " \n",
            "raw reasoning": "<think>The user wants ok.</think>ok",
            "raw tool call": "<tool_call>get_weather<arg_key>city</arg_key></tool_call>",
            "raw special token": "<|tool_calls_section_begin|>",
        }
        for label, content in answers.items():
            with self.subTest(label), make_repo() as repo:
                body = json.dumps({"choices": [{"message": {"content": content}}]})
                script_apis(repo, chat=(body, 0))
                result = repo.run(SCRIPT, "smoke", env=ENV)
                self.assertEqual(result.returncode, 1)
                self.assertIn("answered with empty content or raw <think> or tool-call markup", result.stderr)

    def test_smoke_fails_on_a_thinking_only_messages_or_responses_reply(self):
        thinking = json.dumps({"id": "m1", "type": "message", "stop_reason": "max_tokens",
                               "content": [{"type": "thinking", "thinking": "The user wants ok."}]})
        raw = json.dumps({"id": "m1", "type": "message",
                          "content": [{"type": "text", "text": "<think>ok</think>"}]})
        reasoning = json.dumps({"id": "r1", "object": "response",
                                "output": [{"type": "reasoning", "summary": []}]})
        cases = {
            "messages thinking only": ("messages", thinking, "/v1/messages answered with no text block"),
            "messages raw markup": ("messages", raw, "/v1/messages answered with no text block"),
            "responses reasoning only": ("responses", reasoning, "/v1/responses answered with no output_text"),
        }
        for label, (name, body, message) in cases.items():
            with self.subTest(label), make_repo() as repo:
                script_apis(repo, **{name: (body, 0)})
                result = repo.run(SCRIPT, "smoke", env=ENV)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)

    def test_smoke_refuses_a_tool_call_the_parser_did_not_clean(self):
        def tool_message(content, name="get_weather", arguments='{"city": "Paris"}'):
            call = {"id": "t1", "type": "function", "function": {"name": name, "arguments": arguments}}
            return json.dumps({"choices": [{"message": {"content": content, "tool_calls": [call]}}]})

        bodies = {
            "no tool_calls": json.dumps({"choices": [{"message": {"content": "It is sunny in Paris."}}]}),
            "raw markup beside tool_calls": tool_message('<tool_call>{"name": "get_weather"}</tool_call>'),
            "another tool": tool_message(None, name="get_time"),
            "arguments not JSON": tool_message(None, arguments="city=Paris"),
        }
        for label, body in bodies.items():
            with self.subTest(label), make_repo() as repo:
                script_apis(repo, tool=(body, 0))
                result = repo.run(SCRIPT, "smoke", env=ENV)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("did not return a parsed get_weather tool call", result.stderr)

    def test_smoke_needs_one_served_model_or_a_name(self):
        two = json.dumps({"data": [{"id": "a"}, {"id": "b"}]})
        with make_repo() as repo:
            script_apis(repo, models=(two, 0))
            result = repo.run(SCRIPT, "smoke", env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "smoke: /v1/models does not list exactly one model; set DRIP_AI_MODEL\n")


class CodexTest(unittest.TestCase):
    def test_codex_passes_through_to_codex_link(self):
        with StubRepo() as repo:
            repo.install_stub("shared/lib/codex_link.py", "codex_link")
            repo.stub("codex_link", stdout="ok\n")
            for args in (["link", "--provider", "drip-ai"], ["unlink"], ["status"]):
                result = repo.run(SCRIPT, "codex", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([c.argv for c in repo.calls()], [["link", "--provider", "drip-ai"], ["unlink"], ["status"]])
            self.assertEqual(repo.run(SCRIPT, "codex", "bogus").returncode, 2)


class LinkCommandsTest(unittest.TestCase):
    def make_repo(self):
        repo = make_repo()
        repo.install_stub("shared/lib/codex_link.py", "codex_link")
        repo.install_stub("shared/lib/json_link.py", "json_link")
        return repo

    def test_link_names_one_client_and_pipes_opencode_its_key(self):
        with self.make_repo() as repo:
            repo.stub("json_link", "link", "opencode", stdin=KEY + "\n", stdout="linked opencode to m\n")
            result = repo.run(SCRIPT, "link", "opencode", "--model", "m", "--context-window", "65536", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "opencode: linked (restart opencode)\n")
            links = [c for c in repo.calls() if c.tool == "json_link"]
            self.assertEqual([c.argv[:8] for c in links], [["link", "--client", "opencode", "--model", "m",
                                                            "--port", "8000", "--context-window"]])
            for call in repo.calls():
                self.assertFalse(any(KEY in arg for arg in call.argv), call.argv)

    def test_link_prints_each_note_the_link_program_adds(self):
        with self.make_repo() as repo:
            repo.stub("json_link", "link", stdin=KEY + "\n", stdout=(
                "linked opencode to m (owner pid 7); took over a stale link from pid 4 on this host;"
                " repaired the previous link: kept your change to model\n"))
            result = repo.run(SCRIPT, "link", "opencode", "--model", "m", "--context-window", "65536", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "opencode: linked (restart opencode)\n"
                                            "  took over a stale link from pid 4 on this host\n"
                                            "  repaired the previous link: kept your change to model\n")

    def test_link_reports_a_client_that_is_not_installed(self):
        with self.make_repo() as repo:
            repo.stub("json_link", "link", stdout="Claude Code config not found; skipping\n")
            result = repo.run(SCRIPT, "link", "claude", "--model", "m", "--context-window", "65536", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Claude Code: not installed; skipped\n")

    def test_link_refuses_without_model_and_window_or_with_an_unknown_client(self):
        with self.make_repo() as repo:
            for args in (["link"], ["link", "--model", "m"], ["link", "cursor", "--model", "m", "--context-window", "1"]):
                self.assertEqual(repo.run(SCRIPT, *args, env=ENV).returncode, 2, args)
            self.assertEqual(repo.calls(), [])

    def test_unlink_and_status_cover_every_client_by_default(self):
        with self.make_repo() as repo:
            repo.stub("codex_link", stdout="linked; owner pid 1 dead; model m; model_provider drip-ai\n")
            repo.stub("json_link", stdout="unlinked; no owner; model unset\n")
            result = repo.run(SCRIPT, "status", env=ENV)
            self.assertEqual(result.stdout, "Codex: linked; owner pid 1 dead; model m; model_provider drip-ai\n"
                                            "Claude Code: unlinked; no owner; model unset\n"
                                            "opencode: unlinked; no owner; model unset\n")
            self.assertEqual(repo.run(SCRIPT, "unlink", env=ENV).returncode, 0)
            self.assertEqual(repo.run(SCRIPT, "unlink", "claude", "--force", env=ENV).returncode, 0)
            unlinks = [c.argv for c in repo.calls() if c.argv[0] == "unlink"]
            self.assertEqual(unlinks, [["unlink", "--provider", "drip-ai"], ["unlink", "--client", "claude"],
                                       ["unlink", "--client", "opencode", "--provider", "drip-ai"],
                                       ["unlink", "--client", "claude", "--force"]])


if __name__ == "__main__":
    unittest.main()
