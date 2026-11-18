"""
Compute metrics for the rack dashboard.

Agents POST a sample about every 10 seconds to /api/metrics:

    {
      "schema": 1, "host": "homeshare", "os": "windows", "agent": "Send-Metrics.ps1/2", "commit": "1fdb945c3a2e",
      "collected_at": "...", "uptime_s": 123456, "pending_reboot": false,
      "cpu":  {"model": "...", "sockets": 1, "cores": 2, "threads": 4, "util_pct": 12.5,
               "per_core_pct": [..], "load": [0.4, 0.3, 0.2], "temp_c": 48},
      "mem":  {"total": 17179869184, "used": 8589934592, "swap_total": ..., "swap_used": ...},
      "gpus": [{"name": "...", "util_pct": 3, "mem_used": .., "mem_total": .., "temp_c": 41,
                "power_w": 12.3, "enc_pct": 0, "dec_pct": 0}],
      "net":  [{"name": "Ethernet 2", "rx_bps": 1200.0, "tx_bps": 800.0, "speed_mbps": 10000}],
      "disks":[{"name": "0 C:", "read_bps": 0.0, "write_bps": 4096.0, "busy_pct": 1.2}],
      "procs":{"count": 182, "top": [{"name": "jellyfin", "cpu_pct": 4.1, "mem": 512000000}]}
    }

Everything except host is optional: missing sections are just not shown.
Kept in memory only: the latest sample per host plus a fixed-size history
(HISTORY points) for the sparklines, so nothing grows on disk.
"""
import json
import os
import re
import threading
import time
from collections import deque

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
STEP = 10                        # seconds between agent samples
HISTORY = 360                    # points kept per series (1 h at one sample every 10 s)
OK, WARN, CRIT = "ok", "warn", "crit"
_RANK = {OK: 0, WARN: 1, CRIT: 2}

LATEST_AGENTS = {"metrics_agent.py": 2, "Send-Metrics.ps1": 2}


def _num(v):
    try:
        f = float(v)
        return f if f == f else None   # drop NaN
    except (TypeError, ValueError):
        return None


def validate(sample):
    if not isinstance(sample, dict):
        raise ValueError("sample must be a JSON object")
    if not HOST_RE.match(str(sample.get("host", ""))):
        raise ValueError("host must match [A-Za-z0-9._-]{1,64}")
    for key in ("gpus", "net", "disks"):
        if key in sample and not isinstance(sample[key], list):
            raise ValueError(f"{key} must be a list")
        if len(sample.get(key) or []) > 64:
            raise ValueError(f"too many {key}")
    if sample.get("deploy_status") is not None and not isinstance(sample["deploy_status"], dict):
        raise ValueError("deploy_status must be an object")
    if "tasks" in sample and not (isinstance(sample["tasks"], list) and len(sample["tasks"]) <= 32):
        raise ValueError("tasks must be a list of at most 32 entries")
    per_core = (sample.get("cpu") or {}).get("per_core_pct") or []
    if not isinstance(per_core, list) or len(per_core) > 512:
        raise ValueError("cpu.per_core_pct must be a list of at most 512 values")


DEPLOY_RESULTS = {"ok", "failed", "rejected"}


def clean_deploy_status(st, now):
    """Keep only known fields, sanitised; turn ages back into dashboard-clock times."""
    if not isinstance(st, dict):
        return None
    def text(k, n):
        v = st.get(k)
        return str(v)[:n] if v is not None else None
    def age(k):
        v = _num(st.get(k))
        return now - max(0.0, v) if v is not None else None
    commit = str(st.get("commit") or "").lower()
    committed = _num(st.get("committed_at"))
    return {
        "result": st.get("result") if st.get("result") in DEPLOY_RESULTS else "unknown",
        "message": text("message", 200), "branch": text("branch", 64), "git": text("git", 32),
        "commit": commit if COMMIT_RE.match(commit) else None, "subject": text("subject", 120),
        "committed_at": int(committed) if committed else None,
        "checked_at": age("checked_age_s"), "changed_at": age("changed_age_s"),
        "every_s": int(_num(st.get("every_s")) or 0) or None,
        # Present only while the host's updater is working: since when, and on which step.
        "running_since": age("running_for_s"), "phase": text("phase", 100),
    }


TASK_STATES = {"Ready", "Running", "Disabled", "Queued", "Unknown"}
RUN_STATUSES = {"ok", "warn", "failed", "aborted"}
# Task Scheduler result codes that aren't failures
TS_RUNNING, TS_NEVER_RUN = 0x41301, 0x41303


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def _summary(sm):
    if not isinstance(sm, dict):
        return None
    out = {}
    for k in ("dirs", "files", "bytes"):
        row = sm.get(k)
        if isinstance(row, dict):
            out[k] = {c: _int(row.get(c)) for c in ("total", "copied", "skipped", "mismatch", "failed", "extras")}
    return out or None


def clean_tasks(tasks, now):
    """Sanitise a host's scheduled-task report; ages -> dashboard-clock times."""
    out = []
    for t in tasks if isinstance(tasks, list) else []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        txt = lambda k, n: str(t[k])[:n] if t.get(k) is not None else None
        at = lambda k, sign=-1: (now + sign * _num(t[k])) if _num(t.get(k)) is not None else None
        runs = []
        for r in (t.get("runs") or [])[-60:] if isinstance(t.get("runs"), list) else []:
            if not isinstance(r, dict) or _num(r.get("age_s")) is None:
                continue
            runs.append({
                "end_at": now - _num(r["age_s"]), "duration_s": _int(r.get("duration_s")),
                "status": r.get("status") if r.get("status") in RUN_STATUSES else "failed",
                "message": str(r.get("message") or "")[:200], "exit_code": _int(r.get("exit_code")),
                "summary": _summary(r.get("summary")), "error_count": _int(r.get("error_count")) or 0,
                "errors": [str(e)[:300] for e in (r.get("errors") or [])[:5]] if isinstance(r.get("errors"), list) else [],
            })
        out.append({
            "name": txt("name", 100), "state": t.get("state") if t.get("state") in TASK_STATES else "Unknown",
            "job": txt("job", 64), "description": txt("description", 300),
            "source": txt("source", 260), "destination": txt("destination", 260),
            "kind": t.get("kind") if t.get("kind") in ("mirror", "kopia") else "mirror",
            "last_run_at": at("last_run_age_s"), "next_run_at": at("next_run_in_s", +1),
            "running_since": at("running_for_s"), "last_task_result": _int(t.get("last_task_result")),
            "missed_runs": _int(t.get("missed_runs")), "runs": runs,
            # From the job's schedule (agent, backups/tasks.json): label + longest normal gap.
            "schedule": txt("schedule", 60),
            "interval_s": _int(t.get("interval_s")) if (_int(t.get("interval_s")) or 0) > 0 else None,
        })
    return out[:32]


def task_interval(t):
    """Longest normal gap between runs: from the schedule when the agent reports it,
    otherwise estimated from last/next run (wrong after an off-schedule run)."""
    if t.get("interval_s"):
        return t["interval_s"]
    if t.get("next_run_at") and t.get("last_run_at"):
        return t["next_run_at"] - t["last_run_at"]
    return None


def task_health(t, now):
    """(level, text) for a task: running / disabled / failed / warn / overdue / ok / unknown."""
    if t["state"] == "Disabled":
        return "disabled", "disabled"
    if t["state"] == "Running" or t.get("running_since"):
        return "running", "running"
    runs = t["runs"]
    interval = task_interval(t)
    if runs:
        last = runs[-1]
        # Overdue = it didn't run on schedule (a run that happened but failed is "failed").
        if interval and interval > 0 and now - last["end_at"] > interval * 1.1 + 3600:
            return "overdue", "overdue: hasn't run on schedule"
        if last["status"] in ("failed", "aborted"):
            return "failed", f"last run {last['status']}"
        return ("warn", "completed with warnings") if last["status"] == "warn" else ("ok", "ok")
    code = t.get("last_task_result")
    if code in (None, TS_NEVER_RUN) or not t.get("last_run_at"):
        return "unknown", "not run yet"
    if code == 0:
        return "ok", "ok"
    if code == TS_RUNNING:
        return "running", "running"
    return "failed", f"last result 0x{code & 0xFFFFFFFF:X}"


def series_point(sample):
    """The values tracked over time for one sample."""
    cpu, mem = sample.get("cpu") or {}, sample.get("mem") or {}
    total, used = _num(mem.get("total")), _num(mem.get("used"))
    gpus = sample.get("gpus") or []
    return {
        "cpu": _num(cpu.get("util_pct")),
        "mem": round(100 * used / total, 1) if total and used is not None else None,
        "rx": sum(_num(n.get("rx_bps")) or 0 for n in sample.get("net") or []),
        "tx": sum(_num(n.get("tx_bps")) or 0 for n in sample.get("net") or []),
        "rd": sum(_num(d.get("read_bps")) or 0 for d in sample.get("disks") or []),
        "wr": sum(_num(d.get("write_bps")) or 0 for d in sample.get("disks") or []),
        "gpu": max((_num(g.get("util_pct")) or 0 for g in gpus), default=None) if gpus else None,
    }


def health(sample):
    """(level, reasons) for one host."""
    level, reasons = OK, []

    def add(lv, text):
        nonlocal level
        level = max(level, lv, key=_RANK.get)
        reasons.append({"level": lv, "text": text})

    cpu, mem = sample.get("cpu") or {}, sample.get("mem") or {}
    util = _num(cpu.get("util_pct"))
    if util is not None and util >= 95:
        add(WARN, f"CPU {util:.0f}%")
    t = _num(cpu.get("temp_c"))
    if t is not None:
        if t >= 95:
            add(CRIT, f"CPU {t:.0f}°C")
        elif t >= 85:
            add(WARN, f"CPU {t:.0f}°C")
    total, used = _num(mem.get("total")), _num(mem.get("used"))
    if total and used is not None:
        pct = 100 * used / total
        if pct >= 97:
            add(CRIT, f"memory {pct:.0f}%")
        elif pct >= 90:
            add(WARN, f"memory {pct:.0f}%")
    st, su = _num(mem.get("swap_total")), _num(mem.get("swap_used"))
    if st and su is not None and su / st >= 0.5:
        add(WARN, f"swap {100 * su / st:.0f}% used")
    for g in sample.get("gpus") or []:
        gt = _num(g.get("temp_c"))
        if gt is not None and gt >= 90:
            add(CRIT if gt >= 95 else WARN, f"GPU {gt:.0f}°C")
    return level, reasons


def agent_status(agent):
    name, _, ver = str(agent or "").partition("/")
    latest = LATEST_AGENTS.get(name)
    try:
        v = int(ver)
    except ValueError:
        v = None
    return {"name": name, "version": v, "latest": latest,
            "outdated": bool(latest and v is not None and v < latest)}


class MetricsStore:
    def __init__(self, stale_after=60, expire_after=86400, order=(), reference_host="optiplex-management"):
        self.reference_host = reference_host   # its commit is "current"; others are compared to it
        self.stale_after, self.expire_after = stale_after, expire_after
        self.order = [o.strip() for o in order if o.strip()]
        self.lock = threading.Lock()
        self.hosts = {}   # host -> {"received_at", "sample", "history": deque}

    def put(self, sample, now=None):
        validate(sample)
        now = now or time.time()
        point = dict(series_point(sample), t=round(now))
        with self.lock:
            h = self.hosts.setdefault(sample["host"], {"history": deque(maxlen=HISTORY)})
            h["received_at"], h["sample"] = now, sample
            h["history"].append(point)
            if "deploy_status" in sample:
                h["deploy"] = clean_deploy_status(sample["deploy_status"], now)
            if "tasks" in sample:   # sent about once a minute; kept until the next report
                h["tasks"] = clean_tasks(sample["tasks"], now)

    def view(self, now=None):
        now = now or time.time()
        with self.lock:
            items = [(k, dict(v, history=list(v["history"]))) for k, v in self.hosts.items()
                     if now - v["received_at"] < self.expire_after]
        rank = {h: i for i, h in enumerate(self.order)}
        items.sort(key=lambda kv: (rank.get(kv[0], len(rank)), kv[0]))
        out = []
        for host, v in items:
            s = v["sample"]
            level, reasons = health(s)
            age = max(0, now - v["received_at"])
            out.append({
                "host": host, "os": s.get("os"), "agent": s.get("agent"),
                "agent_status": agent_status(s.get("agent")),
                "age_s": int(age), "stale": age > self.stale_after,
                "health": level, "reasons": reasons,
                "uptime_s": s.get("uptime_s"), "pending_reboot": bool(s.get("pending_reboot")),
                "cpu": s.get("cpu") or {}, "mem": s.get("mem") or {}, "gpus": s.get("gpus") or [],
                "net": s.get("net") or [], "disks": s.get("disks") or [], "procs": s.get("procs") or {},
                "history": v["history"],
            })
        return {"stale_after_s": self.stale_after, "history_points": HISTORY, "hosts": out}

    def tasks_view(self, now=None):
        """Scheduled tasks per host for the Scheduled tasks screen (times as ages)."""
        now = now or time.time()
        with self.lock:
            items = [(k, [dict(t, runs=list(t["runs"])) for t in v.get("tasks") or []], v["received_at"])
                     for k, v in self.hosts.items() if v.get("tasks") and now - v["received_at"] < self.expire_after]
        rank = {h: i for i, h in enumerate(self.order)}
        items.sort(key=lambda kv: (rank.get(kv[0], len(rank)), kv[0]))
        ago = lambda t: round(now - t) if t else None
        out = []
        for host, tasks, received in items:
            rows = []
            for t in tasks:
                level, text = task_health(t, now)
                interval = round(task_interval(t)) if task_interval(t) else None
                runs = [dict({k: v for k, v in r.items() if k != "end_at"}, age_s=ago(r["end_at"])) for r in t["runs"]]
                rows.append({k: v for k, v in t.items() if k not in ("last_run_at", "next_run_at", "running_since", "runs")} | {
                    "health": level, "health_text": text, "interval_s": interval,
                    "last_run_age_s": ago(t.get("last_run_at")),
                    "next_run_in_s": round(t["next_run_at"] - now) if t.get("next_run_at") else None,
                    "running_for_s": ago(t.get("running_since")), "runs": runs})
            out.append({"host": host, "stale": now - received > self.stale_after, "tasks": rows})
        return {"hosts": out}

    def deploy_view(self, now=None):
        """Per-host updater status for the Deployments screen (times as ages)."""
        now = now or time.time()
        with self.lock:
            items = [(k, v.get("deploy"), v["sample"], v["received_at"]) for k, v in self.hosts.items()
                     if now - v["received_at"] < self.expire_after]
        rank = {h: i for i, h in enumerate(self.order)}
        items.sort(key=lambda kv: (rank.get(kv[0], len(rank)), kv[0]))
        ago = lambda t: round(now - t) if t else None
        out = []
        for host, d, s, received in items:
            d = dict(d or {})
            d["checked_age_s"], d["changed_age_s"] = ago(d.pop("checked_at", None)), ago(d.pop("changed_at", None))
            d["committed_age_s"] = ago(d.pop("committed_at", None))
            d["running_for_s"] = ago(d.pop("running_since", None))
            out.append({"host": host, "os": s.get("os"), "agent": s.get("agent"), "agent_status": agent_status(s.get("agent")),
                        "seen_age_s": round(now - received), "stale": now - received > self.stale_after,
                        "reported": bool(d.get("result")), **d})
        ref = next((h.get("commit") for h in out if h["host"] == self.reference_host and h.get("commit")), None)
        return {"reference_host": self.reference_host, "reference_commit": ref, "hosts": out}


class DeployRequest:
    """'Deploy now' for every host. Agents get the latest request (id + age) in the
    response to each metrics POST; each one then does its normal update right away
    instead of at its next 15-minute run. Nothing is executed on behalf of the caller,
    so the endpoint needs no login; it is rate-limited instead. Kept in a small file,
    because the OptiPlex's own deploy restarts the dashboard mid-rollout."""

    def __init__(self, path=None, min_gap=60, max_age=600):
        self.path, self.min_gap, self.max_age = path, min_gap, max_age
        self.lock = threading.Lock()
        self.at = 0.0
        try:
            if path:
                with open(path) as f:
                    self.at = float(json.load(f)["at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def request(self, now=None):
        """Returns (accepted, info). Not accepted if the last request was < min_gap s ago."""
        now = now or time.time()
        with self.lock:
            if now - self.at < self.min_gap:
                return False, self.info(now)
            self.at = now
            if self.path:
                tmp = f"{self.path}.tmp"
                with open(tmp, "w") as f:
                    json.dump({"at": now}, f)
                os.replace(tmp, self.path)
            return True, self.info(now)

    def info(self, now=None):
        """{"id", "age_s", "retry_in_s"} for a request in the last max_age s, else None."""
        now = now or time.time()
        age = now - self.at
        if not self.at or age > self.max_age:
            return None
        return {"id": int(self.at), "age_s": round(max(0.0, age), 1), "retry_in_s": max(0, int(self.min_gap - age))}


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------
def _demo_deploy(commit, subject, every, checked_ago, changed_ago, git):
    return {"result": "ok", "message": "up to date", "branch": "main", "commit": commit, "subject": subject,
            "committed_at": int(time.time()) - changed_ago - 300, "checked_age_s": checked_ago % every,
            "changed_age_s": changed_ago, "every_s": every, "git": git}


def _demo_tasks():
    """The jobs on homeshare, like the real ones: daily mirror to backup-compute, monthly to the archive, daily cloud backup."""
    import random
    rnd = random.Random(42)
    GB = 1024 ** 3

    def run(age, dur, copied_files, copied_bytes, extras, status="ok", failed=0, errors=()):
        code = (1 if copied_files else 0) | (2 if extras else 0) | (8 if failed else 0)
        msg = ", ".join(m for m, on in (("files copied", copied_files), ("extra files removed", extras),
                                        ("some files could not be copied", failed)) if on) or "no changes"
        return {"age_s": age, "duration_s": dur, "status": status, "message": msg, "exit_code": code, "error_count": failed,
                "errors": list(errors),
                "summary": {"dirs": {"total": 8412, "copied": rnd.randint(0, 9), "skipped": 8400, "mismatch": 0, "failed": 0, "extras": 0},
                            "files": {"total": 312455, "copied": copied_files, "skipped": 312455 - copied_files, "mismatch": 0,
                                      "failed": failed, "extras": extras},
                            "bytes": {"total": int(3.41 * 1024 * GB), "copied": copied_bytes, "skipped": 0, "mismatch": 0,
                                      "failed": failed * 48 * 1024 ** 2, "extras": extras * 3 * 1024 ** 2}}}

    daily = []
    for d in range(29, -1, -1):
        age = d * 86400 + 5 * 3600 + 1200
        n = rnd.randint(20, 900)
        if d == 9:
            daily.append(run(age, 1260, n, rnd.randint(1, 40) * GB // 10, rnd.randint(0, 30), "failed", 1,
                             ["ERROR 32 (0x00000020) Copying File D:\\HomeShare\\Mail\\archive.pst"]))
        else:
            daily.append(run(age, rnd.randint(240, 1500), n, rnd.randint(1, 60) * GB // 10, rnd.randint(0, 40)))
    monthly = [run(m * 30 * 86400 + 15 * 86400, rnd.randint(5400, 9000), rnd.randint(2000, 9000),
                   rnd.randint(40, 300) * GB, rnd.randint(100, 900)) for m in range(7, -1, -1)]
    cloud = []
    for d in range(29, -1, -1):
        changed = rnd.randint(15, 700)
        cloud.append({"age_s": d * 86400 + 3 * 3600, "duration_s": rnd.randint(300, 2400), "status": "ok", "exit_code": 0,
                      "message": f"{changed} changed or new file(s) uploaded", "error_count": 0, "errors": [],
                      "summary": {"files": {"total": 312455, "copied": changed, "skipped": 312455 - changed, "mismatch": 0, "failed": 0, "extras": 0},
                                  "bytes": {"total": int(3.41 * 1024 * GB), "copied": None, "skipped": None, "mismatch": 0, "failed": None, "extras": 0}}})
    base = {"state": "Ready", "last_task_result": 0, "missed_runs": 0}
    return [
        dict(base, name="Cloud backup homeshare-to-b2", job="homeshare-to-b2", kind="kopia",
             description="Daily encrypted Kopia snapshot to Backblaze B2 (30 daily, Object Lock)",
             schedule="daily at 04:00", interval_s=86400, source="D:\\HomeShare", destination="b2://homelab-backup-example",
             last_run_age_s=cloud[-1]["age_s"] + cloud[-1]["duration_s"], next_run_in_s=86400 - cloud[-1]["age_s"] - cloud[-1]["duration_s"], runs=cloud),
        dict(base, name="Mirror homeshare-to-backup", job="homeshare-to-backup", description="Daily mirror of the Main pool",
             schedule="daily at 02:00", interval_s=86400,
             source="D:\\HomeShare", destination="\\\\backup-compute.home.arpa\\HomeShareBackup",
             last_run_age_s=daily[-1]["age_s"] + daily[-1]["duration_s"], next_run_in_s=86400 - daily[-1]["age_s"] - daily[-1]["duration_s"], runs=daily),
        dict(base, name="Mirror homeshare-to-archive", job="homeshare-to-archive", description="Monthly mirror to the SAS archive",
             schedule="monthly on day 1 at 03:00", interval_s=31 * 86400,
             source="D:\\HomeShare", destination="A:\\HomeShare",
             last_run_age_s=monthly[-1]["age_s"] + monthly[-1]["duration_s"], next_run_in_s=30 * 86400 - monthly[-1]["age_s"] - monthly[-1]["duration_s"], runs=monthly),
    ]


def demo_samples(t, minute):
    """Synthetic samples at `minute` (a float: 0..60 across the demo hour)."""
    import math
    import random
    rnd = random.Random(round(minute * 6) * 7919)
    wave = lambda period, phase=0: 0.5 + 0.5 * math.sin((minute + phase) / period * 2 * math.pi)
    GB, Gb = 1024 ** 3, 1e9 / 8

    def nics(rx, tx, speed, name):
        return [{"name": name, "rx_bps": rx, "tx_bps": tx, "speed_mbps": speed}]

    jelly = 18 + 30 * wave(23)
    xeon_cores = [max(0.0, min(100.0, (jelly if i < 8 else 3) + rnd.uniform(-6, 6))) for i in range(40)]
    return [
        {"host": "opnsense", "os": "freebsd", "agent": "metrics_agent.py/2", "deploy_status": _demo_deploy("c97b5d4e81f0", "Dashboard: SMART temperature limits from the drive", 900, 2300, 410, "2.46.2"), "uptime_s": int(9 * 86400 + minute * 60),
         "cpu": {"model": "Intel Core i7-7700 @ 3.60GHz", "sockets": 1, "cores": 4, "threads": 8,
                 "util_pct": 4 + 3 * wave(11), "per_core_pct": [rnd.uniform(1, 9) for _ in range(8)],
                 "load": [0.31, 0.27, 0.22], "temp_c": 41 + 4 * wave(17)},
                 "mem": {"total": 32 * GB, "used": 2.1 * GB, "swap_total": 8 * GB, "swap_used": 0},
         "net": nics(0.8 * Gb * wave(9) * 0.05, 0.1 * Gb * wave(9) * 0.03, 1000, "igb0 (WAN)")
                + nics(0.1 * Gb * 0.03, 0.8 * Gb * wave(9) * 0.05, 1000, "igb1 (LAN)"),
         "disks": [{"name": "ada0", "read_bps": 2e3, "write_bps": 4e4 + 2e4 * wave(5), "busy_pct": 0.4}],
         "procs": {"count": 61, "top": [{"name": "suricata", "cpu_pct": 2.1}, {"name": "unbound", "cpu_pct": 0.6},
                                         {"name": "python3.11", "cpu_pct": 0.3}]}},
        {"host": "optiplex-management", "os": "linux", "agent": "metrics_agent.py/2", "deploy_status": _demo_deploy("1fdb945c3a2e", "Merge pull request #20: Compute screen", 120, 40, 1500, "2.43.0"), "uptime_s": int(2 * 3600 + minute * 60),
         "cpu": {"model": "Intel Core i5-8500T @ 2.10GHz", "sockets": 1, "cores": 6, "threads": 6,
                 "util_pct": 9 + 6 * wave(7), "per_core_pct": [rnd.uniform(3, 20) for _ in range(6)],
                 "load": [0.42, 0.38, 0.35], "temp_c": 52 + 6 * wave(13)},
         "mem": {"total": 16 * GB, "used": 3.4 * GB, "swap_total": 1 * GB, "swap_used": 0.02 * GB},
         "net": nics(4e4 + 3e4 * wave(5), 9e4 + 4e4 * wave(5), 1000, "enp0s31f6"),
         "disks": [{"name": "sda", "read_bps": 1e3, "write_bps": 6e4, "busy_pct": 0.8},
                   {"name": "sdb", "read_bps": 0, "write_bps": 2e3, "busy_pct": 0.1}],
         "procs": {"count": 147, "top": [{"name": "chromium", "cpu_pct": 6.2}, {"name": "python3", "cpu_pct": 1.4},
                                          {"name": "Xorg", "cpu_pct": 0.9}]}},
        {"host": "homeshare", "os": "windows", "agent": "Send-Metrics.ps1/2", "tasks": _demo_tasks(), "deploy_status": dict(_demo_deploy("1fdb945c3a2e", "Merge pull request #20: Compute screen", 900, 380, 1100, "2.47.1.windows.1"),
                                                                                        running_for_s=95, phase="applying scheduled tasks"), "uptime_s": 21 * 86400, "pending_reboot": True,
         "cpu": {"model": "Intel Core i3-4160 @ 3.60GHz", "sockets": 1, "cores": 2, "threads": 4,
                 "util_pct": 11 + 25 * wave(19), "per_core_pct": [rnd.uniform(5, 40) for _ in range(4)]},
         "mem": {"total": 16 * GB, "used": 9.8 * GB, "commit_pct": 54},
         "net": nics(1.4 * Gb * wave(19), 0.2 * Gb * wave(19), 10000, "Mellanox ConnectX-3 (10G)"),
         "disks": [{"name": "Main", "read_bps": 1.4 * Gb * wave(19), "write_bps": 1e6, "busy_pct": 30 * wave(19)},
                   {"name": "Archive", "read_bps": 0, "write_bps": 0, "busy_pct": 0}],
         "procs": {"count": 132, "top": [{"name": "robocopy", "cpu_pct": 14 * wave(19)},
                                          {"name": "System", "cpu_pct": 3.1}, {"name": "svchost", "cpu_pct": 0.8}]}},
        {"host": "backup-compute", "os": "windows", "agent": "Send-Metrics.ps1/2", "deploy_status": _demo_deploy("1fdb945c3a2e", "Merge pull request #20: Compute screen", 900, 610, 1300, "2.47.1.windows.1"), "uptime_s": 6 * 86400,
         "cpu": {"model": "Intel Xeon E5-2660 v2 @ 2.20GHz", "sockets": 2, "cores": 20, "threads": 40,
                 "util_pct": sum(xeon_cores) / 40, "per_core_pct": xeon_cores},
         "mem": {"total": 64 * GB, "used": 21 * GB, "commit_pct": 38},
         "gpus": [{"name": "NVIDIA Quadro RTX 5000", "util_pct": 22 + 30 * wave(23), "mem_used": 2.3 * GB,
                   "mem_total": 16 * GB, "temp_c": 54 + 8 * wave(23), "power_w": 61 + 40 * wave(23),
                   "enc_pct": 35 * wave(23), "dec_pct": 28 * wave(23)}],
         "net": nics(0.2 * Gb * wave(19), 1.4 * Gb * wave(19), 10000, "Intel X520 (10G)"),
         "disks": [{"name": "Backup", "read_bps": 3e6, "write_bps": 1.4 * Gb * wave(19), "busy_pct": 25 * wave(19)}],
         "procs": {"count": 168, "top": [{"name": "jellyfin.ffmpeg", "cpu_pct": round(jelly * 8 / 40, 1)},
                                          {"name": "FAHClient", "cpu_pct": 4.0}, {"name": "jellyfin", "cpu_pct": 1.2}]}},
    ]


def seed_demo(store, now=None):
    now = now or time.time()
    for i in range(HISTORY):
        for s in demo_samples(now, i * STEP / 60):
            store.put(dict(s, schema=1), now=now - (HISTORY - 1 - i) * STEP)
