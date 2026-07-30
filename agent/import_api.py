"""HTTP surface consumed by the wger import script.

The import script runs *inside* the wger container (piped into `manage.py shell`) and
talks to these endpoints instead of connecting to the sidecar database directly. Two
reasons:

  1. The script then needs nothing but the Python standard library — no psycopg in the
     wger image, no dependency on what that image happens to ship.
  2. wger is never modified: no app to add to INSTALLED_APPS, no volume mounted into
     its container, no settings module to locate.

Taxonomy is exchanged by NAME rather than by integer id. wger's category, muscle and
equipment ids come from fixtures and are not guaranteed identical across installations,
so names are the portable key — and equipment that doesn't exist yet gets created by
name on the wger side.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from fastapi import APIRouter, Body, HTTPException
from psycopg.rows import dict_row

router = APIRouter(prefix="/api", tags=["import"])

MAPPINGS = json.loads(
    (Path(__file__).resolve().parent.parent / "etl" / "mappings" / "wger_mappings.json")
    .read_text()
)
CATEGORY_NAMES: dict[str, str] = MAPPINGS["category_names"]
MUSCLE_NAMES: dict[str, str] = MAPPINGS["muscle_names"]
EQUIPMENT_NAMES: dict[str, str] = MAPPINGS["equipment_names"]

# wger's Translation.description is a TextField with max_length=2000, and it holds the
# *rendered* HTML of description_source. Markdown-to-HTML roughly doubles a link-heavy
# description, so the source is capped well below that to leave headroom.
MAX_DESCRIPTION_SOURCE = 900


def _names(ids, lookup: dict[str, str]) -> list[str]:
    """Translate wger integer ids to names, dropping anything unmapped."""
    out = []
    for value in ids or []:
        name = lookup.get(str(value))
        if name and name not in out:
            out.append(name)
    return out


def _connect(dsn: str) -> psycopg.Connection:
    try:
        return psycopg.connect(dsn)
    except psycopg.OperationalError as exc:
        raise HTTPException(503, f"sidecar database unreachable: {exc}") from exc


def build_router(dsn_provider) -> APIRouter:
    """`dsn_provider` is a zero-arg callable returning the sidecar DSN."""

    @router.get("/equipment/to-create")
    def equipment_to_create():
        """Equipment names present in the custom database with no wger equivalent.

        wger's /api/v2/equipment/ endpoint is read-only, which is why these are created
        through the ORM by the import script rather than over REST.
        """
        return {"names": MAPPINGS["equipment_to_create"]["names"]}

    @router.get("/exercises/for-import")
    def exercises_for_import(
        limit: int = 500,
        offset: int = 0,
        only_pending: bool = True,
    ):
        """Custom exercises ready to be created in wger.

        `only_pending=false` re-emits already-linked exercises too, which is how you push
        corrected descriptions or taxonomy after re-running the ETL.
        """
        # Approved AI-generated variations are imported by the same path: once a human
        # has approved one it is an ordinary exercise that needs to become loggable.
        where = ["source IN ('ffed-2.9', 'generated-variation')",
                 "description IS NOT NULL"]
        if only_pending:
            where.append("wger_exercise_id IS NULL")

        sql = f"""
            SELECT id, uuid, name, description, wger_category, wger_muscles,
                   wger_muscles_secondary, wger_equipment, primary_equipment,
                   secondary_equipment, video_demo_url, qc_flags
            FROM exercises
            WHERE {' AND '.join(where)}
            ORDER BY id
            LIMIT %s OFFSET %s
        """

        with _connect(dsn_provider()) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (limit, offset))
                rows = cur.fetchall()
                cur.execute(
                    f"SELECT count(*) AS n FROM exercises WHERE {' AND '.join(where)}"
                )
                total = cur.fetchone()["n"]

        items = []
        skipped = []
        for row in rows:
            category = CATEGORY_NAMES.get(str(row["wger_category"]))
            if not category:
                # Without a category wger cannot create the exercise at all; report it
                # rather than letting the script fail mid-run.
                skipped.append({"uuid": str(row["uuid"]), "name": row["name"],
                                "reason": "no mapped wger category"})
                continue

            description = row["description"] or ""
            if len(description) > MAX_DESCRIPTION_SOURCE:
                # Trim on a paragraph boundary so a link block is never cut in half.
                cut = description.rfind("\n\n", 0, MAX_DESCRIPTION_SOURCE)
                description = description[: cut if cut > 200 else MAX_DESCRIPTION_SOURCE]

            # Equipment by name: the mapped wger ids, plus the custom names that the
            # script will create on the wger side.
            equipment = _names(row["wger_equipment"], EQUIPMENT_NAMES)
            for field in ("primary_equipment", "secondary_equipment"):
                value = row[field]
                if value and value != "None" and value not in equipment:
                    # Only names that are not already covered by a mapped wger id.
                    if value in MAPPINGS["equipment_to_create"]["names"]:
                        equipment.append(value)

            items.append({
                "sidecar_id": row["id"],
                "uuid": str(row["uuid"]),
                "name": row["name"][:200],  # wger Translation.name max_length=200
                "description_source": description,
                "category": category,
                "muscles": _names(row["wger_muscles"], MUSCLE_NAMES),
                "muscles_secondary": _names(row["wger_muscles_secondary"], MUSCLE_NAMES),
                "equipment": equipment,
            })

        return {
            "total_matching": total,
            "returned": len(items),
            "offset": offset,
            "skipped": skipped,
            "license_author": (
                "Functional Fitness Exercise Database v2.9 (attributes); "
                "description generated from those attributes"
            ),
            "items": items,
        }

    @router.post("/exercises/link")
    def link_exercises(payload: dict = Body(...)):
        """Record the wger exercise id assigned to each imported exercise.

        Completes the cross-link so the agent knows which exercises are loggable. Keyed
        on uuid, so re-running is harmless.
        """
        links = payload.get("links") or []
        if not links:
            return {"updated": 0}

        pairs = [
            (int(item["wger_exercise_id"]), str(item["uuid"]))
            for item in links
            if item.get("wger_exercise_id") and item.get("uuid")
        ]
        if not pairs:
            raise HTTPException(400, "no usable {uuid, wger_exercise_id} pairs in payload")

        with _connect(dsn_provider()) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE exercises SET wger_exercise_id = %s, wger_uuid = uuid "
                    "WHERE uuid = %s",
                    pairs,
                )
            conn.commit()
        return {"updated": len(pairs)}

    @router.get("/import/status")
    def import_status():
        with _connect(dsn_provider()) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT source, count(*) AS total, "
                "count(wger_exercise_id) AS linked, "
                "count(*) FILTER (WHERE description IS NULL) AS missing_description "
                "FROM exercises GROUP BY source ORDER BY source"
            )
            by_source = [dict(r) for r in cur.fetchall()]
        return {
            "by_source": by_source,
            "note": (
                "linked == total means every exercise is loggable in wger. "
                "Re-run the import script to push updates after re-running the ETL."
            ),
        }

    return router
