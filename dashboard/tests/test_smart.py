"""Run: python3 -m unittest discover dashboard/tests"""
import sys
import tempfile
import os
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import smart  # noqa: E402


def kingston_a400(with_devstat):
    """opnsense's real Kingston A400 240GB (2026-11-16), as smartctl -a / -x report it."""
    attrs = [(9, 100, 50116), (194, 70, 30), (231, 14, 86), (241, 100, 14362)]
    raw = {
        "smartctl": {"exit_status": 0},
        "device": {"name": "/dev/ada0", "type": "atacam", "protocol": "ATA"},
        "model_name": "KINGSTON SA400S37240G", "serial_number": "TESTSERIAL0001",
        "user_capacity": {"bytes": 240_057_409_536}, "rotation_rate": 0,
        "smart_status": {"passed": True}, "temperature": {"current": 30},
        "power_on_time": {"hours": 50116},
        "ata_smart_attributes": {"table": [
            {"id": i, "value": v, "worst": v, "thresh": 0, "raw": {"value": r}} for i, v, r in attrs]},
    }
    if with_devstat:  # -x reads Device Statistics -> smartctl emits endurance_used
        raw["endurance_used"] = {"current_percent": 14}
    return raw


class Wear(unittest.TestCase):
    def test_kingston_231_raw_is_life_left(self):
        d = smart.normalize_smartctl(kingston_a400(with_devstat=False))
        self.assertEqual(d["wear_pct"], 14)
        self.assertEqual(d["health"], smart.OK)

    def test_device_statistics_wins(self):
        self.assertEqual(smart.normalize_smartctl(kingston_a400(with_devstat=True))["wear_pct"], 14)

    def test_samsung_177_normalized_is_life_left(self):
        raw = smart._ata_raw("/dev/sda", "Samsung SSD 870 EVO 1TB", "S1", 1.0, 0, 33, 1000, smart._ssd_attrs(92))
        self.assertEqual(smart.normalize_smartctl(raw)["wear_pct"], 8)


class ExitBits(unittest.TestCase):
    def test_cmd_failed_with_data_is_not_unknown(self):
        # homeshare's SAS drives under `smartctl -x`: unsupported optional logs set bit 0x04
        raw = smart._sas_raw("/dev/sda", "ST2000NM0115", "ZC23AQE0", 36, 19000)
        raw["smartctl"]["exit_status"] = 4
        self.assertEqual(smart.normalize_smartctl(raw)["health"], smart.OK)

    def test_open_failed_is_unknown(self):
        raw = {"device": {"name": "/dev/sdx", "protocol": "ATA"}, "smartctl": {"exit_status": 2}}
        self.assertEqual(smart.normalize_smartctl(raw)["health"], smart.UNKNOWN)

    def test_cmd_failed_without_identity_is_unknown(self):
        raw = {"device": {"name": "/dev/sdx", "protocol": "ATA"}, "smartctl": {"exit_status": 4}}
        self.assertEqual(smart.normalize_smartctl(raw)["health"], smart.UNKNOWN)


def kingston_with_errors(count):
    """backup-compute's boot Kingston: old UNC errors in the log, self-test passes (2026-11-16)."""
    raw = kingston_a400(with_devstat=True)
    raw["serial_number"] = "50026B76831DDDEC"
    raw["smartctl"]["exit_status"] = 0x40
    raw["ata_smart_error_log"] = {"extended": {"count": count}}
    return raw


def report(*drives):
    return {"host": "backup-compute", "drives": list(drives)}


class ErrorLogBaseline(unittest.TestCase):
    def test_old_errors_are_a_note_not_a_warning(self):
        st = smart.SmartStore(None)
        st.put(report(kingston_with_errors(10)))
        d = st.view()["hosts"][0]["drives"][0]
        self.assertEqual(d["health"], smart.OK)
        self.assertIn("10 old errors", d["notes"][0])

    def test_new_errors_warn(self):
        st = smart.SmartStore(None)
        st.put(report(kingston_with_errors(10)))
        st.put(report(kingston_with_errors(12)))
        d = st.view()["hosts"][0]["drives"][0]
        self.assertEqual(d["health"], smart.WARN)
        self.assertIn("2 new errors", d["reasons"][0]["text"])

    def test_no_baseline_warns(self):
        d = smart.normalize_smartctl(kingston_with_errors(10), baselines={})
        self.assertEqual(d["health"], smart.WARN)

    def test_baseline_survives_restart_and_seeds_from_stored_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = smart.SmartStore(tmp)
            st.put(report(kingston_with_errors(10)))
            (Path(tmp) / "baselines.json").unlink()          # simulate upgrading from before baselines
            st2 = smart.SmartStore(tmp)
            self.assertEqual(st2.view()["hosts"][0]["drives"][0]["health"], smart.OK)
            st2.put(report(kingston_with_errors(11)))
            st3 = smart.SmartStore(tmp)
            self.assertEqual(st3.view()["hosts"][0]["drives"][0]["health"], smart.WARN)


def ssd(temp, rated):
    """Based on optiplex-management's Intel 545s (2026-11-16). `smartctl -j -x`:
    temperature = {current: 63, op_limit_max: 85, lifetime_over_limit_minutes: 0}.
    The 85 C comes from Device Statistics (page 5); the SCT log has no limit, and
    Intel's datasheet says 0-70 C (likely a case-temperature figure: attribute
    194 read 50 C at the same moment). rated=None models a drive with no limit."""
    model, serial = ("INTEL SSDSCKKF512G8 SATA 512GB", "BTLA82801HAZ512K") if rated else ("Generic SATA SSD", "NOLIMIT0001")
    raw = smart._ata_raw("/dev/sda", model, serial, 0.51, 0, temp, 11000, smart._ssd_attrs(98))
    if rated is not None:
        raw["temperature"]["op_limit_max"] = rated
    return raw


class TemperatureLimits(unittest.TestCase):
    def test_rated_limit_used(self):
        d = smart.normalize_smartctl(ssd(63, rated=85))       # the Intel, as reported
        self.assertEqual(d["temp_limits"], [75, 85, "rated"])
        self.assertEqual(d["health"], smart.OK)

    def test_rated_warn_and_crit(self):
        self.assertEqual(smart.normalize_smartctl(ssd(76, rated=85))["health"], smart.WARN)
        self.assertEqual(smart.normalize_smartctl(ssd(85, rated=85))["health"], smart.CRIT)

    def test_unrated_drive_keeps_generic_limits(self):
        d = smart.normalize_smartctl(ssd(62, rated=None))
        self.assertEqual(d["temp_limits"], [60, 70, "generic"])
        self.assertEqual(d["health"], smart.WARN)

    def test_implausible_rating_ignored(self):
        self.assertEqual(smart.normalize_smartctl(ssd(63, rated=255))["temp_limits"][2], "generic")


class Enclosure(unittest.TestCase):
    def test_ses_skipped_but_dead_drive_kept(self):
        ses = {"device": {"name": "/dev/ses0", "protocol": "SCSI"}, "smartctl": {"exit_status": 2}}
        dead = {"device": {"name": "/dev/ada1", "protocol": "ATA"}, "smartctl": {"exit_status": 2}}
        v = smart.build_host_view({"host": "opnsense", "drives": [ses, dead]}, 0, 0, 2700)
        self.assertEqual([d["device"] for d in v["drives"]], ["/dev/ada1"])
        self.assertEqual(v["drives"][0]["health"], smart.UNKNOWN)


class Agents(unittest.TestCase):
    def test_outdated(self):
        self.assertTrue(smart.agent_status("smart_agent.py/1")["outdated"])
        self.assertFalse(smart.agent_status(f"smart_agent.py/{smart.LATEST_AGENTS['smart_agent.py']}")["outdated"])
        self.assertFalse(smart.agent_status("something-else/9")["outdated"])



class Deploy(unittest.TestCase):
    def test_rate_limit_and_expiry(self):
        d = metrics.DeployRequest(min_gap=60, max_age=600)
        self.assertIsNone(d.info(now=1000))
        ok, info = d.request(now=1000)
        self.assertTrue(ok)
        self.assertEqual(info["age_s"], 0)
        ok, info = d.request(now=1030)
        self.assertFalse(ok)
        self.assertEqual(info["retry_in_s"], 30)
        self.assertTrue(d.request(now=1061)[0])
        self.assertIsNone(d.info(now=1061 + 601))

    def test_survives_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deploy-request.json")
            metrics.DeployRequest(path).request(now=5000)
            self.assertEqual(metrics.DeployRequest(path).info(now=5010)["age_s"], 10)

    def test_deploy_status(self):
        st = metrics.MetricsStore(reference_host="a")
        st.put({"host": "a", "deploy_status": {"result": "ok", "commit": "1FDB945C3A2E", "branch": "main",
                                              "checked_age_s": 30, "changed_age_s": 600, "git": "2.43.0"}}, now=1000)
        st.put({"host": "b", "deploy_status": {"result": "evil", "commit": "; rm -rf /", "message": "x" * 999}}, now=1000)
        st.put({"host": "c"}, now=1000)
        v = st.deploy_view(now=1010)
        a, b, c = v["hosts"]
        self.assertEqual(v["reference_commit"], "1fdb945c3a2e")
        self.assertEqual((a["checked_age_s"], a["changed_age_s"], a["branch"]), (40, 610, "main"))
        self.assertEqual((b["result"], b["commit"], len(b["message"])), ("unknown", None, 200))
        self.assertFalse(c["reported"])
        with self.assertRaises(ValueError):
            st.put({"host": "b", "deploy_status": "nope"})

    def test_running(self):
        import importlib.util, json, tempfile
        spec = importlib.util.spec_from_file_location("ma", os.path.join(os.path.dirname(__file__), "..", "agents", "metrics_agent.py"))
        ma = importlib.util.module_from_spec(spec); spec.loader.exec_module(ma)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"result": "ok", "commit": "1fdb945c3a2e", "checked_at": time.time() - 900,
                       "running_since": time.time() - 95, "phase": "applying scheduled tasks"}, f)
        try:
            fwd = ma.read_deploy_status(f.name)        # what the agent sends
        finally:
            os.unlink(f.name)
        self.assertTrue(94 <= fwd["running_for_s"] <= 97)
        st = metrics.MetricsStore()
        st.put({"host": "a", "deploy_status": fwd}, now=1000)
        st.put({"host": "b", "deploy_status": {"result": "ok", "checked_age_s": 5}}, now=1000)
        a, b = st.deploy_view(now=1010)["hosts"]
        self.assertTrue(104 <= a["running_for_s"] <= 107)
        self.assertEqual(a["phase"], "applying scheduled tasks")
        self.assertIsNone(b["running_for_s"])         # not running: nothing shown


import metrics  # noqa: E402


class Metrics(unittest.TestCase):
    def sample(self, **kw):
        s = {"host": "homeshare", "agent": "Send-Metrics.ps1/1",
             "cpu": {"util_pct": 20, "per_core_pct": [10, 30]},
             "mem": {"total": 16e9, "used": 8e9},
             "net": [{"name": "nic", "rx_bps": 100, "tx_bps": 50}]}
        s.update(kw)
        return s

    def test_history_is_bounded(self):
        st = metrics.MetricsStore()
        for i in range(metrics.HISTORY + 25):
            st.put(self.sample(), now=1000 + i * metrics.STEP)
        h = st.view(now=1000 + (metrics.HISTORY + 25) * metrics.STEP)["hosts"][0]
        self.assertEqual(len(h["history"]), metrics.HISTORY)
        self.assertEqual(h["history"][-1]["mem"], 50.0)
        self.assertEqual(h["history"][-1]["rx"], 100)

    def test_health_rules(self):
        self.assertEqual(metrics.health(self.sample())[0], metrics.OK)
        self.assertEqual(metrics.health(self.sample(mem={"total": 100, "used": 92}))[0], metrics.WARN)
        self.assertEqual(metrics.health(self.sample(mem={"total": 100, "used": 98}))[0], metrics.CRIT)
        self.assertEqual(metrics.health(self.sample(cpu={"util_pct": 50, "temp_c": 96}))[0], metrics.CRIT)

    def test_stale(self):
        st = metrics.MetricsStore(stale_after=180)
        st.put(self.sample(), now=1000)
        self.assertFalse(st.view(now=1100)["hosts"][0]["stale"])
        self.assertTrue(st.view(now=1200)["hosts"][0]["stale"])

    def test_validation(self):
        st = metrics.MetricsStore()
        for bad in ({"host": "../x"}, {"host": "a", "net": "nope"}, {"host": "a", "cpu": {"per_core_pct": [0] * 600}}):
            with self.assertRaises(ValueError):
                st.put(bad)

    def test_demo_seeds_full_history(self):
        st = metrics.MetricsStore()
        metrics.seed_demo(st)
        v = st.view()
        self.assertEqual(len(v["hosts"]), 4)
        self.assertTrue(all(len(h["history"]) == metrics.HISTORY for h in v["hosts"]))


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import metrics_agent  # noqa: E402


class FreeBSDParsers(unittest.TestCase):
    """Sample output in FreeBSD 14 / OPNsense format."""

    NETSTAT = """Name    Mtu Network            Address              Ipkts Ierrs Idrop     Ibytes    Opkts Oerrs     Obytes  Coll
igb0   1500 <Link#1>           00:25:90:aa:bb:cc  12345678     0     0 9876543210  2345678     0  123456789     0
igb0      - 203.0.113.0/24     203.0.113.10         45678     -     -    4567890    56789     -    5678901     -
igb1   1500 <Link#2>           00:25:90:aa:bb:cd   2345678     0     0  234567890  3456789     0 8765432100     0
lo0   16384 <Link#3>           lo0                    1234     0     0      98765     1234     0      98765     0
pflog0 33152 <Link#4>                                    0     0     0          0      812     0      91234     0
"""
    IOSTAT = """                        extended device statistics
device       r/s     w/s     kr/s     kw/s  ms/r  ms/w  ms/o  ms/t qlen  %b
ada0           1      30     12.0    480.5     0     0     0     0    0   1
pass0          0       0      0.0      0.0     0     0     0     0    0   0
                        extended device statistics
device       r/s     w/s     kr/s     kw/s  ms/r  ms/w  ms/o  ms/t qlen  %b
ada0           0       4      0.0     40.1     0     0     0     0    0   2
pass0          0       0      0.0      0.0     0     0     0     0    0   0
"""
    IFCONFIG = """igb0: flags=1008843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST,LOWER_UP> metric 0 mtu 1500
\tmedia: Ethernet autoselect (1000baseT <full-duplex>)
\tstatus: active
ix0: flags=1008843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST,LOWER_UP> metric 0 mtu 1500
\tmedia: Ethernet autoselect (10Gbase-SR <full-duplex,rxpause,txpause>)
lo0: flags=1008049<UP,LOOPBACK,RUNNING,MULTICAST,LOWER_UP> metric 0 mtu 16384
"""
    PS = """%CPU    RSS COMMAND
 16.0  91234 suricata
  4.8  20480 unbound
  0.8  10240 python3.11
"""

    def test_netstat(self):
        n = metrics_agent.parse_netstat_ibn(self.NETSTAT)
        self.assertEqual(n, {"igb0": (9876543210, 123456789), "igb1": (234567890, 8765432100)})

    def test_iostat_takes_last_block(self):
        self.assertEqual(metrics_agent.parse_iostat_x(self.IOSTAT)["ada0"], (0.0, 40.1, 2.0))

    def test_ifconfig_speeds(self):
        self.assertEqual(metrics_agent.parse_ifconfig_speeds(self.IFCONFIG), {"igb0": 1000, "ix0": 10000})

    def test_ps_normalised_to_machine(self):
        rows = metrics_agent.parse_ps(self.PS, threads=8)
        self.assertEqual(rows[0], {"name": "suricata", "cpu_pct": 2.0, "mem": 91234 * 1024})

    def test_cpu_util(self):
        self.assertEqual(metrics_agent.cpu_util((100, 1000), (150, 1100)), 50.0)


class Tasks(unittest.TestCase):
    def task(self, runs, last_age=3600, next_in=82800, **kw):
        return dict({"name": "Mirror x", "state": "Ready", "job": "x", "last_run_age_s": last_age, "next_run_in_s": next_in,
                     "last_task_result": 0, "runs": runs}, **kw)

    def run_(self, age, status="ok"):
        return {"age_s": age, "duration_s": 60, "status": status, "message": "m", "exit_code": 1,
                "summary": {"files": {"total": 10, "copied": 1, "skipped": 9, "mismatch": 0, "failed": 0, "extras": 0}}}

    def health(self, task, now=10_000_000):
        st = metrics.MetricsStore()
        st.put({"host": "h", "tasks": [task]}, now=now)
        t = st.tasks_view(now=now)["hosts"][0]["tasks"][0]
        return t["health"], t

    def test_states(self):
        self.assertEqual(self.health(self.task([self.run_(90000), self.run_(3600)]))[0], "ok")
        self.assertEqual(self.health(self.task([self.run_(90000), self.run_(3600, "failed")]))[0], "failed")
        self.assertEqual(self.health(self.task([self.run_(3600, "warn")]))[0], "warn")
        self.assertEqual(self.health(self.task([self.run_(3 * 86400)], last_age=3 * 86400, next_in=-2 * 86400 + 600))[0], "overdue")
        self.assertEqual(self.health(self.task([], state="Disabled"))[0], "disabled")
        self.assertEqual(self.health(self.task([self.run_(3600)], running_for_s=30))[0], "running")
        self.assertEqual(self.health(self.task([], last_task_result=0x41303))[0], "unknown")
        h, t = self.health(self.task([], job=None, last_task_result=1))
        self.assertEqual((h, t["health_text"]), ("failed", "last result 0x1"))

    def test_interval_from_schedule(self):
        # An off-schedule run (manual, 5 h before the 02:00 run) made the estimate 5 h;
        # with the schedule reported, it's the real daily interval, and no false "overdue".
        h, t = self.health(self.task([self.run_(7 * 3600)], last_age=7 * 3600, next_in=5 * 3600,
                                     schedule="daily at 02:00", interval_s=86400))
        self.assertEqual((h, t["interval_s"], t["schedule"]), ("ok", 86400, "daily at 02:00"))
        h, _ = self.health(self.task([self.run_(7 * 3600)], last_age=7 * 3600, next_in=-2 * 3600))
        self.assertEqual(h, "overdue")   # without the schedule: estimated 5 h, so the healthy job looks overdue (the old bug)
        h, _ = self.health(self.task([self.run_(3 * 86400)], last_age=3 * 86400, next_in=3600, interval_s=86400))
        self.assertEqual(h, "overdue")   # really missed two daily runs

    def test_interval_and_sanitising(self):
        _, t = self.health(self.task([self.run_(3600, "weird")], state="Hacked", name="n" * 500))
        self.assertEqual((t["interval_s"], t["state"], len(t["name"]), t["runs"][0]["status"]), (86400, "Unknown", 100, "failed"))
        with self.assertRaises(ValueError):
            metrics.MetricsStore().put({"host": "h", "tasks": "nope"})


class Creds(unittest.TestCase):
    PUB = "<RSAKeyValue><Modulus>abc</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>"

    def test_relay_and_validation(self):
        import creds, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "creds.json")
            st = creds.CredStore(path)
            fp = st.put_key({"id": "job", "host": "homeshare", "pubkey": self.PUB})["fp"]
            self.assertEqual(fp, creds.fingerprint(self.PUB))
            st.put_secret({"id": "job", "host": "backup-compute", "user": "BC\\svc-backup", "key_fp": fp, "ciphertext": "QUJD"})
            again = creds.CredStore(path)                      # survives a restart
            self.assertEqual(again.get("key", "job")["fp"], fp)
            self.assertEqual(again.get("secret", "job")["ciphertext"], "QUJD")
            self.assertIsNone(again.get("secret", "other"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        bad = [
            ("key", {"id": "job", "host": "h", "pubkey": self.PUB.replace("</RSAKeyValue>", "<D>x</D></RSAKeyValue>")}),  # private key
            ("key", {"id": "../x", "host": "h", "pubkey": self.PUB}),
            ("secret", {"id": "job", "host": "h", "user": "u", "key_fp": "f", "ciphertext": "not base64!"}),
            ("secret", {"id": "job", "host": "h", "user": "u", "key_fp": "f"}),
        ]
        st = creds.CredStore()
        for kind, body in bad:
            with self.assertRaises(ValueError):
                (st.put_key if kind == "key" else st.put_secret)(body)


class PowerShellFiles(unittest.TestCase):
    def test_ascii_only(self):
        # Windows PowerShell 5.1 reads .ps1 files without a BOM as ANSI, so any non-ASCII
        # character in a string comes out mangled on the servers (e.g. "Â·").
        import glob
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        files = glob.glob(os.path.join(root, "backups", "*.ps1")) + glob.glob(os.path.join(root, "dashboard", "agents", "*.ps1"))
        self.assertTrue(files)
        for f in files:
            for n, line in enumerate(open(f, encoding="utf-8"), 1):
                self.assertTrue(line.isascii(), f"{os.path.basename(f)}:{n} has non-ASCII: {line.strip()[:60]}")

if __name__ == "__main__":
    unittest.main()
