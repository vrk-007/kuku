"""Streamlit web app: upload screenplay episodes -> ranked 20-60s promo clips.

Run:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import streamlit as st

from promo_finder.llm_client import DEFAULT_MODELS, LLMClient, load_dotenv_simple
from promo_finder.pipeline import parse_uploaded_files, run_pipeline

st.set_page_config(page_title="Promo Finder", page_icon="🎬", layout="wide")
load_dotenv_simple()

WEB_OUT_ROOT = Path("outputs_web")


def _secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


st.title("🎬 Microdrama Promo Finder")
st.caption("Upload screenplay episodes → get ranked 20–60s promo clips (hook + cliffhanger + standalone clarity).")

# ---------------- Sidebar ----------------
with st.sidebar:
    st.header("⚙️ Settings")
    provider_labels = {
        "gemini": "gemini",
        "openai": "openai",
        "anthropic": "anthropic",
        "runcrate": "runcrate",
        "custom": "custom (any OpenAI-compatible)",
    }
    provider = st.selectbox(
        "Provider",
        list(provider_labels.keys()),
        format_func=lambda k: provider_labels[k],
        index=0,
    )
    base_url = ""
    if provider == "custom":
        base_url = st.text_input(
            "Base URL",
            placeholder="https://api.groq.com/openai/v1",
            help="Works with Groq, OpenRouter, Together, DeepSeek, Mistral, Ollama, or any OpenAI-compatible endpoint.",
        )
    default_model = os.environ.get("LLM_MODEL", "") if provider == os.environ.get("LLM_PROVIDER", "gemini") else ""
    model = st.text_input(
        "Model",
        value=default_model or DEFAULT_MODELS[provider],
        placeholder="e.g. llama-3.3-70b-versatile" if provider == "custom" else "",
    )
    env_key_name = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "runcrate": "RUNCRATE_API_KEY",
        "custom": "CUSTOM_API_KEY",
    }[provider]
    preset_key = _secret(env_key_name) or os.environ.get(env_key_name, "")
    typed_key = st.text_input(
        "API key",
        type="password",
        placeholder="Already default key is in use" if preset_key else "Paste your API key",
        help="Leave blank to use the default key, or paste your own key to override it for this session (never stored on disk).",
    )
    api_key = typed_key.strip() or preset_key
    with st.expander("Advanced settings", expanded=False):
        delay = st.slider(
            "Seconds between API calls",
            0.0,
            10.0,
            1.0,
            0.5,
            help="Raise this if you hit API rate limits (HTTP 429).",
        )
        use_cache = st.checkbox("Reuse cached AI answers", value=True)
        st.caption("If you upload the exact same files again, it reuses saved results instead of calling the AI again.")

# ---------------- Upload ----------------
show_title = st.text_input("Show title", value="Uploaded Show")
uploads = st.file_uploader(
    "Upload episode screenplay files (.txt)", type=["txt"], accept_multiple_files=True
)

if uploads:
    st.write(f"**{len(uploads)}** file(s) selected.")

run_clicked = st.button("🚀 Find Promos", type="primary", disabled=not uploads)

if run_clicked:
    if not api_key:
        st.error(f"Please paste a {provider} API key in the sidebar.")
        st.stop()
    if provider == "custom" and (not base_url.strip() or not model.strip()):
        st.error("Please enter both a Base URL and a Model ID for the custom provider.")
        st.stop()
    try:
        files = [(u.name, u.getvalue()) for u in uploads]
        parsed = parse_uploaded_files(files, show_title=show_title.strip() or "Uploaded Show")
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not parse uploads: {e}")
        st.stop()

    st.success(
        f"Parsed {parsed['episode_count']} episodes · {parsed['total_blocks']} blocks · "
        f"{len(parsed['all_unique_speakers'])} speaker tags · {parsed['total_duration_sec']/60:.1f} min"
    )

    digest = hashlib.sha256(
        json.dumps([(n, hashlib.sha256(b).hexdigest()) for n, b in files]).encode()
    ).hexdigest()[:10]
    out_dir = WEB_OUT_ROOT / digest
    out_dir.mkdir(parents=True, exist_ok=True)

    client = LLMClient(
        provider=provider,
        model=model.strip() or None,
        api_key=api_key,
        base_url=base_url.strip() or None,
        cache_dir=out_dir / ".cache",
        min_delay_sec=delay,
    )
    if not use_cache:
        orig_call = client.call_json
        client.call_json = lambda stage, s, u, use_cache=True: orig_call(stage, s, u, use_cache=False)

    bar = st.progress(0.0, text="Starting...")

    def _progress(frac: float, msg: str) -> None:
        bar.progress(min(1.0, max(0.0, frac)), text=msg)

    try:
        result = run_pipeline(parsed, client, out_dir, progress=_progress)
    except Exception as e:  # noqa: BLE001
        st.error(f"Pipeline failed: {e}")
        st.info("Cached progress is kept — fix the issue (key, quota, delay) and click Find Promos again.")
        st.stop()

    st.session_state["result"] = {
        "ranked": result["ranked"],
        "audit": result["audit_log"],
        "metrics": result["metrics"],
        "paths": {k: str(v) for k, v in result["paths"].items()},
    }

# ---------------- Results ----------------
res = st.session_state.get("result")
if res:
    ranked, audit, metrics, paths = res["ranked"], res["audit"], res["metrics"], res["paths"]

    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Ready-to-use promos",
        len(ranked),
        help="Clips that passed all hard rules (20–60s, clean sentence cuts, verified timestamps) and were scored by the blind judge.",
    )
    c2.metric(
        "Filtered out by rules",
        audit["total_rejected"],
        help="AI draft clips automatically dropped for breaking a hard rule (e.g. >60s, <20s, or duplicate overlap). Check the Validation audit tab to see why.",
    )
    c3.metric(
        "AI calls (cache hits)",
        f"{metrics['api_calls']} ({metrics['cache_hits']})",
        help="Live API calls made vs. saved answers reused from cache.",
    )

    tab_cards, tab_audit = st.tabs(["🏆 Ranked promos", "🛡️ Validation audit"])

    with tab_cards:
        if not ranked:
            st.warning("No promos passed validation. Check the audit tab for rejection reasons.")
        tier_badges = {
            "Tier 1: Post First (Cold Traffic)": "🚀 Best for New Viewers",
            "Tier 2: Scale / A-B Test": "🧪 Good for A/B Testing",
            "Tier 3: Retargeting / Editor Review": "🎯 Editor Review / Retargeting",
        }
        for p in ranked:
            sc = p["scores"]
            arc_title = p["arc_label"].replace("_", " ").strip().title()
            badge = tier_badges.get(p["posting_tier"], p["posting_tier"])
            with st.expander(
                f"#{p['rank']} — {arc_title}  |  ⭐ {p['final_score']:.1f}/10  |  "
                f"⏱️ {p['total_duration_sec']:.0f}s  |  {badge}",
                expanded=p["rank"] <= 3,
            ):
                st.markdown(f"**Playback order:** " + " → ".join(
                    f"Ep {s['episode']} ({s['start_ts']}–{s['end_ts']}, {s['duration_sec']:.1f}s)" for s in p["segments"]
                ))
                st.markdown(f"**Hook (0–3s):** {p['hook']}")
                st.markdown(f"**Cliffhanger:** {p['cliffhanger']}")
                st.markdown(f"**Why it works:** {p['why_it_works']}")
                st.markdown(f"**Honest critique:** {p['honest_critique']}")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Hook", sc["hook_3s"])
                m2.metric("Cliffhanger", sc["cliffhanger"])
                m3.metric("Clarity", sc["standalone_clarity"])
                m4.metric("Spoiler risk", p["spoiler_risk"])
                if p["quality_flags"]:
                    st.caption("Flags: " + ", ".join(p["quality_flags"]))
                with st.expander("Clip script"):
                    st.code(p["blind_clip_script"], language=None)

    with tab_audit:
        st.caption(
            "Every draft clip is checked against hard rules (20–60s duration, clean timestamps, no duplicates) "
            "so only ready-to-edit promos reach your ranked list."
        )
        rejected = audit.get("rejected_candidates", [])
        if not rejected:
            st.success("All AI draft clips passed the rules — nothing was filtered out!")
        else:
            rule_labels = {
                "DURATION_TOO_LONG": "too long (>60s)",
                "DURATION_TOO_SHORT": "too short (<20s)",
                "DUPLICATE_OVERLAP": "duplicate overlap",
                "STITCH_SINGLE_EPISODE": "single-episode mashup",
                "HALLUCINATED_BLOCK_ID": "invalid scene reference",
                "REVERSED_INTRA_SEGMENT": "reversed timestamps",
                "INVALID_EPISODE": "unknown episode",
                "EMPTY_SEGMENTS": "empty clip",
            }
            summary_parts = [
                f"{count} {rule_labels.get(rule, rule.lower().replace('_', ' '))}"
                for rule, count in audit.get("rejection_counts_by_rule", {}).items()
            ]
            if summary_parts:
                st.markdown(f"**Filtered out ({len(rejected)}):** " + " · ".join(summary_parts))

            for r in rejected:
                title = (r.get("arc_label") or "Draft Clip").replace("_", " ").strip().title()
                segs = r.get("segments") or []
                dur = r.get("total_duration_sec")
                rule = r.get("rule_violated", "")

                if segs:
                    clip_loc = " → ".join(
                        f"Ep {s['episode']} ({s['start_ts']}–{s['end_ts']}, {s['duration_sec']:.1f}s)"
                        for s in segs
                    )
                else:
                    raw_eps = sorted({s.get("episode") for s in (r.get("raw_segments") or []) if s.get("episode") is not None})
                    clip_loc = ", ".join(f"Ep {e}" for e in raw_eps) if raw_eps else "Episode unavailable"

                if rule == "DURATION_TOO_LONG":
                    dur_txt = f" ({dur:.0f}s)" if dur is not None else ""
                    reason = f"Too long{dur_txt} — promos must be **20–60 seconds**."
                elif rule == "DURATION_TOO_SHORT":
                    dur_txt = f" ({dur:.0f}s)" if dur is not None else ""
                    reason = f"Too short{dur_txt} — promos must be **20–60 seconds**."
                elif rule == "STITCH_SINGLE_EPISODE":
                    eps = sorted({s["episode"] for s in segs}) or sorted(
                        {s.get("episode") for s in (r.get("raw_segments") or []) if s.get("episode") is not None}
                    )
                    ep_txt = f"Episode {eps[0]}" if len(eps) == 1 else "a single episode"
                    reason = f"Pitched as a cross-episode mashup, but it only used **{ep_txt}**."
                elif rule == "DUPLICATE_OVERLAP":
                    reason = "Mostly duplicates another clip from the same episode — kept one copy to avoid repeats."
                elif rule in ("HALLUCINATED_BLOCK_ID", "REVERSED_INTRA_SEGMENT", "INVALID_EPISODE"):
                    reason = "Referenced a scene or timestamp that doesn't match the screenplay — safely dropped."
                else:
                    reason = r.get("detail", "Did not pass validation rules.")

                header = f"**{title}**" + (f"  |  ⏱️ {dur:.0f}s" if dur is not None else "")
                with st.container(border=True):
                    st.markdown(header)
                    st.markdown(f"**Clip:** {clip_loc}")
                    st.markdown(f"🚫 {reason}")
                    if r.get("clip_script"):
                        with st.expander("Clip script"):
                            st.code(r["clip_script"], language=None)
