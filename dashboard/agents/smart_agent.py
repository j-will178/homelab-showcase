#!/usr/bin/env python3
"""
SMART agent for Linux and FreeBSD (OptiPlex, OPNsense).

Runs `smartctl --scan-open` and `smartctl -j -x` for every device and POSTs the
raw JSON to the rack dashboard. Standard library only. Needs root (smartctl
does) and smartmontools >= 7.0 for JSON output.

    smart_agent.py --url http://127.0.0.1:8080/api/smart --token-file /etc/rack-dashboard/agent-token --name optiplex-management
    smart_agent.py --dry-run          # collect and print a summary, don't send
"""
import argparse
import json
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "smart_agent.py/2"   # bump with LATEST_AGENTS in smart.py


def smartctl_json(smartctl, args, timeout):
    p = subprocess.run([smartctl, "-j", *args], capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = {"agent_error": f"unparseable smartctl output: {(p.stderr or p.stdout).strip()[:200]}"}
    data.setdefault("smartctl", {})["exit_status"] = p.returncode
    return data


def collect(smartctl, timeout):
    errors, drives = [], []
    scan = smartctl_json(smartctl, ["--scan-open"], timeout)
    devices = scan.get("devices") or []
    if not devices:
        errors.append("smartctl --scan-open found no devices" + (f" ({scan['agent_error']})" if "agent_error" in scan else ""))
    for dev in devices:
        name, dtype = dev.get("name"), dev.get("type")
        if dev.get("open_error"):
            errors.append(f"{name}: {dev['open_error']}")
            continue
        try:
            # -x (not -a): includes ATA Device Statistics, the authoritative SSD wear value
            raw = smartctl_json(smartctl, ["-x", "-d", dtype, name] if dtype else ["-x", name], timeout)
        except subprocess.TimeoutExpired:
            raw = {"agent_error": f"smartctl timed out after {timeout}s"}
        raw.setdefault("device", {"name": name, "type": dtype})
        drives.append(raw)
    return drives, errors


def main():
    ap = argparse.ArgumentParser(description="Push SMART data to the rack dashboard")
    ap.add_argument("--url", help="dashboard ingest URL, e.g. http://optiplex-management:8080/api/smart")
    ap.add_argument("--token-file", help="file containing the agent bearer token")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0].lower(), help="host name shown on the dashboard")
    ap.add_argument("--smartctl", default=shutil.which("smartctl") or "/usr/local/sbin/smartctl")
    ap.add_argument("--timeout", type=int, default=60, help="seconds per smartctl call")
    ap.add_argument("--dry-run", action="store_true", help="print a summary instead of sending")
    args = ap.parse_args()

    errors = []
    try:
        drives, errors = collect(args.smartctl, args.timeout)
    except FileNotFoundError:
        drives, errors = [], [f"smartctl not found at {args.smartctl} (install smartmontools)"]

    report = {
        "schema": 1,
        "host": args.name,
        "agent": VERSION,
        "os": platform.system().lower(),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "drives": drives,
        "errors": errors,
    }

    if args.dry_run:
        for d in drives:
            print(f"{(d.get('device') or {}).get('name')}: {d.get('model_name') or d.get('scsi_product')} "
                  f"exit={d['smartctl'].get('exit_status')} passed={(d.get('smart_status') or {}).get('passed')}")
        for e in errors:
            print("error:", e)
        return 0

    if not args.url or not args.token_file:
        ap.error("--url and --token-file are required unless --dry-run")
    with open(args.token_file) as f:
        token = f.read().strip()
    req = urllib.request.Request(args.url, data=json.dumps(report).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": VERSION})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"sent {len(drives)} drive(s) as '{args.name}': HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"dashboard rejected report: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"could not reach dashboard at {args.url}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
