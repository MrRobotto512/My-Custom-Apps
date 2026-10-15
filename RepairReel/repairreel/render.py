"""Render an edit plan to a finished MP4 with ffmpeg."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from . import beats, media

Log = Callable[[str], None]


@dataclass
class RenderOptions:
    aspect: str = "16:9"          # "16:9" or "9:16"
    music: str = ""               # path to a music file, or ""
    music_volume: float = 0.9     # music gain (montage) / bed level (story)
    keep_original_audio: bool = True
    original_volume: float = 1.0  # original audio gain when music is present (montage default lowers it)
    captions: bool = True
    title_card: bool = True
    crf: int = 20
    encoder: str = "auto"         # auto = h264_nvenc if available, else libx264; or h264_amf / h264_qsv / hevc_nvenc
    preset: str = "medium"
    fps: int = 30
    height: int = 1080            # 1080 -> 1920x1080 or 1080x1920
    font: str = ""                # path to a .ttf; auto-detected if empty
    extra_args: list = field(default_factory=list)


_ENCODERS: set[str] | None = None


def available_encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is None:
        ff, _ = media.find_tools()
        try:
            out = media.run([ff, "-hide_banner", "-encoders"], check=False).stdout.decode("utf-8", "replace")
            _ENCODERS = set(re.findall(r"^\s*V[\w.]{5}\s+(\S+)", out, flags=re.M))
        except Exception:
            _ENCODERS = set()
    return _ENCODERS


_GPU_REASON = ""


def gpu_encoder_candidates() -> list[str]:
    if sys.platform == "darwin":
        return ["h264_videotoolbox"]
    return ["h264_nvenc"]


def encoder_works(enc: str) -> bool:
    """True if ffmpeg can actually open this encoder (listed AND driver/hardware usable)."""
    global _GPU_REASON
    if enc not in available_encoders():
        _GPU_REASON = f"this ffmpeg build has no {enc} encoder"
        return False
    ff, _ = media.find_tools()
    sink = "NUL" if os.name == "nt" else os.devnull
    p = media.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:r=30", "-frames:v", "10",
                   "-c:v", enc, "-f", "mp4", "-y", sink], check=False)
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0 or (enc.split("_")[-1] in err.lower() and ("error" in err.lower() or "not support" in err.lower())):
        line = next((l.strip() for l in err.splitlines() if enc.split("_")[-1] in l.lower()),
                    err.strip().splitlines()[-1] if err.strip() else "unknown error")
        _GPU_REASON = line
        return False
    return True


def nvenc_works() -> bool:
    return encoder_works("h264_nvenc")


def gpu_encoder() -> str:
    """The first working hardware encoder for this platform, or ''."""
    for enc in gpu_encoder_candidates():
        if encoder_works(enc):
            return enc
    return ""


def nvenc_reason() -> str:
    return _GPU_REASON


gpu_reason = nvenc_reason


_FFVER: int | None = None


def ffmpeg_major_version() -> int:
    global _FFVER
    if _FFVER is None:
        ff, _ = media.find_tools()
        _FFVER = 0
        try:
            out = media.run([ff, "-version"], check=False).stdout.decode("utf-8", "replace")
            m = re.search(r"ffmpeg version\s+n?(\d+)", out)
            if m:
                _FFVER = int(m.group(1))
        except Exception:
            pass
    return _FFVER


def filter_script_args(script_path: str) -> list[str]:
    """FFmpeg 7+ reads option values from files with '-/opt file'; older builds use -filter_complex_script."""
    if ffmpeg_major_version() >= 7:
        return ["-/filter_complex", script_path]
    return ["-filter_complex_script", script_path]


_FILTERS: set[str] | None = None


def available_filters() -> set[str]:
    global _FILTERS
    if _FILTERS is None:
        ff, _ = media.find_tools()
        try:
            out = media.run([ff, "-hide_banner", "-filters"], check=False).stdout.decode("utf-8", "replace")
            _FILTERS = set(re.findall(r"^\s*[TSC.]{3}\s+(\S+)", out, flags=re.M))
        except Exception:
            _FILTERS = set()
    return _FILTERS


def resolve_encoder(requested: str, log: Log) -> str:
    if requested and requested != "auto":
        return requested
    enc = gpu_encoder()
    if enc:
        log(f"  encoder: {enc} ({'Apple VideoToolbox' if 'videotoolbox' in enc else 'NVIDIA GPU'})")
        return enc
    log("  encoder: libx264 (CPU) - hardware encoder not usable: " + gpu_reason())
    if "driver does not support" in gpu_reason().lower() or "minimum required" in gpu_reason().lower():
        log("  -> this ffmpeg needs a newer NVIDIA driver than installed; use the ffmpeg 7.1.1 in bin/ (setup does this)")
    return "libx264"


def find_font(explicit: str = "") -> str:
    cands = [explicit] if explicit else []
    if sys.platform == "win32":
        win = os.environ.get("WINDIR", "C:/Windows")
        cands += [os.path.join(win, "Fonts", n) for n in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf", "calibrib.ttf")]
    elif sys.platform == "darwin":
        cands += ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"]
    cands += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def ffpath(p: str) -> str:
    """Escape a path for an unquoted filter-graph option (fontfile=, textfile=).

    The graph passes through two levels of ffmpeg's token parser, so each special
    character needs three backslashes. Verified against paths containing
    ':', ''', ',', '[', ']' and spaces (Windows drive letters included).
    """
    p = p.replace("\\", "/")
    return re.sub(r"([:'\[\],;\\])", lambda m: "\\\\\\" + m.group(1), p)


def dims(opts: RenderOptions) -> tuple[int, int]:
    h = opts.height
    if opts.aspect == "9:16":
        # vertical: width is the "height" figure (1080), height 1920
        return h, (h * 16 // 9) // 2 * 2
    return (h * 16 // 9) // 2 * 2, h


def quantize_to_beats(segments: list[dict], clips: dict, bpm: float, min_beats: int = 2, max_beats: int = 8) -> list[dict]:
    """Make every segment an exact multiple of the beat so cuts land on beats."""
    b = 60.0 / bpm
    out = []
    for s in segments:
        c = clips[s["clip"]]
        d = s["end"] - s["start"]
        nb = int(round(d / b))
        nb = max(min_beats, min(max_beats, nb))
        nd = nb * b
        start = s["start"]
        if start + nd > c["duration"]:
            start = max(0.0, c["duration"] - nd)
        nd = min(nd, c["duration"] - start)
        if nd < b:
            continue
        t = dict(s)
        t["start"], t["end"] = round(start, 3), round(start + nd, 3)
        out.append(t)
    return out


def _drawtext_caption(text_file: str, font: str, W: int, H: int, dur: float) -> str:
    size = int(H * 0.042) if W > H else int(W * 0.05)
    show = min(dur - 0.2, 5.0)
    return (
        f"drawtext=fontfile={ffpath(font)}:textfile={ffpath(text_file)}:expansion=none:fontsize={size}:fontcolor=white"
        f":box=1:boxcolor=black@0.45:boxborderw=14:x=(w-text_w)/2:y=h-text_h-{int(H * 0.09)}"
        f":enable='between(t,0.15,{show:.2f})'"
    )


def _drawtext_title(text_file: str, font: str, W: int, H: int) -> str:
    size = int(H * 0.075) if W > H else int(W * 0.075)
    return (
        f"drawtext=fontfile={ffpath(font)}:textfile={ffpath(text_file)}:expansion=none:fontsize={size}:fontcolor=white"
        f":x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=12"
    )


def render(plan: dict, data: dict, output: str, opts: RenderOptions, log: Log,
           progress: Callable[[float], None] | None = None) -> str:
    try:
        return _render(plan, data, output, opts, log, progress)
    except RuntimeError as e:
        msg = str(e)
        used_gpu = any(k in msg.lower() for k in ("nvenc", "amf", "qsv", "videotoolbox"))
        if used_gpu and opts.encoder in ("auto", "h264_nvenc", "hevc_nvenc", "h264_amf", "h264_qsv", "h264_videotoolbox", "hevc_videotoolbox"):
            first = next((l for l in msg.splitlines() if "nvenc" in l.lower() or "driver" in l.lower()), msg.splitlines()[1] if "\n" in msg else msg)
            log("  GPU encoder failed: " + first.strip())
            if "driver does not support" in msg.lower() or "minimum required" in msg.lower():
                log("  -> your NVIDIA driver is older than this ffmpeg needs; update it to get GPU encoding back")
            log("  retrying on the CPU encoder (libx264) so you still get the video ...")
            cpu = RenderOptions(**{**opts.__dict__, "encoder": "libx264"})
            return _render(plan, data, output, cpu, log, progress)
        raise


def _encoder_args(encoder: str, opts: RenderOptions) -> list[str]:
    args = ["-c:v", encoder, "-pix_fmt", "yuv420p", "-r", str(opts.fps)]
    if encoder == "libx264":
        args += ["-preset", opts.preset, "-crf", str(opts.crf)]
    elif "nvenc" in encoder:
        args += ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(opts.crf), "-b:v", "0", "-bf", "2", "-spatial-aq", "1"]
    elif "videotoolbox" in encoder:
        mbps = 12 if opts.height <= 1080 else 40
        args += ["-b:v", f"{mbps}M", "-allow_sw", "1"]
    else:
        args += ["-b:v", "12M"]
    return args


def _run_ffmpeg(cmd: list[str], total_s: float, on_progress, tmpdir: str, label: str) -> None:
    with open(os.path.join(tmpdir, "command.txt"), "a", encoding="utf-8") as f:
        f.write(f"# {label}\n" + subprocess.list2cmdline(cmd) + "\n\n")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    assert proc.stdout is not None
    for line in proc.stdout:
        m = re.match(r"out_time_us=(\d+)", line.strip())
        if m and on_progress and total_s > 0:
            on_progress(min(1.0, int(m.group(1)) / 1e6 / total_s))
    err = proc.stderr.read() if proc.stderr else ""
    proc.wait()
    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-12:])
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}) while {label}:\n{tail}\n(commands saved in {tmpdir}/command.txt)")


def _render(plan: dict, data: dict, output: str, opts: RenderOptions, log: Log,
            progress: Callable[[float], None] | None = None) -> str:
    """Segment-by-segment: each piece is decoded (GPU when possible), scaled/captioned and encoded (GPU when
    possible) to its own intermediate, then the pieces are joined without re-encoding the video.
    Memory stays flat no matter how many segments there are.

    Frame accuracy: every piece is cut to an exact number of video frames and audio samples on one cumulative
    time grid, and the joiner is told those exact durations, so cuts never drift off the beat."""
    ff, _ = media.find_tools()
    encoder = resolve_encoder(opts.encoder, log)
    hw = media.hwaccel_args()
    log(f"  decoding: {media.hwaccel_name()}")
    clips = {c["index"]: c for c in data["clips"]}
    segments = [dict(s) for s in plan["segments"] if s["clip"] in clips]
    if not segments:
        raise RuntimeError("Plan has no segments.")
    W, H = dims(opts)
    FPS, SR = int(opts.fps), 48000
    font = find_font(opts.font) if (opts.captions or opts.title_card) else ""
    if (opts.captions or opts.title_card) and not font:
        log("  no TrueType font found; captions/title disabled")
    if font and "drawtext" not in available_filters():
        log("  this ffmpeg build has no 'drawtext' filter; captions/title disabled (install the full build: winget install Gyan.FFmpeg)")
        font = ""
    tmpdir = tempfile.mkdtemp(prefix="repairreel_")

    music_info = None
    if opts.music:
        log("  analysing music beats ...")
        music_info = beats.analyze_music(opts.music)
        log(f"  music: {music_info['bpm']:.0f} BPM, {music_info['duration']:.0f}s")
        if plan.get("style") == "montage":
            segments = quantize_to_beats(segments, clips, music_info["bpm"])
            log(f"  cuts snapped to beats: {len(segments)} segments")

    title_dur = 2.5 if (opts.title_card and font and plan.get("title")) else 0.0
    total_out = title_dur + sum(s["end"] - s["start"] for s in segments)
    enc_args = _encoder_args(encoder, opts)
    base = [ff, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    vparts: list[tuple[str, float]] = []   # (file, exact duration)
    aparts: list[tuple[str, float]] = []
    t_cursor = 0.0        # cumulative target time
    f_cursor = 0          # cumulative frames emitted
    s_cursor = 0          # cumulative samples emitted
    done = 0.0

    def seg_progress(frac_of_piece: float, piece_dur: float) -> None:
        if progress:
            progress(min(0.97, (done + frac_of_piece * piece_dur) / max(total_out, 1e-6)))

    def grid(dur: float) -> tuple[int, int]:
        """Frames and samples this piece must contribute so the cumulative totals stay on the grid."""
        nonlocal t_cursor, f_cursor, s_cursor
        t_cursor += dur
        nf = int(round(t_cursor * FPS)) - f_cursor
        ns = int(round(t_cursor * SR)) - s_cursor
        f_cursor += nf
        s_cursor += ns
        return max(1, nf), max(1, ns)

    log(f"  rendering {len(segments)} segments -> {W}x{H}, {total_out:.0f}s, encoder {encoder}")

    # --- title card
    if title_dur > 0:
        tf = os.path.join(tmpdir, "title.txt")
        with open(tf, "w", encoding="utf-8") as f:
            f.write(plan["title"])
        nf, ns = grid(title_dur)
        vout, aout = os.path.join(tmpdir, "title.mp4"), os.path.join(tmpdir, "title.wav")
        vf = f"{_drawtext_title(tf, font, W, H)},fade=t=in:d=0.4,fade=t=out:st={title_dur - 0.5:.2f}:d=0.5,format=yuv420p,setsar=1"
        cmd = base + ["-f", "lavfi", "-t", f"{title_dur + 1}", "-i", f"color=c=0x0f1419:s={W}x{H}:r={FPS}",
                      "-f", "lavfi", "-t", f"{title_dur + 1}", "-i", "anullsrc=r=48000:cl=stereo",
                      "-map", "0:v", "-vf", vf, "-frames:v", str(nf)] + enc_args + ["-an", vout,
                      "-map", "1:a", "-af", f"atrim=end_sample={ns}", "-c:a", "pcm_s16le", "-vn", aout]
        _run_ffmpeg(cmd, title_dur, lambda fr: seg_progress(fr, title_dur), tmpdir, "rendering the title card")
        vparts.append((vout, nf / FPS))
        aparts.append((aout, ns / SR))
        done += title_dur

    # --- segments, one at a time
    hw_ok = True
    for i, s in enumerate(segments):
        c = clips[s["clip"]]
        dur = s["end"] - s["start"]
        nf, ns = grid(dur)
        if opts.aspect == "9:16":
            cx = float(s.get("crop_x", 0.5))
            vf = (f"crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':x='(iw-ow)*{cx:.3f}':y='(ih-oh)/2',"
                  f"scale={W}:{H}:flags=lanczos")
        else:
            vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
        vf += f",fps={FPS},setsar=1,format=yuv420p"
        if opts.captions and font and s.get("caption"):
            tf = os.path.join(tmpdir, f"cap_{i:03d}.txt")
            with open(tf, "w", encoding="utf-8") as f:
                f.write(s["caption"])
            vf += "," + _drawtext_caption(tf, font, W, H, dur)
        vf += ",tpad=stop_mode=clone:stop_duration=2"   # pad by cloning the last frame if the clip runs out early
        vout, aout = os.path.join(tmpdir, f"part_{i:03d}.mp4"), os.path.join(tmpdir, f"part_{i:03d}.wav")
        af = f"aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,apad,atrim=end_sample={ns}"

        def build(use_hw: bool) -> list[str]:
            cmd = base + (hw if use_hw else []) + ["-ss", f"{s['start']:.3f}", "-t", f"{dur + 1.0:.3f}", "-i", c["path"]]
            if not c.get("has_audio"):
                cmd += ["-f", "lavfi", "-t", f"{dur + 1.0:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            a_src = "0:a:0" if c.get("has_audio") else "1:a:0"
            return cmd + ["-map", "0:v:0", "-vf", vf, "-frames:v", str(nf)] + enc_args + ["-an", vout,
                          "-map", a_src, "-af", af, "-c:a", "pcm_s16le", "-vn", aout]

        label = f"rendering segment {i + 1}/{len(segments)} ({c['name']} {s['start']:.1f}-{s['end']:.1f}s)"
        try:
            _run_ffmpeg(build(hw_ok and bool(hw)), dur, lambda fr: seg_progress(fr, dur), tmpdir, label)
        except RuntimeError as e:
            if hw and hw_ok:
                detail = str(e).splitlines()[1][:100] if "\n" in str(e) else ""
                log("  GPU decode failed on this clip, using software decoding from here on: " + detail)
                hw_ok = False
                _run_ffmpeg(build(False), dur, lambda fr: seg_progress(fr, dur), tmpdir, label)
            else:
                raise
        vparts.append((vout, nf / FPS))
        aparts.append((aout, ns / SR))
        done += dur

    # --- join: video copied bit-for-bit, audio joined sample-exactly, music mixed in
    def write_list(path: str, parts: list[tuple[str, float]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for fn, d in parts:
                f.write("file '" + fn.replace("\\", "/").replace("'", "'\\''") + "'\n")
                f.write(f"duration {d:.6f}\n")

    vlist, alist = os.path.join(tmpdir, "video.txt"), os.path.join(tmpdir, "audio.txt")
    write_list(vlist, vparts)
    write_list(alist, aparts)
    exact_total = f_cursor / FPS
    cmd = base + ["-f", "concat", "-safe", "0", "-i", vlist, "-f", "concat", "-safe", "0", "-i", alist]
    filters = ["[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo[orig0]"]
    if music_info:
        cmd += ["-stream_loop", "-1", "-ss", f"{music_info['beats'][0] if music_info['beats'] else 0:.3f}", "-i", opts.music]
        fade = min(2.0, exact_total / 4)
        filters.append(
            "[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            + (f"adelay={int(title_dur * 1000)}|{int(title_dur * 1000)}," if title_dur > 0 else "")
            + f"atrim=0:{exact_total:.4f},asetpts=PTS-STARTPTS,afade=t=out:st={exact_total - fade:.3f}:d={fade:.3f},"
            f"volume={opts.music_volume:.3f}[music]"
        )
        if opts.keep_original_audio:
            filters.append(f"[orig0]volume={opts.original_volume:.3f}[orig];[orig][music]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            filters.append("[music]acopy[aout]")
    else:
        filters.append("[orig0]acopy[aout]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{exact_total:.4f}"] + list(opts.extra_args) + [output]
    log("  joining the pieces ...")
    _run_ffmpeg(cmd, exact_total, lambda fr: progress(0.97 + 0.03 * fr) if progress else None, tmpdir, "joining the pieces")
    for fn, _ in vparts + aparts:
        try:
            os.remove(fn)
        except Exception:
            pass
    if progress:
        progress(1.0)
    log(f"  wrote {output}")
    return output
