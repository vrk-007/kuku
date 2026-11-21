# 🎬 MicroDrama Promo Finder

A lightweight **hybrid Python + AI** tool that scans multi-episode microdrama screenplays (`.txt`) and extracts a ranked shortlist of ready-to-edit **20–60 second promo clips** for Reels, TikTok, and YouTube Shorts.

Each promo clip is selected for a strong **0–3s hook**, **standalone clarity** for cold viewers, and an **unresolved cliffhanger** — supporting both single continuous scenes and cross-episode / flash-forward stitches.

---

## How It Works

**Core idea:** AI handles storytelling and taste, while pure Python enforces hard production rules (exact timestamps, 20–60s runtime, clean sentence cuts, and zero duplicates).

```text
Upload (.txt) → Parser (Python) → Show Summary (AI, 1 call) → Moment Finder (AI, 1/ep)
→ Stitcher (AI, 1 call) → Validator (Python) → Blind Judge (AI, 6/batch) → Ranker (Python)
```

| Step | Engine | Role |
|---|---|---|
| **1. Parser** | Python (`$0`) | Numbers timestamped lines, extracts speakers, and keeps split sentences (`…`) intact. |
| **2. Show Summary** | AI (1 call) | Maps characters, major arcs, and spoilers once across the show. |
| **3. Moment Finder** | AI (1 call/ep) | Pitches continuous clips using line IDs (never invents timestamps). |
| **4. Stitcher** | AI (1 call) | Proposes cross-episode mashups and flash-forward cold opens. |
| **5. Validator** | Python (`$0`) | Verifies timestamps, enforces **20–60s** length (±4s auto-snap), removes duplicates, and logs every filtered clip in the **Validation Audit** tab. |
| **6. Blind Judge** | AI (6 clips/call) | Reads *only* the clip script (zero plot context) and scores Hook, Cliffhanger, and Clarity like a first-time viewer. |
| **7. Ranker** | Python (`$0`) | Combines scores with an early-episode bonus, spoiler penalty, and diversity cap into ready-to-use tiers. |

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure an API key
Copy `.env.example` to `.env` and add your API key (or paste it directly in the Streamlit sidebar):
```bash
cp .env.example .env
```

### 3. Run the Web App (Streamlit)
```bash
python -m streamlit run app.py
```
Upload your episode `.txt` files, pick a provider/model in the sidebar, and click **🚀 Find Promos**.

### 4. Run via CLI (Batch Mode)
```bash
python run.py --input-dir "path/to/episodes_folder" --delay 4
```
Optional flags: `--max-episodes N`, `--provider gemini|openai|anthropic|runcrate|custom`, `--model MODEL_ID`.

Outputs are saved to `outputs/` (`promos.json`, `promos.csv`, `report.html`, `validation_audit_log.json`, `run_metrics.json`).

---

## Supported AI Providers

- **Gemini** (e.g., `gemini-3.5-flash-lite`, `gemini-2.5-flash`)
- **OpenAI** (e.g., `gpt-4o-mini`)
- **Anthropic** (e.g., `claude-3-5-haiku-latest`)
- **Runcrate**
- **Custom (any OpenAI-compatible endpoint)** — works with Groq (`https://api.groq.com/openai/v1`), OpenRouter, DeepSeek, Together, or local Ollama (`http://localhost:11434/v1`).

All AI responses are cached on disk (`outputs/.cache/`), so re-runs on the same files cost `$0`.

---

## Project Structure

```text
├── app.py                  # Streamlit web app (editor-facing UI)
├── run.py                  # CLI entry point for batch runs
├── requirements.txt        # Python dependencies
├── .env.example            # Example environment variables
├── promo_finder/
│   ├── parser.py           # Timestamped screenplay parser
│   ├── llm_client.py       # Swappable LLM client, retries & disk cache
│   ├── show_bible.py       # Step 2: Show Summary (characters, arcs, spoilers)
│   ├── moment_finder.py    # Step 3: Per-episode clip extraction
│   ├── stitcher.py         # Step 4: Cross-episode & flash-forward stitches
│   ├── validator.py        # Step 5: Deterministic rules, auto-snap & audit log
│   ├── judge.py            # Step 6: Blind cold-viewer scoring
│   ├── ranker.py           # Step 7: Weighted ranking, diversity cap & tiers
│   ├── report.py           # JSON / CSV / HTML report exporters
│   └── pipeline.py         # End-to-end orchestration used by app.py
└── outputs/                # Sample run outputs (cache & raw scripts gitignored)
```
