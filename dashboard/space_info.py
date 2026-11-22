"""Facts about what Hubble and Webb are looking at, for the Space section's info panel.

For each scheduled visit (target name, RA/Dec, program number) this looks up, once, and caches
for good in state/space-info.json (an object's facts don't change):

  what it is            CDS Sesame resolves the name (NGC 1365, M 51, TRAPPIST-1...) to SIMBAD's
                        entry; SIMBAD TAP gives its type, number of papers, redshift and measured
                        distances (median). Working names like ABELL2744-PAR-3 don't resolve: then
                        SIMBAD's most-studied object within 12' is shown as the nearest known object.
  a short summary       Wikipedia's page summary (first two sentences), only for names that resolve
                        and only if the page is about an astronomical object. CC BY-SA: credited.
  why it's observed     MAST (the STScI archive) has each program's title and PI on its earlier
                        observations.
  what the sky there    CDS hips2fits: a 256 px colour cutout of the Digitized Sky Survey (DSS2),
  looks like            about 10' across, saved under state/space-img/. A survey image, not the
                        telescope's own (those stay private for months).

SpaceInfo.prefetch() runs from the space refresh loop with a small budget, current targets first,
so the kiosk's panel is filled before anyone looks. Lookups never run on a viewer's request.
"""
import hashlib
import json
import logging
import math
import os
import re
import statistics
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("space")

SESAME = "https://cds.unistra.fr/cgi-bin/nph-sesame/-oxI/SNV?{name}"
SIMBAD_TAP = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}?redirect=true"
MAST = "https://mast.stsci.edu/api/v0/invoke"
HIPS2FITS = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"
MAST_DOWNLOAD = "https://mast.stsci.edu/api/v0.1/Download/file?uri={uri}"
# Survey cutouts, tried in order: NASA Goddard SkyView, then STScI's own DSS server. (CDS hips2fits is
# last: every *.unistra.fr host times out from the lab's network, as of 2026-11-22.)
SURVEY_URLS = [
    ("NASA SkyView", "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl?Survey=DSS2+Red&Position={ra:.6f},{dec:.6f}"
                     "&Size={fov}&Pixels=256&Return=JPEG"),
    ("STScI DSS", "https://archive.stsci.edu/cgi-bin/dss_search?v=poss2ukstu_red&r={ra:.6f}&d={dec:.6f}&e=J2000"
                  "&h={arcmin:.1f}&w={arcmin:.1f}&f=gif&c=none"),
    ("CDS hips2fits", HIPS2FITS + "?hips=CDS%2FP%2FDSS2%2Fcolor&width=256&height=256&fov={fov}&projection=TAN"
                      "&coordsys=icrs&ra={ra:.6f}&dec={dec:.6f}&format=jpg"),
]
PREVIEW_RADIUS = 0.04     # deg: an earlier public Hubble/Webb image counts if its pointing is within ~2.4'
MAX_IMAGE_BYTES = 3 * 1024 * 1024
COLLECTION = {"hst": "HST", "jwst": "JWST"}
PROGRAM_URL = {"hst": "https://www.stsci.edu/hst-program-info/program/?program={id}",
               "jwst": "https://www.stsci.edu/jwst/science-execution/program-information?id={id}"}

FOV_DEG = 0.17            # ~10 arcmin cutout
NEAR_DEG = 0.2            # "nearest known object" search radius
MAX_ENTRIES = 3000
MAX_IMAGES = 200
BUDGET = 6                # new targets looked up per refresh (every 5 min)
FAIL_RETRY = 6 * 3600     # after network trouble, don't retry a target for this long
LY_PER_PC = 3.26156
ASTRO_WORDS = re.compile(r"galax|nebula|star|cluster|constellation|light[- ]years?|planet|quasar|supernova|"
                         r"remnant|magellanic|exoplanet|black hole|interstellar|astronom", re.I)


def _key(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def ly_text(ly):
    if ly is None:
        return None
    if ly < 1000:
        return f"~{ly:,.0f} light years"
    for div, word in ((1e9, "billion"), (1e6, "million"), (1e3, "thousand")):
        if ly >= div:
            v = ly / div
            return f"~{v:.1f} {word} light years" if v < 10 else f"~{v:,.0f} {word} light years"


def median_ly(rows):
    """SIMBAD mesDistance rows [dist, unit] -> median distance in light years (None if unusable)."""
    scale = {"pc": 1, "kpc": 1e3, "mpc": 1e6}
    vals = []
    for d, unit in rows:
        try:
            f = float(d) * scale[str(unit).strip().lower()] * LY_PER_PC
        except (KeyError, TypeError, ValueError):
            continue
        if f > 0 and math.isfinite(f):
            vals.append(f)
    return statistics.median(vals) if vals else None


def redshift_ly(z):
    """Rough distance from redshift (Hubble's law, H0 = 70) - only where that's fair, z < 0.1."""
    if z is None or not 0.002 < z < 0.1:
        return None
    return z * 299792.458 / 70 * 1e6 * LY_PER_PC


def first_sentences(text, n=2, cap=320):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", (text or "").strip())
    s = " ".join(parts[:n])
    return s if len(s) <= cap else s[:cap].rsplit(" ", 1)[0] + "…"


def parse_sesame(xml_text):
    """Sesame XML -> {main_id, otype, ra, dec, morph, aliases} or None when nothing resolved."""
    root = ET.fromstring(xml_text)
    for el in root.iter():                       # tolerate a namespace on the elements
        el.tag = el.tag.split("}")[-1]
    for res in root.iter("Resolver"):
        name = res.findtext("oname")
        if not name:
            continue
        def num(tag):
            try:
                return float(res.findtext(tag))
            except (TypeError, ValueError):
                return None
        return {"main_id": name.strip(), "otype": (res.findtext("otype") or "").strip(),
                "ra": num("jradeg"), "dec": num("jdedeg"), "morph": (res.findtext("MType") or "").strip() or None,
                "aliases": [(x.text or "").strip() for x in res.findall("alias") if (x.text or "").strip()]}
    return None


def tap_rows(payload):
    """SIMBAD TAP JSON -> list of dicts."""
    cols = [c["name"] for c in payload.get("metadata", [])]
    return [dict(zip(cols, row)) for row in payload.get("data", [])]


def adql_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def wiki_titles(target, obj):
    """Wikipedia titles worth trying, best first: NAME aliases, Messier spelled out, the main id."""
    out = []
    for a in (obj or {}).get("aliases", []):
        if a.startswith("NAME "):
            out.append(a[5:])
    for name in [target, (obj or {}).get("main_id")] + (obj or {}).get("aliases", [])[:6]:
        if not name:
            continue
        n = re.sub(r"\s+", " ", name).strip()
        m = re.fullmatch(r"M ?(\d{1,3})", n)
        out.append(f"Messier {m.group(1)}" if m else n)
    seen, uniq = set(), []
    for t in out:
        if t and t.lower() not in seen and not t.startswith(("2MASS", "Gaia", "TIC ", "WISE", "[", "SDSS", "NAME ")):
            seen.add(t.lower())
            uniq.append(t)
    return uniq[:4]


class SpaceInfo:
    def __init__(self, state_dir=None, opener=None, poster=None, fetch_bytes=None):
        self.path = Path(state_dir) / "space-info.json" if state_dir else None
        self.img_dir = Path(state_dir) / "space-img" if state_dir else None
        self.open, self.post, self.fetch_bytes = opener, poster, fetch_bytes
        self.lock = threading.Lock()
        self.data = {"targets": {}, "programs": {}, "captions": {}}
        if self.path and self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except (OSError, ValueError) as e:
                log.warning("space: ignoring unreadable info cache: %s", e)
        self.captions = self.data.setdefault("captions", {})
        # A restart (usually a deploy) retries failed and partial lookups instead of waiting out FAIL_RETRY.
        for k in [k for k, v in self.data["targets"].items() if v.get("failed") or v.get("errors")]:
            del self.data["targets"][k]

    # ------------------------------------------------------------ lookups (network)
    def _tap(self, query):
        url = SIMBAD_TAP + "?" + urllib.parse.urlencode({"request": "doQuery", "lang": "adql", "format": "json", "query": query})
        return tap_rows(json.loads(self.open(url)))

    def _object(self, name, ra, dec):
        obj = None
        try:
            obj = parse_sesame(self.open(SESAME.format(name=urllib.parse.quote(name))))
        except ET.ParseError:
            obj = None
        # A name can resolve to something unrelated ("FIELD-1"): trust it only near the scheduled position.
        if obj and ra is not None and obj["ra"] is not None and _sep(ra, dec, obj["ra"], obj["dec"]) > 1.0:
            obj = None
        if not obj:
            return None
        rows = self._tap("SELECT b.main_id, b.otype, d.description, b.nbref, b.rvz_redshift FROM basic AS b "
                         "LEFT JOIN otypedef AS d ON d.otype = b.otype WHERE b.main_id = " + adql_str(obj["main_id"]))
        if rows:
            r = rows[0]
            obj.update(type=r.get("description") or obj["otype"], papers=r.get("nbref"), z=r.get("rvz_redshift"))
        dist = self._tap("SELECT m.dist, m.unit FROM mesDistance AS m JOIN basic AS b ON b.oid = m.oidref "
                         "WHERE b.main_id = " + adql_str(obj["main_id"]))
        obj["distance_ly"] = median_ly([(r.get("dist"), r.get("unit")) for r in dist]) or redshift_ly(obj.get("z"))
        return obj

    def _nearest(self, ra, dec):
        rows = self._tap(
            "SELECT TOP 1 b.main_id, d.description, b.nbref, b.rvz_redshift, "
            f"DISTANCE(POINT('ICRS', b.ra, b.dec), POINT('ICRS', {ra:.6f}, {dec:.6f})) AS sep "
            "FROM basic AS b LEFT JOIN otypedef AS d ON d.otype = b.otype "
            f"WHERE CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {NEAR_DEG})) = 1 "
            "ORDER BY b.nbref DESC")
        if not rows:
            return None
        r = rows[0]
        dist = self._tap("SELECT m.dist, m.unit FROM mesDistance AS m JOIN basic AS b ON b.oid = m.oidref "
                         "WHERE b.main_id = " + adql_str(r["main_id"]))
        ly = median_ly([(x.get("dist"), x.get("unit")) for x in dist]) or redshift_ly(r.get("rvz_redshift"))
        return {"name": _tidy(r["main_id"]), "type": r.get("description"), "sep_arcmin": round(float(r["sep"]) * 60, 1),
                "distance": ly_text(ly)}

    def _wiki(self, titles):
        titles = [t for t in titles if not re.search(r"[_]|\d{2}[:+-]\d{2}|^\W", t)] or []   # skip working names
        for t in titles:
            try:
                p = json.loads(self.open(WIKI.format(title=urllib.parse.quote(t.replace(" ", "_"), safe=""))))
            except Exception:    # noqa: BLE001 - 404 and friends: try the next title
                continue
            text = p.get("extract") or ""
            if p.get("type") == "standard" and ASTRO_WORDS.search(text):
                url = ((p.get("content_urls") or {}).get("desktop") or {}).get("page")
                return {"summary": first_sentences(text), "url": url, "title": p.get("title")}
        return None

    def _program(self, tel, prog):
        req = {"service": "Mast.Caom.Filtered", "format": "json", "pagesize": 1, "page": 1,
               "params": {"columns": "obs_title,proposal_pi,proposal_id",
                          "filters": [{"paramName": "obs_collection", "values": [COLLECTION[tel]]},
                                      {"paramName": "proposal_id", "values": [str(prog)]}]}}
        res = json.loads(self.post(MAST, {"request": json.dumps(req)}))
        rows = res.get("data") or []
        title = _tidy(rows[0].get("obs_title")) if rows else None
        pi = _tidy(rows[0].get("proposal_pi")) if rows else None
        return {"id": str(prog), "title": title, "pi": pi, "url": PROGRAM_URL[tel].format(id=prog)}

    @staticmethod
    def _kind(body):
        if body.startswith(b"\xff\xd8"):
            return "jpg"
        if body.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if body.startswith(b"\x89PNG"):
            return "png"
        return None

    def _save(self, key, body):
        ext = self._kind(body)
        if not ext or len(body) > MAX_IMAGE_BYTES:
            raise ValueError(f"not a usable image ({len(body)} bytes, starts {body[:12]!r})")
        self.img_dir.mkdir(parents=True, exist_ok=True)
        dest = self.img_dir / f"{key}.{ext}"
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, dest)
        self._trim_images()
        return dest.name

    def _preview(self, ra, dec):
        """The newest public Hubble or Webb preview within PREVIEW_RADIUS of the pointing (MAST), mosaics first."""
        req = {"service": "Mast.Caom.Filtered.Position", "format": "json", "pagesize": 300, "page": 1,
               "params": {"columns": "obs_collection,instrument_name,filters,t_min,proposal_id,jpegURL,dataRights,calib_level,target_name",
                          "filters": [{"paramName": "obs_collection", "values": ["HST", "JWST"]},
                                      {"paramName": "dataRights", "values": ["PUBLIC"]},
                                      {"paramName": "dataproduct_type", "values": ["image"]},
                                      {"paramName": "intentType", "values": ["science"]}],
                          "position": f"{ra:.6f}, {dec:.6f}, {PREVIEW_RADIUS}"}}
        rows = [r for r in json.loads(self.post(MAST, {"request": json.dumps(req)})).get("data") or []
                if r.get("jpegURL") and str(r.get("dataRights", "")).upper() == "PUBLIC"]
        rows.sort(key=lambda r: (int(r.get("calib_level") or 0), float(r.get("t_min") or 0)), reverse=True)
        return rows[:3]

    def _image(self, ra, dec, key):
        """-> (file name, caption) for the best picture of this spot, or (None, None).
        1. the newest public Hubble/Webb preview of it (MAST), 2. a DSS2 survey cutout."""
        if not self.img_dir:
            return None, None
        for f in self.img_dir.glob(f"{key}.*"):
            if f.suffix in (".jpg", ".gif", ".png"):
                return f.name, self.captions.get(key)
        errors = []
        try:
            for r in self._preview(ra, dec):
                try:
                    name = self._save(key, self.fetch_bytes(MAST_DOWNLOAD.format(uri=urllib.parse.quote(r["jpegURL"], safe=":/"))))
                except Exception as e:   # noqa: BLE001 - try the next preview
                    errors.append(f"MAST preview: {e}")
                    continue
                year = ""
                if r.get("t_min"):                                     # MJD (days since 1858-11-17) -> year
                    year = str((datetime(1858, 11, 17) + timedelta(days=float(r["t_min"]))).year)
                tel = "Webb" if r.get("obs_collection") == "JWST" else "Hubble"
                cap = " · ".join(x for x in (f"{tel}'s own earlier image", _tidy(r.get("instrument_name")), year,
                                             f"program {r['proposal_id']}" if r.get("proposal_id") else None) if x)
                self.captions[key] = {"kind": "telescope", "text": cap, "source": "MAST"}
                return name, self.captions[key]
        except Exception as e:           # noqa: BLE001 - MAST down or changed: fall back to the survey
            errors.append(f"MAST search: {e}")
        for label, url in SURVEY_URLS:
            try:
                name = self._save(key, self.fetch_bytes(url.format(ra=ra, dec=dec, fov=FOV_DEG, arcmin=FOV_DEG * 60)))
            except Exception as e:       # noqa: BLE001 - try the next survey server
                errors.append(f"{label}: {e}")
                continue
            self.captions[key] = {"kind": "survey", "text": f"Sky survey (DSS2) via {label} · 10′ across", "source": label}
            return name, self.captions[key]
        raise ValueError("no image: " + " | ".join(e[:120] for e in errors))

    def _trim_images(self):
        files = sorted((f for f in self.img_dir.iterdir() if f.suffix in (".jpg", ".gif", ".png")),
                       key=lambda f: f.stat().st_mtime)
        for f in files[:-MAX_IMAGES]:
            f.unlink(missing_ok=True)

    def lookup(self, tel, visit, now=None):
        """Look one visit up and cache what worked. Each part (what it is, summary, image, program) is
        independent: one unreachable service only loses its own part. Raises only if every part failed."""
        now = now or time.time()
        name, ra, dec, prog = visit["target"], visit.get("ra"), visit.get("dec"), visit.get("proposal")
        info = {"name": name, "fetched_at": now, "sources": []}
        errors = []

        def part(label, fn, *a):
            try:
                return fn(*a)
            except Exception as e:       # noqa: BLE001 - recorded; the other parts still run
                errors.append(f"{label}: {str(e)[:160]}")
                return None
        obj = part("SIMBAD", self._object, name, ra, dec)
        if obj is None and not any(e.startswith("SIMBAD") for e in errors) and ra is not None:
            info["resolved"] = False
            info["nearest"] = part("SIMBAD nearby", self._nearest, ra, dec)
            if info["nearest"]:
                info["sources"].append("SIMBAD (nearby search)")
        if obj:
            info.update(resolved=True, main_id=_tidy(obj["main_id"]), type=obj.get("type"), morph=obj.get("morph"),
                        distance=ly_text(obj.get("distance_ly")), papers=obj.get("papers"),
                        aliases=[x for x in dict.fromkeys(_tidy(a[5:] if a.startswith("NAME ") else a) for a in obj["aliases"]
                                                          if a.startswith(("NAME ", "M ", "NGC ", "IC ")))
                                 if x != _tidy(obj["main_id"])][:3])
            info["sources"].append("SIMBAD")
        # Wikipedia works without SIMBAD too: the scheduled name is tried directly (NGC 1365, Messier 51...).
        wiki = part("Wikipedia", self._wiki, wiki_titles(name, obj))
        if wiki:
            info["wiki"] = wiki
            info["sources"].append("Wikipedia")
        if ra is not None:
            img, cap = part("image", self._image, ra, dec, _key(round(ra, 3), round(dec, 3))) or (None, None)
            if img:
                info["image"], info["image_caption"] = img, cap
                info["sources"].append(cap["source"] if cap else "image")
        if prog and self.program(tel, prog) is None and not (visit.get("program") or {}).get("title"):
            p = part("MAST program", self._program, tel, prog)
            if p:
                with self.lock:
                    self.data["programs"][f"{tel}:{prog}"] = p
        useful = obj or info.get("nearest") or info.get("wiki") or info.get("image") or \
            (prog and self.program(tel, prog)) or (visit.get("program") or {}).get("title")
        if errors and not useful:                          # nothing at all: back off as a failure
            raise RuntimeError("; ".join(errors))
        if errors:
            info["errors"] = errors                       # partial: retried after FAIL_RETRY, and on restart
            log.info("space: info for %s partly unavailable: %s", name, "; ".join(errors))
        with self.lock:
            self.data["targets"][self.target_key(tel, visit)] = info
        return info

    # ------------------------------------------------------------ cache
    @staticmethod
    def target_key(tel, v):
        ra = round(v["ra"], 3) if v.get("ra") is not None else None
        dec = round(v["dec"], 3) if v.get("dec") is not None else None
        return _key(v["target"].lower(), ra, dec)

    def program(self, tel, prog):
        return self.data["programs"].get(f"{tel}:{prog}")

    def get(self, tel, visit):
        """Cached facts for a visit, shaped for the browser (None when not looked up yet)."""
        info = self.data["targets"].get(self.target_key(tel, visit))
        if not info or info.get("failed"):
            return None
        out = {k: info.get(k) for k in ("resolved", "main_id", "type", "morph", "distance", "aliases", "nearest", "wiki")}
        out["image"] = f"api/space/image?k={info['image'].rsplit('.', 1)[0]}" if info.get("image") else None
        out["image_caption"] = info.get("image_caption")
        out["sources"] = list(info.get("sources") or [])
        prog = visit.get("proposal")
        sched = visit.get("program") or {}
        if sched.get("title"):                    # from Space Telescope Live's schedule: no lookup needed
            out["program"] = dict(sched, url=PROGRAM_URL[tel].format(id=sched["id"]))
            out["sources"].append("STScI")
            return out
        p = self.program(tel, prog) if prog else None
        if p:
            out["program"] = p
            if p.get("title"):
                out["sources"].append("MAST")
        return out

    def prefetch(self, wanted, now=None, budget=BUDGET):
        """wanted: [(tel, visit)], most important first. Looks up at most `budget` new ones."""
        now = now or time.time()
        done = 0
        for tel, v in wanted:
            if done >= budget:
                break
            k = self.target_key(tel, v)
            cur = self.data["targets"].get(k)
            if cur and not ((cur.get("failed") or cur.get("errors")) and now - cur["fetched_at"] > FAIL_RETRY):
                continue
            done += 1
            try:
                self.lookup(tel, v, now)
            except Exception as e:   # noqa: BLE001 - network or format trouble: back off this target
                log.warning("space: info lookup for %s failed: %s", v["target"], e)
                with self.lock:
                    self.data["targets"][k] = {"name": v["target"], "fetched_at": now, "failed": str(e)[:200]}
        if done:
            self._trim()
            self.save()
        return done

    def _trim(self):
        t = self.data["targets"]
        if len(t) > MAX_ENTRIES:
            for k in sorted(t, key=lambda k: t[k].get("fetched_at", 0))[:len(t) - MAX_ENTRIES]:
                del t[k]

    def save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            with self.lock:
                tmp.write_text(json.dumps(self.data, separators=(",", ":")))
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("space: cannot save info cache: %s", e)

    def image_path(self, k):
        """Path of a cached cutout for GET /api/space/image?k=..., or None (never fetches)."""
        if not self.img_dir or not re.fullmatch(r"[0-9a-f]{16}", k or ""):
            return None
        for ext in ("jpg", "gif", "png"):
            p = self.img_dir / f"{k}.{ext}"
            if p.is_file():
                return p
        return None


def _sep(ra1, dec1, ra2, dec2):
    d = math.pi / 180
    a = math.sin(dec1 * d) * math.sin(dec2 * d) + math.cos(dec1 * d) * math.cos(dec2 * d) * math.cos((ra1 - ra2) * d)
    return math.acos(max(-1, min(1, a))) / d


def _tidy(s):
    return re.sub(r"\s+", " ", str(s)).strip() if s else s


def pending_info(tel, visit):
    """Before a target's lookup finishes: what the schedule itself says (the program), marked pending."""
    p = visit.get("program") or {}
    if not p.get("title"):
        return None
    return {"pending": True, "program": dict(p, url=PROGRAM_URL[tel].format(id=p["id"])), "sources": ["STScI"]}


# ---------------------------------------------------------------- demo facts (no network)

DEMO_FACTS = {
    "NGC 1365": ("Barred spiral galaxy", "~56 million light years",
                 "A large barred spiral in Fornax with an active nucleus; its bar funnels gas toward the central black hole."),
    "Abell 2744": ("Galaxy cluster", "~4 billion light years",
                   "A massive cluster of merging galaxy groups whose gravity lenses and magnifies galaxies far behind it."),
    "WR 140": ("Wolf-Rayet star", "~5,600 light years",
               "A pair of massive stars whose colliding winds make carbon dust, puffed out in rings every eight years."),
    "Messier 51": ("Interacting spiral galaxy", "~31 million light years",
                   "The Whirlpool Galaxy, a face-on spiral being tugged by its small companion NGC 5195."),
    "Crab Nebula": ("Supernova remnant", "~6,500 light years",
                    "The debris of a star seen exploding in 1054, powered by a spinning neutron star at its center."),
    "Orion Nebula": ("Star-forming nebula", "~1,340 light years",
                     "The closest large stellar nursery to Earth, lit by a cluster of hot young stars."),
    "TRAPPIST-1": ("Red dwarf star with planets", "~40 light years",
                   "A small, cool star with seven rocky planets, three of them in its habitable zone."),
}


def demo_info(tel, visit):
    f = DEMO_FACTS.get(visit["target"])
    out = {"resolved": bool(f), "main_id": visit["target"], "type": f[0] if f else None, "distance": f[1] if f else None,
           "wiki": {"summary": f[2], "url": None, "title": visit["target"]} if f else None,
           "nearest": None if f else {"name": "a catalogued object", "type": "galaxy", "sep_arcmin": 4.0, "distance": None},
           "aliases": [], "morph": None, "image": None, "sources": ["demo"]}
    if visit.get("proposal"):
        out["program"] = {"id": visit["proposal"], "title": "Example program (simulated)", "pi": "example name",
                          "url": PROGRAM_URL[tel].format(id=visit["proposal"])}
    return out
