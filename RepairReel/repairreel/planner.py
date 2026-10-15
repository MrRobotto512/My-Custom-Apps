"""Turn clip analysis into an edit plan: with Claude (vision + transcript) or with plain signals."""
from __future__ import annotations

import json
import os
from typing import Callable

import numpy as np

from . import analyze

Log = Callable[[str], None]

PLAN_TOOL = {
    "name": "submit_edit_plan",
    "description": "Submit the final edit decision list for the highlight video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short video title, max 8 words, e.g. 'PS5 HDMI Port Replacement'"},
            "summary": {"type": "string", "description": "One or two sentences on what the repair was and the story of the edit."},
            "segments": {
                "type": "array",
                "description": "Ordered list of segments to include, in playback order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "clip": {"type": "integer", "description": "Clip index as given"},
                        "start": {"type": "number", "description": "Start time in seconds within the clip"},
                        "end": {"type": "number", "description": "End time in seconds within the clip"},
                        "caption": {"type": "string", "description": "Optional on-screen caption, max 6 words. Empty string for none."},
                        "why": {"type": "string", "description": "A few words on why this moment is in"},
                        "crop_x": {"type": "number", "description": "Where the action is horizontally, 0=left edge, 0.5=center, 1=right edge. Used when cropping to vertical."},
                    },
                    "required": ["clip", "start", "end", "caption", "why", "crop_x"],
                },
            },
        },
        "required": ["title", "summary", "segments"],
    },
}

SYSTEM_PROMPT = """You are a sharp video editor who cuts videos for an electronics / game-console repair shop's channel. \
You are given the analysis of the raw clips captured during ONE repair job: per-clip transcripts with timestamps, \
motion and loudness cues, and a 3x3 contact sheet of frames (frame times are listed). Your job is to choose the moments \
and their order for a highlight video, as a precise edit decision list (clip index, start, end).

General rules:
- Only use times inside each clip's duration. Never overlap segments from the same clip unless intentional.
- Skip: camera fumbling, setting up the shot, dead air, off-topic chatter, repeated takes (pick the best take), \
  long stretches where nothing changes on screen.
- The repair is the story: problem -> diagnosis -> the fix -> proving it works. Keep chronological order unless \
  reordering clearly helps the story. The last segment should be the payoff (device working, satisfied result) if one exists.
- Transcript times are approximate (about +/-0.3 s). Start a bit before a sentence begins and end a bit after it ends \
  so words are not clipped. Never cut in the middle of a sentence.
- Set crop_x to where the interesting thing is horizontally (hands, the board, the port) based on the contact sheet.
- Captions are short lower-thirds naming the step ("Removing the HDMI port"). Use them for key steps only, empty otherwise.
- Hit the target length within about 15%. If the raw material is too thin for the target, return less rather than padding with junk.
"""

STORY_RULES = """Style: STORY. The viewer should understand what was wrong and how it was fixed, told mostly in the \
technician's own narration. Prefer moments with speech; use silent footage only for visually important actions \
(soldering, removing a part, the device powering on). Segments are typically 4-30 seconds each."""

MONTAGE_RULES = """Style: MONTAGE. Fast, satisfying, music-driven. Speech is not needed (the music replaces it). \
Choose the most visually striking moments: close-ups of the work, flux flowing, a part coming off, the screen lighting up. \
Each segment 1.5-4 seconds. Vary the shots; do not use two near-identical moments back to back. Build up to the \
result shot at the end. Choose about target_length / 2.5 segments."""


class PlanError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Claude planner
# ----------------------------------------------------------------------------
def pick_model(client, requested: str, log: Log) -> str:
    if requested and requested.lower() != "auto":
        return requested
    try:
        models = list(client.models.list(limit=100).data)
    except Exception as e:
        log(f"  could not list models ({e}); using claude-sonnet-4-5")
        return "claude-sonnet-4-5"
    sonnets = [m for m in models if "sonnet" in m.id.lower()]
    pool = sonnets or models
    if not pool:
        return "claude-sonnet-4-5"
    pool.sort(key=lambda m: str(getattr(m, "created_at", "")), reverse=True)
    return pool[0].id


def build_messages(data: dict, style: str, target_length: int, aspect: str, notes: str, with_images: bool = True) -> list[dict]:
    content: list[dict] = []
    intro = (
        f"Settings:\n- style: {style}\n- target_length: {target_length} seconds\n- output aspect: {aspect}\n"
        + (f"- notes from the technician: {notes}\n" if notes.strip() else "")
        + f"\nThere are {len(data['clips'])} clips, listed in recording order. For each clip you get a JSON summary "
        "and its contact sheet (9 frames, left-to-right, top-to-bottom, times given).\n"
    )
    content.append({"type": "text", "text": intro})
    for c in data["clips"]:
        summ = analyze.summarize_clip(c)
        content.append({"type": "text", "text": f"=== Clip {c['index']}: {c['name']} ===\n" + json.dumps(summ, indent=1)})
        b64 = analyze.sheet_b64(c) if with_images else None
        if b64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    content.append(
        {
            "type": "text",
            "text": (STORY_RULES if style == "story" else MONTAGE_RULES)
            + "\n\nNow produce the edit plan by calling submit_edit_plan.",
        }
    )
    return [{"role": "user", "content": content}]


def plan_with_claude(data: dict, style: str, target_length: int, aspect: str, notes: str, api_key: str,
                     model: str, log: Log, client=None) -> dict:
    try:
        import anthropic  # type: ignore
    except Exception:
        raise PlanError("The 'anthropic' package is not installed. Run setup.bat.")
    if client is None:
        if not api_key:
            raise PlanError("No API key. Paste your Anthropic API key in Settings, or choose the 'signals only' planner.")
        client = anthropic.Anthropic(api_key=api_key)
    model_id = pick_model(client, model, log)
    log(f"  asking {model_id} for a {style} plan of about {target_length}s ...")
    messages = build_messages(data, style, target_length, aspect, notes)
    def call(force_tool: bool):
        kw = dict(model=model_id, max_tokens=8192, system=SYSTEM_PROMPT, tools=[PLAN_TOOL], messages=messages)
        kw["tool_choice"] = {"type": "tool", "name": "submit_edit_plan"} if force_tool else {"type": "auto"}
        return client.messages.create(**kw)

    try:
        try:
            resp = call(True)
        except Exception as e:  # noqa: BLE001
            # Some models refuse a forced tool call (400 "tool_choice: type 'tool' and 'any' are not supported").
            # Ask normally instead; the prompt already tells it to call submit_edit_plan.
            if "tool_choice" in str(e).lower():
                log("  this model does not accept a forced tool call; asking it normally")
                resp = call(False)
            else:
                raise
    except Exception as e:
        raise PlanError(f"Claude API call failed: {e}")
    plan = None
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "submit_edit_plan":
            plan = dict(block.input)
            break
    if plan is None:
        # last resort: the model answered in text - look for a JSON object with a "segments" list
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        plan = _json_from_text(text)
    if plan is None:
        raise PlanError("Claude did not return an edit plan. Try again, or pin a different model in Settings (e.g. claude-sonnet-4-5).")
    usage = getattr(resp, "usage", None)
    if usage:
        log(f"  tokens: {usage.input_tokens} in / {usage.output_tokens} out")
    plan["planner"] = model_id
    return finalize_plan(plan, data, style, target_length, aspect, log)


def _json_from_text(text: str) -> dict | None:
    """Pull the first JSON object containing a 'segments' list out of free text (handles ```json fences)."""
    import re

    for cand in re.findall(r"\{.*\}", text, flags=re.S):
        # try progressively shorter tails in case of trailing prose
        for end in range(len(cand), 0, -1):
            if cand[end - 1] != "}":
                continue
            try:
                obj = json.loads(cand[:end])
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("segments"), list):
                return obj
    return None


# ----------------------------------------------------------------------------
# Signals-only planner (no AI)
# ----------------------------------------------------------------------------
def plan_with_signals(data: dict, style: str, target_length: int, aspect: str, log: Log) -> dict:
    segs = []
    clips = data["clips"]
    total_dur = sum(c["duration"] for c in clips)
    share = target_length / max(total_dur, 1e-6)
    W = analyze.WINDOW
    for c in clips:
        motion = np.array(c["motion"], dtype=np.float32)
        loud = np.array(c["loudness"], dtype=np.float32)
        if len(motion) == 0:
            continue
        n = min(len(motion), len(loud))
        motion, loud = motion[:n], loud[:n]
        m_norm = motion / (motion.max() + 1e-6)
        l_norm = loud / 60.0
        score = 0.6 * m_norm + 0.4 * l_norm
        if style == "story":
            # speech-first: chunks of transcript up to 20 s
            want = c["duration"] * share
            chunks = []
            cur = None
            for s in c["transcript"]:
                if cur and s["start"] - cur["end"] < 1.5 and s["end"] - cur["start"] <= 20:
                    cur["end"] = s["end"]
                    cur["text"] += " " + s["text"]
                else:
                    if cur:
                        chunks.append(cur)
                    cur = dict(s)
            if cur:
                chunks.append(cur)
            if not chunks:
                # no speech: take the most active stretches
                chunks = [{"start": t, "end": min(c["duration"], t + 8), "text": "", "why": "most activity"} for t in _top_times(score, W, 8, k=6)]
            chunks.sort(key=lambda ch: -_mean_score(score, W, ch["start"], ch["end"]))
            got, chosen = 0.0, []
            for ch in chunks:
                if got >= want:
                    break
                chosen.append(ch)
                got += ch["end"] - ch["start"]
            chosen.sort(key=lambda ch: ch["start"])
            for ch in chosen:
                segs.append({"clip": c["index"], "start": max(0, ch["start"] - 0.3), "end": min(c["duration"], ch["end"] + 0.4),
                             "caption": "", "why": ch.get("why", "speech"), "crop_x": 0.5})
        else:
            k = max(1, int(round(c["duration"] * share / 2.5)))
            for t in _top_times(score, W, 2.5, k=k):
                segs.append({"clip": c["index"], "start": t, "end": min(c["duration"], t + 2.5), "caption": "",
                             "why": "high motion", "crop_x": 0.5})
    segs.sort(key=lambda s: (s["clip"], s["start"]))
    plan = {"title": os.path.basename(os.path.normpath(data["folder"])), "summary": "Picked by motion and loudness only.",
            "segments": segs, "planner": "signals"}
    return finalize_plan(plan, data, style, target_length, aspect, log)


def _mean_score(score: np.ndarray, W: float, a: float, b: float) -> float:
    i, j = int(a / W), max(int(a / W) + 1, int(b / W))
    return float(score[i:j].mean()) if j <= len(score) and j > i else 0.0


def _top_times(score: np.ndarray, W: float, width_s: float, k: int) -> list[float]:
    w = max(1, int(width_s / W))
    sm = np.convolve(score, np.ones(w) / w, mode="same")
    used = np.zeros(len(sm), dtype=bool)
    out = []
    for _ in range(k):
        masked = np.where(used, -1e9, sm)
        j = int(np.argmax(masked))
        if masked[j] <= -1e8:
            break
        out.append(round(max(0.0, j * W - width_s / 2), 2))
        used[max(0, j - 2 * w) : j + 2 * w] = True
    return sorted(out)


# ----------------------------------------------------------------------------
# Validation / normalisation
# ----------------------------------------------------------------------------
def finalize_plan(plan: dict, data: dict, style: str, target_length: int, aspect: str, log: Log) -> dict:
    clips = {c["index"]: c for c in data["clips"]}
    out = []
    for s in plan.get("segments", []):
        try:
            ci = int(s["clip"])
            c = clips[ci]
            a, b = float(s["start"]), float(s["end"])
        except Exception:
            continue
        a = max(0.0, min(a, c["duration"]))
        b = max(0.0, min(b, c["duration"]))
        if b - a < 0.5:
            continue
        out.append({
            "clip": ci,
            "file": c["name"],
            "start": round(a, 2),
            "end": round(b, 2),
            "caption": str(s.get("caption") or "").strip()[:60],
            "why": str(s.get("why") or "").strip()[:120],
            "crop_x": float(min(1.0, max(0.0, s.get("crop_x", 0.5) or 0.5))),
        })
    if not out:
        raise PlanError("The plan came back empty - nothing usable was found in the clips.")
    total = sum(s["end"] - s["start"] for s in out)
    log(f"  plan: {len(out)} segments, {total:.0f}s total (target {target_length}s)")
    return {
        "style": style,
        "aspect": aspect,
        "target_length": target_length,
        "title": str(plan.get("title") or "").strip()[:80],
        "summary": str(plan.get("summary") or "").strip(),
        "planner": plan.get("planner", "?"),
        "folder": data["folder"],
        "segments": out,
    }


def plan_path(folder: str) -> str:
    return os.path.join(folder, analyze.CACHE_DIR, "plan.json")


def save_plan(plan: dict) -> str:
    p = plan_path(plan["folder"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    return p


def load_plan(folder: str) -> dict | None:
    try:
        with open(plan_path(folder), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
