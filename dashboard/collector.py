#!/usr/bin/env python3
"""
Rack Dashboard - collector

Serves the rack dashboard (a slideshow of screens) and feeds its screens:

  * WAN noise map: receives OPNsense firewall logs (filterlog) over
    syslog/UDP, keeps inbound WAN events, geolocates the source IP with a
    local MaxMind-format database (DB-IP City Lite or GeoLite2-City), and
    streams each event to browsers over Server-Sent Events (/events).
  * Drive health: SMART agents on each machine POST reports to /api/smart
    (bearer token); the page reads them back from GET /api/smart.
  * Compute: the same agents POST a metrics sample every 10 seconds to
    /api/metrics; GET /api/metrics returns the latest + ~1 h of history.
  * Honeypot: Cowrie events aggregated over 24 h at GET /api/honeypot, attack
    patterns over 30 days at GET /api/honeypot/patterns, and every stored event as
    CSV at GET /api/honeypot/export.csv[?days=N]. Simulated (honeypot.Simulator)
    until cloud-ops posts real Cowrie events to POST /api/honeypot/ingest.

Only dependency outside the standard library: maxminddb  (pip install maxminddb)

    python3 collector.py --db dbip-city-lite.mmdb --iface igb0 --token-file /etc/rack-dashboard/agent-token
    python3 collector.py --demo            # simulated data, no OPNsense / database / agents needed
"""
import argparse
import hmac
import ipaddress
import os
import json
import logging
import queue
import re
import socket
import threading
import time
from collections import OrderedDict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import creds
import honeypot
import metrics
import smart

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REPORT_BYTES = 8 * 1024 * 1024
log = logging.getLogger("rack-dashboard")

# The filterlog CSV payload inside a syslog line, e.g.
# 96,,,02f4...,igb0,match,block,in,4,0x0,,64,0,0,DF,6,tcp,60,198.51.100.7,203.0.113.10,50553,4880,0,S,...
FILTERLOG_RE = re.compile(
    r"(\d*,\d*,[^,\s]*,[^,\s]*,[^,\s]+,[^,\s]+,(?:pass|block|reject),(?:in|out),[46],\S*)"
)


def parse_filterlog(line):
    """Return a dict for one OPNsense/pfSense filterlog line, or None."""
    m = FILTERLOG_RE.search(line)
    if not m:
        return None
    f = m.group(1).split(",")
    try:
        ev = {"iface": f[4], "reason": f[5], "action": f[6], "dir": f[7], "ipver": f[8]}
        if f[8] == "4":
            proto, src, dst, rest = f[16].lower(), f[18], f[19], 20
        else:  # IPv6 layout
            proto, src, dst, rest = f[12].lower(), f[15], f[16], 17
        ev.update(proto=proto, src=src, dst=dst, sport=None, dport=None)
        if proto in ("tcp", "udp") and len(f) > rest + 1:
            ev["sport"], ev["dport"] = int(f[rest]), int(f[rest + 1])
        return ev
    except (IndexError, ValueError):
        return None


class Geo:
    """Local GeoIP lookups with a small LRU cache."""

    def __init__(self, path, cache_size=50000):
        import maxminddb  # imported here so --demo works without it installed

        self.reader = maxminddb.open_database(path)
        self.cache = OrderedDict()
        self.cache_size = cache_size

    def lookup(self, ip):
        if ip in self.cache:
            self.cache.move_to_end(ip)
            return self.cache[ip]
        result = None
        rec = self.reader.get(ip)
        if rec and "location" in rec:
            loc = rec["location"]
            country = rec.get("country", {})
            result = {
                "lat": loc.get("latitude"),
                "lon": loc.get("longitude"),
                "cc": country.get("iso_code", ""),
                "country": country.get("names", {}).get("en", ""),
                "city": rec.get("city", {}).get("names", {}).get("en", ""),
            }
            if result["lat"] is None or result["lon"] is None:
                result = None
        self.cache[ip] = result
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return result


class Broker:
    """Fan events out to every connected browser."""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, event):
        data = json.dumps(event, separators=(",", ":"))
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # slow client: drop rather than block the collector


class Pipeline:
    """filter -> dedupe -> rate limit -> geolocate -> publish"""

    def __init__(self, args, geo, broker):
        self.args, self.geo, self.broker = args, geo, broker
        self.recent = OrderedDict()  # (src, dport) -> last seen, for dedupe
        self.tokens, self.last_refill = float(args.max_rate), time.monotonic()
        self.stats = {"lines": 0, "matched": 0, "published": 0, "no_geo": 0, "rate_dropped": 0}

    def _allow_rate(self):
        now = time.monotonic()
        self.tokens = min(self.args.max_rate, self.tokens + (now - self.last_refill) * self.args.max_rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def handle_line(self, line):
        self.stats["lines"] += 1
        ev = parse_filterlog(line)
        if not ev or ev["dir"] != "in":
            return
        if self.args.iface and ev["iface"] != self.args.iface:
            return
        if ev["action"] == "pass" and not self.args.include_pass:
            return
        try:
            if not ipaddress.ip_address(ev["src"]).is_global:
                return
        except ValueError:
            return
        self.stats["matched"] += 1

        key = (ev["src"], ev["dport"])
        now = time.monotonic()
        last = self.recent.get(key)
        self.recent[key] = now
        self.recent.move_to_end(key)
        if len(self.recent) > 20000:
            self.recent.popitem(last=False)
        if last is not None and now - last < self.args.dedupe:
            return
        if not self._allow_rate():
            self.stats["rate_dropped"] += 1
            return

        g = self.geo.lookup(ev["src"])
        if not g:
            self.stats["no_geo"] += 1
            return
        self.broker.publish({
            "t": int(time.time() * 1000), "ip": ev["src"], "proto": ev["proto"],
            "dport": ev["dport"], "action": ev["action"], **g,
        })
        self.stats["published"] += 1


def syslog_listener(port, pipeline):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    log.info("listening for syslog on udp/%d", port)
    while True:
        data, _ = sock.recvfrom(65535)
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                pipeline.handle_line(line)
            except Exception:  # never let one odd line kill the listener
                log.exception("failed to handle line: %r", line[:200])


def make_handler(broker, page_config, smart_store=None, agent_token=None, metrics_store=None, deploy=None, cred_store=None,
                 honeypot_store=None, deploy_keys=None):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC_DIR), **kw)

        def log_message(self, fmt, *a):
            log.debug("http: " + fmt, *a)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/events":
                return self._sse()
            if path == "/config.json":
                return self._json(200, page_config)
            if path == "/api/smart":
                return self._json(200, smart_store.view() if smart_store else {"hosts": []})
            if path == "/api/metrics":
                return self._json(200, metrics_store.view() if metrics_store else {"hosts": []})
            if path == "/api/tasks":
                return self._json(200, metrics_store.tasks_view() if metrics_store else {"hosts": []})
            if path == "/api/deploy":
                view = metrics_store.deploy_view() if metrics_store else {"hosts": []}
                reporting = {h["host"] for h in view["hosts"] if h.get("reported")}
                return self._json(200, dict(view, request=deploy.info() if deploy else None,
                                            pending_keys=deploy_keys.view(reporting) if deploy_keys else []))
            if path in ("/api/creds/key", "/api/creds/secret"):
                return self._creds_get(path.rsplit("/", 1)[1])
            if path == "/api/honeypot":
                return self._json(200, honeypot_store.view() if honeypot_store else {"source": None})
            if path == "/api/honeypot/patterns":
                return self._json(200, honeypot_store.patterns_view() if honeypot_store else {"source": None})
            if path == "/api/honeypot/export.csv":
                return self._honeypot_csv()
            return super().do_GET()

        def _honeypot_csv(self):
            # Every stored honeypot event (30 days at most) as CSV, for analysis outside the dashboard.
            # ?days=N limits it. Streamed: HTTP/1.0, so closing the connection ends the body.
            from urllib.parse import parse_qs, urlsplit
            raw = (parse_qs(urlsplit(self.path).query).get("days") or [None])[0]
            try:
                days = None if raw in (None, "") else float(raw)
                if days is not None and not 0 < days <= 3650:
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "days must be a number above 0"})
            out = honeypot_store.export_csv(days) if honeypot_store else None
            if out is None:
                return self._json(404, {"error": "no honeypot history yet"})
            name, chunks = out
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(b"\xef\xbb\xbf")     # BOM: Excel then reads UTF-8 correctly
                for chunk in chunks:
                    self.wfile.write(chunk.encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            log.info("honeypot: CSV export (%s) to %s", name, self.client_address[0])

        def _authorized(self, what):
            if not agent_token:
                self._json(503, {"error": "disabled: no agent token configured"})
                return False
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {agent_token}".encode()):
                log.warning("%s: rejected request from %s (bad token)", what, self.client_address[0])
                self._json(401, {"error": "bad token"})
                return False
            return True

        def _creds_get(self, kind):
            # Public key or ciphertext only; see creds.py. Token required all the same.
            if not cred_store or not self._authorized("creds"):
                return None if cred_store else self._json(404, {"error": "not found"})
            from urllib.parse import parse_qs, urlsplit
            id_ = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
            try:
                item = cred_store.get(kind, id_)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, item) if item else self._json(404, {"error": f"no {kind} for {id_}"})

        def _creds_post(self, kind):
            if not cred_store or not self._authorized("creds"):
                return None if cred_store else self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("body must be 1..65536 bytes")
                body = json.loads(self.rfile.read(length).decode("utf-8-sig"))
                out = cred_store.put_key(body) if kind == "key" else cred_store.put_secret(body)
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {"error": str(e)})
            log.info("creds: %s for %s from %s (%s)", kind, body["id"], body["host"], self.client_address[0])
            return self._json(200, out)

        def _deploykey_post(self):
            # A new VM's public deploy key, to show on the Deployments screen (see PendingDeployKeys).
            if not deploy_keys or not self._authorized("deploykeys"):
                return None if deploy_keys else self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("body must be 1..4096 bytes")
                out = deploy_keys.put(json.loads(self.rfile.read(length).decode("utf-8-sig")))
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {"error": str(e)})
            log.info("deploykeys: %s is waiting for its GitHub deploy key (from %s)", out["host"], self.client_address[0])
            return self._json(200, out)

        def _honeypot_ingest(self):
            # Cowrie log lines + lab-side status from cloud-ops (see honeypot.HoneypotFeed.ingest).
            if not honeypot_store or not self._authorized("honeypot"):
                return None if honeypot_store else self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2 * 1024 * 1024:
                    raise ValueError("body must be 1 byte to 2 MB")
                out = honeypot_store.ingest(json.loads(self.rfile.read(length).decode("utf-8")))
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {"error": str(e)})
            if out.get("accepted"):
                log.info("honeypot: %d events from %s", out["accepted"], self.client_address[0])
            return self._json(200, out)

        def end_headers(self):
            # Static files: always revalidate, so a deploy shows up on the next
            # reload instead of whenever the browser's cache decides to expire.
            if not self.path.startswith(("/events", "/api/")):
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/deploy":
                return self._deploy()
            if path in ("/api/creds/key", "/api/creds/secret"):
                return self._creds_post(path.rsplit("/", 1)[1])
            if path == "/api/deploykeys":
                return self._deploykey_post()
            if path == "/api/honeypot/ingest":
                return self._honeypot_ingest()
            stores = {"/api/smart": ("smart", smart_store), "/api/metrics": ("metrics", metrics_store)}
            kind, store = stores.get(path, (None, None))
            if store is None:
                return self._json(404, {"error": "not found"})
            if not agent_token:
                return self._json(503, {"error": "ingest disabled: no agent token configured"})
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {agent_token}".encode()):
                log.warning("%s: rejected report from %s (bad token)", kind, self.client_address[0])
                return self._json(401, {"error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_REPORT_BYTES:
                return self._json(413, {"error": f"body must be 1..{MAX_REPORT_BYTES} bytes"})
            try:
                report = json.loads(self.rfile.read(length).decode("utf-8-sig"))
                store.put(report)
            except (ValueError, UnicodeDecodeError) as e:
                log.warning("%s: bad report from %s: %s", kind, self.client_address[0], e)
                return self._json(400, {"error": str(e)})
            if kind == "smart":   # metrics arrive every 10 s: don't log each one
                n = len(report.get("drives") or []) + len(report.get("windows_disks") or [])
                log.info("smart: report from %s (%s) with %d disk entries", report["host"], self.client_address[0], n)
            if kind == "metrics" and deploy:
                return self._json(200, {"ok": True, "deploy": deploy.info()})
            return self._json(200, {"ok": True})

        def _deploy(self):
            # No login (see metrics.DeployRequest), but require a custom header: browsers
            # won't send one cross-site without a CORS preflight we never allow, so another
            # web page can't trigger this through a visitor's browser.
            if not deploy:
                return self._json(404, {"error": "not found"})
            if self.headers.get("X-Rack-Deploy") != "1":
                return self._json(400, {"error": "send header X-Rack-Deploy: 1"})
            ok, info = deploy.request()
            if not ok:
                return self._json(429, {"error": f"deploy already requested {info['age_s']:.0f} s ago", "deploy": info})
            log.info("deploy: all hosts asked to update now (requested from %s)", self.client_address[0])
            return self._json(202, {"ok": True, "deploy": info})

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = broker.subscribe()
            try:
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                broker.unsubscribe(q)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Rack dashboard: WAN noise map + drive health")
    ap.add_argument("--db", help="GeoIP database (.mmdb): DB-IP City Lite or GeoLite2-City")
    ap.add_argument("--iface", default="igb0", help="WAN interface name to keep (default igb0; '' = all)")
    ap.add_argument("--udp-port", type=int, default=5514, help="syslog listen port (default 5514)")
    ap.add_argument("--http-port", type=int, default=8080, help="web page port (default 8080)")
    ap.add_argument("--bind", default="0.0.0.0", help="web page bind address")
    ap.add_argument("--include-pass", action="store_true", help="also show inbound traffic that was allowed")
    ap.add_argument("--dedupe", type=float, default=3.0, help="seconds to ignore repeats of the same source+port")
    ap.add_argument("--max-rate", type=float, default=40, help="max events per second sent to browsers")
    ap.add_argument("--home", help="'lat,lon' to draw arcs toward (optional)")
    ap.add_argument("--attribution", choices=["dbip", "maxmind", "none"], default="dbip")
    ap.add_argument("--demo", action="store_true", help="serve the page in demo mode (simulated data)")
    ap.add_argument("--token-file", help="file holding the SMART agents' bearer token "
                    "(default: $CREDENTIALS_DIRECTORY/agent-token when run by systemd)")
    ap.add_argument("--state-dir", help="where SMART reports are kept (default: $STATE_DIRECTORY or ./state)")
    ap.add_argument("--smart-order", default="", help="comma-separated host display order, e.g. opnsense,optiplex-management,homeshare,backup-compute")
    ap.add_argument("--smart-stale", type=float, default=45, help="minutes without a report before a host is marked stale")
    ap.add_argument("--interval", type=int, default=300, help="default seconds per dashboard screen (0 = no rotation)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    home = None
    if args.home:
        lat, lon = (float(x) for x in args.home.split(","))
        home = [lat, lon]
    page_config = {"mode": "demo" if args.demo else "live", "home": home, "attribution": args.attribution,
                   "interval": args.interval}

    state_dir = None if args.demo else (args.state_dir or os.environ.get("STATE_DIRECTORY", "").split(":")[0] or "state")
    if state_dir:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
    smart_store = smart.SmartStore(state_dir, stale_after=args.smart_stale * 60, order=args.smart_order.split(","))
    agent_token = None
    token_file = args.token_file or (
        os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "agent-token") if os.environ.get("CREDENTIALS_DIRECTORY") else None)
    if token_file and os.path.exists(token_file):
        agent_token = Path(token_file).read_text().strip() or None
    if args.demo:
        for received_at, report in smart.demo_reports():
            smart_store.put(report, now=received_at)
        smart_store.order = ["opnsense", "optiplex-management", "homeshare", "backup-compute"]
    elif agent_token:
        log.info("smart: accepting agent reports at /api/smart (state in %s)", state_dir)
    else:
        log.warning("smart: no agent token found, so /api/smart will reject reports")

    metrics_store = metrics.MetricsStore(order=args.smart_order.split(","))
    cred_store = creds.CredStore(str(Path(state_dir) / "creds.json") if state_dir else None)
    deploy = metrics.DeployRequest(str(Path(state_dir) / "deploy-request.json") if state_dir else None)
    if args.demo:
        metrics_store.order = smart_store.order
        metrics.seed_demo(metrics_store)

        def demo_metrics():   # keep demo hosts fresh: one new sample every STEP seconds
            i = metrics.HISTORY
            while True:
                time.sleep(metrics.STEP)
                for s in metrics.demo_samples(time.time(), i * metrics.STEP / 60):
                    metrics_store.put(dict(s, schema=1))
                i += 1
        threading.Thread(target=demo_metrics, daemon=True).start()

    geo = None
    if not args.demo:
        if not args.db:
            ap.error("--db is required unless --demo is used")
        geo = Geo(args.db)

    # Honeypot: simulated Cowrie events until cloud-ops posts the first real ones to
    # /api/honeypot/ingest, then the real feed for good (30 days in state/honeypot.db).
    # Simulated history lives in RAM only, so it can never mix with the real history on disk.
    reset_file = Path(__file__).with_name("honeypot-reset")
    reset_token = reset_file.read_text().strip() if reset_file.exists() else None
    honeypot_feed = honeypot.HoneypotFeed(str(Path(state_dir) / "honeypot.db") if state_dir else None, geo=geo,
                                          reset_token=reset_token or None)
    honeypot_feed.start_background()
    log.info("honeypot: %s", "real data (honeypot.db)" if honeypot_feed.live else "simulated until cloud-ops sends real events")
    if honeypot_feed.reset_note:
        log.info(honeypot_feed.reset_note)

    broker = Broker()
    if not args.demo:
        pipeline = Pipeline(args, geo, broker)
        threading.Thread(target=syslog_listener, args=(args.udp_port, pipeline), daemon=True).start()

        def report():
            while True:
                time.sleep(300)
                log.info("stats %s", pipeline.stats)
        threading.Thread(target=report, daemon=True).start()

    server = ThreadingHTTPServer((args.bind, args.http_port), make_handler(broker, page_config, smart_store, agent_token, metrics_store, deploy, cred_store,
                                                                         honeypot_feed, metrics.PendingDeployKeys()))
    server.daemon_threads = True
    log.info("dashboard at http://%s:%d/  (mode: %s)", socket.gethostname(), args.http_port, page_config["mode"])
    server.serve_forever()


if __name__ == "__main__":
    main()
