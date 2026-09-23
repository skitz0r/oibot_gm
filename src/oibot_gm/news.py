"""News review (design.md §5.26): what the #news webhook posts, filtered to what this guild plays, and the
proposals a daily review makes from it.

Ingest is code: the bot keyword-filters each embed (title + description only — the linked article is never
fetched, Wowhead is never scraped) and appends matches to `<guild>/news.jsonl`, deduped by url. The review job
(scripts/agents) reads that list, compares it with the effective profile and guild settings, and posts
*proposals*: a cited contradiction, the change in words and as a typed edit. Officers approve or dismiss; the
local apply job carries out approved ones. A proposal is data in `<guild>/proposals/<id>.yaml`, one file each,
every state change a commit (git log = the audit trail).

States: proposed → approved | dismissed → applying → applied | failed | reverted. Approving only flips the state
and records who; nothing is applied by the bot on a button press."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .store import GitStore

NEWS_FILE = "news.jsonl"
PROPOSALS_DIR = "proposals"
KINDS = ("guild_setting", "profile", "needs_developer")
STATES = ("proposed", "approved", "dismissed", "applying", "applied", "failed", "reverted")
CONFIDENCE = ("low", "medium", "high")
# who may move a proposal where: officers decide (web, Discord), the apply job (the owner's token) reports progress
OFFICER_MOVES = {("proposed", "approved"), ("proposed", "dismissed"), ("approved", "dismissed"), ("failed", "approved"), ("failed", "dismissed")}
JOB_MOVES = {("approved", "applying"), ("approved", "failed"), ("applying", "applied"), ("applying", "failed"), ("applying", "reverted"), ("applied", "reverted")}
# a guild setting is one of these config ops (configops.ConfigOp fields); anything else is a profile edit or a developer's job
SETTING_OPS = ("raid_set", "raid_reset", "aura_set", "family_set", "aura_reset", "comp_target", "comp_target_clear", "comp_groups")
MAX_ITEMS_PER_CALL = 200


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
    history: list[dict] = Field(default_factory=list)  # {state, by, at, note}
    channel_id: Optional[int] = None  # where the card is
    message_id: Optional[int] = None


class ProposalError(ValueError):
    pass


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
        p.history.append({"state": "proposed", "by": by, "at": p.created_at, "note": None})
        return self.save(p, f"proposed: {p.title[:70]}")

    def move(self, pid: str, state: str, by: str, *, officer: bool, job: bool, by_id: int | None = None, commit: str | None = None, note: str | None = None) -> Proposal:
        """One state change. `officer`: approve/dismiss allowed; `job`: the apply job's steps (the owner's token)."""
        if state not in STATES:
            raise ProposalError(f"state is one of {', '.join(STATES)}")
        with self.store.lock:
            p = self.get(pid)
            move = (p.state, state)
            if move in OFFICER_MOVES:
                if not officer:
                    raise PermissionError("officers only")
            elif move in JOB_MOVES:
                if not job:
                    raise PermissionError("only the apply job reports that")
            else:
                raise ProposalError(f"{p.id} is {p.state}: it can't become {state}")
            if state == "approved" and p.kind == "needs_developer":
                raise ProposalError("this one needs a developer: the apply job can't do it (dismiss it once it's handled)")
            p.state = state
            if state in ("approved", "dismissed"):
                p.decided_by, p.decided_by_id, p.decided_at = by, by_id, now()
            if commit:
                p.commit = commit
            if note is not None:
                p.note = note[:500] or None
            p.history.append({"state": state, "by": by, "at": now(), "note": (note or None) and note[:500]})
            return self.save(p, f"{state} (by {by})" + (f" @ {commit}" if commit else ""))

    def set_card(self, p: Proposal, channel_id: int, message_id: int) -> None:
        with self.store.lock:
            cur = self.get(p.id)  # the state may have moved while the card was being sent
            cur.channel_id, cur.message_id = channel_id, message_id
            p.channel_id, p.message_id = channel_id, message_id
            self.save(cur, "card posted")


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
