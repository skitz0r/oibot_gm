"""Build the full command tree offline (no Discord connection) and fail on anything Discord would reject:
descriptions over 100 chars, non-coroutine autocompletes, decorator errors. Run before restarting the bot."""
from pathlib import Path

import discord
from discord import app_commands

from oibot_gm import raidcycle as rc
from oibot_gm.discord_help import register_help_commands
from oibot_gm.discord_policy import PolicyContext, register_policy_commands
from oibot_gm.discord_raid import register_raid_commands
from oibot_gm.discord_registry import Guilds, register_commands
from oibot_gm.ops import Ops
from oibot_gm.store import GitStore, resolve_data_root

ROOT = Path(__file__).resolve().parents[1]
store = GitStore(resolve_data_root(ROOT), push=False)
client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)
guilds = Guilds(store, ROOT)


class Bot:  # just enough for registration
    raids = type("R", (), {"store": lambda self, reg: rc.RaidStore(store, reg.key)})()


register_commands(tree, guilds, Ops(client), lambda k, v: "")
register_raid_commands(tree, guilds, Ops(client), Bot())
register_policy_commands(tree, guilds, Ops(client), Bot(), PolicyContext(guilds))
register_help_commands(tree, guilds, Ops(client), Bot())
bad, n = [], 0


def walk(c, p=""):
    global n
    name = f"{p} {c.name}".strip()
    subs = getattr(c, "commands", None)
    if subs:
        for s in subs:
            walk(s, name)
    else:
        n += 1
        if len(c.description or "") > 100:
            bad.append(f"/{name}: description {len(c.description)} chars")


for c in tree.get_commands():
    walk(c)
if bad:
    raise SystemExit("\n".join(bad))
print(f"{n} commands OK")
