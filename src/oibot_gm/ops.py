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


class Ops:
    """One per process; the recent-lines ring is kept PER GUILD (`recent_for(key)`), so one guild's status or /api/ops
    never shows another's lines."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._recent: dict[str, list[tuple[str, str, str]]] = {}  # guild key -> (time, level, text) ring for /gm status

    def recent_for(self, key: str | None) -> list[tuple[str, str, str]]:
        return list(self._recent.get(key or "", []))

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
