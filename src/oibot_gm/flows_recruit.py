"""The owner's `/gm config recruitment` wizard: applications open or closed, the note on the Apply screen, the guild's own
application questions (add · edit · remove · move), the member roles that skip applying, and the public invite link.

One hub screen shows the whole draft; every section edits the draft and comes back to it. Nothing is written until
Save, which makes ONE `Registry.set_recruitment` call with only what changed — validated as a whole, one commit, a
refusal leaves the settings as they were and keeps the draft. A question is checked the moment it is added or edited
with the registry's own validator (`_check_questions`: 45-character label, 100-character hint, no duplicates, at most
QUESTIONS_MAX), so the wizard never re-implements a rule. The note, a question and the invite link are prose: typed,
validated, never parsed. Member roles are Discord's own role picker."""
from __future__ import annotations

import copy

import discord

from .constants import TEAL
from .flows_config import OwnerWizard, _owner_guard, _owner_or_refuse
from .registry import QUESTIONS_MAX, Registry, RegistryError
from .wizard import Field, Form, Opt, flow

NOTE_MAX = 200


class RecruitWizard(OwnerWizard):
    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Recruitment", guard=_owner_guard(reg))
        r, cfg = reg.config.recruitment, reg.config
        self.draft = {"open": r.open, "note": r.note or "", "invite": cfg.invite_url or "", "roles": list(cfg.member_role_ids),
                      "questions": [q.model_dump() for q in r.questions]}
        self.orig = copy.deepcopy(self.draft)
        self.form_vals: dict = {}  # what the last question form held, so a refusal reopens it filled in

    # ---- the hub: the whole draft, every section one press away
    def changes(self) -> dict:
        """The set_recruitment arguments for what differs from the saved settings."""
        d, o, kw = self.draft, self.orig, {}
        if d["open"] != o["open"]:
            kw["open"] = d["open"]
        if d["note"] != o["note"]:
            kw["note"] = d["note"]
        if d["invite"] != o["invite"]:
            kw["invite_url"] = d["invite"]
        if sorted(d["roles"]) != sorted(o["roles"]):
            kw["member_role_ids"] = list(d["roles"])
        if d["questions"] != o["questions"]:
            kw["questions"] = [dict(q) for q in d["questions"]]
        return kw

    def summary(self) -> str:
        d = self.draft
        roles = ", ".join(f"<@&{r}>" for r in d["roles"]) or "none"
        lines = [f"**Applications:** {'open' if d['open'] else 'closed'}",
                 f"**Note on the Apply screen:** {d['note'] or 'none'}",
                 f"**Invite link:** {d['invite'] or 'none'}",
                 f"**Member roles** (holders register without applying): {roles}"]
        n = len(self.changes())
        lines.append(f"-# {n} change(s) in the draft — nothing is saved until **Save**." if n else "-# No changes yet.")
        return "\n".join(lines)

    def questions_embed(self) -> discord.Embed:
        qs = self.draft["questions"]
        body = "\n".join(f"**{i + 1}.** {q['label']}{' · required' if q.get('required') else ''}" + (f"\n-# {q['hint']}" if q.get("hint") else "")
                         for i, q in enumerate(qs))
        return discord.Embed(title=f"Questions ({len(qs)} of {QUESTIONS_MAX})", colour=TEAL,
                             description=body or "No questions: the application asks for name, logs, schedules and an optional *Anything else?*.")

    def role_picker(self) -> discord.ui.RoleSelect:
        s = discord.ui.RoleSelect(placeholder="Member roles (skip applying)", min_values=0, max_values=25,
                                  default_values=[discord.Object(id=r, type=discord.Role) for r in self.draft["roles"][:25]])

        async def cb(i: discord.Interaction) -> None:
            self.draft["roles"] = [r.id for r in s.values]
            await self.hub(i)
        s.callback = cb  # type: ignore[method-assign]
        return s

    async def hub(self, i: discord.Interaction) -> None:
        d, qs = self.draft, self.draft["questions"]
        items: list = []
        if qs:
            items.append(self.select("Pick a question to change", [Opt(str(k), f"{k + 1}. {q['label']}", q.get("hint") or None) for k, q in enumerate(qs)],
                                     self.on_question))
        items += [self.role_picker(),
                  self.button("Close applications" if d["open"] else "Open applications", self.toggle),
                  self.button("Note…", lambda x: self.open_form(x, self.note_form())),
                  self.button("Invite link…", lambda x: self.open_form(x, self.invite_form())),
                  self.button("Add a question…", self.add_question, disabled=len(qs) >= QUESTIONS_MAX),
                  self.button("Save", self.save, discord.ButtonStyle.success),
                  self.button("Cancel", self.cancel)]
        await self.show(i, self.summary(), *items, embed=self.questions_embed())

    async def start(self, i: discord.Interaction) -> None:
        await self.hub(i)

    async def toggle(self, i: discord.Interaction) -> None:
        self.draft["open"] = not self.draft["open"]
        await self.hub(i)

    # ---- note and invite (prose: validated, not parsed; the invite's format is checked by set_recruitment at Save)
    def note_form(self) -> Form:
        return Form("Note on the Apply screen", [Field("note", "Note (empty clears it)", required=False, paragraph=True, default=self.draft["note"] or None,
                                                       max_length=NOTE_MAX, description="one line applicants see, e.g. 'Looking for healers'")], self.note_submitted)

    async def note_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["note"] = v.get("note", "")
        await self.hub(i)

    def invite_form(self) -> Form:
        return Form("Invite link", [Field("invite", "Discord invite (empty clears it)", required=False, default=self.draft["invite"] or None, max_length=100,
                                          placeholder="https://discord.gg/abc123", description="the public invite the website's Join button opens")],
                    self.invite_submitted)

    async def invite_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["invite"] = v.get("invite", "")
        await self.hub(i)

    # ---- questions
    def question_form(self, idx: int | None) -> Form:
        cur = self.form_vals or (self.draft["questions"][idx] if idx is not None else {})
        req = bool(cur.get("required"))
        fields = [Field("label", "Question", default=cur.get("label") or None, max_length=45, description="what the applicant is asked (45 characters)"),
                  Field("hint", "Hint (optional)", required=False, default=cur.get("hint") or None, max_length=100, description="a line under the question (100 characters)"),
                  Field("required", "Must they answer it?", [Opt("yes", "Required", default=req), Opt("no", "Optional", default=not req)])]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            await self.question_submitted(i, idx, v)
        return Form("Edit question" if idx is not None else "Add a question", fields, done)

    async def add_question(self, i: discord.Interaction) -> None:
        self.form_vals = {}
        await self.open_form(i, self.question_form(None))

    async def question_submitted(self, i: discord.Interaction, idx: int | None, v: dict[str, str]) -> None:
        q = {"label": v.get("label", ""), "hint": v.get("hint", "") or None, "required": v.get("required") == "yes"}
        qs = [dict(x) for x in self.draft["questions"]]
        if idx is None:
            qs.append({"id": "", **q})
        else:
            qs[idx] = {**qs[idx], **q}
        try:
            checked = self.reg._check_questions(qs)  # the registry's own rules, before anything is saved
        except RegistryError as e:
            self.form_vals = q
            await self.refuse(i, f"{e} Your text is kept.", lambda x: self.open_form(x, self.question_form(idx)))
            return
        self.form_vals = {}
        self.draft["questions"] = [c.model_dump() for c in checked]
        await self.hub(i)

    async def on_question(self, i: discord.Interaction, values: list[str]) -> None:
        await self.question_screen(i, int(values[0]))

    async def question_screen(self, i: discord.Interaction, idx: int) -> None:
        qs = self.draft["questions"]
        if not 0 <= idx < len(qs):
            await self.hub(i)
            return
        q = qs[idx]
        text = f"Question {idx + 1} of {len(qs)}: **{q['label']}**{' · required' if q.get('required') else ' · optional'}" + (f"\n-# {q['hint']}" if q.get("hint") else "")

        async def edit(x: discord.Interaction) -> None:
            self.form_vals = {}
            await self.open_form(x, self.question_form(idx))
        await self.show(i, text,
                        self.button("Edit…", edit, discord.ButtonStyle.primary),
                        self.button("Move up", lambda x: self.move(x, idx, -1), disabled=idx == 0),
                        self.button("Move down", lambda x: self.move(x, idx, 1), disabled=idx == len(qs) - 1),
                        self.button("Remove", lambda x: self.remove(x, idx), discord.ButtonStyle.danger),
                        self.button("Back", self.hub))

    async def move(self, i: discord.Interaction, idx: int, step: int) -> None:
        qs, j = self.draft["questions"], idx + step
        if 0 <= j < len(qs):
            qs[idx], qs[j] = qs[j], qs[idx]
        await self.question_screen(i, max(0, min(j, len(qs) - 1)))

    async def remove(self, i: discord.Interaction, idx: int) -> None:
        if 0 <= idx < len(self.draft["questions"]):
            self.draft["questions"].pop(idx)
        await self.hub(i)

    # ---- save: one set_recruitment call with what changed
    async def save(self, i: discord.Interaction) -> None:
        kw = self.changes()
        if not kw:
            await self.finish(i, "Nothing changed.")
            return
        await self.working(i, "Saving…")
        try:
            line = self.reg.set_recruitment(i.user.display_name, **kw)
        except RegistryError as e:
            retry = (lambda x: self.open_form(x, self.invite_form())) if "invite" in str(e).lower() else self.hub
            await self.refuse(i, f"{e} Nothing was saved; your draft is kept.", retry, "Fix this")
            return
        await self.finish(i, f"✅ Saved — {line}.")
        ops = getattr(i.client, "ops", None)
        if ops is not None:
            await ops.emit(self.reg.config, "info", f"{line} (by {i.user.display_name})")
        refresh = getattr(i.client, "refresh_registration_card", None)
        if refresh is not None:
            await refresh(self.reg)


@flow("recruitment")
async def open_recruitment(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await _owner_or_refuse(interaction, reg):
        return
    await RecruitWizard(reg, interaction.user.id).start(interaction)


__all__ = ["RecruitWizard", "open_recruitment"]
