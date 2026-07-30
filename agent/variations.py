"""AI-generated exercise variations: validation, staging, review, promotion.

Recombining equipment × posture × grip × movement pattern can produce movements that are
nonsensical or unsafe, and this stack writes into a real training log — so nothing
reaches wger without explicit human approval.

Two distinct dangers, handled separately:

1. **Unsafe or silly movements.** Solved by the review gate: a proposal sits in
   `staged_variations` as `pending` until the trainee approves it.

2. **Poisoned taxonomy.** Subtler and easier to miss. A model that invents
   `"Hip Thrusty Pattern"` or `"Diagonal Plane"` would insert an exercise whose
   attributes match no search filter and no programming rule — it would quietly never be
   selected, or worse, silently skip the validator's pattern and plane checks. So every
   attribute value is checked against the vocabulary that actually exists in the database
   *before* the proposal is even staged, and unusable proposals are rejected at source
   rather than shown to the reviewer as if they were fine.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

# Same base namespace as etl/extract_custom_db.py, so generated UUIDs live in the same
# project-local space and can never collide with an upstream wger.de exercise UUID. The
# "variation:" slug prefix keeps them distinct from imported spreadsheet rows.
UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://github.com/ai-fitness-selfhost/custom-exercise-db",
)

MAPPINGS = json.loads(
    (Path(__file__).resolve().parent.parent / "etl" / "mappings" / "wger_mappings.json")
    .read_text()
)
CATEGORY_MAP: dict[str, int] = MAPPINGS["category"]
MUSCLE_MAP: dict[str, Any] = MAPPINGS["muscle"]
EQUIPMENT_EXISTING: dict[str, int] = MAPPINGS["equipment_existing"]
EQUIPMENT_TO_CREATE: set[str] = set(MAPPINGS["equipment_to_create"]["names"])
DIFFICULTY_RANK: dict[str, int] = MAPPINGS["difficulty_rank"]

VALID_MECHANICS = {"Compound", "Isolation"}
VALID_PLANES = {"Sagittal Plane", "Frontal Plane", "Transverse Plane"}
VALID_LATERALITY = {"Bilateral", "Unilateral", "Contralateral", "Ipsilateral"}
VALID_BODY_REGIONS = {"Upper Body", "Lower Body", "Core", "Full Body"}

# wger requires >= 40 chars for a translation description; a real variation needs more
# than that to be performable from the text alone.
MIN_DESCRIPTION = 60


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized: dict = field(default_factory=dict)


def slugify(name: str) -> str:
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", slug).strip("-").lower()


def load_vocabulary(conn: psycopg.Connection) -> dict[str, set[str]]:
    """Attribute values that actually exist in the exercise database."""
    queries = {
        "movement_patterns": "SELECT DISTINCT unnest(movement_patterns) FROM exercises",
        "posture": "SELECT DISTINCT posture FROM exercises WHERE posture IS NOT NULL",
        "grip": "SELECT DISTINCT grip FROM exercises WHERE grip IS NOT NULL",
        "load_position": (
            "SELECT DISTINCT load_position FROM exercises WHERE load_position IS NOT NULL"
        ),
        "target_muscle_group": (
            "SELECT DISTINCT target_muscle_group FROM exercises "
            "WHERE target_muscle_group IS NOT NULL"
        ),
        "prime_mover_muscle": (
            "SELECT DISTINCT prime_mover_muscle FROM exercises "
            "WHERE prime_mover_muscle IS NOT NULL"
        ),
        "classification": (
            "SELECT DISTINCT classification FROM exercises WHERE classification IS NOT NULL"
        ),
        "primary_equipment": (
            "SELECT DISTINCT primary_equipment FROM exercises "
            "WHERE primary_equipment IS NOT NULL"
        ),
    }
    vocabulary: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        for key, sql in queries.items():
            cur.execute(sql)
            vocabulary[key] = {row[0] for row in cur.fetchall() if row[0]}
    return vocabulary


def _closest(value: str, options: set[str], limit: int = 3) -> list[str]:
    """Cheap suggestion helper for an unrecognized value."""
    import difflib

    return difflib.get_close_matches(value, sorted(options), n=limit, cutoff=0.5)


def validate_proposal(
    proposal: dict,
    vocabulary: dict[str, set[str]],
) -> ValidationResult:
    """Check a proposed variation is usable before it is staged.

    Errors block staging outright. Warnings are shown to the reviewer but do not block —
    an unusual-but-valid combination is exactly what a creative variation looks like.
    """
    errors: list[str] = []
    warnings: list[str] = []
    attributes = proposal.get("attributes") or {}
    normalized: dict = {}

    name = (proposal.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    elif len(name) > 200:
        errors.append("name exceeds 200 characters (wger's limit)")
    normalized["name"] = name

    description = (proposal.get("description") or "").strip()
    if len(description) < MIN_DESCRIPTION:
        errors.append(
            f"description is {len(description)} characters; at least {MIN_DESCRIPTION} "
            "are needed for the movement to be performable from the text"
        )
    normalized["description"] = description

    # --- single-value attributes checked against the real vocabulary ---------
    def check_single(key: str, required: bool, valid: set[str] | None = None):
        raw = attributes.get(key)
        value = (str(raw).strip() if raw is not None else "") or None
        if value is None:
            if required:
                errors.append(f"attributes.{key} is required")
            normalized[key] = None
            return
        allowed = valid if valid is not None else vocabulary.get(key, set())
        if allowed and value not in allowed:
            suggestions = _closest(value, allowed)
            message = f"attributes.{key}={value!r} is not a known value"
            if suggestions:
                message += f" — did you mean {', '.join(repr(s) for s in suggestions)}?"
            (errors if required else warnings).append(message)
            if not required:
                # Drop an unrecognized optional value rather than storing a
                # never-matching string in the database.
                value = None
        normalized[key] = value

    # target_muscle_group is required because without it the variation cannot be mapped
    # to a wger category, and therefore cannot be created in wger at all.
    check_single("target_muscle_group", required=True)
    check_single("prime_mover_muscle", required=True)
    check_single("primary_equipment", required=True)
    check_single("posture", required=False)
    check_single("grip", required=False)
    check_single("load_position", required=False)
    check_single("classification", required=False)
    check_single("body_region", required=True, valid=VALID_BODY_REGIONS)
    check_single("mechanics", required=True, valid=VALID_MECHANICS)
    check_single("laterality", required=True, valid=VALID_LATERALITY)

    if normalized.get("target_muscle_group") and \
            normalized["target_muscle_group"] not in CATEGORY_MAP:
        errors.append(
            f"target_muscle_group {normalized['target_muscle_group']!r} has no wger "
            "category mapping, so this exercise could not be created in the training app"
        )

    equipment = normalized.get("primary_equipment")
    if equipment and equipment not in EQUIPMENT_EXISTING and equipment not in EQUIPMENT_TO_CREATE:
        errors.append(
            f"primary_equipment {equipment!r} is not equipment this system knows about"
        )

    # --- list attributes -----------------------------------------------------
    patterns_raw = attributes.get("movement_patterns") or []
    if isinstance(patterns_raw, str):
        patterns_raw = [patterns_raw]
    patterns = []
    for value in patterns_raw:
        value = str(value).strip()
        if value in vocabulary.get("movement_patterns", set()):
            patterns.append(value)
        else:
            suggestions = _closest(value, vocabulary.get("movement_patterns", set()))
            errors.append(
                f"movement pattern {value!r} is not a known pattern"
                + (f" — did you mean {', '.join(repr(s) for s in suggestions)}?"
                   if suggestions else "")
            )
    if not patterns:
        errors.append(
            "at least one recognized movement_pattern is required, otherwise the "
            "exercise is invisible to programming rules and searches"
        )
    normalized["movement_patterns"] = patterns

    planes_raw = attributes.get("planes_of_motion") or []
    if isinstance(planes_raw, str):
        planes_raw = [planes_raw]
    planes = [str(p).strip() for p in planes_raw if str(p).strip() in VALID_PLANES]
    rejected_planes = [
        str(p).strip() for p in planes_raw if str(p).strip() not in VALID_PLANES
    ]
    for value in rejected_planes:
        errors.append(
            f"plane of motion {value!r} is not valid; must be one of "
            + ", ".join(sorted(VALID_PLANES))
        )
    if not planes:
        errors.append("at least one plane_of_motion is required")
    normalized["planes_of_motion"] = planes

    difficulty = (attributes.get("difficulty") or "").strip() or None
    if difficulty and difficulty not in DIFFICULTY_RANK:
        warnings.append(
            f"difficulty {difficulty!r} is not one of "
            + ", ".join(DIFFICULTY_RANK) + "; it will be left unset"
        )
        difficulty = None
    normalized["difficulty"] = difficulty
    normalized["difficulty_rank"] = DIFFICULTY_RANK.get(difficulty or "")
    if difficulty is None:
        warnings.append(
            "no difficulty tier set, so this exercise will not be matched by "
            "difficulty-filtered searches"
        )

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings,
                            normalized=normalized)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def stage(
    conn: psycopg.Connection,
    proposal: dict,
    model: str,
    vocabulary: dict[str, set[str]] | None = None,
) -> dict:
    """Validate and store a proposal as `pending`. Returns a status dict for the model."""
    vocabulary = vocabulary or load_vocabulary(conn)
    result = validate_proposal(proposal, vocabulary)

    if not result.ok:
        # Not staged: an unusable proposal shown to the reviewer as if it were fine is
        # worse than telling the model to fix it.
        return {
            "staged": False,
            "errors": result.errors,
            "warnings": result.warnings,
            "note": (
                "The variation was NOT queued. Fix the listed problems and propose again, "
                "using only attribute values that appear in search_exercises results."
            ),
        }

    derived_from = [int(i) for i in (proposal.get("derived_from") or [])
                    if str(i).isdigit() or isinstance(i, int)]

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO staged_variations "
            "(name, description, rationale, derived_from, attributes, model) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                result.normalized["name"],
                result.normalized["description"],
                (proposal.get("rationale") or "").strip() or None,
                derived_from,
                json.dumps(result.normalized),
                model,
            ),
        )
        variation_id = cur.fetchone()["id"]
    conn.commit()

    return {
        "staged": True,
        "variation_id": variation_id,
        "warnings": result.warnings,
        "note": (
            "Queued for the trainee to approve or reject. It is NOT a real exercise yet "
            "and must not be used in a routine."
        ),
    }


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def list_variations(
    conn: psycopg.Connection,
    status: str | None = "pending",
    limit: int = 100,
) -> list[dict]:
    sql = (
        "SELECT v.id, v.status, v.name, v.description, v.rationale, v.derived_from, "
        "       v.attributes, v.model, v.reviewer_note, v.reviewed_at, v.created_at, "
        "       v.promoted_exercise_id, e.wger_exercise_id "
        "FROM staged_variations v "
        "LEFT JOIN exercises e ON e.id = v.promoted_exercise_id "
    )
    params: list[Any] = []
    if status:
        sql += "WHERE v.status = %s "
        params.append(status)
    sql += "ORDER BY v.created_at DESC LIMIT %s"
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

    # Resolve the source exercises so the reviewer can see what this was derived from.
    source_ids = sorted({i for r in rows for i in (r["derived_from"] or [])})
    names: dict[int, str] = {}
    if source_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM exercises WHERE id = ANY(%s)", (source_ids,))
            names = {row[0]: row[1] for row in cur.fetchall()}
    for row in rows:
        row["derived_from_names"] = [
            names.get(i, f"#{i} (deleted)") for i in (row["derived_from"] or [])
        ]
        for key in ("reviewed_at", "created_at"):
            if row.get(key):
                row[key] = row[key].isoformat()
    return rows


def counts(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM staged_variations GROUP BY status")
        found = {row[0]: row[1] for row in cur.fetchall()}
    return {status: found.get(status, 0) for status in ("pending", "approved", "rejected")}


def reject(conn: psycopg.Connection, variation_id: int, note: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE staged_variations SET status = 'rejected', reviewer_note = %s, "
            "reviewed_at = now() WHERE id = %s AND status = 'pending' RETURNING id",
            (note, variation_id),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return {"ok": False, "error": "no pending variation with that id"}
    return {"ok": True, "variation_id": variation_id, "status": "rejected"}


def approve(conn: psycopg.Connection, variation_id: int, note: str | None = None) -> dict:
    """Approve a variation and promote it into the exercise pool.

    The promoted exercise has no wger id yet, so it is not loggable until the wger import
    runs. That is deliberate: approval and import are separate steps, and the import is
    already idempotent.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM staged_variations WHERE id = %s AND status = 'pending'",
            (variation_id,),
        )
        variation = cur.fetchone()
        if variation is None:
            return {"ok": False, "error": "no pending variation with that id"}

        attributes = variation["attributes"] or {}
        name = variation["name"]
        base_slug = slugify(name) or f"variation-{variation_id}"
        slug = f"variation-{base_slug}"

        # Deterministic UUID in the project-local namespace, so a re-approval of the same
        # variation cannot create a duplicate exercise.
        exercise_uuid = str(uuid.uuid5(UUID_NAMESPACE, f"variation:{base_slug}"))

        # Provenance is part of the description, permanently. Anyone looking at this
        # exercise in wger months later should be able to tell it was AI-generated.
        approved_on = dt.date.today().isoformat()
        description = (
            variation["description"].rstrip()
            + f"\n\n_AI-generated exercise variation ({variation['model']}), "
              f"reviewed and approved on {approved_on}._"
        )

        category = CATEGORY_MAP.get(attributes.get("target_muscle_group") or "")
        prime = attributes.get("prime_mover_muscle")
        wger_muscle = MUSCLE_MAP.get(prime) if prime else None
        equipment_id = EQUIPMENT_EXISTING.get(attributes.get("primary_equipment") or "")

        cur.execute(
            """
            INSERT INTO exercises (
                uuid, source, slug, name, difficulty, difficulty_rank,
                target_muscle_group, prime_mover_muscle, primary_equipment,
                posture, grip, load_position, movement_patterns, planes_of_motion,
                body_region, mechanics, laterality, classification, description,
                wger_category, wger_muscles, wger_equipment, qc_flags
            ) VALUES (
                %s, 'generated-variation', %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (uuid) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                movement_patterns = EXCLUDED.movement_patterns,
                planes_of_motion = EXCLUDED.planes_of_motion
            RETURNING id
            """,
            (
                exercise_uuid, slug, name,
                attributes.get("difficulty"), attributes.get("difficulty_rank"),
                attributes.get("target_muscle_group"), prime,
                attributes.get("primary_equipment"),
                attributes.get("posture"), attributes.get("grip"),
                attributes.get("load_position"),
                attributes.get("movement_patterns") or [],
                attributes.get("planes_of_motion") or [],
                attributes.get("body_region"), attributes.get("mechanics"),
                attributes.get("laterality"), attributes.get("classification"),
                description,
                category,
                [wger_muscle] if wger_muscle else [],
                [equipment_id] if equipment_id else [],
                ["ai_generated_variation"],
            ),
        )
        exercise_id = cur.fetchone()["id"]

        cur.execute(
            "UPDATE staged_variations SET status = 'approved', reviewer_note = %s, "
            "reviewed_at = now(), promoted_exercise_id = %s WHERE id = %s",
            (note, exercise_id, variation_id),
        )
    conn.commit()

    return {
        "ok": True,
        "variation_id": variation_id,
        "status": "approved",
        "exercise_id": exercise_id,
        "loggable": False,
        "next_step": (
            "Run the wger import to make this exercise loggable in the training app: "
            "docker compose exec -T web python3 manage.py shell "
            "< wger_import/import_exercises.py"
        ),
    }
