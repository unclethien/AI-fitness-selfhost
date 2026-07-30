"""wger REST client: read workout logs, write routines.

Writing a routine means creating a four-level tree — routine → day → slot → slot_entry —
plus a config row per prescribed quantity. There is no bulk endpoint, so a 4-day routine
with 16 exercises is roughly 100 requests. Two consequences shape this module:

  1. **Rollback matters.** A failure at request 80 would otherwise leave a half-built
     routine in the user's account. `create_routine` deletes the partially-created
     routine on any failure, which cascades to its children.

  2. **Validate before writing.** The caller runs the programming validator and a schema
     check first; by the time we get here the plan should be sound. `_check_limits` is
     the last line of defence against wger's string-length limits, which are short
     enough to be easy to overshoot (routine name 25 chars, day name 20).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import requests

log = logging.getLogger(__name__)

# Verified against wger.de/api/v2/schema (v2.7.0a1).
LIMITS = {
    "routine_name": 25,
    "routine_description": 1000,
    "day_name": 20,
    "day_description": 1000,
    "slot_comment": 200,
    "slot_entry_comment": 100,
}

# wger caps a routine at 120 days.
MAX_ROUTINE_DAYS = 120


class WgerError(RuntimeError):
    pass


class WgerClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("WGER_BASE_URL", "http://web:8000")).rstrip("/")
        token = token or os.environ.get("WGER_API_TOKEN", "")
        if not token:
            raise WgerError(
                "WGER_API_TOKEN is not set. Create one in wger under "
                "/en/user/api-key and put it in the agent's environment."
            )
        self.session = requests.Session()
        # wger accepts a permanent token as `Token <value>`; JWT uses `Bearer`.
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        })
        self.timeout = timeout

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v2/{path.strip('/')}/"

    def _post(self, path: str, payload: dict) -> dict:
        response = self.session.post(self._url(path), json=payload, timeout=self.timeout)
        if response.status_code >= 400:
            # wger's validation errors are per-field and genuinely useful; surfacing the
            # body is the difference between a fixable message and "400 Bad Request".
            raise WgerError(
                f"POST {path} failed ({response.status_code}): {response.text[:600]}"
                f"\n  payload was: {payload}"
            )
        return response.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self.session.get(self._url(path), params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise WgerError(f"GET {path} failed ({response.status_code}): {response.text[:400]}")
        return response.json()

    def _delete(self, path: str, pk: int) -> None:
        url = f"{self.base_url}/api/v2/{path.strip('/')}/{pk}/"
        response = self.session.delete(url, timeout=self.timeout)
        if response.status_code >= 400 and response.status_code != 404:
            raise WgerError(f"DELETE {path}/{pk} failed ({response.status_code})")

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def check_connection(self) -> dict:
        """Confirm the base URL and token work before anything is written."""
        try:
            profile = self._get("userprofile")
            version = self._get("version")
            return {
                "ok": True,
                "wger_version": version if isinstance(version, str) else str(version),
                "authenticated": bool(profile.get("results") or profile),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def recent_logs(self, days: int = 28, limit: int = 300) -> list[dict]:
        """Workout logs from the last `days`, newest first.

        This is the "read what was actually trained" input. Reduced to the fields the
        model needs — a raw log dump is mostly ids and would waste context.
        """
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        payload = self._get("workoutlog", {
            "limit": limit,
            "ordering": "-date",
            "date__gte": since,
        })
        logs = []
        for row in payload.get("results", []):
            logs.append({
                "date": row.get("date"),
                "exercise": row.get("exercise"),
                "reps": row.get("repetitions"),
                "weight": row.get("weight"),
                "rir": row.get("rir"),
            })
        return logs

    def recent_sessions(self, days: int = 28, limit: int = 60) -> list[dict]:
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        payload = self._get("workoutsession", {
            "limit": limit,
            "ordering": "-date",
            "date__gte": since,
        })
        return [
            {
                "date": row.get("date"),
                "impression": row.get("impression"),
                "notes": (row.get("notes") or "")[:200],
            }
            for row in payload.get("results", [])
        ]

    # ------------------------------------------------------------------
    # routine writing
    # ------------------------------------------------------------------

    @staticmethod
    def _check_limits(plan: dict) -> list[str]:
        """Last-chance check against wger's string limits before any write."""
        problems = []
        if len(plan.get("name", "")) > LIMITS["routine_name"]:
            problems.append(
                f"routine name is {len(plan['name'])} chars, wger's limit is "
                f"{LIMITS['routine_name']}"
            )
        if len(plan.get("description", "")) > LIMITS["routine_description"]:
            problems.append("routine description exceeds 1000 chars")
        for day in plan.get("days", []):
            if len(day.get("name", "")) > LIMITS["day_name"]:
                problems.append(
                    f"day name {day['name']!r} is {len(day['name'])} chars, "
                    f"wger's limit is {LIMITS['day_name']}"
                )
            for slot in day.get("slots", []):
                if len(slot.get("comment") or "") > LIMITS["slot_comment"]:
                    problems.append(f"slot comment on day {day.get('name')} exceeds 200 chars")
                for entry in slot.get("entries", []):
                    if len(entry.get("comment") or "") > LIMITS["slot_entry_comment"]:
                        problems.append("a slot entry comment exceeds 100 chars")
        return problems

    def create_routine(
        self,
        plan: dict,
        exercise_ids: dict[int, int],
        start: dt.date | None = None,
    ) -> dict:
        """Write a validated routine plan into wger.

        `exercise_ids` maps sidecar exercise id -> wger exercise id. Keeping the mapping
        outside the plan means the model never has to know wger's ids, and a plan can be
        re-pointed at a different wger install.

        Returns {"routine_id", "requests", "url"}. Raises WgerError after rolling back.
        """
        problems = self._check_limits(plan)
        if problems:
            raise WgerError("plan violates wger's field limits: " + "; ".join(problems))

        weeks = int(plan.get("weeks") or 6)
        start = start or dt.date.today()
        duration_days = min(weeks * 7, MAX_ROUTINE_DAYS)
        end = start + dt.timedelta(days=duration_days)

        routine = self._post("routine", {
            "name": plan["name"][: LIMITS["routine_name"]],
            "description": plan.get("description", "")[: LIMITS["routine_description"]],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fit_in_week": False,
        })
        routine_id = routine["id"]
        requests_made = 1

        try:
            for day_plan in sorted(plan.get("days", []), key=lambda d: d.get("order", 0)):
                day = self._post("day", {
                    "routine": routine_id,
                    "order": day_plan.get("order", 1),
                    "name": (day_plan.get("name") or "Day")[: LIMITS["day_name"]],
                    "description": (day_plan.get("description") or "")[:1000],
                    "is_rest": bool(day_plan.get("is_rest")),
                    "type": day_plan.get("type", "custom"),
                })
                requests_made += 1
                if day_plan.get("is_rest"):
                    continue

                for slot_plan in sorted(day_plan.get("slots", []),
                                        key=lambda s: s.get("order", 0)):
                    slot = self._post("slot", {
                        "day": day["id"],
                        "order": slot_plan.get("order", 1),
                        "comment": (slot_plan.get("comment") or "")[:200],
                    })
                    requests_made += 1

                    # More than one entry in a slot IS how wger expresses a superset;
                    # there is no separate superset object.
                    for index, entry_plan in enumerate(slot_plan.get("entries", []), start=1):
                        requests_made += self._write_entry(
                            slot["id"], index, entry_plan, exercise_ids
                        )
        except Exception as exc:
            # Roll back rather than leaving a half-built routine in the account.
            log.error("routine write failed after %s requests: %s", requests_made, exc)
            try:
                self._delete("routine", routine_id)
                log.info("rolled back partially created routine %s", routine_id)
            except Exception as cleanup_exc:  # noqa: BLE001
                raise WgerError(
                    f"{exc}\n\nAND rollback failed: routine {routine_id} may be "
                    f"partially created in wger and should be deleted manually "
                    f"({cleanup_exc})"
                ) from exc
            raise WgerError(f"{exc}\n\n(the partial routine was rolled back)") from exc

        return {
            "routine_id": routine_id,
            "requests": requests_made,
            "url": f"{self.base_url}/en/routine/{routine_id}/view/",
        }

    def _write_entry(
        self,
        slot_id: int,
        order: int,
        entry: dict,
        exercise_ids: dict[int, int],
    ) -> int:
        sidecar_id = entry["exercise_id"]
        wger_exercise_id = exercise_ids.get(sidecar_id)
        if not wger_exercise_id:
            raise WgerError(
                f"sidecar exercise {sidecar_id} has no wger id — it was never imported, "
                "so it cannot be added to a routine. Run the wger import first."
            )

        slot_entry = self._post("slot-entry", {
            "slot": slot_id,
            "exercise": wger_exercise_id,
            "type": entry.get("type", "normal"),
            "order": order,
            "comment": (entry.get("comment") or "")[:100],
        })
        entry_id = slot_entry["id"]
        count = 1

        # Base values live at iteration 1 with operation "r" (replace). This reading of
        # wger's config semantics is the part of the write path most likely to need
        # adjustment against a live server — the schema documents the fields but not
        # their interaction.
        for endpoint, value in (
            ("sets-config", entry.get("sets")),
            ("repetitions-config", entry.get("reps")),
            ("weight-config", entry.get("weight_kg")),
            ("rir-config", entry.get("rir")),
            ("rest-config", entry.get("rest_seconds")),
        ):
            if value is None:
                continue
            self._post(endpoint, {
                "slot_entry": entry_id,
                "iteration": 1,
                "value": str(value),
                "operation": "r",
                "step": "abs",
                "repeat": False,
            })
            count += 1

        # Progression: a rule at iteration 2 with repeat=True applies every cycle.
        progression = entry.get("progression")
        if progression:
            self._post("weight-config", {
                "slot_entry": entry_id,
                "iteration": 1 + int(progression.get("every_iterations", 1)),
                "value": str(progression["value"]),
                "operation": progression.get("operation", "+"),
                "step": progression.get("step", "abs"),
                "repeat": True,
            })
            count += 1

        return count
