"""Step 6 (Call 4): Strictly Blind Cold-Viewer Judge.

Receives ONLY the raw clip script blocks (CAMERA, ACTION, SOUND FX, DIALOGUE)
with ZERO Show Bible, ZERO episode logline, and ZERO continuity notes.
This genuinely tests whether a scrolling stranger on TikTok/Reels understands
the clip and gets hooked in 3 seconds.

Note: `spoiler_risk` is intentionally NOT judged here (computed in Call 2/3
with Show Bible context).
"""

from __future__ import annotations

import threading
from typing import Any

from promo_finder.llm_client import LLMClient, PipelineCancelled

SYSTEM_PROMPT = """You are a cold-audience viewer scrolling Instagram Reels / TikTok who has NEVER heard of this show before.
You will be given a batch of candidate promo clips (ONLY their raw screenplay blocks: CAMERA, ACTION, SOUND FX, DIALOGUE — you have NO background info about the show).

For EACH candidate, score strictly and honestly (do NOT inflate scores — a 9 or 10 should be rare):
1. `hook_3s` (1-10): Does the VERY FIRST 0 to 3 seconds make you stop scrolling? (Physical action, shocking line, intense visual reveal = 8-10; slow setup or unclear dialogue = 3-5.)
2. `cliffhanger` (1-10): Does the ending cut at an unresolved emotional or plot peak that makes you desperate to click "Watch Now"? (Resolved speech or calm fade = 2-4.)
3. `standalone_clarity` (1-10): With ZERO prior knowledge of the show, do you immediately understand who is in conflict, what is at stake, and why it hurts/matters? (Confusing pronouns or unexplained names = 3-5.)

Also output:
- `why_it_works`: One crisp, specific sentence explaining why this promo pulls in a cold viewer (or why it falls short).
- `quality_flags`: Array of applicable flags from: ["needs_context", "weak_hook", "weak_ending", "visual_only_hook", "abrupt_stitch"] (or [] if clean).
- `honest_critique`: One honest sentence pointing out the clip's biggest weakness or failure risk for an editor.

Return STRICT JSON matching this schema:
{
  "evaluations": [
    {
      "candidate_id": "C001",
      "hook_3s": 8.5,
      "cliffhanger": 9.0,
      "standalone_clarity": 8.0,
      "why_it_works": "One concise sentence.",
      "quality_flags": [],
      "honest_critique": "One honest sentence on its main weakness or risk."
    }
  ]
}"""


def judge_candidates_blind(
    valid_candidates: list[dict[str, Any]],
    client: LLMClient,
    batch_size: int = 6,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Run Call 4 in batches over all validated candidates."""
    eval_map: dict[str, dict[str, Any]] = {}

    for i in range(0, len(valid_candidates), batch_size):
        if cancel_event and cancel_event.is_set():
            raise PipelineCancelled("Stopped by user.")
        batch = valid_candidates[i : i + batch_size]
        batch_num = (i // batch_size) + 1
        parts = [f"Evaluate these {len(batch)} promo clips as a strictly blind first-time viewer:\n"]
        for c in batch:
            parts.append(
                f"--- CANDIDATE {c['candidate_id']} | Type: {c['candidate_type']} | "
                f"Total Duration: {c['total_duration_sec']:.1f}s ---\n"
                f"{c['blind_clip_script']}\n"
            )
        user_prompt = "\n".join(parts)
        res = client.call_json(f"blind_judge_b{batch_num:02d}", SYSTEM_PROMPT, user_prompt)
        for ev in res.get("evaluations", []):
            cid = ev.get("candidate_id")
            if cid:
                eval_map[cid] = ev

    judged: list[dict[str, Any]] = []
    for c in valid_candidates:
        ev = eval_map.get(c["candidate_id"], {})
        c_copy = dict(c)
        c_copy["scores"] = {
            "hook_3s": float(ev.get("hook_3s", 5.0)),
            "cliffhanger": float(ev.get("cliffhanger", 5.0)),
            "standalone_clarity": float(ev.get("standalone_clarity", 5.0)),
        }
        c_copy["why_it_works"] = ev.get("why_it_works") or c.get("why_it_works_draft", "")
        c_copy["quality_flags"] = [
            f for f in ev.get("quality_flags", []) if f and f.lower() != "none"
        ]
        c_copy["honest_critique"] = ev.get("honest_critique", "")
        judged.append(c_copy)

    return judged
