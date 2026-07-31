"""Conversational coach loop for the web chat UI.

The chat layer deliberately does **not** reimplement routine generation. It runs the
same read tools the generator uses, plus one extra tool — `generate_routine` — which
invokes the tested draft → validate → critic → revise → write pipeline in
`generate.py` and streams its progress into the transcript.

That keeps one pipeline rather than two, and makes "is this actually a request for a
program?" a judgement the coach makes in context. A mode switch in the UI would force
that decision onto you before you have asked the question, and a separate classifier
call would add a round trip and a failure mode to every message.

Tool calling is required for this surface to work at all. If the gateway silently drops
the tools array (see `llm.py`), the coach degrades into a chatbot that answers from
memory and never touches your exercise database or your logs — which looks like it is
working. `/capabilities` is what makes that visible.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import chat_repo
import exercise_search
import tools as tool_module
from llm import LLMClient
from principles import resolve_prescription

log = logging.getLogger(__name__)

# Tools the coach may use while talking. `submit_routine_plan` is deliberately absent:
# submitting a plan is the generator's job, and exposing both would give the model two
# different ways to produce a routine, only one of which is validated.
CONVERSATION_TOOL_NAMES = {
    "get_trainee_profile",
    "get_recent_training",
    "search_exercises",
    "get_exercise_detail",
    "propose_exercise_variation",
}

GENERATE_ROUTINE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_routine",
        "description": (
            "Build a complete, validated training program and save it into the "
            "training app. This runs the full programming pipeline: a drafting pass "
            "with exercise-database search, deterministic checks on weekly volume, "
            "frequency, movement-pattern and plane coverage, exercise order, session "
            "length and contraindications, then a reviewing-coach critique, then "
            "revision until it passes. It takes tens of seconds and costs several "
            "model calls.\n\n"
            "Call this when the trainee wants an actual program, split, or week of "
            "training they can follow. Do NOT call it to answer a question, to "
            "suggest a single exercise, to explain programming, or to discuss a "
            "routine that already exists — answer those yourself."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request"],
            "properties": {
                "request": {
                    "type": "string",
                    "minLength": 20,
                    "description": (
                        "A complete, self-contained statement of what the program must "
                        "be. The pipeline does not see this conversation, so restate "
                        "everything that matters: days per week, session length, "
                        "emphasis, equipment constraints, anything the trainee said "
                        "earlier in the chat, and any preference you inferred. It reads "
                        "the saved profile and recent logs itself, so do not repeat "
                        "those."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "True to design and validate the program without saving it to "
                        "the training app. Use when the trainee explicitly asks to see "
                        "a draft first."
                    ),
                },
            },
        },
    },
}

CHAT_SYSTEM_PROMPT = """\
You are this trainee's strength and conditioning coach. You have their profile, their \
training history, and an exercise database of roughly 4,070 movements including \
unconventional implements — clubbell, macebell, sandbag, sliders, rings, Indian clubs.

How to work:

- Read before you advise. Call get_trainee_profile and get_recent_training when the \
answer depends on who they are or what they have been doing, which is most of the time. \
Do not guess their age, injuries, equipment or schedule.
- Search, do not recall. When recommending specific exercises, call search_exercises. \
The database contains movements you would not otherwise think of, and only ids it \
returns can actually be logged. Never invent an exercise id or name.
- Call generate_routine when they want a real program. Everything else — questions, \
single-exercise suggestions, explaining a principle, discussing a program that already \
exists, troubleshooting a lift — you answer directly.
- Respect declared restrictions absolutely. A contraindication in their profile is a \
hard boundary, not a preference to weigh.

How to talk:

Be direct and specific. Give the reason behind a recommendation, briefly — they are \
training, not studying. Prefer concrete numbers to hedged ranges when you can justify \
them. If something they are doing is a bad idea, say so plainly and say what to do \
instead. If you genuinely do not know, or the answer depends on something you cannot \
see, say that rather than producing a confident guess.

You are not a clinician. For pain, injury or medical questions, give practical training \
guidance — what to avoid, what to substitute — and be clear about where the line is \
instead of diagnosing.

Plain prose and short lists. No markdown headers, no bold, no tables: the chat renders \
as plain text."""


def conversation_tools() -> list[dict]:
    """The tool set for chat: the generator's read tools, plus generate_routine."""
    selected = [
        definition
        for definition in tool_module.TOOL_DEFINITIONS
        if definition.get("function", {}).get("name") in CONVERSATION_TOOL_NAMES
    ]
    return selected + [GENERATE_ROUTINE_TOOL]


class ChatTurn:
    """Result of one user message: what to persist, and what the UI already saw."""

    def __init__(self) -> None:
        self.reply: str = ""
        self.routine: dict | None = None
        self.error: str | None = None


def respond(
    conn,
    wger_client,
    session_id: int,
    user_text: str,
    *,
    chat_model: str,
    drafting_model: str,
    escalation_model: str,
    critic_model: str,
    emit: Callable[[str, dict], None] | None = None,
) -> ChatTurn:
    """Handle one user message end to end.

    Persists the user turn immediately, then the assistant/tool turns the loop
    produced. Persisting the user message first means a model or gateway failure
    loses the reply but never the question.
    """
    turn = ChatTurn()

    def send(kind: str, data: dict) -> None:
        if emit:
            emit(kind, data)

    chat_repo.append(conn, session_id, "user", content=user_text)
    chat_repo.set_title_if_unset(conn, session_id, user_text)

    context = tool_module.ToolContext(
        conn, wger_client, resolve_prescription, model_name=chat_model
    )

    def dispatch(name: str, arguments: dict) -> Any:
        if name == "generate_routine":
            return _run_generation(
                conn,
                wger_client,
                session_id,
                arguments,
                drafting_model=drafting_model,
                escalation_model=escalation_model,
                critic_model=critic_model,
                send=send,
                turn=turn,
            )
        return tool_module.dispatch(context, name, arguments)

    def on_event(kind: str, data: dict) -> None:
        if kind == "tool_call":
            send("tool_call", {
                "name": data.get("name"),
                "summary": chat_repo.summarize_call(
                    data.get("name", ""), data.get("arguments") or {}
                ),
            })
        elif kind == "tool_result":
            send("tool_result", {
                "name": data.get("name"),
                "summary": _result_summary(data.get("name", ""), data.get("result")),
            })

    sent = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    sent += chat_repo.history(conn, session_id)

    client = LLMClient(model=chat_model)
    try:
        conversation, final_text = client.run_tools(
            sent,
            tools=conversation_tools(),
            dispatch=dispatch,
            on_event=on_event,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("chat turn failed")
        turn.error = f"{type(exc).__name__}: {exc}"
        message = (
            "Something went wrong reaching the model: "
            f"{turn.error}. Your message was saved; try again."
        )
        chat_repo.append(conn, session_id, "assistant", content=message,
                         model=chat_model)
        send("assistant", {"content": message})
        turn.reply = message
        return turn

    # Only the turns the loop added — the system prompt and replayed history are
    # already accounted for.
    produced = conversation[len(sent):]
    for message in produced:
        if message.get("role") == "assistant":
            message.setdefault("model", chat_model)
    chat_repo.append_many(conn, session_id, produced)

    turn.reply = final_text or ""
    if not turn.reply.strip():
        # The loop ran out of rounds, or the model ended on a tool call with no prose.
        turn.reply = (
            "I worked through that but did not produce a written answer. "
            "Ask again, more specifically, and I will."
        )
        chat_repo.append(conn, session_id, "assistant", content=turn.reply,
                         model=chat_model)
    send("assistant", {"content": turn.reply})
    return turn


def _run_generation(
    conn,
    wger_client,
    session_id: int,
    arguments: dict,
    *,
    drafting_model: str,
    escalation_model: str,
    critic_model: str,
    send: Callable[[str, dict], None],
    turn: ChatTurn,
) -> dict:
    """Run the real pipeline, forwarding its progress to the UI.

    The pipeline is slow enough that silence reads as a hang, so every stage is
    reported. The value returned here goes back to the chat model, so it is a compact
    summary rather than the full plan — with exercise names resolved, since the chat
    model did not draft the plan and cannot discuss ids it has never seen.
    """
    import generate

    request_text = (arguments or {}).get("request") or ""
    dry_run = bool((arguments or {}).get("dry_run"))
    send("routine_started", {"request": request_text, "dry_run": dry_run})

    def on_event(kind: str, data: dict) -> None:
        send("routine_progress", {"stage": kind, **_progress_detail(kind, data)})

    result = generate.generate_routine(
        conn,
        wger_client,
        request_text,
        drafting_model=drafting_model,
        escalation_model=escalation_model,
        critic_model=critic_model,
        write=not dry_run,
        session_id=session_id,
        on_event=on_event,
    )

    payload = result.as_dict()
    plan = payload.pop("plan", None)
    summary = {
        "ok": payload["ok"],
        "dry_run": dry_run,
        "routine_id": payload["routine_id"],
        "routine_url": payload["routine_url"],
        "iterations": payload["iterations"],
        "error": payload["error"],
        "critic_verdict": (payload.get("critic") or {}).get("verdict"),
        "unresolved_violations": [
            v for v in payload.get("violations", []) if v.get("severity") != "info"
        ][:10],
    }
    if plan:
        summary["program"] = _describe_plan(conn, plan)

    turn.routine = {"ok": summary["ok"], "url": summary["routine_url"],
                    "name": (plan or {}).get("name"), "error": summary["error"]}
    send("routine_finished", turn.routine)
    return summary


def _describe_plan(conn, plan: dict) -> dict:
    """Compact, name-resolved view of a plan for the chat model to talk about."""
    ids = [
        entry["exercise_id"]
        for day in plan.get("days", [])
        for slot in day.get("slots", [])
        for entry in slot.get("entries", [])
        if isinstance(entry.get("exercise_id"), int)
    ]
    names: dict[int, str] = {}
    if ids:
        try:
            for row in exercise_search.get_exercises(conn, ids, detail=False):
                names[row["id"]] = row["name"]
        except Exception as exc:  # noqa: BLE001
            # A naming failure must not sink a routine that was written successfully.
            log.warning("could not resolve exercise names for the summary: %s", exc)

    days = []
    for day in plan.get("days", []):
        if day.get("is_rest"):
            days.append({"name": day.get("name"), "rest": True})
            continue
        entries = []
        for slot in day.get("slots", []):
            for entry in slot.get("entries", []):
                eid = entry.get("exercise_id")
                entries.append({
                    "exercise": names.get(eid, f"#{eid}"),
                    "sets": entry.get("sets"),
                    "reps": entry.get("reps"),
                    "rest_seconds": entry.get("rest_seconds"),
                    "rir": entry.get("rir"),
                })
        days.append({"name": day.get("name"), "entries": entries})

    return {
        "name": plan.get("name"),
        "weeks": plan.get("weeks"),
        "rationale": plan.get("rationale"),
        "progression": plan.get("progression"),
        "days": days,
    }


def _progress_detail(kind: str, data: dict) -> dict:
    """Turn a pipeline event into something worth showing a human."""
    if kind == "iteration_start":
        return {"text": f"Drafting (attempt {data.get('iteration')}) with "
                        f"{data.get('model')}"}
    if kind == "tool_call":
        return {"text": chat_repo.summarize_call(
            data.get("name", ""), data.get("arguments") or {})}
    if kind == "tool_result":
        return {"text": _result_summary(data.get("name", ""), data.get("result"))}
    if kind == "plan_drafted":
        return {"text": f"Drafted “{data.get('name')}” — checking it"}
    if kind == "validated":
        errors, warnings = data.get("errors", 0), data.get("warnings", 0)
        if errors:
            return {"text": f"Checks found {errors} problem(s) — revising"}
        return {"text": f"Passed every programming check"
                        f"{f' ({warnings} warnings)' if warnings else ''}"}
    if kind == "critiqued":
        verdict = data.get("verdict")
        return {"text": "Reviewing coach approved it" if verdict == "approve"
                        else "Reviewing coach asked for changes — revising"}
    if kind == "escalated":
        return {"text": f"Escalating to {data.get('model')}"}
    if kind == "written":
        return {"text": "Saved to the training app"}
    return {"text": kind.replace("_", " ")}


def _result_summary(name: str, result: Any) -> str:
    """One line describing what a tool returned."""
    if isinstance(result, dict) and result.get("error"):
        return f"{name} failed: {result['error']}"
    if name == "search_exercises":
        matches = result.get("matches") if isinstance(result, dict) else None
        if isinstance(matches, list):
            shown = ", ".join(m.get("name", "") for m in matches[:4] if isinstance(m, dict))
            total = result.get("total", len(matches)) if isinstance(result, dict) else len(matches)
            if not matches:
                return "No exercises matched those filters"
            return f"{total} matches — {shown}{'…' if total > 4 else ''}"
        return "Searched the exercise database"
    if name == "get_trainee_profile":
        return "Profile loaded"
    if name == "get_recent_training":
        if isinstance(result, dict):
            sessions = result.get("sessions")
            if isinstance(sessions, list):
                return f"{len(sessions)} recent sessions"
            if result.get("unavailable"):
                return "Training log unavailable"
        return "Training log read"
    if name == "propose_exercise_variation":
        if isinstance(result, dict) and result.get("staged_id"):
            return f"Staged for your review (#{result['staged_id']})"
        return "Variation proposed"
    return f"{name} returned"

