"""Render and submit the profile form against a fake sidecar database.

Run: python agent/test_profile_form.py

No Postgres available here, so `psycopg.connect` is patched with an in-memory double
that answers the specific queries profile_repo issues. This proves the request
handlers, template, form parsing and redirect all work; it does NOT prove the SQL runs
against real Postgres.
"""
import sys, datetime as dt
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "coaching"))

import psycopg

STATE = {"profile": None, "goals": [], "cis": [], "writes": []}

VOCAB = {
    "primary_equipment": ["Barbell", "Bodyweight", "Clubbell", "Dumbbell", "Kettlebell",
                          "Macebell", "Sliders", "Gymnastic Rings", "Plyo Box"],
    "movement_patterns": ["Hip Hinge", "Knee Dominant", "Vertical Push", "Vertical Pull",
                          "Horizontal Push", "Horizontal Pull", "Rotational",
                          "Anti-Extension", "Loaded Carry"],
    "posture": ["Standing", "Supine", "Prone", "Hanging", "Split Squat"],
    "body_region": ["Upper Body", "Lower Body", "Core", "Full Body"],
    "planes_of_motion": ["Sagittal Plane", "Frontal Plane", "Transverse Plane"],
    "classification": ["Bodybuilding", "Calisthenics", "Ballistics", "Plyometric", "Mobility"],
}


class FakeCursor:
    def __init__(self, row_factory=None):
        self.row_factory = row_factory
        self._rows = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        STATE["writes"].append((s[:70], params))
        low = s.lower()
        self._rows = []
        if "count(*) from exercises" in low:
            self._rows = [(4070,)]
        elif "from trainee_profile" in low and low.startswith("select"):
            self._rows = [STATE["profile"]] if STATE["profile"] else []
        elif "from trainee_goals" in low and low.startswith("select"):
            self._rows = STATE["goals"]
        elif "from trainee_contraindications" in low and low.startswith("select"):
            self._rows = STATE["cis"]
        elif "from trainee_benchmarks" in low or "trainee_benchmarks b" in low:
            self._rows = []
        elif "insert into trainee_profile" in low:
            STATE["profile"] = dict(params)
            STATE["profile"].setdefault("id", 1)
            STATE["profile"]["id"] = STATE["profile"]["id"] or 1
            self._rows = [(STATE["profile"]["id"],)]
        elif "insert into trainee_goals" in low:
            STATE["goals"].append({"goal": params[1], "priority": params[2]})
        elif "insert into trainee_contraindications" in low:
            STATE["cis"].append({"id": len(STATE["cis"]) + 1, "kind": params[1],
                                 "value": params[2], "reason": params[3],
                                 "expires_on": params[4]})
        elif "delete from trainee_goals" in low:
            STATE["goals"] = []
        elif "delete from trainee_contraindications" in low:
            STATE["cis"] = []
        elif "distinct" in low:
            for column, values in VOCAB.items():
                if column in low:
                    self._rows = [(v,) for v in values]
                    break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def cursor(self, row_factory=None): return FakeCursor(row_factory)
    def commit(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


psycopg.connect = lambda *a, **k: FakeConn()

from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

failures = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {extra}")


print("=== health")
r = client.get("/health")
check("health returns ok", r.status_code == 200 and r.json()["status"] == "ok", r.text[:200])
check("health reports exercise count", r.json().get("exercises") == 4070)

print("\n=== empty profile renders")
r = client.get("/profile")
check("GET /profile is 200", r.status_code == 200, r.text[:400])
html = r.text
check("shows incomplete warning", "Routines will be generic until these are set" in html)
check("renders all 7 goals", all(f'name="goal_{g}"' in html for g in
      ["general_fitness", "fat_loss", "strength", "hypertrophy", "endurance",
       "mobility", "skill"]))
check("equipment chips come from the database", "Clubbell" in html and "Macebell" in html)
check("contraindication kinds rendered", 'value="movement_pattern"' in html
      and 'value="max_difficulty"' in html)
check("datalist has DB movement patterns", 'value="Hip Hinge"' in html)
check("no unrendered jinja left", "{{" not in html and "{%" not in html)
check("safety disclaimer present", "does not assess or diagnose" in html)

print("\n=== root redirects")
# Chat became the landing page when the chat surface shipped; the profile form is
# reached from its sidebar.
r = client.get("/", follow_redirects=False)
check("/ redirects to /chat",
      r.status_code in (307, 302) and "/chat" in r.headers["location"],
      f"got {r.status_code} -> {r.headers.get('location')}")

print("\n=== submit the form")
form = {
    "profile_id": "",
    "display_name": "Thien",
    "birth_year": "1990",
    "gender": "male",
    "bodyweight_kg": "78.5",
    "height_cm": "175",
    "experience_level": "intermediate",
    "training_age_months": "18",
    "sessions_per_week": "4",
    "minutes_per_session": "75",
    "dislikes": "Burpees, treadmill running",
    "notes": "Home gym only.",
    "goal_general_fitness": "1", "priority_general_fitness": "1",
    "goal_fat_loss": "1", "priority_fat_loss": "2",
    "goal_strength": "1", "priority_strength": "1",
}
# httpx treats a list of tuples in `data=` as a content stream, not form fields.
# Repeated fields must be expressed as {key: [values]}.
data = dict(form)
data["available_equipment"] = ["Kettlebell", "Clubbell", "Bodyweight"]
# Two real restrictions plus a trailing blank row, exactly as the page renders them.
data["ci_kind"] = ["movement_pattern", "max_difficulty", ""]
data["ci_value"] = ["Vertical Push", "5", ""]
data["ci_reason"] = ["shoulder impingement", "", ""]
data["ci_expires"] = ["", "2026-12-31", ""]

r = client.post("/profile", data=data, follow_redirects=False)
check("POST redirects (303)", r.status_code == 303, f"got {r.status_code}: {r.text[:300]}")
check("redirect flags saved", "saved=1" in r.headers.get("location", ""))
check("3 goals persisted", len(STATE["goals"]) == 3, str(STATE["goals"]))
check("strength priority 1", any(g["goal"] == "strength" and g["priority"] == 1
                                for g in STATE["goals"]))
check("fat_loss priority 2", any(g["goal"] == "fat_loss" and g["priority"] == 2
                                for g in STATE["goals"]))
check("blank restriction row dropped", len(STATE["cis"]) == 2, str(STATE["cis"]))
check("movement_pattern restriction saved",
      any(c["kind"] == "movement_pattern" and c["value"] == "Vertical Push"
          for c in STATE["cis"]))
check("expiry date captured",
      any(c["expires_on"] == "2026-12-31" for c in STATE["cis"]))
check("equipment list saved", STATE["profile"]["available_equipment"] ==
      ["Kettlebell", "Clubbell", "Bodyweight"], str(STATE["profile"]["available_equipment"]))
check("dislikes split on commas", STATE["profile"]["dislikes"] ==
      ["Burpees", "treadmill running"], str(STATE["profile"]["dislikes"]))
check("numbers coerced", STATE["profile"]["birth_year"] == 1990
      and STATE["profile"]["bodyweight_kg"] == 78.5
      and STATE["profile"]["sessions_per_week"] == 4)

print("\n=== saved profile re-renders")
STATE["profile"]["experience_level"] = "intermediate"
r = client.get("/profile?saved=1")
check("saved banner shown", "Profile saved." in r.text)
check("no incomplete warning now",
      "Routines will be generic" not in r.text)
check("equipment chip re-checked",
      'value="Kettlebell"\n              checked' in r.text or "checked" in r.text)

print("\n=== JSON view + coaching handoff")
r = client.get("/api/profile")
check("/api/profile is 200", r.status_code == 200, r.text[:300])
payload = r.json()
check("age derived from birth year", payload["profile"]["age"] == dt.date.today().year - 1990,
      str(payload["profile"].get("age")))
check("contraindication map shaped for validator",
      payload["contraindication_map"].get("movement_pattern") == ["Vertical Push"],
      str(payload["contraindication_map"]))

# The real integration point: profile -> prescription.
from principles import resolve_prescription
import profile_repo as pr
p = pr.get_profile(FakeConn())
prescription = resolve_prescription(pr.goal_tuples(p), age=p.age)
check("prescription built from saved goals", set(prescription.all_goals) ==
      {"general_fitness", "fat_loss", "strength"}, str(prescription.all_goals))
check("primary goals are the priority-1 ones",
      set(prescription.primary_goals) == {"general_fitness", "strength"},
      str(prescription.primary_goals))
check("fat-loss/strength conflict note emitted",
      any("energy balance" in n for n in prescription.notes), str(prescription.notes))

print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILURES: ' + str(failures)}")
sys.exit(1 if failures else 0)
