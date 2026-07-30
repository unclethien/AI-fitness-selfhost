"""JSON Schema for a routine plan.

One schema, three jobs:

  1. Constrains the model's output (OpenRouter `response_format: json_schema`, or the
     `create_routine` tool's parameter schema).
  2. Is the input contract for the programming validator.
  3. Is translated into wger API calls by wger_writer.py.

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

ROUTINE_PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name", "description", "weeks", "progression", "rationale", "days",
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
        "progression": {
            "type": "object",
            "additionalProperties": False,
            "required": ["model", "detail"],
            "properties": {
                "model": {
                    "type": "string",
                    "enum": ["linear", "double_progression", "autoregulated_rir", "block"],
                },
                "detail": {
                    "type": "string",
                    "minLength": 20,
                    "maxLength": 600,
                    "description": (
                        "Concrete instructions with numbers — increments, when to add "
                        "load, when to deload. Not 'add weight when you can'."
                    ),
                },
            },
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
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["order", "name", "is_rest", "slots"],
                "properties": {
                    "order": {"type": "integer", "minimum": 1, "maximum": 7},
                    "name": {"type": "string", "minLength": 1, "maxLength": 20},
                    "description": {"type": "string", "maxLength": 1000},
                    "is_rest": {"type": "boolean"},
                    "type": {"type": "string", "enum": DAY_TYPES},
                    "slots": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["order", "entries"],
                            "properties": {
                                "order": {"type": "integer", "minimum": 1},
                                "comment": {"type": "string", "maxLength": 200},
                                "entries": {
                                    # More than one entry in a slot IS a superset in
                                    # wger's data model — that is the only way to
                                    # express one.
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["exercise_id", "sets", "reps"],
                                        "properties": {
                                            "exercise_id": {
                                                "type": "integer",
                                                "description": (
                                                    "id from the sidecar `exercises` "
                                                    "table. Must be a loggable "
                                                    "exercise (has a wger id)."
                                                ),
                                            },
                                            "exercise_name": {
                                                "type": "string",
                                                "description": (
                                                    "Echoed back for human review; "
                                                    "ignored on write. Mismatch with "
                                                    "exercise_id is a validation error."
                                                ),
                                            },
                                            "type": {
                                                "type": "string",
                                                "enum": SLOT_ENTRY_TYPES,
                                            },
                                            "sets": {
                                                "type": "integer",
                                                "minimum": 1,
                                                "maximum": 10,
                                            },
                                            "reps": {
                                                "type": "integer",
                                                "minimum": 1,
                                                "maximum": 100,
                                            },
                                            "rir": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 6,
                                            },
                                            "rest_seconds": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 600,
                                            },
                                            "weight_kg": {
                                                "type": "number",
                                                "minimum": 0,
                                                "maximum": 500,
                                            },
                                            "comment": {
                                                "type": "string",
                                                "maxLength": 100,
                                            },
                                            "progression": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "required": ["operation", "step", "value"],
                                                "properties": {
                                                    "operation": {
                                                        "type": "string",
                                                        "enum": PROGRESSION_OPERATIONS,
                                                    },
                                                    "step": {
                                                        "type": "string",
                                                        "enum": PROGRESSION_STEPS,
                                                    },
                                                    "value": {"type": "number"},
                                                    "every_iterations": {
                                                        "type": "integer",
                                                        "minimum": 1,
                                                        "maximum": 8,
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def openrouter_response_format() -> dict:
    """Wrap the schema for OpenRouter's structured-outputs parameter."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "routine_plan",
            "strict": True,
            "schema": ROUTINE_PLAN_SCHEMA,
        },
    }
