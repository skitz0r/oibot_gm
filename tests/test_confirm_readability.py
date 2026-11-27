"""The Confirm rule (user decision 2026-10-26, CLAUDE.md Principles; design §5.57): every sentence a confirm prints before
a press — a `decide` screen, a `useConfirm`, a review diff, a preview — is read aloud by a raid leader with a finger on
the button. This harness renders EVERY confirm generator across its cases and lints each sentence: a capital letter (or
a digit, or a name) to start, . ! or ? to end, twenty words or fewer, no parentheses, semicolons, ": " chaining two
clauses, " — " glue or "no new DM", and at most three sentences per confirm. A sentence that fails is rewritten in its
generator, never allow-listed (there is no allow list here, on purpose).

Every rendered confirm is also written to a file, one block per generator and case, so a person can read them all:
out/confirm_sentences.txt, and the path in OIBOT_CONFIRM_SENTENCES when it is set. Passing the lint is necessary, not
sufficient: read the file as a raid leader would."""
from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from conftest import OWNER, join, lock_with_board, open_test_run, weekly as _weekly

from oibot_gm import consequences as cq
from oibot_gm import discord_people, inbox
from oibot_gm import raidcycle as rc
from oibot_gm.registry import Applicant

ROOT = Path(__file__).resolve().parents[1]
RID = "barrow_deeps"
REAL = 470_000_000_000_000_000
OFFICER_ID = 470_000_000_000_000_999

# ------------------------------------------------------------------------------------------------------- the lint
MAX_WORDS = 20
MAX_SENTENCES = 3
_SPLIT = re.compile(r"(?<=[.!?])\s+")
_LEAD = re.compile(r"^(?:-#\s*|[⚠✅❌⏳•☠🔒✓]️?\s*)+")
_BANNED = [("(", "a parenthesis"), (")", "a parenthesis"), (";", "a semicolon"), (": ", "a colon chaining two clauses"),
           (" — ", "a dash gluing fragments"), (" – ", "a dash gluing fragments"), ("no new DM", "\"no new DM\"")]


def sentences(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = _LEAD.sub("", str(line).replace("**", "").strip())
        out += [s.strip() for s in _SPLIT.split(text) if s.strip()]
    return out


def lint(lines: list[str], names: set[str]) -> list[str]:
    """The problems of one confirm (its lines), [] when it reads to the rule. `names`: the proper names a sentence may
    open with in lower case (a member called cbottoms is still a name)."""
    problems: list[str] = []
    ss = sentences(lines)
    if len(ss) > MAX_SENTENCES:
        problems.append(f"{len(ss)} sentences (at most {MAX_SENTENCES})")
    for s in ss:
        first = s.split()[0].strip("“”\"'’,") if s.split() else ""
        if not (s[:1].isupper() or s[:1].isdigit() or s[:1] in "<@" or first.split("'")[0] in names or first.removesuffix("’s") in names):
            problems.append(f"starts in lower case: {s!r}")
        if s[-1:] not in ".!?":
            problems.append(f"no full stop: {s!r}")
        if len(s.split()) > MAX_WORDS:
            problems.append(f"{len(s.split())} words: {s!r}")
        for bad, what in _BANNED:
            if bad in s:
                problems.append(f"{what}: {s!r}")
    return problems


# ------------------------------------------------------------------------------------------------------- the world
PEOPLE = [("Bramble", "Ashgrove", "Warrior", "Protection"), ("cbottoms", "Thornwick", "Warrior", "Protection"),
          ("Cedar", "Larkspur", "Priest", "Holy"), ("Dorran", "Mossvale", "Priest", "Holy"), ("Elske", "Fernhollow", "Priest", "Holy"),
          ("Ivy", "Quillon", "Mage", "Fire"), ("Juniper", "Rowanel", "Mage", "Fire"), ("Kasimir", "Sorrel", "Mage", "Fire"),
          ("Linnea", "Tamsin", "Mage", "Fire"), ("Marrow", "Umber", "Mage", "Fire"), ("Nettle", "Vesper", "Mage", "Fire"),
          ("Orrin", "Wrenna", "Mage", "Fire")]


def people(reg):
    ms = []
    for i, (who, char, cls, spec) in enumerate(PEOPLE):
        reg.add_character(REAL + i, who, char, cls, spec, None, True)
        ms.append(reg.members[REAL + i])
    reg.add_character(OFFICER_ID, "Officer", "Kingsley", "Mage", "Fire", None, True)
    return ms


def names_of(reg) -> set[str]:
    out = {"Barrow", "Nettle", "Ivy", "Group"}
    for m in reg.members.values():
        out.add(m.display_name)
        out |= {c.label for c in m.characters} | {c.name for c in m.characters}
    return out


def weekly(reg):
    reg.set_raid_overrides(RID, {"first_open": "2026-01-01T00:00"}, "t")
    _weekly(reg, RID, ["Tue 19:30", "Thu 19:30", "Sat 19:30"])


def recurring_run(reg, rs, n: int = 0):
    now = reg.now_local()
    t = [t for _s, t in rc.upcoming(reg, RID, now, 24 * 21) if t - now >= timedelta(hours=30)][n]
    return rc.open_run(reg, rs, RID, t, by="t")


def adhoc_run(reg, rs, days: float = 2):
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, RID, start, by="t", schedule=rc.ADHOC_SCHEDULE)


def locked(reg, rs, ev, ms, *, asks: bool = True, off: int | None = None, autofill=True, paused=False):
    """`ms` joined; the first ten rostered by the board; Confirm / Can't asks for them when the run asks for them
    (`off`: that one waits on the site with DMs off)."""
    join(reg, rs, ev, ms)
    seated = ms[:10]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated], hours_to_confirm=2)
    team = rc.run_team(reg, ev)
    team["autofill"] = autofill
    ev.autofill_paused = paused
    if asks and rc.run_confirms(reg, ev):
        for i, m in enumerate(seated):
            reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock", channel="web" if i == off else "dm")
    rs.save(ev, "harness")
    return ev


class Dump:
    """Every confirm, rendered, linted and written out (the operator reads them all)."""

    def __init__(self, names: set[str]):
        self.names, self.blocks, self.problems = names, [], []

    def add(self, generator: str, case: str, lines, extra_names: set[str] = frozenset(), fates=()):
        """`fates`: the lines that are a ripple into runs, one sentence per fate (rc.character_effects, an absence's
        sheets — design §5.57 "one sentence per fate"): each is linted as a confirm of its own, and the rest of the
        lines (what changes · who is told · what can be undone) as one confirm of at most three sentences. The file
        shows them together, as the screen does."""
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(x) for x in (lines or [])]
        self.blocks.append(f"## {generator} · {case}\n" + ("\n".join(lines) if lines else "(nothing)"))
        fates = [f for f in fates if f in lines]
        names = self.names | set(extra_names)
        for p in lint([x for x in lines if x not in fates], names):
            self.problems.append(f"{generator} · {case}: {p}")
        for f in fates:
            for p in lint([f], names):
                self.problems.append(f"{generator} · {case} · a fate: {p}")

    def write(self):
        text = "\n\n".join(self.blocks) + "\n"
        paths = [ROOT / "out" / "confirm_sentences.txt"]
        if os.environ.get("OIBOT_CONFIRM_SENTENCES"):
            paths.append(Path(os.environ["OIBOT_CONFIRM_SENTENCES"]))
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)


# ------------------------------------------------------------------------------------------------------- the cases

def member_action_boxes(reg, rs, ev, lk, ms, add):
    """/raid set's and the row ⋮ menu's box (consequences.member_action_box) for every action that isn't an answer:
    a callout, the pins on an open sheet, a Confirm ask (both answers), No thanks, a fill ask after the lock and Play
    another character of another role on a seat."""
    def first(run, key, **kw):
        m = next(m for m in ms if any(a["key"] == key for a in rc.member_actions(reg, run, m)))
        return cq.member_action_box(reg, run, m, key, by_id=OFFICER_ID, **kw)

    add("member_action_box", "callout, before the lock", first(ev, "callout"))
    add("member_action_box", "callout, rostered after the lock", first(lk, "callout"))
    add("member_action_box", "pin_in", first(ev, "pin_in"))
    add("member_action_box", "pin_out", first(ev, "pin_out"))
    m = next(m for m in ms if str(m.discord_id) in ev.signups)
    pins = dict(ev.pins)
    ev.pins[str(m.discord_id)] = "in"
    add("member_action_box", "unpin", cq.member_action_box(reg, ev, m, "unpin", by_id=OFFICER_ID))
    ev.pins = pins
    add("member_action_box", "confirm_yes", first(lk, "confirm_yes"))
    add("member_action_box", "confirm_no", first(lk, "confirm_no"))
    add("member_action_box", "no_thanks, before the lock", first(ev, "no_thanks"))
    add("member_action_box", "no_thanks, rostered after the lock", first(lk, "no_thanks"))
    # Play another character of another role: the seat follows it, said first (task 7 fix round 1)
    m = next(m for m in ms if lk.seat_of(m))
    role = lk.seat_of(m)[1].role
    cls, spec = next((cls, s) for cls, specs in reg.profile.classes.items() for s in specs if reg.profile.spec(cls, s).role != role)
    reg.add_character(m.discord_id, m.display_name, "Otheralt", cls, spec, None, False)
    m = reg.members[m.discord_id]
    moved = cq.member_action_box(reg, lk, m, "join", "Otheralt", by_id=OFFICER_ID)
    assert "moves from" in moved["sentences"][0]
    add("member_action_box", "join on another character of another role, rostered after the lock", moved)
    m = next(m for m in ms if not lk.seat_of(m) and not any(a.discord_id == m.discord_id and a.open for a in lk.fill_asks))
    lk.fill_asks.append(rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="ranged", character=m.main.label,
                                   spec=m.main.spec, reason="short 1", expires_at=lk.start.isoformat()))
    for key in ("fill_yes", "fill_no"):
        add("member_action_box", key, cq.member_action_box(reg, lk, m, key, by_id=OFFICER_ID))
    lk.fill_asks.pop()


def run_cases(reg, rs, ms):
    """name → run, every state a run verb's confirm can meet."""
    weekly(reg)
    cases = {}
    # open recurring: six joined (one with DMs off), two placed on the board
    ev = recurring_run(reg, rs, 0)
    join(reg, rs, ev, ms[:6])
    join(reg, rs, ev, ms[6:7], "sub")
    ms[3].dm_opt_out = True
    ev.layout = [[ms[0].display_name, ms[2].display_name]]
    rs.save(ev, "harness")
    cases["open recurring"] = ev
    cases["open recurring, nobody joined"] = recurring_run(reg, rs, 1)
    a = adhoc_run(reg, rs, 2)
    join(reg, rs, a, ms[:4])
    cases["open ad hoc"] = a
    cases["locked recurring, autofill on"] = locked(reg, rs, recurring_run(reg, rs, 2), ms, off=3)
    cases["locked recurring, autofill off"] = locked(reg, rs, recurring_run(reg, rs, 2 + 1), ms, autofill=False)
    cases["locked recurring, autofill paused"] = locked(reg, rs, recurring_run(reg, rs, 2 + 2), ms, paused=True)
    cases["locked ad hoc"] = locked(reg, rs, adhoc_run(reg, rs, 4), ms)
    d = locked(reg, rs, adhoc_run(reg, rs, 5), ms)
    rc.end_run(reg, rs, d, "t")
    cases["done"] = d
    c = adhoc_run(reg, rs, 6)
    c.state = "cancelled"
    rs.save(c, "harness")
    cases["cancelled"] = c
    # a lead with a follow-on (design §5.62): the cancel pair sentences and leaving early. Two
    # nights, so the open pair stays open while the other is locked.
    reg.set_raid_overrides("hyjal_summit_forever", {"first_open": "2026-01-01T00:00"}, "t")
    for days, state in ((3, "open"), (4, "locked")):
        start = (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)
        lead = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": RID, "rosters": 2})
        join(reg, rs, lead, ms)
        fev = rs.events[lead.followed_by]
        if state == "locked":
            lock_with_board(reg, rs, lead, [m.display_name for m in ms[:10]], hours_to_confirm=2)
            lock_with_board(reg, rs, fev, [m.display_name for m in ms[:10]], hours_to_confirm=2)
        cases[f"lead {state}"], cases[f"follow-on {state}"] = lead, fev
    # a run of three raids (Task 4): the cancel names the whole lineup from any of its raids
    start = (reg.now_local() + timedelta(days=5)).replace(hour=18, minute=30, second=0, microsecond=0)
    first = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t", schedule=rc.ADHOC_SCHEDULE,
                        lineup=[{"raid": RID, "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}])
    join(reg, rs, first, ms)
    parts = rc.event_parts(rs, first)
    cases["three raids, first"], cases["three raids, second"], cases["three raids, third"] = parts
    return cases


def test_every_confirm_reads_aloud(reg, rs, monkeypatch):
    ms = people(reg)
    runs = run_cases(reg, rs, ms)
    dump = Dump(names_of(reg))

    # ---- consequences.consequence: every verb × every run
    for case, ev in runs.items():
        for verb in cq.VERBS:
            dump.add(f"consequence({verb})", case, cq.consequence(verb, reg, ev, name="Nettle", other="Ivy", to="Group 2"))
        dump.add("consequence(release, no name)", case, cq.consequence("release", reg, ev))
        if ev.follows:
            for seated, lead_seated in ((True, True), (True, False), (False, True), (False, False)):
                dump.add("leave_follow_consequence", f"{case}, {'rostered' if seated else 'not rostered'} here, {'rostered' if lead_seated else 'not rostered'} on the lead",
                         cq.leave_follow_consequence(reg, ev, seated, lead_seated))
        for k, v in cq.consequences_json(reg, ev).items():
            dump.add(f"consequences_json[{k}]", case, v.replace(cq.NAME_TOKEN, "Nettle").replace(cq.OTHER_TOKEN, "Ivy").replace(cq.TO_TOKEN, "Group 2"))
    # remind: reminded within the hour, everyone answered, all on the site
    ev = runs["locked recurring, autofill on"]
    ev.reminded_at, ev.reminded_by = rc.now(), "Officer"
    dump.add("consequence(remind)", "reminded within the hour", cq.consequence("remind", reg, ev))
    ev.reminded_at = None
    for m in ms[:10]:
        for a in m.placement_asks:
            a["answer"] = "yes"
    dump.add("consequence(remind)", "everyone answered", cq.consequence("remind", reg, ev))
    for m in ms[:10]:
        for a in m.placement_asks:
            a["answer"], a["channel"] = None, "web"
    dump.add("consequence(remind)", "every unanswered on the site", cq.consequence("remind", reg, ev))
    for i, m in enumerate(ms[:10]):
        for a in m.placement_asks:
            a["channel"] = "web" if i == 3 else "dm"
    # lock: a test run
    from conftest import open_test_run
    t = open_test_run(reg, rs)
    join(reg, rs, t, reg.test_members()[:6])
    dump.add("consequence(lock)", "test run", cq.consequence("lock", reg, t))

    # ---- regular_remove
    m = ms[0]
    reg.regular_add(m.discord_id, "barrow-deeps", "t")
    dump.add("regular_remove", "a running schedule", cq.regular_remove(reg, reg.members[m.discord_id], "barrow-deeps"))
    dump.add("regular_remove", "a removed schedule", cq.regular_remove(reg, reg.members[m.discord_id], "gone"))

    # ---- inbox.action_consequence: every key member_actions offers, every run, by an officer and on their own record
    for case, ev in runs.items():
        if ev.state not in ("open", "locked"):
            continue
        seen = set()
        for m in ms:
            for act in rc.member_actions(reg, ev, m):
                k = act["key"]
                state = ("rostered" if ev.state == "locked" and ev.seat_of(m) else "not rostered") + ", DMs off" * bool(m.dm_opt_out)
                if (k, state) in seen:
                    continue
                seen.add((k, state))
                dump.add(f"action_consequence({k})", f"{case}, {state}", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))
                dump.add(f"action_consequence({k})", f"{case}, {state}, own record", inbox.action_consequence(reg, ev, m, k, by_id=m.discord_id))
    # fill asks: a fill after the lock (with its DM card) and a pre-lock ask
    ev = runs["locked recurring, autofill on"]
    m = ms[10]
    ev.fill_asks.append(rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="ranged", character=m.main.label,
                                   spec=m.main.spec, reason="short 1", expires_at=ev.start.isoformat(), dm_message_id=5))
    for k in ("fill_yes", "fill_no", "callout", "no_thanks"):
        dump.add(f"action_consequence({k})", "locked recurring, fill ask with its DM card", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))
    ev = runs["open recurring"]
    m = ms[8]
    ev.fill_asks.append(rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="ranged", character=m.main.label,
                                   spec=m.main.spec, reason="short 1", phase="prelock", expires_at=ev.start.isoformat()))
    for k in ("fill_yes", "fill_no"):
        dump.add(f"action_consequence({k})", "open recurring, pre-lock ask", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))
    # Join after the lock with a seat free: DMs on, DMs off, and on an ad hoc run
    for case in ("locked recurring, autofill on", "locked ad hoc"):
        ev = runs[case]
        held = ms[9]
        rc.free_seat(reg, rs, ev, held.discord_id, "officer")
        m = ms[11]
        rc.set_signup(reg, rs, ev, m, None, "sub", source="test")
        for off in (False, True):
            m.dm_opt_out = off
            assert any(a["key"] == "join" for a in rc.member_actions(reg, ev, m))
            dump.add("action_consequence(join)", f"{case}, a seat free" + ", DMs off" * off, inbox.action_consequence(reg, ev, m, "join", by_id=OFFICER_ID))
        m.dm_opt_out = False
        rc.set_signup(reg, rs, ev, m, None, "in", source="test")
        rc.set_signup(reg, rs, ev, held, None, "in", source="test")
        rc.seat_player(reg, ev, ev.signups[str(held.discord_id)])
    # a Confirm card in a DM
    ev = runs["locked recurring, autofill on"]
    m = ms[4]
    for a in m.placement_asks:
        a["dm"] = 77
    for k in ("confirm_yes", "confirm_no", "callout", "no_thanks"):
        dump.add(f"action_consequence({k})", "locked recurring, Confirm card in a DM", inbox.action_consequence(reg, ev, m, k, by_id=OFFICER_ID))

    # ---- the Me page's own Can't make it… (web/api_me.cant_consequence)
    from oibot_gm.web.api_me import QUEUE_NOTE, cant_consequence

    for case in ("locked recurring, autofill on", "locked recurring, autofill off", "locked ad hoc"):
        dump.add("cant_consequence", f"{case}, rostered", cant_consequence(reg, runs[case], True))
        dump.add("cant_consequence", f"{case}, joined, not rostered", cant_consequence(reg, runs[case], False))
    for seated, lead_seated in ((True, True), (True, False), (False, False)):
        dump.add("cant_consequence", f"follow-on locked, {'rostered' if seated else 'not rostered'} here, {'rostered' if lead_seated else 'not rostered'} on the lead",
                 cant_consequence(reg, runs["follow-on locked"], seated, lead_seated))
    for seated in (True, False):  # a lead's Can't takes the follow-on place too
        dump.add("cant_consequence", f"lead locked, {'rostered' if seated else 'not rostered'} on it, a place on the follow-on",
                 cant_consequence(reg, runs["lead locked"], seated, held=[rs.events[runs["lead locked"].followed_by]]))
    dump.add("QUEUE_NOTE", "ad hoc, joined", QUEUE_NOTE)

    # ---- raidcycle's refusals and previews
    for case, ev in runs.items():
        dump.add("cancel_effects via consequence(cancel)", case, cq.consequence("cancel", reg, ev))
        dump.add("remind_refusal", case, rc.remind_refusal(reg, ev) or "")
        dump.add("reopen_refusal", case, rc.reopen_refusal(reg, ev) or "")
        dump.add("end_refusal", case, rc.end_refusal(ev) or "")
    ev = runs["locked recurring, autofill on"]
    dump.add("end_refusal", "locked, after the start", rc.end_refusal(ev, ev.start + timedelta(minutes=5)) or "")
    d = runs["done"]
    dump.add("reopen_refusal", "done, over an hour ago", rc.reopen_refusal(reg, d, d.start + timedelta(days=30)) or "")
    dump.add("closed_run_refusal", "done", rc.closed_run_refusal(rs, d) or "")
    dump.add("closed_run_refusal", "cancelled", rc.closed_run_refusal(rs, runs["cancelled"]) or "")
    # C9: an open whose later raid first opens after the run starts (Task 4)
    when = runs["three raids, first"].start
    reg.set_raid_overrides("onyxias_lair", {"first_open": (when + timedelta(days=9)).astimezone(reg.tz).strftime("%Y-%m-%dT%H:%M")}, "t")
    lineup3 = [{"raid": "hyjal_summit_forever", "rosters": 1}, {"raid": RID, "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}]
    dump.add("before_first_open_sentence", "the third raid opens later", [cq.before_first_open_sentence(reg, lineup3, when) or ""])
    assert cq.before_first_open_sentence(reg, lineup3[:2], when) is None  # every raid open by its start: no line
    # New event (phase 4, Task 7): the refusals and the schedule's-night line its "What this makes" prints
    from oibot_gm import event_view
    dump.add("already_open_sentence", "a night already open", [cq.already_open_sentence(reg, runs["three raids, first"])])
    dump.add("new_event_past_sentence", "a time already past", [cq.new_event_past_sentence(reg, when - timedelta(days=30))])
    dump.add("SCHEDULED_NIGHT", "a schedule's night", [event_view.SCHEDULED_NIGHT.format(name="Tuesday raid night")])
    dump.add("LOCK_CHOICE_REFUSAL", "a lock not on the list", [event_view.LOCK_CHOICE_REFUSAL])
    # a cancel on a later raid once the first raid has finished (fix round 1): only the raids still to play, no "whole"
    later = (reg.now_local() + timedelta(days=6)).replace(hour=18, minute=30, second=0, microsecond=0)
    first = rc.open_run(reg, rs, "hyjal_summit_forever", later, by="t", schedule=rc.ADHOC_SCHEDULE,
                        lineup=[{"raid": RID, "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}])
    first.state = "done"
    rs.save(first, "harness: the first raid finished")
    for k, p in enumerate(rc.event_parts(rs, first)[1:], start=2):
        dump.add("cancel_effects via consequence(cancel)", f"raid {k} of three, the first finished", cq.consequence("cancel", reg, p, rs=rs))
    dump.add("cancel_effects via consequence(cancel)", "three raids, third, with the run store", cq.consequence("cancel", reg, runs["three raids, third"], rs=rs))
    reg.set_raid_overrides("onyxias_lair", {"first_open": None}, "t")
    far = reg.now_local() + timedelta(days=40)
    assert rc.open_row_refusal(reg, rs, RID, far, "barrow-deeps", far - timedelta(days=2)) is None, "no horizon"
    ended = recurring_run(reg, rs, 7)  # a schedule's occurrence whose run is finished: found by its stamps
    ended.state = "done"
    rs.save(ended, "harness")
    dump.add("open_row_refusal", "closed", rc.open_row_refusal(reg, rs, RID, ended.start, ended.schedule, ended.start) or "")
    p = rc.open_preview(reg, rs, RID, None, None)
    dump.add("open_preview", "the next scheduled run", [p["rules"], p["refusal"] or "", p["when_note"]])
    when = (reg.now_local() + timedelta(days=3)).strftime("%Y-%m-%d 21:15")
    p = rc.open_preview(reg, rs, RID, when, rc.ADHOC_SCHEDULE)
    dump.add("open_preview", "an ad hoc run", [p["rules"], p["refusal"] or "", p["when_note"]])
    from oibot_gm.wizard import resolve_local  # noqa: F401 — dst_words is open_preview's when_note
    dump.add("dst_words", "gap", rc.dst_words(reg, "gap", reg.now_local()))
    dump.add("dst_words", "ambiguous", rc.dst_words(reg, "ambiguous", reg.now_local()))

    # ---- absences: what recording one does, and "I'm back"
    for case in ("open recurring", "locked recurring, autofill on"):
        ev = runs[case]
        day = rc.run_day(reg, ev)
        for who in (ms[0], ms[11]):
            dump.add("absence_lines", f"{case}, {'rostered' if ev.seat_of(who) else 'not rostered'}", discord_people.absence_lines(reg, rs, who, day, day))
    m = ms[0]
    ev = runs["open recurring"]
    day = rc.run_day(reg, ev)
    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
    dump.add("absence_clear_lines", "open sheet", discord_people.absence_clear_lines(reg, rs, m, SimpleNamespace(start=day, end=day)))
    ev = runs["locked recurring, autofill on"]
    day2 = rc.run_day(reg, ev)
    ev.signups[str(m.discord_id)].status, ev.signups[str(m.discord_id)].source = "out", "absence"
    dump.add("absence_clear_lines", "locked run", discord_people.absence_clear_lines(reg, rs, m, SimpleNamespace(start=day2, end=day2)))
    ev.signups[str(m.discord_id)].status = "in"
    rc.set_signup(reg, rs, runs["open recurring"], m, None, "in", source="member")

    # ---- character changes ripple into runs
    ev = runs["locked recurring, autofill on"]
    tank = ms[1]
    for change in ({"kind": "retire", "label": tank.main.label}, {"kind": "delete", "label": tank.main.label},
                   {"kind": "spec", "label": tank.main.label, "spec": "Arms", "role": "melee", "old_spec": "Protection", "old_role": "tank"},
                   {"kind": "spec", "label": tank.main.label, "spec": "Protection", "role": "tank", "old_spec": "Protection", "old_role": "tank"},
                   {"kind": "name", "label": tank.main.label, "new_label": "Thornwick Vale"}):
        for e in rc.character_effects(reg, rs, tank, change):
            dump.add(f"character_effects({change['kind']})", f"{e['kind']} on {'a run' if e['ev'] else 'the record'}", e["text"])
    adhoc = runs["locked ad hoc"]
    for e in rc.character_effects(reg, rs, tank, {"kind": "spec", "label": tank.main.label, "spec": "Arms", "role": "melee", "old_spec": "Protection", "old_role": "tank"}):
        if e["ev"] is adhoc:
            dump.add("character_effects(spec)", "role flip on an ad hoc run", e["text"])
    opener = ms[11]
    reg.add_character(opener.discord_id, opener.display_name, "Spareling", "Mage", "Frost", None, False)
    reg.regular_add(opener.discord_id, "barrow-deeps", "t", "Spareling")
    o = runs["open recurring"]
    rc.set_signup(reg, rs, o, opener, "Spareling", "in", source="member")
    for change in ({"kind": "retire", "label": "Spareling"}, {"kind": "spec", "label": "Spareling", "spec": "Arcane", "role": "ranged", "old_spec": "Frost", "old_role": "ranged"},
                   {"kind": "name", "label": "Spareling", "new_label": "Sparrow"}):
        for e in rc.character_effects(reg, rs, opener, change):
            if e["ev"] is o or e["ev"] is None:
                dump.add(f"character_effects({change['kind']})", f"{e['kind']} on {'an open sheet' if e['ev'] else 'the record'}", e["text"])
    rc.set_signup(reg, rs, o, opener, "Spareling", "out", source="member")
    for e in rc.character_effects(reg, rs, opener, {"kind": "retire", "label": "Spareling"}):
        if e["ev"] is o:
            dump.add("character_effects(retire)", "a No thanks moves to another character", e["text"])
    for e in rc.character_effects(reg, rs, opener, {"kind": "main", "label": "Spareling"}):
        dump.add("character_effects(main)", "a new main", e["text"])

    # ---- people (design §5.58): one generator per people verb, by an officer and on the member's own record
    people_cases(reg, rs, runs, ms, dump)

    # ---- the loot council's End and the done line
    from oibot_gm.discord_loot import LootMixin

    ev = runs["locked recurring, autofill on"]
    bot = SimpleNamespace(events={})
    bot.end_sentences = lambda r, s, e: LootMixin.end_sentences(bot, r, s, e)
    dump.add("end_consequence", "no council", LootMixin.end_consequence(bot, reg, rs, ev))
    sess = SimpleNamespace(guild=reg.key, run_key=ev.key)
    bot.events = {"s": sess}
    bot.loot_summary = lambda reg, s: {"awards": 6, "overrides": 1, "never_confirmed": [1], "undistributed": [1, 2], "reasons_pending": [1]}
    dump.add("end_consequence", "a council with loose ends", LootMixin.end_consequence(bot, reg, rs, ev))
    bot.loot_summary = lambda reg, s: {"awards": 1, "overrides": 0, "never_confirmed": [], "undistributed": [], "reasons_pending": []}
    dump.add("end_consequence", "a tidy council", LootMixin.end_consequence(bot, reg, rs, ev))
    dump.add("ended_line", "after End", cq.ended_line(reg, ev))

    # ---- registry
    dump.add("owner_consequence", "the owner hands it over", reg.owner_consequence("Bramble", OWNER))
    from oibot_gm import registry as registry_mod
    monkeypatch.setattr(registry_mod, "is_superuser", lambda uid: uid == 9)
    dump.add("owner_consequence", "a host superuser", reg.owner_consequence("Bramble", 9))
    for kw in ({"bot": True}, {}, {"membership": "no"}, {"membership": "unknown"}):
        uid = OWNER if not kw else REAL
        dump.add("owner_refusal", str(kw or "the current owner"), reg.owner_refusal(uid, by_id=REAL + 5, name="Bramble", **kw) or "")
    dump.add("owner_refusal", "yourself", reg.owner_refusal(REAL + 5, by_id=REAL + 5, name="Bramble") or "")
    for mode in ("", "keep", "move"):
        dump.add("timezone_change_effects", f"mode {mode or 'none'}, every live run", reg.timezone_change_effects("America/New_York", rs, mode))
    rows = reg.timezone_runs("America/New_York", rs)
    picks = {"locked": next(r for r in rows if r["locked"]), "test": next(r for r in rows if r["test"]),
             "open": next(r for r in rows if not r["locked"] and not r["test"])}
    real_runs = reg.timezone_runs
    for what, row in picks.items():
        monkeypatch.setattr(reg, "timezone_runs", lambda zone, rs=None, row=row: [row])
        for mode in ("", "keep", "move"):
            dump.add("timezone_change_effects", f"mode {mode or 'none'}, one {what} run", reg.timezone_change_effects("America/New_York", rs, mode))
        if what == "open":
            monkeypatch.setattr(reg, "timezone_runs", lambda zone, rs=None, row=row: [{**row, "movable": False}])
            dump.add("timezone_change_effects", "mode move, one open run whose lock would pass", reg.timezone_change_effects("America/New_York", rs, "move"))
    monkeypatch.setattr(reg, "timezone_runs", real_runs)
    dump.add("timezone_refusal", "a locked run, no mode", reg.timezone_refusal("America/New_York", rs, "") or "")
    reg.set_schedule("alt", {"slots": "Sat 20:00", "name": "Alt nights"}, "t", raid=RID)
    reg.regular_add(ms[5].discord_id, "alt", "t")
    reg.regular_add(ms[6].discord_id, "alt", "t")
    fx = reg.schedule_removal_effects("alt", rs)
    dump.add("schedule_removal_effects.lines", "two regulars, nothing open", fx.lines())
    dump.add("schedule_removal_effects.sentence", "two regulars, nothing open", fx.sentence())
    fx = reg.schedule_removal_effects("barrow-deeps", rs)
    dump.add("schedule_removal_effects.lines", "the raid's own run times, runs open", fx.lines())
    dump.add("schedule_removal_effects.sentence", "the raid's own run times, runs open", fx.sentence())
    dump.add("aura_reset_sentence", "nothing changed", reg.aura_reset_sentence())
    b = reg.profile.buffs[0]
    reg.set_buff_overrides(b.id, {"note": "ours"}, "t")
    dump.add("aura_reset_sentence", "one buff changed", reg.aura_reset_sentence())
    from oibot_gm import configops

    dump.add("aura_reset_one_sentence", "a changed buff", configops.aura_reset_one_sentence(reg, b.id))
    dump.add("aura_reset_one_sentence", "an unchanged buff", configops.aura_reset_one_sentence(reg, reg.profile.buffs[1].id))
    fam = next(iter(reg.profile.families), None)
    if fam:
        reg.set_family_overrides(fam, {"note": "ours"}, "t")
        dump.add("aura_reset_one_sentence", "a changed family", configops.aura_reset_one_sentence(reg, fam))
    # ---- a raid back to the game's defaults (the raid wizard's Reset)
    other = next(r for r in reg.profile.raids if not reg.raid_override(r))
    dump.add("raid_reset_consequence", "a raid already on the game's defaults", cq.raid_reset_consequence(reg, other))
    dump.add("raid_reset_consequence", "its schedules", cq.raid_reset_consequence(reg, RID))
    reg.set_minimums(RID, {"healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}}, "t")
    reg.set_raid_override(RID, "notes", "Bring flasks.", "t")
    dump.add("raid_reset_consequence", "schedules, minimums and notes, two unsaved edits", cq.raid_reset_consequence(reg, RID, unsaved=2))
    dump.add("raid_reset_consequence", "one unsaved edit", cq.raid_reset_consequence(reg, RID, unsaved=1))
    dump.add("undo_consequence", "rows live", reg.undo_consequence({"live": 3}))
    dump.add("undo_consequence", "already voided", reg.undo_consequence({"live": 0}))
    a = Applicant(discord_id=REAL + 50, display_name="Pell", name="Saffron", cls="Mage", spec="Fire", status="declined", decided_by="Bramble", decided_at=rc.now())
    dump.add("decided_refusal", "declined", reg.decided_refusal(a))
    dump.add("decided_refusal", "none", reg.decided_refusal(None))
    puppet = reg.test_members()[0]
    reg.config.rosters.append({"key": "x-test", "test": True, "test_by": REAL + 1})
    dump.add("puppet_refusal", "another officer's rehearsal", reg.puppet_refusal(puppet, REAL + 2) or "", {puppet.display_name})
    reg.config.rosters.pop()
    dump.add("live_council_refusal", "a live council", reg.live_council_refusal(["Barrow Deeps on Tue 3 Nov"]) or "")

    # ---- configops
    dump.add("schedule_pause_sentence", "pause, nothing open", configops.schedule_pause_sentence(reg, RID, "alt", True, rs))
    dump.add("schedule_pause_sentence", "pause, runs open", configops.schedule_pause_sentence(reg, RID, "barrow-deeps", True, rs))
    dump.add("schedule_pause_sentence", "resume", configops.schedule_pause_sentence(reg, RID, "alt", False, rs))
    for n in (0, 1, 3):  # C10: a schedule's times or week edited while it has runs open
        said = cq.schedule_keeps_sentence(n)
        assert (said is None) == (n == 0)
        dump.add("schedule_keeps_sentence", f"{n} open runs", [said] if said else [])
    Op = configops.ConfigOp
    reg.config.officer_role_ids = [5]
    for op in (Op(op="schedule_remove", target="alt"), Op(op="aura_reset"), Op(op="aura_reset", target=b.id),
               Op(op="role_remove", value="5"), Op(op="recruit", path="open", value="off"),
               Op(op="set", path="timezone", value="America/New_York", field="keep")):
        try:
            got = configops.op_consequences(reg, op)
        except Exception as e:  # noqa: BLE001 — an op this guild can't take still shows what it says
            got = [f"(raised {type(e).__name__})"]
        dump.add(f"op_consequences({op.op})", op.path or op.target or "", got)
    reg.config.signup_channel_id = 11
    reg.config.ops_channel_id = 12
    for path in ("signup_channel", "ops_channel"):
        dump.add("op_consequences(set clear)", path, configops.op_consequences(reg, Op(op="set", path=path, value="none")))
    dump.add("listen_reply_sentence", "a channel starts replying", configops.listen_reply_sentence(11))
    from oibot_gm.memory import Memory
    own = Memory(id="m-1", guild=reg.key, channel=11, channel_name="general", scope="shared", author_id=REAL, author="Bramble", at="2026-09-25T15:52:00+00:00", text="x")
    dump.add("forget_consequence", "your own words", cq.forget_consequence(own, REAL))
    dump.add("forget_consequence", "an officer forgets another's words", cq.forget_consequence(own, OFFICER_ID))
    for n in (0, 1, 212):
        dump.add("memory_off_consequence", f"{n} memories", cq.memory_off_consequence(reg, 11, n))
    dump.add("memory_shared_consequence", "the officers' channel remembers for everyone", cq.memory_shared_consequence(reg, 12))
    dump.add("forget_consequence", "an evening in the guild's zone", cq.forget_consequence(own, REAL, reg.tz))
    dump.add("op_consequences(plain listen)", "reply", configops.op_consequences(reg, Op(op="plain", target="listen <#11>", value="reply")))
    dump.add("_timezone_consequences", "move", configops._timezone_consequences(reg, Op(op="set", path="timezone", value="America/New_York", field="move")))
    d = configops.describe_ops(reg, [Op(op="schedule_remove", target="alt")])
    dump.add("describe_ops consequences", "schedule remove", d.consequences)
    dump.add("plain-text diff (as_discord)", "schedule remove", [x for x in d.as_discord().splitlines() if x.startswith("⚠")])

    # ---- discord_registry
    from oibot_gm import discord_registry

    dump.add("remove_record_consequence", "a member who left", discord_registry.remove_record_consequence(reg, ms[11]))
    ap = Applicant(discord_id=REAL + 51, display_name="Pell", name="Saffron", cls="Mage", spec="Fire")
    dump.add("application_consequence", "accept, no member role", discord_registry.application_consequence(reg, ap, True))
    reg.config.member_role_ids = [33]
    dump.add("application_consequence", "accept, member role", discord_registry.application_consequence(reg, ap, True))
    dump.add("application_consequence", "decline", discord_registry.application_consequence(reg, ap, False))
    ap.history = [{"status": "declined", "at": rc.now(), "by": "Bramble", "note": "full on mages"}]
    dump.add("application_consequence", "decline, applied before", discord_registry.application_consequence(reg, ap, False))

    # ---- discord_pool
    from oibot_gm import discord_pool

    for kind in ("signup", "registration", "absences", "analytics", "applications", "roster", "ops", "news", "loot"):
        cur = getattr(reg.config, discord_pool.CHANNEL_KINDS[kind], None)
        dump.add("channel_consequences", f"{kind} → a new channel", discord_pool.channel_consequences(reg, kind, 99))
        setattr(reg.config, discord_pool.CHANNEL_KINDS[kind], 98)
        dump.add("channel_consequences", f"{kind} cleared", discord_pool.channel_consequences(reg, kind, None))
        dump.add("channel_consequences", f"{kind} the same", discord_pool.channel_consequences(reg, kind, 98))
        dump.add("channel_consequences", f"{kind} moved", discord_pool.channel_consequences(reg, kind, 97))
        setattr(reg.config, discord_pool.CHANNEL_KINDS[kind], cur)
    from oibot_gm import perms

    dump.add("cant_post", "one missing", perms.cant_post("raids", ["embed_links"]))
    dump.add("cant_post", "two missing, one with a reason", perms.cant_post("raids", ["send_messages", "manage_messages"]))
    dump.add("cant_post", "three missing, two with reasons", perms.cant_post("raids", ["send_messages", "manage_messages", "create_public_threads"]))
    roles = {1: SimpleNamespace(is_default=lambda: True, managed=False, name="everyone"),
             2: SimpleNamespace(is_default=lambda: False, managed=True, name="Carl-bot")}
    gfake = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(get_role=lambda rid: roles.get(rid)))
    for rid in (1, 2, 3):
        dump.add("role_refusal", str(rid), discord_pool.SetupMixin.role_refusal(gfake, reg, rid) or "")

    # ---- feed (companion tokens)
    from oibot_gm import feed

    dump.add("issue_consequence", "DMed", feed.issue_consequence(reg, "Raid PC", "Bramble"))
    dump.add("issue_consequence", "shown once", feed.issue_consequence(reg, "Raid PC"))
    rec = SimpleNamespace(label="Raid PC")
    for state in ("live", "disconnected", None):
        dump.add("revoke_consequence", str(state), feed.revoke_consequence(reg, rec, {"state": state, "characters": ["Ashgrove"]} if state else None))

    # ---- the server's guild in the armory (armory link §5.1)
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from oibot_gm.armory import GuildFacts

    born = _dt(2026, 10, 26, 18, 0, tzinfo=_tz.utc)
    wardens = GuildFacts(id=418362, slug="lantern-wardens", name="Lantern Wardens", faction="Horde", kind="Normal", ruleset_id=2, members=8, created=born)
    hall = GuildFacts(id=999001, slug="the-long-hall-of-ash", name="The Long Hall of Ash", faction="Alliance", kind="Roleplay PvP", ruleset_id=15, members=1, created=born)
    guild_names = {"Lantern", "The"}
    dump.add("armory_guild_consequence", "nothing linked yet", cq.armory_guild_consequence(reg, wardens), guild_names)
    dump.add("armory_guild_clear_consequence", "nothing linked", cq.armory_guild_clear_consequence(reg), guild_names)
    reg.set_armory_guild(wardens, "Owner", by_id=OWNER)
    dump.add("armory_guild_consequence", "the same guild again", cq.armory_guild_consequence(reg, wardens), guild_names)
    dump.add("armory_guild_consequence", "another guild, a long name, one member", cq.armory_guild_consequence(reg, hall), guild_names)
    dump.add("armory_guild_clear_consequence", "a guild linked", cq.armory_guild_clear_consequence(reg), guild_names)

    # ---- the guild's auction house (auction prices §4.1): the pick, prices off, and a re-link that clears it (C11)
    from oibot_gm.auctions import House

    horde_ah, other_ah = House(id=6, name="Horde Auction House"), House(id=18, name="Horde Auction House")
    house_names = guild_names | {"Horde", "Auction", "House", "Members", "Prices", "Professions", "Discord"}
    dump.add("market_consequence", "nothing picked yet", cq.market_consequence(reg, horde_ah), house_names)
    dump.add("market_clear_consequence", "nothing picked", cq.market_clear_consequence(reg), house_names)
    reg.set_market(horde_ah, 2, "Owner", by_id=OWNER)
    dump.add("market_consequence", "the same house again", cq.market_consequence(reg, horde_ah), house_names)
    dump.add("market_consequence", "another house of the same name", cq.market_consequence(reg, other_ah), house_names)
    dump.add("market_clear_consequence", "a house picked", cq.market_clear_consequence(reg), house_names)
    dump.add("armory_guild_consequence", "the same ruleset and faction keep the auction house", cq.armory_guild_consequence(reg, wardens), house_names)
    dump.add("armory_guild_consequence", "another faction and ruleset clear the auction house", cq.armory_guild_consequence(reg, hall), house_names)
    dump.add("armory_guild_clear_consequence", "a guild linked with an auction house", cq.armory_guild_clear_consequence(reg), house_names)
    reg.clear_armory_guild("Owner", by_id=OWNER)

    # ---- a character's armory link (armory link §5.2): link, unlink, the "keep professions by hand" switch, refusals
    from oibot_gm import discord_armory
    from oibot_gm.discord_armory import Found

    am = ms[0]
    ac = am.main
    same = Found(id=700101, slug="x", name=ac.name, surname=ac.surname, cls=ac.cls, level=60, source="profile")
    other = Found(id=700102, slug="x", name="Bram", surname="Kettle", cls="Warrior", level=60, source="profile")
    low = Found(id=700105, slug="x", name="Pip", surname="Wobble", cls=None, level=1, source="roster")
    low_same = Found(id=700105, slug="x", name=ac.name, surname=ac.surname, cls="Rogue", level=7, source="roster")
    link_names = {"Bram", "Pip"}
    for who, by in (("own", am.discord_id), ("an officer", OFFICER_ID)):
        dump.add("armory_link_consequence", f"{who}, a new name", cq.armory_link_consequence(reg, am, ac, other, by), link_names)
        dump.add("armory_link_consequence", f"{who}, the same name", cq.armory_link_consequence(reg, am, ac, same, by), link_names)
        dump.add("armory_link_consequence", f"{who}, below level 10, no class", cq.armory_link_consequence(reg, am, ac, low, by), link_names)
        dump.add("armory_link_consequence", f"{who}, below level 10, the same name", cq.armory_link_consequence(reg, am, ac, low_same, by), link_names)
        dump.add("armory_unlink_consequence", who, cq.armory_unlink_consequence(reg, am, ac, by), link_names)
    for on in (True, False):
        dump.add("profession_sync_consequence", f"on={on}", cq.profession_sync_consequence(on, ac.label), link_names)
        dump.add("profession_sync_consequence", f"on={on}, no name", cq.profession_sync_consequence(on), link_names)
        dump.add("profession_sync_consequence", f"on={on}, already so", cq.profession_sync_consequence(on, ac.label, current=on), link_names)
    for text in (discord_armory.NOT_FOUND, discord_armory.SYNC_LIMITED, *discord_armory.OFFICER_ONLY.values(),
                 "That armory character is already linked here. Ask an officer.", "A test character can't be linked to the armory.", "Bram Kettle is held by a test character. Clear the test bench first.",
                 "Bram Kettle in the armory is linked to Bramble's Bram Kettle. Unlink it there first.",
                 "Bram Kettle's professions come from the armory, so turn on Keep professions by hand to edit them."):
        dump.add("armory refusal", text[:24], [text], link_names)

    # ---- a character moved to another member (armory link §5.4): the confirm, then every refusal the verb says
    to = ms[1]

    def mv_char(label, *, main=False, rosters=()):
        return SimpleNamespace(label=label, name=label.split()[0], is_main=main, status="active", rosters=list(rosters))

    def mv_member(name, chars):
        main = next((x for x in chars if x.is_main), None)
        return SimpleNamespace(display_name=name, characters=chars, main=main, active=lambda: list(chars))

    spot, spot2 = "barrow-deeps", "alt"
    bram = mv_char("Bram Kettle", main=True, rosters=[spot])
    wren = mv_char("Wren Hollow")
    giver = mv_member("Bramble", [bram, wren])
    for case, c, frm, recv in (
            ("the main, a regular, to a member with a main", bram, giver, mv_member("cbottoms", [mv_char("Ivo Marsh", main=True)])),
            ("an alt, no regular spot", wren, giver, mv_member("cbottoms", [mv_char("Ivo Marsh", main=True)])),
            ("an alt to a member with no main", wren, giver, mv_member("cbottoms", [])),
            ("the main, its spot given up", bram, giver, mv_member("cbottoms", [mv_char("Ivo Marsh", main=True, rosters=[spot])])),
            ("the main to a member with no main, its only character", mv_char("Bram Kettle", main=True), mv_member("Bramble", []), mv_member("cbottoms", [])),
            ("the main, a spot given up, to a member with no main", bram, giver, mv_member("cbottoms", [mv_char("Ivo Marsh", rosters=[spot])])),
            ("two spots given up", mv_char("Bram Kettle", rosters=[spot, spot2]), giver,
             mv_member("cbottoms", [mv_char("Ivo Marsh", main=True, rosters=[spot, spot2])]))):
        dump.add("move_consequence", case, cq.move_consequence(reg, frm, c, recv), link_names | {"Wren", "Ivo"})
    dump.add("move_consequence", "a real record", cq.move_consequence(reg, am, ac, to), link_names)
    a_run = SimpleNamespace(instance=RID, team=RID, start=reg.now_local() + timedelta(days=3))
    for text in ("They have no registered characters yet.", f"{am.display_name} has no character named Bram Kettle.",
                 "The new member has to register here first.", f"Bram Kettle is already {am.display_name}'s.",
                 "A test character never moves to or from a real member.", "Ask another officer to move it to you.",
                 f"{to.display_name} has left the server.", f"{to.display_name} already has a character called Bram Kettle.",
                 "Bram Kettle has an open ask. Withdraw it first.",
                 f"{to.display_name} is already on {rc.run_words(reg, a_run)}, so sort that out first."):
        dump.add("move refusal", text[:24], [text], link_names)

    # ---- the site's armory routes (armory link §5.4, §5.5; web/api_armory.py): Give to a member…, the seed's confirm,
    # Sync now's answer and the routes' own refusals
    from oibot_gm.web import api_armory

    for main in (True, False):
        dump.add("armory_give_consequence", f"profile, main={main}", cq.armory_give_consequence(reg, am, other, main=main), link_names)
        dump.add("armory_give_consequence", f"roster, no class, main={main}", cq.armory_give_consequence(reg, am, low, main=main), link_names)
    for n, k in ((1, 0), (2, 0), (12, 0), (1, 1), (3, 2), (0, 4)):
        dump.add("armory_seed_consequence", f"{n} picks, {k} refused", cq.armory_seed_consequence(n, k), link_names)
    for result in ("updated", "unchanged", "missing", "limited"):
        dump.add("armory_sync_sentence", result, [cq.armory_sync_sentence("Bram Kettle", result)], link_names)
    from oibot_gm import armory_seed
    from oibot_gm.registry import Registry as _Reg

    for text in (api_armory.LOOK_FIRST, api_armory.FOUND_ANOTHER, api_armory.NO_GUILD_YET, api_armory.NOT_READ_YET, api_armory.SEED_CHANGED,
                 api_armory.NO_PICKS, api_armory.TOO_MANY_LOOKUPS, api_armory.PICK_ON, api_armory.PICK_SHAPE, api_armory.PICK_ROW, api_armory.PICK_TO,
                 _Reg.GIVE_NOT_REGISTERED, armory_seed.GIVE_OFFICER_ONLY, armory_seed.SPEC_NEEDED.format(name="Bram Kettle"),
                 armory_seed.NO_CLASS.format(name="Bram Kettle"), armory_seed.LINK_FAILED,
                 f"Bram Kettle is registered to {am.display_name} but not linked. {armory_seed.LINK_FAILED}"):
        dump.add("armory route refusal", text[:24], [text], link_names)
    armory_long_names(reg, rs, dump)

    # ---- appeals
    from oibot_gm import appeals

    award = SimpleNamespace(item_name="Robe of Tests", item_id=1)
    dump.add("open_consequence", "with an officers' channel", appeals.open_consequence(reg, award))
    reg.config.roster_channel_id = reg.config.ops_channel_id = None
    dump.add("open_consequence", "no officers' channel", appeals.open_consequence(reg, award))
    apl = SimpleNamespace(member_name="Bramble", item="Robe of Tests")
    for outcome in appeals.OUTCOMES:
        dump.add("resolve_consequence", outcome, appeals.resolve_consequence(reg, apl, outcome))

    # ---- the guild guide library (design §5.63)
    from oibot_gm.guides import library as guide_lib
    from oibot_gm.guides.rxp_decode import encode as guide_encode

    for faction in guide_lib.FACTIONS:
        dump.add("upload_consequence", faction, guide_lib.upload_consequence(reg, faction))
    gtag, uploader = "Quillwright#48213", ms[0]
    gtext = guide_encode(["#name 1-6 A\nstep\n.xp 2\n"], gtag)
    from datetime import datetime, timezone

    gclock = [datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)]

    def gnow():  # each upload a day after the last, so the rendered dates differ as they would
        gclock[0] += timedelta(days=1)
        return gclock[0]

    monkeypatch.setattr(guide_lib, "_now", gnow)
    for faction in ("horde", "both"):
        older = guide_lib.upload(reg, gtext, gtag, faction, uploader.discord_id, uploader.display_name)
        newer = guide_lib.upload(reg, gtext, gtag, faction, uploader.discord_id, uploader.display_name)
        dump.add("withdraw_consequence", f"{faction}, the latest, an older one live", guide_lib.withdraw_consequence(reg, newer, by_id=OFFICER_ID))
        dump.add("withdraw_consequence", f"{faction}, the latest, by its uploader", guide_lib.withdraw_consequence(reg, newer, by_id=uploader.discord_id))
        dump.add("withdraw_consequence", f"{faction}, an older one", guide_lib.withdraw_consequence(reg, older, by_id=OFFICER_ID))
        guide_lib.withdraw(reg, newer.id, "Offi", OFFICER_ID)
        dump.add("withdraw_consequence", f"{faction}, the last one live", guide_lib.withdraw_consequence(reg, older, by_id=OFFICER_ID))
        dump.add("restore_consequence", f"{faction}, the newest", guide_lib.restore_consequence(reg, guide_lib.get(reg, newer.id)))
        guide_lib.withdraw(reg, older.id, "Offi", OFFICER_ID)
        guide_lib.restore(reg, newer.id, "Offi", OFFICER_ID)
        dump.add("restore_consequence", f"{faction}, an older one", guide_lib.restore_consequence(reg, guide_lib.get(reg, older.id)))
        dump.add("guide notice", faction, guide_lib.notice_text(reg, older, "Offi"))
    gone = guide_lib.upload(reg, gtext, gtag, "alliance", 480_000_000_000_000_999, "Gone")  # an uploader with no member record
    dump.add("withdraw_consequence", "alliance, an uploader with no record", guide_lib.withdraw_consequence(reg, gone, by_id=OFFICER_ID))
    dump.add("withdraw_consequence", "horde, the Discord escape",
             guide_lib.withdraw_consequence(reg, guide_lib.latest(reg, "horde"), by_id=OFFICER_ID, esc=lambda t: t.replace("_", "\\_")))
    for text in (guide_lib.BAD_FACTION, guide_lib.DAILY_CAP, guide_lib.NO_VERSION, guide_lib.WITHDRAWN, guide_lib.NOT_WITHDRAWN, guide_lib.BUSY,
                 guide_lib.STORED_TOO_BIG, guide_lib.LIBRARY_FULL):
        dump.add("guide refusal", text[:24], text)

    # ---- news proposals
    from oibot_gm import news
    from oibot_gm.news import Proposal

    for kind in ("guild_setting", "profile", "needs_developer"):
        p = Proposal(id="261026-abcd", title="t", affects="timezone", kind=kind, change="Raise the raid size to 12", news=[], state="proposed",
                     edit={"op": "set", "path": "timezone", "value": "America/New_York"} if kind == "guild_setting" else {})
        for state in ("approved", "dismissed"):
            dump.add(f"news.consequence({state})", kind, news.consequence(p, reg, state, None, busy={reg.key: {"raiding": True, "why": "Barrow Deeps is locked"}},
                                                                            next_run="Tue 7:45 PM"))
        p.state = "approved"
        dump.add("news.consequence(dismissed)", f"{kind}, withdrawing an approval", news.consequence(p, reg, "dismissed"))
        dump.add("news.consequence(interrupted)", kind, news.consequence(p, reg, "interrupted"))
    for text in (news.PROFILE_OWNER_ONLY, news.SETTING_OWNER_ONLY, news.INTERRUPTED):
        dump.add("news refusal", text[:20], text)
    p.decided_by, p.decided_at = "Bramble", rc.now()
    dump.add("news.already_text", "approved", news.already_text(p, reg.local12))
    dump.add("news.profile_approval_refusal", "an officer approved it", news.profile_approval_refusal(None, reg, SimpleNamespace(kind="profile", decided_by_id=5, decided_by="Offi")) or "")

    # ---- flows_misc.test_clear_consequence
    from oibot_gm import flows_misc

    dump.add("test_clear_consequence", "puppets and a test run", flows_misc.test_clear_consequence(reg, rs))

    # ---- moving a run and its own sheet timing (phase 3, Task 5)
    reschedule_cases(reg, rs, runs, ms, dump)

    # ---- editing a run's lineup before the lock (phase 3, Task 6)
    lineup_cases(reg, rs, runs, ms, dump, monkeypatch)

    # ---- the confirmation boxes (phase 4, Task 1; design §7b)
    box_cases(reg, rs, runs, ms, dump)

    dump.write()
    assert not dump.problems, f"{len(dump.problems)} confirm sentences break the Confirm rule:\n" + "\n".join(dump.problems)


def lock_choice_cases(reg, rs, add):
    """The lock box's fill choice (issue 14, Task 3): every as-it-stands case beside its auto-fill box, with the
    sentences a raid leader reads asserted — the open slots first, who is rostered, the pause or who is told, by when."""
    ms = [reg.members[REAL + i] for i in range(len(PEOPLE))]
    dms = {m.discord_id: m.dm_opt_out for m in ms}
    for m in ms:
        m.dm_opt_out = False
    sid = _weekly(reg, RID, ["Mon 19:30", "Wed 19:30", "Fri 19:30"], sid="choice")
    now = reg.now_local()
    nights = iter([t for _s, t in rc.occurrences(reg, now, 24 * 21) if _s["id"] == sid and t - now >= timedelta(hours=30)])
    names = [m.display_name for m in ms]

    def night(placed: int, joined: int = 10):
        ev = rc.open_run(reg, rs, RID, next(nights), by="t", schedule=sid)
        join(reg, rs, ev, ms[:joined])
        ev.layout = [names[:min(placed, 5)], names[5:placed]] if placed else None
        return ev

    def both(case, ev, want=None, want_fill=None):
        b, f = cq.lock_box(reg, ev, rs), cq.lock_box(reg, ev, rs, fill=True)
        assert want is None or b["sentences"] == want, (case, b["sentences"])
        assert want_fill is None or f["sentences"] == want_fill, (case, f["sentences"])
        add("lock_box", f"{case}, as it stands", b)
        add("lock_box", f"{case}, auto-fill ticked", f)
        return b

    ev = night(6)
    both("six of ten placed, asks on", ev, ["The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq.LOCK_PAUSE],
         ["The roster has 4 open slots.", "Everyone on the board keeps their place, and the lock fills the open slots from Not placed.",
          f"Everyone rostered gets {cq.CONFIRM_DM}."])
    ev.autofill_paused = True
    both("six of ten placed, asks paused", ev, ["The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq._lock_deadline(reg, ev)])
    ev.autofill_paused = False
    rc.reschedule(reg, rs, ev, timing={"autofill": False}, by="t")
    both("six of ten placed, asks off", rs.events[ev.key], ["The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq._lock_deadline(reg, ev)])
    ms[1].dm_opt_out = True
    both("six of ten placed, asks off, one with DMs off", rs.events[ev.key], ["The roster has 4 open slots.",
         "Only the 6 people on the board are rostered, and 1 of them sees the ask on the site.", cq._lock_deadline(reg, ev)])
    for m in ms[:6]:
        m.dm_opt_out = True
    both("six of ten placed, asks off, everyone with DMs off", rs.events[ev.key], ["The roster has 4 open slots.",
         "Only the 6 people on the board are rostered, and all see the ask on the site.", cq._lock_deadline(reg, ev)])
    for m in ms[:6]:
        m.dm_opt_out = False
    ev = night(6, joined=6)  # everyone who answered placed, four seats spare: the choice all the same (the owner's ruling)
    both("everyone placed, spare slots, asks on", ev, ["The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq.LOCK_PAUSE],
         ["The roster has 4 open slots.", f"The 6 rostered get {cq.CONFIRM_DM}.", cq.LOCK_ASKS])
    rc.reschedule(reg, rs, ev, timing={"autofill": False}, by="t")
    both("everyone placed, spare slots, asks off", rs.events[ev.key], None,
         ["The roster has 4 open slots.", f"The 6 rostered get {cq.CONFIRM_DM}.", cq._lock_deadline(reg, ev)])
    ev = night(1, joined=1)
    both("one person, placed", ev, ["The roster has 9 open slots.",
         f"Only the one person on the board is rostered and gets {cq.CONFIRM_DM}.", cq.LOCK_PAUSE])
    ev = night(6)
    ev.pins[str(ms[8].discord_id)] = "in"
    both("one pinned in off the board", ev, [f"The roster has 4 open slots, and {names[8]} is not rostered.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq.LOCK_PAUSE])
    for i in (6, 7, 9):
        ev.pins[str(ms[i].discord_id)] = "in"
    both("four pinned in off the board", ev, [f"The roster has 4 open slots, and {names[8]}, {names[6]} and 2 others are not rostered.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq.LOCK_PAUSE])
    ev = night(9, joined=9)  # every seat placed, one by Auto-fill from the bench: no open slot, the board is not complete
    join(reg, rs, ev, ms[9:10], "sub")
    ev.layout, ev.auto_placed = [names[:5], names[5:10]], [names[9]]
    b = both("every seat placed, a Bench place Auto-fill made", ev, [cq.AUTO_AGAIN, f"Everyone rostered gets {cq.CONFIRM_DM}.", cq._lock_deadline(reg, ev)])
    assert "choice" not in b, "no open slot: today's box, no choice"
    ev = night(0)
    both("nobody placed", ev, [rc.LOCK_EMPTY_BOARD])
    ev = rc.open_run(reg, rs, RID, next(nights), by="t", schedule=sid)
    both("nobody joined", ev, ["The roster has 10 open slots.", "Nobody is on the board, so the roster locks empty.", cq.LOCK_PAUSE])
    ev = night(4, joined=12)  # two rosters expected (12 joined): roster 1 part-filled, roster 2 empty is not built
    k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    ev.layout = [names[:2], names[2:4]] + [[] for _ in range(k - 2)] + [names[4:7]]
    both("two rosters, both part-filled", ev)
    one = rc.open_run(reg, rs, RID, (now + timedelta(days=9)).replace(hour=20, minute=0, second=0, microsecond=0), by="t",
                      schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, one, ms[:8])
    one.layout = [names[:5]]
    both("one-off, five of eight placed", one, ["The roster has 5 open slots.",
         f"Only the 5 people on the board are rostered and get {cq.CONFIRM_DM}.", cq.LOCK_PAUSE])
    rc.reschedule(reg, rs, one, timing={"autofill": False, "confirm": False}, by="t")
    both("one-off, asks and confirmation off", rs.events[one.key], ["The roster has 5 open slots.",
         "Only the 5 people on the board are rostered, and nobody is asked to confirm."])
    reg.set_raid_overrides("hyjal_summit_forever", {"first_open": "2026-01-01T00:00"}, "t")
    lead = rc.open_run(reg, rs, "hyjal_summit_forever", (now + timedelta(days=11)).replace(hour=18, minute=30, second=0, microsecond=0),
                       by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": RID, "rosters": 2})
    join(reg, rs, lead, ms)
    fev = rs.events[lead.followed_by]
    lead.layout = [names[:5]]
    both("two raids, the later board empty", lead, [rc.LOCK_EMPTY_RAID.format(raid="Barrow Deeps")])
    fev.layout = [names[:5], names[5:10]]
    both("two raids, open slots on the first", lead, ["The Hyjal Summit roster has 15 open slots.",
                                                      "Only the people on each raid's board are rostered, and nobody is asked to confirm.", cq.LOCK_PAUSE])
    fev.layout = [names[:3]]
    both("two raids, open slots on both", lead, ["The rosters have 22 open slots across 2 raids.",
                                                 "Only the people on each raid's board are rostered, and nobody is asked to confirm.", cq.LOCK_PAUSE])
    fev.pins[str(ms[11].discord_id)] = "in"
    both("two raids, one pinned in off the later board", lead, ["The rosters have 22 open slots across 2 raids, and Orrin is not rostered.",
         "Only the people on each raid's board are rostered, and nobody is asked to confirm.", cq.LOCK_PAUSE])
    rc.reschedule(reg, rs, lead, timing={"confirm": True}, by="t")
    both("two raids, confirmation on", rs.events[lead.key])
    reg.seed_test_members(6, "t")  # a test event: the puppets' DMs come to the tester
    t = open_test_run(reg, rs, days=13)
    join(reg, rs, t, reg.test_members()[:6])
    t.layout = [[m.display_name for m in reg.test_members()[:3]]]
    both("a test event, three of six placed", t)
    for m in ms:
        m.dm_opt_out = dms[m.discord_id]


def relock_cases(reg, rs, add):
    """Issue 14, Task 4 fix round 1: the lock box on an event unlocked once (rc.confirm_plan: who THIS lock asks, read by
    the lock's fan-out too) — everyone confirmed (nobody new is asked), a newcomer and a confirmed name left off (the ask
    that closes is said), two left off holding open asks, a newcomer with DMs off, a lineup, each beside its auto-fill box.
    A first lock reads as lock_choice_cases pins it."""
    ms = [reg.members[REAL + i] for i in range(len(PEOPLE))]
    dms = {m.discord_id: m.dm_opt_out for m in ms}
    for m in ms:
        m.dm_opt_out = False
    names = [m.display_name for m in ms]
    sid = _weekly(reg, RID, ["Tue 19:30", "Thu 19:30", "Sat 19:30"], sid="relock")
    now = reg.now_local()
    nights = iter([t for _s, t in rc.occurrences(reg, now, 24 * 21) if _s["id"] == sid and t - now >= timedelta(hours=30)])

    def unlocked(rostered: int, answer="yes"):
        ev = rc.open_run(reg, rs, RID, next(nights), by="t", schedule=sid)
        join(reg, rs, ev, ms[:12])
        lock_with_board(reg, rs, ev, names[:rostered])
        for m in ms[:rostered]:
            m.placement_asks.append({"roster": ev.team, "character": m.main.label, "asked_at": rc.now(), "answer": answer,
                                     "answered_at": rc.now() if answer else None, "by": "lock", "channel": "dm"})
        rc.unlock(reg, rs, ev, "t")
        return rs.events[ev.key]

    def both(case, ev, want=None):
        b, f = cq.lock_box(reg, ev, rs), cq.lock_box(reg, ev, rs, fill=True)
        assert want is None or b["sentences"] == want, (case, b["sentences"])
        add("lock_box", f"after an unlock, {case}, as it stands", b)
        add("lock_box", f"after an unlock, {case}, auto-fill ticked", f)

    ev = unlocked(8)
    both("everyone confirmed", ev, ["The roster has 2 open slots.", "Only the 8 on the board are rostered, and nobody new is asked to confirm.", cq.LOCK_PAUSE])
    ev = unlocked(8)
    ev.layout = [[n for n in g if n != names[0]] for g in ev.layout]
    ev.layout[-1].append(names[9])
    rs.save(ev, "board (test)")
    both("a newcomer, a confirmed name left off", rs.events[ev.key],
         ["The roster has 2 open slots.", f"Only the 8 on the board are rostered, and 1 newly rostered gets {cq.CONFIRM_DM}.",
          f"{names[0]} is left off the roster, so their Confirm ask closes."])
    ev = unlocked(8, answer=None)
    ev.layout = [[n for n in g if n not in names[:2]] for g in ev.layout]
    ev.layout[-1] += names[9:11]
    rs.save(ev, "board (test)")
    both("two newcomers, two left off holding asks", rs.events[ev.key],
         ["The roster has 2 open slots.", f"Only the 8 on the board are rostered, and 2 newly rostered get {cq.CONFIRM_DM}.",
          "The 2 people left off the roster have their Confirm ask closed."])
    ms[9].dm_opt_out = True
    ev = unlocked(8)
    ev.layout[-1].append(names[9])
    rs.save(ev, "board (test)")
    both("a newcomer with DMs off", rs.events[ev.key],
         ["The roster has 1 open slot.", "Only the 9 on the board are rostered, and 1 newly rostered is asked to confirm.", cq.LOCK_PAUSE])
    ms[9].dm_opt_out = False
    lead = rc.open_run(reg, rs, "hyjal_summit_forever", (now + timedelta(days=16)).replace(hour=18, minute=30, second=0, microsecond=0),
                       by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": RID, "rosters": 2})  # lock_choice_cases opened Hyjal
    rc.reschedule(reg, rs, lead, timing={"confirm": True}, by="t")
    join(reg, rs, rs.events[lead.key], ms)
    lead = rs.events[lead.key]
    fev = rc.follow_of(rs, lead)
    assert fev is not None, (lead.instance, lead.followed_by)
    lock_with_board(reg, rs, lead, names[:6])
    lock_with_board(reg, rs, fev, names[:5])
    for m in ms[:6]:
        m.placement_asks.append({"roster": lead.team, "character": m.main.label, "asked_at": rc.now(), "answer": "yes",
                                 "answered_at": rc.now(), "by": "lock", "channel": "dm"})
    rc.unlock(reg, rs, lead, "t")
    lead = rs.events[lead.key]
    lead.layout[-1].append(names[7])
    rs.save(lead, "board (test)")
    both("a lineup, one newcomer on the first raid", rs.events[lead.key])
    b = cq.lock_box(reg, rs.events[lead.key], rs)
    assert "1 newly rostered for the first raid is asked to confirm." in b["sentences"][1], b
    for m in ms:
        m.dm_opt_out = dms[m.discord_id]


def unlock_cases(reg, rs, add):
    """Issue 14, Task 5: the Unlock box (consequences.unlock_box) — nothing out, confirmation asks open (one, many), fill
    asks out (one, many, and with asks open too: the fill asks are said), a lineup (the whole event), and each refusal."""
    ev = open_test_run(reg, rs, days=13)
    pool = reg.test_members()[:10]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool[:8]])
    for m in pool:  # nothing left over from another case on this roster
        m.placement_asks = [a for a in m.placement_asks if a["roster"] != ev.team]
    add("unlock_box", "nothing out", cq.unlock_box(reg, ev, rs))
    for n, case in ((1, "one confirmation ask open"), (8, "eight confirmation asks open")):
        for m in pool[:n]:
            m.placement_asks.append({"roster": ev.team, "answer": None, "asked_at": rc.now(), "channel": "dm"})
        b = cq.unlock_box(reg, ev, rs)
        assert b["sentences"][1] == f"Nobody gets a DM, and {n} confirmation ask{'s stay' if n > 1 else ' stays'} open.", b
        add("unlock_box", case, b)
        for m in pool[:n]:
            m.placement_asks = [a for a in m.placement_asks if a["roster"] != ev.team]
    for n, case in ((1, "one fill ask out"), (2, "two fill asks out")):
        ev.fill_asks = [rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="dps",
                                   character=m.main.label, spec=m.main.spec, reason="short 1") for m in pool[8:8 + n]]
        add("unlock_box", case, cq.unlock_box(reg, ev, rs))
    pool[0].placement_asks.append({"roster": ev.team, "answer": None, "asked_at": rc.now(), "channel": "dm"})
    b = cq.unlock_box(reg, ev, rs)
    assert "withdrawn" in b["sentences"][1] and "confirmation" not in b["sentences"][1], b
    add("unlock_box", "fill asks out and a confirmation ask open", b)
    four = next(e for e in rs.events.values() if e.state == "open" and not rc.is_follow(e) and len(rc.event_parts(rs, e)) == 4)
    add("unlock_box", "an open event", cq.unlock_box(reg, four, rs))
    for p in rc.event_parts(rs, four):
        lock_with_board(reg, rs, p, [])
    b = cq.unlock_box(reg, rc.event_parts(rs, four)[2], rs)
    assert b["sentences"][0].startswith("The whole event"), b
    add("unlock_box", "four raids", b)
    d = rs.store.root / rs.key / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ev.key}.json").write_text("{}")
    add("unlock_box", "a loot council open", cq.unlock_box(reg, ev, rs))
    (d / f"{ev.key}.json").unlink()
    rc.end_run(reg, rs, ev, "t")
    add("unlock_box", "an ended event", cq.unlock_box(reg, ev, rs))


def box_cases(reg, rs, runs, ms, dump):
    """Every box generator across its cases (each variant of the §7b table, DMs on and off, 0 / 1 / many): the title is
    linted as a line of its own, then the sentences as one confirm."""
    hy, ony = "hyjal_summit_forever", "onyxias_lair"

    def add(generator, case, b):
        assert set(b) - {"choice"} == {"title", "sentences", "verb", "danger", "diff", "fp"}, generator
        assert "choice" not in b or (generator == "lock_box" and set(b["choice"]) == {"label", "value"}), generator
        assert b["verb"] is not None or len(b["sentences"]) == 1, f"{generator} · {case}: a refusal is one sentence"
        title = b["title"] if b["title"][-1:] in ".?!" else b["title"] + "."  # a heading: linted for case, length and glue
        dump.add(f"{generator} title", case, [title])
        dump.add(generator, case, b["sentences"])

    for case, ev in runs.items():
        add("lock_box", case, cq.lock_box(reg, ev, rs))
        add("lock_box", f"{case}, auto-fill ticked", cq.lock_box(reg, ev, rs, fill=True))
        add("cancel_box", case, cq.cancel_box(reg, ev, rs))
    four = next(e for e in rs.events.values() if e.state == "open" and not rc.is_follow(e) and len(rc.event_parts(rs, e)) == 4)
    add("lock_box", "four raids", cq.lock_box(reg, four, rs))  # lineup_cases' four-raid event
    add("lock_box", "four raids, auto-fill ticked", cq.lock_box(reg, four, rs, fill=True))
    ev = runs["open recurring"]  # kept-off joiners (an out pin, a drag onto Not placed): never counted as rostered
    joined, pins, layout = ev.by_status("in"), dict(ev.pins), ev.layout
    ev.layout = None  # a board place wins over a pin out: off the board for these cases
    if joined:  # the lock that fills (nobody is on the board, so the lock as it stands is refused)
        whole = cq.lock_box(reg, ev, rs, fill=True)
        ev.pins[str(joined[0].discord_id)] = "out"
        b = cq.lock_box(reg, ev, rs, fill=True)
        assert b["verb"] == "Lock" and b["fp"] != whole["fp"], b  # no count to change: the fingerprint covers the pins
        add("lock_box", "open recurring, one joiner kept off", b)
        for sg in joined:
            ev.pins[str(sg.discord_id)] = "out"
        b = cq.lock_box(reg, ev, rs)
        assert b["verb"] is None and b["sentences"] == [rc.SOLVER_WORDS["all_off"]], b
        add("lock_box", "everyone who joined kept off", b)
    ev.pins, ev.layout = pins, layout
    # Bench answers (2026-11-25 §3, the Task 6 fix): the head states a rule true whatever the solve decides, never a
    # predicted number; the DM sentence counts only when the board is the whole roster
    ms = [reg.members[REAL + i] for i in range(len(PEOPLE))]
    def fresh_run():  # a schedule's night nothing else in this harness has opened (open_run gives back an opened one)
        for n in range(5, 9):
            e = recurring_run(reg, rs, n)
            if e.state == "open" and not e.signups:
                return e
        raise AssertionError("no free night left for the Bench cases")

    def bench_case(case, ev, head=None, told=None):
        """The lock that fills (auto-fill ticked, or a complete board with no choice): its rule head and who is told,
        after the open slots sentence when one leads. The lock as it stands of the same board is linted beside it."""
        b = cq.lock_box(reg, ev, rs, fill=True)
        body = [x for x in b["sentences"] if not re.search(r"\d+ open slots?\b", x)]
        assert head is None or body[0] == head, (case, b)
        assert told is None or body[1] == told, (case, b)
        add("lock_box", case, b)
        add("lock_box", f"{case}, as it stands", cq.lock_box(reg, ev, rs))
        return b

    dms = {m.discord_id: m.dm_opt_out for m in ms}  # the harness's own DMs-off people: put back after these cases
    for m in ms:
        m.dm_opt_out = False
    b1 = fresh_run()
    join(reg, rs, b1, ms[:8])
    join(reg, rs, b1, ms[8:11], "sub")
    bench_case("open recurring, empty board, Bench answers", b1,
               "The lock builds the roster for Barrow Deeps from those who joined, then from the bench.",
               f"Everyone rostered gets {cq.CONFIRM_DM}.")
    b1.layout = [[ms[0].display_name]]
    bench_case("open recurring, one placed, Bench answers", b1,
               "The lock keeps the board and fills open slots from those who joined, then from the bench.")
    b1.auto_placed = [ms[8].display_name]
    b1.layout = [[ms[0].display_name, ms[8].display_name]]
    bench_case("open recurring, a Bench place Auto-fill made", b1, cq.AUTO_AGAIN)
    b1.layout, b1.auto_placed = None, []
    ms[9].dm_opt_out = True
    bench_case("open recurring, Bench answers, one with DMs off", b1,
               told="Everyone rostered gets the Confirm or Can't make it ask, as a DM or on the site.")
    for m in ms[:11]:
        m.dm_opt_out = True
    bench_case("open recurring, Bench answers, everyone with DMs off", b1,
               told="Everyone rostered sees a Confirm or Can't make it ask on the site.")
    for m in ms[:11]:
        m.dm_opt_out = False
    ns = [m.display_name for m in ms[:8]] + [ms[8].display_name, ms[9].display_name]
    b1.layout = [ns[:5], ns[5:]]  # two Bench answers placed by hand: every seat placed, the board is the roster
    bench_case("open recurring, a full board with two Bench answers placed", b1,
               "The board becomes the roster for Barrow Deeps.", f"The 10 rostered get {cq.CONFIRM_DM}.")
    ms[9].dm_opt_out = True
    bench_case("open recurring, a full board, one with DMs off", b1,
               told=f"9 of the 10 rostered get {cq.CONFIRM_DM} and 1 sees it on the site.")
    ms[9].dm_opt_out = False
    b1.layout = None
    k = rc.groups_per_roster(reg, rc.run_size(reg, b1))
    b1.layout = [[] for _ in range(k)] + [[ms[8].display_name]]  # a Bench answer placed in a second roster's group
    bench_case("open recurring, a Bench answer placed in a second roster", b1,
               "The lock keeps the board and fills open slots from those who joined, then from the bench.",
               f"Everyone rostered gets {cq.CONFIRM_DM}.")
    b1.layout = None
    b3 = fresh_run()  # no healer joined: the benched Priest may take a seat for the healer minimum
    join(reg, rs, b3, ms[:2] + ms[5:12] + [reg.members[OFFICER_ID]])
    join(reg, rs, b3, ms[2:3], "sub")
    bench_case("open recurring, a benched healer the joiners lack", b3,
               "If those who joined cannot meet the healer minimum, a healer on the bench is rostered ahead of them.")
    b3.layout = [[ms[2].display_name]]  # the same Bench answer placed by hand
    bench_case("open recurring, a Bench answer placed by hand on a full event", b3,
               "A Bench answer an officer placed or pinned is rostered ahead of people who joined.")
    b3.layout = None
    b3.pins[str(ms[2].discord_id)] = "in"
    join(reg, rs, b3, ms[3:4], "sub")
    b3.pins[str(ms[3].discord_id)] = "in"
    bench_case("open recurring, two Bench answers pinned on a full event", b3,
               "Bench answers an officer placed or pinned are rostered ahead of people who joined.")
    b3.pins = {}
    ns = [s.display_name for s in b3.by_status("in")]
    b3.layout = [ns[:4] + [ms[2].display_name], ns[4:9]]  # a full board holding a hand-placed Bench answer, a joiner off
    bench_case("open recurring, a full board with a Bench answer ahead of a joiner", b3,
               "The board becomes the roster for Barrow Deeps, so its Bench answer is rostered ahead of people who joined.")
    b3.layout = None
    # fix round 2: the exception's "tank or healer" form, and raids of one evening whose boards differ
    b4 = fresh_run()  # nobody who joined tanks or heals: a benched Warrior and a benched Priest
    join(reg, rs, b4, ms[5:12] + [reg.members[OFFICER_ID]])
    join(reg, rs, b4, [ms[0], ms[2]], "sub")
    bench_case("open recurring, a benched tank and healer the joiners lack", b4,
               "If those who joined cannot meet the tank or healer minimum, a benched one is rostered ahead of them.")
    start = (reg.now_local() + timedelta(days=20)).replace(hour=18, minute=30, second=0, microsecond=0)
    pair = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t", schedule=rc.ADHOC_SCHEDULE,
                       lineup=[{"raid": RID, "rosters": 1}])
    join(reg, rs, pair, ms[:8])
    join(reg, rs, pair, ms[8:10], "sub")
    bench_case("two raids, both boards empty, Bench answers", pair,
               "The lock builds the roster for Hyjal Summit and Barrow Deeps from those who joined, then from the bench.")
    later = rc.event_parts(rs, pair)[1]
    later.layout, later.auto_placed = [[ms[8].display_name]], [ms[8].display_name]
    bench_case("two raids, a Bench place on the second decided again", pair, cq.AUTO_AGAIN)
    later.layout, later.auto_placed = [[ms[0].display_name]], []
    bench_case("two raids, one board part-filled, one empty", pair,
               "The lock fills open slots from those who joined first, then from the bench.")
    later.layout = None
    for m in ms:
        m.dm_opt_out = dms[m.discord_id]
    b = cq.cancel_box(reg, four, rs)
    assert b["sentences"][0] == "All 4 raids of this event are cancelled.", b
    add("cancel_box", "four raids", b)
    ev = runs["open recurring"]
    add("reschedule_box", "open, answers", cq.reschedule_box(reg, rs, ev, ev.start + timedelta(hours=1)))
    add("reschedule_pick_box", "nothing picked yet", cq.reschedule_pick_box(reg, rs, ev))
    quiet = runs["open recurring, nobody joined"]
    add("reschedule_box", "nobody answered", cq.reschedule_box(reg, rs, quiet, quiet.start + timedelta(hours=1)))
    add("reschedule_box", "into the past", cq.reschedule_box(reg, rs, ev, reg.now_local() - timedelta(days=2)))
    lk = runs["locked recurring, autofill on"]
    add("reschedule_box", "locked", cq.reschedule_box(reg, rs, lk, lk.start + timedelta(hours=2)))
    # Edit event (the owner's tweaks, B): the same verb's box, titled as the event, with the schedule editor's diff lines
    for case, start, timing in (("confirmation off", None, {"confirm": False}),
                                ("the priority", None, {"weights": {"rank": 1, "regular": 6}}),
                                ("first to join", None, {"first_come": True}),
                                ("the split", None, {"split_policy": "rotation"}),
                                ("the lock, answers told", None, {"lock_hours_before": 6}),
                                ("Auto Lock off", None, {"auto_lock": False}),
                                ("Auto Lock on, the board as it stands", None, {"auto_lock_fill": False}),
                                ("a move and every field", ev.start + timedelta(hours=1),
                                 {"lock_hours_before": 6, "nudge": False, "confirm": False, "autofill": False, "fill_ask_hours": 2,
                                  "first_come": True, "weights": {"rank": 1}, "split_policy": "rotation"})):
        b = cq.reschedule_box(reg, rs, ev, start, timing, edit=True)
        assert b["verb"] == "Save" and b["diff"], (case, b)
        add("reschedule_box edit", case, b)
    add("reschedule_box edit", "locked: the automatic asks", cq.reschedule_box(reg, rs, lk, None, {"autofill": False}, edit=True))
    for case, timing in (("locked: confirmation", {"confirm": False}), ("locked: the priority", {"weights": {"rank": 1}}),
                         ("locked: the split", {"split_policy": "rotation"}), ("locked: the lock", {"lock_hours_before": 6}),
                         ("locked: Auto Lock", {"auto_lock": False})):
        b = cq.reschedule_box(reg, rs, lk, None, timing, edit=True)
        assert b["verb"] is None, (case, b)
        add("reschedule_box edit", case, b)
    three = runs["three raids, first"]
    L = lambda *raids: [{"raid": r, "rosters": 1} if isinstance(r, str) else {"raid": r[0], "rosters": r[1]} for r in raids]  # noqa: E731
    for case, lineup in (("a roster count", L(hy, (RID, 1), ony)), ("remove", L(hy, ony)), ("reorder", L(hy, ony, (RID, 2))),
                         ("add", None), ("locked", "locked")):
        if lineup is None:
            solo = rc.open_run(reg, rs, hy, three.start + timedelta(days=9), by="t", schedule=rc.ADHOC_SCHEDULE)
            b = cq.lineup_box(reg, rs, solo, L(hy, (RID, 2)))
        elif lineup == "locked":
            b = cq.lineup_box(reg, rs, lk, L(RID, ony))
        else:
            b = cq.lineup_box(reg, rs, three, lineup)
        add("lineup_box", case, b)
    bd3 = rc.event_parts(rs, three)[1]  # board places on the second Barrow Deeps roster, which goes
    k = rc.groups_per_roster(reg, rc.run_size(reg, bd3))
    was = bd3.layout
    bd3.layout = [[] for _ in range(k)] + [[ms[1].display_name]]
    b = cq.lineup_box(reg, rs, three, L(hy, (RID, 1), ony))
    assert "Moves made on the removed roster are dropped." in b["sentences"], b
    add("lineup_box", "a roster that goes with board places", b)
    cut = cq.lineup_box(reg, rs, three, L(hy, (RID, 1)))
    said = cut["sentences"]
    assert "Moves made on the removed roster are dropped." in said[:-1] and said[-1].endswith("stand."), \
        ("what changes first, who is told after", said)
    add("lineup_box", "a roster with places goes and a raid is taken out", cut)
    bd3.layout = was
    lk.fill_asks.append(rc.FillAsk(discord_id=ms[11].discord_id, display_name=ms[11].display_name, kind="other", role="healer",
                                   character="X", spec="Holy", reason="short 1 healer"))
    add("cancel_box", "locked, a fill ask out to someone not rostered", cq.cancel_box(reg, lk, rs))
    lk.fill_asks.pop()
    m = ms[2]
    add("switch_box", "before the lock", cq.switch_box(reg, ev, m, None, "Shadow", [RID]))
    add("switch_box", "another character", cq.switch_box(reg, ev, m, "Kingsley", "Fire", [RID, hy]))
    add("switch_box", "after the lock, DMs on", cq.switch_box(reg, lk, m, None, "Shadow", [RID], locked=True))
    m.dm_opt_out = True
    add("switch_box", "after the lock, DMs off", cq.switch_box(reg, lk, m, None, "Shadow", [RID], locked=True))
    m.dm_opt_out = False
    add("switch_box", "after the lock, someone without a seat", cq.switch_box(reg, lk, m, None, "Holy", [RID], locked=True, swap=False))
    add("switch_box", "the officer's own answer", cq.switch_box(reg, ev, m, None, "Holy", [RID, hy], own=True))
    add("switch_box", "the officer's own, another character", cq.switch_box(reg, ev, m, "Kingsley", "Fire", [RID], own=True))
    add("answer_box", "join before the lock", cq.answer_box(reg, quiet, ms[4], "join", ms[4].main.label, ms[4].main.spec, by_id=OFFICER_ID))
    add("answer_box", "join on their own record", cq.answer_box(reg, quiet, ms[4], "join", by_id=ms[4].discord_id))
    add("answer_box", "bench after the lock", cq.answer_box(reg, lk, ms[1], "bench", by_id=OFFICER_ID))
    member_action_boxes(reg, rs, ev, lk, ms, add)
    for n in (1, 4):
        add("undo_box", f"{n} moves", cq.undo_box(n))
    add("strategy_box", "nothing picked yet", cq.strategy_box(reg, quiet, None))
    for st in ("balanced", "first", "rotation"):
        add("strategy_box", f"to {st}", cq.strategy_box(reg, quiet, st))
    add("strategy_box", "locked", cq.strategy_box(reg, lk, "rotation"))
    # More › Clear the board (the owner's tweaks C): what changes, nobody told, Auto-fill again; refused when nothing is placed or locked
    add("clear_board_box", "nothing placed", cq.clear_board_box(reg, rs, quiet))
    rc.set_signup(reg, rs, quiet, ms[4], None, "in", source="test")
    rc.set_signup(reg, rs, quiet, ms[5], None, "in", source="test")
    names = [s.display_name for s in quiet.by_status("in")]
    for n in (1, 2):
        quiet.layout = [names[:n]]
        add("clear_board_box", f"{n} placed", cq.clear_board_box(reg, rs, quiet))
    quiet.layout = None
    for m_ in (ms[4], ms[5]):
        quiet.signups.pop(str(m_.discord_id), None)
    rs.save(quiet, "back to nobody joined")
    add("clear_board_box", "locked", cq.clear_board_box(reg, rs, lk))
    final_review_boxes(reg, rs, lk, add)
    start = reg.now_local() + timedelta(days=6)
    for case, opens, kw in (("three days early", reg.now_local() + timedelta(days=3, minutes=5), {}),
                            ("one day early", reg.now_local() + timedelta(days=1, minutes=5), {}),
                            ("five hours early", reg.now_local() + timedelta(hours=5, minutes=5), {}),
                            ("due now", reg.now_local(), {}), ("no signup channel", reg.now_local() + timedelta(days=2), {"channel": False}),
                            ("no signup channel, asked in a channel", reg.now_local() + timedelta(days=2), {"channel": False, "here": True}),
                            ("a raid first opens after it", reg.now_local() + timedelta(days=2),
                             {"lineup": [{"raid": RID, "rosters": 1}, {"raid": ony, "rosters": 1}]})):
        was = reg.raid_def(ony).get("first_open")
        if kw.get("lineup"):
            reg.set_raid_overrides(ony, {"first_open": (start + timedelta(days=30)).isoformat()}, "t")
        b = cq.open_now_box(reg, start, opens, start - timedelta(hours=24), **kw)
        reg.set_raid_overrides(ony, {"first_open": was}, "t")
        assert not kw.get("lineup") or "first opens" in b["sentences"][1], b
        add("open_now_box", case, b)
    # schedules: the harness's weekly Barrow Deeps schedule has many runs open; a fresh one has none
    sid = "barrow-deeps"
    for case, nxt in (("the schedule's next night", 0), ("a night further ahead", 5)):  # /raid open and the site's open routes
        start = [t for s_, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 60) if s_["id"] == sid][nxt]
        add("open_box_for", case, cq.open_box_for(reg, RID, rc.open_preview(reg, rs, RID, start.isoformat(), sid)))
    # /raid open on a scheduled night three days ahead, a lineup of two, asked in a server channel (phase 5)
    p = next(p for p in (rc.open_preview(reg, rs, RID, t.isoformat(), sid, lineup=[{"raid": ony, "rosters": 1}])
                         for s_, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 60)
                         if s_["id"] == sid and t - reg.now_local() >= timedelta(days=3))
             if p["exists"] is None and p["refusal"] is None)  # a night not open yet
    assert [e["raid"] for e in p["later"]] == [ony]
    b = cq.open_box_for(reg, RID, p, here=True)
    add("open_box_for", "a scheduled night three days ahead, a lineup of two, asked in a channel", b)
    entry = reg.schedule_entry(sid)
    add("schedule_create_box", "three nights a week", cq.schedule_create_box(reg, entry))
    reg.set_schedule("tue", {"kind": "weekly", "slots": ["Tue 18:30"], "name": "Tuesday raid night"}, "t", raid=hy)
    add("schedule_create_box", "one night", cq.schedule_create_box(reg, reg.schedule_entry("tue")))
    reg.set_schedule("lk", {"kind": "lockout", "days": [1], "time": "20:00", "name": "Lockout night"}, "t", raid=ony)
    add("schedule_create_box", "a lockout day", cq.schedule_create_box(reg, reg.schedule_entry("lk")))
    reg.set_schedule("pug", {"kind": "pickup", "name": "Pickup"}, "t", raid=hy)
    add("schedule_create_box", "a pickup template", cq.schedule_create_box(reg, reg.schedule_entry("pug")))
    add("schedule_save_box", "many open", cq.schedule_save_box(reg, rs, sid, {"lock_hours_before": 12}, ["Locks: 24 hours before → 12 hours before"]))
    add("schedule_save_box", "none open", cq.schedule_save_box(reg, rs, "tue", {"name": "Tues"}))
    add("schedule_save_box", "a priority change", cq.schedule_save_box(reg, rs, sid, {"weights": {"rank": 5}}))
    tue_ev = rc.open_run(reg, rs, hy, rc.next_runs_of(reg, hy, reg.schedule_entry("tue"), n=2)[1], schedule="tue")
    add("schedule_save_box", "one open", cq.schedule_save_box(reg, rs, "tue", {"lock_hours_before": 12}))
    add("schedule_save_box", "one open, rosters", cq.schedule_save_box(reg, rs, "tue", {"rosters": 2}))
    add("schedule_save_box", "many open, a time and the lineup", cq.schedule_save_box(reg, rs, sid, {"slots": ["Tue 20:00"], "lineup": []}))
    add("schedule_save_box", "a rename", cq.schedule_save_box(reg, rs, "tue", {"name": "Tues"}))
    for case, x in (("one open", "tue"), ("many open", sid), ("none open", "lk")):
        add("schedule_pause_box", case, cq.schedule_pause_box(reg, rs, x))
        add("schedule_remove_box", case, cq.schedule_remove_box(reg, rs, x))
    reg.set_schedule("lk", {"active": "false"}, "t")
    add("schedule_resume_box", "the next sheet later", cq.schedule_resume_box(reg, rs, "lk"))
    reg.set_schedule("lk", {"signup_lead_hours": 24 * 20}, "t")
    add("schedule_resume_box", "the next sheet now", cq.schedule_resume_box(reg, rs, "lk"))
    add("schedule_resume_box", "a pickup template", cq.schedule_resume_box(reg, rs, "pug"))
    for x in ms[:6]:
        reg.regular_add(x.discord_id, "tue", "t")
    reg.set_schedule("tue", {"weights": {"rank": 5}}, "t")
    add("schedule_remove_box", "6 regulars, one open with its own weights", cq.schedule_remove_box(reg, rs, "tue"))
    add("regular_remove_box", "an open event", cq.regular_remove_box(reg, reg.members[ms[0].discord_id], "tue"))
    rc.end_run(reg, rs, tue_ev, "t")
    add("regular_remove_box", "no open event", cq.regular_remove_box(reg, reg.members[ms[0].discord_id], "tue"))
    add("regular_remove_box", "a removed schedule", cq.regular_remove_box(reg, reg.members[ms[0].discord_id], "gone"))
    for case, rid in (("some open", ony), ("many open", RID)):
        for kind, changed in (("one minimum", {"minimum:healer"}), ("two minimums", {"minimum:healer", "minimum:tank"}),
                              ("priority", {"weight:rank"}), ("lockout", {"lockout_days"}), ("mixed", {"minimum:healer", "weight:rank"}),
                              ("notes", {"notes"})):
            add("raid_save_box", f"{kind}, {case}", cq.raid_save_box(reg, rs, rid, changed, ["Healers, minimum: 5 → 4"]))
    reg.set_minimums(ony, {"healer": {"min": 3}, "Paladin:Holy": {"min": 1}}, "t")
    add("raid_reset_box", "only minimums", cq.raid_reset_box(reg, ony))
    add("raid_reset_box", "already the game's", cq.raid_reset_box(reg, hy))
    from oibot_gm import configops as _co  # a comp clear of a minimum the guild never set: the game's, or none at all

    add("minimum_unchanged_box", "the game's minimum", _co.minimum_unchanged_box(reg, hy, "tank"))
    add("minimum_unchanged_box", "no game minimum", _co.minimum_unchanged_box(reg, hy, "Druid"))
    for n in (1, 2):
        add("leave_box", f"{n} edits", cq.leave_box(n, "Hyjal Summit"))
    for kind, title in cq.REFUSAL_TITLES.items():
        add("refusal_box", kind, cq.refusal_box(title, rc.LINEUP_LOCKED))
    lock_choice_cases(reg, rs, add)  # last: its own schedule's nights would move the harness's recurring_run indices
    relock_cases(reg, rs, add)  # its own schedule too; it locks and unlocks only its own events
    unlock_cases(reg, rs, add)  # after it: it locks the four-raid event


def reschedule_cases(reg, rs, runs, ms, dump):
    """consequences.reschedule_consequence across its cases and rc.reschedule_refusal's every sentence."""
    seen = set()

    def add(case, ev, start, timing=None):
        lines = cq.reschedule_consequence(reg, rs, ev, start, timing)
        seen.add(lines[-1].split()[0] if len(lines) > 2 else "")
        dump.add("reschedule_consequence", case, lines)

    ev = runs["open recurring"]
    add("open, answers", ev, ev.start + timedelta(hours=1))
    add("open, nobody answered", runs["open recurring, nobody joined"], runs["open recurring, nobody joined"].start + timedelta(hours=1))
    add("locked, the roster stands", runs["locked recurring, autofill on"], runs["locked recurring, autofill on"].start + timedelta(hours=2))
    add("timing only, the lock", ev, None, {"lock_hours_before": 6})
    add("timing only, automatic asks", ev, None, {"autofill": False})
    add("timing only, no reminder", ev, None, {"nudge": False})
    add("timing only, reminder off with a new reminder time", ev, None, {"nudge_hours_before": 12, "nudge": False})
    team = rc.run_team(reg, ev)
    was = team.get("reminders")
    team["reminders"] = rc.OFF
    add("timing only, reminder on", ev, None, {"nudge": True})
    add("timing only, reminder on with a new reminder time", ev, None, {"nudge": True, "nudge_hours_before": 12})
    team["reminders"] = was
    add("timing only, the fill ask wait", ev, None, {"fill_ask_hours": 2})
    add("timing only, confirm-by", ev, None, {"confirm_hours_before": 2})
    add("timing only, Auto Lock off", ev, None, {"auto_lock": False})
    add("timing only, Auto Lock and its auto-fill off", ev, None, {"auto_lock": False, "auto_lock_fill": False})
    add("timing only, the lock and Auto Lock off", ev, None, {"lock_hours_before": 6, "auto_lock": False})
    team = rc.run_team(reg, ev)
    was_auto, team["auto_lock"] = team.get("auto_lock", True), False
    add("timing only, the auto-fill of an Auto Lock that is off", ev, None, {"auto_lock_fill": False})
    team["auto_lock"] = was_auto
    add("timing only, two times", ev, None, {"lock_hours_before": 6, "nudge_hours_before": 12})
    add("timing only, everything", ev, None, {"lock_hours_before": 6, "nudge_hours_before": 12, "confirm_hours_before": 2,
                                              "nudge": False, "autofill": False, "fill_ask_hours": 2})
    add("a move with its own lock time and automatic asks", ev, ev.start + timedelta(hours=1), {"lock_hours_before": 6, "autofill": False})
    add("a move with everything", ev, ev.start + timedelta(hours=1), {"lock_hours_before": 6, "nudge_hours_before": 12, "confirm_hours_before": 2,
                                                                     "nudge": False, "autofill": False, "fill_ask_hours": 2})
    # onto the schedule's own night that hasn't opened yet: that night's run is this one
    nxt = ev.start + timedelta(days=21)
    while rc.event_of_occurrence(rs, ev.schedule, rc.occurrence_stamp(reg, nxt), reg=reg) is not None:
        nxt += timedelta(days=7)
    lines = cq.reschedule_consequence(reg, rs, ev, nxt, None)
    assert lines[-1].startswith("That night's"), lines
    dump.add("reschedule_consequence", "onto the schedule's unopened night", lines)
    add("its lock time has passed", runs["open ad hoc"], reg.now_local() + timedelta(hours=1))
    add("three raids, moved from a later raid", runs["three raids, third"], runs["three raids, first"].start + timedelta(hours=1))
    # people also on another run that night (a day no other run of the harness is near)
    other = (ev.start + timedelta(days=16)).replace(minute=0)
    hy = rc.open_run(reg, rs, "hyjal_summit_forever", other + timedelta(hours=1), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, hy, ms[:6])
    add("people also on another run that night", ev, other)
    join(reg, rs, hy, ms[1:6], "out")
    add("one person also on another run that night", ev, other)
    # away answers worked out again: someone away on the new day, with no answer on the sheet
    reg.add_character(REAL + 61, "Quillamere", "Zephyrine", "Mage", "Fire", None, True)
    away = ev.start + timedelta(days=18)
    while rc.open_schedule(reg, ev.instance, away, None) != rc.ADHOC_SCHEDULE:  # never one of the schedule's own nights
        away += timedelta(days=1)  # (the calendar decided: on some days +18 landed on one and that sentence won)
    reg.add_absence(REAL + 61, away.astimezone(reg.tz).date().isoformat(), None, None, "t")
    add("away answers worked out again", ev, away)
    assert {"It", "Bramble", "Away", "You"} <= seen, seen
    # the refusals, one each
    one = runs["open recurring"]
    for case, start, timing in (("past", reg.now_local() - timedelta(hours=2), None), ("same", one.start, None),
                                ("clash", runs["open recurring, nobody joined"].start, None),
                                ("order", None, {"nudge_hours_before": 1, "lock_hours_before": 6}),
                                ("unknown timing", None, {"open_dm": True}),
                                ("the same timing", None, {"lock_hours_before": rc.run_team(reg, one)["cutoff_hard_hours"]})):
        dump.add("reschedule_refusal", case, rc.reschedule_refusal(reg, rs, one, start, timing) or "")
    dump.add("reschedule_refusal", "closed", rc.reschedule_refusal(reg, rs, runs["cancelled"], one.start, None) or "")
    dump.add("reschedule_refusal", "timing on a locked run", rc.reschedule_refusal(reg, rs, runs["locked recurring, autofill on"], None, {"lock_hours_before": 6}) or "")
    for case, timing in (("confirmation on a locked run", {"confirm": False}), ("priority on a locked run", {"first_come": True}),
                         ("split on a locked run", {"split_policy": "rotation"})):
        dump.add("reschedule_refusal", case, rc.reschedule_refusal(reg, rs, runs["locked recurring, autofill on"], None, timing) or "")
    add("an event's own confirmation", one, None, {"confirm": False})
    add("an event's own priority", one, None, {"weights": {"rank": 1}})
    add("an event's own seating", one, None, {"first_come": True})
    add("an event's own split", one, None, {"split_policy": "rotation"})
    add("an event's every own setting", one, None, {"confirm": False, "first_come": True, "weights": {"rank": 1},
                                                    "split_policy": "rotation", "lock_hours_before": 6, "autofill": False})
    add("locked: the automatic asks", runs["locked recurring, autofill on"], None, {"autofill": False})
    dump.add("reschedule_event refusal", "a member", rc.MOVE_OFFICER_ONLY)
    dump.add("reschedule_refusal", "started", rc.MOVE_STARTED)


def lineup_cases(reg, rs, runs, ms, dump, monkeypatch):
    """consequences.lineup_consequence across its cases (add, reorder, a roster count, remove with and without what the
    removed raid held, a schedule's run) and rc.lineup_refusal's every sentence."""
    hy, ony = "hyjal_summit_forever", "onyxias_lair"

    def L(*raids):
        return [{"raid": r, "rosters": 1} if isinstance(r, str) else {"raid": r[0], "rosters": r[1]} for r in raids]

    def add(case, ev, lineup):
        assert rc.lineup_refusal(reg, rs, ev, lineup) is None, case
        dump.add("lineup_consequence", case, cq.lineup_consequence(reg, rs, ev, lineup))

    three = runs["three raids, first"]  # Hyjal, then Barrow Deeps ×2, then Onyxia's Lair
    add("reorder", three, L(hy, ony, (RID, 2)))
    add("a roster count", three, L(hy, (RID, 1), ony))
    add("remove, nothing on its board", three, L(hy, ony))
    add("remove from a later raid", runs["three raids, third"], L(hy, (RID, 2)))
    add("every later raid removed", three, L(hy))
    start = (reg.now_local() + timedelta(days=12)).replace(hour=18, minute=30, second=0, microsecond=0)
    solo = rc.open_run(reg, rs, hy, start, by="t", schedule=rc.ADHOC_SCHEDULE)
    add("add", solo, L(hy, (RID, 2)))
    add("add two", solo, L(hy, RID, ony))
    add("a schedule's run, add", runs["open recurring"], L(RID, ony))
    # what a removed raid held: board places and pins, answers to leave before it
    held = rc.open_run(reg, rs, hy, start + timedelta(days=1), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=L(RID, ony))
    join(reg, rs, held, ms)
    bd, on = rc.event_parts(rs, held)[1:]
    bd.layout, bd.pins = [[m.display_name for m in ms[:5]]], {str(ms[6].discord_id): "out"}
    rs.save(bd, "board")
    add("remove, board places and a pin", held, L(hy, ony))
    rc.leave_follow(reg, rs, bd, ms[8])
    rc.leave_follow(reg, rs, bd, ms[9])
    add("remove, board places and answers to leave before it", held, L(hy, ony))
    on.layout = [[ms[0].display_name]]
    rs.save(on, "board")
    add("remove two, one place on the second", held, L(hy))
    bd.layout, bd.pins, on.layout = None, {}, None
    rs.save(bd, "board cleared")
    rs.save(on, "board cleared")
    add("remove, only answers to leave before it", held, L(hy, ony))
    rc.leave_follow(reg, rs, on, ms[10])
    add("remove two, answers to leave before each", held, L(hy))
    # fix round 1: a kept raid's new time and who is told, open asks, a raid not open yet, the first raid alone
    add("reorder, both kept raids start at another time", three, L(hy, ony, (RID, 2)))
    add("remove the second, the third starts earlier", three, L(hy, ony))
    add("the first raid alone, two rosters", solo, L((hy, 2)))
    bd.fill_asks.append(rc.FillAsk(discord_id=ms[11].discord_id, display_name=ms[11].display_name, kind="other", role="healer",
                                   character="X", spec="Holy", reason="short 1 healer", phase="prelock"))
    bd.layout = [[m.display_name for m in ms[:4]]]
    rs.save(bd, "an ask and a board")
    add("remove, places, plans to leave early and an open ask, the third earlier", held, L(hy, ony))
    late = (reg.now_local() + timedelta(days=15)).replace(hour=22, minute=30, second=0, microsecond=0)
    night = rc.open_run(reg, rs, hy, late, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=L(RID, ony))
    join(reg, rs, night, ms)
    nb = rc.event_parts(rs, night)[1]
    nb.layout = [[m.display_name for m in ms[:3]]]
    rs.save(nb, "board")
    add("past midnight, both kept raids move", night, L(hy, ony, RID))
    add("past midnight, the second taken out with its board", night, L(hy, ony))
    was = reg.raid_def(ony).get("first_open")
    reg.set_raid_overrides(ony, {"first_open": (start + timedelta(days=60)).isoformat()}, "t")
    add("add a raid before it first opens", solo, L(hy, ony))
    add("add two, the second before it first opens", solo, L(hy, RID, ony))
    reg.set_raid_overrides(ony, {"first_open": was}, "t")
    # fix round 2: a loss is never dropped — four raids past midnight, and a swap with a raid not open yet
    import copy as _copy

    extra = {**_copy.deepcopy(reg.profile.raids[RID]), "id": "extra_a", "name": "Extra A", "duration_hours": 2}
    for raids in {id(reg.profile.raids): reg.profile.raids, id(reg.base_profile.raids): reg.base_profile.raids}.values():
        monkeypatch.setitem(raids, "extra_a", extra)  # a fourth raid on the session profile, taken off again (no reorder)
    reg.set_raid_overrides("extra_a", {"first_open": "2026-01-01T00:00"}, "t")
    four = rc.open_run(reg, rs, hy, late + timedelta(days=2), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=L(RID, ony, "extra_a"))
    join(reg, rs, four, ms)
    fb = rc.event_parts(rs, four)[1]
    fb.layout, fb.pins = [[m.display_name for m in ms[:4]]], {str(ms[5].discord_id): "in", str(ms[6].discord_id): "out"}
    rs.save(fb, "board")
    said = cq.lineup_consequence(reg, rs, four, L(hy, ony, "extra_a"))
    assert "Barrow Deeps is taken out with its 4 board places and 2 pins." in said, said
    dump.add("lineup_consequence", "four raids past midnight, raid 2 out with a board and pins", said)
    swap = rc.open_run(reg, rs, hy, late + timedelta(days=4), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=L(RID, ony))
    join(reg, rs, swap, ms)
    sb = rc.event_parts(rs, swap)[1]
    sb.fill_asks.append(rc.FillAsk(discord_id=ms[11].discord_id, display_name=ms[11].display_name, kind="other", role="healer",
                                   character="X", spec="Holy", reason="short 1 healer", phase="prelock"))
    rs.save(sb, "an ask")
    reg.set_raid_overrides("extra_a", {"first_open": (late + timedelta(days=60)).isoformat()}, "t")
    said = cq.lineup_consequence(reg, rs, swap, L(hy, ony, "extra_a"))
    assert "Barrow Deeps is taken out with its open ask." in said and any("Extra A first opens" in x for x in said), said
    dump.add("lineup_consequence", "a swap, the removed raid's open ask, the added raid not open yet", said)
    # the refusals, one each
    for case, ev, lineup in (("another first raid", three, L(RID, hy)), ("no such raid", three, L(hy, "nowhere")),
                             ("five raids", three, L(hy, RID, ony, "nowhere", "elsewhere")), ("a raid twice", three, L(hy, RID, RID)),
                             ("the same lineup", three, L(hy, (RID, 2), ony)),
                             ("out of order", three, [{"raid": hy}, {"raid": RID, "starts": 300}, {"raid": ony, "starts": 200}]),
                             ("locked", runs["locked recurring, autofill on"], L(RID, ony)), ("cancelled", runs["cancelled"], L(RID, ony))):
        said = rc.lineup_refusal(reg, rs, ev, lineup)
        assert said, case
        dump.add("lineup_refusal", case, said)
    dump.add("edit_lineup refusal", "a member", rc.LINEUP_OFFICER_ONLY)


def armory_long_names(reg, rs, dump):
    """Final review B5: every sentence generator added by the armory link, with the longest names a guild has — a
    three-word display name and a two-word character name on both sides — so a confirm stays readable aloud."""
    from oibot_gm import armory_seed, flows_armory
    from oibot_gm import professions as pr
    from oibot_gm.discord_armory import Found
    from oibot_gm.registry import ArmoryLink, Profession, Recipe, Registry

    A, B = REAL + 90, REAL + 91
    reg.add_character(A, "Annabelle Rose Whitford", "Bramwell", "Warrior", "Arms", None, True, surname="Kettleworth")
    reg.add_character(A, "Annabelle Rose Whitford", "Wrenna", "Mage", "Fire", None, False, surname="Hollowmere")
    reg.add_character(B, "Bartholomew James Kingsley", "Ivoryn", "Priest", "Holy", None, True, surname="Marshwater")
    am, bm = reg.members[A], reg.members[B]
    ac = am.main
    names = {"Annabelle", "Bartholomew", "Bramwell", "Wrenna", "Ivoryn"}
    found = {"profile, a new name": Found(id=711001, slug="x", name="Bramwell", surname="Ashcroftson", cls="Warrior", level=60, source="profile"),
             "profile, the same name": Found(id=711001, slug="x", name="Bramwell", surname="Kettleworth", cls="Warrior", level=60, source="profile"),
             "roster, a new name, no class": Found(id=711002, slug="x", name="Bramwell", surname="Ashcroftson", cls=None, level=7, source="roster"),
             "roster, the same name": Found(id=711002, slug="x", name="Bramwell", surname="Kettleworth", cls="Warrior", level=7, source="roster")}
    for case, f in found.items():
        for who, by in (("own", A), ("an officer", OFFICER_ID)):
            dump.add("armory_link_consequence", f"long names, {who}, {case}", cq.armory_link_consequence(reg, am, ac, f, by), names)
        dump.add("armory_give_consequence", f"long names, {case}, main", cq.armory_give_consequence(reg, bm, f, main=True), names)
        dump.add("armory_give_consequence", f"long names, {case}, an alt", cq.armory_give_consequence(reg, bm, f, main=False), names)
    for who, by in (("own", A), ("an officer", OFFICER_ID)):
        dump.add("armory_unlink_consequence", f"long names, {who}", cq.armory_unlink_consequence(reg, am, ac, by), names)
    for on in (True, False):
        for cur in (None, on):
            dump.add("profession_sync_consequence", f"long names, on={on}, current={cur}", cq.profession_sync_consequence(on, ac.label, current=cur), names)
    for result in ("updated", "unchanged", "missing", "limited"):
        dump.add("armory_sync_sentence", f"long names, {result}", [cq.armory_sync_sentence(ac.label, result)], names)
    # the move, every case, the real records and the long ones built to reach each branch
    dump.add("move_consequence", "long names, the main to a member with a main", cq.move_consequence(reg, am, ac, bm), names)
    dump.add("move_consequence", "long names, an alt", cq.move_consequence(reg, am, reg.own_character(am.characters, "Wrenna Hollowmere"), bm), names)
    spot = "barrow-deeps"

    def mv_char(label, *, main=False, rosters=()):
        return SimpleNamespace(label=label, name=label.split()[0], is_main=main, status="active", rosters=list(rosters))

    def mv_member(name, chars):
        main = next((x for x in chars if x.is_main), None)
        return SimpleNamespace(display_name=name, characters=chars, main=main, active=lambda: list(chars))

    bram = mv_char("Bramwell Kettleworth", main=True, rosters=[spot])
    giver = mv_member("Annabelle Rose Whitford", [bram, mv_char("Wrenna Hollowmere")])
    for case, c, frm, recv in (
            ("the main, a regular kept", bram, giver, mv_member("Bartholomew James Kingsley", [mv_char("Ivoryn Marshwater", main=True)])),
            ("the main, its spot given up", bram, giver, mv_member("Bartholomew James Kingsley", [mv_char("Ivoryn Marshwater", main=True, rosters=[spot])])),
            ("the main to a member with no main, a spot given up", bram, giver, mv_member("Bartholomew James Kingsley", [mv_char("Ivoryn Marshwater", rosters=[spot])])),
            ("the only character to a member with none", mv_char("Bramwell Kettleworth", main=True), mv_member("Annabelle Rose Whitford", []),
             mv_member("Bartholomew James Kingsley", []))):
        dump.add("move_consequence", f"long names, {case}", cq.move_consequence(reg, frm, c, recv), names)
    # the refusals, from the verbs themselves
    dump.add("name_taken_refusal", "long names, an officer", [Registry.name_taken_refusal("Bramwell Kettleworth", am, officer=True)], names)
    dump.add("name_taken_refusal", "long names, a member", [Registry.name_taken_refusal("Bramwell Kettleworth", am, officer=False)], names)
    ac.armory = ArmoryLink(id=711003, slug="bramwell-kettleworth", linked_by="t", linked_at="2026-11-12T00:00:00+00:00")
    for officer in (True, False):
        dump.add("link_refusal", f"long names, a second claim, officer={officer}",
                 [reg.link_refusal(B, "Ivoryn Marshwater", 711003, "Bramwell", "Kettleworth", officer=officer) or ""], names)
    dump.add("link_refusal", "long names, already linked", [reg.link_refusal(A, ac.label, 711003, "Bramwell", "Kettleworth", officer=True) or ""], names)
    dump.add("professions_refusal", "long names", [reg.professions_refusal(ac) or ""], names)
    for self_mode in (True, False):
        dump.add("synced_sentence", f"long names, self_mode={self_mode}", [flows_armory.synced_sentence(ac, self_mode=self_mode)], names)
    ac.armory = None
    dump.add("move_refusal", "long names, the same member", [reg.move_refusal(A, ac.label, A, rs=rs) or ""], names)
    dump.add("move_refusal", "long names, to yourself", [reg.move_refusal(A, ac.label, B, rs=rs, by_id=B) or ""], names)
    dump.add("move_refusal", "long names, no such character", [reg.move_refusal(A, "Bramwell Ashcroftson", B, rs=rs) or ""], names)
    bm.left_at = "2026-11-12T00:00:00+00:00"
    dump.add("move_refusal", "long names, left", [reg.move_refusal(A, ac.label, B, rs=rs) or ""], names)
    dump.add("give_refusal", "long names, left", [reg.give_refusal(B, 711004, "Bramwell", "Ashcroftson") or ""], names)
    bm.left_at = None
    # the seed's words, the finder's sentences, and the professions confirm and fix with these names
    for ok, total in ((1, 1), (12, 12), (3, 5)):
        dump.add("batch_words", f"{ok} of {total}", [armory_seed.batch_words(ok, total)], names)
    for n in (1, 7):
        dump.add("sure_share_words", str(n), [armory_seed.sure_share_words(n)], names)
    dump.add("nobody_sentence", "a long recipe name", [pr.nobody_sentence("Runecloth Robe of the Kettleworth Ascendancy")], names)
    for n in (1, 40):
        dump.add("more_sentence", str(n), [pr.more_sentence(n)], names)
    dump.add("profession_consequence", "long names, an officer",
             _said(cq.profession_consequence, reg, am, ac.label, [{"id": "leatherworking", "skill": 250, "recipes": []}], by_id=OFFICER_ID), names)
    ac.professions = [Profession(id="tailoring", skill=300, recipes=[Recipe(name="Moonclth Bag")])]
    reg.save(am, "typed recipe")
    dump.add("fix_unmatched", "long names, the recipe picked",
             _said(pr.fix_unmatched, reg, A, ac.label, "tailoring", "Moonclth Bag", 10431, "Officer", by_id=OFFICER_ID, preview=True), names)
    dump.add("fix_unmatched", "long names, removed",
             _said(pr.fix_unmatched, reg, A, ac.label, "tailoring", "Moonclth Bag", None, "Officer", by_id=OFFICER_ID, preview=True), names)
    reg.save_character_table(A, [{"label": ac.label, "professions": []}], "t", officer=True)


def _said(fn, *a, **kw) -> list[str]:
    """A people generator's sentences, or its refusal (RegistryError: one sentence) — both are read before a press."""
    from oibot_gm.registry import RegistryError

    try:
        return fn(*a, **kw)
    except RegistryError as e:
        return [str(e)]


def people_cases(reg, rs, runs, ms, dump):
    """Every people generator (consequences.people_consequence) across its cases: an officer on someone, and the member
    on their own record ("your"); a rostered main with an alt, an alt, a lone main; a role flip on a recurring and an
    ad hoc run; the refusals."""
    tank = ms[1]  # rostered on the locked runs
    reg.add_character(tank.discord_id, tank.display_name, "Briarvane", "Warrior", "Arms", None, False)
    tank = reg.members[tank.discord_id]
    solo, _c = reg.add_character(REAL + 60, "Solo", "Soloist", "Mage", "Fire", None, True)
    fates = lambda m, changes: rc.character_preview(reg, rs, m, changes)  # noqa: E731
    for who, by in (("an officer", OFFICER_ID), ("own record", tank.discord_id)):
        main, alt = tank.main.label, "Briarvane"
        for verb in ("retire", "delete"):
            for which, label in (("a rostered main with an alt", main), ("an alt", alt)):
                dump.add(f"{verb}_consequence", f"{which}, {who}", _said(cq.people_consequence, reg, rs, tank, verb, by_id=by, label=label),
                         fates=fates(tank, [{"kind": verb, "label": label}]))
            dump.add(f"{verb}_consequence", f"a lone main, {who}", _said(cq.people_consequence, reg, rs, solo, verb, by_id=solo.discord_id if who == "own record" else by,
                                                                        label=solo.main.label), {"Solo", "Soloist"})
        dump.add("main_consequence", f"an alt becomes the main, {who}", _said(cq.main_consequence, reg, rs, tank, alt, by_id=by),
                 fates=fates(tank, [{"kind": "main", "label": alt}]))
        dump.add("main_consequence", f"already the main, {who}", _said(cq.main_consequence, reg, rs, tank, main, by_id=by))
        dump.add("spec_consequence", f"a role flip on a recurring and an ad hoc run, {who}", _said(cq.spec_consequence, reg, rs, tank, main, "Arms", None, by_id=by),
                 fates=fates(tank, rc.character_changes(reg, tank, [{"label": main, "cls": "Warrior", "spec": "Arms"}])))
        dump.add("spec_consequence", f"an offspec only, {who}", _said(cq.spec_consequence, reg, rs, tank, main, "Protection", "Arms", by_id=by))
        dump.add("spec_consequence", f"nothing changes, {who}", _said(cq.spec_consequence, reg, rs, tank, main, "Protection", None, by_id=by))
        dump.add("flex_consequence", f"two extra roles, {who}", _said(cq.flex_consequence, reg, rs, tank, main, ["healer", "ranged"], by_id=by))
        tank.main.flex = ["healer"]
        dump.add("flex_consequence", f"cleared, {who}", _said(cq.flex_consequence, reg, rs, tank, main, [], by_id=by))
        dump.add("flex_consequence", f"the same roles, {who}", _said(cq.flex_consequence, reg, rs, tank, main, ["healer"], by_id=by))
        tank.main.flex = []
        dump.add("profession_consequence", f"one recorded, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}], by_id=by))
        dump.add("profession_consequence", f"two recorded, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "alchemy", "skill": 120}, {"id": "cooking", "skill": 300}], by_id=by))
        dump.add("profession_consequence", f"three recorded, {who}", _said(cq.profession_consequence, reg, tank, main,
                 [{"id": "tailoring", "skill": 300}, {"id": "cooking", "skill": 200}, {"id": "first_aid", "skill": 150}], by_id=by))
        tank.main.professions = reg.check_professions([{"id": "alchemy", "skill": 120}, {"id": "herbalism", "skill": 90}, {"id": "fishing", "skill": 40}])
        dump.add("profession_consequence", f"three recorded and three removed, {who}", _said(cq.profession_consequence, reg, tank, main,
                 [{"id": "tailoring", "skill": 300}, {"id": "cooking", "skill": 200}, {"id": "first_aid", "skill": 150}], by_id=by))
        dump.add("profession_consequence", f"two recorded and two removed, {who}", _said(cq.profession_consequence, reg, tank, main,
                 [{"id": "alchemy", "skill": 300}, {"id": "tailoring", "skill": 300}], by_id=by))
        dump.add("profession_consequence", f"three removed, {who}", _said(cq.profession_consequence, reg, tank, main, [], by_id=by))
        tank.main.professions = reg.check_professions([{"id": "alchemy", "skill": 120}])
        dump.add("profession_consequence", f"removed, {who}", _said(cq.profession_consequence, reg, tank, main, [], by_id=by))
        dump.add("profession_consequence", f"the same professions, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "alchemy", "skill": 120}], by_id=by))
        tank.main.professions = []
        dump.add("rank_consequence", f"trial to core, {who}", _said(cq.rank_consequence, reg, tank, "core", by_id=by))
        dump.add("rank_consequence", f"the same rank, {who}", _said(cq.rank_consequence, reg, tank, tank.rank, by_id=by))
        dump.add("rank_consequence", f"alt, {who}", _said(cq.rank_consequence, reg, tank, "alt", by_id=by))
        dump.add("confirm_consequence", f"a named character, {who}", _said(cq.confirm_consequence, reg, tank, alt, by_id=by))
        for on in (False, True):
            dump.add("dm_consequence", f"turn {'on' if on else 'off'}, {who}", _said(cq.dm_consequence, reg, tank, on, by_id=by))
            tank.dm_opt_out = not on
    tank.dm_opt_out = False
    c = next(x for x in tank.active() if x.label == "Briarvane")
    c.confirmed_by = "Officer"
    dump.add("confirm_consequence", "already confirmed, an officer", _said(cq.confirm_consequence, reg, tank, "Briarvane", by_id=OFFICER_ID))
    reg.set_plan(REAL + 61, "Planner", "Priest", "Holy", None, "main")
    dump.add("confirm_consequence", "a planned character, an officer", _said(cq.confirm_consequence, reg, reg.members[REAL + 61], "Priest (Holy)", by_id=OFFICER_ID), {"Priest"})
    nomain = SimpleNamespace(discord_id=REAL + 62, display_name="Nomain", main=None, rank=None, dm_opt_out=False)
    dump.add("rank_consequence", "no main yet, an officer", _said(cq.rank_consequence, reg, nomain, "core", by_id=OFFICER_ID), {"Nomain"})
    # professions: an officer's fix of a recipe typed before the game's list (armory link §4.3): pick it, or remove it
    from oibot_gm import professions as pr

    crafters = ms[6:11]
    from oibot_gm.registry import Profession, Recipe

    for i, cm in enumerate(crafters):  # spellings typed before the game's list (kept unmatched): what the fix is for
        cm.main.professions = [Profession(id="tailoring", skill=300, recipes=[Recipe(name="Moonclth Bag" if i == 0 else "Mooncloth Bagg")])]
        reg.save(cm, "typed recipe")
    fixed = crafters[0]

    def fix(name, rid):
        return _said(pr.fix_unmatched, reg, fixed.discord_id, fixed.main.label, "tailoring", name, rid, "Officer", by_id=OFFICER_ID, preview=True)

    dump.add("fix_unmatched", "the recipe picked", fix("Moonclth Bag", 10431))
    dump.add("fix_unmatched", "removed", fix("Moonclth Bag", None))
    dump.add("fix_unmatched", "no longer on the record", fix("Mooncloth Bagg", None))
    dump.add("fix_unmatched", "another profession's recipe", fix("Moonclth Bag", 10789))
    fixed.main.professions[0].recipes.append(Recipe(name="Mooncloth Bag", id=10431))
    dump.add("fix_unmatched", "the record already lists it", fix("Moonclth Bag", 10431))
    for cm in crafters:
        reg.save_character_table(cm.discord_id, [{"label": cm.main.label, "professions": []}], "t", officer=True)
    from oibot_gm import configops

    cm = crafters[0]
    lab = cm.main.label
    dump.add("PROFESSIONS_NOT_HERE", "a profession said in a message", [s if s.endswith(".") else s + "." for s in configops.PROFESSIONS_NOT_HERE.split(". ")])
    dump.add("profession_consequence", "no skill said, with recipes, an officer", _said(cq.profession_consequence, reg, cm, lab,
             [{"id": "tailoring", "skill": None, "recipes": [{"name": "Mooncloth Bag"}, {"name": "Bottomless Bag"}]}], by_id=OFFICER_ID))
    dump.add("profession_consequence", "no skill said, own record", _said(cq.profession_consequence, reg, cm, lab, [{"id": "tailoring", "skill": None}], by_id=cm.discord_id))
    # I'm back: an absence over the open sheet it answered No thanks, and one that touched no sheet
    ev = runs["open recurring"]
    m = ms[0]
    day = rc.run_day(reg, ev)
    reg.add_absence(m.discord_id, day, day, None, "t")
    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
    far = (reg.now_local().date() + timedelta(days=60)).isoformat()
    reg.add_absence(m.discord_id, far, (reg.now_local().date() + timedelta(days=62)).isoformat(), None, "t")
    for who, by in (("an officer", OFFICER_ID), ("own record", m.discord_id)):
        a = next(x for x in m.absences if x.start == day)
        dump.add("back_consequence", f"an open sheet answered for it, {who}", _said(cq.back_consequence, reg, rs, m, day, by_id=by),
                 fates=discord_people.absence_clear_lines(reg, rs, m, a))
        dump.add("back_consequence", f"no sheet touched, {who}", _said(cq.back_consequence, reg, rs, m, far, by_id=by))
    dump.add("back_consequence", "no such absence", _said(cq.back_consequence, reg, rs, m, "2020-01-01", by_id=OFFICER_ID))
    # Add the absence: one over a live sheet, one over no sheet, for an officer and the member's own record
    other = ms[1]
    for who, by in (("an officer", OFFICER_ID), ("own record", other.discord_id)):
        out = cq.absence_consequence(reg, rs, other, day, day, by_id=by)
        dump.add("absence_consequence", f"over a live sheet, {who}", out, fates=discord_people.absence_lines(reg, rs, other, day, day))
        dump.add("absence_consequence", f"no sheet in those days, {who}", cq.absence_consequence(reg, rs, other, far, far, by_id=by))
    span = cq.absence_consequence(reg, rs, other, far, (reg.now_local().date() + timedelta(days=62)).isoformat(), by_id=OFFICER_ID)
    dump.add("absence_consequence", "several days, an officer", span)
    rc.set_signup(reg, rs, ev, m, None, "in", source="member")
    # a Save of edited rows: one sentence per fate (the ripple), nothing else
    rows = [{"label": tank.main.label, "cls": "Warrior", "spec": "Arms"}]
    out = cq.save_consequence(reg, rs, tank, rows)
    dump.add("save_consequence", "a respec on two locked runs", out, fates=out)
    # Remove record with one, two and many characters
    from oibot_gm import discord_registry

    dump.add("remove_record_consequence", "two characters", discord_registry.remove_record_consequence(reg, tank))
    dump.add("remove_record_consequence", "no characters", discord_registry.remove_record_consequence(reg, SimpleNamespace(display_name="Pell", characters=[])), {"Pell"})


def test_the_lint_catches_the_two_blobs():
    """The two confirms the user called unintelligible (2026-10-26) fail the lint; their rewrites pass."""
    names = {"cbottoms"}
    blob1 = ["The board becomes the roster. No confirmations — Join is the commitment. the lock decides the roster.",
             "cbottoms sees a notice on their Me page — no new DM."]
    blob2 = ["cbottoms goes to No thanks on Barrow Deeps Wed 4 Nov 7:00 PM and a callout is recorded (hours before counted; "
             "your note, if any, is for officers). cbottoms sees a notice on their Me page — no new DM."]
    assert lint(blob1, names) and lint(blob2, names)
    assert not lint(["The board becomes the roster. On an ad hoc run Join is the commitment, so nobody is asked to confirm."], names)
    assert not lint(["cbottoms goes to No thanks for Barrow Deeps on Wed 4 Nov. The bot records a callout and how late it came.",
                     "They see it on their Me page."], names)


def _box_line(reg, sid, field, value):
    from oibot_gm import configops

    ops = configops.ops_from_changes(reg, [{"kind": "schedule", "target": sid, "fields": {field: value}}], revs={f"config:schedule:{sid}": reg.config_rev(f"schedule:{sid}")})
    box = configops.describe_ops(reg, ops).box
    assert box and len(box["diff"]) == 1, (field, box)
    return box["diff"][0]


def test_a_save_boxs_diff_lines_are_the_pages_words(reg):
    """Phase 4, Task 11 fix round: a schedule's Save box and a raid's Save box print each change as "label: old → new" in
    the Schedules and Raids pages' own words (PAGE_WORDS schedule_fields / raid_fields / priority / split) — "Locks:
    24 hours before → 12 hours before", never the field's name, never the label twice, never true / false."""
    from oibot_gm import configops
    from oibot_gm.constants import schedule_line_label

    hy = "hyjal_summit_forever"
    sid = _weekly(reg, hy, "Tue 18:30", sid="tue")
    changes = {"signup_lead_hours": 72, "nudge_hours_before": 12, "lock_hours_before": 12, "confirm_hours_before": 3, "fill_ask_hours": 2,
               "nudge": False, "autofill": False, "open_dm": True, "confirm": False, "split_policy": "first", "weights": {"rank": 5, "main": None},
               "name": "Tuesdays", "slots": ["Wed 19:00"], "lineup": [{"raid": hy, "rosters": 2}]}
    code = re.compile(r"_|\b(true|false|None|inherit|hours before:|weights|nudge|autofill|open dm|split policy|signup lead)\b", re.I)
    for field, value in changes.items():
        line = _box_line(reg, sid, field, value)
        label = schedule_line_label(field)
        assert line.startswith(f"{label}: ") and line.count(label) == 1, (field, line)
        assert not code.search(line), (field, line)
    assert _box_line(reg, sid, "lock_hours_before", 12) == "Locks: 24 hours before → 12 hours before"
    assert _box_line(reg, sid, "open_dm", True) == "Sheet Opening DM: off → on"
    assert _box_line(reg, sid, "nudge", False) == "Signup Reminder: on → off"
    # the two switches the Details rows show as Yes / No and On / Off print the same values here (issue #13 follow-up)
    assert _box_line(reg, sid, "confirm", False) == "Lock Confirmation: Yes → No"
    assert _box_line(reg, sid, "autofill", False) == "Automated Fill Requests: On → Off"
    # issue 14: Auto Lock and its auto-fill read On / Off, as their Details rows do
    assert _box_line(reg, sid, "auto_lock", False) == "Auto Lock: On → Off"
    assert _box_line(reg, sid, "auto_lock_fill", False) == "Auto-fill on Auto Lock: On → Off"
    assert _box_line(reg, sid, "nudge_hours_before", 12).startswith("Signup Reminder goes out: "), "the select's line names its switch"
    assert _box_line(reg, sid, "split_policy", "first") == "When one night fills two rosters: the raid's → Raid one first"
    for field, value, want in (("lockout_days", 5, "Lockout: 7 days → 5 days"), ("duration_hours", 2.5, "Length: 3 hours → 2.5 hours"),
                               ("weights", {"rank": 5}, "Rank: 3 → 5")):
        ops = configops.ops_from_changes(reg, [{"kind": "raid", "target": hy, "fields": {field: value}}])
        assert configops.describe_ops(reg, ops).box["diff"] == [want], field
    ops = configops.ops_from_changes(reg, [{"kind": "raid", "target": hy, "fields": {"first_open": "2026-12-20T19:00"}}])
    line = configops.describe_ops(reg, ops).box["diff"][0]
    assert line.startswith("First opens: ") and not code.search(line), line


def final_review_boxes(reg, rs, lk, add):
    """The final fix wave's boxes (§7b, built on the server): Remind, End the run, a locked board's moves."""
    from conftest import open_test_run

    from oibot_gm.discord_loot import LootMixin

    rows = rc.open_placement_rows(reg, lk)
    saved = [(a, dict(a)) for _m, a in rows]
    for _m, a in rows:
        a["answer"], a["channel"] = None, "dm"
    add("remind_box", "the unanswered get the DM again", cq.remind_box(reg, lk, rs))
    if rows:
        rows[0][1]["channel"] = "web"
        add("remind_box", "one waits on the site", cq.remind_box(reg, lk, rs))
    lk.reminded_at, lk.reminded_by = rc.now(), "Officer"
    add("remind_box", "reminded within the hour", cq.remind_box(reg, lk, rs))
    lk.reminded_at = lk.reminded_by = None
    for a, was in saved:
        a.clear()
        a.update(was)
    open_ = next(e for e in rs.events.values() if e.state == "open")
    add("remind_box", "an open sheet", cq.remind_box(reg, open_, rs))
    bot = SimpleNamespace(events={})
    bot.end_sentences = lambda r, s, e: LootMixin.end_sentences(bot, r, s, e)
    add("end_box", "no council", LootMixin.end_box(bot, reg, rs, lk))
    sess = SimpleNamespace(guild=reg.key, run_key=lk.key)
    bot.events = {"s": sess}
    bot.loot_summary = lambda r, s: {"awards": 6, "overrides": 1, "never_confirmed": [1], "undistributed": [1, 2], "reasons_pending": [1]}
    add("end_box", "a council with loose ends", LootMixin.end_box(bot, reg, rs, lk))
    bot.events = {}
    add("end_box", "an open sheet", LootMixin.end_box(bot, reg, rs, open_))
    ev = open_test_run(reg, rs, days=9)
    pool = reg.test_members()[:10]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool[:8]])
    lay = rc.board_layout(reg, ev)
    a, bench = lay[0][0], pool[9].display_name
    for case, name, to in (("move to another group", a, (1, 99)), ("swap", a, (1, 0)), ("take off the roster", a, None),
                           ("put on the roster", bench, (1, 99)), ("put in someone's seat", bench, (1, 0)),
                           ("a full group", bench, (0, 99)), ("where they already are", a, (0, 0))):
        add("board_move_box", case, cq.board_move_box(reg, ev, name, to))
    sub, out = reg.test_members()[10], reg.test_members()[11]
    join(reg, rs, ev, [sub], "sub")
    join(reg, rs, ev, [out], "out")
    add("board_move_box", "a Bench answer off the locked roster", cq.board_move_box(reg, ev, sub.display_name, (1, 99)))
    add("board_move_box", "someone who said No thanks", cq.board_move_box(reg, ev, out.display_name, (1, 99)))
    add("board_move_box", "before the lock", cq.board_move_box(reg, open_, next(iter(open_.by_status("in")), SimpleNamespace(display_name="Nobody")).display_name, (0, 99)))


def _one_day_of_events(reg, rs, ms, n_adhoc: int, locked_too: bool):
    """A day five days out holding a three-raid event and `n_adhoc` more ad hoc events of Barrow Deeps (the second one
    locked with ms[0] rostered when `locked_too`), every one joined by ms."""
    weekly(reg)
    reg.set_raid_overrides("hyjal_summit_forever", {"first_open": "2026-01-01T00:00"}, "t")
    base = (reg.now_local() + timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0)
    three = rc.open_run(reg, rs, "hyjal_summit_forever", base, by="t", schedule=rc.ADHOC_SCHEDULE,
                        lineup=[{"raid": RID, "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}])
    join(reg, rs, three, ms)
    evs = [three]
    for i in range(n_adhoc):
        ev = rc.open_run(reg, rs, RID, base.replace(hour=14 + 2 * i), by="t", schedule=rc.ADHOC_SCHEDULE)
        if locked_too and i == 1:
            locked(reg, rs, ev, ms)
        else:
            join(reg, rs, ev, ms)
        evs.append(ev)
    return evs, rc.run_day(reg, three)


def test_an_absence_names_each_event_once(reg, rs):
    """A three-raid event is one event: its sheet is named once, by its lineup (the fix of 2026-11-24)."""
    ms = people(reg)
    _evs, day = _one_day_of_events(reg, rs, ms, 1, False)
    lines = discord_people.absence_lines(reg, rs, ms[11], day, day)
    assert len(lines) == 2 and not lint(lines, names_of(reg))
    assert lines[0] == f"The sheet for the Hyjal Summit event on {reg.day_label(day)} is answered No thanks."
    lineup = [{"raid": "hyjal_summit_forever", "rosters": 1}, {"raid": RID, "rosters": 2}]
    later = (reg.now_local() + timedelta(days=9)).replace(hour=12, minute=0, second=0, microsecond=0)
    two = rc.open_run(reg, rs, "hyjal_summit_forever", later, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=lineup[1:])
    join(reg, rs, two, ms)
    d2 = rc.run_day(reg, two)
    assert discord_people.absence_lines(reg, rs, ms[11], d2, d2) == [
        f"The sheet for Hyjal Summit, then Barrow Deeps × 2 on {reg.day_label(d2)} is answered No thanks."]


def test_an_absence_over_many_events_sums_up_and_keeps_the_freed_seat(reg, rs):
    """Four events or more on the absent day: at most three sentences in all, the freed seat said (a loss is never
    dropped), the rest summed; every confirm that prints them lints on any date."""
    ms = people(reg)
    evs, day = _one_day_of_events(reg, rs, ms, 4, True)
    dump = Dump(names_of(reg))
    rostered, bench = ms[0], ms[11]
    for who, m in (("rostered on one", rostered), ("not rostered", bench)):
        lines = discord_people.absence_lines(reg, rs, m, day, day)
        dump.add("absence_lines", f"five events one day, {who}", lines)
        assert 1 <= len(sentences(lines)) <= 3
        out = cq.absence_consequence(reg, rs, m, day, day, by_id=OFFICER_ID)
        dump.add("absence_consequence", f"five events one day, {who}", out, fates=lines)
    seat = discord_people.absence_lines(reg, rs, rostered, day, day)
    assert any("released" in s for s in seat) and any("No thanks" in s for s in seat)
    assert all("released" not in s for s in discord_people.absence_lines(reg, rs, bench, day, day))
    # I'm back over the same day: the answers the absence wrote
    for ev in evs:
        rc.set_signup(reg, rs, ev, bench, None, "out", source="absence")
    back = discord_people.absence_clear_lines(reg, rs, bench, SimpleNamespace(start=day, end=day))
    dump.add("absence_clear_lines", "five events one day", back)
    assert 1 <= len(sentences(back)) <= 3 and len(set(back)) == len(back)
    assert not dump.problems, "\n".join(dump.problems)
