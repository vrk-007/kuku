# Kuku — AI Generalist Assignment: Problem 1 (Find the Promos)

## 1. Problem & Context
- **Situation:** Editors currently watch every episode of every weekly AI microdrama release by hand to pick social media promo clips.
- **Input Pack:** 50 episodes of *I Married a Broken Soldier. He was a Duke* (English-language US microdrama, 102.7 total minutes, 3,727 script blocks with exact timestamps for `CAMERA`, `ACTION`, `SOUND FX`, and `DIALOGUE` with emotional tone tags).
- **What is a Promo:** A **20–60 second** clip (continuous stretch OR stitched moments from one or multiple episodes) posted on Instagram Reels, TikTok, or YouTube Shorts to pull in cold viewers.

---

## 2. Goals & Objectives
1. **Find promos across all 50 episodes** (both single-episode and cross-episode stitched clips, including ReelShort/DramaBox-style flash-forward cold opens).
2. **Output complete editorial metadata for each promo:**
   - Episode number(s) and exact start/end timestamps of every segment, in playback order.
   - **Hook** (first 0–3 seconds that stops the scroll).
   - **Cliffhanger** (unresolved ending that creates curiosity).
   - **One-line "why it works"**.
3. **Rank the promos** with clear posting-order rationale (which promos to post first for cold audiences and why).
4. **Check quality honestly:** Audit where the tool picks badly, backed by real run data and a deterministic rejection audit log.

---

## 3. Constraints & Evaluation Criteria
- **Hard Promo Rules:**
  - Total duration strictly **20–60 seconds** (computed via `max(end) - min(start)` across parallel tracks, never summing overlapping blocks).
  - Opens on a strong **0–3s hook** (dialogue or high-impact visual/action beat, never a static/silent establishing shot).
  - Ends on an **unresolved cliffhanger**.
  - **Standalone clarity:** Makes sense to a stranger who has never seen the show.
  - Scope is **finding moments & exact cut points only** (no video editing, music, or text overlays).
- **Engineering & Submission Constraints:**
  - **Free-tier friendly & low cost at scale:** Target under \$0.15 per 50-episode show on Flash/mini models.
  - **Reusable & Model-Swappable:** Must run on any brand-new show folder during the interview with one CLI command, and easily swap models (Gemini / OpenAI / Anthropic) via `.env` or CLI flags.
  - **Confidentiality:** Do not publish or share Kuku's screenplay text publicly.
- **5 Evaluation Lenses:**
  1. Problem choice, scoping, and explicit assumptions.
  2. Approach & scale (clear Code vs. AI split, human-in-the-loop, cost per show).
  3. Quality checking (real runs + honest failure modes).
  4. Working prototype on real inputs.
  5. Clear, concise communication.

---

## 4. Architecture: Code vs. AI Split

| Responsibility | Handled By | Why |
|---|---|---|
| Parsing UTF-8 scripts, timestamps, speaker tags, ellipsis sentence grouping | **Deterministic Code** | 0 tokens, 100% accurate, prevents mid-sentence cuts and hallucinated timestamps. |
| Show Bible (character alias map, multi-episode arcs, spoiler list) | **LLM Call 1** (1 call/show) | Reads loglines + continuity notes + extracted unique speaker tags (~5k tokens). |
| Per-episode promo candidates (continuous + intra-episode stitched) | **LLM Call 2** (50 calls) | Reads full episode (including `CAMERA` & `ACTION` beats) + Show Bible; returns exact `block_id`s + `spoiler_risk`. |
| Cross-episode stitched promos (including flash-forward cold opens) | **LLM Call 3** (1 call/show) | Combines top beats across linked episodes into 20–60s mini-arcs. |
| Validation, overlap math, ±4s auto-snap, dedup, rejection audit log | **Deterministic Code** | Enforces 20–60s bounds, `start <= end` within segments, sentence integrity, and logs every failure. |
| Strictly Blind Cold-Viewer Judge | **LLM Call 4** (batched) | Sees **only** raw clip text (no Show Bible) to genuinely test `hook_3s`, `cliffhanger`, and `standalone_clarity`. |
| Final Ranking & Diversity Cap | **Deterministic Code** | Weighted rubric + spoiler penalty + diversity cap so near-identical scenes don't flood the Top 10. |

---

## 5. Planned Steps & Status

- [x] **Step 1 — Deterministic Parser (`promo_finder/parser.py`)**
  - Batch-reads full 50-episode folder (and supports single files / in-memory text via `parse_episode_text()`).
  - Case-insensitive headers, forgiving timestamp regex, missing-`SCENE` fallback, natural tie-breaking sort, consistent `B001` block IDs, true ellipsis sentence grouping (37 groups), and 64 unique speaker tags extracted.
  - Verified on all 50 episodes → `outputs/parsed_episodes.json`.
- [x] **Step 2 — Swappable LLM Client (`promo_finder/llm_client.py`)**
  - Unified wrapper for **Gemini / OpenAI / Anthropic** (stdlib REST fallback, zero mandatory SDK lock-in) with strict JSON mode, 429 exponential backoff, model-keyed disk caching (`outputs/.cache/`), and per-model token/cost tracking.
  - Supports **per-stage model overrides** via `.env` (`LLM_MODEL_BIBLE`, `LLM_MODEL_FINDER`, `LLM_MODEL_STITCHER`, `LLM_MODEL_JUDGE`) so Flash vs. Lite cost/quality trade-offs can be benchmarked cleanly.
  - Verified per-stage routing via automated test.
- [x] **Step 3 — Call 1: Show Bible (`promo_finder/show_bible.py`)**
  - Builds canonical character alias map (e.g., `LADY IN GOLD GOWN → Clarissa`, `BEARDED STRANGER → Alexander`), continuous episode arcs, and major twist/spoiler list.
- [x] **Step 4 — Call 2 & Call 3: Promo Finder & Cross-Episode Stitcher (`moment_finder.py`, `stitcher.py`)**
  - Extracts single-episode (continuous + intra-episode stitched) and cross-episode stitched candidates grounded in exact `block_id`s, preserving `CAMERA`/`ACTION` hooks and tagging `spoiler_risk`.
- [x] **Step 5 — Deterministic Validator (`promo_finder/validator.py`)**
  - Overlap duration math (`max(end) - min(start)`), ±4s auto-snap to sentence boundaries, intra-segment chronological check (allowing cross-segment flash-forwards), overlap deduplication, and `outputs/validation_audit_log.json`.
  - Dry-run verified against real Ep 1–3 parsed data (correctly accepted continuous + flash-forward stitched clips; rejected `DURATION_TOO_SHORT` and `HALLUCINATED_BLOCK_ID`).
- [x] **Step 6 — Call 4: Strictly Blind Cold-Viewer Judge (`promo_finder/judge.py`)**
  - Blind evaluation on raw clip text only → `hook_3s`, `cliffhanger`, `standalone_clarity`, 1-line `why_it_works`, and `needs_context`/`weak_ending` flags.
- [x] **Step 7 — Ranker, Exports, CLI & Repo Hygiene (`ranker.py`, `report.py`, `run.py`, `.gitignore`)**
  - Weighted ranking + Top-10 diversity cap + exports (`outputs/promos.json`, `outputs/promos.csv`, interactive `outputs/report.html`). Dry-run export verified.
  - `.gitignore` configured to protect `.env` API keys and Kuku's confidential screenplay folder.
- [ ] **Step 8 — Live Smoke Test (5 episodes) & Full 50-Episode Run (Gemini)**
  - Run `--max-episodes 5` first to validate live JSON outputs and rate limits, then run all 50 episodes.
- [ ] **Step 9 — Quality Audit, README & 2-page Solution Doc (PDF)**
  - Spot-check top & bottom picks + rejection audit log, document honest failure modes and Flash vs. Lite cost/quality trade-offs, and finalize submission package.

---

## 6. Submission Deliverables Checklist
- [ ] **Prototype link:** Clean GitHub repo (or hosted Streamlit app) with 1-command run instructions.
- [ ] **Raw outputs folder:** Unedited outputs from running on all 50 episodes (`promos.json`, `promos.csv`, `report.html`, `validation_audit_log.json`).
- [ ] **Solution doc:** Concise PDF (max 2 pages) covering problem framing, assumptions, architecture & scale/cost, and quality audit / failure modes.
- [ ] **Walkthrough video (optional, recommended):** ≤ 3-minute screen recording showing the tool running on the input pack.
