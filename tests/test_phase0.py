"""Defects the wizard audit found underneath the typed commands, fixed before any wizard exists."""
import ast
import importlib
import subprocess
from pathlib import Path

import pytest

from oibot_gm.registry import RegistryError

SRC = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


# ---- several raid fields are one change

def test_raid_fields_saved_together_are_one_commit(reg, store):
    n = commits(store)
    reg.set_raid_overrides("barrow_deeps", {"signup_lead_hours": 96, "lock_hours_before": 12, "confirm_hours_before": 4}, "t")
    rd = reg.raid_def("barrow_deeps")
    assert (rd["signup_lead_hours"], rd["lock_hours_before"], rd["confirm_hours_before"]) == (96, 12, 4)
    assert commits(store) == n + 1


def test_a_refused_field_leaves_the_raid_and_git_untouched(reg, store):
    before, n = dict(reg.config.raids.get("barrow_deeps") or {}), commits(store)
    with pytest.raises(RegistryError, match="lock must come before"):
        # lock 2 h but confirm 6 h: the third field is refused, and the first two must not have committed on their own
        reg.set_raid_overrides("barrow_deeps", {"signup_lead_hours": 50, "lock_hours_before": 2, "confirm_hours_before": 6}, "t")
    assert dict(reg.config.raids.get("barrow_deeps") or {}) == before
    assert commits(store) == n


def test_a_change_only_valid_as_a_whole_is_accepted(reg):
    """Raising the nudge past the CURRENT lead is refused field by field, but it is fine with the new lead beside it."""
    lead = reg.raid_def("barrow_deeps")["signup_lead_hours"]
    reg.set_raid_overrides("barrow_deeps", {"nudge_hours_before": lead + 40, "signup_lead_hours": lead + 60}, "t")
    assert reg.raid_def("barrow_deeps")["nudge_hours_before"] == lead + 40
    reg.set_raid_overrides("barrow_deeps", {"tank_max": 5, "tank_min": 4}, "t")  # min above the OLD max, fine with the new one
    assert reg.raid_def("barrow_deeps")["comp"]["tank"] == {"min": 4, "max": 5} or reg.raid_def("barrow_deeps")["comp"]["tank"]["min"] == 4


def test_single_field_path_still_checks_the_whole(reg):
    with pytest.raises(RegistryError):
        reg.set_raid_override("barrow_deeps", "confirm_hours_before", 999, "t")


# ---- removing from a roster that doesn't exist, or that they're not on, is an error, not a green tick

def test_roster_remove_refuses_unknown_roster_and_non_members(reg):
    m = reg.test_members()[0]
    with pytest.raises(RegistryError, match="No roster called"):
        reg.roster_remove(m.discord_id, "no-such-roster", "t")
    key = reg.config.roster_keys()[0] if reg.config.roster_keys() else None
    if key:
        with pytest.raises(RegistryError, match="isn't on"):
            reg.roster_remove(m.discord_id, key, "t")


# ---- an absence is the same day however it is written

def test_clear_absence_matches_the_day_not_the_string(reg):
    m = reg.test_members()[0]
    reg.add_absence(m.discord_id, "2030-12-07", "2030-12-09", None, "t")
    a = reg.clear_absence(m.discord_id, "2030-12-7", "t")
    assert a.start == "2030-12-07"


# ---- people read times and days, never the stored forms

def test_slot_and_day_labels():
    from oibot_gm.registry import Registry
    assert Registry.slot_label(None, "Tue 19:30") == "Tue 7:30 PM"
    assert Registry.slot_label(None, "Sat 00:05") == "Sat 12:05 AM"
    assert Registry.slot_label(None, "Sun 12:00") == "Sun 12:00 PM"
    assert Registry.day_label(None, "2026-12-24") == "Thu 24 Dec"


def test_no_raw_slot_reaches_a_person(reg):
    from oibot_gm import discord_help

    reg.set_raid_override("barrow_deeps", "slots", "Tue 19:30", "t")
    for key in ("about", "schedule"):
        text = discord_help.guide_text(reg, key)
        assert "19:30" not in text, (key, text)
    assert "7:30 PM" in discord_help.guide_text(reg, "schedule")


# ---- /help topic: a picker, and a long section is sent whole

def test_help_topics_are_a_picker_and_sections_arrive_whole():
    from oibot_gm import discord_help

    assert len(discord_help.TOPIC_CHOICES) <= 25
    for key, (marker, _label) in discord_help.TOPICS.items():
        text = discord_help.manual_section(marker)
        assert text, f"topic {key} points at a missing manual section {marker}"
        parts = discord_help.chunk_text(text)
        assert all(len(p) <= 1900 for p in parts)
        assert "\n\n".join(parts).replace("\n", "") == text.replace("\n", "")  # nothing dropped, nothing cut mid-word


# ---- the class of bug that shipped /roster overview broken: an import inside a handler that no longer resolves

def _local_imports():
    """Every `from .x import y` in the package, including the ones inside function bodies (they only run when a
    command is used, so an import-time test never sees them)."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        pkg = "oibot_gm" + ("." + ".".join(path.relative_to(SRC).parent.parts) if path.relative_to(SRC).parent.parts else "")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else pkg
                mod = f"{base}.{node.module}" if node.module else base
                for alias in node.names:
                    yield path.name, node.lineno, mod, alias.name


@pytest.mark.parametrize("where,line,mod,name", list(_local_imports()))
def test_every_relative_import_resolves(where, line, mod, name):
    if name == "*":
        return
    try:
        m = importlib.import_module(mod)
    except ModuleNotFoundError:
        m = importlib.import_module(mod.rsplit(".", 1)[0])
        name = mod.rsplit(".", 1)[1]
    assert hasattr(m, name) or importlib.util.find_spec(f"{mod}.{name}") is not None, f"{where}:{line} imports {name} from {mod}, which has no such name"
