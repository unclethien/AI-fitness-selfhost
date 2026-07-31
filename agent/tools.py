"""Tool definitions and dispatch for the coaching agent.

Six tools. Descriptions are prescriptive about *when* to call each one, not just what it
does — that measurably raises should-call rate, and two of these (the profile and the
logs) must be called before any routine is designed or the output is generic by
construction.

`create_routine` is deliberately NOT a tool the model can call to write directly into
wger. The model proposes a plan; `generate.py` validates it, runs the critic, and only
then writes. Letting the model write unvalidated would defeat the whole design.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import exercise_search
import profile_repo
import routine_schema
import variations

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_trainee_profile",
            "description": (
                "Retrieve the trainee's goals (with priorities), age, experience level, "
                "weekly schedule, available equipment, injuries/contraindications and "
                "known lift benchmarks. ALWAYS call this first, before designing any "
                "routine — without it you are guessing about the person you are "
                "programming for. Also returns the resolved training prescription "
                "(volume landmarks, rep ranges, rest periods) derived from those goals."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_training",
            "description": (
                "Retrieve what the trainee has actually logged recently — exercises, "
                "loads, reps, session dates. Call this after the profile and before "
                "selecting exercises, so the routine accounts for real recent work "
                "rather than repeating it or ignoring accumulated fatigue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How far back to look. Default 28.",
                        "minimum": 1,
                        "maximum": 180,
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_exercises",
            "description": (
                "Search the combined pool of ~4,070 exercises by biomechanical "
                "attributes. This is the main tool for building a routine: call it "
                "several times with different filters to fill each slot deliberately "
                "(e.g. once for a hip hinge, once for a horizontal pull, once for "
                "frontal-plane work). Results are pre-filtered to exclude the trainee's "
                "contraindications and to include only exercises that can actually be "
                "logged, so anything returned is safe to prescribe. Returns at most 60."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "movement_patterns": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "e.g. ['Hip Hinge'], ['Horizontal Pull'], ['Anti-Rotational']. "
                            "Matches any listed pattern."
                        ),
                    },
                    "planes_of_motion": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Sagittal Plane, Frontal Plane, Transverse Plane.",
                    },
                    "equipment": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Restrict to specific implements, e.g. ['Kettlebell', 'Clubbell'].",
                    },
                    "target_muscle_groups": {"type": "array", "items": {"type": "string"}},
                    "body_regions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Upper Body, Lower Body, Core, Full Body.",
                    },
                    "mechanics": {
                        "type": "string", "enum": ["Compound", "Isolation"],
                    },
                    "laterality": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Bilateral, Unilateral, Contralateral, Ipsilateral.",
                    },
                    "force_types": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Push, Pull, Push & Pull.",
                    },
                    "classifications": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "Bodybuilding, Calisthenics, Ballistics, Grinds, Plyometric, "
                            "Balance, Mobility, Olympic Weightlifting, Postural, Animal Flow."
                        ),
                    },
                    "postures": {"type": "array", "items": {"type": "string"}},
                    "difficulty_min": {"type": "integer", "minimum": 1, "maximum": 8},
                    "difficulty_max": {"type": "integer", "minimum": 1, "maximum": 8},
                    "name_contains": {"type": "string"},
                    "exclude_ids": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "Exercise ids already used, to avoid duplicates.",
                    },
                    "require_video": {
                        "type": "boolean",
                        "description": "Only exercises with a demonstration video.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 60},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_exercise_detail",
            "description": (
                "Full attributes plus demonstration video links for specific exercises "
                "by id. Use when a search summary is not enough to decide — for example "
                "to check grip, load position or foot elevation before pairing two "
                "exercises in a superset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise_ids": {
                        "type": "array", "items": {"type": "integer"},
                        "minItems": 1, "maxItems": 20,
                    }
                },
                "required": ["exercise_ids"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_routine_plan",
            "description": (
                "Submit your finished routine for programming review. This does NOT "
                "write to the training app yet: the plan is checked against evidence-"
                "based programming rules (weekly volume per muscle group, frequency, "
                "movement-pattern balance, plane coverage, exercise order, session "
                "length, contraindications) and reviewed by a second coach. If anything "
                "fails you will be told exactly what and asked to revise. Only use "
                "exercise ids returned by search_exercises."
            ),
            "parameters": routine_schema.ROUTINE_PLAN_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_exercise_variation",
            "description": (
                "Propose a NEW exercise variation that does not exist in the database, "
                "by recombining equipment, posture, grip and movement pattern. The "
                "proposal goes to a review queue for the trainee to approve or reject — "
                "it is never added to the training app automatically, and it cannot be "
                "used in a routine until approved. Only use when the trainee explicitly "
                "asks for new or novel variations; prefer real exercises from "
                "search_exercises otherwise."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 200},
                    "description": {
                        "type": "string", "minLength": 60,
                        "description": "Setup and execution, specific enough to perform safely.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this variation is useful and for whom.",
                    },
                    "derived_from": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "Exercise ids this is a variation of.",
                    },
                    "attributes": {
                        "type": "object",
                        "description": (
                            "Taxonomy for the new exercise, using the same vocabulary as "
                            "search_exercises: target_muscle_group, prime_mover_muscle, "
                            "primary_equipment, posture, grip, movement_patterns, "
                            "planes_of_motion, body_region, force_type, mechanics, "
                            "laterality, difficulty."
                        ),
                    },
                },
                "required": ["name", "description", "rationale", "attributes"],
            },
        },
    },
]

# Tools the model may call freely during the conversation. `submit_routine_plan` is
# handled by the orchestrator rather than dispatched, because submitting ends the
# drafting phase and starts review.
TERMINAL_TOOLS = {"submit_routine_plan"}


class ToolContext:
    """Everything the tools need, assembled once per request."""

    def __init__(self, conn, wger_client, prescription_builder: Callable,
                 model_name: str = "unknown"):
        self.conn = conn
        self.wger = wger_client
        self.prescription_builder = prescription_builder
        # Recorded on any staged variation, so a bad proposal can be traced to the
        # model that produced it.
        self.model_name = model_name
        self._profile = None
        # Ids the model has seen, so a routine referencing an unsearched exercise can be
        # caught and explained rather than silently failing at write time.
        self.seen_exercise_ids: set[int] = set()
        self.submitted_plan: dict | None = None
        self.proposed_variations: list[dict] = []

    @property
    def profile(self):
        if self._profile is None:
            self._profile = profile_repo.get_profile(self.conn)
        return self._profile


def dispatch(context: ToolContext, name: str, arguments: dict) -> Any:
    """Execute a tool. Raising is fine — llm.run_tools reports it back to the model."""
    if name == "get_trainee_profile":
        return _get_trainee_profile(context)
    if name == "get_recent_training":
        return _get_recent_training(context, arguments)
    if name == "search_exercises":
        return _search_exercises(context, arguments)
    if name == "get_exercise_detail":
        return _get_exercise_detail(context, arguments)
    if name == "submit_routine_plan":
        # The model emits a flat wire shape (two arrays joined by a day number) because a
        # deeply nested tool schema is rejected by some gateways as "structurally heavy".
        # Expanding it here means the validator, the writer and everything that reads a
        # stored payload keep the nested day -> slots -> entries structure they want, and
        # only this one line knows both shapes exist.
        context.submitted_plan = routine_schema.normalize_plan(arguments)
        return {"status": "received", "note": "plan queued for programming review"}
    if name == "propose_exercise_variation":
        # Validated against the real attribute vocabulary and persisted here rather than
        # held in memory, so a proposal survives the request and reaches the review
        # queue. An unusable proposal is rejected with reasons instead of being staged.
        result = variations.stage(context.conn, arguments, model=context.model_name)
        if result.get("staged"):
            context.proposed_variations.append(arguments)
        return result
    return {"error": f"unknown tool {name!r}"}


def _get_trainee_profile(context: ToolContext) -> dict:
    profile = context.profile
    if profile is None:
        return {
            "profile_exists": False,
            "warning": (
                "No trainee profile has been saved. Ask the trainee to fill in the "
                "profile form, and in the meantime ask them directly for goals, "
                "experience level, available equipment, schedule and any injuries — do "
                "not assume."
            ),
        }

    prescription = context.prescription_builder(
        profile_repo.goal_tuples(profile), profile.age
    )
    complete, missing = profile.is_complete_enough()

    return {
        "profile_exists": True,
        "complete_enough": complete,
        "missing_fields": missing,
        "display_name": profile.display_name,
        "age": profile.age,
        "gender": profile.gender,
        "bodyweight_kg": profile.bodyweight_kg,
        "experience_level": profile.experience_level,
        "goals": profile.goals,
        "sessions_per_week": profile.sessions_per_week,
        "minutes_per_session": profile.minutes_per_session,
        "available_equipment": profile.available_equipment,
        "dislikes": profile.dislikes,
        "notes": profile.notes,
        "contraindications": [
            {k: v for k, v in item.items() if k != "id"}
            for item in profile.contraindications
        ],
        "benchmarks": [
            {"exercise": b.get("exercise_name") or b.get("label"),
             "weight_kg": float(b["weight_kg"]) if b.get("weight_kg") else None,
             "reps": b.get("reps"), "recorded_on": b.get("recorded_on")}
            for b in profile.benchmarks
        ],
        "prescription": {
            "primary_goals": prescription.primary_goals,
            "all_goals": prescription.all_goals,
            "weekly_sets_large_muscles": {
                "min_effective": round(prescription.volume_large.mev, 1),
                "target": round(prescription.volume_large.mav, 1),
                "max_recoverable": round(prescription.volume_large.mrv, 1),
            },
            "weekly_sets_small_muscles": {
                "min_effective": round(prescription.volume_small.mev, 1),
                "target": round(prescription.volume_small.mav, 1),
                "max_recoverable": round(prescription.volume_small.mrv, 1),
            },
            "rep_range": list(prescription.rep_range),
            "rir_range": list(prescription.rir_range),
            "rest_seconds_compound": list(prescription.rest_seconds_compound),
            "rest_seconds_isolation": list(prescription.rest_seconds_isolation),
            "min_frequency_per_muscle_per_week": prescription.min_frequency_per_muscle,
            "min_compound_share_of_sets": prescription.min_compound_share,
            "coaching_notes": prescription.notes,
        },
    }


def _get_recent_training(context: ToolContext, arguments: dict) -> dict:
    days = int(arguments.get("days") or 28)
    if context.wger is None:
        return {
            "available": False,
            "note": "The training app is not reachable, so recent training is unknown.",
        }
    try:
        logs = context.wger.recent_logs(days=days)
        sessions = context.wger.recent_sessions(days=days)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    # Summarize per exercise: a raw log list is mostly repetition and burns context.
    by_exercise: dict[Any, dict] = {}
    for entry in logs:
        key = entry.get("exercise")
        bucket = by_exercise.setdefault(key, {
            "wger_exercise_id": key, "sets_logged": 0,
            "heaviest_kg": None, "most_recent": None,
        })
        bucket["sets_logged"] += 1
        weight = entry.get("weight")
        if weight is not None:
            try:
                weight = float(weight)
                if bucket["heaviest_kg"] is None or weight > bucket["heaviest_kg"]:
                    bucket["heaviest_kg"] = weight
            except (TypeError, ValueError):
                pass
        date = entry.get("date")
        if date and (bucket["most_recent"] is None or date > bucket["most_recent"]):
            bucket["most_recent"] = date

    return {
        "available": True,
        "window_days": days,
        "sessions_logged": len(sessions),
        "total_sets_logged": len(logs),
        "per_exercise": sorted(
            by_exercise.values(), key=lambda r: r["sets_logged"], reverse=True
        )[:40],
        "recent_sessions": sessions[:10],
        "note": (
            "wger_exercise_id here refers to the training app's ids, not the search "
            "ids. Use it to recognize repetition, not as a search argument."
        ),
    }


def _search_exercises(context: ToolContext, arguments: dict) -> dict:
    profile = context.profile
    contraindications = (
        profile_repo.contraindication_map(profile) if profile else None
    )
    available = (
        profile.available_equipment if profile and profile.available_equipment else None
    )

    results = exercise_search.search_exercises(
        context.conn,
        equipment=arguments.get("equipment"),
        available_equipment=available,
        movement_patterns=arguments.get("movement_patterns"),
        planes_of_motion=arguments.get("planes_of_motion"),
        target_muscle_groups=arguments.get("target_muscle_groups"),
        body_regions=arguments.get("body_regions"),
        mechanics=arguments.get("mechanics"),
        laterality=arguments.get("laterality"),
        force_types=arguments.get("force_types"),
        classifications=arguments.get("classifications"),
        postures=arguments.get("postures"),
        difficulty_min=arguments.get("difficulty_min"),
        difficulty_max=arguments.get("difficulty_max"),
        name_contains=arguments.get("name_contains"),
        exclude_ids=arguments.get("exclude_ids"),
        require_video=bool(arguments.get("require_video")),
        contraindications=contraindications,
        limit=int(arguments.get("limit") or 25),
    )
    context.seen_exercise_ids.update(r["id"] for r in results)

    response: dict = {"count": len(results), "exercises": results}
    if not results:
        # An empty result is the most common place a model gets stuck; say what to relax.
        response["note"] = (
            "No exercises matched. Filters are ANDed, so combining several narrow ones "
            "often yields nothing. Try removing the most specific filter (posture or "
            "classification first), widening the difficulty range, or dropping the "
            "equipment restriction. Note that results are already limited to the "
            "trainee's owned equipment and exclude their contraindications."
        )
    return response


def _get_exercise_detail(context: ToolContext, arguments: dict) -> dict:
    ids = [int(i) for i in arguments.get("exercise_ids") or []]
    found = exercise_search.get_exercises(context.conn, ids, detail=True)
    context.seen_exercise_ids.update(found)
    missing = [i for i in ids if i not in found]
    result: dict = {"exercises": list(found.values())}
    if missing:
        result["not_found"] = missing
        result["note"] = "Those ids do not exist. Use ids returned by search_exercises."
    return result
