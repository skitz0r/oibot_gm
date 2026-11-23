"""Going back to older code: one commit per guild, nothing written on a dry run or a refusal.

Two ways back, in this order of how far back they go:

1. To the phase-1 code (schedules at guild level, timing on each schedule, but a raid's make-up still as the owner's
   limits and the officers' aims): `--minimums-only`. Each raid's `minimums` go back to `comp: {tank, healer: {min}}`
   (both written, the missing one from the profile) and `comp_targets: {slot: {min, note}}` for every class, spec, melee
   or ranged minimum (registry.lower_minimums). The phase-1 code reads each schedule's timing as its own values and
   needs nothing else, so the schedules stay where they are.

2. To code from before schedules moved to the guild level (design §5.67): no flag. The minimums are lowered first as
   above, then each raid's schedules and run times are folded back under `raids.<raid>` in the old shape and the
   top-level `schedules` list is removed (registry.lower_schedules). Older code reads a guild.yaml whose schedules sit
   at the top without an error, but it ignores that list: every raid then shows no schedules and opens no runs, and
   its next save writes the file without them.

Either way:

    launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot                     # stop the bot
    uv run python scripts/lower_schedules.py [--minimums-only] --dry-run      # show what would change
    uv run python scripts/lower_schedules.py [--minimums-only]                # do it (commit only; push when you start the older code)
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
from oibot_gm.registry import GuildConfig, RegistryError, lower_minimums, lower_schedules  # noqa: E402
from oibot_gm.store import GitStore, is_fixture_root, resolve_data_root  # noqa: E402

HEADER = "# oibot_GM guild config — edit via /gm config or by PR.\n"  # as Registry.save_config writes it


def lower(data_root: Path, dry_run: bool = False, minimums_only: bool = False) -> list[str]:
    """One line per guild under `data_root`: what was lowered (or would be), that nothing was left to lower, or why
    the guild was refused. One commit per guild lowered; nothing written on a dry run or a refusal. The minimums are
    lowered first; the schedules after them unless `minimums_only`."""
    store = GitStore(data_root, push=False)
    out: list[str] = []
    for gdir in sorted(p for p in data_root.iterdir() if (p / "guild.yaml").exists()):
        path = gdir / "guild.yaml"
        doc = yaml.safe_load(path.read_text()) or {}
        try:
            raids = GameProfile.load(ROOT / "profiles" / doc["game_profile"]).raids
        except Exception:  # noqa: BLE001 — the names only: without the profile the lines use the raid ids (and no profile minimum)
            raids = {}
        cfg = GuildConfig(**doc)
        mins = lower_minimums(cfg, raids)
        if minimums_only or not doc.get("schedules"):
            dropped_empty = not minimums_only and "schedules" in doc
            if mins or dropped_empty:
                if not dry_run:
                    doc["raids"] = cfg.raids
                    if dropped_empty:
                        doc.pop("schedules")
                    store.write_text(Path(gdir.name) / "guild.yaml", HEADER + yaml.safe_dump(doc, sort_keys=False))
                    what = (f"minimums back to limits and aims for older code: {'; '.join(mins)}" if mins else "empty guild-level schedules list removed for older code")
                    store.commit(f"{gdir.name}: {what} (by scripts/lower_schedules.py)"[:400])
            tail = "" if minimums_only else ", no guild-level schedules"
            out.append(f"{gdir.name}: {'would move' if dry_run else 'moved'} the minimums back: {' · '.join(mins)}{tail}" if mins
                       else f"{gdir.name}: no minimums{tail}, nothing to lower")
            continue
        try:
            lines = lower_schedules(cfg, raids)
        except RegistryError as e:
            out.append(f"{gdir.name}: refused, nothing changed. {e}")
            continue
        if not dry_run:
            doc["raids"] = cfg.raids
            doc.pop("schedules", None)
            store.write_text(Path(gdir.name) / "guild.yaml", HEADER + yaml.safe_dump(doc, sort_keys=False))
            store.commit(f"{gdir.name}: schedules moved back under each raid for older code (by scripts/lower_schedules.py): {'; '.join(mins + lines)}"[:400])
        out.append(f"{gdir.name}: {'would move' if dry_run else 'moved'} back under each raid: {' · '.join(mins + lines)}")
    store.flush()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", nargs="?", type=Path, help="data repo root (default: OIBOT_DATA_DIR or ../oibot_gm-data)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--minimums-only", action="store_true", help="lower only the minimums (back to the phase-1 code); the schedules stay")
    a = ap.parse_args(argv)
    data = a.data or resolve_data_root(ROOT)
    if is_fixture_root(ROOT, data) and not a.data:
        print(f"refusing: no data repo found (resolved to the fixtures at {data}); pass the path", file=sys.stderr)
        return 2
    for line in lower(data, a.dry_run, a.minimums_only):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
