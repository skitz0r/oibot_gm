"""The Discord permissions the bot needs, in ONE place (dual interfaces §3.4 X3, §3.5 O-01; design §5.44).

- `missing_for(channel, kind)`: what the bot lacks in one channel for one of its jobs — View Channel, Send Messages and
  Embed Links everywhere; + Manage Messages where it pins its card (registration, absences); + Create Public Threads
  (and Send Messages in Threads) in the roster channel, where the loot council's thread and the updates threads open.
  `SetupMixin.set_channel` refuses a channel that fails it ("I can't post in #x: missing …"), the hourly check warns
  when a configured channel stops passing it, and the Setup card's ✓ is computed by it — one test, three readers.
- `missing_for(guild)`: the server-wide permissions of the invite link (`permissions_value()`) the bot doesn't hold —
  what `on_guild_join` tells the host and the Host card lists.
- `permissions_value()` / `invite_link()`: lifted from scripts/move_guild.py (which now imports them) so the invite
  the host sends and the checks the bot runs can't drift. D39: Manage Webhooks is in the invite (the bot creates the
  news webhook); Manage Roles is NOT (Accept assigns the member role only when the server grants it — D31).

Words are Discord's own permission names, so an owner finds them in Server Settings → Roles.
"""
from __future__ import annotations

from urllib.parse import urlencode

# the running client (set by SetupMixin.setup_tick and the guild events): configops' consequence hook checks permissions with it
CLIENT = None

# every channel the bot posts in
BASE = ("view_channel", "send_messages", "embed_links")
# per kind, on top of BASE: the pinned cards, the officers' threads, the news history read (backfill)
EXTRA: dict[str, tuple[str, ...]] = {
    "registration": ("manage_messages",),
    "absences": ("manage_messages",),
    "roster": ("create_public_threads", "send_messages_in_threads"),
    "news": ("read_message_history",),
}
# what an owner reads (Discord's names); the reason rides along where it isn't obvious
WORDS = {
    "view_channel": "View Channel", "read_messages": "View Channel", "external_emojis": "Use External Emojis", "send_messages": "Send Messages", "embed_links": "Embed Links", "attach_files": "Attach Files",
    "read_message_history": "Read Message History", "manage_messages": "Manage Messages", "create_public_threads": "Create Public Threads",
    "send_messages_in_threads": "Send Messages in Threads", "use_external_emojis": "Use External Emojis",
    "create_instant_invite": "Create Invite", "manage_webhooks": "Manage Webhooks", "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
}
WHY = {"manage_messages": "to pin the card", "create_public_threads": "for the council and updates threads",
       "send_messages_in_threads": "to post in those threads", "read_message_history": "to re-read the news"}
# not in the invite: the bot does without them and says what they would add
OPTIONAL = {"manage_roles": "Accept gives the applicant the member role (D31)",
            "manage_channels": "the registration channel is made read-only for @everyone"}


def required(kind: str | None) -> tuple[str, ...]:
    """The permissions one channel kind needs (BASE + its EXTRA)."""
    return BASE + EXTRA.get(str(kind or ""), ())


def permissions_value() -> int:
    """What the bot's code uses (checked against the source): post and edit cards and sheets, pin cards, threads under
    officer cards, its own app emojis, invites for the homepage's Join button, the news webhook (D39). No Manage Roles
    and no Manage Channels: both are optional (OPTIONAL) and the bot says what they would add."""
    import discord

    return discord.Permissions(view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True,
                               manage_messages=True, create_public_threads=True, send_messages_in_threads=True,
                               use_external_emojis=True, create_instant_invite=True, manage_webhooks=True).value


def guild_flags() -> tuple[str, ...]:
    """The invite's permissions as flag names (what `missing_for(guild)` checks)."""
    import discord

    want = discord.Permissions(permissions_value())
    return tuple(name for name, on in want if on)


def invite_link(client_id: str, guild: str | int | None = None) -> str:
    """The OAuth2 link that adds the bot with exactly `permissions_value()` (optionally pre-selecting a server)."""
    q = {"client_id": str(client_id), "scope": "bot applications.commands", "permissions": str(permissions_value())}
    if guild:
        q.update(guild_id=str(guild), disable_guild_select="true")
    return "https://discord.com/oauth2/authorize?" + urlencode(q)


def _is_channel(target) -> bool:
    return hasattr(target, "permissions_for") and getattr(target, "guild", None) is not None


def _me(guild):
    return getattr(guild, "me", None) if guild is not None else None


def missing_for(target, kind: str | None = None) -> list[str]:
    """The flag names the bot lacks: in a CHANNEL for `kind` (required(kind)), or server-wide in a GUILD for the invite's
    permissions (guild_flags()). [] = all there. The bot's own member unknown (no gateway): [] — an outage is not a
    missing permission; the callers that must know check the gateway first (`set_channel` says Discord is unreachable)."""
    if target is None:
        return []
    if _is_channel(target):
        me = _me(target.guild)
        if me is None:
            return []
        have = target.permissions_for(me)
        return [p for p in required(kind) if not getattr(have, p, False)]
    me = _me(target)
    if me is None:
        return []
    have = getattr(me, "guild_permissions", None)
    if have is None:
        return []
    if getattr(have, "administrator", False):
        return []
    return [p for p in guild_flags() if not getattr(have, p, False)]


def words(missing) -> str:
    """'Send Messages, Manage Messages (to pin the card)' — Discord's names, the reason where it isn't obvious."""
    return ", ".join(WORDS.get(p, p) + (f" ({WHY[p]})" if p in WHY else "") for p in missing)


def cant_post(channel_name: str, missing) -> str:
    """THE refusal / warning sentence: 'I can't post in #raids: missing Send Messages, Embed Links.'"""
    return f"I can't post in #{channel_name}: missing {words(missing)}."


def optional_missing(guild) -> list[str]:
    """OPTIONAL permissions the bot doesn't hold in the server (the Setup card's amber lines)."""
    me = _me(guild)
    have = getattr(me, "guild_permissions", None) if me is not None else None
    if have is None or getattr(have, "administrator", False):
        return []
    return [p for p in OPTIONAL if not getattr(have, p, False)]


__all__ = ["BASE", "EXTRA", "WORDS", "OPTIONAL", "required", "permissions_value", "guild_flags", "invite_link", "missing_for", "words", "cant_post",
           "optional_missing"]
