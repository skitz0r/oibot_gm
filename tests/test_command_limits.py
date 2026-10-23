"""Discord's limits on the command tree, checked offline (review item 9): a group holds at most 25 subcommands, and
`/gm config` is close to it (23 in October 2026). discord.py refuses the 26th while the commands are being registered with a bare ValueError;
`discord_registry.tree_build_error` turns that into words naming the group, so the next builder knows what to do."""
from __future__ import annotations

import discord
import pytest
from discord import app_commands

from oibot_gm import discord_registry as dr
from oibot_gm.discord_registry import DISCORD_MAX_CHILDREN, Guilds, group_sizes, register_commands, tree_build_error, tree_limit_problems
from oibot_gm.ops import Ops


def build(reg) -> app_commands.CommandTree:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    try:
        register_commands(tree, Guilds.of(reg.store, reg), Ops(client), lambda k, v: "")
    except ValueError as e:
        words = tree_build_error(e)
        if words:
            pytest.fail(words)  # a 26th subcommand: the warning in words, not discord.py's bare message
        raise
    return tree


def test_every_group_fits_discords_25_subcommands(reg):
    tree = build(reg)
    sizes = group_sizes(tree)
    assert "gm config" in sizes and sizes["gm config"] <= DISCORD_MAX_CHILDREN, dr.too_many_subcommands("gm config", sizes.get("gm config", 0))
    assert not tree_limit_problems(tree)


def test_a_26th_subcommand_is_refused_in_words(reg):
    tree = build(reg)
    gm = next(c for c in tree.get_commands() if c.name == "gm")
    config = next(c for c in gm.commands if c.name == "config")
    room = DISCORD_MAX_CHILDREN - len(config.commands)

    async def cb(interaction: discord.Interaction) -> None:
        return None

    with pytest.raises(ValueError) as caught:
        for k in range(room + 1):
            config.add_command(app_commands.Command(name=f"extra{k}", description="x", callback=cb))
    words = tree_build_error(caught.value)
    assert words is not None and words.startswith("/gm config would have 26 subcommands") and "Discord allows 25" in words
    assert "Fold the new one into an existing hub" in words
    assert tree_build_error(ValueError("something else")) is None
    assert tree_limit_problems(tree) == [dr.too_many_subcommands("gm config", 26)], "the offline check sees the 26th too"
