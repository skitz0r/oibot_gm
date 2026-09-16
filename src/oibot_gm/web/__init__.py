"""Officer/member web dashboard, served from inside the bot process (it needs the live objects:
registries, raid stores, ops ring, LLM spend, companions). Bound to localhost; a Cloudflare Tunnel
publishes it. Discord OAuth2 identifies the visitor; the bot's own rules decide what they may see."""
