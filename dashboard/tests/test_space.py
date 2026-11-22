"""Space section data (space.py). Run: python3 -m unittest discover dashboard/tests

The network is never touched: SpaceFeed takes an `opener`, and these tests give it canned replies
shaped like the real ones (ACROSS from its published OpenAPI SDK models, Horizons' CSV vector table,
CelesTrak's TLE text)."""
import json
import math
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import space  # noqa: E402

T = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).timestamp()
TLE = ("HST\n"
       "1 20580U 90037B   26326.25000000  .00002000  00000-0  10000-3 0  9991\n"
       "2 20580  28.4700 150.0000 0002400  90.0000 270.0000 15.27000000 10002")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")


def obs(name, begin, end, ra=10.0, dec=20.0, inst="i-wfc3", created=T - 86400):
    return {"instrument_id": inst, "object_name": name, "pointing_position": {"ra": ra, "dec": dec},
            "date_range": {"begin": iso(begin), "end": iso(end)}, "external_observation_id": f"{name}-{begin}",
            "type": "imaging", "status": "planned", "proposal_reference": "17001", "id": f"id-{name}-{begin}",
            "schedule_id": "s1", "created_on": iso(created), "bandpass": {}}


def horizons_text(t0):
    lines = []
    for k in range(13):
        t = t0 - 86400 + k * 6 * 3600
        jd = t / 86400 + 2440587.5
        lon = (space.sun_ecliptic_lon(t) + 180) * math.pi / 180
        lines.append(f"{jd:.9f}, A.D. 2026-Nov-22 00:00:00.0000, {1.5e6 * math.cos(lon):.6E}, "
                     f"{1.5e6 * math.sin(lon):.6E}, {3.0e5:.6E},")
    return {"result": "*** header ***\n$$SOE\n" + "\n".join(lines) + "\n$$EOE\n*** footer ***"}


class AstronomyTest(unittest.TestCase):
    def test_sun_longitude_at_equinox_and_solstice(self):
        eq = datetime(2026, 3, 20, 14, 46, tzinfo=timezone.utc).timestamp()
        sol = datetime(2026, 6, 21, 8, 24, tzinfo=timezone.utc).timestamp()
        self.assertLess(abs(space._wrap(space.sun_ecliptic_lon(eq))), 0.1)
        self.assertLess(abs(space.sun_ecliptic_lon(sol) - 90), 0.1)

    def test_equatorial_ecliptic_round_trip_and_pole(self):
        lon, lat = space.eq_to_ecl(83.63, 22.01, T)        # Crab Nebula sits almost on the ecliptic
        self.assertLess(abs(lat), 2)
        ra, dec = space.ecl_to_eq(lon, lat, T)
        self.assertAlmostEqual(ra, 83.63, places=6)
        self.assertAlmostEqual(dec, 22.01, places=6)
        self.assertGreater(space.eq_to_ecl(270, 66.56, T)[1], 89.9)   # north ecliptic pole

    def test_hubble_from_tle(self):
        el = space.parse_tle(TLE)
        self.assertEqual(datetime.fromtimestamp(el["epoch"], timezone.utc).strftime("%Y-%m-%d %H:%M"), "2026-11-22 06:00")
        p = space.hubble_position(el, T)
        self.assertTrue(480 < p["alt_km"] < 560, p)            # HST flies at ~510-540 km
        self.assertAlmostEqual(p["period_min"], 94.3, delta=0.2)
        lats = [space.hubble_position(el, T + k * 300)["ecl_lat"] for k in range(20)]
        self.assertLess(max(abs(x) for x in lats), 28.47 + 23.5)   # bounded by inclination + obliquity
        self.assertGreater(max(lats) - min(lats), 20)              # and it really moves around the orbit

    def test_webb_from_horizons(self):
        rows = space.parse_horizons(horizons_text(T))
        self.assertEqual(len(rows), 13)
        p = space.webb_position(rows, T)
        self.assertLess(abs(p["from_anti_sun_deg"]), 1)
        self.assertAlmostEqual(p["dist_km"], math.hypot(1.5e6, 3e5), delta=2000)
        self.assertFalse(p["extrapolated"])
        with self.assertRaises(ValueError):
            space.parse_horizons({"result": "No ephemeris for target \"-170\" prior to A.D. 2021"})


class ScheduleTest(unittest.TestCase):
    def test_versions_deduped_and_exposures_merged_into_visits(self):
        items = [obs("M51", T - 7200, T - 6000), obs("M51", T - 5400, T - 4000),           # one visit, 2 exposures
                 obs("M51", T - 5400, T - 4000, created=T - 3600, inst="i-acs"),            # newer version of the 2nd
                 obs("Crab Nebula", T - 3000, T + 1800, ra=83.63, dec=22.01),
                 obs("TRAPPIST-1", T + 3600, T + 7200),
                 {"object_name": "broken"}]                                                 # skipped, not fatal
        exp = space.across_observations(items, {"i-wfc3": "WFC3", "i-acs": "ACS"})
        self.assertEqual(len(exp), 4)
        vs = space.visits(exp)
        self.assertEqual([v["target"] for v in vs], ["M51", "Crab Nebula", "TRAPPIST-1"])
        self.assertEqual(vs[0]["instruments"], ["WFC3", "ACS"])
        cur, recent, upcoming = space.pointing(vs, T)
        self.assertEqual(cur["target"], "Crab Nebula")
        self.assertEqual([v["target"] for v in recent], ["M51"])
        self.assertEqual([v["target"] for v in upcoming], ["TRAPPIST-1"])
        d = space.describe(cur, T)
        self.assertLess(abs(d["ecl_lat"]), 2)
        self.assertGreater(d["sun_angle"], 0)

    def test_missing_or_bad_coordinates_stay_none(self):
        o = obs("Target X", T - 60, T + 60)
        o["pointing_position"] = {"ra": "nan", "dec": 999}
        d = space.describe(space.visits(space.across_observations([o]))[0], T)
        self.assertIsNone(d["ra"])
        self.assertNotIn("ecl_lon", d)


def offline(*_):
    raise OSError("offline in tests")


class FakeNet:
    """Canned replies by URL; records what was asked."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, url):
        self.calls.append(url)
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        for f in self.fail:
            if f in url:
                raise OSError(f"{f} down")
        if "celestrak" in u.netloc:
            return TLE
        if "horizons" in u.path:
            self.horizons_query = q
            return json.dumps(horizons_text(T))
        if u.path.endswith("/observatory/"):
            name = q["name"][0]
            return json.dumps([{"id": f"obs-{name}", "name": name, "short_name": name, "telescopes": [{"id": f"tel-{name}", "name": name}]}])
        if u.path.endswith("/instrument/"):
            return json.dumps([{"id": "i-wfc3", "name": "Wide Field Camera 3", "short_name": "WFC3"}])
        if u.path.endswith("/observation/"):
            self.observation_query = q
            page = int(q["page"][0])
            items = [obs("Messier 51", T - 600, T + 600)] if page == 1 else []
            return json.dumps({"total_number": 1, "page": page, "page_limit": 500, "items": items})
        if "cds.unistra.fr" in u.netloc or "wikipedia" in u.netloc:
            raise OSError("not faked here: see test_space_info.py")
        raise AssertionError("unexpected URL " + url)


class FeedTest(unittest.TestCase):
    def test_live_refresh_view_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            net = FakeNet()
            f = space.SpaceFeed(d, opener=net, poster=offline, fetch_bytes=offline)
            f.refresh(now=T)
            q = net.observation_query
            self.assertEqual(q["observatory_ids"], ["obs-JWST"])       # last telescope asked
            self.assertTrue(q["date_range_begin"][0].startswith("2026-11-19T12:00:00"))
            self.assertEqual(net.horizons_query["COMMAND"], ["'-170'"])
            v = f.view(now=T)
            hst, jwst = v["telescopes"]
            self.assertEqual((hst["current"]["target"], hst["current"]["instrument"]), ("Messier 51", "WFC3"))
            self.assertEqual(hst["position"]["kind"], "earth-orbit")
            self.assertEqual(jwst["position"]["kind"], "l2")
            self.assertTrue(all(s["error"] is None for s in v["sources"].values()))
            self.assertIsNone(hst["current"]["info"])                   # lookups failed softly (not faked here)
            json.dumps(v)                                               # serializable as served
            # nothing is due again an hour later except... nothing: schedules are hourly, so +30 min asks nothing
            n = len(net.calls)
            f.refresh(now=T + 1800)
            self.assertEqual(len(net.calls), n)
            # a restart reads the cache instead of refetching
            g = space.SpaceFeed(d, opener=FakeNet(fail=["http"]), poster=offline, fetch_bytes=offline)
            self.assertEqual(g.view(now=T)["telescopes"][0]["current"]["target"], "Messier 51")

    def test_one_source_down_keeps_the_others_and_retries_later(self):
        net = FakeNet(fail=["celestrak"])
        f = space.SpaceFeed(None, opener=net, poster=offline, fetch_bytes=offline)
        f.refresh(now=T)
        v = f.view(now=T)
        self.assertIsNone(v["telescopes"][0]["position"])
        self.assertIn("celestrak down", v["sources"]["tle"]["error"])
        self.assertIsNotNone(v["telescopes"][0]["current"])
        net.fail.clear()
        f.refresh(now=T + 60)                                           # too soon to retry
        self.assertIsNone(f.view(now=T)["telescopes"][0]["position"])
        f.refresh(now=T + space.RETRY + 1)
        self.assertIsNotNone(f.view(now=T)["telescopes"][0]["position"])

    def test_demo_respects_pointing_limits(self):
        f = space.SpaceFeed(None, mode="demo", opener=FakeNet(fail=["http"]), poster=offline, fetch_bytes=offline)
        for k in range(0, 400, 37):                                     # different days through the year
            v = f.view(now=T + k * 86400)
            for t in v["telescopes"]:
                lo, hi = t["sun_limits"]
                for vis in [t["current"]] + t["recent"]:
                    if vis and vis.get("sun_angle") is not None:
                        self.assertTrue(lo - 4 <= vis["sun_angle"] <= hi + 4, (t["id"], vis))   # the Sun moves ~1 deg/day
            self.assertEqual(v["telescopes"][1]["position"]["kind"], "l2")
            self.assertIn("sources", v["telescopes"][0]["current"]["info"])


if __name__ == "__main__":
    unittest.main()
