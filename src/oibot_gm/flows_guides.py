"""The guild guide library in Discord (design §5.63; the verbs and
their words are guides/library.py). `/guide …` is registered by discord_guides.py; every screen here is ephemeral.

- `guide_list`: each faction's latest and up to four older versions, with the uploader, and the Me page's link.
- `guide_get`: faction select (lines with a live version) → version select (latest first, newest 25) → the BattleTag
  modal (prose, validated by validate_battletag, never parsed) → the version locked to that tag, DMed as a .txt (the
  member asked: an act under the DM rule); a refused DM puts the file on the private reply. Nothing is written, no
  ops line, no decide screen.
- `guide_upload`: the attachment and the faction are widgets (the Wizard rule). Membership, the .txt name and the size
  from Discord's metadata are checked BEFORE the file is read; the BattleTag modal → the file read → the decide screen
  with library.upload_consequence → library.upload → one ops line.
- `guide_withdraw` / `guide_restore`: officers, decided live (PRIV_LIVE) on open and on every press → a version select →
  the decide screen with the library's sentence → the verb → one ops line.

D7: the BattleTag and the import string live only in the wizard object while it is open (dropped at its end), never in
a title (the open-screen mirror keeps titles), an ops line or a log record; an unexpected failure is logged by its type
alone and the person reads configops.GENERIC_ERROR."""
from __future__ import annotations

import asyncio
import io
import logging

import discord

from . import guild_routing
from . import ops as ops_mod
from .guides import library as lib
from .guides.build import BAD_TAG, EMPTY_TAG, TEXT_MAX, TOO_BIG, validate_battletag
from .guides.rxp_decode import WRONG_KEY, GuideError
from .registry import Registry
from .wizard import Field, Form, Opt, Wizard, flow

log = logging.getLogger(__name__)

SELECT_MAX = 25
LIST_PER_LINE = 5  # the latest and up to four older versions per faction on /guide list
EMPTY = "No guild guide yet. Upload one with /guide upload or on the Me page."
GET_HINT = "Get one locked to your BattleTag with /guide get."
NOT_TXT = "Attach the guide as a .txt file: the RestedXP import string saved as text."
EMPTY_FILE = "That file is empty. Attach the RestedXP import string saved as a .txt file."
NOTHING_TO_WITHDRAW = "No guild guide is in members' reach to withdraw. Members upload them with /guide upload or on the Me page."
NOTHING_TO_RESTORE = "No guild guide is withdrawn, so there is nothing to restore."
OFFICERS_ONLY = "Officers only."
_TAG_REFUSALS = {BAD_TAG, EMPTY_TAG, WRONG_KEY}  # a refusal the person fixes by typing the BattleTag again


def not_member(reg: Registry) -> str:
    return f"Only members of {reg.config.name} can use its guild guides."


async def belongs(interaction: discord.Interaction, reg: Registry) -> bool:
    """A member of the guild: used inside its own server, else its owner or a member of its server now (strict)."""
    gid = getattr(interaction, "guild_id", None)
    if gid and reg.config.discord_guild_id and int(gid) == int(reg.config.discord_guild_id):
        return True
    return await guild_routing.may_access(interaction.client, reg, interaction.user.id)


async def is_officer(bot, reg: Registry, user) -> bool:
    """Officer or owner, decided now (a write never trusts a cached role — design §2.6)."""
    return (await guild_routing.resolve_role(bot, reg, user, max_age=guild_routing.PRIV_LIVE)).officer


def words(reg: Registry, v: lib.GuideVersion) -> str:
    """'Horde · Mon 9 Nov 3:00 PM': a version as every line names it."""
    return f"{lib.FACTION_WORDS[v.faction]} · {lib.when(reg, v)}"


def site_link(reg: Registry) -> str | None:
    from .help import site_url

    url = site_url(reg, "/app/me")
    return f"{url}&tab=guide" if url else None


def tag_form(on_submit, why: str) -> Form:
    return Form("Your BattleTag", [Field("tag", "Your BattleTag", description=why, placeholder="Name#1234", min_length=3, max_length=40)], on_submit)


async def _say(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _gate(interaction: discord.Interaction, reg: Registry) -> bool:
    """The library is on and the person belongs to the guild; otherwise they are told why (True = go on)."""
    if not lib.available(reg):
        await _say(interaction, lib.NO_TOOL)
        return False
    if not await belongs(interaction, reg):
        await _say(interaction, not_member(reg))
        return False
    return True


def _failed(what: str, reg: Registry, e: BaseException) -> str:
    """An unexpected failure: the log carries the exception's TYPE only (its words may hold a BattleTag or the string)."""
    from .configops import GENERIC_ERROR

    log.error("guild guide %s failed: %s (guild=%s)", what, type(e).__name__, reg.key)
    return f"❌ {GENERIC_ERROR}"


async def _audit(bot, reg: Registry, by: str, by_id: int, line: str) -> None:
    ops = getattr(bot, "ops", None)
    if ops is None:
        return
    try:
        await ops_mod.audited(ops, reg, by, line, via="discord", by_id=by_id)
    except Exception as e:  # noqa: BLE001 — the write landed; the reply still goes out
        log.error("guild guide ops line failed: %s", type(e).__name__)


# ---------------------------------------------------------------- /guide list

def list_text(reg: Registry) -> str:
    esc = discord.utils.escape_markdown
    blocks = []
    for f in lib.FACTIONS:
        vs = lib.versions(reg, f, live_only=True)[:LIST_PER_LINE]
        if vs:
            blocks.append("\n".join([f"**{lib.FACTION_WORDS[f]}**"] + [f"• {lib.when(reg, v)}{' · Latest' if i == 0 else ''} — by {esc(v.by_name)}"
                                                                       for i, v in enumerate(vs)]))
    url = site_link(reg)
    tail = (GET_HINT + (f" On the site: {url}" if url else "")) if blocks else (EMPTY + (f"\n{url}" if url else ""))
    return "\n\n".join(blocks + [tail])


@flow("guide_list")
async def guide_list(interaction: discord.Interaction, reg: Registry) -> None:
    if not await _gate(interaction, reg):
        return
    text = await asyncio.to_thread(list_text, reg)
    await _say(interaction, f"**Guild guides**\n{text}"[:2000])


# ---------------------------------------------------------------- /guide get

class GetWizard(Wizard):
    def __init__(self, reg: Registry, uid: int):
        super().__init__(reg, uid, title="Get a guild guide")
        self.vid: str | None = None

    async def factions(self, interaction: discord.Interaction, lines: list[str]) -> None:
        opts = [Opt(f, lib.FACTION_WORDS[f]) for f in lines]
        await self.show(interaction, "Pick the faction. You get the guide locked to your own BattleTag, by DM.",
                        self.select("Pick the faction…", opts, self.faction_picked), self.button("Cancel", self.cancel))

    async def faction_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        vs = (await asyncio.to_thread(lib.versions, self.reg, values[0], True))[:SELECT_MAX]
        if not vs:
            await self.finish(interaction, EMPTY)
            return
        opts = [Opt(v.id, lib.when(self.reg, v), description=f"{'Latest · ' if i == 0 else ''}by {v.by_name} · {v.guides} guide{'s' if v.guides != 1 else ''}")
                for i, v in enumerate(vs)]
        await self.show(interaction, f"**{lib.FACTION_WORDS[values[0]]}**: pick the version. The latest is first.",
                        self.select("Pick the version…", opts, self.version_picked), self.button("Cancel", self.cancel))

    async def version_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.vid = values[0]
        await self.ask_tag(interaction)

    async def ask_tag(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, tag_form(self.submitted, "The guide is locked to this account. The bot doesn't keep the BattleTag."))

    async def submitted(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        try:
            tag = validate_battletag(values.get("tag", ""))
        except GuideError as e:
            await self.refuse(interaction, str(e), self.ask_tag, "Fix my BattleTag")
            return
        await self.working(interaction, "Locking the guide to your BattleTag…")
        try:
            out, v = await asyncio.to_thread(lambda: (lib.issue(self.reg, self.vid or "", tag), lib.get(self.reg, self.vid or "")))
        except GuideError as e:
            msg = str(e)
            await self.refuse(interaction, msg, self.ask_tag if msg in _TAG_REFUSALS else None, "Fix my BattleTag")
            return
        except Exception as e:  # noqa: BLE001 — D7: never the exception's words
            await self.finish(interaction, _failed("get", self.reg, e))
            return
        data, fn = out.encode(), lib.issue_filename(v, tag)
        where = f" from {self.reg.config.name}" if self.reg.many_guilds else ""
        line = (f"Your guild guide{where} ({words(self.reg, v)}), locked to **{discord.utils.escape_markdown(tag)}**: "
                "import the attached string with RestedXP.")
        try:
            await interaction.user.send(line, file=discord.File(io.BytesIO(data), filename=fn), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:  # DMs closed (Forbidden) or Discord refused: the file comes here instead
            await self.finish_with_file(interaction, f"I can't DM you, so here it is. {line}", discord.File(io.BytesIO(data), filename=fn))
            return
        await self.finish(interaction, f"Sent to your DMs: {words(self.reg, v)}.")

    async def finish_with_file(self, interaction: discord.Interaction, text: str, file: discord.File) -> None:
        """The last screen with the file attached (after `working`, so the original response is edited)."""
        self._closed = True
        self.stop()
        await interaction.edit_original_response(content=f"**{self.title}**\n{text}"[:2000], embed=None, view=None, attachments=[file])


@flow("guide_get")
async def guide_get(interaction: discord.Interaction, reg: Registry) -> None:
    if not await _gate(interaction, reg):
        return
    lines = [f for f in lib.FACTIONS if await asyncio.to_thread(lib.latest, reg, f)]
    if not lines:
        await _say(interaction, EMPTY)
        return
    await GetWizard(reg, interaction.user.id).factions(interaction, lines)


# ---------------------------------------------------------------- /guide upload

class UploadWizard(Wizard):
    """Holds the attachment (read once, after the BattleTag) and the tag only while it is open: dropped at its end."""

    def __init__(self, reg: Registry, uid: int, name: str, file, faction: str):
        super().__init__(reg, uid, title="Upload a guild guide")
        self.name, self.file, self.faction = name, file, faction
        self.text: str | None = None
        self.tag: str | None = None

    def drop(self) -> None:
        self.text = self.tag = None
        self.file = None

    async def finish(self, interaction: discord.Interaction, text: str) -> None:
        self.drop()
        await super().finish(interaction, text)

    async def on_timeout(self) -> None:
        self.drop()
        await super().on_timeout()

    async def ask_tag(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, tag_form(self.submitted, "The account the guide is locked to now. It unlocks it once and isn't kept."))

    async def submitted(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        try:
            self.tag = validate_battletag(values.get("tag", ""))
        except GuideError as e:
            await self.refuse(interaction, str(e), self.ask_tag, "Fix my BattleTag")
            return
        if self.text is None:  # the first submit: read the file now (the checks ran at the command)
            await self.working(interaction, "Reading the file…")
            try:
                data = await self.file.read()
            except Exception as e:  # noqa: BLE001 — Discord's CDN refused: nothing kept
                await self.finish(interaction, _failed("upload read", self.reg, e))
                return
            if len(data) > TEXT_MAX:  # the metadata said less: never trusted alone
                await self.finish(interaction, f"❌ {TOO_BIG}")
                return
            self.text = data.decode("utf-8", "replace")
            if not self.text.strip():
                await self.finish(interaction, f"❌ {EMPTY_FILE}")
                return
        await self.decide(interaction, lib.upload_consequence(self.reg, self.faction), do=self.upload, do_label="Upload")

    async def upload(self, interaction: discord.Interaction, _note: str | None) -> None:
        try:
            v = await asyncio.to_thread(lib.upload, self.reg, self.text or "", self.tag or "", self.faction, interaction.user.id, self.name)
        except GuideError as e:
            msg = str(e)
            await self.refuse(interaction, msg, self.ask_tag if msg in _TAG_REFUSALS else None, "Fix my BattleTag")
            return
        except Exception as e:  # noqa: BLE001 — D7: never the exception's words
            await self.finish(interaction, _failed("upload", self.reg, e))
            return
        self.mark_saved()
        await _audit(interaction.client, self.reg, self.name, interaction.user.id, f"guild guide uploaded: {words(self.reg, v)}")
        await self.finish(interaction, f"✅ Uploaded: {words(self.reg, v)}.")


@flow("guide_upload")
async def guide_upload(interaction: discord.Interaction, reg: Registry, *, file, faction: str) -> None:
    """`file` = the slash command's attachment, `faction` = its Choice. Every check that needs no read comes first: the
    file is opened only after the membership, name and size checks and the BattleTag."""
    if not await _gate(interaction, reg):
        return
    if faction not in lib.FACTIONS:
        await _say(interaction, lib.BAD_FACTION)
        return
    if not (getattr(file, "filename", "") or "").lower().endswith(".txt"):
        await _say(interaction, NOT_TXT)
        return
    if (getattr(file, "size", 0) or 0) > TEXT_MAX:
        await _say(interaction, TOO_BIG)
        return
    wiz = UploadWizard(reg, interaction.user.id, interaction.user.display_name, file, faction)
    await wiz.ask_tag(interaction)


# ---------------------------------------------------------------- /guide withdraw · restore

async def _officer_flow(interaction: discord.Interaction, reg: Registry, verb: str) -> None:
    bot = interaction.client
    if not lib.available(reg):
        await _say(interaction, lib.NO_TOOL)
        return
    if not await is_officer(bot, reg, interaction.user):
        await _say(interaction, OFFICERS_ONLY)
        return
    withdraw = verb == "withdraw"
    vs = [v for v in await asyncio.to_thread(lib.versions, reg) if bool(v.withdrawn_at) != withdraw][:SELECT_MAX]
    if not vs:
        await _say(interaction, NOTHING_TO_WITHDRAW if withdraw else NOTHING_TO_RESTORE)
        return

    async def guard(i: discord.Interaction) -> str | None:  # every press: live, never the cache
        return None if await is_officer(bot, reg, i.user) else OFFICERS_ONLY

    wiz = Wizard(reg, interaction.user.id, title="Withdraw a guild guide" if withdraw else "Restore a guild guide", guard=guard)
    latest = {f: getattr(lib.latest(reg, f), "id", None) for f in lib.FACTIONS}

    def describe(v: lib.GuideVersion) -> str:
        if v.withdrawn_at:
            return f"by {v.by_name} · withdrawn by {v.withdrawn_by or 'an officer'}"
        return f"{'Latest · ' if latest[v.faction] == v.id else ''}by {v.by_name}"

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        v = await asyncio.to_thread(lib.get, reg, values[0])
        if v is None or bool(v.withdrawn_at) == withdraw:  # someone else got there first
            await wiz.finish(i, f"❌ {lib.NO_VERSION if v is None else (lib.NOT_WITHDRAWN if not withdraw else 'Already withdrawn.')}")
            return
        text = lib.withdraw_consequence(reg, v, by_id=i.user.id) if withdraw else lib.restore_consequence(reg, v)

        async def do(di: discord.Interaction, _note: str | None) -> None:
            fn = lib.withdraw if withdraw else lib.restore
            try:
                done = await asyncio.to_thread(fn, reg, v.id, di.user.display_name, di.user.id)
            except GuideError as e:
                await wiz.finish(di, f"❌ {e}")
                return
            except Exception as e:  # noqa: BLE001
                await wiz.finish(di, _failed(verb, reg, e))
                return
            wiz.mark_saved()
            past = "withdrawn" if withdraw else "restored"
            await _audit(bot, reg, di.user.display_name, di.user.id, f"guild guide {past}: {words(reg, done)}")
            await wiz.finish(di, f"✅ {past.title()}: {words(reg, done)}.")

        await wiz.decide(i, text, do=do, do_label="Withdraw" if withdraw else "Restore", danger=withdraw)

    opts = [Opt(v.id, words(reg, v), description=describe(v)) for v in vs]
    head = ("Pick the version to take out of members' reach. It is kept, and you can restore it." if withdraw
            else "Pick the withdrawn version to bring back to members.")
    await wiz.show(interaction, head, wiz.select("Pick the version…", opts, picked), wiz.button("Cancel", wiz.cancel))


@flow("guide_withdraw")
async def guide_withdraw(interaction: discord.Interaction, reg: Registry) -> None:
    await _officer_flow(interaction, reg, "withdraw")


@flow("guide_restore")
async def guide_restore(interaction: discord.Interaction, reg: Registry) -> None:
    await _officer_flow(interaction, reg, "restore")


__all__ = ["GetWizard", "UploadWizard", "guide_get", "guide_list", "guide_restore", "guide_upload", "guide_withdraw", "list_text"]
