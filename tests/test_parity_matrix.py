"""The parity matrix (dual interfaces §2.1, §2.10): every op in configops.OP_GROUP — the one list of what the bot can be
asked to do — has a Discord path (a FLOWS entry, a slash command or a persistent card button), a web route and an MCP
tool, or a written-down exemption: configops.BY_DESIGN_EXEMPT (permanent, with the design's reason) or
configops.PARITY_PENDING (temporary, with the phase that adds it). Adding a verb to one surface fails here until its
row is filled in; retiring a surface's path fails here until the matrix says so.

`SURFACES` is the matrix itself: op → {discord, web, mcp}. Discord: "flow:<name>" (wizard.FLOWS), "slash:<path>" (the
command tree built offline, as scripts/check_commands.py builds it) or "button:<module>.<Class>" (a DynamicItem).
Web: "<METHOD> <path>" exactly as the route is declared. MCP: the tool's name (mcp_server's FastMCP list_tools)."""
from __future__ import annotations

import asyncio
import importlib

import discord
import pytest
from discord import app_commands
from test_mcp import FakeBot

from oibot_gm import configops, wizard_flows  # noqa: F401 — wizard_flows registers every FLOWS entry
from oibot_gm import mcp_server as ms
from oibot_gm.discord_help import register_help_commands
from oibot_gm.discord_policy import PolicyContext, register_policy_commands
from oibot_gm.discord_raid import register_raid_commands
from oibot_gm.discord_registry import Guilds, register_commands
from oibot_gm.ops import Ops
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS

SURFACES: dict[str, dict[str, str]] = {
    "absence": {"discord": "flow:absence", "web": "POST /api/me/absence", "mcp": "add_absence"},
    "absence_clear": {"discord": "slash:me absent clear", "web": "POST /api/me/absence/clear", "mcp": "clear_absence"},
    "dm": {"discord": "slash:me dms", "web": "POST /api/me/dm", "mcp": "set_dm"},
    "character": {"discord": "flow:member_char", "web": "POST /api/me/characters", "mcp": "save_characters"},  # /me char, /roster char
    "set_main": {"discord": "flow:member_char", "web": "POST /api/me/main", "mcp": "set_member"},
    "rank": {"discord": "flow:roster_rank", "web": "POST /api/members/set", "mcp": "set_member"},
    "confirm": {"discord": "flow:roster_confirm", "web": "POST /api/admin/confirm", "mcp": "set_member"},
    "run_open": {"discord": "flow:raid_open", "web": "POST /api/raid/{rid}/open", "mcp": "open_run"},
    "run_answer": {"discord": "flow:raid_set", "web": "POST /api/run/{key}/set", "mcp": "set_answer"},
    "run_lock": {"discord": "flow:raid_lock", "web": "POST /api/run/{key}/lock", "mcp": "lock_run"},
    "run_cancel": {"discord": "flow:raid_cancel", "web": "POST /api/run/{key}/cancel", "mcp": "cancel_run"},
    "run_fill": {"discord": "flow:raid_fill", "web": "POST /api/run/{key}/fill", "mcp": "fill_seats"},
    "run_strategy": {"web": "POST /api/run/{key}/strategy", "mcp": "set_strategy"},
    "run_autofill": {"web": "POST /api/run/{key}/autofill", "mcp": "autofill"},
    "run_board": {"web": "POST /api/run/{key}/layout", "mcp": "set_layout"},
    "run_move": {"web": "POST /api/run/{key}/move"},
    "run_propose": {"web": "POST /api/run/{key}/split", "mcp": "propose_split"},
    "run_sheet": {"discord": "flow:raid_sheet", "web": "POST /api/run/{key}/sheet", "mcp": "repost_sheet"},
    "confirm_for": {"discord": "flow:raid_set", "web": "POST /api/admin/placement", "mcp": "confirm_for"},
    # raid-night levers (phase 2 A): the lock / closed card's RunButton, the Suggest… screen, the run routes, the tools
    "run_remind": {"discord": "button:oibot_gm.raid_buttons.RunButton", "web": "POST /api/run/{key}/remind", "mcp": "remind_unconfirmed"},
    "run_pause": {"discord": "button:oibot_gm.raid_buttons.RunButton", "web": "POST /api/run/{key}/pause", "mcp": "pause_autofill"},
    "run_withdraw": {"discord": "flow:raid_suggest", "web": "POST /api/run/{key}/withdraw", "mcp": "withdraw_ask"},
    "run_reopen": {"discord": "button:oibot_gm.raid_buttons.RunButton", "web": "POST /api/run/{key}/reopen", "mcp": "reopen_run"},
    "comp_target": {}, "comp_target_clear": {}, "comp_groups": {},
    "pin": {"discord": "flow:raid_set", "web": "POST /api/run/{key}/pin", "mcp": "pin"},
    "policy_append": {"discord": "flow:rule", "web": "PUT /api/policy/{doc}", "mcp": "policy_edit"},
    "raid_set": {"discord": "flow:raid_config", "web": "POST /api/admin/raid", "mcp": "raid_set"},
    "raid_reset": {"discord": "flow:raid_config", "web": "POST /api/admin/raid/reset", "mcp": "raid_reset"},
    "schedule_set": {"discord": "flow:raid_config", "web": "POST /api/admin/raid/schedule", "mcp": "schedule_set"},
    "schedule_remove": {"discord": "flow:raid_config", "web": "POST /api/admin/raid/schedule/remove", "mcp": "schedule_remove"},
    "aura_set": {"discord": "flow:aura", "web": "POST /api/admin/aura", "mcp": "aura_set"},
    "family_set": {"discord": "flow:aura", "web": "POST /api/admin/family", "mcp": "family_set"},
    "aura_reset": {"discord": "flow:aura", "web": "POST /api/admin/aura/reset", "mcp": "aura_reset"},
    "set": {"discord": "slash:gm config ops-channel", "web": "POST /api/admin/config", "mcp": "set_config"},
    "role_add": {"discord": "slash:gm config officer-role", "web": "POST /api/admin/config", "mcp": "set_config"},
    "role_remove": {"discord": "slash:gm config officer-role", "web": "POST /api/admin/config", "mcp": "set_config"},
    "recruit": {"discord": "flow:recruitment", "web": "POST /api/admin/recruitment", "mcp": "set_recruitment"},
    "job": {"web": "POST /api/agents/run", "mcp": "run_job"},
    "plain": {"web": "POST /api/admin/plain-permissions", "mcp": "set_plain_permissions"},
    "application": {"discord": "button:oibot_gm.discord_registry.ApplicationButton", "web": "POST /api/admin/application/decide", "mcp": "decide_application"},
    "proposal": {"discord": "button:oibot_gm.discord_news.ProposalButton", "web": "POST /api/proposals/{pid}/resolve", "mcp": "resolve_proposal"},
    "policy": {"discord": "slash:gm policy edit", "web": "PUT /api/policy/{doc}", "mcp": "policy_edit"},
    "regular": {"discord": "flow:roster_add", "web": "POST /api/admin/raid/regulars", "mcp": "set_regular"},
    "test": {"discord": "slash:gm test seed", "web": "POST /api/admin/test", "mcp": "test_bench"},
    # the loot council (dual interfaces phase 1): the thread is the Discord surface (/raid loot opens it), Loot › Tonight the
    # web's, the loot_* tools MCP's; a wishlist is the member's own list (Me › Wishlist, /me wishlist); tiers are the grid
    "loot": {"discord": "slash:raid loot", "web": "POST /api/loot/{run}/confirm", "mcp": "loot_confirm"},
    "wishlist": {"discord": "flow:me_wishlist", "web": "POST /api/me/wishlist", "mcp": "set_wishlist"},
    "tier": {"web": "POST /api/loot/items/{key}/tiers", "mcp": "tier_item"},
}
SURFACE_NAMES = ("discord", "web", "mcp")


@pytest.fixture(scope="module")
def mcp_tools() -> set[str]:
    return {t.name for t in asyncio.run(ms.mcp.list_tools())}


@pytest.fixture
def web_routes(reg, rs) -> set[str]:
    app = web.create_app(FakeBot(reg, rs))
    return {f"{m} {r.path}" for r in app.routes for m in (getattr(r, "methods", None) or ()) if m not in ("HEAD", "OPTIONS")}


@pytest.fixture
def slash_paths(reg) -> set[str]:
    """Every leaf command's path ('gm config ops-channel'), from the tree built offline as check_commands.py builds it."""
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guilds = Guilds.of(reg.store, reg)

    class Bot:
        raids = type("R", (), {"store": lambda self, r: None})()

    register_commands(tree, guilds, Ops(client), lambda k, v: "")
    register_raid_commands(tree, guilds, Ops(client), Bot())
    register_policy_commands(tree, guilds, Ops(client), Bot(), PolicyContext(guilds))
    register_help_commands(tree, guilds, Ops(client), Bot())
    out = set()

    def walk(c, prefix=""):
        name = f"{prefix} {c.name}".strip()
        subs = getattr(c, "commands", None)
        if subs:
            for s in subs:
                walk(s, name)
        else:
            out.add(name)

    for c in tree.get_commands():
        walk(c)
    return out


def discord_path_exists(ref: str, slash_paths: set[str]) -> bool:
    kind, _, name = ref.partition(":")
    if kind == "flow":
        return name in FLOWS
    if kind == "slash":
        return name in slash_paths
    if kind == "button":
        mod, _, cls = name.rpartition(".")
        item = getattr(importlib.import_module(mod), cls, None)
        return item is not None and issubclass(item, discord.ui.DynamicItem)
    if kind == "fn":
        mod, _, fn = name.rpartition(".")
        return callable(getattr(importlib.import_module(mod), fn, None))
    return False


def test_every_op_is_in_the_matrix_and_the_matrix_names_only_ops():
    assert set(SURFACES) == set(configops.OP_GROUP), "an op without a row: say where it lives on each surface"
    for table in (configops.BY_DESIGN_EXEMPT, configops.PARITY_PENDING):
        assert set(table) <= set(configops.OP_GROUP), "an exemption for an op that doesn't exist"
        for op, by_surface in table.items():
            assert set(by_surface) <= set(SURFACE_NAMES)
            for surface, reason in by_surface.items():
                assert isinstance(reason, str) and len(reason) > 15, f"{op}/{surface}: an exemption needs a reason"
                assert surface not in SURFACES[op], f"{op}/{surface}: both a path and an exemption — one or the other"
    both = {op for op in configops.BY_DESIGN_EXEMPT if op in configops.PARITY_PENDING and set(configops.BY_DESIGN_EXEMPT[op]) & set(configops.PARITY_PENDING[op])}
    assert not both, f"{both}: by design and pending at once"


def test_every_op_has_every_surface_or_a_written_exemption(mcp_tools, web_routes, slash_paths):
    missing, dangling = [], []
    for op in configops.OP_GROUP:
        row = SURFACES[op]
        for surface in SURFACE_NAMES:
            ref = row.get(surface)
            exempt = configops.BY_DESIGN_EXEMPT.get(op, {}).get(surface) or configops.PARITY_PENDING.get(op, {}).get(surface)
            if ref is None:
                if not exempt:
                    missing.append(f"{op}: no {surface} path and no exemption")
                continue
            ok = discord_path_exists(ref, slash_paths) if surface == "discord" else (ref in web_routes if surface == "web" else ref in mcp_tools)
            if not ok:
                dangling.append(f"{op}/{surface}: {ref!r} does not exist")
    assert not missing, "\n".join(missing)
    assert not dangling, "\n".join(dangling)


def test_pending_rows_are_few_and_dated_to_a_phase():
    """PARITY_PENDING is the honest list of what phase 2/3 still owes; it must not grow into a second exemption table."""
    for op, by_surface in configops.PARITY_PENDING.items():
        for surface, reason in by_surface.items():
            assert "phase" in reason, f"{op}/{surface}: a pending gap names the phase that closes it"
    assert len(configops.PARITY_PENDING) <= 6


def test_single_surface_features_are_written_down_with_a_living_path(web_routes, slash_paths):
    """§2.1's by-design lists (Discord-only: claim, the application wizard, the cards, the guide, the DM
    picker; web-only: the board, the comp sandbox, test-bench compose, applications review, the policy editor, history,
    the plain-permissions grid) are configops.BY_DESIGN_ONLY: features, not ops, each with its surface, its entry point
    (which must exist) and the reason."""
    # owner transfer left this list in phase 2 (C): /gm config owner AND the Config page (POST /api/owner) — tests/test_people.py
    expected = {"guild_claim", "application_wizard", "registration_card", "absences_card", "guide", "dm_picker",
                "board", "comp_sandbox", "test_bench_compose", "applications_review", "policy_editor", "history", "plain_permissions_grid"}
    assert expected <= set(configops.BY_DESIGN_ONLY)
    assert not set(configops.BY_DESIGN_ONLY) & set(configops.OP_GROUP), "an op belongs in BY_DESIGN_EXEMPT, not here"
    for feature, row in configops.BY_DESIGN_ONLY.items():
        assert set(row) == {"surface", "path", "reason"} and row["surface"] in ("discord", "web"), feature
        assert len(row["reason"]) > 15, f"{feature}: a reason, not a tag"
        if row["surface"] == "discord":
            assert discord_path_exists(row["path"], slash_paths), f"{feature}: {row['path']!r} does not exist"
        else:
            assert row["path"] in web_routes, f"{feature}: {row['path']!r} is not a declared route"
