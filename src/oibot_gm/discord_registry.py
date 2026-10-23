"""Real (non-mock) commands: /register, /char, /roster, /gm.

Resolves the guild from the Discord server. Member commands reply ephemerally
and also work in DMs; officer commands need Manage Server or a configured
officer role; /gm config needs the owner."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import discord
from discord import app_commands

from . import guild_routing, inbox, ops as ops_mod
from .profiles import GameProfile
from .registry import DECISION_NOTE_MAX, is_superuser, GUILD_KEY_RE, RANKS, Registry, RegistryError
from .store import GitStore
from .wizard_flows import FLOWS

RANK_CHOICES = [app_commands.Choice(name=r, value=r) for r in RANKS]
STARTED = time.time()
log = logging.getLogger(__name__)


class Guilds:
    """The configured guilds of this process (design §5.32): one folder per guild in the data repo (`<key>/guild.yaml`),
    each mapped from its Discord server id. Several guilds run in one process; nothing may take "the first guild" for
    routing — use `for_guild` (strict, a server), `only()` (exactly one configured), `by_key`, `get` (key or unique name)
    and `ordered()` (the one stable order for listings). `by_discord` is swapped, never mutated (`add`), so a reader
    iterating it (or an `ordered()` snapshot) never sees it change size."""

    # design §14 K11: a claim in a second server was refused until the DM server picker and the guild-carrying button
    # ids shipped (stage S3); they have, so a second (third, …) server may be claimed. Kept as a switch for tests.
    ALLOW_SECOND = True

    def __init__(self, store: GitStore, code_root: Path):
        import threading

        self.store = store
        store.guilds = self  # the guild index of this data root: ProposalStore.move's shared-profile rule reads it (K3)
        self.code_root = code_root
        self.load_errors: list[str] = []
        self._lock = threading.Lock()
        self._claiming: set[int] = set()
        self.by_discord: dict[int, Registry] = {}
        found: list[tuple[Path, Registry]] = []
        profiles: dict[str, GameProfile] = {}
        for gdir in sorted(p for p in store.root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if not (gdir / "guild.yaml").exists():
                continue
            try:
                import yaml

                cfg = yaml.safe_load((gdir / "guild.yaml").read_text())
                if not isinstance(cfg, dict):
                    raise ValueError("guild.yaml is not a mapping")
                key = str(cfg.get("key") or "")
                if key != gdir.name:
                    raise ValueError(f"key {key!r} differs from the folder name")
                if not GUILD_KEY_RE.fullmatch(key):
                    raise ValueError(f"key {key!r} is not 1-16 characters of a-z, 0-9, _ and -")
                pname = str(cfg["game_profile"])
                if pname not in profiles:
                    proot = code_root / "profiles" / pname
                    if not (proot / "classes.yaml").exists():
                        raise ValueError(f"unknown game_profile {pname!r}")
                    profiles[pname] = GameProfile.load(proot)
                found.append((gdir, Registry(store, gdir.name, profiles[pname])))
            except Exception as e:  # noqa: BLE001 — one broken guild never stops the others (or the bot)
                log.exception("guild %s did not load", gdir.name)
                self.load_errors.append(f"{gdir.name}: {type(e).__name__}: {e}")
        seen: dict[int, list[str]] = {}
        for gdir, reg in found:
            seen.setdefault(int(reg.config.discord_guild_id), []).append(gdir.name)
        for gid, keys in seen.items():
            if len(keys) > 1:  # routing a server to the wrong guild is worse than refusing it
                self.load_errors.append(f"{' and '.join(keys)}: the same discord_guild_id {gid} — neither loaded")
        kept = [r for _, r in found if len(seen[int(r.config.discord_guild_id)]) == 1]
        # a guild whose server removed the bot stays parked (design §3.5 S-06) until on_guild_join / on_guild_available sees it
        self.detached: dict[str, Registry] = {r.key: r for r in kept if r.config.detached_at}
        self.by_discord = {int(r.config.discord_guild_id): r for r in kept if not r.config.detached_at}
        self._mark()

    @classmethod
    def of(cls, store: GitStore, *regs: Registry, code_root: Path | None = None) -> "Guilds":
        """An index over registries already built (tests, offline tools): no directory scan."""
        import threading

        g = cls.__new__(cls)
        g.store, g.code_root, g.load_errors = store, code_root or Path(__file__).resolve().parents[2], []
        if getattr(store, "guilds", None) is None:  # the real loader's index (the bot's) stays the store's; this one only fills a gap
            store.guilds = g
        g._lock, g._claiming = threading.Lock(), set()
        g.detached = {}
        g.by_discord = {int(r.config.discord_guild_id): r for r in regs}
        g._mark()
        return g

    def _mark(self) -> None:
        """Tell every served registry whether the process serves several guilds (Registry.many_guilds: DM and card
        texts name their guild only then)."""
        many = len(self.by_discord) > 1
        for r in self.by_discord.values():
            r.many_guilds = many

    # ---- lookups
    @property
    def by_key(self) -> dict[str, Registry]:
        return {r.key: r for r in self.by_discord.values()}

    def ordered(self) -> list[Registry]:
        """Every guild, by name then key: the one stable order (a snapshot list — safe to iterate across awaits)."""
        return sorted(self.by_discord.values(), key=lambda r: (str(r.config.name).casefold(), r.key))

    def get(self, ref: str | None) -> Registry | None:
        """A guild by key, else by name (case-insensitive) when exactly one guild has that name."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        hit = self.by_key.get(ref)
        if hit is not None:
            return hit
        named = [r for r in self.by_discord.values() if str(r.config.name).casefold() == ref.casefold()]
        return named[0] if len(named) == 1 else None

    def for_guild(self, guild_id: int | None) -> Registry | None:
        """The guild of that Discord server, or None. Strict: never falls back to another guild."""
        return self.by_discord.get(int(guild_id)) if guild_id else None

    def only(self) -> Registry | None:
        """The guild when exactly one is configured (every "single-guild keeps working" branch goes through here)."""
        regs = list(self.by_discord.values())
        return regs[0] if len(regs) == 1 else None

    @property
    def many(self) -> bool:
        return len(self.by_discord) > 1

    def cfg_for(self, key: str | None):
        """A guild's config by key, else the only guild's, else None (the feed and the scheduler's process-wide lines)."""
        reg = self.by_key.get(key) if key else None
        reg = reg or self.only()
        return reg.config if reg is not None else None

    def for_interaction(self, interaction: discord.Interaction) -> Registry | None:
        """Sync resolution: the guild a resolver already chose (`interaction.extras["guild"]`) → the interaction's server
        (strict: an unconfigured server is None, never another guild) → in a DM, the only guild → None. DMs with several
        guilds are guild_routing.need's job (it asks Discord who the person belongs to)."""
        extras = getattr(interaction, "extras", None) or {}
        if extras.get("guild"):
            return self.by_key.get(extras["guild"])
        if interaction.guild_id:
            return self.for_guild(interaction.guild_id)
        return self.only()

    # ---- bootstrap (design §5.32: a server becomes a guild only when someone claims it)
    def taken_keys(self, extra: tuple[str, ...] | list[str] = ()) -> set[str]:
        """Every name a new key must avoid: each directory in the data root (guild or not), `extra` (the mock fixtures'
        key), the reserved names."""
        from .registry import RESERVED_GUILD_KEYS

        return {p.name for p in self.store.root.iterdir()} | set(self.by_key) | set(extra) | set(RESERVED_GUILD_KEYS)

    def claim_refusal(self, guild_id: int) -> str | None:
        """Why a claim of this server can't go ahead now (None = it can)."""
        if self.for_guild(guild_id) is not None or guild_id in self._claiming:
            return "This server is already set up."
        if any(int(r.config.discord_guild_id) == int(guild_id) for r in getattr(self, "detached", {}).values()):
            return "This server already has a guild (parked while the bot was away); it is served again as soon as the bot sees the server."
        if (self.by_discord or self._claiming) and not self.ALLOW_SECOND:  # a first claim still in flight counts too
            return "Serving a second server needs the next update of the bot; nothing was set up."
        return None

    def create(self, guild, owner_id: int, profile_name: str, extra_taken: tuple[str, ...] | list[str] = ()) -> Registry:
        """Write the new guild's folder (registry.create_guild) for a Discord server (`guild`: .id and .name) and return
        its Registry. File work only — runs in a worker thread; the caller adds it with `add()` ON THE LOOP. Refuses a
        configured server and a claim already in flight (a double press, two claimers racing)."""
        from .registry import GuildConfig, create_guild, guild_key_for

        gid = int(guild.id)
        with self._lock:
            why = self.claim_refusal(gid)
            if why:
                raise RegistryError(why)
            self._claiming.add(gid)
        try:
            proot = self.code_root / "profiles" / profile_name
            if not (proot / "classes.yaml").exists():
                raise RegistryError(f"unknown game profile {profile_name!r}")
            key = guild_key_for(guild.name, self.taken_keys(extra_taken))
            cfg = GuildConfig(key=key, name=str(guild.name)[:100] or key, game_profile=profile_name, discord_guild_id=gid, owner_discord_id=int(owner_id))
            return create_guild(self.store, cfg, GameProfile.load(proot), self.code_root)
        except BaseException:
            with self._lock:
                self._claiming.discard(gid)
            raise
        # on success the id stays in _claiming until add() serves it, so a second press in between still refuses

    # ---- detach / attach (design §3.5 S-05/S-06): the bot removed from a server, and back
    def detach(self, key: str, by: str = "on_guild_remove") -> Registry | None:
        """Park a guild whose server removed the bot: `config.detached_at` stamped (kept across restarts), out of
        `by_discord` — hence out of ordered(), the scheduler's tick, the feed's routing, the DM picker, the homepage and
        MCP's list_guilds (superusers still see it there, marked) — and into `detached`. Copy-on-write like `add`; on the
        loop thread only. Nothing is archived or deleted: the files stay, and attach() serves it again."""
        reg = self.by_key.get(key)
        if reg is None:
            return self.detached.get(key)
        from .registry import now as _now

        reg.set_detached(_now(), by)
        self.by_discord = {gid: r for gid, r in self.by_discord.items() if r is not reg}
        self.detached = {**self.detached, reg.key: reg}
        self._mark()
        return reg

    def attach_server(self, guild_id: int, by: str = "on_guild_available") -> Registry | None:
        """The bot is in a parked guild's server again: served once more (detached_at cleared). None = no parked guild has
        that server (or one already serves it)."""
        gid = int(guild_id)
        reg = next((r for r in self.detached.values() if int(r.config.discord_guild_id) == gid), None)
        if reg is None or gid in self.by_discord:
            return None
        reg.set_detached(None, by)
        self.detached = {k: r for k, r in self.detached.items() if r is not reg}
        self.by_discord = {**self.by_discord, gid: reg}
        self._mark()
        return reg

    def detached_list(self) -> list[Registry]:
        """The parked guilds, by name (the Host card, /gm status for superusers, MCP list_guilds for superusers)."""
        return sorted(self.detached.values(), key=lambda r: (str(r.config.name).casefold(), r.key))

    def add(self, reg: Registry) -> None:
        """Serve a newly created guild. On the event-loop thread only: builds a new dict and swaps it in (copy-on-write),
        so nothing iterating the old one can fail. Refuses a key or Discord id already served."""
        gid = int(reg.config.discord_guild_id)
        if gid in self.by_discord or reg.key in self.by_key:
            raise RegistryError(f"{reg.key} is already served")
        self.by_discord = {**self.by_discord, gid: reg}
        self._claiming.discard(gid)
        self._mark()


def rostered_on(bot, reg: Registry, m) -> list:
    """(run, character) for every locked live run `m` holds a seat on — from the run files (raidcycle.rostered_runs);
    [] when the client has no raid store (bare test clients)."""
    from . import raidcycle as rc

    raids = getattr(bot, "raids", None)
    return rc.rostered_runs(reg, raids.store(reg), m) if raids is not None else []


def rostered_text(reg: Registry, held: list, with_char: bool = False) -> str:
    """The runs a member is rostered on as a person reads them: raid name + Discord's native <t:…> stamp (run_label),
    never the run key (its last part is the 24-hour start — the clock rule)."""
    from .raid_views import run_label

    return ", ".join(run_label(reg, ev) + (f" ({char})" if with_char else "") for ev, char in held)


def channel_card_guild(interaction: discord.Interaction, gk: str | None) -> Registry | None:
    """The guild of a card that lives in a server channel (applications, absences, news proposals): the server's own
    guild, never a DM fallback; a card carrying a guild key (`app.<gk>:…`) must also be pressed in that guild's server
    (K1/K2) — anywhere else it is nobody's."""
    reg = interaction.client.registries.for_guild(interaction.guild_id)
    if reg is None or (gk and reg.key != gk):
        return None
    return reg


def is_officer(interaction: discord.Interaction, reg: Registry) -> bool:
    """The command's officer check — an alias of guild_routing.role_sync (design §2.6): the owner, or a Member of the
    guild's server with Manage Server or an officer role. A bare User (a DM) is not an officer here; the async paths
    (`asker_identity`, `plain_identity`) look the person up."""
    return guild_routing.role_sync(interaction.client, reg, interaction.user) in ("owner", "officer")


def is_owner(interaction: discord.Interaction, reg: Registry) -> bool:
    return reg.is_owner_id(interaction.user.id)


from .constants import ROLES, TEAL  # noqa: E402
ROLE_CHOICES = [app_commands.Choice(name=r, value=r) for r in ROLES]
FIELD_MAX = 1024  # Discord's embed field value cap


def config_show_embed(reg: Registry, sections) -> discord.Embed:
    """/gm config show: one embed field per configops.config_summary section; a section past a field's 1024 characters
    keeps the lines that fit and ends "+N more on the site" (the Config page's Overview has them all)."""
    import os

    web = os.environ.get("OIBOT_WEB_URL", "")
    e = discord.Embed(title=f"{reg.config.name} · configuration", colour=TEAL, url=f"{web}/app/config?g={reg.key}" if web.startswith("https") else None)
    left = 5600 - len(e.title or "") - 120  # Discord's 6000-character embed total (the footer and the titles counted in)
    for n, s in enumerate(sections[:25]):
        cap = min(FIELD_MAX, max(120, left // max(1, len(sections) - n))) - 40  # room kept for the "+N more" line
        shown: list[str] = []
        for i, line in enumerate(s.lines):
            if len("\n".join([*shown, line[:cap]])) > cap:
                shown.append(f"-# +{len(s.lines) - i} more on the site")
                break
            shown.append(line[:cap])
        value = "\n".join(shown)[:FIELD_MAX] or "—"
        left -= len(value) + len(s.title)
        e.add_field(name=s.title, value=value, inline=False)
    e.set_footer(text="Everything here is on the site's Config page (Overview); /gm config … changes it.")
    return e


CHAR_ACTION_CHOICES = [app_commands.Choice(name=n, value=v) for v, n in (("spec", "Spec"), ("main", "Main"), ("flex", "Flex (extra roles)"), ("retire", "Retire"),
                                                                          ("delete", "Delete"), ("name", "Name a planned character"), ("add", "Add an alt"))]
ROSTER_CHAR_CHOICES = [app_commands.Choice(name=n, value=v) for v, n in (("spec", "Spec"), ("main", "Main"), ("flex", "Flex (extra roles)"), ("rank", "Rank"),
                                                                          ("retire", "Retire"), ("delete", "Delete"))]


def overview_embed(reg: Registry, ico, *, officer: bool) -> discord.Embed | None:
    """/roster overview: who plans what. Role lines are icon + have/need (the iconography rule); buffs are icon rows
    (Nobody / Only one, a provider's name after the icon); the regulars get ONE FIELD PER SCHEDULE (no 1000-character
    truncation of the whole block) — officers only: recruit candidates and who is away are planning data. None = no
    mains yet."""
    from . import raidcycle as rc

    s = reg.plan_summary()
    if not s["mains"]:
        return None
    e = discord.Embed(title=f"{reg.config.name} · plan · {s['mains']} mains", colour=TEAL)
    for cls, lines in sorted(s["by_class"].items(), key=lambda kv: -len(kv[1])):
        e.add_field(name=f"{ico('class', cls)} {cls} ({len(lines)})", value="\n".join(lines)[:1000], inline=True)
    # every raid's size at once (no typed size: the wizard rule); the recruiting ask is against the biggest
    sized = sorted(((int(reg.raid_def(rid).get("size") or 20), rid) for rid in reg.profile.raids), reverse=True) or [(20, None)]
    for n, rid in sized:
        bounds = reg.role_bounds(rid, n)
        role_lines = []
        for r in ROLES:
            have = s["by_role"].get(r, 0)
            need_min = bounds.get(r, {}).get("min", 0)
            flex = s["flex"].get(r, [])
            mark = "🟢" if not need_min or have >= need_min else ("🟡" if have + len(flex) >= need_min else "🔴")
            role_lines.append(f"{mark} {ico('role', r)} **{have}**" + (f" / {need_min}" if need_min else "") + (f" · flex: {', '.join(flex)}" if flex else ""))
        name = reg.raid_def(rid).get("name", rid) if rid else "a raid"
        e.add_field(name=f"Roles vs {name} ({n})", value="\n".join(role_lines), inline=True)
    n, rid = sized[0]
    bounds = reg.role_bounds(rid, n)
    ids = {b.short: b.id for b in reg.profile.party_buffs()}
    missing = [b for b, who in s["providers"].items() if not who]
    thin = [(b, who[0]) for b, who in s["providers"].items() if len(who) == 1]
    if missing or thin:
        val = ""  # icons only (the iconography rule): the buff icon carries the name; a provider's name is a member, not a label
        if missing:
            val += "Nobody: " + " ".join(ico('buff', ids.get(b, '')) or b for b in missing) + "\n"  # the name only stands in for a missing emoji
        if thin:
            val += "Only one: " + " · ".join(f"{ico('buff', ids.get(b, '')) or b} {who}" for b, who in thin)
        e.add_field(name="Buff coverage", value=val[:1000], inline=False)
    if officer:
        from .raid_views import coverage_line, forecast_lines

        cover = rc.series_outlook(reg)
        many = len({cv["raid"] for cv in cover}) > 1
        room = 25 - len(e.fields) - (1 if s["alts"] else 0)  # an embed holds 25 fields; the classes, roles and alts keep theirs
        shown = cover[:room] if len(cover) <= room else cover[:max(0, room - 1)]
        for cv in shown:  # one field per schedule
            body = "\n".join([coverage_line(ico, cv)] + forecast_lines(ico, cv))
            e.add_field(name=f"Regulars · {cv['raid_name'] + ' · ' if many else ''}{cv['name']}"[:256], value=body[:1000] or "—", inline=False)
        if len(shown) < len(cover):
            e.add_field(name="Regulars · more", value=f"{len(cover) - len(shown)} more schedule(s) — the Rosters page's Regulars tab has them all", inline=False)
    if s["alts"]:
        e.add_field(name=f"Alts ({len(s['alts'])})", value=", ".join(f"{w} · {c}" for w, c in s["alts"])[:1000], inline=False)
    asks = [f"{max(0, bounds[r]['min'] - s['by_role'].get(r, 0))} {r}" for r in ("tank", "healer") if bounds.get(r) and s["by_role"].get(r, 0) < bounds[r]["min"]]
    e.set_footer(text=(("Recruiting ask: " + ", ".join(asks) + " · ") if asks else "") + "counts use each member's primary role preference, else their main spec's role")
    return e


def overview_post_view(reg: Registry, ico) -> discord.ui.View:
    """The officer's *Post to #roster* under their ephemeral overview: the officer version, posted in the roster channel
    (officer standing re-checked at the press)."""
    view = discord.ui.View(timeout=600)
    btn = discord.ui.Button(label="Post to the roster channel", style=discord.ButtonStyle.secondary)

    async def press(i: discord.Interaction) -> None:
        if not is_officer(i, reg):
            await i.response.send_message("Officers only.", ephemeral=True)
            return
        ch = i.client.get_channel(reg.config.roster_channel_id) if reg.config.roster_channel_id else None
        e = overview_embed(reg, ico, officer=True)
        if ch is None or e is None:
            await i.response.send_message("No roster channel the bot can see — the owner sets it with `/gm config roster-channel`.", ephemeral=True)
            return
        try:
            await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await i.response.send_message(f"Couldn't post in <#{ch.id}> — check the bot's permissions there.", ephemeral=True)
            return
        await i.response.edit_message(view=None)
        await i.followup.send(f"✅ Posted in <#{ch.id}>.", ephemeral=True)
    btn.callback = press  # type: ignore[method-assign]
    view.add_item(btn)
    return view


def char_line(ico, c) -> str:
    flags = []
    if c.is_main:
        flags.append("main")
    if c.status == "planned":
        flags.append("planned")
    flags.append(c.rank)
    if not c.confirmed_by and c.status != "planned":
        flags.append("unconfirmed")
    return f"{ico('class', c.cls)} **{c.label}** · {c.spec}{'/' + c.offspec if c.offspec else ''} · _{', '.join(flags)}_"


class RegisterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"reg(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<action>register|alt|status|apply)"):
    """Persistent registration card: Apply to join (newcomers) · Register/Plan main → class → spec/offspec → name
    (optional) · Add an alt · My status. A newcomer pressing Register is sent to the application (flows_register).
    The id carries the guild (`reg.<gk>:apply`: the Apply button also rides on DM replies); an old `reg:apply`
    resolves as before (several guilds in a DM: the person's guilds, the picker when more than one)."""

    LABELS = {"apply": ("Apply to join", discord.ButtonStyle.primary), "register": ("Register / plan my main", discord.ButtonStyle.success),
              "alt": ("Add an alt", discord.ButtonStyle.secondary), "status": ("My status", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, gk: str | None):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"reg{'.' + gk if gk else ''}:{action}"))
        self.action, self.gk = action, gk

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], match["g"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg, interaction = await guild_routing.resolve_button(interaction, self.gk)
        if not reg:
            if not interaction.response.is_done():
                await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if self.action == "status":
            m = reg.members.get(interaction.user.id)
            if not m or not m.active():
                a = reg.applicants.get(interaction.user.id)
                text = (f"Your application for **{a.label}** is waiting for an officer." if a and a.status == "open"
                        else "Nothing registered yet — new here? press **Apply to join**; members press **Register / plan my main**.")
                await interaction.response.send_message(text, ephemeral=True)
                return
            primary, flex = reg.roles_of(m)
            lines = [char_line(bot.ico, c) for c in m.active()]
            lines.append(f"Roles: {primary or '?'}" + (f" (+{', '.join(flex)})" if flex else ""))
            held = rostered_on(bot, reg, m)
            if held:
                lines.append("Rosters: " + rostered_text(reg, held))
            regular = reg.regular_series_of(m)
            if regular:
                lines.append("Regular on: " + "; ".join(f"{reg.series_label(k)} ({c.label})" for k, c in regular) + " — `/me view` to step down")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return
        if self.action == "apply":
            await FLOWS["register"](interaction, reg, mode="apply")
            return
        await FLOWS["register"](interaction, reg, slot="main" if self.action == "register" else "alt")


def registration_card(reg: Registry) -> discord.Embed:
    rec = reg.config.recruitment
    apply = ("**Apply to join** — new here? Start here: class and spec, your character's name, the raid schedules you can make"
             + (" and a few questions from the officers" if rec.questions else "") + ". An officer reviews it and you get a DM either way."
             if rec.open else f"**Apply to join** — {reg.config.name} isn't taking applications right now.")
    if rec.open and rec.note:
        apply += f"\n> {rec.note}"
    e = discord.Embed(title=f"{reg.config.name} · join and register", colour=TEAL,
                      description=f"{apply}\n\n"
                                  "**Members:**\n**Register / plan my main** — class, spec, optional offspec, and the name if the character exists (leave it blank before launch; your role follows the spec).\n"
                                  "**Add an alt** — same flow for an alt.\n**My status** — what the bot has for you.\n\n"
                                  "Keyboard route: `/apply`, `/me plan …`, `/me char`, `/me view`. Away for a while? `/me absent add` or the card in the absences channel.")
    e.set_footer(text="Your Discord account is your identity. Newcomers apply first; members register and add alts.")
    return e


def registration_view(reg: Registry) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for a in ("apply", "register", "alt", "status"):
        v.add_item(RegisterButton(a, reg.key))
    return v


# ---------------- applications: the officers' review card and the ONE decide path (buttons, /roster applicant, web, MCP)
EMBED_TOTAL, FIELD_VALUE_MAX, FIELDS_MAX = 6000, 1024, 25  # Discord's embed limits
# the eight `/gm config <kind>-channel` shortcuts (kind, command, description) → one helper, one confirm (flows_config.ChannelSetWizard)
CHANNEL_SHORTCUTS = (
    ("ops", "ops-channel", "Owner: where the bot mirrors every action"),
    ("applications", "applications-channel", "Owner: where application review cards are posted (defaults to the ops channel)"),
    ("roster", "roster-channel", "Owner: private channel for roster overviews, proposals, officer cards and the loot council"),
    ("registration", "registration-channel", "Owner: public read-only channel with the registration card (buttons open to everyone)"),
    ("analytics", "analytics-channel", "Owner: officer channel with live pool-readiness cards per raid and a change log"),
    ("absences", "absences-channel", "Owner: public channel with the 'I'll be away' card; one line per absence"),
    ("signup", "signup-channel", "Owner: where raid sheets are posted"),
    ("news", "news-channel", "Owner: where the news webhook posts; the bot keeps the items about our game"),
)
APPLICANTS_SHOWN = 10  # /roster applicants: one message holds 10 embeds; the rest are on the Applications page


def _clip(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:max(0, n - 1)].rstrip() + "…"


def application_embed(reg: Registry, a, ico=None) -> discord.Embed:
    """The review card: full name, class/spec icons, logs, the schedules they can make (in words) and every answer as a
    field. Within Discord's limits (25 fields, 1024 per value, 6000 in all): long answers share what is left, each cut
    with '…', and a last field says where the full answers are (the site's Applications page). An earlier verdict on the
    same person leads the description ("Applied before: declined 12 Mar by Jonny — full on healers")."""
    from .wizard_opts import schedule_names

    ico = ico or (lambda kind, key: "")
    title = _clip(f"Application · {a.label}", 256)
    desc = (f"{ico('class', a.cls)}{ico('spec', f'{a.cls}:{a.spec}')} **{a.cls} {a.spec}**{' / ' + a.offspec if a.offspec else ''}"
            f" · from **{a.display_name}** (<@{a.discord_id}>)")
    prior = reg.prior_decision_line(a)
    if prior:
        desc += f"\n-# {_clip(prior, 300)}"
    footer = f"status: {a.status}" + (f" · {a.decided_by}: {a.decision_note or ''}" if a.decided_by else " · officers: Accept / Decline below, or /roster applicant")
    if a.role_note:
        footer += f" · {a.role_note}"
    fixed: list[tuple[str, str]] = []
    if a.logs_url:
        fixed.append(("Logs", _clip(a.logs_url, 300)))
    if a.schedules:
        fixed.append(("Can raid", _clip("\n".join(schedule_names(reg, a.schedules)), FIELD_VALUE_MAX)))
    if a.availability:  # older applications: free-text raid times
        fixed.append(("When they can raid", _clip(a.availability, 300)))
    answers = [(_clip(x.get("label") or "?", 256), str(x.get("answer") or "").strip() or "—") for x in (a.answers or [])]
    if a.about:
        answers.append(("Anything else", a.about))
    room = FIELDS_MAX - len(fixed) - 1  # one field kept for the overflow / full-answers note
    shown, hidden = answers[:room], answers[room:]
    note_reserve = 200
    used = len(title) + len(desc) + len(footer) + sum(len(n) + len(v) for n, v in fixed) + sum(len(n) for n, _ in shown) + note_reserve
    budget = max(0, EMBED_TOTAL - used)
    caps: dict[int, int] = {}
    left = len(shown)
    for i in sorted(range(len(shown)), key=lambda i: len(shown[i][1])):  # shortest first: short answers stay whole, long ones share the rest
        cap = min(FIELD_VALUE_MAX, len(shown[i][1]), max(1, budget // max(1, left)))
        caps[i], budget, left = cap, budget - cap, left - 1
    cut = bool(hidden)
    e = discord.Embed(title=title, colour=TEAL, description=desc)
    for n, v in fixed:
        e.add_field(name=n, value=v or "—", inline=False)
    for i, (n, v) in enumerate(shown):
        cut = cut or len(v) > caps[i]
        e.add_field(name=n, value=_clip(v, caps[i]) or "…", inline=False)
    if cut:
        more = f"{len(hidden)} more answer(s) not shown. " if hidden else ""
        e.add_field(name="…", value=f"{more}Full answers: the site's Applications page (officers), or ask me (“show {_clip(a.name, 40)}'s application”).", inline=False)
    e.set_footer(text=footer)
    return e


async def assign_member_role(client, reg: Registry, discord_id: int) -> str | None:
    """D31: Accept gives the applicant the guild's member role (the first of `member_role_ids`) when the bot can — Manage
    Roles, a role below the bot's own, the person in the server. Returns what happened in words, or None when no member
    role is configured (nothing to say). Never raises: the acceptance stands either way."""
    rids = list(reg.config.member_role_ids or [])
    if not rids:
        return None
    guild = client.get_guild(reg.config.discord_guild_id) if getattr(client, "get_guild", None) else None
    if guild is None:
        return "couldn't give the member role (Discord unreachable) — add it by hand"
    role = guild.get_role(int(rids[0])) if hasattr(guild, "get_role") else None
    if role is None:
        return "the member role is gone from the server — set it again in the recruitment settings"
    me = getattr(guild, "me", None)
    if me is None or not me.guild_permissions.manage_roles:
        return f"couldn't give them @{role.name}: the bot needs Manage Roles (add it by hand)"
    top = getattr(me, "top_role", None)
    if top is not None and role >= top:
        return f"couldn't give them @{role.name}: it sits above the bot's own role (add it by hand)"
    member = guild.get_member(int(discord_id)) if hasattr(guild, "get_member") else None
    if member is None:
        try:
            member = await guild.fetch_member(int(discord_id))
        except Exception:  # noqa: BLE001 — left the server, or Discord didn't answer
            return f"couldn't give them @{role.name}: they're not in the server"
    if role in getattr(member, "roles", []):
        return f"they already have @{role.name}"
    try:
        await member.add_roles(role, reason=f"oibot_GM: application accepted ({reg.config.name})")
    except discord.Forbidden:  # a role-hierarchy race, or Manage Roles taken away meanwhile
        return f"couldn't give them @{role.name}: the bot isn't allowed to give that role (add it by hand)"
    except Exception as e:  # noqa: BLE001 — words for the officer (§2.7: never a type name); the error to the ops channel / log
        from .configops import report_error

        await report_error(getattr(client, "ops", None), reg.config, e, "giving the member role")
        return f"couldn't give them @{role.name}: something went wrong (add it by hand)"
    return f"gave them @{role.name}"


def remove_record_consequence(reg: Registry, m) -> str:
    """THE sentence of an officer's Remove record (the decide screen, the Members page's confirm)."""
    chars = ", ".join(c.label for c in m.characters) or "no characters"
    return (f"{m.display_name}'s record goes: {chars}, their absences, wishlists and notices. Loot already awarded stays in the ledger; the data repo's "
            "history keeps the file. They registered again from scratch if they come back. Nobody is DMed.")


async def remove_record(client, reg: Registry, discord_id: int, by: str, *, by_id: int | None = None, via: str = "discord") -> str:
    """THE officers' Remove record (design §3.3 D; D29: never self-service): only for someone who left the server
    (Member.left_at, set by on_member_remove), never while they hold a seat on a live run (the run files say), one ops
    line. The Discord command, the Members page and MCP call it."""
    m = reg.members.get(int(discord_id))
    from .raid_views import run_title

    seats = [run_title(reg, ev) for ev, _c in rostered_on(client, reg, m)] if m else []
    line = await asyncio.to_thread(reg.remove_record, int(discord_id), by, seats=seats)
    ops = getattr(client, "ops", None)
    if ops is not None:
        await ops_mod.audited(ops, reg, by, line, via=via, by_id=by_id)
    return line


async def remove_record_decide(interaction: discord.Interaction, reg: Registry, discord_id: int) -> None:
    """`/roster remove-record member:` — refused at once unless they left the server; else the decide screen."""
    from .wizard import Wizard

    m = reg.members.get(int(discord_id))
    if m is None:
        await interaction.response.send_message("No record for that member.", ephemeral=True)
        return
    if not m.left_at:
        await interaction.response.send_message(f"{m.display_name} is still in the server — retire their characters instead (`/roster char`).", ephemeral=True)
        return

    async def guard(i: discord.Interaction) -> str | None:
        return None if is_officer(i, reg) else "Officers only."

    wiz = Wizard(reg, interaction.user.id, title=f"Remove {m.display_name}'s record", guard=guard)

    async def do(i: discord.Interaction, _note: str | None) -> None:
        try:
            line = await remove_record(i.client, reg, discord_id, i.user.display_name, by_id=i.user.id)
        except RegistryError as e:
            await wiz.finish(i, f"❌ {e}")
            return
        await wiz.finish(i, f"✅ {line}.")

    await wiz.decide(interaction, remove_record_consequence(reg, m), do=do, do_label="Remove record", danger=True)


async def settle_application(client, reg: Registry, discord_id: int, accept: bool, by: str, note: str | None, *, by_id: int | None = None,
                             via: str = "discord") -> str:
    """Accept or decline an open application — the one path for the card's buttons, /roster applicant, the web and MCP
    (`OibotGM.decide_application`). Registry decide (accept registers the main as a confirmed trial; the applicant's
    notice — D26), the member role when the bot can (D31), DM the applicant (a decision they wait on: the DM rule),
    update the review card, ONE ops line (ops.audited: `via` the surface, `by_id` marks a host superuser — callers write
    none). Returns the verdict line; raises RegistryError when there is nothing to decide ("Already declined by X").
    Declining never removes anyone from the server: they keep the invite's access and can apply again later."""
    a, _char = reg.decide(discord_id, accept, by, note)
    note = (note or "").strip() or None
    verdict = f"✅ Accepted {a.display_name} as {a.label} (trial, confirmed)" if accept else f"⛔ Declined {a.display_name} ({a.label})"
    if accept:
        role_note = await assign_member_role(client, reg, a.discord_id)
        if role_note:
            a.role_note = role_note
            await asyncio.to_thread(reg.save_applicant, a, f"application of {a.display_name}: {role_note}")
            verdict += f" · {role_note}"
    try:
        user = await client.fetch_user(a.discord_id)
        if accept:
            await user.send(f"✅ Your application for **{a.label}** to {reg.config.name} was accepted. You're registered as a trial; `/me view` shows your status."
                            + (f"\n\n{note}" if note else ""))
        else:
            await user.send(f"Your application for **{a.label}** to {reg.config.name} wasn't accepted this time." + (f"\n\n{note}" if note else "")
                            + "\n\nYou're welcome to stay in the server, and you can apply again later.")
    except Exception:  # noqa: BLE001 — closed DMs: the officers still see the verdict
        verdict += " · couldn't DM them (their DMs are closed)"
    ch_id = reg.config.applications_channel_id or reg.config.ops_channel_id
    if a.message_id and ch_id:
        try:
            ch = client.get_channel(ch_id)
            msg = await ch.fetch_message(a.message_id)
            await msg.edit(embed=application_embed(reg, a, getattr(client, "ico", None)), view=None)
        except Exception:  # noqa: BLE001 — the card is a convenience; the decision is saved
            pass
    ops = getattr(client, "ops", None)
    if ops is not None:
        await ops_mod.audited(ops, reg, by, verdict.lstrip("✅⛔ ") + (f" — {note}" if note else ""), via=via, by_id=by_id)
    return verdict


async def client_decide(client, reg: Registry, discord_id: int, accept: bool, by: str, note: str | None, *, by_id: int | None = None, via: str = "discord") -> str:
    """The client's decide_application when it has one (OibotGM), else settle_application (bare test clients)."""
    if hasattr(client, "decide_application"):
        return await client.decide_application(reg, discord_id, accept, by, note, by_id=by_id, via=via)
    return await settle_application(client, reg, discord_id, accept, by, note, by_id=by_id, via=via)


def application_consequence(reg: Registry, a, accept: bool) -> list[str]:
    """What Accept / Decline does to the applicant, from code (dual interfaces §2.3) — the `decide` screen's text and the
    Applications page's confirm (GET /api/applications `consequence`). The DM is one they are waiting on (the DM rule);
    a note typed on the screen rides along with it; the decision also lands on their Me page (a notice)."""
    prior = reg.prior_decision_line(a)
    lead = [f"-# {prior}"] if prior else []
    if accept:
        rids = list(reg.config.member_role_ids or [])
        role = (f" They get the member role <@&{rids[0]}> when the bot has Manage Roles (it says so if it can't)." if rids else "")
        return lead + [f"{a.display_name} is registered as **{a.label}** ({a.cls} {a.spec}), rank trial, confirmed, and gets a DM with the decision (your note included).{role}",
                       "The review card shows it decided."]
    return lead + [f"{a.display_name} gets a DM that the application for **{a.label}** wasn't accepted this time (your note included); they keep the server and can apply again.",
                   "The review card shows it decided."]


async def application_decide(interaction: discord.Interaction, reg: Registry, discord_id: int, accept: bool, note: str | None = None) -> None:
    """The ONE confirm screen for an application decision (wizard.Wizard.decide): the consequence, then Accept /
    Accept with a note… / Back (Decline likewise — the note is the old DeclineModal's box, now on both verdicts). The
    card's buttons and /roster applicant both open it (`note`: the command's typed note, used by the plain press). The
    write is `client_decide` → the client's decide path; the officer check is re-run at every press."""
    from .wizard import Wizard

    a = reg.applicants.get(discord_id)
    if not a or a.status != "open":
        await interaction.response.send_message(reg.decided_refusal(a), ephemeral=True)
        return
    verb = "Accept" if accept else "Decline"

    async def guard(i: discord.Interaction) -> str | None:
        return None if is_officer(i, reg) else "Officers only."

    wiz = Wizard(reg, interaction.user.id, title=f"{verb} {a.display_name}'s application", guard=guard)

    async def do(i: discord.Interaction, typed: str | None) -> None:
        try:
            verdict = await client_decide(i.client, reg, discord_id, accept, i.user.display_name, typed or note, by_id=i.user.id)
        except RegistryError as e:
            await wiz.finish(i, f"❌ {e}")
            return
        await wiz.finish(i, verdict)

    text = "\n".join(application_consequence(reg, a, accept)) + (f"\nYour note: {_clip(note, 400)}" if note else "")
    await wiz.decide(interaction, text, do=do,
                     note={"label": "Note to the applicant (optional)", "max_length": DECISION_NOTE_MAX, "placeholder": "goes into their DM"}, do_label=verb, danger=not accept)


class ApplicationButton(discord.ui.DynamicItem[discord.ui.Button], template=r"app(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<action>accept|decline):(?P<uid>[0-9]{1,20})"):
    """Accept / Decline… on a review card; persistent, so a card posted before a restart still works. The id carries
    the guild (`app.<gk>:…`); a card only works in its own server either way (an old id: that server's guild). Both
    open the same `decide` screen (consequence · verdict · verdict with a note… · Back) — DeclineModal is retired."""

    def __init__(self, action: str, discord_id: int, gk: str | None):
        label, style = ("Accept", discord.ButtonStyle.success) if action == "accept" else ("Decline…", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"app{'.' + gk if gk else ''}:{action}:{discord_id}"))
        self.action, self.discord_id, self.gk = action, discord_id, gk

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["uid"]), match["g"])

    async def callback(self, interaction: discord.Interaction):
        reg = channel_card_guild(interaction, self.gk)  # a channel-only card: strict, never a DM fallback
        if not reg or not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        await application_decide(interaction, reg, self.discord_id, self.action == "accept")


class ApplicationView(discord.ui.View):
    def __init__(self, reg: Registry, discord_id: int):
        super().__init__(timeout=None)
        self.reg, self.discord_id = reg, discord_id
        self.add_item(ApplicationButton("accept", discord_id, reg.key))
        self.add_item(ApplicationButton("decline", discord_id, reg.key))


async def post_application(client, reg: Registry, a) -> str:
    """Post an application's review card (Accept / Decline) where officers see it; used by /apply and the apply
    wizard. Returns a line saying where it went."""
    ch_id = reg.config.applications_channel_id or reg.config.ops_channel_id
    ch = client.get_channel(ch_id) if ch_id else None
    if not ch:
        await client.ops.emit(reg.config, "warn", f"application from {a.display_name} ({a.name}, {a.cls} {a.spec}) — no applications/ops channel configured; use /roster applicants")
        return "no applications channel is set, so officers will find it under /roster applicants"
    msg = await ch.send(embed=application_embed(reg, a, getattr(client, "ico", None)), view=ApplicationView(reg, a.discord_id))
    a.message_id = msg.id
    reg.save_applicant(a, f"application card posted for {a.name}")
    return f"posted for review in #{getattr(ch, 'name', 'applications')}"


async def refresh_registration_card(client, reg: Registry) -> bool:
    """Edit the registration card in place after a recruitment change (open/closed, the note) — and give a card posted
    before the Apply button existed its buttons. False when there is no card or it can't be edited."""
    ch_id, msg_id = reg.config.registration_channel_id, reg.config.registration_message_id
    if not ch_id or not msg_id:
        return False
    try:
        ch = client.get_channel(ch_id)
        msg = await ch.fetch_message(msg_id)
        await msg.edit(embed=registration_card(reg), view=registration_view(reg))
        return True
    except Exception:  # noqa: BLE001 — a deleted card is re-posted with /gm config registration-channel
        return False


DISCORD_MAX_CHILDREN = 25  # Discord: subcommands (and subcommand groups) per command group — /gm config is close to it


def group_sizes(tree: app_commands.CommandTree) -> dict[str, int]:
    """Every command group of the tree by its path ('gm config') → how many subcommands it holds."""
    out: dict[str, int] = {}

    def walk(c, path: str = "") -> None:
        name = f"{path} {c.name}".strip()
        if isinstance(c, app_commands.Group):
            out[name] = len(c.commands)
            for sub in c.commands:
                walk(sub, name)
    for c in tree.get_commands():
        walk(c)
    return out


def too_many_subcommands(path: str, n: int) -> str:
    """The warning, in words, for the next builder who adds one subcommand too many."""
    return (f"/{path} would have {n} subcommands — Discord allows {DISCORD_MAX_CHILDREN} per group. Fold the new one into an "
            "existing hub (one subcommand that opens a wizard, like /gm config companion or /gm config recruitment) or "
            "another group; never a 26th.")


def tree_build_error(e: BaseException) -> str | None:
    """discord.py's bare "maximum number of child commands exceeded" (raised while the commands are being registered)
    → too_many_subcommands for the group it happened in (found in the traceback; the group already holds the 26th);
    None for any other error."""
    if not (isinstance(e, ValueError) and "maximum number of child commands" in str(e)):
        return None
    group, tb = None, e.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "add_command":
            group = tb.tb_frame.f_locals.get("self")
        tb = tb.tb_next
    path = getattr(group, "qualified_name", None) or "a command group"
    return too_many_subcommands(path, len(getattr(group, "commands", None) or []) or DISCORD_MAX_CHILDREN + 1)


def tree_limit_problems(tree: app_commands.CommandTree) -> list[str]:
    """What Discord would refuse about the groups' sizes (the offline check in scripts/check_commands.py)."""
    return [too_many_subcommands(p, n) for p, n in group_sizes(tree).items() if n > DISCORD_MAX_CHILDREN]


def register_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: ops_mod.Ops, ico) -> None:
    async def need(interaction: discord.Interaction) -> tuple[Registry | None, discord.Interaction]:
        """guild_routing.need over this tree's Guilds: (guild, interaction) — rebind both at every call site."""
        return await guild_routing.need(interaction, guilds=guilds)

    def _ns(interaction: discord.Interaction, *names: str):
        """First present option value from the interaction namespace (renamed options use their display name)."""
        for n in names:
            v = getattr(interaction.namespace, n, None)
            if v:
                return getattr(v, "value", v)
        return None

    async def member_char_autocomplete(interaction: discord.Interaction, current: str):
        """Characters of the member picked in the same command (officer commands with a `member` option)."""
        reg = guilds.for_interaction(interaction)
        member = _ns(interaction, "member")
        if reg is None or member is None:
            return []
        mid = getattr(member, "id", None) or (int(member) if str(member).isdigit() else None)
        m = reg.members.get(mid) if mid else None
        if not m:
            return []
        return [app_commands.Choice(name=f"{c.label} · {c.cls} {c.spec}"[:100], value=c.name or c.label) for c in m.active() if current.lower() in (c.name or c.label).lower()][:25]

    async def instance_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        return [app_commands.Choice(name=f"{r['name']} ({r.get('size', '?')})", value=rid) for rid, r in reg.profile.raids.items() if current.lower() in rid or current.lower() in r["name"].lower()][:25]

    async def any_char_autocomplete(interaction: discord.Interaction, current: str):
        """Every character in the guild (officers), planned ones by label with the owner's name."""
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        out = []
        for m, c in reg.all_characters():
            key = c.name or c.label
            if current.lower() in key.lower() or current.lower() in m.display_name.lower():
                out.append(app_commands.Choice(name=f"{c.label} · {c.cls} · {m.display_name}"[:100], value=key))
        return out[:25]

    # ---------------- /register
    @tree.command(name="register", description="Register a character: pick class and spec, then name it (blank = plan it)")
    @app_commands.describe(main="Make this your main")
    async def register(interaction: discord.Interaction, main: bool = False):
        reg, interaction = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        await FLOWS["register"](interaction, reg, slot="main" if main or not (m and m.main) else "alt")

    # ---------------- /plan (pre-launch: what are you going to play?)
    me = app_commands.Group(name="me", description="Your plan, characters and absences")
    plan = app_commands.Group(name="plan", description="Pre-launch: what you're planning to play (no character name needed)", parent=me)

    @plan.command(name="main", description="What you plan to main (no name needed yet)")
    async def plan_main(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, slot="main", plan_only=True)

    @plan.command(name="alt", description="What you plan to play as an alt")
    async def plan_alt(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, slot="alt", plan_only=True)

    # ---------------- /me char: ONE command (a Group can't itself be invoked); the Choice pre-seeds the hub and skips its
    # action row (design §3.3 cluster A). Spec · Main · Flex · Retire · Delete · Name · Add — every write is a one-row table
    # through THE character verb (discord_people.save_characters), so a seated character's retire frees the seat and asks
    # the bench, a respec rewrites the sheets, exactly as on the Me page.
    @me.command(name="char", description="Your characters: spec, main, extra roles, retire, delete, name a planned one, add an alt")
    @app_commands.describe(action="what to change (leave empty to see your characters and pick)")
    @app_commands.choices(action=CHAR_ACTION_CHOICES)
    async def me_char(interaction: discord.Interaction, action: app_commands.Choice[str] | None = None):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["member_char"](interaction, reg, action=action.value if action else None)


    # ---------------- /me absent, /me
    absent = app_commands.Group(name="absent", description="Future absences (reason is officer-only)", parent=me)

    @absent.command(name="add", description="Say you'll be away: pick the first day and how long")
    async def absent_add(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        await FLOWS["absence"](interaction, reg)

    @absent.command(name="list", description="Your upcoming absences")
    async def absent_list(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        await show_my_absences(interaction, reg)

    @absent.command(name="clear", description="I'm back: clear one of your absences — its sheets re-open for you")
    async def absent_clear(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        await FLOWS["absence_clear"](interaction, reg)

    async def show_my_absences(interaction: discord.Interaction, reg: Registry) -> None:
        """Your upcoming absences, each with its own Clear button — the same reply as the absences card's *My absences*."""
        from .discord_pool import clear_absences_view, my_absences_text

        m = reg.members.get(interaction.user.id)
        ups = m.upcoming_absences(reg.now_local().date().isoformat()) if m else []
        if not ups:
            await interaction.response.send_message("No upcoming absences. Add one with `/me absent add`.", ephemeral=True)
            return
        await interaction.response.send_message(my_absences_text(reg, ups), view=clear_absences_view(reg, m, ups), ephemeral=True)


    @me.command(name="wishlist", description="Your wishlist: add or remove items, place and main/offspec (the Me page for long lists)")
    async def me_wishlist(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["me_wishlist"](interaction, reg)

    @me.command(name="loot-ask", description="Ask the council about one of your awards (the answer comes to your Me page)")
    async def me_loot_ask(interaction: discord.Interaction):
        """L32 (design §5.49): pick your award → your words (one modal) → Send. flows_appeal.me_loot_ask; no LLM, no DM."""
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["me_loot_ask"](interaction, reg)

    @tree.command(name="apply", description="Apply to join the guild")
    async def apply(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, mode="apply")

    @me.command(name="view", description="Your plan, characters and upcoming absences")
    async def me_view(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        own = reg.application_state(interaction.user.id)  # their own application first (design §3.3 D)
        if not m:
            await interaction.response.send_message((own["text"] + "\n") if own else "Nothing registered yet — /register to start.", ephemeral=True)
            return
        today = reg.now_local().date().isoformat()
        e = discord.Embed(title=f"{m.display_name} · {reg.config.name}", colour=TEAL)
        if own and own["state"] == "open":
            e.description = own["text"]
        e.add_field(name="Characters", value="\n".join(char_line(ico, c) for c in m.active()) or "none", inline=False)
        held = rostered_on(interaction.client, reg, m)
        if held:
            e.add_field(name="Rosters", value=rostered_text(reg, held, with_char=True), inline=True)
        regular = reg.regular_series_of(m)
        if regular:  # the recurring schedules they're a regular of, as a person reads them (never a series key)
            e.add_field(name="Regular on", value="\n".join(f"{reg.series_label(k)} ({c.label})" for k, c in regular)[:1000], inline=False)
        primary, flex = reg.roles_of(m)
        e.add_field(name="Roles", value=f"{primary or '?'}" + (f" (+{', '.join(flex)})" if flex else "") + "\n-# from your spec/offspec · `/me char` → Flex adds more", inline=True)
        e.add_field(name="DMs", value=("off — " + inbox.DM_OFF_SENTENCE if m.dm_opt_out else "on") + "\n-# `/me dms` switches them", inline=True)
        box = inbox.inbox_lines(reg, interaction.client.raids.store(reg), m.discord_id) if hasattr(interaction.client, "raids") else []
        if box:  # asks to answer, then the unread notices (D26: outcomes are told here and on the Me page — never by DM)
            e.insert_field_at(0, name="Inbox", value="\n".join(box)[:1000], inline=False)
        ups = m.upcoming_absences(today)
        e.add_field(name="Upcoming absences", value="\n".join(reg.span_label(a.start, a.end) for a in ups) or "none", inline=True)
        e.add_field(name="Attendance", value=(await asyncio.to_thread(reg.attendance_text, m)) + "\n-# the Me page lists each run", inline=True)  # D49 (bench counts here; parses run files: off the loop)
        from .loot_ops import guild_ctx, loot_block, member_awards

        got = member_awards(reg, guild_ctx(interaction.client, reg), m, 10)
        e.add_field(name="Loot", value=loot_block(reg, got, ico) or "nothing yet — `/me wishlist` to say what you want", inline=False)
        wished = sum(len(c.wishlist) for c in m.active())
        if wished:
            e.add_field(name="Wishlist", value=f"{wished} item(s) across your characters · `/me wishlist`", inline=True)
        e.set_footer(text="Sheets: answer each run in the signup channel (Join / Bench / No thanks). Your loot and wishlist are on the Me page too.")
        view = None
        if regular:  # D7: a member can step down from a series' regulars themselves (volunteering is a request to officers)
            view = discord.ui.View(timeout=600)
            btn = discord.ui.Button(label="Step down from a regular raid…", style=discord.ButtonStyle.secondary)

            async def step_down(i: discord.Interaction) -> None:
                await FLOWS["regular_step_down"](i, reg)
            btn.callback = step_down
            view.add_item(btn)
        if reg.notices(m.discord_id, unread=True):  # the inbox's notices: read here, marked read with one press
            view = view or discord.ui.View(timeout=600)
            seen = discord.ui.Button(label="Mark notices read", style=discord.ButtonStyle.secondary)

            async def mark_read(i: discord.Interaction) -> None:
                n = reg.mark_read(i.user.id)
                await i.response.send_message(f"✅ {n} notice(s) marked read.", ephemeral=True)
            seen.callback = mark_read
            view.add_item(seen)
        await interaction.response.send_message(embed=e, ephemeral=True, **({"view": view} if view else {}))

    @me.command(name="dms", description="DMs from the bot on or off (off: your asks wait on the Me page)")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    async def me_dms(interaction: discord.Interaction, state: app_commands.Choice[str]):
        """The one member setting that had no Discord control (design B2): Registry.set_dm, the verb the Me page, plain
        text and MCP share; the reply is the one DM sentence (inbox.DM_ON/OFF_SENTENCE)."""
        reg, interaction = await need(interaction)
        if not reg:
            return
        if interaction.user.id not in reg.members:
            await interaction.response.send_message("Nothing registered yet — /register to start.", ephemeral=True)
            return
        on = state.value == "on"
        line = reg.set_dm(interaction.user.id, on, reg.members[interaction.user.id].display_name)
        await interaction.response.send_message(f"✅ {line}. " + (inbox.DM_ON_SENTENCE if on else inbox.DM_OFF_SENTENCE), ephemeral=True)

    tree.add_command(me)

    # ---------------- /roster (officers)
    roster = app_commands.Group(name="roster", description="Officer: the guild's registered characters")

    async def officer(interaction: discord.Interaction) -> tuple[Registry | None, discord.Interaction]:
        reg, interaction = await need(interaction)
        if reg and not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only (Manage Server or a configured officer role).", ephemeral=True)
            return None, interaction
        return reg, interaction

    @roster.command(name="list", description="All registered characters by class")
    async def roster_list(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        chars = reg.all_characters()
        if not chars:
            await interaction.response.send_message("Nobody has registered yet.", ephemeral=True)
            return
        e = discord.Embed(title=f"{reg.config.name} · {len(chars)} characters · {len(reg.members)} members", colour=TEAL)
        by_cls: dict[str, list[str]] = {}
        for m, c in chars:
            by_cls.setdefault(c.cls, []).append(f"{'★ ' if c.is_main else ''}{c.label} · {c.spec} · {c.rank}{' ⏳' if not c.confirmed_by else ''} · {m.display_name}")
        for cls, lines in by_cls.items():
            e.add_field(name=f"{ico('class', cls)} {cls} ({len(lines)})", value="\n".join(lines)[:1000], inline=True)
        pend = reg.pending()
        from .flows_roster import confirm_pending_view

        e.set_footer(text=f"★ main · ⏳ unconfirmed ({len(pend)})")
        view = confirm_pending_view(reg, len(pend))
        await interaction.response.send_message(embed=e, view=view or discord.utils.MISSING, ephemeral=True)

    @roster.command(name="confirm", description="Confirm registered characters: pick from the unconfirmed ones, one after another")
    async def roster_confirm(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_confirm"](interaction, reg)

    @roster.command(name="rank", description="Set a member's character rank: pick the character and rank")
    @app_commands.describe(member="whose character")
    async def roster_rank(interaction: discord.Interaction, member: discord.User):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_rank"](interaction, reg, member=member)

    @roster.command(name="set-main", description="Set a member's main on their behalf: pick the character")
    @app_commands.describe(member="whose main")
    async def roster_set_main(interaction: discord.Interaction, member: discord.User):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_set_main"](interaction, reg, member=member)

    @roster.command(name="overview", description="Who is planning to play what: classes, roles, flexibility, buff coverage")
    async def roster_plan(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        officer_view = is_officer(interaction, reg)
        e = overview_embed(reg, ico, officer=officer_view)
        if e is None:
            await interaction.response.send_message("Nobody has planned a main yet — point people at the registration card (`/gm config registration-channel`) or `/me plan main`.", ephemeral=True)
            return
        # always ephemeral (design §2.8): officers get *Post to #roster* for the officer version, never a public reply by accident
        view = overview_post_view(reg, ico) if officer_view and reg.config.roster_channel_id else None
        await interaction.response.send_message(embed=e, ephemeral=True, **({"view": view} if view else {}))

    @roster.command(name="comp", description="Desired comp: what readiness aims for per role, class or spec (bounds stay the owner's)")
    async def roster_comp(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:  # flows_config.CompTargetsWizard: only the targets block of the Group make-up screen → configops (authorize_ops + apply_ops)
            await FLOWS["roster_comp"](interaction, reg)

    @roster.command(name="char", description="A member's characters on their behalf: spec, main, extra roles, rank, retire, delete")
    @app_commands.describe(member="whose characters", action="what to change (leave empty to see their characters and pick)")
    @app_commands.choices(action=ROSTER_CHAR_CHOICES)
    async def roster_char(interaction: discord.Interaction, member: discord.User, action: app_commands.Choice[str] | None = None):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["member_char"](interaction, reg, member=member, action=action.value if action else None)

    @roster.command(name="back", description="Someone is back early: clear one of their absences — its sheets re-open for them")
    @app_commands.describe(member="who is back")
    async def roster_back(interaction: discord.Interaction, member: discord.User):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["absence_clear"](interaction, reg, member=member)

    @roster.command(name="add", description="Make someone a regular of a recurring schedule: pick the schedule, member and character")
    @app_commands.describe(member="who to add (optional: pick them in the wizard)")
    async def roster_add(interaction: discord.Interaction, member: discord.User | None = None):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_add"](interaction, reg, member=member)

    @roster.command(name="remove", description="Take someone off a schedule's regulars (they can still Join its sheets)")
    @app_commands.describe(member="who to remove (optional: pick them in the wizard)")
    async def roster_remove(interaction: discord.Interaction, member: discord.User | None = None):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_remove"](interaction, reg, member=member)

    @roster.command(name="members", description="A recurring schedule's regulars by role: pick the schedule")
    async def roster_members_cmd(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        await FLOWS["roster_members"](interaction, reg)

    @roster.command(name="absences", description="Upcoming absences across the guild (with reasons): pick how far ahead")
    async def roster_absences(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:  # the horizon is a preset select (the wizard rule), the days read as words (the clock rule)
            await FLOWS["roster_absences"](interaction, reg)

    @roster.command(name="absent", description="Record an absence on a member's behalf: pick the days, no typing")
    @app_commands.describe(member="who is away")
    async def roster_absent(interaction: discord.Interaction, member: discord.User):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await FLOWS["absence"](interaction, reg, member=member)

    @roster.command(name="remove-record", description="Remove the record of someone who left the server (one confirm screen)")
    @app_commands.describe(member="who left the Discord server")
    async def roster_remove_record(interaction: discord.Interaction, member: discord.User):
        reg, interaction = await officer(interaction)
        if reg:
            await remove_record_decide(interaction, reg, member.id)

    @roster.command(name="applicants", description="Open applications")
    async def roster_applicants(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        opens = reg.open_applicants()  # oldest first: the longest wait is decided first
        if not opens:
            await interaction.response.send_message("No open applications.", ephemeral=True)
            return
        shown = opens[:APPLICANTS_SHOWN]
        more = f"{len(opens) - len(shown)} more on the site's Applications page." if len(opens) > len(shown) else None
        await interaction.response.send_message(content=more, embeds=[application_embed(reg, a, ico) for a in shown], ephemeral=True)

    @roster.command(name="applicant", description="Accept or decline an application by member (one confirm screen)")
    @app_commands.choices(decision=[app_commands.Choice(name="accept", value="accept"), app_commands.Choice(name="decline", value="decline")])
    @app_commands.describe(note="optional: goes to the applicant with the verdict")
    async def roster_applicant(interaction: discord.Interaction, member: discord.User, decision: app_commands.Choice[str], note: str | None = None):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await application_decide(interaction, reg, member.id, decision.value == "accept", note)

    tree.add_command(roster)

    # ---------------- /gm (owner / officers)
    gm = app_commands.Group(name="gm", description="oibot_GM administration")
    config = app_commands.Group(name="config", description="Owner: bot configuration", parent=gm)

    @config.command(name="owner", description="Set up this server for the bot (first claim) or transfer ownership")
    async def cfg_owner(interaction: discord.Interaction, user: discord.User | None = None):
        if guilds.for_interaction(interaction) is None:  # design §5.32: the only command that works in an unconfigured server
            if interaction.guild_id:
                await FLOWS["guild_claim"](interaction, None, owner=user)
                return
            if not (getattr(guilds, "ordered", None) and guilds.ordered()):
                await interaction.response.send_message("Run this in the server you want to set up.", ephemeral=True)
                return
        reg, interaction = await need(interaction)
        if not reg:
            return
        if reg.config.owner_discord_id:  # a transfer: the owner (or a host superuser) picks the person, then one decide screen (D50)
            if not is_owner(interaction, reg):
                await interaction.response.send_message("Only the current owner can transfer ownership.", ephemeral=True)
                return
            await FLOWS["owner_transfer"](interaction, reg, user=user)
            return
        if not reg.config.owner_discord_id:  # first claim: only the server owner or someone with Manage Server
            m = interaction.user if isinstance(interaction.user, discord.Member) else None
            if not (is_superuser(interaction.user.id) or (m and interaction.guild and (interaction.guild.owner_id == m.id or m.guild_permissions.manage_guild))):
                await interaction.response.send_message("Nobody owns this guild yet; the Discord server owner (or someone with Manage Server) claims it first.", ephemeral=True)
                return
        target = user or interaction.user
        try:  # THE owner verb (refusals: a bot, someone not in the server); claiming it yourself is no "that's you"
            line = await asyncio.to_thread(reg.set_owner, target.id, interaction.user.display_name, by_id=None if target.id == interaction.user.id else interaction.user.id,
                                           bot=bool(getattr(target, "bot", False)), membership="yes" if isinstance(target, discord.Member) or target.id == interaction.user.id else "unknown",
                                           name=target.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Owner: {target.mention}", ephemeral=True)
        await ops_mod.audited(ops, reg, interaction.user.display_name, line, level="warn", by_id=interaction.user.id)

    # ---- channels (design §3.4 X3): eight shortcuts → ONE helper and the one-screen confirm (flows_config.ChannelSetWizard);
    # the hub /gm config channels drafts several at once. None of them writes reg.config: SetupMixin.set_channel does,
    # with its permission check, the card posted first and its ONE ops line.
    async def channel_command(interaction: discord.Interaction, kind: str, channel: discord.TextChannel | None, clear: bool) -> None:
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["channel_set"](interaction, reg, kind=kind, channel=channel, clear=clear)

    def channel_shortcut(kind: str, name: str, desc: str) -> None:
        @config.command(name=name, description=desc)
        @app_commands.describe(channel="the text channel (Discord's picker)", clear="tick to stop using a channel for this")
        async def cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None, clear: bool = False):
            await channel_command(interaction, kind, channel, clear)

    for _kind, _name, _desc in CHANNEL_SHORTCUTS:
        channel_shortcut(_kind, _name, _desc)

    @config.command(name="channels", description="Owner: every bot channel at once — pick, review what each change does, save")
    async def cfg_channels(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["channels"](interaction, reg)

    @config.command(name="officers", description="Owner: the officer roles — Discord's role picker, review, save")
    async def cfg_officers(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["officers"](interaction, reg)

    @config.command(name="companion", description="Owner: loot companion tokens — issue one (DMed once to the raid leader), revoke, presence")
    async def cfg_companion(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["companion"](interaction, reg)

    @config.command(name="officer-role", description="Owner: add or remove a Discord role that counts as officer")
    async def cfg_role(interaction: discord.Interaction, role: discord.Role, remove: bool = False):
        reg, interaction = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        # one widget, reversible, immediate — through the op path (configops.apply_async: role refusal, one audit line), so the
        # empty-list consequence comes from configops.op_consequences once, never composed here
        from . import configops

        op = configops.ConfigOp(op="role_remove" if remove else "role_add", value=str(role.id))
        cons = configops.op_consequences(reg, op)
        try:
            await configops.apply_async(reg, op, interaction.user.display_name, True, bot=interaction.client, by_id=interaction.user.id, via="discord")
        except (RegistryError, ValueError) as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        names = ", ".join(f"<@&{r}>" for r in reg.config.officer_role_ids) or "none"
        await interaction.response.send_message(f"✅ Officer roles: {names}" + "".join(f"\n⚠ {c}" for c in cons), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @config.command(name="errors-to", description="Owner: who gets the bot's error alerts (one DM per distinct error per 30 min)")
    @app_commands.describe(member="who gets them (or stops getting them, with remove)", remove="tick to take them off the list",
                           reset="tick to go back to the default: the owner")
    async def cfg_errors_to(interaction: discord.Interaction, member: discord.User | None = None, remove: bool = False, reset: bool = False):
        reg, interaction = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        from . import configops

        if reset:
            value = "owner"
        elif member is None:
            await interaction.response.send_message(f"Error alerts go to {reg.error_dm_words()}. Pick a member (tick remove to take them off), or tick reset.",
                                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        elif getattr(member, "bot", False):
            await interaction.response.send_message("A bot can't read alerts — pick a person.", ephemeral=True)
            return
        else:
            have = reg.error_dm_targets()
            want = [u for u in have if u != member.id] if remove else list(dict.fromkeys(have + [member.id]))
            value = ", ".join(f"<@{u}>" for u in want) or "none"
        try:
            line = await configops.apply_async(reg, configops.ConfigOp(op="set", path="error_dms", value=value), interaction.user.display_name, True,
                                               bot=interaction.client, by_id=interaction.user.id, via="discord")
        except (RegistryError, ValueError) as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {line[:1:].upper() + line[1:]}. Errors only — info and warnings stay in the ops channel.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @config.command(name="aura", description="Owner: what we know about a buff — scope, stacking family, strength, status, who benefits")
    async def cfg_aura(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["aura"](interaction, reg)

    @config.command(name="loot-channel", description="Owner: the public loot log — one line per confirmed award (log off: awards stay on the site)")
    @app_commands.describe(channel="where award lines go (Discord's picker)", clear="tick to stop using a channel: award lines go to each run's updates thread",
                           log="the public loot log on or off")
    async def cfg_loot_channel(interaction: discord.Interaction, channel: discord.TextChannel | None = None, clear: bool = False, log: bool | None = None):
        # the ninth channel kind: the same helper and one-screen confirm as the eight shortcuts (flows_config.ChannelSetWizard),
        # with the log toggle said and saved on that screen
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["channel_set"](interaction, reg, kind="loot", channel=channel, clear=clear, log=log)

    @config.command(name="ask-audience", description="Owner: who may ask the bot free-form questions (/ask, DMs, @mentions); others get the static guide")
    @app_commands.choices(audience=[app_commands.Choice(name=a, value=a) for a in ("officers", "confirmed", "registered", "everyone")])
    async def cfg_ask_audience(interaction: discord.Interaction, audience: app_commands.Choice[str]):
        reg, interaction = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        reg.config.ask_audience = audience.value
        reg.save_config(f"ask audience → {audience.value}")
        await interaction.response.send_message(f"✅ Free-form questions: **{audience.value}**. Everyone else gets the static guide (about, schedule, how to register, signups, apply, contact).", ephemeral=True)

    @config.command(name="raid", description="Owner: a raid's rules — run times, signup cadence, make-up, weights, lockout, notes")
    async def cfg_raid(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["raid_config"](interaction, reg)

    @config.command(name="about", description="Owner: one-paragraph public blurb for the static guide ('About the guild')")
    async def cfg_about(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["about"](interaction, reg)

    @config.command(name="recruitment", description="Owner: applications open or closed, the note, the guild's questions, member roles, invite")
    async def cfg_recruitment(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["recruitment"](interaction, reg)

    @config.command(name="timezone", description="Owner: the guild's timezone for every run time, lock and lockout")
    async def cfg_tz(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["timezone"](interaction, reg)

    @config.command(name="show", description="Effective configuration")
    async def cfg_show(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if not reg:
            return
        if not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        from .configops import config_summary  # words, mentions and 12-hour times — never the raw YAML (the clock rule)

        audience = "owner" if reg.is_owner_id(interaction.user.id) else "officer"  # the Host section: the owner (and the host) only
        await interaction.response.send_message(embed=config_show_embed(reg, config_summary(reg, interaction.client, audience)), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def roster_regcard(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await interaction.response.send_message(embed=registration_card(reg), view=registration_view(reg))
        try:
            msg = await interaction.original_response()
            await msg.pin()
        except Exception:  # noqa: BLE001
            pass
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} posted the registration card in #{getattr(interaction.channel, 'name', '?')}")

    @gm.command(name="status", description="Bot health: data repo, registry, spend, recent ops")
    async def gm_status(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        st = guilds.store
        up = int(time.time() - STARTED)
        provider = getattr(interaction.client, "ctx", None) and interaction.client.ctx.provider
        chars = reg.all_characters()
        whole = " (whole bot)" if getattr(guilds, "many", False) else ""  # with one guild the card reads exactly as before
        e = discord.Embed(title=f"oibot_GM status · {reg.config.name}", colour=TEAL)
        e.add_field(name="Bot" + whole, value=f"up {up // 3600}h{(up % 3600) // 60}m · profile {reg.profile.name}", inline=True)
        e.add_field(name="Data repo" + whole, value=f"{st.root.name} @ {st.head()} · push {'on' if st.push_enabled else 'off'}", inline=True)
        e.add_field(name="Registry", value=f"{len(reg.members)} members · {len(chars)} characters · {len(reg.pending())} unconfirmed", inline=True)
        e.add_field(name="Config", value=f"owner {'<@%d>' % reg.config.owner_discord_id if reg.config.owner_discord_id else '—'} · ops {'<#%d>' % reg.config.ops_channel_id if reg.config.ops_channel_id else '—'} · officer roles {', '.join(reg.officer_role_names()) or '—'}", inline=False)
        e.add_field(name="LLM" + whole, value=provider.summary()[:1000] if provider else "off", inline=False)
        from .discord_feed import feed_status

        e.add_field(name="Loot feed", value=feed_status(interaction.client, reg)[:500], inline=False)
        gov = getattr(interaction.client, "governance_status", None)  # NewsMixin: the news review and the apply job (design §3.4)
        if gov is not None:
            e.add_field(name="Agents", value="\n".join(gov(reg))[:1000], inline=False)
        checklist = getattr(interaction.client, "setup_checklist", None)
        if checklist is not None:  # the Setup card's rows the runtime marks red (perms.missing_for, a required channel unset …)
            bad = [r for r in checklist(reg) if r["state"] == "bad"]
            e.add_field(name="Setup", value=("\n".join(f"✗ {r['label']}: {r['text']}" for r in bad)[:1000] + "\n-# /gm config channels · the Config page's Setup card")
                        if bad else "✓ ready — every channel the bot uses passes its permission check", inline=False)
        parked = guilds.detached_list() if is_superuser(interaction.user.id) and hasattr(guilds, "detached_list") else []
        if parked:  # the host only: guilds whose server removed the bot (nothing runs; nothing deleted)
            e.add_field(name="Parked (host)", value=", ".join(r.config.name for r in parked)[:500], inline=False)
        recent = ops.recent_for(reg.key)
        if recent:
            e.add_field(name="Recent ops", value="\n".join(f"`{t}` {LEVEL[l]} {x}"[:120] for t, l, x in recent[-8:])[:1000], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ---------------- /gm test: seed puppet members and rehearse the whole cycle in Discord within the hour
    test = app_commands.Group(name="test", description="Officers: test bench — seed fake members, open a compressed run, answer for them", parent=gm)

    @test.command(name="seed", description="Officer: add test members to the bench (5, 10, 20 or all of them)")
    async def test_seed(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["test_seed"](interaction, reg)

    @test.command(name="run", description="Officer: open a short test run now (pick raid and tempo)")
    async def test_run(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["test_run"](interaction, reg)

    @test.command(name="answer", description="Officer: test members answer a sheet (a random mix, or one member)")
    async def test_answer(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["test_answer"](interaction, reg)

    @test.command(name="compose", description="Officer: the bench by composition — class, spec, how many; review, then Save")
    async def test_compose(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:  # flows_test.TestComposeWizard → SetupMixin.test_compose (the site's spec grid writes through it too)
            await FLOWS["test_compose"](interaction, reg)

    @test.command(name="comp", description="Officer: try a comp — every test member joins a sandbox run; its board builds the best comp")
    async def test_comp(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:  # flows_test.TestCompWizard → SetupMixin.test_comp, then a link to the sandbox run's board
            await FLOWS["test_comp"](interaction, reg)

    @test.command(name="clear", description="Officer: end the test — cancel test runs, remove every test member and their placements")
    async def test_clear(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:  # the decide screen (consequence · Clear the test bench · Back), then SetupMixin.test_bench_clear
            await FLOWS["test_clear"](interaction, reg)

    tree.add_command(gm)
    # exported for other command modules
    register_commands.member_char_autocomplete = member_char_autocomplete  # type: ignore[attr-defined]


LEVEL = ops_mod.LEVEL_ICON
