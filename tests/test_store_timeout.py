"""A git call can't hang the bot: a timeout reads as a failed call (124), prompts are off."""
import subprocess

import pytest

from oibot_gm.store import GitStore


def test_git_timeout_is_a_failed_call(tmp_path, monkeypatch):
    s = GitStore(tmp_path, push=False)
    seen = {}

    def hang(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    r = s._git("push", "--quiet", check=False)
    assert r.returncode == 124 and seen["timeout"] == 60 and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    with pytest.raises(subprocess.CalledProcessError):
        s._git("status", check=True)
    assert seen["timeout"] == 30
