"""Deterministic exercise descriptions built from the taxonomy attributes.

The source spreadsheet has no prose for any of its 3,242 exercises, but wger's
`exercise-translation.description_source` field enforces a 40-character minimum, so
every imported exercise needs text. Rather than pay an LLM to invent instructions it
cannot verify, this module states exactly what the database already knows: which
muscles, which equipment, which posture, grip, movement pattern and plane.

The output is factual by construction, free, reproducible, and comfortably over the
40-character floor. Individual exercises can be upgraded to AI-written coaching cues
later without touching this path.

Written as Markdown because wger renders `description_source` into the read-only
`description` HTML field itself.
"""

from __future__ import annotations

# Grip values that describe the absence of a grip; mentioning them reads badly.
_NULL_GRIPS = {"No Grip"}
_NULL_LOADS = {"No Load"}

_ARTICLE_EXCEPTIONS = {"Bodyweight"}


def _join(items: list[str]) -> str:
    """Join with commas and a final 'and'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def _equipment_phrase(exercise: dict) -> str:
    primary = exercise.get("primary_equipment")
    if not primary:
        return ""
    count = exercise.get("primary_items") or 1
    if primary in _ARTICLE_EXCEPTIONS:
        phrase = "bodyweight only"
    else:
        phrase = f"{count}x {primary}" if count > 1 else primary.lower()

    secondary = exercise.get("secondary_equipment")
    if secondary and secondary != "None":
        sec_count = exercise.get("secondary_items") or 1
        sec = f"{sec_count}x {secondary}" if sec_count > 1 else secondary.lower()
        phrase = f"{phrase}, supported by {sec}"
    return phrase


def build_description(exercise: dict) -> str:
    """Return a Markdown description for one normalized exercise record."""
    name = exercise["name"]
    paragraphs: list[str] = []

    # --- Sentence 1: what it is and what it trains -------------------------------
    bits = []
    difficulty = exercise.get("difficulty")
    mechanics = exercise.get("mechanics")
    descriptor = " ".join(
        p for p in [difficulty.lower() if difficulty else None,
                    mechanics.lower() if mechanics else None] if p
    )
    target = exercise.get("target_muscle_group")
    lead = f"**{name}** is a"
    if descriptor:
        lead += f"n {descriptor}" if descriptor[0] in "aeiou" else f" {descriptor}"
    lead += " exercise"
    if target:
        lead += f" targeting the {target.lower()}"
    bits.append(lead)

    muscles = []
    if exercise.get("prime_mover_muscle"):
        muscles.append(f"prime mover: {exercise['prime_mover_muscle']}")
    supporting = _join([
        m for m in (exercise.get("secondary_muscle"), exercise.get("tertiary_muscle")) if m
    ])
    if supporting:
        muscles.append(f"also working {supporting}")
    if muscles:
        bits.append(f" ({'; '.join(muscles)})")
    paragraphs.append(_sentence("".join(bits)))

    # --- Sentence 2: how it is set up --------------------------------------------
    setup = []
    posture = exercise.get("posture")
    if posture:
        setup.append(f"performed in a {posture.lower()} position")
    equipment = _equipment_phrase(exercise)
    if equipment:
        setup.append(f"using {equipment}")
    arm = exercise.get("arm_involvement")
    if arm and arm != "No Arms":
        arm_text = arm.lower()
        action = exercise.get("arm_action")
        if action == "Alternating":
            arm_text = f"alternating {arm_text}"
        setup.append(arm_text)
    grip = exercise.get("grip")
    if grip and grip not in _NULL_GRIPS:
        setup.append(f"{grip.lower()} grip")
    load = exercise.get("load_position")
    if load and load not in _NULL_LOADS:
        setup.append(f"load finishing in the {load.lower()} position")
    if setup:
        paragraphs.append(_sentence(_join(setup)))

    # --- Sentence 3: biomechanics ------------------------------------------------
    mech = []
    patterns = exercise.get("movement_patterns") or []
    if patterns:
        label = "Movement pattern" if len(patterns) == 1 else "Movement patterns"
        mech.append(f"{label}: {_join([p.lower() for p in patterns])}")
    planes = exercise.get("planes_of_motion") or []
    if planes:
        mech.append(f"worked in the {_join([p.lower() for p in planes])}")
    laterality = exercise.get("laterality")
    if laterality:
        mech.append(laterality.lower())
    foot = exercise.get("foot_elevation")
    if foot and foot != "No Elevation":
        mech.append(foot.lower())
    if mech:
        if len(mech) > 2:
            # Each fact becomes its own sentence, so each needs its own capital.
            paragraphs.append(" ".join(_sentence(m) for m in mech))
        else:
            paragraphs.append(_sentence(_join(mech)))

    # --- Sentence 4: classification ----------------------------------------------
    tail = []
    if exercise.get("body_region"):
        tail.append(f"body region: {exercise['body_region'].lower()}")
    if exercise.get("force_type"):
        tail.append(f"force type: {exercise['force_type'].lower()}")
    if exercise.get("classification"):
        tail.append(f"classification: {exercise['classification'].lower()}")
    if exercise.get("is_combo"):
        tail.append("this is a combination exercise made of more than one movement")
    if tail:
        paragraphs.append(_sentence(_join(tail)))

    # --- Video links --------------------------------------------------------------
    links = []
    if exercise.get("video_demo_url"):
        links.append(f"- [Video demonstration]({exercise['video_demo_url']})")
    if exercise.get("video_explain_url"):
        links.append(f"- [In-depth explanation]({exercise['video_explain_url']})")

    text = " ".join(p for p in paragraphs if p)
    if links:
        text += "\n\n" + "\n".join(links)

    # Attribution: the taxonomy is someone else's work and the import records that.
    text += (
        "\n\n_Attributes from the Functional Fitness Exercise Database (v2.9)._"
    )

    # wger rejects anything under 40 characters. Every record has at least a name,
    # so this only guards against a pathologically sparse row.
    if len(text) < 40:
        text = (
            f"**{name}**. Imported from the Functional Fitness Exercise Database "
            "(v2.9); attribute data for this entry is incomplete."
        )
    return text


if __name__ == "__main__":
    import json
    import sys

    # Smoke-check against real records: print the shortest and longest output so the
    # 40-char floor and general readability can be eyeballed.
    path = sys.argv[1] if len(sys.argv) > 1 else "build/exercises.jsonl"
    records = [json.loads(line) for line in open(path)]
    rendered = [(len(build_description(r)), r["name"], build_description(r)) for r in records]
    rendered.sort()
    print(f"{len(rendered)} descriptions generated")
    print(f"shortest: {rendered[0][0]} chars | longest: {rendered[-1][0]} chars")
    under = [r for r in rendered if r[0] < 40]
    print(f"under wger's 40-char minimum: {len(under)}")
    for label, item in (("SHORTEST", rendered[0]), ("LONGEST", rendered[-1])):
        print(f"\n----- {label}: {item[1]} ({item[0]} chars)\n{item[2]}")
