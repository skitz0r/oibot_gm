"""The lookups the answer loop offers the model (answering questions with lookup tools, design §6, §7): the Viewer, the
tool registry and its caps, the members' tools — find, recipe, crafters, price, member, event, schedule — and the
officer-aware ones — loot, attendance, manual, section.

Every tool is a READ and a thin wrapper over a reader the site or the help snapshot already uses (answer_names.resolve,
professions.who_can_make / crafters_of / answer_lines, prices.price_answer / cost_line, help.member_lines / run_lines /
who_line, schedule_view.schedule_sections, loot_ops.log_rows / member_awards, help._loot, Registry.attendance_text,
help.manual_read, help.SECTIONS' builders): nothing here writes, sends or searches on its own. Who asks is fixed in
code — the Viewer, built once with help.guild_state's rule — and is never read from a tool's arguments. Every result is
cut to its cap on whole lines (`cut`), neutralised (`llm.provider.unfence`) and wrapped in one data block (`run_tool`).
Times are guild words (Registry.local12), never a Discord stamp.

Arguments are COERCED, not validated: each handler reads the keys it knows from the parsed dict (`_arg`), so a missing
argument reads as empty, a number as its text and an unknown extra key is ignored; a string is cut to ARG_MAX. Nothing
raises into the loop: run_tool never raises — an unknown lookup or a handler's exception is the TOOL_FAILED result.
A cut list of matches ends "… and N more — narrow it"; a result that is ONE thing (an event, a member's record, one
recipe, a manual part, a section — a handler returns it as `One`) ends "… and N more lines left out." instead."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import help as help_mod
from . import prices
from . import professions
from . import raidcycle as rc
from .answer_names import KINDS, match_lines, people, resolve
from .constants import PAGE_WORDS
from .llm.provider import unfence
from .profiles import GameProfile
from .raid_views import run_title
from .registry import RegistryError

log = logging.getLogger(__name__)

RESULT_CAP = 1_500       # one lookup's result (design §6)
BIG_RESULT_CAP = 4_000   # one manual part, one snapshot section or one event
FIND_RECIPES = 5         # recipes the `recipe` lookup shows for one name
CHARACTER_RECIPES = 40   # a character's recipe names the `crafters` lookup lists before a count
RECIPE_CRAFTERS = 8      # crafters one recipe's line names, highest skill first, before "and N more"
LOOT_ROWS = 15           # awards the `loot` lookup shows, newest first
ARG_MAX = 200            # every string argument, cut in code as the schema says

OFFICERS_ONLY = "Only officers can see this."
TOOL_FAILED = "This lookup failed and shows nothing."
NO_MATCH = "Nothing in the guild matches {name}."
NAME_ONE = "Name one of these."
PROFESSION_OR_CHARACTER = "Name a profession or a character, one of the two."
NO_SCHEDULES = "No raid has a schedule yet. Officers add one on the Schedules page."
FENCES = ("data", "question", "conversation", "memory", "member-text")
NO_RECORD = "The asker has no record in this guild yet. They add a character with /register or on the Me page."
ATTENDANCE_PRIVATE = ("Attendance shows only where nobody else reads the reply. The asker sees theirs in a DM to the bot, "
                      "with /me view or on the Me page.")
UNKNOWN_SOURCE = "unknown"  # the wrapper's source for a lookup name that isn't one of TOOLS: never the model's own words


# ---------------------------------------------------------------- who asks


@dataclass(frozen=True)
class Viewer:
    """Who asks and who reads the reply, fixed when the loop starts. `officer` is already downgraded where members read
    the reply; `audience_public` = anyone else reads it, so even the asker's own private details stay out; `public` =
    an officer asked where members read it (help.Ask's meaning)."""
    reg: Any
    rs: Any
    user_id: int
    officer: bool
    public: bool
    audience_public: bool
    owner: bool
    question: str
    extras: help_mod.HelpExtras | None = None
    data: str | None = None
    name: str = ""  # the asker's name as Discord shows it, for an asker with no record (the start's asker line)

    @classmethod
    def of(cls, reg, rs, user_id: int, officer: bool, *, public: bool, question: str, extras=None, data=None,
           name: str = "") -> "Viewer":
        """help.guild_state's rule, exactly: the level follows who can READ the reply, not who asked."""
        audience_public = bool(public)
        public = bool(public and officer)
        officer = bool(officer and not public)
        return cls(reg, rs, int(user_id), officer, public, audience_public, bool(reg.is_owner_id(user_id)),
                   question or "", extras, data, str(name or ""))

    def ask(self) -> help_mod.Ask:
        """The same fields as help.Ask, for the snapshot's section builders."""
        return help_mod.Ask(self.reg, self.rs, self.user_id, self.officer, self.question, self.extras,
                            public=self.public, audience_public=self.audience_public)


# ---------------------------------------------------------------- the registry


def _always(profile: GameProfile) -> bool:
    return True


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Viewer, dict], str]
    cap: int
    available: Callable[[GameProfile], bool] = field(default=_always)

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


TOOLS: dict[str, Tool] = {}


def tools_for(profile: GameProfile) -> list[Tool]:
    """The lookups this game version offers, sorted by name: the same list for every request of a profile."""
    return sorted((t for t in TOOLS.values() if t.available(profile)), key=lambda t: t.name)


def _schema(props: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _string(description: str) -> dict:
    return {"type": "string", "maxLength": ARG_MAX, "description": description}


def _register(name: str, description: str, schema: dict, *, cap: int = RESULT_CAP, available=_always):
    def wrap(fn):
        TOOLS[name] = Tool(name, description, schema, fn, cap, available)
        return fn
    return wrap


# ---------------------------------------------------------------- the cut and the wrapper


class One(str):
    """A result that is ONE thing (an event, a member's record, one recipe, a manual part, a section): a cut says how
    many lines it left out, never "narrow it", which only a list of matches can do."""


def _more(n: int, one: bool = False) -> str:
    return f"… and {n} more lines left out." if one else f"… and {n} more — narrow it"


def cut(text: str, cap: int) -> str:
    """`text` in at most `cap` characters, on whole lines: when lines are left out the last line says how many — "narrow
    it" for a list of matches, "lines left out" for a `One`. A first line longer than the cap is shortened with an
    ellipsis (nothing else would fit)."""
    one = isinstance(text, One)
    text = str(text or "")
    if len(text) <= cap:
        return text
    lines = text.split("\n")
    more = lambda n: _more(n, one)  # noqa: E731
    room = cap - len(more(len(lines))) - 1  # the marker always fits, whatever the count
    out: list[str] = []
    used = 0
    for line in lines:
        n = len(line) + (1 if out else 0)
        if used + n > room:
            break
        out.append(line)
        used += n
    if not out:  # one line alone is longer than the room
        if len(lines) == 1:
            return text[: cap - 1] + "…"
        out = [lines[0][: max(0, room - 1)] + "…"]
        return "\n".join(out + [more(len(lines) - 1)])
    return "\n".join(out + [more(len(lines) - len(out))])


def run_tool(viewer: Viewer, name: str, args: dict) -> tuple[str, bool]:
    """One lookup → (its text in a data block, is_error). An unknown lookup, one this game version doesn't offer or a
    handler that raises gives TOOL_FAILED; the exception is logged, never shown."""
    tool = TOOLS.get(name)
    try:
        if tool is None or not tool.available(viewer.reg.profile):
            raise LookupError(f"no lookup {name!r} here")
        text = tool.handler(viewer, dict(args or {}))
    except Exception:  # noqa: BLE001 — a lookup's failure is a result the model reads, never a crash
        log.exception("answer lookup %r failed", str(name)[:ARG_MAX])
        return _wrap(tool.name if tool else UNKNOWN_SOURCE, TOOL_FAILED), True
    return _wrap(tool.name, cut(text, tool.cap)), False


def _wrap(source: str, text: str) -> str:
    """`source` is a name from TOOLS or UNKNOWN_SOURCE — never a name the model wrote."""
    return f'<data source="{source}">\n{unfence(text, *FENCES)}\n</data>'


def _arg(args: dict, key: str) -> str:
    """A string argument as the schema allows it: stripped, at most ARG_MAX characters (code enforces the limit)."""
    v = args.get(key)
    return str(v).strip()[:ARG_MAX] if isinstance(v, (str, int)) and not isinstance(v, bool) else ""


def _no_match(name: str) -> str:
    return NO_MATCH.format(name=name or "that")


# ---------------------------------------------------------------- find


@_register("find", "Finds what a name or a word means in this guild: recipes, items, members, characters, raids, schedules, "
                   "professions, commands and manual parts, the best matches first, with how many matched in all.",
           _schema({"text": _string("The name or words to look up, as the person wrote them."),
                    "kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}, "maxItems": len(KINDS),
                              "description": "Only these kinds of thing."}}, ("text",)))
def _find(v: Viewer, args: dict) -> str:
    kinds = [k for k in (args.get("kinds") or []) if k in KINDS] if isinstance(args.get("kinds"), list) else None
    return "\n".join(match_lines(*resolve(v.reg, _arg(args, "text"), kinds=kinds or None)))


# ---------------------------------------------------------------- recipe


def _reagents(e) -> str:
    return ", ".join(f"{x.get('name')} × {int(x.get('quantity') or 1)}" for x in e.recipe.reagents)


@_register("recipe", "One recipe by name, or '#<id>' from a find: who in the guild makes it, highest skill first, what it "
                     "makes, its reagents, and its cost to make when prices show.",
           _schema({"name": _string("The recipe's name, or '#' and its id.")}, ("name",)))
def _recipe(v: Viewer, args: dict) -> str:
    reg = v.reg
    name = _arg(args, "name")
    every = professions.who_can_make(reg, name, None)
    hits = every[:FIND_RECIPES]
    shown = [replace(e, crafters=e.crafters[:RECIPE_CRAFTERS]) for e in hits]  # a crafter line the cut can't drop whole
    out = professions.answer_lines(reg, shown, len(every))
    for i, e in enumerate(hits):
        if len(e.crafters) > RECIPE_CRAFTERS:
            out[i] += f", and {len(e.crafters) - RECIPE_CRAFTERS} more."
    for e in hits:
        if e.recipe is None:
            continue
        m = e.recipe.makes
        if m:
            lo, hi = int(m.get("quantity") or 1), int(m.get("quantity_max") or 0)
            out.append(f"{e.name} makes {m.get('name')} × {lo if hi <= lo else f'{lo} to {hi}'}.")
        if e.recipe.reagents:
            out.append(f"{e.name} needs {_reagents(e)}.")
        cost = prices.cost_line(reg, e.recipe)
        if cost:
            out.append(f"{e.name}: {cost}")
    return One("\n".join(out)) if len(every) == 1 else "\n".join(out)


# ---------------------------------------------------------------- crafters


def _profession(reg, text: str):
    t = text.strip().lower()
    return next((d for pid, d in reg.profile.professions.items() if t in (pid.lower(), d.name.lower())), None)


def _recipe_name(reg, r) -> str:
    d = reg.profile.recipes.get(r.id) if r.id is not None else None
    return d.name if d is not None else r.name


@_register("crafters", "Either one profession's crafters in the guild with their skill, highest first; or one character's "
                       "professions with skill and the recipes on their record. Give a profession or a character, not both.",
           _schema({"profession": _string("A profession's name."), "character": _string("A character's name.")}))
def _crafters(v: Viewer, args: dict) -> str:
    reg = v.reg
    pname, cname = _arg(args, "profession"), _arg(args, "character")
    if bool(pname) == bool(cname):
        return PROFESSION_OR_CHARACTER
    if pname:
        d = _profession(reg, pname)
        if d is None:
            return _no_match(pname)
        cs = professions.crafters_of(reg, d.id)
        if not cs:
            return f"Nobody in the guild has {d.name} on their record yet."
        return "\n".join([f"{d.name}, highest skill first:"]
                         + [f"{c.label} ({c.member}, {professions.skill_words(c.skill)})" for c in cs])
    try:
        found = reg.find(cname)
    except RegistryError as e:  # two characters share the name: the registry's own sentence
        return str(e)
    if found is None or not professions.visible(reg, *found):
        return _no_match(cname)
    m, c = found
    if not c.professions:
        return f"{c.label} ({m.display_name}) has no profession on their record."
    out = []
    names: list[str] = []
    for p in c.professions:
        d = reg.profile.professions.get(p.id)
        out.append(f"{c.label} ({m.display_name}): {d.name if d else p.id} {professions.skill_words(p.skill)}"
                   + f", {len(p.recipes)} recipe{'' if len(p.recipes) == 1 else 's'}.")
        names += [_recipe_name(reg, r) for r in p.recipes]
    if names:
        out.append("Recipes: " + ", ".join(names[:CHARACTER_RECIPES]) + ".")
        if len(names) > CHARACTER_RECIPES:
            out.append(f"And {len(names) - CHARACTER_RECIPES} more recipes.")
    return One("\n".join(out))


# ---------------------------------------------------------------- price


def _has_auctions(profile: GameProfile) -> bool:
    return bool(getattr(profile, "auctions", None))


@_register("price", "An item's lowest price listed on the guild's auction house, how many are listed and as of when.",
           _schema({"name": _string("The item's name, or '#' and its id.")}, ("name",)), available=_has_auctions)
def _price(v: Viewer, args: dict) -> str:
    return "\n".join(x for x in prices.price_answer(v.reg, _arg(args, "name"), v.owner, stamp=False) if x)


# ---------------------------------------------------------------- member


SELF_WORDS = frozenset({"me", "my", "myself", "mine", "i"})


def _member_named(reg, name: str):
    """The member a name means, through help.mentioned_members (the snapshot's own finder); test puppets never. An
    exact display or character name wins over a part; several left → them all."""
    here = {m.discord_id for m, _chars in people(reg)}  # the resolver's population: no puppet, nobody who left
    hits = [m for m in help_mod.mentioned_members(reg, name) if m.discord_id in here]
    n = name.strip().lower()
    exact = [m for m in hits if n in {m.display_name.lower(), *(c.name.lower() for c in m.characters if c.name),
                                       *(c.label.lower() for c in m.characters if c.name)}]
    return exact or hits


@_register("member", "One member's record by their name or a character's: their characters, class, spec and rank; more "
                     "when the asker may see it. 'me' is the asker.",
           _schema({"name": _string("A member's or a character's name, or 'me'.")}, ("name",)))
def _member(v: Viewer, args: dict) -> str:
    reg = v.reg
    name = _arg(args, "name")
    if name.lower() in SELF_WORDS:
        m = reg.members.get(v.user_id)
        if m is None or m.test:
            return NO_RECORD
        found = [m]
    else:
        found = _member_named(reg, name)
    if not found:
        return _no_match(name)
    if len(found) > 1:
        return "\n".join([m.display_name for m in found] + [NAME_ONE])
    m = found[0]
    if m.discord_id == v.user_id:  # their own record — but not the private parts where others read the reply
        return One("\n".join(help_mod.member_lines(reg, v.rs, m, "Your record", officer_view=not v.audience_public)))
    if v.officer:
        return One("\n".join(help_mod.member_lines(reg, v.rs, m, "Member", officer_view=True)))
    return help_mod.who_line(reg, m) or f"{m.display_name} has no character on their record yet."


# ---------------------------------------------------------------- event


def _raid_of(reg, text: str) -> str | None:
    t = text.strip().lower().removeprefix("the ").strip()
    for rid in reg.profile.raids:
        if t in {rid.lower(), rid.lower().replace("_", " "), str(reg.raid_def(rid).get("name", "")).lower()}:
            return rid
    return None


def _tester(reg, ev) -> int | None:
    return (reg.config.team(ev.team) or {}).get("test_by")


def _listed(reg, ev, viewer_id: int) -> bool:
    """An event the lookup may pick: never a raid taken out of its event's lineup, never a rehearsal (rc.rehearsal_run,
    as event_view leaves them out) — but the viewer's own rehearsal, which they are testing."""
    if ev.dropped:
        return False
    return not rc.rehearsal_run(reg, ev) or _tester(reg, ev) == viewer_id


def _events(reg, rs, which: str, viewer_id: int) -> list:
    w = which.strip().lower()
    live = [e for e in rs.live() if _listed(reg, e, viewer_id)]
    closed = sorted((e for e in rs.events.values() if e.state in ("done", "cancelled") and _listed(reg, e, viewer_id)),
                    key=lambda e: e.starts_at, reverse=True)
    if w in ("next", "the next", "next event", "the next event"):
        return live[:1]
    if w in ("tonight", "today"):
        today = reg.now_local().date()
        return [e for e in live if e.start.astimezone(reg.tz).date() == today]
    if w.startswith("last ") or w in ("last", "the last"):
        rest = w.removeprefix("the ").removeprefix("last").strip()
        if not rest:
            return closed[:1]
        rid = _raid_of(reg, rest)
        return [e for e in closed if e.instance == rid][:1] if rid else []
    titled = [e for e in [*live, *closed] if w in (run_title(reg, e).lower(), e.key.lower())]
    if titled:
        return titled
    rid = _raid_of(reg, w)
    return [e for e in live if e.instance == rid] if rid else []


@_register("event", "One event as the asker may see it: its state, times, answers, rosters and recent activity. Name it "
                    "'next', 'tonight', a raid's name, an event's title as a list shows it, or 'last' and a raid's name.",
           _schema({"which": _string("'next', 'tonight', a raid's name, an event's title, or 'last <raid>'.")}, ("which",)),
           cap=BIG_RESULT_CAP)
def _event(v: Viewer, args: dict) -> str:
    reg, rs = v.reg, v.rs
    which = _arg(args, "which")
    found = _events(reg, rs, which, v.user_id) if rs is not None else []
    if not found:
        return _no_match(which)
    if len(found) > 1:
        return "\n".join([f"{run_title(reg, e)} · {rc.RUN_STATE_WORDS.get(e.state, e.state)}" for e in found] + [NAME_ONE])
    ev = found[0]
    out = [f"{run_title(reg, ev)} · {rc.RUN_STATE_WORDS.get(ev.state, ev.state)}"
           + (f" · {PAGE_WORDS['test_run']}" if rc.rehearsal_run(reg, ev) else "")]
    me = reg.members.get(v.user_id)
    if me is not None and not me.test:
        s = ev.signups.get(str(v.user_id))
        seat = ev.seat_of(me) if ev.all_rosters else None
        out.append(f"The asker's answer: {rc.answer_word(ev, s.status if s else None, seat[0] + 1 if seat else None)}.")
    out += help_mod.run_lines(reg, rs, ev, v.officer, detail=True, viewer_id=v.user_id)
    return One("\n".join(out))


# ---------------------------------------------------------------- schedule


@_register("schedule", "Each raid's schedules and the next three events of each, in the guild's time. Give a raid's name "
                       "for that raid alone.",
           _schema({"raid": _string("A raid's name.")}))
def _schedule(v: Viewer, args: dict) -> str:
    from .schedule_view import schedule_sections

    reg = v.reg
    raid = _arg(args, "raid")
    sections = schedule_sections(reg)
    if raid:
        rid = _raid_of(reg, raid)
        name = str(reg.raid_def(rid).get("name", rid)) if rid else None
        sections = [s for s in sections if name is not None and s["raid"] == name]
        if not sections:
            return _no_match(raid) if rid is None else f"{name} has no schedule yet. Officers add one on the Schedules page."
    if not sections:
        return NO_SCHEDULES
    out = []
    for s in sections:
        out.append(f"{s['raid']}" + (f" · {int(s['size'])} players" if s.get("size") else ""))
        for r in s["schedules"]:
            head = (f"{r['title']}: " if r.get("title") else "") + r["label"]
            if r.get("paused"):
                tail = "paused for now"
            elif r["runs"]:
                tail = "next: " + ", ".join(r["runs"])
            else:
                tail = ""
            out.append(f"  {head}" + (f" — {tail}" if tail else ""))
    return "\n".join(out)


# ---------------------------------------------------------------- loot


def _loot_line(r: dict) -> str:
    """One award as the Loot log shows it to every member: day · item · character · boss · MS|OS — never a tier, a
    weight, the bot's pick, who recorded it or a reason (design §12, the owner's decision)."""
    boss = r.get("boss") if r.get("boss") and r.get("boss") != "?" else None
    return " · ".join(str(x) for x in (r["date_label"], r["item"], r["character"], boss, "OS" if r["offspec"] else "MS") if x)


def _council_view(v: Viewer) -> bool:
    """An officer asking where nobody else reads the reply: the council's view and the larger cap."""
    return v.officer and not v.audience_public


def _loot_cut(v: Viewer, text: str) -> str:
    """The loot result at the viewer's cap: BIG_RESULT_CAP for the council's view (fifteen rows and the precedents
    overrun 1,500), RESULT_CAP for everyone else. The tool's own cap is the larger, so run_tool never cuts it again."""
    return cut(text, BIG_RESULT_CAP if _council_view(v) else RESULT_CAP)


def _loot_rows(rows: list[dict], head: str) -> list[str]:
    out = [head] + [_loot_line(r) for r in rows[:LOOT_ROWS]]
    if len(rows) > LOOT_ROWS:
        out.append(f"And {len(rows) - LOOT_ROWS} more awards match.")
    return out


def _needle(item: str):
    t = item.strip().casefold()
    return lambda r: not t or t in str(r["item"]).casefold()


@_register("loot", "Awards from the guild's loot log, newest first: day, item, character, boss and main or off spec. Give a "
                   "member or character, part of an item's name, or both; with neither, the asker's own awards. For an "
                   "officer asking in private, also the council's precedents and reasons.",
           _schema({"member": _string("A member's or a character's name, or 'me'."),
                    "item": _string("An item's name, or part of it.")}),
           cap=BIG_RESULT_CAP)
def _loot(v: Viewer, args: dict) -> str:
    from . import loot_ops
    from .lootctx import RegistryLootContext

    reg = v.reg
    ctx = RegistryLootContext(reg, reg.store, None)  # a fresh read in this worker thread, as help._loot does
    who, item = _arg(args, "member"), _arg(args, "item")
    council = help_mod._loot(v.ask()) if _council_view(v) else []  # precedents with their reasons, the ledger's last rows
    if who.lower() in SELF_WORDS or (not who and not item and not _council_view(v)):
        # the asker's own awards, as My loot finds them: no filter and 'me' read the same way
        me = reg.members.get(v.user_id)
        if me is None or me.test:
            return NO_RECORD
        memo: dict = {}
        rows = [r for r in (loot_ops.log_row(reg, a, memo, False, False, ctx, v.user_id)
                            for a in loot_ops.member_awards(reg, ctx, me, loot_ops.LOG_MAX)) if _needle(item)(r)]
        if not rows:
            out = ["The asker has no award in the loot log yet." if not item else "No award of the asker's matches that."]
        else:
            out = _loot_rows(rows, "The asker's own awards, newest first:")
        return _loot_cut(v, "\n".join(out + council))
    uid = None
    if who:
        found = _member_named(reg, who)
        if not found:
            return _no_match(who)
        if len(found) > 1:
            return "\n".join([m.display_name for m in found] + [NAME_ONE])
        uid = found[0].discord_id
    rows = loot_ops.log_rows(reg, ctx, member_uid=uid, item=item or None, officer=False, scores=False, viewer_uid=v.user_id)
    out = _loot_rows(rows, "Awards from the loot log, newest first:") if rows else ["No award in the loot log matches that."]
    return _loot_cut(v, "\n".join(out + council))


# ---------------------------------------------------------------- attendance


@_register("attendance", "A member's attendance: how many of the guild's events they played in the window. The asker's "
                         "own where nobody else reads the reply; another member's only for an officer.",
           _schema({"member": _string("A member's or a character's name, or 'me'. Leave it out for the asker.")}))
def _attendance(v: Viewer, args: dict) -> str:
    reg = v.reg
    who = _arg(args, "member")
    if not who or who.lower() in SELF_WORDS:
        me = reg.members.get(v.user_id)
        found = [me] if me is not None and not me.test else []
        if not found:  # the asker's own words: no record is said before any officer check
            return NO_RECORD
    else:
        found = _member_named(reg, who)
    own = len(found) == 1 and found[0].discord_id == v.user_id
    if own and v.audience_public:  # their own attendance stays out where others read the reply (design §7)
        return ATTENDANCE_PRIVATE
    if not own and not v.officer:  # decided before saying whether the name matched anyone
        return OFFICERS_ONLY
    if not found:
        return _no_match(who)
    if len(found) > 1:
        return "\n".join([m.display_name for m in found] + [NAME_ONE])
    m = found[0]
    return f"{m.display_name}'s attendance: {reg.attendance_text(m)}."


# ---------------------------------------------------------------- manual


NO_MANUAL = "The manual has no part on this."
MEMBER_READS = ("This reply is for a member: a part marked for officers or the owner says what they do, not what this "
                "member can do.")
_PART_HEAD = re.compile(r"^### \S+ \(§(\S+) ", re.M)


@_register("manual", "Parts of the bot's manual: give part ids from the contents list, words to look for, or both. A part "
                     "whose section is marked for officers or the owner says so in its heading.",
           _schema({"ids": {"type": "array", "items": {"type": "string", "maxLength": ARG_MAX}, "maxItems": help_mod.READ_PARTS,
                            "description": "Part or section ids from the contents list, such as '4.3' or '4'."},
                    "query": _string("Words to look for in the manual.")}),
           cap=BIG_RESULT_CAP)
def _manual(v: Viewer, args: dict) -> str:
    raw = args.get("ids")
    ids = [str(x).strip()[:ARG_MAX] for x in raw if isinstance(x, (str, int)) and not isinstance(x, bool)] if isinstance(raw, list) else []
    text = help_mod.manual_read(ids[:help_mod.READ_PARTS], question=_arg(args, "query") or v.question)
    if not text.strip():
        return NO_MANUAL
    if not v.officer:  # the parts stay marked as today (help.section_tier in their headings); never shown as the member's to use
        tiers = {s["id"]: s["tier"] for s in help_mod.manual_sections()}
        if any(tiers.get(sid) for sid in _PART_HEAD.findall(text)):
            text = MEMBER_READS + "\n" + text
    return One(text)


# ---------------------------------------------------------------- section


@_register("section", "One of the bot's ready-made summaries, as the asker may see it: " + ", ".join(help_mod.SECTIONS) + ". "
                      "Some are for officers only.",
           _schema({"key": {"type": "string", "enum": list(help_mod.SECTIONS), "maxLength": ARG_MAX, "description": "Which summary."}},
                   ("key",)),
           cap=BIG_RESULT_CAP)
def _section(v: Viewer, args: dict) -> str:
    key = _arg(args, "key")
    if key not in help_mod.SECTIONS:
        return _no_match(key)
    _pattern, build, cap, officers = help_mod.SECTIONS[key]
    if officers and not v.officer:  # each section keeps its own flag; an officer where members read is a member here
        return OFFICERS_ONLY
    lines = help_mod._clip(build(v.ask()), min(cap, BIG_RESULT_CAP))
    return One("\n".join(lines)) if lines else "This summary has nothing to show yet."
