"""Screenshot every dashboard page (desktop + phone) from localhost as the dev user, for usability review."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "out" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = ["/app/me", "/app/rosters", "/app/raids", "/app/members", "/app/ops", "/app/config"]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788"
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, w in (("desktop", 1360), ("phone", 400)):
        ctx = b.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()
        for path in PAGES:
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(600)
            fn = OUT / f"{name}-{path.removeprefix('/app/')}.png"
            page.screenshot(path=str(fn), full_page=True)
            print(fn.name, "scrollW", page.evaluate("document.documentElement.scrollWidth"), "of", w)
        ctx.close()
    b.close()
