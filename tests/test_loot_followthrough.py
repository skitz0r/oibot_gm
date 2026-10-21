"""Loot follow-through (dual interfaces phase 3, G4 — design §3.5 L12, §3.1 Items page, D12, D53):

- kills drive undistributed() (killed bosses first) and, when the raid's last boss is down, ONE "All bosses down — End
  the run?" prompt: a thread line once and the lock card's "Bosses N / M" + the End button (the decide screen, then the
  ONE end_run verb — never a second meaning for "end");
- the companion ships epoch seconds (the thread line is <t:unix:t>); a kill stamped with a clock time only is refused
  by feed.py with one "companion too old" warn per companion;
- Loot › Items: Claude's tier DRAFT is a proposal (nothing written until an officer saves tiers for that item), the
  CSV export / import of the grid (preview, dedupe, unknown items, bad cells, the rev, the re-weight ask), the
  companions card (token variable NAMES, never values), attendance routes, MCP draft_tiers / export_tiers /
  get_member's attendance."""
from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import json
import types
from datetime import timedelta
from pathlib import Path

import discord
import httpx
import pytest
from conftest import GUILD, OWNER
from test_loot_verbs import LootBot, real_run, run, tick_one, tiered_item
from test_loot_web import OFFICER, H, as_user, env, opened, tick, w  # noqa: F401 — fixtures
from wizard_harness import Harness

from oibot_gm import feed as feed_mod
from oibot_gm import mcp_server as ms
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.discord_feed import _log_clock
from oibot_gm.loot import session as ls
from oibot_gm.loot import tierdraft
from oibot_gm.loot.session import Drop, LootSession
from oibot_gm.loot_ops import guild_ctx
from oibot_gm.raid_buttons import RunButton
from oibot_gm.registry import RegistryError, item_key
from oibot_gm.web import api_loot

BOSSES = ["Gatekeeper", "Warden of Tests"]
ACTOR = H | {"X-Oibot-Actor": str(OFFICER)}


@pytest.fixture(autouse=True)
def _no_drafts_between_tests():
    api_loot.DRAFTS.clear()
    yield
    api_loot.DRAFTS.clear()


@pytest.fixture
def bosses(monkeypatch):
    """The raid's boss list (the Forever profile lists none before unlock)."""
    monkeypatch.setattr(ls, "raid_bosses", lambda reg, instance: list(BOSSES))
    return BOSSES


def ico(*_a, **_k):
    return ""


def text_of(view) -> str:
    return "\n".join(str(c.content) for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def end_button(view):
    hit = next(c for c in view.walk_children() if getattr(c, "custom_id", "") and c.custom_id.endswith(":end"))
    return getattr(hit, "item", hit)  # a DynamicItem wraps its Button


# ---------------------------------------------------------------- L12: kills order undistributed()

def test_killed_bosses_come_first_in_undistributed():
    s = LootSession(id="r", run_key="r", instance="barrow_deeps", date="2026-12-09", guild=GUILD, origin="raid")
    s.drop_list = [Drop(drop_id="d1", item_name="Late Ring", boss="Warden of Tests"), Drop(drop_id="d2", item_name="Early Robe", boss="Gatekeeper"),
                   Drop(drop_id="d3", item_name="Odd Shard", boss="?"), Drop(drop_id="d4", item_name="Late Cloak", boss="Warden of Tests")]
    assert [d.drop_id for d in s.undistributed()] == ["d1", "d4", "d2", "d3"], "no kills yet: tick order, grouped by boss"
    s.bosses_done = ["Gatekeeper"]
    assert [d.drop_id for d in s.undistributed()] == ["d2", "d1", "d4", "d3"], "a killed boss's drops first"
    s.bosses_done = ["Gatekeeper", "Warden of Tests"]
    assert [d.drop_id for d in s.undistributed()][:3] == ["d2", "d1", "d4"]


def test_boss_progress_counts_only_listed_bosses():
    assert ls.boss_progress(["Gatekeeper", "Somebody Else"], BOSSES) == {"done": 1, "total": 2, "all_down": False}
    assert ls.boss_progress(["gatekeeper", "Warden of Tests"], BOSSES)["all_down"]
    assert ls.boss_progress(["A", "B"], []) == {"done": 2, "total": None, "all_down": False}, "no list: no denominator, never all down"


# ---------------------------------------------------------------- the end prompt, once

def kill(c, boss, ts=1_765_000_000):
    return run(c.bot._feed_kill(c.reg, c.s, {"type": "kill", "boss": boss, "ts": ts}))


@pytest.fixture
def council(reg, rs):
    ev, members = real_run(reg, rs)
    bot = LootBot(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=1))
    return types.SimpleNamespace(bot=bot, reg=reg, rs=rs, ev=ev, s=s, members=members)


def test_all_bosses_down_prompts_once_and_the_kill_line_is_a_native_stamp(council, bosses):
    c = council
    kill(c, "Gatekeeper")
    assert ("kill", "☠ **Gatekeeper** down (<t:1765000000:t>)") in c.bot.posts, "epoch seconds → a native Discord stamp"
    assert not [p for p in c.bot.posts if p[0] == "all_down"]
    kill(c, "Gatekeeper")  # a second companion's report of the same kill
    kill(c, "Warden of Tests")
    prompts = [p for p in c.bot.posts if p[0] == "all_down"]
    assert prompts == [("all_down", c.bot.ALL_DOWN_LINE)], "the kill that completes the set prompts — once"
    kill(c, "Warden of Tests")
    kill(c, "A Trash Pack")  # not on the raid's list: never completes anything
    assert len([p for p in c.bot.posts if p[0] == "all_down"]) == 1
    assert c.s.bosses_done[:2] == ["Gatekeeper", "Warden of Tests"]


def test_the_lock_card_shows_bosses_and_the_end_prompt(council, bosses):
    c = council
    card = views.lock_layout(c.reg, c.ev, rc.run_team(c.reg, c.ev), ico, 0, c.rs)
    assert "☠ Bosses 0 / 2" in text_of(card) and "All bosses down" not in text_of(card)
    assert end_button(card).label == "End the run now"
    kill(c, "Gatekeeper")
    assert "☠ Bosses 1 / 2" in text_of(views.lock_layout(c.reg, c.ev, rc.run_team(c.reg, c.ev), ico, 0, c.rs))
    kill(c, "Warden of Tests")
    card = views.lock_layout(c.reg, c.ev, rc.run_team(c.reg, c.ev), ico, 0, c.rs)
    text = text_of(card)
    assert "☠ Bosses 2 / 2" in text and "**All bosses down — End the run?**" in text
    btn = end_button(card)
    assert btn.label == "End the run" and not btn.disabled and btn.custom_id == f"runact.{c.reg.key}:{c.ev.key}:end"
    assert "attendance" not in text.lower() and "factor" not in text.lower(), "numbers and icons on Discord — no factor prose"


def test_no_council_no_boss_line(reg, rs, bosses):
    ev, _ = real_run(reg, rs)
    assert "Bosses" not in text_of(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs))


class EndBot(LootBot):
    """LootBot with what the RunButton press needs from the client (officer check, the guild index)."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.registries.for_interaction = lambda i: reg
        self.calls: list = []

    async def is_officer_anywhere(self, reg, uid):
        return True

    async def refresh_cards(self, reg, ev):
        self.calls.append(("refresh_cards", ev.key))

    async def refresh_sheet(self, reg, ev):
        return None


def test_the_end_prompt_press_goes_through_the_decide_screen_to_end_run(reg, rs, bosses):
    """Harness: the lock card's End the run (shown once all bosses are down) → the decide screen → End the run now →
    LootMixin.end_run (the council archived, the run done)."""
    ev, _ = real_run(reg, rs)
    ev.starts_at = (reg.now_local() - timedelta(hours=2)).replace(second=0, microsecond=0).isoformat()
    rs.save(ev, "t: tonight")
    bot = EndBot(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=1))
    for b in BOSSES:
        run(bot._feed_kill(reg, s, {"type": "kill", "boss": b, "ts": 1_765_000_000}))
    assert ("refresh_cards", ev.key) in bot.calls, "the last kill redraws the lock card"
    card = views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs)
    assert end_button(card).label == "End the run"
    h = Harness(reg, rs, client=bot, user=types.SimpleNamespace(id=424242, display_name="Tester", mention="@Tester"))

    async def go():
        await RunButton(reg.key, ev.key, "end").callback(h.card_press())
        assert h.buttons() == ["End the run now", "Back"] and "End " in h.text
        await h.press("End the run now")
    run(go())
    assert rs.events[ev.key].state == "done"
    assert (GUILD, ev.key) not in bot.events, "the council ended with the run (one verb)"


# ---------------------------------------------------------------- L12: stamps (companion + feed.py)

def companion():
    spec = importlib.util.spec_from_file_location("oibot_companion_g4", Path(__file__).resolve().parents[1] / "companion" / "oibot_companion.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_companion_ships_epoch_seconds():
    mod = companion()
    ev = mod.parse_line("12/9 20:15:33.120  [Raid Warning] Jon: Gatekeeper has been defeated!", year=2026)
    assert ev["type"] == "kill" and isinstance(ev["ts"], float)
    from datetime import datetime

    assert ev["ts"] == round(datetime(2026, 12, 9, 20, 15, 33, 120000).timestamp(), 3), "the PC's own clock and zone"
    assert feed_mod.ts_kind(ev["ts"]) == "epoch" and feed_mod.refuse_event(ev) is None
    assert _log_clock(ev["ts"]) == f"<t:{int(ev['ts'])}:t>"


@pytest.mark.parametrize("ts,kind", [(1765000000, "epoch"), ("1765000000.5", "epoch"), ("20:15", "clock"), ("20:15:33", "clock"),
                                     ("2026-12-09T20:15:33", "clock"), ("2026-12-09T20:15:33-08:00", "zoned"), (None, None)])
def test_ts_kind(ts, kind):
    assert feed_mod.ts_kind(ts) == kind


def test_a_clock_only_kill_is_refused_with_one_warn():
    """A real FeedServer on a loopback port: an old companion's HH:MM kill gets an error frame (not an ack), the ops
    warn goes out once however often it re-sends; a drop with the same stamp and an epoch kill go through."""
    seen: list = []
    warns: list = []

    async def handler(ev, comp):
        seen.append(ev["type"])
        return {"routes_to": "r"} if ev["type"] == "hello" else {"ok": True}

    async def go():
        fs = feed_mod.FeedServer(handler, "tok", "127.0.0.1:0")

        async def warn(comp, text):
            warns.append((comp.character, text))
        fs.on_warn = warn
        await fs.start()
        port = fs._runner.addresses[0][1]
        import aiohttp

        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(f"http://127.0.0.1:{port}/feed") as ws:
                    await ws.send_json({"type": "hello", "token": "tok", "character": "Rhozy", "client": "oibot-companion/0.1"})
                    assert (await ws.receive_json())["type"] == "welcome"
                    for i in range(3):
                        await ws.send_json({"type": "kill", "boss": "Gatekeeper", "ts": "20:15", "idem": "k1"})
                        r = await ws.receive_json()
                        assert r["type"] == "error" and "companion too old" in r["message"] and r["idem"] == "k1"
                    await ws.send_json({"type": "drop", "boss": "Gatekeeper", "items": [{"name": "Robe"}], "ts": "20:15", "idem": "d1"})
                    assert (await ws.receive_json())["type"] == "ack", "only kills are refused for a clock-only stamp"
                    await ws.send_json({"type": "kill", "boss": "Gatekeeper", "ts": 1765000000, "idem": "k2"})
                    assert (await ws.receive_json())["type"] == "ack"
        finally:
            await fs.stop()
    asyncio.run(go())
    assert seen == ["hello", "drop", "kill"], "the refused kill never reached the bot's handler"
    assert len(warns) == 1 and warns[0][0] == "Rhozy" and "too old" in warns[0][1]


def test_ops_warner_goes_to_the_companions_guild_ops(reg):
    lines: list = []

    class Ops:
        async def emit(self, cfg, level, text, exc=None, dm=True):
            lines.append((cfg.key, level, text))
    bot = types.SimpleNamespace(companion_guild=lambda comp: reg.key, registries=types.SimpleNamespace(cfg_for=lambda k: reg.config), ops=Ops())
    asyncio.run(feed_mod.ops_warner(bot)(types.SimpleNamespace(character="Rhozy"), "companion Rhozy is too old"))
    assert lines == [(reg.key, "warn", "companion Rhozy is too old")]


def test_token_state_names_variables_never_values():
    env = {"OIBOT_FEED_TOKEN": "shared-secret", "OIBOT_FEED_TOKEN_DEMO": "guild-secret"}
    st = feed_mod.token_state("demo", many=False, env=env)
    assert st == {"shared": True, "shared_feeds": True, "var": "OIBOT_FEED_TOKEN_DEMO", "set": True, "listener": True, "bind_set": False}
    assert "secret" not in json.dumps(st)
    assert feed_mod.token_state("demo", many=True, env={"OIBOT_FEED_TOKEN": "x"})["shared_feeds"] is False, "several guilds: the shared token feeds none"


# ---------------------------------------------------------------- web: the companions card

def test_companions_card_shows_names_not_values(w, monkeypatch):
    monkeypatch.setenv("OIBOT_FEED_TOKEN_DEMO", "very-secret-value")
    monkeypatch.delenv("OIBOT_FEED_TOKEN", raising=False)
    r = w.c.get("/api/loot/companions", headers=H)  # the MCP token acts as the owner
    assert r.status_code == 200
    d = r.json()
    assert d["tokens"]["var"] == "OIBOT_FEED_TOKEN_DEMO" and d["tokens"]["set"] and not d["tokens"]["shared"]
    assert "very-secret-value" not in r.text and d["companions"] == []
    off = w.c.get("/api/loot/companions", headers=as_user(w.c, OFFICER)).json()
    assert off["tokens"] is None and not off["owner"], "officers see presence; which tokens are set is the owner's"
    assert w.c.get("/api/loot/companions", headers=as_user(w.c, w.ms[2].discord_id)).status_code == 403


# ---------------------------------------------------------------- web: Bosses N / M on Tonight

def test_tonight_carries_bosses_progress(w, bosses):
    d = opened(w)
    c = d["state"]["councils"][0]
    assert c["bosses_progress"] == {"done": 0, "total": 2, "all_down": False}
    s = w.bot.events[(GUILD, w.ev.key)]
    run(w.bot._feed_kill(w.reg, s, {"type": "kill", "boss": "Gatekeeper"}))
    c = w.c.get(f"/api/loot/run/{w.ev.key}", headers=H).json()["councils"][0]
    assert c["bosses_progress"]["done"] == 1


# ---------------------------------------------------------------- Loot › Items: the Claude draft is a proposal

class DraftProvider:
    name = "fake"

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def complete(self, workload, system, user, schema):
        self.calls.append((workload, system, user))
        return schema(tiers=[tierdraft.SpecTier(**r) for r in self.rows], summary="for casters", warnings=["known only by name"])


def test_claude_draft_never_writes_without_a_confirm(w):
    opened(w)
    tick(w, "Shard of Doom")
    key = item_key(0, "Shard of Doom")
    m = w.ms[0].main
    good, other = f"{m.cls}:{m.spec}", f"{w.ms[1].main.cls}:{w.ms[1].main.spec}"
    prov = DraftProvider([{"spec": good, "tier": "S", "why": "their best cloak"}, {"spec": "Priest:Nope", "tier": "S"},
                          {"spec": other, "tier": "Z"}, {"spec": "role:healer", "tier": "A"}, {"spec": good, "tier": "B"}])
    guild_ctx(w.bot, w.reg).provider = prov
    before = (w.reg.store.root / GUILD / "items" / f"{key}.yaml").read_text()
    r = w.c.post(f"/api/loot/items/{key}/draft", json={}, headers=H)  # MCP without an actor: a draft decides nothing
    assert r.status_code == 200, r.text
    dr = r.json()["draft"]
    assert dr["tiers"] == {good: "S"} and dr["why"][good] == "their best cloak"
    assert len(dr["dropped"]) == 3 and any("not a spec" in x or "no spec" in x for x in dr["dropped"]) and any("single specs" in x for x in dr["dropped"])
    assert "unverified" in dr["provenance"] and dr["summary"] == "for casters"
    assert prov.calls[0][0] == "tier_draft" and "<item>" in prov.calls[0][2] and "Shard of Doom" in prov.calls[0][2]
    assert (w.reg.store.root / GUILD / "items" / f"{key}.yaml").read_text() == before, "nothing written"
    assert not w.reg.items()[key].tiers
    assert w.c.get(f"/api/loot/items/{key}", headers=H).json()["draft"]["tiers"] == {good: "S"}, "the grid shows the draft until saved"
    # the officer confirms it per item through the ordinary Save (the decision; D8's actor from MCP)
    r = w.c.post(f"/api/loot/items/{key}/tiers", json={"tiers": {good: "S"}}, headers=ACTOR).json()
    assert w.reg.items()[key].tiers == {good: "S"} and r["item"]["draft"] is None, "saved: the draft is gone"


def test_draft_without_the_model_says_so(w):
    opened(w)
    tick(w, "Shard of Doom")
    guild_ctx(w.bot, w.reg).provider = None
    r = w.c.post(f"/api/loot/items/{item_key(0, 'Shard of Doom')}/draft", json={}, headers=H)
    assert r.status_code == 503 and r.json()["error"] == tierdraft.LLM_DOWN
    assert w.c.post(f"/api/loot/items/{item_key(0, 'Shard of Doom')}/draft", json={}, headers=as_user(w.c, w.ms[2].discord_id)).status_code == 403


# ---------------------------------------------------------------- Loot › Items: CSV export / import

def rows_of(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def to_csv(rows) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def test_csv_round_trip_dedupe_unknown_and_the_rev(w):
    robe = tiered_item(w.reg, tiers={"Priest:Holy": "S", "role:tank": "A"})
    r = w.c.get("/api/loot/items/export", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    text = r.text
    rows = rows_of(text)
    header = rows[0]
    assert header[:3] == ["item_id", "item", "boss"] and "Priest:Holy" in header and "role:tank" in header, "wide keys only when an item has one"
    # round trip: the file as exported changes nothing
    _r = w.c.post("/api/loot/items/import", json={"csv": text}, headers=H)
    assert "preview" in _r.json(), _r.text
    pv = _r.json()["preview"]
    assert pv["cells"] == 0 and pv["items"] == [] and pv["unchanged"] == pv["rows"] and not pv["errors"]
    # an edit, a duplicate row (merged: the later cell wins), an unknown item and a bad cell
    col = {h: i for i, h in enumerate(header)}
    robe_row = next(rw for rw in rows[1:] if rw[1] == "Robe of Tests")
    edit = list(robe_row)
    edit[col["Priest:Holy"]] = "A"
    dup = [""] * len(header)
    dup[col["item"]], dup[col["Mage:Fire"] if "Mage:Fire" in col else col["Priest:Holy"]] = "robe of tests", "B"
    bad = [""] * len(header)
    bad[col["item"]], bad[col["role:tank"]] = "Imaginary Blade", "S"
    badcell = list(robe_row)
    badcell[col["role:tank"]] = "Z"
    pv = w.c.post("/api/loot/items/import", json={"csv": to_csv([header, edit, dup, bad, badcell])}, headers=H).json()["preview"]
    assert pv["duplicates"] == ["Robe of Tests"] and pv["unknown"] == ["Imaginary Blade"]
    assert any("'Z' is not a tier" in e for e in pv["errors"])
    (item,) = pv["items"]
    assert item["name"] == "Robe of Tests" and item["awards"] == 0
    assert robe.tiers["Priest:Holy"] == "S", "a preview writes nothing"
    # apply needs the actor from MCP (a decision, D8) and the preview's rev
    body = {"csv": to_csv([header, edit]), "preview": False}
    assert w.c.post("/api/loot/items/import", json=body, headers=H).status_code == 403
    pv1 = w.c.post("/api/loot/items/import", json={"csv": body["csv"]}, headers=H).json()["preview"]
    w.reg.set_item_tiers(item_key(robe.id, robe.name), {"role:tank": "B"}, "Mira")  # someone saved the grid meanwhile
    stale = w.c.post("/api/loot/items/import", json={**body, "rev": pv1["rev"]}, headers=ACTOR)
    assert stale.status_code == 409 and "changed since" in stale.json()["error"]
    pv2 = w.c.post("/api/loot/items/import", json={"csv": body["csv"]}, headers=H).json()["preview"]
    ok = w.c.post("/api/loot/items/import", json={**body, "rev": pv2["rev"]}, headers=ACTOR)
    assert ok.status_code == 200, ok.text
    assert "1 tier(s) on 1 item(s)" in ok.json()["message"] or "tier(s) on 1 item(s)" in ok.json()["message"]
    t = w.reg.item(robe.id).tiers
    assert t["Priest:Holy"] == "A" and t["role:tank"] == "A", "the row's other cells were the file's (A): applied as written"
    # and exporting again round-trips the new grid
    pv = w.c.post("/api/loot/items/import", json={"csv": w.c.get("/api/loot/items/export", headers=H).text}, headers=H).json()["preview"]
    assert pv["cells"] == 0


def test_csv_apply_needs_the_rev_and_checks_it_under_the_config_lock(w):
    """Regression: the rev check and the item writes run under ONE Registry.config_lock (a grid Save can't land between
    them), and an apply without the preview's rev is refused — by the route (400) and by tierdraft.apply_csv."""
    robe = tiered_item(w.reg, tiers={"Priest:Holy": "S"})
    header = rows_of(w.c.get("/api/loot/items/export", headers=H).text)[0]
    row = [""] * len(header)
    row[1], row[header.index("Priest:Holy")] = "Robe of Tests", "A"
    text = to_csv([header, row])
    r = w.c.post("/api/loot/items/import", json={"csv": text, "preview": False}, headers=ACTOR)
    assert r.status_code == 400 and "Preview the file first" in r.json()["error"]
    assert w.reg.item(robe.id).tiers["Priest:Holy"] == "S", "no rev: nothing written"
    pv = tierdraft.parse_csv(w.reg, text)
    with pytest.raises(RegistryError, match="Preview the file first"):
        tierdraft.apply_csv(w.reg, pv, "Officer", None)
    held = []
    real = tierdraft._rev

    def spy(items, keys):
        held.append(w.reg.config_lock._is_owned())
        return real(items, keys)

    tierdraft._rev = spy
    try:
        done = tierdraft.apply_csv(w.reg, pv, "Officer", pv["rev"])
    finally:
        tierdraft._rev = real
    assert held == [True], "the rev is checked while the config lock is held"
    assert done and w.reg.item(robe.id).tiers["Priest:Holy"] == "A"


def test_csv_import_asks_to_reweight_past_awards(w):
    opened(w)
    tick(w, "Shard of Doom")
    s = w.bot.events[(GUILD, w.ev.key)]
    run(w.bot.distribute(w.reg, s, by="Officer"))
    run(w.bot.confirm(w.reg, s, by="Officer"))
    key = item_key(0, "Shard of Doom")
    header = rows_of(w.c.get("/api/loot/items/export", headers=H).text)[0]
    row = [""] * len(header)
    row[1], row[header.index("Priest:Holy")] = "Shard of Doom", "S"
    text = to_csv([header, row])
    _r = w.c.post("/api/loot/items/import", json={"csv": text}, headers=H)
    assert "preview" in _r.json(), _r.text
    pv = _r.json()["preview"]
    assert pv["with_awards"] == 1 and pv["items"][0]["awards"] == 1
    r = w.c.post("/api/loot/items/import", json={"csv": text, "preview": False, "rev": pv["rev"]}, headers=ACTOR).json()
    assert "re-weight" in r["ask"] and not w.reg.items()[key].tiers, "D12: asked, nothing written"
    r = w.c.post("/api/loot/items/import", json={"csv": text, "preview": False, "rev": pv["rev"], "reweight": False}, headers=ACTOR).json()
    assert w.reg.items()[key].tiers == {"Priest:Holy": "S"} and r["reweighted"] == 0


def test_csv_header_and_profile_tier_refusals(reg):
    with pytest.raises(Exception, match="name the columns"):
        tierdraft.parse_csv(reg, "foo,bar\n1,2\n")
    with pytest.raises(Exception, match="empty"):
        tierdraft.parse_csv(reg, "")
    pv = tierdraft.parse_csv(reg, "item,Nope:Spec\nWhatever,S\n")
    assert pv["errors"] and "column Nope:Spec" in pv["errors"][0] and pv["unknown"] == ["Whatever"]


# ---------------------------------------------------------------- web: attendance routes

def test_attendance_routes(w):
    from test_attendance import done_run

    m = w.ms[0]
    for i in range(3):
        done_run(w.reg, w.rs, [m], [m], 3 + i, confirm=[(m, "yes")], n=40 + i)
    me = w.c.get("/api/me/attendance", headers=as_user(w.c, m.discord_id)).json()
    assert me["attended"] == 3 and me["total"] == 3 and len(me["runs"]) == 3 and me["sentence"].startswith("3 of 3")
    assert me["runs"][0]["outcome_label"] == "rostered" and "loot" not in me, "the loot factor's count is officers'"
    off = w.c.get("/api/members/attendance", params={"member": m.display_name}, headers=H).json()
    assert off["loot"] == {"attended": 3, "total": 3} and off["member"] == m.display_name
    assert w.c.get("/api/members/attendance", params={"member": m.display_name}, headers=as_user(w.c, w.ms[1].discord_id)).status_code == 403


# ---------------------------------------------------------------- MCP

@pytest.fixture
def mocked():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read() or b"{}") if request.method != "GET" else dict(request.url.params)
        calls.append((request.method, request.url.path, body))
        p = request.url.path
        if p == "/api/member":
            return httpx.Response(200, json={"uid": "123", "display_name": "Kessa"})
        if p == "/api/members/attendance":
            return httpx.Response(200, json={"member": "Kessa", "sentence": "5 of 6 in the last 30 days", "loot": {"attended": 4, "total": 6}})
        if p == "/api/loot/items/export":
            return httpx.Response(200, text="item_id,item,boss\n", headers={"content-type": "text/csv"})
        if p.endswith("/draft"):
            return httpx.Response(200, json={"draft": {"tiers": {"Priest:Holy": "S"}}, "item": {"name": "Robe"}})
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token="t" * 64, transport=httpx.MockTransport(handler)))
    yield calls
    ms.use(None)


def test_mcp_attendance_draft_and_export(mocked):
    out = ms.get_member("Kessa")
    assert out["attendance"] == {"sentence": "5 of 6 in the last 30 days", "loot": {"attended": 4, "total": 6}}
    assert mocked[-1] == ("GET", "/api/members/attendance", {"member": "123"})
    d = ms.draft_tiers("Robe of Tests")
    assert d == {"draft": {"tiers": {"Priest:Holy": "S"}}, "item": "Robe"} and mocked[-1][:2] == ("POST", "/api/loot/items/Robe of Tests/draft")
    assert ms.export_tiers() == "item_id,item,boss\n"
    names = {t.name for t in asyncio.run(ms.mcp.list_tools())}
    assert {"draft_tiers", "export_tiers"} <= names


def test_me_view_has_the_attendance_line():
    src = (Path(__file__).resolve().parents[1] / "src" / "oibot_gm" / "discord_registry.py").read_text()
    assert 'e.add_field(name="Attendance", value=(await asyncio.to_thread(reg.attendance_text, m))' in src, \
        "the attendance line, computed off the event loop (it parses the run files)"


# ---------------------------------------------------------------- review fixes (G4 fixer)

def test_members_get_403_on_the_tier_csv_routes(w):
    """The CSV export and both halves of the import are officers' (who(officer=True)): a member is refused."""
    member = as_user(w.c, w.ms[2].discord_id)
    assert w.c.get("/api/loot/items/export", headers=member).status_code == 403
    assert w.c.post("/api/loot/items/import", json={"csv": "item,Priest:Holy\nRobe of Tests,S\n"}, headers=member).status_code == 403
    assert w.c.post("/api/loot/items/import", json={"csv": "item,Priest:Holy\nRobe of Tests,S\n", "preview": False, "rev": "x"}, headers=member).status_code == 403


def test_a_grid_save_reads_the_item_under_the_config_lock(reg):
    """Regression: Registry.set_item_tiers read the item file BEFORE taking config_lock, so a grid Save that read
    first could overwrite a CSV import's write that landed between its read and its write. Now the read and the
    write are one locked section (an RLock: apply_csv already holding it nests)."""
    robe = tiered_item(reg, tiers={"Priest:Holy": "S"})
    held = []
    real = reg.item

    def spy(*a, **k):
        held.append(reg.config_lock._is_owned())
        return real(*a, **k)

    reg.item = spy
    try:
        reg.set_item_tiers(item_key(robe.id, robe.name), {"Priest:Holy": "A"}, "Mira")
    finally:
        del reg.item
    assert held and all(held), "the item is read while the config lock is held"
    assert reg.item(robe.id).tiers["Priest:Holy"] == "A"


def test_csv_export_escapes_formula_names_and_round_trips(reg):
    """Learned item names come from the companion's chat-log parse: a name starting with '=' (or + - @) is written
    with a leading quote so a spreadsheet opens it as text, and the import reads the quote back off."""
    import yaml

    d = reg.store.root / reg.key / "items"
    d.mkdir(parents=True, exist_ok=True)
    (d / "90007.yaml").write_text(yaml.safe_dump({"id": 90007, "name": '=HYPERLINK("http://x","Robe")', "slot": "chest", "type": "misc", "boss": "@Boss", "tiers": {}}))
    reg.__dict__.pop("_items_cache", None)
    it = reg.item(90007)
    text = tierdraft.export_csv(reg)
    row = next(r for r in rows_of(text)[1:] if "HYPERLINK" in r[1])
    assert row[1].startswith("'=") and row[2] == "'@Boss", "no cell opens as a formula"
    assert tierdraft.csv_untext(row[1]) == it.name and tierdraft.csv_untext("'plain") == "'plain"
    header = rows_of(text)[0]
    edit = list(row)
    edit[header.index("Priest:Holy")] = "S"
    pv = tierdraft.parse_csv(reg, to_csv([header, edit]))
    assert not pv["unknown"] and [r["name"] for r in pv["items"]] == [it.name], "the escaped name finds its item"


def test_csv_text_escapes_every_formula_lead():
    for lead in ("=", "+", "-", "@", "\t", "\r"):
        assert tierdraft.csv_text(lead + "x") == "'" + lead + "x"
        assert tierdraft.csv_untext(tierdraft.csv_text(lead + "x")) == lead + "x"
    assert tierdraft.csv_text("Robe of Tests") == "Robe of Tests" and tierdraft.csv_text(None) == ""


def test_the_kill_line_stamp_for_every_kind():
    """Regression: a zoned ISO stamp rendered as a clock in the raider's offset and a string epoch as an empty '()'.
    Both name an instant: a native <t:unix:t> stamp (feed.ts_epoch); a zone-less stamp is a 12-hour clock; junk is ''."""
    assert _log_clock("1765000000") == "<t:1765000000:t>" and _log_clock("1765000000.5") == "<t:1765000000:t>"
    from datetime import datetime as dt

    zoned = "2026-12-09T20:15:33-08:00"
    assert _log_clock(zoned) == f"<t:{int(dt.fromisoformat(zoned).timestamp())}:t>", "the instant, not the raider's offset"
    assert _log_clock("2026-12-09T20:15:33") == "8:15 PM"
    assert _log_clock("junk") == "" and _log_clock(None) == "" and _log_clock("") == ""
    assert feed_mod.ts_epoch("20:15") is None and feed_mod.ts_epoch(1765000000) == 1765000000.0


def test_a_kill_with_an_unreadable_stamp_has_no_empty_parentheses(council, bosses):
    c = council
    run(c.bot._feed_kill(c.reg, c.s, {"type": "kill", "boss": "Gatekeeper", "ts": "1765000000"}))
    assert ("kill", "☠ **Gatekeeper** down (<t:1765000000:t>)") in c.bot.posts, "a string epoch is an instant too"


class CouncilPosts(LootBot):
    async def loot_notify(self, reg, session, line=None, *, kind="", drop_id=None):
        self.posts.append((kind, line or "", session.id))


def test_all_bosses_down_reaches_every_council_of_a_split_run(reg, rs, bosses):
    """Regression: on a split run ALL_DOWN_LINE went only to the thread of the council whose kill completed the set;
    every council of the run now gets it, once per thread."""
    ev, _ = real_run(reg, rs)
    bot = CouncilPosts(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=1))
    other = LootSession(**{**s.model_dump(), "id": f"{ev.key}-r1", "roster_index": 1, "raid_thread_id": 7777, "bosses_done": list(BOSSES)})
    other.attach(s.store())
    other.save("t: roster 2's council, its bosses down")
    bot.events[(GUILD, other.id)] = other
    for b in BOSSES:
        run(bot._feed_kill(reg, s, {"type": "kill", "boss": b, "ts": 1_765_000_000}))
    downs = [p for p in bot.posts if p[0] == "all_down"]
    assert sorted(p[2] for p in downs) == sorted([s.id, other.id]), "each council's thread, once"
    run(bot._feed_kill(reg, s, {"type": "kill", "boss": BOSSES[-1], "ts": 1_765_000_001}))
    assert len([p for p in bot.posts if p[0] == "all_down"]) == 2, "a duplicate report prompts nobody again"


def test_the_web_run_card_shows_bosses(w, bosses):
    """Design §3.5 L12: the web run card (ev_json loot) carries 'Bosses N / M' as words from the server."""
    opened(w)
    card = w.c.get(f"/api/run/{w.ev.key}", headers=H).json()["loot"]
    assert card["bosses"] == {"done": 0, "total": 2, "all_down": False, "text": "Bosses 0 / 2"}
    s = w.bot.events[(GUILD, w.ev.key)]
    for b in BOSSES:
        run(w.bot._feed_kill(w.reg, s, {"type": "kill", "boss": b}))
    card = w.c.get(f"/api/run/{w.ev.key}", headers=H).json()["loot"]
    assert card["bosses"]["all_down"] and card["bosses"]["text"].startswith("Bosses 2 / 2 · all down")
    sheet = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "board" / "SheetCard.tsx").read_text()
    assert "e.loot.bosses.text" in sheet, "the card prints the server's words"


def test_members_attendance_column_route(w):
    """Design §3.5 M43: the Members page's 'att 5/6' — officers only; the view's count (bench counts) and the loot
    factor's once the window is tracked."""
    from test_attendance import done_run

    m = w.ms[0]
    for i in range(3):
        done_run(w.reg, w.rs, [m], [m], 3 + i, confirm=[(m, "yes")], n=60 + i)
    r = w.c.get("/api/members/attendance/all", headers=H)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][str(m.discord_id)]
    assert row["text"] == "att 3/3" and row["loot"] == "loot 3/3" and row["attended"] == 3
    assert r.json()["rows"][str(w.ms[5].discord_id)]["text"] is None, "nothing counted: no column text"
    assert w.c.get("/api/members/attendance/all", headers=as_user(w.c, w.ms[1].discord_id)).status_code == 403
    page = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Members.tsx").read_text()
    assert "/api/members/attendance/all" in page and "meta.viewer.officer" in page
