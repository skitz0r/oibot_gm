"""`/me wishlist` (design §3.1 "Wishlists", owner decision D5): a short Discord path onto the native wishlist — the Me
page is the list builder. Character (skipped with one) → the list as it stands → one item select over the guild's
items by name (learned drops first, then the tables; past 24 a *Search by name…* box — an item name is prose, matched
against the items, and one the tables don't know yet can be kept by name) → one form: place on the list (a preset
select) and main/offspec, an optional note → Confirm. Nothing is written before Confirm; the write is
Registry.wish_add / wish_remove (the plain-text `wishlist` op's verbs). The public loot log's *Add to my wishlist*
button opens this wizard on its item."""
from __future__ import annotations

import asyncio
import os

import discord

from .registry import WISH_NOTE_MAX, WISHLIST_MAX, Registry, RegistryError, item_key
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, flow

SEARCH = "__search"
BYNAME = "__byname"


def me_page(reg: Registry) -> str:
    web = os.environ.get("OIBOT_WEB_URL", "")
    return f"{web}/app/me?g={reg.key}" if web.startswith("http") else "the site's Me page"


class WishlistWizard(Wizard):
    def __init__(self, reg: Registry, owner_id: int, display_name: str):
        super().__init__(reg, owner_id, title="My wishlist")
        self.display_name = display_name
        self.draft = {"character": None, "item": None, "name": None, "rank": None, "offspec": False, "note": "", "q": ""}

    # ---- who and what
    def member(self):
        return self.reg.members.get(self.owner_id)

    def char(self):
        m = self.member()
        return next((c for c in (m.characters if m else []) if c.label == self.draft["character"]), None)

    async def start(self, interaction: discord.Interaction, item: str | None = None) -> None:
        m = self.member()
        chars = [c for c in (m.characters if m else []) if c.status in ("active", "planned")]
        if not chars:
            await self.finish(interaction, "Your wishlist belongs to a character — register one first with `/register`.")
            return
        if item:
            it = self.reg.items().get(item) or self.reg.item(item, item)
            if it is not None:
                self.draft["item"], self.draft["name"] = item_key(it.id, it.name), it.name
        if len(chars) == 1:
            self.draft["character"] = chars[0].label
            await (self.details_prompt(interaction) if self.draft["name"] else self.home(interaction))
            return
        main = next((c for c in chars if c.is_main), chars[0])
        ico = getattr(interaction.client, "ico", None)
        opts = [Opt(c.label, c.label, f"{c.cls} {c.spec}" + (" · main" if c.is_main else "") + f" · {len(c.wishlist)}/{WISHLIST_MAX}",
                    emoji=ico("class", c.cls) if ico else None, default=c.label == main.label) for c in sorted(chars, key=lambda c: (not c.is_main, c.label.lower()))]
        what = f" **{self.draft['name']}** goes on" if self.draft["name"] else ""
        await self.show(interaction, f"Whose wishlist{what}? Each character keeps its own list (up to {WISHLIST_MAX}).",
                        self.select("Pick the character", opts[:MAX_OPTIONS], self.char_picked), self.button("Cancel", self.cancel))

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["character"] = values[0]
        await (self.details_prompt(interaction) if self.draft["name"] else self.home(interaction))

    def list_text(self) -> str:
        c = self.char()
        if c is None or not c.wishlist:
            return "_empty so far_"
        return "\n".join(f"{e.rank}. {e.name} · {'OS' if e.offspec else 'MS'}" + (f" — {e.note}" if e.note else "") for e in c.wishlist)

    async def home(self, interaction: discord.Interaction, note: str | None = None) -> None:
        c = self.char()
        n = len(c.wishlist) if c else 0
        head = (note + "\n\n" if note else "") + f"**{self.draft['character']}** · {n}/{WISHLIST_MAX}\n{self.list_text()}\n\n"
        head += f"-# Long lists and re-ordering: {me_page(self.reg)} (Me › Wishlist)."
        items = []
        if n < WISHLIST_MAX:
            items.append(self.select("Add an item…", self.item_opts(), self.item_picked))
        if n:
            items.append(self.button("Remove one…", self.remove_menu))
        items.append(self.button("Done", self.done, discord.ButtonStyle.primary))
        await self.show(interaction, head + ("" if n < WISHLIST_MAX else f"\nThe list is full ({WISHLIST_MAX}) — remove one to add another."), *items)

    async def done(self, interaction: discord.Interaction) -> None:
        c = self.char()
        await self.finish(interaction, f"**{self.draft['character']}** · {len(c.wishlist) if c else 0}/{WISHLIST_MAX}\n{self.list_text()}")

    def item_opts(self) -> list[Opt]:
        """The guild's items by name: learned drops first (most dropped), then the tables A–Z; already on the list are
        left out. Past 24, the last option opens the name search (an item name is prose: matched, never parsed)."""
        c = self.char()
        mine = {e.key for e in (c.wishlist if c else [])} | {"n:" + e.name.lower() for e in (c.wishlist if c else [])}
        rows = []
        for k, it in self.reg.items().items():
            if k in mine or "n:" + it.name.lower() in mine:
                continue
            meta = self.reg.item_meta(k)
            rows.append((0 if meta else 1, -int(meta.get("times_dropped") or 0), it.name.lower(), k, it))
        rows.sort()
        opts = [Opt(k, it.name, " · ".join(x for x in (it.boss, (it.slot or "").replace("_", " ") if it.slot != "unknown" else "", "learned from a drop" if lm == 0 else "") if x) or None)
                for lm, _, _, k, it in rows[:MAX_OPTIONS - 1]]
        opts.append(Opt(SEARCH, "🔎 Search by name… (or an item not listed)", f"{len(rows)} items known" if rows else "no items on the tables yet"))
        return opts

    async def item_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        v = values[0]
        if v == SEARCH:
            await self.open_form(interaction, Form("Find an item", [Field("q", "Item name", default=self.draft["q"] or None, max_length=100,
                                                                           description="part of the name is enough", placeholder="e.g. Robe of")], self.searched))
            return
        it = self.reg.items().get(v)
        if it is None:
            await self.home(interaction, "❌ That item is gone from the list — pick again.")
            return
        self.draft["item"], self.draft["name"] = v, it.name
        await self.open_form(interaction, self.details_form())

    async def searched(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        q = " ".join((v.get("q") or "").split())[:100]
        self.draft["q"] = q
        if len(q) < 2:
            await self.show(interaction, "❌ Type at least two letters of the item's name.", self.button("Search again", self.search_again, discord.ButtonStyle.primary),
                            self.button("Back", self.home))
            return
        low = q.lower()
        hits = sorted(((k, it) for k, it in self.reg.items().items() if low in it.name.lower()), key=lambda kv: (not kv[1].name.lower().startswith(low), kv[1].name.lower()))
        exact = next(((k, it) for k, it in hits if it.name.lower() == low), None)
        if exact is not None:
            self.draft["item"], self.draft["name"] = exact
            self.draft["name"] = exact[1].name
            await self.details_prompt(interaction)
            return
        opts = [Opt(k, it.name, it.boss or None) for k, it in hits[:MAX_OPTIONS - 1]]
        opts.append(Opt(BYNAME, f"Keep “{q[:60]}” by name", "not on the tables yet — matched by name once it drops"))
        lead = f"{len(hits)} item(s) match “{q}”" + (" (the first 24 shown)" if len(hits) > MAX_OPTIONS - 1 else "") + "." if hits else f"No item called “{q}” on the tables or among the learned drops — keep it by name, or search again."
        await self.show(interaction, lead, self.select("Pick the item", opts, self.found_picked), self.button("Search again", self.search_again), self.button("Back", self.home))

    async def search_again(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, Form("Find an item", [Field("q", "Item name", default=self.draft["q"] or None, max_length=100)], self.searched))

    async def found_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        v = values[0]
        if v == BYNAME:
            self.draft["item"], self.draft["name"] = None, self.draft["q"]
        else:
            it = self.reg.items().get(v)
            if it is None:
                await self.home(interaction, "❌ That item is gone from the list — pick again.")
                return
            self.draft["item"], self.draft["name"] = v, it.name
        await self.open_form(interaction, self.details_form())

    # ---- place and MS/OS
    async def details_prompt(self, interaction: discord.Interaction) -> None:
        """After a modal (a form can't open from another form) or from the log button: one press to the details form."""
        await self.show(interaction, f"**{self.draft['name']}** for **{self.draft['character']}** — pick its place on the list and main or offspec.",
                        self.button("Place and MS/OS…", self.open_details, discord.ButtonStyle.primary), self.button("Back", self.home))

    async def open_details(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.details_form())

    def details_form(self) -> Form:
        c = self.char()
        entries = list(c.wishlist) if c else []
        on = next((e for e in entries if (self.draft["item"] and e.key == self.draft["item"]) or e.name.lower() == (self.draft["name"] or "").lower()), None)
        last = len(entries) if on else min(len(entries) + 1, WISHLIST_MAX)
        cur = on.rank if on else last
        ranks = [Opt(str(n), f"#{n}" + (" — most wanted" if n == 1 else " — last" if n == last and n > 1 else ""), default=n == cur) for n in range(1, last + 1)]
        off = on.offspec if on else self.draft["offspec"]
        kinds = [Opt("ms", "Main spec (MS)", default=not off), Opt("os", "Offspec (OS)", default=off)]
        return Form(f"{self.draft['name']}"[:45], [
            Field("rank", "Place on the list", ranks[:MAX_OPTIONS], description="1 = the item you want most"),
            Field("kind", "For", kinds),
            Field("note", "Note (optional)", required=False, default=(on.note if on else self.draft["note"]) or None, max_length=WISH_NOTE_MAX,
                  placeholder="e.g. BiS for tanking"),
        ], self.details_done)

    async def details_done(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        try:
            self.draft["rank"] = max(1, min(int(v.get("rank") or 1), WISHLIST_MAX))
        except ValueError:
            self.draft["rank"] = None
        self.draft["offspec"] = v.get("kind") == "os"
        self.draft["note"] = " ".join((v.get("note") or "").split())[:WISH_NOTE_MAX]
        d = self.draft
        c = self.char()
        moving = c is not None and any((d["item"] and e.key == d["item"]) or e.name.lower() == (d["name"] or "").lower() for e in c.wishlist)
        known = d["item"] is not None
        text = (f"{'Move' if moving else 'Add'} **{d['name']}** {'to' if moving else 'at'} **#{d['rank']}** on **{d['character']}**'s wishlist · "
                f"{'offspec' if d['offspec'] else 'main spec'}" + (f" — “{d['note']}”" if d["note"] else "")
                + ("" if known else "\n-# Not on the tables yet: kept by name, matched once it drops."))
        await self.show(interaction, text, self.button("Confirm", self.save, discord.ButtonStyle.success), self.button("Change", self.open_details),
                        self.button("Back", self.home))

    async def save(self, interaction: discord.Interaction) -> None:
        d = self.draft
        it = self.reg.items().get(d["item"]) if d["item"] else None
        try:
            line = await asyncio.to_thread(  # a git commit: off the event loop
                self.reg.wish_add, self.owner_id, d["character"], int(it.id) if it is not None and it.id else d["name"], by=self.display_name,
                rank=d["rank"], offspec=d["offspec"], note=d["note"] or None, name=d["name"])
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.open_details, "Change")
            return
        await self._audit(interaction, line)
        d.update(item=None, name=None, rank=None, note="", offspec=False)
        await self.home(interaction, f"✅ {line}")

    # ---- remove
    async def remove_menu(self, interaction: discord.Interaction) -> None:
        c = self.char()
        opts = [Opt(e.name, f"{e.rank}. {e.name}", "offspec" if e.offspec else "main spec") for e in (c.wishlist if c else [])]
        await self.show(interaction, "Which item comes off the list? (an item you received stays on the site, greyed — remove it here when you're done with it)",
                        self.select("Pick the item", opts, self.remove_picked), self.button("Back", self.home))

    async def remove_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        name = values[0]

        async def go(i: discord.Interaction) -> None:
            try:
                line = await asyncio.to_thread(self.reg.wish_remove, self.owner_id, self.draft["character"], name, by=self.display_name)
            except RegistryError as e:
                await self.refuse(i, str(e), self.home, "Back")
                return
            await self._audit(i, line)
            await self.home(i, f"✅ {line}")

        await self.show(interaction, f"Remove **{name}** from **{self.draft['character']}**'s wishlist?",
                        self.button("Remove", go, discord.ButtonStyle.danger), self.button("Back", self.home))

    async def _audit(self, interaction: discord.Interaction, line: str) -> None:
        ops = getattr(interaction.client, "ops", None)
        if ops is None:
            return
        from . import ops as ops_mod

        try:
            await ops_mod.audited(ops, self.reg, self.display_name, line, via="discord", by_id=self.owner_id)
        except Exception:  # noqa: BLE001 — the write happened; the ops line is a courtesy
            pass


@flow("me_wishlist")
async def open_wishlist(interaction: discord.Interaction, reg: Registry, *, item: str | None = None) -> None:
    """Your own wishlist (`item`: an item key to add — the loot log's *Add to my wishlist*)."""
    await WishlistWizard(reg, interaction.user.id, interaction.user.display_name).start(interaction, item=item)


__all__ = ["WishlistWizard", "open_wishlist"]
