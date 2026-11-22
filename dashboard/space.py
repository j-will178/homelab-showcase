"""Space section data: where Hubble and Webb are, and what they're observing.

Everything is fetched on the server, on a timer, and cached in state/space.json, so viewers
(and the kiosk) never call NASA themselves and a restart doesn't refetch:

  what they observe   NASA ACROSS API (api.across.sciencecloud.nasa.gov): the published weekly
                      schedules of HST and JWST, one record per planned exposure, with target
                      name, RA/Dec, instrument and times. Public, no key. Hourly.
  where Hubble is     CelesTrak two-line elements (TLE) for NORAD 20580. Every 12 h (CelesTrak
                      blocks clients that re-download unchanged data too often).
  where Webb is       JPL Horizons vectors for NAIF id -170 (Earth-centred, ecliptic). Daily,
                      3 days of 6-hourly points, interpolated.

These are *planned* schedules, not telemetry: the API says what each telescope was scheduled
to point at. Target-of-opportunity observations can override them.

GET /api/space returns one snapshot (see SpaceFeed.view). Positions and directions are given
in ecliptic longitude/latitude (the top-down view) and RA/Dec (the sky map), computed here so
the browser only draws. --demo (and the static demo) use generated data from DEMO_TARGETS.
"""
import json
import logging
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import space_info

log = logging.getLogger("space")

ACROSS = "https://api.across.sciencecloud.nasa.gov/v1"
# STScI's Space Telescope Live (spacetelescopelive.org), the public "what are Hubble and Webb observing" app.
# Its JSON back end isn't documented: GET /api/get/<webb|hubble> with an `endpoint:` header choosing the data
# ("current", "schedule/next24hours/<UTC time>", or an observation id). Times far in the future get 403.
STL = "https://spacetelescopelive.org/api/get/{slug}"
CELESTRAK_TLE = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE"
HORIZONS = "https://ssd.jpl.nasa.gov/api/horizons.api"
USER_AGENT = "homelab-rack-dashboard/1 (+https://github.com/j-will178/homelab)"

TELESCOPES = [
    # across: observatory name for the ACROSS lookup; sun: allowed angle from the Sun (degrees)
    {"id": "hst", "name": "Hubble", "stl": "hubble", "across": "HST", "orbit": "tle", "norad": 20580,
     "sun": [50, 180], "hst_uuid": "eb43bec9-0002-4c05-b294-de71453d95b6"},
    {"id": "jwst", "name": "Webb", "stl": "webb", "across": "JWST", "orbit": "horizons", "naif": -170, "sun": [85, 135]},
]
EVERY = {"schedule": 3600, "tle": 12 * 3600, "horizons": 24 * 3600}
# Bump when the cached schedule's shape or the parsers change: a cache written by another version
# refetches the schedules on start instead of serving its old reading for up to an hour.
SCHEDULE_FORMAT = 3
RETRY = 15 * 60                     # after a failed fetch
PAST, AHEAD = 3 * 86400, 2 * 86400  # schedule window around now
VISIT_GAP = 2 * 3600                # exposures of one target closer than this are one visit
RECENT, NEXT = 6, 3
MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 40

MU = 398600.4418                    # km^3/s^2
R_EARTH = 6378.137                  # km
J2 = 1.08263e-3
DEG = math.pi / 180


# ---------------------------------------------------------------- astronomy (low precision, fine for a picture)

def jd(t):
    return t / 86400 + 2440587.5


def obliquity(t):
    return 23.439 - 0.0000004 * (jd(t) - 2451545.0)


def sun_ecliptic_lon(t):
    """Apparent ecliptic longitude of the Sun in degrees (accurate to ~0.01 deg)."""
    n = jd(t) - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = (357.528 + 0.9856003 * n) % 360 * DEG
    return (L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360


def ecl_to_eq(lon, lat, t):
    e = obliquity(t) * DEG
    lon, lat = lon * DEG, lat * DEG
    x = math.cos(lat) * math.cos(lon)
    y = math.cos(lat) * math.sin(lon) * math.cos(e) - math.sin(lat) * math.sin(e)
    z = math.cos(lat) * math.sin(lon) * math.sin(e) + math.sin(lat) * math.cos(e)
    return math.atan2(y, x) / DEG % 360, math.asin(max(-1, min(1, z))) / DEG


def eq_to_ecl(ra, dec, t):
    e = obliquity(t) * DEG
    ra, dec = ra * DEG, dec * DEG
    x = math.cos(dec) * math.cos(ra)
    y = math.cos(dec) * math.sin(ra) * math.cos(e) + math.sin(dec) * math.sin(e)
    z = -math.cos(dec) * math.sin(ra) * math.sin(e) + math.sin(dec) * math.cos(e)
    return math.atan2(y, x) / DEG % 360, math.asin(max(-1, min(1, z))) / DEG


def separation(ra1, dec1, ra2, dec2):
    a = (math.sin(dec1 * DEG) * math.sin(dec2 * DEG)
         + math.cos(dec1 * DEG) * math.cos(dec2 * DEG) * math.cos((ra1 - ra2) * DEG))
    return math.acos(max(-1, min(1, a))) / DEG


def sun(t):
    lon = sun_ecliptic_lon(t)
    ra, dec = ecl_to_eq(lon, 0, t)
    return {"ecl_lon": round(lon, 3), "ra": round(ra, 3), "dec": round(dec, 3)}


# ---------------------------------------------------------------- Hubble: TLE + two-body propagation with J2

def parse_tle(text):
    """Three-line or two-line TLE -> orbital elements (degrees, rev/day, epoch as unix time)."""
    lines = [l.rstrip() for l in str(text).splitlines() if l.strip()]
    l1 = next(l for l in lines if l.startswith("1 "))
    l2 = next(l for l in lines if l.startswith("2 "))
    yy, doy = int(l1[18:20]), float(l1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    epoch = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() + (doy - 1) * 86400
    return {"epoch": epoch, "inc": float(l2[8:16]), "raan": float(l2[17:25]), "ecc": float("0." + l2[26:33].strip()),
            "argp": float(l2[34:42]), "m": float(l2[43:51]), "n": float(l2[52:63]), "line1": l1, "line2": l2}


def propagate(el, t):
    """Earth-centred position (km, equatorial frame) at unix time t. Two-body plus the J2 drift of
    the node and perigee: a few km to tens of km off after a day, plenty for the top-down picture."""
    n = el["n"] * 2 * math.pi / 86400                       # rad/s
    a = (MU / n ** 2) ** (1 / 3)
    e, i = el["ecc"], el["inc"] * DEG
    p = a * (1 - e * e)
    k = 1.5 * J2 * (R_EARTH / p) ** 2 * n
    dt = t - el["epoch"]
    raan = el["raan"] * DEG - k * math.cos(i) * dt
    argp = el["argp"] * DEG + k * (2 - 2.5 * math.sin(i) ** 2) * dt
    M = (el["m"] * DEG + n * dt) % (2 * math.pi)
    E = M
    for _ in range(12):
        E -= (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    nu = 2 * math.atan2(math.sqrt(1 + e) * math.sin(E / 2), math.sqrt(1 - e) * math.cos(E / 2))
    r = a * (1 - e * math.cos(E))
    u = argp + nu
    x = r * (math.cos(raan) * math.cos(u) - math.sin(raan) * math.sin(u) * math.cos(i))
    y = r * (math.sin(raan) * math.cos(u) + math.cos(raan) * math.sin(u) * math.cos(i))
    z = r * math.sin(u) * math.sin(i)
    return x, y, z


def hubble_position(el, t):
    x, y, z = propagate(el, t)
    r = math.sqrt(x * x + y * y + z * z)
    ra, dec = math.atan2(y, x) / DEG % 360, math.asin(z / r) / DEG
    lon, lat = eq_to_ecl(ra, dec, t)
    period = 1440 / el["n"]
    return {"kind": "earth-orbit", "ecl_lon": round(lon, 2), "ecl_lat": round(lat, 2), "alt_km": round(r - R_EARTH),
            "period_min": round(period, 1), "tle_epoch": _iso(el["epoch"]),
            "elements": {k: el[k] for k in ("epoch", "inc", "raan", "ecc", "argp", "m", "n")}}


# ---------------------------------------------------------------- Webb: JPL Horizons vectors

def horizons_url(t):
    q = {"format": "json", "COMMAND": "'-170'", "OBJ_DATA": "'NO'", "MAKE_EPHEM": "'YES'", "EPHEM_TYPE": "'VECTORS'",
         "CENTER": "'500@399'", "REF_PLANE": "'ECLIPTIC'", "REF_SYSTEM": "'ICRF'", "VEC_TABLE": "'1'",
         "OUT_UNITS": "'KM-S'", "CSV_FORMAT": "'YES'", "STEP_SIZE": "'6 h'",
         "START_TIME": "'" + _day(t - 86400) + "'", "STOP_TIME": "'" + _day(t + 2 * 86400) + "'"}
    return HORIZONS + "?" + urllib.parse.urlencode(q)


def parse_horizons(payload):
    """Horizons JSON (or its text 'result') -> [[unix time, x, y, z km]] from the $$SOE..$$EOE block."""
    text = payload.get("result", "") if isinstance(payload, dict) else str(payload)
    if "$$SOE" not in text:
        raise ValueError("no ephemeris in Horizons reply: " + text.strip()[:200])
    rows = []
    for line in text.split("$$SOE", 1)[1].split("$$EOE", 1)[0].splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 5 and f[0]:
            rows.append([(float(f[0]) - 2440587.5) * 86400, float(f[2]), float(f[3]), float(f[4])])
    if not rows:
        raise ValueError("empty Horizons ephemeris")
    return rows


def webb_position(rows, t):
    rows = sorted(rows)
    a = max((r for r in rows if r[0] <= t), default=rows[0])
    b = min((r for r in rows if r[0] >= t), default=rows[-1])
    f = 0 if b[0] == a[0] else (t - a[0]) / (b[0] - a[0])
    x, y, z = (a[k] + f * (b[k] - a[k]) for k in (1, 2, 3))
    r = math.sqrt(x * x + y * y + z * z)
    lon = math.atan2(y, x) / DEG % 360
    anti_sun = (sun_ecliptic_lon(t) + 180) % 360
    return {"kind": "l2", "ecl_lon": round(lon, 2), "ecl_lat": round(math.asin(z / r) / DEG, 2),
            "dist_km": round(r), "from_anti_sun_deg": round(_wrap(lon - anti_sun), 1),
            "extrapolated": not rows[0][0] <= t <= rows[-1][0]}


# ---------------------------------------------------------------- schedules: NASA ACROSS

def across_observations(items, instruments=None):
    """ACROSS observation records -> [{target, ra, dec, begin, end, instrument, proposal}], one per
    exposure, newest version of each kept (ACROSS keeps superseded schedule versions too)."""
    instruments = instruments or {}
    best = {}
    for o in items or []:
        try:
            dr = o["date_range"]
            begin, end = _ts(dr["begin"]), _ts(dr["end"])
        except (KeyError, TypeError, ValueError):
            continue
        pos = o.get("pointing_position") or o.get("object_position") or {}
        rec = {"target": _text(o.get("object_name")) or "unnamed", "begin": begin, "end": max(end, begin),
               "ra": _num(pos.get("ra"), 0, 360), "dec": _num(pos.get("dec"), -90, 90),
               "instrument": instruments.get(o.get("instrument_id")) or "",
               "proposal": _text(o.get("proposal_reference")), "status": _text(o.get("status")),
               "created": _ts(o["created_on"]) if o.get("created_on") else 0}
        key = (rec["target"], round(begin / 60))
        if key not in best or rec["created"] >= best[key]["created"]:
            best[key] = rec
    return sorted(best.values(), key=lambda r: r["begin"])


INSTRUMENT_NAMES = {"NIRCAM": "NIRCam", "NIRSPEC": "NIRSpec", "MIRI": "MIRI", "NIRISS": "NIRISS", "FGS": "FGS",
                    "WFC3": "WFC3", "ACS": "ACS", "COS": "COS", "STIS": "STIS"}


def stl_time(t):
    """Space Telescope Live writes hours without a leading zero: 2026-11-22T7:50:00Z (10-minute steps)."""
    d = datetime.fromtimestamp(t - t % 600, timezone.utc)
    return f"{d:%Y-%m-%d}T{d.hour}:{d:%M}:00Z"


def stl_program(p):
    """A Space Telescope Live `proposal` object -> {id, title, pi, category, type} (same for Webb and Hubble)."""
    if not isinstance(p, dict) or not p:
        return None
    pi = (p.get("primaryInvestigator") or {}).get("formalName")
    cat = (p.get("scientificCategory") or {}).get("title") or (p.get("proposalType") or {}).get("title")
    typ = p.get("proposalTypeList") or (p.get("proposalType") or {}).get("code")
    return {"id": _text(p.get("proposalID")), "title": _text(p.get("title"), 200), "pi": _text(pi),
            "category": _text(cat), "type": _text(typ)}


def stl_target_name(raw):
    """Hubble lists several targets of one visit comma-separated: show the first and how many more."""
    names = [n.strip() for n in str(raw or "").split(",") if n.strip()]
    if not names:
        return None
    return _text(names[0]) + (f" (+{len(names) - 1})" if len(names) > 1 else "")


def stl_observations(items):
    """Space Telescope Live schedule records -> exposures like across_observations().

    Two shapes: Webb's are full visit records (targets, boresight, proposal with title/PI/category, visit
    type); Hubble's are flat (targetName, startTime/endTime, target RA/Dec, scienceInstrument, executed)
    with no program, which SpaceFeed fetches per observation id (`stl_id`) for the visits on screen."""
    out = {}
    for o in items or []:
        if not isinstance(o, dict) or o.get("deleteTime"):
            continue
        if "targetName" in o:                                      # Hubble's flat record
            try:
                begin, end = _ts(o["startTime"]), _ts(o["endTime"])
            except (KeyError, TypeError, ValueError):
                continue
            inst = str(o.get("scienceInstrument") or "")
            out[o.get("id") or (o["targetName"], begin)] = {
                "target": stl_target_name(o.get("targetName")) or "unnamed", "begin": begin, "end": max(end, begin),
                "ra": _num(o.get("targetRightAscensionInDegrees"), 0, 360),
                "dec": _num(o.get("targetDeclinationInDegrees"), -90, 90),
                "instrument": INSTRUMENT_NAMES.get(inst.upper()) or _text(inst) or "", "proposal": None, "program": None,
                "status": "executed" if o.get("executed") else None, "visit_type": None, "category": None,
                "stl_id": _text(o.get("id")), "created": 0}
            continue
        try:
            begin, end = _ts(o["scheduledStartTime"]), _ts(o["scheduledEndTime"])
        except (KeyError, TypeError, ValueError):
            continue
        tg = next((t for t in o.get("targets") or [] if not t.get("background")), None) or {}
        vt = _text((o.get("visitType") or {}).get("title"))
        # No named target (calibrations, some parallels): say what the visit is instead of "unnamed".
        name = (_text(tg.get("standardName")) or _text(tg.get("name")) or vt
                or _text((o.get("proposal") or {}).get("title"), 60) or "unnamed")
        ra = _num(o.get("boreSightRightAscensionInDegrees"), 0, 360)
        dec = _num(o.get("boreSightDeclinationInDegrees"), -90, 90)
        if ra is None or dec is None:
            ra, dec = _num(tg.get("rightAscensionInDegrees"), 0, 360), _num(tg.get("declinationInDegrees"), -90, 90)
        p = o.get("proposal") or {}
        insts = [INSTRUMENT_NAMES.get(str(i.get("code", "")).upper()) or _text(i.get("title")) for i in p.get("instruments") or []]
        program = stl_program(p)
        out[o.get("id") or (name, begin)] = {
            "target": name, "begin": begin, "end": max(end, begin), "ra": ra, "dec": dec,
            "instrument": ", ".join(i for i in insts if i), "proposal": program and program["id"], "program": program,
            "status": _text(o.get("executionStatus")), "visit_type": _text((o.get("visitType") or {}).get("title")),
            "category": _text(tg.get("category")), "stl_id": _text(o.get("id")), "created": 0}
    return sorted(out.values(), key=lambda r: r["begin"])


def with_programs(vs, programs):
    """Fill in programs fetched per observation (Hubble) from {stl_id: program}."""
    for v in vs:
        if not v.get("program") and v.get("stl_id") in programs:
            v["program"] = programs[v["stl_id"]]
            v["proposal"] = v["program"] and v["program"]["id"]
    return vs


def visits(exposures):
    """Consecutive exposures of one target -> one visit (Hubble splits a visit into many)."""
    out = []
    for e in exposures:
        v = out[-1] if out else None
        if v and v["target"] == e["target"] and e["begin"] - v["end"] <= VISIT_GAP:
            v["end"] = max(v["end"], e["end"])
            if e["instrument"] and e["instrument"] not in v["instruments"]:
                v["instruments"].append(e["instrument"])
            if v["ra"] is None and e["ra"] is not None:
                v["ra"], v["dec"] = e["ra"], e["dec"]
        else:
            out.append({"target": e["target"], "begin": e["begin"], "end": e["end"], "ra": e["ra"], "dec": e["dec"],
                        "instruments": [e["instrument"]] if e["instrument"] else [], "proposal": e["proposal"],
                        "program": e.get("program"), "category": e.get("category"), "visit_type": e.get("visit_type"),
                        "stl_id": e.get("stl_id")})
    return out


def describe(v, t):
    """A visit for the API: times as ISO, plus ecliptic direction and angle from the Sun."""
    d = {"target": v["target"], "begin": _iso(v["begin"]), "end": _iso(v["end"]),
         "instrument": ", ".join(v["instruments"]), "proposal": v.get("proposal"), "ra": None, "dec": None,
         "category": v.get("category"), "visit_type": v.get("visit_type")}
    if v["ra"] is not None and v["dec"] is not None:
        lon, lat = eq_to_ecl(v["ra"], v["dec"], t)
        s = sun(t)
        d.update(ra=round(v["ra"], 4), dec=round(v["dec"], 4), ecl_lon=round(lon, 2), ecl_lat=round(lat, 2),
                 sun_angle=round(separation(v["ra"], v["dec"], s["ra"], s["dec"]), 1))
    return d


def pointing(vs, t):
    """-> (current visit or None, recent visits newest first, next visits)."""
    current = next((v for v in vs if v["begin"] <= t < v["end"]), None)
    recent = [v for v in vs if v["end"] <= t and v is not current][-RECENT:][::-1]
    upcoming = [v for v in vs if v["begin"] > t][:NEXT]
    return current, recent, upcoming


# ---------------------------------------------------------------- demo data (no network)

DEMO_TARGETS = [   # name, RA, Dec (J2000 degrees), instruments (hst, jwst)
    ("NGC 1365", 53.40, -36.14), ("Abell 2744", 3.58, -30.40), ("WR 140", 305.12, 43.85),
    ("Messier 51", 202.47, 47.20), ("Crab Nebula", 83.63, 22.01), ("Orion Nebula", 83.82, -5.39),
    ("Carina Nebula", 161.27, -59.87), ("TRAPPIST-1", 346.62, -5.04), ("Messier 87", 187.71, 12.39),
    ("NGC 628", 24.17, 15.78), ("Cassiopeia A", 350.85, 58.81), ("Hubble Ultra Deep Field", 53.16, -27.79),
    ("Westerlund 1", 251.77, -45.85), ("Pillars of Creation", 274.70, -13.81), ("Sombrero Galaxy", 189.997, -11.62),
    ("Ring Nebula", 283.40, 33.03), ("Tarantula Nebula", 84.68, -69.10), ("Stephan's Quintet", 339.01, 33.96),
    ("Small Magellanic Cloud", 13.19, -72.83), ("Andromeda Galaxy", 10.68, 41.27), ("Large Magellanic Cloud", 80.89, -69.76),
    ("Whirlpool companion NGC 5195", 202.50, 47.27), ("Cartwheel Galaxy", 9.42, -33.72), ("Helix Nebula", 337.41, -20.84),
]
DEMO_INSTRUMENTS = {"hst": ["WFC3/UVIS", "WFC3/IR", "ACS/WFC", "COS", "STIS"],
                    "jwst": ["NIRCam Imaging", "NIRSpec IFU", "MIRI Imaging", "NIRISS WFSS"]}
DEMO_TLE = ("HST\n"
            "1 20580U 90037B   26325.50000000  .00002000  00000-0  10000-3 0  9990\n"
            "2 20580  28.4700 150.0000 0002400  90.0000 270.0000 15.27000000 10000")


def demo_schedule(tel, t, seed=None):
    """A believable week around t: targets the telescope is allowed to point at right now."""
    rng = random.Random(seed if seed is not None else f"{tel['id']}:{int(t // 86400)}")
    s = sun(t)
    lo, hi = tel["sun"]
    ok = [x for x in DEMO_TARGETS if lo <= separation(x[1], x[2], s["ra"], s["dec"]) <= hi] or DEMO_TARGETS
    out, cur = [], t - PAST
    while cur < t + AHEAD:
        name, ra, dec = rng.choice(ok)
        dur = rng.uniform(1.5, 9) * 3600
        out.append({"target": name, "ra": ra, "dec": dec, "begin": cur, "end": cur + dur,
                    "instrument": rng.choice(DEMO_INSTRUMENTS[tel["id"]]), "proposal": str(rng.randint(1000, 9999)),
                    "status": "planned", "created": 0})
        cur += dur + rng.uniform(0.1, 0.4) * 3600          # slew + overheads
    return out


def demo_webb_rows(t):
    """An L2 halo orbit, roughly: 1.5 million km anti-sunward, swinging ~25 deg over ~6 months."""
    rows = []
    for k in range(-4, 9):
        tt = t + k * 6 * 3600
        phase = 2 * math.pi * (tt / 86400) / 182
        lon = (sun_ecliptic_lon(tt) + 180 + 18 * math.sin(phase)) * DEG
        lat = 12 * math.cos(phase) * DEG
        r = 1.5e6
        rows.append([tt, r * math.cos(lat) * math.cos(lon), r * math.cos(lat) * math.sin(lon), r * math.sin(lat)])
    return rows


# ---------------------------------------------------------------- the feed

class SpaceFeed:
    """Background fetcher + cache. mode: 'live' (fetch, cache in state_dir), 'demo' (generated)."""

    def __init__(self, state_dir=None, mode="live", opener=None, poster=None, fetch_bytes=None, stl_opener=None):
        self.mode = mode
        self.path = Path(state_dir) / "space.json" if state_dir else None
        self.open = opener or self._http
        self.open_stl = stl_opener or self._http          # (url, headers) -> text
        # Facts about each target for the info panel (SIMBAD, Wikipedia, MAST, sky cutouts).
        # Target lookups get 10 s per request (vs 30 s): one unreachable service mustn't stall the queue.
        quick = opener or (lambda url: self._http(url, timeout=10))
        self.info = space_info.SpaceInfo(state_dir, opener=quick, poster=poster or self._http_post,
                                         fetch_bytes=fetch_bytes or self._http_bytes)
        self.lock = threading.Lock()
        self.cache = {"sources": {}, "schedules": {}, "tle": None, "horizons": None, "across_ids": {}, "stl_programs": {}}
        if self.path and self.path.exists():
            try:
                self.cache.update(json.loads(self.path.read_text()))
            except (OSError, ValueError) as e:
                log.warning("space: ignoring unreadable cache %s: %s", self.path, e)
        # A restart (usually a deploy with new code) retries failed sources right away instead of
        # waiting out RETRY from before the restart. Sources that worked keep their schedule.
        for src in self.cache["sources"].values():
            if src.get("error"):
                src["tried_at"] = 0
        if self.cache.get("schedule_format") != SCHEDULE_FORMAT:
            for name, src in self.cache["sources"].items():
                if name.startswith("schedule:"):
                    src["ok_at"] = src["tried_at"] = 0
            self.cache["schedule_format"] = SCHEDULE_FORMAT

    # -- network
    @staticmethod
    def _http(url, headers=None, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain",
                                                   **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("reply too large")
        return body.decode("utf-8", "replace")

    @staticmethod
    def _http_bytes(url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("reply too large")
        return body

    @staticmethod
    def _http_post(url, form):
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("reply too large")
        return body.decode("utf-8", "replace")

    def _json(self, url):
        """GET + JSON. A failure names the request ("api.across...nasa.gov/v1/observatory/: HTTP Error 500"),
        so /api/space's `sources` says which call broke, not just how."""
        try:
            return json.loads(self.open(url))
        except Exception as e:           # noqa: BLE001 - re-raised with the request named
            u = urllib.parse.urlsplit(url)
            raise RuntimeError(f"{u.netloc}{u.path}: {e}") from e

    def _due(self, name, every, now):
        s = self.cache["sources"].get(name) or {}
        last_ok, last_try = s.get("ok_at") or 0, s.get("tried_at") or 0
        return now - last_ok >= every and now - last_try >= (RETRY if s.get("error") else 0)

    def _record(self, name, now, error=None):
        s = self.cache["sources"].setdefault(name, {})
        s["tried_at"] = now
        if error:
            s["error"] = str(error)[:300]
            log.warning("space: %s fetch failed: %s", name, error)
        else:
            s.update(ok_at=now, error=None)

    def _across_id(self, tel):
        ids = self.cache["across_ids"]
        if tel["id"] not in ids:
            obs = self._json(f"{ACROSS}/observatory/?" + urllib.parse.urlencode({"name": tel["across"]}))
            match = [o for o in obs if tel["across"].lower() in (str(o.get("short_name", "")).lower(), str(o.get("name", "")).lower())]
            o = (match or obs or [None])[0]
            if not o and tel.get("hst_uuid"):
                o = {"id": tel["hst_uuid"], "telescopes": []}
            if not o:
                raise ValueError(f"ACROSS has no observatory named {tel['across']}")
            names = {}
            for tscope in o.get("telescopes") or []:
                for inst in self._json(f"{ACROSS}/instrument/?" + urllib.parse.urlencode({"telescope_id": tscope["id"]})):
                    names[inst["id"]] = inst.get("short_name") or inst.get("name") or ""
            ids[tel["id"]] = {"observatory": o["id"], "instruments": names}
        return ids[tel["id"]]

    def stl_programs(self, tel, exposures, now, budget=10):
        """Hubble's schedule has no program: fetch it per observation (`endpoint: <id>`) for the visits on
        screen (current, next, recent), once each, at most `budget` per refresh. Failures only log."""
        progs = self.cache.setdefault("stl_programs", {})
        cur, recent, upcoming = pointing(visits(exposures), now)
        want = [v["stl_id"] for v in ([cur] if cur else []) + upcoming + recent
                if v.get("stl_id") and not v.get("program") and v["stl_id"] not in progs]
        url = STL.format(slug=tel["stl"])
        for oid in want[:budget]:
            try:
                res = json.loads(self.open_stl(url, {"endpoint": oid, "Referer": f"https://spacetelescopelive.org/{tel['stl']}"}))
                progs[oid] = stl_program((res.get("data") or {}).get("proposal"))
            except Exception as e:       # noqa: BLE001 - the panel just lacks the program for now
                log.warning("space: %s program for %s: %s", tel["id"], oid, e)
                break
        if len(progs) > 2000:                                     # bounded: keep the newest ids (ULIDs sort by time)
            for k in sorted(progs)[:len(progs) - 2000]:
                del progs[k]

    def fetch_stl(self, tel, now):
        """Four 24-hour windows from Space Telescope Live: the past 3 days and the next day."""
        url = STL.format(slug=tel["stl"])
        items = []
        for k in (3, 2, 1, 0):
            hdr = {"endpoint": f"schedule/next24hours/{stl_time(now - k * 86400)}",
                   "Referer": f"https://spacetelescopelive.org/{tel['stl']}"}
            try:
                res = json.loads(self.open_stl(url, hdr))
            except Exception as e:       # noqa: BLE001 - re-raised with the request named
                raise RuntimeError(f"spacetelescopelive.org/api/get/{tel['stl']} ({hdr['endpoint']}): {e}") from e
            meta = res.get("_meta") or {}
            if meta.get("code") not in (200, None) or not isinstance(res.get("data"), list):
                raise RuntimeError(f"spacetelescopelive.org ({hdr['endpoint']}): {meta.get('code')} {meta.get('message')}")
            items += res["data"]
        exp = stl_observations(items)
        if items and not exp:                 # a reply we can't read is a failure, not an empty schedule
            raise RuntimeError(f"spacetelescopelive.org/api/get/{tel['stl']}: {len(items)} records, none readable "
                               f"(fields: {', '.join(sorted(items[0])[:8]) if isinstance(items[0], dict) else type(items[0]).__name__})")
        return exp

    def fetch_schedule(self, tel, now):
        ref = self._across_id(tel)
        fmt = "%Y-%m-%dT%H:%M:%S.%f+0000"
        q = {"observatory_ids": ref["observatory"], "page_limit": 500,
             "date_range_begin": datetime.fromtimestamp(now - PAST, timezone.utc).strftime(fmt),
             "date_range_end": datetime.fromtimestamp(now + AHEAD, timezone.utc).strftime(fmt)}
        items = []
        for page in range(1, MAX_PAGES + 1):
            res = self._json(f"{ACROSS}/observation/?" + urllib.parse.urlencode(dict(q, page=page)))
            got = res.get("items") or []
            items += got
            total = res.get("total_number") or 0
            if not got or len(items) >= total:
                break
        return across_observations(items, ref["instruments"])

    def refresh(self, now=None):
        """Fetch whatever is due. Each source fails on its own; the last good data stays."""
        now = now or time.time()
        if self.mode == "demo":
            return
        for tel in TELESCOPES:
            name = "schedule:" + tel["id"]
            if self._due(name, EVERY["schedule"], now):
                # Space Telescope Live first (also has program titles, PIs and execution status); ACROSS
                # if it fails. `via` says which one the schedule came from; `error` keeps both failures.
                errors = []
                for via, fetch in (("spacetelescopelive", self.fetch_stl), ("across", self.fetch_schedule)):
                    try:
                        exp = fetch(tel, now)
                    except Exception as e:   # noqa: BLE001 - try the next source
                        errors.append(f"{via}: {e}")
                        if via == "across":
                            self.cache["across_ids"].pop(tel["id"], None)
                        continue
                    with self.lock:
                        self.cache["schedules"][tel["id"]] = exp
                    self._record(name, now)
                    self.cache["sources"][name]["via"] = via
                    if via == "spacetelescopelive":
                        self.stl_programs(tel, exp, now)
                    if errors:
                        log.info("space: %s schedule from %s (%s)", tel["id"], via, errors[0])
                    break
                else:
                    self._record(name, now, " | ".join(errors))
        if self._due("tle", EVERY["tle"], now):
            try:
                el = parse_tle(self.open(CELESTRAK_TLE.format(norad=TELESCOPES[0]["norad"])))
                with self.lock:
                    self.cache["tle"] = el
                self._record("tle", now)
            except Exception as e:       # noqa: BLE001
                self._record("tle", now, e)
        if self._due("horizons", EVERY["horizons"], now):
            try:
                rows = parse_horizons(self._json(horizons_url(now)))
                with self.lock:
                    self.cache["horizons"] = rows
                self._record("horizons", now)
            except Exception as e:       # noqa: BLE001
                self._record("horizons", now, e)
        self._save()
        self.prefetch_info(now)

    def prefetch_info(self, now):
        """Look up facts for what's on screen: every current target, then next, then recent."""
        with self.lock:
            progs = self.cache.get("stl_programs") or {}
            per_tel = {t["id"]: with_programs(visits(self.cache["schedules"].get(t["id"]) or []), progs) for t in TELESCOPES}
        cur, nxt, rec = [], [], []
        for tel, vs in per_tel.items():
            c, r, n = pointing(vs, now)
            if c:
                cur.append((tel, c))
            nxt += [(tel, v) for v in n[:1]]
            rec += [(tel, v) for v in r]
        try:
            self.info.prefetch(cur + nxt + rec, now)
        except Exception:                # noqa: BLE001 - info is a nice-to-have
            log.exception("space: info prefetch failed")

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            with self.lock:
                tmp.write_text(json.dumps(self.cache, separators=(",", ":")))
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("space: cannot save cache: %s", e)

    def start_background(self, every=300):
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception:        # noqa: BLE001 - never let the thread die
                    log.exception("space: refresh failed")
                time.sleep(every)
        threading.Thread(target=loop, name="space", daemon=True).start()

    # -- the API
    def view(self, now=None):
        now = now or time.time()
        demo = self.mode == "demo"
        with self.lock:
            cache = json.loads(json.dumps(self.cache))
        out = {"generated_at": _iso(now), "mode": self.mode, "sun": sun(now), "telescopes": [],
               "sources": {} if demo else {k: {"ok_at": _iso(v.get("ok_at")), "error": v.get("error"), "via": v.get("via")}
                                           for k, v in cache["sources"].items()},
               "note": "Scheduled pointings from published plans (STScI Space Telescope Live, or NASA ACROSS), "
                       "not live telemetry."}
        for tel in TELESCOPES:
            d = {"id": tel["id"], "name": tel["name"], "sun_limits": tel["sun"], "position": None,
                 "current": None, "recent": [], "next": []}
            try:
                if tel["orbit"] == "tle":
                    el = parse_tle(DEMO_TLE) if demo else cache["tle"]
                    if el:
                        d["position"] = hubble_position(el, now)
                else:
                    rows = demo_webb_rows(now) if demo else cache["horizons"]
                    if rows:
                        d["position"] = webb_position(rows, now)
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
                log.warning("space: %s position: %s", tel["id"], e)
            exp = demo_schedule(tel, now) if demo else cache["schedules"].get(tel["id"]) or []
            cur, recent, upcoming = pointing(with_programs(visits(exp), cache.get("stl_programs") or {}), now)
            facts = (lambda v: space_info.demo_info(tel["id"], v)) if demo else (lambda v: self.info.get(tel["id"], v)
                                                                                  or space_info.pending_info(tel["id"], v))
            d["current"] = dict(describe(cur, now), info=facts(cur)) if cur else None
            d["recent"] = [dict(describe(v, now), ago_s=round(now - v["end"]), info=facts(v)) for v in recent]
            d["next"] = [dict(describe(v, now), info=facts(v)) for v in upcoming]
            d["schedule_covers_now"] = bool(exp) and exp[0]["begin"] <= now <= max(e["end"] for e in exp)
            out["telescopes"].append(d)
        return out


# ---------------------------------------------------------------- helpers

def _ts(s):
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def _day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _wrap(a):
    return (a + 180) % 360 - 180


def _num(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi and math.isfinite(v) else None


def _text(v, cap=120):
    if v is None:
        return None
    s = "".join(c for c in str(v) if c.isprintable()).strip()
    return s[:cap] or None
