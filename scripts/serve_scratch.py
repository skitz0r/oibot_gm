"""Serve THIS worktree's site on a scratch copy of the demo fixtures, for screenshots (phase 4: never the live bot).

    uv run python scripts/serve_scratch.py [--data DIR] [--guild KEY] [--port 8799] [--seed] [--member]

What it does:
- builds a throw-away data repo under the system temp directory (or `--data`, a NEW or scratch-marked folder strictly
  below it): the public `fixtures/demo` copy plus a guild.yaml (owner, officer role, timezone), `git init`, one commit,
  `GitStore(push=False)` — NEVER the real data repo (../oibot_gm-data), never a remote;
- registers the demo characters (fixtures/demo/characters.yaml, anonymized) as members through Registry.import_members;
- with `--seed`: a class minimum on Hyjal Summit, a weekly schedule with regulars, a one-off Hyjal Summit → Barrow
  Deeps ×2 three days out with 18 mains joined (an open, two-raid event), a locked Barrow Deeps one-off and a finished
  one four days ago (History, its board as it was locked);
- serves `web.create_app(tests/test_raid_night.NightBot)` — the suite's fullest bot double: the real RaidMixin verbs
  (open_run_and_post, lock, reschedule, lineup…) with Discord recorded, never called — with uvicorn on 127.0.0.1. The
  scratch login is the suite's MCP bearer token (tests/test_mcp.TOKEN, the guild's owner and so an officer), honoured
  from this machine only; `scripts/shots.py http://127.0.0.1:8799 --guild demo` sends it. The server's loop is the bot
  double's (attach_loop), so live updates reach open pages: a change made by a second request shows without a reload.
- with `--member`: the site as a MEMBER who is not an officer — a throw-away cookie secret for this process only, the
  lowest-id seeded main who is not the owner admitted to the scratch server, and `<data>/member_cookie.txt` = a session
  for them signed as web/app.py signs one; `scripts/shots.py … --member-cookie <data>/member_cookie.txt` shoots with it.

Safety: it refuses to start when DISCORD_TOKEN is set (a real bot's environment), on the live port, or on a data folder
that is not strictly below the temp directory or that holds anything this script didn't write (MARKER). Before any of
the bot's modules is imported, .env loading is switched off and every secret is taken out of the environment (again
after the imports); every module-level output path (agents, armory, auctions, guides, pending views, the listen log,
the LLM usage log, the icon cache) points into the scratch folder; the network reads the test suite refuses are refused
here too, no game icon is fetched (a missing one is drawn as the generated badge) and the launchd controls do nothing.
The live port is every port OIBOT_WEB_BIND names (the environment, or that one line of a .env in this checkout or above)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

LIVE_PORT = 8788  # the live site's port when nothing says otherwise (web_config's default address): never served from here
BIND_KEY = "OIBOT_WEB_BIND"  # the live site's host:port, in the environment or a checkout's .env


def _bind_port(value: str | None) -> int | None:
    port = (value or "").strip().strip("'\"").rpartition(":")[2]
    return int(port) if port.isdigit() else None


def live_ports(env=None, roots=None) -> set[int]:
    """Every port the live site may be on: the default, the environment's OIBOT_WEB_BIND, and the OIBOT_WEB_BIND line of
    the .env in this checkout and each folder above it (a worktree sits inside the bot's checkout). Only that one line
    is read; nothing from .env enters the environment."""
    env = os.environ if env is None else env
    ports = {LIVE_PORT} | ({_bind_port(env.get(BIND_KEY))} - {None})
    for root in (roots if roots is not None else [ROOT, *ROOT.parents]):
        f = Path(root) / ".env"
        if not f.is_file():
            continue
        for line in f.read_text(errors="replace").splitlines():
            key, sep, value = line.removeprefix("export ").partition("=")
            if sep and key.strip() == BIND_KEY and _bind_port(value):
                ports.add(_bind_port(value))
    return ports
DEMO_IDS = 300_000_000_000_000_000  # made-up Discord ids for the demo characters (below Registry.TEST_BASE)
MARKER = ".oibot-scratch"  # written into every data folder this script builds; nothing without it is ever deleted
# environment variables that grant access to something real (.env.example and the code's os.environ reads): taken out
SECRET_PREFIXES = ("DISCORD_", "ANTHROPIC_", "TYPESAFE_", "BLIZZARD_", "WCL_", "OIBOT_", "GIT_SSH")


def refuse(why: str) -> None:
    print(f"serve_scratch: {why}", file=sys.stderr)
    raise SystemExit(2)


def _temp_roots() -> set[Path]:
    return {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/private/tmp").resolve(),
            Path("/private/var/folders").resolve()}


def under_temp(p: Path) -> bool:
    """Strictly below a temp root — never a temp root itself."""
    rp = p.resolve()
    roots = _temp_roots()
    return rp not in roots and any(r in rp.parents for r in roots)


def data_refusal(p: Path) -> str | None:
    """Why `p` can't be the scratch data folder (None = it can): not strictly below the temp directory, a temp root, or
    a folder that exists with something in it and no MARKER (another session's scratch, a parent of several)."""
    if not under_temp(p):
        return f"{p} is not a folder below the system temp directory; the scratch server never opens a real data repo."
    if p.exists() and (not p.is_dir() or (any(p.iterdir()) and not (p / MARKER).is_file())):
        return f"{p} already holds files this script didn't write; name a new folder."
    return None


def build_data(root: Path, guild: str) -> None:
    import yaml

    from conftest import OFFICER_ROLE, OWNER

    why = data_refusal(root)
    if why:
        refuse(why)
    if root.exists() and (root / MARKER).is_file():  # only ever a folder this script built
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("built by scripts/serve_scratch.py; deleted and rebuilt by it\n")
    shutil.copytree(ROOT / "fixtures" / "demo", root / guild)
    (root / guild / "guild.yaml").write_text(yaml.safe_dump({
        "key": guild, "name": "Demo Guild", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": OWNER,
        "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE],
    }, sort_keys=False))
    git = ["git", "-c", "user.email=scratch@localhost", "-c", "user.name=scratch"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "scratch seed"], cwd=root, check=True)


def scrub_env(env=None) -> list[str]:
    """Take every variable that grants access out of `env` (default os.environ); → the names removed."""
    env = os.environ if env is None else env
    gone = [k for k in list(env) if k.startswith(SECRET_PREFIXES)]
    for k in gone:
        env.pop(k, None)
    return gone


def no_dotenv() -> None:
    """.env is never read in this process: dotenv.load_dotenv becomes a no-op BEFORE any module that calls it is
    imported (`from dotenv import load_dotenv` then binds the no-op)."""
    import dotenv
    import dotenv.main

    def skip(*a, **k) -> bool:
        return False

    dotenv.load_dotenv = skip
    dotenv.main.load_dotenv = skip


def sandbox(out: Path, setattr_=setattr) -> None:
    """Point every module-level output path at `out`, refuse the network reads the suite refuses, make the launchd
    controls and the LLM unusable. `setattr_` = the test's monkeypatch.setattr (restored after the test)."""
    from oibot_gm import agents, discord_armory, discord_auctions, discord_bot, pending_views, price_reference
    guides_build = importlib.import_module("oibot_gm.guides.build")  # the package exports a build() that shadows the module
    from oibot_gm.llm import decisions
    from oibot_gm import memory
    from oibot_gm.web import api_agents

    out.mkdir(parents=True, exist_ok=True)

    def no_network(url):
        raise OSError("the scratch server reads nothing from the network")

    setattr_(price_reference, "fetch_text", no_network)
    setattr_(agents, "OUT", out / "agents")
    setattr_(agents, "kickstart", lambda *a, **k: (False, "The scratch server doesn't start jobs."))
    setattr_(agents, "start_job", lambda *a, **k: (False, "The scratch server doesn't start jobs."))
    setattr_(agents, "launchd_status", lambda *a, **k: {})
    setattr_(api_agents, "LOG_FILE", out / "oibot.log")
    setattr_(discord_armory, "OUT", out / "armory")
    setattr_(discord_auctions, "OUT", out / "auctions")
    setattr_(guides_build, "OUT", out / "guides")
    setattr_(pending_views, "PATH", out / "pending_views.jsonl")
    setattr_(discord_bot, "OUT", out)
    setattr_(discord_bot, "LISTEN_LOG", out / "listen.jsonl")
    setattr_(decisions, "DECISIONS", None)
    setattr_(memory, "MEMORIES", None)


def attach_loop(app, bot) -> None:
    """Live updates: web/api's publish hands every topic to `bot.loop` (a real bot's Discord loop) with
    call_soon_threadsafe. The bot double has none, so at startup the server's own loop becomes the bot's: a save from
    any request — a second browser, a curl — reaches every open page's /api/events stream, as on the live site."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        bot.loop = asyncio.get_running_loop()
        async with inner(a) as state:
            yield state

    app.router.lifespan_context = lifespan


def import_demo(reg) -> None:
    import yaml

    from oibot_gm.registry import RANKS

    rows = []
    doc = yaml.safe_load((ROOT / "fixtures" / "demo" / "characters.yaml").read_text())
    for i, c in enumerate(doc.get("characters") or []):
        row = {"discord_id": str(DEMO_IDS + i), "name": c["name"], "character": c["name"], "cls": c["class"], "spec": c["spec"]}
        if c.get("rank") in RANKS and c.get("rank") != "alt":
            row["rank"] = c["rank"]
        rows.append(row)
    out = reg.import_members(rows, "scratch")
    print(f"serve_scratch: {len(out['added'])} demo members, {len(out['skipped'])} skipped")


def seed(reg, rs) -> None:
    from conftest import OWNER

    from oibot_gm import raidcycle as rc

    hyjal, barrow = "hyjal_summit_forever", "barrow_deeps"
    classes = list(reg.profile.classes)
    cls = "Paladin" if "Paladin" in classes else classes[0]
    reg.set_minimums(hyjal, {"healer": {"min": 4, "note": "four healers for the trash packs"}, cls: {"min": 2}}, "scratch")
    if not reg.all_schedules():
        reg.set_schedule("tuesday", {"kind": "weekly", "slots": "Tue 18:30", "name": "Tuesday raid night",
                                     "lineup": [{"raid": hyjal, "rosters": 1}, {"raid": barrow, "rosters": 2}]}, "scratch", raid=hyjal)
    sid = reg.all_schedules()[0]["id"]
    mains = sorted((m for m in reg.members.values() if not m.test and m.main), key=lambda m: m.discord_id)
    for m in mains[:8]:
        try:
            reg.regular_add(m.discord_id, sid, "scratch")
        except Exception as e:  # noqa: BLE001 — a seed row that doesn't fit is left out, said once
            print(f"serve_scratch: regular {m.display_name}: {e}")
    now = reg.now_local()
    start = (now + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, hyjal, start, by="scratch", lineup=[{"raid": barrow, "rosters": 2}])
    for m in mains[:18]:
        rc.set_signup(reg, rs, ev, m, None, "in", source="officer")
    for m in mains[28:30]:  # two Bench answers: the Events board's Bench section under Not placed
        rc.set_signup(reg, rs, ev, m, None, "sub", source="officer")
    late = (now + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    lk = rc.open_run(reg, rs, barrow, late, by="scratch")
    for m in mains[18:28]:
        rc.set_signup(reg, rs, lk, m, None, "in", source="officer")
    size = rc.run_size(reg, lk)
    g = int(reg.profile.comp_rules["group_size"])
    names = [m.main.label for m in mains[18:18 + size]]
    lk.rosters = rc.board_rosters(reg, lk, [names[i:i + g] for i in range(0, len(names), g)], size)
    lk.state, lk.locked_at = "locked", rc.now()
    lk.confirmations_sent_at = lk.locked_at
    lk.confirm_by = (lk.start - timedelta(hours=6)).isoformat()
    # the locked event's Bot activity tab (design 2026-11-28-event-activity-tab): Confirm asks in several states, an open
    # seat asked (one not delivered), one withdrawn, the opening DM with one person it missed, an officer's Remind
    rostered = mains[18:18 + size]
    for m in rostered[:6]:
        reg.add_placement_ask(m.discord_id, lk.team, m.main.label, "lock")
    for m, answer in zip(rostered[1:4], ("yes", "no", "expired")):
        reg.close_placement_ask(m.discord_id, lk.team, answer)
    soon = (now + timedelta(hours=5)).isoformat(timespec="seconds")
    for i, m in enumerate(mains[30:32]):
        a = rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="healer", character=m.main.label,
                       spec=m.main.spec, reason="short 1 healer", expires_at=soon, approved_by="Officer")
        if i:
            a.answer, a.answered_at, a.answered_by = "withdrawn", rc.now(), "Officer"
        else:
            a.channel, a.dm_failed = "web", True
        lk.fill_asks.append(a)
    rc.record_dms(lk, "open", [m.discord_id for m in mains[18:27]], [mains[27].discord_id])
    rc.record_dms(lk, "remind", [m.discord_id for m in rostered[4:6]], by="Officer")
    rc.callout(reg, rs, lk, mains[32], rc.run_team(reg, lk), "work ran late", source="dm") if len(mains) > 32 else None
    lk.dms_whole = True
    rs.save(lk, "locked (scratch board)")
    past = (now - timedelta(days=4)).replace(hour=20, minute=0, second=0, microsecond=0)  # History: its board as it was locked
    done = rc.open_run(reg, rs, barrow, past, by="scratch")
    for m in mains[8:18]:
        rc.set_signup(reg, rs, done, m, None, "in", source="officer")
    names = [m.main.label for m in mains[8:8 + size]]
    done.rosters = rc.board_rosters(reg, done, [names[i:i + g] for i in range(0, len(names), g)], size)
    done.state, done.locked_at, done.closed_at = "done", (past - timedelta(hours=2)).isoformat(), (past + timedelta(hours=3)).isoformat()
    rs.save(done, "finished (scratch board)")
    print(f"serve_scratch: seeded schedule {sid}, an open two-raid event, a locked and a finished event (owner {OWNER})")


def member_session(reg, data: Path, bot) -> None:
    """--member: admit the lowest-id seeded main who is not the owner to the scratch server (with the owner, so the
    bearer still works) and write `<data>/member_cookie.txt`, their session signed with this process's throw-away
    OIBOT_WEB_SECRET exactly as web/app.py signs one (URLSafeSerializer(secret, salt="session"), {uid, name, iat})."""
    import time
    import types

    from conftest import OWNER, MemberDirectory
    from itsdangerous import URLSafeSerializer

    m = min((m for m in reg.members.values() if not m.test and m.main and m.discord_id != reg.config.owner_discord_id),
            key=lambda m: m.discord_id, default=None)
    if m is None:
        refuse("--member: no seeded member who is not the owner.")

    def person(uid):
        return types.SimpleNamespace(id=uid, display_name=f"u{str(uid)[-2:]}", roles=[])

    MemberDirectory({(1, OWNER): person(OWNER), (1, m.discord_id): person(m.discord_id)}).install(bot, servers=(1,))
    cookie = URLSafeSerializer(os.environ["OIBOT_WEB_SECRET"], salt="session").dumps(
        {"uid": str(m.discord_id), "name": m.display_name, "iat": time.time()})
    (data / "member_cookie.txt").write_text(cookie + "\n")
    print(f"serve_scratch: member session for {m.display_name} in {data / 'member_cookie.txt'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve this worktree's site on scratch demo data (screenshots only).")
    ap.add_argument("--data", default=None, help="the scratch data folder (new, below the system temp directory)")
    ap.add_argument("--guild", default="demo")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--seed", action="store_true", help="add a schedule, minimums, an open and a locked event")
    ap.add_argument("--member", action="store_true", help="also write <data>/member_cookie.txt: a member's session (not an officer)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    if os.environ.get("DISCORD_TOKEN"):
        refuse("DISCORD_TOKEN is set: this is a real bot's environment. Run it from a plain shell.")
    if args.port in live_ports():
        refuse(f"port {args.port} is the live bot's site.")
    data = Path(args.data) if args.data else Path(tempfile.gettempdir()) / "oibot-scratch" / "data"
    why = data_refusal(data)
    if why:
        refuse(why)
    out = data.parent / f"{data.name}-out"

    no_dotenv()
    scrub_env()
    from test_mcp import TOKEN
    from test_raid_night import NightBot

    from oibot_gm import raidcycle as rc
    from oibot_gm.profiles import GameProfile
    from oibot_gm.registry import Registry
    from oibot_gm.store import GitStore
    from oibot_gm.web import app as web

    left = scrub_env()  # anything an import put back
    if left:
        refuse("an import put secrets back into the environment: " + ", ".join(sorted(left)))
    sandbox(out)
    os.environ["OIBOT_USAGE_LOG"] = str(out / "llm_usage.jsonl")
    os.environ[web.MCP_TOKEN_ENV] = TOKEN
    if args.member:  # a throw-away cookie secret, this process only (scrub_env took any real one out above)
        import secrets
        os.environ["OIBOT_WEB_SECRET"] = secrets.token_hex(16)

    build_data(data, args.guild)
    store = GitStore(data, push=False)
    reg = Registry(store, args.guild, GameProfile.load(ROOT / "profiles" / "forever"))
    import_demo(reg)
    rs = rc.RaidStore(store, args.guild)
    if args.seed:
        seed(reg, rs)
    import uvicorn

    print(f"serve_scratch: http://127.0.0.1:{args.port}/app/raids  (data {data}, guild {args.guild})")
    bot = NightBot(reg, rs)
    if args.member:
        member_session(reg, data, bot)
    # the icon cache is the scratch folder's and nothing is fetched: a missing icon is the generated badge
    app = web.create_app(bot, icon_dir=out / "icons", icon_cdn=None)
    attach_loop(app, bot)  # server-sent events: the page updates live when a second request changes something
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
