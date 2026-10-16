"""The Windows companion's delivery (companion/oibot_companion.py): an event stays in the Outbox until the bot acks
it by idem — a dropped socket re-sends it first after the reconnect, a refusal (an error reply) is retried with the same
idem, a heartbeat's reply never counts as the event's ack."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("oibot_companion", Path(__file__).resolve().parents[1] / "companion" / "oibot_companion.py")
comp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comp)


class FakeWS:
    """A socket that replies from a script; `None` in the script = the connection drops."""

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def send(self, frame):
        self.sent.append(json.loads(frame))

    async def recv(self):
        r = self.replies.pop(0)
        if r is None:
            raise ConnectionError("socket closed")
        return json.dumps(r)


def test_an_event_cut_off_before_its_ack_is_resent_first_after_the_reconnect():
    box = comp.Outbox()
    box.put({"type": "loot", "idem": "a"})
    box.put({"type": "loot", "idem": "b"})
    ev = box.next(0)
    with pytest.raises(ConnectionError):
        asyncio.run(comp.deliver(FakeWS([None]), ev, box))
    again = box.next(1)
    assert again["idem"] == "a", "never lost: the in-flight event goes first on the new socket"
    ws = FakeWS([{"type": "ack", "idem": None}, {"type": "ack", "idem": "a", "dup": True}])  # a heartbeat's ack first
    assert asyncio.run(comp.deliver(ws, again, box)) == "ack" and box.next(2)["idem"] == "b"
    assert ws.sent == [{"type": "loot", "idem": "a"}], "the same idem: the bot dedupes the re-send"


def test_a_refused_event_is_retried_with_the_same_idem_then_given_up():
    box = comp.Outbox(retry_s=30, retry_for_s=600)
    box.put({"type": "loot", "idem": "r"})
    ev = box.next(0)
    ws = FakeWS([{"type": "error", "idem": "r", "message": "several loot councils are live"}])
    assert asyncio.run(comp.deliver(ws, ev, box)) == "refused"
    assert box.next(10) is None and len(box) == 1, "kept for a retry, not dropped"
    retry = box.next(10_000_000_000)
    assert retry["idem"] == "r" and "_first_refused" not in json.loads(comp.wire(retry))
    assert not box.refused(retry, retry["_first_refused"] + 601), "given up after RETRY_FOR_S"
    assert len(box) == 0
