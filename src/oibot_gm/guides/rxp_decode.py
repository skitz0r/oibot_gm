"""An RXPGuides import string ↔ plain guide texts. The string is `N|` then, per section, `<adler32>:<base64>%` where the
base64 holds zlib-compressed guide texts (NUL-separated) RC4-encrypted with a key derived from the BattleTag of the
account the guide was issued to (a port of the addon's CheckDataIntegrity / ReadCacheData). `encode` is the inverse —
the tests build fixtures with it; the bot never issues a string."""
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
    a, b = 1, 0
    for x in data:
        a = (a + x) % 65521
        b = (b + a) % 65521
    return (b << 16 | a) & 0xFFFFFFFF


# `<adler32>:<base64>%` per section; the last % may be missing from a paste that was trimmed (padding is restored below)
_SECTION = re.compile(r"(-?\d+)(\D)([A-Za-z0-9+/]+=*)(?:%|\s|$)")
NOT_A_STRING = "That isn't a RestedXP import string: it starts with a number and a bar, like 78|…, and the addon's Export button gives you one."
WRONG_KEY = "That BattleTag doesn't unlock this guide: an import string opens only with the BattleTag of the account it was issued to."


def decode(text: str, battletag: str) -> list[str]:
    """The guide texts (RXP step syntax) inside an import string, in order. GuideError when the paste isn't an import
    string or the BattleTag isn't its key (a checksum mismatch after decrypting is the same wrong key)."""
    s = re.sub(r"^[^\d]*?(?=\d+\|)", "", text.strip())  # a label pasted in front of the string ("guide: 78|…") is dropped
    if not re.match(r"^\d+\|", s):
        raise GuideError(NOT_A_STRING)
    S = rc4_sbox(make_base(battletag))
    guides: list[str] = []
    for h, mode, content in _SECTION.findall(s):
        if mode != ":":
            continue
        try:
            raw = base64.b64decode(content + "=" * (-len(content) % 4))
            txt = zlib.decompress(rc4(S, raw))
        except (ValueError, zlib.error) as e:
            raise GuideError(WRONG_KEY) from e
        if a32(txt) != int(h) % 4294967296:
            raise GuideError(WRONG_KEY)
        guides += [g.decode("utf-8", "replace") for g in txt.split(b"\0") if g.strip()]
    if not guides:
        raise GuideError(NOT_A_STRING)
    return guides


def encode(guides: list[str], battletag: str, sections: int = 1) -> str:
    """The inverse of decode: guide texts → an import string the given BattleTag unlocks (fixtures; never a bot output)."""
    S = rc4_sbox(make_base(battletag))
    out = [f"{len(guides)}|"]
    per = max(1, -(-len(guides) // max(1, sections)))
    for i in range(0, len(guides), per):
        txt = b"\0".join(g.encode() for g in guides[i:i + per])
        out.append(f"{a32(txt)}:{base64.b64encode(rc4(S, zlib.compress(txt, 9))).decode()}%")
    return "".join(out)
