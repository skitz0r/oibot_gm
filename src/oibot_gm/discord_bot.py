"""The Discord client: OibotGM mixes in every surface (raids, loot, feed, pool cards, setup, help, news) and owns the
gateway events (on_message's routing, member updates, ready). `/mock` is retired (owner decision D11): a rehearsal is a
test-bench run whose council is a test session (nothing reaches the guild's ledger). `GuildContext` carries the start
directory's key, profile and the LLM provider; no guild's loot reads the demo fixtures."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import discord
import yaml
from discord import app_commands

from . import configops, guild_routing, render
from .discord_feed import FeedMixin
from .discord_loot import LootMixin
from .discord_loot_surface import LootSurfaceMixin
from .discord_policy import CfgApplyButton, PolicyContext, handle_change, left_server, register_policy_commands
from .discord_help import ACT as HELP_ACT, GuideSelect, HelpMixin, apply_view as help_apply_view, guide_intro, guide_view, register_help_commands
from .discord_pool import PoolMixin
from .feed import FeedServer, authorize as feed_authorize, feed_config, guild_tokens
from .discord_pool import AbsenceButton, AbsencesMixin, LootLogMixin, LootWishButton, SetupMixin
from .flows_test import TestAnswerButton
from .discord_news import NewsMixin, ProposalButton
from .discord_raid import RaidContext, RaidMixin
from .raid_buttons import FillButton, LootButton, LootPick, LootStartButton, PlaceButton, RunButton, SignupButton
from .raid_commands import register_raid_commands
from .raid_scheduler import RaidSchedulerMixin
from .discord_registry import ApplicationButton, Guilds, RegisterButton, register_commands
from .importers import signup as signup_mod
from .ops import Ops
from .llm.provider import Provider, get_provider
from .loot import session as loot_session
from .loot.session import LootSession
from .profiles import GameProfile
from .registry import RegistryError, superusers
from .store import GitStore

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out"
log = logging.getLogger(__name__)
STORE: GitStore | None = None  # set in run(); None = fixtures-only mode (no git)
# The demo fixtures' guild key: the directory name of the guild dir the bot was started on (run() sets it from
# the real path; before that, OIBOT_GUILD_DIR or the public demo fixtures). Never a real guild's name in code.
GUILD_KEY = Path(os.environ.get("OIBOT_GUILD_DIR") or "fixtures/demo").name


def _store() -> GitStore:
    assert STORE is not None, "store not initialised"
    return STORE

CLASS_COLOURS = {"Warrior": 0xC79C6E, "Paladin": 0xF58CBA, "Hunter": 0xABD473, "Rogue": 0xFFF569, "Priest": 0xFFFFFF, "Shaman": 0x0070DE, "Mage": 0x69CCF0, "Warlock": 0x9482C9, "Druid": 0xFF7D0A}
MEMBER_BLIP_RETRY_S = 15  # cached_member: a transient fetch failure is retried after this, never cached as a miss
ROLE_ICON = {"tank": "🛡", "healer": "✚", "melee": "⚔", "ranged": "🏹"}
# Filled at startup with application emojis (class_<name>, role_<name>); falls back to text.
EMOJI: dict[str, str] = {}
ICON_EMOJI: dict[str, str] = {}  # Blizzard icon file name -> "<:name:id>" (real class/spec/role/buff art as app emojis)
ICON_NAMES: dict[str, dict[str, str]] = {"class": {}, "role": {}, "spec": {}, "buff": {}}  # kind -> key -> icon file name


def ico(kind: str, key: str) -> str:
    """Emoji for a class / role / spec ('Class:Spec') / buff id: the real icon when uploaded, else the generated badge."""
    name = ICON_NAMES.get(kind, {}).get(key)
    if name and name in ICON_EMOJI:
        return ICON_EMOJI[name]
    return EMOJI.get(f"{kind}_{key.lower()}", ROLE_ICON.get(key, "") if kind == "role" else "")


# ---------------------------------------------------------------- guild context

class GuildContext:
    """What the client still takes from the guild directory it was started on: its key (reserved when a server is
    claimed), its game profile (the generated emoji set) and the LLM provider. The retired /mock flow's fixtures
    (signups, the imported ledger, wishlists, policy) are no longer loaded (D11); every guild's loot reads its own
    RegistryLootContext."""

    def __init__(self, guild_dir: Path, signup_file: str | None = None):
        self.guild_dir = guild_dir
        self.guild_key = guild_dir.name
        self.guild, _registry, _smap = signup_mod.load_registry(guild_dir / "characters.yaml")
        self.profile = GameProfile.load(ROOT / "profiles" / self.guild["game_profile"])
        self.provider: Provider | None = get_provider()


# ---------------------------------------------------------------- event state

def load_events() -> dict:
    """Live loot sessions from every guild directory in the data repo, keyed (guild key, session id)."""
    return loot_session.load_sessions(_store())


# ---------------------------------------------------------------- rendering

def wowhead(item, profile: GameProfile | None = None) -> str | None:
    """A link to the item on the profile's database site (`integrations.yaml: wowhead_item_url`, `{id}` filled), or
    None: an item known only by name, or a profile with no such site (WoW: Forever until it has one)."""
    if not item or not getattr(item, "id", 0):
        return None
    root = getattr(profile, "root", None)
    tmpl = None
    if root is not None:
        f = Path(root) / "integrations.yaml"
        if f.exists():
            tmpl = (yaml.safe_load(f.read_text()) or {}).get("wowhead_item_url")
    return tmpl.format(id=item.id) if tmpl else None


# ---------------------------------------------------------------- bot

class OibotGM(FeedMixin, LootSurfaceMixin, LootMixin, LootLogMixin, RaidMixin, RaidSchedulerMixin, PoolMixin, AbsencesMixin, SetupMixin, HelpMixin, NewsMixin, discord.Client):
    ico = staticmethod(ico)

    async def post_application(self, reg, a) -> str:
        """The application review card, for /apply and the apply wizard (one implementation)."""
        from .discord_registry import post_application

        return await post_application(self, reg, a)

    async def decide_application(self, reg, discord_id: int, accept: bool, by: str, note: str | None, *, by_id: int | None = None,
                                 via: str = "discord") -> str:
        """Accept or decline an open application: registry decide, the member role (D31), DM the applicant, update the
        review card, ONE ops line (audited with `via` / `by_id`). The one path for the card's buttons, /roster applicant,
        the web and MCP. Returns the verdict line; raises RegistryError when there is no open application ("Already
        declined by X"). Never removes anyone from the server."""
        from .discord_registry import settle_application

        return await settle_application(self, reg, discord_id, accept, by, note, by_id=by_id, via=via)

    async def refresh_registration_card(self, reg) -> bool:
        """The registration card edited in place after a recruitment change (and given the Apply button)."""
        from .discord_registry import refresh_registration_card

        return await refresh_registration_card(self, reg)

    async def close(self) -> None:
        """Shutdown: send the puppet digests still waiting in their quiet window (the tester's one DM per batch — lost
        at a restart otherwise), push any unpushed data-repo commits, stop the feed listener, then disconnect. Every
        step is bounded, so a stopping bot always lets go of its ports (launchd waits ExitTimeOut, then kills it)."""
        try:
            await asyncio.wait_for(self.flush_puppet_digests(), timeout=5)
        except Exception as e:  # noqa: BLE001 — the test feed still has each DM until the process ends
            log.warning("shutdown: puppet digests not sent: %s", type(e).__name__)
        try:
            if STORE is not None:
                await asyncio.wait_for(asyncio.to_thread(STORE.flush), timeout=20)
        except Exception as e:  # noqa: BLE001 — the commits stay local; the next start pushes them
            log.warning("shutdown push skipped: %s", type(e).__name__)
        try:
            if self.feed is not None:
                await asyncio.wait_for(self.feed.stop(), timeout=5)
        except Exception as e:  # noqa: BLE001
            log.warning("feed listener stop: %s", type(e).__name__)
        finally:
            await super().close()

    def __init__(self, ctx: GuildContext, test_guild: int | None):
        intents = discord.Intents.default()
        intents.message_content = True  # channel chat → roster changes / loot feedback
        # member text and names are data: never let them ping @everyone / roles through the bot
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.ctx = ctx
        self.tree = app_commands.CommandTree(self)
        self.test_guild = test_guild
        self.events: dict[tuple[str, str], LootSession] = load_events()  # loot sessions by (guild key, session id)
        self.contexts: dict = {}  # guild key -> RegistryLootContext (lazy, one per guild; reload() after an import)
        for s in self.events.values():
            self.loot_index(s)  # + the by-thread map event_for reads
        # the guilds: registry, officer tools, ops feed
        self.ops = Ops(self)
        self.registries = Guilds(_store(), ROOT)
        register_commands(self.tree, self.registries, self.ops, ico)
        self.raids = RaidContext(self.registries)
        register_raid_commands(self.tree, self.registries, self.ops, self)
        self.policies = PolicyContext(self.registries)
        register_policy_commands(self.tree, self.registries, self.ops, self, self.policies)
        register_help_commands(self.tree, self.registries, self.ops, self)
        self._check_descriptions()

    def _check_descriptions(self) -> None:
        """Discord rejects the whole sync when one description exceeds 100 chars; fail here with the name instead."""
        bad = []

        def walk(cmd, path=""):
            name = f"{path} {cmd.name}".strip()
            subs = getattr(cmd, "commands", None)
            if subs:
                for c in subs:
                    walk(c, name)
            elif len(cmd.description or "") > 100:
                bad.append(f"/{name} ({len(cmd.description)})")

        for c in self.tree.get_commands():
            walk(c)
        if bad:
            raise SystemExit("command descriptions over 100 chars: " + ", ".join(bad))
        self.add_dynamic_items(SignupButton, FillButton, PlaceButton, RunButton, RegisterButton, ApplicationButton, GuideSelect, AbsenceButton, ProposalButton, CfgApplyButton,
                               LootButton, LootPick, LootStartButton, LootWishButton, TestAnswerButton)
        self.tree.on_error = self._on_command_error

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # the guild the command resolved to (need() tags it), else the server's own (strict), else — a DM that failed before
        # need() ran — the only guild, as with one guild before; with several, the log only. Never a guess, never a prompt.
        # After the DM server picker (guild_routing.pick) the original carries the chosen key AND the press: the apology
        # answers the press, which the command was answering (else Discord shows "This interaction failed" on it).
        extras = getattr(interaction, "extras", None) or {}
        key = extras.get("guild")
        target = extras.get("press") or interaction
        if key:
            reg = self.registries.by_key.get(key)
        elif interaction.guild_id:
            reg = self.registries.for_guild(interaction.guild_id)
        else:
            reg = self.registries.only()
        cmd = interaction.command.qualified_name if interaction.command else "?"
        msg = f"`/{cmd}` by {interaction.user.display_name}: {type(error).__name__}: {str(error)[:200]}"
        if reg:
            await self.ops.emit(reg.config, "error", msg, exc=error)
        else:
            print("[ops:error]", msg)
        try:
            text = "Something went wrong; the owner has been notified." if reg else "Something went wrong; it has been logged."
            if target.response.is_done():
                await target.followup.send(text, ephemeral=True)
            else:
                await target.response.send_message(text, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    async def ensure_emojis(self) -> None:
        """Upload class/role badges as application emojis once; map names → emoji strings."""
        try:
            existing = {e.name: e for e in await self.fetch_application_emojis()}
        except Exception as e:  # noqa: BLE001
            print(f"application emojis unavailable: {e}")
            return
        wanted = {f"class_{c.lower()}": ("class", c) for c in render.CLASS} | {f"role_{r}": ("role", r) for r in render.ROLE_COLOUR}
        buff_defs = {}
        for reg in self.registries.ordered() + [self.ctx]:
            for b in reg.profile.party_buffs():
                buff_defs[b.id] = (b.abbr, b.colour)
        wanted |= {f"buff_{bid}": ("buff", bid) for bid in buff_defs}
        for name, (kind, key) in wanted.items():
            em = existing.get(name)
            if em is None:
                try:
                    png = render.class_badge_png(key) if kind == "class" else render.role_badge_png(key) if kind == "role" else render.buff_badge_png(*buff_defs[key])
                    em = await self.create_application_emoji(name=name, image=png)
                except Exception as e:  # noqa: BLE001
                    print(f"emoji {name}: {e}")
                    continue
            EMOJI[name] = str(em)
        print(f"emojis ready: {len(EMOJI)}")
        await self.ensure_icon_emojis(existing)

    async def ensure_icon_emojis(self, existing: dict) -> None:
        """The profile's Blizzard icons (classes, roles, specs, buff art) as app emojis so sheets and cards show real art."""
        import httpx

        icon_dir = Path(__file__).resolve().parents[2] / "out" / "icons"
        icon_dir.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, str] = {}
        for reg in self.registries.ordered():
            ic = reg.profile.icons
            for kind, key_map in (("class", ic.get("classes", {})), ("role", ic.get("roles", {})), ("spec", ic.get("specs", {}))):
                for key, name in key_map.items():
                    ICON_NAMES[kind][key] = name
                    wanted[name] = name
            for b in reg.profile.buffs:
                if b.art:
                    ICON_NAMES["buff"][b.id] = b.art
                    wanted[b.art] = b.art
        n_new = 0
        async with httpx.AsyncClient(timeout=10) as hc:
            for name in wanted:
                ename = ("ic_" + name)[:32]
                em = existing.get(ename)
                if em is None:
                    f = icon_dir / f"{name}.jpg"
                    if not f.exists():
                        try:
                            r = await hc.get(f"https://render.worldofwarcraft.com/us/icons/56/{name}.jpg")
                            if r.status_code != 200:
                                continue
                            f.write_bytes(r.content)
                        except httpx.HTTPError:
                            continue
                    try:
                        em = await self.create_application_emoji(name=ename, image=f.read_bytes())
                        n_new += 1
                    except Exception as e:  # noqa: BLE001
                        print(f"icon emoji {ename}: {e}")
                        continue
                ICON_EMOJI[name] = str(em)
        print(f"icon emojis ready: {len(ICON_EMOJI)} ({n_new} uploaded)")

    async def cached_member(self, guild, uid: int, max_age: float = 300):
        """A guild member by id without the privileged members intent: the gateway cache first, else one REST
        fetch per (guild, uid) per `max_age` seconds — misses (left the server, 404) are cached too, so a
        page that polls or a scanner can't turn into a stream of fetches."""
        if guild is None:
            return None
        m = guild.get_member(uid)
        if m is not None:
            note = getattr(self, "note_membership", None)
            if note is not None:
                note(guild.id, uid, True)  # in the gateway's cache: in the server (a left_at from before is cleared)
            return m
        cache: dict[tuple[int, int], tuple[float, object]] = self.__dict__.setdefault("_member_cache", {})
        key = (guild.id, uid)
        hit = cache.get(key)
        now = time.time()
        if hit and now - hit[0] < max_age:
            return hit[1]
        gone: set[tuple[int, int]] = self.__dict__.setdefault("_member_gone", set())  # misses that were Discord's own 404
        # a fetch Discord didn't answer (5xx, rate limit, network) is NOT an answer: it is never cached as "not a
        # member" (that would read as 'unknown' — a 503 on the web — for max_age); it is only throttled for a few seconds
        blips: dict[tuple[int, int], float] = self.__dict__.setdefault("_member_blip", {})
        b = blips.get(key)
        if b is not None and now - b < min(max_age, MEMBER_BLIP_RETRY_S):
            return None
        try:
            m = await guild.fetch_member(uid)
        except Exception as e:  # noqa: BLE001 — not a member (404) or transient
            if not isinstance(e, discord.NotFound):
                gone.discard(key)
                if len(blips) > 4096:
                    blips.clear()
                blips[key] = now
                return None
            m = None
            gone.add(key)
        else:
            gone.discard(key)
        note = getattr(self, "note_membership", None)  # SetupMixin: Discord's own answer keeps Member.left_at true (§3.3 D)
        if note is not None:
            note(guild.id, uid, m is not None)
        blips.pop(key, None)
        if len(cache) > 4096:  # bounded: drop what has aged out
            for k in [k for k, (t, _) in cache.items() if now - t >= max_age]:
                cache.pop(k, None)
                gone.discard(k)
        cache[key] = (now, m)
        last = self.__dict__.setdefault("_member_last_yes", {})  # §2.9: a last-known "yes" for reads while Discord is unsure
        if m is not None:
            if len(last) > 4096:
                last.clear()
            last[key] = (now, m)
        else:
            last.pop(key, None)  # Discord said no: not a member any more
        return m

    def saw_member(self, guild_id: int | None, member) -> None:
        """Evidence beats the cache: a message or an interaction someone made inside a server proves they are in it now,
        so a cached 404 ("left", or "not yet joined") for that server is dropped at once — DM routing (guilds_of) and the
        web read the fresh answer instead of waiting out max_age."""
        uid = getattr(member, "id", None)
        if not guild_id or uid is None:
            return
        key = (guild_id, uid)
        self.__dict__.setdefault("_member_gone", set()).discard(key)
        note = getattr(self, "note_membership", None)
        if note is not None:
            note(guild_id, uid, True)  # back in the server: a left_at is cleared
        cache = self.__dict__.setdefault("_member_cache", {})
        if isinstance(member, discord.Member):
            cache[key] = (time.time(), member)
        else:
            cache.pop(key, None)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id:  # a listener only: the tree and the views answer the interaction itself
            self.saw_member(interaction.guild_id, interaction.user)
        if interaction.type == discord.InteractionType.modal_submit:
            # a loot reason box opened before a restart (no live Modal claims it): its persistent `lootmodal.<gk>:…` id
            # carries everything the submit needs, so it is the same write as a live one (design §2.9)
            from .flows_loot import loot_modal_raw

            try:
                await loot_modal_raw(interaction)
            except Exception:  # noqa: BLE001
                log.exception("loot modal (restart path) failed")

    # ---- several guilds (design §5.32)
    async def claim_guild(self, guild, owner_id: int, profile: str, by) -> "object":
        """Make an unconfigured Discord server a guild (the claim wizard's Confirm): the folder and its commit in a
        worker thread, then served ON THE LOOP (copy-on-write add), hooked up like boot (guild_added), announced to the
        HOST's superusers (OIBOT_SUPERUSERS — they run the bot and can act on it: the Public Bot switch, the shared LLM
        budget); another guild's owner is never DMed about someone else's server (the DM rule), the ops line stays.
        Raises RegistryError when the server is configured or a claim is in flight."""

        if guild is None:
            raise RegistryError("Invite the bot to this server first.")
        reg = await asyncio.to_thread(self.registries.create, guild, owner_id, profile, (self.ctx.guild_key,))
        self.registries.add(reg)
        await self.guild_added(reg)
        who = getattr(by, "display_name", None) or str(by)
        line = f"guild created for {guild.name} by {who}; owner <@{owner_id}> · profile {reg.profile.label}"
        await self.ops.emit(reg.config, "warn", line)  # no ops channel yet: the log (and the new guild's ring)
        for other in self.registries.ordered():  # S-01: every other guild's ops channel hears it — an ops line, never a DM to its owner (D44)
            if other is not reg:
                await self.ops.emit(other.config, "info", f"the bot now also serves another server: {guild.name} (shares the bot's LLM budget)")
        claimer = getattr(by, "id", None)
        for oid in sorted(superusers()):  # the bot host learns at once that a new server is served (K5)
            if claimer is not None and int(claimer) == oid:
                continue  # they claimed it themselves: nothing to act on
            try:
                user = self.get_user(oid) or await self.fetch_user(oid)
                await user.send(f"🆕 The bot now also serves **{discord.utils.escape_markdown(str(guild.name))}** — claimed by {discord.utils.escape_markdown(who)}, "
                                f"owner <@{owner_id}>. It shares this bot's LLM budget. If you didn't expect this, check that Public Bot is off in the Discord Developer Portal.")
            except Exception as e:  # noqa: BLE001 — closed DMs: the log line stays
                log.warning("claim DM to owner %s failed: %s", oid, e)
        return reg

    # ---- the bot added to / removed from servers (design §3.5 O-01 / S-05 / S-06; §5.44)
    def host_ids(self) -> list[int]:
        """Who the host-facing DMs go to: the superusers (OIBOT_SUPERUSERS — they run the bot). With none configured, the
        owner of the ONE guild the process serves (effectively the host); with several and no superusers, nobody — a
        guild's owner can't act on another server, its .env or its invite (the DM rule): the process log carries it."""
        ids = sorted(superusers())
        if ids:
            return ids
        regs = list(self.registries.ordered())
        if len(regs) == 1 and regs[0].config.owner_discord_id:
            return [int(regs[0].config.owner_discord_id)]
        return []

    async def on_guild_join(self, guild) -> None:
        """Added to a server. A server whose guild was parked (the bot was removed, now back) is served again at once.
        Otherwise ONE DM to the host (host_ids): it isn't a guild yet — `/gm config owner` there claims it — and the
        server-wide permissions the invite should have given and didn't (perms.missing_for). The host must act (claim it
        or remove the bot), so the DM rule allows it; nobody else is told (no superusers and several guilds: the process
        log and the Host card's list of servers that aren't guilds only)."""
        from . import perms

        perms.CLIENT = self
        if await self.reattach(guild, "on_guild_join"):
            return
        if self.registries.for_guild(guild.id) is not None:
            return
        missing = perms.missing_for(guild)
        name = discord.utils.escape_markdown(str(getattr(guild, "name", guild.id)))
        text = (f"🆕 I was added to **{name}**. It isn't a guild yet — `/gm config owner` there claims it."
                + (f" Missing permissions: {perms.words(missing)} — re-invite me with the link on the Config page's Host card." if missing else " Permissions: all there."))
        hosts = self.host_ids()
        if not hosts:
            log.warning("added to server %s (%s), not a guild yet; no OIBOT_SUPERUSERS to tell", getattr(guild, "name", "?"), guild.id)
        for uid in hosts:
            try:
                user = self.get_user(uid) or await self.fetch_user(uid)
                await user.send(text[:1990])
            except Exception as e:  # noqa: BLE001 — closed DMs: the log line stays
                log.warning("join DM to %s failed: %s", uid, e)
        log.info("added to server %s (%s); not a guild yet", getattr(guild, "name", "?"), guild.id)

    async def on_guild_available(self, guild) -> None:
        """A server came (back) into view — at startup for every server, after an outage for one: a parked guild whose
        server this is, is served again."""
        from . import perms

        perms.CLIENT = self
        await self.reattach(guild, "on_guild_available")

    async def reattach(self, guild, by: str) -> object | None:
        """Serve a parked guild again when the bot is in its server (Guilds.attach_server), hooked up like a claim, and
        say so on its ops channel. None when no parked guild has that server."""
        if getattr(guild, "unavailable", False):
            return None
        reg = self.registries.attach_server(guild.id, by)
        if reg is None:
            return None
        await self.guild_added(reg)
        await self.ops.emit(reg.config, "warn", f"the bot is back in {getattr(guild, 'name', 'the server')}: {reg.config.name} is served again (runs, the scheduler, the site)")
        return reg

    async def on_guild_remove(self, guild) -> None:
        """Removed from a server (kicked, banned, or the server deleted). An OUTAGE is not a removal (`guild.unavailable`):
        nothing is parked then. Otherwise the guild is detached (Guilds.detach: config.detached_at, out of routing, the
        scheduler, the feed, the picker, the homepage; pending DMs stop — send_member_card refuses a detached guild) and
        one ops error line is written (its error alert goes to the guild's error_dm_ids — the owner runs it)."""
        if getattr(guild, "unavailable", False):
            return
        reg = self.registries.for_guild(guild.id)
        if reg is None:
            return
        self.registries.detach(reg.key, "on_guild_remove")
        await self.ops.emit(reg.config, "error", f"the bot was removed from {getattr(guild, 'name', 'the server')}: {reg.config.name} is parked — nothing opens, "
                                                 "no DMs go out, the site no longer lists it. Invite the bot back to that server and it is served again "
                                                 "(nothing was deleted).")

    async def on_member_remove(self, member) -> None:
        """Someone left a configured server: their record gets `left_at` (Registry.member_left) — officers see "left the
        server" on the Members page and may Remove record (D29: never the member themselves). No notice, no DM. This event
        needs the privileged members intent, which the bot doesn't request: in production `left_at` comes from Discord's
        own 404 in `cached_member` (SetupMixin.note_membership) and the hourly sweep (SetupMixin.sweep_left)."""
        reg = self.registries.for_guild(getattr(getattr(member, "guild", None), "id", None))
        if reg is None or member.id not in reg.members:
            return
        await asyncio.to_thread(reg.member_left, member.id, True)

    async def on_member_join(self, member) -> None:
        """Back in the server: `left_at` is cleared (the record was kept)."""
        reg = self.registries.for_guild(getattr(getattr(member, "guild", None), "id", None))
        if reg is None or member.id not in reg.members or not reg.members[member.id].left_at:
            return
        await asyncio.to_thread(reg.member_left, member.id, False)

    async def guild_added(self, reg) -> None:
        """Hook a guild claimed at run time up the way boot does: analytics listener, officer roles, guild-scoped command
        sync (development mode), and the icons of a game profile no loaded guild used before. The raid/policy stores
        are lazy and the web reads Guilds live, so nothing restarts."""
        self.attach_pool_listener(reg)
        await self.resolve_officer_roles(reg)
        if os.environ.get("OIBOT_COMMAND_SCOPE", "global") == "guild":
            try:
                obj = discord.Object(id=int(reg.config.discord_guild_id))
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
            except Exception as e:  # noqa: BLE001 — the claim stands; a restart syncs it
                log.warning("command sync for %s failed: %s", reg.key, e)
        if reg.profile.name not in {r.profile.name for r in self.registries.ordered() if r is not reg}:
            try:
                existing = {e.name: e for e in await self.fetch_application_emojis()}
                await self.ensure_icon_emojis(existing)
                self.check_emoji_conflicts()
            except Exception as e:  # noqa: BLE001 — icons fall back to the generated badges
                log.warning("icon emojis for %s: %s", reg.profile.name, e)

    def check_emoji_conflicts(self) -> list[str]:
        """App emoji names are shared by every loaded profile: warn when two profiles map one icon name to different art."""
        seen: dict[tuple[str, str], tuple[str, str]] = {}
        out = []
        for reg in self.registries.ordered():
            ic = reg.profile.icons
            pairs = [(kind, key, name) for kind, km in (("class", ic.get("classes", {})), ("role", ic.get("roles", {})), ("spec", ic.get("specs", {}))) for key, name in km.items()]
            pairs += [("buff", b.id, b.art) for b in reg.profile.buffs if b.art]
            for kind, key, name in pairs:
                prev = seen.setdefault((kind, key), (name, reg.profile.name))
                if prev[0] != name and prev[1] != reg.profile.name:
                    out.append(f"{kind} {key}: {prev[1]} uses {prev[0]}, {reg.profile.name} uses {name}")
        for line in out:
            log.warning("emoji conflict between profiles — %s", line)
        return out

    async def dm_guild(self, message: discord.Message):
        """Which guild a plain DM is about: the only one; else the one the sender belongs to; in several, the server
        picker (buttons, asked every time — owner decision D2), then the chosen one; None after saying why (in none:
        every server's Join link) or when nobody picked in time. Only the guilds that answer DMs count (plain-text
        policy `dm` not "ignore"): a guild whose owner switched DMs off is never offered, and when every guild has,
        a DM gets silence — as one guild with DMs off always has."""
        import types as _types

        from . import guild_routing
        from .guild_picker import choose_guild

        only = self.registries.only()
        if only is not None:
            return only  # one guild: plain_text applies its DM mode, as before
        regs = self.registries.ordered()
        answering = [r for r in regs if r.plain_mode(None) != "ignore"]
        if not answering:
            return None
        mine = await guild_routing.guilds_of(self, message.author.id, _types.SimpleNamespace(ordered=lambda: answering))
        if len(mine) == 1:
            return mine[0]
        if mine:
            reg, _press = await choose_guild(message, mine)
            return reg
        await message.reply(guild_routing.not_in_any(regs), allowed_mentions=discord.AllowedMentions.none())
        return None

    # ---- chat handlers
    def officiates(self, user, guild) -> bool:
        """Officer check for buttons and chat: owner, Manage Server, or one of the configured officer roles (matched by
        role id — `GuildConfig.officer_by_roles`; legacy names only until `resolve_officer_roles` has run). Manage
        Server counts only in a configured server (design §14 K24): an unconfigured server's admin is nobody's officer."""
        reg = self.registries.for_guild(guild.id) if guild else None
        if reg and reg.is_owner_id(user.id):
            return True
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.manage_guild and reg is not None:
            return True
        return bool(reg and reg.config.officer_by_roles(user.roles))

    def officer_role_names(self, reg) -> list[str]:
        """The configured officer roles as names (resolved against the live guild when it is available)."""
        g = self.get_guild(reg.config.discord_guild_id)
        return reg.resolve_officer_roles(g) if g else reg.officer_role_names()

    async def resolve_officer_roles(self, reg=None) -> None:
        """Turn legacy officer role names into role ids against the live guild (one commit) and refresh the name cache;
        on ready and whenever a role is edited so a rename never drops an officer."""
        for r in ([reg] if reg else self.registries.ordered()):
            g = self.get_guild(r.config.discord_guild_id)
            if not g:
                continue
            before = list(r.config.officer_roles)
            try:
                names = await asyncio.to_thread(r.resolve_officer_roles, g)
            except Exception:  # noqa: BLE001 — a failed commit must not stop the bot from starting
                import logging

                logging.getLogger(__name__).exception("officer role resolution failed for %s", r.key)
                continue
            if before and before != r.config.officer_roles:
                await self.ops.emit(r.config, "info", f"officer roles now stored by role id: {', '.join(names) or '(none)'}")
            if r.config.officer_roles:
                await self.ops.emit(r.config, "warn", f"officer role name(s) not found in the server: {', '.join(r.config.officer_roles)} — re-add with /gm config officer-role")

    async def on_guild_role_update(self, before, after):
        reg = self.registries.by_discord.get(after.guild.id)
        if reg:
            await self.resolve_officer_roles(reg)

    async def on_guild_role_delete(self, role):
        reg = self.registries.by_discord.get(role.guild.id)
        if reg:
            await self.resolve_officer_roles(reg)

    async def on_guild_role_create(self, role):
        reg = self.registries.by_discord.get(role.guild.id)
        if reg and reg.config.officer_roles:  # a legacy name may now resolve
            await self.resolve_officer_roles(reg)

    def _addressed(self, message: discord.Message) -> bool:
        """@user mention, a mention of the bot's own (integration) role, or a reply to one of the bot's messages."""
        if self.user in message.mentions:
            return True
        me = message.guild.me if message.guild else None
        if me and any(r in message.role_mentions for r in me.roles):
            return True
        ref = message.reference.resolved if message.reference else None
        return isinstance(ref, discord.Message) and ref.author.id == self.user.id

    # ---- identity (dual interfaces, phase 0 — design §2.6): guild_routing.resolve_role is THE predicate; these are its names
    async def role_of(self, reg, user_or_uid, *, max_age: float = guild_routing.PRIV_ROUTING) -> str:
        """owner | officer | member | outside for a Member (live from its roles), a bare User or a Discord id (the cached
        lookup at most `max_age` old: PRIV_LIVE for writes, PRIV_ROUTING for presses, PRIV_WEB for the web)."""
        return await guild_routing.role_of(self, reg, user_or_uid, max_age=max_age)

    async def identity(self, reg, user, *, max_age: float = guild_routing.PRIV_ROUTING) -> guild_routing.Resolved:
        """(role, role_ids) — what Registry.may_plain needs (unpacks as a pair; `.stale` says the cache answered)."""
        return await guild_routing.identity(self, reg, user, max_age=max_age)

    async def plain_identity(self, reg, user, *, max_age: float = guild_routing.PRIV_ROUTING) -> tuple[bool, list[int]]:
        """(officer, role ids) for the plain-text check (Registry.may_plain) — an alias of `identity`: a bare User from a
        DM is looked up as the guild member (Discord hands over no roles there). A write passes `max_age=PRIV_LIVE`
        (ConfigConfirmView's Apply): the answer is fetched now, never the cache's."""
        r = await self.identity(reg, user, max_age=max_age)
        return r.officer, r.role_ids

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """A nick change in a configured server (needs the members intent to arrive): the registered member's display
        name follows (guild_routing.sync_name → Registry.rename, the one rename path). Seats, callouts and asks are by
        id, so nothing else moves and no run is relabelled."""
        reg = self.registries.by_discord.get(after.guild.id) if getattr(after, "guild", None) is not None else None
        if reg is not None:
            guild_routing.sync_name(reg, after)

    async def in_guild(self, reg, user) -> bool:
        """Whether `user` is a member of the guild's Discord server now (the gateway cache, else one cached fetch: a
        404 is remembered). The web treats server membership as the whitelist; plain text does too (discord_policy.left_server).
        Fails open: False only on Discord's own Unknown Member (404) — a guild missing from the gateway cache or a
        transient fetch error (a 5xx, a timeout) is not a departure, so a member is never locked out by an outage."""
        guild = self.get_guild(reg.config.discord_guild_id)
        if guild is None:
            return True
        if isinstance(user, discord.Member) and user.guild.id == guild.id:
            return True
        if await self.cached_member(guild, user.id) is not None:
            return True
        return (guild.id, user.id) not in self.__dict__.get("_member_gone", set())

    async def plain_text(self, message: discord.Message, reg, text: str, channel) -> None:
        """An @mention or a DM, routed by intent under the guild's plain-text policy (design §5.25).
        An "act" channel (ops/analytics by default): configops.parse first — a change becomes the diff + Apply, a
        question gets the parser's own reply from the live config (one model call either way). Elsewhere (DMs and
        other channels): the help answer first, which also says whether the message asks the bot to DO something;
        only then, and only if the person could act here, is it parsed into a change — a plain question costs one call,
        a change two. Outside the ask audience a person who may act on their own record is parsed directly. A newcomer
        who must apply (Registry.must_apply) while applications are open gets the persistent Apply to join button on the
        reply, so the application wizard opens straight from the chat (typed class/spec is never parsed)."""
        mode = reg.plain_mode(channel)
        if mode == "ignore":
            return
        uid = message.author.id
        quiet = discord.AllowedMentions.none()
        if await left_server(self, reg, message.author):  # a DM from someone no longer in the server: unregistered, whatever the registry holds
            await message.reply(guide_intro(reg), view=guide_view(reg), allowed_mentions=quiet)
            return
        officer, role_ids = await self.plain_identity(reg, message.author)
        provider, ps = self.ctx.provider, self.policies.store(reg)
        if mode == "act" and provider is not None:
            async with message.channel.typing():
                await handle_change(message, reg, ps, provider, self.ops, text, bot=self, channel=channel)
            return
        # the snapshot and the parser context follow who can READ the reply: an officer asking where members read it
        # gets the member view (help_answer adds where the officer view is)
        public = not self.reply_private(reg, message.channel if message.guild else None)
        owner = reg.is_owner_id(uid)
        may_act = provider is not None and (owner or (mode == "self" and reg.may_plain("self", uid, officer=officer, role_ids=role_ids, channel=channel, fallback="members") is None))

        def apply_button(base=None):  # a newcomer who must apply gets the Apply to join button (the wizard opens from here, DMs too)
            return help_apply_view(reg, uid, officer=officer, role_ids=role_ids, base=base)

        async with message.channel.typing():
            if may_act and not reg.may_ask(uid, officer):
                req = await asyncio.to_thread(configops.parse, provider, reg, text, reg.members[uid].display_name if uid in reg.members else message.author.display_name, officer=officer and not public)
                if req.kind == "change" and req.ops:
                    await handle_change(message, reg, ps, provider, self.ops, text, bot=self, channel=channel, req=req)
                else:
                    await message.reply(guide_intro(reg), view=apply_button(guide_view(reg)), allowed_mentions=quiet)
                return
            answer = await self.help_answer(reg, uid, officer, text, may_act=may_act, public=public)
            if answer == HELP_ACT:
                await handle_change(message, reg, ps, provider, self.ops, text, bot=self, channel=channel)
                return
        if answer is None:  # outside the ask audience: static guide only
            await message.reply(guide_intro(reg), view=apply_button(guide_view(reg)), allowed_mentions=quiet)
        else:
            await message.reply(answer, view=apply_button(), allowed_mentions=quiet)

    async def on_message(self, message: discord.Message):
        if await self.news_message(message):  # the news webhook (a bot) is read before the bot filter below
            return
        if message.author.bot:
            return
        if message.guild:
            self.saw_member(message.guild.id, message.author)
        if not message.guild:  # a DM to the bot: a question, or a change to the sender's own record (plain-text policy "DMs")
            text = message.content.strip()
            if not text:
                return
            reg = await self.dm_guild(message)
            if reg is not None:
                await self.plain_text(message, reg, text, None)
            return
        reg = self.registries.for_guild(message.guild.id)
        # an @mention: routed by intent and by the channel's plain-text mode (the ops and analytics channels act by default;
        # a loot thread keeps its own chat below)
        if reg and self._addressed(message) and (message.channel.id in (reg.config.ops_channel_id, reg.config.analytics_channel_id) or not self.event_for(message.channel.id)):
            text = re.sub(r"<@[!&]?\d+>", "", message.content).strip()
            if text:
                await self.plain_text(message, reg, text, message.channel.id)
            return
        ev = self.event_for(message.channel.id)
        if ev is not None and reg is not None and ev.guild == reg.key and message.channel.id == ev.raid_thread_id:
            # a council's thread: only an @mention, a leading "?" or a reply to one of its cards reaches the model (D9);
            # the rest is banter. A question → help_answer with the proposals as data; an override → a diff + Apply
            text = self.loot_addressed(message, ev)
            parent = getattr(message.channel, "parent_id", None) or ev.cards.get("channel")
            if text and reg.plain_mode(parent) != "ignore":
                await self.loot_chat(message, ev, text)

    def sync_servers(self) -> list[int]:
        """The servers a guild-scoped sync covers: the test server and every configured server, each once."""
        ids = ([self.test_guild] if self.test_guild else []) + [int(r.config.discord_guild_id) for r in self.registries.ordered()]
        return list(dict.fromkeys(ids))

    async def setup_hook(self):
        # One scope only, or Discord lists every command twice. Default: global (works in every server and in DMs;
        # new commands can take a while to propagate). OIBOT_COMMAND_SCOPE=guild (development) gives instant updates
        # in the test server and every configured server, but no DM commands — and an unconfigured server then has no
        # commands at all, so it can't be claimed in that mode.
        scope = os.environ.get("OIBOT_COMMAND_SCOPE", "global")
        g = discord.Object(id=self.test_guild) if self.test_guild else None
        if scope == "guild" and (g or self.registries.ordered()):
            for gid in self.sync_servers():
                obj = discord.Object(id=gid)
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()
            if g:
                self.tree.clear_commands(guild=g)
                await self.tree.sync(guild=g)
        print(f"commands synced ({scope})")
        self.loop.create_task(self.scheduler())
        self.attach_pool_listeners()
        self.attach_loot_listeners()  # a seat change on a run with a council → "now a candidate" (candidates are read live)
        self.attach_loot_log()  # every new award row → one line in the public loot log (LootLogMixin)
        token, bind = feed_config()
        per_guild = guild_tokens()  # OIBOT_FEED_TOKEN_<KEY>: a token that feeds one guild only
        if token or per_guild:
            self.feed = FeedServer(self.handle_feed_event, token, bind,
                                   auth=lambda t: feed_authorize(t, token, per_guild, lambda: list(self.registries.by_key)))
            self.feed.on_disconnect = self.feed_disconnected
            await self.feed.start()
        else:
            print("loot feed disabled (set OIBOT_FEED_TOKEN or OIBOT_FEED_TOKEN_<KEY>, and OIBOT_FEED_BIND)")
        from .web.app import serve as web_serve

        self.started_at = time.time()
        self.loop.create_task(web_serve(self))

    async def on_ready(self):
        await self.ensure_emojis()
        self.check_emoji_conflicts()
        await self.resolve_officer_roles()  # legacy officer role names → ids, name cache for display
        st = _store()
        regs = ", ".join(f"{r.config.name}({len(r.members)}m/{len(r.all_characters())}c)" for r in self.registries.ordered())
        print(f"oibot_GM online as {self.user} · registries: {regs} · data: {st.root} @ {st.head()} (push {'on' if st.push_enabled else 'off'}) · llm: {self.ctx.provider.name if self.ctx.provider else 'off'} · loot sessions: {len(self.events)}")
        for err in self.registries.load_errors:  # a guild that didn't load has no ops channel to tell
            print(f"guild NOT loaded — {err}")
            log.error("guild not loaded: %s", err)
        only = self.registries.only()
        if only is not None:  # K7: the agents' runs written before runs carried a guild are this (the first) guild's
            try:
                from . import agents

                agents.tag_legacy_runs(only.key)
            except OSError as e:
                log.warning("legacy agent runs not tagged: %s", e)
        for reg in self.registries.ordered():
            await self.ops.emit(reg.config, "info", f"bot online · data @ {st.head()} · {len(reg.members)} members / {len(reg.all_characters())} characters · {len(reg.pending())} unconfirmed")
            if reg.config.analytics_channel_id and not getattr(self, "_pool_booted", False):
                await self.refresh_pool(reg)  # state may have changed while offline (PRs to the data repo)
            if not getattr(self, "_pool_booted", False):
                await self.refresh_registration_card(reg)  # the card's text and buttons as this version draws them (Apply to join)
        if not getattr(self, "_loot_booted", False):
            self._loot_booted = True
            for line in await self.loot_restore():  # an interrupted Distribute is reported, never re-run; pending threads made
                log.info("loot restore: %s", line)
            await self.loot_cards_restore()  # every live council's cards redrawn as this version draws them
        else:
            await self.loot_threads_pending()  # a later ready (a full reconnect): a council opened while Discord was down gets its thread
        self._pool_booted = True


def run(guild_dir: Path, signup_file: str) -> None:
    global STORE, GUILD_KEY  # GUILD_KEY: the start directory's guild (sessions carry their own `guild`)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing from .env")
    test_guild = int(os.environ["DISCORD_TEST_GUILD_ID"]) if os.environ.get("DISCORD_TEST_GUILD_ID") else None
    guild_dir = guild_dir.resolve()
    STORE = GitStore(guild_dir.parent)
    loot_session.STORE = STORE  # sessions created in this process save through it
    GUILD_KEY = guild_dir.name
    ctx = GuildContext(guild_dir, signup_file)
    # logging is configured by cli.setup_logging (rotating out/oibot.log); discord.py's own handler is off
    OibotGM(ctx, test_guild).run(token, log_handler=None)
