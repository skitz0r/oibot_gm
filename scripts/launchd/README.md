# Running the bot under launchd (Mac mini)

`gg.earlyandoften.oibot.plist` runs `uv run oibot discord` from the repo as a per-user agent: it starts at login,
restarts when the process exits (crash, `pkill`), and logs stdout/stderr to `out/launchd.log`. Rotating logs stay in
`out/oibot.log` (the bot's own handler). Secrets are read from the repo's `.env`, not from the plist.

The plist hard-codes the repo path (`/Users/jonathanlin/dev/oibot_gm`) and uv's Homebrew location
(`/opt/homebrew/bin/uv`, from `which uv`). Edit both if either moves.

## Install (once)

```sh
mkdir -p ~/Library/LaunchAgents out
cp scripts/launchd/gg.earlyandoften.oibot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/gg.earlyandoften.oibot.plist
```

`bootstrap` loads the agent and, because `RunAtLoad` is set, starts the bot right away. A user must be logged in
(GUI session) for the `gui/<uid>` domain to exist; enable automatic login on the mini if it should come back after a
power cut.

## Everyday

```sh
# restart after a deploy (the pre-restart routine still applies: uv run python scripts/check_commands.py)
launchctl kickstart -k gui/$(id -u)/gg.earlyandoften.oibot

# status: PID, last exit code
launchctl print gui/$(id -u)/gg.earlyandoften.oibot | head -20

# stop until next login (KeepAlive would otherwise bring it back)
launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot

# start again after a bootout
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/gg.earlyandoften.oibot.plist

# logs
tail -f out/launchd.log
```

After editing the plist: copy it to `~/Library/LaunchAgents/` again, then `bootout` + `bootstrap` (launchd reads the
file only at bootstrap).

## While the agent is installed

Do not also start the bot by hand (`nohup uv run oibot discord …`): two processes would answer the same Discord
token. `pkill -f "oibot discord"` is fine — launchd restarts it within `ThrottleInterval` (15 s) — but
`kickstart -k` is the cleaner restart.
