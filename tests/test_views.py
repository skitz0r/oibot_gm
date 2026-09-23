"""Discord card buttons keep one order in every state (issue #3); fill DMs use the confirmation DM's label pair (#7)."""
from conftest import join, lock_with_board, open_test_run

from oibot_gm import raid_views as views
from oibot_gm.raid_buttons import FillButton, PlaceButton, SignupButton

ico = lambda kind, key: ""  # noqa: E731


def row_of(view):
    """(label, disabled) for the buttons of the last action row in a layout."""
    rows = [c for c in view.walk_children() if c.__class__.__name__ == "ActionRow"]
    return [(getattr(b, "label", None) or b.item.label, getattr(b, "disabled", None) if hasattr(b, "disabled") else b.item.disabled) for b in rows[-1].children]


def test_officer_card_buttons_never_move(reg, rs):
    ev = open_test_run(reg, rs)
    members = reg.test_members()[:10]
    join(reg, rs, ev, members)
    from oibot_gm import raidcycle as rc
    team = rc.run_team(reg, ev)
    open_row = row_of(views.health_layout(reg, rs, ev, team, ico))
    lock_with_board(reg, rs, ev, [m.display_name for m in members])
    locked_row = row_of(views.lock_layout(reg, ev, team, ico, 0))
    strip = lambda row: [(l, d) for l, d in row if l != "Open the board"]  # the link needs OIBOT_WEB_URL  # noqa: E731
    assert [l for l, _ in strip(open_row)] == ["Lock now", "Fill seats", "Cancel run"]
    assert [l for l, _ in strip(locked_row)] == ["Locked", "Fill seats", "Cancel run"]
    assert dict(strip(open_row)) == {"Lock now": False, "Fill seats": True, "Cancel run": False}
    assert dict(strip(locked_row)) == {"Locked": True, "Fill seats": False, "Cancel run": False}


def test_yes_no_dms_share_one_label_pair_and_cant_is_grey():
    import discord
    assert [FillButton("k", 1, a).item.label for a in ("yes", "no")] == [PlaceButton("k", 1, a).item.label for a in ("yes", "no")] == ["Confirm", "Can't make it"]
    assert SignupButton("k", "cant").item.style == discord.ButtonStyle.secondary


def test_sheet_is_an_embed_with_class_then_group_columns(reg, rs):
    """Raid-Helper-style arrangement: inline fields are columns — a class per column while open, a group per column
    once locked — one member per line; icons carry no labels; buttons match the state."""
    from oibot_gm import raidcycle as rc
    ev = open_test_run(reg, rs)
    members = reg.test_members()[:12]
    join(reg, rs, ev, members)
    join(reg, rs, ev, reg.test_members()[12:14], status="sub")
    team = rc.run_team(reg, ev)
    tag = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731
    e, view = views.sheet_message(reg, ev, team, tag)
    cols = [f for f in e.fields if f.inline]
    classes = {m.main.cls for m in members}
    assert len(cols) == len(classes) and all(f.name.startswith("<class:") and f.name.endswith(")") for f in cols)
    assert sum(len(f.value.split("\n")) for f in cols) == 12 and all(ln.startswith("<spec:") for f in cols for ln in f.value.split("\n"))
    assert [f.name for f in e.fields if not f.inline] == ["Bench (2)"]
    assert [b.item.label for b in view.children] == ["Join", "Bench", "No thanks"] and "<role:tank> " in e.description
    lock_with_board(reg, rs, ev, [m.display_name for m in members[:10]])
    e, view = views.sheet_message(reg, ev, team, tag)
    groups = [f for f in e.fields if f.inline]
    assert [f.name for f in groups] == ["Group 1", "Group 2"] and all(len(f.value.split("\n")) == 5 for f in groups)
    assert [b.item.label for b in view.children] == ["Can't make it"] and "Not rostered (2)" in [f.name for f in e.fields]
    ev.state = "cancelled"
    e, view = views.sheet_message(reg, ev, team, tag)
    assert e.title.startswith("🧪 Cancelled") and view is None and not e.fields
    assert len(e) < 6000


def test_away_row_holds_registered_absences_only(reg, rs, monkeypatch):
    """Away = a registered absence covers the run's day; No thanks = people who chose it. Someone away who joins anyway
    is not Away; the row names 15 then +N."""
    from oibot_gm import raidcycle as rc
    ev = open_test_run(reg, rs)
    day = ev.start.astimezone(reg.tz).date().isoformat()
    ms = reg.test_members()
    monkeypatch.setattr(views, "AWAY_SHOWN", 10)  # a short cap so the fixture's 16 members exercise the "+N" tail
    away, chose, joined_anyway = ms[:12], ms[12:14], ms[14]
    for m in away + [joined_anyway]:
        reg.add_absence(m.discord_id, day, day, "trip", "t")
    join(reg, rs, ev, away + chose, status="out")
    join(reg, rs, ev, [joined_anyway])
    e, _ = views.sheet_message(reg, ev, rc.run_team(reg, ev), ico)
    rows = {f.name: f.value for f in e.fields if not f.inline}
    assert rows["No thanks (2)"].split(" · ") == [m.display_name for m in chose]
    assert rows["Away (12)"].endswith(" · +2") and len(rows["Away (12)"].split(" · ")) == 11
    assert joined_anyway.display_name not in rows["Away (12)"] and "✈" not in str(rows)
