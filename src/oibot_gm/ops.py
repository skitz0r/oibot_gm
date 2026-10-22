"""Ops feed: every significant action goes to the guild's ops channel; errors
also DM the owner. Silence is never assumed to be success. Every line is also
written to the process log at the matching level."""
from __future__ import annotations

import logging
import re
import time
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


ERROR_WINDOW_S = 30 * 60  # one error alert per (guild, error signature) per window (D46)
EDIT_EVERY_S = 60  # a repeat burst edits the alert DM at most this often (its count catches up at the next repeat after it)
OPS_FAIL_ALERT = 3  # consecutive failed posts to the ops channel before the error alert says so (design §3.4 X3)
OPS_CHANNEL_SIG = "ops-channel"  # the throttle signature of "I can't post in the ops channel" (and the hourly check's report of it)


def error_signature(text: str, exc: BaseException | None = None) -> str:
    """What makes two errors 'the same' for the throttle: the exception type and the line with numbers, ids and
    quoted values blanked — '/raid lock by Jon: KeyError: 123' and '… by Ana: KeyError: 456' are one error."""
    t = re.sub(r"`[^`]*`|'[^']*'|\"[^\"]*\"", "…", str(text or ""))
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"\bby [^:]+:", "by …:", t)
    return f"{type(exc).__name__ if exc else ''}|{t[:160]}"


def error_targets(cfg) -> list[int]:
    """Who an error alert DMs (GuildConfig.error_dm_ids; None = the owner, [] = nobody) — Registry.error_dm_targets's rule
    read off the config, so the feed needs no Registry."""
    ids = getattr(cfg, "error_dm_ids", None)
    if ids is None:
        owner = getattr(cfg, "owner_discord_id", None)
        return [int(owner)] if owner else []
    return [int(x) for x in ids]


class Ops:
    """One per process; the recent-lines ring is kept PER GUILD (`recent_for(key)`), so one guild's status or /api/ops
    never shows another's lines. Error alerts are throttled per (guild key, error signature): the first in a window is
    DMed to the guild's error_dm_ids (default: the owner); the next ones inside ERROR_WINDOW_S edit that DM in place
    ("and 4 more like this since 7:12 PM — details in #ops") instead of sending another, and count towards the Ops page's
    badge (`suppressed_for`)."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._recent: dict[str, list[tuple[str, str, str]]] = {}  # guild key -> (time, level, text) ring for /gm status
        # (guild key, signature) -> {"at": monotonic start, "since": guild-time stamp, "more": n, "msgs": [(Message, text)]}
        self._alerts: dict[tuple[str, str], dict] = {}
        self._suppressed: dict[str, int] = {}  # guild key -> errors not DMed (folded into an earlier alert) since the bot started
        self._send_fail: dict[str, int] = {}  # guild key -> consecutive failed posts to its ops channel
        self._fail_alerted: set[str] = set()  # guild keys whose current failure streak was already DMed

    def recent_for(self, key: str | None) -> list[tuple[str, str, str]]:
        return list(self._recent.get(key or "", []))

    def suppressed_for(self, key: str | None) -> int:
        """Errors this guild had that were folded into an earlier alert instead of a DM of their own (the Ops page badge)."""
        return int(self._suppressed.get(key or "", 0))

    def ops_channel_failing(self, key: str | None) -> int:
        """Consecutive failed posts to this guild's ops channel (0 = the last one went through)."""
        return int(self._send_fail.get(key or "", 0))

    async def audited(self, reg, by: str, line: str, via: str = "discord", level: str = "info", by_id: int | None = None) -> str:
        """The audit line of an action a person took, to the guild's ops channel, ring and log (the module's `audited`)."""
        return await audited(self, reg, by, line, via=via, level=level, by_id=by_id)

    async def emit(self, cfg, level: str, text: str, exc: BaseException | None = None, dm: bool = True, dm_only: set[int] | None = None) -> None:
        """One ops line: the guild's ops channel, its ring, the process log; an error also alerts the guild's error_dm_ids
        (default the owner) — throttled per error signature (`dm` False: not this time; `dm_only`: only these of the
        guild's error targets — a process-wide failure DMs each distinct error target once)."""
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
            await self._post(cfg, key, line)
        if level == "error" and dm:
            await self._alert(cfg, key, stamp, line, text, exc, only=dm_only)

    async def _post(self, cfg, key: str, line: str) -> None:
        """The line in the ops channel; the third failure in a row alerts the error_dm_ids once per streak (an ops channel
        the bot can't write to is otherwise silent forever)."""
        ch = self.client.get_channel(cfg.ops_channel_id)
        err: str | None = None
        if ch is None and not getattr(self.client, "is_ready", lambda: True)():
            return  # the gateway isn't up yet (startup): not a failure of the channel
        if ch is None:
            err = "the bot can't see that channel (deleted, or no View Channel)"
        elif getattr(getattr(ch, "guild", None), "unavailable", False):
            return  # the server is in an outage: nothing is wrong with the channel's permissions, and nothing to count
        else:
            try:
                await ch.send(line[:1900])
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                log.warning("ops channel send failed: %s", e)
        if err is None:
            self._send_fail.pop(key, None)
            self._fail_alerted.discard(key)
            return
        n = self._send_fail[key] = self._send_fail.get(key, 0) + 1
        if n >= OPS_FAIL_ALERT and key not in self._fail_alerted and getattr(self.client, "is_ready", lambda: True)():
            self._fail_alerted.add(key)
            text = (f"✖ I can't post in the ops channel <#{cfg.ops_channel_id}> ({n} tries in a row: {err[:200]}). Give the bot View Channel, "
                    "Send Messages and Embed Links there, or pick another with `/gm config ops-channel`.")
            stamp = datetime.now(ZoneInfo(getattr(cfg, "timezone", "UTC"))).strftime("%I:%M %p").lstrip("0")
            await self._alert(cfg, key, stamp, text, text, None, sig=OPS_CHANNEL_SIG)  # one per 30 min however it flaps (D46)

    def known(self, key: str, sig: str) -> None:
        """An alert already sent another way (the hourly channel check reported the ops channel): the same problem under
        `sig` inside the window is folded, never a second DM."""
        self._alerts.setdefault((key, sig), {"at": time.monotonic(), "since": "", "more": 0, "msgs": []})

    async def host_alert(self, ids, sig: str, text: str) -> None:
        """A host-level error (the MCP token leaked): DMed to `ids` (the superusers — only they can act on .env) at most
        once per ERROR_WINDOW_S per signature; never a guild's ops channel or its error targets."""
        log.error("[host] %s", text)
        k = ("", f"host|{sig}")
        now = time.monotonic()
        held = self._alerts.get(k)
        if held is not None and now - held["at"] < ERROR_WINDOW_S:
            held["more"] += 1
            return
        delivered = False
        for uid in ids:
            try:
                user = self.client.get_user(uid) or await self.client.fetch_user(uid)
                await user.send(text[:1990])
                delivered = True
            except Exception as e:  # noqa: BLE001 — closed DMs: the log line stays
                log.warning("host alert DM to %s failed: %s", uid, e)
        if delivered:  # the window opens only on a delivered DM (as _alert)
            self._alerts[k] = {"at": now, "since": "", "more": 0, "msgs": []}

    def _guild_prefix(self, cfg, key: str) -> str:
        many = bool(getattr(getattr(self.client, "registries", None), "many", False))
        return f"**{discord.utils.escape_markdown(str(getattr(cfg, 'name', key)))}** · " if many else ""  # which guild, once there are several

    async def _dm_all(self, cfg, key: str, text: str, only: set[int] | None = None) -> list[tuple[object, str]]:
        """DM every error target once (`only`: those of them); returns the sent messages (for the throttle's later edits)."""
        sent = []
        body = (self._guild_prefix(cfg, key) + text)[:1990]
        for uid in error_targets(cfg):
            if only is not None and uid not in only:
                continue
            try:
                user = self.client.get_user(uid) or await self.client.fetch_user(uid)
                msg = await user.send(body)
                sent.append((msg, body))
            except Exception as e:  # noqa: BLE001 — closed DMs: the ops line stays
                log.warning("error alert DM to %s failed: %s", uid, e)
        return sent

    async def _alert(self, cfg, key: str, stamp: str, line: str, text: str, exc: BaseException | None, sig: str | None = None,
                     only: set[int] | None = None) -> None:
        """The throttled error alert (D46): the first of its signature in ERROR_WINDOW_S is DMed with the traceback tail;
        the next ones edit that DM's last line ('and 4 more like this since 7:12 PM — details in #ops') at most once per
        EDIT_EVERY_S (a burst of repeats is one edit, not one each). The window opens only on a DELIVERED DM: when every
        DM failed (closed DMs, no target reachable) the next error tries again. The ops line goes every time (emit).
        `sig`: a fixed signature instead of the one computed from the text (the ops-channel alert)."""
        sig = (key, sig or error_signature(text, exc))
        now = time.monotonic()
        for k in [k for k, a in self._alerts.items() if now - a["at"] >= ERROR_WINDOW_S]:
            self._alerts.pop(k, None)
        held = self._alerts.get(sig)
        if held is not None:
            held["more"] += 1
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            if now - held.get("edited", float("-inf")) < EDIT_EVERY_S:
                return  # edited a moment ago: the count catches up at the next repeat after EDIT_EVERY_S
            held["edited"] = now
            where = "details in the ops channel" if cfg.ops_channel_id else "details in the bot's log"
            tail = f"\n-# and {held['more']} more like this since {held['since']} — {where}"
            for msg, body in held["msgs"]:
                edit = getattr(msg, "edit", None)
                if edit is None:
                    continue
                try:
                    await edit(content=(body[:1990 - len(tail)] + tail))
                except Exception as e:  # noqa: BLE001
                    log.warning("error alert edit failed: %s", e)
            return
        detail = ("\n```" + "".join(traceback.format_exception(exc))[-900:] + "```") if exc else ""
        msgs = await self._dm_all(cfg, key, line + detail, only)
        if msgs:  # delivered: the window opens; nothing delivered → no window, the next one tries again
            self._alerts[sig] = {"at": now, "since": stamp, "more": 0, "msgs": msgs}
