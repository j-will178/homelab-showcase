#!/usr/bin/env python3
"""
Take the showcase README's screenshots from the static demo (simulated data only).

    python3 dashboard/tools/take_screenshots.py showcase/screenshots

Builds the static demo into a temp folder, serves it on 127.0.0.1, and saves one PNG per
screen at 1600x900, with the demo badge hidden. Needs Playwright with a Chromium/Chrome
(CHROME=/path, default /usr/bin/google-chrome).
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
RACK = {"noise": "noise-map", "smart": "drive-health", "compute": "compute", "tasks": "scheduled-tasks",
        "deploy": "deployments", "honeypot": "honeypot", "patterns": "attack-patterns"}
HIDE = ("document.addEventListener('DOMContentLoaded',()=>{const s=document.createElement('style');"
        "s.textContent='.demo-badge{display:none!important}';document.head.appendChild(s)})")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def shoot(url, out):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": W, "height": H})
        await page.add_init_script(HIDE)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        async def save(name, wait=2500):
            await page.wait_for_timeout(wait)
            await page.screenshot(path=str(out / f"{name}.png"))
            print("saved", name)

        await page.goto(url + "home/"); await save("home")
        await page.goto(url + "space/"); await save("space", 4000)
        await page.evaluate("document.getElementById('spCards').scrollIntoView({block: 'end'})"); await save("space-targets", 800)
        await page.goto(url + "space/gallery/"); await save("space-gallery")
        for sid, name in RACK.items():
            await page.goto(url + f"rack/?screen={sid}&interval=0")
            await page.wait_for_timeout(3000)
            await page.keyboard.press("r")            # stop rotating
            await save(name, 500)
        await browser.close()
        if errors:
            sys.exit("page errors: " + "; ".join(errors))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        subprocess.run([sys.executable, str(HERE / "build_static_demo.py"), str(site)], check=True, stdout=subprocess.DEVNULL)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(site)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            asyncio.run(shoot(f"http://127.0.0.1:{srv.server_port}/", args.out.resolve()))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    main()
