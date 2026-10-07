"""Pins `shared/lib/codex_link.py`: linking Codex keeps every other byte and never holds a key."""

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

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "codex_link.py"


def load_codex_link():
    spec = importlib.util.spec_from_file_location("codex_link", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


codex_link = load_codex_link()
link_files = sys.modules["link_files"]  # codex_link imports it; swaps read through it

AUTH_COMMAND = "/bin/sh"  # any absolute executable; the tool never runs it
AUTH_ARGS = [
    "ssm", "get-parameter", "--name", "/drip-ai/api-key", "--with-decryption",
    "--query", "Parameter.Value", "--output", "text", "--region", "us-east-1",
]
CANARY_KEY = "CanaryApiKey0123456789abcdefghijklmnopqrstuvwxyzAB"  # 48+ letters and digits
ORIGINAL = (
    "# Codex settings\n"
    'model = "gpt-5-codex"\n'
    'approval_policy = "on-request"\n'
    "notify = [\n"
    '  "notify-send",\n'
    '  "[not a header]",\n'
    "]\n"
    'instructions = """\n'
    "[fake.header]\n"
    'model = "nope"\n'
    '"""\n'
    "\n"
    "[desktop]\n"
    'theme = "dark"\n'
    "\n"
    '[projects."/a b"]\n'
    'trust_level = "trusted"\n'
    "\n"
    "# MCP servers\n"
    "[mcp_servers.x]\n"
    'command = "npx"\n'
    'args = ["-y", "x"]\n'
)
APP_TABLE = '\n[projects."z"]\ntrust_level = "trusted"\n'


def find_tomllib_python():
    for name in ("python3.13", "python3.12", "python3.11", "python3"):
        path = shutil.which(name)
        if path and subprocess.run([path, "-I", "-c", "import tomllib"]).returncode == 0:
            return path
    return None


TOMLLIB_PYTHON = find_tomllib_python()


def toml_load(data):
    """Parse with a Python 3.11+ tomllib, or None when no such interpreter exists."""
    if TOMLLIB_PYTHON is None:
        return None
    script = "import json, sys, tomllib; print(json.dumps(tomllib.load(sys.stdin.buffer)))"
    result = subprocess.run(
        [TOMLLIB_PYTHON, "-I", "-c", script], input=data, capture_output=True, check=True
    )
    return json.loads(result.stdout)


def link_argv(model="drip-coder", owner_pid=None, auth_args=AUTH_ARGS, command=AUTH_COMMAND):
    argv = [
        "link", "--provider", "drip-ai", "--model", model,
        "--base-url", "http://127.0.0.1:8000/v1", "--context-window", "131072",
        "--auth-command", command, "--owner-pid", str(owner_pid or os.getpid()),
    ]
    return argv + [f"--auth-arg={arg}" for arg in auth_args]


class CodexHomeCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.home = self.root / "codex"
        self.home.mkdir(mode=0o700)
        self.config = self.home / "config.toml"
        self.sidecar = self.home / "drip-ai-link.json"
        self.lock = self.home / "drip-ai-link.lock"
        env = {"CODEX_HOME": str(self.home), "HOME": str(self.root / "user")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = codex_link.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def write_config(self, text, mode=0o600):
        self.config.write_bytes(text.encode())
        os.chmod(self.config, mode)

    def link(self, **kwargs):
        code, out, err = self.run_tool(*link_argv(**kwargs))
        self.assertEqual((code, err), (0, ""), out)
        return out

    def unlink(self, *extra):
        code, out, err = self.run_tool("unlink", *extra)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def assert_no_link_files(self):
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(self.lock.exists())


class RoundTripTest(CodexHomeCase):
    def test_link_unlink_round_trip_is_byte_identical(self):
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=repr(eol)):
                original = ORIGINAL.replace("\n", eol).encode()
                self.config.write_bytes(original)
                self.link()
                linked = self.config.read_bytes()
                text = linked.decode()
                self.assertEqual(linked.count(b"\n"), linked.count(eol.encode()))
                self.assertIn(f'model = "drip-coder"{eol}approval_policy', text)
                top_level = text[: text.index("[desktop]")]
                self.assertIn(
                    f']{eol}instructions = """{eol}[fake.header]{eol}model = "nope"{eol}"""{eol}'
                    f'{eol}model_provider = "drip-ai"{eol}model_context_window = 131072{eol}'
                    f"model_auto_compact_token_limit = 117964{eol}",
                    top_level,
                )
                self.assertTrue(text.endswith(f"refresh_interval_ms = 300000{eol}"))
                self.assertEqual(linked.replace(b"\r\n", b"\n").count(b"\n[model_providers"), 2)
                self.unlink()
                self.assertEqual(self.config.read_bytes(), original)
                self.assert_no_link_files()

    def test_previous_values_absent_are_removed_on_unlink(self):
        for original in ('[desktop]\ntheme = "dark"\n', 'approval_policy = "never"', ""):
            with self.subTest(original=original):
                self.write_config(original)
                self.link()
                linked = self.config.read_text()
                for line in ('model = "drip-coder"\n', 'model_provider = "drip-ai"\n'):
                    self.assertIn(line, linked)
                if original.startswith("[desktop]"):
                    self.assertLess(linked.index("model_context_window"), linked.index("[desktop]"))
                record = json.loads(self.sidecar.read_text())
                self.assertEqual(set(record["previous"].values()), {None})
                self.unlink()
                self.assertEqual(self.config.read_text(), original)

    def test_user_changed_model_is_kept(self):
        self.write_config(ORIGINAL)
        self.link()
        self.write_config(self.config.read_text().replace('model = "drip-coder"', 'model = "o3"'))
        out = self.unlink()
        self.assertIn("kept your change to model", out)
        self.assertIn("restored model_provider, model_context_window, model_auto_compact_token_limit", out)
        self.assertEqual(self.config.read_text(), ORIGINAL.replace('"gpt-5-codex"', '"o3"'))

    def test_app_appended_table_survives_unlink(self):
        self.write_config(ORIGINAL)
        self.link()
        with open(self.config, "a") as handle:
            handle.write(APP_TABLE)
        out = self.unlink()
        self.assertIn("removed [model_providers.drip-ai]", out)
        self.assertEqual(self.config.read_text(), ORIGINAL + APP_TABLE)

    def test_app_reflow_without_blank_line_keeps_lines_apart(self):
        self.write_config(ORIGINAL)
        self.link()
        linked = self.config.read_text()
        reflowed = linked.replace('"x"]\n\n[model_providers', '"x"]\n[model_providers')
        self.assertNotEqual(reflowed, linked)
        self.write_config(reflowed + APP_TABLE)
        self.unlink()
        self.assertEqual(self.config.read_text(), ORIGINAL + APP_TABLE)

    def test_changed_provider_table_is_kept_whole(self):
        self.write_config(ORIGINAL)
        self.link()
        edited = self.config.read_text().replace("timeout_ms = 15000", "timeout_ms = 9000")
        self.write_config(edited)
        out = self.unlink()
        self.assertIn("kept [model_providers.drip-ai] because it changed", out)
        text = self.config.read_text()
        self.assertIn("[model_providers.drip-ai]\n", text)
        self.assertIn("[model_providers.drip-ai.auth]\n", text)
        self.assertTrue(text.startswith(ORIGINAL))


class RefusalTest(CodexHomeCase):
    def assert_refused(self, argv, fragment):
        before = sorted(p.name for p in self.home.iterdir())
        config = self.config.read_bytes() if self.config.exists() else None
        code, out, err = self.run_tool(*argv)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(fragment, err)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), before)
        self.assertEqual(self.config.read_bytes() if config is not None else None, config)

    def test_handwritten_provider_refuses_unchanged(self):
        handwritten = (
            ORIGINAL + '[model_providers.drip-ai]\nname = "m"\nbase_url = "http://127.0.0.1:9"\n',
            ORIGINAL + '[model_providers]\ndrip-ai = { name = "mine" }\n',
            'model_providers = { drip-ai = { name = "mine" } }\n' + ORIGINAL,
            'model_providers.drip-ai.name = "mine"\n' + ORIGINAL,
        )
        for text in handwritten:
            with self.subTest(text=text):
                self.write_config(text)
                self.assert_refused(link_argv(), "drip-ai did not write it")

    def test_other_providers_table_is_not_a_refusal(self):
        self.write_config(ORIGINAL + '[model_providers]\n\n[model_providers.other]\nname = "o"\n')
        self.link()

    def test_non_simple_managed_key_refuses(self):
        for line in ('model = """gpt"""\n', 'model = "a"\nmodel = "b"\n', '"model" = "gpt"\n'):
            with self.subTest(line=line):
                self.write_config(line + "[desktop]\n")
                self.assert_refused(link_argv(), "model")

    def test_symlink_refuses(self):
        target = self.root / "dotfiles.toml"
        target.write_text(ORIGINAL)
        self.config.symlink_to(target)
        self.assert_refused(link_argv(), "symlink")
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.read_text(), ORIGINAL)

    def test_no_file_under_codex_home_contains_key(self):
        self.write_config(ORIGINAL)
        for arg in (f"sk-{CANARY_KEY}", CANARY_KEY, f"--value={CANARY_KEY}", "hf_abcdefgh1234"):
            with self.subTest(arg=arg):
                self.assert_refused(link_argv(auth_args=AUTH_ARGS + [arg]), "looks like a secret")
                for path in self.home.rglob("*"):
                    self.assertNotIn(CANARY_KEY.encode(), path.read_bytes())

    def test_auth_command_absolute_and_args_exact(self):
        self.write_config(ORIGINAL)
        self.assert_refused(link_argv(command="sh"), "absolute path")
        self.assert_refused(link_argv(command=str(self.root)), "not an executable file")
        tricky = AUTH_ARGS + ['say "hi" \\ there', "ünïcode", "--profile=task-definition"]
        self.link(auth_args=tricky)
        text = self.config.read_text()
        self.assertIn('command = "/bin/sh"\n', text)
        self.assertIn(
            'args = ["ssm", "get-parameter", "--name", "/drip-ai/api-key", "--with-decryption", '
            '"--query", "Parameter.Value", "--output", "text", "--region", "us-east-1", '
            '"say \\"hi\\" \\\\ there", "ünïcode", "--profile=task-definition"]\n',
            text,
        )
        parsed = toml_load(self.config.read_bytes())
        if parsed is not None:
            auth = parsed["model_providers"]["drip-ai"]["auth"]
            self.assertEqual(auth["args"], tricky)


class FileSafetyTest(CodexHomeCase):
    def test_mode_0600_kept(self):
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                self.write_config(ORIGINAL, mode)
                self.link()
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), mode)
                for name in ("drip-ai-link.json", "drip-ai-link.lock", "config.toml.drip-ai.bak"):
                    self.assertEqual(stat.S_IMODE((self.home / name).stat().st_mode), 0o600)
                self.assertEqual((self.home / "config.toml.drip-ai.bak").read_text(), ORIGINAL)
                self.unlink()
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), mode)
        self.config.unlink()
        self.link()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_cas_retry_on_concurrent_edit(self):
        self.write_config(ORIGINAL)
        real_read = link_files.read_config_bytes
        config_reads = []

        def read_with_app_edit(path):
            if Path(path) == self.config:
                config_reads.append(path)
                if len(config_reads) == 2:  # the re-read just before the first swap
                    with open(self.config, "a") as handle:
                        handle.write(APP_TABLE)
            return real_read(path)

        with mock.patch.object(link_files, "read_config_bytes", read_with_app_edit):
            self.link()
        self.assertEqual(len(config_reads), 4)
        text = self.config.read_text()
        self.assertIn('model = "drip-coder"\n', text)
        self.assertIn(APP_TABLE, text)
        self.unlink()
        self.assertEqual(self.config.read_text(), ORIGINAL + APP_TABLE)

    def test_cas_gives_up_without_writing_when_edits_never_stop(self):
        self.write_config(ORIGINAL)
        real_read = link_files.read_config_bytes
        reads = []

        def read_with_endless_edits(path):
            if Path(path) == self.config:
                reads.append(path)
                if len(reads) % 2 == 0:
                    with open(self.config, "a") as handle:
                        handle.write(f"# app edit {len(reads)}\n")
            return real_read(path)

        with mock.patch.object(link_files, "read_config_bytes", read_with_endless_edits):
            code, _, err = self.run_tool(*link_argv())
        self.assertEqual(code, 1)
        self.assertIn("kept changing", err)
        edits = "# app edit 2\n# app edit 4\n# app edit 6\n"
        self.assertEqual(self.config.read_text(), ORIGINAL + edits)
        self.assert_no_link_files()


class OwnershipTest(CodexHomeCase):
    def test_live_pid_refuses_second_link(self):
        self.write_config(ORIGINAL)
        self.link(owner_pid=os.getpid())
        linked = self.config.read_bytes()
        code, _, err = self.run_tool(*link_argv(model="other", owner_pid=os.getppid()))
        self.assertEqual(code, 1)
        self.assertIn(f"already linked by pid {os.getpid()}", err)
        self.assertEqual(self.config.read_bytes(), linked)
        self.assertEqual(json.loads(self.lock.read_text())["pid"], os.getpid())

    def test_stale_pid_repairs(self):
        self.write_config(ORIGINAL)
        process = subprocess.Popen(["/usr/bin/true"])
        process.wait()
        self.link(owner_pid=process.pid)
        out = self.link(model="drip-coder-2", owner_pid=os.getpid())
        self.assertIn(f"took over a stale link from pid {process.pid}", out)
        self.assertIn("repaired the previous link", out)
        record = json.loads(self.sidecar.read_text())
        self.assertEqual(record["previous"]["model"], 'model = "gpt-5-codex"')
        self.assertEqual(self.config.read_text().count("[model_providers.drip-ai]"), 1)
        self.unlink()
        self.assertEqual(self.config.read_text(), ORIGINAL)

    def link_then_lose_sidecar(self, original=ORIGINAL):
        self.write_config(original)
        self.link()
        self.sidecar.unlink()
        return self.config.read_bytes().decode()

    def test_force_unlink_without_sidecar_clears_dangling_provider(self):
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=repr(eol)):
                original = ORIGINAL.replace("\n", eol)
                linked = self.link_then_lose_sidecar(original)
                self.assertIn("nothing to restore", self.unlink())
                self.assertEqual(self.config.read_bytes().decode(), linked)
                out = self.unlink("--force")
                self.assertEqual(
                    out.splitlines(),
                    [
                        "unlinked Codex by force: removed [model_providers.drip-ai]",
                        'removed model_provider = "drip-ai"',
                        'removed model = "drip-coder"',
                        "removed model_context_window = 131072",
                        "removed model_auto_compact_token_limit = 117964",
                        "Codex will use its built-in default model; "
                        "set your own model in config.toml if needed.",
                    ],
                )
                left = self.config.read_bytes().decode()
                self.assertEqual(
                    left.rstrip("\r\n"),
                    original.replace(f'model = "gpt-5-codex"{eol}', "").rstrip("\r\n"),
                )
                self.assert_no_link_files()

    def test_force_unlink_keeps_model_when_it_is_not_ours(self):
        linked = self.link_then_lose_sidecar()
        self.write_config(linked.replace('model = "drip-coder"', 'model = "o3"'))
        edited = self.config.read_bytes()
        for extra, removes_model in (([], False), (["--model", "o3"], True)):
            with self.subTest(extra=extra):
                self.config.write_bytes(edited)
                out = self.unlink("--force", *extra)
                text = self.config.read_text()
                self.assertNotIn("model_provider", text.replace("[model_providers", ""))
                self.assertNotIn("model_context_window", text)
                self.assertEqual('model = "o3"' in text, not removes_model)
                self.assertEqual('kept model: it is not exactly "drip-coder"' in out, not extra)
                self.assertEqual("built-in default model" in out, removes_model)
                self.assertIn('removed model_provider = "drip-ai"', out)

    def test_force_unlink_leaves_provider_pointing_elsewhere(self):
        linked = self.link_then_lose_sidecar()
        self.write_config(linked.replace('model_provider = "drip-ai"', 'model_provider = "other"'))
        out = self.unlink("--force")
        text = self.config.read_text()
        self.assertNotIn("[model_providers", text)
        for line in ('model_provider = "other"', 'model = "drip-coder"', "model_context_window"):
            self.assertIn(line, text)
        self.assertIn('kept model_provider: it is not exactly "drip-ai"', out)
        self.assertNotIn("removed model", out)
        self.assertNotIn("built-in default model", out)
        self.assert_no_link_files()

    def test_force_unlink_refuses_a_foreign_shape(self):
        foreign = (
            '[model_providers.drip-ai]\nname = "x"\nbase_url = "https://example.com/v1"\n'
            'wire_api = "responses"\n'
        )
        self.write_config(ORIGINAL + foreign)
        code, _, err = self.run_tool("unlink", "--force")
        self.assertEqual(code, 1)
        self.assertIn("not the shape drip-ai writes", err)
        self.assertEqual(self.config.read_text(), ORIGINAL + foreign)

    def test_status_reports_link_and_owner(self):
        self.write_config(ORIGINAL)
        self.link(owner_pid=os.getpid())
        _, out, _ = self.run_tool("status")
        owner = f"owner pid {os.getpid()} alive"
        self.assertEqual(out, f"linked; {owner}; model drip-coder; model_provider drip-ai\n")
        self.unlink()
        _, out, _ = self.run_tool("status")
        self.assertEqual(out, "unlinked; no owner; model gpt-5-codex; model_provider unset\n")


class MissingCodexTest(unittest.TestCase):
    def test_missing_codex_home_skips(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for env in ({"CODEX_HOME": str(root / "absent")}, {"CODEX_HOME": "", "HOME": str(root)}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = codex_link.main(link_argv())
                skipped = (0, "Codex config not found; skipping\n")
                self.assertEqual((code, stdout.getvalue()), skipped)
                self.assertEqual(list(root.iterdir()), [])


@unittest.skipIf(TOMLLIB_PYTHON is None, "no Python 3.11+ with tomllib on PATH")
class TomllibTest(CodexHomeCase):
    def run_cli(self, *argv):
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        return subprocess.run(
            [TOMLLIB_PYTHON, "-I", str(MODULE_PATH), *argv],
            env=env, capture_output=True, text=True, check=True,
        )

    def test_tomllib_parse_when_available(self):
        self.write_config(ORIGINAL)
        before = toml_load(ORIGINAL.encode())
        self.run_cli(*link_argv())  # the 3.11+ runtime also checks the result with tomllib
        parsed = toml_load(self.config.read_bytes())
        self.assertEqual(
            (parsed["model"], parsed["model_provider"], parsed["model_context_window"],
             parsed["model_auto_compact_token_limit"]),
            ("drip-coder", "drip-ai", 131072, 117964),
        )
        self.assertEqual(
            parsed["model_providers"]["drip-ai"],
            {
                "name": "drip-ai",
                "base_url": "http://127.0.0.1:8000/v1",
                "wire_api": "responses",
                "auth": {
                    "command": AUTH_COMMAND,
                    "args": AUTH_ARGS,
                    "timeout_ms": 15000,
                    "refresh_interval_ms": 300000,
                },
            },
        )
        for key in ("notify", "instructions", "desktop", "projects", "mcp_servers"):
            self.assertEqual(parsed[key], before[key])
        self.run_cli("unlink")
        self.assertEqual(self.config.read_text(), ORIGINAL)


if __name__ == "__main__":
    unittest.main()
