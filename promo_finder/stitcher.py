"""Step 4b (Call 3): Cross-Episode Promo Stitcher.

Runs ONE LLM call over the Show Bible's continuous arcs + all notable stitch beats
and top episode moments extracted by Call 2. Proposes 5-8 high-impact multi-episode
stitched promos (20-60s total), including ReelShort/DramaBox-style flash-forward
cold opens (e.g., 4s peak shock from Ep 3 -> Ep 1 betrayal setup -> Ep 3 proposal).
"""

from __future__ import annotations

from typing import Any

from promo_finder.llm_client import LLMClient
from promo_finder.parser import compute_segment_span
from promo_finder.show_bible import compact_bible_for_prompt

SYSTEM_PROMPT = """You are a creative director for vertical microdrama paid-social ads (ReelShort, DramaBox, Kuku TV).
Your job is to assemble 5 to 8 killer CROSS-EPISODE STITCHED PROMOS (2 to 3 segments from different episodes, total combined duration strictly 20 to 60 seconds).

WINNING STITCHED AD FORMULAS:
1. FLASH-FORWARD COLD OPEN: Start with a 3-6s explosive visual/dialogue shock from a later episode (e.g., a punch, a slap, a public proposal, a dramatic rescue) -> cut back to the earlier episode setup (betrayal/humiliation) -> jump to the unresolved cliffhanger.
2. MINI-ARC MONTAGE: Stitch 2-3 consecutive episodes in order (e.g. Ep 1 altar betrayal -> Ep 3 guards dragging her + stranger rescuing & proposing) so a cold viewer gets the full emotional hook in 35-55 seconds.

STRICT RULES:
- Use ONLY exact `episode`, `start_block_id`, and `end_block_id` values from the provided beat menu (or tighter sub-ranges within those same episodes).
- Total summed duration of all segments in a promo MUST be between 22 and 58 seconds (aim for 30-50s).
- Every stitched promo MUST include segments from at least 2 DIFFERENT episodes.
- Segment order should be the intended playback order (flash-forward cold opens ARE encouraged!).
- Tag `spoiler_risk` honestly using the Show Bible's spoiler list.

Return STRICT JSON matching this schema:
{
  "stitched_promos": [
    {
      "candidate_type": "cross_episode_stitched",
      "arc_label": "short_snake_case_theme",
      "segments": [
        {
          "episode": 3,
          "start_block_id": "B056",
          "end_block_id": "B061",
          "role": "cold_open_hook"
        },
        {
          "episode": 1,
          "start_block_id": "B025",
          "end_block_id": "B036",
          "role": "setup"
        },
        {
          "episode": 3,
          "start_block_id": "B090",
          "end_block_id": "B105",
          "role": "cliffhanger"
        }
      ],
      "hook": "1 concise sentence describing the 0-3s opening hook",
      "cliffhanger": "1 concise sentence describing the unresolved cut at the end",
      "why_it_works_draft": "1 punchy line on why this cross-episode stitch converts cold viewers",
      "spoiler_risk": "none | low | medium | high",
      "spoiler_reason": "short phrase or 'None'"
    }
  ]
}"""


def _resolve_beat_preview(
    ep_map: dict[int, dict[str, Any]], ep_num: int, start_id: str, end_id: str
) -> str | None:
    ep = ep_map.get(ep_num)
    if not ep:
        return None
    id_to_idx = {b["block_id"]: i for i, b in enumerate(ep["blocks"])}
    if start_id not in id_to_idx or end_id not in id_to_idx:
        return None
    s_i, e_i = id_to_idx[start_id], id_to_idx[end_id]
    if s_i > e_i:
        return None
    sub = ep["blocks"][s_i : e_i + 1]
    span = compute_segment_span(sub)
    dialogues = [
        f"{b['speaker']}: {b['text']}" if b["block_type"] == "DIALOGUE" else f"[{b['block_type']}] {b['text']}"
        for b in sub
        if b["block_type"] in ("DIALOGUE", "ACTION")
    ]
    snippet = " / ".join(dialogues[:4])[:220]
    return f"Ep {ep_num:02d} {start_id}-{end_id} ({span['start_ts']}-{span['end_ts']}, {span['duration_sec']:.1f}s): {snippet}"


def generate_cross_episode_promos(
    parsed_show: dict[str, Any],
    bible: dict[str, Any],
    episode_results: list[dict[str, Any]],
    client: LLMClient,
) -> list[dict[str, Any]]:
    """Run Call 3 to synthesize cross-episode stitched promos."""
    ep_map = {ep["episode_number"]: ep for ep in parsed_show["episodes"]}
    lines = [
        "=== SHOW BIBLE ===",
        compact_bible_for_prompt(bible),
        "",
        "=== CONTINUOUS ARCS ===",
    ]
    for arc in bible.get("continuous_arcs", []):
        lines.append(
            f"- {arc.get('arc_id')} (Eps {arc.get('episodes')}): {arc.get('title')} — {arc.get('core_conflict')}"
        )

    lines.append("\n=== AVAILABLE STITCH BEATS (with exact verified durations) ===")
    beat_count = 0
    for er in episode_results:
        ep_num = er.get("episode_number")
        for beat in er.get("notable_stitch_beats", []):
            prev = _resolve_beat_preview(
                ep_map,
                int(beat.get("episode", ep_num)),
                str(beat.get("start_block_id", "")),
                str(beat.get("end_block_id", "")),
            )
            if prev:
                lines.append(
                    f"  * [{beat.get('beat_type', 'beat')}] {prev} | Note: {beat.get('summary', '')}"
                )
                beat_count += 1
        # Also expose strong episode candidate segments (trimmed previews) if short enough
        for cand in er.get("candidates", []):
            for seg in cand.get("segments", []):
                prev = _resolve_beat_preview(
                    ep_map,
                    int(seg.get("episode", ep_num)),
                    str(seg.get("start_block_id", "")),
                    str(seg.get("end_block_id", "")),
                )
                if prev:
                    lines.append(f"  * [cand_seg:{seg.get('role', 'main')}] {prev}")
                    beat_count += 1

    if beat_count < 2:
        return []

    user_prompt = "\n".join(lines)
    res = client.call_json("cross_episode_stitcher", SYSTEM_PROMPT, user_prompt)
    return res.get("stitched_promos", [])
