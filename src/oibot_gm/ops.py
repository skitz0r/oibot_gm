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


class Ops:
    def __init__(self, client: discord.Client):
        self.client = client
        self.recent: list[tuple[str, str, str]] = []  # (time, level, text) ring for /gm status

    async def emit(self, cfg, level: str, text: str, exc: BaseException | None = None) -> None:
        stamp = datetime.now(ZoneInfo(getattr(cfg, "timezone", "UTC"))).strftime("%I:%M %p").lstrip("0")  # guild time
        line = f"{LEVEL_ICON.get(level, '•')} `{stamp}` {text}"
        self.recent = (self.recent + [(stamp, level, text)])[-RECENT_LINES:]
        log.log(LOG_LEVEL.get(level, logging.INFO), "[%s] %s", getattr(cfg, "key", "?"), text, exc_info=exc)
        if cfg.ops_channel_id:
            ch = self.client.get_channel(cfg.ops_channel_id)
            if ch:
                try:
                    await ch.send(line[:1900])
                except Exception as e:  # noqa: BLE001
                    log.warning("ops channel send failed: %s", e)
        if level == "error" and cfg.owner_discord_id:
            try:
                user = self.client.get_user(cfg.owner_discord_id) or await self.client.fetch_user(cfg.owner_discord_id)
                detail = ("\n```" + "".join(traceback.format_exception(exc))[-900:] + "```") if exc else ""
                await user.send((line + detail)[:1990])
            except Exception as e:  # noqa: BLE001
                log.warning("owner DM failed: %s", e)
