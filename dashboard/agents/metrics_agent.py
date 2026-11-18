#!/usr/bin/env python3
"""
Compute metrics agent for Linux (optiplex-management) and FreeBSD (opnsense).

Measures CPU, network, disk and processes between two snapshots and posts the
sample to the rack dashboard's /api/metrics. Standard library only.

    metrics_agent.py --url URL --token-file FILE --name optiplex-management --loop 10   # normal: every 10 s, forever
    metrics_agent.py --url URL --token-file FILE                                        # one sample (1 s window)
    metrics_agent.py --dry-run        # print one sample instead of sending it

In --loop mode each sample is averaged over the whole interval, and the agent exits
when its own file changes so the supervisor restarts it on the new version.

"Deploy now": the dashboard answers each sample with the latest deploy request (if
any). The agent doesn't run anything it's sent; it only brings this machine's normal
update forward, as configured locally:
  --deploy-stamp FILE                    delete FILE (the wrapper's "last full run"
                                         stamp) if it's older than the request, so the
                                         every-minute job does update + SMART now
  --deploy-stamp FILE --deploy-command C run C (e.g. start homelab-deploy) if FILE is
                                         missing or older than the request, then touch FILE
--deploy-status FILE: forward this machine's updater status (JSON: commit, branch, last
check, result, git version), shown on the dashboard's Deployments screen.

--url may also be the SMART URL (.../api/smart): it's rewritten to /api/metrics,
so OPNsense's run.sh can pass the same arguments to both agents.
"""
import argparse
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "metrics_agent.py/3"   # bump with LATEST_AGENTS in metrics.py
SYSTEM = platform.system().lower()   # 'linux' | 'freebsd'


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def cpu_util(a, b):
    """Percent busy between two (busy, total) tick tuples."""
    db, dt = b[0] - a[0], b[1] - a[1]
    return round(100.0 * db / dt, 1) if dt > 0 else 0.0


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
def linux_cpu_ticks():
    out = {}
    for line in read("/proc/stat").splitlines():
        if line.startswith("cpu"):
            name, *vals = line.split()
            v = [int(x) for x in vals[:8]] + [0] * (8 - len(vals[:8]))
            idle = v[3] + v[4]                       # idle + iowait
            total = sum(v)
            out[name] = (total - idle, total)
    return out


def linux_net():
    out = {}
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        f = rest.split()
        if len(f) >= 9 and os.path.exists(f"/sys/class/net/{name}/device"):   # physical NICs only
            out[name] = (int(f[0]), int(f[8]))
    return out


def linux_disks():
    out = {}
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) < 13:
            continue
        name = f[2]
        if re.match(r"^(loop|ram|zram|sr|fd|dm-)", name) or not os.path.exists(f"/sys/block/{name}"):
            continue
        out[name] = (int(f[5]) * 512, int(f[9]) * 512, int(f[12]))    # read B, written B, busy ms
    return out


def linux_procs():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        s = read(f"/proc/{pid}/stat")
        if not s:
            continue
        comm = s[s.find("(") + 1:s.rfind(")")]
        f = s[s.rfind(")") + 2:].split()
        try:
            out[pid] = (comm, int(f[11]) + int(f[12]), int(f[21]) * os.sysconf("SC_PAGE_SIZE"))
        except (IndexError, ValueError):
            pass
    return out


def linux_static():
    info = read("/proc/cpuinfo")
    model = next((l.split(":", 1)[1].strip() for l in info.splitlines() if l.startswith("model name")), platform.processor())
    phys = {l.split(":")[1].strip() for l in info.splitlines() if l.startswith("physical id")}
    cores_per = next((int(l.split(":")[1]) for l in info.splitlines() if l.startswith("cpu cores")), None)
    threads = os.cpu_count() or 1
    sockets = max(1, len(phys))
    mem = {k.strip(): int(v.split()[0]) * 1024 for k, v in
           (l.split(":", 1) for l in read("/proc/meminfo").splitlines() if ":" in l)}
    temp = None
    for hw in sorted(os.listdir("/sys/class/hwmon")) if os.path.isdir("/sys/class/hwmon") else []:
        base = f"/sys/class/hwmon/{hw}"
        if read(f"{base}/name").strip() in ("coretemp", "k10temp", "zenpower"):
            vals = [int(read(f"{base}/{f}") or 0) / 1000 for f in os.listdir(base) if re.match(r"temp\d+_input$", f)]
            if vals:
                temp = round(max(vals), 1)
                break
    speeds = {}
    for nic in os.listdir("/sys/class/net"):
        try:
            sp = int(read(f"/sys/class/net/{nic}/speed").strip() or -1)
            if sp > 0:
                speeds[nic] = sp
        except ValueError:
            pass
    return {
        "model": model, "sockets": sockets, "cores": (cores_per or threads) * sockets, "threads": threads,
        "temp_c": temp,
        "mem": {"total": mem.get("MemTotal"), "used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
                "swap_total": mem.get("SwapTotal"), "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)},
        "uptime_s": int(float(read("/proc/uptime").split()[0] or 0)),
        "pending_reboot": os.path.exists("/var/run/reboot-required"),
        "speeds": speeds,
    }


# ---------------------------------------------------------------------------
# FreeBSD (OPNsense)
# ---------------------------------------------------------------------------
def sysctl(name):
    return run(["sysctl", "-n", name]).strip()


def freebsd_cpu_ticks():
    """user nice sys intr idle, per CPU from kern.cp_times plus the aggregate."""
    vals = [int(x) for x in sysctl("kern.cp_times").split()]
    out = {}
    for i in range(0, len(vals) - len(vals) % 5, 5):
        u, n, s, it, idle = vals[i:i + 5]
        out[f"cpu{i // 5}"] = (u + n + s + it, u + n + s + it + idle)
    if out:
        out["cpu"] = (sum(v[0] for v in out.values()), sum(v[1] for v in out.values()))
    return out


def parse_netstat_ibn(text):
    """netstat -ibn: Link# rows -> {iface: (ibytes, obytes)}."""
    lines = text.splitlines()
    if not lines:
        return {}
    hdr = lines[0].split()
    try:
        ii, oi = hdr.index("Ibytes"), hdr.index("Obytes")
    except ValueError:
        return {}
    out = {}
    for line in lines[1:]:
        f = line.split()
        if len(f) < len(hdr) - 1 or not f[2].startswith("<Link"):
            continue
        name = f[0]
        if re.match(r"^(lo|enc|pflog|pfsync|ovpn|wg|tun|tap|gif|gre)\d*", name):
            continue
        # The Address column is empty for some link rows: count from the right.
        off = len(hdr) - len(f)
        try:
            out[name] = (int(f[ii - off]), int(f[oi - off]))
        except (ValueError, IndexError):
            pass
    return out


def parse_iostat_x(text):
    """Last 'device ...' block of `iostat -x`: {dev: (kB_read/s, kB_written/s, busy%)}."""
    blocks, cur, hdr = [], None, None
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "device":
            hdr, cur = f, {}
            blocks.append(cur)
        elif hdr and cur is not None and len(f) == len(hdr):
            row = dict(zip(hdr, f))
            try:
                cur[f[0]] = (float(row.get("kr/s", 0)), float(row.get("kw/s", 0)), float(row.get("%b", 0)))
            except ValueError:
                pass
    return blocks[-1] if blocks else {}


def parse_ifconfig_speeds(text):
    speeds, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^(\S+?):\s+flags=", line)
        if m:
            cur = m.group(1)
            continue
        m = re.search(r"media:.*?\((\d+)(G?)base", line)
        if cur and m:
            speeds[cur] = int(m.group(1)) * (1000 if m.group(2) else 1)
    return speeds


# FreeBSD kernel pseudo-processes that aren't work: "idle" is charged with all unused CPU time.
NOT_WORK = {"idle"}


def top_memory(rows, n=3):
    """Memory by program name (summed over its processes, e.g. 12 browser tabs = one row)."""
    by = {}
    for r in rows:
        if r.get("mem"):
            name = r["name"]
            m, c = by.get(name, (0, 0))
            by[name] = (m + r["mem"], c + 1)
    return [{"name": k, "mem": m, "n": c} for k, (m, c) in sorted(by.items(), key=lambda kv: -kv[1][0])[:n]]


def parse_ps(text, threads):
    rows = []
    for line in text.splitlines()[1:]:
        f = line.split(None, 2)
        if len(f) == 3:
            name = os.path.basename(f[2].strip())
            if name in NOT_WORK:
                continue
            try:
                rows.append({"name": name, "cpu_pct": round(float(f[0]) / threads, 1), "mem": int(f[1]) * 1024})
            except ValueError:
                pass
    return rows


def freebsd_static():
    threads = int(sysctl("hw.ncpu") or 1)
    temps = [float(m.group(1)) for m in re.finditer(r"dev\.cpu\.\d+\.temperature: ([\d.]+)C", run(["sysctl", "dev.cpu"]))]
    page = int(sysctl("hw.pagesize") or 4096)
    phys = int(sysctl("hw.physmem") or 0)
    free = sum(int(sysctl(f"vm.stats.vm.v_{k}_count") or 0) for k in ("free", "inactive")) * page
    swap_total = swap_used = 0
    for line in run(["swapinfo", "-k"]).splitlines()[1:]:
        f = line.split()
        if len(f) >= 3 and f[1].isdigit():
            swap_total += int(f[1]) * 1024
            swap_used += int(f[2]) * 1024
    m = re.search(r"sec = (\d+)", sysctl("kern.boottime"))
    return {
        "model": sysctl("hw.model"), "sockets": 1, "cores": int(sysctl("kern.smp.cores") or threads), "threads": threads,
        "temp_c": round(max(temps), 1) if temps else None,
        "mem": {"total": phys, "used": max(0, phys - free), "swap_total": swap_total, "swap_used": swap_used},
        "uptime_s": int(time.time() - int(m.group(1))) if m else None,
        "pending_reboot": False,
        "speeds": parse_ifconfig_speeds(run(["ifconfig", "-a"])),
    }


# ---------------------------------------------------------------------------
def nvidia_gpus():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    q = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,utilization.encoder,utilization.decoder"
    gpus = []
    for line in run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"]).splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 8:
            continue
        n = lambda x: float(x) if re.match(r"^-?[\d.]+$", x) else None
        gpus.append({"name": f[0], "util_pct": n(f[1]), "mem_used": (n(f[2]) or 0) * 1048576,
                     "mem_total": (n(f[3]) or 0) * 1048576, "temp_c": n(f[4]), "power_w": n(f[5]),
                     "enc_pct": n(f[6]), "dec_pct": n(f[7])})
    return gpus


def collect(interval=1.0):
    linux = SYSTEM == "linux"
    iostat = None
    if not linux:   # FreeBSD: iostat measures the same window as the snapshots below
        try:
            iostat = subprocess.Popen(["iostat", "-x", "-c", "2", "-w", str(int(max(1, interval)))],
                                      stdout=subprocess.PIPE, text=True)
        except OSError:
            pass
    t0 = time.monotonic()
    cpu0 = linux_cpu_ticks() if linux else freebsd_cpu_ticks()
    net0 = linux_net() if linux else parse_netstat_ibn(run(["netstat", "-ibn"]))
    disk0 = linux_disks() if linux else None
    procs0 = linux_procs() if linux else None
    time.sleep(interval)
    dt = time.monotonic() - t0
    cpu1 = linux_cpu_ticks() if linux else freebsd_cpu_ticks()
    net1 = linux_net() if linux else parse_netstat_ibn(run(["netstat", "-ibn"]))
    st = linux_static() if linux else freebsd_static()

    per_core = [cpu_util(cpu0[k], cpu1[k]) for k in sorted((k for k in cpu1 if k != "cpu" and k in cpu0),
                                                          key=lambda k: int(k[3:]))]
    net = [{"name": n, "rx_bps": round((net1[n][0] - net0[n][0]) / dt, 1), "tx_bps": round((net1[n][1] - net0[n][1]) / dt, 1),
            "speed_mbps": st["speeds"].get(n)} for n in sorted(net1) if n in net0]

    if linux:
        disk1, procs1 = linux_disks(), linux_procs()
        disks = [{"name": d, "read_bps": round((disk1[d][0] - disk0[d][0]) / dt, 1),
                  "write_bps": round((disk1[d][1] - disk0[d][1]) / dt, 1),
                  "busy_pct": round(min(100.0, (disk1[d][2] - disk0[d][2]) / (dt * 10)), 1)}
                 for d in sorted(disk1) if d in disk0]
        hz = os.sysconf("SC_CLK_TCK")
        top = sorted(({"name": c, "cpu_pct": round(100.0 * (t - procs0[p][1]) / hz / dt / st["threads"], 1), "mem": m}
                      for p, (c, t, m) in procs1.items() if p in procs0), key=lambda r: -r["cpu_pct"])
        procs = {"count": len(procs1), "top": top[:3],
                 "top_mem": top_memory({"name": c, "mem": m} for c, _, m in procs1.values())}
    else:
        rows = parse_iostat_x(iostat.communicate(timeout=10)[0]) if iostat else {}
        disks = [{"name": d, "read_bps": r * 1024, "write_bps": w * 1024, "busy_pct": b}
                 for d, (r, w, b) in sorted(rows.items()) if not d.startswith(("pass", "cd"))]
        rows = parse_ps(run(["ps", "-axo", "pcpu,rss,comm", "-r"]), st["threads"])
        procs = {"count": len(rows), "top": rows[:3], "top_mem": top_memory(rows)}

    return {
        "schema": 1, "agent": VERSION, "os": SYSTEM,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "uptime_s": st["uptime_s"], "pending_reboot": st["pending_reboot"],
        "cpu": {"model": st["model"], "sockets": st["sockets"], "cores": st["cores"], "threads": st["threads"],
                "util_pct": cpu_util(cpu0["cpu"], cpu1["cpu"]) if "cpu" in cpu1 else None,
                "per_core_pct": per_core, "load": [round(x, 2) for x in os.getloadavg()], "temp_c": st["temp_c"]},
        "mem": st["mem"], "gpus": nvidia_gpus(), "net": net, "disks": disks, "procs": procs,
    }


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [metrics] {msg}", file=sys.stderr, flush=True)


def read_deploy_status(path):
    """The updater's status file, with *_at times turned into ages (*_age_s), so the
    dashboard doesn't depend on this machine's clock."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            st = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(st, dict):
        return None
    now = time.time()
    for k in ("checked_at", "changed_at"):
        v = st.pop(k, None)
        if isinstance(v, (int, float)):
            st[k[:-3] + "_age_s"] = max(0, round(now - v))
    v = st.pop("running_since", None)          # set by the updater while it's working
    if isinstance(v, (int, float)):
        st["running_for_s"] = max(0, round(now - v))
    return st


def handle_deploy(info, args):
    """Bring the local update forward if a deploy request is newer than our stamp."""
    if not info or not args.deploy_stamp:
        return
    try:
        stamp_age = time.time() - os.stat(args.deploy_stamp).st_mtime
    except OSError:
        stamp_age = None                           # no stamp
    newer = stamp_age is None or stamp_age > float(info.get("age_s", 0))
    if args.deploy_command:
        if newer:
            try:
                subprocess.run(shlex.split(args.deploy_command), timeout=30, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                log(f"deploy requested: ran {args.deploy_command!r}")
            except (OSError, subprocess.SubprocessError) as e:
                log(f"deploy requested but {args.deploy_command!r} failed: {e}")
            with open(args.deploy_stamp, "a"):
                os.utime(args.deploy_stamp)
    elif stamp_age is not None and newer:           # no stamp = a full run is already due
        try:
            os.unlink(args.deploy_stamp)
            log("deploy requested: update + SMART on the next run (within a minute)")
        except OSError as e:
            log(f"deploy requested but could not clear {args.deploy_stamp}: {e}")


def post(url, token_file, sample):
    with open(token_file) as f:
        token = f.read().strip()
    req = urllib.request.Request(url, data=json.dumps(sample).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": VERSION})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            try:
                return None, json.loads(r.read() or b"{}")
            except ValueError:
                return None, {}
    except urllib.error.HTTPError as e:
        return f"dashboard rejected metrics: HTTP {e.code} {e.read().decode()[:200]}", {}
    except (urllib.error.URLError, OSError) as e:
        return f"could not reach dashboard at {url}: {e}", {}


def loop(args, url):
    """Send a sample every --loop seconds, each averaged over (almost) the whole interval.
    Logs only when the state changes (failing / recovered), so the log stays small.
    Exits 0 when this file changes on disk (self-update), so the supervisor (systemd,
    daemon -r, Task Scheduler) restarts it on the new code."""
    me, mtime = os.path.abspath(__file__), os.stat(__file__).st_mtime
    failing = None
    log(f"started: {VERSION}, every {args.loop:g} s -> {url}")
    while True:
        t0 = time.monotonic()
        try:
            sample = collect(max(1.0, args.loop - 1.0))
            sample["host"] = args.name
            if args.deploy_status:
                sample["deploy_status"] = read_deploy_status(args.deploy_status)
            err, resp = post(url, args.token_file, sample)
            handle_deploy(resp.get("deploy") if isinstance(resp, dict) else None, args)
        except Exception as e:     # never let one bad sample kill the loop
            err = f"collect failed: {type(e).__name__}: {e}"
        if err and err != failing:
            log(err)
        elif not err and failing:
            log("recovered")
        failing = err
        try:
            if os.stat(me).st_mtime != mtime:
                log("agent file changed on disk: exiting so the new version starts")
                return 0
        except OSError:
            return 0
        time.sleep(max(0.0, t0 + args.loop - time.monotonic()))


def main():
    ap = argparse.ArgumentParser(description="Push compute metrics to the rack dashboard")
    ap.add_argument("--url")
    ap.add_argument("--token-file")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0].lower())
    ap.add_argument("--interval", type=float, default=1.0, help="one-shot mode: seconds between the two snapshots")
    ap.add_argument("--loop", type=float, metavar="SECONDS",
                    help="run forever, sending a sample every SECONDS (averaged over the interval)")
    ap.add_argument("--deploy-status", help="updater status file to forward (see above)")
    ap.add_argument("--deploy-stamp", help="see 'Deploy now' above")
    ap.add_argument("--deploy-command", help="see 'Deploy now' above")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smartctl", help=argparse.SUPPRESS)   # accepted (and ignored) so run.sh can pass SMART args
    ap.add_argument("--timeout", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.dry_run:
        sample = collect(args.interval)
        sample["host"] = args.name
        print(json.dumps(sample, indent=1))
        return 0
    if not args.url or not args.token_file:
        ap.error("--url and --token-file are required unless --dry-run")
    url = re.sub(r"/api/smart/?$", "/api/metrics", args.url)
    if args.loop:
        if args.loop < 2:
            ap.error("--loop must be at least 2 seconds")
        return loop(args, url)
    sample = collect(args.interval)
    sample["host"] = args.name
    if args.deploy_status:
        sample["deploy_status"] = read_deploy_status(args.deploy_status)
    err, _ = post(url, args.token_file, sample)
    if err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
