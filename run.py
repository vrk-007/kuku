"""Main CLI entry point for the Microdrama Promo Finder pipeline.

Usage:
  python run.py --input-dir "I Married a Broken Soldier. He was a Duke"
  python run.py --input-dir "I Married a Broken Soldier. He was a Duke" --provider gemini --model gemini-2.5-flash
  python run.py --input-dir "I Married a Broken Soldier. He was a Duke" --max-episodes 5  # quick smoke test
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from promo_finder.judge import judge_candidates_blind
from promo_finder.llm_client import LLMClient
from promo_finder.moment_finder import find_episode_candidates
from promo_finder.parser import parse_directory
from promo_finder.ranker import rank_promos
from promo_finder.report import export_all_reports
from promo_finder.show_bible import build_show_bible
from promo_finder.stitcher import generate_cross_episode_promos
from promo_finder.validator import validate_candidates


def main() -> None:
    ap = argparse.ArgumentParser(description="Find and rank 20-60s microdrama promos across all episodes.")
    ap.add_argument("--input-dir", required=True, help="Folder of screenplay .txt files (or single .txt file)")
    ap.add_argument("--out-dir", default="outputs", help="Output directory for raw JSON/CSV/HTML artifacts")
    ap.add_argument("--provider", default=None, help="LLM provider: gemini | openai | anthropic (default from .env)")
    ap.add_argument("--model", default=None, help="Model name override (default from .env or provider default)")
    ap.add_argument("--max-episodes", type=int, default=None, help="Optional limit for quick smoke test (e.g. 5)")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between API calls (helps avoid free-tier 429s)")
    ap.add_argument("--no-cache", action="store_true", help="Disable disk cache and force fresh API calls")
    args = ap.parse_args()

    t0 = time.time()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Parse
    print(f"[1/7] Parsing screenplays from: {args.input_dir}")
    parsed_show = parse_directory(args.input_dir)
    if args.max_episodes:
        parsed_show["episodes"] = parsed_show["episodes"][: args.max_episodes]
        parsed_show["episode_count"] = len(parsed_show["episodes"])
    (out_dir / "parsed_episodes.json").write_text(
        json.dumps(parsed_show, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"      Parsed {parsed_show['episode_count']} episodes | "
        f"{parsed_show['total_blocks']} blocks | "
        f"{len(parsed_show['all_unique_speakers'])} unique speaker tags"
    )

    # Step 2: Init swappable LLM client
    client = LLMClient(
        provider=args.provider,
        model=args.model,
        cache_dir=out_dir / ".cache",
        min_delay_sec=args.delay,
    )
    print(f"[2/7] LLM Client ready -> provider={client.provider}, model={client.model}")

    # Step 3: Show Bible (Call 1)
    print("[3/7] Building Show Bible (Call 1: aliases, arcs, spoilers)...")
    bible = build_show_bible(parsed_show, client)
    (out_dir / "show_bible.json").write_text(
        json.dumps(bible, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"      Characters mapped: {len(bible.get('characters', []))} | "
        f"Arcs: {len(bible.get('continuous_arcs', []))} | "
        f"Spoilers flagged: {len(bible.get('major_spoilers', []))}"
    )

    # Step 4a: Episode Moment Finder (Call 2)
    print(f"[4/7] Scanning {parsed_show['episode_count']} episodes for promo candidates (Call 2)...")
    episode_results = []
    raw_candidates = []
    for ep in parsed_show["episodes"]:
        er = find_episode_candidates(ep, bible, client)
        episode_results.append(er)
        ep_cands = er.get("candidates", [])
        raw_candidates.extend(ep_cands)
        print(
            f"      Ep {ep['episode_number']:02d}: "
            f"{len(ep_cands)} candidate(s), {len(er.get('notable_stitch_beats', []))} stitch beat(s)"
        )
    (out_dir / "episode_candidates_raw.json").write_text(
        json.dumps(episode_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Step 4b: Cross-Episode Stitcher (Call 3)
    print("[5/7] Synthesizing cross-episode stitched promos (Call 3)...")
    stitched = generate_cross_episode_promos(parsed_show, bible, episode_results, client)
    print(f"      Proposed {len(stitched)} cross-episode stitched promo(s)")
    raw_candidates.extend(stitched)

    # Step 5: Deterministic Validator + Audit Log
    print(f"[6/7] Validating {len(raw_candidates)} raw candidates (overlap math, ±4s snap, dedup)...")
    valid_candidates, audit_log = validate_candidates(raw_candidates, parsed_show)
    (out_dir / "validation_audit_log.json").write_text(
        json.dumps(audit_log, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"      Valid: {audit_log['total_valid']} | "
        f"Rejected: {audit_log['total_rejected']} | "
        f"Auto-snapped: {audit_log['total_auto_adjusted']}"
    )

    # Step 6: Strictly Blind Cold-Viewer Judge (Call 4)
    print(f"[7/7] Running strictly blind Cold-Viewer Judge on {len(valid_candidates)} valid promos (Call 4)...")
    judged = judge_candidates_blind(valid_candidates, client, batch_size=6)

    # Step 7: Rank & Export
    ranked = rank_promos(judged)
    run_metrics = dict(client.stats)
    run_metrics["estimated_cost_usd"] = client.estimate_cost_usd()
    run_metrics["elapsed_sec"] = round(time.time() - t0, 1)
    (out_dir / "run_metrics.json").write_text(
        json.dumps(run_metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    paths = export_all_reports(out_dir, parsed_show, bible, ranked, audit_log, run_metrics)
    print("\n=== DONE ===")
    print(f"Total ranked promos: {len(ranked)}")
    print(f"API calls: {run_metrics['api_calls']} (cache hits: {run_metrics['cache_hits']})")
    print(f"Tokens: {run_metrics['input_tokens']:,} in / {run_metrics['output_tokens']:,} out")
    print(f"Estimated cost: ${run_metrics['estimated_cost_usd']:.4f} | Time: {run_metrics['elapsed_sec']}s")
    for k, p in paths.items():
        print(f"  -> {k}: {p}")


if __name__ == "__main__":
    main()
