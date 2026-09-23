"""Drive a wizard end to end without Discord: fake interactions that record what the bot answered.

    h = Harness(reg, rs, user=member)            # a person
    await FLOWS["absence"](h.command(), reg)      # the slash command
    form = h.modal                                # what opened
    await h.submit(form, start="2030-01-07", days="3")   # fill the modal's selects / text and submit
    await h.press("Confirm")                      # a button on the current screen, by label
    await h.pick("Pick the week", "2030-02-04")   # a select on the current screen, by placeholder
    h.text                                        # the newest thing the person sees

Officer/owner checks read a real discord.Member, so tests monkeypatch them, e.g.
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)

The fakes mirror what `Wizard.show/working/finish` rely on: response.is_done, send_message / edit_message /
send_modal, edit_original_response, original_response().id, and interaction.message.id for "is this our message?".
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import discord

_ids = itertools.count(10_000)


class FakeResponse:
    def __init__(self, h: "Harness", itx: "FakeInteraction"):
        self.h, self.itx, self._done = h, itx, False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, **kw):
        self._done = True
        mid = next(_ids)
        self.itx._sent_id = mid
        self.h._record(content, view, mid, embed)

    async def edit_message(self, *, content=None, embed=None, view=None, **kw):
        self._done = True
        self.h._record(content, view, self.itx.message.id if self.itx.message else None, embed)

    async def send_modal(self, modal):
        self._done = True
        self.h._lint(modal)
        self.h.modal = modal

    async def defer(self, **kw):
        self._done = True


class FakeInteraction:
    def __init__(self, h: "Harness", *, kind: discord.InteractionType, message_id: int | None):
        self.h, self.type, self.user, self.client = h, kind, h.user, h.client
        self.message = SimpleNamespace(id=message_id) if message_id is not None else None
        self.response = FakeResponse(h, self)
        self.guild_id, self.guild, self.channel = 1, None, None
        self._sent_id: int | None = None
        self.followup = SimpleNamespace(send=self._followup)

    async def _followup(self, content=None, **kw):
        self.h._record(content, kw.get("view"), None, kw.get("embed"))

    async def original_response(self):
        return SimpleNamespace(id=self._sent_id or (self.message.id if self.message else next(_ids)))

    async def edit_original_response(self, *, content=None, embed=None, view=None, **kw):
        mid = self._sent_id or (self.message.id if self.message else None)
        self.h._record(content, view, mid, embed)


class Harness:
    def __init__(self, reg, rs=None, *, user=None, officer: bool = False, client=None):
        self.reg, self.rs = reg, rs
        self.user = user or SimpleNamespace(id=424242, display_name="Tester", mention="@Tester")
        self.announced: list = []
        self.client = client or SimpleNamespace(raids=SimpleNamespace(store=lambda _reg: rs), announce_absence=self._announce, ico=lambda kind, key: "")
        self.modal = None
        self.screens: list[dict] = []  # every render: {"text", "view", "message_id"}

    async def _announce(self, reg, m, a, by):
        self.announced.append((m.display_name, a.start, a.end, by))

    def _record(self, content, view, message_id, embed):
        if view is not None:
            self._lint(view)
        assert len(content or "") <= 2000, f"message over Discord's 2000 characters: {len(content)}"
        self.screens.append({"text": content or "", "view": view, "message_id": message_id, "embed": embed})

    def _lint(self, obj) -> None:
        """Every modal and screen a flow produces must pass wizard.lint — what the Discord API would refuse."""
        from oibot_gm.wizard import lint

        errs = lint(obj)
        assert not errs, f"{type(obj).__name__} would be refused by Discord: {errs}"

    # ---- entry points
    def command(self) -> FakeInteraction:
        return FakeInteraction(self, kind=discord.InteractionType.application_command, message_id=None)

    def card_press(self) -> FakeInteraction:
        """A persistent card's button in a channel: its message is the card, not a wizard."""
        return FakeInteraction(self, kind=discord.InteractionType.component, message_id=999_999)

    # ---- the person's side
    @property
    def screen(self) -> dict:
        return self.screens[-1]

    @property
    def text(self) -> str:
        return self.screen["text"]

    def _on_screen(self) -> FakeInteraction:
        return FakeInteraction(self, kind=discord.InteractionType.component, message_id=self.screen["message_id"])

    async def submit(self, modal=None, **values) -> None:
        """Fill `values` into the modal by field key (select → its value, text box → the string) and submit it.
        The submit carries the message the modal was opened from (a button on the wizard), or none (a command)."""
        modal = modal or self.modal
        payload = []
        for key, item in modal._items.items():
            if key not in values:
                continue
            if isinstance(item, discord.ui.Select):
                payload.append({"type": 18, "component": {"type": 3, "custom_id": item.custom_id, "values": [str(values[key])]}})
            else:
                payload.append({"type": 18, "component": {"type": 4, "custom_id": item.custom_id, "value": str(values[key])}})
        modal._refresh(None, payload, {})
        opened_from = self.screens[-1]["message_id"] if self.screens and self.screens[-1]["view"] is not None else None
        itx = FakeInteraction(self, kind=discord.InteractionType.modal_submit, message_id=opened_from)
        self.modal = None
        await modal.on_submit(itx)

    def _items(self):
        view = self.screen["view"]
        assert view is not None, f"no buttons on the current screen: {self.text!r}"
        return list(view.children)

    async def press(self, label: str) -> None:
        btn = next((b for b in self._items() if isinstance(b, discord.ui.Button) and b.label == label), None)
        assert btn is not None, f"no button {label!r}; have {[getattr(b, 'label', None) for b in self._items()]}"
        itx = self._on_screen()
        view = self.screen["view"]
        assert await view.interaction_check(itx)
        await btn.callback(itx)

    async def pick(self, placeholder: str, *values: str) -> None:
        sel = next((s for s in self._items() if isinstance(s, discord.ui.Select) and s.placeholder == placeholder), None)
        assert sel is not None, f"no select {placeholder!r}; have {[getattr(s, 'placeholder', None) for s in self._items()]}"
        sel._values = list(values)  # what discord.py fills from the component payload
        itx = self._on_screen()
        view = self.screen["view"]
        assert await view.interaction_check(itx)
        await sel.callback(itx)

    async def pick_member(self, placeholder: str, *users) -> None:
        """Discord's member picker (UserSelect): `users` are objects with .id / .display_name (a Member in real life)."""
        sel = next((s for s in self._items() if isinstance(s, discord.ui.UserSelect) and s.placeholder == placeholder), None)
        assert sel is not None, f"no member picker {placeholder!r}"
        sel._values = list(users)
        itx = self._on_screen()
        view = self.screen["view"]
        assert await view.interaction_check(itx)
        await sel.callback(itx)

    def selects(self) -> list[str]:
        return [s.placeholder for s in self._items() if isinstance(s, discord.ui.Select | discord.ui.UserSelect)]

    def buttons(self) -> list[str]:
        return [b.label for b in self._items() if isinstance(b, discord.ui.Button)]
