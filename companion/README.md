# oibot companion

Streams loot events from your WoW chat log to oibot_GM so nobody types drops or
awards into Discord. It reads `WoWChatLog.txt` (a file the game writes when you
run `/chatlog`) and nothing else: no memory reading, no addon networking, no input.

## Run (Windows, the master looter's PC)

1. Install Tailscale and join the tailnet the bot's machine is on.
2. `pip install websockets`
3. In game: `/chatlog` (once per session; the companion warns if the file goes stale).
4. ```
   python oibot_companion.py --server ws://<mini-magicdns>:8787/feed --token <OIBOT_FEED_TOKEN> ^
       --character Rhozy --log "C:\Program Files (x86)\World of Warcraft\_classic_\Logs\WoWChatLog.txt"
   ```

Events it emits: `drop` (Gargul/RCLC announcements with item links), `loot`
(`X receives loot: [item]`), `kill` (boss-mod "defeated" lines), `heartbeat`.
Each carries a chat-log timestamp and an idempotency key, so reconnects never
double-count.

## Test without the game

```
python oibot_companion.py --replay sample_chatlog.txt --dry-run     # just parse
python oibot_companion.py --replay sample_chatlog.txt --token … --server ws://127.0.0.1:8787/feed
```

## Bot side

Set `OIBOT_FEED_TOKEN` (any long random string) and `OIBOT_FEED_BIND=<tailnet-ip>:8787`
in the bot's `.env`. The listener only accepts frames after a `hello` with the token.
When the bot looks after several Discord servers, give each server's companion that server's own token,
`OIBOT_FEED_TOKEN_<KEY>` (guild key in capitals, `-` as `_`): it feeds that server's raid only, whatever `--guild`
says, and the shared token then feeds no real raid. Giving a server's token the shared value keeps an existing
companion working unchanged.
`/gm status` shows connected companions; the ops feed warns when one goes silent mid-raid.
