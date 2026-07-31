"""JSON Schema for a routine plan, plus the normalizer that expands it.

Two shapes, deliberately:

  **Wire shape** (`ROUTINE_PLAN_SCHEMA`) is what the model produces. Two flat arrays —
  `days` and `entries` — joined by a day number, with progression as flat fields. Nothing
  nests more than one level.

  **Internal shape** (`normalize_plan`) is day -> slots -> entries, matching both wger's
  data model and the way the validator reasons about a session.

The split exists for a concrete reason. A gateway rejected the nested schema outright:

    503 {'code': 'chat_admission_busy', 'reason': 'structure_limit',
         'message': 'Structurally heavy chat request capacity is busy'}

Nesting in the data costs roughly three levels of JSON Schema nesting each, so
plan -> days -> slots -> entries -> progression reached depth 16 as a tool parameter
schema, against depth 7 for the same service's other tools, which were accepted. Going
flat on the wire brings it back to 7 while the code downstream keeps the structure it
wants.

Field length limits mirror wger's own serializers exactly (verified against
`wger.de/api/v2/schema`, v2.7.0a1), so the model cannot produce a plan that only
fails at the final POST:

    routine.name          maxLength 25   <- easy to blow past; the model is told
    routine.description   maxLength 1000
    day.name              maxLength 20
    day.description       maxLength 1000
    slot.comment          maxLength 200
    slot_entry.comment    maxLength 100
"""

from __future__ import annotations

# wger's SlotEntryTypeEnum
SLOT_ENTRY_TYPES = [
    "normal", "warmup", "dropset", "myo", "partial", "forced", "tut", "iso", "jump",
]

# wger's DayTypeEnum. `custom` is a standard strength session; the rest are
# conditioning formats that map well onto the functional-fitness exercise pool.
DAY_TYPES = ["custom", "enom", "amrap", "hiit", "tabata", "edt", "rft", "afap"]

# Matches wger's config `operation` / `step` semantics for progression rules.
PROGRESSION_OPERATIONS = ["+", "-", "r"]  # add, subtract, replace
PROGRESSION_STEPS = ["abs", "percent"]

PROGRESSION_MODELS = ["linear", "double_progression", "autoregulated_rir", "block"]

ROUTINE_PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name", "description", "weeks", "progression_model", "progression_detail",
        "rationale", "days", "entries",
    ],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 25,
            "description": "Routine name. Hard limit of 25 characters — wger rejects longer.",
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "What this routine is for and how to run it.",
        },
        "weeks": {
            "type": "integer",
            "minimum": 1,
            "maximum": 16,
            "description": "Block length in weeks before a deload or re-plan.",
        },
        "progression_model": {
            "type": "string",
            "enum": PROGRESSION_MODELS,
        },
        "progression_detail": {
            "type": "string",
            "minLength": 20,
            "maxLength": 600,
            "description": (
                "Concrete instructions with numbers — increments, when to add load, "
                "when to deload. Not 'add weight when you can'."
            ),
        },
        "rationale": {
            "type": "string",
            "minLength": 40,
            "maxLength": 2000,
            "description": (
                "Why this structure fits the trainee's goals, schedule, experience "
                "level and contraindications. Reviewed by the critic pass."
            ),
        },
        "days": {
            "type": "array",
            "minItems": 1,
            "maxItems": 7,
            "description": "One object per day of the week you are programming.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["order", "name", "is_rest"],
                "properties": {
                    "order": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                        "description": "Day number, 1-7. Referenced by entries.day.",
                    },
                    "name": {"type": "string", "minLength": 1, "maxLength": 20},
                    "description": {"type": "string", "maxLength": 1000},
                    "is_rest": {"type": "boolean"},
                    "type": {
                        "type": "string",
                        "description": "One of: " + ", ".join(DAY_TYPES)
                                       + ". Default custom (a normal session).",
                    },
                },
            },
        },
        "entries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 120,
            "description": (
                "Every exercise in the whole routine, in one flat list. `day` says which "
                "day it belongs to and `slot` orders it within that day. Two entries "
                "sharing the same day AND slot are a superset — that is the only way to "
                "express one."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["day", "slot", "exercise_id", "sets", "reps"],
                "properties": {
                    "day": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                        "description": "Matches a days[].order value.",
                    },
                    "slot": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": (
                            "Position within the day, starting at 1. Same day+slot as "
                            "another entry means they are supersetted."
                        ),
                    },
                    "exercise_id": {
                        "type": "integer",
                        "description": (
                            "id from the sidecar `exercises` table, as returned by "
                            "search_exercises. Must be loggable (has a wger id)."
                        ),
                    },
                    "exercise_name": {
                        "type": "string",
                        "description": (
                            "Echoed back for human review; ignored on write. A mismatch "
                            "with exercise_id is a validation error."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": "One of: " + ", ".join(SLOT_ENTRY_TYPES)
                                       + ". Default normal. Use warmup for warmup "
                                         "sets so they are excluded from volume.",
                    },
                    "sets": {"type": "integer", "minimum": 1, "maximum": 10},
                    "reps": {"type": "integer", "minimum": 1, "maximum": 100},
                    "rir": {"type": "integer", "minimum": 0, "maximum": 6},
                    "rest_seconds": {"type": "integer", "minimum": 0, "maximum": 600},
                    "weight_kg": {"type": "number", "minimum": 0, "maximum": 500},
                    "comment": {"type": "string", "maxLength": 100},
                    "slot_comment": {
                        "type": "string",
                        "maxLength": 200,
                        "description": (
                            "Note attached to the slot rather than the exercise, e.g. "
                            "'superset, no rest between'. Taken from the first entry in "
                            "the slot that sets it."
                        ),
                    },
                    "progress_operation": {
                        "type": "string",
                        "description": "One of: + (add), - (subtract), r (replace).",
                    },
                    "progress_step": {
                        "type": "string",
                        "description": "One of: abs (absolute) or percent.",
                    },
                    "progress_value": {"type": "number"},
                    "progress_every_iterations": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
            },
        },
    },
}

# Entry fields that describe progression rather than the set itself.
_PROGRESSION_FIELDS = {
    "progress_operation": "operation",
    "progress_step": "step",
    "progress_value": "value",
    "progress_every_iterations": "every_iterations",
}

# Entry fields that belong to the slot, not the exercise.
_SLOT_FIELDS = {"day", "slot", "slot_comment"}


def normalize_plan(plan: dict) -> dict:
    """Expand the flat wire shape into day -> slots -> entries.

    Idempotent: a plan that already carries nested `days[].slots` is returned unchanged,
    so stored payloads written before the wire format flattened still load.

    Entries are grouped by (day, slot) and slots are ordered by their number. An entry
    naming a day that `days` does not declare gets that day synthesized rather than being
    dropped — losing prescribed work silently would be worse than a validator complaint
    about an unnamed day.
    """
    if not isinstance(plan, dict):
        return plan
    days_in = plan.get("days") or []
    if any(isinstance(d, dict) and "slots" in d for d in days_in):
        return plan
    if "entries" not in plan:
        return plan

    days: dict[int, dict] = {}
    order: list[int] = []
    for day in days_in:
        number = day.get("order")
        if number is None:
            continue
        days[number] = {
            key: value for key, value in day.items() if key != "slots"
        }
        if days[number].get("type") not in DAY_TYPES:
            days[number].pop("type", None)
        days[number]["slots"] = []
        order.append(number)

    grouped: dict[int, dict[int, list[dict]]] = {}
    for raw in plan.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        day_number = raw.get("day", 1)
        slot_number = raw.get("slot", 1)
        entry = {
            key: value
            for key, value in raw.items()
            if key not in _SLOT_FIELDS and key not in _PROGRESSION_FIELDS
        }
        # The schema states the allowed values in prose rather than an `enum` list,
        # because a nested enum costs two levels of schema depth and depth is what the
        # gateway's structure limit measures. Enforcing here keeps the guarantee: an
        # unrecognized value would otherwise reach wger and fail the whole write.
        if entry.get("type") not in SLOT_ENTRY_TYPES:
            entry.pop("type", None)
        progression = {
            target: raw[source]
            for source, target in _PROGRESSION_FIELDS.items()
            if raw.get(source) is not None
        }
        # wger requires all three to define a rule; a partial one is not actionable, and
        # so is one naming an operation or step wger does not implement.
        if (
            {"operation", "step", "value"} <= set(progression)
            and progression["operation"] in PROGRESSION_OPERATIONS
            and progression["step"] in PROGRESSION_STEPS
        ):
            entry["progression"] = progression
        grouped.setdefault(day_number, {}).setdefault(slot_number, []).append(
            (entry, raw.get("slot_comment"))
        )

    for day_number in grouped:
        if day_number not in days:
            days[day_number] = {
                "order": day_number,
                "name": f"Day {day_number}",
                "is_rest": False,
                "slots": [],
            }
            order.append(day_number)

    for day_number, slots in grouped.items():
        for slot_number in sorted(slots):
            pairs = slots[slot_number]
            comment = next((c for _, c in pairs if c), None)
            slot: dict = {
                "order": slot_number,
                "entries": [entry for entry, _ in pairs],
            }
            if comment:
                slot["comment"] = comment
            days[day_number]["slots"].append(slot)

    out = {key: value for key, value in plan.items() if key != "entries"}
    out["days"] = [days[number] for number in sorted(set(order))]
    # The validator and the critic prompt read `progression`; keep the flat fields too so
    # nothing that inspects the raw model output has to know which shape it got.
    if plan.get("progression_model") or plan.get("progression_detail"):
        out["progression"] = {
            "model": plan.get("progression_model"),
            "detail": plan.get("progression_detail"),
        }
    return out


def schema_depth(obj, depth: int = 0) -> int:
    """Maximum nesting depth of a JSON-serializable structure.

    Exposed because it is the number a gateway's structure limit actually measures, so
    it is worth being able to assert on it in tests.
    """
    if isinstance(obj, dict):
        return max([schema_depth(v, depth + 1) for v in obj.values()] or [depth])
    if isinstance(obj, list):
        return max([schema_depth(v, depth + 1) for v in obj] or [depth])
    return depth


def openrouter_response_format() -> dict:
    """Wrap the schema for a `response_format: json_schema` request."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "routine_plan",
            "strict": True,
            "schema": ROUTINE_PLAN_SCHEMA,
        },
    }
