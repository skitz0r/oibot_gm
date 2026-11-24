"""Serve THIS worktree's site on a scratch copy of the demo fixtures, for screenshots (phase 4: never the live bot).

    uv run python scripts/serve_scratch.py [--data DIR] [--guild KEY] [--port 8799] [--seed]

What it does:
- builds a throw-away data repo under the system temp directory (or `--data`, which must be under it): the public
  `fixtures/demo` copy plus a guild.yaml (owner, officer role, timezone), `git init`, one commit, `GitStore(push=False)`
  — NEVER the real data repo (../oibot_gm-data), never a remote;
- registers the demo characters (fixtures/demo/characters.yaml, anonymized) as members through Registry.import_members;
- with `--seed`: a class minimum on Hyjal Summit, a weekly schedule with regulars, a one-off Hyjal Summit → Barrow
  Deeps ×2 three days out with 18 mains joined (an open, two-raid event), and a locked Barrow Deeps one-off;
- serves `web.create_app(tests/test_mcp.FakeBot)` with uvicorn on 127.0.0.1 — no Discord, no network. The scratch
  login is the suite's MCP bearer token (tests/test_mcp.TOKEN, the guild's owner and so an officer), honoured from this
  machine only; `scripts/shots.py http://127.0.0.1:8799 --guild demo` sends it.

It refuses to start when DISCORD_TOKEN is set (a real bot's environment) or when the data root is not under the
system temp directory, so the switch cannot be turned on for the real bot."""
from __future__ import annotations

import argparse
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

LIVE_PORT = 8788  # the live bot's site: never served from here
DEMO_IDS = 300_000_000_000_000_000  # made-up Discord ids for the demo characters (below Registry.TEST_BASE)


def refuse(why: str) -> None:
    print(f"serve_scratch: {why}", file=sys.stderr)
    raise SystemExit(2)


def under_temp(p: Path) -> bool:
    tmp = Path(tempfile.gettempdir()).resolve()
    roots = {tmp, Path("/tmp").resolve(), Path("/private/tmp").resolve(), Path("/private/var/folders").resolve()}
    rp = p.resolve()
    return any(rp == r or r in rp.parents for r in roots)


def build_data(root: Path, guild: str) -> None:
    import yaml

    from conftest import OFFICER_ROLE, OWNER

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(ROOT / "fixtures" / "demo", root / guild)
    (root / guild / "guild.yaml").write_text(yaml.safe_dump({
        "key": guild, "name": "Demo Guild", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": OWNER,
        "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE],
    }, sort_keys=False))
    git = ["git", "-c", "user.email=scratch@localhost", "-c", "user.name=scratch"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "scratch seed"], cwd=root, check=True)


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
    rs.save(lk, "locked (scratch board)")
    print(f"serve_scratch: seeded schedule {sid}, an open two-raid event and a locked event (owner {OWNER})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve this worktree's site on scratch demo data (screenshots only).")
    ap.add_argument("--data", default=None, help="the scratch data root (under the system temp directory)")
    ap.add_argument("--guild", default="demo")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--seed", action="store_true", help="add a schedule, minimums, an open and a locked event")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    if os.environ.get("DISCORD_TOKEN"):
        refuse("DISCORD_TOKEN is set: this is a real bot's environment. Run it from a plain shell.")
    if args.port == LIVE_PORT:
        refuse(f"port {LIVE_PORT} is the live bot's site.")
    data = Path(args.data) if args.data else Path(tempfile.gettempdir()) / "oibot-scratch" / "data"
    if not under_temp(data):
        refuse(f"{data} is not under the system temp directory; the scratch server never opens a real data repo.")

    from test_mcp import TOKEN, FakeBot

    from oibot_gm import raidcycle as rc
    from oibot_gm.profiles import GameProfile
    from oibot_gm.registry import Registry
    from oibot_gm.store import GitStore
    from oibot_gm.web import app as web

    build_data(data, args.guild)
    store = GitStore(data, push=False)
    reg = Registry(store, args.guild, GameProfile.load(ROOT / "profiles" / "forever"))
    import_demo(reg)
    rs = rc.RaidStore(store, args.guild)
    if args.seed:
        seed(reg, rs)
    os.environ[web.MCP_TOKEN_ENV] = TOKEN
    os.environ.pop("OIBOT_WEB_DEV", None)
    os.environ.pop("OIBOT_WEB_URL", None)
    import uvicorn

    print(f"serve_scratch: http://127.0.0.1:{args.port}/app/raids  (data {data}, guild {args.guild})")
    uvicorn.run(web.create_app(FakeBot(reg, rs)), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
