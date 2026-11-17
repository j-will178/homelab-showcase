"""
Credential relay for backup jobs (see backups/README.md, "Share credential").

The dashboard never sees a password. It only relays, per job id:
  - a PUBLIC key, posted by the consumer (the host that runs the job, e.g. homeshare);
  - a CIPHERTEXT, posted by the provider (the host with the share, e.g. backup-compute):
    the account password, RSA-OAEP encrypted to that public key.
Both are useless without the consumer's private key, which never leaves the consumer.
All endpoints require the agent token. Kept in a small state file so a dashboard
restart doesn't lose them.
"""

import hashlib
import json
import os
import re
import threading
import time

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{1,8192}$")


def fingerprint(pubkey):
    return hashlib.sha256(pubkey.strip().encode()).hexdigest()[:16]


class CredStore:
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.Lock()
        self.items = {}
        if path:
            try:
                with open(path) as f:
                    self.items = json.load(f)
            except (OSError, ValueError):
                self.items = {}

    def _save(self):
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.items, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @staticmethod
    def _check(body, *fields):
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        if not ID_RE.match(str(body.get("id", ""))):
            raise ValueError("id must match [A-Za-z0-9._-]{1,64}")
        if not HOST_RE.match(str(body.get("host", ""))):
            raise ValueError("host must match [A-Za-z0-9._-]{1,64}")
        for f in fields:
            if not isinstance(body.get(f), str) or not body[f].strip():
                raise ValueError(f"{f} is required")

    def put_key(self, body, now=None):
        """Consumer publishes its public key (RSA key as .NET XML). Idempotent."""
        self._check(body, "pubkey")
        pub = body["pubkey"].strip()
        if len(pub) > 4096 or "<Modulus>" not in pub or "<D>" in pub:
            raise ValueError("pubkey must be a public RSA key in XML form")
        fp = fingerprint(pub)
        with self.lock:
            item = self.items.setdefault(body["id"], {})
            if item.get("key", {}).get("fp") != fp:
                item["key"] = {"pubkey": pub, "fp": fp, "host": body["host"], "at": now or time.time()}
                self._save()
        return {"fp": fp}

    def put_secret(self, body, now=None):
        """Provider publishes the password encrypted to a key (key_fp says which)."""
        self._check(body, "ciphertext", "user", "key_fp")
        if not B64_RE.match(body["ciphertext"]) or len(body["user"]) > 128:
            raise ValueError("ciphertext must be base64; user at most 128 chars")
        with self.lock:
            item = self.items.setdefault(body["id"], {})
            item["secret"] = {"ciphertext": body["ciphertext"].strip(), "user": body["user"], "key_fp": body["key_fp"],
                              "host": body["host"], "at": now or time.time()}
            self._save()
        return {"ok": True}

    def get(self, kind, id_):
        if not ID_RE.match(str(id_ or "")):
            raise ValueError("bad id")
        with self.lock:
            v = (self.items.get(id_) or {}).get(kind)
            return dict(v) if v else None
