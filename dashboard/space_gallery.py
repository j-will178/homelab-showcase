"""Space gallery: the newest release images from Webb and Hubble, for /space/gallery/.

Release images are the finished colour pictures the mission teams publish (several filters combined,
processed, captioned), not raw pipeline previews. Three public RSS feeds, checked every 6 hours:

  ESA/Webb images             https://esawebb.org/images/feed/          (science release images)
  ESA/Webb Picture of the Month  https://esawebb.org/images/potm/feed/
  ESA/Hubble Picture of the Week https://esahubble.org/images/potw/feed/

Each item names its image page (…/images/<id>/); the screen-size JPEG is the item's <enclosure>, or
https://cdn.<site>/archives/images/screen/<id>.jpg when there's none. Annotated versions, compass/scale
overlays and artist's illustrations are skipped (they aren't pictures of the sky), and a release keeps
at most two images. The newest GALLERY_SIZE are downloaded once into state/space-gallery/ and served
at /api/space/gallery/image?id=…, so the kiosk never waits on ESA's servers.

Credits: ESA/Webb and ESA/Hubble images are published under CC BY 4.0 with a credit line, shown on
every image; the item's own page is linked for the full credit.
"""
import html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("space")

FEEDS = [
    # (label, telescope, url, fallback url, cdn host, default credit)
    ("Webb release", "Webb", "https://esawebb.org/images/feed/", "https://feeds.feedburner.com/esawebb/images/",
     "cdn.esawebb.org", "ESA/Webb, NASA & CSA"),
    ("Webb Picture of the Month", "Webb", "https://esawebb.org/images/potm/feed/", "https://feeds.feedburner.com/esawebb/potm",
     "cdn.esawebb.org", "ESA/Webb, NASA & CSA"),
    ("Hubble Picture of the Week", "Hubble", "https://esahubble.org/images/potw/feed/", "https://feeds.feedburner.com/hubble_potw/",
     "cdn.esahubble.org", "ESA/Hubble & NASA"),
]
EVERY = 6 * 3600
RETRY = 30 * 60
GALLERY_SIZE = 16
PER_RELEASE = 2
MAX_IMAGE_BYTES = 6 * 1024 * 1024
SKIP = re.compile(r"\b(annotated|compass|scale bar|artist|illustration|infographic|diagram|graphic|logo|poster)\b", re.I)
ID_RE = re.compile(r"/images/([a-z0-9_-]+)/?$", re.I)


def _text(s, cap=None):
    s = re.sub(r"<[^>]+>", " ", html.unescape(str(s or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:cap].rsplit(" ", 1)[0] + "…") if cap and len(s) > cap else s


def summary(desc, n=2, cap=360):
    """First sentences of the feed description, without its HTML, links lists or credit lines."""
    t = _text(desc)
    t = re.sub(r"\b(Pan video|Zoom video|Space Sparks Episode)\b.*$", "", t).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", t)
    s = " ".join(parts[:n])
    return s if len(s) <= cap else s[:cap].rsplit(" ", 1)[0] + "…"


def parse_feed(xml_text, feed):
    """RSS -> [{id, title, telescope, kind, date, summary, url, image_url, credit}], newest first."""
    label, telescope, _, _, cdn, credit = feed
    root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or it.findtext("guid") or "").strip()
        m = ID_RE.search(link)
        title = _text(it.findtext("title"))
        if not m or not title or SKIP.search(title):
            continue
        iid = m.group(1).lower()
        enc = it.find("enclosure")
        img = enc.get("url") if enc is not None and (enc.get("type") or "").startswith("image/") else None
        try:
            date = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError):
            date = 0
        out.append({"id": iid, "title": title, "telescope": telescope, "kind": label, "date": date,
                    "summary": summary(it.findtext("description")), "url": link,
                    "image_url": img or f"https://{cdn}/archives/images/screen/{iid}.jpg", "credit": credit})
    return sorted(out, key=lambda x: -x["date"])


def pick(items, size=GALLERY_SIZE):
    """Newest first across feeds, one copy of each id, at most PER_RELEASE images of one release
    (weic2610a/b/c are one release)."""
    seen, per, out = set(), {}, []
    for it in sorted(items, key=lambda x: -x["date"]):
        rel = re.sub(r"[a-z]$", "", it["id"])
        if it["id"] in seen or per.get(rel, 0) >= PER_RELEASE:
            continue
        seen.add(it["id"])
        per[rel] = per.get(rel, 0) + 1
        out.append(it)
        if len(out) >= size:
            break
    return out


class SpaceGallery:
    def __init__(self, state_dir=None, mode="live", opener=None, fetch_bytes=None):
        self.mode = mode
        self.dir = Path(state_dir) / "space-gallery" if state_dir else None
        self.path = Path(state_dir) / "space-gallery.json" if state_dir else None
        self.open, self.fetch_bytes = opener, fetch_bytes
        self.lock = threading.Lock()
        self.data = {"items": [], "ok_at": 0, "tried_at": 0, "error": None}
        if self.path and self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except (OSError, ValueError) as e:
                log.warning("space: ignoring unreadable gallery cache: %s", e)
        if self.data.get("error"):
            self.data["tried_at"] = 0                      # retry at once after a restart

    def refresh(self, now=None):
        now = now or time.time()
        if self.mode != "live":
            return
        d = self.data
        if now - (d.get("ok_at") or 0) < EVERY or now - (d.get("tried_at") or 0) < (RETRY if d.get("error") else 0):
            return
        d["tried_at"] = now
        items, errors = [], []
        for feed in FEEDS:
            for url in (feed[2], feed[3]):
                try:
                    items += parse_feed(self.open(url), feed)
                    break
                except Exception as e:   # noqa: BLE001 - try the mirror, then the next feed
                    errors.append(f"{url}: {str(e)[:120]}")
        if not items:
            d["error"] = " | ".join(errors) or "no items"
            log.warning("space: gallery feeds failed: %s", d["error"])
            self._save()
            return
        chosen = []
        for it in pick(items):
            try:
                it["file"] = self._download(it)
                chosen.append(it)
            except Exception as e:       # noqa: BLE001 - skip this image
                errors.append(f"{it['id']}: {str(e)[:120]}")
        with self.lock:
            if chosen:
                d["items"] = chosen
                d["ok_at"] = now
            d["error"] = "; ".join(errors[:4]) if errors else None
        if errors:
            log.info("space: gallery partly unavailable: %s", d["error"])
        self._trim()
        self._save()

    def _download(self, it):
        if not self.dir:
            return None
        dest = self.dir / f"{it['id']}.jpg"
        if dest.exists():
            return dest.name
        body = self.fetch_bytes(it["image_url"])
        if not body.startswith(b"\xff\xd8") or len(body) > MAX_IMAGE_BYTES:
            raise ValueError(f"not a usable JPEG ({len(body)} bytes)")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, dest)
        return dest.name

    def _trim(self):
        if not self.dir or not self.dir.exists():
            return
        keep = {it.get("file") for it in self.data["items"]}
        for f in self.dir.glob("*.jpg"):
            if f.name not in keep:
                f.unlink(missing_ok=True)

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            with self.lock:
                tmp.write_text(json.dumps(self.data, separators=(",", ":")))
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("space: cannot save gallery cache: %s", e)

    def start_background(self, every=600):
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception:        # noqa: BLE001 - never let the thread die
                    log.exception("space: gallery refresh failed")
                time.sleep(every)
        threading.Thread(target=loop, name="space-gallery", daemon=True).start()

    def view(self, now=None):
        now = now or time.time()
        if self.mode == "demo":
            return demo_view(now)
        with self.lock:
            items = [dict(it) for it in self.data["items"]]
            err, ok = self.data.get("error"), self.data.get("ok_at")
        return {"mode": self.mode, "updated_at": _iso(ok), "error": err,
                "items": [{"id": it["id"], "title": it["title"], "telescope": it["telescope"], "kind": it["kind"],
                           "date": _iso(it["date"]), "summary": it["summary"], "url": it["url"], "credit": it["credit"],
                           "image": f"api/space/gallery/image?id={it['id']}" if it.get("file") else None}
                          for it in items]}

    def image_path(self, iid):
        if not self.dir or not re.fullmatch(r"[a-z0-9_-]{2,40}", iid or ""):
            return None
        p = self.dir / f"{iid}.jpg"
        return p if p.is_file() else None


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def demo_view(now):
    """--demo and the static demo: titles only, no images (no network)."""
    rows = [("Webb", "Webb release", "A barred spiral galaxy in infrared light"),
            ("Hubble", "Hubble Picture of the Week", "A star-forming region in a nearby dwarf galaxy"),
            ("Webb", "Webb Picture of the Month", "Dust shells around a pair of massive stars"),
            ("Hubble", "Hubble Picture of the Week", "A galaxy cluster bending light from far behind it")]
    return {"mode": "demo", "updated_at": _iso(now), "error": None,
            "items": [{"id": f"demo{i}", "title": f"{t} (simulated)", "telescope": tel, "kind": kind,
                       "date": _iso(now - i * 5 * 86400), "summary": "Simulated entry: the live gallery shows ESA/Webb and "
                       "ESA/Hubble release images here.", "url": None,
                       "credit": "ESA/Webb, NASA & CSA" if tel == "Webb" else "ESA/Hubble & NASA", "image": None}
                      for i, (tel, kind, t) in enumerate(rows)]}
