"""An RXPGuides import string ↔ plain guide texts. The string is `N|` then, per section, `<adler32>:<base64>%` where the
base64 holds zlib-compressed guide texts (NUL-separated) RC4-encrypted with a key derived from the BattleTag of the
account the guide was issued to (a port of the addon's CheckDataIntegrity / ReadCacheData). `encode` is the inverse: the
tests build fixtures with it, and the one string the bot issues is the member's own guide locked to the member's own other
account (reissue.py; RestedXP sells a guide per account)."""
from __future__ import annotations

import base64
import re
import zlib


class GuideError(ValueError):
    """A refusal a person reads (the Copy rule): what was wrong with the paste or the key."""


def make_base(battletag: str) -> list[int]:
    base = battletag.lower().encode()
    if len(base) > 16:
        base = base[-16:]
    k = 16 - len(base)
    buf = [0] * 16
    for i in range(16):
        j = (i - k) & 0xF
        buf[i] = base[j] if j < len(base) else 0
    for i in range(16):
        buf[(-i) & 0xF] = buf[(15 - i) & 0xF] ^ buf[(13 - i) & 0xF] ^ buf[(12 - i) & 0xF] ^ buf[(10 - i) & 0xF]
    return buf


def rc4_sbox(base: list[int]) -> list[int]:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + base[i & 0xF]) & 0xFF
        S[i], S[j] = S[j], S[i]
    return S


def rc4(S: list[int], data: bytes) -> bytes:
    S = S[:]
    i = j = 0
    out = bytearray()
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(b ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def a32(data: bytes) -> int:
    """Adler-32 (the addon's checksum): zlib's own, in C, so a 64 MB text is not a Python loop."""
    return zlib.adler32(data) & 0xFFFFFFFF


# `<adler32>:<base64>%` per section; the last % may be missing from a paste that was trimmed (padding is restored below)
# Linear on any input: a section starts only right after the `N|`, a `%` or whitespace (the lookbehinds), and every run is
# possessive, so a long run of digits or base64 is walked once, never retried from each position (a 4 MB paste stays fast)
_SECTION = re.compile(r"(?:(?<=\|)|(?<=%)|(?<=\s))(-?\d++)(\D)([A-Za-z0-9+/]++=*+)(?:%|\s|$)")
NOT_A_STRING = "That isn't a RestedXP import string: it starts with a number and a bar, like 78|…, and the addon's Export button gives you one."
WRONG_KEY = "That BattleTag doesn't unlock this guide: an import string opens only with the BattleTag of the account it was issued to."
TOO_BIG = "That paste is bigger than any guide export; paste the import string alone."  # also the build's paste-size refusal
# the most guide text an import string may unpack to, across every section: a full 1–60 purchase is a few MB of text, so
# 64 MB is far past any real guide and stops a small crafted string from inflating to gigabytes (memory, a32, re-encode)
MAX_PLAIN = 64 * 1024 * 1024


def decode(text: str, battletag: str) -> list[str]:
    """The guide texts (RXP step syntax) inside an import string, in order. GuideError when the paste isn't an import
    string or the BattleTag isn't its key (a checksum mismatch after decrypting is the same wrong key), or when the texts
    would unpack past MAX_PLAIN (refused before unpacking any further)."""
    return [g for sec in decode_sections(text, battletag) for g in sec]


def decode_sections(text: str, battletag: str) -> list[list[str]]:
    """decode, keeping the string's sections: one list of guide texts per section that holds any (the guide library
    keeps the counts so a version is issued in the upload's own layout)."""
    s = re.sub(r"^[^\d]*?(?=\d+\|)", "", text.strip())  # a label pasted in front of the string ("guide: 78|…") is dropped
    if not re.match(r"^\d+\|", s):
        raise GuideError(NOT_A_STRING)
    S = rc4_sbox(make_base(battletag))
    out: list[list[str]] = []
    total = 0
    for h, mode, content in _SECTION.findall(s):
        if mode != ":":
            continue
        try:
            raw = base64.b64decode(content + "=" * (-len(content) % 4))
            d = zlib.decompressobj()
            txt = d.decompress(rc4(S, raw), MAX_PLAIN - total + 1)  # never more than the cap left (+1 to see it overflow)
        except (ValueError, zlib.error) as e:
            raise GuideError(WRONG_KEY) from e
        total += len(txt)
        if d.unconsumed_tail or total > MAX_PLAIN:
            raise GuideError(TOO_BIG)
        if a32(txt) != int(h) % 4294967296:
            raise GuideError(WRONG_KEY)
        sec = [g.decode("utf-8", "replace") for g in txt.split(b"\0") if g.strip()]
        if sec:
            out.append(sec)
    if not out:
        raise GuideError(NOT_A_STRING)
    return out


# the addon refuses a string that doesn't end with `|<number>`, the addon version the string was made for, and one whose
# number is below its own `addon.version` (GuideLoader.lua ImportString: "Incomplete or invalid encoded string")
BASE = 40000
_TRAILER = re.compile(r"\|+(\d{1,9})\s*$")


def trailer(text: str) -> int | None:
    """The number an import string ends with (`…%|40000`), None when it has none: a re-lock keeps it."""
    m = _TRAILER.search((text or "")[-64:])
    return int(m.group(1)) if m else None


def encode(guides: list[str], battletag: str, sections: int = 1, layout: list[int] | None = None, base: int | None = None) -> str:
    """The inverse of decode: guide texts → an import string the given BattleTag unlocks (fixtures, and reissue: the
    member's own guide for their own other account — never anyone else's). `sections` splits the guides evenly;
    `layout` = the guide count of each section, in order (the guide library's upload layout; ValueError unless every
    count is positive and they cover every guide)."""
    if layout:
        if any(int(n) < 1 for n in layout) or sum(int(n) for n in layout) != len(guides):
            raise ValueError("the layout must cover every guide")
        sizes = [int(n) for n in layout]
    else:
        per = max(1, -(-len(guides) // max(1, sections)))
        sizes = [min(per, len(guides) - i) for i in range(0, len(guides), per)]
    S = rc4_sbox(make_base(battletag))
    out = [f"{len(guides)}|"]
    i = 0
    for n in sizes:
        txt = b"\0".join(g.encode() for g in guides[i:i + n])
        i += n
        out.append(f"{a32(txt)}:{base64.b64encode(rc4(S, zlib.compress(txt, 9))).decode()}%")
    out.append(f"|{max(BASE, int(base or 0))}")
    return "".join(out)
