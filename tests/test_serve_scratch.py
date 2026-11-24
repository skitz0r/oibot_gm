"""scripts/serve_scratch.py and scripts/shots.py (phase 4, Task 6): screenshots never touch the live bot — the scratch
server refuses a real bot's environment, the live port and a data root outside the temp directory; shots.py has no
default site."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _scratch():
    spec = importlib.util.spec_from_file_location("serve_scratch", ROOT / "scripts" / "serve_scratch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv,env", [
    (["--data", "/tmp/x"], {"DISCORD_TOKEN": "real"}),
    (["--port", "8788"], {}),
    (["--data", str(ROOT.parent / "oibot_gm-data")], {}),
    (["--data", str(Path.home() / "scratch-data")], {}),
])
def test_the_scratch_server_refuses_anything_real(monkeypatch, argv, env):
    mod = _scratch()
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["serve_scratch.py", *argv])
    built = []
    monkeypatch.setattr(mod, "build_data", lambda *a: built.append(a))
    with pytest.raises(SystemExit) as e:
        mod.main()
    assert e.value.code == 2 and not built, "refused before any data is built"


def test_the_scratch_data_root_is_under_the_temp_directory(tmp_path):
    mod = _scratch()
    assert mod.under_temp(tmp_path / "data") and not mod.under_temp(ROOT.parent / "oibot_gm-data")


def test_shots_has_no_default_site():
    src = (ROOT / "scripts" / "shots.py").read_text()
    assert 'default="http://127.0.0.1:8788"' not in src and "ap.error(" in src
    assert '"/app/readiness"' not in src and "?view=priority" in src and "?view=readiness" in src and "[data-list-item]" in src
