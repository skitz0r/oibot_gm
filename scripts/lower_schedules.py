"""Going back to older code: one commit per guild, nothing written on a dry run or a refusal.

Three ways back, in this order of how far back they go:

1. To the phase-2 code (schedules at guild level with their sheet timing and minimums, but addressed by (raid, id),
   a raid's own run times as its `default` schedule, regulars keyed "<raid>:<schedule>"): `--ids-only`. Each schedule
   goes back to the id its alias came from (registry.lower_schedule_ids: a raid's own run times to `default`), every
   member's regular spots, every applicant's schedules and every run dict's series back to "<raid>:<schedule>", and
   `schedule_aliases` is emptied. Refused when a schedule runs more raids than the older code reads, two schedules of
   one raid would both go back to one id, or ANY live run's key differs from the key the older code builds from its
   raid, start and lowered schedule id (a run opened from `barrow-deeps` has a '-barrow-deeps' key the older code's
   `default` never builds): the older code would open a second sheet for it, so end or cancel that run first.

2. To the phase-1 code (schedules at guild level, timing on each schedule, but a raid's make-up still as the owner's
   limits and the officers' aims): `--minimums-only`. The ids are lowered as above, then each raid's `minimums` go back
   to `comp: {tank, healer: {min}}` (both written, the missing one from the profile) and `comp_targets: {slot: {min,
   note}}` for every class, spec, melee or ranged minimum (registry.lower_minimums). The schedules stay where they are.

3. To code from before schedules moved to the guild level (design §5.67): no flag. The ids and the minimums are
   lowered first as above, then each raid's schedules and run times are folded back under `raids.<raid>` in the old
   shape and the top-level `schedules` list is removed (registry.lower_schedules). Older code reads a guild.yaml whose
   schedules sit at the top without an error, but it ignores that list: every raid then shows no schedules and opens
   no runs, and its next save writes the file without them.

Either way:

    launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot                                   # stop the bot
    uv run python scripts/lower_schedules.py [--ids-only | --minimums-only] --dry-run       # show what would change
    uv run python scripts/lower_schedules.py [--ids-only | --minimums-only]                 # do it (commit only; push when you start the older code)
    # then check out the older code and start the bot again

Data root: the path given, else OIBOT_DATA_DIR, else the sibling ../oibot_gm-data checkout (store.resolve_data_root).
A guild with a stored schedule that can't be read is refused and left exactly as it was; the line names the entry.
Code from this phase lifts a lowered file again at its next start (Registry.load_config)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oibot_gm.profiles import GameProfile  # noqa: E402
from oibot_gm.registry import (Applicant, GuildConfig, Member, RegistryError, lower_minimums, lower_schedule_ids,  # noqa: E402
                               lower_schedules, lowered_id)
from oibot_gm.store import GitStore, is_fixture_root, resolve_data_root  # noqa: E402

HEADER = "# oibot_GM guild config — edit via /gm config or by PR.\n"  # as Registry.save_config writes it


def _records(gdir: Path, sub: str, model) -> list:
    d = gdir / sub
    return [model.model_validate_json(f.read_text()) for f in sorted(d.glob("*.json"))] if d.exists() else []


def old_event_key(instance: str, start: datetime, sid: str | None, test: bool = False) -> str:
    """The key phase 2's code builds for a run (its raidcycle.event_key): no schedule suffix for `default` or none."""
    from oibot_gm import raidcycle as rc

    base = f"{rc.abbr(instance)}-{start.strftime('%m%d-%H%M')}"
    key = base if sid in (None, "", "default") else f"{base}-{sid}"
    if test and sid != "test":
        key += "-test"
    return f"{key}-{start.date().isoformat()}"


def _run_refusal(gdir: Path, cfg: GuildConfig) -> str | None:
    """One sentence when a LIVE run file has a key the older code won't build for it: each run's key is compared with
    old_event_key from its raid, its start and the id its schedule lowers to (a run opened from `barrow-deeps` keyed
    '…-barrow-deeps' where the older code builds no suffix for `default`; `tuesday-2` where it builds `tuesday`; a key
    of Task 3's that holds no time at all). The older code would then open a second sheet for that run."""
    back = {new: old for old, new in cfg.schedule_aliases.items()}
    entries = {e["id"]: e for e in cfg.schedules if isinstance(e, dict) and e.get("id")}
    names = {t.get("key"): t.get("name") for t in cfg.rosters if isinstance(t, dict)}
    dict_sid = {t.get("key"): t.get("schedule_id") for t in cfg.rosters if isinstance(t, dict)}
    bad = []
    for f in sorted((gdir / "raids").glob("*.json")) if (gdir / "raids").exists() else []:
        raw = json.loads(f.read_text())
        if raw.get("state") in ("done", "cancelled"):
            continue
        # the run dict's schedule_id keeps the old suffix ('adhoc', 'after', 'test'); the file's stamp (phase 3) holds
        # a real schedule id only, so a stamped one-off reads its dict
        sid = dict_sid.get(raw.get("team")) or raw.get("schedule")
        old = lowered_id(entries[sid], back) if sid in entries else sid
        try:
            want = old_event_key(raw["instance"], datetime.fromisoformat(raw["starts_at"]), old, bool(raw.get("test")))
        except (KeyError, TypeError, ValueError):
            want = None
        if raw.get("key") != want:
            bad.append(str(names.get(raw.get("team")) or raw.get("key")))
    if not bad:
        return None
    return (f"The older code can't find the run {bad[0]}, so end or cancel it first." if len(bad) == 1
            else f"The older code can't find the runs {', '.join(bad)}, so end or cancel them first.")


def lower(data_root: Path, dry_run: bool = False, minimums_only: bool = False, ids_only: bool = False) -> list[str]:
    """One line per guild under `data_root`: what was lowered (or would be), that nothing was left to lower, or why
    the guild was refused. One commit per guild lowered; nothing written on a dry run or a refusal. The ids are lowered
    first (and only, with `ids_only`), then the minimums, then the schedules unless `minimums_only`."""
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
        members, applicants = _records(gdir, "members", Member), _records(gdir, "applicants", Applicant)
        before_m = [m.model_dump_json() for m in members]
        before_a = [a.model_dump_json() for a in applicants]
        had_ids = bool(doc.get("schedule_aliases"))
        try:
            why = _run_refusal(gdir, cfg)
            if why:
                raise RegistryError(why)
            ids = lower_schedule_ids(cfg, members, applicants)
        except RegistryError as e:
            out.append(f"{gdir.name}: refused, nothing changed. {e}")
            continue
        changed_m = [m for m, b in zip(members, before_m) if m.model_dump_json() != b]
        changed_a = [a for a, b in zip(applicants, before_a) if a.model_dump_json() != b]
        ids_moved = had_ids or bool(ids or changed_m or changed_a)
        id_words = ["ids: " + (", ".join(ids) or "back to raid and id")] if ids_moved else []

        def write(what: str, keep_schedules: bool) -> None:
            if dry_run:
                return
            doc["raids"] = cfg.raids
            doc.pop("schedule_aliases", None)
            if keep_schedules:
                doc["schedules"] = cfg.schedules
            else:
                doc.pop("schedules", None)
            if "rosters" in doc:
                doc["rosters"] = cfg.rosters
            with store.batch(f"{gdir.name}: {what} (by scripts/lower_schedules.py)"[:400]):
                store.write_text(Path(gdir.name) / "guild.yaml", HEADER + yaml.safe_dump(doc, sort_keys=False))
                for m in changed_m:
                    store.write_text(Path(gdir.name) / "members" / f"{m.discord_id}.json", m.model_dump_json(indent=1))
                for a in changed_a:
                    store.write_text(Path(gdir.name) / "applicants" / f"{a.discord_id}.json", a.model_dump_json(indent=1))
                store.commit(f"{gdir.name}: {what} (by scripts/lower_schedules.py)"[:400])

        verb = "would move" if dry_run else "moved"
        if ids_only:
            if ids_moved:
                write(f"schedule ids back to raid and id for older code: {'; '.join(id_words)}", True)
                out.append(f"{gdir.name}: {verb} the schedule ids back: {' · '.join(ids) or 'regulars keyed by raid and id'}")
            else:
                out.append(f"{gdir.name}: no schedule ids of their own, nothing to lower")
            continue
        mins = lower_minimums(cfg, raids)
        if minimums_only or not doc.get("schedules"):
            dropped_empty = not minimums_only and "schedules" in doc
            if mins or dropped_empty or ids_moved:
                what = (f"minimums back to limits and aims for older code: {'; '.join(id_words + mins)}" if mins or ids_moved
                        else "empty guild-level schedules list removed for older code")
                write(what, keep_schedules=not dropped_empty)
            tail = "" if minimums_only else ", no guild-level schedules"
            out.append(f"{gdir.name}: {verb} the minimums back: {' · '.join(id_words + mins)}{tail}" if mins or ids_moved
                       else f"{gdir.name}: no minimums{tail}, nothing to lower")
            continue
        try:
            lines = lower_schedules(cfg, raids)
        except RegistryError as e:
            out.append(f"{gdir.name}: refused, nothing changed. {e}")
            continue
        write(f"schedules moved back under each raid for older code: {'; '.join(id_words + mins + lines)}", keep_schedules=False)
        out.append(f"{gdir.name}: {verb} back under each raid: {' · '.join(id_words + mins + lines)}")
    store.flush()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", nargs="?", type=Path, help="data repo root (default: OIBOT_DATA_DIR or ../oibot_gm-data)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--ids-only", action="store_true", help="lower only the schedule ids and regulars (back to the phase-2 code)")
    which.add_argument("--minimums-only", action="store_true", help="lower the ids and the minimums (back to the phase-1 code); the schedules stay")
    a = ap.parse_args(argv)
    data = a.data or resolve_data_root(ROOT)
    if is_fixture_root(ROOT, data) and not a.data:
        print(f"refusing: no data repo found (resolved to the fixtures at {data}); pass the path", file=sys.stderr)
        return 2
    for line in lower(data, a.dry_run, a.minimums_only, a.ids_only):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
