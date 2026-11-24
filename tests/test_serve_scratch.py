"""scripts/serve_scratch.py and scripts/shots.py (phase 4, Task 6): screenshots never touch the live bot — the scratch
server refuses a real bot's environment, the live port and any data folder it didn't build; it never reads .env, keeps
no secret in its environment and writes nothing outside its scratch folder; shots.py has no default site."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _scratch():
    spec = importlib.util.spec_from_file_location("serve_scratch", ROOT / "scripts" / "serve_scratch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv,env", [
    (["--data", "/tmp/x-oibot-new"], {"DISCORD_TOKEN": "real"}),
    (["--port", "8788"], {}),
    (["--data", str(ROOT.parent / "oibot_gm-data")], {}),
    (["--data", str(Path.home() / "scratch-data")], {}),
    (["--data", "/tmp"], {}),
    (["--data", tempfile.gettempdir()], {}),
])
def test_the_scratch_server_refuses_anything_real(monkeypatch, argv, env):
    mod = _scratch()
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["serve_scratch.py", *argv])
    built = []
    monkeypatch.setattr(mod, "build_data", lambda *a: built.append(a))
    monkeypatch.setattr(mod, "no_dotenv", lambda: built.append("dotenv"))
    with pytest.raises(SystemExit) as e:
        mod.main()
    assert e.value.code == 2 and not built, "refused before anything is imported or built"


def test_a_data_folder_is_new_or_one_this_script_built(tmp_path):
    mod = _scratch()
    assert mod.under_temp(tmp_path / "data") and not mod.under_temp(ROOT.parent / "oibot_gm-data")
    assert not mod.under_temp(Path("/tmp")) and not mod.under_temp(Path(tempfile.gettempdir())), "a temp root itself"
    assert mod.data_refusal(tmp_path / "new") is None
    other = tmp_path / "someone-elses"
    other.mkdir()
    (other / "notes.txt").write_text("keep me")
    assert mod.data_refusal(other) and mod.data_refusal(tmp_path), "a folder holding files this script didn't write"
    with pytest.raises(SystemExit):
        mod.build_data(other, "demo")
    assert (other / "notes.txt").read_text() == "keep me", "never deleted"
    mine = tmp_path / "mine"
    mod.build_data(mine, "demo")
    assert (mine / mod.MARKER).is_file() and (mine / "demo" / "guild.yaml").is_file()
    assert mod.data_refusal(mine) is None
    mod.build_data(mine, "demo")  # rebuilt over its own marker
    assert (mine / "demo" / "guild.yaml").is_file()


def test_no_secret_survives_and_every_output_is_scratch(tmp_path, monkeypatch):
    """A fake .env with every kind of secret, the same names in the environment: after the script's set-up the
    environment holds none of them, a later load_dotenv (what mcp_server does at import) reads nothing, the network
    read is refused and every module-level output path is under the scratch folder."""
    mod = _scratch()
    secrets = {"DISCORD_TOKEN": "d", "ANTHROPIC_API_KEY": "a", "TYPESAFE_API_KEY": "t", "BLIZZARD_CLIENT_SECRET": "b",
               "OIBOT_MCP_TOKEN": "m", "OIBOT_SUPERUSERS": "1", "OIBOT_FEED_TOKEN": "f", "WCL_CLIENT_SECRET": "w",
               "DISCORD_CLIENT_SECRET": "c", "OIBOT_WEB_SECRET": "s"}
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in secrets.items()))
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    import dotenv
    import dotenv.main

    monkeypatch.setattr(dotenv, "load_dotenv", dotenv.load_dotenv)  # restored after the test
    monkeypatch.setattr(dotenv.main, "load_dotenv", dotenv.main.load_dotenv)
    mod.no_dotenv()
    gone = mod.scrub_env()
    assert set(secrets) <= set(gone)
    dotenv.load_dotenv(env_file)
    from dotenv import load_dotenv

    load_dotenv(env_file)
    assert not [k for k in secrets if k in os.environ], "no secret in the environment"
    out = tmp_path / "out"
    mod.sandbox(out, monkeypatch.setattr)
    from oibot_gm import agents, discord_armory, discord_auctions, discord_bot, pending_views, price_reference
    guides_build = importlib.import_module("oibot_gm.guides.build")  # the package exports a build() that shadows the module
    from oibot_gm.llm import decisions
    from oibot_gm.web import api_agents

    for p in (agents.OUT, discord_armory.OUT, discord_auctions.OUT, guides_build.OUT, pending_views.PATH, discord_bot.OUT,
              discord_bot.LISTEN_LOG, api_agents.LOG_FILE):
        assert out in Path(p).parents or Path(p) == out, p
    with pytest.raises(OSError):
        price_reference.fetch_text("https://example.invalid/x.csv")
    assert agents.kickstart("gg.earlyandoften.oibot")[0] is False and decisions.DECISIONS is None


def test_the_scratch_bot_presses_through_the_raid_verbs():
    """The scratch server's bot is the suite's NightBot (the real RaidMixin verbs): a one-off Open has its verb."""
    src = (ROOT / "scripts" / "serve_scratch.py").read_text()
    assert "from test_raid_night import NightBot" in src and "NightBot(reg, rs)" in src
    from test_raid_night import NightBot

    assert hasattr(NightBot, "open_run_and_post")


def test_shots_has_no_default_site():
    src = (ROOT / "scripts" / "shots.py").read_text()
    assert 'default="http://127.0.0.1:8788"' not in src and "ap.error(" in src
    assert '"/app/readiness"' not in src and "?view=priority" in src and "?view=readiness" in src and "[data-list-item]" in src


def test_the_scratch_bot_gets_the_servers_loop_so_live_updates_reach_pages():
    """web/api's publish hands every topic to `bot.loop` (a real bot's Discord loop); the bot double has none, so the
    scratch server makes its own loop the bot's at startup — a save from a second request then reaches /api/events."""
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mod = _scratch()

    class Double:
        pass

    app, bot, seen = FastAPI(), Double(), []

    @app.get("/x")
    async def x():
        seen.append(asyncio.get_running_loop())
        return {}

    mod.attach_loop(app, bot)
    with TestClient(app) as c:
        c.get("/x")
        assert bot.loop is seen[0], "the loop requests run on is the one publish posts to"
    src = (ROOT / "scripts" / "serve_scratch.py").read_text()
    assert "attach_loop(app, bot)" in src[src.index("def main"):]


DRIVER = r'''
import importlib.util, json, os, sys
from pathlib import Path

ROOT, PLANTED, DATA, SECRETS = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4])
spec = importlib.util.spec_from_file_location("serve_scratch", ROOT / "scripts" / "serve_scratch.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert "oibot_gm" not in sys.modules, "nothing of the bot is imported before main"
import uvicorn


def fake_run(app, **kw):
    """Called where the real server would start: everything main set up is in place."""
    import dotenv
    from dotenv import load_dotenv

    dotenv.load_dotenv(PLANTED)
    load_dotenv(PLANTED)
    import httpx

    async def no_fetch(*a, **k):
        raise AssertionError("an icon was fetched from the network")

    httpx.AsyncClient.get = no_fetch
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/img/icon/spell_scratch_probe.jpg")
    print("RESULT " + json.dumps({
        "leaked": sorted(k for k, v in os.environ.items() if v in SECRETS.values()),
        "names": sorted(k for k in SECRETS if k in os.environ and k != "OIBOT_MCP_TOKEN"),
        "host": kw.get("host"), "icon": r.status_code, "icon_type": r.headers.get("content-type"),
        "checkout_icon": (ROOT / "out" / "icons" / "spell_scratch_probe.jpg").exists(),
    }))


uvicorn.run = fake_run
sys.argv = ["serve_scratch.py", "--data", DATA, "--port", "8799"]
mod.main()
'''


def test_main_in_its_real_import_order_keeps_no_secret_and_fetches_nothing(tmp_path):
    """The script's own main, in a fresh process, in its real import order (nothing of the bot imported before it runs):
    a planted .env and the same secrets in the environment; uvicorn.run is the only stub. Where the server would start,
    no planted value is in the environment (a load_dotenv of the planted file reads nothing), it binds this machine
    only, and a game icon is the generated badge: never fetched, never cached outside the scratch folder."""
    import json
    import subprocess

    secrets = {"ANTHROPIC_API_KEY": "sk-planted-a", "TYPESAFE_API_KEY": "planted-t", "BLIZZARD_CLIENT_SECRET": "planted-b",
               "OIBOT_MCP_TOKEN": "planted-m", "OIBOT_FEED_TOKEN": "planted-f", "WCL_CLIENT_SECRET": "planted-w",
               "DISCORD_CLIENT_SECRET": "planted-c", "OIBOT_WEB_SECRET": "planted-s", "OIBOT_SUPERUSERS": "4242"}
    planted = tmp_path / ".env"
    planted.write_text("DISCORD_TOKEN=planted-d\n" + "".join(f"{k}={v}\n" for k, v in secrets.items()))
    env = {k: v for k, v in os.environ.items() if k != "DISCORD_TOKEN"} | secrets
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    every = secrets | {"DISCORD_TOKEN": "planted-d"}
    r = subprocess.run([sys.executable, str(driver), str(ROOT), str(planted), str(tmp_path / "data"), json.dumps(every)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    line = [x for x in r.stdout.splitlines() if x.startswith("RESULT ")]
    assert r.returncode == 0 and line, r.stderr[-3000:]
    out = json.loads(line[0][len("RESULT "):])
    assert out["leaked"] == [] and out["names"] == [], out
    assert out["host"] == "127.0.0.1"
    assert out["icon"] == 200 and out["icon_type"] == "image/png" and not out["checkout_icon"], out
    assert list((tmp_path / "data-out" / "icons").iterdir()) == [], "the scratch cache, and nothing fetched into it"


def test_the_live_port_follows_the_bind_setting(tmp_path):
    mod = _scratch()
    assert mod.live_ports({}, []) == {mod.LIVE_PORT}
    assert 9123 in mod.live_ports({"OIBOT_WEB_BIND": "127.0.0.1:9123"}, [])
    (tmp_path / ".env").write_text("SOMETHING_SECRET=x\nexport OIBOT_WEB_BIND='0.0.0.0:9555'\n")
    assert mod.live_ports({}, [tmp_path]) == {mod.LIVE_PORT, 9555}
    assert "SOMETHING_SECRET" not in os.environ
