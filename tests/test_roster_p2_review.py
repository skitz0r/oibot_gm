"""Roster Phase 2 review fixes, each a regression test that fails without its fix:
- member help: a member snapshot (an officer's public-channel view included) shows only the asker's OWN open ask
  (what it asks, until when) — never other people's asks or their reasons — and leaves the officers' workflow lines
  (suggestions sent, fill sends, approvals, the planner's notes) out of "recent log"; officers keep everything;
- /roster overview: the Regulars field (coverage, recruit candidates, the 4-week forecast with who is away) is
  officer-only; members keep the rest of the overview;
- every officer-triggered fill send after lock shows the exact batch first and sends only what was shown: /raid fill
  and the lock card's Fill seats (the suggestions screen: one ask per line, SWAP marked, the backfill named → Send;
  a stale plan is refused and re-shown), plain-text run_fill (the diff lists the batch, Apply sends it with the plan
  rev, refused when it changed or without one), the site's /fill without a rev (409 "preview first" when the batch
  holds a role swap; a same-role batch still sends) and MCP fill_seats' docstring (preview, then pass the rev)."""
from __future__ import annotations

import asyncio
import types

import discord
import pytest
from discord import app_commands
from fastapi.testclient import TestClient
from test_flows_raid import channel, officer  # noqa: F401 — the flows' fixtures
from test_mcp import TOKEN, bearer
from test_parity_verbs import Bot
from test_roster_p1_fixes import _regulars
from test_roster_p2_asks import _locked_short_healer
from test_roster_p2_fixes import CardBot, _locked_with_free_dps
from test_roster_p2_planner import worked_example
from wizard_harness import Harness

from oibot_gm import configops
from oibot_gm import discord_registry as dr
from oibot_gm import flows_raid  # noqa: F401 — registers the flows
from oibot_gm import help as help_mod
from oibot_gm import mcp_server as ms
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS


def run(coro):
    return asyncio.run(coro)


def _shape(asks) -> list[tuple]:
    return [(a.discord_id, a.kind, a.role, a.character, a.spec, a.pair) for a in asks]


# ---------------------------------------------------------------- 1. member help: own ask only, no officer workflow

def _asked_prelock(reg, rs):
    """The worked example with the officers' suggestions sent: Kael (tank offspec) and Ana (healer flex) asked."""
    ev, p = worked_example(reg, rs)
    sent, _plan = run(Bot(reg, rs).send_plan(reg, rs, ev, rev=rc.role_plan(reg, rs, ev).rev, by="Officer"))
    assert [a.display_name for a in sent] == ["Kael", "Ana"]
    assert any(ln.startswith("suggestions sent by Officer: Kael, Ana") for ln in ev.log)
    return ev, p


def test_member_snapshot_shows_only_the_askers_own_ask(reg, rs):
    ev, p = _asked_prelock(reg, rs)
    kael = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=False, detail=True, viewer_id=p["Kael"].discord_id))
    own = next(ln for ln in kael.splitlines() if "your open ask" in ln)
    assert "switch to Protection on" in own and "for tank" in own and "answer by " in own
    assert "Ana" not in own and "flex" not in own, "nobody else's ask"
    assert "fill asks outstanding" not in kael and "Ana (flex" not in kael and "recent answers" not in kael
    other = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=False, detail=True, viewer_id=p["Gorm"].discord_id))
    assert "your open ask" not in other and "Kael (offspec" not in other and "Ana (flex" not in other


def test_member_snapshot_log_leaves_out_the_officers_workflow(reg, rs):
    ev, p = _asked_prelock(reg, rs)
    ev.log += ["fill (Officer): asked Kael", "fill: a role swap would cover it — waiting for an officer's Ask", "suggestion: Ana declined (flex)",
               "lock (Officer): answers or pins changed during the solve — solving again", "Officer approved the swap"]
    member = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=False, detail=True, viewer_id=p["Kael"].discord_id))
    log = next(ln for ln in member.splitlines() if ln.strip().startswith("recent log:"))
    for hidden in ("suggestions sent by", "fill (Officer)", "role swap would cover", "suggestion: Ana", "solving again", "approved"):
        assert hidden not in log, hidden
    assert "opened (" in log, "the run's own story stays"
    officer_view = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=True, detail=True, viewer_id=p["Kael"].discord_id))
    assert "fill asks outstanding: Kael (offspec" in officer_view and "Ana (flex" in officer_view
    assert "suggestions sent by Officer: Kael, Ana" in officer_view and "approved the swap" in officer_view


def test_an_officer_asking_in_public_gets_the_member_view(reg, rs):
    ev, p = _asked_prelock(reg, rs)
    q = f"what's going on with {ev.key}?"
    private = help_mod.guild_state(reg, rs, p["Kael"].discord_id, True, q)
    public = help_mod.guild_state(reg, rs, p["Kael"].discord_id, True, q, public=True)
    assert "fill asks outstanding: Kael (offspec" in private and "suggestions sent by Officer" in private
    assert "fill asks outstanding" not in public and "suggestions sent by" not in public and "Ana (flex" not in public
    assert "your open ask" in public, "their own ask stays"


# ---------------------------------------------------------------- 2. /roster overview: Regulars for officers only

def _overview(reg, monkeypatch, officer_flag: bool) -> dict:
    _regulars(reg)
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    dr.register_commands(tree, types.SimpleNamespace(for_interaction=lambda i: reg), None, lambda kind, key: f"<{kind}:{key}>")
    overview = tree.get_command("roster").get_command("overview")
    said = []

    async def send_message(content=None, **kw):
        said.append(kw)
    itx = types.SimpleNamespace(guild_id=reg.config.discord_guild_id, user=types.SimpleNamespace(id=1, roles=[]),
                                response=types.SimpleNamespace(send_message=send_message))
    monkeypatch.setattr(dr, "is_officer", lambda i, r: officer_flag)
    run(overview.callback(itx))
    return said[0]


def test_roster_overview_regulars_field_is_officer_only(reg, monkeypatch):
    member = _overview(reg, monkeypatch, False)
    names = [f.name for f in member["embed"].fields]
    assert "Regulars" not in names and member["ephemeral"] is True
    assert any(n.startswith("Roles vs") for n in names), "members keep the rest of the overview"


def test_roster_overview_regulars_field_for_an_officer(reg, monkeypatch):
    officer_said = _overview(reg, monkeypatch, True)
    assert any(f.name == "Regulars" for f in officer_said["embed"].fields) and officer_said["ephemeral"] is False


# ---------------------------------------------------------------- 3. fill sends show the exact batch

def test_batch_lines_mark_the_swap_and_name_the_backfill():
    swap = rc.FillAsk(discord_id=1, display_name="Kael", kind="offspec", role="tank", character="Kaelthas", spec="Protection", reason="short 1 tank", pair=2, vacates="melee")
    back = rc.FillAsk(discord_id=2, display_name="Ana", kind="sub", role="melee", character="Anabelle", spec="Fury", reason="covering Kael's swap", pair=1)
    a, b = rc.batch_lines([swap, back])
    assert a == "Kael as Kaelthas (Protection) for tank — short 1 tank; offspec · SWAP (leaves melee) · backfill: Ana"
    assert b.startswith("Ana as Anabelle (Fury) for melee") and "SWAP" not in b and b.endswith("backfills Kael's swap")


def test_the_cap_is_part_of_the_plan_rev(reg, rs):
    """What a send cuts under FILL_MAX_OPEN is what the preview showed: an ask answered meanwhile (more room, same seats)
    makes the shown plan stale instead of sending more than was listed."""
    ev, extra = _locked_with_free_dps(reg, rs)
    ev.fill_asks += [rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="melee", character=m.display_name, spec="Combat", reason="short")
                     for m in extra[:2]]
    shown = rc.role_plan(reg, rs, ev)
    asks, held = rc.send_batch(shown)
    assert len(asks) == 1 and held == 1 and "1 more wait" in "\n".join(rc.batch_preview(shown))
    ev.fill_asks[0].answer = "no"  # room grows; the seat it held counts as open again
    bot = Bot(reg, rs)
    with pytest.raises(rc.PlanChanged):
        run(bot.send_plan(reg, rs, ev, rev=shown.rev, by="Officer"))
    assert not bot.dms


def test_raid_fill_after_lock_shows_the_batch_then_sends_exactly_it(reg, rs, officer, channel):  # noqa: F811
    ev, people = _locked_short_healer(reg, rs)  # recurring: the only cover is Offy's healer offspec (a role swap)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_fill"](h.command(), reg))
    assert "Send DMs exactly these:" in h.text and "1. Offy as Offy (Restoration) for healer" in h.text and "SWAP" in h.text
    assert not bot.dms and not ev.fill_asks, "nothing goes out before Send"
    shown, _ = rc.send_batch(rc.role_plan(reg, rs, ev))
    run(h.press("Send 1 ask"))
    assert _shape(ev.fill_asks) == _shape(shown) and set(bot.dms) == {people["Offy"].discord_id}
    assert ev.fill_asks[0].approved_by == "Tester" and "Asked Offy" in h.text


def test_raid_fill_after_lock_refuses_a_stale_plan_and_reshows_it(reg, rs, officer, channel):  # noqa: F811
    ev, people = _locked_short_healer(reg, rs)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_fill"](h.command(), reg))
    rc.free_seat(reg, rs, ev, "Heala", "declined")  # meanwhile the board changed: a second healer seat is open
    run(h.press("Send 1 ask"))
    assert not bot.dms and not ev.fill_asks and "⚠️ The sheet changed" in h.text and "Send DMs exactly these:" in h.text


def test_raid_fill_preview_has_no_send(reg, rs, officer, channel):  # noqa: F811
    ev, people = _locked_short_healer(reg, rs)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_fill"](h.command(), reg, preview=True))
    assert "A Send would DM:" in h.text and "Offy as Offy" in h.text and not bot.dms
    with pytest.raises(AssertionError, match="no buttons"):
        h._items()


def test_lock_card_fill_button_shows_the_batch_before_sending(reg, rs, officer):  # noqa: F811
    ev, people = _locked_short_healer(reg, rs)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(ev.key, "fill").callback(h.card_press()))
    assert "Offy as Offy (Restoration) for healer" in h.text and "SWAP" in h.text and not bot.dms
    run(h.press("Send 1 ask"))
    assert set(bot.dms) == {people["Offy"].discord_id}


def test_plain_text_fill_after_lock_lists_the_batch_and_sends_exactly_it(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    send = ConfigOp(op="run_fill", target=ev.key, value="send")
    said = configops.describe(reg, send)
    assert "send exactly these fill DMs" in said and "1. Offy as Offy (Restoration) for healer" in said and "SWAP" in said
    revs = configops.board_revs(reg, [send], rs)
    rev = configops.op_rev(send, ev.key, revs)
    assert rev == rc.role_plan(reg, rs, ev).rev, "the diff carries the plan rev after lock too"
    shown, _ = rc.send_batch(rc.role_plan(reg, rs, ev))
    bot = Bot(reg, rs)
    with pytest.raises(RegistryError, match="preview the suggestions first"):
        run(configops.apply_async(reg, send, "Officer", False, bot=bot))
    assert not bot.dms and not ev.fill_asks, "no blind send after lock"
    line = run(configops.apply_async(reg, send, "Officer", False, bot=bot, rev=rev))
    assert line.startswith(f"{ev.key}: asked Offy (offspec)") and _shape(ev.fill_asks) == _shape(shown)


def test_plain_text_fill_after_lock_refuses_a_changed_plan(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    send = ConfigOp(op="run_fill", target=ev.key, value="send")
    rev = configops.op_rev(send, ev.key, configops.board_revs(reg, [send], rs))
    rc.free_seat(reg, rs, ev, "Heala", "declined")  # the plan changed after the diff
    bot = Bot(reg, rs)
    with pytest.raises(rc.PlanChanged):
        run(configops.apply_async(reg, send, "Officer", False, bot=bot, rev=rev))
    assert not bot.dms and not ev.fill_asks


@pytest.fixture
def client_bot(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    bot = Bot(reg, rs)
    return TestClient(web.create_app(bot)), bot


def test_web_fill_without_rev_refuses_a_role_swap(reg, rs, client_bot):
    c, bot = client_bot
    ev, people = _locked_short_healer(reg, rs)
    r = c.post(f"/api/run/{ev.key}/fill", json={}, headers=bearer())
    assert r.status_code == 409 and "preview first" in r.json()["error"] and r.json()["plan"]["rev"]
    assert not bot.dms and not ev.fill_asks
    pv = c.post(f"/api/run/{ev.key}/fill/preview", json={}, headers=bearer()).json()
    assert [a["display_name"] for a in pv["batch"]] == ["Offy"] and any("SWAP" in ln for ln in pv["plan"]["batch_lines"])
    ok = c.post(f"/api/run/{ev.key}/fill", json={"rev": pv["plan"]["rev"]}, headers=bearer())
    assert ok.status_code == 200 and [a.display_name for a in ev.fill_asks] == ["Offy"]


def test_web_fill_without_rev_still_sends_a_same_role_batch(reg, rs, client_bot):
    c, bot = client_bot
    ev, extra = _locked_with_free_dps(reg, rs, free=1, spares=2)
    assert not any(a.swap for a in rc.fill_batch(reg, rs, ev))
    r = c.post(f"/api/run/{ev.key}/fill", json={}, headers=bearer())
    assert r.status_code == 200, r.text
    assert ev.fill_asks and not any(a.swap for a in ev.fill_asks)


def test_mcp_fill_seats_says_preview_first_and_pass_the_rev():
    doc = ms.fill_seats.__doc__ if hasattr(ms.fill_seats, "__doc__") and ms.fill_seats.__doc__ else ""
    doc = doc or getattr(getattr(ms.fill_seats, "fn", None), "__doc__", "") or ""
    assert "ALWAYS preview first (send=false" in doc and "returned `plan.rev`" in doc and "role swap" in doc
