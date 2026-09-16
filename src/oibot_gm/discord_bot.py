"""Discord front end — mock event flow (shadow data; nothing real is written).

  /mock signup instance:<raid>   seed a signup sheet from the fixture (one event per channel)
  /mock set name status          tweak a signup (signed | bench | absent)
  /mock lock                     lock signups, propose roster + groups; chat in the channel to
                                 request changes ("swap Olnick and Rhozlad", "bench Zyro, bring thilly")
  /mock accept                   accept the proposed roster (or press the button)
  /mock start                    open the raid thread (instance carried from the signup)
  pick a boss → multi-select what dropped → /mock distribute (or the button)
  chat in the thread to adjust  ("give the drape to Paldebaran, Boviche just got Syphon")
  Confirm awards → next boss     /mock end for the summary
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import discord
import yaml
from discord import app_commands
from pydantic import BaseModel, Field

from . import nl, render
from .discord_feed import FeedMixin
from .discord_policy import PolicyContext, handle_change, register_policy_commands
from .discord_help import GuideSelect, HelpMixin, guide_intro, guide_view, register_help_commands
from .discord_pool import PoolMixin
from .feed import FeedServer, feed_config
from .discord_raid import FillButton, PlaceButton, RaidContext, RaidMixin, SignupButton, register_raid_commands
from .discord_registry import Guilds, PlanButton, RegisterButton, is_officer, is_owner, register_commands
from .importers import biscouncil, signup as signup_mod, wcl
from .ops import Ops
from .llm.provider import Provider, get_provider
from .loot import recommend as rec_mod, scoring
from .models import DropResult, LootAward, Player, RosterResult
from .profiles import GameProfile, Item
from .roster import coverage as cov_mod, explain, solver
from .store import GitStore, resolve_data_root

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out"
STORE: GitStore | None = None  # set in run(); None = fixtures-only mode (no git)
GUILD_KEY = "25bg"


def _store() -> GitStore:
    assert STORE is not None, "store not initialised"
    return STORE

CLASS_COLOURS = {"Warrior": 0xC79C6E, "Paladin": 0xF58CBA, "Hunter": 0xABD473, "Rogue": 0xFFF569, "Priest": 0xFFFFFF, "Shaman": 0x0070DE, "Mage": 0x69CCF0, "Warlock": 0x9482C9, "Druid": 0xFF7D0A}
TEAL = 0x2B7A78
ROLE_ICON = {"tank": "🛡", "healer": "✚", "melee": "⚔", "ranged": "🏹"}
# Filled at startup with application emojis (class_<name>, role_<name>); falls back to text.
EMOJI: dict[str, str] = {}


def ico(kind: str, key: str) -> str:
    return EMOJI.get(f"{kind}_{key.lower()}", ROLE_ICON.get(key, "") if kind == "role" else "")


# ---------------------------------------------------------------- guild context

class GuildContext:
    def __init__(self, guild_dir: Path, signup_file: str):
        self.guild_dir = guild_dir
        self.guild_key = guild_dir.name
        self.guild, self.registry, smap = signup_mod.load_registry(guild_dir / "characters.yaml")
        self.profile = GameProfile.load(ROOT / "profiles" / self.guild["game_profile"])
        att = wcl.attendance(guild_dir / "wcl_attendance_1060.json")
        self.seed_event, self.seed_players = signup_mod.load_signup(guild_dir / signup_file, self.profile, self.registry, smap, att)
        _, imported = biscouncil.parse(next(guild_dir.glob("biscouncil_loot_*.csv")))
        # native ledger (awards confirmed through the bot) on top of the imported export, deduped
        native = [LootAward(**{k: v for k, v in r.items() if k in LootAward.model_fields}) for r in (STORE.read_jsonl(Path(guild_dir.name) / "ledger.jsonl") if STORE else [])]
        seen = {(a.raider, a.item_id, str(a.received)) for a in imported}
        self.ledger = imported + [a for a in native if (a.raider, a.item_id, str(a.received)) not in seen]
        self.precedents: list[dict] = STORE.read_jsonl(Path(guild_dir.name) / "precedents.jsonl") if STORE else []
        self.wishlists = yaml.safe_load((guild_dir / "wishlists.yaml").read_text())["wishlists"]
        self.policy = (guild_dir / "policy.md").read_text()
        persona = guild_dir / "persona.md"
        self.persona = persona.read_text() if persona.exists() else ""
        prov = guild_dir / "provenance.md"
        self.provenance = prov.read_text() if prov.exists() else ""
        self.provider: Provider | None = get_provider()

    def policy_for_llm(self) -> str:
        """Policy + voice + data provenance + recent precedents from earlier raids."""
        text = self.policy + ("\n\n## Voice\n" + self.persona if self.persona else "") + ("\n\n## Data provenance\n" + self.provenance if self.provenance else "")
        recent = [p for p in self.precedents if p.get("status", "active") == "active"][-20:]
        if recent:
            text += "\n\n## Precedents from earlier raids (council overrides, with reasons; cite when relevant)\n" + "\n".join(f"- {p.get('date','?')} {p['item']}: bot picked {p['bot']}, council awarded {p['human']} — “{p['reason']}”" for p in recent)
        return text


# ---------------------------------------------------------------- event state

class Proposal(BaseModel):
    item_id: int
    item_name: str
    boss: str
    award_to: str
    source: str  # bot | override
    reason: Optional[str] = None
    result: DropResult
    order: int = 0  # judging order within the batch (1 = first)
    impact: float = 0.0  # weight × best eligible tier
    order_sensitive: Optional[str] = None  # who would win under kill order, if different


class MockEvent(BaseModel):
    """A loot/raid session. Despite the name it serves both the mock flow (shadow
    fixtures) and real raids opened with /raid loot; `guild` decides which context
    (ledger, policy, precedents) it reads and writes."""

    id: str
    channel_id: int
    instance: str
    date: str
    guild: str = "25bg"
    origin: str = "mock"  # mock | raid
    state: str = "signup_open"  # signup_open | roster_proposed | roster_locked | raid | ended
    signups: list[Player]
    roster: Optional[RosterResult] = None
    roster_log: list[str] = Field(default_factory=list)
    keep_together: list[list[str]] = Field(default_factory=list)
    keep_apart: list[list[str]] = Field(default_factory=list)
    force_out: list[str] = Field(default_factory=list)  # benched by request, sticky across re-solves
    force_in: list[str] = Field(default_factory=list)
    raid_thread_id: Optional[int] = None
    current_boss: Optional[str] = None
    drops: dict[str, list[int]] = Field(default_factory=dict)  # boss -> selected item ids
    distributed: list[int] = Field(default_factory=list)  # item ids already awarded/confirmed
    pending_drops: list[int] = Field(default_factory=list)  # working set for the current distribution
    proposals: list[Proposal] = Field(default_factory=list)
    awards: list[LootAward] = Field(default_factory=list)
    overrides: list[dict] = Field(default_factory=list)
    bosses_done: list[str] = Field(default_factory=list)
    pending_reasons: list[dict] = Field(default_factory=list)  # in-game overrides awaiting a reason

    @property
    def raid_name(self) -> str:
        return self.instance.replace("_", " ").title()

    def active_signups(self) -> list[Player]:
        return [p for p in self.signups if p.status != "absent"]

    def undistributed(self, profile: GameProfile) -> list[int]:
        """Selected drops not yet awarded, in boss (kill) order."""
        order = [b["name"] for b in profile.raids[self.instance]["bosses"]]
        return [i for boss in order for i in self.drops.get(boss, []) if i not in self.distributed]

    def rel_path(self) -> Path:
        return Path(self.guild) / "events" / f"{self.channel_id}.json"

    def save(self, message: str | None = None) -> None:
        st = _store()
        st.write_text(self.rel_path(), self.model_dump_json(indent=1))
        st.commit(message or f"{self.id}: {self.state}")

    def archive(self) -> None:
        st = _store()
        st.write_text(Path(self.guild) / "events" / "archive" / f"{self.id}-{self.date}-{self.channel_id}.json", self.model_dump_json(indent=1))
        live = st.root / self.rel_path()
        if live.exists():
            live.unlink()
        st.commit(f"{self.id}: archived")


def load_events() -> dict[int, MockEvent]:
    """Live sessions from every guild directory in the data repo."""
    out = {}
    for gdir in _store().root.iterdir():
        d = gdir / "events"
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                ev = MockEvent.model_validate_json(f.read_text())
                if ev.guild != gdir.name:
                    ev.guild = gdir.name  # pre-refactor files had no guild field
                out[ev.channel_id] = ev
            except Exception as e:  # noqa: BLE001
                print(f"could not load {f}: {e}")
    return out


# ---------------------------------------------------------------- rendering

def signup_embed(ev: MockEvent, ctx: GuildContext) -> discord.Embed:
    active = ev.active_signups()
    signed = [p for p in active if p.status == "signed"]
    bench = [p for p in active if p.status == "bench"]
    counts = {r: sum(1 for p in signed if p.role == r) for r in ("tank", "healer", "melee", "ranged")}
    e = discord.Embed(title=f"{ev.raid_name} signup · {ev.id}", colour=TEAL, description=f"{ev.date} · **{len(signed)}** signed (+{len(bench)} bench) · " + " · ".join(f"{ico('role', r)} {n}" for r, n in counts.items()))
    by_cls: dict[str, list[Player]] = {}
    for p in sorted(signed, key=lambda p: p.pos):
        by_cls.setdefault(p.cls, []).append(p)
    for cls, ps in by_cls.items():
        e.add_field(name=f"{ico('class', cls)} {cls} ({len(ps)})", value="\n".join(f"{ico('role', p.role)} `{p.pos:>2}` **{p.signup_name}** · {p.spec}" for p in ps), inline=True)
    if bench:
        e.add_field(name=f"Bench ({len(bench)})", value="\n".join(f"{ico('class', p.cls)} {p.signup_name} · {p.spec}" for p in bench), inline=False)
    e.set_footer(text=f"state: {ev.state} · /mock set to tweak · /mock lock to propose a roster")
    return e


def roster_text(ev: MockEvent) -> str:
    r = ev.roster
    assert r
    by = {p.signup_name: p for p in ev.signups}
    lines = []
    for gi, names in enumerate(r.groups, 1):
        lines.append(f"Group {gi} (+{r.group_reports[gi-1].value}): " + ", ".join(f"{n} [{by[n].cls} {by[n].spec}]" for n in names))
    lines.append("Bench: " + (", ".join(f"{p.signup_name} [{p.cls} {p.spec}]" for p in r.benched) or "nobody"))
    return "\n".join(lines)


def roster_embed(ev: MockEvent, ctx: GuildContext, note: str | None = None) -> tuple[discord.Embed, discord.File]:
    """Roster card as an image (class colours, coverage matrix) + a slim embed with the text that matters."""
    r = ev.roster
    assert r
    png = render.roster_png(ctx.profile, ev.active_signups(), r, f"{ev.raid_name} · {ev.id}", f"{len(r.selected)} in · synergy {r.synergy_value} · {ev.date}")
    file = discord.File(BytesIO(png), filename="roster.png")
    e = discord.Embed(title=f"Proposed roster · {ev.raid_name} · {ev.id}", colour=TEAL, description=(note + "\n\n" if note else "") + f"{len(r.selected)} in · " + " · ".join(f"{ico('role', k)} {v}" for k, v in r.role_counts.items()) + f" · synergy **{r.synergy_value}**")
    e.set_image(url="attachment://roster.png")
    e.add_field(name="Bench", value="\n".join(f"{ico('class', p.cls)} {p.signup_name} · {p.spec}" for p in r.benched) or "nobody", inline=False)
    if r.advisories:
        e.add_field(name="Advisories", value="\n".join(f"• {a[:300]}" for a in r.advisories[:4])[:1000], inline=False)
    e.set_footer(text="Type changes here (“swap Olnick and Rhoz”, “bench Zyro, bring thilly”, “why not thilly?”) · Accept to lock")
    return e, file


def wowhead(item: Item) -> str:
    return f"https://www.wowhead.com/tbc/item={item.id}"


def candidate_block(r: DropResult, limit: int = 7) -> str:
    rows = []
    for i, c in enumerate(r.candidates[:limit], 1):
        flag = "⚠" if c.unmapped else ""
        base = "HAS" if c.already_has else f"{c.base_score:.2f}"
        wl = f"#{c.wishlist_rank}" if c.wishlist_rank else "-"
        rows.append(f"{ico('class', c.cls)} **{c.character}**{flag} {c.spec} · {c.tier} · att {c.attendance_str} · wl {wl} · 14d {c.recent_power:.2f} · **{base}**")
    return "\n".join(rows)


def drop_embed(p: Proposal, item: Item) -> discord.Embed:
    r, rec = p.result, p.result.recommendation
    colour = CLASS_COLOURS.get(next((c.cls for c in r.candidates if c.character == p.award_to), ""), 0x5865F2)
    e = discord.Embed(title=item.name, url=wowhead(item), description=f"**{item.boss}** · {item.slot.replace('_', ' ')} · {item.type}", colour=colour)
    e.add_field(name="Candidates (tier · attendance · wishlist · 14d loot power · base)", value=candidate_block(r), inline=False)
    if p.source == "override":
        e.add_field(name=f"Council award → **{p.award_to}**", value=f"Overrides bot pick {rec.primary}: “{p.reason}”", inline=False)
    else:
        flags = [f for f, on in (("close call", rec.close_call), ("deviates from score", rec.deviates_from_score)) if on]
        e.add_field(name=f"Recommendation → **{rec.primary}**" + (f"  _({', '.join(flags)})_" if flags else ""), value=rec.justification[:1000], inline=False)
        if rec.deviation_reason:
            e.add_field(name="Why not the top score", value=rec.deviation_reason[:300], inline=False)
    if rec.alternates:
        e.add_field(name="Alternates", value=", ".join(rec.alternates), inline=True)
    if rec.warnings:
        e.add_field(name="Warnings", value="\n".join(f"• {w}" for w in rec.warnings[:3])[:800], inline=False)
    e.set_footer(text=f"judged #{p.order} · impact {p.impact:.2f} · source: {r.source}")
    return e


def proposal_summary(ev: MockEvent) -> discord.Embed:
    e = discord.Embed(title=f"Proposed awards · {len(ev.proposals)} items", colour=TEAL)
    by_boss: dict[str, list[Proposal]] = {}
    for p in ev.proposals:
        by_boss.setdefault(p.boss, []).append(p)
    lines = []
    for boss, ps in by_boss.items():
        lines.append(f"**{boss}**")
        for p in ps:
            flags = []
            if p.source == "override":
                flags.append(f"override: {p.reason}")
            if p.order_sensitive:
                flags.append("order-sensitive")
            if p.result.recommendation.close_call and p.source == "bot":
                flags.append("close call")
            lines.append(f"`#{p.order:>2}` {p.item_name} → **{p.award_to}**" + (f" _({'; '.join(flags)})_" if flags else ""))
    e.description = "\n".join(lines)[:4000]
    sens = sum(1 for p in ev.proposals if p.order_sensitive)
    e.set_footer(text=f"# = judging order (impact first) · {sens} order-sensitive · “Details for…” shows the full card · reply here to adjust · Confirm to record")
    return e


def by_character_embed(ev: MockEvent, ctx: GuildContext, proposals: list[Proposal] | None = None, title: str = "By character") -> discord.Embed:
    ps = proposals if proposals is not None else ev.proposals
    by_char: dict[str, list[Proposal]] = {}
    for p in ps:
        by_char.setdefault(p.award_to, []).append(p)
    cls_of = {(p.character or p.signup_name): p.cls for p in ev.signups}
    lines = []
    for who, items in sorted(by_char.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        power = sum(next((c.upgrade_value for c in p.result.candidates if c.character == who), 0.0) for p in items)
        lines.append(f"{ico('class', cls_of.get(who, ''))} **{who}** ({len(items)}, +{power:.2f}): " + ", ".join(p.item_name.replace(" (T6 token)", "") for p in items))
    e = discord.Embed(title=f"{title} · {len(by_char)} recipients, {len(ps)} items", colour=TEAL, description="\n".join(lines)[:4000] or "nothing")
    return e


class DetailSelect(discord.ui.Select):
    def __init__(self, bot: "OibotGM", ev: MockEvent):
        self.bot, self.ev = bot, ev
        opts = [discord.SelectOption(label=f"{p.item_name} → {p.award_to}"[:100], value=str(p.item_id), description=f"{p.boss} · judged #{p.order}"[:100]) for p in ev.proposals][:25]
        super().__init__(placeholder="Details for…", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        p = next((p for p in self.ev.proposals if p.item_id == int(self.values[0])), None)
        if not p:
            await interaction.response.send_message("That proposal is gone (confirmed or discarded).", ephemeral=True)
            return
        await interaction.response.send_message(embed=drop_embed(p, self.bot.ctx.profile.items[p.item_id]), ephemeral=True)


def proposal_text(ev: MockEvent) -> str:
    """Compact digest for the feedback parser (~80 tokens/item, not the full cards)."""
    out = []
    for p in ev.proposals:
        r = p.result
        top = "; ".join(f"{c.character} ({c.cls[:3]} {c.spec[:5]}, {c.tier}, {c.base_score:.2f}{', has it' if c.already_has else ''})" for c in r.candidates[:4])
        why = r.recommendation.justification.split(". ")[0][:160]
        out.append(f"- item_id {p.item_id} {p.item_name} [{p.boss}] → {p.award_to} [{p.source}{': ' + p.reason if p.reason else ''}] · top: {top} · why: {why}")
    return "\n".join(out)


def table_signature(cands) -> str:
    """What the model actually judged on: top candidates and their rounded scores."""
    return "|".join(f"{c.character}:{c.base_score:.2f}:{c.already_has}" for c in cands[:6])


# ---------------------------------------------------------------- roster ops

def apply_ops(ctx: GuildContext, ev: MockEvent, ops: list[nl.RosterOp]) -> list[str]:
    """Apply structured roster ops with code. Returns human-readable change notes."""
    assert ev.roster
    players = ev.active_signups()
    names = {p.signup_name.lower(): p.signup_name for p in players}
    notes: list[str] = []
    groups = [list(g) for g in ev.roster.groups]

    def find(n: str | None) -> str | None:
        if not n:
            return None
        return names.get(n.lower()) or next((v for k, v in names.items() if k.startswith(n.lower())), None)

    def loc(n: str) -> int | None:
        return next((gi for gi, g in enumerate(groups) if n in g), None)

    resolve_needed = False
    opts = solver.SolveOptions(
        time_limit_s=10,
        force_in=tuple(ev.force_in),
        force_out=tuple(ev.force_out),
        keep_together=tuple(tuple(x) for x in ev.keep_together),
        keep_apart=tuple(tuple(x) for x in ev.keep_apart),
    )
    for op in ops:
        a, b = find(op.a), find(op.b)
        if op.type == "swap" and a and b:
            ga, gb = loc(a), loc(b)
            if ga is not None and gb is not None:
                groups[ga][groups[ga].index(a)], groups[gb][groups[gb].index(b)] = b, a
                notes.append(f"swapped {a} (G{ga+1}) ↔ {b} (G{gb+1})")
            elif ga is not None and gb is None:
                groups[ga][groups[ga].index(a)] = b
                notes.append(f"{b} takes {a}'s slot in G{ga+1}; {a} to bench")
            elif gb is not None and ga is None:
                groups[gb][groups[gb].index(b)] = a
                notes.append(f"{a} takes {b}'s slot in G{gb+1}; {b} to bench")
        elif op.type == "move" and a and op.group and 1 <= op.group <= len(groups):
            ga, gt = loc(a), op.group - 1
            if ga is None:
                notes.append(f"{a} is not in the raid; use promote first")
                continue
            if ga == gt:
                continue
            if len(groups[gt]) < ctx.profile.comp_rules["group_size"]:
                groups[ga].remove(a)
                groups[gt].append(a)
                notes.append(f"moved {a} G{ga+1} → G{gt+1}")
            else:  # swap with whichever member of the target group costs least
                best, best_syn = None, -1
                for partner in groups[gt]:
                    trial = [list(g) for g in groups]
                    trial[ga][trial[ga].index(a)], trial[gt][trial[gt].index(partner)] = partner, a
                    _, syn = solver.group_reports(ctx.profile, players, trial)
                    if syn > best_syn:
                        best, best_syn = partner, syn
                groups[ga][groups[ga].index(a)], groups[gt][groups[gt].index(best)] = best, a
                notes.append(f"moved {a} G{ga+1} → G{gt+1}, {best} back to G{ga+1}")
        elif op.type == "bench" and a:
            if a in ev.force_in:
                ev.force_in.remove(a)
            if a not in ev.force_out:
                ev.force_out.append(a)
            opts.force_out = tuple(ev.force_out)
            opts.force_in = tuple(ev.force_in)
            opts.pins = {**(opts.pins or {}), **{n: gi for gi, g in enumerate(groups) for n in g if n != a and n not in ev.force_out}}
            resolve_needed = True
            notes.append(f"benched {a}; solver fills the slot")
        elif op.type == "promote" and a:
            if a in ev.force_out:
                ev.force_out.remove(a)
            if a not in ev.force_in:
                ev.force_in.append(a)
            opts.force_in = tuple(ev.force_in)
            opts.force_out = tuple(ev.force_out)
            resolve_needed = True
            notes.append(f"bringing {a}; solver re-seats the raid")
        elif op.type == "keep_together" and a and b:
            ev.keep_together.append([a, b])
            opts.keep_together = opts.keep_together + ((a, b),)
            resolve_needed = True
            notes.append(f"rule: keep {a} with {b}")
        elif op.type == "keep_apart" and a and b:
            ev.keep_apart.append([a, b])
            opts.keep_apart = opts.keep_apart + ((a, b),)
            resolve_needed = True
            notes.append(f"rule: keep {a} away from {b}")
        elif op.type == "regenerate":
            resolve_needed = True
            notes.append("regenerated from scratch")
        else:
            notes.append(f"could not apply {op.type} ({op.a}, {op.b})")

    before = ev.roster.synergy_value
    if resolve_needed:
        if opts.force_in and opts.pins:
            opts.pins = None  # promote needs a free slot
        try:
            result = solver.solve(ctx.profile, players, ev.instance, opts)
        except RuntimeError as e:
            notes.append(f"infeasible: {e}")
            return notes
        ev.roster = explain.annotate(ctx.profile, players, ev.instance, result, whatif=False)
    else:
        ev.roster = solver.rebuild(ctx.profile, players, groups, ev.roster)
        ev.roster = explain.annotate(ctx.profile, players, ev.instance, ev.roster, whatif=False)
    notes.append(f"synergy {before} → {ev.roster.synergy_value}")
    ev.roster_log.extend(notes)
    ev.save()
    return notes


# ---------------------------------------------------------------- loot ops

def _award_for(ev: MockEvent, item: Item, cands, who: str, raid_date: date) -> LootAward | None:
    c = next((c for c in cands if c.character == who), None)
    if not c:
        return None
    return LootAward(raider=who, item_id=item.id, tier=c.tier, total_weight=c.upgrade_value, offspec=c.offspec, received=raid_date, instance=ev.raid_name, boss=item.boss)


def run_distribution(ctx: GuildContext, ev: MockEvent, keep_overrides: bool = True, progress=None, concurrency: int = 6) -> None:
    """Two-pass: a fast deterministic pass gives provisional awards so every item's
    table can be built up front; Claude then judges items in parallel; finally the
    tables are re-scored against the actual picks and any ranking shift is flagged."""
    from concurrent.futures import ThreadPoolExecutor

    assert ev.roster
    roster = ev.roster.selected
    raid_date = date.fromisoformat(ev.date)
    ledger = ctx.ledger + ev.awards
    policy = ctx.policy_for_llm()
    if ev.overrides:
        policy += "\n\n## Precedents (council overrides this raid, with reasons)\n" + "\n".join(f"- {o['item']}: bot picked {o['bot']}, council awarded {o['human']} — “{o['reason']}”" for o in ev.overrides)
    prior = {p.item_id: p for p in ev.proposals if p.source == "override"} if keep_overrides else {}
    # previous bot judgments: reused when an item's table is unchanged (saves a Claude call per item)
    previous = {p.item_id: p for p in ev.proposals if p.source == "bot"} if keep_overrides else {}
    kill_order = list(ev.pending_drops)

    def provisional_pass(seq: list[int]) -> tuple[dict[int, list[LootAward]], dict[int, str | None]]:
        """Deterministic sequential pass: per-item context (awards before it) and provisional winner."""
        awards: list[LootAward] = []
        contexts: dict[int, list[LootAward]] = {}
        winners: dict[int, str | None] = {}
        for item_id in seq:
            item = ctx.profile.items[item_id]
            contexts[item_id] = list(awards)
            cands = scoring.candidates(ctx.profile, item, roster, ledger + awards, ctx.wishlists, raid_date)
            who = prior[item_id].award_to if item_id in prior else ((next((c for c in cands if not c.already_has), None) or (cands[0] if cands else None)) or None)
            who = who if isinstance(who, str) or who is None else who.character
            winners[item_id] = who
            if who:
                a = _award_for(ev, item, cands, who, raid_date)
                if a:
                    awards.append(a)
        return contexts, winners

    # impact = weight × best eligible tier, on a clean table; big items settle first
    impact: dict[int, float] = {}
    for item_id in kill_order:
        item = ctx.profile.items[item_id]
        cands = scoring.candidates(ctx.profile, item, roster, ledger, ctx.wishlists, raid_date)
        impact[item_id] = max((c.upgrade_value for c in cands if not c.already_has), default=0.0)
    mode = ctx.profile.loot.get("distribution", {}).get("order", "impact")
    order = sorted(kill_order, key=lambda i: (-impact[i], kill_order.index(i))) if mode == "impact" else kill_order

    # pass 1: provisional context in the judging order, plus kill-order winners for sensitivity
    contexts, impact_winners = provisional_pass(order)
    _, kill_winners = provisional_pass(kill_order)

    # pass 2: judgment, parallel
    done = 0

    def judge(item_id: int) -> Proposal:
        nonlocal done
        item = ctx.profile.items[item_id]
        cands = scoring.candidates(ctx.profile, item, roster, ledger + contexts[item_id], ctx.wishlists, raid_date)
        if item_id in prior:
            p = prior[item_id]
            res = rec_mod.recommend(ctx.profile, item, cands, policy, None)
            out = Proposal(item_id=item_id, item_name=item.name, boss=item.boss, award_to=p.award_to, source="override", reason=p.reason, result=DropResult(**{**res.model_dump(), "recommendation": p.result.recommendation}))
        elif item_id in previous and table_signature(cands) == table_signature(previous[item_id].result.candidates) and previous[item_id].result.source.startswith("claude"):
            p = previous[item_id]  # same table as last time: keep the judgment, skip the call
            out = Proposal(item_id=item_id, item_name=item.name, boss=item.boss, award_to=p.award_to, source="bot", result=DropResult(item_id=item_id, item_name=item.name, boss=item.boss, candidates=cands, recommendation=p.result.recommendation, source=p.result.source + " (unchanged)"))
        else:
            res = rec_mod.recommend(ctx.profile, item, cands, policy, ctx.provider)
            out = Proposal(item_id=item_id, item_name=item.name, boss=item.boss, award_to=res.recommendation.primary, source="bot", result=res)
        out.order = order.index(item_id) + 1
        out.impact = impact[item_id]
        if mode == "impact" and kill_winners.get(item_id) != impact_winners.get(item_id) and out.source == "bot":
            out.order_sensitive = kill_winners.get(item_id)
            out.result.recommendation.warnings.append(f"order-sensitive: under kill order this would go to {kill_winners.get(item_id)}; decide this one deliberately")
        done += 1
        if progress:
            progress(done, len(order), item.name)
        return out

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(judge, order))

    # pass 3: re-score against actual picks (in judging order), flag shifts
    actual: list[LootAward] = []
    for p in sorted(results, key=lambda p: p.order):
        item = ctx.profile.items[p.item_id]
        fresh = scoring.candidates(ctx.profile, item, roster, ledger + actual, ctx.wishlists, raid_date)
        top = next((c for c in fresh if not c.already_has), None)
        if top and p.source == "bot" and p.award_to != top.character and not p.result.recommendation.deviates_from_score:
            rank = next((i for i, c in enumerate(fresh, 1) if c.character == p.award_to), None)
            p.result.recommendation.warnings.insert(0, f"earlier awards moved the table: {p.award_to} now ranks #{rank}, top score is {top.character} — re-check")
            p.result.recommendation.close_call = True
        p.result.candidates = fresh
        a = _award_for(ev, item, fresh, p.award_to, raid_date)
        if a:
            actual.append(a)
    ev.proposals = sorted(results, key=lambda p: kill_order.index(p.item_id))  # stored/presented in kill order
    ev.save()


def confirm_awards(ctx: GuildContext, ev: MockEvent, only: set[int] | None = None) -> list[str]:
    """Record proposals as awards (all, or just `only` item ids) and drop them from the pending list."""
    raid_date = date.fromisoformat(ev.date)
    lines = []
    st = _store()
    chosen = [p for p in ev.proposals if only is None or p.item_id in only]
    for p in chosen:
        c = next((c for c in p.result.candidates if c.character == p.award_to), None)
        award = LootAward(raider=p.award_to, item_id=p.item_id, tier=c.tier if c else "?", total_weight=c.upgrade_value if c else 0.5, offspec=bool(c and c.offspec), received=raid_date, instance=ev.raid_name, boss=p.boss)
        ev.awards.append(award)
        ctx.ledger.append(award)
        st.append_jsonl(Path(ev.guild) / "ledger.jsonl", {**award.model_dump(), "event": ev.id, "source": p.source, "bot_pick": p.result.recommendation.primary, "import_id": f"{ev.id}-{p.item_id}-{p.award_to}"})
        lines.append(f"{p.item_name} → {p.award_to}" + (" (override)" if p.source == "override" else ""))
        ev.distributed.append(p.item_id)
        if p.boss not in ev.bosses_done:
            ev.bosses_done.append(p.boss)
    ev.proposals = [p for p in ev.proposals if p not in chosen]
    ev.pending_drops = [i for i in ev.pending_drops if only is not None and i not in only] if ev.proposals else []
    ev.save(f"{ev.id}: confirmed {len(lines)} awards")
    return lines


# ---------------------------------------------------------------- views

class RosterView(discord.ui.View):
    def __init__(self, bot: "OibotGM", ev: MockEvent):
        super().__init__(timeout=None)
        self.bot, self.ev = bot, ev

    @discord.ui.button(label="Accept roster", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self.bot.gate(interaction):
            return
        await self.bot.accept_roster(self.ev, interaction)


def boss_embed(bot: "OibotGM", ev: MockEvent, boss: str, items: list[Item]) -> discord.Embed:
    chosen = set(ev.drops.get(boss, []))
    e = discord.Embed(title=boss, colour=TEAL)
    lines = []
    for it in items:
        mark = "✅" if it.id in chosen and it.id in ev.distributed else ("🟡" if it.id in chosen else "▫️")
        lines.append(f"{mark} [{it.name}]({wowhead(it)}) · {it.slot.replace('_', ' ')}")
    e.description = "\n".join(lines)[:4000]
    if chosen:
        e.set_footer(text=f"{len(chosen)} selected · 🟡 awaiting distribution · ✅ awarded")
    else:
        e.set_footer(text="Select what dropped ↓")
    return e


class DropsSelect(discord.ui.Select):
    """Per-boss multi-select; the selection persists on the event and the menu re-renders with it."""

    def __init__(self, bot: "OibotGM", ev: MockEvent, boss: str, items: list[Item]):
        self.bot, self.ev, self.boss, self.items = bot, ev, boss, items
        chosen = set(ev.drops.get(boss, []))
        opts = [discord.SelectOption(label=it.name[:100], value=str(it.id), description=f"{it.slot.replace('_', ' ')} · {it.type}"[:100], default=it.id in chosen) for it in items][:25]
        super().__init__(placeholder=f"What dropped from {boss}?"[:150], options=opts, min_values=0, max_values=len(opts))

    async def callback(self, interaction: discord.Interaction):
        if not await self.bot.gate(interaction):
            return
        keep = [i for i in self.ev.drops.get(self.boss, []) if i in self.ev.distributed]  # never un-award via the menu
        self.ev.drops[self.boss] = sorted(set(keep) | {int(v) for v in self.values}, key=lambda i: [it.id for it in self.items].index(i))
        self.ev.save()
        view = discord.ui.View(timeout=None)
        view.add_item(DropsSelect(self.bot, self.ev, self.boss, self.items))
        await interaction.response.edit_message(embed=boss_embed(self.bot, self.ev, self.boss, self.items), view=view)


class DistributeView(discord.ui.View):
    def __init__(self, bot: "OibotGM", ev: MockEvent):
        super().__init__(timeout=None)
        self.bot, self.ev = bot, ev

    @discord.ui.button(label="Distribute selected drops", style=discord.ButtonStyle.primary)
    async def go(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self.bot.gate(interaction):
            return
        await self.bot.distribute(self.ev, interaction)


class ConfirmView(discord.ui.View):
    def __init__(self, bot: "OibotGM", ev: MockEvent):
        super().__init__(timeout=None)
        self.bot, self.ev = bot, ev
        if ev.proposals:
            self.add_item(DetailSelect(bot, ev))

    @discord.ui.button(label="Confirm awards", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self.bot.gate(interaction):
            return
        if not self.ev.proposals:
            await interaction.response.send_message("Nothing pending.", ephemeral=True)
            return
        confirmed = list(self.ev.proposals)
        confirm_awards(self.bot.ctx, self.ev)
        await interaction.response.send_message(f"✅ Recorded {len(confirmed)} awards. Select more drops above as bosses die, then distribute again.", embed=by_character_embed(self.ev, self.bot.ctx, confirmed, "Awarded"), view=DistributeView(self.bot, self.ev))
        self.stop()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary, row=1)
    async def discard(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self.bot.gate(interaction):
            return
        self.ev.proposals = []
        self.ev.pending_drops = []
        self.ev.save()
        await interaction.response.send_message("Discarded; the selected drops are still marked and can be distributed again.", view=DistributeView(self.bot, self.ev))
        self.stop()


# ---------------------------------------------------------------- bot

class OibotGM(FeedMixin, RaidMixin, PoolMixin, HelpMixin, discord.Client):
    ico = staticmethod(ico)

    def __init__(self, ctx: GuildContext, test_guild: int | None):
        intents = discord.Intents.default()
        intents.message_content = True  # channel chat → roster changes / loot feedback
        super().__init__(intents=intents)
        self.ctx = ctx
        self.tree = app_commands.CommandTree(self)
        self.test_guild = test_guild
        self.events: dict[int, MockEvent] = load_events()
        self.contexts: dict[str, object] = {}  # guild key -> RegistryLootContext (lazy)
        self._register()
        # real (non-mock) surface: registry, officer tools, ops feed
        self.ops = Ops(self)
        self.registries = Guilds(_store(), ROOT)
        register_commands(self.tree, self.registries, self.ops, ico)
        self.raids = RaidContext(self.registries)
        register_raid_commands(self.tree, self.registries, self.ops, self)
        self.policies = PolicyContext(self.registries)
        register_policy_commands(self.tree, self.registries, self.ops, self, self.policies)
        register_help_commands(self.tree, self.registries, self.ops, self)
        self.add_dynamic_items(SignupButton, FillButton, PlaceButton, PlanButton, RegisterButton, GuideSelect)
        self.tree.on_error = self._on_command_error

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        reg = self.registries.for_interaction(interaction)
        cmd = interaction.command.qualified_name if interaction.command else "?"
        msg = f"`/{cmd}` by {interaction.user.display_name}: {type(error).__name__}: {str(error)[:200]}"
        if reg:
            await self.ops.emit(reg.config, "error", msg, exc=error)
        else:
            print("[ops:error]", msg)
        try:
            text = "Something went wrong; the owner has been notified."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    # ---- helpers
    def loot_ctx(self, ev: MockEvent):
        """The context a session reads/writes: the mock fixtures, or the registered guild's data."""
        if ev.guild == self.ctx.guild_key:
            return self.ctx
        if ev.guild not in self.contexts:
            from .lootctx import RegistryLootContext

            reg = next((r for r in self.registries.by_discord.values() if r.key == ev.guild), None)
            if reg is None:
                raise RuntimeError(f"no registry for guild {ev.guild}")
            self.contexts[ev.guild] = RegistryLootContext(reg, _store(), self.ctx.provider)
        return self.contexts[ev.guild]

    def event_for(self, channel_id: int) -> MockEvent | None:
        ev = self.events.get(channel_id)
        if ev:
            return ev
        return next((e for e in self.events.values() if e.raid_thread_id == channel_id), None)

    async def accept_roster(self, ev: MockEvent, interaction: discord.Interaction):
        if not ev.roster:
            await interaction.response.send_message("No roster proposed yet — /mock lock first.", ephemeral=True)
            return
        ev.state = "roster_locked"
        ev.save()
        await interaction.response.send_message(f"🔒 Roster accepted for **{ev.raid_name}** ({len(ev.roster.selected)} in, {len(ev.roster.benched)} benched). `/mock start` opens the raid thread.")

    async def propose_roster(self, ev: MockEvent, note: str | None = None) -> tuple[discord.Embed, discord.ui.View, discord.File]:
        embed, file = await asyncio.to_thread(roster_embed, ev, self.ctx, note)
        return embed, RosterView(self, ev), file

    async def ensure_emojis(self) -> None:
        """Upload class/role badges as application emojis once; map names → emoji strings."""
        try:
            existing = {e.name: e for e in await self.fetch_application_emojis()}
        except Exception as e:  # noqa: BLE001
            print(f"application emojis unavailable: {e}")
            return
        wanted = {f"class_{c.lower()}": ("class", c) for c in render.CLASS} | {f"role_{r}": ("role", r) for r in render.ROLE_COLOUR}
        buff_defs = {}
        for reg in list(self.registries.by_discord.values()) + [self.ctx]:
            for b in reg.profile.party_buffs():
                buff_defs[b.id] = (b.abbr, b.colour)
        wanted |= {f"buff_{bid}": ("buff", bid) for bid in buff_defs}
        for name, (kind, key) in wanted.items():
            em = existing.get(name)
            if em is None:
                try:
                    png = render.class_badge_png(key) if kind == "class" else render.role_badge_png(key) if kind == "role" else render.buff_badge_png(*buff_defs[key])
                    em = await self.create_application_emoji(name=name, image=png)
                except Exception as e:  # noqa: BLE001
                    print(f"emoji {name}: {e}")
                    continue
            EMOJI[name] = str(em)
        print(f"emojis ready: {len(EMOJI)}")

    async def distribute(self, ev: MockEvent, interaction: discord.Interaction):
        ctx = self.loot_ctx(ev)
        pending = ev.undistributed(ctx.profile)
        if not pending:
            await interaction.response.send_message("Nothing selected that hasn't been awarded yet — tick drops on the boss messages first.", ephemeral=True)
            return
        if ev.proposals:
            await interaction.response.send_message("A proposal is already waiting for Confirm/Discard above.", ephemeral=True)
            return
        ev.pending_drops = pending
        ev.save()
        names = ", ".join(ctx.profile.item_name(i) for i in pending)
        await interaction.response.send_message(f"⚖️ Distributing **{len(pending)}** items: {names}"[:1900])
        status = await interaction.original_response()
        loop = asyncio.get_running_loop()
        state = {"done": 0, "last": ""}

        def progress(done: int, total: int, name: str):
            state["done"], state["last"] = done, name

        async def ticker():
            while True:
                await asyncio.sleep(4)
                try:
                    await status.edit(content=f"⚖️ Distributing **{len(pending)}** items… {state['done']}/{len(pending)} judged (latest: {state['last']})"[:1900])
                except Exception:  # noqa: BLE001
                    pass

        t = loop.create_task(ticker())
        try:
            await asyncio.to_thread(run_distribution, ctx, ev, True, progress)
        finally:
            t.cancel()
        await status.edit(content=f"⚖️ Distributed **{len(pending)}** items — proposals below."[:1900])
        await self.post_proposals(ev, interaction.channel)

    async def post_boss_tables(self, ev: MockEvent, dest) -> None:
        ctx = self.loot_ctx(ev)
        raid = ctx.profile.raids.get(ev.instance, {"bosses": []})
        posted = 0
        for b in raid.get("bosses", []):
            items = [i for i in ctx.profile.items.values() if i.boss == b["name"]]
            if not items:
                continue
            view = discord.ui.View(timeout=None)
            view.add_item(DropsSelect(self, ev, b["name"], items))
            await dest.send(embed=boss_embed(self, ev, b["name"], items), view=view)
            posted += 1
        if posted:
            await dest.send("Tick what dropped on each boss as you go, then distribute whenever the council is ready. Reply here in plain text to adjust a proposal.", view=DistributeView(self, ev))
        else:
            await dest.send(f"No loot table is modelled for **{ev.raid_name}** yet. Awards from the companion feed are still recorded here; item data can be added to `profiles/{ctx.profile.name}/items/`.")

    async def post_proposals(self, ev: MockEvent, dest, prefix: str | None = None):
        """Compact: item → character by boss, then character → items. Full cards via the Details dropdown."""
        await dest.send(content=prefix, embeds=[proposal_summary(ev), by_character_embed(ev, self.loot_ctx(ev))], view=ConfirmView(self, ev))

    async def end_session(self, ev: MockEvent, interaction: discord.Interaction) -> None:
        ctx = self.loot_ctx(ev)
        e = discord.Embed(title=f"Raid summary · {ev.raid_name} · {ev.id}", colour=TEAL)
        by_char: dict[str, list[str]] = {}
        power: dict[str, float] = {}
        for a in ev.awards:
            by_char.setdefault(a.raider, []).append(ctx.profile.item_name(a.item_id).replace(" (T6 token)", ""))
            power[a.raider] = power.get(a.raider, 0.0) + a.total_weight
        tally = "\n".join(f"• **{w}** ({len(i)}, +{power[w]:.2f}): {', '.join(i)}" for w, i in sorted(by_char.items(), key=lambda kv: (-len(kv[1]), kv[0])))
        e.add_field(name=f"Awards ({len(ev.awards)} items, {len(by_char)} recipients)", value=tally[:1000] or "(none)", inline=False)
        if ev.overrides:
            e.add_field(name=f"Council overrides ({len(ev.overrides)})", value="\n".join(f"• {o['item']}: {o['bot']} → **{o['human']}** — “{o['reason']}”" for o in ev.overrides)[:1000], inline=False)
        if ev.roster_log:
            e.add_field(name="Roster history", value="\n".join(f"• {l}" for l in ev.roster_log[-8:])[:1000], inline=False)
        agree = len(ev.awards) - len(ev.overrides)
        if ctx.provider:
            e.add_field(name="LLM usage (this bot process)", value=ctx.provider.summary()[:1000], inline=False)
        e.set_footer(text=f"council agreed with the bot on {max(agree, 0)}/{len(ev.awards)} awards · data repo {_store().head()}")
        ev.state = "ended"
        ev.archive()
        self.events.pop(ev.channel_id, None)
        await interaction.response.send_message(embed=e)

    # ---- chat handlers
    def officiates(self, user, guild) -> bool:
        """Officer check for buttons and chat: the guild's registry rules if configured, else Manage Server."""
        reg = self.registries.by_discord.get(guild.id) if guild else None
        if reg and reg.config.owner_discord_id == user.id:
            return True
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.manage_guild:
            return True
        return bool(reg and any(r.name in reg.config.officer_roles for r in user.roles))

    async def gate(self, interaction: discord.Interaction) -> bool:
        """Refuse non-officers on decision buttons; returns True when the press may proceed."""
        if self.officiates(interaction.user, interaction.guild):
            return True
        await interaction.response.send_message("Officers only.", ephemeral=True)
        return False

    def _addressed(self, message: discord.Message) -> bool:
        """@user mention, a mention of the bot's own (integration) role, or a reply to one of the bot's messages."""
        if self.user in message.mentions:
            return True
        me = message.guild.me if message.guild else None
        if me and any(r in message.role_mentions for r in me.roles):
            return True
        ref = message.reference.resolved if message.reference else None
        return isinstance(ref, discord.Message) and ref.author.id == self.user.id

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:  # a DM to the bot = a question about how it works
            reg = next(iter(self.registries.by_discord.values()), None) if len(self.registries.by_discord) == 1 else next((r for r in self.registries.by_discord.values() if message.author.id in r.members), None)
            if reg and message.content.strip():
                async with message.channel.typing():
                    text = await self.help_answer(reg, message.author.id, reg.config.owner_discord_id == message.author.id, message.content)
                if text is None:  # outside the ask audience: static guide only
                    await message.reply(guide_intro(reg), view=guide_view())
                else:
                    await message.reply(text)
            return
        reg = self.registries.by_discord.get(message.guild.id)
        # @mention anywhere else = a question about how the bot works (no state changes)
        if reg and message.channel.id not in (reg.config.ops_channel_id, reg.config.analytics_channel_id) and self._addressed(message) and not self.event_for(message.channel.id):
            text = re.sub(r"<@[!&]?\d+>", "", message.content).strip()
            if text:
                async with message.channel.typing():
                    answer = await self.help_answer(reg, message.author.id, self.officiates(message.author, message.guild), text)
                if answer is None:
                    await message.reply(guide_intro(reg), view=guide_view(), allowed_mentions=discord.AllowedMentions.none())
                else:
                    await message.reply(answer, allowed_mentions=discord.AllowedMentions.none())
            return
        # @mention in the ops or analytics channel = plain-text configuration (comp ideals live in analytics)
        if reg and message.channel.id in (reg.config.ops_channel_id, reg.config.analytics_channel_id) and self._addressed(message):
            text = re.sub(r"<@[!&]?\d+>", "", message.content).strip()
            if text:
                member = message.author
                owner = reg.config.owner_discord_id == member.id
                officer = owner or (isinstance(member, discord.Member) and (member.guild_permissions.manage_guild or any(r.name in reg.config.officer_roles for r in member.roles)))
                async with message.channel.typing():
                    await handle_change(message, reg, self.policies.store(reg), self.ctx.provider, self.ops, text, owner, officer)
            return
        ev = self.event_for(message.channel.id)
        if not ev or not self.ctx.provider:
            return
        if not self.officiates(message.author, message.guild):
            return  # members may chat in the roster/loot channels; only officers steer the bot
        if ev.state == "roster_proposed" and message.channel.id == ev.channel_id and ev.roster:
            async with message.channel.typing():
                try:
                    req = await asyncio.to_thread(nl.parse_roster_request, self.ctx.provider, ev.raid_name, roster_text(ev), message.content)
                except Exception as e:  # noqa: BLE001
                    await message.reply(f"Couldn't parse that ({type(e).__name__}).")
                    return
                if req.kind == "ignore":
                    return
                if req.kind == "accept":
                    ev.state = "roster_locked"
                    ev.save()
                    await message.reply(f"🔒 Roster accepted. `/mock start` opens the raid thread.")
                    return
                if req.kind == "question":
                    await message.reply(req.reply[:1900])
                    return
                notes = await asyncio.to_thread(apply_ops, self.ctx, ev, req.ops)
                embed, view, file = await self.propose_roster(ev, (req.reply + "\n" if req.reply else "") + "Changes: " + "; ".join(notes))
                await message.reply(embed=embed, view=view, file=file)
        elif ev.state == "raid" and message.channel.id == ev.raid_thread_id and ev.pending_reasons and not ev.proposals:
            line = await self.record_pending_reason(ev, message.content, message.author.display_name)
            if line:
                await message.reply(line)
        elif ev.state == "raid" and message.channel.id == ev.raid_thread_id and ev.proposals:
            ctx = self.loot_ctx(ev)
            async with message.channel.typing():
                try:
                    fb = await asyncio.to_thread(nl.parse_loot_feedback, ctx.provider, ctx.policy_for_llm(), proposal_text(ev), message.content)
                except Exception as e:  # noqa: BLE001
                    await message.reply(f"Couldn't parse that ({type(e).__name__}: {str(e)[:160]}).")
                    return
                if fb.kind == "ignore":
                    return
                if fb.kind == "question":
                    await message.reply(fb.reply[:1900])
                    return
                if fb.kind == "confirm":
                    confirmed = list(ev.proposals)
                    confirm_awards(ctx, ev)
                    await message.reply(f"✅ Recorded {len(confirmed)} awards. Select more drops above as bosses die, then distribute again.", embed=by_character_embed(ev, ctx, confirmed, "Awarded"), view=DistributeView(self, ev))
                    return
                applied = []
                for ch in fb.changes:
                    p = next((p for p in ev.proposals if p.item_id == ch.item_id), None)
                    if not p or ch.award_to not in {c.character for c in p.result.candidates}:
                        applied.append(f"couldn't apply {ch.award_to} ← item {ch.item_id}")
                        continue
                    precedent = {"date": ev.date, "event": ev.id, "item": p.item_name, "item_id": p.item_id, "bot": p.result.recommendation.primary, "human": ch.award_to, "reason": ch.reason, "by": message.author.display_name, "status": "active"}
                    ev.overrides.append(precedent)
                    ctx.precedents.append(precedent)
                    _store().append_jsonl(Path(ev.guild) / "precedents.jsonl", precedent)
                    p.award_to, p.source, p.reason = ch.award_to, "override", ch.reason
                    applied.append(f"{p.item_name} → {ch.award_to}")
                ev.save(f"{ev.id}: override {', '.join(applied)}"[:120])
                before = len(ctx.provider.log) if ctx.provider else 0
                await asyncio.to_thread(run_distribution, ctx, ev, True)  # re-score; re-judge only tables that changed
                rejudged = (len(ctx.provider.log) - before) if ctx.provider else 0
                await self.post_proposals(ev, message.channel, prefix=(fb.reply + "\n" if fb.reply else "") + "Applied: " + "; ".join(applied) + f". Re-scored all; re-judged {rejudged} item(s) whose table changed.")

    # ---- commands
    def _register(self):
        bot = self

        class MockGroup(app_commands.Group):
            async def interaction_check(self, interaction: discord.Interaction) -> bool:  # officers only: it spends LLM budget
                if bot.officiates(interaction.user, interaction.guild):
                    return True
                await interaction.response.send_message("Officers only.", ephemeral=True)
                return False

        mock = MockGroup(name="mock", description="Officer: mock event on shadow data — signup → roster → raid → loot council")
        raid_choices = [app_commands.Choice(name=r["name"], value=rid) for rid, r in self.ctx.profile.raids.items()]

        @mock.command(name="signup", description="Seed a mock signup sheet for this channel")
        @app_commands.choices(instance=raid_choices)
        async def signup(interaction: discord.Interaction, instance: app_commands.Choice[str], date_: str = ""):
            d = date_ or self.ctx.seed_event["starts_at"][:10]
            ev = MockEvent(id=f"{instance.value.split('_')[0].upper()[:3]}-{d[5:].replace('-', '')}", channel_id=interaction.channel_id, instance=instance.value, date=d, signups=copy.deepcopy(self.ctx.seed_players))
            self.events[ev.channel_id] = ev
            ev.save()
            await interaction.response.send_message(embed=signup_embed(ev, self.ctx))

        @mock.command(name="set", description="Change a signup: signed / bench / absent")
        @app_commands.choices(status=[app_commands.Choice(name=s, value=s) for s in ("signed", "bench", "absent")])
        async def set_(interaction: discord.Interaction, name: str, status: app_commands.Choice[str]):
            ev = self.event_for(interaction.channel_id)
            if not ev or ev.state != "signup_open":
                await interaction.response.send_message("No open signup here (/mock signup first, or it's already locked).", ephemeral=True)
                return
            p = next((p for p in ev.signups if p.signup_name.lower() == name.lower()), None)
            if not p:
                await interaction.response.send_message(f"No signup named {name}. Names: " + ", ".join(s.signup_name for s in ev.signups), ephemeral=True)
                return
            p.status = status.value
            ev.save()
            await interaction.response.send_message(embed=signup_embed(ev, self.ctx))

        @mock.command(name="lock", description="Lock signups and propose roster + groups")
        async def lock(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev:
                await interaction.response.send_message("No signup here — /mock signup first.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            players = ev.active_signups()

            def build():
                r = solver.solve(self.ctx.profile, players, ev.instance, solver.SolveOptions(time_limit_s=12))
                return explain.annotate(self.ctx.profile, players, ev.instance, r, whatif=True)

            ev.roster = await asyncio.to_thread(build)
            ev.state = "roster_proposed"
            ev.roster_log = ["proposed by solver"]
            ev.save()
            embed, view, file = await self.propose_roster(ev)
            await interaction.followup.send(embed=embed, view=view, file=file)

        @mock.command(name="accept", description="Accept the proposed roster")
        async def accept(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev:
                await interaction.response.send_message("No event here.", ephemeral=True)
                return
            await self.accept_roster(ev, interaction)

        @mock.command(name="start", description="Start the raid with the accepted roster (opens a thread)")
        async def start(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev or ev.state not in ("roster_locked", "raid"):
                await interaction.response.send_message("Accept a roster first (/mock lock → /mock accept).", ephemeral=True)
                return
            assert ev.roster
            roster_txt = ", ".join(p.character or p.signup_name + "?" for p in ev.roster.selected)
            e = discord.Embed(title=f"⚔ {ev.raid_name} · {ev.id} · {ev.date}", colour=TEAL, description=f"Roster ({len(ev.roster.selected)}): {roster_txt}"[:4000])
            e.add_field(name="Ledger", value=f"{len(self.ctx.ledger)} real awards loaded + {len(ev.awards)} from this raid. Awards feed the loot divisor for later drops.", inline=False)
            e.set_footer(text=f"loot judgment: {self.ctx.provider.name if self.ctx.provider else 'deterministic'} · tick drops per boss → Distribute → chat to adjust → Confirm")
            await interaction.response.send_message(embed=e)
            msg = await interaction.original_response()
            thread = await msg.create_thread(name=f"{ev.raid_name} · {ev.id}"[:100])
            ev.raid_thread_id = thread.id
            ev.state = "raid"
            ev.save()
            await self.post_boss_tables(ev, thread)

        @mock.command(name="bosses", description="Re-post the boss loot tables in the raid thread")
        async def bosses(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev or ev.state != "raid":
                await interaction.response.send_message("Not in a raid thread.", ephemeral=True)
                return
            await interaction.response.send_message("Loot tables:")
            await self.post_boss_tables(ev, interaction.channel)

        @mock.command(name="distribute", description="Run the loot council on the recorded drops")
        async def distribute(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev or ev.state != "raid":
                await interaction.response.send_message("Not in a raid thread.", ephemeral=True)
                return
            await self.distribute(ev, interaction)

        @mock.command(name="status", description="Where is this channel's mock event?")
        async def status(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev:
                await interaction.response.send_message("No event here.", ephemeral=True)
                return
            spend = self.ctx.provider.summary() if self.ctx.provider else "LLM off"
            await interaction.response.send_message(f"**{ev.id}** {ev.raid_name} · state `{ev.state}` · signups {len(ev.active_signups())} · roster {'yes' if ev.roster else 'no'} · selected drops {sum(len(v) for v in ev.drops.values())} (undistributed {len(ev.undistributed(self.ctx.profile))}) · awards {len(ev.awards)} · overrides {len(ev.overrides)}\n{spend}"[:1900], ephemeral=True)

        @mock.command(name="end", description="Summarise the raid's awards and overrides")
        async def end(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev:
                await interaction.response.send_message("No event here.", ephemeral=True)
                return
            await self.end_session(ev, interaction)

        @mock.command(name="reset", description="Forget this channel's mock event (archived, not deleted)")
        async def reset(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if ev:
                self.events.pop(ev.channel_id, None)
                ev.archive()
            await interaction.response.send_message("Reset (event archived). /mock signup to start over.", ephemeral=True)

        self.tree.add_command(mock)

    async def setup_hook(self):
        # One scope only, or Discord lists every command twice. Default: global (works in DMs;
        # new commands can take a while to propagate). OIBOT_COMMAND_SCOPE=guild gives instant
        # updates in the test server but no DM commands.
        scope = os.environ.get("OIBOT_COMMAND_SCOPE", "global")
        g = discord.Object(id=self.test_guild) if self.test_guild else None
        if scope == "guild" and g:
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()
            if g:
                self.tree.clear_commands(guild=g)
                await self.tree.sync(guild=g)
        print(f"commands synced ({scope})")
        self.loop.create_task(self.scheduler())
        self.attach_pool_listeners()
        token, bind = feed_config()
        if token:
            self.feed = FeedServer(self.handle_feed_event, token, bind)
            await self.feed.start()
        else:
            print("loot feed disabled (set OIBOT_FEED_TOKEN and OIBOT_FEED_BIND)")
        from .web.app import serve as web_serve

        self.started_at = time.time()
        self.loop.create_task(web_serve(self))

    async def on_ready(self):
        await self.ensure_emojis()
        st = _store()
        regs = ", ".join(f"{r.config.name}({len(r.members)}m/{len(r.all_characters())}c)" for r in self.registries.by_discord.values())
        print(f"oibot_GM online as {self.user} · mock data: {self.ctx.guild['name']} · registries: {regs} · data: {st.root} @ {st.head()} (push {'on' if st.push_enabled else 'off'}) · llm: {self.ctx.provider.name if self.ctx.provider else 'off'} · events loaded: {len(self.events)} · ledger {len(self.ctx.ledger)} · precedents {len(self.ctx.precedents)}")
        for reg in self.registries.by_discord.values():
            await self.ops.emit(reg.config, "info", f"bot online · data @ {st.head()} · {len(reg.members)} members / {len(reg.all_characters())} characters · {len(reg.pending())} unconfirmed")
            if reg.config.analytics_channel_id and not getattr(self, "_pool_booted", False):
                await self.refresh_pool(reg)  # state may have changed while offline (PRs to the data repo)
        self._pool_booted = True


def run(guild_dir: Path, signup_file: str) -> None:
    global STORE, GUILD_KEY  # GUILD_KEY: the mock fixtures' guild (sessions carry their own `guild`)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing from .env")
    test_guild = int(os.environ["DISCORD_TEST_GUILD_ID"]) if os.environ.get("DISCORD_TEST_GUILD_ID") else None
    guild_dir = guild_dir.resolve()
    STORE = GitStore(guild_dir.parent)
    GUILD_KEY = guild_dir.name
    ctx = GuildContext(guild_dir, signup_file)
    # discord.py reports view/modal callback failures through logging; keep them in the log file
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    OibotGM(ctx, test_guild).run(token, log_handler=None)
