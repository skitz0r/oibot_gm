"""Screenshot every dashboard page (desktop + phone) from localhost, for usability review. Signs in with the MCP
bearer token from .env (owner view) — the dev-user login no longer exists on a public URL. The SSE stream never goes
idle, so pages are awaited on `load` plus a pause rather than `networkidle`."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
HEADERS = {"Authorization": f"Bearer {os.environ['OIBOT_MCP_TOKEN']}"} if os.environ.get("OIBOT_MCP_TOKEN") else {}

OUT = Path(__file__).resolve().parents[1] / "out" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = ["/app/me", "/app/rosters", "/app/raids", "/app/members", "/app/auras", "/app/ops", "/app/config"]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788"
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, w in (("desktop", 1360), ("phone", 400)):
        ctx = b.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1, extra_http_headers=HEADERS)
        page = ctx.new_page()
        for path in PAGES:
            page.goto(BASE + path, wait_until="load")
            page.wait_for_timeout(1800)
            fn = OUT / f"{name}-{path.removeprefix('/app/')}.png"
            page.screenshot(path=str(fn), full_page=True)
            print(fn.name, "scrollW", page.evaluate("document.documentElement.scrollWidth"), "of", w)
        ctx.close()
    b.close()
