"""
Domain B - the world bible.

This is the canonical source of truth for a generated world. Everything
downstream (NPCs, quests, enemies, lore, dialogue, art, maps) is derived from
it, so consistency here is worth more than richness. A vague bible produces
seven generators that quietly contradict each other.

Usage:
    python -m core.world_bible "rain-drowned neon port city ruled by cartels"
"""

import os
import sys
import json
import re
from typing import Any

from .llm import generate_json, LLMError

BIBLE_DIR = "output/bibles"

# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
# Keep this in sync with schema/world_bundle.json. It is the contract between
# every other domain in the project - change it only by agreement.

SCHEMA = """{
  "world_name": str,
  "one_line_pitch": str,
  "tone": [str],
  "genre_tags": [str],
  "timeline": [
    {"era": str, "event": str, "years_ago": int}
  ],
  "geography": [
    {"name": str, "type": str, "description": str, "danger_level": int}
  ],
  "factions": [
    {"name": str, "goal": str, "methods": str, "attitude_to_player": str,
     "seat_of_power": str}
  ],
  "magic_or_tech_rules": [
    {"rule": str, "cost": str}
  ],
  "conflicts": [
    {"summary": str, "parties": [str], "stakes": str}
  ],
  "visual_style": {
    "palette": [str],
    "lighting": str,
    "architecture": str,
    "materials": [str],
    "art_direction_keywords": [str]
  }
}"""

SYSTEM = f"""You are a senior game world architect.

Given a short creative brief, produce the CANONICAL BIBLE for that world.
Every later asset will be derived from this document, so it must be
internally consistent, specific, and free of contradictions.

Return ONLY valid JSON matching this schema exactly:
{SCHEMA}

Hard rules:
- Invent proper nouns. Never write "the ancient evil" when you could name it.
- 3 to 5 entries in every list. Exactly one visual_style object.
- timeline: years_ago must be a positive integer. Older events have larger
  numbers. Events must not contradict each other.
- geography: danger_level is 1 to 5. seat_of_power on each faction must be
  the name of a place that exists in geography.
- conflicts: every entry in "parties" must be a faction name that exists in
  factions, or a named place from geography.
- magic_or_tech_rules: every rule needs a real cost or limit. A power with no
  cost gives quest designers nothing to work with.
- visual_style feeds an image model. Use concrete visual nouns and material
  words, not moods. "wet basalt, guttering tallow lamps" not "atmospheric".
- Obey the brief's genre. Do not drift toward generic fantasy.
"""

REQUIRED_KEYS = [
    "world_name", "one_line_pitch", "tone", "genre_tags", "timeline",
    "geography", "factions", "magic_or_tech_rules", "conflicts", "visual_style",
]

VISUAL_KEYS = ["palette", "lighting", "architecture", "materials",
               "art_direction_keywords"]


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(bible: dict) -> list[str]:
    """
    Return a list of human-readable problems. Empty list means the bible is
    structurally sound. This does not raise - a slightly flawed bible is still
    usable, and we would rather warn than lose a generation.
    """
    problems: list[str] = []

    for key in REQUIRED_KEYS:
        if key not in bible:
            problems.append(f"missing top-level key: {key}")

    if problems:
        return problems  # nothing else is checkable

    for key in ("timeline", "geography", "factions", "conflicts"):
        if not isinstance(bible[key], list) or len(bible[key]) < 2:
            problems.append(f"{key} should be a list with at least 2 entries")

    for key in VISUAL_KEYS:
        if key not in bible.get("visual_style", {}):
            problems.append(f"visual_style missing: {key}")

    place_names = {p.get("name") for p in bible.get("geography", [])
                   if isinstance(p, dict)}
    faction_names = {f.get("name") for f in bible.get("factions", [])
                     if isinstance(f, dict)}

    # timeline coherence
    for event in bible.get("timeline", []):
        if not isinstance(event, dict):
            continue
        years = event.get("years_ago")
        if not isinstance(years, int) or years < 0:
            problems.append(
                f"timeline event '{event.get('event', '?')}' has bad "
                f"years_ago: {years!r}"
            )

    # referential integrity - the checks that actually catch contradictions
    for faction in bible.get("factions", []):
        if not isinstance(faction, dict):
            continue
        seat = faction.get("seat_of_power")
        if seat and seat not in place_names:
            problems.append(
                f"faction '{faction.get('name')}' sits in '{seat}', "
                f"which is not in geography"
            )

    known = place_names | faction_names
    for conflict in bible.get("conflicts", []):
        if not isinstance(conflict, dict):
            continue
        for party in conflict.get("parties", []):
            if party not in known:
                problems.append(
                    f"conflict party '{party}' is not a known faction or place"
                )

    for place in bible.get("geography", []):
        if not isinstance(place, dict):
            continue
        danger = place.get("danger_level")
        if not isinstance(danger, int) or not 1 <= danger <= 5:
            problems.append(
                f"place '{place.get('name')}' has danger_level {danger!r}, "
                f"expected 1-5"
            )

    return problems


def _repair(bible: dict, problems: list[str], brief: str) -> dict:
    """One targeted follow-up call that fixes only what validation flagged."""
    prompt = (
        f"Original brief: {brief}\n\n"
        f"Here is a world bible with problems:\n{json.dumps(bible, indent=2)}\n\n"
        f"Problems found:\n- " + "\n- ".join(problems) + "\n\n"
        "Return the COMPLETE corrected bible as JSON. Fix only the listed "
        "problems. Preserve every name, event and description that was not "
        "flagged - do not reinvent the world."
    )
    return generate_json(prompt, system=SYSTEM, temperature=0.4)


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

def generate_bible(brief: str, repair: bool = True, verbose: bool = True) -> dict:
    if verbose:
        print(f"[bible] generating from brief: {brief!r}")

    bible = generate_json(brief, system=SYSTEM, temperature=1.0)
    bible["_brief"] = brief

    problems = validate(bible)

    if problems and repair:
        if verbose:
            print(f"[bible] {len(problems)} problem(s), running repair pass")
            for p in problems:
                print(f"  - {p}")
        try:
            repaired = _repair(bible, problems, brief)
            repaired["_brief"] = brief
            remaining = validate(repaired)
            if len(remaining) < len(problems):
                bible, problems = repaired, remaining
        except LLMError as err:
            if verbose:
                print(f"[bible] repair failed, keeping original: {err}")

    bible["_validation_warnings"] = problems
    if verbose:
        status = "clean" if not problems else f"{len(problems)} warning(s)"
        print(f"[bible] done: {bible.get('world_name')} ({status})")
    return bible


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "world"


def save_bible(bible: dict, directory: str = BIBLE_DIR) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{slug(bible.get('world_name'))}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bible, fh, indent=2, ensure_ascii=False)
    return path


def load_bible(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    brief_arg = " ".join(sys.argv[1:]) or input("Describe your world: ")
    result = generate_bible(brief_arg)
    saved = save_bible(result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nsaved to {saved}")