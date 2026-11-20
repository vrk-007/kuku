"""Step 7a: Deterministic Ranker with Spoiler Penalty, Cold-Audience Bonus & Diversity Cap."""

from __future__ import annotations

from typing import Any

SPOILER_PENALTIES = {
    "none": 0.0,
    "low": -0.2,
    "medium": -0.7,
    "high": -1.5,
}

FLAG_PENALTIES = {
    "needs_context": -0.6,
    "weak_hook": -0.6,
    "weak_ending": -0.5,
    "abrupt_stitch": -0.5,
    "visual_only_hook": -0.15,
}


def _score_candidate(c: dict[str, Any]) -> tuple[float, dict[str, float]]:
    sc = c["scores"]
    base = 0.38 * sc["hook_3s"] + 0.32 * sc["cliffhanger"] + 0.30 * sc["standalone_clarity"]

    eps = c.get("episodes", [])
    max_ep = max(eps) if eps else 50
    if max_ep <= 10:
        cold_bonus = 0.40
    elif max_ep <= 20:
        cold_bonus = 0.15
    else:
        cold_bonus = 0.0

    spoiler_pen = SPOILER_PENALTIES.get(str(c.get("spoiler_risk", "none")).lower(), 0.0)

    stitch_bonus = 0.0
    if c.get("candidate_type") in ("cross_episode_stitched", "intra_episode_stitched"):
        if sc["standalone_clarity"] >= 7.0:
            stitch_bonus = 0.25

    flag_pen = sum(FLAG_PENALTIES.get(f, 0.0) for f in c.get("quality_flags", []))
    opener_pen = 0.0 if c.get("has_active_3s_opener", True) else -0.4

    final_score = round(max(0.0, min(10.0, base + cold_bonus + spoiler_pen + stitch_bonus + flag_pen + opener_pen)), 2)
    breakdown = {
        "weighted_base": round(base, 2),
        "cold_audience_bonus": cold_bonus,
        "spoiler_penalty": spoiler_pen,
        "stitch_bonus": stitch_bonus,
        "flag_penalty": round(flag_pen, 2),
        "opener_penalty": opener_pen,
    }
    return final_score, breakdown


def rank_promos(
    judged_candidates: list[dict[str, Any]],
    max_per_arc_in_top10: int = 2,
    max_per_ep_in_top10: int = 2,
) -> list[dict[str, Any]]:
    """Compute final scores and apply Top-10 diversity cap so similar scenes don't flood the top."""
    scored: list[dict[str, Any]] = []
    for c in judged_candidates:
        item = dict(c)
        final_score, breakdown = _score_candidate(item)
        item["final_score"] = final_score
        item["score_breakdown"] = breakdown
        scored.append(item)

    scored.sort(
        key=lambda x: (
            x["final_score"],
            x["scores"]["hook_3s"],
            x["scores"]["cliffhanger"],
        ),
        reverse=True,
    )

    top10: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    arc_counts: dict[str, int] = {}
    ep_counts: dict[int, int] = {}

    for item in scored:
        arc = item.get("arc_label", "general")
        primary_ep = item["episodes"][0] if item.get("episodes") else 0
        if len(top10) < 10:
            if arc_counts.get(arc, 0) < max_per_arc_in_top10 and ep_counts.get(primary_ep, 0) < max_per_ep_in_top10:
                item["diversity_deferred"] = False
                top10.append(item)
                arc_counts[arc] = arc_counts.get(arc, 0) + 1
                ep_counts[primary_ep] = ep_counts.get(primary_ep, 0) + 1
            else:
                item["diversity_deferred"] = True
                deferred.append(item)
        else:
            item["diversity_deferred"] = False
            deferred.append(item)

    ranked = top10 + deferred
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx
        sp = str(item.get("spoiler_risk", "none")).lower()
        flags = item.get("quality_flags", [])
        if idx <= 5 and sp in ("none", "low") and "needs_context" not in flags:
            item["posting_tier"] = "Tier 1: Post First (Cold Traffic)"
        elif idx <= 15 and sp != "high":
            item["posting_tier"] = "Tier 2: Scale / A-B Test"
        else:
            item["posting_tier"] = "Tier 3: Retargeting / Editor Review"

    return ranked
