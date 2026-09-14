"""Git-backed native data store.

Native guild state (characters, events, ledger, precedents, policy) is small,
so a private git repo *is* the store: one file per entity, git history as the
audit log, push as the backup. Foreign reference data (profiles/) stays in the
code repo. The bot is the only writer; commits are immediate, pushes are
debounced in a background thread.

Layout (per guild):  <data>/<guild>/{characters.yaml, policy.md, events/, ledger.jsonl, precedents.jsonl, imports/...}
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class GitStore:
    def __init__(self, root: Path, push: bool | None = None):
        self.root = root.resolve()
        self.lock = threading.RLock()
        self.is_repo = (self.root / ".git").exists()
        self.push_enabled = (os.environ.get("OIBOT_DATA_PUSH", "1") == "1") if push is None else push
        self._push_timer: threading.Timer | None = None
        if self.is_repo and self.push_enabled:
            self._git("pull", "--rebase", "--quiet", check=False)

    # ---- paths
    def guild_dir(self, guild: str) -> Path:
        return self.root / guild

    # ---- atomic writes
    def write_text(self, rel: Path | str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, p)
        return p

    def write_json(self, rel: Path | str, obj: Any) -> Path:
        return self.write_text(rel, json.dumps(obj, indent=1, default=str, sort_keys=False) + "\n")

    def append_jsonl(self, rel: Path | str, row: dict) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return p

    def read_jsonl(self, rel: Path | str) -> list[dict]:
        p = self.root / rel
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    # ---- git
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=check)

    def commit(self, message: str) -> bool:
        """Stage everything and commit if anything changed. Schedules a debounced push."""
        if not self.is_repo:
            return False
        with self.lock:
            self._git("add", "-A")
            if not self._git("diff", "--cached", "--quiet", check=False).returncode:
                return False  # nothing staged
            self._git("commit", "-q", "-m", message)
            self._schedule_push()
            return True

    def _schedule_push(self, delay: float = 5.0) -> None:
        if not self.push_enabled:
            return
        with self.lock:
            if self._push_timer:
                self._push_timer.cancel()
            self._push_timer = threading.Timer(delay, self.push)
            self._push_timer.daemon = True
            self._push_timer.start()

    def push(self, retries: int = 3) -> None:
        for attempt in range(retries):
            r = self._git("push", "--quiet", check=False)
            if r.returncode == 0:
                return
            # someone edited via PR: rebase and retry
            self._git("pull", "--rebase", "--quiet", check=False)
            time.sleep(2 * (attempt + 1))
        print(f"data push failed after {retries} attempts: {r.stderr.strip()[:200]}")

    def head(self) -> str:
        if not self.is_repo:
            return "no-git"
        return self._git("rev-parse", "--short", "HEAD", check=False).stdout.strip() or "empty"


def resolve_data_root(code_root: Path) -> Path:
    """OIBOT_DATA_DIR, else a sibling `oibot_gm-data` checkout, else the in-repo fixtures."""
    env = os.environ.get("OIBOT_DATA_DIR")
    if env:
        return Path(env)
    sibling = code_root.parent / "oibot_gm-data"
    if sibling.exists():
        return sibling
    return code_root / "fixtures"
