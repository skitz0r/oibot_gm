"""Pre-restart check: build the full command tree offline (no Discord connection) and fail on anything Discord would
reject — descriptions over 100 chars, a group past 25 subcommands (in words; /gm config is close to it), non-coroutine
autocompletes, decorator errors — then run the fast test suite
(`pytest -q -m "not slow"`; the solver tests are left to a full `uv run pytest -q`). Also fails when a guild folder in the
data repo would not load (a broken guild.yaml, a key that isn't its folder name, two guilds on one Discord server).
Exit code 1 on any failure."""
import subprocess
import sys
from pathlib import Path

import discord
from discord import app_commands

from oibot_gm import raidcycle as rc
from oibot_gm.discord_help import register_help_commands
from oibot_gm.discord_policy import PolicyContext, register_policy_commands
from oibot_gm.discord_raid import register_raid_commands
from oibot_gm.discord_registry import DISCORD_MAX_CHILDREN, Guilds, group_sizes, register_commands, tree_build_error, tree_limit_problems
from oibot_gm.ops import Ops
from oibot_gm.store import GitStore, resolve_data_root

ROOT = Path(__file__).resolve().parents[1]
store = GitStore(resolve_data_root(ROOT), push=False)
client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)
guilds = Guilds(store, ROOT)
if guilds.load_errors:  # a guild that won't load would be skipped by the bot (and two folders claiming one server: both)
    raise SystemExit("guild(s) that would not load:\n" + "\n".join(f"  {e}" for e in guilds.load_errors))


class Bot:  # just enough for registration
    raids = type("R", (), {"store": lambda self, reg: rc.RaidStore(store, reg.key)})()


try:  # a group past Discord's 25 subcommands fails HERE (discord.py refuses the 26th) — said in words, not a bare ValueError
    register_commands(tree, guilds, Ops(client), lambda k, v: "")
    register_raid_commands(tree, guilds, Ops(client), Bot())
    register_policy_commands(tree, guilds, Ops(client), Bot(), PolicyContext(guilds))
    register_help_commands(tree, guilds, Ops(client), Bot())
except ValueError as e:
    words = tree_build_error(e)
    if words is None:
        raise
    raise SystemExit(words) from None
bad, n = list(tree_limit_problems(tree)), 0


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
print(f"{n} commands OK", flush=True)
for path, size in sorted(group_sizes(tree).items()):
    if size >= DISCORD_MAX_CHILDREN - 2:  # the next builder's warning: a group about full takes no new subcommand lightly
        print(f"note: /{path} holds {size} of Discord's {DISCORD_MAX_CHILDREN} subcommands — fold a new one into a hub", flush=True)

# the fast tests (tests/, everything not marked slow); their output follows, ours is the verdict line
r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not slow"], cwd=ROOT)
if r.returncode != 0:
    raise SystemExit(f"tests failed (pytest exit {r.returncode}) — fix before restarting the bot")
print("tests OK")
