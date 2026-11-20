"""Step 5: Deterministic Validator, Boundary Snapper, Deduplicator & Rejection Audit Log.

Enforces hard promo rules in pure Python (0 LLM tokens):
  - All block IDs exist in the parsed screenplay (zero hallucinated timestamps).
  - Intra-segment chronological order (`start_block_index <= end_block_index`).
    Cross-segment order is preserved as-is to support flash-forward cold opens.
  - Ellipsis sentence-group integrity (never cuts mid-sentence).
  - True overlap duration math: `max(end_sec) - min(start_sec)` per segment.
  - Auto-snap borderline durations within ±4s (16-20s or 60-64s) to clean boundaries.
  - Hard 20.0s - 60.0s duration gate.
  - Deduplication of >= 65% overlapping single-episode candidates.
  - Writes every rejection and auto-snap to `validation_audit_log.json`.
"""

from __future__ import annotations

from typing import Any

from promo_finder.parser import compute_segment_span, format_block_line

MIN_DURATION = 20.0
MAX_DURATION = 60.0
SNAP_TOLERANCE = 4.0  # Auto-snap clips within 16-20s or 60-64s


def _expand_for_sentence_groups(
    blocks: list[dict[str, Any]], start_idx: int, end_idx: int
) -> tuple[int, int, bool]:
    """Expand [start_idx, end_idx] if either boundary lands inside an ellipsis sentence group."""
    changed = False
    s_grp = blocks[start_idx].get("sentence_group_id")
    if s_grp is not None:
        while start_idx > 0 and blocks[start_idx - 1].get("sentence_group_id") == s_grp:
            start_idx -= 1
            changed = True
        # Also catch any earlier block in the same group separated by CAMERA/ACTION
        for i in range(start_idx - 1, max(-1, start_idx - 6), -1):
            if blocks[i].get("sentence_group_id") == s_grp:
                start_idx = i
                changed = True

    e_grp = blocks[end_idx].get("sentence_group_id")
    if e_grp is not None:
        for i in range(end_idx + 1, min(len(blocks), end_idx + 6)):
            if blocks[i].get("sentence_group_id") == e_grp:
                end_idx = i
                changed = True

    return start_idx, end_idx, changed


def _total_duration(resolved_segments: list[dict[str, Any]]) -> float:
    return round(sum(seg["duration_sec"] for seg in resolved_segments), 2)


def _build_resolved_segment(
    ep: dict[str, Any], s_idx: int, e_idx: int, role: str
) -> dict[str, Any]:
    sub = ep["blocks"][s_idx : e_idx + 1]
    span = compute_segment_span(sub)
    return {
        "episode": ep["episode_number"],
        "role": role,
        "start_block_id": ep["blocks"][s_idx]["block_id"],
        "end_block_id": ep["blocks"][e_idx]["block_id"],
        "start_index": s_idx,
        "end_index": e_idx,
        "start_ts": span["start_ts"],
        "end_ts": span["end_ts"],
        "start_sec": span["start_sec"],
        "end_sec": span["end_sec"],
        "duration_sec": span["duration_sec"],
        "block_count": len(sub),
        "raw_script_lines": [format_block_line(b) for b in sub],
    }


def _try_auto_snap(
    resolved: list[dict[str, Any]], ep_map: dict[int, dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None]:
    """If total duration is within ±4s of [20, 60], try adjusting the last segment."""
    dur = _total_duration(resolved)
    if MIN_DURATION <= dur <= MAX_DURATION:
        return resolved, None

    # Adjust the longest segment (usually 'main' or 'cliffhanger')
    target_pos = max(range(len(resolved)), key=lambda i: resolved[i]["duration_sec"])
    seg = resolved[target_pos]
    ep = ep_map[seg["episode"]]
    blocks = ep["blocks"]
    s_idx, e_idx = seg["start_index"], seg["end_index"]

    if (MIN_DURATION - SNAP_TOLERANCE) <= dur < MIN_DURATION:
        # Try expanding end_index up to 4 blocks (or start_index back up to 3 blocks)
        best = None
        for step in range(1, 5):
            if e_idx + step < len(blocks):
                ns, ne, _ = _expand_for_sentence_groups(blocks, s_idx, e_idx + step)
                cand_seg = _build_resolved_segment(ep, ns, ne, seg["role"])
                cand_list = list(resolved)
                cand_list[target_pos] = cand_seg
                nd = _total_duration(cand_list)
                if MIN_DURATION <= nd <= MAX_DURATION:
                    best = (cand_list, f"auto_snap_expand: {dur:.1f}s -> {nd:.1f}s")
                    break
        if best is None:
            for step in range(1, 4):
                if s_idx - step >= 0:
                    ns, ne, _ = _expand_for_sentence_groups(blocks, s_idx - step, e_idx)
                    cand_seg = _build_resolved_segment(ep, ns, ne, seg["role"])
                    cand_list = list(resolved)
                    cand_list[target_pos] = cand_seg
                    nd = _total_duration(cand_list)
                    if MIN_DURATION <= nd <= MAX_DURATION:
                        best = (cand_list, f"auto_snap_expand_start: {dur:.1f}s -> {nd:.1f}s")
                        break
        if best:
            return best

    elif MAX_DURATION < dur <= (MAX_DURATION + SNAP_TOLERANCE):
        # Try trimming end_index back up to 4 blocks (or start_index forward up to 3 blocks)
        for step in range(1, 5):
            if e_idx - step > s_idx:
                ns, ne, _ = _expand_for_sentence_groups(blocks, s_idx, e_idx - step)
                if ne <= ns:
                    continue
                cand_seg = _build_resolved_segment(ep, ns, ne, seg["role"])
                cand_list = list(resolved)
                cand_list[target_pos] = cand_seg
                nd = _total_duration(cand_list)
                if MIN_DURATION <= nd <= MAX_DURATION:
                    return cand_list, f"auto_snap_trim: {dur:.1f}s -> {nd:.1f}s"

    return resolved, None


def _overlap_ratio(seg_a: dict[str, Any], seg_b: dict[str, Any]) -> float:
    if seg_a["episode"] != seg_b["episode"]:
        return 0.0
    inter = max(0.0, min(seg_a["end_sec"], seg_b["end_sec"]) - max(seg_a["start_sec"], seg_b["start_sec"]))
    union = max(seg_a["end_sec"], seg_b["end_sec"]) - min(seg_a["start_sec"], seg_b["start_sec"])
    return inter / union if union > 0 else 0.0


def validate_candidates(
    raw_candidates: list[dict[str, Any]],
    parsed_show: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate, snap, deduplicate candidates and produce a structured audit log."""
    ep_map = {ep["episode_number"]: ep for ep in parsed_show["episodes"]}
    valid: list[dict[str, Any]] = []
    rejected_entries: list[dict[str, Any]] = []
    adjusted_entries: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}

    def _reject(
        cand: dict[str, Any],
        rule: str,
        detail: str,
        resolved_segs: list[dict[str, Any]] | None = None,
        total_duration_sec: float | None = None,
    ) -> None:
        rule_counts[rule] = rule_counts.get(rule, 0) + 1
        segs_summary = [
            {
                "episode": s["episode"],
                "start_ts": s["start_ts"],
                "end_ts": s["end_ts"],
                "duration_sec": s["duration_sec"],
            }
            for s in (resolved_segs or [])
        ]
        if total_duration_sec is None and resolved_segs:
            total_duration_sec = _total_duration(resolved_segs)
        rejected_entries.append(
            {
                "candidate_id": cand.get("candidate_id"),
                "candidate_type": cand.get("candidate_type"),
                "arc_label": cand.get("arc_label"),
                "rule_violated": rule,
                "detail": detail,
                "total_duration_sec": total_duration_sec,
                "segments": segs_summary,
                "raw_segments": cand.get("segments", []),
            }
        )

    for idx, cand in enumerate(raw_candidates, start=1):
        cand_id = cand.get("candidate_id") or f"C{idx:03d}"
        cand["candidate_id"] = cand_id
        segs = cand.get("segments", [])
        if not segs:
            _reject(cand, "EMPTY_SEGMENTS", "Candidate has no segments.")
            continue

        resolved: list[dict[str, Any]] = []
        failed = False
        sentence_snapped = False

        for seg in segs:
            ep_num = seg.get("episode")
            if ep_num not in ep_map:
                _reject(cand, "INVALID_EPISODE", f"Episode {ep_num} does not exist.")
                failed = True
                break
            ep = ep_map[ep_num]
            id_to_idx = {b["block_id"]: i for i, b in enumerate(ep["blocks"])}
            s_id = str(seg.get("start_block_id", "")).strip().upper()
            e_id = str(seg.get("end_block_id", "")).strip().upper()

            if s_id not in id_to_idx or e_id not in id_to_idx:
                _reject(
                    cand,
                    "HALLUCINATED_BLOCK_ID",
                    f"Ep {ep_num}: {s_id} or {e_id} not found in screenplay.",
                )
                failed = True
                break

            s_idx, e_idx = id_to_idx[s_id], id_to_idx[e_id]
            if s_idx > e_idx:
                _reject(
                    cand,
                    "REVERSED_INTRA_SEGMENT",
                    f"Ep {ep_num}: start {s_id} comes after end {e_id}.",
                )
                failed = True
                break

            ns, ne, snapped = _expand_for_sentence_groups(ep["blocks"], s_idx, e_idx)
            if snapped:
                sentence_snapped = True
            resolved.append(_build_resolved_segment(ep, ns, ne, seg.get("role", "main")))

        if failed:
            continue

        # Cross-episode check for stitched promos
        if cand.get("candidate_type") == "cross_episode_stitched":
            distinct_eps = {s["episode"] for s in resolved}
            if len(distinct_eps) < 2:
                _reject(
                    cand,
                    "STITCH_SINGLE_EPISODE",
                    f"Cross-episode promo only uses episode(s) {sorted(distinct_eps)}.",
                    resolved_segs=resolved,
                )
                continue

        # Auto-snap ±4s if borderline
        orig_dur = _total_duration(resolved)
        resolved, snap_note = _try_auto_snap(resolved, ep_map)
        final_dur = _total_duration(resolved)

        if snap_note or sentence_snapped:
            adjusted_entries.append(
                {
                    "candidate_id": cand_id,
                    "orig_duration_sec": orig_dur,
                    "final_duration_sec": final_dur,
                    "sentence_group_snapped": sentence_snapped,
                    "duration_snap_note": snap_note,
                }
            )

        if final_dur < MIN_DURATION:
            _reject(
                cand,
                "DURATION_TOO_SHORT",
                f"Duration {final_dur:.1f}s < {MIN_DURATION}s (even after ±{SNAP_TOLERANCE}s snap).",
                resolved_segs=resolved,
                total_duration_sec=final_dur,
            )
            continue
        if final_dur > MAX_DURATION:
            _reject(
                cand,
                "DURATION_TOO_LONG",
                f"Duration {final_dur:.1f}s > {MAX_DURATION}s (even after ±{SNAP_TOLERANCE}s snap).",
                resolved_segs=resolved,
                total_duration_sec=final_dur,
            )
            continue

        # Check 0-3s hook activity (flag if purely static establishing shot)
        first_ep = ep_map[resolved[0]["episode"]]
        first_sub = first_ep["blocks"][resolved[0]["start_index"] : resolved[0]["end_index"] + 1]
        seg_start = resolved[0]["start_sec"]
        early_blocks = [b for b in first_sub if b["start_sec"] <= seg_start + 3.5]
        has_active_opener = any(
            b["block_type"] in ("DIALOGUE", "ACTION", "SOUND FX") for b in early_blocks
        )

        # Deduplicate overlapping single-segment candidates on the same episode
        if len(resolved) == 1:
            is_dup = False
            for existing in valid:
                if len(existing["segments"]) == 1:
                    ov = _overlap_ratio(resolved[0], existing["segments"][0])
                    if ov >= 0.65:
                        _reject(
                            cand,
                            "DUPLICATE_OVERLAP",
                            f"Overlaps {ov*100:.0f}% with {existing['candidate_id']} on Ep {resolved[0]['episode']}.",
                            resolved_segs=resolved,
                            total_duration_sec=final_dur,
                        )
                        is_dup = True
                        break
            if is_dup:
                continue

        # Assemble clean validated candidate
        episodes_used = []
        for s in resolved:
            if s["episode"] not in episodes_used:
                episodes_used.append(s["episode"])

        # Build blind script text for Call 4 (NO Show Bible, NO loglines)
        blind_lines: list[str] = []
        for i, s in enumerate(resolved, start=1):
            if len(resolved) > 1:
                blind_lines.append(f"=== CLIP SEGMENT {i} ({s['duration_sec']:.1f}s) ===")
            blind_lines.extend(s["raw_script_lines"])

        valid.append(
            {
                "candidate_id": cand_id,
                "candidate_type": cand.get("candidate_type", "continuous"),
                "arc_label": cand.get("arc_label", "general_drama"),
                "episodes": episodes_used,
                "total_duration_sec": final_dur,
                "has_active_3s_opener": has_active_opener,
                "auto_adjusted": bool(snap_note or sentence_snapped),
                "adjustment_note": snap_note or ("sentence_group_snap" if sentence_snapped else None),
                "segments": [
                    {
                        "episode": s["episode"],
                        "role": s["role"],
                        "start_block_id": s["start_block_id"],
                        "end_block_id": s["end_block_id"],
                        "start_ts": s["start_ts"],
                        "end_ts": s["end_ts"],
                        "start_sec": s["start_sec"],
                        "end_sec": s["end_sec"],
                        "duration_sec": s["duration_sec"],
                    }
                    for s in resolved
                ],
                "hook": cand.get("hook", ""),
                "cliffhanger": cand.get("cliffhanger", ""),
                "why_it_works_draft": cand.get("why_it_works_draft", ""),
                "spoiler_risk": cand.get("spoiler_risk", "none"),
                "spoiler_reason": cand.get("spoiler_reason", "None"),
                "blind_clip_script": "\n".join(blind_lines),
            }
        )

    audit_log = {
        "total_raw_candidates": len(raw_candidates),
        "total_valid": len(valid),
        "total_rejected": len(rejected_entries),
        "total_auto_adjusted": len(adjusted_entries),
        "rejection_counts_by_rule": rule_counts,
        "auto_adjustments": adjusted_entries,
        "rejected_candidates": rejected_entries,
    }
    return valid, audit_log
