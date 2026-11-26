"""The scripted stand-in for the answering model (answer tools, design §10): `ScriptedProvider(steps)` plays a fixed
list of turns through `converse` — no network, no model. A step is a list of `call(name, **input)` dicts (tool calls)
or a string (a text-only turn). Every request is kept in `.requests` so a test can read what the loop sent."""
from __future__ import annotations

import copy

from oibot_gm.llm.provider import ToolCall, Turn

USAGE = {"input": 100, "output": 20, "cache_read": 0, "cache_write": 0, "cost": 0.001, "model": "scripted"}


def call(name: str, /, **input) -> dict:
    """One scripted tool call (`name` is positional-only: tools take a `name` argument of their own)."""
    return {"name": name, "input": dict(input)}


def reply(**fields) -> dict:
    """The scripted ending: a call to the `reply` tool."""
    return call("reply", **fields)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, steps: list[list[dict] | str]):
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.n = 0

    def converse(self, workload: str, system: str, tools: list[dict], messages: list[dict], *, force: str | None = None) -> Turn:
        self.requests.append({"system": system, "tools": copy.deepcopy(tools), "messages": copy.deepcopy(messages), "force": force})
        if self.n >= len(self.steps):
            raise AssertionError(f"the scripted model ran past its script ({len(self.steps)} steps)")
        step = self.steps[self.n]
        self.n += 1
        if isinstance(step, str):
            return Turn(content=[{"type": "text", "text": step}], calls=[], text=step, stop_reason="end_turn", usage=dict(USAGE))
        calls = [ToolCall(f"call-{self.n}-{i}", c["name"], dict(c["input"])) for i, c in enumerate(step)]
        content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": dict(c.input)} for c in calls]
        return Turn(content=content, calls=calls, text="", stop_reason="tool_use", usage=dict(USAGE))

    def complete(self, *args, **kwargs):
        raise AssertionError("the answering loop never calls complete")
