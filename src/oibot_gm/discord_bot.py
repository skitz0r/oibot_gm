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
import logging
import os
import re
import time
from io import BytesIO
from pathlib import Path

import discord
import yaml
from discord import app_commands

from . import configops, guild_routing, nl, render
from .discord_feed import FeedMixin
from .discord_loot import LootMixin
from .discord_policy import CfgApplyButton, PolicyContext, handle_change, left_server, register_policy_commands
from .discord_help import ACT as HELP_ACT, GuideSelect, HelpMixin, apply_view as help_apply_view, guide_intro, guide_view, register_help_commands
from .discord_pool import PoolMixin
from .feed import FeedServer, authorize as feed_authorize, feed_config, guild_tokens
from .discord_pool import AbsenceButton, AbsencesMixin, SetupMixin
from .discord_news import NewsMixin, ProposalButton
from .discord_raid import RaidContext, RaidMixin
from .raid_buttons import FillButton, PlaceButton, RunButton, SignupButton
from .raid_commands import register_raid_commands
from .raid_scheduler import RaidSchedulerMixin
from .discord_registry import ApplicationButton, Guilds, RegisterButton, is_officer, is_owner, register_commands
from .importers import biscouncil, signup as signup_mod, wcl
from .ops import Ops
from .llm.provider import Provider, get_provider
from .loot import session as loot_session
from .loot.session import DISENCHANT, LootChanged, LootSession, Proposal, table_signature  # noqa: F401 — re-exported (old import sites)
from .models import DropResult, LootAward, Player, RosterResult
from .profiles import GameProfile, Item
from .registry import RegistryError
from .roster import coverage as cov_mod, explain, solver
from .store import GitStore, resolve_data_root

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out"
log = logging.getLogger(__name__)
STORE: GitStore | None = None  # set in run(); None = fixtures-only mode (no git)
# The mock fixtures' guild key: the directory name of the guild dir the bot was started on (run() sets it from
# the real path; before that, OIBOT_GUILD_DIR or the public demo fixtures). Never a real guild's name in code.
GUILD_KEY = Path(os.environ.get("OIBOT_GUILD_DIR") or "fixtures/demo").name


def _store() -> GitStore:
    assert STORE is not None, "store not initialised"
    return STORE

CLASS_COLOURS = {"Warrior": 0xC79C6E, "Paladin": 0xF58CBA, "Hunter": 0xABD473, "Rogue": 0xFFF569, "Priest": 0xFFFFFF, "Shaman": 0x0070DE, "Mage": 0x69CCF0, "Warlock": 0x9482C9, "Druid": 0xFF7D0A}
from .constants import ROLES, TEAL  # noqa: E402
MEMBER_BLIP_RETRY_S = 15  # cached_member: a transient fetch failure is retried after this, never cached as a miss
ROLE_ICON = {"tank": "🛡", "healer": "✚", "melee": "⚔", "ranged": "🏹"}
# Filled at startup with application emojis (class_<name>, role_<name>); falls back to text.
EMOJI: dict[str, str] = {}
ICON_EMOJI: dict[str, str] = {}  # Blizzard icon file name -> "<:name:id>" (real class/spec/role/buff art as app emojis)
ICON_NAMES: dict[str, dict[str, str]] = {"class": {}, "role": {}, "spec": {}, "buff": {}}  # kind -> key -> icon file name


def ico(kind: str, key: str) -> str:
    """Emoji for a class / role / spec ('Class:Spec') / buff id: the real icon when uploaded, else the generated badge."""
    name = ICON_NAMES.get(kind, {}).get(key)
    if name and name in ICON_EMOJI:
        return ICON_EMOJI[name]
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

# The loot session lives in loot/session.py (keyed (guild key, run key), drop ids); `MockEvent` is its old name, kept
# while /mock lives (D11 retires it). Its verbs are discord_loot.LootMixin.
MockEvent = LootSession


def load_events() -> dict:
    """Live loot sessions from every guild directory in the data repo, keyed (guild key, session id)."""
    return loot_session.load_sessions(_store())


# ---------------------------------------------------------------- rendering

def signup_embed(ev: MockEvent, ctx: GuildContext) -> discord.Embed:
    active = ev.active_signups()
    signed = [p for p in active if p.status == "signed"]
    bench = [p for p in active if p.status == "bench"]
    counts = {r: sum(1 for p in signed if p.role == r) for r in ROLES}
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


def wowhead(item, profile: GameProfile | None = None) -> str | None:
    """A link to the item on the profile's database site (`integrations.yaml: wowhead_item_url`, `{id}` filled), or
    None: an item known only by name, or a profile with no such site (WoW: Forever until it has one)."""
    if not item or not getattr(item, "id", 0):
        return None
    root = getattr(profile, "root", None)
    tmpl = None
    if root is not None:
        f = Path(root) / "integrations.yaml"
        if f.exists():
            tmpl = (yaml.safe_load(f.read_text()) or {}).get("wowhead_item_url")
    return tmpl.format(id=item.id) if tmpl else None


def candidate_block(r: DropResult, limit: int = 7) -> str:
    rows = []
    for i, c in enumerate(r.candidates[:limit], 1):
        flag = "⚠" if c.unmapped else ""
        base = "HAS" if c.already_has else f"{c.base_score:.2f}"
        wl = f"#{c.wishlist_rank}" if c.wishlist_rank else "-"
        rows.append(f"{ico('class', c.cls)} **{c.character}**{flag} {c.spec} · {c.tier} · att {c.attendance_str} · wl {wl} · 14d {c.recent_power:.2f} · **{base}**")
    return "\n".join(rows)


def drop_embed(p: Proposal, item=None, profile: GameProfile | None = None) -> discord.Embed:
    r, rec = p.result, p.result.recommendation
    colour = CLASS_COLOURS.get(next((c.cls for c in r.candidates if c.character == p.to), ""), 0x5865F2)
    desc = f"**{p.boss}**" + (f" · {item.slot.replace('_', ' ')} · {item.type}" if item is not None and item.slot != "unknown" else "")
    if not p.tiered:
        desc += "\nNo tier data for this item — everyone rostered is a candidate, ranked by attendance and recent loot only."
    e = discord.Embed(title=p.item_name, url=wowhead(item, profile), description=desc, colour=colour)
    e.add_field(name="Candidates (tier · attendance · wishlist · 14d loot power · base)", value=candidate_block(r) or "nobody", inline=False)
    if p.override:
        e.add_field(name=f"Council award → **{p.to}** (pending until Confirm)", value=f"Overrides bot pick {p.award_to}: “{p.override.reason}” — {p.override.by}", inline=False)
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
            if p.override:
                flags.append(f"override by {p.override.by}: {p.override.reason}")
            if p.order_sensitive:
                flags.append("order-sensitive")
            if p.result.recommendation.close_call and not p.override:
                flags.append("close call")
            lines.append(f"`#{p.order:>2}` {p.item_name} → **{p.to}**" + (f" _({'; '.join(flags)})_" if flags else ""))
    e.description = "\n".join(lines)[:4000]
    sens = sum(1 for p in ev.proposals if p.order_sensitive)
    e.set_footer(text=f"# = judging order (impact first) · {sens} order-sensitive · “Details for…” shows the full card · reply here to adjust · Confirm to record")
    return e


def by_character_embed(ev: MockEvent, ctx: GuildContext, proposals: list[Proposal] | None = None, title: str = "By character") -> discord.Embed:
    ps = proposals if proposals is not None else ev.proposals
    by_char: dict[str, list[Proposal]] = {}
    for p in ps:
        by_char.setdefault(p.to, []).append(p)
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
        opts = [discord.SelectOption(label=f"{p.item_name} → {p.to}"[:100], value=p.drop_id, description=f"{p.boss} · judged #{p.order}"[:100]) for p in ev.proposals][:25]
        super().__init__(placeholder="Details for…", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        p = self.ev.pending(self.values[0])
        if not p:
            await interaction.response.send_message("That proposal is gone (confirmed or discarded).", ephemeral=True)
            return
        ctx = self.bot.loot_ctx(self.ev)  # the session's own context: a real guild's items, never the fixtures'
        await interaction.response.send_message(embed=drop_embed(p, ctx.item(p.item_id or None, p.item_name), ctx.profile), ephemeral=True)


def proposal_text(ev: MockEvent) -> str:
    """Compact digest for the feedback parser (~80 tokens/item, not the full cards)."""
    out = []
    for p in ev.proposals:
        r = p.result
        top = "; ".join(f"{c.character} ({c.cls[:3]} {c.spec[:5]}, {c.tier}, {c.base_score:.2f}{', has it' if c.already_has else ''})" for c in r.candidates[:4])
        why = r.recommendation.justification.split(". ")[0][:160]
        out.append(f"- item_id {p.item_id} {p.item_name} [{p.boss}] → {p.to} [{p.source}{': ' + p.reason if p.reason else ''}] · top: {top} · why: {why}")
    return "\n".join(out)


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
    states = {}
    for d in ev.drop_list:
        if d.boss == boss:
            states.setdefault(d.item_id, []).append(ev.drop_state(d))
    e = discord.Embed(title=boss, colour=TEAL)
    lines = []
    for it in items:
        st = states.get(it.id, [])
        mark = "✅" if st and all(x == "awarded" for x in st) else ("🟡" if st else "▫️")
        url = wowhead(it, bot.loot_ctx(ev).profile)
        lines.append(f"{mark} " + (f"[{it.name}]({url})" if url else it.name) + f" · {it.slot.replace('_', ' ')}" + (f" ×{len(st)}" if len(st) > 1 else ""))
    e.description = "\n".join(lines)[:4000]
    if states:
        e.set_footer(text=f"{sum(len(v) for v in states.values())} ticked · 🟡 awaiting distribution · ✅ awarded")
    else:
        e.set_footer(text="Select what dropped ↓")
    return e


class DropsSelect(discord.ui.Select):
    """Per-boss multi-select: the selection is a diff against the session's ticked drops, applied with LootMixin.tick
    (un-ticking an awarded drop is refused there)."""

    def __init__(self, bot: "OibotGM", ev: MockEvent, boss: str, items: list[Item]):
        self.bot, self.ev, self.boss, self.items = bot, ev, boss, items
        chosen = {d.item_id for d in ev.drop_list if d.boss == boss}
        opts = [discord.SelectOption(label=it.name[:100], value=str(it.id), description=f"{it.slot.replace('_', ' ')} · {it.type}"[:100], default=it.id in chosen) for it in items][:25]
        super().__init__(placeholder=f"What dropped from {boss}?"[:150], options=opts, min_values=0, max_values=len(opts))

    async def callback(self, interaction: discord.Interaction):
        if not await self.bot.gate(interaction):
            return
        want = {int(v) for v in self.values}
        mine = [d for d in self.ev.drop_list if d.boss == self.boss]
        have = {d.item_id for d in mine}
        add = [{"item_id": i, "boss": self.boss} for i in sorted(want - have, key=lambda i: [it.id for it in self.items].index(i))]
        remove = [d.drop_id for d in mine if d.item_id not in want and self.ev.drop_state(d) != "awarded"]  # never un-award via the menu
        try:
            await self.bot.tick(self.bot.loot_reg(self.ev), self.ev, add=add, remove=remove, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await interaction.response.send_message(configops.user_error(e), ephemeral=True)
            return
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
        await self.bot.distribute_ui(self.ev, interaction)


class ConfirmView(discord.ui.View):
    """Confirm / Discard the proposals as shown: both carry the batch_rev of what the presser saw (a stale press is
    refused with what changed)."""

    def __init__(self, bot: "OibotGM", ev: MockEvent):
        super().__init__(timeout=None)
        self.bot, self.ev = bot, ev
        self.rev = loot_session.batch_rev(ev)
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
        try:
            await self.bot.confirm(self.bot.loot_reg(self.ev), self.ev, batch_rev=self.rev, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await interaction.response.send_message(configops.user_error(e), ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Recorded {len(confirmed)} awards. Select more drops above as bosses die, then distribute again.", embed=by_character_embed(self.ev, self.bot.ctx, confirmed, "Awarded"), view=DistributeView(self.bot, self.ev))
        self.stop()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary, row=1)
    async def discard(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self.bot.gate(interaction):
            return
        try:
            line = await self.bot.discard(self.bot.loot_reg(self.ev), self.ev, batch_rev=self.rev, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await interaction.response.send_message(configops.user_error(e), ephemeral=True)
            return
        await interaction.response.send_message(line, view=DistributeView(self.bot, self.ev))
        self.stop()


# ---------------------------------------------------------------- bot

class OibotGM(FeedMixin, LootMixin, RaidMixin, RaidSchedulerMixin, PoolMixin, AbsencesMixin, SetupMixin, HelpMixin, NewsMixin, discord.Client):
    ico = staticmethod(ico)

    async def post_application(self, reg, a) -> str:
        """The application review card, for /apply and the apply wizard (one implementation)."""
        from .discord_registry import post_application

        return await post_application(self, reg, a)

    async def decide_application(self, reg, discord_id: int, accept: bool, by: str, note: str | None) -> str:
        """Accept or decline an open application: registry decide, DM the applicant, update the review card, ops line.
        The one path for the card's buttons, /roster applicant, the web and MCP. Returns the verdict line; raises
        RegistryError when there is no open application. Never removes anyone from the server."""
        from .discord_registry import settle_application

        return await settle_application(self, reg, discord_id, accept, by, note)

    async def refresh_registration_card(self, reg) -> bool:
        """The registration card edited in place after a recruitment change (and given the Apply button)."""
        from .discord_registry import refresh_registration_card

        return await refresh_registration_card(self, reg)

    async def close(self) -> None:
        """Shutdown: push any unpushed data-repo commits, stop the feed listener, then disconnect. Every step is
        bounded, so a stopping bot always lets go of its ports (launchd waits ExitTimeOut, then kills it)."""
        try:
            if STORE is not None:
                await asyncio.wait_for(asyncio.to_thread(STORE.flush), timeout=20)
        except Exception as e:  # noqa: BLE001 — the commits stay local; the next start pushes them
            log.warning("shutdown push skipped: %s", type(e).__name__)
        try:
            if self.feed is not None:
                await asyncio.wait_for(self.feed.stop(), timeout=5)
        except Exception as e:  # noqa: BLE001
            log.warning("feed listener stop: %s", type(e).__name__)
        finally:
            await super().close()

    def __init__(self, ctx: GuildContext, test_guild: int | None):
        intents = discord.Intents.default()
        intents.message_content = True  # channel chat → roster changes / loot feedback
        # member text and names are data: never let them ping @everyone / roles through the bot
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.ctx = ctx
        self.tree = app_commands.CommandTree(self)
        self.test_guild = test_guild
        self.events: dict[tuple[str, str], MockEvent] = load_events()  # loot sessions by (guild key, session id)
        self.contexts: dict = {}  # guild key -> RegistryLootContext (lazy, one per guild; reload() after an import)
        for s in self.events.values():
            self.loot_index(s)  # + the by-thread map event_for reads
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
        self._check_descriptions()

    def _check_descriptions(self) -> None:
        """Discord rejects the whole sync when one description exceeds 100 chars; fail here with the name instead."""
        bad = []

        def walk(cmd, path=""):
            name = f"{path} {cmd.name}".strip()
            subs = getattr(cmd, "commands", None)
            if subs:
                for c in subs:
                    walk(c, name)
            elif len(cmd.description or "") > 100:
                bad.append(f"/{name} ({len(cmd.description)})")

        for c in self.tree.get_commands():
            walk(c)
        if bad:
            raise SystemExit("command descriptions over 100 chars: " + ", ".join(bad))
        self.add_dynamic_items(SignupButton, FillButton, PlaceButton, RunButton, RegisterButton, ApplicationButton, GuideSelect, AbsenceButton, ProposalButton, CfgApplyButton)
        self.tree.on_error = self._on_command_error

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # the guild the command resolved to (need() tags it), else the server's own (strict), else — a DM that failed before
        # need() ran — the only guild, as with one guild before; with several, the log only. Never a guess, never a prompt.
        # After the DM server picker (guild_routing.pick) the original carries the chosen key AND the press: the apology
        # answers the press, which the command was answering (else Discord shows "This interaction failed" on it).
        extras = getattr(interaction, "extras", None) or {}
        key = extras.get("guild")
        target = extras.get("press") or interaction
        if key:
            reg = self.registries.by_key.get(key)
        elif interaction.guild_id:
            reg = self.registries.for_guild(interaction.guild_id)
        else:
            reg = self.registries.only()
        cmd = interaction.command.qualified_name if interaction.command else "?"
        msg = f"`/{cmd}` by {interaction.user.display_name}: {type(error).__name__}: {str(error)[:200]}"
        if reg:
            await self.ops.emit(reg.config, "error", msg, exc=error)
        else:
            print("[ops:error]", msg)
        try:
            text = "Something went wrong; the owner has been notified." if reg else "Something went wrong; it has been logged."
            if target.response.is_done():
                await target.followup.send(text, ephemeral=True)
            else:
                await target.response.send_message(text, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    # ---- helpers (loot_ctx, event_for, loot_reg … are LootMixin's)
    def loot_reg(self, session):
        """A /mock session has no registry (its context is the fixtures'); a real one has its guild's."""
        if getattr(session, "origin", "raid") != "raid":
            return None
        return LootMixin.loot_reg(self, session)

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
        for reg in self.registries.ordered() + [self.ctx]:
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
        await self.ensure_icon_emojis(existing)

    async def ensure_icon_emojis(self, existing: dict) -> None:
        """The profile's Blizzard icons (classes, roles, specs, buff art) as app emojis so sheets and cards show real art."""
        import httpx

        icon_dir = Path(__file__).resolve().parents[2] / "out" / "icons"
        icon_dir.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, str] = {}
        for reg in self.registries.ordered():
            ic = reg.profile.icons
            for kind, key_map in (("class", ic.get("classes", {})), ("role", ic.get("roles", {})), ("spec", ic.get("specs", {}))):
                for key, name in key_map.items():
                    ICON_NAMES[kind][key] = name
                    wanted[name] = name
            for b in reg.profile.buffs:
                if b.art:
                    ICON_NAMES["buff"][b.id] = b.art
                    wanted[b.art] = b.art
        n_new = 0
        async with httpx.AsyncClient(timeout=10) as hc:
            for name in wanted:
                ename = ("ic_" + name)[:32]
                em = existing.get(ename)
                if em is None:
                    f = icon_dir / f"{name}.jpg"
                    if not f.exists():
                        try:
                            r = await hc.get(f"https://render.worldofwarcraft.com/us/icons/56/{name}.jpg")
                            if r.status_code != 200:
                                continue
                            f.write_bytes(r.content)
                        except httpx.HTTPError:
                            continue
                    try:
                        em = await self.create_application_emoji(name=ename, image=f.read_bytes())
                        n_new += 1
                    except Exception as e:  # noqa: BLE001
                        print(f"icon emoji {ename}: {e}")
                        continue
                ICON_EMOJI[name] = str(em)
        print(f"icon emojis ready: {len(ICON_EMOJI)} ({n_new} uploaded)")

    async def distribute_ui(self, ev: MockEvent, interaction: discord.Interaction):
        """The Distribute button / command: the verb (LootMixin.distribute) with a ticker, then the proposals."""
        reg = self.loot_reg(ev)
        pending = ev.undistributed()
        if not pending:
            await interaction.response.send_message("Nothing ticked that hasn't been proposed or awarded yet — tick drops on the boss messages first.", ephemeral=True)
            return
        names = ", ".join(d.item_name for d in pending)
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
            res = await self.distribute(reg, ev, by=interaction.user.display_name, by_id=interaction.user.id, progress=progress)
        except RegistryError as e:
            await status.edit(content=configops.user_error(e)[:1900])
            return
        finally:
            t.cancel()
        await status.edit(content=f"⚖️ Distributed **{len(res['proposals'])}** items — proposals below." + (f" {len(res['stale'])} changed while judging; distribute again." if res["stale"] else ""))
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

    async def end_mock_session(self, ev: MockEvent, interaction: discord.Interaction) -> None:
        """/mock end: the summary, then the session is archived (a real council ends through LootMixin.end_run)."""
        summary = self.loot_summary(None, ev)
        e = discord.Embed(title=f"Raid summary · {ev.raid_name} · {ev.label or ev.id}", colour=TEAL, description=summary["text"][:4000])
        if ev.roster_log:
            e.add_field(name="Roster history", value="\n".join(f"• {l}" for l in ev.roster_log[-8:])[:1000], inline=False)
        ctx = self.loot_ctx(ev)
        if ctx.provider:
            e.add_field(name="LLM usage (this bot process)", value=ctx.provider.summary()[:1000], inline=False)
        e.set_footer(text=f"data repo {_store().head()}")
        ev.state = "ended"
        ev.archive(summary)
        self.loot_forget(ev)
        await interaction.response.send_message(embed=e)

    async def cached_member(self, guild, uid: int, max_age: float = 300):
        """A guild member by id without the privileged members intent: the gateway cache first, else one REST
        fetch per (guild, uid) per `max_age` seconds — misses (left the server, 404) are cached too, so a
        page that polls or a scanner can't turn into a stream of fetches."""
        if guild is None:
            return None
        m = guild.get_member(uid)
        if m is not None:
            return m
        cache: dict[tuple[int, int], tuple[float, object]] = self.__dict__.setdefault("_member_cache", {})
        key = (guild.id, uid)
        hit = cache.get(key)
        now = time.time()
        if hit and now - hit[0] < max_age:
            return hit[1]
        gone: set[tuple[int, int]] = self.__dict__.setdefault("_member_gone", set())  # misses that were Discord's own 404
        # a fetch Discord didn't answer (5xx, rate limit, network) is NOT an answer: it is never cached as "not a
        # member" (that would read as 'unknown' — a 503 on the web — for max_age); it is only throttled for a few seconds
        blips: dict[tuple[int, int], float] = self.__dict__.setdefault("_member_blip", {})
        b = blips.get(key)
        if b is not None and now - b < min(max_age, MEMBER_BLIP_RETRY_S):
            return None
        try:
            m = await guild.fetch_member(uid)
        except Exception as e:  # noqa: BLE001 — not a member (404) or transient
            if not isinstance(e, discord.NotFound):
                gone.discard(key)
                if len(blips) > 4096:
                    blips.clear()
                blips[key] = now
                return None
            m = None
            gone.add(key)
        else:
            gone.discard(key)
        blips.pop(key, None)
        if len(cache) > 4096:  # bounded: drop what has aged out
            for k in [k for k, (t, _) in cache.items() if now - t >= max_age]:
                cache.pop(k, None)
                gone.discard(k)
        cache[key] = (now, m)
        return m

    def saw_member(self, guild_id: int | None, member) -> None:
        """Evidence beats the cache: a message or an interaction someone made inside a server proves they are in it now,
        so a cached 404 ("left", or "not yet joined") for that server is dropped at once — DM routing (guilds_of) and the
        web read the fresh answer instead of waiting out max_age."""
        uid = getattr(member, "id", None)
        if not guild_id or uid is None:
            return
        key = (guild_id, uid)
        self.__dict__.setdefault("_member_gone", set()).discard(key)
        cache = self.__dict__.setdefault("_member_cache", {})
        if isinstance(member, discord.Member):
            cache[key] = (time.time(), member)
        else:
            cache.pop(key, None)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id:  # a listener only: the tree and the views answer the interaction itself
            self.saw_member(interaction.guild_id, interaction.user)

    # ---- several guilds (design §5.32)
    async def claim_guild(self, guild, owner_id: int, profile: str, by) -> "object":
        """Make an unconfigured Discord server a guild (the claim wizard's Confirm): the folder and its commit in a
        worker thread, then served ON THE LOOP (copy-on-write add), hooked up like boot (guild_added), announced to the
        owners of every other guild. Raises RegistryError when the server is configured or a claim is in flight."""
        from .registry import RegistryError

        if guild is None:
            raise RegistryError("Invite the bot to this server first.")
        reg = await asyncio.to_thread(self.registries.create, guild, owner_id, profile, (self.ctx.guild_key,))
        self.registries.add(reg)
        await self.guild_added(reg)
        who = getattr(by, "display_name", None) or str(by)
        line = f"guild created for {guild.name} by {who}; owner <@{owner_id}> · profile {reg.profile.label}"
        await self.ops.emit(reg.config, "warn", line)  # no ops channel yet: the log (and the new guild's ring)
        told: set[int] = set()
        for other in self.registries.ordered():  # the bot host learns at once that a new server is served (K5)
            oid = other.config.owner_discord_id
            if other is reg or not oid or oid in told:
                continue
            told.add(oid)
            try:
                user = self.get_user(oid) or await self.fetch_user(oid)
                await user.send(f"🆕 The bot now also serves **{discord.utils.escape_markdown(str(guild.name))}** — claimed by {discord.utils.escape_markdown(who)}, "
                                f"owner <@{owner_id}>. It shares this bot's LLM budget. If you didn't expect this, check that Public Bot is off in the Discord Developer Portal.")
            except Exception as e:  # noqa: BLE001 — closed DMs: the log line stays
                log.warning("claim DM to owner %s failed: %s", oid, e)
        return reg

    async def guild_added(self, reg) -> None:
        """Hook a guild claimed at run time up the way boot does: analytics listener, officer roles, guild-scoped command
        sync (development mode), and the icons of a game profile no loaded guild used before. The raid/policy stores
        are lazy and the web reads Guilds live, so nothing restarts."""
        self.attach_pool_listener(reg)
        await self.resolve_officer_roles(reg)
        if os.environ.get("OIBOT_COMMAND_SCOPE", "global") == "guild":
            try:
                obj = discord.Object(id=int(reg.config.discord_guild_id))
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
            except Exception as e:  # noqa: BLE001 — the claim stands; a restart syncs it
                log.warning("command sync for %s failed: %s", reg.key, e)
        if reg.profile.name not in {r.profile.name for r in self.registries.ordered() if r is not reg}:
            try:
                existing = {e.name: e for e in await self.fetch_application_emojis()}
                await self.ensure_icon_emojis(existing)
                self.check_emoji_conflicts()
            except Exception as e:  # noqa: BLE001 — icons fall back to the generated badges
                log.warning("icon emojis for %s: %s", reg.profile.name, e)

    def check_emoji_conflicts(self) -> list[str]:
        """App emoji names are shared by every loaded profile: warn when two profiles map one icon name to different art."""
        seen: dict[tuple[str, str], tuple[str, str]] = {}
        out = []
        for reg in self.registries.ordered():
            ic = reg.profile.icons
            pairs = [(kind, key, name) for kind, km in (("class", ic.get("classes", {})), ("role", ic.get("roles", {})), ("spec", ic.get("specs", {}))) for key, name in km.items()]
            pairs += [("buff", b.id, b.art) for b in reg.profile.buffs if b.art]
            for kind, key, name in pairs:
                prev = seen.setdefault((kind, key), (name, reg.profile.name))
                if prev[0] != name and prev[1] != reg.profile.name:
                    out.append(f"{kind} {key}: {prev[1]} uses {prev[0]}, {reg.profile.name} uses {name}")
        for line in out:
            log.warning("emoji conflict between profiles — %s", line)
        return out

    async def dm_guild(self, message: discord.Message):
        """Which guild a plain DM is about: the only one; else the one the sender belongs to; in several, the server
        picker (buttons, asked every time — owner decision D2), then the chosen one; None after saying why (in none:
        every server's Join link) or when nobody picked in time. Only the guilds that answer DMs count (plain-text
        policy `dm` not "ignore"): a guild whose owner switched DMs off is never offered, and when every guild has,
        a DM gets silence — as one guild with DMs off always has."""
        import types as _types

        from . import guild_routing
        from .guild_picker import choose_guild

        only = self.registries.only()
        if only is not None:
            return only  # one guild: plain_text applies its DM mode, as before
        regs = self.registries.ordered()
        answering = [r for r in regs if r.plain_mode(None) != "ignore"]
        if not answering:
            return None
        mine = await guild_routing.guilds_of(self, message.author.id, _types.SimpleNamespace(ordered=lambda: answering))
        if len(mine) == 1:
            return mine[0]
        if mine:
            reg, _press = await choose_guild(message, mine)
            return reg
        await message.reply(guild_routing.not_in_any(regs), allowed_mentions=discord.AllowedMentions.none())
        return None

    # ---- chat handlers
    def officiates(self, user, guild, mock: bool = False) -> bool:
        """Officer check for buttons and chat: owner, Manage Server, or one of the configured officer roles (matched by
        role id — `GuildConfig.officer_by_roles`; legacy names only until `resolve_officer_roles` has run). Manage
        Server counts only in a configured server (design §14 K24): an unconfigured server's admin is nobody's officer.
        `mock`: the /mock shadow sessions keep their old rule (Manage Server anywhere)."""
        reg = self.registries.for_guild(guild.id) if guild else None
        if reg and reg.is_owner_id(user.id):
            return True
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.manage_guild and (reg is not None or mock):
            return True
        return bool(reg and reg.config.officer_by_roles(user.roles))

    def officer_role_names(self, reg) -> list[str]:
        """The configured officer roles as names (resolved against the live guild when it is available)."""
        g = self.get_guild(reg.config.discord_guild_id)
        return reg.resolve_officer_roles(g) if g else reg.officer_role_names()

    async def resolve_officer_roles(self, reg=None) -> None:
        """Turn legacy officer role names into role ids against the live guild (one commit) and refresh the name cache;
        on ready and whenever a role is edited so a rename never drops an officer."""
        for r in ([reg] if reg else self.registries.ordered()):
            g = self.get_guild(r.config.discord_guild_id)
            if not g:
                continue
            before = list(r.config.officer_roles)
            try:
                names = await asyncio.to_thread(r.resolve_officer_roles, g)
            except Exception:  # noqa: BLE001 — a failed commit must not stop the bot from starting
                import logging

                logging.getLogger(__name__).exception("officer role resolution failed for %s", r.key)
                continue
            if before and before != r.config.officer_roles:
                await self.ops.emit(r.config, "info", f"officer roles now stored by role id: {', '.join(names) or '(none)'}")
            if r.config.officer_roles:
                await self.ops.emit(r.config, "warn", f"officer role name(s) not found in the server: {', '.join(r.config.officer_roles)} — re-add with /gm config officer-role")

    async def on_guild_role_update(self, before, after):
        reg = self.registries.by_discord.get(after.guild.id)
        if reg:
            await self.resolve_officer_roles(reg)

    async def on_guild_role_delete(self, role):
        reg = self.registries.by_discord.get(role.guild.id)
        if reg:
            await self.resolve_officer_roles(reg)

    async def on_guild_role_create(self, role):
        reg = self.registries.by_discord.get(role.guild.id)
        if reg and reg.config.officer_roles:  # a legacy name may now resolve
            await self.resolve_officer_roles(reg)

    async def gate(self, interaction: discord.Interaction) -> bool:
        """Refuse non-officers on the /mock decision buttons; returns True when the press may proceed."""
        if self.officiates(interaction.user, interaction.guild, mock=True):
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

    # ---- identity (dual interfaces, phase 0 — design §2.6): guild_routing.resolve_role is THE predicate; these are its names
    async def role_of(self, reg, user_or_uid, *, max_age: float = guild_routing.PRIV_ROUTING) -> str:
        """owner | officer | member | outside for a Member (live from its roles), a bare User or a Discord id (the cached
        lookup at most `max_age` old: PRIV_LIVE for writes, PRIV_ROUTING for presses, PRIV_WEB for the web)."""
        return await guild_routing.role_of(self, reg, user_or_uid, max_age=max_age)

    async def identity(self, reg, user, *, max_age: float = guild_routing.PRIV_ROUTING) -> guild_routing.Resolved:
        """(role, role_ids) — what Registry.may_plain needs (unpacks as a pair; `.stale` says the cache answered)."""
        return await guild_routing.identity(self, reg, user, max_age=max_age)

    async def plain_identity(self, reg, user, *, max_age: float = guild_routing.PRIV_ROUTING) -> tuple[bool, list[int]]:
        """(officer, role ids) for the plain-text check (Registry.may_plain) — an alias of `identity`: a bare User from a
        DM is looked up as the guild member (Discord hands over no roles there). A write passes `max_age=PRIV_LIVE`
        (ConfigConfirmView's Apply): the answer is fetched now, never the cache's."""
        r = await self.identity(reg, user, max_age=max_age)
        return r.officer, r.role_ids

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """A nick change in a configured server (needs the members intent to arrive): the registered member's display
        name follows (guild_routing.sync_name → Registry.rename, the one rename path). Seats, callouts and asks are by
        id, so nothing else moves and no run is relabelled."""
        reg = self.registries.by_discord.get(after.guild.id) if getattr(after, "guild", None) is not None else None
        if reg is not None:
            guild_routing.sync_name(reg, after)

    async def in_guild(self, reg, user) -> bool:
        """Whether `user` is a member of the guild's Discord server now (the gateway cache, else one cached fetch: a
        404 is remembered). The web treats server membership as the whitelist; plain text does too (discord_policy.left_server).
        Fails open: False only on Discord's own Unknown Member (404) — a guild missing from the gateway cache or a
        transient fetch error (a 5xx, a timeout) is not a departure, so a member is never locked out by an outage."""
        guild = self.get_guild(reg.config.discord_guild_id)
        if guild is None:
            return True
        if isinstance(user, discord.Member) and user.guild.id == guild.id:
            return True
        if await self.cached_member(guild, user.id) is not None:
            return True
        return (guild.id, user.id) not in self.__dict__.get("_member_gone", set())

    async def plain_text(self, message: discord.Message, reg, text: str, channel) -> None:
        """An @mention or a DM, routed by intent under the guild's plain-text policy (design §5.25).
        An "act" channel (ops/analytics by default): configops.parse first — a change becomes the diff + Apply, a
        question gets the parser's own reply from the live config (one model call either way). Elsewhere (DMs and
        other channels): the help answer first, which also says whether the message asks the bot to DO something;
        only then, and only if the person could act here, is it parsed into a change — a plain question costs one call,
        a change two. Outside the ask audience a person who may act on their own record is parsed directly. A newcomer
        who must apply (Registry.must_apply) while applications are open gets the persistent Apply to join button on the
        reply, so the application wizard opens straight from the chat (typed class/spec is never parsed)."""
        mode = reg.plain_mode(channel)
        if mode == "ignore":
            return
        uid = message.author.id
        quiet = discord.AllowedMentions.none()
        if await left_server(self, reg, message.author):  # a DM from someone no longer in the server: unregistered, whatever the registry holds
            await message.reply(guide_intro(reg), view=guide_view(reg), allowed_mentions=quiet)
            return
        officer, role_ids = await self.plain_identity(reg, message.author)
        provider, ps = self.ctx.provider, self.policies.store(reg)
        if mode == "act" and provider is not None:
            async with message.channel.typing():
                await handle_change(message, reg, ps, provider, self.ops, text, bot=self, channel=channel)
            return
        # the snapshot and the parser context follow who can READ the reply: an officer asking where members read it
        # gets the member view (help_answer adds where the officer view is)
        public = not self.reply_private(reg, message.channel if message.guild else None)
        owner = reg.is_owner_id(uid)
        may_act = provider is not None and (owner or (mode == "self" and reg.may_plain("self", uid, officer=officer, role_ids=role_ids, channel=channel, fallback="members") is None))

        def apply_button(base=None):  # a newcomer who must apply gets the Apply to join button (the wizard opens from here, DMs too)
            return help_apply_view(reg, uid, officer=officer, role_ids=role_ids, base=base)

        async with message.channel.typing():
            if may_act and not reg.may_ask(uid, officer):
                req = await asyncio.to_thread(configops.parse, provider, reg, text, reg.members[uid].display_name if uid in reg.members else message.author.display_name, officer=officer and not public)
                if req.kind == "change" and req.ops:
                    await handle_change(message, reg, ps, provider, self.ops, text, bot=self, channel=channel, req=req)
                else:
                    await message.reply(guide_intro(reg), view=apply_button(guide_view(reg)), allowed_mentions=quiet)
                return
            answer = await self.help_answer(reg, uid, officer, text, may_act=may_act, public=public)
            if answer == HELP_ACT:
                await handle_change(message, reg, ps, provider, self.ops, text, bot=self, channel=channel)
                return
        if answer is None:  # outside the ask audience: static guide only
            await message.reply(guide_intro(reg), view=apply_button(guide_view(reg)), allowed_mentions=quiet)
        else:
            await message.reply(answer, view=apply_button(), allowed_mentions=quiet)

    async def on_message(self, message: discord.Message):
        if await self.news_message(message):  # the news webhook (a bot) is read before the bot filter below
            return
        if message.author.bot:
            return
        if message.guild:
            self.saw_member(message.guild.id, message.author)
        if not message.guild:  # a DM to the bot: a question, or a change to the sender's own record (plain-text policy "DMs")
            text = message.content.strip()
            if not text:
                return
            reg = await self.dm_guild(message)
            if reg is not None:
                await self.plain_text(message, reg, text, None)
            return
        reg = self.registries.for_guild(message.guild.id)
        # an @mention: routed by intent and by the channel's plain-text mode (the ops and analytics channels act by default;
        # the mock loot/roster channels keep their own chat below)
        if reg and self._addressed(message) and (message.channel.id in (reg.config.ops_channel_id, reg.config.analytics_channel_id) or not self.event_for(message.channel.id)):
            text = re.sub(r"<@[!&]?\d+>", "", message.content).strip()
            if text:
                await self.plain_text(message, reg, text, message.channel.id)
            return
        ev = self.event_for(message.channel.id)
        if not ev or not self.ctx.provider:
            return
        if not self.officiates(message.author, message.guild, mock=True):
            return  # members may chat in the roster/loot channels; only officers steer the bot
        if ev.state == "roster_proposed" and message.channel.id == ev.channel_id and ev.roster:
            async with message.channel.typing():
                try:
                    req = await asyncio.to_thread(nl.parse_roster_request, self.ctx.provider, ev.raid_name, roster_text(ev), message.content, message.author.display_name)
                except Exception as e:  # noqa: BLE001
                    await message.reply(await configops.report_error(self.ops, reg.config if reg else None, e, "roster chat"))  # §2.7: refusals as written, bugs as GENERIC_ERROR + an ops error
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
        elif message.channel.id == ev.raid_thread_id and self.loot_live(ev) and ev.proposals:
            await self.loot_chat(message, ev)

    async def loot_chat(self, message: discord.Message, ev: MockEvent) -> None:
        """An officer's sentence in the loot thread while proposals wait (interim — the Discord surfaces make this
        addressed-only and route writes through the `loot` op): a question is answered; a confirm or an override goes
        through LootMixin.confirm / override (names checked against the candidate tables), then the other picks are
        re-judged once."""
        ctx = self.loot_ctx(ev)
        reg = self.loot_reg(ev)
        cfg = reg.config if reg else (self.registries.for_guild(message.guild.id).config if message.guild and self.registries.for_guild(message.guild.id) else None)
        async with message.channel.typing():
            try:
                fb = await asyncio.to_thread(nl.parse_loot_feedback, ctx.provider, ctx.policy_for_llm(), proposal_text(ev), message.content, message.author.display_name)
            except Exception as e:  # noqa: BLE001
                await message.reply(await configops.report_error(self.ops, cfg, e, "loot chat"))
                return
            if fb.kind == "ignore":
                return
            if fb.kind == "question":
                await message.reply(fb.reply[:1900])
                return
            by, by_id = message.author.display_name, message.author.id
            if fb.kind == "confirm":
                confirmed = list(ev.proposals)
                try:
                    await self.confirm(reg, ev, by=by, by_id=by_id)
                except RegistryError as e:
                    await message.reply(configops.user_error(e))
                    return
                await message.reply(f"✅ Recorded {len(confirmed)} awards. Select more drops above as bosses die, then distribute again.", embed=by_character_embed(ev, ctx, confirmed, "Awarded"), view=DistributeView(self, ev))
                return
            applied, touched = [], set()
            for ch in fb.changes:
                p = next((p for p in ev.proposals if p.item_id == ch.item_id and p.drop_id not in touched), None)
                if not p or ch.award_to not in {c.character for c in p.result.candidates}:
                    applied.append(f"couldn't apply {ch.award_to} ← item {ch.item_id}")
                    continue
                try:
                    await self.override(reg, ev, p.drop_id, ch.award_to, ch.reason, by=by, by_id=by_id, replace=True, rejudge=False)
                except RegistryError as e:
                    applied.append(f"{p.item_name}: {configops.user_error(e)}")
                    continue
                touched.add(p.drop_id)
                applied.append(f"{p.item_name} → {ch.award_to}")
            rejudged = 0
            others = [p.drop_id for p in ev.proposals if not p.override]
            if touched and others:
                try:
                    res = await self.distribute(reg, ev, by=by, by_id=by_id, only=others, rejudge=True)
                    rejudged = len(res["proposals"])
                except RegistryError:
                    pass
            await self.post_proposals(ev, message.channel, prefix=(fb.reply + "\n" if fb.reply else "") + "Applied: " + "; ".join(applied) + (f". Re-scored the rest ({rejudged})." if rejudged else ".") + " Overrides are recorded at Confirm.")

    # ---- commands
    def _register(self):
        bot = self

        class MockGroup(app_commands.Group):
            async def interaction_check(self, interaction: discord.Interaction) -> bool:  # officers only: it spends LLM budget
                if bot.officiates(interaction.user, interaction.guild, mock=True):
                    return True
                await interaction.response.send_message("Officers only.", ephemeral=True)
                return False

        mock = MockGroup(name="mock", description="Officer: mock event on shadow data — signup → roster → raid → loot council")
        raid_choices = [app_commands.Choice(name=r["name"], value=rid) for rid, r in self.ctx.profile.raids.items()]

        @mock.command(name="signup", description="Seed a mock signup sheet for this channel")
        @app_commands.choices(instance=raid_choices)
        async def signup(interaction: discord.Interaction, instance: app_commands.Choice[str], date_: str = ""):
            d = date_ or self.ctx.seed_event["starts_at"][:10]
            ev = MockEvent(id=f"mock-{interaction.channel_id}", label=f"{instance.value.split('_')[0].upper()[:3]}-{d[5:].replace('-', '')}", channel_id=interaction.channel_id, instance=instance.value, date=d, guild=self.ctx.guild_key, signups=copy.deepcopy(self.ctx.seed_players))
            old = self.events.get(ev.key)
            if old is not None:
                old.archive()
            ev.attach(_store())
            self.loot_index(ev)
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
            self.loot_index(ev)
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
            await self.distribute_ui(ev, interaction)

        @mock.command(name="status", description="Where is this channel's mock event?")
        async def status(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev:
                await interaction.response.send_message("No event here.", ephemeral=True)
                return
            spend = self.ctx.provider.summary() if self.ctx.provider else "LLM off"
            await interaction.response.send_message(f"**{ev.id}** {ev.raid_name} · state `{ev.state}` · signups {len(ev.active_signups())} · roster {'yes' if ev.roster else 'no'} · ticked drops {len(ev.drop_list)} (undistributed {len(ev.undistributed())}) · awards {len(ev.awards)} · overrides {len(ev.overrides)}\n{spend}"[:1900], ephemeral=True)

        @mock.command(name="end", description="Summarise the raid's awards and overrides")
        async def end(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if not ev:
                await interaction.response.send_message("No event here.", ephemeral=True)
                return
            await self.end_mock_session(ev, interaction)

        @mock.command(name="reset", description="Forget this channel's mock event (archived, not deleted)")
        async def reset(interaction: discord.Interaction):
            ev = self.event_for(interaction.channel_id)
            if ev:
                self.loot_forget(ev)
                ev.archive()
            await interaction.response.send_message("Reset (event archived). /mock signup to start over.", ephemeral=True)

        self.tree.add_command(mock)

    def sync_servers(self) -> list[int]:
        """The servers a guild-scoped sync covers: the test server and every configured server, each once."""
        ids = ([self.test_guild] if self.test_guild else []) + [int(r.config.discord_guild_id) for r in self.registries.ordered()]
        return list(dict.fromkeys(ids))

    async def setup_hook(self):
        # One scope only, or Discord lists every command twice. Default: global (works in every server and in DMs;
        # new commands can take a while to propagate). OIBOT_COMMAND_SCOPE=guild (development) gives instant updates
        # in the test server and every configured server, but no DM commands — and an unconfigured server then has no
        # commands at all, so it can't be claimed in that mode.
        scope = os.environ.get("OIBOT_COMMAND_SCOPE", "global")
        g = discord.Object(id=self.test_guild) if self.test_guild else None
        if scope == "guild" and (g or self.registries.ordered()):
            for gid in self.sync_servers():
                obj = discord.Object(id=gid)
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
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
        self.attach_loot_listeners()  # a seat change on a run with a council → "now a candidate" (candidates are read live)
        token, bind = feed_config()
        per_guild = guild_tokens()  # OIBOT_FEED_TOKEN_<KEY>: a token that feeds one guild only
        if token or per_guild:
            self.feed = FeedServer(self.handle_feed_event, token, bind,
                                   auth=lambda t: feed_authorize(t, token, per_guild, lambda: list(self.registries.by_key)))
            self.feed.on_disconnect = self.feed_disconnected
            await self.feed.start()
        else:
            print("loot feed disabled (set OIBOT_FEED_TOKEN or OIBOT_FEED_TOKEN_<KEY>, and OIBOT_FEED_BIND)")
        from .web.app import serve as web_serve

        self.started_at = time.time()
        self.loop.create_task(web_serve(self))

    async def on_ready(self):
        await self.ensure_emojis()
        self.check_emoji_conflicts()
        await self.resolve_officer_roles()  # legacy officer role names → ids, name cache for display
        st = _store()
        regs = ", ".join(f"{r.config.name}({len(r.members)}m/{len(r.all_characters())}c)" for r in self.registries.ordered())
        print(f"oibot_GM online as {self.user} · mock data: {self.ctx.guild['name']} · registries: {regs} · data: {st.root} @ {st.head()} (push {'on' if st.push_enabled else 'off'}) · llm: {self.ctx.provider.name if self.ctx.provider else 'off'} · loot sessions: {len(self.events)} · ledger {len(self.ctx.ledger)} · precedents {len(self.ctx.precedents)}")
        for err in self.registries.load_errors:  # a guild that didn't load has no ops channel to tell
            print(f"guild NOT loaded — {err}")
            log.error("guild not loaded: %s", err)
        only = self.registries.only()
        if only is not None:  # K7: the agents' runs written before runs carried a guild are this (the first) guild's
            try:
                from . import agents

                agents.tag_legacy_runs(only.key)
            except OSError as e:
                log.warning("legacy agent runs not tagged: %s", e)
        for reg in self.registries.ordered():
            await self.ops.emit(reg.config, "info", f"bot online · data @ {st.head()} · {len(reg.members)} members / {len(reg.all_characters())} characters · {len(reg.pending())} unconfirmed")
            if reg.config.analytics_channel_id and not getattr(self, "_pool_booted", False):
                await self.refresh_pool(reg)  # state may have changed while offline (PRs to the data repo)
            if not getattr(self, "_pool_booted", False):
                await self.refresh_registration_card(reg)  # the card's text and buttons as this version draws them (Apply to join)
        if not getattr(self, "_loot_booted", False):
            self._loot_booted = True
            for line in await self.loot_restore():  # an interrupted Distribute is reported, never re-run
                log.info("loot restore: %s", line)
        self._pool_booted = True


def run(guild_dir: Path, signup_file: str) -> None:
    global STORE, GUILD_KEY  # GUILD_KEY: the mock fixtures' guild (sessions carry their own `guild`)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing from .env")
    test_guild = int(os.environ["DISCORD_TEST_GUILD_ID"]) if os.environ.get("DISCORD_TEST_GUILD_ID") else None
    guild_dir = guild_dir.resolve()
    STORE = GitStore(guild_dir.parent)
    loot_session.STORE = STORE  # sessions created in this process save through it
    GUILD_KEY = guild_dir.name
    ctx = GuildContext(guild_dir, signup_file)
    # logging is configured by cli.setup_logging (rotating out/oibot.log); discord.py's own handler is off
    OibotGM(ctx, test_guild).run(token, log_handler=None)
