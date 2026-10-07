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
GONE = "This button's server isn't served by the bot any more."
OTHER_SERVER = "This button belongs to another server."
LOOKUP_TIMEOUT_S = 1.5  # per guild, run concurrently: `need` stays inside Discord's 3-second window


async def member_of(bot, reg, uid: int, timeout: float | None = LOOKUP_TIMEOUT_S) -> tuple[str, object | None]:
    """(answer, member): `membership`'s answer plus the Discord member when it is 'yes' (the web needs the member for
    the officer check and the display name). `timeout` None = wait as long as the lookup takes (the web; Discord's
    3-second window is the reason for the default)."""
    get_guild = getattr(bot, "get_guild", None)
    guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
    if guild is None:
        return "no", None  # a server the bot can't see
    cached_member = getattr(bot, "cached_member", None)
    if cached_member is None:
        return "unknown", None
    try:
        m = await (asyncio.wait_for(cached_member(guild, uid), timeout) if timeout else cached_member(guild, uid))
    except Exception:  # noqa: BLE001 — a timeout or a transient error is not a departure
        return "unknown", None
    if m is not None:
        return "yes", m
    gone = getattr(bot, "__dict__", {}).get("_member_gone", set())
    return ("no" if (guild.id, uid) in gone else "unknown"), None


async def membership(bot, reg, uid: int) -> str:
    """'yes' | 'no' | 'unknown': is `uid` in the guild's Discord server right now (gateway cache, else one cached fetch)."""
    return (await member_of(bot, reg, uid))[0]


async def guilds_of(bot, uid: int, guilds=None) -> list:
    """The guilds `uid` belongs to (their owner, or in the server — 'unknown' counts, so a blip asks), in ordered() order."""
    guilds = guilds if guilds is not None else bot.registries
    regs = guilds.ordered()
    answers = await asyncio.gather(*(membership(bot, r, uid) for r in regs))
    return [r for r, a in zip(regs, answers) if r.config.owner_discord_id == uid or a in ("yes", "unknown")]


async def may_access(bot, reg, uid: int) -> bool:
    """The single strict "belongs to": the guild's owner, or a member of its server now."""
    if reg.config.owner_discord_id == uid:
        return True
    return await membership(bot, reg, uid) == "yes"


def join_lines(regs) -> str:
    """Each server's name and its Join link (when it has one) — for someone who is in none of them."""
    out = []
    for r in regs:
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
    return "You're not in any server I look after yet. Join one first:\n" + join_lines(regs)


async def pick(interaction, regs):
    """The picker on `interaction`; (reg, press) with the press tagged, or (None, interaction) when nobody picked."""
    from .guild_picker import choose_guild

    reg, press = await choose_guild(interaction, regs)
    if reg is None:
        return None, interaction
    _tag(press, reg)
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
