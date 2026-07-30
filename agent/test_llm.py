"""Tests for the provider-neutral LLM client.

The point is the failure modes, not the happy path. Each test simulates a specific
badly-behaved gateway and asserts the client detects or works around it:

  - a provider that silently drops the tools array
  - a provider that emulates tool calling in message text
  - a provider that rejects json_schema response_format
  - a provider that returns JSON wrapped in markdown fences
  - a provider that returns unparseable JSON until asked to fix it
  - a tool that raises

Run: python agent/test_llm.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from llm import Capabilities, LLMClient, StructuredOutputError  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "sets": {"type": "integer"}},
    "required": ["name", "sets"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Fake gateway plumbing
# ---------------------------------------------------------------------------

def message(content=None, tool_calls=None):
    calls = None
    if tool_calls:
        calls = [
            SimpleNamespace(
                id=f"call_{i}",
                type="function",
                function=SimpleNamespace(name=name, arguments=json.dumps(args)),
            )
            for i, (name, args) in enumerate(tool_calls)
        ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))]
    )


class FakeGateway:
    """Scripted responder. `behaviour` inspects the request and returns a response,
    or raises to simulate a provider rejecting a parameter."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests: list[dict] = []

    def install(self, client: LLMClient):
        def create(**kwargs):
            self.requests.append(kwargs)
            return self.behaviour(kwargs, len(self.requests) - 1)
        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            models=SimpleNamespace(list=lambda: SimpleNamespace(data=[])),
        )
        return client


def make_client(behaviour, **kw) -> tuple[LLMClient, FakeGateway]:
    client = LLMClient(model="test/model", base_url="http://fake/v1", **kw)
    gateway = FakeGateway(behaviour)
    gateway.install(client)
    return client, gateway


class FakeBadRequest(llm.BadRequestError):
    def __init__(self, msg="unsupported parameter"):
        # Bypass the SDK's constructor, which wants a real httpx response.
        Exception.__init__(self, msg)


failures: list[str] = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


# ---------------------------------------------------------------------------
# Compression header — the sharpest risk with OmniRoute
# ---------------------------------------------------------------------------

print("=== compression defaults")

client, gateway = make_client(lambda kw, i: message(content='{"name":"x","sets":3}'))
client.structured([{"role": "user", "content": "go"}], SCHEMA)
headers = gateway.requests[0].get("extra_headers") or {}
check("compression disabled by default", headers.get("x-omniroute-compression") == "off",
      str(headers))

client, gateway = make_client(lambda kw, i: message(content='{"name":"x","sets":3}'),
                             compression=True)
client.structured([{"role": "user", "content": "go"}], SCHEMA)
headers = gateway.requests[0].get("extra_headers") or {}
check("compression opt-in omits the off header",
      "x-omniroute-compression" not in headers, str(headers))


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

print("\n=== preflight detects provider behaviour")

# A well-behaved native provider.
def native(kw, i):
    if "tools" in kw:
        return message(tool_calls=[("report_ready", {"ok": True})])
    return message(content='{"ok": true}')

client, _ = make_client(native)
caps = client.preflight()
check("native provider: reachable", caps.reachable)
check("native provider: tool calling detected", caps.native_tool_calling)
check("native provider: json_schema detected", caps.json_schema_response_format)
check("native provider: usable for routines", caps.usable_for_routines)
check("native provider: no warnings", caps.notes == [], str(caps.notes))

# The dangerous one: tools silently dropped, model just answers in text.
def drops_tools(kw, i):
    return message(content="Sure, everything looks fine!")

client, _ = make_client(drops_tools)
caps = client.preflight()
check("dropped tools: detected as unusable", not caps.usable_for_routines)
check("dropped tools: explains silent drop",
      any("silently dropping" in n for n in caps.notes), str(caps.notes))

# Emulated tool calling: intent left in the text as a <tool> block.
def emulates_tools(kw, i):
    if "tools" in kw:
        return message(content='<tool>{"name":"report_ready","ok":true}</tool>')
    return message(content='{"ok": true}')

client, _ = make_client(emulates_tools)
caps = client.preflight()
check("emulated tools: not treated as native", not caps.native_tool_calling)
check("emulated tools: warns about brittleness",
      any("emulates tool calling" in n for n in caps.notes), str(caps.notes))

# Native tools, but json_schema rejected.
def no_json_schema(kw, i):
    if "response_format" in kw:
        raise FakeBadRequest("response_format not supported")
    return message(tool_calls=[("report_ready", {"ok": True})])

client, _ = make_client(no_json_schema)
caps = client.preflight()
check("no json_schema: still usable for routines", caps.usable_for_routines)
check("no json_schema: recorded as a note",
      any("json_schema response_format rejected" in n for n in caps.notes), str(caps.notes))

# Gateway down.
def unreachable(kw, i):
    raise ConnectionError("connection refused")

client, _ = make_client(unreachable)
caps = client.preflight()
check("unreachable gateway: not reachable", not caps.reachable)
check("unreachable gateway: not usable", not caps.usable_for_routines)
check("capabilities serialize", isinstance(caps.as_dict()["notes"], list))


# ---------------------------------------------------------------------------
# structured output degradation
# ---------------------------------------------------------------------------

print("\n=== structured output falls back cleanly")

client, _ = make_client(lambda kw, i: message(content='{"name":"Squat","sets":4}'))
obj, strategy = client.structured([{"role": "user", "content": "go"}], SCHEMA)
check("json_schema path used when available", strategy == "json_schema", strategy)
check("json_schema path parses", obj == {"name": "Squat", "sets": 4}, str(obj))

def tool_fallback(kw, i):
    if "response_format" in kw:
        raise FakeBadRequest()
    return message(tool_calls=[("result", {"name": "Deadlift", "sets": 3})])

client, gateway = make_client(tool_fallback)
obj, strategy = client.structured([{"role": "user", "content": "go"}], SCHEMA)
check("falls back to forced tool call", strategy == "forced_tool_call", strategy)
check("forced tool call parses arguments", obj == {"name": "Deadlift", "sets": 3}, str(obj))
check("schema passed as tool parameters",
      gateway.requests[1]["tools"][0]["function"]["parameters"] == SCHEMA)
check("tool_choice forces the function",
      gateway.requests[1]["tool_choice"]["function"]["name"] == "result")

def repair_fallback(kw, i):
    if "response_format" in kw:
        raise FakeBadRequest()
    if "tools" in kw:
        raise FakeBadRequest("tools not supported")
    # Markdown-fenced JSON, the most common real-world violation.
    return message(content='```json\n{"name":"Press","sets":5}\n```')

client, _ = make_client(repair_fallback)
obj, strategy = client.structured([{"role": "user", "content": "go"}], SCHEMA)
check("falls back to json_repair", strategy == "json_repair", strategy)
check("strips markdown fences", obj == {"name": "Press", "sets": 5}, str(obj))

# Unparseable once, then corrected — proves the repair conversation works.
state = {"n": 0}
def repairs_on_second_try(kw, i):
    if "response_format" in kw or "tools" in kw:
        raise FakeBadRequest()
    state["n"] += 1
    if state["n"] == 1:
        return message(content='{"name": "Row", "sets": }')
    return message(content='{"name":"Row","sets":4}')

client, gateway = make_client(repairs_on_second_try)
obj, strategy = client.structured([{"role": "user", "content": "go"}], SCHEMA)
check("recovers from a parse error", obj == {"name": "Row", "sets": 4}, str(obj))
check("repair prompt shows the parse error",
      any("did not parse as JSON" in str(m.get("content"))
          for m in gateway.requests[-1]["messages"]))

def never_valid(kw, i):
    if "response_format" in kw or "tools" in kw:
        raise FakeBadRequest()
    return message(content="I'd rather write prose than JSON, honestly.")

client, _ = make_client(never_valid)
try:
    client.structured([{"role": "user", "content": "go"}], SCHEMA, max_repair_attempts=1)
    check("raises when JSON is unobtainable", False, "no exception")
except StructuredOutputError as exc:
    check("raises when JSON is unobtainable", "could not obtain" in str(exc))


# ---------------------------------------------------------------------------
# tool loop
# ---------------------------------------------------------------------------

print("\n=== tool loop")

def two_round_tools(kw, i):
    if i == 0:
        return message(tool_calls=[("search_exercises", {"equipment": "Kettlebell"})])
    return message(content="Here is your routine.")

calls_seen: list[tuple[str, dict]] = []

def dispatch(name, arguments):
    calls_seen.append((name, arguments))
    return {"results": [{"id": 9, "name": "Kettlebell Swing"}]}

client, _ = make_client(two_round_tools)
conversation, final = client.run_tools(
    [{"role": "user", "content": "kettlebell routine"}],
    tools=[{"type": "function", "function": {"name": "search_exercises",
                                            "parameters": {"type": "object"}}}],
    dispatch=dispatch,
)
check("tool was dispatched", calls_seen == [("search_exercises", {"equipment": "Kettlebell"})],
      str(calls_seen))
check("final text returned", final == "Here is your routine.", final)
check("assistant turn preserves tool_calls",
      any(m.get("role") == "assistant" and m.get("tool_calls") for m in conversation))
check("tool result carries tool_call_id",
      any(m.get("role") == "tool" and m.get("tool_call_id") for m in conversation))

# A raising tool must be reported to the model, not crash the request.
def one_tool_then_done(kw, i):
    if i == 0:
        return message(tool_calls=[("broken_tool", {})])
    return message(content="I could not complete that.")

def raising_dispatch(name, arguments):
    raise ValueError("database is on fire")

client, _ = make_client(one_tool_then_done)
conversation, final = client.run_tools(
    [{"role": "user", "content": "go"}],
    tools=[{"type": "function", "function": {"name": "broken_tool",
                                            "parameters": {"type": "object"}}}],
    dispatch=raising_dispatch,
)
tool_messages = [m for m in conversation if m.get("role") == "tool"]
check("tool failure becomes a tool result",
      tool_messages and "database is on fire" in tool_messages[0]["content"],
      str(tool_messages))
check("request survives a raising tool", final == "I could not complete that.", final)

# Malformed tool arguments from the provider.
def bad_arguments(kw, i):
    if i == 0:
        bad = SimpleNamespace(
            id="call_x", type="function",
            function=SimpleNamespace(name="search_exercises", arguments="{not json"),
        )
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=None, tool_calls=[bad]))])
    return message(content="done")

client, _ = make_client(bad_arguments)
conversation, final = client.run_tools(
    [{"role": "user", "content": "go"}],
    tools=[{"type": "function", "function": {"name": "search_exercises",
                                            "parameters": {"type": "object"}}}],
    dispatch=dispatch,
)
tool_messages = [m for m in conversation if m.get("role") == "tool"]
check("unparseable tool arguments reported back",
      tool_messages and "not valid JSON" in tool_messages[0]["content"],
      str(tool_messages))

# Runaway loop must terminate rather than spin forever.
client, _ = make_client(lambda kw, i: message(tool_calls=[("search_exercises", {})]))
conversation, final = client.run_tools(
    [{"role": "user", "content": "go"}],
    tools=[{"type": "function", "function": {"name": "search_exercises",
                                            "parameters": {"type": "object"}}}],
    dispatch=dispatch,
    max_rounds=3,
)
check("max_rounds terminates the loop", final == "")
check("max_rounds still returns the transcript", len(conversation) > 3)


print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILURES: ' + str(failures)}")
sys.exit(1 if failures else 0)
