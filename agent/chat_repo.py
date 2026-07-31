"""Chat session persistence.

Messages are stored in the OpenAI wire format — `tool_calls` and `tool_call_id`
preserved verbatim — so a conversation replays to the model exactly as it happened
rather than being reconstructed from a lossy summary. Reconstruction is where
multi-turn tool conversations usually break: drop the `tool_calls` from an assistant
turn and the paired tool result becomes an orphan the API rejects.

Lives in the sidecar rather than in wger so wger's schema stays untouched and wiping
the AI layer can never touch training data.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

# How many stored messages to replay. Tool results here are large (an exercise search
# returns dozens of rows), so a long session would otherwise grow the request without
# bound even on a million-token model — and cost scales with every resent token.
DEFAULT_REPLAY_LIMIT = 60

# Titles are derived from the first user message; long enough to tell sessions apart
# in a list, short enough not to wrap.
TITLE_LENGTH = 60


def create_session(conn: psycopg.Connection, title: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_sessions (title) VALUES (%s) RETURNING id", (title,)
        )
        session_id = cur.fetchone()[0]
    conn.commit()
    return session_id


def list_sessions(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    """Sessions newest-first, with the message count so empty ones are recognizable."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at,
                   count(m.id) FILTER (WHERE m.role IN ('user', 'assistant')) AS turns
              FROM chat_sessions s
              LEFT JOIN chat_messages m ON m.session_id = s.id
             GROUP BY s.id
             ORDER BY s.updated_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        return [_iso_dates(row) for row in cur.fetchall()]


def get_session(conn: psycopg.Connection, session_id: int) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, title, created_at, updated_at FROM chat_sessions WHERE id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        return _iso_dates(row) if row else None


def delete_session(conn: psycopg.Connection, session_id: int) -> None:
    """Messages go with it — the FK is ON DELETE CASCADE."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chat_sessions WHERE id = %s", (session_id,))
    conn.commit()


def append(
    conn: psycopg.Connection,
    session_id: int,
    role: str,
    *,
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_messages
                (session_id, role, content, tool_calls, tool_call_id, model,
                 prompt_tokens, completion_tokens)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                session_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                model,
                prompt_tokens,
                completion_tokens,
            ),
        )
        message_id = cur.fetchone()[0]
        # Drives the session list ordering, so it has to move on every message.
        cur.execute(
            "UPDATE chat_sessions SET updated_at = now() WHERE id = %s", (session_id,)
        )
    conn.commit()
    return message_id


def append_many(conn: psycopg.Connection, session_id: int, messages: list[dict]) -> None:
    """Persist a run of wire-format messages produced by one tool loop.

    Takes the messages exactly as the loop appended them, so ordering — and therefore
    the assistant/tool pairing — survives.
    """
    for message in messages:
        append(
            conn,
            session_id,
            message.get("role", "assistant"),
            content=_as_text(message.get("content")),
            tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"),
            model=message.get("model"),
        )


def _as_text(content: Any) -> str | None:
    """Content is normally a string, but some gateways return the array form."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts) or json.dumps(content, default=str)
    return json.dumps(content, default=str)


def _rows(conn: psycopg.Connection, session_id: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, role, content, tool_calls, tool_call_id, model, created_at
              FROM chat_messages
             WHERE session_id = %s
             ORDER BY id
            """,
            (session_id,),
        )
        return list(cur.fetchall())


def history(
    conn: psycopg.Connection,
    session_id: int,
    limit: int = DEFAULT_REPLAY_LIMIT,
) -> list[dict]:
    """The conversation in OpenAI wire format, ready to send.

    Trimming is group-aware. A naive "last N messages" slice can begin in the middle
    of a tool exchange, leaving `role: "tool"` messages whose originating assistant
    turn is gone — the API rejects that with an invalid-request error rather than
    tolerating it. So the cut point is walked back to a message that can legally start
    a request.
    """
    rows = _rows(conn, session_id)
    if len(rows) > limit:
        start = len(rows) - limit
        # A tool result cannot lead, and neither can an assistant turn that only
        # carries tool_calls; back up until the first message is a plain user turn.
        while start > 0 and rows[start]["role"] != "user":
            start -= 1
        rows = rows[start:]

    wire: list[dict] = []
    for row in rows:
        message: dict[str, Any] = {"role": row["role"]}
        if row["role"] == "tool":
            # Both fields are required for the pairing; a tool result with neither is
            # unusable, so drop it rather than send something the API will reject.
            if not row["tool_call_id"]:
                continue
            message["tool_call_id"] = row["tool_call_id"]
            message["content"] = row["content"] or ""
        else:
            message["content"] = row["content"]
            if row["tool_calls"]:
                message["tool_calls"] = row["tool_calls"]
        wire.append(message)

    # Any orphaned tool results left at the front by an unusual history are dropped
    # here as a last resort, for the same reason.
    while wire and wire[0]["role"] == "tool":
        wire.pop(0)
    return wire


def visible_messages(conn: psycopg.Connection, session_id: int) -> list[dict]:
    """What the transcript shows: the human-readable turns, plus a one-line trace of
    each tool the coach used.

    Tool activity is summarized rather than hidden. Which exercises were searched, and
    whether the profile was actually read, is the difference between a program built
    from the database and one improvised from the model's memory — worth being able
    to see.
    """
    out: list[dict] = []
    for row in _rows(conn, session_id):
        role = row["role"]
        if role == "tool":
            continue
        if role == "assistant" and not (row["content"] or "").strip():
            # A pure tool-call turn has no prose; represent it by the calls it made.
            for call in row["tool_calls"] or []:
                out.append({
                    "role": "trace",
                    "content": _describe_call(call),
                    "created_at": _iso(row["created_at"]),
                })
            continue
        if role == "system":
            continue
        out.append({
            "role": role,
            "content": row["content"] or "",
            "model": row["model"],
            "created_at": _iso(row["created_at"]),
        })
    return out


def _describe_call(call: dict) -> str:
    function = call.get("function") or {}
    name = function.get("name", "tool")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    return summarize_call(name, arguments)


def summarize_call(name: str, arguments: dict) -> str:
    """One line describing a tool call, for the transcript and the live stream.

    Shared by both so a call reads identically while it happens and afterwards.
    """
    if name == "search_exercises":
        filters = {
            key: value
            for key, value in (arguments or {}).items()
            if value not in (None, "", [], {}) and key != "limit"
        }
        if not filters:
            return "Searched the exercise database"
        rendered = ", ".join(
            f"{key.replace('_', ' ')}: {_short(value)}" for key, value in filters.items()
        )
        return f"Searched exercises — {rendered}"
    if name == "get_trainee_profile":
        return "Read your profile and training prescription"
    if name == "get_recent_training":
        days = (arguments or {}).get("days")
        return f"Read your recent training log{f' ({days} days)' if days else ''}"
    if name == "get_exercise_detail":
        return "Looked up exercise details"
    if name == "propose_exercise_variation":
        return f"Proposed a new exercise variation: {(arguments or {}).get('name', '')}"
    if name == "generate_routine":
        return "Started building a full program"
    return f"Called {name}"


def _short(value: Any, width: int = 60) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    else:
        text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def set_title_if_unset(conn: psycopg.Connection, session_id: int, text: str) -> None:
    """Name a session after its opening message, and only then — a later rename by
    the user must not be overwritten by the next message."""
    title = " ".join(text.split())[:TITLE_LENGTH].strip()
    if not title:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chat_sessions SET title = %s WHERE id = %s AND title IS NULL",
            (title, session_id),
        )
    conn.commit()


def _iso(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _iso_dates(row: dict | None) -> dict:
    return {key: _iso(value) for key, value in (row or {}).items()}
