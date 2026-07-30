#!/usr/bin/env python3
"""Load exercises into the sidecar intelligence store.

Two sources, one schema, so the agent queries a single pool:

  --custom   build/exercises.jsonl produced by etl/extract_custom_db.py (3,242 rows)
  --wger     the wger instance's own exercises via /api/v2/exerciseinfo/ (828 rows)

Both are upserts keyed on `uuid`, so re-running is safe and idempotent. Custom rows
carry a project-local UUIDv5 (see etl/extract_custom_db.py); wger rows carry wger's
own UUID, so the two can never collide.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg
import requests
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
from describe import build_description  # noqa: E402

UPSERT = """
INSERT INTO exercises (
    uuid, source, source_row, slug, name,
    difficulty, difficulty_rank,
    target_muscle_group, prime_mover_muscle, secondary_muscle, tertiary_muscle,
    primary_equipment, primary_items, secondary_equipment, secondary_items,
    posture, arm_involvement, arm_action, grip, load_position, leg_action,
    foot_elevation, movement_patterns, planes_of_motion, body_region, force_type,
    mechanics, laterality, classification, is_combo,
    video_demo_url, video_explain_url, description,
    wger_exercise_id, wger_uuid, wger_category,
    wger_muscles, wger_muscles_secondary, wger_equipment,
    qc_flags
) VALUES (
    %(uuid)s, %(source)s, %(source_row)s, %(slug)s, %(name)s,
    %(difficulty)s, %(difficulty_rank)s,
    %(target_muscle_group)s, %(prime_mover_muscle)s, %(secondary_muscle)s, %(tertiary_muscle)s,
    %(primary_equipment)s, %(primary_items)s, %(secondary_equipment)s, %(secondary_items)s,
    %(posture)s, %(arm_involvement)s, %(arm_action)s, %(grip)s, %(load_position)s, %(leg_action)s,
    %(foot_elevation)s, %(movement_patterns)s, %(planes_of_motion)s, %(body_region)s, %(force_type)s,
    %(mechanics)s, %(laterality)s, %(classification)s, %(is_combo)s,
    %(video_demo_url)s, %(video_explain_url)s, %(description)s,
    %(wger_exercise_id)s, %(wger_uuid)s, %(wger_category)s,
    %(wger_muscles)s, %(wger_muscles_secondary)s, %(wger_equipment)s,
    %(qc_flags)s
)
ON CONFLICT (uuid) DO UPDATE SET
    slug = EXCLUDED.slug,
    name = EXCLUDED.name,
    difficulty = EXCLUDED.difficulty,
    difficulty_rank = EXCLUDED.difficulty_rank,
    target_muscle_group = EXCLUDED.target_muscle_group,
    prime_mover_muscle = EXCLUDED.prime_mover_muscle,
    secondary_muscle = EXCLUDED.secondary_muscle,
    tertiary_muscle = EXCLUDED.tertiary_muscle,
    primary_equipment = EXCLUDED.primary_equipment,
    primary_items = EXCLUDED.primary_items,
    secondary_equipment = EXCLUDED.secondary_equipment,
    secondary_items = EXCLUDED.secondary_items,
    posture = EXCLUDED.posture,
    arm_involvement = EXCLUDED.arm_involvement,
    arm_action = EXCLUDED.arm_action,
    grip = EXCLUDED.grip,
    load_position = EXCLUDED.load_position,
    leg_action = EXCLUDED.leg_action,
    foot_elevation = EXCLUDED.foot_elevation,
    movement_patterns = EXCLUDED.movement_patterns,
    planes_of_motion = EXCLUDED.planes_of_motion,
    body_region = EXCLUDED.body_region,
    force_type = EXCLUDED.force_type,
    mechanics = EXCLUDED.mechanics,
    laterality = EXCLUDED.laterality,
    classification = EXCLUDED.classification,
    is_combo = EXCLUDED.is_combo,
    video_demo_url = EXCLUDED.video_demo_url,
    video_explain_url = EXCLUDED.video_explain_url,
    description = EXCLUDED.description,
    wger_category = EXCLUDED.wger_category,
    wger_muscles = EXCLUDED.wger_muscles,
    wger_muscles_secondary = EXCLUDED.wger_muscles_secondary,
    wger_equipment = EXCLUDED.wger_equipment,
    qc_flags = EXCLUDED.qc_flags
    -- wger_exercise_id is deliberately NOT overwritten: it is set by the wger import
    -- command and must survive a re-run of this loader.
"""

# Every column the upsert names, so a record missing an optional key doesn't raise.
FIELDS = [
    "uuid", "source", "source_row", "slug", "name", "difficulty", "difficulty_rank",
    "target_muscle_group", "prime_mover_muscle", "secondary_muscle", "tertiary_muscle",
    "primary_equipment", "primary_items", "secondary_equipment", "secondary_items",
    "posture", "arm_involvement", "arm_action", "grip", "load_position", "leg_action",
    "foot_elevation", "movement_patterns", "planes_of_motion", "body_region",
    "force_type", "mechanics", "laterality", "classification", "is_combo",
    "video_demo_url", "video_explain_url", "description", "wger_exercise_id",
    "wger_uuid", "wger_category", "wger_muscles", "wger_muscles_secondary",
    "wger_equipment", "qc_flags",
]

DEFAULTS = {
    "movement_patterns": [], "planes_of_motion": [], "wger_muscles": [],
    "wger_muscles_secondary": [], "wger_equipment": [], "qc_flags": [],
    "is_combo": False,
}


def to_row(record: dict) -> dict:
    row = {field: record.get(field, DEFAULTS.get(field)) for field in FIELDS}
    return row


def load_custom(conn, jsonl_path: Path) -> int:
    records = [json.loads(line) for line in jsonl_path.open()]
    rows = []
    for record in records:
        wger = record.pop("wger", {})
        row = to_row({
            **record,
            "wger_category": wger.get("category"),
            "wger_muscles": wger.get("muscles", []),
            "wger_muscles_secondary": wger.get("muscles_secondary", []),
            "wger_equipment": wger.get("equipment", []),
        })
        # Generated here rather than at import time so the sidecar is the single
        # source of truth for description text.
        row["description"] = build_description(record)
        row["source"] = "ffed-2.9"
        rows.append(row)

    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    conn.commit()
    return len(rows)


def fetch_wger_exercises(base_url: str, token: str | None) -> list[dict]:
    """Page through /api/v2/exerciseinfo/ and flatten into the sidecar shape."""
    session = requests.Session()
    if token:
        session.headers["Authorization"] = f"Token {token}"
    url = f"{base_url.rstrip('/')}/api/v2/exerciseinfo/?format=json&limit=100"
    out: list[dict] = []

    while url:
        response = session.get(url, timeout=60)
        response.raise_for_status()
        payload = response.json()
        for item in payload["results"]:
            english = next(
                (t for t in item.get("translations", []) if t.get("language") == 2),
                None,
            )
            if english is None or not english.get("name"):
                # Without an English name there is nothing for the agent to reason
                # about or for the user to recognize; skip rather than store a blank.
                continue
            out.append({
                "uuid": item["uuid"],
                "source": "wger-upstream",
                "slug": f"wger-{item['id']}",
                "name": english["name"],
                "description": english.get("description_source") or english.get("description"),
                "target_muscle_group": (item.get("category") or {}).get("name"),
                "prime_mover_muscle": next(
                    (m["name"] for m in item.get("muscles") or []), None
                ),
                "secondary_muscle": next(
                    (m["name"] for m in item.get("muscles_secondary") or []), None
                ),
                "primary_equipment": next(
                    (e["name"] for e in item.get("equipment") or []), None
                ),
                "wger_exercise_id": item["id"],
                "wger_uuid": item["uuid"],
                "wger_category": (item.get("category") or {}).get("id"),
                "wger_muscles": sorted(m["id"] for m in item.get("muscles") or []),
                "wger_muscles_secondary": sorted(
                    m["id"] for m in item.get("muscles_secondary") or []
                ),
                "wger_equipment": sorted(e["id"] for e in item.get("equipment") or []),
                # wger carries none of the biomechanical taxonomy, so these stay empty.
                # Flagged so the agent can tell "no data" from "genuinely not applicable"
                # and weight its selection accordingly.
                "qc_flags": ["no_biomechanical_taxonomy"],
            })
        url = payload.get("next")
        if url:
            time.sleep(0.2)  # be a polite client to a self-hosted instance
    return out


def load_wger(conn, base_url: str, token: str | None) -> int:
    records = fetch_wger_exercises(base_url, token)
    rows = [to_row(r) for r in records]
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    conn.commit()
    return len(rows)


def summarize(conn) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT source, count(*) AS n, "
            "count(wger_exercise_id) AS loggable, "
            "count(description) AS described "
            "FROM exercises GROUP BY source ORDER BY source"
        )
        print("\nsidecar contents:")
        for row in cur.fetchall():
            print(
                f"  {row['source']:<22} {row['n']:>5} exercises  "
                f"{row['loggable']:>5} loggable in wger  {row['described']:>5} described"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--custom", action="store_true", help="load build/exercises.jsonl")
    parser.add_argument("--wger", action="store_true", help="mirror wger's own exercises")
    parser.add_argument("--jsonl", default="build/exercises.jsonl")
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "SIDECAR_DSN", "postgresql://fitness@127.0.0.1:5433/exercise_intel"
        ),
    )
    parser.add_argument(
        "--wger-url", default=os.environ.get("WGER_BASE_URL", "http://localhost")
    )
    parser.add_argument("--wger-token", default=os.environ.get("WGER_API_TOKEN"))
    args = parser.parse_args()

    if not args.custom and not args.wger:
        parser.error("pass --custom and/or --wger")

    try:
        conn = psycopg.connect(args.dsn)
    except psycopg.OperationalError as exc:
        sys.exit(
            f"error: cannot reach the sidecar database at {args.dsn}\n  {exc}\n"
            "  Is it running?  docker compose up -d sidecar-db"
        )

    with conn:
        if args.custom:
            path = Path(args.jsonl)
            if not path.exists():
                sys.exit(
                    f"error: {path} not found — run etl/extract_custom_db.py first"
                )
            count = load_custom(conn, path)
            print(f"upserted {count} custom exercises")
        if args.wger:
            count = load_wger(conn, args.wger_url, args.wger_token)
            print(f"upserted {count} wger exercises from {args.wger_url}")
        summarize(conn)


if __name__ == "__main__":
    main()
