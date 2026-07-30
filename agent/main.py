"""Agent service — HTTP surface.

Currently serves the trainee profile form and a health check. The chat interface,
routine generation loop and variation review queue land here in later phases; this
file is the entry point they attach to.

Server-rendered HTML with no build step: the whole UI is two templates and a stylesheet,
which is the right size for a single-user self-hosted tool.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import psycopg
from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import import_api
import profile_repo
from profile_repo import (
    CONTRAINDICATION_KINDS,
    EXPERIENCE_LEVELS,
    GOALS,
    Profile,
    form_vocabulary,
    get_profile,
    save_profile,
)

BASE_DIR = Path(__file__).parent
SIDECAR_DSN = os.environ.get(
    "SIDECAR_DSN", "postgresql://fitness@127.0.0.1:5433/exercise_intel"
)

app = FastAPI(title="AI fitness agent", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Endpoints consumed by wger_import/import_exercises.py, which runs inside the wger
# container and needs no database driver of its own.
app.include_router(import_api.build_router(lambda: SIDECAR_DSN))


@contextmanager
def db():
    conn = psycopg.connect(SIDECAR_DSN)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> JSONResponse:
    """Liveness plus a real dependency check, so an unreachable database is visible
    in the container's health status rather than only on first use."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM exercises")
            exercise_count = cur.fetchone()[0]
        return JSONResponse({"status": "ok", "exercises": exercise_count})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"status": "degraded", "error": f"{type(exc).__name__}: {exc}"},
            status_code=503,
        )


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/profile")


@app.get("/capabilities")
def capabilities():
    """Probe what the configured LLM gateway and models actually support.

    Worth checking before trusting a routine: a provider that silently drops the
    `tools` array produces an agent that appears to work and never searches the
    exercise database. This makes that visible instead of mysterious.

    Also lists the model ids the gateway advertises, since naming differs between
    gateways and a misconfigured id is otherwise a confusing runtime error.
    """
    from llm import LLMClient

    models = {
        "routine": os.environ.get("MODEL_ROUTINE", "anthropic/claude-sonnet-5"),
        "escalation": os.environ.get("MODEL_ROUTINE_ESCALATION", "anthropic/claude-opus-5"),
        "variation": os.environ.get("MODEL_VARIATION", "anthropic/claude-sonnet-5"),
        "critic": os.environ.get("MODEL_CRITIC", "anthropic/claude-sonnet-5"),
    }

    report: dict = {"base_url": os.environ.get("LLM_BASE_URL"), "roles": {}}
    # Distinct model ids only — probing the same id four times is pure cost.
    probed: dict[str, dict] = {}
    for role, model in models.items():
        if model not in probed:
            probed[model] = LLMClient(model=model).preflight().as_dict()
        report["roles"][role] = probed[model]

    first = next(iter(models.values()), None)
    report["advertised_models"] = LLMClient(model=first).list_models() if first else []
    report["ready"] = all(r.get("usable_for_routines") for r in report["roles"].values())
    return JSONResponse(report)


# ---------------------------------------------------------------------------
# Trainee profile form
# ---------------------------------------------------------------------------

@app.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, saved: int = 0):
    with db() as conn:
        profile = get_profile(conn) or Profile()
        vocabulary = form_vocabulary(conn)

    complete, missing = profile.is_complete_enough()
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "profile": profile,
            "vocab": vocabulary,
            "goals": GOALS,
            "experience_levels": EXPERIENCE_LEVELS,
            "contraindication_kinds": CONTRAINDICATION_KINDS,
            "selected_goals": {g["goal"]: g["priority"] for g in profile.goals},
            "current_year": dt.date.today().year,
            "saved": bool(saved),
            "complete": complete,
            "missing": missing,
        },
    )


def _parse_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_list(value: str | None) -> list[str]:
    """Split a comma- or newline-separated free-text field."""
    if not value:
        return []
    parts = [p.strip() for chunk in value.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


@app.post("/profile")
async def profile_save(request: Request):
    form = await request.form()

    profile = Profile(
        id=_parse_int(form.get("profile_id")),
        display_name=(form.get("display_name") or "").strip() or None,
        birth_year=_parse_int(form.get("birth_year")),
        gender=(form.get("gender") or "").strip() or None,
        bodyweight_kg=_parse_float(form.get("bodyweight_kg")),
        height_cm=_parse_int(form.get("height_cm")),
        experience_level=form.get("experience_level") or "novice",
        training_age_months=_parse_int(form.get("training_age_months")),
        sessions_per_week=_parse_int(form.get("sessions_per_week")) or 3,
        minutes_per_session=_parse_int(form.get("minutes_per_session")) or 60,
        available_equipment=form.getlist("available_equipment"),
        dislikes=_parse_list(form.get("dislikes")),
        notes=(form.get("notes") or "").strip() or None,
    )

    # Goals: a checkbox selects the goal, a paired select carries its priority.
    goals: list[tuple[str, int]] = []
    for goal_key, _ in GOALS:
        if form.get(f"goal_{goal_key}"):
            priority = _parse_int(form.get(f"priority_{goal_key}")) or 1
            goals.append((goal_key, max(1, min(priority, 5))))

    # Contraindications arrive as parallel lists from the repeating form rows.
    kinds = form.getlist("ci_kind")
    values = form.getlist("ci_value")
    reasons = form.getlist("ci_reason")
    expiries = form.getlist("ci_expires")
    contraindications: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, kind in enumerate(kinds):
        value = (values[i] if i < len(values) else "").strip()
        # Blank rows are how the UI represents "no restriction here" — skip silently.
        if not kind or not value or kind not in CONTRAINDICATION_KINDS:
            continue
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        contraindications.append({
            "kind": kind,
            "value": value,
            "reason": (reasons[i] if i < len(reasons) else "").strip() or None,
            "expires_on": (expiries[i] if i < len(expiries) else "").strip() or None,
        })

    with db() as conn:
        save_profile(conn, profile, goals, contraindications)

    return RedirectResponse("/profile?saved=1", status_code=303)


def _jsonable(value):
    """Coerce dates, Decimals and sets into JSON-serializable values.

    JSONResponse has no `default=` hook, so conversion happens here rather than at
    encode time — a date in a contraindication expiry would otherwise 500 the endpoint.
    """
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Variation review gate
#
# Generated variations are the one place where the AI can add something permanent to the
# exercise pool, so approval is an explicit human step and the UI is built around
# actually reading the movement rather than clicking through a queue.
# ---------------------------------------------------------------------------

@app.get("/variations", response_class=HTMLResponse)
def variations_page(request: Request, status: str = "pending", flash: str = ""):
    import variations as variations_module

    if status not in ("pending", "approved", "rejected"):
        status = "pending"
    with db() as conn:
        items = variations_module.list_variations(conn, status=status)
        counts = variations_module.counts(conn)
    return templates.TemplateResponse(
        request=request,
        name="variations.html",
        context={"variations": items, "counts": counts, "status": status, "flash": flash},
    )


@app.post("/variations/{variation_id}/decide")
async def variations_decide(variation_id: int, request: Request):
    import variations as variations_module

    form = await request.form()
    decision = form.get("decision")
    note = (form.get("note") or "").strip() or None

    with db() as conn:
        if decision == "approve":
            result = variations_module.approve(conn, variation_id, note)
            message = (
                f"Approved. It is now in the exercise pool but not yet loggable — run the "
                f"wger import to create it in the training app."
                if result.get("ok") else result.get("error", "could not approve")
            )
        elif decision == "reject":
            result = variations_module.reject(conn, variation_id, note)
            message = "Rejected." if result.get("ok") else result.get("error", "could not reject")
        else:
            message = "Unknown decision."

    return RedirectResponse(
        f"/variations?status=pending&flash={quote(message)}", status_code=303
    )


@app.get("/api/variations")
def variations_json(status: Optional[str] = "pending"):
    import variations as variations_module

    with db() as conn:
        return JSONResponse(_jsonable({
            "counts": variations_module.counts(conn),
            "variations": variations_module.list_variations(
                conn, status=status if status != "all" else None
            ),
        }))


@app.post("/api/routine/generate")
def routine_generate(payload: dict = Body(...)):
    """Run the full draft → validate → critic → revise → write pipeline.

    Synchronous and slow by design — a routine involves several model calls plus up to
    ~100 wger requests, so expect tens of seconds. The streaming chat surface in the next
    phase wraps this; this endpoint is what makes the pipeline usable and testable now.

    Body: {"request": "...", "write": true}
    """
    request_text = (payload.get("request") or "").strip()
    if not request_text:
        return JSONResponse({"error": "'request' is required"}, status_code=400)

    import generate

    write = bool(payload.get("write", True))
    wger = None
    if write:
        try:
            from wger_client import WgerClient
            wger = WgerClient()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": f"cannot reach the training app: {exc}"}, status_code=503
            )
    else:
        # Recent-log reading still needs wger even on a dry run; degrade if unavailable
        # rather than refusing, since a routine without log context is still useful.
        try:
            from wger_client import WgerClient
            wger = WgerClient()
        except Exception:  # noqa: BLE001
            wger = None

    with db() as conn:
        result = generate.generate_routine(
            conn,
            wger,
            request_text,
            drafting_model=os.environ.get("MODEL_ROUTINE", "anthropic/claude-sonnet-5"),
            escalation_model=os.environ.get(
                "MODEL_ROUTINE_ESCALATION", "anthropic/claude-opus-5"
            ),
            critic_model=os.environ.get("MODEL_CRITIC", "anthropic/claude-sonnet-5"),
            write=write,
            session_id=payload.get("session_id"),
        )

    return JSONResponse(_jsonable(result.as_dict()),
                        status_code=200 if result.ok else 422)


@app.get("/api/profile")
def profile_json():
    """The same profile the agent reads, exposed for inspection and debugging."""
    with db() as conn:
        profile = get_profile(conn)
        if profile is None:
            return JSONResponse({"error": "no profile saved yet"}, status_code=404)
        complete, missing = profile.is_complete_enough()
        return JSONResponse(_jsonable({
            "profile": {**vars(profile), "age": profile.age},
            "complete_enough": complete,
            "missing": missing,
            "contraindication_map": profile_repo.contraindication_map(profile),
        }))
