"""Buff coverage matrix: for every player in every group, which party buffs
they would benefit from and whether a provider is in their group."""
from __future__ import annotations

from pydantic import BaseModel

from ..models import Player, RosterResult
from ..profiles import GameProfile


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


class Coverage(BaseModel):
    buffs: list[str]  # column order (buff names)
    groups: list[GroupCoverage]
    unmet_raidwide: list[str]  # buffs nobody on the roster can provide


def compute(profile: GameProfile, players: list[Player], result: RosterResult) -> Coverage:
    by_name = {p.signup_name: p for p in players}
    buffs = profile.party_buffs()
    anyone = {b.id: any(b.provided_by(profile.spec(p.cls, p.spec)) for p in result.selected) for b in buffs}
    groups: list[GroupCoverage] = []
    for gi, names in enumerate(result.groups, 1):
        members = [by_name[n] for n in names]
        specs = {p.signup_name: profile.spec(p.cls, p.spec) for p in members}
        pcs: list[PlayerCoverage] = []
        missing_tally: dict[str, tuple[int, float]] = {}
        for p in members:
            cells = []
            cov = miss = 0.0
            for b in buffs:
                provs = [(q.character or q.signup_name) for q in members if q is not p and b.provided_by(specs[q.signup_name])]
                if b.stacking == "unique":
                    # a provider also covers itself for unique buffs (totems, auras)
                    if b.provided_by(specs[p.signup_name]):
                        provs = [p.character or p.signup_name] + provs
                val = b.benefit(specs[p.signup_name])
                present = bool(provs)
                if val > 0:
                    if present:
                        cov += val
                    else:
                        miss += val
                        n, v = missing_tally.get(b.name, (0, 0.0))
                        missing_tally[b.name] = (n + 1, v + val)
                cells.append(Cell(buff=b.name, value=val, present=present, providers=provs))
            pcs.append(PlayerCoverage(name=p.character or p.signup_name, cls=p.cls, spec=p.spec, role=p.role, cells=cells, covered=cov, missing=miss))
        summary = [f"{name} (wanted by {n}, +{v:.0f}){' — nobody on roster' if not anyone[next(b.id for b in buffs if b.name == name)] else ''}" for name, (n, v) in sorted(missing_tally.items(), key=lambda kv: -kv[1][1])]
        groups.append(GroupCoverage(index=gi, players=pcs, missing_summary=summary))
    return Coverage(buffs=[b.name for b in buffs], groups=groups, unmet_raidwide=[b.name for b in buffs if not anyone[b.id]])


GLYPH_PRESENT, GLYPH_MISSING, GLYPH_NA = "●", "○", "·"


def markdown(cov: Coverage) -> str:
    short = [_short(b) for b in cov.buffs]
    out = ["## Buff coverage", "", "● benefits and present · ○ benefits but missing · · not applicable", "", "| group | player | " + " | ".join(short) + " | cover |", "|---|---|" + "---|" * len(short) + "---|"]
    for g in cov.groups:
        for pc in g.players:
            cells = " | ".join(GLYPH_PRESENT if c.present and c.value > 0 else GLYPH_MISSING if c.value > 0 else GLYPH_NA for c in pc.cells)
            out.append(f"| G{g.index} | {pc.name} ({pc.spec}) | {cells} | {pc.pct:.0%} |")
        if g.missing_summary:
            out.append(f"| G{g.index} | _missing_ | " + " | ".join([""] * len(short)) + f" | {'; '.join(g.missing_summary)} |")
    if cov.unmet_raidwide:
        out += ["", "Nobody on the roster provides: " + ", ".join(cov.unmet_raidwide)]
    return "\n".join(out)


def _short(name: str) -> str:
    return name.split(" (")[0].replace(" Totem", "").replace(" / ", "/")[:14]
