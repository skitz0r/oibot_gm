"""Phase 4, Task 5: the Schedules and Raids pages' read models (schedule_view.py, raid_page.py, web/api_schedules.py)
— the list and one schedule in words, a raid's Rules / Priority / Readiness, regulars by schedule id, and every
schedule or raid Save previewing its box first. Every fact comes from the existing readers; a schedule id travels only
as an id or an href, never in a word."""
from __future__ import annotations

import re

import pytest
from conftest import lock_with_board, weekly  # noqa: F401
from test_events_api import HYJAL, RID, officer_client, people, tuesday, words_of  # noqa: F401 — the fixtures
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS
from oibot_gm.registry import RESERVED_SCHEDULE_IDS, SCHEDULE_TIMING

ONY = "onyxias_lair"


@pytest.fixture(autouse=True)
def _no_board_solves(monkeypatch):
    from oibot_gm import event_board

    monkeypatch.setattr(event_board.BoardCache, "start", lambda self, *a, **k: None)


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def onyxia(reg, active: bool = False) -> str:
    reg.set_schedule("ony", {"kind": "lockout", "days": "1", "time": "20:00", "name": "Sunday Onyxia"}, "t", raid=ONY)
    if not active:
        reg.set_schedule("ony", {"active": False}, "t")
    return "ony"


def post(web_bot, path, body):
    return web_bot.c.post(path, json=body, headers=H())


def change(web_bot, changes, **kw):
    return post(web_bot, "/api/ops/change", {"changes": changes, **kw})


def no_ids_in_words(reg, j, *ids):
    j = {k: v for k, v in j.items() if k not in ("stored", "rev")}  # machine form for the editor, never read
    for w in words_of(j):
        for sid in ids:
            assert not re.search(rf"\b{re.escape(sid)}\b", w), (sid, w)
        assert not re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b(?! ?[AP]M)", w), f"a 24-hour time a person reads: {w}"


# ---------------------------------------------------------------- the brief's tests

def test_schedules_are_grouped_and_read_in_words(officer_client, reg):
    tuesday(reg)
    onyxia(reg)
    j = officer_client.get("/api/schedules").json()
    assert j["active"] == [{"id": "tue", "name": "Tuesday raid night", "repeats": "Every Tuesday at 6:30 PM"}]
    assert j["paused"] == [{"id": "ony", "name": "Sunday Onyxia", "repeats": "Day 1 of each lockout at 8:00 PM"}]


def test_a_schedule_reads_by_its_sections(officer_client, reg, rs):
    tuesday(reg)
    j = officer_client.get("/api/schedules/tue").json()
    assert [f["label"] for f in j["details"]] == ["Repeats", "First event", "Priority", "Confirmation", "Open seats after the lock", "Answers"]
    assert j["details"][0]["value"] == "Every Tuesday at 6:30 PM" and j["details"][-1]["value"] == PAGE_WORDS["answers_one"]
    assert j["chip"] == "active" and j["gone"] is None
    assert [(s["name"], s["seats"], s["rosters"], s["time_words"]) for s in j["lineup"]] == [("Hyjal Summit", 20, 1, "6:30 PM"), ("Barrow Deeps", 10, 2, "9:30 PM")]
    assert len(j["next"]) == 3 and all(n["key"] is None for n in j["next"])
    starts = rc.next_runs_of(reg, HYJAL, reg.schedule_entry("tue"), 3)
    assert [n["starts"] for n in j["next"]] == [reg.local12(t) for t in starts]
    assert j["details"][1]["value"] == reg.local12(starts[0])
    assert [f["label"] for f in j["timing"]] == ["Sheet opens", "Reminder", "Locks", "Confirm by", "Open events keeping their own timing"]
    assert j["timing"][0]["value"] == "5 days before" and j["timing"][2]["value"] == "24 hours before" and j["timing"][-1]["value"] == "0"
    r = j["regulars"]
    assert r["words"] == "0 for 20 seats" and r["expected"] == "Every main" and r["short"] is None and r["rows"] == []
    assert set(j["boxes"]) == {"pause", "remove"} and j["boxes"]["pause"]["verb"] == "Pause" and j["boxes"]["remove"]["danger"]
    assert j["rev"] == reg.config_rev("schedule:tue")
    no_ids_in_words(reg, j, "tue")


def test_a_stale_schedule_save_is_refused(web_bot, reg):
    """Review Focus 4: two officers edit the same schedule from the same rev — the first Save applies, the second
    carries the stale config:schedule:<id> rev and answers 409; the schedule holds the first one's value."""
    tuesday(reg)
    rev = web_bot.c.get("/api/schedules/tue", headers=H()).json()["rev"]
    revs = {"config:schedule:tue": rev}
    a = change(web_bot, [{"kind": "schedule", "target": "tue", "fields": {"lock_hours_before": 30}}], apply=True, confirmed=True, revs=revs)
    assert a.status_code == 200 and a.json()["applied"], a.text
    b = change(web_bot, [{"kind": "schedule", "target": "tue", "fields": {"lock_hours_before": 12}}], apply=True, confirmed=True, revs=revs)
    assert b.status_code == 409, b.text
    assert float(reg.schedule_def(HYJAL, "tue")["lock_hours_before"]) == 30, "nothing overwritten"


def test_a_raid_row_names_where_it_is_played(officer_client, reg):
    tuesday(reg)
    rows = {r["id"]: r for r in officer_client.get("/api/raids").json()["raids"]}
    page = rows[RID]["page"]
    played = next(f for f in page["details"] if f["label"] == "Played in")
    assert played["value"] == "Tuesday raid night" and played["href"] == "/schedules/tue"
    assert played["links"] == [{"name": "Tuesday raid night", "href": "/schedules/tue"}]
    assert [f["label"] for f in page["details"]] == ["Size", "Groups", "Lockout", "Length", "First opens", "Played in"]
    assert [f["value"] for f in rows[HYJAL]["page"]["details"][:5]] == ["20 players", "4 of 5", "7 days", "About 3 hours", PAGE_WORDS["open_now"]]

    def keys(obj):
        if isinstance(obj, dict):
            return set(obj) | {k for v in obj.values() for k in keys(v)}
        if isinstance(obj, list):
            return {k for v in obj for k in keys(v)}
        return set()
    for r in rows.values():
        assert not (set(r) & set(SCHEDULE_TIMING)), "no sheet timing on a raid row"
        assert not (keys(r["page"]) & set(SCHEDULE_TIMING)) and not r["slots"], "no timing and no run time on the raid"
        no_ids_in_words(reg, r["page"], "tue")


def test_schedule_and_raid_saves_always_preview(web_bot, reg):
    tuesday(reg)
    r = change(web_bot, [{"kind": "schedule", "target": "tue", "fields": {"lock_hours_before": 30}}], apply=True).json()
    assert r.get("needs_review") and r["described"]["box"]["verb"] == "Save" and not r.get("applied")
    assert float(reg.schedule_def(HYJAL, "tue")["lock_hours_before"]) == 24, "nothing written before the box"
    r = change(web_bot, [{"kind": "raid", "target": HYJAL, "fields": {"notes": "Bring flasks"}}], apply=True).json()
    assert r.get("needs_review") and r["described"]["box"]["title"] == "Save Hyjal Summit?"
    r = change(web_bot, [{"kind": "schedule", "target": "tue", "fields": {"active": False}}], apply=True).json()
    assert r.get("needs_review") and r["described"]["box"]["verb"] == "Pause"
    r = change(web_bot, [{"kind": "schedule_remove", "target": "tue"}], apply=True).json()
    assert r.get("needs_review") and r["described"]["box"]["verb"] == "Remove schedule"
    assert reg.schedule_entry("tue")["active"], "a pause waits for its box too"


# ---------------------------------------------------------------- create vs edit, round trip, open events

def test_what_the_editor_sends_back_unchanged_is_no_edit(web_bot, reg):
    tuesday(reg)
    onyxia(reg)
    for sid in ("tue", "ony"):
        j = web_bot.c.get(f"/api/schedules/{sid}", headers=H()).json()
        before = reg.store.root.joinpath(reg.key, "guild.yaml").read_text()
        r = change(web_bot, [{"kind": "schedule", "target": sid, "fields": j["stored"]}], apply=True, confirmed=True,
                   revs={f"config:schedule:{sid}": j["rev"]})
        assert r.status_code == 200 and not r.json().get("applied") and r.json()["summary"] == "nothing to change", r.text
        assert reg.store.root.joinpath(reg.key, "guild.yaml").read_text() == before
    j = web_bot.c.get("/api/schedules/tue", headers=H()).json()
    edited = {**j["stored"], "lock_hours_before": 30}
    r = change(web_bot, [{"kind": "schedule", "target": "tue", "fields": edited}], apply=True, confirmed=True, revs={"config:schedule:tue": j["rev"]})
    assert r.status_code == 200 and len(r.json()["applied"]) == 1, r.text
    assert float(reg.schedule_def(HYJAL, "tue")["lock_hours_before"]) == 30


def test_a_new_schedule_on_a_taken_id_is_refused(web_bot, reg):
    tuesday(reg)
    r = change(web_bot, [{"kind": "schedule", "target": "tue", "fields": {"kind": "weekly", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Wed 19:00"]}}],
               apply=True, confirmed=True)
    assert r.status_code in (400, 200) and "Another schedule is already called" in r.text
    assert reg.schedule_entry("tue")["raid"] == HYJAL


def test_editing_run_times_with_an_open_event_says_it_keeps_its_timing(web_bot, reg, rs):
    sid = tuesday(reg)
    t = rc.next_runs_of(reg, HYJAL, reg.schedule_entry(sid), 1)[0]
    ev = rc.open_run(reg, rs, HYJAL, t, by="t", schedule=sid)
    j = web_bot.c.get(f"/api/schedules/{sid}", headers=H()).json()
    assert j["timing"][-1] == {"label": "Open events keeping their own timing", "value": "1"}
    assert j["next"][0]["key"] == ev.key, "a night with an event links to it"
    r = change(web_bot, [{"kind": "schedule", "target": sid, "fields": {"lock_hours_before": 30}}], apply=True).json()
    keeps = f"The {reg.local12(ev.start, '%a %d %b')} event is already open and keeps its timing."
    assert keeps in r["described"]["box"]["sentences"]
    r = change(web_bot, [{"kind": "schedule", "target": sid, "fields": {"name": "Tuesdays"}}], apply=True).json()
    assert not any("keeps its timing" in x for x in r["described"]["box"]["sentences"]), "only a time field says so"


def test_a_paused_schedule_has_no_next_events(officer_client, reg):
    onyxia(reg)
    j = officer_client.get("/api/schedules/ony").json()
    assert j["chip"] == "paused" and j["next"] == [] and j["next_empty"] == PAGE_WORDS["paused_next"]
    labels = [f["label"] for f in j["details"]]
    assert "Next event if resumed" in labels and "First event" not in labels
    assert j["details"][0]["value"] == "Day 1 of each 5-day lockout at 8:00 PM"
    assert set(j["boxes"]) == {"resume", "remove"} and j["boxes"]["resume"]["verb"] == "Resume"
    assert "Open events keeping their own timing" not in [f["label"] for f in j["timing"]]


@pytest.mark.parametrize("gone", [RID, HYJAL], ids=["a later raid left", "the first raid left"])
def test_a_schedule_whose_raid_left_the_game_is_listed_and_removed(web_bot, reg, monkeypatch, gone):
    """Carried 5: the list shows it, its page says so in one sentence and shows no next events, its Remove works."""
    sid = tuesday(reg)
    kept = {k: v for k, v in reg.profile.raids.items() if k != gone}
    monkeypatch.setattr(reg.profile, "raids", kept)
    monkeypatch.setattr(reg.base_profile, "raids", kept)
    j = web_bot.c.get("/api/schedules", headers=H()).json()
    assert [x["id"] for x in j["active"]] == [sid]
    one = web_bot.c.get(f"/api/schedules/{sid}", headers=H()).json()
    name = "Barrow Deeps" if gone == RID else "Hyjal Summit Forever"  # a raid no profile names: its id in words
    assert one["gone"] == f"{name} is no longer a raid of this game, so this schedule opens no events."
    assert one["next"] == [] and one["next_empty"] == one["gone"]
    box = one["boxes"]["remove"]
    assert box["verb"] == "Remove schedule"
    r = change(web_bot, [{"kind": "schedule_remove", "target": sid}], apply=True, confirmed=True, revs={f"config:schedule:{sid}": one["rev"]})
    assert r.status_code == 200 and r.json()["applied"], r.text
    assert sid not in {s["id"] for s in reg.all_schedules()}


def test_reserved_ids_come_from_the_server(officer_client, reg):
    """Carried 6: the ids no new schedule takes reach the site through the API; no page file holds a copy."""
    from pathlib import Path

    assert officer_client.get("/api/schedules").json()["reserved_ids"] == list(RESERVED_SCHEDULE_IDS)
    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    assert officer_client.get("/api/meta").json()["reserved_schedule_ids"] == list(RESERVED_SCHEDULE_IDS)
    for f in [*src.rglob("*.tsx"), *src.rglob("*.ts")]:
        for lst in re.findall(r"\[[^\[\]]*\]", f.read_text()):
            assert not ('"adhoc"' in lst and '"after"' in lst), f"{f.name} copies the reserved ids: {lst}"


# ---------------------------------------------------------------- regulars by schedule id

def test_regulars_are_by_schedule_id_and_removal_shows_its_box(web_bot, reg):
    sid = tuesday(reg)
    ms = people(reg, 3)
    r = post(web_bot, "/api/admin/raid/regulars", {"schedule": sid, "uid": str(ms[0].discord_id), "on": True})
    assert r.status_code == 200, r.text
    j = web_bot.c.get(f"/api/schedules/{sid}", headers=H()).json()
    reg_ = j["regulars"]
    assert reg_["words"] == "1 for 20 seats" and "expected" not in reg_ and reg_["short"] == {"tank": 1, "healer": 5}, "a Protection Warrior: one of two tanks"
    assert reg_["outlook"] == PAGE_WORDS["short_every_week"]
    row = reg_["rows"][0]
    assert row["uid"] == str(ms[0].discord_id) and row["box"]["title"] == f"Take {ms[0].display_name} off the regulars?"
    assert str(ms[0].discord_id) not in {p["uid"] for p in j["people"]}
    body = {"schedule": sid, "uid": row["uid"], "on": False, "rev": row["rev"]}
    pv = post(web_bot, "/api/admin/raid/regulars", {**body, "preview": True}).json()
    assert pv["box"] == row["box"]
    stale = post(web_bot, "/api/admin/raid/regulars", {**body, "seen": "0" * 12})
    assert stale.status_code == 409 and stale.json()["error"] == CONFIRM_WORDS["changed"] and stale.json()["box"]["fp"] == row["box"]["fp"]
    assert reg.regulars(sid), "nothing written on a stale box"
    ok = post(web_bot, "/api/admin/raid/regulars", {**body, "seen": row["box"]["fp"]})
    assert ok.status_code == 200 and not reg.regulars(sid), ok.text


# ---------------------------------------------------------------- the raid: Reset, minimums, Readiness

def test_the_raid_reset_box_names_its_minimums_and_checks_the_whole_raid(web_bot, reg):
    """Carried 1: a raid with only minimums can be reset; the box is raid_reset_box (the minimums named); the write is
    checked against config:raid_all:<rid>."""
    reg.config.raids.pop(ONY, None)
    reg.save_config("t: onyxia back to defaults")
    reg.set_minimums(ONY, {"Paladin": {"min": 4}}, "t")
    row = next(r for r in web_bot.c.get("/api/raids", headers=H()).json()["raids"] if r["id"] == ONY)
    box = row["page"]["boxes"]["reset"]
    assert box == cq.raid_reset_box(reg, ONY) and box["verb"] == "Reset" and box["danger"]
    assert "minimums for Paladins" in box["sentences"][1]
    pv = change(web_bot, [{"kind": "raid_reset", "target": ONY}], apply=True).json()
    assert pv.get("needs_review") and pv["described"]["box"]["sentences"] == box["sentences"]
    stale = change(web_bot, [{"kind": "raid_reset", "target": ONY}], apply=True, confirmed=True, revs={"config:raid_all:" + ONY: "stale"})
    assert stale.status_code == 409 and reg.config.raids.get(ONY)
    ok = change(web_bot, [{"kind": "raid_reset", "target": ONY}], apply=True, confirmed=True, revs={"config:raid_all:" + ONY: row["reset_rev"]})
    assert ok.status_code == 200 and ok.json()["applied"], ok.text
    assert "Paladin" not in reg.minimums(ONY)


def test_a_zero_minimum_is_cleared_never_saved(web_bot, reg):
    """Carried 4: the Save the Raids page sends (a `comp` change) never stores 0 — it clears the guild's own."""
    reg.set_minimums(HYJAL, {"Paladin": {"min": 4}}, "t")
    rev = reg.config_rev(f"raids:{HYJAL}")
    r = change(web_bot, [{"kind": "comp", "target": HYJAL, "fields": {"Paladin": {"min": 0}}}], apply=True, confirmed=True, revs={f"config:raids:{HYJAL}": rev})
    assert r.status_code == 200 and r.json()["applied"], r.text
    assert "Paladin" not in (reg.config.raids.get(HYJAL) or {}).get("minimums", {}) and "Paladin" not in reg.minimums(HYJAL)
    before = reg.store.root.joinpath(reg.key, "guild.yaml").read_text()
    r = change(web_bot, [{"kind": "comp", "target": HYJAL, "fields": {"Druid": {"min": 0}}}], apply=True, confirmed=True)
    assert r.json()["applied"] == ["Druids already have no minimum of their own."], r.text
    assert reg.store.root.joinpath(reg.key, "guild.yaml").read_text() == before, "a 0 for a slot with none of the guild's is no write"
    reg.set_minimums(HYJAL, {"Druid": {"min": 0}}, "t")
    assert "Druid" not in (reg.config.raids.get(HYJAL) or {}).get("minimums", {}), "the verb never stores 0 either"


def test_readiness_gives_one_answer_to_what_the_minimums_are(web_bot, reg):
    """Carried 2: the Rules' make-up and Readiness' Minimum column read Registry.minimums alone; a class the buff
    matrix wants (Paladins for blessings, Shamans for totems) is never listed as a minimum."""
    people(reg, 12)
    raids = {r["id"]: r for r in web_bot.c.get("/api/raids", headers=H()).json()["raids"]}
    ready = {r["raid"]: r for r in web_bot.c.get("/api/readiness", headers=H()).json()["raids"]}
    for rid in reg.profile.raids:
        mins = reg.minimums(rid)
        make_up = raids[rid]["page"]["make_up"]
        view = ready[rid]["view"]
        assert {x["slot"]: x["min"] for x in make_up} == mins
        assert {x["slot"]: x["min"] for x in view["make_up"]} == mins
        assert [x["slot"] for x in view["make_up"]] == [x["slot"] for x in make_up]
        assert not ({"Paladin", "Shaman"} & {x["slot"] for x in view["make_up"]}), "a buff-derived want is no minimum"
        assert [f["label"] for f in view["who_could_play"]] == ["Standing", "Mains", "Alts", "Could switch role"]
    hy = ready[HYJAL]["view"]
    assert hy["who_could_play"][1]["value"] == PAGE_WORDS["for_seats"].format(n=ready[HYJAL]["headcount"]["mains"], seats=20)
    tanks = next(x for x in hy["make_up"] if x["slot"] == "tank")
    assert tanks["label"] == "Tanks" and tanks["have"] == ready[HYJAL]["comp"][0]["have"]


def test_the_priority_and_rules_words(officer_client, reg):
    page = next(r for r in officer_client.get("/api/raids").json()["raids"] if r["id"] == HYJAL)["page"]
    assert page["used_when"] == PAGE_WORDS["used_when"] and page["one_off_rule"] == PAGE_WORDS["one_off_rule"]
    vals = [p["value"] for p in page["priority"]]
    assert vals == sorted(vals, reverse=True) and {p["label"] for p in page["priority"]} == set(PAGE_WORDS["priority"].values())
    assert [x["label"] for x in page["make_up"]] == ["Tanks", "Healers"] and page["open_events"] == 0
    assert next(f for f in page["details"] if f["label"] == "Played in")["value"] == PAGE_WORDS["played_nowhere"]


# ---------------------------------------------------------------- fix round 1

def test_a_zero_minimum_reads_as_what_applies_on_every_preview(reg):
    """A 0 clears, so no preview says 'at least 0': plain text and the structured describe say the game's minimum
    (a slot the game gives one) or no minimum of its own (one it gives none) — before the press."""
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp
    from oibot_gm.registry import GAME_MINIMUM_WORDS, NO_MINIMUM_WORDS

    reg.set_minimums(HYJAL, {"tank": {"min": 3}, "Paladin": {"min": 4}}, "t")
    for value in ("0", '{"min": 0}'):
        tank = configops.describe(reg, ConfigOp(op="comp_target", target=HYJAL, field="tank", value=value))
        pal = configops.describe(reg, ConfigOp(op="comp_target", target=HYJAL, field="Paladin", value=value))
        assert tank.endswith(f"at least 3 → {GAME_MINIMUM_WORDS}") and "at least 0" not in tank, tank
        assert pal.endswith(f"at least 4 → {NO_MINIMUM_WORDS}") and "at least 0" not in pal, pal
    d = configops.describe_ops(reg, [ConfigOp(op="comp_target", target=HYJAL, field="Paladin", value='{"min": 0}')])
    assert [(p.before, p.after) for p in d.lines[0].parts] == [("at least 4", NO_MINIMUM_WORDS)]
    line = configops.apply(reg, ConfigOp(op="comp_target", target=HYJAL, field="tank", value="0"), "t", True)
    assert line.endswith(f"Tanks: back to {GAME_MINIMUM_WORDS}") and reg.minimums(HYJAL)["tank"] == 2


def test_a_stored_zero_is_read_as_absent(reg):
    """A {min: 0} written before 0 meant clear is read as absent — the game's minimum applies — with no write."""
    reg.config.raids.setdefault(HYJAL, {})["minimums"] = {"tank": {"min": 0}, "Druid": {"min": 0}}
    reg.save_config("t: an older file's zeros")
    before = reg.store.root.joinpath(reg.key, "guild.yaml").read_text()
    assert reg.minimums(HYJAL)["tank"] == 2 and "Druid" not in reg.minimums(HYJAL)
    assert reg.minimum_words(HYJAL, "tank", {"min": 0}) == "the game's minimum"
    assert reg.store.root.joinpath(reg.key, "guild.yaml").read_text() == before, "no migration write"


def test_the_discord_minimums_block_offers_0_as_what_applies(reg, monkeypatch):
    import asyncio

    from wizard_harness import Harness

    from oibot_gm import discord_registry
    from oibot_gm import flows_config  # noqa: F401 — registers the flows
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    reg.set_minimums(ONY, {"healer": {"min": 10}}, "t")
    h = Harness(reg)

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        await h.pick("Pick a raid", ONY)
        await h.pick("Pick a section", "comp")
        await h.pick("Set a minimum for…", "healer")
        zero = next(o for o in h.modal._items["min"].options if o.value == "0")
        assert zero.label == "the game's minimum"
        await h.submit(h.modal, min="0", note="")
        assert "Healers: back to the game's minimum — not saved yet." in h.text and "at least 0" not in h.text
        await h.pick("Set a minimum for…", "Druid")
        zero = next(o for o in h.modal._items["min"].options if o.value == "0")
        assert zero.label == "no minimum of its own"
        await h.submit(h.modal, min="0", note="")
        assert "Druids: no minimum of its own." in h.text
        await h.press("Save minimums")
    asyncio.run(go())
    assert "healer" not in (reg.config.raids.get(ONY) or {}).get("minimums", {}), "saved as a clear, never a 0"


def test_a_regulars_preview_never_writes(web_bot, reg):
    sid = tuesday(reg)
    ms = people(reg, 1)
    r = post(web_bot, "/api/admin/raid/regulars", {"schedule": sid, "uid": str(ms[0].discord_id), "on": True, "preview": True})
    assert r.status_code == 400 and r.json()["box"]["verb"] is None and len(r.json()["box"]["sentences"]) == 1
    assert not reg.regulars(sid), "a preview of an add added nobody"


def test_a_paused_schedule_shows_its_outlook_if_resumed(officer_client, reg):
    sid = onyxia(reg)
    for m in people(reg, 2):
        reg.regular_add(m.discord_id, sid, "t")
    r = officer_client.get(f"/api/schedules/{sid}").json()["regulars"]
    assert rc.series_forecast(reg, sid) == [], "every other surface: a paused series has no forecast"
    assert rc.series_forecast(reg, sid, if_resumed=True), "the same computation, the pause not counted"
    assert r["words"] == "2 for 40 seats" and r["outlook"] == PAGE_WORDS["short_every_week"]


def test_could_switch_role_counts_people_not_names(web_bot, reg):
    """Two members with one display name who could each switch role count twice (by Discord id)."""
    base = 481_000_000_000_000_000
    for i in range(2):
        reg.add_character(base + i, "Twin", f"Twin{chr(97 + i)}", "Druid", "Feral", None, True)
        m = reg.members[base + i]
        m.main.offspec = "Restoration"
        reg.save(m, "t: offspec")
    reg.set_minimums(ONY, {"healer": {"min": 40}}, "t")  # healers short: the cover data lists who could switch to it
    row = next(r for r in web_bot.c.get("/api/readiness", headers=H()).json()["raids"] if r["raid"] == ONY)
    ids = {u for ro in row["roles"] for k in ("offspec", "flex") for u in ro["cover_ids"].get(k, [])}
    assert {str(base), str(base + 1)} <= ids
    assert next(f for f in row["view"]["who_could_play"] if f["label"] == "Could switch role")["value"] == str(len(ids))


# ---------------------------------------------------------------- fix round 2

def _view(web_bot, rid):
    return next(r for r in web_bot.c.get("/api/readiness", headers=H()).json()["raids"] if r["raid"] == rid)


def test_standing_reads_only_what_the_page_prints(web_bot, reg):
    """The seed's Barrow Deeps: enough mains, tanks and healers met, every minimum row met — comp.readiness is red only
    from the buff matrix's class wants (no minimums), and Standing reads ready."""
    row = _view(web_bot, RID)
    assert row["level"] == "red" and any(c["level"] == "red" and c["source"] == "derived" and c["key"] not in ("tank", "healer") for c in row["comp"])
    v = row["view"]
    assert v["level"] == "green" and v["standing_why"] is None and v["who_could_play"][0]["value"] == "ready"
    assert all(x["level"] == "green" and x["have"] >= x["min"] for x in v["make_up"])


def test_a_healer_minimum_unmet_reads_short_and_says_so(web_bot, reg):
    reg.set_minimums(RID, {"healer": {"min": 10}}, "t")
    v = _view(web_bot, RID)["view"]
    assert v["who_could_play"][0]["value"] == "short" and v["standing_why"] == "Healers are under the minimum."
    healers = next(x for x in v["make_up"] if x["slot"] == "healer")
    assert healers["own"] is True and healers["level"] == "red"
    assert next(x for x in v["make_up"] if x["slot"] == "tank")["own"] is False


def test_too_few_mains_reads_short_and_says_so(web_bot, reg):
    v = _view(web_bot, HYJAL)["view"]  # 16 mains for 20 seats, tanks and healers met
    assert v["who_could_play"][0]["value"] == "short" and v["standing_why"] == PAGE_WORDS["standing_mains"]


def test_make_up_rows_say_whose_minimum_it_is(officer_client, reg):
    reg.set_minimums(HYJAL, {"Paladin": {"min": 3}}, "t")
    page = next(r for r in officer_client.get("/api/raids").json()["raids"] if r["id"] == HYJAL)["page"]
    assert {x["slot"]: x["own"] for x in page["make_up"]} == {"tank": False, "healer": False, "Paladin": True}


def test_clearing_a_minimum_the_guild_never_set_says_nothing_changes(web_bot, reg):
    from oibot_gm import configops

    r = change(web_bot, [{"kind": "comp", "target": HYJAL, "fields": {"tank": None}}], apply=True).json()
    box = r["described"]["box"]
    assert box["verb"] is None and box["sentences"] == ["Tanks are already at the game's minimum, 2."] and box["title"] == "Nothing to save"
    assert configops.minimum_unchanged_sentence(reg, HYJAL, "Druid") == "Druids already have no minimum of their own."
    assert not (reg.config.raids.get(HYJAL) or {}).get("minimums")


def test_help_links_readiness_on_the_raids_page(reg, monkeypatch):
    from oibot_gm import help as help_mod

    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    assert help_mod.PAGES["readiness"][:2] == ("/app/raids?view=readiness", "Raids")
    assert help_mod.site_url(reg, help_mod.PAGES["readiness"][0]) == f"https://gm.example/app/raids?view=readiness&g={reg.key}"
    assert help_mod.site_url(reg, "/app/me") == f"https://gm.example/app/me?g={reg.key}"
