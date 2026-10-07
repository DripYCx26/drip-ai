"""Runs repo scripts in a temporary repo copy with stub `terraform`, `aws`, and `curl` CLIs.

Real Terraform and AWS are never reached: the stubs come first on PATH, log every call, and
answer only from rules the test declares. A call that no rule matches fails with exit 97, so an
unexpected CLI call cannot pass silently.

    with StubRepo(files={"ec2/terraform/terraform.tfvars": 'model_source = "m"\\n'}) as repo:
        repo.stub("aws", "sts", "get-caller-identity", stdout="111122223333\\tarn:aws:iam::111122223333:user/a\\n")
        repo.stub("terraform", "output", "-raw", "instance_id", stdout="i-0abc")
        result = repo.run("ec2/server.sh", "status")
        calls = repo.calls()  # [Call(tool, argv, stdin, tfvars), ...] in call order
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STUB_TOOLS = ("terraform", "aws", "curl")
NO_RULE_EXIT = 97
REAL_SLEEP = shutil.which("sleep") or "/bin/sleep"

# Not copied into a StubRepo: version-control data, provider plugins, bytecode, and Terraform
# state and variable files, which hold the operator's real values and would leak into tests.
_NEVER_COPIED = shutil.ignore_patterns(
    ".git", ".terraform", "__pycache__", ".DS_Store", "*.tfstate", "*.tfstate.*", "*.tfplan", "*.tfvars*"
)


def _ignored_in_copy(directory: str, names: list) -> set:
    """Skips `_NEVER_COPIED` names everywhere and the local model weights at the repo root."""
    skipped = set(_NEVER_COPIED(directory, names))
    if Path(directory) == REPO_ROOT:
        skipped.add("models")
    return skipped


# Stands in for `sleep`: logs the seconds asked for and returns at once. Waits under a second (a
# process-stop poll) still sleep.
SLEEP_SOURCE = """#!/bin/sh
printf '%s\\n' "$*" >> {log}
case "$1" in
  0.* | .*) exec {sleep} "$1" ;;
esac
"""

# The stub reads stdin only when a candidate rule matches on stdin, because a real `terraform
# apply` that a test does not script must not swallow input meant for a later prompt.
STUB_SOURCE = """#!{python} -I
import json, os, sys
TOOL, STATE, TFVARS = {tool!r}, {state!r}, {tfvars!r}

def main():
    argv = sys.argv[1:]
    with open(STATE + "/rules.json") as f:
        rules = json.load(f)
    with open(STATE + "/hits.json") as f:
        hits = json.load(f)
    candidates = [
        i for i, rule in enumerate(rules)
        if rule["tool"] == TOOL
        and all(token in argv for token in rule["argv"])
        and (rule["times"] is None or hits.get(str(i), 0) < rule["times"])
    ]
    stdin = None
    if any(rules[i]["stdin"] is not None for i in candidates):
        stdin = sys.stdin.read()
        candidates = [i for i in candidates if rules[i]["stdin"] in (None, stdin)]
    try:
        with open(TFVARS, newline="") as f:
            tfvars = f.read()
    except OSError:
        tfvars = None
    body = None
    if "--body" in argv[:-1]:
        try:
            with open(argv[argv.index("--body") + 1]) as f:
                body = f.read()
        except OSError:
            pass
    with open(STATE + "/calls.jsonl", "a") as f:
        f.write(json.dumps({{"tool": TOOL, "argv": argv, "stdin": stdin, "tfvars": tfvars, "env": dict(os.environ), "body": body}}) + "\\n")
    if not candidates:
        sys.stderr.write("stub " + TOOL + ": no rule for " + json.dumps(argv) + "\\n")
        return {no_rule_exit}
    rule = rules[candidates[0]]
    hits[str(candidates[0])] = hits.get(str(candidates[0]), 0) + 1
    # Replaced whole, so a stub running at the same time never reads a half-written file.
    scratch = STATE + "/hits.json." + str(os.getpid())
    with open(scratch, "w") as f:
        json.dump(hits, f)
    os.replace(scratch, STATE + "/hits.json")
    if rule.get("last_arg_file") is not None:
        with open(argv[-1], "w") as f:
            f.write(rule["last_arg_file"])
    sys.stdout.write(rule["stdout"])
    sys.stderr.write(rule["stderr"])
    return rule["exit"]

sys.exit(main())
"""


@dataclass(frozen=True)
class Call:
    """One stub invocation. `stdin` is None unless a rule matched on stdin; `tfvars` is the
    content of the file `StubRepo(tfvars=...)` seeds at call time (None when absent); `env` is
    the stub's environment; `body` is the content of the file after `--body` (None when absent)."""

    tool: str
    argv: list
    stdin: Optional[str]
    tfvars: Optional[str]
    env: Optional[dict] = None
    body: Optional[str] = None


class StubRepo:
    """A temp copy of the repo tree (see `_ignored_in_copy`) plus stub CLIs; use as a context manager.

    `tfvars` is written verbatim (CRLF kept) to `tfvars_path` when not None. No script reads
    that path, so a test seeds it only to assert that a script leaves it untouched.
    `files` maps repo-relative paths to text, for example a stack's `ec2/terraform/terraform.tfvars`,
    its `.terraform/terraform.tfstate`, or model folders under models/.
    """

    def __init__(self, tfvars: Optional[str] = None, files: Optional[Mapping[str, str]] = None):
        self._tmp = tempfile.TemporaryDirectory(prefix="drip-ai-stub-")
        base = Path(self._tmp.name)
        self.root = base / "repo"
        self.home = base / "home"
        self._bin = base / "bin"
        self._state = base / "state"
        for directory in (self.home, self._bin, self._state):
            directory.mkdir(parents=True)
        shutil.copytree(REPO_ROOT, self.root, symlinks=True, ignore=_ignored_in_copy)
        self.tfvars_path = self.root / "infra" / "terraform.tfvars"
        if tfvars is not None:
            self.write(self.tfvars_path, tfvars)
        for relative, text in (files or {}).items():
            self.write(self.root / relative, text)
        self._rules: list = []
        self._tools = set(STUB_TOOLS)
        self._save_rules()
        (self._state / "hits.json").write_text("{}")
        (self._state / "calls.jsonl").write_text("")
        for tool in STUB_TOOLS:
            self._write_stub(self._bin / tool, tool)

    def _write_stub(self, path: Path, tool: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            STUB_SOURCE.format(
                python=sys.executable,
                tool=tool,
                state=str(self._state),
                tfvars=str(self.tfvars_path),
                no_rule_exit=NO_RULE_EXIT,
            )
        )
        path.chmod(0o755)

    def install_stub(self, relative: str, tool: str) -> None:
        """Replaces the repo file at `relative` with a stub that logs and answers as `tool`, for a
        program run by path (`python3 -I shared/lib/codex_link.py ...`) rather than from PATH."""
        self._write_stub(self.root / relative, tool)
        self._tools.add(tool)

    def skip_sleeps(self) -> None:
        """Puts SLEEP_SOURCE first on PATH; `sleeps()` lists the seconds asked for. A fake that must
        keep running uses REAL_SLEEP."""
        path = self._bin / "sleep"
        path.write_text(SLEEP_SOURCE.format(log=shlex.quote(str(self._state / "sleeps.log")),
                                            sleep=shlex.quote(REAL_SLEEP)))
        path.chmod(0o755)

    def sleeps(self) -> list:
        path = self._state / "sleeps.log"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def bin_dir(self) -> Path:
        """The directory of the stub CLIs, first on the scripts' PATH."""
        return self._bin

    def __enter__(self) -> "StubRepo":
        return self

    def __exit__(self, *exc_info) -> None:
        self._tmp.cleanup()

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            f.write(text)

    def stub(
        self,
        tool: str,
        *argv: str,
        stdout: str = "",
        stderr: str = "",
        exit: int = 0,
        stdin: Optional[str] = None,
        times: Optional[int] = None,
        last_arg_file: Optional[str] = None,
    ) -> None:
        """Answers a `tool` call whose argv contains every token in `argv` (any order).

        Rules match in the order added; the first wins. `stdin`, when set, must equal the call's
        whole stdin. `times` retires the rule after that many matches, which scripts a value
        that changes between calls. `last_arg_file`, when set, is written to the path in the
        call's last argument, as `aws s3api get-object ... OUTFILE` does.
        """
        if tool not in self._tools:
            raise ValueError(f"no stub for {tool}")
        self._rules.append(
            {
                "tool": tool,
                "argv": list(argv),
                "stdout": stdout,
                "stderr": stderr,
                "exit": exit,
                "stdin": stdin,
                "times": times,
                "last_arg_file": last_arg_file,
            }
        )
        self._save_rules()

    def run(
        self,
        script: str,
        *args: str,
        input: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess:
        """Runs a repo-relative script with a minimal environment (no AWS_* or TF_VAR_*)."""
        return subprocess.run(
            ["bash", str(self.root / script), *args],
            cwd=self.root,
            env=self._env(env),
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def popen(self, script: str, *args: str, env: Optional[Mapping[str, str]] = None, **kwargs) -> subprocess.Popen:
        """Starts a repo-relative script like `run`, for tests that signal or read it while it runs."""
        return subprocess.Popen(
            ["bash", str(self.root / script), *args], cwd=self.root, env=self._env(env), **kwargs
        )

    def _env(self, extra: Optional[Mapping[str, str]]) -> dict:
        run_env = {
            "PATH": f"{self._bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "TMPDIR": str(self.home),
        }
        run_env.update(extra or {})
        return run_env

    def calls(self) -> list:
        with open(self._state / "calls.jsonl") as f:
            return [Call(**json.loads(line)) for line in f if line.strip()]

    def tfvars(self) -> str:
        with open(self.tfvars_path, newline="") as f:
            return f.read()

    def _save_rules(self) -> None:
        (self._state / "rules.json").write_text(json.dumps(self._rules))
