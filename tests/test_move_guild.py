"""scripts/move_guild.py (design §5.32, D7): the tool for an actual move of one guild to another Discord server and for
the invite link. Everything runs on a scratch data repo (GitStore push off) and a scratch .env — never the real ones."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from conftest import GUILD, GUILD2, ROOT, add_duo

from oibot_gm.registry import GuildConfig

NEW = "123456789012345678"  # a Discord snowflake (18 digits)
OTHER = "223456789012345678"


def load():
    spec = importlib.util.spec_from_file_location("move_guild", ROOT / "scripts" / "move_guild.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mg(monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "bot_running", lambda bind, launchd=None: False)
    return mod


def write_yaml(path: Path, **changes) -> None:
    cfg = yaml.safe_load(path.read_text())
    cfg.update(changes)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def commit(root: Path, msg: str = "t") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg], cwd=root, check=True)


def env_file(tmp_path: Path, test_id: str | None = "1") -> Path:
    env = tmp_path / "scratch.env"
    lines = ["# a comment line", 'DISCORD_CLIENT_ID="999888777666555444"  # the app', "OTHER=keep me"]
    if test_id:
        lines.append(f"DISCORD_TEST_GUILD_ID={test_id}")
    env.write_text("\n".join(lines) + "\n")
    return env


# ---------------------------------------------------------------- pure pieces

def test_plan_move_clears_what_belongs_to_the_old_server_and_keeps_the_rest(mg):
    cfg = {"key": "oi", "name": "Oi", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": 5, "timezone": "UTC",
           "ops_channel_id": 10, "registration_message_id": 11, "invite_url": "https://discord.gg/x", "analytics_message_ids": {"bd": 12},
           "officer_role_ids": [20, 21], "member_role_ids": [22], "rosters": [{"key": "core"}], "raids": {"barrow_deeps": {"lockout_days": 7}},
           "plain": {"channels": {"10": "act"}, "groups": {"self": ["everyone", "role:22"], "officer": ["role:20"]}, "dm": "self"}}
    new, lines = mg.plan_move(cfg, int(NEW))
    assert new["discord_guild_id"] == int(NEW) and new["ops_channel_id"] is None and new["invite_url"] is None and new["registration_message_id"] is None
    assert new["officer_role_ids"] == [] and new["member_role_ids"] == [] and new["analytics_message_ids"] == {}
    assert new["plain"] == {"channels": {}, "groups": {"self": ["everyone"], "officer": []}, "dm": "self"}, "role grants dropped, tiers kept"
    assert new["owner_discord_id"] == 5 and new["rosters"] == cfg["rosters"] and new["raids"] == cfg["raids"] and new["key"] == "oi"
    assert cfg["discord_guild_id"] == 1, "the input is not changed"
    text = "\n".join(lines)
    assert "plain.groups: 2 Discord role grant(s) cleared" in text and "officer_role_ids: 2 cleared" in text and NEW not in text, "ids are not printed"
    GuildConfig.model_validate(new)


def test_side_files_clear_application_and_proposal_card_ids(mg, tmp_path):
    g = tmp_path / "oi"
    (g / "applicants").mkdir(parents=True)
    (g / "proposals").mkdir()
    (g / "applicants" / "1.json").write_text(json.dumps({"discord_id": 1, "message_id": 55, "name": "A"}))
    (g / "applicants" / "2.json").write_text(json.dumps({"discord_id": 2, "message_id": None}))
    (g / "proposals" / "260101-abcd.yaml").write_text(yaml.safe_dump({"id": "260101-abcd", "channel_id": 7, "message_id": 8, "title": "t"}))
    writes, lines = mg.plan_side_files(g)
    assert [p for p, _ in writes] == ["oi/applicants/1.json", "oi/proposals/260101-abcd.yaml"]
    assert json.loads(writes[0][1]) == {"discord_id": 1, "message_id": None, "name": "A"}
    assert yaml.safe_load(writes[1][1]) == {"id": "260101-abcd", "channel_id": None, "message_id": None, "title": "t"}
    assert any("1 review-card" in ln for ln in lines) and any("1 news-proposal" in ln for ln in lines)


def test_env_helpers_touch_only_their_line(mg, tmp_path):
    env = env_file(tmp_path)
    assert mg.env_value(env, "DISCORD_CLIENT_ID") == "999888777666555444" and mg.env_value(env, "MISSING") is None
    mg.set_env_value(env, "DISCORD_TEST_GUILD_ID", NEW)
    mg.set_env_value(env, "NEW_KEY", "v")
    assert env.read_text().splitlines() == ["# a comment line", 'DISCORD_CLIENT_ID="999888777666555444"  # the app', "OTHER=keep me",
                                            f"DISCORD_TEST_GUILD_ID={NEW}", "NEW_KEY=v"]
    assert mg.env_value(tmp_path / "none.env", "X") is None


def test_invite_link(mg):
    import discord

    q = parse_qs(urlparse(mg.invite_link("42", NEW)).query)
    perms = discord.Permissions(int(q["permissions"][0]))
    assert q["client_id"] == ["42"] and q["scope"] == ["bot applications.commands"] and q["guild_id"] == [NEW] and q["disable_guild_select"] == ["true"]
    assert perms.send_messages and perms.create_instant_invite and perms.manage_messages and not perms.administrator and not perms.manage_roles
    assert "guild_id" not in parse_qs(urlparse(mg.invite_link("42")).query)


def test_invite_link_command(mg, tmp_path, capsys):
    env = env_file(tmp_path)
    assert mg.main(["--invite-link", "--env", str(env)]) == 0 and "client_id=999888777666555444" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="server id"):
        mg.main(["--invite-link", "--guild", "abc", "--env", str(env)])
    with pytest.raises(SystemExit, match="DISCORD_CLIENT_ID is not in"):
        mg.main(["--invite-link", "--env", str(tmp_path / "missing.env")])


def test_picking_the_guild(mg, data_root, reg):
    assert mg.guild_dir(data_root, None) == data_root / GUILD, "one guild: it"
    add_duo(data_root)
    with pytest.raises(SystemExit, match="several guilds: pick one with --key"):
        mg.guild_dir(data_root, None)
    assert mg.guild_dir(data_root, GUILD2) == data_root / GUILD2 and mg.guild_dir(data_root, "duo guild") == data_root / GUILD2, "a key or a unique name"
    with pytest.raises(SystemExit, match="no guild 'nope'"):
        mg.guild_dir(data_root, "nope")


def test_live_runs(mg, data_root, reg, rs):
    from conftest import open_test_run

    assert mg.live_runs(data_root / GUILD) == []
    ev = open_test_run(reg, rs)
    assert mg.live_runs(data_root / GUILD) == [f"{ev.key} (open)"]
    ev.state = "cancelled"
    rs.save(ev, "t")
    assert mg.live_runs(data_root / GUILD) == []


def test_bot_running_also_asks_launchd():
    mod = load()
    assert mod.bot_running("127.0.0.1:1", launchd=lambda label: {"pid": 4321}) is True
    assert mod.bot_running("127.0.0.1:1", launchd=lambda label: {"pid": None}) is False


# ---------------------------------------------------------------- refusals

def test_every_refusal(mg, data_root, reg, rs, tmp_path, monkeypatch):
    env = env_file(tmp_path)
    args = ["--data", str(data_root), "--env", str(env)]
    with pytest.raises(SystemExit, match="must be a Discord server id"):
        mg.main(["--to", "12345", *args])
    write_yaml(data_root / GUILD / "guild.yaml", discord_guild_id=int(NEW))
    commit(data_root)
    with pytest.raises(SystemExit, match="already the guild's server"):
        mg.main(["--to", NEW, *args])
    add_duo(data_root)
    write_yaml(data_root / GUILD2 / "guild.yaml", discord_guild_id=int(OTHER))
    commit(data_root)
    with pytest.raises(SystemExit, match="already the guild duo"):
        mg.main(["--to", OTHER, "--key", GUILD, *args])
    from conftest import open_test_run

    open_test_run(reg, rs)
    with pytest.raises(SystemExit, match="open or locked runs"):
        mg.main(["--to", "323456789012345678", "--key", GUILD, *args])
    monkeypatch.setattr(mg, "bot_running", lambda bind, launchd=None: True)
    with pytest.raises(SystemExit, match="the bot is running"):
        mg.main(["--to", "323456789012345678", "--key", GUILD, *args])
    with pytest.raises(SystemExit, match="fixtures"):
        mg.main(["--to", "323456789012345678", "--data", str(ROOT / "fixtures"), "--env", str(env)])


# ---------------------------------------------------------------- a dry run and a real one

def test_dry_run_writes_nothing_and_a_real_run_commits_a_valid_config(mg, data_root, reg, tmp_path, capsys):
    reg.config.ops_channel_id, reg.config.officer_role_ids = 10, [20]
    reg.save_config("t")
    a = reg.apply(77, "Newbie", "Kessa", *next((c, s) for c, specs in reg.profile.classes.items() for s in specs), None, None, None, None)
    a.message_id = 999
    reg.save_applicant(a, "card posted")
    env = env_file(tmp_path, test_id="1")  # the test server is the guild's server
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=data_root, capture_output=True, text=True).stdout
    assert mg.main(["--to", NEW, "--data", str(data_root), "--env", str(env), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run — nothing written" in out and "ops_channel_id: cleared" in out and "1 review-card" in out
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=data_root, capture_output=True, text=True).stdout == head
    assert mg.main(["--to", NEW, "--data", str(data_root), "--env", str(env)]) == 0
    out = capsys.readouterr().out
    cfg = GuildConfig.model_validate(yaml.safe_load((data_root / GUILD / "guild.yaml").read_text()))
    assert cfg.discord_guild_id == int(NEW) and cfg.ops_channel_id is None and cfg.officer_role_ids == [] and cfg.key == GUILD
    assert json.loads((data_root / GUILD / "applicants" / "77.json").read_text())["message_id"] is None
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=data_root, capture_output=True, text=True).stdout
    assert log.strip() == f"{GUILD}: moved to Discord server {NEW} (scripts/move_guild.py)"
    assert mg.env_value(env, "DISCORD_TEST_GUILD_ID") == NEW and "followed the guild" in out
    from oibot_gm.discord_registry import Guilds
    from oibot_gm.store import GitStore

    g = Guilds(GitStore(data_root, push=False), ROOT)
    assert not g.load_errors and g.for_guild(int(NEW)).key == GUILD and len(g.by_key[GUILD].members) == len(reg.members), "members carried over"


def test_the_test_server_id_stays_when_it_is_another_server(mg, data_root, reg, tmp_path, capsys):
    env = env_file(tmp_path, test_id="555555555555555555")
    assert mg.main(["--to", NEW, "--data", str(data_root), "--env", str(env)]) == 0
    assert mg.env_value(env, "DISCORD_TEST_GUILD_ID") == "555555555555555555" and "left alone" in capsys.readouterr().out
