"""Registry data as select options — the one place a wizard's choices come from.

Option VALUES are machine keys (a raid id, a run's roster key, a character's name) that a person never reads; LABELS
are what they read. A set that can outgrow 25 options is never silently clipped: callers narrow first (Discord's own
member picker, then a member's characters, which is always a handful)."""
from __future__ import annotations

from collections.abc import Callable

from .registry import RANKS, Member, Registry
from .wizard import MAX_OPTIONS, Opt

Ico = Callable[[str, str], str]


def raids(reg: Registry, *, current: str | None = None) -> list[Opt]:
    """Every raid in the game profile: name, size and its run times (12-hour)."""
    out = []
    for rid in reg.profile.raids:
        rd = reg.raid_def(rid)
        slots = ", ".join(reg.slot_label(s) for s in rd.get("slots") or []) or "no run times yet"
        out.append(Opt(rid, rd.get("name", rid), f"{rd.get('size', '?')}-player · {slots}", default=rid == current))
    return out


def live_runs(reg: Registry, rs, *, states: tuple[str, ...] = ("open", "locked")) -> list[Opt]:
    """Live runs, soonest first; the value is the run's roster key (what current_event / rs.for_team resolve)."""
    from .raid_views import raid_name, sheet_state

    out = []
    for ev in rs.live():
        st = sheet_state(ev)
        if st not in states:
            continue
        joined = len(ev.seated()) if st == "locked" else len(ev.by_status("in"))
        out.append(Opt(ev.team, f"{raid_name(reg, ev)} · {reg.local12(ev.start)}", f"{st} · {joined} {'rostered' if st == 'locked' else 'joined'}"))
    return out[:MAX_OPTIONS]


def characters_of(reg: Registry, m: Member | None, ico: Ico | None = None, *, current: str | None = None) -> list[Opt]:
    """A member's active characters (planned ones by label), main first. Always a handful — no ceiling to hit."""
    if not m:
        return []
    chars = sorted(m.active(), key=lambda c: (not c.is_main, c.label.lower()))
    return [Opt(c.name or c.label, c.label, f"{c.cls} {c.spec}" + (" · main" if c.is_main else "") + ("" if c.confirmed_by or c.status == "planned" else " · unconfirmed"),
                emoji=ico("class", c.cls) if ico else None, default=(c.name or c.label) == current) for c in chars][:MAX_OPTIONS]


def classes(reg: Registry, ico: Ico | None = None, *, current: str | None = None) -> list[Opt]:
    """The game version's classes, from the profile (never a hardcoded list)."""
    return [Opt(c, c, emoji=ico("class", c) if ico else None, default=c == current) for c in reg.profile.classes][:MAX_OPTIONS]


def specs_of(reg: Registry, cls: str, ico: Ico | None = None, *, current: str | None = None, none_label: str | None = None) -> list[Opt]:
    """A class's specs with the role each plays; `none_label` adds a leading 'none' choice (an optional offspec)."""
    out = [Opt("-", none_label, default=current in (None, "", "-"))] if none_label else []
    for s in reg.profile.classes.get(cls, {}):
        out.append(Opt(s, s, reg.profile.spec(cls, s).role, emoji=ico("spec", f"{cls}:{s}") if ico else None, default=s == current))
    return out[:MAX_OPTIONS]


def ranks(*, current: str | None = None) -> list[Opt]:
    return [Opt(r, r, default=r == current) for r in RANKS]


def rosters(reg: Registry, *, current: str | None = None) -> list[Opt]:
    return [Opt(k, (reg.config.roster(k) or {}).get("name") or k, default=k == current) for k in reg.config.roster_keys()][:MAX_OPTIONS]
