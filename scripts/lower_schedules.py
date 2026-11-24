"""Going back to older code: one commit per guild, nothing written on a dry run or a refusal.

Three ways back, in this order of how far back they go:

1. To the phase-2 code (schedules at guild level with their sheet timing and minimums, but addressed by (raid, id),
   a raid's own run times as its `default` schedule, regulars keyed "<raid>:<schedule>"): `--ids-only`. Each schedule
   goes back to the id its alias came from (registry.lower_schedule_ids: a raid's own run times to `default`), every
   member's regular spots, every applicant's schedules and every run dict's series back to "<raid>:<schedule>", and
   `schedule_aliases` is emptied. The older code to go back to is commit 8b3bc81 (phase 2, = 1821e4d's code) — never
   a55a112, which already holds phase 3's first task and would lift the ids again at its start. Refused when a schedule
   runs more raids than the older code reads, two schedules of one raid would both go back to one id, or a run file is
   one the older code's scheduler would get wrong (it finds a run only by the key it builds from the raid, the start
   and the lowered schedule id, and never reads `stands_for`):
   - a live run whose key differs (a run opened from `barrow-deeps` has a '-barrow-deeps' key the older code's
     `default` never builds; a phase-3 key holds no time at all): a one-off is safe once ended or cancelled, a
     schedule's run once it has been played and ended;
   - a cancelled or finished schedule run whose key differs and whose own night is still ahead: the older code would
     open that night again, so wait until it has passed, or pause or remove the schedule first;
   - a run that stands in for another night of its schedule still ahead: the older code would open that night too, so
     wait until it has passed.

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
All or nothing: every guild is checked first and nothing is written unless every guild passes (a refused guild's line
says why, the others say they were not written; exit status 1). `--guild KEY` (repeatable) lowers only those guilds.
A guild with a stored schedule that can't be read is refused and left exactly as it was; the line names the entry. A
finished run file that names no raid (old history) is skipped; a live one refuses its guild.
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


def _run_words(raw: dict, raids: dict, tz) -> str:
    """A run as the refusal names it: its raid and its start in 12-hour guild time ('Barrow Deeps Tue 8 Dec 7:30 PM'),
    never its key (random letters since phase 3)."""
    from oibot_gm.constants import fmt12

    name = (raids.get(raw.get("instance")) or {}).get("name") or raw.get("instance") or "The run"
    try:
        return f"{name} {fmt12(datetime.fromisoformat(raw['starts_at']).astimezone(tz))}"
    except (KeyError, TypeError, ValueError):
        return str(name)


def _ahead(stamp: str | None, tz, now: datetime) -> bool:
    """An occurrence stamp ('YYYY-MM-DDTHH:MM', guild wall time) that is still to come."""
    try:
        return bool(stamp) and datetime.fromisoformat(stamp).replace(tzinfo=tz) > now
    except ValueError:
        return False


def _run_refusal(gdir: Path, cfg: GuildConfig, raids: dict | None = None, now: datetime | None = None) -> str | None:
    """One sentence per run file the phase-2 scheduler would get wrong — it finds a run ONLY by the key it builds from
    the raid, the start and the id its schedule lowers to (old_event_key), and it never reads `stands_for`:
    - a LIVE run whose key differs (a run opened from `barrow-deeps` keyed '…-barrow-deeps' where the older code builds
      no suffix for `default`; `tuesday-2` where it builds `tuesday`; a key of phase 3's that holds no time at all): it
      would open a second sheet for it. A one-off is safe once it is ended or cancelled; a schedule's run once it has
      been played (a cancelled one would be opened again — the next case);
    - a CANCELLED or FINISHED schedule run whose key differs and whose own night is still ahead: the older code finds
      nothing for that night and opens a fresh sheet for it. Safe once the night has passed, or with the schedule paused
      or removed;
    - a run (not cancelled) that stands in for another night of its schedule still ahead (`stands_for`, its file's or
      its run dict's): the older code opens that night too. Safe once that night has passed.
    None when no run is in the way."""
    from zoneinfo import ZoneInfo

    raids = raids or {}
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    back = {new: old for old, new in cfg.schedule_aliases.items()}
    entries = {e["id"]: e for e in cfg.schedules if isinstance(e, dict) and e.get("id")}
    dicts = {t.get("key"): t for t in cfg.rosters if isinstance(t, dict)}
    said = []
    for f in sorted((gdir / "raids").glob("*.json")) if (gdir / "raids").exists() else []:
        raw = json.loads(f.read_text())
        t = dicts.get(raw.get("team")) or {}
        live = raw.get("state") not in ("done", "cancelled")
        run = _run_words(raw, raids, tz)
        if not raw.get("instance"):  # an old file naming no raid: finished history is never read again; a live one can't be
            if live:
                said.append(f"The run {run} names no raid, so the older code can't read it. End or cancel it first.")
            continue
        # the run dict's schedule_id keeps the old suffix ('adhoc', 'after', 'test'); the file's stamp (phase 3) holds
        # a real schedule id only, so a stamped one-off reads its dict
        sid = t.get("schedule_id") or raw.get("schedule")
        old = lowered_id(entries[sid], back) if sid in entries else sid
        try:
            want = old_event_key(raw["instance"], datetime.fromisoformat(raw["starts_at"]), old, bool(raw.get("test")))
        except (KeyError, TypeError, ValueError):
            want = None
        stands = t.get("stands_for", raw.get("stands_for")) if t else raw.get("stands_for")
        entry = entries.get(sid) or {}  # a paused schedule or a pickup template opens nothing on its own
        scheduled = (bool(entry) and entry.get("active", True) and entry.get("kind") != "pickup"
                     and not raw.get("follows") and not raw.get("part") and not raw.get("test"))
        if stands and raw.get("state") != "cancelled" and _ahead(stands, tz, now):  # a night gone by is never opened
            said.append(f"The run {run} stands in for another night of its schedule, which the older code would open "
                        "too, so wait until that night has passed.")
        elif raw.get("key") == want:
            continue
        elif live and scheduled:
            said.append(f"The older code can't find the run {run}, so wait until it has been played and ended.")
        elif live:
            said.append(f"The older code can't find the run {run}, so end or cancel it first.")
        elif scheduled and _ahead(raw.get("occurrence") or None, tz, now):
            said.append(f"The older code would open {run} again, so wait until that night has passed, "
                        "or pause or remove its schedule first.")
    return " ".join(said) or None


def _plan(gdir: Path, store: GitStore, dry_run: bool, minimums_only: bool, ids_only: bool):
    """One guild, read and lowered IN MEMORY: (its line, the write that commits it or None). RegistryError = refused."""
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
    why = _run_refusal(gdir, cfg, raids)
    if why:
        raise RegistryError(why)
    ids = lower_schedule_ids(cfg, members, applicants)
    changed_m = [m for m, b in zip(members, before_m) if m.model_dump_json() != b]
    changed_a = [a for a, b in zip(applicants, before_a) if a.model_dump_json() != b]
    ids_moved = had_ids or bool(ids or changed_m or changed_a)
    id_words = ["ids: " + (", ".join(ids) or "back to raid and id")] if ids_moved else []

    def writer(what: str, keep_schedules: bool):
        def write() -> None:
            doc["raids"] = cfg.raids
            doc.pop("schedule_aliases", None)
            if keep_schedules:
                doc["schedules"] = cfg.schedules
            else:
                doc.pop("schedules", None)
            if "rosters" in doc:
                doc["rosters"] = cfg.rosters
            with store.batch(f"{gdir.name}: {what} (by scripts/lower_schedules.py)"[:400]):
                for m in changed_m:
                    store.write_text(Path(gdir.name) / "members" / f"{m.discord_id}.json", m.model_dump_json(indent=1))
                for a in changed_a:
                    store.write_text(Path(gdir.name) / "applicants" / f"{a.discord_id}.json", a.model_dump_json(indent=1))
                # guild.yaml LAST: it drops schedule_aliases, the map a re-run needs to lower member files a crash left
                # behind — written first, an interrupted run would leave members no later run can map back
                store.write_text(Path(gdir.name) / "guild.yaml", HEADER + yaml.safe_dump(doc, sort_keys=False))
                store.commit(f"{gdir.name}: {what} (by scripts/lower_schedules.py)"[:400])
        return None if dry_run else write

    verb = "would move" if dry_run else "moved"
    if ids_only:
        if ids_moved:
            return (f"{gdir.name}: {verb} the schedule ids back: {' · '.join(ids) or 'regulars keyed by raid and id'}",
                    writer(f"schedule ids back to raid and id for older code: {'; '.join(id_words)}", True))
        return f"{gdir.name}: no schedule ids of their own, nothing to lower", None
    mins = lower_minimums(cfg, raids)
    if minimums_only or not doc.get("schedules"):
        dropped_empty = not minimums_only and "schedules" in doc
        w = None
        if mins or dropped_empty or ids_moved:
            what = (f"minimums back to limits and aims for older code: {'; '.join(id_words + mins)}" if mins or ids_moved
                    else "empty guild-level schedules list removed for older code")
            w = writer(what, keep_schedules=not dropped_empty)
        tail = "" if minimums_only else ", no guild-level schedules"
        return (f"{gdir.name}: {verb} the minimums back: {' · '.join(id_words + mins)}{tail}" if mins or ids_moved
                else f"{gdir.name}: no minimums{tail}, nothing to lower"), w
    lines = lower_schedules(cfg, raids)
    return (f"{gdir.name}: {verb} back under each raid: {' · '.join(id_words + mins + lines)}",
            writer(f"schedules moved back under each raid for older code: {'; '.join(id_words + mins + lines)}", keep_schedules=False))


NOT_WRITTEN = "{name}: checked, and not written because another guild was refused."
UNKNOWN_GUILD = "No guild folder named {names} here, so nothing was checked or written."


def lower(data_root: Path, dry_run: bool = False, minimums_only: bool = False, ids_only: bool = False,
          guilds: list[str] | None = None) -> list[str]:
    """One line per guild under `data_root` (only those named in `guilds`, when given): what was lowered (or would be),
    that nothing was left to lower, or why the guild was refused. ALL OR NOTHING: every guild is checked and lowered in
    memory first, and only when none is refused is anything written — one commit per guild. One refused guild = nothing
    written anywhere (the others' lines say so), never data half-lowered; `guilds` lowers the ones that pass on their
    own. Any unexpected failure while reading a guild is that guild's refusal, in one sentence, never a traceback. The ids
    are lowered first (and only, with `ids_only`), then the minimums, then the schedules unless `minimums_only`."""
    store = GitStore(data_root, push=False)
    planned: list[tuple[str, str, object, bool]] = []  # (guild, line, write, refused)
    dirs = sorted(p for p in data_root.iterdir() if (p / "guild.yaml").exists())
    unknown = sorted(set(guilds or ()) - {p.name for p in dirs})
    if unknown:  # a mistyped --guild: refused in one sentence, never "nothing printed, exit 0"
        return [f"refused: {UNKNOWN_GUILD.format(names=', '.join(unknown))}"]
    for gdir in dirs:
        if guilds and gdir.name not in guilds:
            continue
        try:
            line, write = _plan(gdir, store, dry_run, minimums_only, ids_only)
        except RegistryError as e:
            planned.append((gdir.name, f"{gdir.name}: refused, nothing changed. {e}", None, True))
        except Exception as e:  # noqa: BLE001 — one guild's unreadable data is its refusal, never a crash mid-way
            print(f"{gdir.name}: {e!r}", file=sys.stderr)  # the detail, for whoever fixes the files
            planned.append((gdir.name, f"{gdir.name}: refused, nothing changed. Its files couldn't be read, so fix them "
                                       "or leave this guild out with --guild.", None, True))
        else:
            planned.append((gdir.name, line, write, False))
    if any(refused for *_x, refused in planned):
        return [line if refused or write is None else NOT_WRITTEN.format(name=name) for name, line, write, refused in planned]
    for _name, _line, write, _r in planned:
        if write is not None:
            write()
    store.flush()
    return [line for _n, line, _w, _r in planned]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", nargs="?", type=Path, help="data repo root (default: OIBOT_DATA_DIR or ../oibot_gm-data)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--ids-only", action="store_true", help="lower only the schedule ids and regulars (back to the phase-2 code)")
    which.add_argument("--minimums-only", action="store_true", help="lower the ids and the minimums (back to the phase-1 code); the schedules stay")
    ap.add_argument("--guild", action="append", metavar="KEY", help="only this guild's folder (repeatable); every guild by default")
    a = ap.parse_args(argv)
    data = a.data or resolve_data_root(ROOT)
    if is_fixture_root(ROOT, data) and not a.data:
        print(f"refusing: no data repo found (resolved to the fixtures at {data}); pass the path", file=sys.stderr)
        return 2
    said = lower(data, a.dry_run, a.minimums_only, a.ids_only, a.guild)
    for line in said:
        print(line)
    return 1 if any(": refused, nothing changed." in line or line.startswith("refused: ") for line in said) else 0


if __name__ == "__main__":
    sys.exit(main())
