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
