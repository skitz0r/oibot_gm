"""Git-backed native data store.

Native guild state (characters, events, ledger, precedents, policy) is small,
so a private git repo *is* the store: one file per entity, git history as the
audit log, push as the backup. Foreign reference data (profiles/) stays in the
code repo. The bot is the only writer; commits are immediate, pushes are
debounced in a background thread.

Layout (per guild):  <data>/<guild>/{characters.yaml, policy.md, events/, ledger.jsonl, precedents.jsonl, imports/...}
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PUSH_DEBOUNCE_S = 5.0
PUSH_RETRIES = 3
_URL_RE = re.compile(r"(https?://|ssh://|git@)\S+")


def redact(text: str) -> str:
    """Git's stderr echoes the remote (token-bearing URLs included): never log it verbatim."""
    return _URL_RE.sub("<remote>", text or "")


class GitStore:
    def __init__(self, root: Path, push: bool | None = None):
        self.root = root.resolve()
        self.lock = threading.RLock()
        self.is_repo = (self.root / ".git").exists()
        self.push_enabled = (os.environ.get("OIBOT_DATA_PUSH", "1") == "1") if push is None else push
        self._push_timer: threading.Timer | None = None
        self._unpushed = False  # a commit happened since the last successful push
        self._batch_depth = 0
        self._batch_messages: list[str] = []
        if self.is_repo and self.push_enabled:
            self._git("pull", "--rebase", "--quiet", check=False)
        atexit.register(self.flush)  # a clean interpreter exit still pushes what the debounce timer was holding

    # ---- paths
    def guild_dir(self, guild: str) -> Path:
        return self.root / guild

    # ---- atomic writes
    def write_text(self, rel: Path | str, text: str) -> Path:
        p = self.root / rel
        with self.lock:  # tmp + rename under the same lock as commit(), so a commit never sees a half-written file
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(text)
            os.replace(tmp, p)
        return p

    def write_json(self, rel: Path | str, obj: Any) -> Path:
        return self.write_text(rel, json.dumps(obj, indent=1, default=str, sort_keys=False) + "\n")

    def append_jsonl(self, rel: Path | str, row: dict) -> Path:
        p = self.root / rel
        with self.lock:
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
    GIT_TIMEOUT_S = {"push": 60, "fetch": 60, "pull": 60}  # network calls; anything local gets 30 s

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """A git call that can never hang the bot: no credential prompts, and a timeout (a stuck push used to hold the
        store lock and keep a stopping process alive with its ports bound). A timeout reads as a failed call (124)."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")}
        try:
            return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=check, env=env,
                                  timeout=self.GIT_TIMEOUT_S.get(args[0] if args else "", 30))
        except subprocess.TimeoutExpired as e:
            log.warning("git %s timed out after %ss", " ".join(args[:2]), e.timeout)
            r = subprocess.CompletedProcess(e.cmd, 124, stdout="", stderr=f"timed out after {e.timeout}s")
            if check:
                raise subprocess.CalledProcessError(124, e.cmd, "", r.stderr) from None
            return r

    def commit(self, message: str) -> bool:
        """Stage everything and commit if anything changed. Schedules a debounced push.
        Inside `batch()` the commit is deferred: the message is kept and one commit lands when the batch exits."""
        if not self.is_repo:
            return False
        with self.lock:
            if self._batch_depth:
                self._batch_messages.append(message)
                return False
            return self._commit_now(message)

    def _commit_now(self, message: str) -> bool:
        with self.lock:
            self._git("add", "-A")
            if not self._git("diff", "--cached", "--quiet", check=False).returncode:
                return False  # nothing staged
            self._git("commit", "-q", "-m", message)
            self._unpushed = True
            self._schedule_push()
            return True

    @contextmanager
    def batch(self, message: str | None = None) -> Iterator["GitStore"]:
        """Defer commits until the block exits: every `commit()` inside becomes one commit (the messages are folded
        into its body, or `message` names it). Nested batches fold into the outermost one."""
        with self.lock:
            self._batch_depth += 1
        try:
            yield self
        finally:
            with self.lock:
                self._batch_depth -= 1
                if self._batch_depth == 0 and self._batch_messages:
                    msgs, self._batch_messages = self._batch_messages, []
                    if message:
                        head = message
                    elif len(msgs) == 1:
                        head = msgs[0]
                    else:
                        head = f"{msgs[0]} (+{len(msgs) - 1} more)"
                    body = ("\n\n" + "\n".join(f"- {m}" for m in msgs)) if len(msgs) > 1 or message else ""
                    if self.is_repo:
                        self._commit_now(head + body)

    def save_many(self, files: Iterable[tuple[Path | str, str]], message: str) -> bool:
        """Write several files and commit them together."""
        with self.batch(message):
            for rel, text in files:
                self.write_text(rel, text)
            self.commit(message)
        return True

    def _schedule_push(self, delay: float = PUSH_DEBOUNCE_S) -> None:
        if not self.push_enabled:
            return
        with self.lock:
            if self._push_timer:
                self._push_timer.cancel()
            self._push_timer = threading.Timer(delay, self.push)
            self._push_timer.daemon = True  # never keeps the process alive; flush() is the shutdown path
            self._push_timer.start()

    def push(self, retries: int = PUSH_RETRIES) -> bool:
        if not (self.is_repo and self.push_enabled):
            return False
        r = None
        for attempt in range(retries):
            r = self._git("push", "--quiet", check=False)
            if r.returncode == 0:
                with self.lock:
                    self._unpushed = False
                return True
            if attempt + 1 < retries:  # someone edited via PR: rebase and retry
                self._git("pull", "--rebase", "--quiet", check=False)
                time.sleep(2 * (attempt + 1))
        log.error("data push failed after %d attempts: %s", retries, redact((r.stderr if r else "").strip())[:200])
        return False

    def flush(self) -> bool:
        """Shutdown hook: cancel the debounced timer and push now. Cheap when nothing is pending (no git call),
        so it is safe from `OibotGM.close()`, atexit and tests. Returns True when a push happened and succeeded."""
        with self.lock:
            if self._push_timer:
                self._push_timer.cancel()
                self._push_timer = None
            pending = self._unpushed and self.is_repo and self.push_enabled
        if not pending:
            return False
        return self.push(retries=1)

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


def is_fixture_root(code_root: Path, data_root: Path) -> bool:
    """True when the data root lives under the code repo's fixtures/ (demo data, never real guild state)."""
    try:
        data_root.resolve().relative_to((code_root / "fixtures").resolve())
        return True
    except ValueError:
        return False
