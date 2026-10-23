"""Which guild an interaction is about (design §5.32) — the one routing module, so there is one code path.

In a server, the server decides: a command or button used in server B acts on guild B, and an unconfigured server never
reaches another guild (it is told it isn't set up). In a DM: one configured guild → that one (as before, newcomers
included); several → the guilds the person belongs to (`guilds_of`): one → it, none → every server's Join link, two or
more → the server picker (`guild_picker.choose_guild`), every time, never remembered (owner decision D2).

Persistent buttons carry their guild in the custom id (`<prefix>.<gk>:…`): `resolve_button` routes them without asking,
and refuses a press from any other server (K1). An old-format id (no guild) resolves as before — the server, else the
only guild — and with several guilds in a DM, by the button's own `legacy(reg)` matcher (one match → it, several → the
picker).

"Belongs to" is `membership`, which has three answers: yes, no (Discord's own 404, or a server the bot can't see) and
unknown (a transient fetch error or a lookup past LOOKUP_TIMEOUT_S). DM candidates are yes AND unknown, so a Discord
hiccup makes the bot ask instead of silently picking the other guild; `may_access` (the web) is yes only."""
from __future__ import annotations

import asyncio
import inspect
import logging

import discord

log = logging.getLogger(__name__)

NOT_SET_UP = ("This server isn't set up for oibot_GM yet. The server owner (or someone with Manage Server) sets it up with "
              "`/gm config owner`.")
NO_GUILDS = "I'm not set up for any server yet. Run `/gm config owner` in the server you want me to look after."
# someone in none of the servers when no served guild is listed (B10): no guild named, no invite — ask whoever invited you
NOT_IN_ANY_UNLISTED = "You're not in any server I look after. If someone invited you, ask them for the server's invite."
GONE = "This button's server isn't served by the bot any more."
OTHER_SERVER = "This button belongs to another server."
LOOKUP_TIMEOUT_S = 1.5  # per guild, run concurrently: `need` stays inside Discord's 3-second window
# DM routing decides WHICH guild a message or command is handled for without asking when only one answers yes, so it reads
# a much fresher membership than the web's 5 minutes: someone who just joined the second server is asked, someone who just
# left it is not offered it (the bot has no members intent, so no join/leave event ever evicts the cache)
ROUTING_MAX_AGE_S = 10


STALE_YES_S = 6 * 3600  # design §2.9: a last-known "yes" keeps a person's READS going this long while Discord can't answer


def stale_yes(bot, guild_id: int, uid: int, within: float = STALE_YES_S):
    """The member object of the last time Discord said "yes" for (server, uid), when within `within` seconds — else None.
    For reads only (the web's membership while Discord is unsure); writes re-resolve live (PRIV_LIVE) and refuse."""
    import time as _time

    hit = getattr(bot, "__dict__", {}).get("_member_last_yes", {}).get((guild_id, uid))
    return hit[1] if hit and _time.time() - hit[0] < within else None


async def member_of(bot, reg, uid: int, timeout: float | None = LOOKUP_TIMEOUT_S, max_age: float | None = None, stale_ok: bool = False) -> tuple[str, object | None]:
    """(answer, member): `membership`'s answer plus the Discord member when it is 'yes' (the web needs the member for
    the officer check and the display name). `timeout` None = wait as long as the lookup takes (the web; Discord's
    3-second window is the reason for the default). `max_age`: how old a cached answer may be (None: cached_member's own
    default; DM routing passes ROUTING_MAX_AGE_S). `stale_ok` (the web's reads, §2.9): an answer Discord couldn't give
    ('unknown') falls back to the last-known "yes" of the past STALE_YES_S — answered "stale"; anything else is unchanged."""
    get_guild = getattr(bot, "get_guild", None)
    guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
    if guild is None:
        return "no", None  # a server the bot can't see
    cached_member = getattr(bot, "cached_member", None)
    if cached_member is None:
        return "unknown", None
    try:
        look = cached_member(guild, uid) if max_age is None else cached_member(guild, uid, max_age=max_age)
        m = await (asyncio.wait_for(look, timeout) if timeout else look)
    except Exception:  # noqa: BLE001 — a timeout or a transient error is not a departure
        m = None
        if not stale_ok:
            return "unknown", None
    if m is not None:
        return "yes", m
    gone = getattr(bot, "__dict__", {}).get("_member_gone", set())
    if (guild.id, uid) in gone:
        return "no", None
    last = stale_yes(bot, guild.id, uid) if stale_ok else None
    return ("stale", last) if last is not None else ("unknown", None)


async def membership(bot, reg, uid: int, max_age: float | None = None) -> str:
    """'yes' | 'no' | 'unknown': is `uid` in the guild's Discord server right now (gateway cache, else one cached fetch)."""
    return (await member_of(bot, reg, uid, max_age=max_age))[0]


async def guilds_of(bot, uid: int, guilds=None) -> list:
    """The guilds `uid` belongs to (their owner, or in the server — 'unknown' counts, so a blip asks), in ordered() order.
    Read at most ROUTING_MAX_AGE_S old: this decides, without asking, which guild a DM is handled for."""
    guilds = guilds if guilds is not None else bot.registries
    regs = guilds.ordered()
    answers = await asyncio.gather(*(membership(bot, r, uid, max_age=ROUTING_MAX_AGE_S) for r in regs))
    return [r for r, a in zip(regs, answers) if r.is_owner_id(uid) or a in ("yes", "unknown")]


async def may_access(bot, reg, uid: int) -> bool:
    """The single strict "belongs to": the guild's owner, or a member of its server now."""
    if reg.is_owner_id(uid):
        return True
    return await membership(bot, reg, uid) == "yes"


# ---- who is an officer (dual interfaces, phase 0 — design §2.6): ONE predicate, every surface an alias
# Freshness windows for a lookup by id (a discord.Member in hand always decides live from its roles):
PRIV_LIVE = 0.0       # a write that needs officer or owner rights: fetch now, never a cached answer
PRIV_ROUTING = ROUTING_MAX_AGE_S  # DM routing and button presses: at most 10 s old
PRIV_WEB = 60.0       # the web's reads: a minute
ROLE_RANK = {"owner": 3, "officer": 2, "member": 1, "outside": 0}


class Resolved:
    """The answer of `resolve_role`: `role` owner | officer | member | outside, the Discord `role_ids` the person holds in
    that server (for Registry.may_plain), `stale` when the answer came from the process's own cache rather than the
    gateway, a live Member or a fresh fetch, and the `member` object when there is one."""

    __slots__ = ("role", "role_ids", "stale", "member")

    def __init__(self, role: str, role_ids: list[int], stale: bool, member=None):
        self.role, self.role_ids, self.stale, self.member = role, list(role_ids), stale, member

    @property
    def officer(self) -> bool:
        return self.role in ("owner", "officer")

    def __iter__(self):  # so `role, role_ids = await identity(...)` reads naturally
        yield self.role
        yield self.role_ids


def _uid(who) -> int | None:
    if who is None:
        return None
    if isinstance(who, int):
        return who
    uid = getattr(who, "id", None)
    try:
        return int(uid) if uid is not None else int(who)
    except (TypeError, ValueError):
        return None


def _live_member(who, guild):
    """`who` as a discord.Member OF THAT SERVER, else None (a Member of another server or a bare User decides nothing)."""
    if guild is None or not isinstance(who, discord.Member):
        return None
    return who if getattr(getattr(who, "guild", None), "id", None) == guild.id else None


def _role_of_member(bot, reg, member, guild) -> str:
    officiates = getattr(bot, "officiates", None)
    return "officer" if (officiates is not None and officiates(member, guild)) else "member"


def role_sync(bot, reg, who) -> str | None:
    """The role decided WITHOUT a lookup: the owner (or a superuser) anywhere; a discord.Member of the guild's server from
    its roles (Manage Server or an officer role → officer). None when it can't be told here (a bare User in a DM):
    callers that may await use `resolve_role`."""
    uid = _uid(who)
    if uid is None:
        return None
    if reg.is_owner_id(uid):
        return "owner"
    get_guild = getattr(bot, "get_guild", None)
    guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
    m = _live_member(who, guild)
    if m is None:
        return None
    saw = getattr(bot, "saw_member", None)
    if saw is not None:  # evidence beats the cache: the person is in the server now, with these roles
        saw(guild.id, m)
    return _role_of_member(bot, reg, m, guild)


async def resolve_role(bot, reg, who, *, max_age: float = PRIV_ROUTING) -> Resolved:
    """THE identity predicate (design §2.6). `who` is a discord.Member, a discord.User or a Discord id. The owner and the
    host's superusers (Registry.is_owner_id) are `owner` everywhere. A Member of the guild's server decides live from
    its roles and refreshes the cache; anyone else is looked up by id through `bot.cached_member` at most `max_age`
    seconds old (PRIV_LIVE for a write, PRIV_ROUTING for routing and presses, PRIV_WEB for the web's reads) — not in the
    server, or the server unreachable, is `outside`."""
    uid = _uid(who)
    if uid is None:
        return Resolved("outside", [], False)
    get_guild = getattr(bot, "get_guild", None)
    guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
    live = _live_member(who, guild)
    if live is not None:
        saw = getattr(bot, "saw_member", None)
        if saw is not None:
            saw(guild.id, live)
        role = "owner" if reg.is_owner_id(uid) else _role_of_member(bot, reg, live, guild)
        return Resolved(role, [r.id for r in getattr(live, "roles", None) or []], False, live)
    member, stale = None, False
    cached_member = getattr(bot, "cached_member", None)
    if guild is not None and cached_member is not None:
        fresh = guild.get_member(uid) if hasattr(guild, "get_member") else None
        try:
            member = await cached_member(guild, uid, max_age=max_age)
        except Exception:  # noqa: BLE001 — a lookup Discord didn't answer: no member, never a crash on a permission check
            member = None
        if member is not None and fresh is None:
            hit = getattr(bot, "__dict__", {}).get("_member_cache", {}).get((guild.id, uid))
            stale = bool(hit and hit[1] is member)  # the process's cache answered, not the gateway or a fetch
    role_ids = [r.id for r in getattr(member, "roles", None) or []] if member is not None else []
    if reg.is_owner_id(uid):
        return Resolved("owner", role_ids, stale, member)
    if member is None:
        return Resolved("outside", [], stale, None)
    return Resolved(_role_of_member(bot, reg, member, guild), role_ids, stale, member)


async def role_of(bot, reg, who, *, max_age: float = PRIV_ROUTING) -> str:
    """owner | officer | member | outside — `resolve_role`'s role alone."""
    return (await resolve_role(bot, reg, who, max_age=max_age)).role


async def identity(bot, reg, who, *, max_age: float = PRIV_ROUTING) -> Resolved:
    """(role, role_ids) for Registry.may_plain — `resolve_role` under the name the plain-text paths use (unpacks as a pair)."""
    return await resolve_role(bot, reg, who, max_age=max_age)


def listed(regs) -> list:
    """The LISTED guilds of `regs` (GuildConfig.public, B10 — the same predicate as web/app.listed): the only ones a
    refusal to someone outside every guild may name."""
    return [r for r in regs if getattr(r.config, "public", True)]


def join_lines(regs) -> str:
    """Each LISTED server's name and its Join link (when it has one) — for someone who is in none of them. An unlisted
    guild is never named, nor its invite handed out (B10)."""
    out = []
    for r in listed(regs):
        name = discord.utils.escape_markdown(str(r.config.name))
        out.append(f"• **{name}**" + (f" — {r.config.invite_url}" if r.config.invite_url else ""))
    return "\n".join(out)


async def _say(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def _tag(interaction: discord.Interaction, reg) -> None:
    extras = getattr(interaction, "extras", None)
    if isinstance(extras, dict):
        extras["guild"] = reg.key
    sync_name(reg, getattr(interaction, "user", None))  # a command or a press in the server: the name on record follows the nick


def sync_name(reg, who) -> bool:
    """THE trigger of `Registry.rename` (design §2.2: verbs never rename as a side effect; this is the explicit step): a
    discord.Member OF THE GUILD'S SERVER seen with a display name other than the one on record — a command, a button
    press (`_tag`) or a member-update event (OibotGM.on_member_update). A bare User (a DM: no nick to read), a Member of
    another server, someone unregistered or an unchanged name is a no-op. Seats, callouts and asks are keyed by id and a
    run keeps the name a person answered under, so nothing else moves. Returns True when the record changed."""
    if not isinstance(who, discord.Member):
        return False
    gid = getattr(getattr(reg, "config", None), "discord_guild_id", None)
    if not gid or getattr(getattr(who, "guild", None), "id", None) != gid:
        return False
    m = reg.members.get(who.id)
    name = str(getattr(who, "display_name", "") or "").strip()
    if m is None or not name or name == m.display_name:
        return False
    try:
        reg.rename(who.id, name, "Discord")
    except Exception:  # noqa: BLE001 — a name that won't save never breaks the command it rode in on
        log.exception("rename of %s in %s failed", who.id, reg.key)
        return False
    return True


async def need(interaction: discord.Interaction, reply: bool = True, guilds=None, ask: bool | None = None):
    """(guild, interaction) for a command, or (None, interaction) after telling the person why (`reply=False`: say
    nothing — /help adds its own line; `ask`: still show the DM server picker, default `reply`). Callers rebind:
    `reg, interaction = await need(interaction)` — after the DM server picker the command continues on the PRESS, a new
    interaction. On success the chosen key is in `interaction.extras["guild"]` (the error handler and the sync
    `for_interaction` read it)."""
    guilds = guilds if guilds is not None else interaction.client.registries
    reg = guilds.for_interaction(interaction)
    if reg is not None:
        _tag(interaction, reg)
        return reg, interaction
    if interaction.guild_id:  # a server that isn't configured: never another guild
        if reply:
            await _say(interaction, NOT_SET_UP)
        return None, interaction
    ordered = getattr(guilds, "ordered", None)
    regs = ordered() if ordered else []
    if len(regs) < 2:  # none configured (one would have resolved above)
        if reply:
            await _say(interaction, NO_GUILDS)
        return None, interaction
    mine = await guilds_of(interaction.client, interaction.user.id, guilds)
    if len(mine) == 1:
        _tag(interaction, mine[0])
        return mine[0], interaction
    if not mine:
        if reply:
            await _say(interaction, not_in_any(regs))
        return None, interaction
    if not (reply if ask is None else ask):  # a silent caller asks only when it says so (/help: the tier follows the guild)
        return None, interaction
    return await pick(interaction, mine)


def not_in_any(regs) -> str:
    """The reply to someone in none of the servers: the LISTED ones with their Join links; none listed → NO_GUILDS'
    wording (an unlisted guild is never named to someone outside it, B10)."""
    if not listed(regs):
        return NOT_IN_ANY_UNLISTED
    return "You're not in any server I look after yet. Join one first:\n" + join_lines(regs)


async def pick(interaction, regs):
    """The picker on `interaction`; (reg, press) with the press tagged, or (None, interaction) when nobody picked
    (then `interaction.response.is_done()`: the picker was its response and now says so — the caller says nothing more).
    The ORIGINAL interaction is tagged too, and keeps the press in `extras["press"]`: discord.py hands the tree's error
    handler the original, so a command failing after the picker still reaches the chosen guild's ops channel and owner,
    and its apology answers the press (the interaction the command was answering)."""
    from .guild_picker import choose_guild

    reg, press = await choose_guild(interaction, regs)
    if reg is None:
        return None, interaction
    _tag(press, reg)
    _tag(interaction, reg)
    extras = getattr(interaction, "extras", None)
    if isinstance(extras, dict):
        extras["press"] = press
    return reg, press


def _key_lookup(guilds, g: str, interaction):
    by_key = getattr(guilds, "by_key", None)
    if by_key is None:  # a test double that only knows for_interaction
        return guilds.for_interaction(interaction)
    return by_key.get(g)


async def resolve_button(interaction: discord.Interaction, g: str | None, legacy=None, guilds=None):
    """(guild, interaction) for a persistent button, or (None, interaction). When None, the caller answers with its own
    "closed" line unless `interaction.response.is_done()` (this already said why: the server is gone, another server,
    or the picker ran out).

    `g` set (a new id): that guild, never asking (D2) — refused when pressed in any other server, configured or not
    (K1: an unconfigured server's Manage Server never counts as an officer). `g` None (an id from before guilds were
    carried): in a server, that server's guild; in a DM, the only guild (unambiguous with one); with several, the
    person's guilds where `legacy(reg)` holds (one → it, several → the picker, none → None)."""
    guilds = guilds if guilds is not None else interaction.client.registries
    if g:
        reg = _key_lookup(guilds, g, interaction)
        if reg is None:
            await _say(interaction, GONE)
            return None, interaction
        gid = getattr(interaction, "guild_id", None)
        if gid and int(gid) != int(reg.config.discord_guild_id):
            await _say(interaction, OTHER_SERVER)
            return None, interaction
        _tag(interaction, reg)
        return reg, interaction
    reg = guilds.for_interaction(interaction)  # strict in a server; the only guild in a DM
    if reg is not None or getattr(interaction, "guild_id", None):
        if reg is not None:
            _tag(interaction, reg)
        return reg, interaction
    ordered = getattr(guilds, "ordered", None)
    if not ordered or len(ordered()) < 2:
        return None, interaction
    mine = await guilds_of(interaction.client, interaction.user.id, guilds)
    hits = []
    for r in mine:
        ok = legacy(r) if legacy is not None else True
        if inspect.isawaitable(ok):
            ok = await ok
        if ok:
            hits.append(r)
    if len(hits) == 1:
        _tag(interaction, hits[0])
        return hits[0], interaction
    if not hits:
        return None, interaction
    return await pick(interaction, hits)
