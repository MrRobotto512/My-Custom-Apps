"""Per-clip analysis: motion, loudness, scene changes, transcript, contact sheet. Cached as JSON."""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
from typing import Callable

import numpy as np

from . import media

CACHE_DIR = ".repairreel"
WINDOW = 0.5  # seconds per analysis window

Log = Callable[[str], None]


def _cache_path(folder: str) -> str:
    return os.path.join(folder, CACHE_DIR, "analysis.json")


def _file_key(path: str) -> str:
    st = os.stat(path)
    return hashlib.md5(f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()


def load_cache(folder: str) -> dict:
    try:
        with open(_cache_path(folder), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(folder: str, data: dict) -> None:
    os.makedirs(os.path.join(folder, CACHE_DIR), exist_ok=True)
    with open(_cache_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def _windows(values: np.ndarray, per_sec: float, duration: float) -> list[float]:
    """Average a per-frame series into WINDOW-second buckets covering the whole clip."""
    n = max(1, int(np.ceil(duration / WINDOW)))
    out = np.zeros(n, dtype=np.float32)
    if len(values) == 0:
        return out.tolist()
    idx = np.minimum((np.arange(len(values)) / per_sec / WINDOW).astype(int), n - 1)
    counts = np.bincount(idx, minlength=n).astype(np.float32)
    sums = np.bincount(idx, weights=values, minlength=n).astype(np.float32)
    out = np.where(counts > 0, sums / np.maximum(counts, 1), 0)
    return [round(float(v), 4) for v in out]


def motion_and_scenes(path: str, duration: float) -> tuple[list[float], list[float]]:
    """Motion per window and scene-change times, from a single decode of the clip."""
    fps = 4.0
    frames, scenes = media.analyze_video(path, fps=fps, scenes=True)
    if len(frames) < 2:
        return _windows(np.zeros(0), fps, duration), scenes
    f = frames.astype(np.float32)
    diff = np.abs(f[1:] - f[:-1]).mean(axis=(1, 2)) / 255.0  # 0..1 mean abs pixel change
    diff = np.concatenate([[0.0], diff])
    return _windows(diff, fps, duration), scenes


def motion_series(path: str, duration: float) -> list[float]:
    return motion_and_scenes(path, duration)[0]


def loudness_series(audio: np.ndarray, sr: int, duration: float) -> list[float]:
    """RMS loudness in dBFS per window (-60 = silence)."""
    if audio.size == 0:
        return [-60.0] * max(1, int(np.ceil(duration / WINDOW)))
    hop = int(sr * 0.1)
    n = len(audio) // hop
    rms = np.sqrt(np.maximum(1e-10, (audio[: n * hop].reshape(n, hop) ** 2).mean(axis=1)))
    db = 20 * np.log10(np.maximum(rms, 1e-5))
    db = np.clip(db, -60, 0)
    return _windows(db + 60.0, 10.0, duration)  # store as 0..60 (higher = louder)


def transcribe(audio: np.ndarray, model_name: str, jargon: str, log: Log) -> list[dict]:
    """Transcribe the narration. Returns [{start, end, text}]. Empty list if unavailable/disabled."""
    if audio.size == 0 or not model_name or model_name.lower() == "none":
        return []
    backend = whisper_backend()
    if backend == "mlx":
        return _transcribe_mlx(audio, model_name, jargon, log)
    if backend == "none":
        log("  no transcriber installed; skipping transcript (run setup to add faster-whisper / mlx-whisper)")
        return []
    return _transcribe_faster(audio, model_name, jargon, log)


def whisper_backend() -> str:
    """'mlx' on Apple Silicon with mlx-whisper installed, 'faster' if faster-whisper is installed, else 'none'."""
    forced = os.environ.get("REPAIRREEL_WHISPER_BACKEND", "").lower()
    if forced in ("mlx", "faster"):
        return forced
    import platform
    import sys

    if sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper  # type: ignore  # noqa: F401

            return "mlx"
        except Exception:
            pass
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        return "faster"
    except Exception:
        return "none"


# ---- faster-whisper (CTranslate2): NVIDIA GPU or CPU
def _transcribe_faster(audio: np.ndarray, model_name: str, jargon: str, log: Log) -> list[dict]:
    model = _get_whisper(model_name, log)
    segs, _info = model.transcribe(
        audio,
        language="en" if model_name.endswith(".en") else None,
        beam_size=3,
        vad_filter=True,
        initial_prompt=jargon or None,
        condition_on_previous_text=False,
    )
    out = []
    for s in segs:
        text = s.text.strip()
        if text:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
    return out


# ---- mlx-whisper: Apple Silicon GPU
MLX_MODELS = {
    "tiny.en": "mlx-community/whisper-tiny.en-mlx", "base.en": "mlx-community/whisper-base.en-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx", "medium.en": "mlx-community/whisper-medium.en-mlx",
    "tiny": "mlx-community/whisper-tiny", "base": "mlx-community/whisper-base-mlx", "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx", "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo", "turbo": "mlx-community/whisper-large-v3-turbo",
}
_MLX_ANNOUNCED = set()


def _transcribe_mlx(audio: np.ndarray, model_name: str, jargon: str, log: Log) -> list[dict]:
    import mlx_whisper  # type: ignore

    repo = MLX_MODELS.get(model_name, model_name if "/" in model_name else MLX_MODELS["small.en"])
    if repo not in _MLX_ANNOUNCED:
        log(f"  transcribing with '{repo}' on the Apple GPU via MLX (first time downloads it)")
        _MLX_ANNOUNCED.add(repo)
    kwargs = {"path_or_hf_repo": repo, "initial_prompt": jargon or None, "condition_on_previous_text": False, "verbose": None}
    if model_name.endswith(".en"):
        kwargs["language"] = "en"
    result = mlx_whisper.transcribe(audio.astype(np.float32), **kwargs)
    out = []
    for s in result.get("segments", []):
        text = str(s.get("text", "")).strip()
        if text:
            out.append({"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": text})
    return out


_WHISPER = {}


def _get_whisper(model_name: str, log: Log):
    if model_name not in _WHISPER:
        register_cuda_dlls()
        from faster_whisper import WhisperModel  # type: ignore

        device = os.environ.get("REPAIRREEL_WHISPER_DEVICE", "auto").lower()
        if device == "auto":
            device = "cpu"
            try:
                import ctranslate2  # type: ignore

                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
            except Exception:
                pass
        compute = "float16" if device == "cuda" else "int8"
        log(f"  loading whisper model '{model_name}' on {'the GPU' if device == 'cuda' else 'the CPU'} (first time downloads it)")
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute)
            if device == "cuda":
                # make the CUDA libraries load now, so a missing DLL fails here with a clear message
                list(model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if device == "cuda":
                raise RuntimeError(
                    "The GPU transcriber could not start: " + msg + "\n\n"
                    "The CUDA runtime libraries are missing from the app's environment. In a terminal, in the RepairReel folder, run:\n"
                    "    .venv\\Scripts\\pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
                    "then start the app again (setup.bat now installs these). Also make sure the NVIDIA driver is current."
                )
            raise
        _WHISPER[model_name] = model
    return _WHISPER[model_name]


def register_cuda_dlls() -> list[str]:
    """Make pip-installed NVIDIA libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12) findable on Windows.

    pip drops the DLLs into site-packages/nvidia/<lib>/bin, which is not on the DLL search path,
    so ctranslate2 reports 'cublas64_12.dll is not found' even though it is installed.
    """
    found: list[str] = []
    if os.name != "nt":
        return found
    import site
    import sys

    roots = set(site.getsitepackages() if hasattr(site, "getsitepackages") else [])
    roots.add(os.path.join(sys.prefix, "Lib", "site-packages"))
    for root in roots:
        nv = os.path.join(root, "nvidia")
        if not os.path.isdir(nv):
            continue
        for name in sorted(os.listdir(nv)):
            b = os.path.join(nv, name, "bin")
            if os.path.isdir(b):
                found.append(b)
    for b in found:
        try:
            os.add_dll_directory(b)
        except Exception:
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
    return found


def analyze_folder(folder: str, log: Log, whisper_model: str = "small.en", jargon: str = "", force: bool = False,
                   progress: Callable[[float], None] | None = None) -> dict:
    """Analyze every clip in the folder (cached). Returns {"clips": [...], "folder": folder}."""
    clips = media.list_clips(folder)
    if not clips:
        raise RuntimeError("No video files found in " + folder)
    cache = load_cache(folder) if not force else {}
    by_key = {c.get("key"): c for c in cache.get("clips", [])}
    result = []
    log(f"video decoding: {media.hwaccel_name()}")
    for i, path in enumerate(clips):
        key = _file_key(path)
        cached = by_key.get(key)
        if cached and (cached.get("whisper_model") == whisper_model or not cached.get("has_audio")):
            log(f"[{i + 1}/{len(clips)}] {os.path.basename(path)} - cached")
            result.append(cached)
            if progress:
                progress((i + 1) / len(clips))
            continue
        log(f"[{i + 1}/{len(clips)}] {os.path.basename(path)}")
        info = media.probe(path)
        dur = info["duration"]
        log(f"  {info['width']}x{info['height']} {dur:.1f}s" + (f"  recorded {info['created']}" if info.get("created") else ""))
        audio = media.decode_audio_mono(path, 16000)
        motion, scenes = motion_and_scenes(path, dur)
        loud = loudness_series(audio, 16000, dur)
        transcript = transcribe(audio, whisper_model, jargon, log) if info["has_audio"] else []
        if transcript:
            log(f"  transcript: {len(transcript)} segments, e.g. \"{transcript[0]['text'][:70]}\"")
        sheet, times = media.contact_sheet(path, dur)
        os.makedirs(os.path.join(folder, CACHE_DIR), exist_ok=True)
        sheet_path = os.path.join(folder, CACHE_DIR, f"sheet_{i:03d}.jpg")
        with open(sheet_path, "wb") as f:
            f.write(sheet)
        entry = {
            "key": key,
            "index": i,
            "path": path,
            "name": os.path.basename(path),
            "duration": round(dur, 3),
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "has_audio": info["has_audio"],
            "created": info.get("created"),
            "motion": motion,
            "loudness": loud,
            "scenes": [round(s, 2) for s in scenes],
            "transcript": transcript,
            "whisper_model": whisper_model,
            "sheet": sheet_path,
            "sheet_times": [round(t, 2) for t in times],
        }
        result.append(entry)
        if progress:
            progress((i + 1) / len(clips))
        # save as we go so a crash doesn't lose work
        save_cache(folder, {"folder": folder, "clips": result})
    # order by recording time when available, else by name
    def sort_key(c):
        ts = c.get("created")
        try:
            return (0, _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(), c["name"].lower())
        except Exception:
            return (1, 0.0, c["name"].lower())

    result.sort(key=sort_key)
    for i, c in enumerate(result):
        c["index"] = i
    data = {"folder": folder, "clips": result}
    save_cache(folder, data)
    return data


def summarize_clip(c: dict, max_transcript_chars: int = 1500) -> dict:
    """Compact, LLM-friendly summary of one clip's analysis."""
    motion = np.array(c["motion"], dtype=np.float32)
    loud = np.array(c["loudness"], dtype=np.float32)
    dur = c["duration"]

    def top_windows(series: np.ndarray, k: int = 4, width_s: float = 4.0) -> list[dict]:
        if len(series) == 0:
            return []
        w = max(1, int(width_s / WINDOW))
        kernel = np.ones(w) / w
        sm = np.convolve(series, kernel, mode="same")
        picks, used = [], np.zeros(len(sm), dtype=bool)
        for _ in range(k):
            sm_masked = np.where(used, -1e9, sm)
            j = int(np.argmax(sm_masked))
            if sm_masked[j] <= -1e8:
                break
            picks.append({"t": round(j * WINDOW, 1), "score": round(float(sm[j]), 3)})
            used[max(0, j - w) : j + w] = True
        return sorted(picks, key=lambda p: p["t"])

    speech = ""
    for s in c["transcript"]:
        speech += f"[{media.fmt_time(s['start'])}] {s['text']}\n"
    if len(speech) > max_transcript_chars:
        speech = speech[: max_transcript_chars - 20] + "\n...(truncated)"
    return {
        "clip": c["index"],
        "file": c["name"],
        "duration_s": round(dur, 1),
        "recorded": c.get("created"),
        "avg_motion": round(float(motion.mean()) if len(motion) else 0, 3),
        "most_active_moments": top_windows(motion, 4),
        "loudest_moments": top_windows(loud, 3),
        "silent_share": round(float((loud < 15).mean()) if len(loud) else 1.0, 2),
        "camera_moves_at": c["scenes"][:12],
        "transcript": speech.strip() or "(no speech detected)",
        "contact_sheet_frame_times": c["sheet_times"],
    }


def sheet_b64(c: dict) -> str | None:
    try:
        with open(c["sheet"], "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None
