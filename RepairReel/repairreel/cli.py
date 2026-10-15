"""Command-line interface:  python -m repairreel <command> [options]"""
from __future__ import annotations

import argparse
import os
import sys

from . import analyze, config, planner, render


def _log(msg: str) -> None:
    print(msg, flush=True)


def _progress_bar(frac: float) -> None:
    n = int(frac * 30)
    print(f"\r  [{'#' * n}{'.' * (30 - n)}] {frac * 100:3.0f}%", end="", flush=True)
    if frac >= 1.0:
        print()


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser(prog="repairreel", description="Turn a folder of repair clips into a highlight video.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="Analyze every clip in a folder (motion, audio, transcript, frames).")
    a.add_argument("folder")
    a.add_argument("--whisper", default=cfg["whisper_model"], help="faster-whisper model: tiny.en, base.en, small.en, medium.en, large-v3, none")
    a.add_argument("--force", action="store_true", help="Ignore the cache and redo everything")

    p = sub.add_parser("plan", help="Ask Claude (or plain signals) for an edit plan; writes .repairreel/plan.json")
    p.add_argument("folder")
    p.add_argument("--style", choices=["story", "montage"], default=cfg["style"])
    p.add_argument("--length", type=int, default=cfg["target_length"], help="Target length in seconds")
    p.add_argument("--aspect", choices=["16:9", "9:16"], default=cfg["aspect"])
    p.add_argument("--planner", choices=["claude", "signals"], default=cfg["planner"])
    p.add_argument("--model", default=cfg["model"])
    p.add_argument("--notes", default="", help="Anything you want the editor to know ('the payoff is in clip 7')")

    r = sub.add_parser("render", help="Render .repairreel/plan.json to a video")
    r.add_argument("folder")
    r.add_argument("-o", "--output", default="")
    r.add_argument("--music", default="")
    r.add_argument("--aspect", choices=["16:9", "9:16"], default="")
    r.add_argument("--no-captions", action="store_true")
    r.add_argument("--no-title", action="store_true")
    r.add_argument("--encoder", default=cfg["encoder"])
    r.add_argument("--crf", type=int, default=cfg["crf"])
    r.add_argument("--height", type=int, default=cfg["height"])

    auto = sub.add_parser("auto", help="analyze + plan + render in one go")
    for parser in (auto,):
        parser.add_argument("folder")
        parser.add_argument("--style", choices=["story", "montage"], default=cfg["style"])
        parser.add_argument("--length", type=int, default=cfg["target_length"])
        parser.add_argument("--aspect", choices=["16:9", "9:16"], default=cfg["aspect"])
        parser.add_argument("--planner", choices=["claude", "signals"], default=cfg["planner"])
        parser.add_argument("--model", default=cfg["model"])
        parser.add_argument("--notes", default="")
        parser.add_argument("--whisper", default=cfg["whisper_model"])
        parser.add_argument("-o", "--output", default="")
        parser.add_argument("--music", default="")
        parser.add_argument("--no-captions", action="store_true")
        parser.add_argument("--no-title", action="store_true")
        parser.add_argument("--encoder", default=cfg["encoder"])
        parser.add_argument("--crf", type=int, default=cfg["crf"])
        parser.add_argument("--height", type=int, default=cfg["height"])

    k = sub.add_parser("set-key", help="Store your Anthropic API key")
    k.add_argument("key")
    sub.add_parser("gpu-check", help="Check that Whisper and the video encoder can use the GPU")

    args = ap.parse_args(argv)

    if args.cmd == "gpu-check":
        return gpu_check()

    if args.cmd == "set-key":
        cfg["api_key"] = args.key.strip()
        config.save(cfg)
        print("saved to", config.config_path())
        return 0

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print("Not a folder:", folder)
        return 2

    if args.cmd in ("analyze", "auto"):
        _log(f"Analyzing clips in {folder}")
        data = analyze.analyze_folder(folder, _log, whisper_model=args.whisper, jargon=cfg["jargon"],
                                      force=getattr(args, "force", False))
        _log(f"{len(data['clips'])} clips analyzed.")
        if args.cmd == "analyze":
            return 0
    else:
        data = analyze.load_cache(folder)
        if not data.get("clips"):
            print("No analysis yet - run:  python -m repairreel analyze", folder)
            return 2

    if args.cmd in ("plan", "auto"):
        _log(f"Planning ({args.planner}) ...")
        if args.planner == "claude":
            plan = planner.plan_with_claude(data, args.style, args.length, args.aspect, args.notes, cfg["api_key"], args.model, _log)
        else:
            plan = planner.plan_with_signals(data, args.style, args.length, args.aspect, _log)
        path = planner.save_plan(plan)
        _log(f"Title: {plan['title']}\n{plan['summary']}")
        for i, s in enumerate(plan["segments"]):
            _log(f"  {i + 1:2d}. clip {s['clip']} {s['file']}  {s['start']:.1f}-{s['end']:.1f}s  ({s['end'] - s['start']:.1f}s)"
                 + (f"  [{s['caption']}]" if s['caption'] else "") + (f"  - {s['why']}" if s['why'] else ""))
        _log(f"Plan saved: {path}  (edit it and run 'render', or re-plan with --notes)")
        if args.cmd == "plan":
            return 0
    else:
        plan = planner.load_plan(folder)
        if not plan:
            print("No plan yet - run:  python -m repairreel plan", folder)
            return 2

    # render
    opts = render.RenderOptions()
    opts.aspect = args.aspect or plan.get("aspect", "16:9")
    opts.music = args.music or (cfg["music"] if plan.get("style") == "montage" else "")
    if plan.get("style") == "montage":
        opts.music_volume = cfg["music_volume_montage"]
        opts.original_volume = cfg["original_volume_montage"]
    else:
        opts.music_volume = cfg["music_volume_story"]
        opts.original_volume = 1.0
    opts.keep_original_audio = cfg["keep_original_audio"]
    opts.captions = not args.no_captions and cfg["captions"]
    opts.title_card = not args.no_title and cfg["title_card"]
    opts.encoder, opts.crf, opts.height = args.encoder, args.crf, args.height
    if plan.get("style") == "montage" and not opts.music:
        _log("  note: montage without a music file - add --music to cut on the beat")
    out = args.output or os.path.join(folder, _safe_name(plan.get("title") or os.path.basename(folder)) + (
        "_short" if opts.aspect == "9:16" else "_highlight") + ".mp4")
    _log("Rendering ...")
    render.render(plan, data, out, opts, _log, _progress_bar)
    _log(f"Done: {out}")
    return 0


def gpu_check() -> int:
    import platform
    import time

    ok = True
    ff, fp = __import__("repairreel.media", fromlist=["x"]).find_tools()
    print("ffmpeg in use:", ff, f"(version {render.ffmpeg_major_version()}.x)")
    from . import media as _m

    print("Video decoding:", _m.hwaccel_name())
    backend = analyze.whisper_backend()
    if sys.platform == "darwin":
        print("Platform: macOS", platform.machine())
        if platform.machine() == "arm64" and backend != "mlx":
            print("Whisper: mlx-whisper is NOT installed - transcription would run on the CPU. Run setup.sh.")
            ok = False
        else:
            print("Whisper backend:", "MLX (Apple GPU)" if backend == "mlx" else backend)
        try:
            t0 = time.time()
            import numpy as _np

            analyze.transcribe(_np.zeros(16000, dtype=_np.float32), "tiny.en", "", _log)
            print(f"Whisper test: OK ({time.time() - t0:.1f}s incl. model download)")
        except Exception as e:
            print("Whisper test: FAILED\n", e)
            ok = False
    else:
        dirs = analyze.register_cuda_dlls()
        print("NVIDIA DLL folders registered:", len(dirs))
        for d in dirs:
            print("   ", d)
        try:
            import ctranslate2  # type: ignore

            n = ctranslate2.get_cuda_device_count()
            print(f"CUDA devices seen by ctranslate2: {n}")
            if n == 0:
                ok = False
        except Exception as e:
            print("ctranslate2 not importable:", e)
            ok = False
        try:
            t0 = time.time()
            m = analyze._get_whisper("tiny.en", _log)
            dev = getattr(getattr(m, "model", None), "device", "?")
            print(f"Whisper loaded on: {dev} ({time.time() - t0:.1f}s incl. model download)")
            if dev != "cuda":
                ok = False
        except Exception as e:
            print("Whisper on GPU: FAILED\n", e)
            ok = False
    enc = render.available_encoders()
    hw = sorted(x for x in enc if any(k in x for k in ("nvenc", "amf", "qsv", "videotoolbox")))
    print("ffmpeg hardware encoders present:", ", ".join(hw) or "none")
    g = render.gpu_encoder()
    print("Hardware encode test:", f"OK ({g})" if g else "FAILED - " + render.gpu_reason())
    if not g:
        ok = False
    print("\nRESULT:", "GPU ready" if ok else "GPU NOT ready - see above")
    return 0 if ok else 1


def _safe_name(s: str) -> str:
    import re

    s = re.sub(r"[^\w\- ]+", "", s).strip().replace(" ", "_")
    return s[:60] or "highlight"


if __name__ == "__main__":
    sys.exit(main())
