"""Step 7b: Export promos.json, promos.csv, and interactive report.html."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any


def _format_segments_str(segments: list[dict[str, Any]]) -> str:
    return " -> ".join(
        f"Ep {s['episode']} ({s['start_ts']}-{s['end_ts']}, {s['duration_sec']:.1f}s)"
        for s in segments
    )


def export_all_reports(
    out_dir: str | Path,
    parsed_show: dict[str, Any],
    bible: dict[str, Any],
    ranked_promos: list[dict[str, Any]],
    audit_log: dict[str, Any],
    run_metrics: dict[str, Any],
) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. promos.json (clean output without verbose blind_clip_script duplicated, or keep it in separate field)
    promos_json_path = out / "promos.json"
    promos_json_path.write_text(
        json.dumps(
            {
                "show_title": parsed_show["show_title"],
                "episode_count": parsed_show["episode_count"],
                "total_promos_ranked": len(ranked_promos),
                "run_metrics": run_metrics,
                "promos": ranked_promos,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 2. promos.csv (editor-ready spreadsheet)
    promos_csv_path = out / "promos.csv"
    with promos_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Rank",
                "Posting Tier",
                "Final Score",
                "Type",
                "Arc",
                "Duration (s)",
                "Segments (In Playback Order)",
                "Hook (0-3s)",
                "Cliffhanger",
                "Why It Works",
                "Hook Score",
                "Cliffhanger Score",
                "Standalone Clarity",
                "Spoiler Risk",
                "Quality Flags",
                "Honest Critique",
            ]
        )
        for p in ranked_promos:
            sc = p["scores"]
            writer.writerow(
                [
                    p["rank"],
                    p["posting_tier"],
                    p["final_score"],
                    p["candidate_type"],
                    p["arc_label"],
                    p["total_duration_sec"],
                    _format_segments_str(p["segments"]),
                    p["hook"],
                    p["cliffhanger"],
                    p["why_it_works"],
                    sc["hook_3s"],
                    sc["cliffhanger"],
                    sc["standalone_clarity"],
                    p["spoiler_risk"],
                    ", ".join(p["quality_flags"]) if p["quality_flags"] else "clean",
                    p["honest_critique"],
                ]
            )

    # 3. Interactive report.html
    report_html_path = out / "report.html"
    report_html_path.write_text(
        _build_html(parsed_show, bible, ranked_promos, audit_log, run_metrics),
        encoding="utf-8",
    )

    return {
        "promos_json": promos_json_path,
        "promos_csv": promos_csv_path,
        "report_html": report_html_path,
    }


def _build_html(
    parsed_show: dict[str, Any],
    bible: dict[str, Any],
    promos: list[dict[str, Any]],
    audit_log: dict[str, Any],
    metrics: dict[str, Any],
) -> str:
    esc = html.escape
    cards_html = []
    for p in promos:
        sc = p["scores"]
        seg_pills = "".join(
            f'<span class="seg-pill">Ep {s["episode"]} &middot; {esc(s["start_ts"])}–{esc(s["end_ts"])} ({s["duration_sec"]:.1f}s) <em>[{esc(s["role"])}]</em></span>'
            for s in p["segments"]
        )
        flags_html = (
            "".join(f'<span class="flag">{esc(fl)}</span>' for fl in p["quality_flags"])
            if p["quality_flags"]
            else '<span class="clean-badge">clean</span>'
        )
        tier_cls = "tier1" if "Tier 1" in p["posting_tier"] else ("tier2" if "Tier 2" in p["posting_tier"] else "tier3")
        cards_html.append(
            f"""
        <div class="card" data-tier="{tier_cls}" data-type="{esc(p['candidate_type'])}" data-spoiler="{esc(p['spoiler_risk'])}">
          <div class="card-head">
            <div class="rank-badge">#{p['rank']}</div>
            <div class="title-col">
              <div class="meta-row">
                <span class="pill {tier_cls}">{esc(p['posting_tier'])}</span>
                <span class="pill type">{esc(p['candidate_type'])}</span>
                <span class="pill arc">{esc(p['arc_label'])}</span>
                <span class="pill dur">{p['total_duration_sec']:.1f}s</span>
                <span class="pill spoiler-{esc(p['spoiler_risk'])}">spoiler: {esc(p['spoiler_risk'])}</span>
              </div>
              <div class="segs">{seg_pills}</div>
            </div>
            <div class="score-box">
              <div class="score-num">{p['final_score']:.2f}</div>
              <div class="score-sub">H:{sc['hook_3s']} C:{sc['cliffhanger']} S:{sc['standalone_clarity']}</div>
            </div>
          </div>
          <div class="card-body">
            <p><strong>Hook (0–3s):</strong> {esc(p['hook'])}</p>
            <p><strong>Cliffhanger:</strong> {esc(p['cliffhanger'])}</p>
            <p><strong>Why it works:</strong> {esc(p['why_it_works'])}</p>
            <p class="critique"><strong>Blind-Judge Critique:</strong> {esc(p['honest_critique'])} {flags_html}</p>
            <details>
              <summary>View exact clip script ({len(p['segments'])} segment(s))</summary>
              <pre>{esc(p.get('blind_clip_script', ''))}</pre>
            </details>
          </div>
        </div>"""
        )

    rej_rows = "".join(
        f"<tr><td>{esc(r)}</td><td>{cnt}</td></tr>"
        for r, cnt in audit_log.get("rejection_counts_by_rule", {}).items()
    ) or "<tr><td colspan='2'>No rejections</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Promo Finder Report — {esc(parsed_show['show_title'])}</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:24px; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  h1 {{ margin:0 0 6px; font-size:24px; }}
  .sub {{ color:#8b949e; margin-bottom:20px; font-size:14px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }}
  .stat {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px 14px; }}
  .stat .k {{ color:#8b949e; font-size:12px; text-transform:uppercase; }}
  .stat .v {{ font-size:20px; font-weight:700; margin-top:4px; }}
  .filters {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; }}
  .btn {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; border-radius:20px; padding:6px 14px; cursor:pointer; font-size:13px; }}
  .btn.active, .btn:hover {{ background:#238636; border-color:#2ea043; color:#fff; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:16px; margin-bottom:14px; }}
  .card-head {{ display:flex; gap:14px; align-items:flex-start; }}
  .rank-badge {{ background:#1f6feb; color:#fff; font-weight:700; font-size:16px; padding:6px 10px; border-radius:8px; min-width:36px; text-align:center; }}
  .title-col {{ flex:1; }}
  .meta-row {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }}
  .pill {{ font-size:11px; padding:2px 8px; border-radius:12px; font-weight:600; background:#21262d; border:1px solid #30363d; }}
  .pill.tier1 {{ background:#1b4721; border-color:#2ea043; color:#7ee787; }}
  .pill.tier2 {{ background:#1c2d4a; border-color:#388bfd; color:#79c0ff; }}
  .pill.tier3 {{ background:#341a1a; border-color:#f85149; color:#ffa198; }}
  .pill.spoiler-high {{ background:#490202; color:#ff7b72; border-color:#f85149; }}
  .pill.spoiler-medium {{ background:#3d2b00; color:#e3b341; border-color:#d29922; }}
  .segs {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .seg-pill {{ background:#0d1117; border:1px solid #30363d; padding:3px 8px; border-radius:6px; font-size:12px; color:#a5d6ff; }}
  .score-box {{ text-align:right; min-width:95px; }}
  .score-num {{ font-size:24px; font-weight:800; color:#58a6ff; }}
  .score-sub {{ font-size:11px; color:#8b949e; }}
  .card-body p {{ margin:8px 0; font-size:14px; }}
  .critique {{ color:#c9d1d9; }}
  .flag {{ background:#490202; color:#ff7b72; font-size:11px; padding:2px 6px; border-radius:6px; margin-left:4px; }}
  .clean-badge {{ background:#1b4721; color:#7ee787; font-size:11px; padding:2px 6px; border-radius:6px; margin-left:4px; }}
  details {{ margin-top:10px; }}
  summary {{ cursor:pointer; color:#58a6ff; font-size:13px; }}
  pre {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; overflow-x:auto; font-size:12px; white-space:pre-wrap; color:#c9d1d9; }}
  table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
  th, td {{ border:1px solid #30363d; padding:8px 10px; text-align:left; }}
  th {{ background:#21262d; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Promo Finder Report: {esc(parsed_show['show_title'])}</h1>
  <div class="sub">{esc(bible.get('premise', ''))}</div>

  <div class="stats">
    <div class="stat"><div class="k">Episodes Scanned</div><div class="v">{parsed_show['episode_count']}</div></div>
    <div class="stat"><div class="k">Ranked Promos</div><div class="v">{len(promos)}</div></div>
    <div class="stat"><div class="k">Rejected by Rules</div><div class="v">{audit_log.get('total_rejected', 0)}</div></div>
    <div class="stat"><div class="k">Auto-Snapped</div><div class="v">{audit_log.get('total_auto_adjusted', 0)}</div></div>
    <div class="stat"><div class="k">Model</div><div class="v" style="font-size:14px">{esc(metrics.get('model', ''))}</div></div>
    <div class="stat"><div class="k">Est. API Cost</div><div class="v">${metrics.get('estimated_cost_usd', 0):.4f}</div></div>
  </div>

  <div class="filters">
    <button class="btn active" onclick="filterCards('all', this)">All ({len(promos)})</button>
    <button class="btn" onclick="filterCards('tier1', this)">Tier 1: Post First</button>
    <button class="btn" onclick="filterCards('stitched', this)">Stitched Only</button>
    <button class="btn" onclick="filterCards('continuous', this)">Continuous Only</button>
  </div>

  <div id="cards">{"".join(cards_html)}</div>

  <div class="card">
    <h2 style="margin-top:0;font-size:18px;">Validation Rejection Audit Summary</h2>
    <table><thead><tr><th>Rule Violated</th><th>Count</th></tr></thead><tbody>{rej_rows}</tbody></table>
  </div>
</div>
<script>
function filterCards(mode, btn) {{
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card[data-tier]').forEach(el => {{
    let show = true;
    if (mode === 'tier1') show = el.dataset.tier === 'tier1';
    else if (mode === 'stitched') show = el.dataset.type.includes('stitched');
    else if (mode === 'continuous') show = el.dataset.type === 'continuous';
    el.style.display = show ? 'block' : 'none';
  }});
}}
</script>
</body>
</html>"""
