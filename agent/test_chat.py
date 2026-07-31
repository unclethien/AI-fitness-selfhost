"""Tests for the chat surface.

Fakes the model, the sidecar and the routine pipeline, then asserts the behaviour that
matters for a conversation that can spend money and write to a training log:

  - a plain question is answered without invoking the routine pipeline
  - a program request routes through the real pipeline, once
  - the pipeline's progress reaches the UI as events
  - the conversation replays in wire format without orphaned tool results
  - history trimming never cuts an assistant/tool pair apart
  - the transcript shows what tools were used, not the raw tool payloads
  - a model failure keeps the user's message

Run: PYTHONPATH=agent:coaching python agent/test_chat.py
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path

# One test deliberately makes the gateway raise, and chat.py logs that with
# log.exception. Left alone, the traceback prints above the results and reads like a
# test failure. Silencing only chat's logger keeps any other module's warnings visible.
logging.getLogger("chat").addHandler(logging.NullHandler())
logging.getLogger("chat").propagate = False

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "coaching"))

failures: list[str] = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


# ---------------------------------------------------------------------------
# Fake sidecar holding just the chat tables
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.sessions: dict[int, dict] = {}
        self.messages: list[dict] = []
        self.next_session = 1
        self.next_message = 1
        self.commits = 0


class FakeCursor:
    def __init__(self, store: FakeStore, row_factory=None):
        self.store = store
        self.row_factory = row_factory
        self._rows: list = []

    def execute(self, sql, params=None):
        low = " ".join(sql.split()).lower()
        params = params or ()
        self._rows = []

        if "insert into chat_sessions" in low:
            sid = self.store.next_session
            self.store.next_session += 1
            self.store.sessions[sid] = {
                "id": sid, "title": params[0],
                "created_at": "2026-07-31T00:00:00+00:00",
                "updated_at": "2026-07-31T00:00:00+00:00",
            }
            self._rows = [(sid,)]

        elif "insert into chat_messages" in low:
            mid = self.store.next_message
            self.store.next_message += 1
            # Mirror the jsonb round trip: psycopg is handed a JSON *string* and hands
            # back a parsed structure. Storing the string and parsing on read is what
            # keeps this fake honest — the opposite choice hides real bugs.
            self.store.messages.append({
                "id": mid, "session_id": params[0], "role": params[1],
                "content": params[2], "tool_calls_raw": params[3],
                "tool_call_id": params[4], "model": params[5],
                "created_at": "2026-07-31T00:00:00+00:00",
            })
            self._rows = [(mid,)]

        elif "update chat_sessions set title" in low:
            title, sid = params
            session = self.store.sessions.get(sid)
            if session and session.get("title") is None:
                session["title"] = title

        elif "update chat_sessions set updated_at" in low:
            pass

        elif "delete from chat_sessions" in low:
            sid = params[0]
            self.store.sessions.pop(sid, None)
            self.store.messages = [
                m for m in self.store.messages if m["session_id"] != sid
            ]

        elif "from chat_sessions s" in low:
            rows = []
            for session in sorted(self.store.sessions.values(),
                                  key=lambda s: s["id"], reverse=True):
                turns = sum(
                    1 for m in self.store.messages
                    if m["session_id"] == session["id"]
                    and m["role"] in ("user", "assistant")
                )
                rows.append({**session, "turns": turns})
            self._rows = rows

        elif "from chat_sessions where id" in low:
            session = self.store.sessions.get(params[0])
            self._rows = [dict(session)] if session else []

        elif "from chat_messages" in low:
            sid = params[0]
            for message in self.store.messages:
                if message["session_id"] != sid:
                    continue
                raw = message["tool_calls_raw"]
                self._rows.append({
                    "id": message["id"], "role": message["role"],
                    "content": message["content"],
                    "tool_calls": json.loads(raw) if raw else None,
                    "tool_call_id": message["tool_call_id"],
                    "model": message["model"],
                    "created_at": message["created_at"],
                })
        else:
            raise AssertionError(f"unexpected SQL in the chat fake: {low[:120]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, store: FakeStore):
        self.store = store

    def cursor(self, row_factory=None):
        return FakeCursor(self.store, row_factory)

    def commit(self):
        self.store.commits += 1


# ---------------------------------------------------------------------------
# Fake model: a script of rounds, driven exactly the way llm.run_tools drives one
# ---------------------------------------------------------------------------

class FakeClient:
    """Replays a script. Each entry is ("tools", [(name, args), ...]) or ("text", str)."""

    def __init__(self, script, model="fake/model"):
        self.script = list(script)
        self.model = model
        self.calls: list[tuple[str, dict]] = []
        self.tools_offered: list[str] = []

    def run_tools(self, messages, tools, dispatch, max_rounds=12, on_event=None):
        self.tools_offered = [t["function"]["name"] for t in tools]
        conversation = list(messages)
        counter = 0
        for kind, payload in self.script:
            if kind == "text":
                conversation.append({"role": "assistant", "content": payload})
                return conversation, payload

            tool_calls = []
            for name, arguments in payload:
                counter += 1
                tool_calls.append({
                    "id": f"call_{counter}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                })
            conversation.append(
                {"role": "assistant", "content": None, "tool_calls": tool_calls}
            )
            for call, (name, arguments) in zip(tool_calls, payload):
                self.calls.append((name, arguments))
                if on_event:
                    on_event("tool_call", {"name": name, "arguments": arguments})
                result = dispatch(name, arguments)
                if on_event:
                    on_event("tool_result", {"name": name, "result": result})
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, default=str),
                })
        return conversation, ""


class ExplodingClient:
    model = "fake/model"

    def run_tools(self, *args, **kwargs):
        raise RuntimeError("gateway refused the connection")


class FakeToolContext:
    def __init__(self, *args, **kwargs):
        self.submitted_plan = None


import chat  # noqa: E402
import chat_repo  # noqa: E402

MODELS = {
    "chat_model": "fake/chat",
    "drafting_model": "fake/draft",
    "escalation_model": "fake/strong",
    "critic_model": "fake/critic",
}


def install_fakes(script, generation_result=None):
    """Point chat.py at fakes. Returns (store, conn, events, client, generated)."""
    store = FakeStore()
    conn = FakeConn(store)
    events: list[tuple[str, dict]] = []
    generated: list[str] = []

    client = FakeClient(script) if not isinstance(script, type) else script()
    chat.LLMClient = lambda model: client
    chat.tool_module.ToolContext = FakeToolContext
    chat.tool_module.dispatch = lambda ctx, name, args: {"ok": True, "tool": name}

    class FakeResult:
        def __init__(self, payload):
            self._payload = payload

        def as_dict(self):
            return self._payload

    def fake_generate_routine(conn_, wger, request, *, on_event=None, **kwargs):
        generated.append(request)
        if on_event:
            on_event("iteration_start", {"iteration": 1, "model": "fake/draft"})
            on_event("validated", {"iteration": 1, "errors": 0, "warnings": 1})
            on_event("critiqued", {"iteration": 1, "verdict": "approve"})
            on_event("written", {"routine_id": 7})
        return FakeResult(generation_result or {
            "ok": True, "routine_id": 7,
            "routine_url": "http://web:8000/en/routine/7/view/",
            "iterations": 1, "violations": [], "error": None,
            "critic": {"verdict": "approve"},
            "plan": {
                "name": "Test Block", "weeks": 4,
                "progression": {"model": "double progression", "detail": "..."},
                "rationale": "because",
                "days": [
                    {"name": "Lower", "is_rest": False, "slots": [
                        {"order": 1, "entries": [
                            {"exercise_id": 1, "sets": 3, "reps": "5", "rest_seconds": 180},
                        ]},
                    ]},
                    {"name": "Rest", "is_rest": True, "slots": []},
                ],
            },
        })

    fake_module = types.ModuleType("generate")
    fake_module.generate_routine = fake_generate_routine
    sys.modules["generate"] = fake_module

    # Name resolution for the plan summary; the real one hits the exercises table.
    chat.exercise_search.get_exercises = lambda conn_, ids, detail=False: [
        {"id": i, "name": f"Exercise {i}"} for i in ids
    ]

    return store, conn, events, client, generated


# ---------------------------------------------------------------------------

print("\nconversation tool set")

tool_names = [t["function"]["name"] for t in chat.conversation_tools()]
check("generate_routine is offered", "generate_routine" in tool_names)
check("submit_routine_plan is NOT offered", "submit_routine_plan" not in tool_names,
      f"got {tool_names}")
check("read tools are offered",
      {"get_trainee_profile", "get_recent_training", "search_exercises"}
      <= set(tool_names), f"got {tool_names}")


print("\na plain question does not run the pipeline")

store, conn, events, client, generated = install_fakes([
    ("tools", [("get_trainee_profile", {})]),
    ("text", "Your posterior chain volume is fine. Keep the hinge work at 2 days."),
])
session_id = chat_repo.create_session(conn)
turn = chat.respond(conn, None, session_id, "Am I doing enough hinging?",
                    emit=lambda k, d: events.append((k, d)), **MODELS)

check("replied with prose", turn.reply.startswith("Your posterior chain"))
check("pipeline was never called", generated == [], f"generated={generated}")
check("no routine attached", turn.routine is None)
check("profile tool was called", ("get_trainee_profile", {}) in client.calls)
kinds = [k for k, _ in events]
check("emitted tool_call and assistant", "tool_call" in kinds and "assistant" in kinds,
      f"got {kinds}")

roles = [m["role"] for m in store.messages]
check("persisted user, assistant(tool_calls), tool, assistant",
      roles == ["user", "assistant", "tool", "assistant"], f"got {roles}")
check("session titled from the first message",
      store.sessions[session_id]["title"] == "Am I doing enough hinging?",
      f"got {store.sessions[session_id]['title']!r}")


print("\na program request routes through the real pipeline")

store, conn, events, client, generated = install_fakes([
    ("tools", [("generate_routine", {"request": "4-day week, 60 minutes, kettlebell focus"})]),
    ("text", "Done — I built you a 4-day block. Lower day opens with the front squat."),
])
session_id = chat_repo.create_session(conn)
turn = chat.respond(conn, None, session_id, "Build me a 4-day week",
                    emit=lambda k, d: events.append((k, d)), **MODELS)

check("pipeline ran exactly once", len(generated) == 1, f"generated={generated}")
check("the pipeline got the restated request",
      generated and "kettlebell" in generated[0], f"got {generated}")
check("routine reported on the turn",
      turn.routine is not None and turn.routine["ok"] is True and
      turn.routine["url"].endswith("/routine/7/view/"), f"got {turn.routine}")

kinds = [k for k, _ in events]
check("emitted routine_started", "routine_started" in kinds, f"got {kinds}")
check("emitted routine_finished", "routine_finished" in kinds, f"got {kinds}")
progress = [d["stage"] for k, d in events if k == "routine_progress"]
check("forwarded every pipeline stage",
      progress == ["iteration_start", "validated", "critiqued", "written"],
      f"got {progress}")
texts = [d.get("text", "") for k, d in events if k == "routine_progress"]
check("stages carry human-readable text", all(texts) and "Passed" in texts[1],
      f"got {texts}")

# The summary handed back to the chat model must be talkable-about: resolved exercise
# names, not bare ids the chat model has never seen.
tool_message = [m for m in store.messages if m["role"] == "tool"][0]
summary = json.loads(tool_message["content"])
check("summary carries the program", "program" in summary, f"got {list(summary)}")
check("exercise names resolved in the summary",
      summary["program"]["days"][0]["entries"][0]["exercise"] == "Exercise 1",
      f"got {summary['program']['days'][0]}")
check("rest days marked, not listed as work",
      summary["program"]["days"][1] == {"name": "Rest", "rest": True},
      f"got {summary['program']['days'][1]}")
check("full plan not echoed back verbatim", "slots" not in json.dumps(summary))


print("\na failed program is reported, not hidden")

store, conn, events, client, generated = install_fakes(
    [("tools", [("generate_routine", {"request": "something impossible to program"})]),
     ("text", "I could not save that program.")],
    generation_result={
        "ok": False, "routine_id": None, "routine_url": None, "iterations": 4,
        "violations": [{"severity": "error", "message": "weekly volume too low"}],
        "error": "Routine still failing review after 3 revisions (2 errors).",
        "critic": {"verdict": "revise"}, "plan": None,
    },
)
session_id = chat_repo.create_session(conn)
turn = chat.respond(conn, None, session_id, "Build me something impossible",
                    emit=lambda k, d: events.append((k, d)), **MODELS)

finished = [d for k, d in events if k == "routine_finished"][0]
check("failure surfaced in the stream", finished["ok"] is False, f"got {finished}")
check("failure reason surfaced", "failing review" in (finished["error"] or ""))
tool_message = [m for m in store.messages if m["role"] == "tool"][0]
summary = json.loads(tool_message["content"])
check("violations passed back to the model so it can explain",
      summary["unresolved_violations"] and
      summary["unresolved_violations"][0]["message"] == "weekly volume too low")


print("\na model failure keeps the question")

store, conn, events, client, generated = install_fakes(ExplodingClient)
session_id = chat_repo.create_session(conn)
turn = chat.respond(conn, None, session_id, "What should I do about my knee?",
                    emit=lambda k, d: events.append((k, d)), **MODELS)

check("error recorded on the turn", turn.error and "gateway refused" in turn.error)
check("user message survived",
      any(m["role"] == "user" and "knee" in m["content"] for m in store.messages))
check("the failure is explained in the transcript, not silent",
      any(m["role"] == "assistant" and "gateway refused" in (m["content"] or "")
          for m in store.messages))
check("reply is shown to the user", "gateway refused" in turn.reply)


print("\nwire-format replay")

store = FakeStore()
conn = FakeConn(store)
session_id = chat_repo.create_session(conn)
chat_repo.append(conn, session_id, "user", content="hello")
chat_repo.append(conn, session_id, "assistant", content=None, tool_calls=[
    {"id": "c1", "type": "function",
     "function": {"name": "search_exercises", "arguments": '{"target_muscle_group":"Back"}'}}
])
chat_repo.append(conn, session_id, "tool", content='{"matches":[]}', tool_call_id="c1")
chat_repo.append(conn, session_id, "assistant", content="Here are some rows.")

wire = chat_repo.history(conn, session_id)
check("replays in order",
      [m["role"] for m in wire] == ["user", "assistant", "tool", "assistant"],
      f"got {[m['role'] for m in wire]}")
check("tool_calls survive the round trip",
      wire[1]["tool_calls"][0]["function"]["name"] == "search_exercises",
      f"got {wire[1]}")
check("tool_call_id survives", wire[2]["tool_call_id"] == "c1")
check("tool message content is a string", isinstance(wire[2]["content"], str))

visible = chat_repo.visible_messages(conn, session_id)
check("transcript hides raw tool payloads",
      not any('"matches"' in m["content"] for m in visible), f"got {visible}")
check("transcript summarizes the tool call as a trace",
      any(m["role"] == "trace" and "Searched exercises" in m["content"]
          for m in visible), f"got {visible}")
check("transcript keeps both prose turns",
      [m["role"] for m in visible if m["role"] != "trace"] == ["user", "assistant"],
      f"got {[m['role'] for m in visible]}")


print("\ntrimming never orphans a tool result")

store = FakeStore()
conn = FakeConn(store)
session_id = chat_repo.create_session(conn)
# Ten turns, each: user -> assistant(tool_call) -> tool -> assistant. Any naive
# "last N" slice will land mid-group for some N, which the API rejects outright.
for i in range(10):
    chat_repo.append(conn, session_id, "user", content=f"question {i}")
    chat_repo.append(conn, session_id, "assistant", content=None, tool_calls=[
        {"id": f"c{i}", "type": "function",
         "function": {"name": "search_exercises", "arguments": "{}"}}
    ])
    chat_repo.append(conn, session_id, "tool", content="{}", tool_call_id=f"c{i}")
    chat_repo.append(conn, session_id, "assistant", content=f"answer {i}")

bad_limits = []
for limit in range(1, 41):
    wire = chat_repo.history(conn, session_id, limit=limit)
    if not wire:
        continue
    if wire[0]["role"] != "user":
        bad_limits.append((limit, "does not start with a user turn"))
        continue
    open_ids: set[str] = set()
    for message in wire:
        if message["role"] == "assistant":
            for call in message.get("tool_calls") or []:
                open_ids.add(call["id"])
        elif message["role"] == "tool":
            if message["tool_call_id"] not in open_ids:
                bad_limits.append((limit, f"orphan tool result {message['tool_call_id']}"))
check("every replay limit produces a valid request", not bad_limits,
      f"broken at {bad_limits[:5]}")
check("trimming actually trims",
      len(chat_repo.history(conn, session_id, limit=8)) <= 8,
      f"got {len(chat_repo.history(conn, session_id, limit=8))}")
check("a limit above the history returns all of it",
      len(chat_repo.history(conn, session_id, limit=500)) == 40)


print("\ntitle is set once, not overwritten")

store = FakeStore()
conn = FakeConn(store)
session_id = chat_repo.create_session(conn)
chat_repo.set_title_if_unset(conn, session_id, "first message")
chat_repo.set_title_if_unset(conn, session_id, "second message")
check("first message wins", store.sessions[session_id]["title"] == "first message",
      f"got {store.sessions[session_id]['title']!r}")

chat_repo.set_title_if_unset(conn, session_id + 100, "no such session")
check("a missing session does not raise", True)


print("\ncall summaries read like sentences")

check("search summary names its filters",
      "Back" in chat_repo.summarize_call(
          "search_exercises", {"target_muscle_group": "Back", "limit": 20}),
      chat_repo.summarize_call("search_exercises",
                               {"target_muscle_group": "Back", "limit": 20}))
check("limit is not shown as a filter",
      "limit" not in chat_repo.summarize_call("search_exercises", {"limit": 20}))
check("empty search still reads sensibly",
      chat_repo.summarize_call("search_exercises", {}) ==
      "Searched the exercise database")
check("unknown tool degrades gracefully",
      chat_repo.summarize_call("mystery_tool", {}) == "Called mystery_tool")
check("result summary reports a tool error",
      "failed" in chat._result_summary("search_exercises", {"error": "boom"}))
check("result summary lists match names",
      "KB Swing" in chat._result_summary(
          "search_exercises", {"total": 9, "matches": [{"name": "KB Swing"}]}))


print("\nchat template renders")

# StrictUndefined turns a mistyped context variable into a hard failure instead of a
# silently blank page — which is the difference between catching it here and catching it
# as a 500 in production.
from jinja2 import Environment, FileSystemLoader, StrictUndefined  # noqa: E402

env = Environment(
    loader=FileSystemLoader(str(REPO / "agent" / "templates")),
    autoescape=True,
    undefined=StrictUndefined,
)
template = env.get_template("chat.html")

empty_context = {
    "session": {"id": 3, "title": None,
                "created_at": "2026-07-31T00:00:00+00:00",
                "updated_at": "2026-07-31T00:00:00+00:00"},
    "sessions": [{"id": 3, "title": None, "turns": 0,
                  "created_at": "2026-07-31T00:00:00+00:00",
                  "updated_at": "2026-07-31T00:00:00+00:00"}],
    "messages": [],
    "profile_complete": False,
    "profile_missing": ["profile"],
    "has_profile": False,
}
try:
    html = template.render(**empty_context)
    rendered = True
    error = ""
except Exception as exc:  # noqa: BLE001
    html, rendered, error = "", False, f"{type(exc).__name__}: {exc}"

check("empty session renders", rendered, error)
check("no unrendered jinja left", rendered and "{{" not in html and "{%" not in html)
check("session id reaches the client", 'data-session-id="3"' in html)
check("missing profile is called out", "No trainee profile yet" in html)
check("example prompts shown on an empty chat", "clubbell" in html)

populated = dict(
    empty_context,
    has_profile=True,
    profile_complete=True,
    messages=[
        {"role": "user", "content": "build me a week",
         "created_at": "2026-07-31T00:00:00+00:00", "model": None},
        {"role": "trace", "content": "Searched exercises — target muscle group: Back",
         "created_at": "2026-07-31T00:00:00+00:00"},
        # Model output lands in the DOM, so autoescaping is a real requirement here.
        {"role": "assistant", "content": "<script>alert(1)</script> here you go",
         "created_at": "2026-07-31T00:00:00+00:00", "model": "fake/chat"},
    ],
)
try:
    html = template.render(**populated)
    rendered = True
    error = ""
except Exception as exc:  # noqa: BLE001
    html, rendered, error = "", False, f"{type(exc).__name__}: {exc}"

check("populated session renders", rendered, error)
check("no profile warning when complete", rendered and "No trainee profile yet" not in html)
check("empty state gone once there are messages", "clubbell" not in html)
check("trace rendered as a trace line", 'class="trace"' in html)
check("model output is escaped, not injected",
      "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html)


print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all chat tests passed")
