#!/usr/bin/env python3
"""
Build a static, backend-free copy of the rack dashboard for GitHub Pages.

    python3 dashboard/tools/build_static_demo.py OUT_DIR [--repo-url URL]

Copies dashboard/static/, snapshots the collector's own demo data (the same generators
`collector.py --demo` uses) into demo-data.json, and adds demo-api.js: an in-browser
stand-in for the collector's api/* endpoints. The WAN noise map already simulates its
traffic in the browser in demo mode, so it needs nothing.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent
sys.path.insert(0, str(DASHBOARD))

import metrics  # noqa: E402
import smart    # noqa: E402

ORDER = ["opnsense", "optiplex-management", "homeshare", "backup-compute"]


def snapshot():
    now = time.time()
    smart_store = smart.SmartStore(None, order=ORDER)
    for received_at, report in smart.demo_reports():
        smart_store.put(report, now=received_at)
    store = metrics.MetricsStore(order=ORDER)
    metrics.seed_demo(store)
    deploy = store.deploy_view(now=now)
    deploy["request"] = None
    for h in deploy["hosts"]:       # start idle; "Deploy all" in the demo plays a rollout
        h["running_for_s"], h["phase"] = None, None
    return {
        "built_at": int(now),
        "smart": smart_store.view(now=now),
        "metrics": store.view(now=now),
        "tasks": store.tasks_view(now=now),
        "deploy": deploy,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--repo-url", default="", help="link shown in the demo badge")
    ap.add_argument("--interval", type=int, default=25, help="seconds per screen in the slideshow")
    args = ap.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    shutil.copytree(DASHBOARD / "static", args.out)
    shutil.copy(HERE / "demo-api.js", args.out / "demo-api.js")

    (args.out / "demo-data.json").write_text(json.dumps(snapshot(), separators=(",", ":")))
    # An arbitrary point (Kansas City) for the noise map's arcs; not anyone's location.
    (args.out / "config.json").write_text(json.dumps(
        {"mode": "demo", "home": [39.1, -94.58], "attribution": "", "interval": args.interval,
         "repo_url": args.repo_url}))

    index = args.out / "index.html"
    html = index.read_text()
    first_script = html.index("<script src=")
    html = html[:first_script] + '<script src="demo-api.js"></script>\n' + html[first_script:]
    index.write_text(html)
    (args.out / ".nojekyll").write_text("")   # serve files as-is on GitHub Pages
    print(f"static demo written to {args.out}")


if __name__ == "__main__":
    main()
