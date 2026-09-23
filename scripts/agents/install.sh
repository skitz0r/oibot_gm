#!/bin/sh
# Install (or reinstall) the news-review, apply and menu-bar LaunchAgents. Does not touch the bot's own agent.
#   sh scripts/agents/install.sh              # all three
#   sh scripts/agents/install.sh review apply # some of them (review | apply | menubar)
# Undo: sh scripts/agents/uninstall.sh
set -eu
cd "$(dirname "$0")/../.."
REPO="$(pwd)"
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
JOBS="${*:-review apply menubar}"

label() {
  case "$1" in
    review) echo gg.earlyandoften.oibot.news-review ;;
    apply) echo gg.earlyandoften.oibot.apply ;;
    menubar) echo gg.earlyandoften.oibot.menubar ;;
    *) echo "unknown job $1 (review | apply | menubar)" >&2; exit 2 ;;
  esac
}

mkdir -p "$REPO/out/agents/runs" "$AGENTS"
command -v claude >/dev/null || echo "warning: claude is not on PATH here; the plists look in ~/.local/bin" >&2
uv run python scripts/agents/review.py --dry-run >/dev/null  # fails early if the MCP server or the tool list is broken

for job in $JOBS; do
  l="$(label "$job")"
  src="$REPO/scripts/launchd/$l.plist"
  plutil -lint "$src" >/dev/null
  cp "$src" "$AGENTS/"
  launchctl bootout "$DOMAIN/$l" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$l.plist"
  echo "installed $l"
done
echo "status: launchctl print $DOMAIN/<label> | head -20 · monitor: the site's Agents page, or the GM item in the menu bar"
