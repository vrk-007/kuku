"""Step 4a (Call 2): Per-Episode Promo Candidate & Stitch-Beat Finder.

Processes 1 episode at a time with:
  - Compact Show Bible (canonical characters, aliases, spoiler list)
  - Full formatted episode script (CAMERA, ACTION, SOUND FX, DIALOGUE + tone + [SENT_GRP:N])

Returns exact block IDs (`B001`..`Bxxx`) for:
  - 0 to 2 promo candidates (continuous OR intra-episode stitched, 20-60s)
  - 0 to 2 short notable stitch beats (3-12s) for Call 3 (Cross-Episode Stitcher)
  - `spoiler_risk` evaluated with Show Bible context (NOT delegated to the blind judge)
"""

from __future__ import annotations

from typing import Any

from promo_finder.llm_client import LLMClient
from promo_finder.parser import format_episode_for_llm
from promo_finder.show_bible import compact_bible_for_prompt

SYSTEM_PROMPT = """You are an elite performance-marketing video editor for vertical microdrama apps (ReelShort, DramaBox, Kuku TV).
Your job is to inspect ONE episode screenplay (with numbered blocks B001..Bxxx, exact timestamps, CAMERA, ACTION, SOUND FX, and DIALOGUE) and find the best 20 to 60-second social-media ad promo candidates.

WHAT MAKES A WINNING MICRODRAMA PROMO:
1. HOOK (0-3 seconds): Stops a scrolling stranger instantly — a slap, punch, shocking betrayal line, altar abandonment, sudden kneel/proposal, or dramatic visual close-up. NEVER start on a slow/quiet establishing shot or empty hallway.
2. CLIFFHANGER ENDING: Cuts at an unresolved emotional or plot peak (a threat, reveal, accusation, proposal, or cold-defiance close-up) so the viewer MUST click to see what happens next.
3. STANDALONE CLARITY: Makes emotional sense to someone who has NEVER seen the show.
4. DURATION: Total duration of segments MUST be between 20 and 60 seconds (calculate segment duration as last_block_end - first_block_start, since CAMERA/ACTION/DIALOGUE overlap).
5. CLEAN CUTS: Never split blocks that share the same [SENT_GRP:N] tag. If a dialogue line is in [SENT_GRP:N], include the entire sentence group.
6. INTRA-EPISODE STITCHING ALLOWED: You may propose either:
   - "continuous": 1 continuous segment (e.g. B015 to B048), OR
   - "intra_episode_stitched": 2 segments from this episode (e.g. a 3-5s flash-forward shocking hook -> earlier buildup -> climax/cliffhanger), totaling 20-60s.
7. BE SELECTIVE: If an episode is calm, purely transitional, or has no real hook/cliffhanger (e.g. cozy fireplace ending), set `episode_has_promo: false` and return an empty `candidates` list. Do NOT force weak promos.
8. SPOILER CHECK: Using the Show Bible's spoiler list, honestly tag `spoiler_risk` as "none", "low", "medium", or "high".

Return STRICT JSON matching this schema:
{
  "episode_number": 1,
  "episode_has_promo": true,
  "episode_summary_beat": "1-sentence dramatic summary of what happens in this episode",
  "candidates": [
    {
      "candidate_type": "continuous | intra_episode_stitched",
      "arc_label": "short_snake_case_theme (e.g. altar_betrayal, stranger_rescue, witchcraft_trial)",
      "segments": [
        {
          "episode": 1,
          "start_block_id": "B012",
          "end_block_id": "B045",
          "role": "cold_open_hook | setup | main | cliffhanger"
        }
      ],
      "hook": "1 concise sentence describing what happens in the first 0-3 seconds",
      "cliffhanger": "1 concise sentence describing the unresolved cut point at the end",
      "why_it_works_draft": "1 punchy line on why this pulls in a scrolling viewer",
      "spoiler_risk": "none | low | medium | high",
      "spoiler_reason": "short phrase or 'None'"
    }
  ],
  "notable_stitch_beats": [
    {
      "episode": 1,
      "start_block_id": "B025",
      "end_block_id": "B030",
      "beat_type": "hook_peak | betrayal_setup | rescue_action | reveal_cliffhanger",
      "summary": "short 1-line description of this 3-15s beat for cross-episode stitching"
    }
  ]
}"""


def find_episode_candidates(
    episode: dict[str, Any],
    bible: dict[str, Any],
    client: LLMClient,
) -> dict[str, Any]:
    """Run Call 2 on a single parsed episode."""
    bible_txt = compact_bible_for_prompt(bible)
    script_txt = format_episode_for_llm(episode)
    user_prompt = (
        f"=== SHOW BIBLE ===\n{bible_txt}\n\n"
        f"=== EPISODE SCRIPT ===\n{script_txt}\n\n"
        f"Identify 0-2 best promo candidates (20-60s total) and 0-2 short notable_stitch_beats (3-15s) "
        f"from Episode {episode['episode_number']}. Use exact block IDs like B012."
    )
    res = client.call_json(f"ep_{episode['episode_number']:02d}", SYSTEM_PROMPT, user_prompt)
    res["episode_number"] = episode["episode_number"]
    return res
