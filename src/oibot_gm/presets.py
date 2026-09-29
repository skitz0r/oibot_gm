"""Named presets every surface shares: the raid cadence presets (the /gm config raid wizard's picker, plain text's
`raid_set field=cadence`) and the test-bench tempos (/gm test run, plain text's `test run <raid> quick|standard|slow`).
One table each, so the wizard, chat and the web can never drift apart."""
from __future__ import annotations

# (key, name, opens, nudge, lock, confirm) in hours before the run — each legal by construction:
# confirm ≤ lock ≤ nudge ≤ opens
PRESETS: list[tuple[str, str, int, int, int, int]] = [
    ("standard", "Standard", 120, 72, 24, 6),
    ("short", "Short notice", 48, 36, 24, 12),
    ("same_week", "Same week", 72, 48, 24, 6),
]
CADENCE_FIELDS = ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before")

# name → (label, start_in, nudge_in, lock_in, confirm_in), all minutes; each row holds start > nudge > lock > confirm ≥ 0
TEMPOS: dict[str, tuple[str, int, int, int, int]] = {
    "quick": ("Quick", 20, 16, 12, 8),
    "standard": ("Standard", 40, 32, 25, 15),
    "slow": ("Slow", 90, 70, 55, 35),
}
DEFAULT_TEMPO = "standard"


def preset_fields(key: str) -> dict[str, str]:
    """A cadence preset as the raid fields it sets ({signup_lead_hours: '120', …}), ready for
    Registry.set_raid_overrides / set_schedule. Unknown key → ValueError naming the options."""
    row = next((p for p in PRESETS if p[0] == key), None)
    if row is None:
        raise ValueError(f"cadence is one of {', '.join(p[0] for p in PRESETS)}")
    return {f: str(h) for f, h in zip(CADENCE_FIELDS, row[2:])}


def tempo_cutoffs(key: str, *, open_dm: bool = False, test_by: int | None = None) -> tuple[int, dict]:
    """A test tempo as (start_in minutes, the cutoffs dict rc.open_run takes for a test run)."""
    if key not in TEMPOS:
        raise ValueError(f"tempo is one of {', '.join(TEMPOS)}")
    _, start_in, nudge_in, lock_in, confirm_in = TEMPOS[key]
    return start_in, {"soft": nudge_in / 60, "hard": lock_in / 60, "confirm": confirm_in / 60, "open_dm": bool(open_dm), "test_by": test_by}
