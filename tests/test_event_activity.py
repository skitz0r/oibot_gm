"""The event page's Bot activity tab (design 2026-11-28-event-activity-tab): event_activity.activity_json over what an
event stores (Confirm asks, fill / pre-lock / role switch asks, callouts) and RaidEvent.dms, the record the bot now
keeps of its opening DMs, reminders, an officer's Remind and every DM it could not deliver (raidcycle.record_dms, the one
writer). Every word a row prints is the server's; the route is officers' only; nothing on the tab's path sends a DM."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import first_slot, join, lock_with_board, weekly
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import event_activity as ea
from oibot_gm import raidcycle as rc

RID, HYJAL = "barrow_deeps", "hyjal_summit_forever"
KEY = "barrow-deeps"
REAL = 476_000_000_000_000_000
SPECS = [("Warrior", "Protection"), ("Priest", "Holy"), ("Shaman", "Restoration"), ("Rogue", "Combat"), ("Mage", "Fire"),
         ("Hunter", "Marksmanship"), ("Druid", "Balance"), ("Warlock", "Destruction")]
NAMES = ["Brannoc", "Mira", "Ona", "Kael", "Ana", "Jex", "Tovi", "Rell", "Sabe", "Quill", "Dorn", "Peli"]
FRONT = Path(__file__).resolve().parents[1] / "frontend" / "src"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def real(reg, n: int):
    cls, spec = SPECS[n % len(SPECS)]
    uid = REAL + n
    reg.add_character(uid, f"Player{n}", NAMES[n % len(NAMES)] + "abcdefghij"[n // len(NAMES)], cls, spec, None, True)
    return reg.members[uid]


def scheduled(reg, rs, n: int = 8):
    """A schedule's event a day or more out with `n` regulars (real members), open."""
    weekly(reg, RID, ["Tue 19:30"])
    ms = [real(reg, i) for i in range(n)]
    for m in ms:
        reg.regular_add(m.discord_id, KEY, "officer")
    ev = rc.open_run(reg, rs, RID, first_slot(reg, RID), by="t")
    return ev, ms


def locked(reg, rs, n: int = 8):
    ev, ms = scheduled(reg, rs, n)
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    return ev, ms


def ask(m, *, kind="sub", phase="fill", hours=2.0, answer=None, by=None, reason="short 1 healer"):
    later = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
    a = rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind=kind, role="healer", character=m.main.label,
                   spec=m.main.spec, reason=reason, expires_at=later, phase=phase, answer=answer, answered_by=by)
    if answer:
        a.answered_at = rc.now()
    return a


def rows_of(j, kind=None):
    return [r for r in j["pending"] + j["history"] if kind is None or r["kind"] == kind]


def by_person(rows):
    return {r["person"]["display_name"]: r for r in rows if r["person"]}


# ---------------------------------------------------------------- Confirm asks

def test_a_locked_events_confirmations_in_each_state(reg, rs):
    ev, ms = locked(reg, rs, 7)
    for m in ms:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    for m, answer, by in ((ms[1], "yes", None), (ms[2], "no", None), (ms[3], "expired", None), (ms[4], "unrostered", "the lock"),
                          (ms[5], "cancelled", None), (ms[6], "expired", "end")):
        reg.close_placement_ask(m.discord_id, ev.team, answer, by)
    j = ea.activity_json(reg, rs, ev)
    assert j["pending_count"] == 1 and [r["kind"] for r in j["pending"]] == ["confirm"]
    row = j["pending"][0]
    assert row["person"]["display_name"] == ms[0].display_name and row["kind_label"] == "Confirm"
    assert row["person"]["cls"] == ms[0].main.cls and row["person"]["spec"] == ms[0].main.spec, "the spec icon beside the person"
    assert [t["label"] for t in row["times"]] == ["Sent", "Closes"] and row["times"][1]["when"] == reg.local12(ev.confirm_by)
    assert row["times"][1]["rel"].startswith("in ") and row["withdraw"] is None, "a Confirm row has no button: the board takes someone off"
    assert row["where"] is None, "one raid, one roster: no raid or roster named"
    done = by_person(j["history"])
    assert {n: done[m.display_name]["outcome"] for n, m in enumerate(ms[1:], 1)} == {
        1: "said yes", 2: "can't make it", 3: "no answer in time", 4: "closed when the event locked again without them",
        5: "closed when the event was cancelled", 6: "closed when the event ended"}
    assert done[ms[1].display_name]["tone"] == "yes" and done[ms[2].display_name]["tone"] == "no"
    assert [t["label"] for t in done[ms[1].display_name]["times"]] == ["Sent", "Answered"]
    assert [t["label"] for t in done[ms[4].display_name]["times"]] == ["Sent", "Closed"]


# ---------------------------------------------------------------- fill, pre-lock and role switch asks

def test_fill_asks_pending_answered_expired_and_withdrawn_with_who(reg, rs):
    ev, ms = locked(reg, rs, 6)
    extra = [real(reg, 20 + i) for i in range(5)]
    ev.fill_asks += [ask(extra[0], hours=3), ask(extra[1], answer="yes"), ask(extra[2], answer="no"),
                     ask(extra[3], answer="expired"), ask(extra[4], answer="withdrawn", by="Kara")]
    rs.save(ev, "asks")
    j = ea.activity_json(reg, rs, ev)
    (row,) = [r for r in j["pending"] if r["kind"] == "fill"]
    assert row["kind_label"] == "Open seat" and row["detail"] == "short 1 healer"
    assert row["withdraw"] == {"path": f"/api/run/{ev.key}/withdraw", "uid": str(extra[0].discord_id), "label": "Withdraw"}
    assert [t["label"] for t in row["times"]] == ["Sent", "Closes"]
    done = by_person(rows_of(j, "fill"))
    assert [done[m.display_name]["outcome"] for m in extra[1:]] == ["said yes", "said no", "no answer in time", "withdrawn by Kara"]
    assert all(done[m.display_name]["withdraw"] is None for m in extra[1:])


def test_a_pre_lock_ask_on_an_open_event_and_a_role_switch_ask(reg, rs):
    ev, ms = scheduled(reg, rs, 6)
    ev.fill_asks += [ask(ms[0], phase="prelock", hours=5), ask(ms[1], kind="offspec", phase="prelock", hours=1)]
    rs.save(ev, "asks")
    j = ea.activity_json(reg, rs, ev)
    assert [(r["kind"], r["kind_label"]) for r in j["pending"]] == [("swap", "Role switch"), ("prelock", "Could you come?")], \
        "soonest deadline first"
    assert all(r["withdraw"] and r["withdraw"]["path"] == f"/api/run/{ev.key}/withdraw" for r in j["pending"])
    assert j["pending_count"] == 2


def test_pending_by_deadline_and_history_newest_first(reg, rs):
    ev, ms = locked(reg, rs, 6)
    reg.add_placement_ask(ms[0].discord_id, ev.team, ms[0].main.label, "lock")  # closes at confirm_by: an hour before the start
    soon, late = real(reg, 30), real(reg, 31)
    ev.fill_asks += [ask(late, hours=200), ask(soon, hours=0.5)]
    old, new = ask(real(reg, 32), answer="no"), ask(real(reg, 33), answer="yes")
    old.answered_at, new.answered_at = "2026-01-01T10:00:00+00:00", "2026-01-02T10:00:00+00:00"
    ev.fill_asks += [old, new]
    rs.save(ev, "asks")
    j = ea.activity_json(reg, rs, ev)
    assert [r["person"]["display_name"] for r in j["pending"]] == [soon.display_name, ms[0].display_name, late.display_name]
    hist = [r["person"]["display_name"] for r in j["history"] if r["kind"] == "fill"]
    assert hist == [new.display_name, old.display_name]


# ---------------------------------------------------------------- what the bot now records

def test_the_opening_dm_and_the_reminder_are_one_row_each_with_counts_and_names(reg, rs):
    ev, ms = scheduled(reg, rs, 4)
    bot = NightBot(reg, rs)
    bot.dm_fail.add(ms[3].discord_id)
    team = rc.run_team(reg, ev)
    run(bot.dm_on_open(reg, rs, ev, team))
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    run(bot.post_health(reg, rs, ev, None, nudge=True))
    j = ea.activity_json(reg, rs, ev)
    assert j["unrecorded"] is None, "a new event is recorded from its opening"
    (opening,) = rows_of(j, "open")
    assert opening["kind_label"] == "Sheet Opening DM" and opening["summary"] == "Sent to 3 people."
    assert sorted(p["display_name"] for p in opening["people"]) == sorted(m.display_name for m in ms[:3])
    assert opening["failed_summary"] == "Not delivered to 1 person." and [p["display_name"] for p in opening["failed_people"]] == [ms[3].display_name]
    (reminder,) = rows_of(j, "reminder")
    assert reminder["kind_label"] == "Signup Reminder" and reminder["summary"] == "Sent to 2 people."
    assert sorted(p["display_name"] for p in reminder["people"]) == sorted(m.display_name for m in ms[1:3])
    assert reminder["failed_summary"] == "Not delivered to 1 person."
    assert all(t["label"] == "Sent" for r in (opening, reminder) for t in r["times"])


def test_an_undeliverable_ask_and_confirm_are_marked_not_delivered(reg, rs):
    ev, ms = locked(reg, rs, 6)
    bot = NightBot(reg, rs)
    outsider = real(reg, 40)
    bot.dm_fail |= {outsider.discord_id, ms[0].discord_id}
    team = rc.run_team(reg, ev)
    deadline = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="seconds")
    run(bot._send_asks(reg, rs, ev, team, [ask(outsider)], phase="fill", approved_by="Kara", deadline=deadline))
    sg = ev.signups[str(ms[0].discord_id)]
    assert run(bot.confirm_one(reg, ev, sg, 0, 0, "lock")) is False
    rs.save(ev, "sent")
    j = ea.activity_json(reg, rs, ev)
    rows = by_person(j["pending"])
    assert rows[outsider.display_name]["delivery"] == "Not delivered, so it waits on their Me page."
    assert rows[ms[0].display_name]["kind"] == "confirm" and rows[ms[0].display_name]["delivery"] == "Not delivered, so it waits on their Me page."
    assert {d["kind"] for d in ev.dms} == {"ask", "confirm"}


def test_an_officers_remind_is_one_row_with_who_pressed_it(reg, rs):
    ev, ms = locked(reg, rs, 5)
    bot = NightBot(reg, rs)
    for m in ms:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    bot.dm_fail.add(ms[4].discord_id)
    run(bot.remind(reg, rs, ev, by="Kara"))
    j = ea.activity_json(reg, rs, ev)
    (row,) = rows_of(j, "remind")
    assert row["kind_label"] == "Remind the unanswered" and row["summary"] == "Kara sent it to 4 people."
    assert row["failed_summary"] == "Not delivered to 1 person." and row["failed_people"][0]["display_name"] == ms[4].display_name


def test_a_callout_from_a_dm_card_is_a_history_row_with_its_note(reg, rs):
    ev, ms = locked(reg, rs, 6)
    rc.callout(reg, rs, ev, ms[2], rc.run_team(reg, ev), "car broke down")
    j = ea.activity_json(reg, rs, ev)
    (row,) = rows_of(j, "callout")
    assert row["kind_label"] == "Can't make it" and row["detail"] == "car broke down" and row["person"]["display_name"] == ms[2].display_name
    assert row["outcome"].endswith("h before the start, after the lock") or row["outcome"].endswith("h before the start")
    assert row["times"][0]["label"] == "Said"


def test_the_record_is_capped_and_needs_someone(reg, rs):
    ev, ms = scheduled(reg, rs, 2)
    assert rc.record_dms(ev, "open", [], []) is None and ev.dms == []
    for _ in range(rc.DMS_KEPT + 5):
        rc.record_dms(ev, "reminder", [ms[0].discord_id])
    assert len(ev.dms) == rc.DMS_KEPT
    with pytest.raises(ValueError):
        rc.record_dms(ev, "courtesy", [ms[0].discord_id])


# ---------------------------------------------------------------- raids and rosters

def test_a_multi_raid_event_names_the_raid_and_a_single_raid_event_does_not(reg, rs):
    ms = [real(reg, 90 + i) for i in range(4)]
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, HYJAL, start, by="t", lineup=[{"raid": RID, "rosters": 1}])
    parts = rc.event_parts(rs, ev)
    assert len(parts) == 2
    parts[0].fill_asks.append(ask(ms[0], phase="prelock"))
    parts[1].fill_asks.append(ask(ms[1], phase="prelock"))
    for p in parts:
        rs.save(p, "asks")
    j = ea.activity_json(reg, rs, parts[1])
    assert j["key"] == ev.key, "any raid's key answers as its event"
    assert sorted(r["where"] for r in j["pending"]) == sorted([reg.raid_def(HYJAL)["name"], reg.raid_def(RID)["name"]])
    assert all(r["withdraw"]["path"].endswith(f"/{r['id'].split(':')[1]}/withdraw") for r in j["pending"]), "each row withdraws on its raid"
    single, sms = scheduled(reg, rs, 2)
    single.fill_asks.append(ask(sms[0], phase="prelock"))
    assert [r["where"] for r in ea.activity_json(reg, rs, single)["pending"]] == [None]


def test_a_raid_of_two_rosters_names_the_roster_on_a_confirm(reg, rs):
    ev, ms = locked(reg, rs, 8)
    half = [m.display_name for m in ms]
    size = rc.run_size(reg, ev)
    ev.rosters = rc.board_rosters(reg, ev, [half[:4], half[4:]], size) + rc.board_rosters(reg, ev, [], size)
    if len(ev.all_rosters) < 2:
        pytest.skip("this profile's board_rosters makes one roster")
    rs.save(ev, "two rosters")
    reg.add_placement_ask(ms[0].discord_id, ev.team, ms[0].main.label, "lock")
    (row,) = ea.activity_json(reg, rs, ev)["pending"]
    assert row["where"] == "roster 1"


# ---------------------------------------------------------------- older events, empty states, a finished event

def test_an_old_event_shows_what_it_has_and_one_sentence(reg, rs):
    ev, ms = locked(reg, rs, 4)
    ev.dms_whole = False  # a file from before the record: no `dms_whole` key reads False
    ev.fill_asks.append(ask(real(reg, 50), answer="yes"))
    rs.save(ev, "old")
    j = ea.activity_json(reg, rs, ev)
    assert j["unrecorded"] == ea.WORDS["unrecorded"]
    assert [r["outcome"] for r in rows_of(j, "fill")] == ["said yes"]
    stored = rc.RaidEvent.model_validate({k: v for k, v in ev.model_dump().items() if k not in ("dms", "dms_whole")})
    assert stored.dms == [] and stored.dms_whole is False, "a stored event loads unchanged"


def test_empty_states_name_what_is_missing(reg, rs):
    ev, _ms = scheduled(reg, rs, 2)
    j = ea.activity_json(reg, rs, ev)
    assert j["pending"] == [] and j["history"] == [] and j["pending_count"] == 0
    assert j["empty_pending"] == "The bot is not waiting on anyone for this event."
    assert j["empty_history"] == "The bot has sent no DMs for this event yet."
    assert j["filters"]["kinds"] == [] and j["filters"]["all_kinds"] == "Every kind" and j["filters"]["everyone"] == "Everyone"


def test_filters_list_the_kinds_and_people_present(reg, rs):
    ev, ms = locked(reg, rs, 4)
    reg.add_placement_ask(ms[0].discord_id, ev.team, ms[0].main.label, "lock")
    ev.fill_asks.append(ask(ms[1]))
    j = ea.activity_json(reg, rs, ev)
    assert [k["value"] for k in j["filters"]["kinds"]] == ["confirm", "fill"]
    assert {p["label"] for p in j["filters"]["people"]} == {ms[0].display_name, ms[1].display_name}
    assert all(r["uids"] for r in rows_of(j))


def test_a_paused_event_says_so_above_the_list(reg, rs):
    ev, _ms = locked(reg, rs, 4)
    ev.autofill_paused, ev.autofill_paused_by = True, "Kara"
    assert ea.activity_json(reg, rs, ev)["paused"] == ["Automated Fill Requests are paused by Kara."]


def test_a_finished_events_tab_is_read_only_history(reg, rs):
    ev, ms = locked(reg, rs, 4)
    ev.fill_asks.append(ask(real(reg, 60)))
    rs.save(ev, "asks")
    rc.end_run(reg, rs, ev, "t")
    j = ea.activity_json(reg, rs, ev)
    assert j["read_only"] is True and j["pending"] == []
    assert all(r["withdraw"] is None for r in j["history"])


def test_relative_times_read_both_ways(reg):
    now = reg.now_local()
    assert ea.relative(reg, (now + timedelta(hours=3, minutes=5)).isoformat(), now) == "in 3 h"
    assert ea.relative(reg, (now - timedelta(minutes=20)).isoformat(), now) == "20 min ago"
    assert ea.relative(reg, (now - timedelta(days=4)).isoformat(), now) == "4 days ago"
    assert ea.relative(reg, now.isoformat(), now) == "just now"


# ---------------------------------------------------------------- the route

def test_the_route_is_the_read_model_for_officers(web_bot, reg, rs):
    ev, ms = locked(reg, rs, 4)
    ev.fill_asks.append(ask(ms[1]))
    rs.save(ev, "asks")
    r = web_bot.c.get(f"/api/event/{ev.key}/activity", headers=H())
    assert r.status_code == 200, r.text
    assert r.json()["pending_count"] == 1 and r.json()["pending"][0]["kind_label"] == "Open seat"
    assert web_bot.c.get("/api/event/nope/activity", headers=H()).status_code == 404


def test_a_member_gets_403(reg, rs, monkeypatch):
    from test_member_events import as_user, member_client

    ev, ms = locked(reg, rs, 4)
    c = member_client(reg, rs, monkeypatch)
    r = c.get(f"/api/event/{ev.key}/activity", headers=as_user(c, ms[0].discord_id))
    assert r.status_code == 403, r.text


def test_withdraw_from_a_row_closes_the_ask(web_bot, reg, rs):
    ev, ms = locked(reg, rs, 4)
    out = real(reg, 70)
    ev.fill_asks.append(ask(out))
    rs.save(ev, "asks")
    row = web_bot.c.get(f"/api/event/{ev.key}/activity", headers=H()).json()["pending"][0]
    r = web_bot.c.post(row["withdraw"]["path"], json={"uid": row["withdraw"]["uid"]}, headers=H())
    assert r.status_code == 200, r.text
    j = web_bot.c.get(f"/api/event/{ev.key}/activity", headers=H()).json()
    assert j["pending"] == [] and rows_of(j, "fill")[0]["outcome"].startswith("withdrawn by ")


def test_nothing_on_the_tabs_path_sends_a_dm(web_bot, reg, rs, monkeypatch):
    ev, ms = locked(reg, rs, 4)
    for m in ms:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    ev.fill_asks.append(ask(real(reg, 80)))
    rs.save(ev, "asks")

    async def no_dm(*a, **k):
        raise AssertionError("the Bot activity tab sent a DM")
    monkeypatch.setattr(type(web_bot.bot), "send_member_card", no_dm)
    before = {k: list(v) for k, v in web_bot.bot.dms.items()}
    assert web_bot.c.get(f"/api/event/{ev.key}/activity", headers=H()).status_code == 200
    assert web_bot.bot.dms == before
    src = (Path(ea.__file__)).read_text()
    assert not re.search(r"send_member|\.send\(|notify\(|\.save\(", src), "the read model sends and writes nothing"


def test_every_row_word_is_free_of_code_nouns(reg, rs):
    ev, ms = locked(reg, rs, 6)
    reg.add_placement_ask(ms[0].discord_id, ev.team, ms[0].main.label, "lock")
    ev.fill_asks += [ask(ms[1]), ask(ms[2], answer="withdrawn", by="Kara")]
    rc.record_dms(ev, "reminder", [ms[3].discord_id], [ms[4].discord_id])
    j = ea.activity_json(reg, rs, ev)
    words = [w for r in rows_of(j) for w in (r["kind_label"], r["summary"], r["failed_summary"], r["outcome"], r["delivery"])
             + tuple(t["label"] for t in r["times"]) if w]
    words += [j["empty_pending"], j["empty_history"], ea.WORDS["unrecorded"]]
    for w in words:
        assert not re.search(r"\b(?:run|runs|solver|seated|pool|ev|rc|series)\b", w, re.I), w
        assert ev.key not in w, w


# ---------------------------------------------------------------- the page

def test_the_event_page_has_two_tabs_with_the_count_in_the_url_hash():
    page = (FRONT / "pages" / "Events.tsx").read_text()
    tabs = (FRONT / "components" / "events" / "BotActivity.tsx").read_text()
    assert "EventTabs" in page
    assert ">Event<" in tabs and "Bot activity" in tabs and "#bot-activity" in tabs and "pending_count" in tabs
    assert "/activity" in tabs and "replaceState" in tabs


def test_asks_and_callouts_is_gone_from_the_events_more_menu():
    more = (FRONT / "components" / "events" / "EventMore.tsx").read_text()
    assert "Asks and callouts" not in more and "Detail" not in more


def test_the_tab_prints_only_the_servers_words():
    tabs = (FRONT / "components" / "events" / "BotActivity.tsx").read_text()
    for w in ("said yes", "said no", "no answer in time", "Not delivered", "Sent to", "Open seat", "Could you come?", "Role switch",
              "The bot is not waiting", "The bot has sent no DMs", "were not recorded", "withdrawn by", "Every kind", "Everyone"):
        assert w not in tabs, w
    for k in ("kind_label", "outcome", "delivery", "summary", "failed_summary", "empty_pending", "empty_history", "unrecorded",
              "all_kinds", "everyone", "withdraw.label"):
        assert k in tabs, k
