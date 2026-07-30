"""Routine generation loop: draft → validate → critic → revise → write.

This is the Option C pipeline. The ordering is deliberate:

  1. **Draft** with tools. The model must fetch the profile and recent logs before it can
     search exercises sensibly, so those tools are described as mandatory-first.
  2. **Deterministic validation.** Code, not a model, checks weekly volume per muscle
     group, frequency, movement-pattern and plane coverage, exercise order, session
     length, contraindications and variety. Errors are non-negotiable.
  3. **Critic pass.** A second model call framed as reviewing *another coach's* work,
     which catches judgement errors a rule checker cannot — "technically balanced but a
     stupid order for someone with this knee history".
  4. **Revise** with the violations and critique as explicit instructions.
  5. **Write** only once clean, and only through the validated writer.

Escalation: repeated failure switches to a stronger model rather than looping on the same
one, because a model that produced an invalid plan twice usually produces it a third time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import exercise_search
import profile_repo
import tools as tool_module
import routine_schema
from principles import resolve_prescription
from validate import (
    TraineeContext,
    Violation,
    as_revision_prompt,
    summarize,
    validate,
)
from llm import LLMClient

log = logging.getLogger(__name__)

MAX_REVISIONS = 3

DRAFTING_SYSTEM_PROMPT = """\
You are the head strength and conditioning coach for a single trainee, writing programs \
that will be loaded into their training app and actually performed.

Work in this order, without exception:
1. Call get_trainee_profile. It returns their goals, experience level, schedule, \
equipment, injuries and a resolved training prescription (weekly volume landmarks, rep \
ranges, rest periods). Program to that prescription.
2. Call get_recent_training. Account for what they have actually been doing.
3. Call search_exercises repeatedly — once per slot you intend to fill, with filters \
chosen for that slot's job. Do not design the routine from memory: the database has \
around 4,070 exercises including unconventional implements (clubbell, macebell, \
sandbag, sliders, rings) that you will not otherwise consider. Exercise selection is \
where this program earns its keep.
4. Call submit_routine_plan when the routine is complete.

Programming requirements your plan will be mechanically checked against:
- Weekly sets per muscle group inside the prescribed min/max range.
- Each trained muscle group hit at least the prescribed number of days per week.
- Fundamental patterns covered across the week: a squat pattern, a hinge, pressing, \
pulling, and direct core work.
- All three planes of motion represented when the goal includes general fitness.
- Compound movements before isolation within each session.
- Every entry given explicit sets, reps and rest. Rest is part of the prescription.
- No exercise matching a declared contraindication.
- Sessions that fit the stated time budget.
- A named progression model with concrete numbers, not "add weight when you can".

Only use exercise ids returned by search_exercises. Never invent an id. The routine name \
must be 25 characters or fewer and day names 20 or fewer — these are hard limits in the \
training app.

Explain your reasoning in the plan's `rationale` field: why this split, why these \
exercises, how it serves their specific goals and works around their specific \
restrictions."""

CRITIC_SYSTEM_PROMPT = """\
You are an experienced strength coach reviewing a program another coach wrote for a \
client. You did not write it and have no stake in defending it.

You are given the client's profile and the proposed program. It has already passed \
automated checks on volume, frequency, balance and session length, so do not re-derive \
those numbers. Your job is the judgement a rule checker cannot make:

- Does the exercise selection actually suit this client, or is it generic work that \
happens to satisfy the constraints?
- Is the session order sensible given their injuries and experience?
- Are the prescribed loads and progressions realistic for their level and benchmarks?
- Is anything unsafe or ill-advised for this specific person?
- Is the program coherent as a block, or a collection of individually-defensible slots?
- Does it match what the client asked for?

Be specific and concrete. Approve a program that is genuinely good; do not manufacture \
objections to look thorough. If you would sign your name to it, approve it."""

CRITIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "assessment"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "assessment": {
            "type": "string", "minLength": 40, "maxLength": 2000,
            "description": "Your overall read on the program.",
        },
        "required_changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete changes needed. Empty when approving.",
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class GenerationResult:
    ok: bool
    plan: dict | None = None
    routine_id: int | None = None
    routine_url: str | None = None
    iterations: int = 0
    violations: list[dict] = field(default_factory=list)
    critic: dict | None = None
    strategy_notes: list[str] = field(default_factory=list)
    error: str | None = None
    transcript: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "routine_id": self.routine_id,
            "routine_url": self.routine_url,
            "iterations": self.iterations,
            "violations": self.violations,
            "critic": self.critic,
            "strategy_notes": self.strategy_notes,
            "error": self.error,
            "plan": self.plan,
        }


def build_trainee_context(conn, profile) -> TraineeContext:
    """Assemble the validator's view of the trainee from the saved profile."""
    if profile is None:
        # No profile: validate against a neutral general-fitness prescription rather
        # than skipping validation, which would be the worse failure.
        return TraineeContext(prescription=resolve_prescription([("general_fitness", 1)]))

    return TraineeContext(
        prescription=resolve_prescription(
            profile_repo.goal_tuples(profile), profile.age
        ),
        experience_level=profile.experience_level,
        age=profile.age,
        sessions_per_week=profile.sessions_per_week,
        minutes_per_session=profile.minutes_per_session,
        available_equipment=set(profile.available_equipment or []) or None,
        contraindications=profile_repo.contraindication_map(profile),
        dislikes=set(profile.dislikes or []),
        recent_exercise_ids=exercise_search.recent_routine_exercise_ids(conn, last_n=2),
    )


def _critique(
    critic: LLMClient,
    profile_summary: dict,
    plan: dict,
) -> tuple[dict, str]:
    messages = [
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "Client profile:\n"
            + json.dumps(profile_summary, indent=2, default=str)
            + "\n\nProposed program:\n"
            + json.dumps(plan, indent=2, default=str)
        )},
    ]
    return critic.structured(messages, CRITIC_SCHEMA, schema_name="program_review")


def generate_routine(
    conn,
    wger_client,
    request: str,
    *,
    drafting_model: str,
    escalation_model: str,
    critic_model: str,
    write: bool = True,
    session_id: int | None = None,
    on_event: Callable[[str, dict], None] | None = None,
) -> GenerationResult:
    """Run the full pipeline for one routine request."""
    result = GenerationResult(ok=False)

    def emit(kind: str, data: dict) -> None:
        if on_event:
            on_event(kind, data)

    context = tool_module.ToolContext(
        conn, wger_client, resolve_prescription, model_name=drafting_model
    )
    profile = context.profile
    trainee_context = build_trainee_context(conn, profile)

    client = LLMClient(model=drafting_model)
    messages = [
        {"role": "system", "content": DRAFTING_SYSTEM_PROMPT},
        {"role": "user", "content": request},
    ]

    for iteration in range(1, MAX_REVISIONS + 2):
        result.iterations = iteration
        emit("iteration_start", {"iteration": iteration, "model": client.model})

        # --- draft ----------------------------------------------------------
        context.submitted_plan = None
        try:
            messages, final_text = client.run_tools(
                messages,
                tools=tool_module.TOOL_DEFINITIONS,
                dispatch=lambda name, args: tool_module.dispatch(context, name, args),
                on_event=lambda kind, data: emit(kind, data),
            )
        except Exception as exc:  # noqa: BLE001
            result.error = f"drafting failed: {type(exc).__name__}: {exc}"
            log.exception("drafting failed")
            return result

        plan = context.submitted_plan
        if plan is None:
            # The model talked instead of submitting. One nudge, then give up — this is
            # usually a sign the request was conversational, not a routine request.
            if iteration > MAX_REVISIONS:
                result.error = (
                    "The model never submitted a routine plan. Last reply: "
                    + (final_text or "")[:500]
                )
                return result
            messages.append({"role": "user", "content": (
                "You have not submitted a routine yet. Call submit_routine_plan with the "
                "complete plan now, or say plainly what information you still need."
            )})
            continue

        result.plan = plan
        emit("plan_drafted", {"iteration": iteration, "name": plan.get("name")})

        # --- deterministic validation ---------------------------------------
        referenced = [
            entry["exercise_id"]
            for day in plan.get("days", [])
            for slot in day.get("slots", [])
            for entry in slot.get("entries", [])
            if isinstance(entry.get("exercise_id"), int)
        ]
        exercises = exercise_search.get_exercises(conn, referenced, detail=False)
        violations = validate(plan, exercises, trainee_context)
        report = summarize(violations)
        result.violations = report["violations"]
        emit("validated", {
            "iteration": iteration,
            "errors": report["errors"],
            "warnings": report["warnings"],
        })

        # --- critic pass ------------------------------------------------------
        critic_result: dict | None = None
        if report["passed"]:
            try:
                critic = LLMClient(model=critic_model)
                critic_result, strategy = _critique(
                    critic,
                    {
                        "goals": profile.goals if profile else [],
                        "age": profile.age if profile else None,
                        "experience_level": profile.experience_level if profile else None,
                        "sessions_per_week": profile.sessions_per_week if profile else None,
                        "minutes_per_session": profile.minutes_per_session if profile else None,
                        "available_equipment": profile.available_equipment if profile else [],
                        "contraindications": profile.contraindications if profile else [],
                        "benchmarks": profile.benchmarks if profile else [],
                        "original_request": request,
                    },
                    plan,
                )
                result.critic = critic_result
                result.strategy_notes.append(f"critic structured output via {strategy}")
                emit("critiqued", {
                    "iteration": iteration,
                    "verdict": critic_result.get("verdict"),
                })
            except Exception as exc:  # noqa: BLE001
                # A failed critic must not block a routine that already passed every
                # deterministic check — degrade rather than deny.
                log.warning("critic pass failed: %s", exc)
                result.strategy_notes.append(f"critic unavailable ({type(exc).__name__})")
                critic_result = {"verdict": "approve",
                                 "assessment": "Critic review unavailable."}
                result.critic = critic_result

        # --- accept or revise -------------------------------------------------
        approved = report["passed"] and (critic_result or {}).get("verdict") == "approve"
        if approved:
            break

        if iteration > MAX_REVISIONS:
            result.error = (
                f"Routine still failing review after {MAX_REVISIONS} revisions "
                f"({report['errors']} errors)."
            )
            return result

        instructions = [as_revision_prompt(violations)]
        if critic_result and critic_result.get("verdict") == "revise":
            changes = critic_result.get("required_changes") or []
            instructions.append(
                "## REVIEWING COACH\n"
                + (critic_result.get("assessment") or "")
                + ("\n\nRequired changes:\n" + "\n".join(f"- {c}" for c in changes)
                   if changes else "")
            )
        messages.append({"role": "user", "content": "\n\n".join(instructions)})

        # Escalate rather than repeat: a model that failed twice rarely succeeds on the
        # third identical attempt.
        if iteration >= 2 and client.model != escalation_model:
            client = LLMClient(model=escalation_model)
            result.strategy_notes.append(
                f"escalated to {escalation_model} after {iteration} failed reviews"
            )
            emit("escalated", {"model": escalation_model})

    # --- write ---------------------------------------------------------------
    if not write:
        result.ok = True
        result.strategy_notes.append("dry run: plan not written to the training app")
        return result

    if wger_client is None:
        result.error = "the training app is not reachable, so the routine was not saved"
        return result

    id_map = exercise_search.wger_id_map(conn, referenced)
    missing = [i for i in set(referenced) if i not in id_map]
    if missing:
        result.error = (
            f"{len(missing)} selected exercises have not been imported into the training "
            f"app and cannot be logged: {missing[:10]}. Run the wger import."
        )
        return result

    try:
        written = wger_client.create_routine(result.plan, id_map)
    except Exception as exc:  # noqa: BLE001
        result.error = f"writing the routine failed: {exc}"
        log.exception("routine write failed")
        return result

    result.routine_id = written["routine_id"]
    result.routine_url = written["url"]
    result.ok = True
    emit("written", written)

    _record(conn, result, request, session_id, drafting_model)
    return result


def _record(conn, result: GenerationResult, request: str, session_id, model: str) -> None:
    """Persist the routine and its review history so quality is auditable over time."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO generated_routines "
                "(wger_routine_id, session_id, name, request_summary, payload, model) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (result.routine_id, session_id, (result.plan or {}).get("name", "")[:200],
                 request[:1000], json.dumps(result.plan), model),
            )
            routine_row_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO routine_reviews "
                "(generated_routine_id, iteration, violations, critic_model, "
                " critic_verdict, critic_notes) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (routine_row_id, result.iterations, json.dumps(result.violations),
                 model, (result.critic or {}).get("verdict"),
                 (result.critic or {}).get("assessment")),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        # Bookkeeping must never lose a routine the user already has.
        log.warning("could not record generated routine: %s", exc)
