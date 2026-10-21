"""Leftover screens after a restart (dual interfaces §2.9, design §5.44): every open wizard (wizard.Wizard) and every
plain-text diff / policy reading (discord_policy.PendingView) is written to `out/pending_views.jsonl` while it waits, and
removed when it finishes, expires or is pressed. On the next `on_ready` the bot edits each one left over to "The bot
restarted; nothing was changed. Run the command again." — a channel message by its id, an ephemeral reply through its
interaction webhook while Discord still accepts the token (15 minutes). `live_count()` is what GET /api/agents/quiet
counts (a restart now would cut a person off mid-screen).

The file holds interaction tokens that can only edit that one reply and die after 15 minutes; it lives in out/
(gitignored, never the data repo). Process memory is the source of truth; the file is its mirror for the next process."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "out" / "pending_views.jsonl"
TOKEN_LIFE_S = 15 * 60 - 30  # an interaction token edits its reply for 15 minutes
RESTARTED = "The bot restarted; nothing was changed. Run the command again."

_live: dict[str, dict] = {}
_lock = threading.Lock()
ENABLED = False  # the file is written only by the running bot (restore_pending_views switches it on); tests and tools keep memory only


def _path() -> Path:
    return PATH


def _write() -> None:
    if not ENABLED:
        return
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in _live.values()))
        os.replace(tmp, path)
    except OSError as e:  # the screens still work; only the after-restart edit is lost
        log.debug("pending views not written: %s", e)


def track(key: str, *, app_id: int | None = None, token: str | None = None, channel_id: int | None = None, message_id: int | None = None,
          title: str = "") -> None:
    """Remember an open screen: a channel message (`channel_id` + `message_id`) or an ephemeral reply (`app_id` + the
    interaction `token`). Nothing to edit later → not tracked."""
    if not ((channel_id and message_id) or (app_id and token)):
        return
    with _lock:
        _live[key] = {"key": key, "app_id": app_id, "token": token, "channel_id": channel_id, "message_id": message_id, "title": title[:80], "at": time.time()}
        _write()


def untrack(key: str) -> None:
    with _lock:
        if _live.pop(key, None) is not None:
            _write()


def live_count() -> int:
    return len(_live)


def track_interaction(key: str, interaction, title: str = "") -> None:
    """track() from an interaction whose reply is the screen (ephemeral: edited later through the webhook)."""
    track(key, app_id=getattr(interaction, "application_id", None), token=getattr(interaction, "token", None), title=title)


def leftovers() -> list[dict]:
    """What the previous process left in the file (read before this process tracks anything)."""
    try:
        return [json.loads(ln) for ln in _path().read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


async def restore_pending_views(bot, now: float | None = None) -> int:
    """Edit every screen the previous process left open to RESTARTED (buttons gone); then the file mirrors this process
    again. A reply whose token has died, or a message Discord refuses, is skipped. Returns how many were edited."""
    import discord

    global ENABLED
    now = now or time.time()
    old = [e for e in leftovers() if e.get("key") not in _live]
    done = 0
    for e in old:
        try:
            if e.get("channel_id") and e.get("message_id"):
                ch = bot.get_channel(int(e["channel_id"]))
                if ch is None:
                    continue
                await ch.get_partial_message(int(e["message_id"])).edit(content=RESTARTED, embed=None, view=None)
            elif e.get("app_id") and e.get("token") and now - float(e.get("at") or 0) < TOKEN_LIFE_S:
                hook = discord.Webhook.partial(int(e["app_id"]), str(e["token"]), client=bot)
                await hook.edit_message("@original", content=(f"**{e['title']}**\n" if e.get("title") else "") + RESTARTED, embed=None, view=None)
            else:
                continue
            done += 1
        except Exception as ex:  # noqa: BLE001 — gone, expired or Discord down: nothing was changed either way
            log.debug("pending view %s not edited: %s", e.get("key"), ex)
    with _lock:
        ENABLED = True
        _write()
    return done
