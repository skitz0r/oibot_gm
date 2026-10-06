"""Roster redesign Phase 1: REGULARS of a recurring series, as amended by the owner (out/roster_decisions.md):
- D1: nothing is pre-filled — a regular starts unanswered; officers see the regulars who haven't answered (web sheet
  card data, the Discord health card, MCP get_run, the officer help snapshot); absences still mark people away, both
  when the sheet opens and when the absence is recorded later, and clearing it returns them to unanswered;
- the reminder goes to the regulars who haven't answered (every main while the series has none; ad hoc: nobody);
- D2: a non-regular's Join stays Join; D9: being a regular is a seat weight (above one rank step);
- officers edit regulars (registry, the site's route, plain text, MCP); a member can step down (D7);
- a test run treats the puppets as virtual regulars and touches no real membership."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import OWNER, by_role, join, open_test_run
from fastapi.testclient import TestClient
from test_analytics_cards import text_of
from test_mcp import TOKEN, FakeBot, bearer
from test_parity_verbs import Bot

from oibot_gm import configops, help as help_mod
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RAID_WEIGHT_DEFAULTS, RegistryError
from oibot_gm.web import app as web

RID = "barrow_deeps"
KEY = f"{RID}:default"
REAL = 400_000_000_000_000_000
ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def opened(reg):
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")


SPECS = [("Warrior", "Protection"), ("Priest", "Holy"), ("Shaman", "Restoration"), ("Rogue", "Combat"), ("Mage", "Fire"), ("Hunter", "Marksmanship")]
NAMES = ["Brannoc", "Mira", "Ona", "Kael", "Ana", "Jex"]


def real(reg, n: int):
    """A registered (non-puppet) member: regulars are real members only."""
    cls, spec = SPECS[n % len(SPECS)]
    uid = REAL + n
    reg.add_character(uid, f"Player{n}", NAMES[n % len(NAMES)] + ("" if n < len(NAMES) else str(n)), cls, spec, None, True)
    return reg.members[uid]


def with_regulars(reg, n: int = 4):
    opened(reg)
    ms = [real(reg, i) for i in range(n)]
    for m in ms:
        reg.regular_add(m.discord_id, KEY, "officer")
    return ms


def next_run(reg, rs):
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    return rc.open_run(reg, rs, RID, t, by="t")


# ---------------------------------------------------------------- the registry verbs

def test_regular_add_remove_and_their_refusals(reg):
    ms = with_regulars(reg, 2)
    a = ms[0]
    assert [m.discord_id for m, _ in reg.regulars(KEY)] == sorted([m.discord_id for m in ms], key=lambda u: reg.members[u].display_name.lower())
    with pytest.raises(RegistryError, match="already a regular"):
        reg.regular_add(a.discord_id, KEY, "t")
    with pytest.raises(RegistryError, match="ad hoc"):
        reg.regular_add(a.discord_id, f"{RID}:pug", "t")
    with pytest.raises(RegistryError, match="no recurring schedule"):
        reg.regular_add(a.discord_id, f"{RID}:nope", "t")
    with pytest.raises(RegistryError, match="test puppet"):
        reg.regular_add(reg.test_members()[0].discord_id, KEY, "t")
    # one character per member per series: another character replaces the first
    reg.add_character(a.discord_id, a.display_name, "Altanna", "Druid", "Restoration", None, False)
    m, c, warn = reg.regular_add(a.discord_id, KEY, "t", "Altanna")
    assert c.label == "Altanna" and KEY not in m.main.rosters and not warn
    reg.regular_remove(a.discord_id, KEY, "t")
    assert all(KEY not in c.rosters for c in reg.members[a.discord_id].characters)
    with pytest.raises(RegistryError, match="isn't a regular"):
        reg.regular_remove(a.discord_id, KEY, "t")


def test_same_character_on_two_series_of_one_raid_warns(reg):
    opened(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    m = real(reg, 0)
    reg.regular_add(m.discord_id, KEY, "t")
    _m, _c, warn = reg.regular_add(m.discord_id, f"{RID}:alt", "t")
    assert warn and "lockout" in warn[0]


def test_removed_schedule_leaves_orphans_that_can_be_cleared(reg):
    opened(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    m = real(reg, 0)
    reg.regular_add(m.discord_id, f"{RID}:alt", "t")
    reg.remove_schedule(RID, "alt", "t")
    assert [(k, mm.discord_id) for k, mm, _c in reg.orphan_regulars()] == [(f"{RID}:alt", m.discord_id)], "never stripped silently"
    reg.regular_remove(m.discord_id, f"{RID}:alt", "t")
    assert not reg.orphan_regulars()


# ---------------------------------------------------------------- D1: nothing pre-filled; absences mark people away

def test_regulars_start_unanswered_and_absences_mark_anyone_away(reg, rs):
    ms = with_regulars(reg, 4)
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    day = t.astimezone(reg.tz).date().isoformat()
    reg.add_absence(ms[0].discord_id, day, None, "work", "t")
    outsider = reg.test_members()[0]
    reg.add_absence(outsider.discord_id, day, None, None, "t")
    ev = rc.open_run(reg, rs, RID, t, by="t")
    assert not [s for s in ev.signups.values() if s.status != "out"], "D1: nobody is pre-filled Join"
    assert ev.signups[str(ms[0].discord_id)].source == "absence" and ev.signups[str(outsider.discord_id)].source == "absence"
    assert sorted(ev.regulars) == sorted(m.discord_id for m in ms)
    waiting = [m.discord_id for m, _c in rc.not_answered(reg, ev)]
    assert sorted(waiting) == sorted(m.discord_id for m in ms[1:]), "officers see the regulars who haven't answered — the away one has an answer"


def test_absence_recorded_after_open_and_cleared_again(reg, rs):
    ms = with_regulars(reg, 3)
    reg.add_character(ms[1].discord_id, ms[1].display_name, "Altmira", "Mage", "Frost", None, False)
    reg.regular_add(ms[1].discord_id, KEY, "t", "Altmira")  # a regular on an alt is marked away on that alt
    ev = next_run(reg, rs)
    day = ev.start.astimezone(reg.tz).date().isoformat()
    bot = Bot(reg, rs)
    for m in (ms[1], reg.test_members()[3]):
        _m, a = reg.add_absence(m.discord_id, day, None, None, "t")
        run(bot.after_absence(reg, m, a.start, a.end, "t"))
        sg = ev.signups[str(m.discord_id)]
        assert (sg.status, sg.source) == ("out", "absence")
    assert ev.signups[str(ms[1].discord_id)].character == "Altmira"
    assert ms[1].discord_id not in [m.discord_id for m, _c in rc.not_answered(reg, ev)]
    a = reg.clear_absence(ms[1].discord_id, day, "t")
    run(bot.after_absence_cleared(reg, reg.members[ms[1].discord_id], a))
    assert str(ms[1].discord_id) not in ev.signups, "cleared: back to unanswered, not to a pre-filled Join"
    assert ms[1].discord_id in [m.discord_id for m, _c in rc.not_answered(reg, ev)]


def test_expected_is_regulars_else_everyone(reg, rs):
    opened(reg)
    ev = next_run(reg, rs)
    assert rc.expected_from(reg, ev) == "everyone" and len(rc.expected(reg, ev)) == sum(1 for m in reg.members.values() if m.main)
    m = real(reg, 0)
    reg.regular_add(m.discord_id, KEY, "t")
    assert rc.expected_from(reg, ev) == "regulars" and [x.discord_id for x, _ in rc.expected(reg, ev)] == [m.discord_id]


# ---------------------------------------------------------------- officers see who hasn't answered

def test_health_card_names_the_regulars_who_havent_answered(reg, rs):
    ms = with_regulars(reg, 3)
    ev = next_run(reg, rs)
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    body = text_of(views.health_layout(reg, rs, ev, rc.run_team(reg, ev), ico))
    assert "**No answer** 2 · regulars" in body
    lines = body.splitlines()
    for m in ms[1:]:
        c = m.main
        assert f"<spec:{c.cls}:{c.spec}> {c.label}" in lines, "one member per line, spec icon + character"
    assert reg.test_members()[0].main.label not in body, "non-regulars aren't chased"


def test_nudge_goes_to_unanswered_regulars_only(reg, rs):
    ms = with_regulars(reg, 3)
    ev = next_run(reg, rs)
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    bot = Bot(reg, rs)
    run(bot.post_health(reg, rs, ev, None, nudge=True))
    assert sorted(ev.nudged) == sorted(m.discord_id for m in ms[1:])
    assert set(bot.dms) == {m.discord_id for m in ms[1:]}


def test_get_run_lists_the_regulars_and_their_answers(reg, rs, monkeypatch):
    ms = with_regulars(reg, 3)
    ev = next_run(reg, rs)
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    rc.set_signup(reg, rs, ev, ms[1], None, "out")
    outsider = reg.test_members()[0]
    rc.set_signup(reg, rs, ev, outsider, None, "in")
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    c = TestClient(web.create_app(FakeBot(reg, rs)))
    d = c.get(f"/api/run/{ev.key}", headers=bearer()).json()
    assert (d["kind"], d["series"], d["expected_from"]) == ("recurring", KEY, "regulars") and d["series_name"] == "Barrow Deeps · Raid nights"
    assert [r["uid"] for r in d["not_answered"]] == [str(ms[2].discord_id)]
    answers = {r["uid"]: r["status"] for r in d["regulars"]}
    assert answers == {str(ms[0].discord_id): "in", str(ms[1].discord_id): "out", str(ms[2].discord_id): None}
    assert d["health"]["not_answered"] == [ms[2].display_name] and d["health"]["expected_from"] == "regulars"
    assert d["signups"] and next(s for s in d["signups"] if s["uid"] == str(outsider.discord_id))["status"] == "in", "D2: a non-regular's Join stays Join"


def test_officer_help_snapshot_names_regulars_and_member_record(reg, rs):
    ms = with_regulars(reg, 2)
    ev = next_run(reg, rs)
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    text = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=True))
    assert "not answered (regulars)" in text and ms[1].display_name in text and "series Barrow Deeps · Raid nights" in text
    assert f"{ms[0].display_name} ({ms[0].main.label}) Join" in text
    member = "\n".join(help_mod.member_lines(reg, rs, ms[0], "asker", True))
    assert f"regular on: Barrow Deeps · Raid nights ({ms[0].main.label})" in member
    public = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=False))
    assert "  regulars:" not in public


# ---------------------------------------------------------------- the lock: regulars weigh more, non-regulars stay Join

def test_seat_bonus_credits_a_regular_on_their_regular_character_only(reg, rs):
    ms = with_regulars(reg, 2)
    ev = next_run(reg, rs)
    reg.add_character(ms[1].discord_id, ms[1].display_name, "Altjex", "Mage", "Frost", None, False)
    outsider = real(reg, 5)
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    rc.set_signup(reg, rs, ev, ms[1], "Altjex", "in")
    rc.set_signup(reg, rs, ev, outsider, None, "in")
    players = rc.players_for(reg, ev)
    bonus = rc.seat_bonus(reg, rs, ev, players)
    without = dict(bonus)
    reg.regular_remove(ms[0].discord_id, KEY, "t")
    after = rc.seat_bonus(reg, rs, ev, players)
    assert bonus[ms[0].display_name] - after[ms[0].display_name] == RAID_WEIGHT_DEFAULTS["regular"], "the regular weight, on their regular character"
    assert bonus[ms[1].display_name] == after[ms[1].display_name] == without[ms[1].display_name], "signed with an alt that isn't their regular character: no weight"
    assert bonus[outsider.display_name] == after[outsider.display_name], "a non-regular gets none"


@pytest.mark.slow
def test_regulars_beat_equal_outsiders_at_lock(reg, rs):
    """D9 weighted: 12 joiners for 10 seats, the 2 outsiders joined FIRST and play the same DPS role at the same rank as
    the regulars — the regular weight seats the regulars."""
    opened(reg)
    specs = [("Warrior", "Protection"), ("Paladin", "Protection"), ("Priest", "Holy"), ("Shaman", "Restoration"),
             ("Rogue", "Combat"), ("Mage", "Fire"), ("Hunter", "Marksmanship"), ("Warlock", "Destruction"), ("Warrior", "Fury"), ("Shaman", "Enhancement")]
    regs, outsiders = [], []
    for i, (cls, spec) in enumerate(specs):  # every real member registers as a trial main
        reg.add_character(REAL + 100 + i, f"Regular{i}", f"Regu{chr(97 + i)}", cls, spec, None, True)
        reg.regular_add(REAL + 100 + i, KEY, "t")
        regs.append(reg.members[REAL + 100 + i])
    for i, (cls, spec) in enumerate(specs[4:6]):  # twins of two regular DPS: same class, spec and rank
        reg.add_character(REAL + 200 + i, f"Outsider{i}", f"Outs{chr(97 + i)}", cls, spec, None, True)
        outsiders.append(reg.members[REAL + 200 + i])
    ev = next_run(reg, rs)
    for m in outsiders + regs:  # the outsiders answer first
        rc.set_signup(reg, rs, ev, m, None, "in")
    _, r = rc.propose(reg, rs, ev, save=False)
    seated = {p.signup_name for p in r.selected}
    assert len(seated) == 10
    for out, twin in zip(outsiders, regs[4:6]):  # only the regular weight tells them apart (the outsider even joined first)
        assert out.display_name not in seated and twin.display_name in seated, f"{out.display_name} took {twin.display_name}'s seat"


def test_fill_asks_unanswered_regulars_before_other_mains(reg, rs):
    from conftest import lock_with_board

    ms = with_regulars(reg, 2)
    ev = next_run(reg, rs)
    roles = by_role(reg)
    ten = roles["tank"][:2] + roles["healer"][:2] + (roles["melee"] + roles["ranged"])[:6]
    join(reg, rs, ev, ten)
    lock_with_board(reg, rs, ev, [m.display_name for m in ten])
    dps = next(p for p in ev.roster.selected if p.role in ("melee", "ranged"))
    rc.free_seat(reg, rs, ev, dps.signup_name, "declined")
    cands = rc.fill_candidates(reg, rs, ev, rc.run_team(reg, ev))
    kinds = {a.discord_id: a.kind for a in cands}
    regular_dps = [m for m in ms if reg.profile.spec(m.main.cls, m.main.spec).role in ("melee", "ranged")]
    if regular_dps:
        assert all(kinds.get(m.discord_id) in (None, "regular") for m in ms)
    assert all(a.kind != "regular" or a.discord_id in {m.discord_id for m in ms} for a in cands), "the regular tier is the series' regulars"


# ---------------------------------------------------------------- a test run: puppets are virtual regulars

def test_test_run_treats_puppets_as_virtual_regulars(reg, rs):
    ms = with_regulars(reg, 2)
    ev = open_test_run(reg, rs)
    ids = {m.discord_id for m, _ in rc.run_regulars(reg, ev)}
    assert {m.discord_id for m in reg.test_members()} <= ids and not ({m.discord_id for m in ms} & ids)
    assert rc.regular_answers(reg, ev) == [] and ev.regulars == []
    assert all(not c.rosters for m in reg.test_members() for c in m.characters), "no membership written"


# ---------------------------------------------------------------- the four surfaces edit the same list

def test_web_route_adds_and_removes_regulars_and_members_step_down(reg, rs, monkeypatch):
    opened(reg)
    m = real(reg, 0)
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    c = TestClient(web.create_app(FakeBot(reg, rs)))
    r = c.post("/api/admin/raid/regulars", json={"raid": "Barrow Deeps", "member": m.display_name, "on": True}, headers=bearer())
    assert r.status_code == 200 and "is a regular of Barrow Deeps · Raid nights" in r.json()["message"]
    bad = c.post("/api/admin/raid/regulars", json={"raid": RID, "schedule": "pug", "member": m.display_name}, headers=bearer())
    assert bad.status_code == 400 and "ad hoc" in bad.json()["error"]
    rows = c.get("/api/rosters", headers=bearer()).json()
    series = next(r for r in rows["raids"] if r["id"] == RID)["series"]
    assert series[0]["key"] == KEY and [x["uid"] for x in series[0]["regulars"]] == [str(m.discord_id)]
    assert any(p["uid"] == str(m.discord_id) for p in rows["people"]) and not any(p["uid"] == str(reg.test_members()[0].discord_id) for p in rows["people"])
    raid = next(r for r in c.get("/api/raids", headers=bearer()).json()["raids"] if r["id"] == RID)
    dflt = next(s for s in raid["schedules"] if s["id"] == "default")
    pug = next(s for s in raid["schedules"] if s["id"] == "pug")
    assert (dflt["class"], dflt["regulars"], pug["class"], pug["source"]["lock_hours_before"]) == ("recurring", 1, "adhoc", "adhoc")
    off = c.post("/api/admin/raid/regulars", json={"raid": RID, "member": str(m.discord_id), "on": False}, headers=bearer())
    assert off.status_code == 200 and not reg.regulars(KEY)
    # the owner (the bearer token's viewer) stepping down from their own regular raid on the Me page
    reg.add_character(OWNER, "Owner", "Ownerchar", "Mage", "Fire", None, True)
    reg.regular_add(OWNER, KEY, "t")
    me = c.get("/api/me", headers=bearer()).json()
    assert me["regulars"] == [{"series": KEY, "label": "Barrow Deeps · Raid nights", "character": "Ownerchar", "cls": "Mage", "spec": "Fire", "role": "ranged"}]
    down = c.post("/api/me/regular/remove", json={"series": KEY}, headers=bearer())
    assert down.status_code == 200 and "stepped down" in down.json()["message"] and not reg.regulars(KEY)


def test_plain_text_regular_op(reg):
    opened(reg)
    m = real(reg, 0)
    op = ConfigOp(op="regular", target=RID, member=m.display_name, value="add")
    assert "becomes a regular" in configops.describe(reg, op)
    assert "is a regular of Barrow Deeps · Raid nights" in configops.apply(reg, op, "officer", is_owner=False)
    assert "Regulars of each recurring schedule" in configops.current_config_text(reg) and f"{m.display_name} ({m.main.label})" in configops.current_config_text(reg)
    # who may: becoming a regular is the officers' call; stepping down is the member's own
    add_self = ConfigOp(op="regular", target=RID, value="add")
    assert configops.bind_self(reg, add_self, m.discord_id)[0] == "members"
    down_self = ConfigOp(op="regular", target=RID, value="remove")
    assert configops.bind_self(reg, down_self, m.discord_id) == ("self", None)
    assert "no longer a regular" in configops.apply(reg, down_self, m.display_name, is_owner=False)
    allowed, refused = configops.authorize(reg, [ConfigOp(op="regular", target=RID, value="add")], m.discord_id, officer=False)
    assert not allowed and refused


def test_mcp_set_regular_calls_the_route():
    import json

    import httpx

    from oibot_gm import mcp_server as ms

    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token="t" * 64, transport=httpx.MockTransport(handler)))
    try:
        assert ms.set_regular("Barrow Deeps", "Ana", character="Anabelle") == "ok"
        assert ms.set_regular(RID, "Ana", on=False, schedule="alt") == "ok"
    finally:
        ms.use(None)
    assert seen == [("/api/admin/raid/regulars", {"raid": "Barrow Deeps", "schedule": "default", "member": "Ana", "character": "Anabelle", "on": True}),
                    ("/api/admin/raid/regulars", {"raid": RID, "schedule": "alt", "member": "Ana", "character": "", "on": False})]


def test_me_view_and_names_never_show_series_keys(reg):
    opened(reg)
    m = real(reg, 0)
    reg.regular_add(m.discord_id, KEY, "t")
    assert reg.series_label(KEY) == "Barrow Deeps · Raid nights"
    assert reg.series_label(f"{RID}:gone") == "Barrow Deeps · gone (removed schedule)"
