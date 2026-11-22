"""Reusable end-to-end pipeline (used by the Streamlit web app; mirrors run.py)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from promo_finder.judge import judge_candidates_blind
from promo_finder.llm_client import LLMClient, PipelineCancelled
from promo_finder.moment_finder import find_episode_candidates
from promo_finder.parser import parse_episode_text
from promo_finder.ranker import rank_promos
from promo_finder.report import export_all_reports
from promo_finder.show_bible import build_show_bible
from promo_finder.stitcher import generate_cross_episode_promos
from promo_finder.validator import validate_candidates


def parse_uploaded_files(files: list[tuple[str, bytes]], show_title: str = "Uploaded Show") -> dict[str, Any]:
    """Parse in-memory uploads [(filename, raw_bytes), ...] into the same structure as parse_directory()."""
    episodes = []
    for name, raw in files:
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
        ep = parse_episode_text(text, filename=name)
        if ep["block_count"] == 0:
            raise ValueError(f"'{name}': no screenplay blocks found (unexpected format).")
        episodes.append(ep)
    episodes.sort(key=lambda e: e["episode_number"])
    nums = [e["episode_number"] for e in episodes]
    if len(set(nums)) != len(nums):
        raise ValueError(f"Duplicate/unknown episode numbers detected: {nums}")
    speakers = sorted({s for e in episodes for s in e["unique_speakers"]})
    return {
        "show_title": show_title,
        "episode_count": len(episodes),
        "total_duration_sec": round(sum(e["duration_sec"] for e in episodes), 2),
        "total_blocks": sum(e["block_count"] for e in episodes),
        "total_ellipsis_groups": sum(e["ellipsis_group_count"] for e in episodes),
        "all_unique_speakers": speakers,
        "episodes": episodes,
    }


def run_pipeline(
    parsed_show: dict[str, Any],
    client: LLMClient,
    out_dir: str | Path,
    progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run Steps 3-7 and export files. Returns dict with ranked promos, audit log, metrics, paths."""
    t0 = time.time()
    p = progress or (lambda frac, msg: None)
    n = max(1, parsed_show["episode_count"])

    def _check_cancel() -> None:
        if cancel_event and cancel_event.is_set():
            raise PipelineCancelled("Stopped by user.")

    _check_cancel()
    p(0.02, "Building Show Summary (Call 1)...")
    bible = build_show_bible(parsed_show, client)

    episode_results: list[dict[str, Any]] = []
    raw_candidates: list[dict[str, Any]] = []
    for i, ep in enumerate(parsed_show["episodes"], start=1):
        _check_cancel()
        p(0.05 + 0.6 * (i - 1) / n, f"Scanning episode {ep['episode_number']} ({i}/{n}) (Call 2)...")
        er = find_episode_candidates(ep, bible, client)
        episode_results.append(er)
        raw_candidates.extend(er.get("candidates", []))

    _check_cancel()
    p(0.68, "Stitching cross-episode promos (Call 3)...")
    raw_candidates.extend(generate_cross_episode_promos(parsed_show, bible, episode_results, client))

    _check_cancel()
    p(0.78, f"Validating {len(raw_candidates)} candidates...")
    valid, audit_log = validate_candidates(raw_candidates, parsed_show)

    _check_cancel()
    p(0.85, f"Blind judge scoring {len(valid)} valid promos (Call 4)...")
    judged = judge_candidates_blind(valid, client, batch_size=6, cancel_event=cancel_event)

    _check_cancel()
    p(0.95, "Ranking and exporting reports...")
    ranked = rank_promos(judged)
    metrics = dict(client.stats)
    metrics["estimated_cost_usd"] = client.estimate_cost_usd()
    metrics["elapsed_sec"] = round(time.time() - t0, 1)
    paths = export_all_reports(out_dir, parsed_show, bible, ranked, audit_log, metrics)
    p(1.0, "Done")
    return {
        "bible": bible,
        "ranked": ranked,
        "audit_log": audit_log,
        "metrics": metrics,
        "paths": paths,
    }
