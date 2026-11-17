"""
SMART data for the rack dashboard.

Agents on each machine POST a report to /api/smart:

    {
      "schema": 1,
      "host": "homeshare",                 # [A-Za-z0-9._-], 1-64 chars
      "agent": "Send-SmartReport.ps1/2",
      "os": "windows",
      "collected_at": "2026-11-16T05:00:00Z",
      "drives": [ <raw `smartctl -j -a` output>, ... ],
      "windows_disks": [ <Get-PhysicalDisk + reliability counters>, ... ],   # optional
      "errors": [ "smartctl not found", ... ]                                 # optional
    }

Raw reports are stored as-is (one per host) and normalized when read, so
changes to the health rules here apply to existing data immediately.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_DRIVES = 128

OK, WARN, CRIT, UNKNOWN = "ok", "warn", "crit", "unknown"
_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, CRIT: 3}

# Temperature thresholds (deg C): (warn, crit). Fallback for drives that don't
# report their own rated limit; see temp_limits().
TEMP_LIMITS = {"hdd": (50, 58), "ssd": (60, 70), "nvme": (70, 80)}
RATED_WARN_MARGIN = 10   # warn this far below a drive's own rated max

# ATA attributes whose *normalized* value is "percent of life remaining".
ATA_LIFE_REMAINING = {177, 202, 231, 233}

# smartctl exit status bits (see `man smartctl`, RETURN VALUES)
EXIT_OPEN_FAILED = 0x02        # device open failed: we have no data
EXIT_CMD_FAILED = 0x04         # some command failed: common and harmless with -x
                               # (optional logs a drive doesn't support)
EXIT_DISK_FAILING = 0x08
EXIT_PREFAIL_BELOW = 0x10
EXIT_PAST_BELOW = 0x20
EXIT_ERROR_LOG = 0x40
EXIT_SELFTEST_ERRORS = 0x80


# Latest agent versions in this repo. Bump when an agent changes, so the
# dashboard can flag hosts still running an older copy.
LATEST_AGENTS = {"smart_agent.py": 2, "Send-SmartReport.ps1": 2}


def agent_status(agent):
    """'smart_agent.py/1' -> {'name', 'version', 'latest', 'outdated'}"""
    name, _, ver = str(agent or "").partition("/")
    latest = LATEST_AGENTS.get(name)
    v = _int(ver)
    return {"name": name, "version": v, "latest": latest,
            "outdated": bool(latest and v is not None and v < latest)}


def ata_error_log_count(raw):
    """Device error log count from smartctl JSON (-x gives 'extended', -a 'summary')."""
    log = raw.get("ata_smart_error_log") or {}
    for key in ("extended", "summary"):
        n = _int((log.get(key) or {}).get("count"))
        if n is not None:
            return n
    return None


def worst(*levels):
    return max(levels, key=lambda lv: _RANK[lv]) if levels else OK


def norm_serial(s):
    return re.sub(r"[\s_-]", "", str(s or "")).upper()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Health:
    def __init__(self):
        self.level, self.reasons, self.notes = OK, [], []

    def add(self, level, reason):
        self.level = worst(self.level, level)
        self.reasons.append({"level": level, "text": reason})


# ---------------------------------------------------------------------------
# smartctl JSON -> normalized drive
# ---------------------------------------------------------------------------
def normalize_smartctl(raw, baselines=None):
    dev = raw.get("device") or {}
    protocol = (dev.get("protocol") or "").upper()
    exit_status = _int((raw.get("smartctl") or {}).get("exit_status")) or 0

    model = raw.get("model_name") or " ".join(
        x for x in (raw.get("scsi_vendor"), raw.get("scsi_product") or raw.get("scsi_model_name")) if x
    ) or dev.get("name") or "unknown"

    if protocol == "NVME":
        kind, bus = "nvme", "NVMe"
    else:
        rr = raw.get("rotation_rate")
        kind = "hdd" if isinstance(rr, int) and rr > 0 else "ssd" if rr is not None else None
        if protocol == "SCSI":
            bus = ((raw.get("scsi_transport_protocol") or {}).get("name") or "SAS").upper()
            bus = "SAS" if "SAS" in bus else bus
        else:
            bus = "SATA" if protocol == "ATA" else protocol or "?"

    d = {
        "source": "smartctl",
        "device": dev.get("info_name") or dev.get("name"),
        "model": model.strip(),
        "serial": raw.get("serial_number"),
        "firmware": raw.get("firmware_version"),
        "capacity_bytes": (raw.get("user_capacity") or {}).get("bytes") or raw.get("nvme_total_capacity"),
        "kind": kind,
        "bus": bus,
        "temp_c": (raw.get("temperature") or {}).get("current"),
        "power_on_hours": (raw.get("power_on_time") or {}).get("hours"),
        "wear_pct": None,          # percent of rated endurance used
        "counters": {},            # the error counters that matter for this drive type
    }
    h = Health()

    if raw.get("agent_error"):
        h.add(UNKNOWN, raw["agent_error"])
    # Unknown only when we really got nothing: open failed, or a command failed
    # and the drive didn't even identify itself.
    got_identity = bool(raw.get("serial_number") or raw.get("model_name"))
    if exit_status & EXIT_OPEN_FAILED or (exit_status & EXIT_CMD_FAILED and not got_identity):
        msgs = "; ".join(m.get("string", "") for m in (raw.get("smartctl") or {}).get("messages", []))
        h.add(UNKNOWN, "smartctl could not read this device" + (f": {msgs}" if msgs else ""))

    passed = (raw.get("smart_status") or {}).get("passed")
    if passed is False or exit_status & EXIT_DISK_FAILING:
        h.add(CRIT, "SMART overall health: FAILED")
    if exit_status & EXIT_PREFAIL_BELOW:
        h.add(CRIT, "pre-fail attribute at or below threshold")
    if exit_status & EXIT_PAST_BELOW:
        h.add(WARN, "an attribute was below threshold in the past")
    if exit_status & EXIT_SELFTEST_ERRORS:
        h.add(WARN, "self-test log reports errors")

    endurance = (raw.get("endurance_used") or {}).get("current_percent")
    if endurance is not None:
        d["wear_pct"] = endurance

    if protocol == "ATA":
        _ata(raw, d, h, exit_status, (baselines or {}).get(norm_serial(raw.get("serial_number"))))
    elif protocol == "NVME":
        _nvme(raw, d, h)
    elif protocol == "SCSI":
        _scsi(raw, d, h)

    if d["kind"] is None:
        d["kind"] = "ssd" if d["wear_pct"] is not None else "hdd"
    d["temp_limits"] = temp_limits(d, raw)
    _temp_and_wear(d, h)
    d["health"], d["reasons"], d["notes"] = h.level, h.reasons, h.notes
    return d


def _ata(raw, d, h, exit_status, baseline=None):
    attrs = {a.get("id"): a for a in (raw.get("ata_smart_attributes") or {}).get("table", [])}

    def rawv(i):
        a = attrs.get(i)
        return _int((a.get("raw") or {}).get("value")) if a else None

    c = d["counters"]
    for key, i in (("reallocated", 5), ("pending", 197), ("offline_uncorrectable", 198),
                   ("reported_uncorrect", 187), ("crc_errors", 199)):
        v = rawv(i)
        if v is not None:
            c[key] = v

    if d["power_on_hours"] is None and rawv(9) is not None:
        d["power_on_hours"] = rawv(9) & 0xFFFFFFFF
    if d["wear_pct"] is None:
        for i in ATA_LIFE_REMAINING:
            if i not in attrs:
                continue
            left = _int(attrs[i].get("value"))
            # 231 SSD_Life_Left: vendors disagree on which column holds "life left".
            # Kingston/Phison put it in RAW (e.g. raw 86, normalized 14 = used), so
            # prefer raw when it's a plausible percentage. Device Statistics
            # (endurance_used, from `smartctl -x`) beats all of this when present.
            raw231 = rawv(231) if i == 231 else None
            if raw231 is not None and 0 <= raw231 <= 100:
                left = raw231
            if left is not None:
                d["wear_pct"] = max(0, 100 - left)
                break

    if c.get("pending"):
        h.add(CRIT, f"{c['pending']} pending sectors")
    if c.get("offline_uncorrectable"):
        h.add(CRIT, f"{c['offline_uncorrectable']} offline uncorrectable sectors")
    if c.get("reallocated"):
        h.add(CRIT if c["reallocated"] >= 100 else WARN, f"{c['reallocated']} reallocated sectors")
    if c.get("reported_uncorrect"):
        h.add(WARN, f"{c['reported_uncorrect']} reported uncorrectable errors")
    if c.get("crc_errors"):
        h.add(WARN, f"{c['crc_errors']} interface CRC errors (check cable/backplane)")
    # Device error log. Old entries never clear (a 6-year-old drive often has a
    # few), so compare against the count when the dashboard first saw the drive:
    # warn only on NEW errors. Without a count, fall back to the exit bit.
    count = ata_error_log_count(raw)
    if count is not None:
        c["error_log"] = count
        base = (baseline or {}).get("error_log")
        if base is None:
            if count:
                h.add(WARN, f"{count} errors in device log (no baseline yet)")
        elif count > base:
            h.add(WARN, f"{count - base} new error{'s' if count - base > 1 else ''} in device log "
                        f"since {baseline.get('since', 'first seen')} ({count} total)")
        elif count:
            h.notes.append(f"{count} old errors in device log, none new since {baseline.get('since', 'first seen')}")
    elif exit_status & EXIT_ERROR_LOG:
        h.add(WARN, "device error log has entries")


def _nvme(raw, d, h):
    log = raw.get("nvme_smart_health_information_log") or {}
    c = d["counters"]
    for key in ("media_errors", "num_err_log_entries", "unsafe_shutdowns"):
        if log.get(key) is not None:
            c[key] = log[key]
    if d["power_on_hours"] is None:
        d["power_on_hours"] = log.get("power_on_hours")
    if d["temp_c"] is None:
        d["temp_c"] = log.get("temperature")
    if d["wear_pct"] is None:
        d["wear_pct"] = log.get("percentage_used")

    cw = _int(log.get("critical_warning")) or 0
    if cw:
        h.add(CRIT, f"NVMe critical warning flags 0x{cw:02x}")
    spare, thresh = log.get("available_spare"), log.get("available_spare_threshold")
    if spare is not None and thresh is not None and spare < thresh:
        h.add(CRIT, f"available spare {spare}% below threshold {thresh}%")
    if c.get("media_errors"):
        h.add(CRIT, f"{c['media_errors']} media/data integrity errors")


def _scsi(raw, d, h):
    c = d["counters"]
    defects = raw.get("scsi_grown_defect_list")
    if defects is not None:
        c["grown_defects"] = defects
    log = raw.get("scsi_error_counter_log") or {}
    unc = [_int((log.get(k) or {}).get("total_uncorrected_errors")) for k in ("read", "write", "verify")]
    if any(u is not None for u in unc):
        c["uncorrected_errors"] = sum(u or 0 for u in unc)
    if d["wear_pct"] is None and raw.get("scsi_percentage_used_endurance_indicator") is not None:
        d["wear_pct"] = raw["scsi_percentage_used_endurance_indicator"]

    if c.get("uncorrected_errors"):
        h.add(CRIT, f"{c['uncorrected_errors']} uncorrected read/write/verify errors")
    if defects:
        h.add(CRIT if defects >= 100 else WARN, f"{defects} grown defects")


def temp_limits(d, raw=None):
    """(warn, crit, source). Prefer the drive's own rated max operating
    temperature (SCT, read by `smartctl -x`; NVMe warning threshold), else
    the generic per-type limits."""
    rated = _int((raw.get("temperature") or {}).get("op_limit_max")) if raw else None
    if rated is not None and 40 <= rated <= 100:
        return [rated - RATED_WARN_MARGIN, rated, "rated"]
    w, c = TEMP_LIMITS.get(d["kind"], TEMP_LIMITS["hdd"])
    return [w, c, "generic"]


def _temp_and_wear(d, h):
    t = d.get("temp_c")
    if not d.get("temp_limits"):
        d["temp_limits"] = temp_limits(d)
    warn_t, crit_t, src = d["temp_limits"]
    if t is not None:
        if t >= crit_t:
            h.add(CRIT, f"temperature {t}°C ({'drive-rated' if src == 'rated' else 'limit'} max {crit_t}°C)")
        elif t >= warn_t:
            h.add(WARN, f"temperature {t}°C (warn at {warn_t}°C{', drive rated ' + str(crit_t) + '°C' if src == 'rated' else ''})")
    w = d.get("wear_pct")
    if w is not None:
        if w >= 90:
            h.add(CRIT, f"{w}% of rated endurance used")
        elif w >= 75:
            h.add(WARN, f"{w}% of rated endurance used")


# ---------------------------------------------------------------------------
# Windows Get-PhysicalDisk / Get-StorageReliabilityCounter -> normalized drive
# ---------------------------------------------------------------------------
def normalize_windows_disk(w):
    media = str(w.get("media_type") or "").upper()
    bus = str(w.get("bus_type") or "").upper()
    kind = "nvme" if bus == "NVME" else "ssd" if media == "SSD" else "hdd" if media == "HDD" else None
    d = {
        "source": "windows",
        "device": f"PhysicalDisk{w.get('device_id')}" if w.get("device_id") is not None else None,
        "model": (w.get("friendly_name") or "unknown").strip(),
        "serial": (w.get("serial") or "").strip() or None,
        "firmware": w.get("firmware"),
        "capacity_bytes": w.get("size"),
        "kind": kind or "hdd",
        "bus": {"NVME": "NVMe"}.get(bus, bus or "?"),
        "temp_c": w.get("temperature") or None,      # 0 means "not reported"
        "power_on_hours": w.get("power_on_hours"),
        "wear_pct": w.get("wear") if w.get("wear") else None,
        "counters": {},
        "pool": w.get("pool"),
    }
    h = Health()
    for key in ("read_errors_uncorrected", "write_errors_uncorrected"):
        if w.get(key) is not None:
            d["counters"][key] = w[key]
    status = str(w.get("health_status") or "")
    if status.lower() == "unhealthy":
        h.add(CRIT, "Windows reports disk Unhealthy")
    elif status.lower() == "warning":
        h.add(WARN, "Windows reports disk Warning")
    unc = sum(d["counters"].get(k) or 0 for k in ("read_errors_uncorrected", "write_errors_uncorrected"))
    if unc:
        h.add(CRIT, f"{unc} uncorrected read/write errors")
    if not w.get("reliability_available", True):
        h.add(UNKNOWN, "no reliability counters (smartctl could not see this disk either)")
    _temp_and_wear(d, h)
    d["health"], d["reasons"], d["notes"] = h.level, h.reasons, h.notes
    return d


def merge_windows(drives, windows_disks):
    """Attach pool names / Windows health to smartctl drives (matched by serial);
    add Windows-only disks that smartctl could not see."""
    by_serial = {norm_serial(d.get("serial")): d for d in drives if d.get("serial")}

    def find(serial):
        s = norm_serial(serial)
        if not s:
            return None
        if s in by_serial:
            return by_serial[s]
        # Windows and smartctl sometimes format the same serial differently
        # (extra prefix/suffix, e.g. SAS). Accept containment for long serials.
        hits = [d for k, d in by_serial.items() if len(k) >= 6 and len(s) >= 6 and (k in s or s in k)]
        return hits[0] if len(hits) == 1 else None

    for w in windows_disks or []:
        wd = normalize_windows_disk(w)
        match = find(wd["serial"])
        if match is None:
            drives.append(wd)
            continue
        match["pool"] = wd.get("pool")
        win_reasons = [r for r in wd["reasons"] if r["text"].startswith("Windows reports")]
        for r in win_reasons:
            match["reasons"].append(r)
            match["health"] = worst(match["health"], r["level"])
    return drives


# ---------------------------------------------------------------------------
# Host report -> view
# ---------------------------------------------------------------------------
# Devices smartctl --scan lists that are not drives (enclosure/backplane
# controllers). Shown as an agent note instead of an "unknown" drive.
NON_DRIVE_RE = re.compile(r"^/dev/(ses\d+|sg\d+.*enclosure)$")


def _is_non_drive(raw):
    name = str((raw.get("device") or {}).get("name") or "")
    return bool(NON_DRIVE_RE.match(name)) and not raw.get("serial_number")


def build_host_view(report, received_at, now, stale_after, baselines=None):
    drives, notes = [], []
    for raw in report.get("drives") or []:
        if isinstance(raw, dict) and _is_non_drive(raw):
            notes.append(f"{raw['device']['name']}: enclosure controller, not a drive (skipped)")
            continue
        if isinstance(raw, dict):
            try:
                drives.append(normalize_smartctl(raw, baselines))
            except Exception as e:  # one weird drive must not hide the rest
                drives.append({"source": "smartctl", "model": "unparseable report", "health": UNKNOWN,
                               "reasons": [{"level": UNKNOWN, "text": f"parse error: {e}"}], "counters": {}})
    merge_windows(drives, report.get("windows_disks"))

    summary = {OK: 0, WARN: 0, CRIT: 0, UNKNOWN: 0}
    for d in drives:
        summary[d["health"]] += 1
    age = max(0, now - received_at)
    errors = ([str(e) for e in (report.get("errors") or [])] + notes)[:20]
    # Agent notes (e.g. a device smartctl can't open) are shown but don't color
    # the host: only real drive problems do. No drives at all -> unknown.
    level = worst(*(d["health"] for d in drives)) if drives else UNKNOWN
    return {
        "host": report["host"],
        "os": report.get("os"),
        "agent": report.get("agent"),
        "agent_status": agent_status(report.get("agent")),
        "collected_at": report.get("collected_at"),
        "received_at": received_at,
        "age_s": int(age),
        "stale": age > stale_after,
        "health": level,
        "summary": summary,
        "errors": errors,
        "drives": drives,
    }


class SmartStore:
    """Latest raw report per host, persisted to <state_dir>/smart.json.

    Also keeps per-drive baselines (by serial) in <state_dir>/baselines.json:
    the device error log count when a drive was first seen. Delete that file
    to re-baseline every drive (e.g. after investigating a warning).
    """

    def __init__(self, state_dir, stale_after=45 * 60, expire_after=7 * 86400, order=()):
        self.path = Path(state_dir) / "smart.json" if state_dir else None
        self.stale_after, self.expire_after = stale_after, expire_after
        self.order = [o.strip() for o in order if o.strip()]
        self.lock = threading.Lock()
        self.hosts = {}  # host -> {"received_at": float, "report": dict}
        self.baselines = {}  # normalized serial -> {"error_log": int, "since": "YYYY-MM-DD"}
        self.baseline_path = self.path.with_name("baselines.json") if self.path else None
        if self.path and self.path.exists():
            try:
                self.hosts = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.hosts = {}
        if self.baseline_path and self.baseline_path.exists():
            try:
                self.baselines = json.loads(self.baseline_path.read_text())
            except (OSError, ValueError):
                self.baselines = {}
        # Seed baselines from reports stored before this feature existed.
        for v in self.hosts.values():
            self._update_baselines(v["report"], v["received_at"])
        self._save_baselines()

    def _update_baselines(self, report, when):
        changed = False
        for raw in report.get("drives") or []:
            if not isinstance(raw, dict):
                continue
            serial, count = norm_serial(raw.get("serial_number")), ata_error_log_count(raw)
            if serial and count is not None and serial not in self.baselines:
                since = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d")
                self.baselines[serial] = {"error_log": count, "since": since}
                changed = True
        return changed

    def _save_baselines(self):
        if not self.baseline_path:
            return
        tmp = self.baseline_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.baselines))
        os.replace(tmp, self.baseline_path)

    @staticmethod
    def validate(report):
        if not isinstance(report, dict):
            raise ValueError("report must be a JSON object")
        if not HOST_RE.match(str(report.get("host", ""))):
            raise ValueError("host must match [A-Za-z0-9._-]{1,64}")
        for key in ("drives", "windows_disks", "errors"):
            if key in report and not isinstance(report[key], list):
                raise ValueError(f"{key} must be a list")
        if len(report.get("drives") or []) + len(report.get("windows_disks") or []) > MAX_DRIVES:
            raise ValueError("too many drives")

    def put(self, report, now=None):
        self.validate(report)
        with self.lock:
            when = now or time.time()
            self.hosts[report["host"]] = {"received_at": when, "report": report}
            self._save()
            if self._update_baselines(report, when):
                self._save_baselines()

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.hosts))
        os.replace(tmp, self.path)

    def view(self, now=None):
        now = now or time.time()
        with self.lock:
            items = [(h, v) for h, v in self.hosts.items() if now - v["received_at"] < self.expire_after]
            baselines = dict(self.baselines)
        rank = {h: i for i, h in enumerate(self.order)}
        items.sort(key=lambda hv: (rank.get(hv[0], len(rank)), hv[0]))
        hosts = [build_host_view(v["report"], v["received_at"], now, self.stale_after, baselines)
                 for _, v in items]
        return {
            "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "stale_after_s": self.stale_after,
            "hosts": hosts,
        }


# ---------------------------------------------------------------------------
# Demo data (used by --demo and by the tests)
# ---------------------------------------------------------------------------
def _ata_raw(dev, model, serial, tb, rpm, temp, poh, attrs=(), passed=True, exit_status=0):
    table = [{"id": i, "name": n, "value": v, "worst": v, "thresh": t, "raw": {"value": r, "string": str(r)}}
             for i, n, v, t, r in attrs]
    return {
        "smartctl": {"version": [7, 4], "exit_status": exit_status},
        "device": {"name": dev, "info_name": dev, "type": "sat", "protocol": "ATA"},
        "model_name": model, "serial_number": serial, "firmware_version": "FW01",
        "user_capacity": {"bytes": int(tb * 1e12)}, "rotation_rate": rpm,
        "smart_status": {"passed": passed}, "temperature": {"current": temp},
        "power_on_time": {"hours": poh}, "ata_smart_attributes": {"table": table},
    }


def _sas_raw(dev, product, serial, temp, poh, defects=0, unc=0):
    return {
        "smartctl": {"version": [7, 4], "exit_status": 0},
        "device": {"name": dev, "info_name": dev, "type": "scsi", "protocol": "SCSI"},
        "scsi_vendor": "SEAGATE", "scsi_product": product, "model_name": f"SEAGATE {product}",
        "serial_number": serial, "user_capacity": {"bytes": 2_000_398_934_016}, "rotation_rate": 7200,
        "scsi_transport_protocol": {"name": "SAS (SPL-4)"},
        "smart_status": {"passed": True}, "temperature": {"current": temp}, "power_on_time": {"hours": poh},
        "scsi_grown_defect_list": defects,
        "scsi_error_counter_log": {k: {"total_uncorrected_errors": unc if k == "read" else 0}
                                   for k in ("read", "write", "verify")},
    }


def _nvme_raw(dev, model, serial, temp, poh, used, media_errors=0, cw=0):
    return {
        "smartctl": {"version": [7, 4], "exit_status": 0},
        "device": {"name": dev, "info_name": dev, "type": "nvme", "protocol": "NVMe"},
        "model_name": model, "serial_number": serial, "nvme_total_capacity": 256_060_514_304,
        "smart_status": {"passed": True}, "temperature": {"current": temp}, "power_on_time": {"hours": poh},
        "nvme_smart_health_information_log": {
            "critical_warning": cw, "temperature": temp, "available_spare": 100, "available_spare_threshold": 10,
            "percentage_used": used, "power_on_hours": poh, "media_errors": media_errors,
            "num_err_log_entries": 0, "unsafe_shutdowns": 7,
        },
    }


def _ssd_attrs(life_left, realloc=0):
    return [(5, "Reallocated_Sector_Ct", 100, 10, realloc), (177, "Wear_Leveling_Count", life_left, 0, 120),
            (199, "UDMA_CRC_Error_Count", 100, 0, 0)]


def _hdd_attrs(realloc=0, pending=0, crc=0):
    return [(5, "Reallocated_Sector_Ct", 100, 10, realloc), (197, "Current_Pending_Sector", 100, 0, pending),
            (198, "Offline_Uncorrectable", 100, 0, 0), (199, "UDMA_CRC_Error_Count", 200, 0, crc)]


def demo_reports():
    """Four fake hosts: one healthy, one warning, one critical, one stale."""
    m1 = [_ata_raw(f"/dev/sd{c}", "Samsung SSD 870 EVO 1TB", f"S6PTNZ0T{n:06d}", 1.0, 0, 33 + n, 15000 + n * 90,
                   _ssd_attrs(95 - n)) for n, c in enumerate("abcd")]
    m1 += [_sas_raw(f"/dev/sd{c}", "ST2000NM0023", f"Z1X0{n:04d}", 36 + n % 3, 61000 + n * 400,
                    defects=12 if n == 3 else 0) for n, c in enumerate("efghij")]
    m1.append(_ata_raw("/dev/sdk", "Kingston SA400S37240G", "50026B7782A1B2C3", 0.24, 0, 31, 9800, _ssd_attrs(88)))
    m1[-1]["ata_smart_error_log"] = {"extended": {"count": 10}}   # old errors -> grey note
    m1_win = [{"serial": f"S6PTNZ0T{n:06d}", "pool": "Main"} for n in range(4)] + \
             [{"serial": f"Z1X0{n:04d}", "pool": "Archive"} for n in range(6)]

    m2 = [_ata_raw(f"/dev/sd{c}", "Crucial MX500 1TB", f"2203E5F{n:05d}", 1.0, 0, 30 + n, 22000 + n * 50,
                   _ssd_attrs(90 - n * 2)) for n, c in enumerate("abcde")]
    m2.append(_ata_raw("/dev/sdf", "WDC WD20EFRX-68EUZN0", "WD-WCC4M0ABCDEF", 2.0, 5400, 41, 48000,
                       _hdd_attrs(realloc=8, pending=2)))
    m2_win = [{"serial": f"2203E5F{n:05d}", "pool": "Main"} for n in range(5)]

    now = time.time()
    return [
        (now - 120, {"schema": 1, "host": "opnsense", "os": "freebsd", "agent": "smart_agent.py/2",
                     "drives": [_ata_raw("/dev/ada0", "KINGSTON SA400S37240G", "50026B7782F0E1D2", 0.24, 0, 38,
                                         1300, _ssd_attrs(99))]}),
        (now - 300, {"schema": 1, "host": "optiplex-management", "os": "linux", "agent": "smart_agent.py/2",
                     "drives": [_nvme_raw("/dev/nvme0", "SK hynix PC401 NVMe 256GB", "ES0BN5ABC1234", 44, 31000, 9)]}),
        (now - 400, {"schema": 1, "host": "homeshare", "os": "windows", "agent": "Send-SmartReport.ps1/2",
                     "drives": m1, "windows_disks": m1_win}),
        (now - 3 * 3600, {"schema": 1, "host": "backup-compute", "os": "windows", "agent": "Send-SmartReport.ps1/2",
                          "drives": m2, "windows_disks": m2_win}),
    ]
