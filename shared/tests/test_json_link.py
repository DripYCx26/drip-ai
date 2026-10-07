"""Pins `shared/lib/json_link.py`: Claude Code and opencode links restore exactly and keep the key
off argv, and off disk except opencode's 0600 key file."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "json_link.py"


def load_json_link():
    spec = importlib.util.spec_from_file_location("json_link", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


json_link = load_json_link()

KEY = "sk-CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyzAB"
AUTH_ARGS = ["ssm", "get-parameter", "--region", "us-east-1", "--name", "/drip-ai/api-key",
             "--with-decryption", "--query", "Parameter.Value", "--output", "text"]
CLAUDE_ORIGINAL = '{\n    "theme": "dark",\n    "env": {"FOO": "1", "ANTHROPIC_MODEL": "opus"}\n}'
OPENCODE_ORIGINAL = '{"$schema": "https://opencode.ai/config.json", "model": "anthropic/claude-sonnet-4-5"}\n'


def link_argv(client, model="drip-coder", owner_pid=None):
    argv = ["link", "--client", client, "--model", model, "--port", "8000",
            "--context-window", "131072", "--owner-pid", str(owner_pid or os.getpid())]
    if client == "claude":
        argv += ["--auth-command", "/bin/sh", *[f"--auth-arg={arg}" for arg in AUTH_ARGS]]
    return argv


class HomeCase(unittest.TestCase):
    client = "claude"

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        if self.client == "claude":
            self.dir = self.home / ".claude"
            self.config = self.dir / "settings.json"
        else:
            self.dir = self.home / ".config" / "opencode"
            self.config = self.dir / "opencode.json"
        self.key_file = self.home / ".config" / "drip-ai" / "opencode-key"
        self.dir.mkdir(parents=True)

    def run_tool(self, *argv, stdin=KEY + "\n"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = json_link.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def link(self, **kwargs):
        code, out, err = self.run_tool(*link_argv(self.client, **kwargs))
        self.assertEqual((code, err), (0, ""), out)
        return out

    def unlink(self):
        code, out, err = self.run_tool("unlink", "--client", self.client)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def data(self):
        return json.loads(self.config.read_text())

    def assert_no_link_files(self):
        for name in ("drip-ai-link.json", "drip-ai-link.lock"):
            self.assertFalse((self.dir / name).exists(), name)
        self.assertFalse(self.key_file.exists())


class ClaudeLinkTest(HomeCase):
    client = "claude"

    def test_link_writes_the_documented_keys_and_unlink_restores_the_bytes(self):
        self.config.write_text(CLAUDE_ORIGINAL)
        self.link()
        data = self.data()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"], {
            "FOO": "1",
            "ANTHROPIC_MODEL": "drip-coder",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "drip-coder",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "drip-coder",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "drip-coder",
            "CLAUDE_CODE_SUBAGENT_MODEL": "drip-coder",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "131072",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "117964",
        })
        self.assertEqual(data["apiKeyHelper"], "/bin/sh " + " ".join(AUTH_ARGS))
        for name in ("drip-ai-link.json", "drip-ai-link.lock"):
            self.assertEqual(stat.S_IMODE((self.dir / name).stat().st_mode), 0o600)
        self.unlink()
        self.assertEqual(self.config.read_text(), CLAUDE_ORIGINAL)
        self.assert_no_link_files()

    def test_a_change_made_after_linking_is_kept(self):
        self.config.write_text(CLAUDE_ORIGINAL)
        self.link()
        data = self.data()
        data["env"]["ANTHROPIC_MODEL"] = "mine"
        data["permissions"] = {"allow": ["Bash(ls)"]}
        self.config.write_text(json.dumps(data))
        out = self.unlink()
        self.assertIn("kept your change to env.ANTHROPIC_MODEL", out)
        self.assertEqual(self.data(), {"theme": "dark", "env": {"FOO": "1", "ANTHROPIC_MODEL": "mine"},
                                       "permissions": {"allow": ["Bash(ls)"]}})

    def test_a_missing_settings_file_is_created_then_removed(self):
        self.link()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.unlink()
        self.assertFalse(self.config.exists())
        self.assertTrue(self.dir.is_dir())

    def test_jsonc_is_refused_with_the_keys_to_add_by_hand(self):
        jsonc = '{\n  // my settings\n  "theme": "dark",\n}\n'
        self.config.write_text(jsonc)
        code, _, err = self.run_tool(*link_argv("claude"))
        self.assertEqual(code, 1)
        self.assertIn("settings.json is not plain JSON", err)
        self.assertIn('"ANTHROPIC_BASE_URL": "http://127.0.0.1:8000"', err)
        self.assertIn('"apiKeyHelper": "/bin/sh ssm get-parameter', err)
        self.assertEqual(self.config.read_text(), jsonc)
        self.assert_no_link_files()

    def test_claude_config_dir_is_honored(self):
        other = self.home / "claude-work"
        other.mkdir()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.link()
            self.assertTrue((other / "settings.json").exists())
            self.assertFalse(self.config.exists())
            self.unlink()

    def test_a_missing_claude_folder_is_skipped(self):
        shutil.rmtree(self.dir)
        code, out, err = self.run_tool(*link_argv("claude"))
        self.assertEqual((code, out, err), (0, "Claude Code config not found; skipping\n", ""))
        self.assertFalse(self.dir.exists())

    def test_a_live_owner_blocks_and_a_stale_one_is_taken_over(self):
        self.config.write_text(CLAUDE_ORIGINAL)
        self.link(owner_pid=os.getpid())
        linked = self.config.read_bytes()
        code, _, err = self.run_tool(*link_argv("claude", model="other", owner_pid=os.getppid()))
        self.assertEqual(code, 1)
        self.assertIn(f"Claude Code is already linked by pid {os.getpid()}", err)
        self.assertEqual(self.config.read_bytes(), linked)
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        lock = self.dir / "drip-ai-link.lock"
        lock.write_text(json.dumps({"pid": dead.pid, "host": json_link.socket.gethostname()}))
        out = self.link(model="drip-coder-2")
        self.assertIn(f"took over a stale link from pid {dead.pid}", out)
        self.assertIn("repaired the previous link", out)
        self.assertEqual(self.data()["env"]["ANTHROPIC_MODEL"], "drip-coder-2")
        self.unlink()
        self.assertEqual(self.config.read_text(), CLAUDE_ORIGINAL)

    def test_a_credential_that_outranks_apikeyhelper_is_warned_about(self):
        self.config.write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "x"}}')
        code, _, err = self.run_tool(*link_argv("claude"))
        self.assertEqual(code, 0)
        self.assertIn("settings.json env sets ANTHROPIC_AUTH_TOKEN", err)

    def test_a_damaged_record_needs_force(self):
        self.config.write_text(CLAUDE_ORIGINAL)
        self.link()
        (self.dir / "drip-ai-link.json").write_text("{")
        code, _, err = self.run_tool("unlink", "--client", "claude")
        self.assertEqual(code, 1)
        self.assertIn("drip-ai-link.json is damaged; run unlink --force", err)
        code, out, _ = self.run_tool("unlink", "--client", "claude", "--force")
        self.assertEqual(code, 0)
        self.assertIn("without restoring", out)
        self.assert_no_link_files()


class OpencodeLinkTest(HomeCase):
    client = "opencode"

    def test_link_writes_the_provider_and_unlink_restores_the_bytes(self):
        self.config.write_text(OPENCODE_ORIGINAL)
        self.link()
        data = self.data()
        self.assertEqual(data["model"], "drip-ai/drip-coder")
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")
        self.assertEqual(data["provider"], {"drip-ai": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "drip-ai",
            "options": {"baseURL": "http://127.0.0.1:8000/v1", "apiKey": f"{{file:{self.key_file}}}"},
            "models": {"drip-coder": {"name": "drip-coder", "tool_call": True, "reasoning": True,
                                      "limit": {"context": 131072, "output": 16384}}},
        }})
        self.unlink()
        self.assertEqual(self.config.read_text(), OPENCODE_ORIGINAL)
        self.assert_no_link_files()

    def test_the_key_file_is_private_while_linked_and_gone_after(self):
        self.link()
        self.assertEqual(self.key_file.read_text(), KEY)  # opencode reads it whole: no newline
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.key_file.parent.stat().st_mode), 0o700)
        for path in (self.config, self.dir / "drip-ai-link.json"):
            self.assertNotIn(KEY, path.read_text())
        self.unlink()
        self.assertFalse(self.key_file.exists())
        self.assertFalse(self.key_file.parent.exists())

    def test_unlink_without_a_record_still_deletes_a_left_key(self):
        self.link()
        (self.dir / "drip-ai-link.json").unlink()  # as after a SIGKILL mid-unlink
        self.unlink()
        self.assertFalse(self.key_file.exists())

    def test_a_change_made_after_linking_is_kept(self):
        self.config.write_text(OPENCODE_ORIGINAL)
        self.link()
        data = self.data()
        data["model"] = "openai/gpt-5"
        data["theme"] = "tokyonight"
        self.config.write_text(json.dumps(data))
        out = self.unlink()
        self.assertIn("kept your change to model", out)
        self.assertEqual(self.data(), {"$schema": "https://opencode.ai/config.json", "model": "openai/gpt-5",
                                       "theme": "tokyonight"})

    def test_a_missing_file_and_folder_are_created_then_removed_when_opencode_is_installed(self):
        shutil.rmtree(self.home / ".config")
        code, out, _ = self.run_tool(*link_argv("opencode"))
        self.assertEqual((code, out), (0, "opencode config not found; skipping\n"))
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "opencode").write_text("#!/bin/sh\n")
        (bin_dir / "opencode").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:/usr/bin:/bin"}):
            self.link()
            self.assertEqual(set(self.data()), {"provider", "model"})
            self.unlink()
        self.assertFalse(self.dir.exists())
        self.assertFalse(self.key_file.exists())

    def test_xdg_config_home_is_honored(self):
        xdg = self.home / "xdg"
        (xdg / "opencode").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            self.link()
            config = json.loads((xdg / "opencode" / "opencode.json").read_text())
            self.assertEqual(config["provider"]["drip-ai"]["options"]["apiKey"],
                             f"{{file:{xdg / 'drip-ai' / 'opencode-key'}}}")
            self.unlink()
        self.assertFalse((xdg / "opencode" / "opencode.json").exists())

    def test_a_hand_written_provider_or_a_jsonc_file_is_refused(self):
        cases = {
            "hand-written provider": ("opencode.json", '{"provider": {"drip-ai": {"npm": "x"}}}',
                                      "already defines provider.drip-ai and drip-ai did not write it"),
            # The snippet to paste reads the key from the environment, since no key file is written.
            "comments": ("opencode.json", '{\n  // mine\n  "model": "a/b"\n}\n', '"apiKey": "{env:DRIP_AI_API_KEY}"'),
            "duplicate keys": ("opencode.json", '{"model": "a/b", "model": "c/d"}', "opencode.json is not plain JSON"),
            "opencode.jsonc beside it": ("opencode.jsonc", "{}", "opencode.jsonc exists and wins over opencode.json"),
        }
        for label, (name, text, message) in cases.items():
            with self.subTest(label):
                (self.dir / name).write_text(text)
                code, _, err = self.run_tool(*link_argv("opencode"))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual((self.dir / name).read_text(), text)
                self.assert_no_link_files()
                (self.dir / name).unlink()

    def test_a_live_owner_blocks_and_a_stale_one_is_taken_over(self):
        self.link(owner_pid=os.getpid())
        code, _, err = self.run_tool(*link_argv("opencode", owner_pid=os.getppid()))
        self.assertEqual(code, 1)
        self.assertIn(f"opencode is already linked by pid {os.getpid()}", err)
        self.assertEqual(self.key_file.read_text(), KEY)
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        (self.dir / "drip-ai-link.lock").write_text(
            json.dumps({"pid": dead.pid, "host": json_link.socket.gethostname()}))
        self.assertIn(f"took over a stale link from pid {dead.pid}", self.link(model="other"))
        self.unlink()
        self.assertFalse(self.config.exists())
        self.assert_no_link_files()

    def test_a_malformed_key_on_stdin_writes_nothing(self):
        code, _, err = self.run_tool(*link_argv("opencode"), stdin='sk-"quoted\n')
        self.assertEqual(code, 1)
        self.assertIn("the API key on stdin has an unexpected form", err)
        self.assertFalse(self.config.exists())
        self.assert_no_link_files()


if __name__ == "__main__":
    unittest.main()
