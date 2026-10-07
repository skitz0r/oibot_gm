"""Move the bot's guild to another Discord server.

Everything about the guild is keyed by Discord USER ids, which are the same in every server, so members, characters,
regulars, absences, applications, schedules, auras, policy and history all carry over. What is tied to the old server
is cleared here and set again in the new one (Config page or /gm config): the server id, every channel and the card
messages in them, officer/member roles, the invite link, and per-channel plain-text permissions.

    # 1. the invite link for the new server (same bot application; minimal permissions)
    uv run python scripts/move_guild.py --invite-link [--guild <new server id>]

    # 2. with the bot STOPPED (launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot):
    uv run python scripts/move_guild.py --to <new server id> --dry-run
    uv run python scripts/move_guild.py --to <new server id> [--push]

    # 3. start the bot (launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/gg.earlyandoften.oibot.plist),
    #    then set channels / officer role / member roles / news channel / invite in the new server.

Several guilds (design §5.32): the data repo holds one folder per guild and the bot serves them all; this moves ONE
guild (`--key`, a key or a unique name; required when there are several) and keeps its key. A new server is normally
claimed as a guild of its own with `/gm config owner` instead — this script is for an actual move. Stopping the bot
stops every guild for the duration.

Refuses: a server id that isn't a Discord snowflake, the same server again, a server another guild already uses, the
in-repo fixtures as the data root, a bot that is still running (its in-memory config would write the old ids back:
the web bind answers, or launchd shows a pid), and open or locked runs (their sheets live in the old server — cancel or
finish them first). --force skips the last two checks. Also cleared: the review-card message ids of applications and
the card ids of news proposals (those messages stay in the old server). DISCORD_TEST_GUILD_ID in .env follows only
when it was the old server."""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlencode

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SNOWFLAKE = re.compile(r"^\d{17,20}$")
CLEARED_SCALARS = ("ops_channel_id", "applications_channel_id", "signup_channel_id", "roster_channel_id", "registration_channel_id",
                   "registration_message_id", "analytics_channel_id", "absences_channel_id", "absences_message_id", "news_channel_id",
                   "invite_url")
CLEARED_LISTS = ("officer_role_ids", "officer_roles", "member_role_ids")


def permissions_value() -> int:
    """What the bot's code uses (checked against the source): post and edit cards and sheets, pin cards, threads under
    officer cards, its own app emojis, invites for the homepage's Join button. No Manage Roles: the registration
    channel's read-only overwrite is optional (the bot says so; deny Send Messages for @everyone there yourself)."""
    import discord

    return discord.Permissions(view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True,
                               manage_messages=True, create_public_threads=True, send_messages_in_threads=True,
                               use_external_emojis=True, create_instant_invite=True).value


def invite_link(client_id: str, guild: str | None = None) -> str:
    q = {"client_id": client_id, "scope": "bot applications.commands", "permissions": str(permissions_value())}
    if guild:
        q.update(guild_id=guild, disable_guild_select="true")
    return "https://discord.com/oauth2/authorize?" + urlencode(q)


def env_value(env: Path, key: str) -> str | None:
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == key:
            return v.split("#", 1)[0].strip().strip('"').strip("'") or None
    return None


def set_env_value(env: Path, key: str, value: str) -> bool:
    """Replace (or append) KEY=value in .env without printing anything else from the file."""
    lines = env.read_text().splitlines() if env.exists() else []
    out, done = [], False
    for line in lines:
        k, sep, _ = line.partition("=")
        if sep and k.strip() == key and not line.lstrip().startswith("#"):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    env.write_text("\n".join(out) + "\n")
    return True


def load_guilds(data: Path):
    """The guilds the bot would load from `data` (discord_registry.Guilds, the real loader; no pushes)."""
    from oibot_gm.discord_registry import Guilds
    from oibot_gm.store import GitStore

    return Guilds(GitStore(data, push=False), ROOT)


def pick_guild(guilds, key: str | None):
    """The guild to move: `key` (a key, or a unique name — Guilds.get), else the only guild."""
    if key:
        reg = guilds.get(key)
        if reg is None:
            raise SystemExit(f"no guild {key!r}; the guilds: {', '.join(r.key for r in guilds.ordered()) or 'none'}")
        return reg
    reg = guilds.only()
    if reg is None:
        raise SystemExit(f"several guilds: pick one with --key ({', '.join(r.key for r in guilds.ordered())})" if guilds.ordered() else "no guild to move")
    return reg


def guild_dir(data: Path, key: str | None) -> Path:
    return data / pick_guild(load_guilds(data), key).key


def live_runs(gdir: Path) -> list[str]:
    out = []
    for f in sorted((gdir / "raids").glob("*.json")) if (gdir / "raids").exists() else []:
        try:
            state = json.loads(f.read_text()).get("state")
        except (OSError, ValueError):
            continue
        if state in ("open", "locked"):
            out.append(f"{f.stem} ({state})")
    return out


def bot_running(bind: str | None, launchd=None) -> bool:
    """The web bind answers, or launchd shows the bot's LaunchAgent with a pid (`launchd`: agents.launchd_status, for tests)."""
    host, _, port = (bind or "127.0.0.1:8788").rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        pass
    from oibot_gm import agents

    return bool((launchd or agents.launchd_status)(agents.BOT_LABEL).get("pid"))


def plan_side_files(gdir: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Ids left in the old server outside guild.yaml: an application's review-card message (applicants/*.json
    `message_id`) and a news proposal's card (proposals/*.yaml `channel_id`/`message_id`). Returns ([(path relative to
    the data root, new text)], lines)."""
    writes, lines = [], []
    n_apps = n_props = 0
    for f in sorted((gdir / "applicants").glob("*.json")) if (gdir / "applicants").exists() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("message_id"):
            d["message_id"] = None
            writes.append((f"{gdir.name}/applicants/{f.name}", json.dumps(d, indent=1, ensure_ascii=False)))
            n_apps += 1
    for f in sorted((gdir / "proposals").glob("*.yaml")) if (gdir / "proposals").exists() else []:
        try:
            d = yaml.safe_load(f.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if d.get("channel_id") or d.get("message_id"):
            d["channel_id"], d["message_id"] = None, None
            writes.append((f"{gdir.name}/proposals/{f.name}", yaml.safe_dump(d, sort_keys=False, allow_unicode=True)))
            n_props += 1
    if n_apps:
        lines.append(f"applicants: {n_apps} review-card message id(s) cleared (the cards stay in the old server)")
    if n_props:
        lines.append(f"proposals: {n_props} news-proposal card id(s) cleared (a card is posted again on its next change)")
    return writes, lines


def plan_move(cfg: dict, new_id: int) -> tuple[dict, list[str]]:
    """The new guild.yaml contents and one line per change (ids are not printed, only what happens to them)."""
    new = dict(cfg)
    lines = ["discord_guild_id: old server → new server"]
    new["discord_guild_id"] = new_id
    for k in CLEARED_SCALARS:
        if new.get(k) not in (None, ""):
            lines.append(f"{k}: cleared (set it again in the new server)")
            new[k] = None
    if new.get("analytics_message_ids"):
        lines.append(f"analytics_message_ids: {len(new['analytics_message_ids'])} card(s) forgotten")
        new["analytics_message_ids"] = {}
    for k in CLEARED_LISTS:
        if new.get(k):
            lines.append(f"{k}: {len(new[k])} cleared")
            new[k] = []
    plain = dict(new.get("plain") or {})
    if plain.get("channels"):
        lines.append(f"plain.channels: {len(plain['channels'])} per-channel mode(s) cleared (DMs and the default stay)")
        plain["channels"] = {}
    groups = {g: [w for w in who if not str(w).startswith("role:")] for g, who in (plain.get("groups") or {}).items()}
    dropped = sum(len(who) for who in (plain.get("groups") or {}).values()) - sum(len(w) for w in groups.values())
    if dropped:
        lines.append(f"plain.groups: {dropped} Discord role grant(s) cleared (tiers kept)")
    if plain:
        plain["groups"] = groups
        new["plain"] = plain
    return new, lines


def main(argv: list[str] | None = None) -> int:
    from oibot_gm.registry import GuildConfig
    from oibot_gm.store import GitStore, is_fixture_root, resolve_data_root

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--invite-link", action="store_true", help="print the OAuth2 invite link for the bot")
    ap.add_argument("--guild", help="with --invite-link: preselect this server")
    ap.add_argument("--to", help="the new server id")
    ap.add_argument("--data", type=Path, help="the data repo (default: the bot's)")
    ap.add_argument("--key", help="the guild key or name (default: the only guild; required when there are several)")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true", help="push the data repo after the commit")
    ap.add_argument("--force", action="store_true", help="skip the bot-running and live-run checks")
    a = ap.parse_args(argv)

    if a.invite_link:
        cid = env_value(a.env, "DISCORD_CLIENT_ID")
        if not cid:
            raise SystemExit(f"DISCORD_CLIENT_ID is not in {a.env}: it is the application id (Developer Portal → your app → General Information)")
        if a.guild and not SNOWFLAKE.match(a.guild):
            raise SystemExit("--guild must be a Discord server id (17–20 digits)")
        print(invite_link(cid, a.guild))
        return 0

    if not a.to:
        ap.error("--to <new server id> or --invite-link")
    if not SNOWFLAKE.match(a.to):
        raise SystemExit("--to must be a Discord server id (17–20 digits): Developer Mode on, right-click the server icon → Copy Server ID")
    data = (a.data or resolve_data_root(ROOT)).resolve()
    if is_fixture_root(ROOT, data):
        raise SystemExit(f"{data} is this repo's fixtures (demo data), not a guild's data repo — pass --data or set OIBOT_DATA_DIR")
    guilds = load_guilds(data)
    reg = pick_guild(guilds, a.key)
    gdir = data / reg.key
    cfg = yaml.safe_load((gdir / "guild.yaml").read_text()) or {}
    old_id = str(cfg.get("discord_guild_id") or "")
    if old_id == a.to:
        raise SystemExit("that is already the guild's server — nothing to move")
    other = guilds.for_guild(int(a.to))
    if other is not None and other.key != reg.key:
        raise SystemExit(f"that server is already the guild {other.key} ({other.config.name}) — one server, one guild")
    if not a.force:
        if bot_running(env_value(a.env, "OIBOT_WEB_BIND")):
            raise SystemExit("the bot is running — stop it first (launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot), or its config would write the old ids back")
        live = live_runs(gdir)
        if live:
            raise SystemExit(f"open or locked runs live in the old server: {', '.join(live)} — cancel or finish them first (or --force)")

    new, lines = plan_move(cfg, int(a.to))
    GuildConfig.model_validate(new)  # the bot must load what is written
    side, side_lines = plan_side_files(gdir)
    print(f"{gdir.name}: move to server {a.to}")
    for ln in lines + side_lines:
        print("  " + ln)
    print("  kept: members, characters, regulars, absences, applications, schedules, raids, auras, policy, history")
    if a.dry_run:
        print("dry run — nothing written")
        return 0
    store = GitStore(data, push=a.push)
    store.write_text(Path(gdir.name) / "guild.yaml", "# oibot_GM guild config — edit via /gm config or by PR.\n" + yaml.safe_dump(new, sort_keys=False, allow_unicode=True))
    for rel, text in side:
        store.write_text(Path(rel), text)
    store.commit(f"{gdir.name}: moved to Discord server {a.to} (scripts/move_guild.py)")
    if a.push:
        store.flush()
    test_id = env_value(a.env, "DISCORD_TEST_GUILD_ID")
    if test_id and test_id == old_id:
        set_env_value(a.env, "DISCORD_TEST_GUILD_ID", a.to)
        print(f"committed; {a.env.name}: DISCORD_TEST_GUILD_ID followed the guild to the new server")
    else:
        print(f"committed; {a.env.name}: DISCORD_TEST_GUILD_ID left alone ({'not set' if not test_id else 'it is another server'})")
    print("next: start the bot, then in the new server set the channels, officer role(s), member roles, news channel and invite "
          "(Config page or /gm config); members join the new server to log in and get DMs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
