"""Step 3 (Call 1): Show Bible Generator.

Makes ONE lightweight LLM call over all 50 episode loglines, continuity notes,
and deterministically extracted per-episode unique speaker tags.

Outputs:
  - premise (2 sentences)
  - characters + script-only alias map (resolving descriptive/role tags to canonical names)
  - continuous_arcs (multi-episode story groups for cross-episode stitching)
  - major_spoilers (late twists/reveals so Call 2/3 & Ranker can flag spoiler_risk)
"""

from __future__ import annotations

from typing import Any

from promo_finder.llm_client import LLMClient

SYSTEM_PROMPT = """You are a senior story editor for vertical microdrama apps (like ReelShort, DramaBox, Kuku TV).
You will receive the episode loglines, continuity notes, and raw script speaker tags for an entire microdrama series.

Your job is to build a concise, accurate JSON Show Bible that downstream promo-finding stages will rely on.
Pay special attention to resolving visual, descriptive, or role-based script speaker tags (such as appearance descriptions, unnamed titles, or generic roles) to their canonical character names using the episode loglines and continuity notes.

Return STRICT JSON matching this exact schema:
{
  "show_title": "string",
  "premise": "2-sentence cold-audience summary of the show's core hook",
  "characters": [
    {
      "canonical_name": "string",
      "role": "protagonist | love_interest | antagonist | family | supporting",
      "description": "1 short phrase",
      "aliases": ["EXACT UPPERCASE SCRIPT TAGS that refer to this character"]
    }
  ],
  "continuous_arcs": [
    {
      "arc_id": "snake_case_id",
      "title": "short arc name",
      "episodes": [1, 2, 3],
      "core_conflict": "1-sentence description",
      "is_cold_audience_friendly": true
    }
  ],
  "major_spoilers": [
    {
      "episode": 35,
      "twist": "short description of the late twist or identity reveal",
      "severity": "high | medium"
    }
  ]
}"""


def build_show_bible(parsed_show: dict[str, Any], client: LLMClient) -> dict[str, Any]:
    """Run Call 1 to generate the Show Bible."""
    lines = [
        f"SHOW TITLE: {parsed_show['show_title']}",
        f"TOTAL EPISODES: {parsed_show['episode_count']}",
        f"ALL UNIQUE SCRIPT SPEAKER TAGS: {', '.join(parsed_show['all_unique_speakers'])}",
        "",
        "EPISODE SUMMARIES & PER-EPISODE SPEAKER TAGS:",
    ]
    for ep in parsed_show["episodes"]:
        spks = ", ".join(ep["unique_speakers"])
        lines.append(
            f"Ep {ep['episode_number']:02d} ({ep['duration_ts']}): "
            f"LOGLINE: {ep['logline']} | "
            f"CONTINUITY: {ep['continuity_notes']} | "
            f"SPEAKERS: [{spks}]"
        )

    user_prompt = "\n".join(lines)
    bible = client.call_json("show_bible", SYSTEM_PROMPT, user_prompt)
    return bible


def compact_bible_for_prompt(bible: dict[str, Any]) -> str:
    """Render a compact text version of the Show Bible for Call 2 and Call 3."""
    lines = [
        f"SHOW PREMISE: {bible.get('premise', '')}",
        "CHARACTERS & ALIASES:",
    ]
    for c in bible.get("characters", []):
        aliases = ", ".join(c.get("aliases", []))
        lines.append(
            f"  - {c.get('canonical_name')} ({c.get('role')}): {c.get('description')} | Script tags: [{aliases}]"
        )
    lines.append("MAJOR SPOILERS TO WATCH FOR:")
    for s in bible.get("major_spoilers", []):
        lines.append(f"  - Ep {s.get('episode')} [{s.get('severity')}]: {s.get('twist')}")
    return "\n".join(lines)
