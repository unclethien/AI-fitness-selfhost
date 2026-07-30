"""Exercise search over the sidecar store.

This is the agent's retrieval surface and the reason the sidecar exists: filtering by
movement pattern, plane of motion, posture, laterality and difficulty tier is what makes
exercise selection defensible rather than name-matching. None of these attributes exist
in wger's own data model.

Two rules enforced here rather than trusted to the model:

  1. Only exercises with a wger id are returned by default. An exercise that was never
     imported cannot be logged, so putting one in a routine is a dead end.
  2. Contraindications are applied in SQL. Relying on the model to remember an injury
     across a long tool-calling session is exactly the kind of thing that fails quietly;
     a contraindicated exercise should never appear in a candidate list at all.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

# Columns returned for a search hit. Deliberately not SELECT * — the full row includes a
# multi-hundred-character description that would flood the model's context when 40
# candidates come back.
SUMMARY_COLUMNS = """
    id, name, difficulty, difficulty_rank, target_muscle_group, prime_mover_muscle,
    secondary_muscle, primary_equipment, secondary_equipment, posture, grip,
    load_position, arm_involvement, laterality, movement_patterns, planes_of_motion,
    body_region, force_type, mechanics, classification, is_combo, wger_exercise_id
"""

DETAIL_COLUMNS = SUMMARY_COLUMNS + """
    , tertiary_muscle, arm_action, leg_action, foot_elevation, primary_items,
    secondary_items, description, video_demo_url, video_explain_url, qc_flags, source
"""

MAX_LIMIT = 60


def _apply_contraindications(
    where: list[str],
    params: dict[str, Any],
    contraindications: dict[str, set[str]] | None,
) -> None:
    """Translate the profile's contraindication map into SQL exclusions."""
    if not contraindications:
        return

    simple = {
        "equipment": ("primary_equipment", "secondary_equipment"),
        "posture": ("posture",),
        "body_region": ("body_region",),
        "classification": ("classification",),
    }
    for kind, columns in simple.items():
        values = contraindications.get(kind)
        if not values:
            continue
        key = f"ci_{kind}"
        params[key] = list(values)
        for column in columns:
            where.append(f"({column} IS NULL OR {column} <> ALL(%({key})s))")

    if contraindications.get("movement_pattern"):
        params["ci_patterns"] = list(contraindications["movement_pattern"])
        # && is array overlap: exclude anything sharing a pattern with the block list.
        where.append("NOT (movement_patterns && %(ci_patterns)s::text[])")

    if contraindications.get("plane_of_motion"):
        params["ci_planes"] = list(contraindications["plane_of_motion"])
        where.append("NOT (planes_of_motion && %(ci_planes)s::text[])")

    if contraindications.get("exercise"):
        ids = [int(v) for v in contraindications["exercise"] if str(v).isdigit()]
        if ids:
            params["ci_exercise_ids"] = ids
            where.append("id <> ALL(%(ci_exercise_ids)s)")

    ceilings = contraindications.get("max_difficulty")
    if ceilings:
        numeric = [int(v) for v in ceilings if str(v).isdigit()]
        if numeric:
            params["ci_max_difficulty"] = min(numeric)
            where.append(
                "(difficulty_rank IS NULL OR difficulty_rank <= %(ci_max_difficulty)s)"
            )


def search_exercises(
    conn: psycopg.Connection,
    *,
    equipment: list[str] | None = None,
    available_equipment: list[str] | None = None,
    movement_patterns: list[str] | None = None,
    planes_of_motion: list[str] | None = None,
    target_muscle_groups: list[str] | None = None,
    body_regions: list[str] | None = None,
    mechanics: str | None = None,
    laterality: list[str] | None = None,
    force_types: list[str] | None = None,
    classifications: list[str] | None = None,
    postures: list[str] | None = None,
    difficulty_min: int | None = None,
    difficulty_max: int | None = None,
    name_contains: str | None = None,
    exclude_ids: list[int] | None = None,
    require_video: bool = False,
    loggable_only: bool = True,
    contraindications: dict[str, set[str]] | None = None,
    limit: int = 25,
) -> list[dict]:
    """Filter the combined exercise pool. Every argument is optional and ANDed together.

    `equipment` restricts to specific implements the caller wants used;
    `available_equipment` restricts to what the trainee owns. Both can apply at once —
    "kettlebell exercises, but only if I own the kettlebell".
    """
    where: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(limit, MAX_LIMIT))}

    if loggable_only:
        where.append("wger_exercise_id IS NOT NULL")

    if equipment:
        params["equipment"] = equipment
        where.append(
            "(primary_equipment = ANY(%(equipment)s) OR "
            " secondary_equipment = ANY(%(equipment)s))"
        )

    if available_equipment:
        params["available"] = available_equipment
        # Both implements must be owned, and "None"/NULL secondary counts as owned.
        where.append("primary_equipment = ANY(%(available)s)")
        where.append(
            "(secondary_equipment IS NULL OR secondary_equipment = 'None' "
            " OR secondary_equipment = ANY(%(available)s))"
        )

    if movement_patterns:
        params["patterns"] = movement_patterns
        where.append("movement_patterns && %(patterns)s::text[]")

    if planes_of_motion:
        params["planes"] = planes_of_motion
        where.append("planes_of_motion && %(planes)s::text[]")

    for values, column, key in (
        (target_muscle_groups, "target_muscle_group", "muscle_groups"),
        (body_regions, "body_region", "body_regions"),
        (laterality, "laterality", "laterality"),
        (force_types, "force_type", "force_types"),
        (classifications, "classification", "classifications"),
        (postures, "posture", "postures"),
    ):
        if values:
            params[key] = values
            where.append(f"{column} = ANY(%({key})s)")

    if mechanics:
        params["mechanics"] = mechanics
        where.append("mechanics = %(mechanics)s")

    if difficulty_min is not None:
        params["difficulty_min"] = difficulty_min
        where.append("difficulty_rank >= %(difficulty_min)s")
    if difficulty_max is not None:
        params["difficulty_max"] = difficulty_max
        where.append("difficulty_rank <= %(difficulty_max)s")

    if name_contains:
        params["name_contains"] = f"%{name_contains}%"
        where.append("name ILIKE %(name_contains)s")

    if exclude_ids:
        params["exclude_ids"] = exclude_ids
        where.append("id <> ALL(%(exclude_ids)s)")

    if require_video:
        where.append("video_demo_url IS NOT NULL")

    _apply_contraindications(where, params, contraindications)

    sql = f"""
        SELECT {SUMMARY_COLUMNS}
        FROM exercises
        {"WHERE " + " AND ".join(where) if where else ""}
        -- Prefer entries with a demonstration video and complete taxonomy: they are
        -- more useful to the trainee and safer for the model to reason about.
        ORDER BY (video_demo_url IS NOT NULL) DESC,
                 cardinality(qc_flags) ASC,
                 difficulty_rank NULLS LAST,
                 name
        LIMIT %(limit)s
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def count_matching(conn: psycopg.Connection, **kwargs) -> int:
    """How many exercises match, ignoring the limit.

    Lets the agent tell "no such exercise exists" apart from "my filters were too
    narrow", which changes what it should do next.
    """
    kwargs.pop("limit", None)
    rows = search_exercises(conn, limit=MAX_LIMIT, **kwargs)
    return len(rows)


def get_exercises(
    conn: psycopg.Connection,
    ids: list[int],
    detail: bool = True,
) -> dict[int, dict]:
    """Fetch specific exercises by sidecar id, keyed by id."""
    if not ids:
        return {}
    columns = DETAIL_COLUMNS if detail else SUMMARY_COLUMNS
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {columns} FROM exercises WHERE id = ANY(%s)", (list(ids),)
        )
        return {row["id"]: dict(row) for row in cur.fetchall()}


def wger_id_map(conn: psycopg.Connection, ids: list[int]) -> dict[int, int]:
    """sidecar id -> wger exercise id, for the routine writer."""
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, wger_exercise_id FROM exercises "
            "WHERE id = ANY(%s) AND wger_exercise_id IS NOT NULL",
            (list(ids),),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def recent_routine_exercise_ids(conn: psycopg.Connection, last_n: int = 2) -> set[int]:
    """Exercise ids used by the most recent generated routines.

    Feeds the validator's variety check. Extracted in Python rather than with a jsonb
    path query because the plan shape is the application's business, not the database's.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM generated_routines ORDER BY created_at DESC LIMIT %s",
            (last_n,),
        )
        payloads = [row[0] for row in cur.fetchall()]

    ids: set[int] = set()
    for payload in payloads:
        for day in (payload or {}).get("days", []):
            for slot in day.get("slots", []):
                for entry in slot.get("entries", []):
                    exercise_id = entry.get("exercise_id")
                    if isinstance(exercise_id, int):
                        ids.add(exercise_id)
    return ids
