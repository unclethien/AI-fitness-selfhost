"""Trainee profile persistence and the derived coaching context.

Single source of truth for reading and writing the profile, so the web form and the
agent's `get_trainee_profile` tool cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

# Kinds accepted by trainee_contraindications.kind. Mirrors the SQL enum; a mismatch
# would only surface as a database error at write time, so the form validates here.
CONTRAINDICATION_KINDS = [
    "exercise", "movement_pattern", "equipment", "posture", "body_region",
    "plane_of_motion", "classification", "max_difficulty",
]

GOALS = [
    ("general_fitness", "General fitness"),
    ("fat_loss", "Fat loss"),
    ("strength", "Strength"),
    ("hypertrophy", "Muscle growth"),
    ("endurance", "Endurance / conditioning"),
    ("mobility", "Mobility"),
    ("skill", "Skill acquisition"),
]

EXPERIENCE_LEVELS = [
    ("novice", "Novice — under 6 months of consistent training"),
    ("intermediate", "Intermediate — 6 months to 2 years"),
    ("advanced", "Advanced — 2+ years"),
]


@dataclass
class Profile:
    id: int | None = None
    display_name: str | None = None
    birth_year: int | None = None
    gender: str | None = None
    bodyweight_kg: float | None = None
    height_cm: int | None = None
    experience_level: str = "novice"
    training_age_months: int | None = None
    sessions_per_week: int = 3
    minutes_per_session: int = 60
    available_equipment: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    notes: str | None = None
    goals: list[dict[str, Any]] = field(default_factory=list)
    contraindications: list[dict[str, Any]] = field(default_factory=list)
    benchmarks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def age(self) -> int | None:
        if self.birth_year is None:
            return None
        return dt.date.today().year - self.birth_year

    def is_complete_enough(self) -> tuple[bool, list[str]]:
        """Whether a routine generated from this profile would be genuinely personal.

        Deliberately advisory: an incomplete profile still generates a routine, it just
        generates a more generic one, and the UI says which fields would improve it.
        """
        missing = []
        if not self.goals:
            missing.append("at least one training goal")
        if self.birth_year is None:
            missing.append("birth year (drives the volume ceiling)")
        if not self.available_equipment:
            missing.append("available equipment (otherwise any exercise is fair game)")
        return (not missing, missing)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _iso(value) -> str | None:
    """Render a date as an ISO string, tolerating a value that is already one."""
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def get_profile(conn: psycopg.Connection, profile_id: int | None = None) -> Profile | None:
    """Fetch a profile with its child rows. Defaults to the single/first profile."""
    with conn.cursor(row_factory=dict_row) as cur:
        if profile_id is None:
            cur.execute("SELECT * FROM trainee_profile ORDER BY id LIMIT 1")
        else:
            cur.execute("SELECT * FROM trainee_profile WHERE id = %s", (profile_id,))
        row = cur.fetchone()
        if row is None:
            return None

        profile = Profile(
            id=row["id"],
            display_name=row["display_name"],
            birth_year=row["birth_year"],
            gender=row["gender"],
            bodyweight_kg=float(row["bodyweight_kg"]) if row["bodyweight_kg"] else None,
            height_cm=row["height_cm"],
            experience_level=row["experience_level"],
            training_age_months=row["training_age_months"],
            sessions_per_week=row["sessions_per_week"],
            minutes_per_session=row["minutes_per_session"],
            available_equipment=list(row["available_equipment"] or []),
            dislikes=list(row["dislikes"] or []),
            notes=row["notes"],
        )

        cur.execute(
            "SELECT goal, priority FROM trainee_goals WHERE profile_id = %s "
            "ORDER BY priority, goal",
            (profile.id,),
        )
        profile.goals = [dict(r) for r in cur.fetchall()]

        # Expired restrictions are excluded: a healed injury should stop constraining
        # programming without the trainee having to remember to delete the row.
        cur.execute(
            "SELECT id, kind, value, reason, expires_on "
            "FROM trainee_contraindications "
            "WHERE profile_id = %s "
            "  AND (expires_on IS NULL OR expires_on >= CURRENT_DATE) "
            "ORDER BY kind, value",
            (profile.id,),
        )
        # Normalize dates to ISO strings at the repository boundary. psycopg returns
        # `datetime.date`, the HTML form submits a string, and JSON needs a string —
        # converting once here keeps every consumer from having to handle both.
        profile.contraindications = [
            {**dict(r), "expires_on": _iso(r["expires_on"])} for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT b.id, b.exercise_id, b.label, b.weight_kg, b.reps, b.recorded_on, "
            "       e.name AS exercise_name "
            "FROM trainee_benchmarks b "
            "LEFT JOIN exercises e ON e.id = b.exercise_id "
            "WHERE b.profile_id = %s ORDER BY b.recorded_on DESC",
            (profile.id,),
        )
        profile.benchmarks = [
            {**dict(r), "recorded_on": _iso(r["recorded_on"])} for r in cur.fetchall()
        ]

    return profile


def goal_tuples(profile: Profile) -> list[tuple[str, int]]:
    """Shape the goals for principles.resolve_prescription()."""
    return [(g["goal"], g["priority"]) for g in profile.goals]


def contraindication_map(profile: Profile) -> dict[str, set[str]]:
    """Shape the contraindications for the validator: kind -> set of values."""
    out: dict[str, set[str]] = {}
    for item in profile.contraindications:
        out.setdefault(item["kind"], set()).add(str(item["value"]))
    return out


# ---------------------------------------------------------------------------
# Form vocabulary, sourced from the exercise database rather than hardcoded
#
# The form's dropdowns are built from the values that actually exist in the loaded
# exercise data. Hardcoding them would let the form drift from the database and
# silently produce contraindications that match nothing.
# ---------------------------------------------------------------------------

def form_vocabulary(conn: psycopg.Connection) -> dict[str, list[str]]:
    queries = {
        "equipment": (
            "SELECT DISTINCT primary_equipment AS v FROM exercises "
            "WHERE primary_equipment IS NOT NULL "
            "UNION "
            "SELECT DISTINCT secondary_equipment FROM exercises "
            "WHERE secondary_equipment IS NOT NULL AND secondary_equipment <> 'None' "
            "ORDER BY v"
        ),
        "movement_pattern": (
            "SELECT DISTINCT unnest(movement_patterns) AS v FROM exercises ORDER BY v"
        ),
        "posture": (
            "SELECT DISTINCT posture AS v FROM exercises "
            "WHERE posture IS NOT NULL ORDER BY v"
        ),
        "body_region": (
            "SELECT DISTINCT body_region AS v FROM exercises "
            "WHERE body_region IS NOT NULL ORDER BY v"
        ),
        "plane_of_motion": (
            "SELECT DISTINCT unnest(planes_of_motion) AS v FROM exercises ORDER BY v"
        ),
        "classification": (
            "SELECT DISTINCT classification AS v FROM exercises "
            "WHERE classification IS NOT NULL ORDER BY v"
        ),
    }
    vocabulary: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        for key, sql in queries.items():
            cur.execute(sql)
            vocabulary[key] = [r[0] for r in cur.fetchall()]
    return vocabulary


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

UPSERT_PROFILE = """
INSERT INTO trainee_profile (
    id, display_name, birth_year, gender, bodyweight_kg, height_cm,
    experience_level, training_age_months, sessions_per_week, minutes_per_session,
    available_equipment, dislikes, notes
) OVERRIDING SYSTEM VALUE
VALUES (
    COALESCE(%(id)s, (SELECT COALESCE(MAX(id), 0) + 1 FROM trainee_profile)),
    %(display_name)s, %(birth_year)s, %(gender)s, %(bodyweight_kg)s, %(height_cm)s,
    %(experience_level)s, %(training_age_months)s, %(sessions_per_week)s,
    %(minutes_per_session)s, %(available_equipment)s, %(dislikes)s, %(notes)s
)
ON CONFLICT (id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    birth_year = EXCLUDED.birth_year,
    gender = EXCLUDED.gender,
    bodyweight_kg = EXCLUDED.bodyweight_kg,
    height_cm = EXCLUDED.height_cm,
    experience_level = EXCLUDED.experience_level,
    training_age_months = EXCLUDED.training_age_months,
    sessions_per_week = EXCLUDED.sessions_per_week,
    minutes_per_session = EXCLUDED.minutes_per_session,
    available_equipment = EXCLUDED.available_equipment,
    dislikes = EXCLUDED.dislikes,
    notes = EXCLUDED.notes
RETURNING id
"""


def save_profile(
    conn: psycopg.Connection,
    profile: Profile,
    goals: list[tuple[str, int]],
    contraindications: list[dict[str, Any]],
) -> int:
    """Write the profile and replace its goals and contraindications.

    Child rows are replaced wholesale rather than diffed: the form always submits the
    complete set, so a diff would only add a way for them to disagree.
    """
    with conn.cursor() as cur:
        cur.execute(UPSERT_PROFILE, {
            "id": profile.id,
            "display_name": profile.display_name,
            "birth_year": profile.birth_year,
            "gender": profile.gender,
            "bodyweight_kg": profile.bodyweight_kg,
            "height_cm": profile.height_cm,
            "experience_level": profile.experience_level,
            "training_age_months": profile.training_age_months,
            "sessions_per_week": profile.sessions_per_week,
            "minutes_per_session": profile.minutes_per_session,
            "available_equipment": profile.available_equipment,
            "dislikes": profile.dislikes,
            "notes": profile.notes,
        })
        profile_id = cur.fetchone()[0]

        cur.execute("DELETE FROM trainee_goals WHERE profile_id = %s", (profile_id,))
        for goal, priority in goals:
            cur.execute(
                "INSERT INTO trainee_goals (profile_id, goal, priority) "
                "VALUES (%s, %s, %s) ON CONFLICT (profile_id, goal) DO NOTHING",
                (profile_id, goal, priority),
            )

        cur.execute(
            "DELETE FROM trainee_contraindications WHERE profile_id = %s", (profile_id,)
        )
        for item in contraindications:
            cur.execute(
                "INSERT INTO trainee_contraindications "
                "(profile_id, kind, value, reason, expires_on) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (profile_id, kind, value) DO NOTHING",
                (profile_id, item["kind"], item["value"],
                 item.get("reason"), item.get("expires_on") or None),
            )
    conn.commit()
    return profile_id
