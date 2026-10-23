"""Screenshot every dashboard page (desktop + phone) from localhost, for usability review. Signs in with the MCP
bearer token from .env (owner view) — the dev-user login no longer exists on a public URL. The SSE stream never goes
idle, so pages are awaited on `load` plus a pause rather than `networkidle`.

    uv run python scripts/shots.py [BASE] [--guild KEY]

Several guilds (design §5.32): `--guild` names the one to shoot (the bearer acts as its owner; sent as the
X-Oibot-Guild header and ?g= on every page). Without it a bot serving several guilds answers 400 "Which guild?"."""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
HEADERS = {"Authorization": f"Bearer {os.environ['OIBOT_MCP_TOKEN']}"} if os.environ.get("OIBOT_MCP_TOKEN") else {}

OUT = Path(__file__).resolve().parents[1] / "out" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = ["/app/me", "/app/rosters", "/app/raids", "/app/members", "/app/readiness", "/app/loot", "/app/loot-log", "/app/applications",
         "/app/policy", "/app/agents", "/app/auras", "/app/ops", "/app/config", "/app/help", "/schedule"]
ap = argparse.ArgumentParser(description="Screenshot every dashboard page.")
ap.add_argument("base", nargs="?", default="http://127.0.0.1:8788")
ap.add_argument("--guild", default=None, help="the guild key to shoot (several guilds)")
args = ap.parse_args(sys.argv[1:])
BASE = args.base
if args.guild:
    HEADERS["X-Oibot-Guild"] = args.guild
QUERY = f"?g={args.guild}" if args.guild else ""
# the tabbed views a plain page load doesn't show: (page, tab text) → <view>-<page>-<tab>.png, the tab clicked by its text
TABS = {"/app/loot": ["History", "Precedents", "Items", "Wishlists", "Appeals", "Companions"],
        "/app/config": ["Settings", "Channels", "Recruitment and more", "Companions"]}


def settle(page) -> None:
    """A slow route (Members' attendance, Loot's history) can still say Loading… after the pause: wait up to 8 s more."""
    try:
        page.wait_for_function("!document.body.innerText.includes('Loading…')", timeout=8000)
    except Exception:  # noqa: BLE001 — a page that genuinely keeps a Loading… line is shot as it is
        pass


def shot_name(view: str, path: str, tab: str | None = None) -> Path:
    """desktop-me.png, desktop-schedule.png (a page outside /app: no leading slash, no subdirectory), desktop-loot-items.png."""
    page = path.removeprefix("/app/").strip("/").replace("/", "-")
    suffix = f"-{tab.lower().split()[0]}" if tab else ""
    return OUT / f"{view}-{(args.guild + '-') if args.guild else ''}{page}{suffix}.png"


with sync_playwright() as p:
    b = p.chromium.launch()
    for name, w in (("desktop", 1360), ("phone", 400)):
        ctx = b.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1, extra_http_headers=HEADERS)
        page = ctx.new_page()
        for path in PAGES:
            page.goto(BASE + path + QUERY, wait_until="load")
            page.wait_for_timeout(1800)
            settle(page)
            fn = shot_name(name, path)
            page.screenshot(path=str(fn), full_page=True)
            print(fn.name, "scrollW", page.evaluate("document.documentElement.scrollWidth"), "of", w)
            for tab in TABS.get(path, []):
                t = page.get_by_role("tab", name=tab, exact=True)
                if not t.count():  # an owner-only tab (Config › Companions) the viewer doesn't get
                    print(f"{name} {path}: no tab {tab!r}")
                    continue
                t.first.click()
                page.wait_for_timeout(1200)
                settle(page)
                fn = shot_name(name, path, tab)
                page.screenshot(path=str(fn), full_page=True)
                print(fn.name, "scrollW", page.evaluate("document.documentElement.scrollWidth"), "of", w)
        ctx.close()
    b.close()
