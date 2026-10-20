"""Dual interfaces phase 2 (C), owner decision D25: rank is per MEMBER, stored on the main — every alt reads "alt". Old
member files can carry a rank on an alt (the three old conditional carries left some behind) or a main reading "alt"
while an alt holds the player's standing. Registry.migrate_ranks fixes both: idempotent, one data-repo commit per guild.

    uv run python scripts/migrate_ranks.py --dry-run     # show what would change
    uv run python scripts/migrate_ranks.py               # do it (commit only; the bot pushes on its next push)
    uv run python scripts/migrate_ranks.py --push        # … and push the data repo now

Data root: --data, else OIBOT_DATA_DIR, else the sibling ../oibot_gm-data checkout (store.resolve_data_root).
Run it while the bot is stopped (it keeps members in memory and would write the old ranks back on its next save of
that member): launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot → this script → start the bot again."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oibot_gm.profiles import GameProfile  # noqa: E402
from oibot_gm.registry import Registry  # noqa: E402
from oibot_gm.store import GitStore, is_fixture_root, resolve_data_root  # noqa: E402


def migrate(data_root: Path, guild: str | None = None, dry_run: bool = False, push: bool = False) -> dict[str, list[str]]:
    """{guild key: lines} for every guild under `data_root` (or just `guild`)."""
    store = GitStore(data_root, push=bool(push and not dry_run))
    out: dict[str, list[str]] = {}
    for gdir in sorted(p for p in data_root.iterdir() if (p / "guild.yaml").exists()):
        if guild and gdir.name != guild:
            continue
        cfg = yaml.safe_load((gdir / "guild.yaml").read_text())
        reg = Registry(store, gdir.name, GameProfile.load(ROOT / "profiles" / cfg["game_profile"]))
        out[gdir.name] = reg.migrate_ranks(by="scripts/migrate_ranks.py", dry_run=dry_run)
    store.flush()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, help="data repo root (default: OIBOT_DATA_DIR or ../oibot_gm-data)")
    ap.add_argument("--guild", help="one guild key (default: every guild in the data repo)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--push", action="store_true", help="push the data repo after the commit")
    a = ap.parse_args(argv)
    data = a.data or resolve_data_root(ROOT)
    if is_fixture_root(ROOT, data) and not a.data:
        print(f"refusing: no data repo found (resolved to the fixtures at {data}); pass --data", file=sys.stderr)
        return 2
    res = migrate(data, a.guild, a.dry_run, a.push)
    for g, lines in res.items():
        print(f"{g}: " + ("nothing to change" if not lines else ("would change" if a.dry_run else "changed") + f" {len(lines)} member(s)"))
        for ln in lines:
            print(f"  {ln}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
