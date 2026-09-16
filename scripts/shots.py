"""Screenshot every dashboard page (desktop + phone) from localhost as the dev user, for usability review."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "out" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = ["/", "/admin", "/rosters", "/raids", "/bank", "/config", "/ops", "/admin/build"]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788"
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, w in (("desktop", 1360), ("phone", 400)):
        ctx = b.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()
        for path in PAGES:
            page.goto(BASE + path, wait_until="networkidle")
            fn = OUT / f"{name}-{path.strip('/').replace('/', '_') or 'me'}.png"
            page.screenshot(path=str(fn), full_page=True)
            print(fn.name, page.evaluate("document.body.scrollHeight"))
        ctx.close()
    b.close()
