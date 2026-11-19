"""Run: python3 -m unittest discover dashboard/tests"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector  # noqa: E402
import honeypot  # noqa: E402

TEST_TOKEN = "y" * 24  # not a real token


def line(eventid, t, session="s1", ip="203.0.113.7", **kw):
    ts = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return json.dumps(dict(eventid=eventid, timestamp=ts, session=session, src_ip=ip, **kw))


def session_lines(t, session="s1"):
    return [line("cowrie.session.connect", t, session, dst_port=22, protocol="ssh"),
            line("cowrie.login.success", t + 1, session, username="root", password="hunter2"),
            line("cowrie.command.input", t + 2, session, input="uname -a"),
            line("cowrie.session.closed", t + 3, session, duration="3.0")]


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.dir.name) / "honeypot.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_simulated_until_first_real_lines(self):
        f = honeypot.HoneypotFeed(self.db)
        self.assertTrue(f.view()["simulated"])
        f.ingest({"status": {"state": "waiting-build"}})          # status alone doesn't switch
        self.assertTrue(f.view()["simulated"])
        self.assertFalse(Path(self.db).exists())
        out = f.ingest({"id": "a", "lines": session_lines(time.time() - 60)})
        self.assertEqual(out["accepted"], 4)
        v = f.view()
        self.assertEqual((v["source"], v["simulated"], v["summary"]["sessions"]), ("cowrie", False, 1))
        self.assertTrue(f.sim.stopped.is_set())
        self.assertTrue(Path(self.db).exists())

    def test_duplicate_batch_ignored(self):
        f = honeypot.HoneypotFeed(self.db, simulate=False)
        lines = session_lines(time.time() - 60)
        f.ingest({"id": "same", "lines": lines})
        self.assertEqual(f.ingest({"id": "same", "lines": lines}), {"accepted": 0, "duplicate": True})
        self.assertEqual(f.view()["summary"]["login_attempts"], 1)

    def test_restart_keeps_real_data_and_mode(self):
        f = honeypot.HoneypotFeed(self.db)
        f.ingest({"id": "a", "lines": session_lines(time.time() - 120)})
        g = honeypot.HoneypotFeed(self.db)                     # dashboard restarted
        v = g.view()
        self.assertEqual((v["source"], v["summary"]["sessions"], v["summary"]["commands"]), ("cowrie", 1, 1))
        self.assertEqual(g.patterns_view()["shell_sessions"], 1)
        g.ingest({"id": "b", "lines": session_lines(time.time() - 30, "s2")})
        self.assertEqual(g.view()["summary"]["sessions"], 2)

    def test_hostile_and_bad_input(self):
        f = honeypot.HoneypotFeed(self.db, simulate=False)
        out = f.ingest({"id": "x", "lines": ["not json", "{}", '{"eventid": "cowrie.login.failed"}',
                                             line("cowrie.login.failed", time.time(), username="<script>", password="x")]})
        self.assertEqual((out["accepted"], out["ignored"]), (1, 3))
        for bad in ([], {"lines": "x"}, {"lines": [1]}, {"lines": ["a"] * 20001}, {"id": 5, "lines": ["a"]}):
            with self.assertRaises(ValueError, msg=str(bad)[:40]):
                f.ingest(bad)

    def test_no_state_dir_never_goes_real(self):
        f = honeypot.HoneypotFeed(None)
        with self.assertRaises(ValueError):
            f.ingest({"id": "a", "lines": session_lines(time.time())})
        self.assertTrue(f.view()["simulated"])

    def test_status_passed_through_filtered(self):
        f = honeypot.HoneypotFeed(self.db, simulate=False)
        f.ingest({"status": {"state": "ok", "exposure": "lab-only", "pulled_at": 5, "evil": "x", "error": {"no": 1}}}, now=100)
        st = f.view(now=110)["feed_status"]
        self.assertEqual((st["state"], st["exposure"], st["age_s"]), ("ok", "lab-only", 10))
        self.assertNotIn("evil", st)
        self.assertNotIn("error", st)                          # not a scalar: dropped


class IngestEndpointTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.feed = honeypot.HoneypotFeed(str(Path(self.dir.name) / "honeypot.db"), simulate=False)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), collector.make_handler(None, {}, agent_token=TEST_TOKEN,
                                                                                honeypot_store=self.feed))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def post(self, body, token=TEST_TOKEN):
        req = urllib.request.Request(self.base + "/api/honeypot/ingest", data=body if isinstance(body, bytes) else json.dumps(body).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None
        except urllib.error.URLError:
            return "refused", None          # rejected before the body was read

    def test_flow(self):
        self.assertEqual(self.post({"lines": session_lines(time.time())}, token="nope")[0], 401)
        self.assertEqual(self.post(b"{"), (400, None))
        self.assertIn(self.post(b"x" * (2 * 1024 * 1024 + 1))[0], (400, "refused"))
        code, out = self.post({"id": "1", "lines": session_lines(time.time() - 10), "status": {"state": "ok"}})
        self.assertEqual((code, out["accepted"]), (200, 4))
        with urllib.request.urlopen(self.base + "/api/honeypot", timeout=5) as r:
            v = json.load(r)
        self.assertEqual((v["source"], v["feed_status"]["state"]), ("cowrie", "ok"))


if __name__ == "__main__":
    unittest.main()
