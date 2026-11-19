"""Run: python3 -m unittest discover dashboard/tests"""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector  # noqa: E402
import metrics  # noqa: E402

TEST_TOKEN = "x" * 24  # not a real token
KEY = "ssh-ed25519 " + "A" * 68 + " root@cloud-ops"


class PendingDeployKeysTest(unittest.TestCase):
    def test_accepts_one_ed25519_key(self):
        k = metrics.PendingDeployKeys()
        self.assertEqual(k.put({"host": "Cloud-Ops", "public_key": KEY}, now=100), {"host": "cloud-ops"})
        self.assertEqual(k.view(now=160), [{"host": "cloud-ops", "public_key": KEY, "age_s": 60}])

    def test_rejects_bad_input(self):
        k = metrics.PendingDeployKeys()
        for body in ([], {"host": "a b", "public_key": KEY}, {"host": "x", "public_key": "ssh-rsa AAAA"},
                     {"host": "x", "public_key": KEY + "\nssh-ed25519 " + "B" * 68},
                     {"host": "x", "public_key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----"}):
            with self.assertRaises(ValueError, msg=body):
                k.put(body)
        self.assertEqual(k.view(), [])

    def test_dropped_once_reporting_or_old(self):
        k = metrics.PendingDeployKeys(max_age=3600)
        k.put({"host": "a", "public_key": KEY}, now=0)
        k.put({"host": "b", "public_key": KEY}, now=0)
        self.assertEqual([h["host"] for h in k.view({"a"}, now=10)], ["b"])
        self.assertEqual(k.view(now=4000), [])

    def test_bounded(self):
        k = metrics.PendingDeployKeys()
        for i in range(30):
            k.put({"host": f"h{i}", "public_key": KEY}, now=i + 1)
        hosts = [h["host"] for h in k.view(now=31)]
        self.assertEqual(len(hosts), 20)
        self.assertNotIn("h0", hosts)


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.keys = metrics.PendingDeployKeys()
        handler = collector.make_handler(None, {}, agent_token=TEST_TOKEN, deploy_keys=self.keys)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body, token=TEST_TOKEN):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + "/api/deploykeys", data=data, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_post_then_listed(self):
        self.assertEqual(self.post({"host": "cloud-ops", "public_key": KEY}), 200)
        with urllib.request.urlopen(self.base + "/api/deploy", timeout=5) as r:
            pending = json.load(r)["pending_keys"]
        self.assertEqual([p["host"] for p in pending], ["cloud-ops"])

    def test_needs_token_and_valid_body(self):
        self.assertEqual(self.post({"host": "cloud-ops", "public_key": KEY}, token="wrong"), 401)
        self.assertEqual(self.post({"host": "cloud-ops", "public_key": "nope"}), 400)
        self.assertEqual(self.post(b"x" * 5000), 400)
        self.assertEqual(self.keys.view(), [])


if __name__ == "__main__":
    unittest.main()
