"""Simple beat grid estimation for a music track (no librosa needed)."""
from __future__ import annotations

import numpy as np

from . import media


def onset_envelope(audio: np.ndarray, sr: int, hop: int = 512, win: int = 1024) -> tuple[np.ndarray, float]:
    """Spectral-flux onset strength; returns (envelope, frames_per_second)."""
    if audio.size < win * 2:
        return np.zeros(1), sr / hop
    n = (len(audio) - win) // hop
    window = np.hanning(win).astype(np.float32)
    frames = np.lib.stride_tricks.as_strided(
        audio, shape=(n, win), strides=(audio.strides[0] * hop, audio.strides[0])
    ) * window
    mag = np.abs(np.fft.rfft(frames, axis=1))
    logmag = np.log1p(mag * 10)
    # broadband spectral flux (snares, hats, clicks)
    flux = np.maximum(np.diff(logmag, axis=0), 0).sum(axis=1)
    # low-band energy onsets (kicks / bass hits), so beats outrank offbeat hats
    lo_bins = max(2, int(200.0 / (sr / 2) * mag.shape[1]))
    low = np.log1p(mag[:, :lo_bins].sum(axis=1) * 10)
    low_on = np.maximum(np.diff(low), 0)

    def _norm(x: np.ndarray) -> np.ndarray:
        x = np.concatenate([[0.0], x])
        k = int(sr / hop * 0.5)
        if k > 1:
            x = np.maximum(x - np.convolve(x, np.ones(k) / k, mode="same"), 0)
        nz = x[x > 0]
        scale = float(np.percentile(nz, 90)) if nz.size > 10 else (float(x.max()) if x.max() > 0 else 1.0)
        return np.minimum(x / max(scale, 1e-9), 1.0)

    env = 0.5 * _norm(flux) + 0.5 * _norm(low_on)
    return env.astype(np.float32), sr / hop


def estimate_tempo(env: np.ndarray, fps: float, lo: float = 70.0, hi: float = 180.0) -> float:
    """Tempo in BPM via autocorrelation of the onset envelope."""
    if env.size < int(fps * 4):
        return 120.0
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1 :]
    ac = ac / (ac[0] + 1e-9)
    min_lag, max_lag = int(fps * 60 / hi), int(fps * 60 / lo)
    if max_lag >= len(ac):
        max_lag = len(ac) - 1
    if max_lag <= min_lag:
        return 120.0
    seg = ac[min_lag:max_lag]
    # mild preference for tempos near 120
    lags = np.arange(min_lag, max_lag)
    bpm = 60.0 * fps / lags
    weight = np.exp(-0.5 * ((np.log2(bpm / 120.0)) / 0.6) ** 2)
    lag = int(lags[np.argmax(seg * weight)])
    return 60.0 * fps / lag


def refine(env: np.ndarray, fps: float, bpm: float) -> tuple[float, float]:
    """Fine-tune bpm and phase by maximising onset energy on the beat grid. Returns (bpm, phase_seconds)."""
    best = (bpm, 0.0, -1.0)
    n = len(env)
    for cand in np.linspace(bpm - 4.0, bpm + 4.0, 161):
        pf = 60.0 / cand * fps
        for phase in np.linspace(0, pf, 32, endpoint=False):
            idx = np.arange(phase, n - 1, pf)
            i0 = idx.astype(int)
            frac = idx - i0
            vals = env[i0] * (1 - frac) + env[np.minimum(i0 + 1, n - 1)] * frac
            score = float(vals.sum()) / max(1, len(idx)) * np.sqrt(len(idx))
            if score > best[2]:
                best = (float(cand), float(phase / fps), score)
    return best[0], best[1]


def beat_grid(env: np.ndarray, fps: float, bpm: float, duration: float) -> list[float]:
    """Beat times (s): a regular grid at the refined bpm/phase that best matches onsets."""
    bpm, t0 = refine(env, fps, bpm)
    period = 60.0 / bpm
    return [round(float(t), 3) for t in np.arange(t0, duration, period)]


def analyze_music(path: str) -> dict:
    sr = 22050
    audio = media.decode_audio_mono(path, sr)
    duration = len(audio) / sr
    env, fps = onset_envelope(audio, sr)
    bpm = estimate_tempo(env, fps)
    beats = beat_grid(env, fps, bpm, duration)
    if len(beats) > 2:
        bpm = 60.0 / (beats[1] - beats[0])
    return {"path": path, "duration": round(duration, 3), "bpm": round(bpm, 2), "beats": beats}
