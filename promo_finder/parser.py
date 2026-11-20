"""Step 1: Deterministic Screenplay Parser.

Reads all episode .txt files from a folder (or a single file), normalizes UTF-8
and case-insensitive headers, extracts structured blocks (CAMERA, ACTION,
SOUND FX, DIALOGUE with tone/modifiers), sorts chronologically, extracts unique
speaker tags per episode (for Show Bible alias mapping), and groups
ellipsis-continued dialogue lines so promo cuts never break a sentence.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

NON_DIALOGUE_TYPES = {"CAMERA", "ACTION", "SOUND FX", "SFX", "MUSIC"}
OFFSCREEN_MARKERS = {"o.s.", "off-screen", "offscreen", "v.o.", "voice-over"}
TS_PATTERN = r"\d{1,2}:\d{2}(?:\.\d+)?"


def ts_to_sec(ts: str) -> float:
    """Convert MM:SS.ss timestamp string to float seconds."""
    parts = ts.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid timestamp format: {ts!r}")
    minutes = int(parts[0])
    seconds = float(parts[1])
    return round(minutes * 60.0 + seconds, 2)


def sec_to_ts(sec: float) -> str:
    """Convert float seconds back to MM:SS.ss string."""
    sec = max(0.0, sec)
    minutes = int(sec // 60)
    rem = sec - minutes * 60
    return f"{minutes:02d}:{rem:05.2f}"


def compute_segment_span(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute true segment time span using min/max overlap math (never sum overlapping tracks)."""
    if not blocks:
        return {
            "start_sec": 0.0,
            "end_sec": 0.0,
            "start_ts": "00:00.00",
            "end_ts": "00:00.00",
            "duration_sec": 0.0,
        }
    start_sec = min(b["start_sec"] for b in blocks)
    end_sec = max(b["end_sec"] for b in blocks)
    return {
        "start_sec": round(start_sec, 2),
        "end_sec": round(end_sec, 2),
        "start_ts": sec_to_ts(start_sec),
        "end_ts": sec_to_ts(end_sec),
        "duration_sec": round(end_sec - start_sec, 2),
    }


def _parse_block_header(raw_header: str) -> tuple[str, str | None, list[str], str | None]:
    """Parse bold header like 'LADY JULIANA (Off-screen) (Sharp, Piercing):'."""
    cleaned = raw_header.strip().rstrip(":").strip()

    # Extract all parenthetical groups first so **CAMERA (Close-up):** is caught too
    parens = re.findall(r"\(([^)]*)\)", cleaned)
    base_speaker = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()
    base_upper = base_speaker.upper()

    if base_upper in NON_DIALOGUE_TYPES:
        normalized = "SOUND FX" if base_upper == "SFX" else base_upper
        return normalized, None, [], "; ".join(p.strip() for p in parens if p.strip()) or None

    modifiers: list[str] = []
    tones: list[str] = []
    for p in parens:
        p_clean = p.strip()
        if not p_clean:
            continue
        if p_clean.lower() in OFFSCREEN_MARKERS:
            modifiers.append(p_clean)
        else:
            tones.append(p_clean)

    tone_str = "; ".join(tones) if tones else None
    return "DIALOGUE", base_upper, modifiers, tone_str


def _assign_ellipsis_sentence_groups(blocks: list[dict[str, Any]]) -> int:
    """Link consecutive dialogue blocks from the same speaker truly split across ellipses.

    Links only when the continuation starts with '...' or a lowercase letter
    after a trailing '...', avoiding false joins like "No..." -> "You knew this!".
    """
    group_counter = 0
    last_dialogue_by_speaker: dict[str, dict[str, Any]] = {}

    for block in blocks:
        block["sentence_group_id"] = None
        if block["block_type"] != "DIALOGUE":
            continue

        speaker = block["speaker"] or ""
        raw_text = block["text"].strip()
        unquoted = raw_text.strip('"“”\'').strip()

        starts_ellipsis = unquoted.startswith("...") or unquoted.startswith("…")
        next_starts_lower = bool(unquoted) and unquoted[0].islower()

        prev = last_dialogue_by_speaker.get(speaker)
        prev_continued = False
        if prev is not None:
            prev_unquoted = prev["text"].strip().strip('"“”\'').strip()
            prev_ends_ellipsis = prev_unquoted.endswith("...") or prev_unquoted.endswith("…")
            gap = block["start_sec"] - prev["end_sec"]
            if (starts_ellipsis or (prev_ends_ellipsis and next_starts_lower)) and -2.0 <= gap <= 8.0:
                prev_continued = True

        if prev_continued and prev is not None:
            if prev["sentence_group_id"] is None:
                group_counter += 1
                prev["sentence_group_id"] = group_counter
            block["sentence_group_id"] = prev["sentence_group_id"]

        last_dialogue_by_speaker[speaker] = block

    return group_counter


def parse_episode_text(text: str, filename: str = "") -> dict[str, Any]:
    """Parse raw screenplay text into structured JSON (works in-memory for CLI & web uploads)."""
    text = text.replace("\r\n", "\n")

    # Normalize mojibake en-dashes if any crept in
    text = text.replace("â€“", "–").replace("â€”", "—")

    # Extract header metadata (case-insensitive)
    ep_match = re.search(r"(?i)^EPISODE NUMBER:\s*(.+)$", text, re.MULTILINE)
    logline_match = re.search(r"(?i)^EPISODE LOGLINE:\s*(.+)$", text, re.MULTILINE)
    continuity_match = re.search(r"(?i)^CONTINUITY NOTES:\s*(.+)$", text, re.MULTILINE)

    ep_num = None
    if ep_match:
        num_m = re.search(r"\d+", ep_match.group(1))
        if num_m:
            ep_num = int(num_m.group(0))
    if ep_num is None and filename:
        fn_m = re.search(r"episode[-_]?(\d+)", filename, re.IGNORECASE)
        ep_num = int(fn_m.group(1)) if fn_m else 0
    if ep_num is None:
        ep_num = 0

    logline = logline_match.group(1).strip() if logline_match else ""
    continuity_notes = continuity_match.group(1).strip() if continuity_match else ""

    # Isolate script body
    start_marker = re.search(r"(?i)---\s*SCRIPT START\s*---", text)
    end_marker = re.search(r"(?i)---\s*SCRIPT END\s*---", text)
    body = text[
        start_marker.end() if start_marker else 0 : end_marker.start() if end_marker else len(text)
    ]

    # Split into scenes (with fallback to Scene 1 if no SCENE headers exist)
    scene_pattern = re.compile(r"(?im)^SCENE\s+(\d+)\s*$")
    scene_matches = list(scene_pattern.finditer(body))

    scenes: list[dict[str, Any]] = []
    raw_blocks: list[dict[str, Any]] = []

    block_re = re.compile(
        r"\*\*(?P<header>[^*\n]+)\*\*:?\s*\n"
        rf"Start:\s*\((?P<start>{TS_PATTERN})\)\s*\|\s*End:\s*\((?P<end>{TS_PATTERN})\)\s*\n"
        r"(?P<content>.*?)(?=\n\*\*[^*\n]+\*\*:?\s*\n|\Z)",
        re.DOTALL,
    )

    scene_chunks: list[tuple[int, str]] = []
    if scene_matches:
        for idx, sm in enumerate(scene_matches):
            scene_num = int(sm.group(1))
            chunk_start = sm.end()
            chunk_end = scene_matches[idx + 1].start() if idx + 1 < len(scene_matches) else len(body)
            scene_chunks.append((scene_num, body[chunk_start:chunk_end]))
    else:
        scene_chunks.append((1, body))

    for scene_num, chunk in scene_chunks:
        heading_m = re.search(r"(?im)^SCENE HEADING:\s*(.+)$", chunk)
        s_start_m = re.search(rf"(?im)^SCENE START:\s*({TS_PATTERN})", chunk)
        s_end_m = re.search(rf"(?im)^SCENE END:\s*({TS_PATTERN})", chunk)

        heading = heading_m.group(1).strip() if heading_m else ""
        s_start_ts = s_start_m.group(1) if s_start_m else "00:00.00"
        s_end_ts = s_end_m.group(1) if s_end_m else "00:00.00"

        scenes.append(
            {
                "scene_number": scene_num,
                "heading": heading,
                "start_ts": sec_to_ts(ts_to_sec(s_start_ts)),
                "end_ts": sec_to_ts(ts_to_sec(s_end_ts)),
                "start_sec": ts_to_sec(s_start_ts),
                "end_sec": ts_to_sec(s_end_ts),
            }
        )

        for bm in block_re.finditer(chunk):
            block_type, speaker, modifiers, tone = _parse_block_header(bm.group("header"))
            start_sec = ts_to_sec(bm.group("start"))
            end_sec = ts_to_sec(bm.group("end"))
            content = " ".join(line.strip() for line in bm.group("content").strip().splitlines() if line.strip())

            raw_blocks.append(
                {
                    "scene_number": scene_num,
                    "block_type": block_type,
                    "speaker": speaker,
                    "modifiers": modifiers,
                    "tone": tone,
                    "start_ts": sec_to_ts(start_sec),
                    "end_ts": sec_to_ts(end_sec),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "text": content,
                    "_file_order": len(raw_blocks),
                }
            )

    # Sort chronologically by start_sec, preserving natural script order for ties
    raw_blocks.sort(key=lambda b: (b["start_sec"], b["_file_order"]))

    blocks: list[dict[str, Any]] = []
    for i, b in enumerate(raw_blocks, start=1):
        b.pop("_file_order", None)
        b["block_id"] = f"B{i:03d}"
        b["block_index"] = i
        blocks.append(b)

    ellipsis_groups = _assign_ellipsis_sentence_groups(blocks)

    unique_speakers = sorted(
        {b["speaker"] for b in blocks if b["block_type"] == "DIALOGUE" and b["speaker"]}
    )

    max_block_end = max((b["end_sec"] for b in blocks), default=0.0)
    max_scene_end = max((s["end_sec"] for s in scenes), default=0.0)
    duration_sec = round(max(max_block_end, max_scene_end), 2)

    return {
        "episode_number": ep_num,
        "filename": filename,
        "logline": logline,
        "continuity_notes": continuity_notes,
        "duration_sec": duration_sec,
        "duration_ts": sec_to_ts(duration_sec),
        "scene_count": len(scenes),
        "block_count": len(blocks),
        "ellipsis_group_count": ellipsis_groups,
        "unique_speakers": unique_speakers,
        "scenes": scenes,
        "blocks": blocks,
    }


def parse_episode_file(filepath: str | Path) -> dict[str, Any]:
    """Parse a single screenplay .txt file into structured JSON."""
    path = Path(filepath)
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_episode_text(text, filename=path.name)


def parse_directory(input_dir: str | Path) -> dict[str, Any]:
    """Parse all screenplay .txt files in a directory (or a single file path)."""
    p = Path(input_dir)
    if p.is_file():
        files = [str(p)]
        show_title = p.parent.name
    else:
        files = sorted(glob.glob(str(p / "*.txt")))
        show_title = p.name

    episodes = [parse_episode_file(f) for f in files]
    episodes.sort(key=lambda e: e["episode_number"])

    all_speakers = sorted({spk for ep in episodes for spk in ep["unique_speakers"]})
    total_duration_sec = round(sum(ep["duration_sec"] for ep in episodes), 2)

    return {
        "show_title": show_title,
        "episode_count": len(episodes),
        "total_duration_sec": total_duration_sec,
        "total_blocks": sum(ep["block_count"] for ep in episodes),
        "total_ellipsis_groups": sum(ep["ellipsis_group_count"] for ep in episodes),
        "all_unique_speakers": all_speakers,
        "episodes": episodes,
    }


def format_block_line(b: dict[str, Any]) -> str:
    """Format a single block compactly for LLM prompts (preserves CAMERA, ACTION, SOUND FX, DIALOGUE)."""
    bid = b["block_id"]
    ts = f"{b['start_ts']}-{b['end_ts']}"
    grp = f" [SENT_GRP:{b['sentence_group_id']}]" if b.get("sentence_group_id") else ""

    if b["block_type"] == "DIALOGUE":
        mod = f" ({', '.join(b['modifiers'])})" if b.get("modifiers") else ""
        tone = f" [{b['tone']}]" if b.get("tone") else ""
        return f"[{bid} {ts}] DIALOGUE | {b['speaker']}{mod}{tone}: {b['text']}{grp}"
    return f"[{bid} {ts}] {b['block_type']}: {b['text']}"


def format_episode_for_llm(episode: dict[str, Any]) -> str:
    """Render a full episode with scene headings and numbered blocks for Call 2."""
    lines = [
        f"EPISODE {episode['episode_number']} ({episode['duration_ts']})",
        f"LOGLINE: {episode['logline']}",
        f"CONTINUITY: {episode['continuity_notes']}",
        "",
    ]
    scene_headings = {s["scene_number"]: s["heading"] for s in episode["scenes"]}
    current_scene = None
    for b in episode["blocks"]:
        if b["scene_number"] != current_scene:
            current_scene = b["scene_number"]
            lines.append(f"--- SCENE {current_scene:02d}: {scene_headings.get(current_scene, '')} ---")
        lines.append(format_block_line(b))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse microdrama screenplay folder into structured JSON.")
    ap.add_argument("--input-dir", required=True, help="Path to folder containing episode .txt files (or single file)")
    ap.add_argument("--out", default="outputs/parsed_episodes.json", help="Output JSON path")
    args = ap.parse_args()

    parsed = parse_directory(args.input_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Show: {parsed['show_title']}")
    print(f"Episodes parsed: {parsed['episode_count']}")
    print(f"Total blocks: {parsed['total_blocks']}")
    print(f"Total duration: {parsed['total_duration_sec']:.1f}s ({parsed['total_duration_sec']/60:.1f} min)")
    print(f"Ellipsis sentence groups linked: {parsed['total_ellipsis_groups']}")
    print(f"Unique speaker tags extracted ({len(parsed['all_unique_speakers'])}): {', '.join(parsed['all_unique_speakers'][:15])}...")
    print(f"Saved -> {out_path}")


if __name__ == "__main__":
    main()
