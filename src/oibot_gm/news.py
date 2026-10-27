"""News review (design.md §5.26): what the #news webhook posts, filtered to what this guild plays, and the
proposals a daily review makes from it.

Ingest is code: the bot keyword-filters each embed (title + description only — the linked article is never
fetched, Wowhead is never scraped) and appends matches to `<guild>/news.jsonl`, deduped by url. The review job
(scripts/agents) reads that list, compares it with the effective profile and guild settings, and posts
*proposals*: a cited contradiction, the change in words and as a typed edit. Officers approve or dismiss; the
local apply job carries out approved ones. A proposal is data in `<guild>/proposals/<id>.yaml`, one file each,
every state change a commit (git log = the audit trail).

States: proposed → approved | dismissed → applying → applied | failed | reverted. Approving only flips the state
and records who; nothing is applied by the bot on a button press.

Governance (dual interfaces phase 3, design §5.45): approving a `profile` proposal is the OWNER's (it edits shared game
files and restarts the whole process, every served guild with it); the apply job's steps need the job's own header
(`X-Oibot-Job`, so `job=True`) and a job never makes an officer's decision; a repeated decision is an informational
refusal naming the first decider (`AlreadyDecided`); `annotate` writes the job's "waiting: …" line onto the proposal
without moving it; `guild_busy` is D33's raid guard (a locked run within 4 h of its start until it closes, an open run
within the hour, a live loot council)."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .store import GitStore


def join_words(words: list[str]) -> str:
    """'A', 'A and B', 'A, B and C' (registry.join_words, kept here so news imports no registry)."""
    words = [str(w) for w in words]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""

NEWS_FILE = "news.jsonl"
PROPOSALS_DIR = "proposals"
KINDS = ("guild_setting", "profile", "needs_developer")
STATES = ("proposed", "approved", "dismissed", "applying", "applied", "failed", "reverted")
CONFIDENCE = ("low", "medium", "high")
# who may move a proposal where: officers decide (web, Discord), the apply job (the owner's token) reports progress
OFFICER_MOVES = {("proposed", "approved"), ("proposed", "dismissed"), ("approved", "dismissed"), ("failed", "approved"), ("failed", "dismissed")}
JOB_MOVES = {("approved", "applying"), ("approved", "failed"), ("applying", "applied"), ("applying", "failed"), ("applying", "reverted"), ("applied", "reverted")}
# a guild setting is one of these config ops (configops.ConfigOp fields); anything else is a profile edit or a developer's job
PROFILE_OWNER_ONLY = ("Only the owner approves a game-data change. It edits the shared game files, and the bot restarts for every "
                      "server it serves.")
SETTING_OWNER_ONLY = ("Only the owner approves this change. Raids and buffs are the owner's settings on every surface, and a "
                      "proposal doesn't widen that.")
JOB_NOT_OFFICER = "the apply job only reports its own steps; an officer decides whether a proposal goes ahead"
INTERRUPTED = "The apply run was interrupted. Approve it again to retry."  # the job's recovery note and the owner's Mark as interrupted
RECOVER_MOVE = ("applying", "failed")  # the owner's "Mark as interrupted" (Agents page) while the apply job is not running
DECISIONS = ("approved", "dismissed")  # an officer's verdict (OFFICER_MOVES' targets); from MCP it needs X-Oibot-Actor (D8)
MCP_ACTOR_NEEDED = ("Approving or dismissing a proposal is a decision a person makes: from MCP it needs the X-Oibot-Actor header "
                    "naming an officer of {guild} (set OIBOT_MCP_ACTOR to their Discord id; a game-data change, the owner's) — "
                    "the owner's token alone can read proposals, post them and run the jobs.")
BUSY_LOCKED_HOURS = 4  # D33: a locked run counts as raid time from 4 h before its start until it closes
BUSY_OPEN_HOURS = 1  # D33: an open run counts from 1 h before its start
BUSY_LOOT_MIN = 30  # D33: a live loot council holds a restart however late it runs; `until` is at least this far out (D10's idle end)
SETTING_OPS = ("raid_set", "raid_reset", "aura_set", "family_set", "aura_reset", "comp_target", "comp_target_clear", "comp_groups")
MAX_ITEMS_PER_CALL = 200
PROPOSAL_LISTENERS: list = []  # fn(guild key, proposal id) after every proposal save (web/api_agents publishes `agents`)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- keywords

def version_terms(profile) -> list[str]:
    """The game version's own names, from `profiles/<version>/news.yaml` (data, not code)."""
    p = Path(profile.root) / "news.yaml"
    doc = yaml.safe_load(p.read_text()) if p.exists() else {}
    return [str(t) for t in (doc or {}).get("version_terms", []) if str(t).strip()]


def keyword_sets(reg) -> dict[str, list[str]]:
    """What an embed is matched against: the version's names, the guild's raids (effective names), buff names,
    class names, and the guild's extra list (`news_keywords`, set on the Config page or in plain text)."""
    prof = reg.profile
    raids = sorted({str(reg.raid_def(rid).get("name") or rid) for rid in prof.raids})
    buffs = sorted({b.short for b in prof.buffs if b.short})
    return {"version": version_terms(reg.base_profile), "raid": raids, "buff": buffs, "class": sorted(prof.classes),
            "extra": [k for k in (reg.config.news_keywords or []) if k.strip()]}


def _pattern(term: str) -> re.Pattern:
    t = re.escape(term.strip())
    head = r"\b" if term.strip()[:1].isalnum() else ""
    tail = r"\b" if term.strip()[-1:].isalnum() else ""
    return re.compile(head + t + tail, re.IGNORECASE)


def match(sets: dict[str, list[str]], text: str) -> tuple[bool, list[str]]:
    """(relevant, matched keywords). Relevant = a version name, a raid name or one of the guild's extra words
    appears; buff and class names only annotate (they also appear in every other version's news)."""
    hits: dict[str, list[str]] = {}
    for group, terms in sets.items():
        for term in terms:
            if term and _pattern(term).search(text or ""):
                hits.setdefault(group, []).append(term)
    relevant = any(hits.get(g) for g in ("version", "raid", "extra"))
    matched = [t for g in ("version", "raid", "extra", "buff", "class") for t in hits.get(g, [])]
    return relevant, list(dict.fromkeys(matched))


def embed_items(message) -> list[dict]:
    """A Discord message's news items: one per embed (title, description, url), or the message text when it has no
    embed. Only the text Discord already holds; the linked page is never fetched."""
    out = []
    for e in getattr(message, "embeds", None) or []:
        title, desc, url = (getattr(e, "title", None) or "").strip(), (getattr(e, "description", None) or "").strip(), (getattr(e, "url", None) or "").strip()
        if title or desc:
            out.append({"title": title[:300], "description": desc[:2000], "url": url})
    if not out and (getattr(message, "content", "") or "").strip():
        text = message.content.strip()
        url = next(iter(re.findall(r"https?://\S+", text)), "")
        out.append({"title": text.splitlines()[0][:300], "description": text[:2000], "url": url})
    return out


# ---------------------------------------------------------------- the news list

class NewsStore:
    """`<guild>/news.jsonl`: one row per relevant item, deduped by url (by title when there is none)."""

    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key
        self.rel = Path(guild_key) / NEWS_FILE

    def all(self) -> list[dict]:
        return self.store.read_jsonl(self.rel)

    @staticmethod
    def ident(row: dict) -> str:
        return (row.get("url") or "").strip().rstrip("/").lower() or "title:" + (row.get("title") or "").strip().lower()

    def add(self, item: dict, matched: list[str], posted_at: str, message_id: int | None = None, channel_id: int | None = None) -> dict | None:
        """Append one item unless its url is already there; one commit. Returns the row, or None for a duplicate."""
        row = {"url": item.get("url") or "", "title": item.get("title") or "", "description": item.get("description") or "",
               "posted_at": posted_at, "matched": list(matched), "message_id": message_id, "channel_id": channel_id, "ingested_at": now()}
        with self.store.lock:
            if any(self.ident(r) == self.ident(row) for r in self.all()):
                return None
            self.store.append_jsonl(self.rel, row)
            self.store.commit(f"{self.key}: news: {row['title'][:70]}")
        return row

    def since(self, since: str | None = None, limit: int = MAX_ITEMS_PER_CALL) -> list[dict]:
        rows = self.all()
        if since:
            cut = _aware(since)
            rows = [r for r in rows if _aware(r.get("ingested_at") or r.get("posted_at")) > cut]  # ingested: a backfilled old post is still new to the review
        return rows[-limit:]

    def urls(self) -> set[str]:
        return {self.ident(r) for r in self.all()}


def _aware(value) -> datetime:
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def ingest(reg, store: GitStore, message) -> list[dict]:
    """Filter a news-channel message and record what is relevant. Returns the rows added (duplicates skipped)."""
    sets = keyword_sets(reg)
    ns = NewsStore(store, reg.key)
    posted = getattr(message, "created_at", None)
    posted_at = posted.astimezone(timezone.utc).isoformat(timespec="seconds") if posted else now()
    added = []
    for item in embed_items(message):
        relevant, matched = match(sets, f"{item['title']}\n{item['description']}")
        if not relevant:
            continue
        row = ns.add(item, matched, posted_at, getattr(message, "id", None), getattr(getattr(message, "channel", None), "id", None))
        if row:
            added.append(row)
    return added


# ---------------------------------------------------------------- proposals

class Proposal(BaseModel):
    id: str
    title: str = Field(description="one line: the contradiction, e.g. 'Blood Pact stacks with Fortitude'")
    news: list[str] = Field(default_factory=list, description="urls of the news items cited (must be in the news list)")
    affects: str = Field(description="what it touches: 'guild setting raid_set barrow_deeps lockout_days' or 'profiles/forever/buffs.yaml: blood_pact.family'")
    kind: Literal["guild_setting", "profile", "needs_developer"]
    change: str = Field(description="the proposed change in words, for officers")
    edit: dict = Field(default_factory=dict, description="machine-readable: a ConfigOp (guild_setting) or {file, key, value} (profile)")
    evidence: str = Field(default="", description="the sentence(s) in the news that say so, quoted")
    confidence: Literal["low", "medium", "high"] = "medium"
    state: Literal["proposed", "approved", "dismissed", "applying", "applied", "failed", "reverted"] = "proposed"
    created_at: str = Field(default_factory=now)
    created_by: str = "review"
    decided_by: Optional[str] = None  # who approved or dismissed (display name)
    decided_by_id: Optional[int] = None
    decided_at: Optional[str] = None
    commit: Optional[str] = None
    note: Optional[str] = None
    status_note: Optional[str] = None  # the apply job's annotation ("waiting: … raids until …"); cleared by any state move
    history: list[dict] = Field(default_factory=list)  # {state, by, at, note}; an annotation is {state: None, by, at, note}
    channel_id: Optional[int] = None  # where the card is
    message_id: Optional[int] = None


class ProposalError(ValueError):
    pass


def stamp(value: str | None) -> str:
    """Discord renders `<t:unix:f>` in each reader's own locale and clock (never a typed or 24-hour time)."""
    if not value:
        return ""
    try:
        return f"<t:{int(datetime.fromisoformat(str(value)).timestamp())}:f>"
    except ValueError:
        return ""


def already_text(p: "Proposal", when=None) -> str:
    """'already approved by Jonny <t:…:f> — nothing to do': a repeated decision is told who decided first, never a silent
    success (design §3.4 X2). `when` formats the time per surface (Discord: `stamp`; the site: Registry.local12)."""
    verb = "approved" if p.state == "approved" else "dismissed"
    at = (when or stamp)(p.decided_at) if p.decided_at else ""
    return f"{p.decided_by or 'Someone'} already {verb} it" + (f" on {at}" if at else "") + ", so there is nothing to do."


class AlreadyDecided(ProposalError):
    """The proposal already is where the press would put it (two officers within a second; a stale card)."""

    def __init__(self, p: "Proposal"):
        self.p = p
        super().__init__(already_text(p))


def new_id() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(2)


class ProposalStore:
    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key
        self.dir = Path(guild_key) / PROPOSALS_DIR

    def path(self, pid: str) -> Path:
        if not re.fullmatch(r"[0-9]{6}-[0-9a-f]{4}", pid or ""):
            raise ProposalError(f"no proposal {pid!r}")
        return self.dir / f"{pid}.yaml"

    def get(self, pid: str) -> Proposal:
        p = self.store.root / self.path(pid)
        if not p.exists():
            raise ProposalError(f"no proposal {pid}")
        return Proposal.model_validate(yaml.safe_load(p.read_text()))

    def all(self, state: str | None = None) -> list[Proposal]:
        d = self.store.root / self.dir
        out = [Proposal.model_validate(yaml.safe_load(f.read_text())) for f in sorted(d.glob("*.yaml"))] if d.exists() else []
        return [p for p in out if not state or p.state in state.split(",")]

    def save(self, p: Proposal, message: str) -> Proposal:
        self.store.write_text(self.path(p.id), yaml.safe_dump(p.model_dump(), sort_keys=False, allow_unicode=True))
        self.store.commit(f"{self.key}: proposal {p.id}: {message}")
        for fn in list(PROPOSAL_LISTENERS):  # the web's `agents` topic: every save publishes, whichever surface moved it (§2.4)
            try:
                fn(self.key, p.id)
            except Exception:  # noqa: BLE001 — a listener must never break a save
                pass
        return p

    def create(self, reg, data: dict, by: str = "review") -> Proposal:
        """Validate and store a new proposal (state proposed). Cited news must be in the news list; the typed edit
        must fit its kind (`validate_edit`)."""
        p = Proposal.model_validate({**{k: v for k, v in data.items() if k not in ("id", "state", "history", "decided_by", "decided_by_id", "decided_at", "commit", "channel_id", "message_id")},
                                     "id": new_id(), "created_by": by})
        if not p.title.strip() or not p.change.strip():
            raise ProposalError("a proposal needs a title and the change in words")
        if not p.news:
            raise ProposalError("cite at least one news item (its url from list_news)")
        known = NewsStore(self.store, self.key).urls()
        unknown = [u for u in p.news if NewsStore.ident({"url": u}) not in known]
        if unknown:
            raise ProposalError(f"not in the news list: {', '.join(unknown[:3])}")
        validate_edit(reg, p)
        if p.kind == "profile":
            hit = dismissed_cover(self.store, reg, p.news)
            if hit:
                raise ProposalError(f"every news item it cites was cited by {hit}, which an officer dismissed — propose it again only "
                                    "when newer news says more (cite that item)")
        p.history.append({"state": "proposed", "by": by, "at": p.created_at, "note": None})
        return self.save(p, f"proposed: {p.title[:70]}")

    def move(self, pid: str, state: str, by: str, *, officer: bool, job: bool, by_id: int | None = None, commit: str | None = None,
             note: str | None = None, owner: bool | None = None, recover: bool = False) -> Proposal:
        """One state change — the one path for the card's buttons, the site, MCP and plain text (configops), all through
        NewsMixin.decide_proposal. `officer`: approve/dismiss allowed; `job`: the apply job's steps (the bearer WITH the
        X-Oibot-Job header) — a job never makes an officer's decision. Approving a `profile` proposal needs the OWNER
        (`owner`, else `Registry.is_owner_id(by_id)` through the Guilds index the store carries), even of a lone guild
        (design §3.4: it restarts the whole process); when several served guilds run this guild's game version, the
        owner of every one of them (K3). Dismissing stays with the proposing guild's officers. Moving to the state it
        already has raises AlreadyDecided (who decided, when). `recover`: the owner's "Mark as interrupted" — the one
        job move a person makes (RECOVER_MOVE, applying → failed), only as the owner; the route checks the job isn't running."""
        if state not in STATES:
            raise ProposalError(f"state is one of {', '.join(STATES)}")
        with self.store.lock:
            p = self.get(pid)
            move = (p.state, state)
            if p.state == state and state in ("approved", "dismissed"):
                raise AlreadyDecided(p)
            if move in OFFICER_MOVES:
                if job:
                    raise PermissionError(JOB_NOT_OFFICER)
                if not officer:
                    raise PermissionError("officers only")
            elif move in JOB_MOVES:
                if recover and move == RECOVER_MOVE:
                    if not (owner if owner is not None else owner_of(self.store, self.key, by_id)):
                        raise PermissionError("only the owner marks an apply run as interrupted")
                elif not job:
                    raise PermissionError("only the apply job reports that")
            else:
                raise ProposalError(f"{p.id} is {p.state}: it can't become {state}")
            if state == "approved" and p.kind == "needs_developer":
                raise ProposalError("this one needs a developer: the apply job can't do it (dismiss it once it's handled)")
            if state == "approved" and p.kind == "profile":
                why = shared_profile_refusal(getattr(self.store, "guilds", None), self.key, by_id)  # names the other guilds (K3)
                if why:
                    raise PermissionError(why)
                if not (owner if owner is not None else owner_of(self.store, self.key, by_id)):
                    raise PermissionError(PROFILE_OWNER_ONLY)
            if state == "approved" and owner_setting(p) and not (owner if owner is not None else owner_of(self.store, self.key, by_id)):
                raise PermissionError(SETTING_OWNER_ONLY)
            p.state = state
            p.status_note = None
            if state in ("approved", "dismissed"):
                p.decided_by, p.decided_by_id, p.decided_at = by, by_id, now()
            if commit:
                p.commit = commit
            if note is not None:
                p.note = note[:500] or None
            p.history.append({"state": state, "by": by, "at": now(), "note": (note or None) and note[:500]})
            return self.save(p, f"{state} (by {by})" + (f" @ {commit}" if commit else ""))

    def annotate(self, pid: str, note: str | None, by: str) -> tuple[Proposal, bool]:
        """The apply job's "waiting: Barrow Deeps raids until …" on the proposal, without moving it (design §3.4 X2's
        annotate verb): one history row and `status_note`, so the card and the Agents page stop promising "within 15
        minutes" for hours. The same note again writes nothing (the job polls every 15 minutes). None clears it.
        Returns (proposal, written)."""
        text = (note or "").strip()[:300] or None
        with self.store.lock:
            p = self.get(pid)
            if p.status_note == text:
                return p, False
            p.status_note = text
            p.history.append({"state": None, "by": by, "at": now(), "note": text})
            return self.save(p, f"note: {(text or 'cleared')[:70]}"), True

    def clear_card(self, p: Proposal) -> None:
        """The card was deleted in Discord: forget its ids so the next update posts a fresh one."""
        with self.store.lock:
            cur = self.get(p.id)
            cur.channel_id = cur.message_id = None
            p.channel_id = p.message_id = None
            self.save(cur, "card gone")

    def set_card(self, p: Proposal, channel_id: int, message_id: int) -> None:
        with self.store.lock:
            cur = self.get(p.id)  # the state may have moved while the card was being sent
            cur.channel_id, cur.message_id = channel_id, message_id
            p.channel_id, p.message_id = channel_id, message_id
            self.save(cur, "card posted")


def restart_waiting(status: dict, key: str, pid: str) -> str | None:
    """The apply job's committed-but-not-restarted profile edit for proposal `pid` of guild `key` (its status file's
    `restart_pending`), in words — None when nothing waits for it. While it waits the proposal reads `applying` with no
    job running, but it is NOT interrupted: the commit is on main and goes live at the next restart, so Mark as
    interrupted is neither offered nor accepted for it."""
    pending = (status or {}).get("restart_pending") or None
    if not pending or str(pending.get("id") or "") != pid or (pending.get("guild") or key) != key:
        return None
    return restart_pending_text(pending)


def restart_pending_text(pending: dict) -> str:
    """The apply job's pending restart in words (its status file's `restart_pending`): the proposal's line above and the
    Agents page's banner. `auto` False (the proposal was moved while it was applied) restarts the same way — the next
    quiet poll or the owner's Apply now — without moving the proposal."""
    commit = pending.get("commit") or "?"
    if pending.get("auto"):
        return f"Committed {commit}; only the restart waits (raid guard or a busy bot) — it restarts at the next quiet poll."
    return (f"Committed {commit}; the bot was not restarted ({pending.get('why') or 'held'}) — it restarts at the next quiet poll, "
            "or press Apply now.")


def owner_setting(p) -> bool:
    """A guild-setting proposal whose config op is the owner's (configops.OWNER_OPS: raid_set, aura_reset, …) — an
    officer may not approve it, exactly as they may not write it on any other surface; the apply job re-checks the
    approver before it applies (an officer could otherwise write one and approve it themselves)."""
    if getattr(p, "kind", None) != "guild_setting":
        return False
    from .configops import OWNER_OPS

    return str((getattr(p, "edit", None) or {}).get("op") or "") in OWNER_OPS


def needs_owner(p) -> bool:
    """Approving `p` is the owner's: a game-data (profile) edit or an owner-only guild setting."""
    return getattr(p, "kind", None) == "profile" or owner_setting(p)


def owner_of(store: GitStore, key: str, by_id: int | None) -> bool:
    """Whether `by_id` owns guild `key` (its owner or a host superuser), through the Guilds index attached to the store;
    False without one (an offline store: the caller passes `owner=` itself)."""
    guilds = getattr(store, "guilds", None)
    reg = getattr(guilds, "by_key", {}).get(key) if guilds is not None else None
    return bool(reg is not None and by_id is not None and reg.is_owner_id(by_id))


def same_profile_guilds(guilds, reg) -> list:
    """Every served guild (Registry) on the game profile of `reg` (a Registry or a guild key) when more than one guild
    uses it, else [] — the guilds a `profile` proposal's edit is shared by (K3)."""
    ordered = getattr(guilds, "ordered", None)
    regs = ordered() if ordered else []
    key = reg if isinstance(reg, str) else reg.key
    mine = next((r for r in regs if r.key == key), None if isinstance(reg, str) else reg)
    if mine is None:
        return []
    same = [r for r in regs if r.config.game_profile == mine.config.game_profile]
    return same if len(same) > 1 else []


def shared_profile_owners(guilds, reg) -> list[tuple[str, int | None]]:
    """(name, owner id) of every guild on `reg`'s game profile when more than one guild uses it, else [] (K3)."""
    return [(str(r.config.name), r.config.owner_discord_id) for r in same_profile_guilds(guilds, reg)]


def shared_profile_others(guilds, reg) -> list[str]:
    """The names of the OTHER guilds a profile edit of `reg`'s guild is shared with ([] = not shared)."""
    key = reg if isinstance(reg, str) else reg.key
    return [str(r.config.name) for r in same_profile_guilds(guilds, reg) if r.key != key]


def shared_profile_refusal(guilds, key: str, by_id: int | None) -> str | None:
    """Why `by_id` may not approve a profile proposal of guild `key` (K3), or None: when other guilds run the same game
    version, the approver must own every guild on it. The message names only the OTHER guilds (the proposing guild's
    officers already know their own)."""
    same = same_profile_guilds(guilds, key)
    if not same or all(r.is_owner_id(by_id) for r in same):
        return None
    others = shared_profile_others(guilds, key)
    return (f"This edits game data that {join_words(others)} also use{'s' if len(others) == 1 else ''}. "
            "The owner of every guild on this game version approves it.")


def profile_approval_refusal(guilds, reg, p) -> str | None:
    """At the apply job's approved → applying gate: why a PROFILE proposal's approval doesn't stand (it was not made by
    the owner — an officer's approval from before the owner-only rule, or one who doesn't own every guild on the game
    version, K3), or None. Re-authorized by privilege at the apply, as owner_setting proposals are."""
    if getattr(p, "kind", None) != "profile":
        return None
    by_id = getattr(p, "decided_by_id", None)
    if by_id is not None and reg.is_owner_id(by_id) and shared_profile_refusal(guilds, reg.key, by_id) is None:
        return None
    return f"{PROFILE_OWNER_ONLY} It was approved by {getattr(p, 'decided_by', None) or 'someone else'}, so the owner approves it again to apply it."


def dismissed_cover(store: GitStore, reg, urls) -> str | None:
    """The dismissed game-data proposal(s) whose cited news covers ALL of `urls` ("<id>" here, "<guild name>'s <id>"
    for another guild on the same game profile — a profile edit is shared), or None. A new profile proposal citing only
    news an officer already said no to is refused in code (design §3.4 OFF-14), not only by the review's prompt."""
    want = {NewsStore.ident({"url": u}) for u in (urls or [])}
    if not want:
        return None
    guilds = getattr(store, "guilds", None)
    ordered = getattr(guilds, "ordered", None)
    regs = [reg] + [o for o in (ordered() if ordered else []) if o.key != reg.key and o.config.game_profile == reg.config.game_profile]
    seen: set[str] = set()
    ids: list[str] = []
    for r in regs:
        for d in ProposalStore(store, r.key).all("dismissed"):
            if d.kind != "profile":
                continue
            cited = {NewsStore.ident({"url": u}) for u in d.news} & want
            if cited:
                ids.append(d.id if r.key == reg.key else f"{r.config.name}'s {d.id}")
                seen |= cited
    if want <= seen:
        return ", ".join(ids[:3])
    return None


def duplicate_profile_proposal(guilds, reg, data: dict) -> str | None:
    """`<guild key>/<proposal id>` of another guild's open (proposed/approved) profile proposal citing one of the same
    news items, when that guild runs the same game profile: a profile edit is shared, so one proposal is enough."""
    if str(data.get("kind") or "") != "profile":
        return None
    cited = {NewsStore.ident({"url": u}) for u in (data.get("news") or [])}
    ordered = getattr(guilds, "ordered", None)
    for other in (ordered() if ordered else []):
        if other.key == reg.key or other.config.game_profile != reg.config.game_profile:
            continue
        for p in ProposalStore(guilds.store, other.key).all("proposed,approved"):
            if p.kind == "profile" and cited & {NewsStore.ident({"url": u}) for u in p.news}:
                return f"{other.key}/{p.id}"
    return None


def decision_refusal(store: GitStore, reg, pid: str, state: str, by_id: int | None, *, owner: bool | None = None, when=None) -> str | None:
    """Why an officer's Approve / Dismiss of `pid` would be refused right now, or None — the same checks `move` makes
    for an officer, read without writing, so the confirm screen refuses at once instead of after a press. `move` runs
    them again at the write (the state may move in between)."""
    ps = ProposalStore(store, reg.key)
    try:
        p = ps.get(pid)
    except ProposalError as e:
        return str(e)
    if p.state == state and state in ("approved", "dismissed"):
        return already_text(p, when)
    if (p.state, state) not in OFFICER_MOVES:
        return f"This proposal is {p.state}, so it can't be {state}."
    if state == "approved" and p.kind == "needs_developer":
        return "This one needs a developer, so the apply job can't do it. Dismiss it once it's handled."
    if state == "approved" and p.kind == "profile":
        why = shared_profile_refusal(getattr(store, "guilds", None), reg.key, by_id)
        if why:
            return why
        if not (owner if owner is not None else (by_id is not None and reg.is_owner_id(by_id))):
            return PROFILE_OWNER_ONLY
    if state == "approved" and owner_setting(p) and not (owner if owner is not None else (by_id is not None and reg.is_owner_id(by_id))):
        return SETTING_OWNER_ONLY
    return None


def served_names(guilds, reg) -> list[str]:
    """Every server the process serves (a profile approval restarts them all — one LaunchAgent), `reg`'s first."""
    ordered = getattr(guilds, "ordered", None)
    regs = list(ordered()) if ordered else []
    return [str(reg.config.name)] + [str(r.config.name) for r in regs if r.key != reg.key]


def edit_words(reg, p: Proposal) -> str:
    """A guild-setting proposal's typed edit as the diff line (configops.describe), else the change in words."""
    if p.kind == "guild_setting":
        try:
            from .configops import ConfigOp, describe

            return describe(reg, ConfigOp.model_validate(p.edit))
        except Exception:  # noqa: BLE001 — an edit the config no longer reads: the words the review wrote
            pass
    return p.change


def consequence(p: Proposal, reg, state: str, guilds=None, *, busy: dict[str, dict] | None = None, next_run: str | None = None) -> list[str]:
    """What Approve / Dismiss does, in words, from the proposal's kind and state (dual interfaces §2.3; D34: Dismiss
    gets the same screen). The Discord `decide` screen, the site's confirm and GET /api/proposals print these lines;
    none composes its own. `busy`: {guild key: guild_busy(...)} for every served guild (a profile approval names each
    one raiding: the job waits, D33); `next_run`: the apply job's next poll in words ('Tue 7:45 PM')."""
    when = f"at its next run, {next_run}" if next_run else "at its next run, within 15 minutes"
    if state == "approved":
        if p.kind == "guild_setting":
            return [f"The apply job makes this change {when}.", "The bot doesn't restart for it."]
        at = f"At the apply job's next run, {next_run}" if next_run else "At the apply job's next run, within 15 minutes"
        if p.kind == "profile":
            version = getattr(reg.base_profile, "name", None) or reg.config.game_profile
            served = served_names(guilds, reg)
            lines = [f"{at}, Claude edits the game data for {version}.",
                     f"The tests run, then the bot restarts for about two minutes for {join_words(served) if len(served) <= 3 else f'all {len(served)} servers it serves'}."]
            ordered = getattr(guilds, "ordered", None)
            busy_names = [r.config.name for r in (list(ordered()) if ordered else [reg]) if ((busy or {}).get(r.key) or {}).get("raiding")]
            if busy_names:
                lines.append(f"⚠ {join_words(busy_names)} {'has' if len(busy_names) == 1 else 'have'} a run right now, so the job waits until it's over.")
            return lines
        return ["This one needs a developer, so the apply job can't do it."]
    if state == "interrupted":
        return ["This marks it failed, because the apply run that held it is gone.", "Nothing is committed, restarted or reverted by this.",
                "Approve it again to retry."]
    if state == "dismissed":
        lines = ["Nothing changes, and the review won't propose it again unless newer news says more."]
        if p.kind == "profile":
            lines = ["Nothing changes.", "A game-data proposal citing only this news is refused from now on, unless newer news says more."]
        if p.state == "approved":
            lines.insert(0, "This withdraws the approval before the job picks it up.")
        return lines
    return []


def guild_busy(reg, runs, loot_runs=(), now: datetime | None = None) -> dict:
    """D33's raid guard for one guild: {raiding, until (ISO or None), until_label, why} from its live runs (test runs
    never count) — a LOCKED run from BUSY_LOCKED_HOURS before its start until it closes (rc.run_close_at), an OPEN run
    from BUSY_OPEN_HOURS before its start — and every live loot council on its own (`loot_runs`: the live sessions, or
    their run keys): from its run's 4-hour window on, a council counts whatever its run's state and however far past
    the scheduled close it runs (D10
    ends one only past close + 30 min idle), so a restart never cuts into one; a test run's council never counts.
    Computed on demand by the bot (GET /api/guilds carries it per guild); the apply job defers a restart while any
    guild is busy."""
    from . import raidcycle as rc

    now = now or datetime.now(timezone.utc)
    by_key = {ev.key: ev for ev in runs}
    hits: list[tuple[datetime, str]] = []
    councils: set[str] = set()
    for s in loot_runs or ():
        key = s if isinstance(s, str) else getattr(s, "run_key", None)
        if not key or key in councils:
            continue
        ev = by_key.get(key)
        if getattr(s, "test", False) or getattr(ev, "test", False):
            continue
        if ev is not None and now < ev.start - timedelta(hours=BUSY_LOCKED_HOURS):
            continue  # a council opened days early is not raid time yet (the run's own window starts 4 h before)
        councils.add(key)
        inst = getattr(ev, "instance", None) or getattr(s, "instance", None)
        name = reg.raid_def(inst).get("name", inst) if inst else (getattr(ev, "team", None) or key)
        until = now + timedelta(minutes=BUSY_LOOT_MIN)
        if ev is not None:
            until = max(until, rc.run_close_at(reg, ev))
        hits.append((until, f"{name}: the loot council is open"))
    for ev in runs:
        if getattr(ev, "test", False) or ev.state not in ("open", "locked") or ev.key in councils:
            continue
        start, end = ev.start, rc.run_close_at(reg, ev)
        name = reg.raid_def(ev.instance).get("name", ev.instance) if ev.instance else ev.team
        if ev.state == "locked" and start - timedelta(hours=BUSY_LOCKED_HOURS) <= now <= end:
            hits.append((end, f"{name} {reg.local12(start)} is locked"))
        elif ev.state == "open" and start - timedelta(hours=BUSY_OPEN_HOURS) <= now <= end:
            hits.append((end, f"{name} {reg.local12(start)} starts within the hour"))
    if not hits:
        return {"raiding": False, "until": None, "until_label": "", "why": ""}
    until = max(t for t, _ in hits)
    return {"raiding": True, "until": until.astimezone(timezone.utc).isoformat(timespec="seconds"), "until_label": reg.local12(until),
            "why": "; ".join(w for _, w in hits)}


def validate_edit(reg, p: Proposal) -> None:
    """The typed edit must match the kind: a whitelisted config op that `configops.describe` understands, or a
    profile file of this game version. Raises ProposalError."""
    if p.kind == "guild_setting":
        from .configops import ConfigOp, describe

        try:
            op = ConfigOp.model_validate(p.edit)
        except Exception as e:  # noqa: BLE001
            raise ProposalError(f"edit is not a config op: {e}")
        if op.op not in SETTING_OPS:
            raise ProposalError(f"a guild setting is one of {', '.join(SETTING_OPS)} (got {op.op})")
        try:
            describe(reg, op)
        except Exception as e:  # noqa: BLE001
            raise ProposalError(f"edit: {e}")
    elif p.kind == "profile":
        f = str(p.edit.get("file") or "")
        root = Path(reg.base_profile.root)
        want = (root.parent.parent / f).resolve() if f.startswith("profiles/") else (root / f).resolve()
        if not f.endswith(".yaml") or root.resolve() not in want.parents or not want.exists():
            raise ProposalError(f"edit.file must be a YAML file under profiles/{root.name}/ (got {f or 'nothing'})")
        if not str(p.edit.get("key") or "").strip():
            raise ProposalError("edit.key: which entry and field (e.g. blood_pact.family)")
