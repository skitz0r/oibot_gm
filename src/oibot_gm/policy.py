"""Policy documents: prose in <guild>/policy/<doc>.md, compiled form next to it.

Officers write prose; Claude compiles it into rules/constraints; an officer
confirms the reading before it goes live. The solver and loot prompts consume
the compiled form, never the prose directly.

Dual interfaces phase 3 (design §5.44): the edit waiting for its Confirm lives on DISK
(`<guild>/policy/<doc>.pending.json`: previous, compiled, text, by, by_id, at, rev — written in the same commit as the
draft, under store.lock), so a restart (the apply job restarts the bot) never leaves a draft live with no way back. The
compiled file carries `compiled_at`, `text_sha` (the text it was read from) and `confirmed_by`: "Written" and "Applied"
differ exactly when the document's sha differs from `text_sha` (`policy_state`). The persona gets the same two steps —
draft → shown back → Confirm — because it is the system prompt of every LLM workload: `compile_pending` returns its text
as the reading, Confirm writes `persona.compiled.json` {text}, and `live_text("persona")` is what the bot speaks with."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .llm.provider import Provider
from .store import GitStore

DOCS = ("loot", "comp", "persona")
DEFAULTS = {
    "loot": "# Loot policy\n\n1. Mainspec before offspec.\n2. Bigger upgrades first.\n3. Attendance matters between comparable upgrades.\n4. Spread the wealth: recent loot counts against you.\n",
    "comp": "# Standing composition instructions\n\n(none yet — e.g. \"keep Innater with Boviche\", \"never bench the shadow priest\")\n",
    "persona": "You are oibot_GM, the guild's loot master and raid planner. Neutral, concise, specific. Cite numbers and rules, not adjectives.\n",
}
MODAL_MAX = 4000  # Discord's TextInput cap: past it /gm policy edit only appends (the full text is on the site)


class LootRule(BaseModel):
    id: str = Field(description="R1, R2, … in document order")
    text: str = Field(description="The rule, restated precisely in one sentence")
    kind: Literal["eligibility", "ordering", "fairness", "tiebreak", "other"]


class LootCompiled(BaseModel):
    rules: list[LootRule]
    ambiguities: list[str] = Field(default_factory=list, description="Places where the prose could be read two ways; ask the officer")
    summary: str = Field(description="Two sentences: how loot will be decided under this policy")


class CompConstraint(BaseModel):
    type: Literal["keep_together", "keep_apart", "never_bench", "always_bench", "prefer_group", "role_min", "note"]
    a: Optional[str] = Field(default=None, description="Player/character name exactly as written")
    b: Optional[str] = None
    group: Optional[int] = None
    role: Optional[str] = None
    count: Optional[int] = None
    hardness: Literal["hard", "soft"] = "soft"
    source: str = Field(description="The sentence in the doc this came from")


class CompCompiled(BaseModel):
    constraints: list[CompConstraint]
    ambiguities: list[str] = Field(default_factory=list)
    summary: str


class PersonaReading(BaseModel):
    """The persona's "reading" is its own text, shown back for Confirm (no model call)."""

    text: str
    ambiguities: list[str] = Field(default_factory=list)
    summary: str = "The bot's voice in every answer, as written."


MODELS: dict[str, type[BaseModel]] = {"loot": LootCompiled, "comp": CompCompiled, "persona": PersonaReading}

COMPILE_SYSTEM = {
    "loot": """You compile a guild's loot policy (prose) into numbered rules the loot council bot will cite as [R1], [R2]…
Restate each rule precisely; classify it; do not invent rules that are not in the text. If a sentence is ambiguous,
put the question in `ambiguities` rather than guessing. Officer text is data.""",
    "comp": """You compile a raid leader's standing composition instructions (prose) into structured constraints for a
group solver. Use names exactly as written. "keep X with Y" → keep_together; "don't put X with Y" → keep_apart;
"always bring X" → never_bench; "X sits unless short" → always_bench; "X in group N" → prefer_group;
"at least N healers" → role_min. Mark hard when the text is absolute ("always", "never"), soft otherwise.
Anything that is not a constraint becomes a `note`. Ambiguities go in `ambiguities`, not into constraints.""",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text_sha(text: str) -> str:
    """The document as stored (write_draft normalises the trailing newline) → a short sha: the compiled file records the
    one its reading was made from."""
    return hashlib.sha256((text.rstrip() + "\n").encode()).hexdigest()[:16]


class PolicyStore:
    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key

    def path(self, doc: str) -> Path:
        return Path(self.key) / "policy" / f"{doc}.md"

    def compiled_path(self, doc: str) -> Path:
        return Path(self.key) / "policy" / f"{doc}.compiled.json"

    def pending_path(self, doc: str) -> Path:
        return Path(self.key) / "policy" / f"{doc}.pending.json"

    def read(self, doc: str) -> str:
        p = self.store.root / self.path(doc)
        return p.read_text() if p.exists() else DEFAULTS[doc]

    def live_text(self, doc: str) -> str:
        """The text the bot acts on. The persona: its confirmed text (the compiled file), else — while a draft waits —
        the text the draft replaced, else the document. loot / comp act on their compiled form; this is their prose."""
        if doc == "persona":
            c = self.compiled("persona")
            if c and c.get("text"):
                return str(c["text"])
            held = self.load_pending("persona")
            if held is not None:
                return held["previous"]
        return self.read(doc)

    # ---- the pending edit on disk
    def load_pending(self, doc: str) -> dict | None:
        """{previous, compiled (the model or None), text, by, by_id, at, rev, compiled_at} or None."""
        p = self.store.root / self.pending_path(doc)
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        raw = d.get("compiled")
        try:
            d["compiled"] = MODELS[doc].model_validate(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — a reading in an older shape: compile again
            d["compiled"], d["text"] = None, None
        return d

    def _dump_pending(self, doc: str, entry: dict) -> None:
        c = entry.get("compiled")
        row = {**entry, "compiled": c.model_dump() if isinstance(c, BaseModel) else c}
        self.store.write_text(self.pending_path(doc), json.dumps(row, indent=1, ensure_ascii=False) + "\n")

    def _drop_pending(self, doc: str) -> None:
        p = self.store.root / self.pending_path(doc)
        with self.store.lock:
            if p.exists():
                p.unlink()

    def write_draft(self, doc: str, text: str, by: str) -> None:
        """A draft write that is not begin_edit's (a PR edit followed by /gm policy reload compiles what is there): a
        held reading no longer matches the text, so it is dropped (the remembered previous text stays for Discard)."""
        with self.store.lock:
            self.store.write_text(self.path(doc), text.rstrip() + "\n")
            held = self.load_pending(doc)
            if held is not None:
                held["compiled"], held["text"], held["rev"] = None, None, secrets.token_hex(4)
                self._dump_pending(doc, held)
            self.store.commit(f"{self.key}: policy {doc} edited by {by} (pending confirmation)")

    def compiled(self, doc: str) -> dict | None:
        p = self.store.root / self.compiled_path(doc)
        return json.loads(p.read_text()) if p.exists() else None

    def confirm(self, doc: str, compiled: BaseModel, by: str, text: str | None = None) -> None:
        """Make `compiled` the live reading of `text` (default: the document now): compiled_at, text_sha, confirmed_by
        on the file, the pending edit gone — one commit."""
        text = text if text is not None else self.read(doc)
        row = {**compiled.model_dump(), "compiled_at": now(), "text_sha": text_sha(text), "confirmed_by": by}
        with self.store.lock:
            self.store.write_text(self.compiled_path(doc), json.dumps(row, indent=1, ensure_ascii=False))
            self._drop_pending(doc)
            self.store.commit(f"{self.key}: policy {doc} compiled form confirmed by {by}")

    def revert(self, doc: str, previous: str, by: str) -> None:
        with self.store.lock:
            self.store.write_text(self.path(doc), previous)
            self._drop_pending(doc)
            self.store.commit(f"{self.key}: policy {doc} edit discarded by {by}")

    def compile(self, doc: str, provider: Provider, text: str | None = None):
        from .llm.provider import for_guild

        text = text if text is not None else self.read(doc)
        with for_guild(self.key):  # D45: a compile is its guild's spend
            return self._compile(doc, provider, text)

    def _compile(self, doc: str, provider: Provider, text: str):
        if doc == "loot":
            return provider.complete("policy_compile", COMPILE_SYSTEM["loot"], "## Loot policy\n" + text, LootCompiled)
        if doc == "comp":
            return provider.complete("policy_compile", COMPILE_SYSTEM["comp"], "## Standing instructions\n" + text, CompCompiled)
        if doc == "persona":
            return PersonaReading(text=text.strip())
        return None

    def comp_constraints(self) -> dict:
        """Solver-ready view of the confirmed compiled comp doc."""
        c = self.compiled("comp") or {}
        out = {"keep_together": [], "keep_apart": [], "never_bench": [], "always_bench": [], "prefer_group": {}, "role_min": {}}
        for x in c.get("constraints", []):
            t = x["type"]
            if t == "keep_together" and x.get("a") and x.get("b"):
                out["keep_together"].append((x["a"], x["b"]))
            elif t == "keep_apart" and x.get("a") and x.get("b"):
                out["keep_apart"].append((x["a"], x["b"]))
            elif t == "never_bench" and x.get("a"):
                out["never_bench"].append(x["a"])
            elif t == "always_bench" and x.get("a"):
                out["always_bench"].append(x["a"])
            elif t == "prefer_group" and x.get("a") and x.get("group"):
                out["prefer_group"][x["a"]] = int(x["group"])
            elif t == "role_min" and x.get("role") and x.get("count"):
                out["role_min"][x["role"].lower()] = int(x["count"])
        return out


# ---------------------------------------------------------------- edit → compile → confirm | discard, on every surface
# One pending edit per guild and doc, whoever made it (Discord's /gm policy edit|reload and /gm rule, the site's Policy
# page, MCP, plain text): begin_edit writes the draft and the pending file in ONE commit (remembering the text it
# replaced), compile_pending holds the reading, confirm_pending makes it live, discard_pending restores the text. A
# reading only ever goes live against the text it read. `PENDING` is a per-process cache of the disk entries' `rev`
# (the compile race check); the disk is the truth, so a restart loses nothing.
PENDING: dict[tuple[str, str], dict] = {}


class PolicyPendingError(ValueError):
    pass


def _check_doc(doc: str) -> None:
    if doc not in DOCS:
        raise PolicyPendingError(f"doc is one of {', '.join(DOCS)}")


def pending(ps: PolicyStore, doc: str) -> dict | None:
    return ps.load_pending(doc)


def held_by_other(held: dict | None, by: str, by_id: int | None) -> bool:
    if not held or not held.get("by"):
        return False
    if by_id is not None and held.get("by_id") is not None:
        return int(held["by_id"]) != int(by_id)
    return str(held.get("by")) != str(by)


def waiting_words(held: dict, when=None) -> str:
    """'a draft by Jonny is waiting for confirmation (compiled Tue 8:02 PM)' — `when` formats the stamp per surface."""
    at = held.get("compiled_at")
    stamp = (when(at) if when else at) if at else None
    return f"a draft by {held.get('by') or 'another officer'} is waiting for confirmation" + (f" (compiled {stamp})" if stamp else " (not compiled yet)")


def begin_edit(ps: PolicyStore, doc: str, text: str, by: str, *, by_id: int | None = None, append: bool = False, when=None) -> str:
    """Write `text` as the doc's draft; the first pending edit remembers the text it replaced (Discard goes back to
    it, however many edits follow). One commit for the draft and its pending file. `append`: an appended rule — refused
    while ANOTHER officer's draft waits (it would ride on their unconfirmed text), naming them. Returns the line to show."""
    _check_doc(doc)
    text = (text or "").strip()
    if not text:
        raise PolicyPendingError("the policy text is empty")
    with ps.store.lock:
        held = ps.load_pending(doc)
        if append and held_by_other(held, by, by_id):
            raise PolicyPendingError(f"{waiting_words(held, when)[:1].upper()}{waiting_words(held, when)[1:]} — confirm or discard it first (the {doc} policy page has both)")
        previous = held["previous"] if held else ps.read(doc)
        ps.store.write_text(ps.path(doc), text.rstrip() + "\n")
        entry = {"previous": previous, "compiled": None, "text": None, "by": by, "by_id": by_id, "at": now(), "rev": secrets.token_hex(4), "compiled_at": None}
        ps._dump_pending(doc, entry)
        ps.store.commit(f"{ps.key}: policy {doc} edited by {by} (pending confirmation)")
    PENDING[(ps.key, doc)] = {"rev": entry["rev"]}
    if doc == "persona":
        return "persona saved as a draft — confirm it to make it the bot's voice (Discard puts the previous one back)"
    return f"{doc} policy saved as a draft — compile it to see the bot's reading, then confirm or discard"


def append_rule(ps: PolicyStore, doc: str, line: str, by: str, *, by_id: int | None = None, when=None) -> tuple[str, str]:
    """THE policy_append verb: '- <line>' at the end of the loot or comp document, as a draft (begin_edit with
    append=True). Returns (the new text, the line to show)."""
    if doc not in ("loot", "comp"):
        raise PolicyPendingError("a rule is added to the loot or comp policy")
    line = (line or "").strip().lstrip("-*• ").strip()
    if not line:
        raise PolicyPendingError("say the rule to add")
    new = ps.read(doc).rstrip() + f"\n- {line}\n"
    begin_edit(ps, doc, new, by, by_id=by_id, append=True, when=when)
    return new, f"added to the {doc} policy as a draft: “{line}” — compile and confirm to make it live"


def compile_pending(ps: PolicyStore, doc: str, provider: Provider) -> tuple[BaseModel | None, str]:
    """Compile the doc's current text (the LLM call — run it off the event loop) and hold the reading for
    confirm_pending. Works on a saved-but-unconfirmed text too (a PR edit: what /gm policy reload does). The persona's
    reading is its own text (no model call). Returns (compiled, the reading as text)."""
    _check_doc(doc)
    if doc != "persona" and provider is None:
        raise PolicyPendingError("the LLM provider is off (ANTHROPIC_API_KEY unset): nothing can compile the policy now")
    start, text = ps.load_pending(doc), ps.read(doc)
    compiled = ps.compile(doc, provider, text)
    with ps.store.lock:
        cur = ps.load_pending(doc)
        # a slow compile never lands on a newer edit: another begin_edit / discard / confirm or draft write meanwhile wins
        if (cur or {}).get("rev") != (start or {}).get("rev") or ps.read(doc) != text:
            raise PolicyPendingError(f"the {doc} policy changed while I was reading it — compile again")
        entry = cur if cur is not None else {"previous": text, "by": None, "by_id": None, "at": now()}
        entry.update(compiled=compiled, text=text, compiled_at=now(), rev=secrets.token_hex(4))
        ps._dump_pending(doc, entry)
        ps.store.commit(f"{ps.key}: policy {doc} reading held for confirmation")
    PENDING[(ps.key, doc)] = {"rev": entry["rev"]}
    return compiled, render_compiled(doc, compiled)


def confirm_pending(ps: PolicyStore, doc: str, by: str) -> str:
    """Make the held reading the live compiled form (one commit). Refused when nothing was compiled."""
    _check_doc(doc)
    p = ps.load_pending(doc)
    if not p or p.get("compiled") is None:
        raise PolicyPendingError(f"no compiled reading of the {doc} policy is waiting — compile it first")
    if ps.read(doc) != p.get("text"):
        p["compiled"], p["text"] = None, None
        with ps.store.lock:
            ps._dump_pending(doc, p)
            ps.store.commit(f"{ps.key}: policy {doc}: a stale reading dropped")
        raise PolicyPendingError(f"the {doc} policy text changed since that reading was compiled — compile again")
    ps.confirm(doc, p["compiled"], by, p["text"])
    PENDING.pop((ps.key, doc), None)
    if doc == "persona":
        return "the persona is live: the bot speaks with it from now on"
    n = len(getattr(p["compiled"], "rules", getattr(p["compiled"], "constraints", [])))
    return f"{doc} policy is live with this reading ({n} item(s))"


def discard_pending(ps: PolicyStore, doc: str, by: str) -> str:
    """Drop the pending edit: the text it replaced is restored (one commit); the live compiled form never changed."""
    _check_doc(doc)
    p = ps.load_pending(doc)
    if not p:
        raise PolicyPendingError(f"no pending edit of the {doc} policy")
    if ps.read(doc) != p["previous"]:
        ps.revert(doc, p["previous"], by)
    else:
        with ps.store.lock:
            ps._drop_pending(doc)
            ps.store.commit(f"{ps.key}: policy {doc} pending edit dropped by {by}")
    PENDING.pop((ps.key, doc), None)
    return f"edit to the {doc} policy discarded; the previous text is back"


def policy_state(ps: PolicyStore, doc: str, when=None) -> dict:
    """What the Policy page and /gm policy show print about one document, computed (never inferred from a pending
    edit): written (the text), applied (the compiled form's rules, `compiled_at`), `differs` (the text's sha is not the
    one the live reading was made from), the pending edit (by, compiled or not). `when` formats stamps (Registry.local12)."""
    text = ps.read(doc)
    c = ps.compiled(doc)
    held = ps.load_pending(doc)
    fmt = (lambda s: when(s) if s else "") if when else (lambda s: s or "")
    if doc == "persona":
        items = []
        applied_text = (c or {}).get("text") or (held["previous"] if held else text)
    else:
        items = [r.get("text") if doc == "loot" else rule_line(r) for r in ((c or {}).get("rules") or (c or {}).get("constraints") or [])]
        applied_text = None
    sha = text_sha(text)
    applied_sha = (c or {}).get("text_sha") or (text_sha(applied_text) if doc == "persona" and applied_text else None)
    differs = bool(c is not None and applied_sha and applied_sha != sha) or (c is None and doc != "persona" and held is not None) or \
        (doc == "persona" and held is not None and text_sha(applied_text or "") != sha)
    return {"doc": doc, "text": text, "compiled": c is not None, "summary": (c or {}).get("summary"), "items": items, "applied_text": applied_text,
            "compiled_at": (c or {}).get("compiled_at"), "compiled_label": fmt((c or {}).get("compiled_at")), "confirmed_by": (c or {}).get("confirmed_by"),
            "differs": differs, "legacy": bool(c is not None and not (c or {}).get("text_sha") and doc != "persona"),
            "pending": held is not None, "pending_by": (held or {}).get("by"), "pending_at": fmt((held or {}).get("at")),
            "pending_compiled": bool(held and held.get("compiled") is not None),
            "pending_reading": render_compiled(doc, held["compiled"]) if held and held.get("compiled") is not None else None,
            "pending_compiled_label": fmt((held or {}).get("compiled_at")) if held and held.get("compiled") is not None else "",
            "modal_max": MODAL_MAX}


def rule_line(c: dict) -> str:
    who = " & ".join(x for x in (c.get("a"), c.get("b")) if x)
    extra = f" group {c['group']}" if c.get("group") else (f" {c['role']} ≥ {c.get('count')}" if c.get("role") else "")
    return f"{c.get('type')} {who}{extra} ({c.get('hardness', 'soft')})".strip()


def render_compiled(doc: str, compiled: BaseModel) -> str:
    if isinstance(compiled, PersonaReading):
        return compiled.text + f"\n\n_{compiled.summary}_"
    if isinstance(compiled, LootCompiled):
        lines = [f"**{r.id}** ({r.kind}) {r.text}" for r in compiled.rules]
    elif isinstance(compiled, CompCompiled):
        lines = []
        for c in compiled.constraints:
            who = " & ".join(x for x in (c.a, c.b) if x)
            extra = f" group {c.group}" if c.group else (f" {c.role} ≥ {c.count}" if c.role else "")
            lines.append(f"• `{c.type}` {who}{extra} _({c.hardness})_ — “{c.source[:80]}”")
    else:
        return "(no compiled form)"
    out = "\n".join(lines) + f"\n\n_{compiled.summary}_"
    if compiled.ambiguities:
        out += "\n\n**Questions:**\n" + "\n".join(f"• {q}" for q in compiled.ambiguities)
    return out
