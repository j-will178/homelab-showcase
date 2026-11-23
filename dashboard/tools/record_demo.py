#!/usr/bin/env python3
"""
Record the README hero: the static demo cycling through its screens and playing a
"Deploy all" rollout, the honeypot screens, the home page and the Space section, with a play button and "Open the live demo" drawn over it,
encoded as an animated WebP.

    python3 dashboard/tools/record_demo.py showcase/screenshots/demo.webp

Needs Playwright with a Chromium/Chrome (CHROME=/path, default /usr/bin/google-chrome)
and ffmpeg built with libwebp.
"""

import argparse
import asyncio
import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
W, H = 1600, 900

OVERLAY = """<html><head><meta charset="utf-8"></head><body style="margin:0;background:transparent">
<div style="position:relative;width:%(w)dpx;height:%(h)dpx">
  <div style="position:absolute;inset:0;background:radial-gradient(ellipse at center,rgba(5,8,12,.55) 0%%,rgba(5,8,12,.25) 45%%,rgba(5,8,12,0) 75%%)"></div>
  <div style="position:absolute;left:50%%;top:50%%;transform:translate(-50%%,-50%%);display:flex;flex-direction:column;align-items:center;gap:22px;font-family:system-ui,sans-serif">
    <div style="width:150px;height:150px;border-radius:50%%;background:rgba(56,189,248,.94);box-shadow:0 0 0 14px rgba(56,189,248,.25),0 18px 60px rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center">
      <div style="width:0;height:0;border-left:52px solid #061018;border-top:32px solid transparent;border-bottom:32px solid transparent;margin-left:12px"></div></div>
    <div style="font-size:48px;font-weight:700;color:#fff;text-shadow:0 4px 24px rgba(0,0,0,.9)">Open the live demo</div>
    <div style="font-size:24px;color:#e2e8f0;text-shadow:0 2px 14px rgba(0,0,0,.9)">Runs in your browser &middot; simulated data &middot; no install</div>
  </div></div></body></html>""" % {"w": W, "h": H}


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory):
    handler = functools.partial(QuietHandler, directory=directory)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def record(site_url, overlay_path, video_dir):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
        # The overlay: transparent PNG, drawn on top of every frame later.
        page = await browser.new_page(viewport={"width": W, "height": H})
        await page.set_content(OVERLAY)
        await page.screenshot(path=overlay_path, omit_background=True)
        await page.close()
        # The demo: noise -> drives -> compute -> tasks -> deployments, a rollout (5x speed), then honeypot and attack patterns.
        ctx = await browser.new_context(viewport={"width": W, "height": H}, record_video_dir=video_dir, record_video_size={"width": W, "height": H})
        page = await ctx.new_page()
        await page.add_init_script("document.addEventListener('DOMContentLoaded',()=>{const s=document.createElement('style');"
                                   "s.textContent='.demo-badge{display:none!important}';document.head.appendChild(s)})")
        await page.goto(f"{site_url}?screen=noise&interval=3&speed=5")
        await page.wait_for_function("document.querySelector('.screen.active')?.dataset.id === 'deploy'", timeout=60000)
        await page.wait_for_timeout(700)
        await page.keyboard.press("r")                # stop rotating; stay on Deployments
        await page.click("#deployAllBtn")
        await page.wait_for_timeout(13000)
        await page.keyboard.press("6")                # then the honeypot screens
        await page.wait_for_timeout(4500)
        await page.keyboard.press("7")
        await page.wait_for_timeout(4500)
        await page.keyboard.press("Home")             # then the home page and the Space section
        await page.wait_for_timeout(2500)
        await page.keyboard.press("2")
        await page.wait_for_timeout(5500)
        await ctx.close()
        await browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--width", type=int, default=1100, help="output width in pixels")
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--quality", type=int, default=60, help="WebP quality 0-100")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp, "site")
        subprocess.run([sys.executable, str(HERE / "build_static_demo.py"), str(site)], check=True, stdout=subprocess.DEVNULL)
        srv = serve(str(site))
        overlay, vids = Path(tmp, "overlay.png"), Path(tmp, "video")
        try:
            asyncio.run(record(f"http://127.0.0.1:{srv.server_port}/", str(overlay), str(vids)))
        finally:
            srv.shutdown()
        video = next(vids.glob("*.webm"))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.2", "-i", str(video), "-i", str(overlay),
                        "-filter_complex", f"[0][1]overlay=0:0,fps={args.fps},scale={args.width}:-1:flags=lanczos",
                        "-c:v", "libwebp_anim", "-lossless", "0", "-q:v", str(args.quality),
                        "-compression_level", "6", "-loop", "0", str(args.out)], check=True)
    print(f"{args.out}: {args.out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
