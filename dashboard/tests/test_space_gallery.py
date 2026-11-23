"""Space gallery (space_gallery.py). Offline: RSS shaped like the ESA/Webb and ESA/Hubble feeds
(as published in 2026: <item> with link, pubDate, description and an image <enclosure>)."""
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import space_gallery as sg  # noqa: E402

WEBB = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>ESAWebb Images</title>
<item><title>GLIMPSE-17775 in Abell S1063 (NIRCam image annotated)</title><link>https://esawebb.org/images/weic2610a/</link>
<description></description><pubDate>Wed, 10 Jun 2026 16:00:00 +0200</pubDate><guid>https://esawebb.org/images/weic2610a/</guid>
<enclosure length="210372" type="image/jpeg" url="https://cdn.esawebb.org/archives/images/screen/weic2610a.jpg"/></item>
<item><title>Abell S1063 galaxy cluster</title><link>https://esawebb.org/images/weic2610c/</link>
<description>&lt;p&gt;This image shows the galaxy cluster Abell S1063. Its gravity magnifies galaxies far behind it. More text follows here.&lt;/p&gt;</description>
<pubDate>Wed, 10 Jun 2026 16:00:00 +0200</pubDate><guid>https://esawebb.org/images/weic2610c/</guid>
<enclosure length="300000" type="image/jpeg" url="https://cdn.esawebb.org/archives/images/screen/weic2610c.jpg"/></item>
<item><title>Abell S1063 (MIRI)</title><link>https://esawebb.org/images/weic2610d/</link><description>MIRI view.</description>
<pubDate>Wed, 10 Jun 2026 15:00:00 +0200</pubDate></item>
<item><title>Abell S1063 (wide)</title><link>https://esawebb.org/images/weic2610e/</link><description>Wide view.</description>
<pubDate>Wed, 10 Jun 2026 14:00:00 +0200</pubDate></item>
<item><title>Artist's illustration of a rogue planet</title><link>https://esawebb.org/images/weic2609b/</link>
<pubDate>Wed, 03 Jun 2026 16:00:00 +0200</pubDate></item>
</channel></rss>"""
POTW = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>ESAHubble Picture Of The Week</title>
<item><title>A neighbouring vista of stellar birth</title><link>https://esahubble.org/images/potw2552a/</link>
<description>&lt;img src="https://www.esahubble.orghttps://cdn.esahubble.org/archives/images/news/potw2552a.jpg"/&gt;
This week's image shows a star-forming region. Young stars light the gas. Third sentence.</description>
<pubDate>Mon, 29 Dec 2025 06:00:00 +0100</pubDate><guid>https://esahubble.org/images/potw2552a/</guid>
<enclosure length="501686" type="image/jpeg" url="https://cdn.esahubble.org/archives/images/screen/potw2552a.jpg"/></item>
</channel></rss>"""
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 500


class ParseTest(unittest.TestCase):
    def test_parse_skip_and_fallback_url(self):
        items = sg.parse_feed(WEBB, sg.FEEDS[0])
        self.assertEqual([i["id"] for i in items], ["weic2610c", "weic2610d", "weic2610e"])   # annotated + artist's skipped
        c = items[0]
        self.assertEqual(c["summary"], "This image shows the galaxy cluster Abell S1063. Its gravity magnifies galaxies far behind it.")
        self.assertEqual(c["image_url"], "https://cdn.esawebb.org/archives/images/screen/weic2610c.jpg")
        self.assertEqual(items[1]["image_url"], "https://cdn.esawebb.org/archives/images/screen/weic2610d.jpg")  # no enclosure
        self.assertEqual((c["telescope"], c["credit"]), ("Webb", "ESA/Webb, NASA & CSA"))

    def test_pick_limits_per_release_and_orders_by_date(self):
        items = sg.parse_feed(WEBB, sg.FEEDS[0]) + sg.parse_feed(POTW, sg.FEEDS[2])
        ids = [i["id"] for i in sg.pick(items)]
        self.assertEqual(ids, ["weic2610c", "weic2610d", "potw2552a"])                     # weic2610e: 3rd of one release
        self.assertTrue(sg.parse_feed(POTW, sg.FEEDS[2])[0]["summary"].startswith("This week's image shows"))


class FeedTest(unittest.TestCase):
    def make(self, d, down=()):
        calls = []

        def opener(url):
            calls.append(url)
            if any(x in url for x in down):
                raise urllib.error.URLError("timed out")
            return WEBB if "esawebb" in url and "potm" not in url else POTW if "hubble" in url else "<rss><channel/></rss>"

        def fetch(url):
            calls.append(url)
            return JPEG
        return sg.SpaceGallery(d, opener=opener, fetch_bytes=fetch), calls

    def test_refresh_download_view_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            g, calls = self.make(d)
            g.refresh(now=1_800_000_000)
            v = g.view()
            self.assertEqual([i["id"] for i in v["items"]], ["weic2610c", "weic2610d", "potw2552a"])
            self.assertEqual(v["items"][0]["image"], "api/space/gallery/image?id=weic2610c")
            self.assertTrue(g.image_path("weic2610c").read_bytes().startswith(b"\xff\xd8"))
            self.assertIsNone(g.image_path("../x"))
            self.assertIsNone(v["error"])
            n = len(calls)
            g.refresh(now=1_800_000_000 + 3600)                       # every 6 h: nothing asked
            self.assertEqual(len(calls), n)
            g2, calls2 = self.make(d)                                 # restart: cache served, nothing asked
            self.assertEqual(len(g2.view()["items"]), 3)
            g2.refresh(now=1_800_000_000 + 60)
            self.assertEqual(calls2, [])
            json.dumps(v)

    def test_mirror_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            g, calls = self.make(d, down=["esawebb.org/images/feed"])
            g.refresh(now=1_800_000_000)
            self.assertTrue(any("feedburner.com/esawebb/images" in u for u in calls))   # fell back to the mirror
            self.assertEqual(len(g.view()["items"]), 3)
            g3, _ = self.make(None, down=["http"])
            g3.refresh(now=1_800_000_000)
            v = g3.view()
            self.assertEqual(v["items"], [])
            self.assertIn("timed out", v["error"])

    def test_demo(self):
        v = sg.SpaceGallery(None, mode="demo").view(now=1_800_000_000)
        self.assertEqual(len(v["items"]), 4)
        self.assertTrue(all(i["image"] is None for i in v["items"]))


if __name__ == "__main__":
    unittest.main()
