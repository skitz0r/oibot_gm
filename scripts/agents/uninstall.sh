#!/bin/sh
# Stop and remove the news-review, apply and menu-bar LaunchAgents (the bot's own agent is left alone).
#   sh scripts/agents/uninstall.sh              # all three
#   sh scripts/agents/uninstall.sh apply        # one (review | apply | menubar)
# Status files and transcripts in out/agents stay; delete that folder to forget them.
set -eu
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
JOBS="${*:-review apply menubar}"

for job in $JOBS; do
  case "$job" in
    review) l=gg.earlyandoften.oibot.news-review ;;
    apply) l=gg.earlyandoften.oibot.apply ;;
    menubar) l=gg.earlyandoften.oibot.menubar ;;
    *) echo "unknown job $job (review | apply | menubar)" >&2; exit 2 ;;
  esac
  launchctl bootout "$DOMAIN/$l" 2>/dev/null || true
  rm -f "$AGENTS/$l.plist"
  echo "removed $l"
done
