"""Re-issue (design §5.55, addendum 2026-11-05): RestedXP sells a guide per Battle.net account, so a member who owns
several accounts may lock the guide they bought to another of their own. The import string and the BattleTag it was
issued to unlock the texts; the same texts are locked again to the other account's BattleTag. One core for both surfaces:
the Me page's download (web/api_guides.py) and a DM to the bot (discord_bot.guide_convert, read in code before
any model, memory or log). Nothing is written or logged: the inputs and the answer live only in the request."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from .build import EMPTY_TEXT, TEXT_MAX, TOO_BIG, validate_battletag
from .rxp_decode import _SECTION, GuideError, decode, encode, trailer

EMPTY_NEW_TAG = "Enter your other account's BattleTag (Name#1234)."
SAME_TAG = "Those are the same BattleTag: the second one is your other account's."
CHANNEL_REFUSAL = "Send me that in a DM. The import string is your purchase, and the answer is a file for you alone. Delete your message here."
CONVERT_FAILED = "Something went wrong and nothing was converted. Try again in a minute."
BUSY = "Other guides are being converted right now. Try again in a minute."
USAGE = "Write it like this: convert guide from Name#1234 to Other#5678, with the import string attached as a .txt file."


def reissue(text: str, battletag: str, new_battletag: str) -> str:
    """The import string `text` (unlocked by `battletag`) locked to `new_battletag` instead, with as many sections as the
    paste had. GuideError = a refusal for the person, in the same words as the build."""
    if not (text or "").strip():
        raise GuideError(EMPTY_TEXT)
    if len(text) > TEXT_MAX:
        raise GuideError(TOO_BIG)
    tag = validate_battletag(battletag)
    if not (new_battletag or "").strip():
        raise GuideError(EMPTY_NEW_TAG)
    new = validate_battletag(new_battletag)
    if tag.lower() == new.lower():  # the key is case-insensitive (the addon lowercases it)
        raise GuideError(SAME_TAG)
    guides = decode(text, tag)
    sections = sum(1 for _h, mode, _c in _SECTION.findall(text) if mode == ":")
    return encode(guides, new, sections=max(1, sections), base=trailer(text))


# at most two re-issues unpack at once, whoever asks (the DM path needs only server membership and has no per-member lock)
SLOTS = threading.BoundedSemaphore(2)


def reissue_guarded(text: str, battletag: str, new_battletag: str) -> str:
    """reissue under SLOTS: GuideError(BUSY) when both are taken, never a queue. Both surfaces call this one."""
    if not SLOTS.acquire(blocking=False):
        raise GuideError(BUSY)
    try:
        return reissue(text, battletag, new_battletag)
    finally:
        SLOTS.release()


def reissue_filename(new_battletag: str) -> str:
    """RXPGuides-import-<Name>.txt: the new tag's name part, kept to letters, digits, _ and -."""
    name = re.sub(r"[^\w-]+", "_", new_battletag.strip().split("#", 1)[0]).strip("_") or "account"
    return f"RXPGuides-import-{name}.txt"


@dataclass
class ConvertRequest:
    tag: str
    new_tag: str
    text: str  # the inline string after `given`, else "" (the DM's attached .txt is read by the caller)


_CONVERT = re.compile(r"^\s*convert\s+guide\b(.*)$", re.I | re.S)
_MENTION = re.compile(r"<@[!&]?\d+>")
# the shape of an import string: N|, a checksum, a colon and 32 base64 characters (enough not to match prose). Linear: it
# starts only where a digit run starts (the lookbehind) and every run is bounded, so a 4 MB run of digits is one pass
_STRING = re.compile(r"(?<!\d)\d{1,6}\|-?\d{1,10}:[A-Za-z0-9+/]{32}")
_TRAIL = ".,;!?"


def looks_like_string(text: str) -> bool:
    """True when the text carries something shaped like an import string: never handed to a model or memory."""
    return bool(_STRING.search(text or ""))


def convert_request(content: str) -> ConvertRequest | None:
    """`convert guide from <Tag1> to <Tag2> [given <string>]`, read in code (never a model): None when the message is not
    one. The string may also follow the tags without `given` (a long message Discord turned into message.txt). Trailing
    .,;!? come off the tags. A request with a missing part is still a request, so the string it may carry never falls
    through to a prompt."""
    m = _CONVERT.match(_MENTION.sub("", content or ""))
    if not m:
        return None
    rest = m.group(1)
    given = re.search(r"\bgiven\s+(.*)$", rest, re.I | re.S)
    shaped = None if given else _STRING.search(rest)
    cut = given.start() if given else shaped.start() if shaped else len(rest)
    head = rest[:cut]
    text = given.group(1).strip() if given else rest[cut:].strip()
    frm = re.search(r"\bfrom\s+(\S+)", head, re.I)
    to = re.search(r"\bto\s+(\S+)", head, re.I)
    return ConvertRequest(tag=frm.group(1).rstrip(_TRAIL) if frm else "", new_tag=to.group(1).rstrip(_TRAIL) if to else "", text=text)
