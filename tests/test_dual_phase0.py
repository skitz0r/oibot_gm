"""Dual interfaces, phase 0 (out/dual_interface_design.md §2.3, §2.7, §2.9; owner decisions D34): the consequence
sentences from code and their one place on the web (`ev_json.consequences`), the ONE Discord confirm screen
(`Wizard.decide`) on the lock / cancel card buttons, the application card, test clear and the proposal card, the one
error helper (`configops.user_error`) on the web routes, the confirm views' expiry and their persistent `cfgapply`
buttons, and the clock / iconography / wizard-rule sweep (§4 item 16)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from conftest import OWNER, join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, FakeBot, bearer
from wizard_harness import Harness

from oibot_gm import configops, consequences, discord_policy, news
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.raid_buttons import RunButton
from oibot_gm.registry import RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS, Wizard, lint

MILITARY = re.compile(r"\b([01]\d|2[0-3]):[0-5]\d\b(?! [AP]M)")  # a 24-hour stamp
ISO_DAY = re.compile(r"\b20\d\d-\d\d-\d\d\b")


def run(coro):
    return asyncio.run(coro)


def officer_user(name="Officer", uid=OWNER):
    return SimpleNamespace(id=uid, display_name=name, mention=f"<@{uid}>")


# ---------------------------------------------------------------- consequence sentences, from code

def test_lock_consequence_counts_dms_and_names_the_deadline_in_words(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms)
    ms[0].dm_opt_out = True
    reg.save(ms[0], "dms off")
    lines = consequences.consequence("lock", reg, ev)
    text = " ".join(lines)
    assert lines[0] == "The board becomes the roster."
    assert "6 rostered get a confirmation DM (1 with DMs off sees it on the site)." in text
    assert re.search(r"Confirm by \w{3} \d\d \w{3} \d{1,2}:\d\d [AP]M\.", text), text
    assert not MILITARY.search(text) and not ISO_DAY.search(text), "the clock rule: words, never 24-hour or ISO"
    assert "Test run: the puppets' DMs arrive in the tester's DMs." in lines, "a test run says where the DMs really go"


def test_lock_consequence_on_an_ad_hoc_run_promises_no_dms(reg, rs):
    start = (reg.now_local() + timedelta(days=2, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members()[:4])
    assert rc.run_kind(reg, ev) == "adhoc" and not rc.run_confirms(reg, ev)
    text = " ".join(consequences.consequence("lock", reg, ev))
    assert "Joining was the commitment — nobody is asked to confirm." in text and "confirmation DM" not in text


def test_cancel_clear_and_release_consequences_follow_the_state(reg, rs):
    ev = open_test_run(reg, rs)
    names = [m.display_name for m in reg.test_members()[:5]]
    join(reg, rs, ev, reg.test_members()[:5])
    assert consequences.consequence("cancel", reg, ev)[0] == "The sheet closes for the 5 people who answered."
    assert consequences.consequence("cancel", reg, ev)[1].startswith("Nobody is DMed")  # the DM rule (phase 2)
    assert consequences.consequence("clear", reg, ev) == ["The board is empty — nothing to clear."]
    ev.layout = [names[:3], names[3:]]
    assert consequences.consequence("clear", reg, ev)[0] == "5 placements go back to the bank."
    assert consequences.consequence("release", reg, ev) == ["Nothing is sent before lock — the seat only comes off the board."]
    lock_with_board(reg, rs, ev, names)
    for m in reg.test_members()[:5]:
        reg.add_placement_ask(m.discord_id, ev.team, m.display_name, "t")
    cancel = consequences.consequence("cancel", reg, ev)
    assert cancel[0] == "The sheet closes; 5 rostered seats are released, 5 open confirmation asks close." and cancel[1].startswith("Nobody is DMed")
    assert consequences.consequence("clear", reg, ev)[0].startswith("Every seat is released: 5 rostered see it on their confirmation card")
    assert consequences.consequence("release", reg, ev, name="Jonny")[0].startswith("Jonny sees the seat was released")
    assert consequences.consequence("release", reg, ev)[0].startswith("The seat is released: they see it")
    assert consequences.consequence("lock", reg, ev) == ["Already locked."]
    assert consequences.consequence("nonsense", reg, ev) == []
    assert set(consequences.consequences_json(reg, ev)) == {"lock", "cancel", "clear", "release", "release_named"}


def test_release_named_is_the_per_member_sentence_with_a_token_the_board_fills_in(reg, rs):
    """The board's drag-to-bank names the dragged member: the server writes the sentence with NAME_TOKEN where the
    name goes and BoardView only substitutes it — the per-member copy the old browser text had, without the browser
    composing a word of it. Before lock the named and general sentences are the same no-name line."""
    from pathlib import Path

    ev = open_test_run(reg, rs)
    names = [m.display_name for m in reg.test_members()[:5]]
    join(reg, rs, ev, reg.test_members()[:5])
    before = consequences.consequences_json(reg, ev)
    assert before["release_named"] == before["release"] == "Nothing is sent before lock — the seat only comes off the board."
    lock_with_board(reg, rs, ev, names)
    after = consequences.consequences_json(reg, ev)
    assert consequences.NAME_TOKEN in after["release_named"] and consequences.NAME_TOKEN not in after["release"]
    assert after["release_named"].replace(consequences.NAME_TOKEN, "Jonny") == " ".join(consequences.consequence("release", reg, ev, name="Jonny"))
    board = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "board" / "BoardView.tsx").read_text()
    assert "e.consequences.release_named.replace(\"{name}\", d.name)" in board, "BoardView fills the token, nothing more"


def test_ev_json_carries_the_consequences_so_the_browser_never_composes_them(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    c = TestClient(web.create_app(FakeBot(reg, rs)))
    start = (reg.now_local() + timedelta(days=2, hours=3)).replace(second=0, microsecond=0)
    adhoc = rc.open_run(reg, rs, "barrow_deeps", start, by="t", schedule=rc.ADHOC_SCHEDULE)
    d = c.get(f"/api/run/{adhoc.key}", headers=bearer()).json()
    assert d["consequences"]["lock"] == "The board becomes the roster. Joining was the commitment — nobody is asked to confirm."
    assert "DM" not in d["consequences"]["lock"], "the old SheetCard text promised confirmation DMs on ad hoc runs (§4 item 5)"
    assert set(d["consequences"]) == {"lock", "cancel", "clear", "release", "release_named"}
    from pathlib import Path

    sheet = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "board" / "SheetCard.tsx"
    text = sheet.read_text()
    assert "e.consequences?.lock" in text and "e.consequences?.cancel" in text and "everyone rostered gets a confirmation DM" not in text


def test_news_consequence_and_decision_refusal(reg):
    from test_news import add_news, proposal_data

    add_news(reg)
    ps = news.ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data())
    assert news.consequence(p, reg, "approved") == ["Approved: the apply job makes this guild setting within 15 minutes — no restart."]
    assert news.consequence(p, reg, "dismissed")[0].startswith("Dismissed: nothing changes")
    assert news.decision_refusal(reg.store, reg, p.id, "approved", 5) is None
    assert "no proposal" in news.decision_refusal(reg.store, reg, "000000-dead", "approved", 5)
    ps.move(p.id, "approved", "Kessa", officer=True, job=False, by_id=5)
    assert news.decision_refusal(reg.store, reg, p.id, "approved", 5) == f"{p.id} is approved: it can't become approved"
    q = ps.get(p.id)
    assert news.consequence(q, reg, "dismissed")[0] == "This withdraws the approval before the job picks it up."
    dev = ps.create(reg, proposal_data(kind="needs_developer"))
    assert "needs a developer" in news.decision_refusal(reg.store, reg, dev.id, "approved", 5)


# ---------------------------------------------------------------- the one error helper (§2.7)

def test_user_error_reads_refusals_as_written_and_bugs_as_the_generic_line():
    assert configops.user_error(RegistryError("not yours")) == "not yours"
    assert configops.user_error(ValueError("bad day")) == "bad day"
    assert configops.user_error(RuntimeError("Discord said 503")) == configops.GENERIC_ERROR
    assert configops.user_error(KeyError("x")) == configops.GENERIC_ERROR
    assert "owner has the details" in configops.GENERIC_ERROR and "RuntimeError" not in configops.GENERIC_ERROR


def test_report_error_tells_the_owner_only_about_bugs(reg):
    lines = []

    class Ops:
        async def emit(self, cfg, level, text, exc=None, dm=True):
            lines.append((level, text, exc))

    assert run(configops.report_error(Ops(), reg.config, RegistryError("nope"), "x")) == "nope" and lines == []
    assert run(configops.report_error(Ops(), reg.config, RuntimeError("boom"), "lock by Jon")) == configops.GENERIC_ERROR
    assert lines and lines[0][0] == "error" and "lock by Jon" in lines[0][1] and "boom" in lines[0][1] and isinstance(lines[0][2], RuntimeError)
    assert run(configops.report_error(None, None, RuntimeError("boom"), "offline")) == configops.GENERIC_ERROR, "no ops / no guild: logged only"


def test_web_routes_answer_a_bug_with_the_generic_line_and_an_ops_error(reg, rs, monkeypatch):
    """run(): a refusal is a 400 with its text; anything else is a 500 with GENERIC_ERROR (never a type name) and
    an ops error line — the same text an MCP tool repeats in its ToolError."""
    from oibot_gm.registry import Registry

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = FakeBot(reg, rs)
    c = TestClient(web.create_app(bot), raise_server_exceptions=False)
    m = reg.test_members()[0]
    r = c.post("/api/members/dm", json={"member": "nobody-here", "on": True}, headers=bearer())
    assert r.status_code == 400 and "RuntimeError" not in r.json()["error"]

    def broken(self, *a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(Registry, "save", broken)
    r = c.post("/api/members/dm", json={"member": m.display_name, "on": False}, headers=bearer())
    assert r.status_code == 500 and r.json()["error"] == configops.GENERIC_ERROR
    assert any(lvl == "error" and "disk on fire" in text for lvl, text in bot.ops.lines), bot.ops.lines


def test_the_catch_all_api_handler_reads_refusals_as_written_and_bugs_as_the_generic_line(reg, rs, monkeypatch):
    """A GET route (not through run()) that raises: a ValueError is a 400 with its text, anything else a 500 with
    GENERIC_ERROR and an ops error line to the selected guild — never a traceback or a type name for the person."""
    from oibot_gm.registry import Registry

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = FakeBot(reg, rs)
    c = TestClient(web.create_app(bot), raise_server_exceptions=False)
    m = reg.test_members()[0]
    assert c.get("/api/member", params={"name": m.display_name}, headers=bearer()).status_code == 200

    def refuses(self, uid):
        raise ValueError("that record is being rebuilt")

    monkeypatch.setattr(Registry, "verification", refuses)
    r = c.get("/api/member", params={"name": m.display_name}, headers=bearer())
    assert r.status_code == 400 and r.json() == {"error": "that record is being rebuilt"}
    assert not any(lvl == "error" for lvl, _ in bot.ops.lines), "a refusal is not the owner's business"

    def broken(self, uid):
        raise RuntimeError("index corrupt")

    monkeypatch.setattr(Registry, "verification", broken)
    r = c.get("/api/member", params={"name": m.display_name}, headers=bearer())
    assert r.status_code == 500 and r.json() == {"error": configops.GENERIC_ERROR}
    assert "RuntimeError" not in r.text and "index corrupt" not in r.text
    assert any(lvl == "error" and "/api/member" in text and "index corrupt" in text for lvl, text in bot.ops.lines), bot.ops.lines


# ---------------------------------------------------------------- the ONE confirm screen (Wizard.decide)

def test_decide_screen_shape_do_it_note_and_back(reg):
    done = []

    class W(Wizard):
        async def start(self, i):
            await self.decide(i, "Something happens.", do=self.go, note={"label": "Why?", "max_length": 100})

        async def go(self, i, note):
            done.append(note)
            await self.finish(i, f"done ({note})")

    h = Harness(reg)

    async def go():
        await W(reg, h.user.id, title="Test").start(h.command())
        assert h.buttons() == ["Do it", "Do it with a note…", "Back"] and "Something happens." in h.text
        await h.press("Back")
        assert done == [] and "Nothing was changed." in h.text
        await W(reg, h.user.id, title="Test").start(h.command())
        await h.press("Do it")
        assert done == [None] and h.text.endswith("done (None)")
        await W(reg, h.user.id, title="Test").start(h.command())
        await h.press("Do it with a note…")
        assert h.modal is not None and not lint(h.modal) and h.modal.title == "Test: a note"
        await h.submit(note="  because  ")
        assert done == [None, "because"] and h.text.endswith("done (because)")
        # no note configured: two buttons only
        class W2(W):
            async def start(self, i):
                await self.decide(i, "x", do=self.go, do_label="Lock now", danger=True)
        await W2(reg, h.user.id, title="Test").start(h.command())
        assert h.buttons() == ["Lock now", "Back"] and h.screen["view"].children[0].style == discord.ButtonStyle.danger

    run(go())


def test_decide_reruns_the_guard_on_the_note_submit(reg):
    """A Modal has no interaction_check: the note's Submit re-checks the guard itself (an officer demoted mid-screen
    changes nothing), and someone else's submit is refused."""
    allowed = {"ok": True}

    async def guard(i):
        return None if allowed["ok"] else "Officers only."

    done = []

    class W(Wizard):
        async def start(self, i):
            await self.decide(i, "x", do=self.go, note={"label": "Why?", "max_length": 50})

        async def go(self, i, note):
            done.append(note)
            await self.finish(i, "done")

    h = Harness(reg)

    async def go():
        await W(reg, h.user.id, title="T", guard=guard).start(h.command())
        await h.press("Do it with a note…")
        allowed["ok"] = False
        await h.submit(note="late")
        assert done == [] and "Officers only." in h.text

    run(go())


def card_bot(reg, rs, **verbs):
    async def is_officer_anywhere(reg_, uid):
        return True

    ops = SimpleNamespace(lines=[])

    async def emit(cfg, level, text, exc=None, dm=True):
        ops.lines.append((level, text))
    ops.emit = emit
    return SimpleNamespace(registries=SimpleNamespace(for_interaction=lambda i: reg), raids=SimpleNamespace(store=lambda r: rs), is_officer_anywhere=is_officer_anywhere,
                           ops=ops, ico=lambda k, v: "", **verbs)


def test_cancel_card_button_is_the_one_cancel_screen_with_reason_and_notify(reg, rs):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:3])
    calls = []

    async def cancel_run(reg_, rs_, ev_, by, reason, notify=True, by_id=None, via="discord"):
        calls.append((ev_.key, by, reason, notify))
        return f"Cancelled {ev_.key}"

    bot = card_bot(reg, rs, cancel_run=cancel_run)
    h = Harness(reg, rs, user=officer_user(), client=bot)

    async def go():
        await RunButton(reg.key, ev.key, "cancel").callback(h.card_press())
        assert "The sheet closes for the 3 people who answered." in h.text and h.buttons() == ["Cancel the run…", "Back"]
        assert h.screen["view"].children[0].style == discord.ButtonStyle.danger
        await h.press("Back")
        assert calls == [] and "Nothing was changed." in h.text
        await RunButton(reg.key, ev.key, "cancel").callback(h.card_press())
        await h.press("Cancel the run…")
        await h.submit(reason="server down", notify="quiet")  # one Form: the optional reason + "Tell the channel?"
        assert calls == [(ev.key, "Officer", "server down", False)] and h.text.endswith(f"Cancelled {ev.key}")
        # the old 60-second plain View is gone: nothing else answers a cancel press
        from oibot_gm import raid_buttons

        import inspect

        assert "Keep the run" not in inspect.getsource(raid_buttons)

    run(go())


def test_lock_card_button_locks_on_the_press_and_reports_a_bug_with_the_generic_line(reg, rs):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:3])
    state = {"fail": False}

    async def lock_run(reg_, rs_, ev_, by="scheduler"):
        if state["fail"]:
            raise RuntimeError("solver exploded")
        ev_.state, ev_.lock_error = "locked", None
        return f"{ev_.key}: locked by {by}, 1 roster(s), 3 confirmation DM(s)"

    bot = card_bot(reg, rs, lock_run=lock_run)
    h = Harness(reg, rs, user=officer_user(), client=bot)

    async def go():
        state["fail"] = True
        await RunButton(reg.key, ev.key, "lock").callback(h.card_press())
        assert "3 rostered get a confirmation DM" in h.text
        await h.press("Lock now")
        assert configops.GENERIC_ERROR in h.text and "solver exploded" not in h.text
        assert any(lvl == "error" and "solver exploded" in text for lvl, text in bot.ops.lines)
        state["fail"] = False
        await RunButton(reg.key, ev.key, "lock").callback(h.card_press())
        await h.press("Lock now")
        assert "locked by Officer" in h.text and ("info", f"{ev.key}: locked by Officer, 1 roster(s), 3 confirmation DM(s)") in bot.ops.lines
        await RunButton(reg.key, ev.key, "lock").callback(h.card_press())
        assert h.text == "Already locked."

    run(go())


def test_test_clear_is_the_decide_screen(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    ev = open_test_run(reg, rs)
    calls = []

    async def test_bench_clear(reg_, by, *, via="discord", by_id=None):  # the verb writes its own audit line (the flow adds none)
        calls.append((by, by_id))
        await bot.ops.emit(reg_.config, "warn", f"{by}: test bench: 16 test member(s) removed, 1 test run(s) cancelled")
        return "16 test member(s) removed, 1 test run(s) cancelled"

    bot = card_bot(reg, rs, test_bench_clear=test_bench_clear)
    h = Harness(reg, rs, user=officer_user(), client=bot)

    async def go():
        await FLOWS["test_clear"](h.command(), reg)
        assert "Cancels 1 test run, deletes 16 puppets, analytics cards resume." in h.text and h.buttons() == ["Clear the test bench", "Back"]
        assert calls == []
        await h.press("Clear the test bench")
        assert calls == [("Officer", h.user.id)] and "16 test member(s) removed" in h.text and bot.ops.lines[-1][0] == "warn"
        assert sum("test member(s) removed" in ln for _lvl, ln in bot.ops.lines) == 1, "one line: the verb's"
        reg.clear_test_members("t")
        ev.state = "cancelled"
        rs.save(ev, "x")
        await FLOWS["test_clear"](h.command(), reg)
        assert h.text == "Nothing to clear — no test members and no test runs."

    run(go())


# ---------------------------------------------------------------- confirm views: expiry and the cfgapply DynamicItem (§2.9)

class Editor:
    def __init__(self):
        self.edits = []

    async def __call__(self, **kw):
        self.edits.append(kw)


def test_config_confirm_view_expires_in_words_and_its_buttons_carry_the_guild(reg):
    req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", value="off")])
    view = discord_policy.ConfigConfirmView(reg, req, None, None, OWNER, "Owner", None)
    ids = [c.custom_id for c in view.children]
    assert [i.split(":")[0] for i in ids] == [f"cfgapply.{reg.key}", f"cfgapply.{reg.key}"] and ids[0].endswith(":apply") and ids[1].endswith(":cancel")
    assert all(len(i) <= 100 for i in ids) and view.token in discord_policy.PENDING_CONFIRMS
    m = re.fullmatch(discord_policy.CfgApplyButton.__discord_ui_compiled_template__, ids[0])
    assert m and m["g"] == reg.key and m["token"] == view.token and m["action"] == "apply"
    assert re.fullmatch(discord_policy.CfgApplyButton.__discord_ui_compiled_template__, "cfgapply:abcdefgh1234:cancel"), "an id without a guild still matches"
    ed = Editor()
    view.bind(ed)
    run(view.on_timeout())
    assert ed.edits == [{"content": discord_policy.EXPIRED, "embed": None, "view": None}] and view.token not in discord_policy.PENDING_CONFIRMS
    run(view.on_timeout())  # twice is harmless
    assert len(ed.edits) == 1


def test_compile_confirm_view_expires_too(reg):
    from oibot_gm.policy import LootCompiled, PolicyStore

    ps = PolicyStore(reg.store, reg.key)
    view = discord_policy.CompileConfirmView(ps, "loot", LootCompiled(rules=[], summary="x"), ps.read("loot"), None, reg)
    assert [c.custom_id.rsplit(":", 1)[1] for c in view.children] == ["confirm", "discard"]
    ed = Editor()
    view.bind(ed)
    run(view.on_timeout())
    assert ed.edits[-1]["content"] == discord_policy.EXPIRED and ed.edits[-1]["view"] is None


def test_a_press_after_a_restart_says_so_instead_of_interaction_failed(reg):
    """The token is process memory: after a restart (or an expiry) the DynamicItem finds nothing and edits the old
    message to EXPIRED_RESTART — never Discord's "This interaction failed"."""
    edits = []

    class Resp:
        def __init__(self):
            self._done = False

        def is_done(self):
            return self._done

        async def edit_message(self, **kw):
            self._done = True
            edits.append(kw)

        async def send_message(self, content=None, **kw):
            self._done = True
            edits.append({"content": content})

    itx = SimpleNamespace(response=Resp(), user=officer_user(), client=SimpleNamespace())
    btn = discord_policy.CfgApplyButton(reg.key, "gone-after-restart", "apply")
    run(btn.callback(itx))
    assert edits == [{"content": discord_policy.EXPIRED_RESTART, "embed": None, "view": None}]
    # a live token dispatches to the view: Cancel claims the diff
    view = discord_policy.ConfigConfirmView(reg, ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", value="off")]), None, None, OWNER, "Owner", None)

    async def plain_identity(reg_, user, *, max_age=None):  # the real one takes max_age: a press re-resolves live (PRIV_LIVE)
        return True, []
    client = SimpleNamespace(plain_identity=plain_identity, in_guild=None)
    itx2 = SimpleNamespace(response=Resp(), user=officer_user(), client=client)
    run(discord_policy.CfgApplyButton(reg.key, view.token, "cancel").callback(itx2))
    assert view.busy and edits[-1]["content"] == "Cancelled."


def test_the_client_registers_the_cfgapply_item():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "oibot_gm" / "discord_bot.py").read_text()
    assert re.search(r"add_dynamic_items\([^)]*CfgApplyButton", src)


# ---------------------------------------------------------------- the rule sweep (§4 item 16)

def test_absence_json_carries_the_days_in_words(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    c = TestClient(web.create_app(FakeBot(reg, rs)))
    m = reg.test_members()[0]
    reg.add_absence(m.discord_id, "2099-01-02", "2099-01-04", "holiday", "t")
    d = c.get(f"/api/member?name={m.display_name}", headers=bearer()).json()
    a = d["absences"][0]
    assert a["start"] == "2099-01-02" and a["span"] == reg.span_label("2099-01-02", "2099-01-04") and not ISO_DAY.search(a["span"])
    from pathlib import Path

    me = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "AbsencesCard.tsx").read_text()  # the Me page's card (phase 2 C)
    assert "a.span" in me and 'ff="monospace">{a.start}' not in me


def test_absences_card_and_my_absences_read_in_words(reg):
    from oibot_gm.discord_pool import absences_card
    from oibot_gm.flows_roster import absences_text

    reg.add_character(410_000_000_000_000_777, "Realone", "Realone", "Priest", "Holy", None, True)  # puppets stay off these lists (phase 2 C)
    m = reg.members[410_000_000_000_000_777]
    reg.add_absence(m.discord_id, "2099-01-02", "2099-01-04", "holiday", "t")
    field = next(f for f in absences_card(reg).fields if f.name == "Upcoming")
    assert reg.span_label("2099-01-02", "2099-01-04") in field.value and not ISO_DAY.search(field.value)
    text = absences_text(reg, 36500)
    assert m.display_name in text and "holiday" in text and not ISO_DAY.search(text)
    assert absences_text(reg, 7) == "No absences in the next week."


def test_roster_absences_is_a_horizon_select_not_a_typed_number(reg, monkeypatch):
    from discord import app_commands

    from oibot_gm import discord_registry
    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree, Guilds.of(reg.store, reg), Ops(client), lambda k, v: "")
    roster = next(c for c in tree.get_commands() if c.name == "roster")
    absences = next(c for c in roster.commands if c.name == "absences")
    assert absences.parameters == [], "the wizard rule: no `days:int` argument — the horizon is a select"
    me = next(c for c in tree.get_commands() if c.name == "me")
    plan = next(c for c in me.commands if c.name == "plan")
    assert "roles" not in [c.name for c in plan.commands], "phase 2 C: /me plan roles is gone — /me char → Flex"
    char = next(c for c in me.commands if c.name == "char")
    assert [p.name for p in char.parameters] == ["action"], "/me char: one command, a fixed Choice (the wizard rule)"
    h = Harness(reg, user=officer_user())

    async def go():
        await FLOWS["roster_absences"](h.command(), reg)
        assert h.selects() == ["How far ahead?"] and "No absences in the next 30 days." in h.text
        await h.pick("How far ahead?", "90")
        assert "No absences in the next 3 months." in h.text

    run(go())


def test_roster_absences_rechecks_officer_standing_on_every_press(reg, monkeypatch):
    """The list is officer-only data (reasons, who recorded them): an officer demoted mid-screen is refused on the
    next horizon press and the list is not redrawn — the guard every officer wizard carries, not a check at open only."""
    from oibot_gm import discord_registry

    allowed = {"ok": True}
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: allowed["ok"])
    h = Harness(reg, user=officer_user())

    async def go():
        await FLOWS["roster_absences"](h.command(), reg)
        listing = h.screen
        allowed["ok"] = False
        itx = h._on_screen()
        assert not await listing["view"].interaction_check(itx), "the guard runs on every press, not only at the start"
        assert "Officers only" in h.text and h.text != listing["text"]
        allowed["ok"] = True
        assert await listing["view"].interaction_check(h._on_screen())

    run(go())


def test_config_show_is_words_not_yaml(reg):
    sections = configops.config_summary(reg)  # phase 3: sections (one embed field each on /gm config show)
    text = configops.summary_text(sections)
    assert sections[0].lines[0].startswith(reg.config.name) and "now " in sections[0].lines[1] and (f"<@{OWNER}>" in text or "Owner: " in text)
    assert "Barrow Deeps" in text and "locks" in text and "Recruitment:" in text and "Plain text:" in text
    assert not MILITARY.search(text) and not ISO_DAY.search(text) and "discord_guild_id" not in text and "rosters:" not in text
    assert len(text) < 2000


def test_one_clock_implementation(reg):
    from oibot_gm import agents, flows_config, raid_views, wizard
    from oibot_gm.constants import CLOCK12, fmt12
    from oibot_gm.discord_feed import _log_clock
    from oibot_gm.registry import CLOCK12 as REG_CLOCK12

    assert CLOCK12 == REG_CLOCK12
    t = datetime(2026, 9, 22, 19, 5)
    assert fmt12(t) == "Tue 22 Sep 7:05 PM" and wizard.fmt12(t) == fmt12(t) and flows_config.fmt12 is wizard.fmt12
    aware = t.replace(tzinfo=reg.tz)
    assert raid_views.clock12(reg, aware) == reg.local12(aware) == "Tue 22 Sep 7:05 PM"
    assert agents.clock12(aware.isoformat(), reg.tz) == "Tue 22 Sep 7:05 PM"
    assert _log_clock("2026-12-09T20:15:03.120") == "8:15 PM" and _log_clock("garbage") == ""
    import inspect

    assert "re.sub" not in inspect.getsource(raid_views.clock12) and "re.sub" not in inspect.getsource(agents.clock12), "one implementation (constants.fmt12)"


def test_sheetcard_shortfall_is_icons_not_words():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "board" / "SheetCard.tsx").read_text()
    assert "short: {short}" not in text and "`${k} ${role}`" not in text
    assert 'kind="role" id={role}' in text, "a shortfall is the role icon plus a number"


def test_roster_overview_lines_put_no_name_beside_an_icon():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "oibot_gm" / "discord_registry.py").read_text()
    assert "{ico('role', r)} {r}:" not in src and "{ico('buff', ids.get(b, ''))} {b} ({who})" not in src
    flows = (Path(__file__).resolve().parents[1] / "src" / "oibot_gm" / "flows_raid.py").read_text()
    assert '", ".join(p.character or p.signup_name for p in ev.roster.selected)' not in flows, "the loot thread's roster is one member per line"


def test_companion_kill_line_is_twelve_hour():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "oibot_gm" / "discord_feed.py").read_text()
    assert "[11:16])\")" not in src and "_log_clock(ev['ts'])" in src
