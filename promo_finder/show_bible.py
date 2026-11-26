"""Step 3 (Call 1): Show Bible Generator.

Makes ONE lightweight LLM call over all episode loglines, continuity notes,
and deterministically extracted per-episode unique speaker tags WITH sample
dialogue evidence for every speaker tag (preventing blind alias guessing).

Outputs:
  - premise (2 sentences)
  - characters + script-only alias map (evidence-backed mapping of descriptive/role tags)
  - continuous_arcs (multi-episode story groups for cross-episode stitching)
  - major_spoilers (late twists/reveals so Call 2/3 & Ranker can flag spoiler_risk)
"""

from __future__ import annotations

from typing import Any

from promo_finder.llm_client import LLMClient

SYSTEM_PROMPT = """You are a senior story editor for vertical microdrama apps (like ReelShort, DramaBox, Kuku TV).
You will receive:
1. Episode loglines, continuity notes, and per-episode speaker tags.
2. SPEAKER DIALOGUE EVIDENCE: every unique script speaker tag with the episodes it appears in and actual sample dialogue lines spoken under that tag.

Your job is to build a concise, accurate JSON Show Bible that downstream promo-finding stages will rely on.

STRICT ALIAS MAPPING RULES:
- Use the ACTUAL DIALOGUE EVIDENCE (who the speaker addresses, what they claim, which episodes they appear in) to resolve visual, descriptive, or role-based script tags to their canonical character names. NEVER guess purely from a tag's wording.
- Check carefully who is speaking vs. who is being spoken to (e.g., if a tag says "You should turn around, [Name]" or "People change their minds, sister", that tag is NOT [Name]!).
- Only include tags in `aliases` that exist verbatim in the provided speaker tag list and are genuinely confirmed by the dialogue. Leave unnamed one-off background/crowd tags unmapped rather than forcing them onto a main character.
- If a generic tag is clearly used for different characters in different episodes, only list it under the primary matching character (or note the episode split in `description`).

CONTINUOUS MINI-ARC RULES:
- Break the series into TIGHT 2–5 episode mini-arcs, where each arc captures ONE focused dramatic conflict or setup-to-payoff sequence (NEVER giant 10–25 episode acts). For a 40–50 episode series, produce roughly 8–15 tight mini-arcs.
- If only 1–3 episodes are provided, return a single arc covering those uploaded episodes.
- Set `is_cold_audience_friendly` to `true` ONLY if a first-time viewer who knows nothing about the show can immediately understand the emotional stakes of that mini-arc; otherwise `false`.
- Every number in `episodes` must be an actual episode number present in the uploaded series.

MAJOR SPOILER RULES:
- List ALL major plot twists across the uploaded episodes: hidden identity reveals, true mastermind/conspiracy unmaskings, major character deaths or arrests, and climax/finale resolutions (for a 40–50 episode series, typically 5–12 spoilers; for short 1–5 episode uploads, list only genuine twists within those episodes, or `[]` if none).
- `severity`:
  - "high" = spoils a core series mystery, hidden identity, major death, or finale resolution (must be avoided in cold-traffic ads).
  - "medium" = spoils a notable mid-series reversal or turning point (usable with care / retargeting).
- Every `episode` value MUST be an actual integer episode number present in the uploaded series.

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
      "episodes": [<int>, <int>],
      "core_conflict": "1-sentence description",
      "is_cold_audience_friendly": true
    }
  ],
  "major_spoilers": [
    {
      "episode": <int>,
      "twist": "short description of the twist, reveal, or resolution",
      "severity": "high | medium"
    }
  ]
}"""


def _clip_line(text: str, max_len: int = 110) -> str:
    cleaned = " ".join(text.split()).strip().strip('"“”')
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _format_ep_list(eps: list[int], max_show: int = 8) -> str:
    if len(eps) <= max_show:
        return ", ".join(str(e) for e in eps)
    head = ", ".join(str(e) for e in eps[: max_show - 2])
    return f"{head}, …, {eps[-2]}, {eps[-1]}"


def _pick_sample_lines(lines: list[tuple[int, str]], max_samples: int = 3) -> list[tuple[int, str]]:
    """Pick up to `max_samples` informative, chronologically spread dialogue lines for a speaker."""
    if len(lines) <= max_samples:
        return lines

    # Group by episode so we capture earliest + later episodes if a tag spans multiple arcs
    by_ep: dict[int, list[str]] = {}
    for ep_num, txt in lines:
        by_ep.setdefault(ep_num, []).append(txt)

    def _best_in_ep(ep_lines: list[str], exclude: set[str]) -> str | None:
        # Prefer first reasonably informative line (>= 20 chars), else longest remaining
        candidates = [t for t in ep_lines if t not in exclude]
        if not candidates:
            return None
        for t in candidates:
            if len(t) >= 20:
                return t
        return max(candidates, key=len)

    ep_nums = sorted(by_ep)
    picked: list[tuple[int, str]] = []
    seen_texts: set[str] = set()

    if len(ep_nums) == 1:
        ep = ep_nums[0]
        while len(picked) < max_samples:
            choice = _best_in_ep(by_ep[ep], seen_texts)
            if choice is None:
                break
            picked.append((ep, choice))
            seen_texts.add(choice)
        return picked

    # Multi-episode speaker: pick from first ep, then (if 3+ eps) a second line from first or middle ep, then last ep
    target_eps = [ep_nums[0]]
    if len(ep_nums) >= 3:
        target_eps.append(ep_nums[len(ep_nums) // 2])
    else:
        target_eps.append(ep_nums[0])
    target_eps.append(ep_nums[-1])

    for ep in target_eps:
        if len(picked) >= max_samples:
            break
        choice = _best_in_ep(by_ep[ep], seen_texts)
        if choice is not None:
            picked.append((ep, choice))
            seen_texts.add(choice)

    # Fill any remaining slot from any episode
    if len(picked) < max_samples:
        for ep in ep_nums:
            choice = _best_in_ep(by_ep[ep], seen_texts)
            if choice is not None:
                picked.append((ep, choice))
                seen_texts.add(choice)
                if len(picked) >= max_samples:
                    break

    return picked


def build_speaker_evidence(parsed_show: dict[str, Any], max_samples_per_speaker: int = 3) -> list[str]:
    """Collect episode spread + sample dialogue lines for every unique speaker tag."""
    lines_by_speaker: dict[str, list[tuple[int, str]]] = {}
    for ep in parsed_show.get("episodes", []):
        ep_num = ep["episode_number"]
        for b in ep.get("blocks", []):
            if b.get("block_type") == "DIALOGUE" and b.get("speaker") and b.get("text"):
                lines_by_speaker.setdefault(b["speaker"], []).append((ep_num, b["text"]))

    evidence_lines: list[str] = []
    for spk in parsed_show.get("all_unique_speakers", []):
        spk_lines = lines_by_speaker.get(spk, [])
        if not spk_lines:
            evidence_lines.append(f"- {spk} (0 lines)")
            continue
        eps = sorted({e for e, _ in spk_lines})
        samples = _pick_sample_lines(spk_lines, max_samples=max_samples_per_speaker)
        sample_str = " | ".join(f'Ep {e}: "{_clip_line(t)}"' for e, t in samples)
        evidence_lines.append(
            f"- {spk} ({len(spk_lines)} lines, Eps [{_format_ep_list(eps)}]): {sample_str}"
        )
    return evidence_lines


def build_show_bible(parsed_show: dict[str, Any], client: LLMClient) -> dict[str, Any]:
    """Run Call 1 to generate the Show Bible with dialogue-backed alias resolution."""
    lines = [
        f"SHOW TITLE: {parsed_show['show_title']}",
        f"TOTAL EPISODES: {parsed_show['episode_count']}",
        f"ALL UNIQUE SCRIPT SPEAKER TAGS: {', '.join(parsed_show['all_unique_speakers'])}",
        "",
        "SPEAKER DIALOGUE EVIDENCE (use these actual lines to map tags to canonical characters):",
    ]
    lines.extend(build_speaker_evidence(parsed_show))
    lines.append("")
    lines.append("EPISODE SUMMARIES & PER-EPISODE SPEAKER TAGS:")
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

    # Guardrail 1: keep only valid speaker tags that actually exist in the parsed scripts
    valid_tags = set(parsed_show.get("all_unique_speakers", []))
    if valid_tags and isinstance(bible.get("characters"), list):
        for ch in bible["characters"]:
            if isinstance(ch, dict) and isinstance(ch.get("aliases"), list):
                cleaned: list[str] = []
                for tag in ch["aliases"]:
                    norm = str(tag).strip().upper()
                    if norm in valid_tags and norm not in cleaned:
                        cleaned.append(norm)
                ch["aliases"] = cleaned

    # Guardrail 2: keep only valid uploaded episode numbers in continuous_arcs
    valid_eps = {ep["episode_number"] for ep in parsed_show.get("episodes", [])}
    if valid_eps and isinstance(bible.get("continuous_arcs"), list):
        cleaned_arcs: list[dict[str, Any]] = []
        for arc in bible["continuous_arcs"]:
            if not isinstance(arc, dict):
                continue
            raw_eps = arc.get("episodes", [])
            if not isinstance(raw_eps, list):
                continue
            kept_eps = sorted({int(e) for e in raw_eps if isinstance(e, (int, float, str)) and str(e).isdigit() and int(e) in valid_eps})
            if not kept_eps:
                continue
            arc["episodes"] = kept_eps
            arc["is_cold_audience_friendly"] = bool(arc.get("is_cold_audience_friendly", False))
            cleaned_arcs.append(arc)
        bible["continuous_arcs"] = cleaned_arcs

    # Guardrail 3: keep only valid uploaded episode numbers & normalized severities in major_spoilers
    if valid_eps and isinstance(bible.get("major_spoilers"), list):
        cleaned_spoilers: list[dict[str, Any]] = []
        for sp in bible["major_spoilers"]:
            if not isinstance(sp, dict):
                continue
            raw_ep = sp.get("episode")
            if not (isinstance(raw_ep, (int, float, str)) and str(raw_ep).strip().isdigit()):
                continue
            ep_num = int(str(raw_ep).strip())
            if ep_num not in valid_eps:
                continue
            twist = str(sp.get("twist", "")).strip()
            if not twist:
                continue
            sev = str(sp.get("severity", "medium")).strip().lower()
            sev = "high" if sev == "high" else "medium"
            cleaned_spoilers.append({"episode": ep_num, "twist": twist, "severity": sev})
        cleaned_spoilers.sort(key=lambda s: s["episode"])
        bible["major_spoilers"] = cleaned_spoilers

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
