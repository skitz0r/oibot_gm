"""Which guild an interaction is about (design §5.32) — the one routing module, so there is one code path.

In a server, the server decides: a command or button used in server B acts on guild B, and an unconfigured server never
reaches another guild (it is told it isn't set up). In a DM: one configured guild → that one (as before, newcomers
included); several → the guilds the person belongs to (`guilds_of`): one → it, none → every server's Join link, two or
more → for now "use this in the server" (the server picker is the next stage; design §14 K11 keeps a second claim
refused until then).

"Belongs to" is `membership`, which has three answers: yes, no (Discord's own 404, or a server the bot can't see) and
unknown (a transient fetch error or a lookup past LOOKUP_TIMEOUT_S). DM candidates are yes AND unknown, so a Discord
hiccup makes the bot ask instead of silently picking the other guild; `may_access` (the web) is yes only."""
from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger(__name__)

NOT_SET_UP = ("This server isn't set up for oibot_GM yet. The server owner (or someone with Manage Server) sets it up with "
              "`/gm config owner`.")
NO_GUILDS = "I'm not set up for any server yet. Run `/gm config owner` in the server you want me to look after."
SEVERAL = "You're in more than one server I look after; use this command in the server."
LOOKUP_TIMEOUT_S = 1.5  # per guild, run concurrently: `need` stays inside Discord's 3-second window


async def membership(bot, reg, uid: int) -> str:
    """'yes' | 'no' | 'unknown': is `uid` in the guild's Discord server right now (gateway cache, else one cached fetch)."""
    get_guild = getattr(bot, "get_guild", None)
    guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
    if guild is None:
        return "no"  # a server the bot can't see
    cached_member = getattr(bot, "cached_member", None)
    if cached_member is None:
        return "unknown"
    try:
        m = await asyncio.wait_for(cached_member(guild, uid), LOOKUP_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — a timeout or a transient error is not a departure
        return "unknown"
    if m is not None:
        return "yes"
    gone = getattr(bot, "__dict__", {}).get("_member_gone", set())
    return "no" if (guild.id, uid) in gone else "unknown"


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


async def need(interaction: discord.Interaction, reply: bool = True, guilds=None):
    """(guild, interaction) for a command, or (None, interaction) after telling the person why (`reply=False`: say
    nothing — /help adds its own line). Callers rebind: `reg, interaction = await need(interaction)` — the interaction
    may be a different one once the DM server picker exists. On success the chosen key is in `interaction.extras["guild"]`
    (the error handler and the sync `for_interaction` read it)."""
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
    if reply:
        await _say(interaction, SEVERAL if mine else "You're not in any server I look after yet. Join one first:\n" + join_lines(regs))
    return None, interaction
