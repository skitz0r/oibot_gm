# Running the bot under launchd (Mac mini)

The bot runs as a **per-user LaunchAgent** (`gui/<uid>` domain), the same way the Cloudflare tunnel does
(`~/Library/LaunchAgents/com.cloudflare.cloudflared.plist`). It is an agent, not a boot-time daemon, on purpose:
pushes to the private data repo authenticate through the **login keychain** (`credential.helper osxkeychain`),
which only exists once the user is logged in.

## Coming back after a power cut
Three things have to be true; check them once:

1. **The Mac powers on by itself**: `pmset -g | grep autorestart` shows `1` (`sudo pmset -a autorestart 1`).
2. **The user logs in by itself**: System Settings → Users & Groups → *Automatically log in as* → your account.
   Needs FileVault off (`fdesetup status`). To keep the console closed, also set *Require password after screen
   saver begins: immediately* and a short screen-saver delay: the session exists, the screen is locked.
3. **Agents load at login**: this plist and the cloudflared one are in `~/Library/LaunchAgents` (`RunAtLoad`).

`KeepAlive` restarts the bot when it exits or crashes (15 s throttle). On SIGTERM the bot flushes unpushed
data-repo commits (`ExitTimeOut` 30 s); on start it pulls.

## Commands
```
# install / reinstall after editing the plist
cp scripts/launchd/gg.earlyandoften.oibot.plist ~/Library/LaunchAgents/
launchctl bootout   gui/$(id -u)/gg.earlyandoften.oibot 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/gg.earlyandoften.oibot.plist

# restart (after a code change; run scripts/check_commands.py first)
launchctl kickstart -k gui/$(id -u)/gg.earlyandoften.oibot

# status / stop
launchctl print gui/$(id -u)/gg.earlyandoften.oibot | head -20
launchctl bootout gui/$(id -u)/gg.earlyandoften.oibot
```
Logs: `out/oibot.log` (rotating, the bot's own) and `out/launchd.log` (stdout/stderr of the process).
Do not also start the bot with `nohup`: two processes would fight over the Discord gateway and the web port.

## The news review, apply and menu-bar agents
`gg.earlyandoften.oibot.news-review.plist` (daily 9:00 AM), `gg.earlyandoften.oibot.apply.plist` (every 15 minutes) and
`gg.earlyandoften.oibot.menubar.plist` (the GM status item) sit next to this one; they are installed and removed
with `scripts/agents/install.sh` / `uninstall.sh`. How they work and how to read the monitor: `scripts/agents/README.md`.

## Why not Docker / a LaunchDaemon
Docker Desktop on macOS only starts after login too, and adds a VM between the bot and the things it needs on
the host (data repo + git credentials, `.env`, the tunnel to 127.0.0.1:8788, the MCP server and local agents).
A LaunchDaemon starts before login but cannot read the login keychain, so data pushes would fail until someone
switches the data repo to a deploy key. On a Linux host, package it as a container instead.
