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

import honeypot  # noqa: E402
import metrics  # noqa: E402
import smart    # noqa: E402
import space    # noqa: E402

ORDER = ["opnsense", "optiplex-management", "homeshare", "backup-compute"]


def honeypot_stream(sim, now, minutes=20):
    """The next `minutes` of simulated honeypot events, as feed rows with an offset in seconds;
    demo-api.js plays them (looping) so the honeypot screen's feed keeps moving."""
    rows, t = [], now
    while t < now + minutes * 60:
        t += sim.rng.expovariate(sim.rate / 3600)
        lines, geo = sim.session_lines(t)
        proto = None
        for tt, line in lines:
            ev = honeypot.parse_cowrie(line)
            if not ev:
                continue
            proto = ev.get("protocol", proto)
            k = ev["kind"]
            text = (f'{ev["username"]} / {ev["password"]}' if k.startswith("login") else ev.get("input") or ev.get("url")
                    or ev.get("version") or (f'{round(ev["duration"])} s' if k == "closed" else ev.get("protocol", "")))
            rows.append({"offset_s": round(tt - now, 1), "kind": k, "ip": ev["ip"], "text": text[:160], "protocol": proto,
                         "cc": geo.get("cc"), "city": geo.get("city"), "country": geo.get("country")})
    rows.sort(key=lambda r: r["offset_s"])
    return {"period_s": minutes * 60, "rows": rows}


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
    hp = honeypot.HoneypotStore(source="simulated", db=honeypot.HoneypotDB(":memory:"))
    sim = honeypot.Simulator(hp, seed=2026, now=now)
    sim.backfill(now=now)
    sim.backfill_history(now=now)
    # The export button's file: the last 24 h only (30 days of simulated events would be far too big).
    csv_text = "".join(honeypot.csv_rows(hp.db.iter_events(now - 86400)))
    return csv_text, {
        "built_at": int(now),
        "honeypot": hp.view(now=now),
        # Space: generated, like --demo (the static demo never calls NASA). A snapshot at build time.
        "space": space.SpaceFeed(None, mode="demo").view(now=now),
        "honeypot_patterns": hp.patterns_view(now=now),
        "honeypot_stream": honeypot_stream(sim, now),
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

    csv_text, data = snapshot()
    (args.out / "demo-data.json").write_text(json.dumps(data, separators=(",", ":")))
    (args.out / "api" / "honeypot").mkdir(parents=True)
    (args.out / "api" / "honeypot" / "export.csv").write_text("\ufeff" + csv_text, encoding="utf-8")
    # An arbitrary point (Kansas City) for the noise map's arcs; not anyone's location.
    (args.out / "config.json").write_text(json.dumps(
        {"mode": "demo", "home": [39.1, -94.58], "attribution": "", "interval": args.interval,
         "repo_url": args.repo_url}))

    # Every section page fetches api/*: demo-api.js answers those calls from demo-data.json.
    for page in ("rack/index.html", "home/index.html", "space/index.html"):
        index = args.out / page
        html = index.read_text()
        first_script = html.index("<script src=")
        html = html[:first_script] + '<script src="demo-api.js"></script>\n' + html[first_script:]
        index.write_text(html)
    (args.out / ".nojekyll").write_text("")   # serve files as-is on GitHub Pages
    print(f"static demo written to {args.out}")


if __name__ == "__main__":
    main()
