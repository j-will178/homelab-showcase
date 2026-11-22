"""Target facts for the Space info panel (space_info.py). No network: canned replies shaped like
CDS Sesame XML, SIMBAD TAP JSON, Wikipedia's page summary, MAST's invoke reply and a JPEG."""
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import space_info  # noqa: E402

SESAME_NGC1365 = """<?xml version="1.0" encoding="UTF-8"?>
<Sesame xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://vizier.u-strasbg.fr/xml/sesame_4x.xsd">
<Target option="SNV">
  <name>NGC 1365</name>
  <Resolver name="S=Simbad (CDS, via client/server)">
    <INFO>*** from cache</INFO>
    <otype>Sy1</otype>
    <jpos>03:33:36.45 -36:08:26.3</jpos>
    <jradeg>53.40187</jradeg>
    <jdedeg>-36.14065</jdedeg>
    <MType>SB(s)b</MType>
    <oname>NGC  1365</oname>
    <alias>NAME Great Barred Spiral Galaxy</alias>
    <alias>ESO 358-17</alias>
    <alias>NGC  1365</alias>
  </Resolver>
</Target>
</Sesame>"""
SESAME_NONE = """<?xml version="1.0" encoding="UTF-8"?>
<Sesame><Target option="SNV"><name>ABELL2744-PAR-3</name>
<Resolver name="S=Simbad (CDS, via client/server)"><INFO>*** Nothing found *** </INFO></Resolver></Target></Sesame>"""
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


def tap(cols, rows):
    return json.dumps({"metadata": [{"name": c} for c in cols], "data": rows})


class Net:
    def __init__(self):
        self.urls, self.posts = [], []

    def open(self, url):
        self.urls.append(url)
        u = urllib.parse.urlsplit(url)
        if "nph-sesame" in u.path:
            name = urllib.parse.unquote(u.query)
            return SESAME_NGC1365 if name == "NGC 1365" else SESAME_NONE
        if "sim-tap" in u.path:
            q = urllib.parse.parse_qs(u.query)["query"][0]
            if "FROM mesDistance" in q:
                if "NGC  1365" in q:
                    return tap(["dist", "unit"], [[17.2, "Mpc"], [18.0, "Mpc"], [17.6, "Mpc"], [5, "furlongs"]])
                return tap(["dist", "unit"], [])
            if "CONTAINS" in q:
                return tap(["main_id", "description", "nbref", "rvz_redshift", "sep"], [["ACO  2744", "Cluster of Galaxies", 2100, 0.308, 0.1]])
            return tap(["main_id", "otype", "description", "nbref", "rvz_redshift"], [["NGC  1365", "Sy1", "Seyfert 1 Galaxy", 1900, 0.00546]])
        if "wikipedia" in u.netloc:
            title = urllib.parse.unquote(u.path.rsplit("/", 1)[1])
            if title == "Great_Barred_Spiral_Galaxy":
                return json.dumps({"type": "standard", "title": "NGC 1365",
                                   "extract": "NGC 1365, also known as the Great Barred Spiral Galaxy, is a double-barred spiral galaxy about 56 million light-years away in the constellation Fornax. It was discovered in 1826. It is a Seyfert galaxy.",
                                   "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/NGC_1365"}}})
            raise OSError("404")
        raise AssertionError("unexpected " + url)

    def post(self, url, form):
        self.posts.append(json.loads(form["request"]))
        return json.dumps({"status": "COMPLETE", "data": [{"obs_title": "Star  formation in nearby barred spirals", "proposal_pi": "Example, A.", "proposal_id": "2107"}]})

    def fetch_bytes(self, url):
        self.urls.append(url)
        return JPEG


class InfoTest(unittest.TestCase):
    def test_famous_object(self):
        with tempfile.TemporaryDirectory() as d:
            net = Net()
            si = space_info.SpaceInfo(d, net.open, net.post, net.fetch_bytes)
            v = {"target": "NGC 1365", "ra": 53.40, "dec": -36.14, "proposal": "2107"}
            self.assertEqual(si.prefetch([("jwst", v)]), 1)
            got = si.get("jwst", v)
            self.assertTrue(got["resolved"])
            self.assertEqual((got["main_id"], got["type"], got["morph"]), ("NGC 1365", "Seyfert 1 Galaxy", "SB(s)b"))
            self.assertEqual(got["distance"], "~57 million light years")        # median of 17.2/17.6/18.0 Mpc
            self.assertEqual(got["aliases"], ["Great Barred Spiral Galaxy"])
            self.assertTrue(got["wiki"]["summary"].endswith("constellation Fornax. It was discovered in 1826."))
            self.assertEqual(got["program"]["title"], "Star formation in nearby barred spirals")
            self.assertEqual(net.posts[0]["params"]["filters"][0]["values"], ["JWST"])
            self.assertTrue(got["image"].startswith("api/space/image?k="))
            k = got["image"].split("k=")[1]
            self.assertTrue(si.image_path(k).read_bytes().startswith(b"\xff\xd8"))
            self.assertIsNone(si.image_path("../../etc/passwd"))
            self.assertIn("SIMBAD", got["sources"])
            # cached: a second prefetch asks nothing, and a restart reads the cache
            n = len(net.urls)
            self.assertEqual(si.prefetch([("jwst", v)]), 0)
            self.assertEqual(len(net.urls), n)
            si2 = space_info.SpaceInfo(d, None, None, None)
            self.assertEqual(si2.get("jwst", v)["main_id"], "NGC 1365")

    def test_unnamed_field_gets_nearest_known_object(self):
        net = Net()
        si = space_info.SpaceInfo(None, net.open, net.post, net.fetch_bytes)
        v = {"target": "ABELL2744-PAR-3", "ra": 3.50, "dec": -30.30, "proposal": None}
        si.prefetch([("hst", v)])
        got = si.get("hst", v)
        self.assertFalse(got["resolved"])
        self.assertEqual(got["nearest"]["name"], "ACO 2744")
        self.assertEqual(got["nearest"]["sep_arcmin"], 6.0)
        self.assertIsNone(got["nearest"]["distance"])            # z = 0.3 is too far for Hubble's law
        self.assertIsNone(got["image"])                          # no state dir, no image cache
        self.assertNotIn("program", got)

    def test_name_resolving_far_from_the_pointing_is_ignored(self):
        net = Net()
        si = space_info.SpaceInfo(None, net.open, net.post, net.fetch_bytes)
        v = {"target": "NGC 1365", "ra": 200.0, "dec": 10.0, "proposal": None}   # schedule says elsewhere
        si.prefetch([("hst", v)])
        self.assertFalse(si.get("hst", v)["resolved"])

    def test_failure_backs_off_and_budget_holds(self):
        calls = []

        def down(url):
            calls.append(url)
            raise OSError("down")
        si = space_info.SpaceInfo(None, down, None, None)
        vs = [("hst", {"target": f"T{i}", "ra": 10.0 + i, "dec": 0.0, "proposal": None}) for i in range(10)]
        self.assertEqual(si.prefetch(vs, now=1000), space_info.BUDGET)
        self.assertIsNone(si.get(*vs[0]))
        self.assertEqual(si.prefetch(vs[:space_info.BUDGET], now=1060), 0)          # backing off
        self.assertEqual(si.prefetch(vs[:1], now=1000 + space_info.FAIL_RETRY + 1), 1)

    def test_helpers(self):
        self.assertEqual(space_info.ly_text(1340), "~1.3 thousand light years")
        self.assertEqual(space_info.ly_text(40), "~40 light years")
        self.assertEqual(space_info.ly_text(4.1e9), "~4.1 billion light years")
        self.assertEqual(space_info.wiki_titles("M 51", {"main_id": "M  51", "aliases": ["NAME Whirlpool Galaxy", "NGC  5194"]})[:3],
                         ["Whirlpool Galaxy", "Messier 51", "NGC 5194"])
        self.assertEqual(space_info.first_sentences("A is a galaxy. It is big. It is far."), "A is a galaxy. It is big.")
        self.assertEqual(space_info.adql_str("O'Brien"), "'O''Brien'")


if __name__ == "__main__":
    unittest.main()
