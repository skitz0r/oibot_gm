"""Check an import string against the bot's own unlock / lock code, offline (design §5.63).

    uv run python scripts/guide_roundtrip.py <file> <BattleTag> [--rounds 3]

Unlocks the file with the BattleTag, locks it again, unlocks that, `--rounds` times, and for another BattleTag and back.
Prints counts and short hashes only: never the string, the guide text or the tag. Exit 0 when every round gives the
same guide texts and the same output string."""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oibot_gm.guides.rxp_decode import GuideError, decode_sections, encode, trailer  # noqa: E402

ap = argparse.ArgumentParser(description="Unlock, lock and unlock an import string again; report whether it is stable.")
ap.add_argument("file")
ap.add_argument("battletag")
ap.add_argument("--rounds", type=int, default=3)
a = ap.parse_args()
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()[:12]  # noqa: E731
ref = Path(a.file).read_text(encoding="utf-8", errors="replace")
try:
    first = decode_sections(ref, a.battletag)
except GuideError as e:
    sys.exit(f"refused: {e}")
print(f"file: {len(ref)} characters, {sum(len(x) for x in first)} guides in sections {[len(x) for x in first]}, ends with |{trailer(ref)}, sha {sha(ref)}")
ok, s, outs = True, ref, []
for i in range(a.rounds):
    secs = decode_sections(s, a.battletag)
    s = encode([g for x in secs for g in x], a.battletag, layout=[len(x) for x in secs], base=trailer(s))
    outs.append(sha(s))
    same = secs == first
    ok &= same
    print(f"round {i + 1}: guide texts same as the file's: {same} · locked again: {len(s)} characters, sha {outs[-1]}")
stable = len(set(outs)) == 1
other = "Roundtrip#0000"
back = decode_sections(encode([g for x in first for g in x], other, layout=[len(x) for x in first], base=trailer(ref)), other) == first
print(f"same output every round: {stable} · another BattleTag and back: {back} · output identical to the file: {s == ref}")
sys.exit(0 if ok and stable and back else 1)
