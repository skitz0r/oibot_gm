"""The build: an import string + its BattleTag → one member's folder under out/guides/<guild>/<uid>/ holding the four
files the Me page offers (FILES) plus the parsed dataset (guides.json, kept for later: the status-sharing idea) and
status.json. The inputs are never written: the string is the member's purchase and the BattleTag its key. The folder is
generated output (out/ is gitignored, never the data repo); a member rebuilds it from the string whenever they like.
An RXPGuides addon zip the host points at (OIBOT_RXP_ADDON) adds the free starter guides, so the explorer covers 1–60."""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import rxp_json, rxp_md
from .rxp_decode import GuideError, decode
from .rxp_json import GuideGame

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "out" / "guides"
HERE = Path(__file__).resolve().parent
ADDON_ENV = "OIBOT_RXP_ADDON"  # the host's copy of the RXPGuides addon zip (CurseForge / Wago): the free starter guides
TEXT_MAX = 4_000_000  # an import string is ~1 MB for a full 1–60 purchase
ADDON_ZIP_MAX = 60_000_000
BATTLETAG_RE = re.compile(r"^[^\s#]{3,12}#\d{4,7}$")
ADDON_NAME = "RXPGuides_LocalGuides"
# what the member downloads: file name → (content type, the words on the button)
FILES = {
    "explorer.html": ("text/html; charset=utf-8", "the route explorer, one file that works offline"),
    f"{ADDON_NAME}.zip": ("application/zip", "the companion addon — unzip into Interface/AddOns next to RXPGuides"),
    "guide-text.zip": ("application/zip", "the guides as readable text, one file per group, with a route summary"),
}
_locks: dict[tuple[str, int], threading.Lock] = {}
_locks_guard = threading.Lock()

EMPTY_TEXT = "Paste the import string first."
EMPTY_TAG = "Enter the BattleTag the guide was issued to (Name#1234)."
BAD_TAG = "A BattleTag is a name, a # and 4 to 7 digits: Name#1234."
TOO_BIG = "That paste is bigger than any guide export; paste the import string alone."
BUSY = "Your guide is being built already; wait for it to finish."


def validate_battletag(tag: str) -> str:
    t = (tag or "").strip()
    if not t:
        raise GuideError(EMPTY_TAG)
    if not BATTLETAG_RE.match(t):
        raise GuideError(BAD_TAG)
    return t


def member_dir(guild_key: str, uid: int) -> Path:
    return OUT / guild_key / str(int(uid))


def lock_for(guild_key: str, uid: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((guild_key, int(uid)), threading.Lock())


def addon_zip_path() -> Path | None:
    p = (os.environ.get(ADDON_ENV) or "").strip()
    return Path(p).expanduser() if p else None


def starter_guides(zip_path: Path, folder: str) -> list[str]:
    """The free guides registered inside the RXPGuides addon zip (Guides/<folder>/*.lua, RegisterGuide([=[ … ]=]) bodies)."""
    if not zip_path.is_file() or zip_path.stat().st_size > ADDON_ZIP_MAX:
        return []
    out: list[str] = []
    pat = re.compile(r"/Guides/" + re.escape(folder) + r"/[^/]+\.lua$", re.I)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if not pat.search(name):
                continue
            src = z.read(name).decode("utf-8", "replace")
            for _eq, body in re.findall(r"RegisterGuide\(\s*\[(=*)\[(.*?)\]\1\]", src, re.S):
                if re.search(r"#name\s+\S", body):
                    out.append(body)
    return out


def _addon_zip(texts: list[str], game: GuideGame) -> bytes:
    """The companion addon: every decoded guide as RXPGuides.RegisterGuide([=[ … ]=]) plus the .toc and guides.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        files = []
        for i, body in enumerate(texts, 1):
            body = body.strip("\n").replace("]=]", "] =]")
            m = re.search(r"#name\s+(.+)", body)
            fn = (re.sub(r"[^A-Za-z0-9_-]+", "_", m.group(1).strip()).strip("_") if m else f"guide{i}") or f"guide{i}"
            if f"{fn}.lua" in files:
                fn = f"{fn}_{i}"
            files.append(f"{fn}.lua")
            z.writestr(f"{ADDON_NAME}/guides/{fn}.lua", "RXPGuides.RegisterGuide([=[\n" + body + "\n]=])\n")
        z.writestr(f"{ADDON_NAME}/guides.xml", '<Ui xmlns="http://www.blizzard.com/wow/ui/">\n' + "".join(f'    <Script file="guides/{fn}"/>\n' for fn in files) + "</Ui>\n")
        z.writestr(f"{ADDON_NAME}/{ADDON_NAME}.toc", f"## Interface: {game.addon_interface}\n## Title: RestedXP Local Guides\n"
                   "## Notes: Loads plain-text RXP guides via RXPGuides.RegisterGuide().\n## Dependencies: RXPGuides\n## Version: 1.0\nguides.xml\n")
    return buf.getvalue()


def _text_zip(markdown: dict[str, str], summaries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn, body in markdown.items():
            z.writestr(f"guides/{fn}", body)
        for fn, body in summaries.items():
            z.writestr(f"summary/{fn}", body)
    return buf.getvalue()


def explorer_html(data: dict) -> str:
    """The single-file explorer: the vendored page with the dataset inlined as base64 gzip (no fetch, so file:// works)."""
    page = (HERE / "explorer.html").read_text()
    b64 = base64.b64encode(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 9)).decode()
    return page.replace(">GUIDES_DATA</script>", ">" + b64 + "</script>", 1)


def build(text: str, battletag: str, folder: Path, game: GuideGame, addon_zip: Path | None = None) -> dict:
    """Decode, parse and write every output into `folder` (replaced whole; a failed build leaves the previous one).
    Returns the status (what status() reads back). GuideError = a refusal for the person."""
    if not (text or "").strip():
        raise GuideError(EMPTY_TEXT)
    if len(text) > TEXT_MAX:
        raise GuideError(TOO_BIG)
    tag = validate_battletag(battletag)
    paid = decode(text, tag)
    starters = starter_guides(addon_zip, game.addon_folder) if addon_zip else []
    data = rxp_json.dataset([("starter", t) for t in starters] + [("paid", t) for t in paid], game)
    md = rxp_md.render(paid)
    summary_files, rows = rxp_json.summaries(data, game)
    mine = [g for g in data["guides"] if g["src"] == "paid"]
    lo = min((g["lo"] for g in mine if g["hi"]), default=0)
    hi = max((g["hi"] for g in mine), default=0)
    status = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "guides": len(mine), "steps": sum(len(g["steps"]) for g in mine), "starter": len(starters),
        "range": f"{lo}–{hi}" if hi else "", "groups": rows,
    }
    tmp = folder.with_name(folder.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    (tmp / "explorer.html").write_text(explorer_html(data))
    (tmp / f"{ADDON_NAME}.zip").write_bytes(_addon_zip(paid, game))
    (tmp / "guide-text.zip").write_bytes(_text_zip(md, summary_files))
    (tmp / "guides.json").write_text(json.dumps(data, separators=(",", ":")))
    status["sizes"] = {fn: (tmp / fn).stat().st_size for fn in FILES}
    (tmp / "status.json").write_text(json.dumps(status, indent=1))
    shutil.rmtree(folder, ignore_errors=True)
    tmp.rename(folder)
    return status


def status(folder: Path) -> dict | None:
    """status.json of a built folder, or None when nothing is built (or the folder is incomplete)."""
    f = folder / "status.json"
    if not f.is_file() or not all((folder / fn).is_file() for fn in FILES):
        return None
    try:
        return json.loads(f.read_text())
    except ValueError:
        return None


def clear(folder: Path) -> bool:
    had = folder.is_dir()
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(folder.with_name(folder.name + ".tmp"), ignore_errors=True)
    return had
