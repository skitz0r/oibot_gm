"""Rewrite the loot session files under their new names (dual interfaces phase 1, design §3.1 decisions 1–2).

Old files were keyed by the Discord thread / channel id (`<guild>/events/<channel id>.json`, items keyed by item id);
new ones are `<guild>/events/<run key>.json` (a /mock session: `mock-<channel>.json`) with a drop id per drop. The bot
reads either shape (the model converts on load), so this is housekeeping, not a precondition — but run it once so the
data repo's file names match what the bot writes. Idempotent: a second run finds nothing to do. One commit.

    uv run python scripts/migrate_loot_sessions.py --data ../oibot_gm-data --dry-run
    uv run python scripts/migrate_loot_sessions.py --data ../oibot_gm-data

Run it with the bot stopped (or between raids): a live session saved by the bot under its new name in the meantime is
simply the one kept. Push is left to the bot's debounced push (or `git push` in the data repo)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oibot_gm.loot.session import migrate_sessions  # noqa: E402
from oibot_gm.store import GitStore, resolve_data_root  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=None, help="the data repo (default: OIBOT_DATA_DIR, else ../oibot_gm-data)")
    ap.add_argument("--dry-run", action="store_true", help="list what would move; write nothing")
    args = ap.parse_args()
    root = (args.data or resolve_data_root(ROOT)).resolve()
    store = GitStore(root, push=False)
    lines = migrate_sessions(store, dry_run=args.dry_run)
    for line in lines:
        print(("would move " if args.dry_run else "moved ") + line)
    print(f"{len(lines)} file(s){' (dry run)' if args.dry_run else ''} · data {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
