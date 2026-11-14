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
    # comp targets (phase 3, RD-12): /roster comp, the Readiness page + the Raids page's Desired comp (typed `comp` changes), MCP;
    # comp_groups is plain text only on every surface (D48): /gm change, the officer pages' Tell the bot box, plain_change
    "comp_target": {"discord": "flow:roster_comp", "web": "POST /api/ops/change", "mcp": "set_comp_target"},
    "comp_target_clear": {"discord": "flow:roster_comp", "web": "POST /api/ops/change", "mcp": "set_comp_target"},
    "comp_groups": {"discord": "slash:gm change", "web": "POST /api/ops/change", "mcp": "plain_change"},
    "pin": {"discord": "flow:raid_set", "web": "POST /api/run/{key}/pin", "mcp": "pin"},
    "policy_append": {"discord": "flow:rule", "web": "POST /api/policy/{doc}/append", "mcp": "policy_append"},  # the one append verb (design §5.45)
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
    "proposal": {"discord": "flow:proposal_decide", "web": "POST /api/proposals/{pid}/resolve", "mcp": "resolve_proposal"},  # the card opens the decide flow
    "policy": {"discord": "slash:gm policy edit", "web": "PUT /api/policy/{doc}", "mcp": "policy_edit"},
    "regular": {"discord": "flow:roster_add", "web": "POST /api/admin/raid/regulars", "mcp": "set_regular"},
    "test": {"discord": "slash:gm test seed", "web": "POST /api/test/seed", "mcp": "test_bench"},  # every action: TEST_BENCH below
    # the loot council (dual interfaces phase 1): the thread is the Discord surface (/raid loot opens it), Loot › Tonight the
    # web's, the loot_* tools MCP's; a wishlist is the member's own list (Me › Wishlist, /me wishlist); tiers are the grid
    "loot": {"discord": "slash:raid loot", "web": "POST /api/loot/{run}/confirm", "mcp": "loot_confirm"},
    "wishlist": {"discord": "flow:me_wishlist", "web": "POST /api/me/wishlist", "mcp": "set_wishlist"},
    "tier": {"web": "POST /api/loot/items/{key}/tiers", "mcp": "tier_item"},
    # the scoring weights (phase 4, §5.48): Config › Loot scoring (useOpsSave → /api/ops/change; POST /api/loot/weights is the
    # translator), MCP set_loot_weights; in Discord the owner says it in plain text (/gm change) — a table of numbers has no wizard
    "loot_weights": {"discord": "slash:gm change", "web": "POST /api/loot/weights", "mcp": "set_loot_weights"},
}
SURFACE_NAMES = ("discord", "web", "mcp")
# the `test` op's actions, one row each (dual interfaces phase 2, B9: both halves of the test bench on both surfaces):
# Discord = /gm test <action>, web = /api/test/<action>, MCP = test_bench(action=…). The feed is the web's mirror of a
# rehearsal (D20); its Discord side IS the cards and the digest, so it has no command.
TEST_BENCH: dict[str, dict[str, str]] = {
    "seed": {"discord": "slash:gm test seed", "web": "POST /api/test/seed", "mcp": "test_bench"},
    "run": {"discord": "slash:gm test run", "web": "POST /api/test/run", "mcp": "test_bench"},
    "answer": {"discord": "slash:gm test answer", "web": "POST /api/test/answer", "mcp": "test_bench"},
    "compose": {"discord": "slash:gm test compose", "web": "POST /api/test/compose", "mcp": "test_bench"},
    "comp": {"discord": "slash:gm test comp", "web": "POST /api/test/comp", "mcp": "test_bench"},
    "clear": {"discord": "slash:gm test clear", "web": "POST /api/test/clear", "mcp": "test_bench"},
    "answer_digest": {"discord": "button:oibot_gm.flows_test.TestAnswerButton", "web": "POST /api/test/answer", "mcp": "test_bench"},
    "feed": {"web": "GET /api/test/feed", "mcp": "test_bench"},
}


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
    """§2.1's by-design lists (Discord-only: claim, owner transfer, the application wizard, the cards, the guide, the DM
    picker; web-only: the board, applications review, the policy editor, history,
    the plain-permissions grid) are configops.BY_DESIGN_ONLY: features, not ops, each with its surface, its entry point
    (which must exist) and the reason."""
    # owner transfer is Discord-only (§2.1): /gm config owner in the server — tests/test_people.py
    expected = {"guild_claim", "owner_transfer", "application_wizard", "registration_card", "absences_card", "guide", "dm_picker",
                "board", "applications_review", "policy_editor", "history", "plain_permissions_grid", "memories_list", "my_memories"}
    assert expected <= set(configops.BY_DESIGN_ONLY)
    # phase 2 B9: the comp sandbox and test-bench compose are on both surfaces now (/gm test comp | compose) — never web-only again
    assert not {"comp_sandbox", "test_bench_compose"} & set(configops.BY_DESIGN_ONLY)
    assert not set(configops.BY_DESIGN_ONLY) & set(configops.OP_GROUP), "an op belongs in BY_DESIGN_EXEMPT, not here"
    for feature, row in configops.BY_DESIGN_ONLY.items():
        assert set(row) == {"surface", "path", "reason"} and row["surface"] in ("discord", "web"), feature
        assert len(row["reason"]) > 15, f"{feature}: a reason, not a tag"
        if row["surface"] == "discord":
            assert discord_path_exists(row["path"], slash_paths), f"{feature}: {row['path']!r} does not exist"
        else:
            assert row["path"] in web_routes, f"{feature}: {row['path']!r} is not a declared route"


def test_recipe_rename_and_merge_are_retired_and_the_unmatched_fix_is_web_only(mcp_tools, web_routes):
    """armory link C7: recipe names are the game's, so Rename… / Merge… are gone from every surface; the officers' fix
    for a recipe typed before the list (§4.3) lives beside the list of them on the Professions page."""
    assert not {"POST /api/professions/rename", "POST /api/professions/merge"} & web_routes
    assert not {"rename_recipe", "merge_recipe"} & mcp_tools
    assert configops.BY_DESIGN_ONLY["recipe_fix"]["path"] == "POST /api/professions/unmatched"


def test_every_test_bench_action_is_on_every_surface(mcp_tools, web_routes, slash_paths):
    """Both halves of the test bench on both surfaces (B9): each action of the `test` op has its Discord path, its web
    route and the MCP tool; the feed is web + MCP by design (Discord's side of a rehearsal is the cards and the digest)."""
    missing = []
    for action, row in TEST_BENCH.items():
        for surface in SURFACE_NAMES:
            ref = row.get(surface)
            if ref is None:
                if not (action == "feed" and surface == "discord"):
                    missing.append(f"test {action}: no {surface} path")
                continue
            ok = discord_path_exists(ref, slash_paths) if surface == "discord" else (ref in web_routes if surface == "web" else ref in mcp_tools)
            if not ok:
                missing.append(f"test {action}/{surface}: {ref!r} does not exist")
    assert not missing, "\n".join(missing)


# loot appeals (L32, phase 4 — design §5.49): not an op (no plain text: the flow has NO LLM by design, and a member's words
# are never parsed), so their rows live here. MCP reads and resolves (resolving is a decision about a person: D8, the
# actor header) but opens none (an appeal is the member's own voice; the token is the owner's).
APPEALS: dict[str, dict[str, str]] = {
    "open": {"discord": "flow:me_loot_ask", "web": "POST /api/me/loot/{award}/appeal"},
    "open_command": {"discord": "slash:me loot-ask"},
    "list": {"discord": "button:oibot_gm.appeals.AppealButton", "web": "GET /api/loot/appeals", "mcp": "list_appeals"},  # the card's Details; Loot › Appeals
    "resolve": {"discord": "flow:appeal_resolve", "web": "POST /api/loot/appeals/{aid}/resolve", "mcp": "resolve_appeal"},
}
APPEALS_MCP_EXEMPT = {
    "open": "an appeal is a member's own words about their own award (Discord or the Me page); MCP's token is the owner's",
    "open_command": "the slash command is Discord's entry to the same flow",
}


def test_appeals_have_their_discord_and_web_paths_and_written_mcp_exemptions(mcp_tools, web_routes, slash_paths):
    missing = []
    for action, row in APPEALS.items():
        if "discord" in row and not discord_path_exists(row["discord"], slash_paths):
            missing.append(f"appeal {action}/discord: {row['discord']!r}")
        if "web" in row and row["web"] not in web_routes:
            missing.append(f"appeal {action}/web: {row['web']!r}")
        if "mcp" in row:
            assert action not in APPEALS_MCP_EXEMPT, f"appeal {action}: has an MCP tool AND an exemption"
            if row["mcp"] not in mcp_tools:
                missing.append(f"appeal {action}/mcp: {row['mcp']!r}")
        else:
            assert len(APPEALS_MCP_EXEMPT.get(action, "")) > 15, f"appeal {action}: no MCP tool — say why"
    assert not missing, "\n".join(missing)


# the guild guide library (design §5.63): not ops (no plain text: a
# BattleTag and an import string must never reach a model), so their rows live here. Upload and get are the member's own
# with their own BattleTag (configops.BY_DESIGN_ONLY guide_upload / guide_get: the Me page and /guide upload|get); MCP
# lists, withdraws and restores (an officer's decision: D8, the actor header).
GUIDE_LIBRARY: dict[str, dict[str, str]] = {
    "list": {"discord": "flow:guide_list", "web": "GET /api/guides/library", "mcp": "list_guild_guides"},
    "list_command": {"discord": "slash:guide list"},
    "upload": {"discord": "flow:guide_upload", "web": "POST /api/guides/library/upload"},
    "upload_command": {"discord": "slash:guide upload"},
    "get": {"discord": "flow:guide_get", "web": "POST /api/guides/library/{vid}/download"},
    "get_command": {"discord": "slash:guide get"},
    "withdraw": {"discord": "flow:guide_withdraw", "web": "POST /api/guides/library/{vid}/withdraw", "mcp": "withdraw_guild_guide"},
    "withdraw_command": {"discord": "slash:guide withdraw"},
    "restore": {"discord": "flow:guide_restore", "web": "POST /api/guides/library/{vid}/restore", "mcp": "restore_guild_guide"},
    "restore_command": {"discord": "slash:guide restore"},
}
GUIDE_LIBRARY_MCP_EXEMPT = {
    "upload": "the uploader's BattleTag unlocks the string once on the Me page or in Discord; MCP's token is the owner's and a transcript would keep both",
    "get": "a version is locked to the requester's own BattleTag and handed to them alone; the string must not land in a transcript",
    **{f"{a}_command": "the slash command is Discord's entry to the same flow" for a in ("list", "upload", "get", "withdraw", "restore")},
}


def test_the_guide_library_has_its_discord_and_web_paths_mcp_tools_and_written_exemptions(mcp_tools, web_routes, slash_paths):
    missing = []
    for action, row in GUIDE_LIBRARY.items():
        if not discord_path_exists(row["discord"], slash_paths):
            missing.append(f"guide {action}/discord: {row['discord']!r}")
        if "web" in row and row["web"] not in web_routes:
            missing.append(f"guide {action}/web: {row['web']!r}")
        if "mcp" in row:
            assert action not in GUIDE_LIBRARY_MCP_EXEMPT, f"guide {action}: has an MCP tool AND an exemption"
            if row["mcp"] not in mcp_tools:
                missing.append(f"guide {action}/mcp: {row['mcp']!r}")
        else:
            assert len(GUIDE_LIBRARY_MCP_EXEMPT.get(action, "")) > 15, f"guide {action}: no MCP tool — say why"
    assert not missing, "\n".join(missing)
    assert {"guide_upload", "guide_get"} <= set(configops.BY_DESIGN_ONLY)
    assert not any("guide" in t and ("upload" in t or "download" in t or "issue" in t) for t in mcp_tools), "MCP never uploads or fetches a guild guide"


# a character's armory link (armory link §5.2, §5.4, §5.5): features, not ops — never plain text (configops.BY_DESIGN_ONLY
# armory_link / armory_seed / armory_move). The site's routes (web/api_armory.py), MCP's tools and Discord's wizards: the
# character wizard (flow member_char: Link to the armory… / Unlink… / Keep professions by hand / Sync now / Move to another
# member…) and /roster armory's sure batch (flow roster_armory).
ARMORY: dict[str, dict[str, str]] = {
    "find": {"web": "POST /api/me/armory/find", "discord": "flow:member_char", "mcp": "link_character"},  # MCP: link_character(preview=true)
    "find_for": {"web": "POST /api/members/armory/find", "discord": "flow:member_char", "mcp": "link_character"},
    "link": {"web": "POST /api/me/armory/link", "discord": "flow:member_char", "mcp": "link_character"},
    "link_for": {"web": "POST /api/members/armory/link", "discord": "flow:member_char", "mcp": "link_character"},
    "unlink": {"web": "POST /api/me/armory/unlink", "discord": "flow:member_char", "mcp": "unlink_character"},
    "unlink_for": {"web": "POST /api/members/armory/unlink", "discord": "flow:member_char", "mcp": "unlink_character"},
    "switch": {"web": "POST /api/me/armory/switch", "discord": "flow:member_char"},
    "switch_for": {"web": "POST /api/members/armory/switch", "discord": "flow:member_char"},
    "sync": {"web": "POST /api/me/armory/sync", "discord": "flow:member_char"},
    "sync_for": {"web": "POST /api/members/armory/sync", "discord": "flow:member_char"},
    "give": {"web": "POST /api/members/armory/give"},
    "map": {"web": "GET /api/armory/map", "mcp": "armory_map"},
    "seed": {"web": "GET /api/armory/seed", "mcp": "armory_seed"},
    "seed_apply": {"web": "POST /api/armory/seed/apply", "discord": "flow:roster_armory", "mcp": "apply_armory_seed"},
    "move": {"web": "POST /api/members/move", "discord": "flow:member_char", "mcp": "move_character"},
}
ARMORY_MCP_EXEMPT = {
    "switch": "how a character's professions are kept is the member's own choice on the Me page or in Discord; no MCP tool was asked for",
    "switch_for": "an officer flips it on the Members page or in Discord's character wizard; no MCP tool was asked for",
    "sync": "Sync now is a refresh a person presses beside the character; the hourly check does the same for MCP's reads",
    "sync_for": "Sync now is a refresh a person presses beside the character; the hourly check does the same for MCP's reads",
    "give": "Give to a member… registers a character from the guild's roster on the Members page's not-claimed fold; MCP links an existing one",
}
ARMORY_DISCORD_PENDING: dict[str, str] = {}  # every armory action Discord has landed (task 12)
ARMORY_DISCORD_EXEMPT = {
    "give": "Give to a member… lives on the site's not-claimed fold beside the roster rows (§5.4); Discord shows the counts",
    "map": "the map is a table of every member's characters (the Members page); Discord shows the counts in /roster armory",
    "seed": "the per-row review is the site's (task 12 designer's call); Discord shows the counts and links the sure batch",
}


def test_the_armory_link_has_web_routes_mcp_tools_and_written_gaps(mcp_tools, web_routes, slash_paths):
    missing = []
    for action, row in ARMORY.items():
        if row["web"] not in web_routes:
            missing.append(f"armory {action}/web: {row['web']!r}")
        if "mcp" in row:
            assert action not in ARMORY_MCP_EXEMPT, f"armory {action}: an MCP tool AND an exemption"
            if row["mcp"] not in mcp_tools:
                missing.append(f"armory {action}/mcp: {row['mcp']!r}")
        else:
            assert len(ARMORY_MCP_EXEMPT.get(action, "")) > 15, f"armory {action}: no MCP tool — say why"
        if "discord" in row:
            assert action not in ARMORY_DISCORD_PENDING and action not in ARMORY_DISCORD_EXEMPT, f"armory {action}: a Discord path AND a gap"
            if not discord_path_exists(row["discord"], slash_paths):
                missing.append(f"armory {action}/discord: {row['discord']!r}")
        else:
            why = ARMORY_DISCORD_PENDING.get(action) or ARMORY_DISCORD_EXEMPT.get(action, "")
            assert len(why) > 15, f"armory {action}: no Discord path — say why"
            assert not (action in ARMORY_DISCORD_PENDING and action in ARMORY_DISCORD_EXEMPT), action
    assert not missing, "\n".join(missing)
    assert {"armory_link", "armory_seed", "armory_move"} <= set(configops.BY_DESIGN_ONLY)


# auction prices (spec §4.1, §4.5): features, not ops — never plain text. The price check is every member's (the
# Professions page's box, /price, MCP's price_check); the guild's auction house is the owner's (Config, inside
# /gm config armory's hub — /gm config is at Discord's 25-subcommand limit). The watch list and its history (price
# watch §4.2–§4.5) are the member's own: site and Discord (/price) only, never MCP (C11). A Discord half still to come
# is dated in PRICES_DISCORD_PENDING, naming the task that lands it; that task empties the dict again.
PRICES: dict[str, dict[str, str]] = {
    "check": {"web": "GET /api/prices/find", "mcp": "price_check", "discord": "slash:price"},
    "market": {"web": "GET /api/admin/market", "mcp": "market", "discord": "flow:armory_guild"},
    "market_choices": {"web": "POST /api/admin/market", "discord": "flow:armory_guild"},
    "market_set": {"web": "POST /api/admin/market", "discord": "flow:armory_guild"},
    "market_clear": {"web": "POST /api/admin/market", "discord": "flow:armory_guild"},
    "watch_list": {"web": "GET /api/prices/watching", "discord": "slash:price"},
    "watch": {"web": "POST /api/prices/watch", "discord": "slash:price"},
    "unwatch": {"web": "POST /api/prices/watch", "discord": "slash:price"},
    "history": {"web": "GET /api/prices/history", "discord": "slash:price"},
}
WATCH_OWN = "a watch list is its member's own: no tool reads or changes one (C11)"
PRICES_MCP_EXEMPT = {
    "market_choices": "the owner matches sample prices against the game by eye before picking; MCP only reads the choice made",
    "market_set": "the owner matches sample prices against the game by eye before picking; MCP only reads the choice made",
    "market_clear": "turning prices off is the owner's switch beside the picker (Config, /gm config armory); MCP reads the choice",
    "watch_list": WATCH_OWN,
    "watch": WATCH_OWN,
    "unwatch": WATCH_OWN,
    "history": WATCH_OWN,
}
PRICES_DISCORD_PENDING: dict[str, str] = {}


def test_auction_prices_have_web_routes_mcp_tools_and_written_gaps(mcp_tools, web_routes, slash_paths):
    missing = []
    for action, row in PRICES.items():
        if row["web"] not in web_routes:
            missing.append(f"prices {action}/web: {row['web']!r}")
        if "mcp" in row:
            assert action not in PRICES_MCP_EXEMPT, f"prices {action}: an MCP tool AND an exemption"
            if row["mcp"] not in mcp_tools:
                missing.append(f"prices {action}/mcp: {row['mcp']!r}")
        else:
            assert len(PRICES_MCP_EXEMPT.get(action, "")) > 15, f"prices {action}: no MCP tool — say why"
        if "discord" in row:
            assert action not in PRICES_DISCORD_PENDING, f"prices {action}: a Discord path AND a gap"
            if not discord_path_exists(row["discord"], slash_paths):
                missing.append(f"prices {action}/discord: {row['discord']!r}")
        else:
            why = PRICES_DISCORD_PENDING.get(action, "")
            assert "Task" in why and len(why) > 15, f"prices {action}: no Discord path — name the task that lands it"
    assert not missing, "\n".join(missing)
    assert not {"set_market", "market_set", "clear_market"} & mcp_tools, "MCP never picks or clears the auction house"
    assert not PRICES_DISCORD_PENDING, "every price action has its Discord path (price watch Task 5)"
