"""Buff coverage matrix: for every player in every group, which party buffs
they would benefit from and whether a provider is in their group.

Slot-aware: buffs that share a `slot` (shaman totems by element) are mutually
exclusive per provider, so a group with one shaman gets the one air totem that
helps its members most, not every air totem at once."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Player, RosterResult
from ..profiles import Buff, GameProfile, SpecInfo


class Cell(BaseModel):
    buff: str
    value: float  # benefit to this player (0 = not applicable)
    present: bool
    providers: list[str]  # who in the group provides it


class PlayerCoverage(BaseModel):
    name: str
    cls: str
    spec: str
    role: str
    cells: list[Cell]
    covered: float
    missing: float

    @property
    def pct(self) -> float:
        tot = self.covered + self.missing
        return self.covered / tot if tot else 1.0


class GroupCoverage(BaseModel):
    index: int
    players: list[PlayerCoverage]
    missing_summary: list[str]  # "Windfury Totem (wanted by 3, +26)"
    picks: dict[str, str] = Field(default_factory=dict)  # slot -> buff id chosen for this group (totems)
    present: dict[str, list[str]] = Field(default_factory=dict)  # buff id -> providers in group
    wanted: dict[str, float] = Field(default_factory=dict)  # buff id -> total benefit to the group if present
    # buff id -> the buff of the same family that already covers this group (raid-wide or stronger)
    covered: dict[str, str] = Field(default_factory=dict)


class Coverage(BaseModel):
    buffs: list[str]  # column order (buff names)
    groups: list[GroupCoverage]
    unmet_raidwide: list[str]  # buffs nobody on the roster can provide


def group_buffs(profile: GameProfile,
                members: list[tuple[str, SpecInfo]]) -> tuple[dict[str, list[str]], dict[str, str], dict[str, float]]:
    """(present: buff id -> providers, picks: slot -> buff id, wanted: buff id -> group benefit) for one group.
    Per slot, each provider brings the one buff worth most to the group; unslotted buffs are simply present
    when someone provides them."""
    buffs = profile.party_buffs()
    wanted = {b.id: sum(b.benefit(s) for _, s in members) for b in buffs}
    present: dict[str, list[str]] = {}
    picks: dict[str, str] = {}
    slots: dict[str, list[Buff]] = {}
    for b in buffs:
        if b.slot:
            slots.setdefault(b.slot, []).append(b)
        else:
            provs = [n for n, s in members if b.provided_by(s)]
            if provs:
                present[b.id] = provs
    for slot, sbuffs in slots.items():
        # providers of anything in the slot; each fills one buff, best value first
        provs = [n for n, s in members if any(b.provided_by(s) for b in sbuffs)]
        if not provs:
            continue
        ranked = sorted(sbuffs, key=lambda b: -wanted[b.id])
        for b in ranked[: len(provs)]:
            who = [n for n, s in members if b.provided_by(s)]
            if who and wanted[b.id] > 0:
                present[b.id] = who
                picks.setdefault(slot, b.id)
    return present, picks, wanted


def compute(profile: GameProfile, players: list[Player], result: RosterResult) -> Coverage:
    """Family-aware: for each player and stacking family only the strongest present buff counts, and a raid-wide
    buff of the same family provided by anyone seated covers every group (a party buff it beats is `covered`,
    not missing)."""
    by_name = {p.signup_name: p for p in players}
    buffs = profile.party_buffs()
    seated_specs = [profile.spec(p.cls, p.spec) for p in result.selected]
    anyone = {b.id: any(b.provided_by(sp) for sp in seated_specs) for b in buffs}
    raid_present = [b for b in profile.raid_buffs() if any(b.provided_by(sp) for sp in seated_specs)]
    groups: list[GroupCoverage] = []
    for gi, names in enumerate(result.groups, 1):
        members = [by_name[n] for n in names]
        specs = {p.signup_name: profile.spec(p.cls, p.spec) for p in members}
        present, picks, wanted = group_buffs(
            profile, [(p.character or p.signup_name, specs[p.signup_name]) for p in members])
        pcs: list[PlayerCoverage] = []
        missing_tally: dict[str, tuple[int, float]] = {}
        covered: dict[str, str] = {}
        counted: set[str] = set()  # present buffs that actually gave someone something
        outranked: dict[str, str] = {}  # present buff -> the family-mate that beat it for everyone
        for p in members:
            sp = specs[p.signup_name]
            cells = []
            cov = miss = 0.0
            # the best thing each family already gives this player: present party buffs here, or raid-wide ones
            best: dict[str, tuple[float, str]] = {}
            for rb in raid_present:
                v = rb.benefit(sp)
                if v > best.get(rb.family_id, (0.0, ""))[0]:
                    best[rb.family_id] = (v, rb.id)
            for b in buffs:
                if b.id in present:
                    v = b.benefit(sp)
                    if v > best.get(b.family_id, (0.0, ""))[0]:
                        best[b.family_id] = (v, b.id)
            for b in buffs:
                provs = present.get(b.id, [])
                val = b.benefit(sp)
                slot_taken = b.slot and b.id not in picks.values() and any(
                    b2.id in present for b2 in buffs if b2.slot == b.slot)
                if slot_taken and val > 0:
                    val = 0.0  # the slot is taken by a better totem for this group; not "missing"
                top_val, top_id = best.get(b.family_id, (0.0, ""))
                if val > 0 and provs:
                    if top_id == b.id:
                        cov += val
                        counted.add(b.id)
                    else:
                        outranked[b.id] = top_id
                        val = 0.0  # present but outranked by another buff of its family
                elif val > 0 and top_val >= val and top_id:
                    cov += val  # not here, but its family is already covered for this player
                    covered.setdefault(b.id, top_id)
                    val = 0.0
                elif val > 0:
                    miss += val
                    n, v = missing_tally.get(b.name, (0, 0.0))
                    missing_tally[b.name] = (n + 1, v + val)
                cells.append(Cell(buff=b.name, value=val, present=bool(provs), providers=provs))
            pcs.append(PlayerCoverage(name=p.character or p.signup_name, cls=p.cls, spec=p.spec, role=p.role,
                                      cells=cells, covered=cov, missing=miss))
        for bid, top in outranked.items():
            if bid not in counted:  # nobody in the group got anything from it: a family-mate covers it
                present.pop(bid, None)
                covered.setdefault(bid, top)
        id_of: dict[str, str] = {}
        for b in buffs:
            id_of.setdefault(b.name, b.id)  # first buff of that name, as before
        summary = [
            f"{name} (wanted by {n}, +{v:.0f}){'' if anyone[id_of[name]] else ' — nobody on roster'}"
            for name, (n, v) in sorted(missing_tally.items(), key=lambda kv: -kv[1][1])
        ]
        groups.append(GroupCoverage(index=gi, players=pcs, missing_summary=summary, picks=picks, present=present,
                                    wanted=wanted, covered=covered))
    return Coverage(buffs=[b.name for b in buffs], groups=groups,
                    unmet_raidwide=[b.name for b in buffs if not anyone[b.id]])


GLYPH_PRESENT, GLYPH_MISSING, GLYPH_NA = "●", "○", "·"


def markdown(cov: Coverage) -> str:
    short = [_short(b) for b in cov.buffs]
    out = [
        "## Buff coverage", "",
        "● benefits and present · ○ benefits but missing · · not applicable", "",
        "| group | player | " + " | ".join(short) + " | cover |",
        "|---|---|" + "---|" * len(short) + "---|",
    ]
    for g in cov.groups:
        for pc in g.players:
            cells = " | ".join(
                GLYPH_PRESENT if c.present and c.value > 0 else GLYPH_MISSING if c.value > 0 else GLYPH_NA
                for c in pc.cells
            )
            out.append(f"| G{g.index} | {pc.name} ({pc.spec}) | {cells} | {pc.pct:.0%} |")
        if g.missing_summary:
            out.append(f"| G{g.index} | _missing_ | " + " | ".join([""] * len(short))
                       + f" | {'; '.join(g.missing_summary)} |")
    if cov.unmet_raidwide:
        out += ["", "Nobody on the roster provides: " + ", ".join(cov.unmet_raidwide)]
    return "\n".join(out)


def _short(name: str) -> str:
    return name.split(" (")[0].replace(" Totem", "").replace(" / ", "/")[:14]
