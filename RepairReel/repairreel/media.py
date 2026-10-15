"""ffmpeg / ffprobe discovery and low-level media helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".m2ts", ".ts", ".wmv", ".3gp", ".mxf"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}

_FFMPEG: Optional[str] = None
_FFPROBE: Optional[str] = None


def _creationflags() -> int:
    # Don't flash console windows on Windows when launched from the GUI.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def find_tools() -> tuple[str, str]:
    """Return (ffmpeg, ffprobe) paths. Order: env override, PATH, static_ffmpeg, imageio_ffmpeg."""
    global _FFMPEG, _FFPROBE
    if _FFMPEG and _FFPROBE:
        return _FFMPEG, _FFPROBE
    # 1. the app's own bin\ folder (setup.bat puts a known-good ffmpeg there)  2. env override  3. PATH  4. pip packages
    exe = ".exe" if sys.platform == "win32" else ""
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bundled_ff = os.path.join(app_root, "bin", "ffmpeg" + exe)
    bundled_fp = os.path.join(app_root, "bin", "ffprobe" + exe)
    ff = (bundled_ff if os.path.isfile(bundled_ff) else None) or os.environ.get("REPAIRREEL_FFMPEG") or shutil.which("ffmpeg")
    fp = (bundled_fp if os.path.isfile(bundled_fp) else None) or os.environ.get("REPAIRREEL_FFPROBE") or shutil.which("ffprobe")
    if not (ff and fp):
        try:
            from static_ffmpeg import run as _sf  # type: ignore

            ff2, fp2 = _sf.get_or_fetch_platform_executables_else_raise()
            ff, fp = ff or ff2, fp or fp2
        except Exception:
            pass
    if not ff:
        try:
            import imageio_ffmpeg  # type: ignore

            ff = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    if not ff:
        raise RuntimeError(
            "ffmpeg not found. Run setup.bat (installs a copy), or install ffmpeg and put it on PATH."
        )
    if not fp:
        # ffprobe missing: we can live without it (probe() falls back to ffmpeg -i parsing)
        fp = ""
    _FFMPEG, _FFPROBE = ff, fp
    return ff, fp


_HWACCEL: Optional[list] = None


def hwaccel_args() -> list[str]:
    """ffmpeg input options that decode on the GPU (NVDEC on NVIDIA, VideoToolbox on macOS), or [] if not usable.

    ffmpeg falls back to software decoding on its own when a clip's codec isn't supported by the hardware,
    so these are safe to put in front of every input.
    """
    global _HWACCEL
    if _HWACCEL is not None:
        return list(_HWACCEL)
    if os.environ.get("REPAIRREEL_HWACCEL", "").lower() in ("none", "off", "0"):
        _HWACCEL = []
        return []
    ff, _ = find_tools()
    try:
        listed = run([ff, "-hide_banner", "-hwaccels"], check=False).stdout.decode("utf-8", "replace").lower()
    except Exception:
        listed = ""
    want = "videotoolbox" if sys.platform == "darwin" else "cuda"
    _HWACCEL = []
    if want in listed:
        # real test: encode a tiny h264 clip, decode it with the hwaccel, make sure frames come out
        import tempfile

        tmp = os.path.join(tempfile.gettempdir(), "repairreel_hwtest.mp4")
        try:
            run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-frames:v", "12",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", tmp], check=False)
            p = run([ff, "-v", "error", "-hwaccel", want, "-i", tmp, "-f", "rawvideo", "-pix_fmt", "gray", "-s", "16x9", "-"], check=False)
            if p.returncode == 0 and len(p.stdout) >= 16 * 9 * 12 and not p.stderr.strip():
                _HWACCEL = ["-hwaccel", want]
        except Exception:
            _HWACCEL = []
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    return list(_HWACCEL)


def hwaccel_name() -> str:
    a = hwaccel_args()
    return {"cuda": "NVDEC (GPU)", "videotoolbox": "VideoToolbox (GPU)"}.get(a[1], a[1]) if a else "software (CPU)"


def run(cmd: list[str], capture: bool = True, check: bool = True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
        creationflags=_creationflags(),
    )


def probe(path: str) -> dict:
    """Return {duration, width, height, fps, has_audio, created} for a media file."""
    ff, fp = find_tools()
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 30.0, "has_audio": False, "created": None, "rotation": 0}
    if fp:
        p = run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], check=False)
        try:
            data = json.loads(p.stdout.decode("utf-8", "replace"))
        except Exception:
            data = {}
        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration") or 0.0)
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        info["created"] = tags.get("creation_time")
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not info["width"]:
                info["width"] = int(s.get("width") or 0)
                info["height"] = int(s.get("height") or 0)
                r = s.get("avg_frame_rate") or s.get("r_frame_rate") or "30/1"
                try:
                    n, d = r.split("/")
                    info["fps"] = float(n) / float(d) if float(d) else 30.0
                except Exception:
                    pass
                rot = 0
                try:
                    rot = int(float((s.get("tags") or {}).get("rotate", 0)))
                except Exception:
                    pass
                for sd in s.get("side_data_list") or []:
                    if "rotation" in sd:
                        try:
                            rot = int(float(sd["rotation"]))
                        except Exception:
                            pass
                info["rotation"] = rot
                if not info["duration"]:
                    info["duration"] = float(s.get("duration") or 0.0)
                st = (s.get("tags") or {}).get("creation_time")
                if st and not info["created"]:
                    info["created"] = st
            elif s.get("codec_type") == "audio":
                info["has_audio"] = True
    if not info["duration"]:
        # fallback: parse ffmpeg -i banner
        p = run([ff, "-i", path], check=False)
        txt = p.stderr.decode("utf-8", "replace")
        import re

        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})", txt)
        if m and not info["width"]:
            info["width"], info["height"] = int(m.group(1)), int(m.group(2))
        info["has_audio"] = "Audio:" in txt
    # rotation metadata means the displayed frame is swapped
    if info["rotation"] in (90, -90, 270, -270):
        info["width"], info["height"] = info["height"], info["width"]
    return info


def decode_audio_mono(path: str, sr: int = 16000):
    """Decode the audio track to a float32 numpy array (mono, sr Hz). Returns empty array if no audio."""
    import numpy as np

    ff, _ = find_tools()
    p = run([ff, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], check=False)
    if p.returncode != 0 or not p.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def decode_gray_frames(path: str, fps: float = 4.0, w: int = 160, h: int = 90):
    """Decode downscaled grayscale frames as a (n, h, w) uint8 array."""
    frames, _ = analyze_video(path, fps=fps, w=w, h=h, scenes=False)
    return frames


def analyze_video(path: str, fps: float = 4.0, w: int = 160, h: int = 90, scenes: bool = True, scene_threshold: float = 0.35):
    """ONE decode of the clip (on the GPU when possible) that yields both the low-res grayscale frames used for
    motion analysis and the scene-change timestamps. Returns (frames[n,h,w] uint8, [scene times])."""
    import re

    import numpy as np

    ff, _ = find_tools()
    graph = f"[0:v]split[m][s];[m]fps={fps},scale={w}:{h}:flags=area,format=gray[mo];" \
            f"[s]scale=320:-2:flags=fast_bilinear,select='gt(scene,{scene_threshold})',showinfo[so]"
    if not scenes:
        graph = f"[0:v]fps={fps},scale={w}:{h}:flags=area,format=gray[mo]"
    cmd = [ff, "-v", "info", "-nostats"] + hwaccel_args() + ["-i", path, "-filter_complex", graph,
           "-map", "[mo]", "-f", "rawvideo", "-"]
    if scenes:
        cmd += ["-map", "[so]", "-f", "null", "-"]
    p = run(cmd, check=False)
    n = len(p.stdout) // (w * h)
    frames = np.frombuffer(p.stdout[: n * w * h], dtype=np.uint8).reshape(n, h, w) if n else np.zeros((0, h, w), dtype=np.uint8)
    times = [float(t) for t in re.findall(r"pts_time:\s*([0-9.]+)", p.stderr.decode("utf-8", "replace"))] if scenes else []
    return frames, times


def scene_changes(path: str, threshold: float = 0.35) -> list[float]:
    """Timestamps (s) where ffmpeg's scene detector fires (separate pass; analyze_video() combines both)."""
    _, times = analyze_video(path, scenes=True, scene_threshold=threshold)
    return times


def extract_frame(path: str, t: float, width: int = 320):
    """Return a PIL image of the frame at time t (seconds)."""
    from io import BytesIO

    from PIL import Image

    ff, _ = find_tools()
    p = run(
        [ff, "-v", "error"] + hwaccel_args() + ["-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"],
        check=False,
    )
    if not p.stdout:
        return Image.new("RGB", (width, width * 9 // 16), (20, 20, 20))
    return Image.open(BytesIO(p.stdout)).convert("RGB")


def contact_sheet(path: str, duration: float, n: int = 9, tile_w: int = 320, cols: int = 3) -> tuple[bytes, list[float]]:
    """A cols x rows grid of frames with timestamps burned in. Returns (jpeg_bytes, times)."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    times = [duration * (i + 0.5) / n for i in range(n)] if duration > 0 else [0.0] * n
    tiles = [extract_frame(path, t, tile_w) for t in times]
    th = max(im.height for im in tiles)
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w, rows * th), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    font = _label_font(20)
    for i, (im, t) in enumerate(zip(tiles, times)):
        x, y = (i % cols) * tile_w, (i // cols) * th
        sheet.paste(im, (x, y))
        label = f"#{i + 1}  {fmt_time(t)}"
        tw = draw.textlength(label, font=font) if font else 7 * len(label)
        draw.rectangle([x, y, x + 12 + tw, y + 26], fill=(0, 0, 0))
        draw.text((x + 6, y + 3), label, fill=(255, 255, 0), font=font)
    buf = BytesIO()
    sheet.save(buf, format="JPEG", quality=78)
    return buf.getvalue(), times


def _label_font(size: int):
    from PIL import ImageFont

    for cand in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return None


def fmt_time(t: float) -> str:
    t = max(0.0, float(t))
    m, s = divmod(int(t), 60)
    return f"{m}:{s:02d}" if m < 60 else f"{m // 60}:{m % 60:02d}:{s:02d}"


def list_clips(folder: str) -> list[str]:
    out = []
    for name in sorted(os.listdir(folder), key=str.lower):
        p = os.path.join(folder, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in VIDEO_EXTS and not name.startswith("."):
            out.append(p)
    return out
