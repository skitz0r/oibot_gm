"""Menu-bar status for the Mac mini: is the bot up, is a job running, did one fail, are approvals waiting.

    uv run --with rumps python scripts/agents/menubar.py

(its own LaunchAgent, gg.earlyandoften.oibot.menubar; rumps is NOT a project dependency). It reads only local
things — the jobs' status files in out/agents/, `launchctl print`, the proposals in the data repo, and the bot's
/healthz — so it keeps working when the bot is down. The title is the worst state:
  GM ✕  the bot is down        GM ⚠  a job's last run failed
  GM ⟳  a job is running       GM ✓  all idle
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oibot_gm import agents  # noqa: E402

REFRESH_S = 10


def data_root() -> Path:
    from oibot_gm.store import resolve_data_root

    return resolve_data_root(agents.ROOT)


def waiting_approvals(root: Path | None = None) -> int:
    """Proposals waiting for an officer, read straight from the data repo (works with the bot down)."""
    import yaml

    n = 0
    for f in (root or data_root()).glob("*/proposals/*.yaml"):
        try:
            n += (yaml.safe_load(f.read_text()) or {}).get("state") == "proposed"
        except (OSError, yaml.YAMLError):
            continue
    return n


def bot_state(launchctl=None, healthy=None) -> tuple[str, str]:
    """('up'|'down'|'starting', line) from launchd and /healthz."""
    ld = (launchctl or agents.launchd_status)(agents.BOT_LABEL)
    if not ld.get("loaded"):
        return "down", "Bot: not loaded in launchd"
    if not ld.get("pid"):
        return "down", f"Bot: not running (last exit {ld.get('last_exit') or '?'})"
    ok = (healthy or healthz_ok)()
    return ("up", f"Bot: running (pid {ld['pid']})") if ok else ("starting", f"Bot: process {ld['pid']} but the web check doesn't answer")


def healthz_ok() -> bool:
    try:
        import httpx

        r = httpx.get(agents_url() + "/healthz", timeout=2)
        return r.status_code == 200 and bool(r.json().get("bot"))
    except Exception:  # noqa: BLE001 — anything but a clean answer is "not up yet"
        return False


def agents_url() -> str:
    import os

    return (os.environ.get("OIBOT_MCP_URL") or "http://127.0.0.1:8788").rstrip("/")


def snapshot(launchctl=None, root: Path | None = None, out: Path | None = None, healthy=None) -> dict:
    """Everything the menu shows, as data (tests call this without rumps)."""
    out = out or agents.OUT
    bot, bot_line = bot_state(launchctl, healthy)
    ov = agents.overview(out, None, launchctl)
    jobs = {j["job"]: j for j in ov["jobs"]}
    rv, ap = jobs["review"], jobs["apply"]
    try:
        waiting = waiting_approvals(root)
    except Exception:  # noqa: BLE001
        waiting = 0
    if bot != "up":
        title = "GM ✕"
    elif any(j["state"] == "failed" for j in jobs.values()):
        title = "GM ⚠"
    elif any(j["state"] == "running" for j in jobs.values()):
        title = "GM ⟳"
    else:
        title = "GM ✓"

    def job_line(j: dict) -> str:
        if j["state"] == "running":
            return f"{j['name']}: running — {j.get('step') or 'starting'}"
        last = f"last {j['finished_label'] or j['started_label'] or 'never'}" + (f" ({j['result']})" if j.get("result") else "")
        loaded = "" if j["launchd"].get("loaded") else " · not installed"
        return f"{j['name']}: {j['state']} · {last}" + (f" · next {j['next_label']}" if j.get("next_label") else "") + loaded

    lines = [bot_line, job_line(rv), f"Approvals waiting: {waiting}", job_line(ap)]
    return {"title": title, "lines": lines, "bot": bot}


def main() -> None:
    import rumps
    from dotenv import load_dotenv

    load_dotenv(agents.ROOT / ".env")  # OIBOT_WEB_URL / OIBOT_MCP_URL only; nothing is printed

    class App(rumps.App):
        def __init__(self):
            super().__init__("GM …", quit_button="Quit")
            self.info = [rumps.MenuItem("…") for _ in range(4)]
            self.menu = [*self.info, None, rumps.MenuItem("Open Agents page", callback=self.open_page),
                         rumps.MenuItem("Run review now", callback=self.run_review), rumps.MenuItem("Open log folder", callback=self.open_logs), None]
            self.timer = rumps.Timer(self.refresh, REFRESH_S)
            self.timer.start()
            self.refresh(None)

        def refresh(self, _):
            try:
                s = snapshot()
            except Exception as e:  # noqa: BLE001 — the menu must never die
                self.title = "GM ?"
                self.info[0].title = f"status unreadable: {e}"[:120]
                return
            self.title = s["title"]
            for item, line in zip(self.info, s["lines"]):
                item.title = line[:140]

        def open_page(self, _):
            import os

            public = os.environ.get("OIBOT_WEB_URL") or agents_url()
            subprocess.run(["open", public.rstrip("/") + "/app/agents"], check=False)

        def run_review(self, _):
            st = agents.read_status("review")
            if st["state"] == "running":
                rumps.notification("oibot_GM", "News review", "Already running.")
                return
            ok, out = agents.kickstart(agents.JOBS["review"]["label"])
            rumps.notification("oibot_GM", "News review", "Started." if ok else f"launchd refused: {out[:120]} — is it installed? (scripts/agents/install.sh)")
            self.refresh(None)

        def open_logs(self, _):
            (agents.OUT / "runs").mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(agents.OUT)], check=False)

    App().run()


if __name__ == "__main__":
    main()
