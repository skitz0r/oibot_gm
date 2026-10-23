"""Loot appeals (L32, dual interfaces phase 4 — design §5.49): a member asks the council about one of their OWN awards.

One appeal per award; the member's words are data (quoted, escaped, never parsed, no LLM); the card in the officer
channel carries nothing officer-only (D2) — Details (ephemeral) and Loot › Appeals do; only officers resolve, with one
outcome; the member hears it as a NOTICE, never a DM. An officer cannot open one on a member's behalf — by design: the
appeal is the member's own voice (an officer who disagrees with an award corrects it), and MCP's token is the owner's."""
from __future__ import annotations

import asyncio
import itertools
import types
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_loot_web import OFFICER, WebLootBot, as_user, env  # noqa: F401 — env is a fixture
from wizard_harness import Harness

from oibot_gm import appeals, lootctx
from oibot_gm.models import LootAward
from oibot_gm.web import app as web
from oibot_gm.wizard_flows import FLOWS

TOKEN = "a" * 64
H = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}
_ids = itertools.count(70_000)
REASON = "tank progression"  # officer data: never in the card, the thread or a member's reply


class FakeThread:
    def __init__(self, name):
        self.id, self.name, self.sent, self.edits = next(_ids), name, [], []

    async def send(self, content=None, **kw):
        self.sent.append(content)

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeMessage:
    def __init__(self, ch, embed, view, allowed):
        self.id, self.ch, self.embed, self.view, self.allowed = next(_ids), ch, embed, view, allowed
        self.thread = None
        self.edits = []

    async def create_thread(self, name):
        self.thread = FakeThread(name)
        self.ch.bot.channels[self.thread.id] = self.thread
        return self.thread

    async def edit(self, **kw):
        self.edits.append(kw)
        self.embed, self.view = kw.get("embed", self.embed), kw.get("view", self.view)


class FakeChannel:
    def __init__(self, bot, cid=55):
        self.bot, self.id, self.name, self.messages = bot, cid, "roster", []
        bot.channels[cid] = self

    async def send(self, content=None, *, embed=None, view=None, allowed_mentions=None, **kw):
        m = FakeMessage(self, embed, view, allowed_mentions)
        self.messages.append(m)
        return m

    async def fetch_message(self, mid):
        return next(m for m in self.messages if m.id == mid)


class AppealBot(WebLootBot):
    """The real web app + loot context, an officer channel that records, and a DM sender that must never be called."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.channels: dict = {}
        self.roster = FakeChannel(self)
        self.dms: list = []

    def officer_channel(self, reg, ev=None):
        return self.roster

    def get_channel(self, cid):
        return self.channels.get(int(cid)) if cid else None

    async def send_member_dm(self, *a, **kw):  # the DM rule: an appeal never DMs anyone
        self.dms.append((a, kw))
        raise AssertionError("an appeal never DMs")

    async def is_officer_anywhere(self, reg, uid):
        return int(uid) == OFFICER


def award(reg, m, item="Robe of Tests", *, iid=None, offspec=False, other="Somebody"):
    """One confirmed award to `m`'s main, with the council's record (an override with candidates and a reason)."""
    c = m.main or m.characters[0]
    iid = iid or f"run-x-d{next(_ids)}"
    a = LootAward(raider=c.label, item_id=0, tier="A", total_weight=1.5, offspec=offspec, received=date(2026, 10, 6), instance="barrow_deeps", boss="Gatekeeper",
                  item_name=item, discord_id=m.discord_id, by="Jonny", source="override", bot_pick=other, import_id=iid, confirmed_at="2026-10-06T20:13:00-07:00",
                  candidates_snapshot=[{"character": other, "tier": "S", "base": 1.234}, {"character": c.label, "tier": "A", "base": 0.987}])
    reg.store.append_jsonl(f"{reg.key}/ledger.jsonl", a.model_dump(mode="json"))
    reg.store.append_jsonl(f"{reg.key}/precedents.jsonl", {"import_id": iid, "item": item, "bot": other, "human": c.label, "reason": REASON, "date": "2026-10-06"})
    return a


@pytest.fixture
def a(reg, rs, env):  # noqa: F811
    bot = AppealBot(reg, rs)
    m, other = reg.test_members()[0], reg.test_members()[1]
    aw = award(reg, m)
    bot.__dict__.pop("contexts", None)  # the cached loot context reads the ledger as written above
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, m=m, other=other, award=aw, aid=appeals.award_id(aw))


def ask(a, uid, text="I was told I'm next for this after the last two resets — <@123> @everyone **really**", aid=None):
    return a.c.post(f"/api/me/loot/{aid or a.aid}/appeal", json={"text": text}, headers=as_user(a.c, uid))


def card_text(msg) -> str:
    e = msg.embed
    return "\n".join([e.title or "", e.description or "", *(f"{f.name}: {f.value}" for f in e.fields), e.footer.text or ""])


# ---------------------------------------------------------------- open: the member's own award, once

def test_one_appeal_per_award_with_the_members_words_as_data_and_no_scores_in_the_card(a):
    h = as_user(a.c, a.m.discord_id)
    rows = a.c.get("/api/me/loot", headers=h).json()
    row = next(r for r in rows["rows"] if r["item"] == "Robe of Tests")
    assert row["award"] == a.aid and row["appeal"] is None and "reason" not in row
    pv = a.c.post(f"/api/me/loot/{a.aid}/appeal", json={"preview": True}, headers=h).json()
    assert pv["preview"] and "nobody is DMed" in pv["consequence"] and not appeals.store_of(a.reg).all(), "a preview writes nothing"
    r = a.c.post(f"/api/me/loot/{a.aid}/appeal", json={"text": "short"}, headers=h)
    assert r.status_code == 400 and "at least" in r.json()["error"] and not appeals.store_of(a.reg).all()
    r = ask(a, a.m.discord_id)
    assert r.status_code == 200, r.text
    assert "Me page" in r.json()["message"] and r.json()["appeal"]["state"] == "open"
    (ap,) = appeals.store_of(a.reg).all()
    assert ap.id == a.aid and ap.member_id == a.m.discord_id and ap.text.startswith("I was told") and ap.via == "web"
    # the card: the award as the log shows it, the words quoted under "<member> wrote:", markdown/mentions escaped,
    # no mentions allowed — and nothing officer-only (D2)
    (msg,) = a.bot.roster.messages
    text = card_text(msg)
    assert f"{a.m.display_name} wrote:" in text and "> I was told" in text
    assert "\\*\\*really\\*\\*" in text and "@everyone" not in text.replace("@​everyone", ""), "markdown and @everyone escaped"
    assert msg.allowed.everyone is False and msg.allowed.users is False and msg.allowed.roles is False, "the card pings nobody (<@123> stays text)"
    for officer_only in (REASON, "1.234", "0.987", "Somebody", "override", "Jonny", "weight"):
        assert officer_only not in text, f"{officer_only!r} is officer data (D2): never on the card"
    assert msg.thread is not None and msg.thread.name == f"loot · {a.m.display_name} · Robe of Tests" and not msg.thread.sent, "the thread is the officers' to talk in"
    assert {c.custom_id for c in msg.view.children} == {f"appeal.{a.reg.key}:details:{a.aid}", f"appeal.{a.reg.key}:resolve:{a.aid}"}
    assert ap.thread_id == msg.thread.id or appeals.store_of(a.reg).get(a.aid).thread_id == msg.thread.id
    assert any("loot question from" in t for _, _, t in a.bot.ops.lines)
    # one appeal per award
    r = ask(a, a.m.discord_id, "Asking again about the very same award")
    assert r.status_code == 400 and "already asked" in r.json()["error"] and len(appeals.store_of(a.reg).all()) == 1
    assert len(a.bot.roster.messages) == 1 and not a.bot.dms


def test_only_the_member_opens_one_never_someone_else_an_officer_or_mcp(a):
    r = ask(a, a.other.discord_id)
    assert r.status_code == 400 and r.json()["error"] == appeals.NOT_YOURS, "someone else's award"
    r = ask(a, OFFICER)
    assert r.status_code == 400, "an officer cannot open one on a member's behalf: the route asks about the VIEWER's own awards"
    a.c.cookies.clear()
    r = a.c.post(f"/api/me/loot/{a.aid}/appeal", json={"text": "From the owner's token, about someone"}, headers=H)
    assert r.status_code == 403 and "MCP" in r.json()["error"]
    assert not appeals.store_of(a.reg).all()


def test_an_appeal_without_an_officer_channel_is_recorded_and_waits_on_the_site(a):
    a.bot.officer_channel = lambda reg, ev=None: None
    r = ask(a, a.m.discord_id)
    assert r.status_code == 200 and appeals.store_of(a.reg).get(a.aid) is not None
    assert any(lvl == "warn" and "Loot › Appeals" in t for _, lvl, t in a.bot.ops.lines)
    off = a.c.get("/api/loot/appeals", headers=as_user(a.c, OFFICER)).json()
    assert [x["id"] for x in off["open"]] == [a.aid] and off["open"][0]["link"] is None


# ---------------------------------------------------------------- resolve: officers only, a notice, no DM

def test_resolve_writes_a_notice_never_a_dm_and_the_member_cannot_resolve(a):
    assert ask(a, a.m.discord_id).status_code == 200
    hm = as_user(a.c, a.m.discord_id)
    assert a.c.get("/api/loot/appeals", headers=hm).status_code == 403
    assert a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "stands"}, headers=hm).status_code == 403, "a member cannot resolve"
    a.c.cookies.clear()
    r = a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "stands"}, headers=H)
    assert r.status_code == 403 and "X-Oibot-Actor" in r.json()["error"], "D8: resolving answers a person"
    pv = a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "stands", "preview": True}, headers=H).json()
    assert "nobody is DMed" in pv["consequence"] and appeals.store_of(a.reg).get(a.aid).state == "open"
    ho = as_user(a.c, OFFICER)
    lst = a.c.get("/api/loot/appeals", headers=ho).json()
    (row,) = lst["open"]
    assert row["details"]["reason"] == REASON and row["details"]["bot"] == "Somebody" and row["details"]["candidates"], "officers see the council's record"
    assert set(row["consequences"]) == set(appeals.OUTCOMES) and row["link"].endswith(str(a.bot.roster.messages[0].thread.id))
    assert a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "maybe"}, headers=ho).status_code == 400
    r = a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "stands", "note": "checked the sheet; he wasn't next"}, headers=ho)
    assert r.status_code == 200, r.text
    ap = appeals.store_of(a.reg).get(a.aid)
    assert ap.state == "resolved" and ap.outcome == "stands" and ap.resolved_by_id == OFFICER and ap.note.startswith("checked")
    (n,) = [n for n in a.reg.notices(a.m.discord_id) if n["kind"] == "appeal"]
    assert appeals.OUTCOMES["stands"] in n["text"] and "Robe of Tests" in n["text"] and "checked" not in n["text"], "the note stays with the officers"
    assert not a.bot.dms, "the DM rule: an outcome is a notice"
    msg = a.bot.roster.messages[0]
    assert msg.view is None and "Resolved" in card_text(msg), "the card is edited to its final state"
    assert msg.thread.sent and msg.thread.edits[-1] == {"archived": True, "locked": True}
    # the member's view: the state and the outcome line — never the note or who resolved it
    mine = a.c.get("/api/me/loot", headers=as_user(a.c, a.m.discord_id)).json()
    q = mine["appeals"][0]
    assert q["state"] == "resolved" and q["outcome_label"] == appeals.OUTCOME_LABELS["stands"] and "note" not in q and "resolved_by" not in q
    assert next(r for r in mine["rows"] if r["award"] == a.aid)["appeal"]["state"] == "resolved"
    r = a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "noted"}, headers=as_user(a.c, OFFICER))
    assert r.status_code == 400 and "Already resolved" in r.json()["error"]


def test_no_llm_anywhere_in_the_flow(a, monkeypatch):
    """No model reads, routes or answers an appeal: a provider that explodes on use is never touched."""
    from oibot_gm.llm import provider as prov

    monkeypatch.setattr(prov.Provider, "complete", lambda *x, **k: (_ for _ in ()).throw(AssertionError("no LLM in an appeal")))
    assert ask(a, a.m.discord_id).status_code == 200
    assert a.c.post(f"/api/loot/appeals/{a.aid}/resolve", json={"outcome": "noted"}, headers=as_user(a.c, OFFICER)).status_code == 200


# ---------------------------------------------------------------- Discord: /me loot-ask and the card's Resolve… (harness)

def harness_client(a):
    a.bot.raids = types.SimpleNamespace(store=lambda _r: None)
    return a.bot


def test_me_loot_ask_wizard_picks_writes_reviews_and_sends(a):
    user = types.SimpleNamespace(id=a.m.discord_id, display_name=a.m.display_name, mention="@m")
    h = Harness(a.reg, None, user=user, client=harness_client(a))

    async def go():
        await FLOWS["me_loot_ask"](h.command(), a.reg)
        assert "Pick the award" in h.text and "no DM" in h.text
        await h.pick("Pick the award…", a.aid)
        assert h.modal is not None, "the select opens the one prose modal"
        await h.submit(h.modal, text="too short")
        assert "at least" in h.text or "❌" in h.text
        await h.press("Fix my words")
        await h.submit(h.modal, text="I was promised this one after the last reset")
        assert "nobody is DMed" in h.text and "> I was promised" in h.text and f"{a.m.display_name} wrote:" in h.text
        assert not appeals.store_of(a.reg).all(), "nothing written before Send"
        await h.press("Send to the council")
        assert h.text.startswith("**Ask the council about an award**\n✅")
    asyncio.run(go())
    ap = appeals.store_of(a.reg).get(a.aid)
    assert ap is not None and ap.via == "discord" and ap.text == "I was promised this one after the last reset"
    assert a.bot.roster.messages and not a.bot.dms

    async def again():
        await FLOWS["me_loot_ask"](h.command(), a.reg)
    asyncio.run(again())
    assert "No award to ask about" in h.text, "an award already asked about leaves the select"


def test_appeal_resolve_flow_is_officers_only_and_resolves_with_one_outcome(a):
    assert ask(a, a.m.discord_id).status_code == 200
    member = types.SimpleNamespace(id=a.m.discord_id, display_name=a.m.display_name, mention="@m")
    hm = Harness(a.reg, None, user=member, client=harness_client(a))
    asyncio.run(FLOWS["appeal_resolve"](hm.card_press(), a.reg, aid=a.aid))
    assert hm.text == "Officers only." and appeals.store_of(a.reg).get(a.aid).state == "open"
    officer = types.SimpleNamespace(id=OFFICER, display_name="Jonny", mention="@j")
    h = Harness(a.reg, None, user=officer, client=harness_client(a))

    async def go():
        await FLOWS["appeal_resolve"](h.card_press(), a.reg, aid=a.aid)
        assert "Pick the outcome" in h.text and "wrote:" in h.text
        await h.pick("Pick the outcome…", "corrected")
        assert appeals.resolve_consequence(a.reg, appeals.store_of(a.reg).get(a.aid), "corrected") in h.text
        assert set(h.buttons()) >= {"Resolve", "Resolve with a note…", "Back"}
        await h.press("Resolve with a note…")
        await h.submit(h.modal, note="gave it to the tank instead")
        assert "✅" in h.text
    asyncio.run(go())
    ap = appeals.store_of(a.reg).get(a.aid)
    assert ap.outcome == "corrected" and ap.note == "gave it to the tank instead" and ap.resolved_by == "Jonny"
    assert any(n["kind"] == "appeal" for n in a.reg.notices(a.m.discord_id)) and not a.bot.dms


def test_the_card_buttons_carry_the_guild_and_match_after_a_restart(reg):
    b = appeals.AppealButton(reg.key, "resolve", "0123456789ab")
    assert b.item.custom_id == f"appeal.{reg.key}:resolve:0123456789ab"
    m = appeals.AppealButton.__discord_ui_compiled_template__.fullmatch(b.item.custom_id)
    assert m and m["g"] == reg.key and m["action"] == "resolve" and m["aid"] == "0123456789ab"
    assert appeals.AppealButton.__discord_ui_compiled_template__.fullmatch("appeal:details:0123456789ab"), "an id without the guild still matches"


def test_award_ids_are_stable_and_name_the_award(reg):
    m = reg.test_members()[0]
    aw = award(reg, m, iid="run-1-d1")
    assert appeals.award_id(aw) == appeals.award_id(LootAward(**aw.model_dump())) and len(appeals.award_id(aw)) == 12
    assert lootctx.award_ref(aw) == "run-1-d1"
