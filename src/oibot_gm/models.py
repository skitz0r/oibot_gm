"""Shared data models. Pydantic so they double as LLM output schemas."""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class SpecInfo(BaseModel):
    cls: str
    spec: str
    role: str  # tank | healer | melee | ranged
    dmg: str  # physical | spell | none
    mana: bool

    @property
    def key(self) -> str:
        return f"{self.cls}:{self.spec}"


class Character(BaseModel):
    name: str
    cls: str
    spec: str
    offspec: Optional[str] = None
    rank: str = "unknown"
    main: Optional[str] = None  # set when this character is an alt


class Player(BaseModel):
    """A signup resolved against the character registry, ready for the solver."""

    signup_name: str
    pos: int
    discord_id: Optional[int] = None  # the member behind a run's signup: identity across runs (display names change)
    status: str  # signed | bench | tentative
    cls: str
    spec: str
    role: str
    offspec: Optional[str] = None  # the solver may switch to it to meet role minimums
    character: Optional[str] = None  # registry name, None when unmapped
    map_confidence: str = "high"
    spec_confidence: str = "high"
    unmapped: bool = False
    note: Optional[str] = None
    rank: str = "unknown"
    attended: int = 0
    attendance_total: int = 0
    attendance_family: Optional[str] = None  # main name if attendance was rolled up
    tank_capable_main: Optional[str] = None  # main/offspec that could tank instead

    @property
    def attendance(self) -> float:
        return self.attended / self.attendance_total if self.attendance_total else 0.0

    @property
    def label(self) -> str:
        who = self.character or f"{self.signup_name}?"
        return f"{who} ({self.cls[:4]} {self.spec})"


class GroupReport(BaseModel):
    index: int
    members: list[str]
    buffs: list[str]  # "Windfury Totem (Boviche) → 3 benefit"
    value: int


class RosterResult(BaseModel):
    selected: list[Player]
    benched: list[Player]
    groups: list[list[str]]  # signup names per group
    group_reports: list[GroupReport]
    objective: int
    synergy_value: int
    role_counts: dict[str, int]
    advisories: list[str]  # one-liners, level dot first (🔴/🟡/🟢)
    details: list[str] = Field(default_factory=list)  # longer explanations behind the advisories
    spec_switches: dict[str, str] = Field(default_factory=dict)  # signup_name -> offspec the solver chose
    bench_whatif: dict[str, int] = Field(default_factory=dict)  # name -> objective if forced in
    solver_status: str = ""
    narrative: Optional[str] = None


class LootAward(BaseModel):
    """One award (a ledger row, and the session's record of it). The unit is the DROP: `drop_id` within its run, and the
    ledger's `import_id` (`<run>-<drop_id>`) is what makes a re-derived row idempotent. An item the profile doesn't know
    is still an award (design §3.1 decision 4): `item_id` 0, `item_name` carries it, `tiered` False."""

    raider: str
    item_id: int
    tier: str
    total_weight: float
    offspec: bool
    received: date
    instance: str
    boss: str
    item_name: Optional[str] = None  # what the item is called (the only key an unknown item has)
    discord_id: Optional[int] = None  # the member behind the character (None: a sub / guest stored by name)
    tiered: bool = True  # False: scored at loot_cfg.untiered_weight (no tier data when it was awarded)
    by: Optional[str] = None  # who recorded it: an officer's display name, or "companion:<character>"
    policy_version: Optional[str] = None  # the loot policy the award was judged under (sha of its text)
    candidates_snapshot: list[dict] = Field(default_factory=list)  # top candidates at award time (character, spec, tier, base)
    drop_id: Optional[str] = None
    import_id: Optional[str] = None
    source: Optional[str] = None  # bot | override | companion | manual | correct
    bot_pick: Optional[str] = None
    confirmed_at: Optional[str] = None
    void: bool = False  # superseded by a correction (the ledger keeps the row; a void row marks it)


class Candidate(BaseModel):
    character: str
    cls: str
    spec: str
    rank: str
    unmapped: bool = False
    tier: str
    offspec: bool = False
    upgrade_value: float
    attendance: float
    attendance_str: str
    recent_power: float
    recent_items: list[str]
    wishlist_rank: Optional[int] = None
    base_score: float
    factors: dict[str, float]
    already_has: bool = False
    attendance_known: bool = True  # False: no run history yet — the factor is neutral (1.0), never 0.5 (design §3.1 decision 10)
    tiered: bool = True  # False: the item has no tier data; upgrade is loot_cfg.untiered_weight for everyone


class LootRecommendation(BaseModel):
    """Structured output the LLM must produce (and the fallback mimics)."""

    primary: str = Field(description="Character name of the recommended recipient")
    alternates: list[str] = Field(default_factory=list, description="Up to two runners-up, in order")
    justification: str = Field(description="2-4 sentences, cite policy rule numbers like [R3]")
    cites_rules: list[int] = Field(default_factory=list)
    close_call: bool = Field(description="True if the top two are within a whisker")
    deviates_from_score: bool = Field(description="True if primary is not the top base score")
    deviation_reason: Optional[str] = Field(default=None, description="Required when deviates_from_score")
    warnings: list[str] = Field(default_factory=list, description="Missing data, unmapped players, policy conflicts")


class DropResult(BaseModel):
    item_id: int
    item_name: str
    boss: str
    candidates: list[Candidate]
    recommendation: LootRecommendation
    source: str  # "claude:<model>" | "deterministic"


class RosterNarrative(BaseModel):
    summary: str = Field(description="3-6 sentences for the raid leader")
    notes: list[str] = Field(default_factory=list, description="Short bullets: risks, asks, swaps to consider")
