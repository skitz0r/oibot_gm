"""Going back to code from before schedules moved to the guild level (design §5.67): fold each raid's schedules and run
times back under `raids.<raid>` in the old shape and remove the top-level `schedules` list — one commit per guild.

Older code reads a guild.yaml whose schedules sit at the top without an error, but it ignores that list: every raid
then shows no schedules and opens no runs, and its next save writes the file without them. So, in this order:

    launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot     # stop the bot
    uv run python scripts/lower_schedules.py --dry-run        # show what would change
    uv run python scripts/lower_schedules.py                  # do it (commit only; push when you start the older code)
    # then check out the older code and start the bot again

Data root: the path given, else OIBOT_DATA_DIR, else the sibling ../oibot_gm-data checkout (store.resolve_data_root).
A guild with a stored schedule that can't be read is refused and left exactly as it was; the line names the entry.
Code from this phase lifts a lowered file again at its next start (Registry.load_config)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oibot_gm.profiles import GameProfile  # noqa: E402
from oibot_gm.registry import GuildConfig, RegistryError, lower_schedules  # noqa: E402
from oibot_gm.store import GitStore, is_fixture_root, resolve_data_root  # noqa: E402

HEADER = "# oibot_GM guild config — edit via /gm config or by PR.\n"  # as Registry.save_config writes it


def lower(data_root: Path, dry_run: bool = False) -> list[str]:
    """One line per guild under `data_root`: what was lowered (or would be), that nothing was left to lower, or why
    the guild was refused. One commit per guild lowered; nothing written on a dry run or a refusal."""
    store = GitStore(data_root, push=False)
    out: list[str] = []
    for gdir in sorted(p for p in data_root.iterdir() if (p / "guild.yaml").exists()):
        path = gdir / "guild.yaml"
        doc = yaml.safe_load(path.read_text()) or {}
        if not doc.get("schedules"):
            if "schedules" in doc and not dry_run:
                doc.pop("schedules")
                store.write_text(Path(gdir.name) / "guild.yaml", HEADER + yaml.safe_dump(doc, sort_keys=False))
                store.commit(f"{gdir.name}: empty guild-level schedules list removed for older code (by scripts/lower_schedules.py)")
            out.append(f"{gdir.name}: no guild-level schedules, nothing to lower")
            continue
        try:
            raids = GameProfile.load(ROOT / "profiles" / doc["game_profile"]).raids
        except Exception:  # noqa: BLE001 — the names only: without the profile the lines use the raid ids
            raids = {}
        cfg = GuildConfig(**doc)
        try:
            lines = lower_schedules(cfg, raids)
        except RegistryError as e:
            out.append(f"{gdir.name}: refused, nothing changed. {e}")
            continue
        if not dry_run:
            doc["raids"] = cfg.raids
            doc.pop("schedules", None)
            store.write_text(Path(gdir.name) / "guild.yaml", HEADER + yaml.safe_dump(doc, sort_keys=False))
            store.commit(f"{gdir.name}: schedules moved back under each raid for older code (by scripts/lower_schedules.py): {'; '.join(lines)}"[:400])
        out.append(f"{gdir.name}: {'would move' if dry_run else 'moved'} back under each raid: {' · '.join(lines)}")
    store.flush()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", nargs="?", type=Path, help="data repo root (default: OIBOT_DATA_DIR or ../oibot_gm-data)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    a = ap.parse_args(argv)
    data = a.data or resolve_data_root(ROOT)
    if is_fixture_root(ROOT, data) and not a.data:
        print(f"refusing: no data repo found (resolved to the fixtures at {data}); pass the path", file=sys.stderr)
        return 2
    for line in lower(data, a.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
