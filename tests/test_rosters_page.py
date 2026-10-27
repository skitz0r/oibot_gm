"""The Rosters page reads by its shape (design §5.56): every live run a card in time order, the Raids card's "When it
runs" block from the same server fields (`live_runs`, `next_run`), each active schedule's next three runs whatever the
horizon with `can_open` + `why` from the server, the run's week as steps for the strip (`week`) and the short line's
clause (`short_why`). The page itself is checked in tests/test_frontend_source.py; here, the JSON it reads."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, FakeBot, bearer
from test_raid_night import locked_ten

from oibot_gm import raidcycle as rc
from oibot_gm.web import app as web

RID = "barrow_deeps"
REAL = 410_000_000_000_000_000


@pytest.fixture
def c(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FakeBot(reg, rs)))


def raid(c) -> dict:
    return next(x for x in c.get("/api/rosters", headers=bearer()).json()["raids"] if x["id"] == RID)


def weekly(reg, first_open: str = "2026-01-01T00:00") -> None:
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": first_open}, "t")


def test_upcoming_lists_three_runs_per_active_schedule_whatever_the_horizon(reg, c):
    weekly(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")  # a template opens nothing on its own
    rows = raid(c)["upcoming"]
    assert sorted(u["schedule"] for u in rows) == ["alt"] * 3 + ["default"] * 3
    assert [u["start_iso"] for u in rows] == sorted(u["start_iso"] for u in rows)  # soonest first across schedules
    assert all("PM" in u["start"] and "19:30" not in u["start"] and u["opens"] for u in rows)  # words, 12-hour
    assert all(u["can_open"] and u["why"] is None for u in rows)  # all within three weeks


def test_a_raid_that_opens_later_lists_its_first_runs_and_says_why_they_cant_open_yet(reg, c):
    weekly(reg, (reg.now_local() + timedelta(days=40)).strftime("%Y-%m-%dT15:00"))
    r = raid(c)
    rows = r["upcoming"]
    assert len(rows) == 3, "no 'No upcoming runs before the raid opens': the first runs are listed"
    assert not any(u["can_open"] for u in rows)
    assert all(u["why"].startswith("Too far ahead to open now. Open works from ") and "three weeks before the run" in u["why"] for u in rows)
    assert r["next_run"] == {"when": rows[0]["start"], "schedule": rows[0]["schedule_name"], "opens": rows[0]["opens"]}
    assert r["live_runs"] == [] and r["opened"] is False and r["first_open_words"]
    # the row's reason matches what Open next run says on every surface
    nope = c.post(f"/api/raid/{RID}/open", json={"when": ""}, headers={**bearer(), "X-Requested-With": "oibot"})
    assert nope.status_code == 400 and "before it opens" in nope.json()["error"]


def test_live_runs_soonest_first_never_a_test_run_and_next_run_skips_them(reg, rs, c):
    weekly(reg)
    starts = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)]
    a = rc.open_run(reg, rs, RID, starts[1], by="t")
    b = rc.open_run(reg, rs, RID, starts[0], by="t")
    open_test_run(reg, rs)  # a rehearsal: its own tab, never a live line
    r = raid(c)
    assert [x["key"] for x in r["live_runs"]] == [b.key, a.key]
    assert all(x["state"] == "open" and "PM" in x["when"] for x in r["live_runs"])
    keys = {u["key"] for u in r["upcoming"]}
    assert a.key not in keys and b.key not in keys  # a live run has its own card, not an upcoming row
    assert r["next_run"]["when"] == reg.local12(starts[2])


def test_a_closed_run_at_an_upcoming_time_says_why_it_cant_open(reg, rs, c):
    weekly(reg)
    first = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)][0]
    ev = rc.open_run(reg, rs, RID, first, by="t")
    rc.end_run(reg, rs, ev, "t")
    r = raid(c)
    row = next(u for u in r["upcoming"] if u["key"] == ev.key)
    assert row["can_open"] is False and "already ended" in row["why"]
    assert r["next_run"]["when"] != row["start"]  # the next run is one that can still have a sheet


def test_the_run_week_steps_in_time_order_with_what_is_done(reg, rs, c):
    ev, _seated = locked_ten(reg, rs)
    j = c.get(f"/api/run/{ev.key}", headers=bearer()).json()
    steps = [w["step"] for w in j["week"]]
    assert steps[0] == "signup_lead_hours" and steps[-1] == "raid" and "lock_hours_before" in steps
    assert [w["at"] for w in j["week"]] == sorted(w["at"] for w in j["week"])
    done = {w["step"]: w["done"] for w in j["week"]}
    assert done["signup_lead_hours"] and done["lock_hours_before"] and not done["raid"]
    assert all("PM" in w["when"] or "AM" in w["when"] for w in j["week"])


def test_the_short_line_says_why_after_the_lock(reg, rs, c):
    ev, seated = locked_ten(reg, rs)
    rc.free_seat(reg, rs, ev, seated[-1].discord_id, "no-confirm")
    j = c.get(f"/api/run/{ev.key}", headers=bearer()).json()
    assert j["needs"]["headcount"] >= 1
    assert "on the bench could fill it" in (j["short_why"] or "")


def test_the_short_line_names_the_regulars_who_havent_answered(reg, rs, c):
    weekly(reg)
    names = []
    for n, (cls, spec) in enumerate([("Priest", "Holy"), ("Shaman", "Restoration")]):
        uid = REAL + n
        names.append(f"Regular{'ab'[n]}")
        reg.add_character(uid, f"P{n}", names[-1], cls, spec, None, True)
        reg.regular_add(uid, f"{RID}:default", "t")
    first = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)][0]
    ev = rc.open_run(reg, rs, RID, first, by="t")
    assert rc.short_why(reg, ev) == f"2 regulars haven't answered: {names[0]}, {names[1]}"
    join(reg, rs, ev, [reg.members[REAL]])
    assert rc.short_why(reg, ev) == f"1 regular hasn't answered: {names[1]}"
    r = raid(c)
    card = next(e for e in r["open"] if e["key"] == ev.key)
    assert card["short_why"] == rc.short_why(reg, ev) and card["week"][0]["step"] == "signup_lead_hours"



# ---------------------------------------------------------------- phase 2: one vocabulary (design §5.56 "Phase 2 — one vocabulary")

def test_the_run_vocabulary_reaches_the_site_from_python(c):
    """Every state word, mark, ask kind, answer word and run verb's label is defined once in Python and reaches the site
    through /api/meta — the Discord buttons and the web's are the same words (RunButton.LABELS is built from RUN_WORDS)."""
    from oibot_gm.constants import MARK, MARK_WORDS
    from oibot_gm.raid_buttons import RUN_WORDS, RunButton

    m = c.get("/api/meta", headers=bearer()).json()
    assert m["run_states"] == rc.RUN_STATE_WORDS and set(m["run_states"]) == {"open", "locked", "done", "cancelled"}
    assert m["run_states"]["done"] == "finished" and m["run_states"]["open"] == "open for signup"
    assert m["run_labels"] == RUN_WORDS
    assert all(RunButton.LABELS[k][0] == RUN_WORDS[k] for k in RunButton.LABELS)
    for k in ("preview_lock", "lock", "fill", "suggest", "loot", "end", "reopen", "remind", "pause", "place_rest"):
        assert m["run_labels"][k], k
    assert (m["run_labels"]["fill"], m["run_labels"]["remind"], m["run_labels"]["pause"], m["run_labels"]["end"]) == (
        "Fill seats…", "Remind the unanswered", "Pause automatic asks", "End the run")
    assert m["marks"]["yes"] == {"mark": MARK["yes"], "words": MARK_WORDS["yes"]} and m["marks"]["waiting"]["mark"] == MARK[None]
    assert m["ask_kinds"] == rc.ASK_HOW and m["ask_answers"]["waiting"] == rc.ASK_ANSWER_WORDS[None]
    assert m["level_words"] == {"green": "ready", "amber": "thin", "red": "short"}
    w = m["run_words"]
    assert w["no_confirmations"] == rc.NO_CONFIRMATIONS and w["bank"] == {"open": "Not placed", "locked": "Not rostered"}
    assert w["leftovers"] == "Leftovers" and set(w["pins"]) == {"in", "out"} and "bench" not in " ".join(w["pins"].values()).lower()


def test_a_run_and_its_live_line_carry_their_state_in_words(reg, rs, c):
    weekly(reg)
    first = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)][0]
    ev = rc.open_run(reg, rs, RID, first, by="t")
    r = raid(c)
    assert next(x for x in r["live_runs"] if x["key"] == ev.key)["state_word"] == "open for signup"
    assert next(e for e in r["open"] if e["key"] == ev.key)["state_word"] == "open for signup"
    rc.end_run(reg, rs, ev, "t")
    assert c.get(f"/api/run/{ev.key}", headers=bearer()).json()["state_word"] == "finished"


def test_the_fill_state_reads_in_sentences(reg, rs):
    """Never "engine", never a bare code, never "autofill": how open seats are filled, as a sentence."""
    ev = open_test_run(reg, rs)
    assert "engine" not in rc.fill_state_text(reg, ev) and rc.fill_state_text(reg, ev).endswith(".")
    ev, _seated = locked_ten(reg, rs)
    for state in ("idle", "asking", "filled", "exhausted", "suggested"):
        ev.fill_state = state
        text = rc.fill_state_text(reg, ev)
        assert text[0].isupper() and text.endswith(".") and "engine" not in text and "fill:" not in text, text
    ev.autofill_paused, ev.autofill_paused_by = True, "Jon"
    assert rc.fill_state_text(reg, ev).startswith("Automatic asks paused by Jon. ") and "autofill" not in rc.fill_state_text(reg, ev).lower()
    rc.end_run(reg, rs, ev, "t")
    assert rc.fill_state_text(reg, ev) == "The run is finished."


def test_the_me_badge_says_not_rostered_for_a_joiner_the_lock_left_out(reg, rs):
    ev, seated = locked_ten(reg, rs)
    left = next(m for m in reg.test_members() if m not in seated and ev.signups[str(m.discord_id)].status == "in")
    assert rc.answer_word(ev, "in", None) == "not rostered" and left
    assert rc.answer_word(ev, "in", 1) == "rostered" and rc.answer_word(ev, "in", 2) == "rostered · roster 2"
    assert rc.answer_word(ev, "sub", None) == "Bench" and rc.answer_word(ev, None, None) == "not answered"
    fresh = open_test_run(reg, rs, days=5)
    assert rc.answer_word(fresh, "in", None) == "Join", "before the lock a joiner is just joined"


def test_split_rosters_compare_in_words_never_numbers():
    assert rc.balance_words([42]) is None and rc.balance_words([0, 0]) is None
    assert rc.balance_words([40, 38]) == "the two rosters are close"
    assert rc.balance_words([40, 20]) == "roster 2 is the weaker" and rc.balance_words([10, 40, 39]) == "roster 1 is the weakest"


def test_the_health_hints_use_the_fill_screens_words(reg, rs):
    """Who could cover a short role reads as the fill modal does ("from the bench: A"), never "+1 sub"."""
    from conftest import by_role

    ev = open_test_run(reg, rs)
    roles = by_role(reg)
    join(reg, rs, ev, roles["tank"][:2] + roles["melee"] + roles["ranged"])
    join(reg, rs, ev, roles["healer"][:1], status="sub")
    healer = next(r for r in rc.health_data(reg, ev, rc.run_team(reg, ev))["roles"] if r["role"] == "healer")
    assert healer["need"] and healer["hint"].startswith(f"{rc.ASK_HOW['sub']}: ") and "+" not in healer["hint"], healer["hint"]


def test_the_confirmations_off_sentence_is_one_constant_on_every_discord_card(reg, rs, monkeypatch):
    """One sentence for a run whose rostered aren't asked to confirm (rc.NO_CONFIRMATIONS): the lock card, the lock's
    consequence — and no group-building number on the lock card."""
    from oibot_gm import consequences, raid_views as views

    ev, _seated = locked_ten(reg, rs)
    fresh = open_test_run(reg, rs, days=5)
    monkeypatch.setattr(rc, "run_confirms", lambda reg, ev: False)  # an ad hoc run's default (D5)
    card = views.lock_layout(reg, ev, rc.run_team(reg, ev), lambda kind, ident: "", 0)
    text = "\n".join(i.content for i in card.walk_children() if isinstance(getattr(i, "content", None), str))
    assert rc.NO_CONFIRMATIONS in text and "synergy" not in text and "first come —" not in text
    lock = " ".join(consequences.consequence("lock", reg, fresh))
    assert "Join is the commitment, so nobody is asked to confirm." in lock and "Joining was" not in lock


# ---------------------------------------------------------------- phase 3: behaviour (design §5.56 "Phase 3 — behaviour")

from oibot_gm import consequences as cq  # noqa: E402
from conftest import lock_with_board  # noqa: E402


def adhoc_run(reg, rs, n: int = 6):
    start = (reg.now_local() + timedelta(days=2, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, RID, start, by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members()[:n])
    assert rc.run_kind(reg, ev) == "adhoc" and not rc.run_confirms(reg, ev)
    return ev


def said(verb, reg, ev, **names) -> str:
    return " ".join(cq.consequence(verb, reg, ev, **({"name": "Nettle", "other": "Ivy", "to": "Group 2"} | names)))


@pytest.mark.parametrize("kind", ["adhoc", "recurring"])
def test_board_moves_before_the_lock_tell_nobody_on_either_kind_of_run(reg, rs, kind):
    ev = adhoc_run(reg, rs) if kind == "adhoc" else open_test_run(reg, rs)
    if kind == "recurring":
        join(reg, rs, ev, reg.test_members()[:6])
        assert rc.run_confirms(reg, ev)
    for verb in cq.BOARD_VERBS:
        text = said(verb, reg, ev)
        assert text.endswith(cq.BEFORE_LOCK) and "DM" not in text and "bench is asked" not in text, (verb, text)
    assert said("move", reg, ev).startswith("Nettle takes an open seat in Group 2.")  # the Board mockup's confirm
    assert said("substitute", reg, ev).startswith("Nettle takes Ivy's place in Group 2. Ivy goes back to Not placed.")
    assert said("remind", reg, ev) == "Remind is for a locked roster, and this sheet is still open."
    assert "Reopen works for an hour if this was a mistake." in said("end", reg, ev)


def test_board_moves_after_the_lock_on_a_run_with_confirmations(reg, rs):
    ev, seated = locked_ten(reg, rs, asks=True)
    assert said("move", reg, ev) == "Nettle moves to Group 2 and stays rostered. Nobody is told."
    assert said("swap", reg, ev) == "Nettle and Ivy swap places and both stay rostered. Nobody is told."
    roster = said("roster", reg, ev)
    assert roster == ("Nettle takes an open seat in Group 2 and gets the Confirm or Can't make it DM. With DMs off it waits on their Me page. "
                      "The seat is theirs once they say yes.")
    sub = said("substitute", reg, ev)
    assert sub.startswith("Nettle takes Ivy's seat in Group 2 and gets the Confirm or Can't make it DM.")
    assert sub.endswith("Ivy sees the seat was released on their card and their Me page.")
    assert "bench is asked" not in sub and "Ivy is told" not in sub, "the substitute takes the seat: nobody else is asked, Ivy is never DMed"
    assert "The bench is asked to fill it." in said("release", reg, ev)
    ev.autofill_paused = True
    assert "Automatic asks are paused, so the seat waits for Fill seats." in said("release", reg, ev)
    # Remind: who gets the card again, who waits on the site
    seated[0].dm_opt_out = True
    reg.save(seated[0], "dms off")
    assert said("remind", reg, ev) == ("The 9 rostered who haven't answered get their Confirm or Can't make it DM again. "
                                       "One more waits on the site with DMs off. Remind works once an hour.")


def test_board_moves_after_the_lock_on_an_ad_hoc_run_promise_no_dm(reg, rs):
    ev = adhoc_run(reg, rs, 10)
    lock_with_board(reg, rs, ev, [m.display_name for m in reg.test_members()[:8]])
    roster, sub = said("roster", reg, ev), said("substitute", reg, ev)
    for text in (roster, sub):
        assert "Confirm or Can't make it" not in text and "DM" not in text, text
    assert roster == "Nettle takes an open seat in Group 2. On an ad hoc run Join is the commitment, so the seat is theirs now."
    assert sub == ("Nettle takes Ivy's seat in Group 2. On an ad hoc run they aren't asked to confirm. "
                   "Ivy sees the seat was released on their Me page.")
    assert said("remind", reg, ev) == "On an ad hoc run Join is the commitment, so there is nothing to remind."
    assert "card" not in said("release", reg, ev)


def test_end_reads_the_reopen_window_from_code(reg, rs, monkeypatch):
    ev, _ = locked_ten(reg, rs)
    assert said("end", reg, ev) == ("The run is finished. The sheet and the lock card close and no more fill asks go out. "
                                    "Reopen works for an hour if this was a mistake.")
    monkeypatch.setattr(rc, "REOPEN_GRACE_HOURS", 2)
    assert said("end", reg, ev).endswith("Reopen works for 2 hours if this was a mistake.")
    rc.end_run(reg, rs, ev, "t")
    assert said("end", reg, ev) == "This run is already finished."


def test_ev_json_carries_every_board_sentence_with_tokens_for_the_names(reg, rs, c):
    ev, _ = locked_ten(reg, rs, asks=True)
    j = c.get(f"/api/run/{ev.key}", headers=bearer()).json()["consequences"]
    assert set(j) == set(cq.VERBS) | {"release_named"}
    for verb in cq.BOARD_VERBS:
        assert cq.NAME_TOKEN in j[verb] and j[verb] == " ".join(cq.consequence(verb, reg, ev, name=cq.NAME_TOKEN, other=cq.OTHER_TOKEN, to=cq.TO_TOKEN))
    assert cq.TO_TOKEN in j["move"] and cq.OTHER_TOKEN in j["substitute"]
    assert j["remind"] == " ".join(cq.consequence("remind", reg, ev)) and "Reopen works for an hour" in j["end"]


def test_a_regulars_remove_and_an_orphan_clear_carry_the_servers_sentence_and_a_rev(reg, rs, c):
    weekly(reg)
    reg.add_character(REAL, "P0", "Bramble", "Priest", "Holy", None, True)
    reg.regular_add(REAL, f"{RID}:default", "t")
    m = reg.members[REAL]
    row = next(x for s in raid(c)["series"] for x in s["regulars"] if x["uid"] == str(REAL))
    assert row["remove"] == " ".join(cq.regular_remove(reg, m, f"{RID}:default"))
    assert row["remove"].startswith("Bramble stops being a regular of ") and "nobody is told" in row["remove"] and "weights them as a regular" in row["remove"]
    post = lambda body: c.post("/api/admin/raid/regulars", json=body, headers={**bearer(), "X-Requested-With": "oibot"})  # noqa: E731
    stale = post({"raid": RID, "schedule": "default", "uid": str(REAL), "on": False, "rev": "0" * 12})
    assert stale.status_code == 409 and "changed since you loaded them" in stale.json()["error"]
    assert f"{RID}:default" in m.main.rosters, "a stale remove writes nothing"
    # a removed schedule's leftover: listed with its own sentence and rev
    m.main.rosters.append(f"{RID}:gone")
    reg.save(m, "t")
    o = next(x for x in raid(c)["orphan_regulars"] if x["key"] == f"{RID}:gone")
    assert "a removed Barrow Deeps schedule" in o["clear"] and o["rev"] == reg.characters_rev(m)
    ok = post({"raid": RID, "schedule": "gone", "uid": str(REAL), "on": False, "rev": o["rev"]})
    assert ok.status_code == 200 and f"{RID}:gone" not in m.main.rosters


def test_place_the_rest_refuses_a_board_that_changed_since_it_was_drawn(reg, rs, c):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:6])
    r = c.post(f"/api/run/{ev.key}/autofill", json={"rev": "stale"}, headers={**bearer(), "X-Requested-With": "oibot"})
    assert r.status_code == 409 and "board" in r.json() and r.json()["rev"] == rc.board_rev(reg, ev)
    assert ev.layout is None, "nothing solved, nothing written"


def test_who_could_cover_says_not_on_the_sheet_once_for_every_role_it_covers():
    roles = [{"role": "tank", "have": 0, "need": 2, "hint": "not on the sheet: A, B"},
             {"role": "healer", "have": 1, "need": 3, "hint": "not on the sheet: B, C"},
             {"role": "melee", "have": 0, "need": 1, "hint": "from the bench: D"},
             {"role": "ranged", "have": 3, "need": 2, "hint": ""}]
    assert rc.cover_hints(roles) == [{"roles": ["melee"], "hint": "from the bench: D"},
                                     {"roles": ["tank", "healer"], "hint": "not on the sheet: A, B, C"}]
