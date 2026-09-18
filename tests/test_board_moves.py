"""Co-editing: single moves apply to the current board; whole-board writes are tied to a revision."""
import pytest

from oibot_gm import raidcycle as rc
from conftest import join, open_test_run


@pytest.fixture
def run(reg, rs):
    ev = open_test_run(reg, rs)
    members = reg.test_members()[:12]
    join(reg, rs, ev, members)
    return ev, [m.display_name for m in members]


def test_move_from_bank_then_between_groups(reg, rs, run):
    ev, n = run
    ev.layout = rc.apply_move(reg, ev, n[0], (0, 0))
    ev.layout = rc.apply_move(reg, ev, n[1], (0, 5))
    assert rc.board_layout(reg, ev)[0] == [n[0], n[1]]
    ev.layout = rc.apply_move(reg, ev, n[0], (1, 0))
    lay = rc.board_layout(reg, ev)
    assert lay[0] == [n[1]] and lay[1] == [n[0]]
    ev.layout = rc.apply_move(reg, ev, n[0], None)  # back to the bank
    assert n[0] not in [x for g in rc.board_layout(reg, ev) for x in g]


def test_two_officers_merge_instead_of_overwriting(reg, rs, run):
    """Officer A and B both looked at an empty board; each drags someone. Both placements survive."""
    ev, n = run
    ev.layout = rc.apply_move(reg, ev, n[0], (0, 0))  # A
    ev.layout = rc.apply_move(reg, ev, n[1], (1, 0))  # B, computed against the board A just wrote
    lay = rc.board_layout(reg, ev)
    assert lay[0] == [n[0]] and lay[1] == [n[1]]


def test_drop_on_someone_swaps(reg, rs, run):
    ev, n = run
    ev.layout = [[n[0], n[1]], [n[2]]]
    ev.layout = rc.apply_move(reg, ev, n[2], (0, 0))
    lay = rc.board_layout(reg, ev)
    assert lay[0] == [n[2], n[1]] and lay[1] == [n[0]]


def test_full_group_and_unknown_name_refused(reg, rs, run):
    ev, n = run
    size = int(reg.profile.comp_rules["group_size"])
    ev.layout = [n[:size]]
    with pytest.raises(ValueError, match="full"):
        rc.apply_move(reg, ev, n[size], (0, size))
    with pytest.raises(ValueError, match="joined"):
        rc.apply_move(reg, ev, "Nobody", (0, 0))


def test_reorder_within_a_group(reg, rs, run):
    ev, n = run
    ev.layout = [[n[0], n[1], n[2]]]
    ev.layout = rc.apply_move(reg, ev, n[0], (0, 3))
    assert rc.board_layout(reg, ev)[0] == [n[1], n[2], n[0]]


def test_rev_changes_with_the_board_and_pins_only(reg, rs, run):
    ev, n = run
    r0 = rc.board_rev(reg, ev)
    join(reg, rs, ev, reg.test_members()[12:13])  # a new Join is not a board change
    assert rc.board_rev(reg, ev) == r0
    ev.layout = rc.apply_move(reg, ev, n[0], (0, 0))
    r1 = rc.board_rev(reg, ev)
    assert r1 != r0
    rc.set_pin(ev, n[1], "in")
    assert rc.board_rev(reg, ev) != r1
