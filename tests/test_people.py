"""Dual interfaces phase 2, part C — people (design §3.3, owner decisions D24 D25 D28 D29 D30 D50; docs/design.md §5.41):
the character verb's ripple into sheets and seats (discord_people.save_characters over the REAL RaidMixin verbs), rank per
member, confirm by (member, label), validate-on-copy and the table rev, absences (overlap, reason cap, I'm back), the
owner transfer's refusals, a removed schedule's regulars — all over a scratch GitStore (conftest)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import OWNER, join, lock_with_board
from test_parity_verbs import Bot

from oibot_gm import configops, discord_people
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_pool import absences_card
from oibot_gm.registry import ABSENCE_REASON_MAX, CharactersChanged, RegistryError

REAL = 410_000_000_000_000_000


def run(coro):
    return asyncio.run(coro)


def real(reg, n: int, cls="Priest", spec="Holy", name=None, surname=None):
    """A real (non-puppet) member with a main: real runs take real members only."""
    names = ["Anabelle", "Brannoc", "Corwin", "Delphine", "Elowen", "Fintan", "Gwendol", "Halvard", "Isolde", "Jorund", "Kestrel", "Liora"]
    uid = REAL + n
    reg.add_character(uid, f"Member{n}", name or names[n % len(names)], cls, spec, None, True, surname=surname)
    return reg.members[uid]


def real_run(reg, rs, *, days=3, schedule=None):
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t", schedule=schedule)
    assert not rc.is_test_run(reg, ev)
    return ev


class PeopleBot(Bot):
    """The real RaidMixin + AbsencesMixin (+ PeopleMixin) verbs; the fill engine's batch and the run's updates line are
    recorded instead of sent, and no DM can leave (send_member_dm records — the DM rule says none should)."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.fills, self.updates, self.sent_dms = [], [], []

    async def run_fill(self, reg, rs, ev, team, by="scheduler", approved_by=None, swaps=None):
        self.fills.append((ev.key, by))
        return [], {}

    async def post_run_update(self, reg, ev, text):
        self.updates.append((ev.key, text))

    async def send_member_dm(self, reg, m, text=None, view=None, ev=None):
        self.sent_dms.append((m.discord_id, text))
        return True


@pytest.fixture
def bot(reg, rs):
    return PeopleBot(reg, rs)


def seated(reg, rs, n=6, *, schedule=None):
    """A locked real run around `n` real members (first a healer, then dps), every one seated."""
    ms = [real(reg, 0, "Priest", "Holy")] + [real(reg, i, "Mage", "Fire") for i in range(1, n)]
    ev = real_run(reg, rs, schedule=schedule)
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    rc.run_team(reg, ev)["autofill"] = True
    return ev, ms


# ---------------------------------------------------------------- the ripple (D24)

def test_respec_rewrites_the_open_sheet_answer(reg, rs, bot):
    m = real(reg, 0, "Priest", "Holy")
    ev = real_run(reg, rs)
    join(reg, rs, ev, [m])
    before = ev.signups[str(m.discord_id)]
    res = run(bot.save_characters(reg, m.discord_id, [{"label": m.main.label, "cls": "Priest", "spec": "Shadow"}], m.display_name, by_id=m.discord_id))
    sg = ev.signups[str(m.discord_id)]
    assert (sg.spec, sg.role, sg.status, sg.source) == ("Shadow", "ranged", "in", "respec")
    assert sg.joined_at == before.order_at  # a Join that stays a Join keeps its queue place
    assert any("now reads Shadow" in line for line in res["ripple"])
    assert not bot.sent_dms


def test_role_flip_on_a_locked_recurring_run_keeps_the_seat_and_tells_officers(reg, rs, bot):
    ev, ms = seated(reg, rs)
    healer = ms[0]
    assert rc.run_kind(reg, ev) == "recurring"
    res = run(bot.save_characters(reg, healer.discord_id, [{"label": healer.main.label, "cls": "Priest", "spec": "Shadow"}], healer.display_name, by_id=healer.discord_id))
    seat = ev.seat_of(healer)
    assert seat is not None and seat[1].role == "healer" and seat[1].spec == "Holy", "the roster stays as locked: an officer decides"
    assert any("kept as it is" in t for _k, t in bot.updates) and any("officers are told" in r for r in res["ripple"])
    assert any(level == "warn" and "respecced" in text for level, text in bot.ops.lines)
    assert not bot.fills and not bot.sent_dms  # nothing freed, nobody DMed (D24, the DM rule)
    assert reg.members[healer.discord_id].main.spec == "Shadow"


def test_role_flip_on_a_locked_adhoc_run_hands_the_seat_back(reg, rs, bot):
    ev, ms = seated(reg, rs, schedule=rc.ADHOC_SCHEDULE)
    assert rc.run_kind(reg, ev) == "adhoc"
    healer = ms[0]
    run(bot.save_characters(reg, healer.discord_id, [{"label": healer.main.label, "cls": "Priest", "spec": "Shadow"}], healer.display_name, by_id=healer.discord_id))
    assert ev.seat_of(healer) is None and ev.signups[str(healer.discord_id)].status == "out"
    assert bot.fills, "the freed seat goes to the fill engine"


def test_same_role_respec_follows_the_seat(reg, rs, bot):
    ev, ms = seated(reg, rs)
    healer = ms[0]
    run(bot.save_characters(reg, healer.discord_id, [{"label": healer.main.label, "cls": "Priest", "spec": "Discipline"}], healer.display_name, by_id=healer.discord_id))
    seat = ev.seat_of(healer)
    assert seat is not None and (seat[1].spec, seat[1].role) == ("Discipline", "healer")
    assert ev.signups[str(healer.discord_id)].spec == "Discipline" and not bot.fills


def test_deleting_a_seated_character_frees_the_seat_and_asks_the_bench(reg, rs, bot):
    ev, ms = seated(reg, rs)
    m = ms[2]
    reg.add_character(m.discord_id, m.display_name, "Spareone", "Warrior", "Protection", None, False)
    label = m.main.label
    res = run(bot.save_characters(reg, m.discord_id, [{"label": label, "delete": True}], "Officer", by_id=OWNER, officer=True))
    assert ev.seat_of(m) is None and ev.signups[str(m.discord_id)].status == "out"
    assert bot.fills == [(ev.key, "character deleted")]
    assert any("handed back and the bench is asked" in r for r in res["ripple"])
    assert reg.members[m.discord_id].main.label == "Spareone"
    assert not bot.sent_dms


def test_retire_clears_an_open_join_and_drops_the_regular(reg, rs, bot):
    reg.set_raid_overrides("barrow_deeps", {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    m = real(reg, 0, "Priest", "Holy")
    reg.add_character(m.discord_id, m.display_name, "Altoni", "Mage", "Fire", None, False)
    reg.regular_add(m.discord_id, "barrow_deeps:default", "t", "Altoni")
    ev = real_run(reg, rs)
    rc.set_signup(reg, rs, ev, m, "Altoni", "in", source="member")
    preview = discord_people.character_preview(bot, reg, m.discord_id, [{"label": "Altoni", "retire": True}])
    assert any("comes off it" in p for p in preview) and any("answer is cleared" in p for p in preview)
    run(bot.save_characters(reg, m.discord_id, [{"label": "Altoni", "retire": True}], m.display_name, by_id=m.discord_id))
    assert str(m.discord_id) not in ev.signups
    assert not reg.regulars("barrow_deeps:default")


def test_preview_writes_nothing_and_refuses_like_the_save(reg, rs, bot):
    ev, ms = seated(reg, rs)
    m = ms[1]
    rev = reg.characters_rev(m)
    res = run(bot.save_characters(reg, m.discord_id, [{"label": m.main.label, "delete": True}], "Officer", by_id=OWNER, officer=True, preview=True))
    assert res["preview"] if "preview" in res else True
    assert any("handed back" in r for r in res["ripple"]) and reg.characters_rev(reg.members[m.discord_id]) == rev and ev.seat_of(m)
    with pytest.raises(RegistryError, match="Only an officer sets a rank"):
        run(bot.save_characters(reg, m.discord_id, [{"label": m.main.label, "rank": "core"}], m.display_name, by_id=m.discord_id, preview=True))


# ---------------------------------------------------------------- rank per member (D25), confirm by (member, label)

def test_rank_lives_on_the_member_and_follows_the_main(reg):
    m = real(reg, 0)
    reg.set_rank(m.discord_id, "core", "Officer", by_id=OWNER)
    reg.add_character(m.discord_id, m.display_name, "Secondo", "Mage", "Fire", None, False)
    mm = reg.members[m.discord_id]
    assert mm.rank == "core" and next(c for c in mm.characters if c.label == "Secondo").rank == "alt"
    reg.set_main(m.discord_id, "Secondo")
    mm = reg.members[m.discord_id]
    assert mm.main.label == "Secondo" and mm.rank == "core" and all(c.rank == "alt" for c in mm.characters if not c.is_main)
    reg.retire(m.discord_id, "Secondo")  # the main retires: the standing follows the player to the next main
    mm = reg.members[m.discord_id]
    assert mm.main.label == "Anabelle" and mm.rank == "core"
    reg.add_character(m.discord_id, m.display_name, "Terzo", "Rogue", "Combat", None, True)  # a new main takes the rank too
    assert reg.members[m.discord_id].main.label == "Terzo" and reg.members[m.discord_id].rank == "core"


def test_set_rank_refuses_your_own_and_an_alt_row(reg):
    m = real(reg, 0)
    with pytest.raises(RegistryError, match="your own rank"):
        reg.set_rank(m.discord_id, "core", m.display_name, by_id=m.discord_id)
    reg.config.owner_discord_id = m.discord_id  # the owner is excepted
    reg.set_rank(m.discord_id, "core", m.display_name, by_id=m.discord_id)
    reg.add_character(m.discord_id, m.display_name, "Altissa", "Mage", "Fire", None, False)
    with pytest.raises(RegistryError, match="rank lives on the main"):
        reg.save_character_table(m.discord_id, [{"label": "Altissa", "rank": "raider"}], "Officer", officer=True, by_id=OWNER)


def test_a_member_cannot_rank_themselves_through_the_table(reg):
    m = real(reg, 0)
    with pytest.raises(RegistryError, match="Only an officer sets a rank"):
        reg.save_character_table(m.discord_id, [{"label": m.main.label, "rank": "core"}], m.display_name)
    assert reg.members[m.discord_id].rank == "trial"


def test_main_then_rank_in_one_save(reg):
    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Jonny", "Mage", "Fire", None, False)
    lines = reg.save_character_table(m.discord_id, [{"label": "Jonny", "main": True, "rank": "raider"}], "Officer", officer=True, by_id=OWNER)
    mm = reg.members[m.discord_id]
    assert mm.main.label == "Jonny" and mm.rank == "raider" and any("rank raider" in line for line in lines)


def test_confirm_by_member_and_label_never_touches_a_namesake(reg):
    a = real(reg, 0, name="Jon", surname="Smith")
    b = real(reg, 1, name="Jon", surname="Jones")
    reg.confirm(b.discord_id, "Jon Jones", "Officer", by_id=OWNER)
    assert reg.members[b.discord_id].main.confirmed_by == "Officer" and not reg.members[a.discord_id].main.confirmed_by
    with pytest.raises(RegistryError, match="your own character"):
        reg.confirm(a.discord_id, "Jon Smith", a.display_name, by_id=a.discord_id)
    with pytest.raises(RegistryError, match="already confirmed"):
        reg.confirm(b.discord_id, "Jon Jones", "Someone", by_id=OWNER)


def test_plain_text_rank_names_the_member_on_the_main(reg):
    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Altrank", "Mage", "Fire", None, False)
    assert configops.apply(reg, ConfigOp(op="rank", character=m.main.label, value="core"), "Officer", is_owner=True) == f"{m.display_name} → core"
    with pytest.raises(RegistryError, match="is an alt"):
        configops.apply(reg, ConfigOp(op="rank", character="Altrank", value="raider"), "Officer", is_owner=True)


# ---------------------------------------------------------------- the table: validate on a copy, the rev

def test_a_refused_row_leaves_nothing_applied(reg):
    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Secunda", "Mage", "Fire", None, False)
    rev = reg.characters_rev(m)
    with pytest.raises(RegistryError):
        reg.save_character_table(m.discord_id, [{"label": m.main.label, "cls": "Priest", "spec": "Shadow"},
                                                {"label": "Secunda", "cls": "Mage", "spec": "Holy"}], m.display_name)  # row 2: not a Mage spec
    mm = reg.members[m.discord_id]
    assert reg.characters_rev(mm) == rev and mm.main.spec == "Holy", "row 1 must not have been committed"


def test_a_stale_rev_is_refused(reg):
    m = real(reg, 0)
    rev = reg.characters_rev(m)
    reg.save_character_table(m.discord_id, [{"label": m.main.label, "cls": "Priest", "spec": "Shadow"}], m.display_name, rev=rev)
    with pytest.raises(CharactersChanged):
        reg.save_character_table(m.discord_id, [{"label": m.main.label, "cls": "Priest", "spec": "Discipline"}], m.display_name, rev=rev)
    assert reg.members[m.discord_id].main.spec == "Shadow"


def test_a_puppet_belongs_to_its_rehearsals_tester(reg, rs):
    from conftest import open_test_run

    tester, other = 777_000_000_000_001, 777_000_000_000_002
    open_test_run(reg, rs, test_by=tester)
    p = reg.test_members()[0]
    with pytest.raises(RegistryError, match="test puppet"):
        reg.set_rank(p.discord_id, "core", "Other", by_id=other)
    reg.set_rank(p.discord_id, "core", "Tester", by_id=tester)
    assert reg.members[p.discord_id].rank == "core"


# ---------------------------------------------------------------- absences

def test_an_overlapping_absence_is_refused_with_the_span_and_who(reg):
    m = real(reg, 0)
    reg.add_absence(m.discord_id, "2099-03-10", "2099-03-12", None, "Kessa")
    with pytest.raises(RegistryError, match="already away") as e:
        reg.add_absence(m.discord_id, "2099-03-12", "2099-03-14", None, m.display_name)
    assert "recorded by Kessa" in str(e.value) and "I'm back" in str(e.value)
    reg.add_absence(m.discord_id, "2099-03-13", "2099-03-14", None, m.display_name)  # the day after is fine
    assert len(reg.members[m.discord_id].absences) == 2


def test_the_reason_cap_is_at_the_verb(reg):
    m = real(reg, 0)
    with pytest.raises(RegistryError, match=f"at most {ABSENCE_REASON_MAX}"):
        reg.add_absence(m.discord_id, "2099-03-10", None, "x" * (ABSENCE_REASON_MAX + 1), m.display_name)
    assert not reg.members[m.discord_id].absences


def test_im_back_reopens_the_sheet_the_absence_answered(reg, rs, bot):
    m = real(reg, 0)
    ev = real_run(reg, rs)
    day = rc.run_day(reg, ev)
    _m, a, touched = run(bot.record_absence(reg, m.discord_id, day, None, "trip", m.display_name, by_id=m.discord_id))
    assert touched == [ev.key] and ev.signups[str(m.discord_id)].source == "absence"
    assert discord_people.absence_clear_lines(reg, rs, reg.members[m.discord_id], a) == [f"{rc._run_words(reg, ev)}: the sheet re-opens — answer it again"]
    a2, lines = run(bot.im_back(reg, m.discord_id, a.start, m.display_name, by_id=m.discord_id))
    assert a2.start == a.start and str(m.discord_id) not in ev.signups and lines
    assert not reg.members[m.discord_id].absences


def test_join_while_away_says_the_absence_stands(reg, rs, bot):
    m = real(reg, 0)
    ev = real_run(reg, rs)
    day = rc.run_day(reg, ev)
    reg.add_absence(m.discord_id, day, None, None, m.display_name)
    line = run(bot.set_answer(reg, rs, ev, m, "in", None, by=m.display_name, by_id=m.discord_id))
    assert "joined for this run" in line and "still stands" in line
    assert ev.signups[str(m.discord_id)].status == "in" and reg.members[m.discord_id].absences  # Join is pure; the record stands


def test_puppets_never_reach_the_public_absences_card(reg):
    p = reg.test_members()[0]
    m = real(reg, 0)
    today = reg.now_local().date()
    reg.add_absence(p.discord_id, (today + timedelta(days=2)).isoformat(), None, None, "t")
    reg.add_absence(m.discord_id, (today + timedelta(days=2)).isoformat(), None, "secret", m.display_name)
    text = absences_card(reg).fields[0].value
    assert m.display_name in text and p.display_name not in text and "secret" not in text  # D28: names + days in words


# ---------------------------------------------------------------- owner transfer (D50) and schedules (D30)

def test_set_owner_refusals_and_the_write(reg):
    m = real(reg, 0)
    with pytest.raises(RegistryError, match="bot"):
        reg.set_owner(m.discord_id, "Owner", by_id=OWNER, bot=True, name=m.display_name)
    with pytest.raises(RegistryError, match="not in"):
        reg.set_owner(m.discord_id, "Owner", by_id=OWNER, membership="no", name=m.display_name)
    with pytest.raises(RegistryError, match="Couldn't check Discord"):
        reg.set_owner(m.discord_id, "Owner", by_id=OWNER, membership="unknown", name=m.display_name)
    with pytest.raises(RegistryError, match="already owns"):
        reg.set_owner(OWNER, "Owner", by_id=OWNER, name="Owner")
    with pytest.raises(RegistryError, match="That's you"):
        reg.set_owner(555, "Host", by_id=555, name="Host")
    assert reg.config.owner_discord_id == OWNER
    reg.set_owner(m.discord_id, "Owner", by_id=OWNER, name=m.display_name)
    assert reg.config.owner_discord_id == m.discord_id and reg.load_config().owner_discord_id == m.discord_id
    assert "You lose owner rights" in reg.owner_consequence("X", OWNER) and "nobody is DMed" in reg.owner_consequence("X", OWNER)


def test_transfer_owner_verb_audits_and_dms_nobody(reg, rs, bot, monkeypatch):
    from oibot_gm import guild_routing

    m = real(reg, 0)

    async def member_of(_bot, _reg, uid, timeout=None, max_age=None):
        return "yes", type("M", (), {"display_name": m.display_name, "bot": False})()
    monkeypatch.setattr(guild_routing, "member_of", member_of)
    line = run(bot.transfer_owner(reg, m.discord_id, "Owner", by_id=OWNER))
    assert m.display_name in line and reg.config.owner_discord_id == m.discord_id
    assert any(level == "warn" and "owner →" in text for level, text in bot.ops.lines) and not bot.sent_dms and not bot.dms


def test_removing_a_schedule_drops_its_regulars_without_a_dm(reg, bot):
    reg.set_schedule("barrow_deeps", "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    a, b = real(reg, 0), real(reg, 1)
    reg.regular_add(a.discord_id, "barrow_deeps:alt", "t")
    reg.regular_add(b.discord_id, "barrow_deeps:alt", "t")
    op = ConfigOp(op="schedule_remove", target="barrow_deeps", field="alt")
    assert "2 regulars come off it" in configops.describe(reg, op) and "nobody is DMed" in configops.describe(reg, op)
    line = reg.remove_schedule("barrow_deeps", "alt", "Owner")
    assert "2 regulars off it" in line and a.display_name in line
    assert not any("barrow_deeps:alt" in c.rosters for m in reg.members.values() for c in m.characters)
    assert not reg.orphan_regulars() and not bot.sent_dms and not bot.dms


def test_owner_transfer_is_discord_and_web_never_mcp(reg, rs):
    """Owner transfer left BY_DESIGN_ONLY (phase 2 C): /gm config owner and POST /api/owner, both owner-only; no MCP tool
    — the token IS the owner."""
    from test_mcp import FakeBot

    from oibot_gm import mcp_server as ms
    from oibot_gm.web import app as web
    from oibot_gm.wizard import FLOWS

    assert "owner_transfer" not in configops.BY_DESIGN_ONLY and "owner_transfer" in FLOWS
    routes = {f"{m} {r.path}" for r in web.create_app(FakeBot(reg, rs)).routes for m in (getattr(r, "methods", None) or ())}
    assert "POST /api/owner" in routes
    assert not any("owner" in t.name for t in asyncio.run(ms.mcp.list_tools()))


def test_migrate_ranks_puts_the_standing_on_the_main_once(reg, data_root):
    import sys
    from pathlib import Path

    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Oldalt", "Mage", "Fire", None, False)
    mm = reg.members[m.discord_id]
    mm.main.rank = "alt"  # an old file: the main reads alt while the alt held the player's standing
    next(c for c in mm.characters if c.label == "Oldalt").rank = "core"
    reg.save(mm, "t: old ranks")
    assert reg.migrate_ranks(dry_run=True) and reg.members[m.discord_id].main.rank == "alt"  # a dry run changes nothing
    lines = reg.migrate_ranks()
    assert lines == [f"{m.display_name}: rank core on {m.main.label}; Oldalt → alt"]
    assert reg.members[m.discord_id].rank == "core" and not reg.migrate_ranks(), "idempotent"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import migrate_ranks

    assert migrate_ranks.migrate(data_root, dry_run=True) == {"demo": []}


def test_a_puppets_absence_posts_no_public_line(reg, rs, bot):
    from test_parity_verbs import Chan

    ch = bot.channels[55] = Chan(55, "away")
    reg.config.absences_channel_id = 55
    p, m = reg.test_members()[0], real(reg, 0)
    day = (reg.now_local().date() + timedelta(days=9)).isoformat()
    run(bot.record_absence(reg, p.discord_id, day, None, None, "t", by_id=OWNER))
    assert not ch.sent, "a rehearsal stays private"
    run(bot.record_absence(reg, m.discord_id, day, None, "private", m.display_name, by_id=m.discord_id))
    assert len(ch.sent) == 1 and m.display_name in ch.sent[0]["content"] and "private" not in ch.sent[0]["content"]


# ---------------------------------------------------------------- one verb, one lock (review fixes)

def _blocks_on_the_config_lock(reg, fn) -> None:
    """`fn` (a registry people write) waits while another thread holds Registry.config_lock — the web's worker threads
    and the Discord loop never interleave a check with someone else's write (design §2.4)."""
    import threading

    reg.config_lock.acquire()
    t = threading.Thread(target=fn, daemon=True)
    try:
        t.start()
        t.join(0.4)
        assert t.is_alive(), "the write went ahead while another writer held the lock"
    finally:
        reg.config_lock.release()
    t.join(10)
    assert not t.is_alive()


def test_people_writes_hold_the_config_lock(reg):
    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Lockalt", "Mage", "Fire", None, False)
    _blocks_on_the_config_lock(reg, lambda: reg.add_absence(m.discord_id, "2099-05-01", None, None, "t"))
    assert reg.members[m.discord_id].absences
    _blocks_on_the_config_lock(reg, lambda: reg.clear_absence(m.discord_id, "2099-05-01", "t"))
    assert not reg.members[m.discord_id].absences
    _blocks_on_the_config_lock(reg, lambda: reg.set_dm(m.discord_id, False, "t"))
    _blocks_on_the_config_lock(reg, lambda: reg.set_rank(m.discord_id, "core", "Officer", by_id=OWNER))
    _blocks_on_the_config_lock(reg, lambda: reg.confirm(m.discord_id, "Lockalt", "Officer", by_id=OWNER))
    mm = reg.members[m.discord_id]
    assert mm.dm_opt_out and mm.rank == "core" and next(c for c in mm.characters if c.label == "Lockalt").confirmed_by == "Officer"


def test_two_overlapping_absences_at_once_one_wins(reg):
    """The overlap check and the write are one step under the lock: two writers racing for the same days (a web thread
    and the loop) can't both pass the check."""
    import threading

    m = real(reg, 0)
    errors: list[Exception] = []
    gate = threading.Barrier(2)

    def add(by):
        gate.wait()
        try:
            reg.add_absence(m.discord_id, "2099-06-01", "2099-06-03", None, by)
        except RegistryError as e:
            errors.append(e)
    ts = [threading.Thread(target=add, args=(b,)) for b in ("web", "discord")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len(reg.members[m.discord_id].absences) == 1 and len(errors) == 1 and "already away" in str(errors[0])


def test_the_ripple_waits_for_a_board_writer(reg, rs, bot):
    """rc.after_character_change rewrites run files on the loop; a lock_run / board write holding board_lock across an
    await (the solver in a worker thread) would otherwise overwrite it, or see it land half-way. The ripple waits."""
    m = real(reg, 0, "Priest", "Holy")
    ev = real_run(reg, rs)
    join(reg, rs, ev, [m])

    async def go():
        lock = bot.board_lock(reg, ev.key)
        await lock.acquire()
        task = asyncio.create_task(bot.save_characters(reg, m.discord_id, [{"label": m.main.label, "cls": "Priest", "spec": "Shadow"}], m.display_name, by_id=m.discord_id))
        for _ in range(200):  # the registry write (a worker thread) lands; the run-file rewrite must not
            await asyncio.sleep(0.02)
            if reg.members[m.discord_id].main.spec == "Shadow":
                break
        await asyncio.sleep(0.1)
        assert reg.members[m.discord_id].main.spec == "Shadow"
        assert ev.signups[str(m.discord_id)].spec == "Holy", "the ripple wrote the run while a board writer held it"
        lock.release()
        await asyncio.wait_for(task, 10)
    run(go())
    assert ev.signups[str(m.discord_id)].spec == "Shadow"


def test_plain_text_rank_and_confirm_go_through_the_character_verb(reg, rs, bot, monkeypatch):
    calls = []
    real_verb = discord_people.save_characters

    async def spy(*a, **kw):
        calls.append((a[3], kw.get("officer")))
        return await real_verb(*a, **kw)
    monkeypatch.setattr(discord_people, "save_characters", spy)
    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Confirmee", "Mage", "Fire", None, False)
    run(configops.apply_async(reg, ConfigOp(op="rank", member=m.display_name, value="core"), "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    run(configops.apply_async(reg, ConfigOp(op="confirm", member=m.display_name, character="Confirmee"), "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    assert calls == [([{"label": m.main.label, "rank": "core"}], True), ([{"label": "Confirmee", "confirm": True}], True)]
    mm = reg.members[m.discord_id]
    assert mm.rank == "core" and next(c for c in mm.characters if c.label == "Confirmee").confirmed_by == "Officer"
    audit = [t for _lvl, t in bot.ops.lines if "rank core" in t or "confirmed Confirmee" in t]
    assert len(audit) == 2, f"one audit line per change (the verb's), not two: {bot.ops.lines}"


def test_a_shared_planned_label_never_ranks_the_wrong_member(reg):
    """Planned labels ('Priest (Holy)') are shared: without a member, a rank / confirm naming one is refused when more
    than one member plans it (Registry.find returned whoever it met first)."""
    a = reg.set_plan(REAL + 1, "Plannera", "Priest", "Holy", None, "main")[0]
    b = reg.set_plan(REAL + 2, "Plannerb", "Priest", "Holy", None, "main")[0]
    label = a.main.label
    assert label == b.main.label
    with pytest.raises(RegistryError, match="2 members plan") as e:
        configops.apply(reg, ConfigOp(op="rank", character=label, value="core"), "Officer", is_owner=True, by_id=OWNER)
    assert "Plannera" in str(e.value) and "Plannerb" in str(e.value)
    assert "2 members plan" in configops.describe(reg, ConfigOp(op="confirm", character=label))
    assert reg.members[a.discord_id].rank != "core" and reg.members[b.discord_id].rank != "core"
    configops.apply(reg, ConfigOp(op="rank", member="Plannerb", character=label, value="core"), "Officer", is_owner=True, by_id=OWNER)
    assert reg.members[b.discord_id].rank == "core" and reg.members[a.discord_id].rank != "core"
    assert reg.find_unique("Anyone (Nothing)") is None


def test_plain_text_diff_prints_the_ripple_for_every_character_field(reg, rs):
    """The words of the Discord decide screen and the web preview, in the plain-text diff too: spec, offspec, name and
    main (not only retire / delete)."""
    m = real(reg, 0, "Priest", "Holy")
    reg.add_character(m.discord_id, m.display_name, "Ripplealt", "Mage", "Fire", None, False)
    ev = real_run(reg, rs)
    join(reg, rs, ev, [m])
    spec = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=m.main.label, field="spec", value="Shadow"))
    assert "now reads Shadow" in spec
    off = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=m.main.label, field="offspec", value="Shadow"))
    assert "offspec" in off
    main = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character="Ripplealt", field="main"))
    assert "moves to Ripplealt" in main
    p = reg.set_plan(REAL + 5, "Planname", "Priest", "Holy", None, "main")[0]
    rc.set_signup(reg, rs, ev, p, p.main.label, "in", source="member")
    name = configops.describe(reg, ConfigOp(op="character", member="Planname", character=p.main.label, field="name", value="Newname"))
    assert "now names Newname" in name
    retire = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=m.main.label, field="retire"))
    assert "answer is cleared" in retire or "stays No thanks" in retire


# ---------------------------------------------------------------- review fixes (the C fixer)

def test_plain_text_set_main_goes_through_the_character_verb(reg, rs, bot, monkeypatch):
    """set_main used to call Registry.officer_set_main straight: no config_lock, no puppet refusal, no audit line from the
    verb. Now it is a {label, main: true} row through discord_people.save_characters, like rank and confirm."""
    m = real(reg, 0)
    reg.set_rank(m.discord_id, "core", "Officer", by_id=OWNER)
    reg.add_character(m.discord_id, m.display_name, "Newmain", "Mage", "Fire", None, False)
    calls = []
    verb = discord_people.save_characters

    async def spy(*a, **k):
        calls.append(a[3])  # (bot, reg, uid, rows, by)
        return await verb(*a, **k)
    monkeypatch.setattr(discord_people, "save_characters", spy)
    op = ConfigOp(op="set_main", member=m.display_name, character="Newmain")
    line = run(configops.apply_async(reg, op, "Officer", is_owner=False, bot=bot, by_id=OWNER, via="chat"))
    assert calls == [[{"label": "Newmain", "main": True}]]
    mm = reg.members[m.discord_id]
    assert mm.main.label == "Newmain" and mm.rank == "core" and "main → Newmain" in line  # D25: the rank moved with the main
    assert len([t for _lvl, t in bot.ops.lines if "main is Newmain" in t]) == 1  # one audit line, the verb's
    with pytest.raises(RegistryError, match="already"):
        run(configops.apply_async(reg, op, "Officer", is_owner=False, bot=bot, by_id=OWNER, via="chat"))


def test_set_main_and_absences_refuse_another_officers_puppet_offline_too(reg, rs):
    """The offline (no-bot) branches of configops.apply pass by_id: set_main, absence and absence_clear refuse a puppet of
    a rehearsal someone else runs, as the bot's paths do."""
    from conftest import open_test_run

    tester, other = 777_000_000_000_001, 777_000_000_000_002
    p = reg.test_members()[0]
    reg.add_character(p.discord_id, p.display_name, "Puppetalt", "Mage", "Fire", None, False)
    day = (reg.now_local().date() + timedelta(days=2)).isoformat()
    reg.add_absence(p.discord_id, day, None, None, "t")
    open_test_run(reg, rs, test_by=tester)
    later = (reg.now_local().date() + timedelta(days=9)).isoformat()
    for op in (ConfigOp(op="set_main", member=p.display_name, character="Puppetalt"),
               ConfigOp(op="absence", member=p.display_name, start=later),
               ConfigOp(op="absence_clear", member=p.display_name, start=day)):
        with pytest.raises(RegistryError, match="test puppet"):
            configops.apply(reg, op, "Other", is_owner=False, by_id=other)
    pp = reg.members[p.discord_id]
    assert pp.main.label != "Puppetalt" and [a.start for a in pp.absences] == [day]
    configops.apply(reg, ConfigOp(op="set_main", member=p.display_name, character="Puppetalt"), "Tester", is_owner=False, by_id=tester)
    configops.apply(reg, ConfigOp(op="absence_clear", member=p.display_name, start=day), "Tester", is_owner=False, by_id=tester)
    pp = reg.members[p.discord_id]
    assert pp.main.label == "Puppetalt" and not pp.absences


def test_a_host_superuser_transfers_a_guild_they_host_but_do_not_own(reg, rs, bot, monkeypatch):
    """Registry.is_owner_id: a host superuser (OIBOT_SUPERUSERS) has owner rights in every guild, so they may hand one
    over; the confirm sentence says they act as the host (they had no owner rights of their own to lose)."""
    from oibot_gm import guild_routing

    su = 424_242_424_242_424_242
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(su))
    m = real(reg, 0)
    assert reg.config.owner_discord_id == OWNER and reg.is_owner_id(su)
    words = reg.owner_consequence(m.display_name, su)
    assert "you act as the host here" in words and "You lose owner rights" not in words

    async def member_of(_bot, _reg, uid, timeout=None, max_age=None):
        return "yes", type("M", (), {"display_name": m.display_name, "bot": False})()
    monkeypatch.setattr(guild_routing, "member_of", member_of)
    line = run(bot.transfer_owner(reg, m.discord_id, "Host", by_id=su))
    assert m.display_name in line and reg.config.owner_discord_id == m.discord_id
    assert any(level == "warn" and "owner →" in text and "(host)" in text for level, text in bot.ops.lines)
    assert not bot.sent_dms and not bot.dms


def test_the_rank_line_names_the_member_once(reg):
    m = real(reg, 0)
    lines = reg.save_character_table(m.discord_id, [{"label": m.main.label, "rank": "core"}], "Officer", officer=True, by_id=OWNER)
    assert lines == ["rank core"]


def test_save_characters_reads_the_changes_under_the_same_lock_as_the_save(reg, rs, bot, monkeypatch):
    """The ripple's changes come from the record the save applies to: rc.character_changes runs holding config_lock,
    in the same critical section as save_character_table (it used to run on the loop before the locked save)."""
    m = real(reg, 0, "Priest", "Holy")
    held = []
    changes = rc.character_changes

    def spy(*a, **k):
        held.append(reg.config_lock._is_owned())
        return changes(*a, **k)
    monkeypatch.setattr(rc, "character_changes", spy)
    run(bot.save_characters(reg, m.discord_id, [{"label": m.main.label, "cls": "Priest", "spec": "Shadow"}], m.display_name, by_id=m.discord_id))
    assert held == [True] and reg.members[m.discord_id].main.spec == "Shadow"


@pytest.mark.parametrize("verb", ["add_character", "set_plan", "name_character", "set_flex", "set_main", "regular_add"])
def test_member_writes_hold_the_config_lock(reg, monkeypatch, verb):
    """Registration, plans and the other member writes take config_lock like the character table (they run on the loop
    while a web save may hold it in a worker thread)."""
    reg.set_raid_overrides("barrow_deeps", {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    m = real(reg, 0)
    reg.add_character(m.discord_id, m.display_name, "Lockalt", "Mage", "Fire", None, False)
    reg.set_plan(REAL + 7, "Planner", "Priest", "Holy", None, "main")
    held = []
    save = reg.save

    def spy(member, message):
        held.append(reg.config_lock._is_owned())
        return save(member, message)
    monkeypatch.setattr(reg, "save", spy)
    {"add_character": lambda: reg.add_character(REAL + 8, "Newbie", "Freshone", "Mage", "Fire", None, True),
     "set_plan": lambda: reg.set_plan(REAL + 9, "Plantwo", "Priest", "Holy", None, "main"),
     "name_character": lambda: reg.name_character(REAL + 7, "Namedone"),
     "set_flex": lambda: reg.set_flex(m.discord_id, "Lockalt", ["tank"]),
     "set_main": lambda: reg.set_main(m.discord_id, "Lockalt"),
     "regular_add": lambda: reg.regular_add(m.discord_id, "barrow_deeps:default", "t")}[verb]()
    assert held and all(held)


# ---------------------------------------------------------------- final fixer: a PLANNED character respecced

def _planned(reg, n: int, cls="Priest", spec="Holy"):
    uid = REAL + 40 + n
    m, c = reg.set_plan(uid, f"Planner{n}", cls, spec, None, "main")
    assert not c.name and c.label == f"{cls} ({spec})"
    return reg.members[uid]


def test_respec_of_a_planned_main_rewrites_the_open_sheet_answer(reg, rs, bot):
    """Regression: a planned character's label is 'Class (Spec)', so a respec renames it; the ripple looked the character
    up by the OLD label after the save and skipped the rewrite while saying 'the answer now reads Shadow'."""
    m = _planned(reg, 0)
    ev = real_run(reg, rs)
    join(reg, rs, ev, [m])
    res = run(bot.save_characters(reg, m.discord_id, [{"label": "Priest (Holy)", "cls": "Priest", "spec": "Shadow"}], m.display_name, by_id=m.discord_id))
    assert any("now reads Shadow" in line for line in res["ripple"])
    sg = ev.signups[str(m.discord_id)]
    assert (sg.character, sg.spec, sg.role) == ("Priest (Shadow)", "Shadow", "ranged"), "the sheet says what the ripple said"


def test_same_role_respec_of_a_planned_main_follows_the_locked_seat(reg, rs, bot):
    m = _planned(reg, 1)
    others = [real(reg, i, "Mage", "Fire") for i in range(1, 6)]
    ev = real_run(reg, rs)
    join(reg, rs, ev, [m] + others)
    lock_with_board(reg, rs, ev, [m.display_name] + [o.display_name for o in others])
    run(bot.save_characters(reg, m.discord_id, [{"label": "Priest (Holy)", "cls": "Priest", "spec": "Discipline"}], m.display_name, by_id=m.discord_id))
    seat = ev.seat_of(m)
    assert seat is not None and (seat[1].spec, seat[1].role) == ("Discipline", "healer")
    assert seat[1].character == "Priest (Discipline)" and ev.signups[str(m.discord_id)].character == "Priest (Discipline)"
