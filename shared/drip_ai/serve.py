"""Port and registry policy for the tunnels, and the way to run one detached.

    serve.py prepare --backend sagemaker|ec2 --region R --endpoint NAME --pid PID
    serve.py holder --port P
    serve.py region --port P
    serve.py list [--json]
    serve.py stop (--port P | --endpoint NAME)
    serve.py detach --log NAME -- COMMAND...

A tunnel (shared/lib/common.sh tunnel_with_link, run by `sagemaker tunnel` and `ec2 tunnel`) asks
`prepare` for its port before it starts the proxy: a port a registered drip-ai tunnel already serves
for the same endpoint is reused (the new tunnel exits at once); one another process holds is
refused, naming its pid and command, unless ports.auto takes the next free port in
ports.base..ports.base+ports.range-1. `prepare` registers the tunnel's shell, whose pid the client
links name as their owner, and picks its opencode provider: drip-ai, or drip-ai-<region> while
another tunnel runs. A process is stopped only when it is registered and its start time still
matches the record; anything else is reported, never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from drip_ai import config, conventions, procs, state_dir  # noqa: E402

LOG_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def fail(message: str) -> int:
    print(f"drip-ai: {message}", file=sys.stderr)
    return 1


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            return True
    return False


def holder_text(port: int) -> str:
    found = procs.listeners(port, port)
    if not found:
        return ""
    first = found[0]
    return f"pid {first.pid} ({first.command})"


def prepare(args: argparse.Namespace, environ: dict) -> int:
    cfg = config.resolve(environ)
    base, span, auto = cfg["ports.base"], cfg["ports.range"], cfg["ports.auto"]
    live = [record for record, ok in procs.records(environ) if ok and record.get("pid") != args.pid]
    candidates = list(range(base, base + span)) if auto else [base]
    chosen = None
    for port in candidates:
        tunnel = next((r for r in live if r.get("port") == port), None)
        if tunnel and tunnel.get("endpoint") == args.endpoint and tunnel.get("region") == args.region:
            print(f"reuse=pid {tunnel['pid']} on 127.0.0.1:{port}")
            return 0
        if tunnel:
            if not auto:
                return fail(f"port {port} is held by the drip-ai tunnel pid {tunnel['pid']} for {tunnel.get('endpoint')}"
                            f" ({tunnel.get('region')}); set DRIP_AI_PORT, or run: bin/drip-ai config set ports.auto=true")
            continue
        if not port_free(port):
            if not auto:
                holder = holder_text(port) or "another process"
                return fail(f"port {port} is held by {holder}, which drip-ai did not start; set DRIP_AI_PORT, or run:"
                            " bin/drip-ai config set ports.auto=true to take the next free port")
            continue
        chosen = port
        break
    if chosen is None:
        return fail(f"every port from {base} to {base + span - 1} is in use; raise ports.range or free one"
                    " (bin/drip-ai doctor names the holders)")
    providers = {r.get("provider") for r in live}
    provider = "drip-ai" if not live else f"drip-ai-{args.region}"
    if provider in providers:
        provider = f"drip-ai-{args.region}-{chosen}"
    record = procs.register(environ, args.pid, ["tunnel", args.backend, args.region, args.endpoint, str(chosen)],
                            port=chosen, endpoint=args.endpoint, region=args.region, backend=args.backend,
                            provider=provider)
    print(f"port={chosen}")
    print(f"provider={provider}")
    print(f"record={record}")
    print(f"default_model={'yes' if cfg['clients.opencode.set_default_model'] else 'no'}")
    return 0


def list_command(args: argparse.Namespace, environ: dict) -> int:
    rows = [{**record, "live": ok} for record, ok in procs.records(environ)]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No drip-ai tunnel is registered on this machine.")
    for row in rows:
        state = "running" if row["live"] else "gone (stale record)"
        print(f"pid {row.get('pid')}  127.0.0.1:{row.get('port')}  {row.get('endpoint')} ({row.get('region')})"
              f"  provider {row.get('provider')}  {state}")
    return 0


def stop_command(args: argparse.Namespace, environ: dict) -> int:
    matches = [(r, ok) for r, ok in procs.records(environ)
               if (args.port is not None and r.get("port") == args.port)
               or (args.endpoint is not None and r.get("endpoint") == args.endpoint)]
    if not matches:
        return fail("no registered drip-ai tunnel matches; bin/drip-ai serve list shows them")
    status = 0
    for record, ok in matches:
        if ok and procs.stop(record):
            print(f"Stopped the tunnel pid {record['pid']} on 127.0.0.1:{record['port']}; it unlinked its clients.")
        else:
            print(f"The tunnel record for pid {record.get('pid')} is stale; removed it.")
        procs.unregister(record["path"])
    return status


def detach(args: argparse.Namespace, environ: dict) -> int:
    """Runs COMMAND in its own session with its output in the state folder; returns at once."""
    if not args.log or not set(args.log) <= LOG_NAME_CHARS:
        return fail("--log takes lowercase letters, digits, and hyphens")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        return fail("detach needs a command after --")
    log = state_dir(environ, "procs") / f"{args.log}.log"
    pid = os.fork()
    if pid:
        print(f"Started in the background (pid {pid}); its log is {log}.")
        return 0
    os.setsid()
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.execvp(command[0], command)
    return 1


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="serve.py", description="Tunnel ports, registry, and detach.")
    commands = parser.add_subparsers(dest="command_name", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--backend", choices=("sagemaker", "ec2"), required=True)
    prep.add_argument("--region", required=True)
    prep.add_argument("--endpoint", required=True)
    prep.add_argument("--pid", type=int, required=True)
    hold = commands.add_parser("holder")
    hold.add_argument("--port", type=int, required=True)
    where = commands.add_parser("region")
    where.add_argument("--port", type=int, required=True)
    lst = commands.add_parser("list")
    lst.add_argument("--json", action="store_true")
    stp = commands.add_parser("stop")
    target = stp.add_mutually_exclusive_group(required=True)
    target.add_argument("--port", type=int)
    target.add_argument("--endpoint")
    det = commands.add_parser("detach")
    det.add_argument("--log", required=True)
    det.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    try:
        if args.command_name == "prepare":
            conventions.check_region(args.region)
            return prepare(args, environ)
        if args.command_name == "holder":
            text = holder_text(args.port)
            if text:
                print(text)
            return 0 if text else 1
        if args.command_name == "region":
            region = next((r.get("region") for r, ok in procs.records(environ) if ok and r.get("port") == args.port), None)
            if region:
                print(region)
            return 0 if region else 1
        if args.command_name == "list":
            return list_command(args, environ)
        if args.command_name == "stop":
            return stop_command(args, environ)
        return detach(args, environ)
    except (config.ConfigError, ValueError, OSError) as error:
        return fail(str(error))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
