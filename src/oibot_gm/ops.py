"""Ops feed: every significant action goes to the guild's ops channel; errors
also DM the owner. Silence is never assumed to be success. Every line is also
written to the process log at the matching level."""
from __future__ import annotations

import logging
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger(__name__)

LEVEL_ICON = {"info": "•", "warn": "⚠", "error": "✖"}
LOG_LEVEL = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
RECENT_LINES = 30  # ring kept for /gm status


LISTENERS: list = []  # fn(guild_key) after every emitted line (the web's event stream, scoped per guild)

# the surface a change came through → the prefix people read in the ops channel (design §2.6: the surface is a prefix,
# never part of the name); Discord itself has none
VIA_PREFIX = {"web": "[web] ", "mcp": "[mcp] ", "chat": "[chat] ", "discord": "", "": ""}


def audit_line(reg, by: str, line: str, *, via: str = "discord", by_id: int | None = None) -> str:
    """'[web] Jon (host): signup channel → #raids' — ONE format for every surface: `by` is a person's display name, the
    surface is the prefix from `via`, and a superuser acting in a guild they don't own is marked ' (host)' (Registry.is_owner_id
    counts both; `config.owner_discord_id` tells them apart). No DM follows (the DM rule: DMs only when someone must act)."""
    from .registry import is_superuser

    prefix = VIA_PREFIX.get((via or "").lower(), f"[{via}] ")
    who = str(by or "?")
    cfg = getattr(reg, "config", reg)
    owner = getattr(cfg, "owner_discord_id", None)
    if by_id is not None and is_superuser(by_id) and (owner is None or int(by_id) != int(owner)):
        who += " (host)"
    return f"{prefix}{who}: {line}"


async def audited(ops, reg, by: str, line: str, via: str = "discord", level: str = "info", by_id: int | None = None) -> str:
    """Write the audit line of an action a person took (`audit_line`) through `ops.emit` — `ops` is the process's Ops (or
    anything with `emit(cfg, level, text)`). set_channel, apply_async and the web's mutation routes all come through here,
    so the format, the via tag and the host suffix live once. Returns the text written; `reg` is a Registry or its GuildConfig."""
    text = audit_line(reg, by, line, via=via, by_id=by_id)
    await ops.emit(getattr(reg, "config", reg), level, text)
    return text


class Ops:
    """One per process; the recent-lines ring is kept PER GUILD (`recent_for(key)`), so one guild's status or /api/ops
    never shows another's lines."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._recent: dict[str, list[tuple[str, str, str]]] = {}  # guild key -> (time, level, text) ring for /gm status

    def recent_for(self, key: str | None) -> list[tuple[str, str, str]]:
        return list(self._recent.get(key or "", []))

    async def audited(self, reg, by: str, line: str, via: str = "discord", level: str = "info", by_id: int | None = None) -> str:
        """The audit line of an action a person took, to the guild's ops channel, ring and log (the module's `audited`)."""
        return await audited(self, reg, by, line, via=via, level=level, by_id=by_id)

    async def emit(self, cfg, level: str, text: str, exc: BaseException | None = None, dm: bool = True) -> None:
        """One ops line: the guild's ops channel, its ring, the process log; an error also DMs the guild's owner (`dm`
        False: not this time — a process-wide failure DMs each distinct owner once)."""
        key = getattr(cfg, "key", None) or "?"
        stamp = datetime.now(ZoneInfo(getattr(cfg, "timezone", "UTC"))).strftime("%I:%M %p").lstrip("0")  # guild time
        line = f"{LEVEL_ICON.get(level, '•')} `{stamp}` {text}"
        self._recent[key] = (self._recent.get(key, []) + [(stamp, level, text)])[-RECENT_LINES:]
        for fn in list(LISTENERS):
            try:
                fn(key)
            except Exception:  # noqa: BLE001 — a listener must never break the feed
                log.exception("ops listener failed")
        log.log(LOG_LEVEL.get(level, logging.INFO), "[%s] %s", key, text, exc_info=exc)
        if cfg.ops_channel_id:
            ch = self.client.get_channel(cfg.ops_channel_id)
            if ch:
                try:
                    await ch.send(line[:1900])
                except Exception as e:  # noqa: BLE001
                    log.warning("ops channel send failed: %s", e)
        if level == "error" and dm and cfg.owner_discord_id:
            try:
                user = self.client.get_user(cfg.owner_discord_id) or await self.client.fetch_user(cfg.owner_discord_id)
                detail = ("\n```" + "".join(traceback.format_exception(exc))[-900:] + "```") if exc else ""
                many = bool(getattr(getattr(self.client, "registries", None), "many", False))
                prefix = f"**{discord.utils.escape_markdown(str(getattr(cfg, 'name', key)))}** · " if many else ""  # which guild, once there are several
                await user.send((prefix + line + detail)[:1990])
            except Exception as e:  # noqa: BLE001
                log.warning("owner DM failed: %s", e)
