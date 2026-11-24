"""Screenshot every dashboard page (desktop + phone), for usability review. Signs in with an MCP bearer token (owner
view). The SSE stream never goes idle, so pages are awaited on `load` plus a pause rather than `networkidle`.

    uv run python scripts/shots.py BASE [--guild KEY] [--token T] [--out DIR]

BASE is required: there is no default, so a run never lands on the live bot by accident (phase 4 — screenshots are
taken on scripts/serve_scratch.py, http://127.0.0.1:8799, whose token is the test suite's and is used when BASE is that
address). `--token` names another token; otherwise OIBOT_MCP_TOKEN from the environment / .env.

Several guilds (design §5.32): `--guild` names the one to shoot (the bearer acts as its owner; sent as the
X-Oibot-Guild header and ?g= on every page). Without it a bot serving several guilds answers 400 "Which guild?"."""
import argparse
import re
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
SCRATCH = "http://127.0.0.1:8799"  # scripts/serve_scratch.py
SCRATCH_TOKEN = "a" * 64  # tests/test_mcp.TOKEN: the scratch server's owner login

PAGES = ["/app/me", "/app/events", "/app/events/new", "/app/schedules", "/app/raids", "/app/members", "/app/loot", "/app/loot-log", "/app/professions", "/app/applications",
         "/app/policy", "/app/agents", "/app/auras", "/app/ops", "/app/config", "/app/help", "/schedule"]
# list pages (phase 4): on the phone the list shows first; the first [data-list-item] is tapped and the detail shot too
DETAIL = {"/app/raids", "/app/events", "/app/schedules"}
ap = argparse.ArgumentParser(description="Screenshot every dashboard page.")
ap.add_argument("base", nargs="?", default=None, help=f"the site to shoot (required; the scratch server is {SCRATCH})")
ap.add_argument("--guild", default=None, help="the guild key to shoot (several guilds)")
ap.add_argument("--token", default=None, help="the bearer token (default: the scratch token on the scratch server, else OIBOT_MCP_TOKEN)")
ap.add_argument("--out", default=None, help="where the images go (default out/shots)")
args = ap.parse_args(sys.argv[1:])
if not args.base:
    ap.error(f"name the site to shoot, e.g. {SCRATCH} (scripts/serve_scratch.py); there is no default")
BASE = args.base.rstrip("/")
TOKEN = args.token or (SCRATCH_TOKEN if BASE == SCRATCH else os.environ.get("OIBOT_MCP_TOKEN"))
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
OUT = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "out" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
if args.guild:
    HEADERS["X-Oibot-Guild"] = args.guild
QUERY = f"g={args.guild}" if args.guild else ""
# the tabbed views a plain page load doesn't show: (page, tab text) → <view>-<page>-<tab>.png, the tab clicked by its text
TABS = {"/app/me": ["Leveling guide"], "/app/ops": ["Plain-text permissions", "Listening"],
        "/app/members": ["Absences", "Test bench"],
        "/app/loot": ["History", "Precedents", "Items", "Wishlists", "Appeals", "Companions"],
        "/app/config": ["Settings", "Channels", "Recruitment and more", "Companions"]}


def url(path: str) -> str:
    """The page with ?g= joined to its own query (a Raids view carries ?view=)."""
    if not QUERY:
        return BASE + path
    return BASE + path + ("&" if "?" in path else "?") + QUERY


def settle(page) -> None:
    """A slow route (Members' attendance, Loot's history) can still say Loading… after the pause: wait up to 8 s more."""
    try:
        page.wait_for_function("!document.body.innerText.includes('Loading…')", timeout=8000)
    except Exception:  # noqa: BLE001 — a page that genuinely keeps a Loading… line is shot as it is
        pass


def shot_name(view: str, path: str, tab: str | None = None) -> Path:
    """desktop-me.png, desktop-schedule.png (a page outside /app: no leading slash, no subdirectory), desktop-loot-items.png,
    desktop-raids-<raid>-priority.png (a Raids view)."""
    page = re.sub(r"[?&]view=", "-", path.removeprefix("/app/")).strip("/").replace("/", "-")
    suffix = f"-{tab.lower().split()[0]}" if tab else ""
    return OUT / f"{view}-{(args.guild + '-') if args.guild else ''}{page}{suffix}.png"


def raid_views(ctx) -> list[str]:
    """The Raids page's other two views of its first raid (/api/raids' first row)."""
    r = ctx.request.get(url("/api/raids"))
    raids = r.json().get("raids") if r.ok else None
    if not raids:
        print("no raids to shoot the Priority and Readiness views of")
        return []
    rid = raids[0]["id"]
    return [f"/app/raids/{rid}?view=priority", f"/app/raids/{rid}?view=readiness"]


def shoot(page, fn: Path, w: int) -> None:
    page.screenshot(path=str(fn), full_page=True)
    print(fn.name, "scrollW", page.evaluate("document.documentElement.scrollWidth"), "of", w)


with sync_playwright() as p:
    b = p.chromium.launch()
    for name, w in (("desktop", 1360), ("phone", 400)):
        ctx = b.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1, extra_http_headers=HEADERS)
        page = ctx.new_page()
        pages = list(PAGES)
        at = pages.index("/app/raids") + 1
        pages[at:at] = raid_views(ctx)
        for path in pages:
            page.goto(url(path), wait_until="load")
            page.wait_for_timeout(1800)
            settle(page)
            shoot(page, shot_name(name, path), w)
            if name == "phone" and path in DETAIL:
                item = page.locator("[data-list-item]")
                if item.count():
                    item.first.click()
                    page.wait_for_timeout(1200)
                    settle(page)
                    shoot(page, shot_name(name, path, "detail"), w)
                else:
                    print(f"{name} {path}: no list item to tap")
            for tab in TABS.get(path, []):
                t = page.get_by_role("tab", name=re.compile(rf"^{re.escape(tab)}(\s|$)"))  # "Absences · 2" carries a count
                if not t.count():  # an owner-only tab (Config › Companions) the viewer doesn't get
                    print(f"{name} {path}: no tab {tab!r}")
                    continue
                t.first.click()
                page.wait_for_timeout(1200)
                settle(page)
                shoot(page, shot_name(name, path, tab), w)
        ctx.close()
    b.close()
