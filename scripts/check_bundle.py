"""Is the committed frontend bundle current? Builds `frontend/` into a temporary directory (`vite build --outDir`)
and compares every file's hash with `src/oibot_gm/web/static/app`. Exit 1 with the differing paths when they
disagree (someone changed frontend/ without `npm run build`, or committed a stale bundle). Never writes into the repo."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
BUNDLE = ROOT / "src" / "oibot_gm" / "web" / "static" / "app"


def hashes(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main() -> int:
    if not BUNDLE.is_dir():
        print(f"no committed bundle at {BUNDLE} — run `cd frontend && npm run build`")
        return 1
    if not (FRONTEND / "node_modules").is_dir():
        print("frontend/node_modules missing — run `cd frontend && npm install` first")
        return 1
    npm = shutil.which("npm")
    if not npm:
        print("npm not on PATH")
        return 1
    with tempfile.TemporaryDirectory(prefix="oibot-bundle-") as tmp:
        out_dir = Path(tmp) / "app"
        # package.json's build is `tsc -b && vite build`; npm appends our args to the end, i.e. to `vite build`
        r = subprocess.run([npm, "run", "build", "--", "--outDir", str(out_dir), "--emptyOutDir"], cwd=FRONTEND, capture_output=True, text=True, env={**os.environ, "CI": "1"})
        if r.returncode != 0:
            print(r.stdout[-3000:])
            print(r.stderr[-3000:])
            print("frontend build failed")
            return 1
        fresh, committed = hashes(out_dir), hashes(BUNDLE)
    missing = sorted(set(fresh) - set(committed))
    stale = sorted(set(committed) - set(fresh))
    changed = sorted(k for k in fresh.keys() & committed.keys() if fresh[k] != committed[k])
    if not (missing or stale or changed):
        print(f"bundle OK ({len(committed)} files match a fresh build)")
        return 0
    for label, items in (("not in the committed bundle", missing), ("only in the committed bundle", stale), ("content differs", changed)):
        for k in items:
            print(f"  {label}: {k}")
    print(f"bundle out of date ({len(missing) + len(stale) + len(changed)} differences) — run `cd frontend && npm run build` and commit src/oibot_gm/web/static/app")
    return 1


if __name__ == "__main__":
    sys.exit(main())
