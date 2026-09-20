"""The analytics channel posts native cards (no images): one per raid, icons + numbers, edited in place."""
import discord

from oibot_gm import discord_pool as dp

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def text_of(view) -> str:
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def test_pool_card_is_native_and_icon_only(reg):
    roster = reg.raid_shell("barrow_deeps")
    view, level = dp.pool_layout(reg, roster, ico)
    assert isinstance(view, discord.ui.LayoutView) and level in ("green", "amber", "red")
    body = text_of(view)
    assert "Barrow Deeps" in body and "mains" in body
    for role in ("tank", "healer"):
        assert f"<role:{role}> " in body          # a role count is the icon plus numbers …
        assert f"{role} " not in body.replace(f"<role:{role}>", "")  # … never the role's name beside it
    assert not hasattr(dp, "bank_card") and not hasattr(dp, "groups_card")  # the image cards are gone


def test_refresh_edits_in_place_and_removes_legacy_cards(reg):
    import asyncio

    class Msg:
        def __init__(self, mid): self.id, self.edits, self.deleted = mid, 0, False
        async def edit(self, **kw): self.edits += 1; return self
        async def delete(self): self.deleted = True

    class Ch:
        name = "analytics"
        def __init__(self): self.msgs, self.sent = {}, 0
        async def fetch_message(self, mid): return self.msgs[mid]
        async def send(self, **kw):
            assert "view" in kw and "file" not in kw and "embed" not in kw
            self.sent += 1; m = Msg(1000 + self.sent); self.msgs[m.id] = m; return m

    class Bot(dp.PoolMixin):
        ico = staticmethod(ico)
        class ops:  # noqa: N801
            @staticmethod
            async def emit(*a, **k): pass
        def __init__(self, ch): self.ch = ch
        def get_channel(self, cid): return self.ch

    ch = Ch(); legacy = Msg(7); ch.msgs[7] = legacy
    reg.config.analytics_channel_id, reg.config.analytics_message_ids = 1, {"_bank": 7}
    bot = Bot(ch)
    asyncio.run(bot.refresh_pool(reg))
    n = len(reg.profile.raids)
    assert legacy.deleted and ch.sent == n and "_bank" not in reg.config.analytics_message_ids
    assert set(reg.config.analytics_message_ids) == {f"{dp.CARD_PREFIX}{rid}" for rid in reg.profile.raids}
    asyncio.run(bot.refresh_pool(reg))  # second pass: nothing new is posted, every card is edited
    assert ch.sent == n and all(m.edits == 1 for m in ch.msgs.values() if m is not legacy)


def test_every_comp_line_has_an_icon_and_the_raid_thumbnail_renders(reg):
    from oibot_gm import render
    assert dp._comp_icon(ico, "dps") == "<role:melee><role:ranged>" and dp._comp_icon(ico, "tank") == "<role:tank>"
    assert dp._comp_icon(ico, "Shaman") == "<class:Shaman>" and dp._comp_icon(ico, "Shaman:Restoration") == "<spec:Shaman:Restoration>"
    for rid, rd in reg.profile.raids.items():  # the card's thumbnail: a removed renderer once took a constant with it
        assert render.raid_thumb_png(rid, rd.get("name", rid), int(rd.get("size") or 0), 7)[:4] == b"\x89PNG"
