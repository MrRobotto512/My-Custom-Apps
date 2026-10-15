# RepairReel

Point it at a folder of clips from one repair. It looks at every clip (what you said, what moved on screen, what the frames show), asks Claude to pick the moments and put them in order, shows you the plan so you can tweak it, and renders one finished video — a narration-led **story** for YouTube, or a beat-synced **montage** for Shorts/Reels.

Everything except the planning step runs on your own PC. The planning step sends Claude a text summary of each clip plus one 3x3 thumbnail sheet per clip (never the raw video); a typical 10–20 clip job costs a few cents. If you'd rather not use the API at all, the "signals" planner picks moments from motion and loudness alone — dumber, but free and offline.

## Setup (once) — Windows

1. Install Python 3.10+ from https://www.python.org/downloads/windows/ — tick **Add python.exe to PATH**.
2. Unzip this folder somewhere permanent, double-click **`setup.bat`**. It makes a private environment, installs the packages, and downloads ffmpeg. A few minutes.
3. Get an API key at https://console.anthropic.com (Settings → API keys), then either paste it into the app's **Settings...** or run: `run.bat set-key sk-ant-...`

`setup.bat` puts ffmpeg 7.1.1 (Gyan's build: NVENC GPU encoding, `drawtext` for captions, works with NVIDIA drivers from 551 up) into the app's `bin\` folder, and the app always prefers that copy. If you ever want a different ffmpeg, drop `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe` into `bin\` and they'll be used. Note that the very newest ffmpeg 8 builds need NVIDIA driver 610+ for GPU encoding — that's why the app pins 7.1.1.

## Setup (once) — macOS

1. Install Python 3.10+ from https://www.python.org/downloads/macos/ (the python.org installer includes Tk, which the window needs; Homebrew Python needs `brew install python-tk@3.12` as well). Homebrew itself (https://brew.sh) is recommended because `brew install ffmpeg` gives the best ffmpeg build.
2. Unzip this folder somewhere permanent and double-click **`setup.command`** (or run `./setup.sh` in Terminal). If macOS refuses to open it because it was downloaded, right-click → Open once, or run `xattr -d com.apple.quarantine *.command *.sh` in the folder.
3. API key as above: paste it into **Settings...** or run `./run.sh set-key sk-ant-...`

On Apple Silicon the transcriber runs on the GPU through Apple's MLX framework (`mlx-whisper`), and video encoding uses the hardware encoder (VideoToolbox) automatically. `./run.sh gpu-check` shows both. Intel Macs run the transcriber on the CPU. Settings live in `~/Library/Application Support/RepairReel/`.

## Using it

Double-click **`run.bat`** (Windows) or **`RepairReel.command`** (macOS).

1. **Clips folder** — Browse to the folder with the clips from one repair (phone videos, OBS captures, whatever; MP4/MOV/MKV...). They're ordered by recording time from the file metadata, falling back to file name.
2. Pick **Story** or **Montage**, a target length, and 16:9 or 9:16. For a montage, choose a music track — cuts will land on its beats. For a story, music is optional and plays quietly under your voice.
3. **1. Analyze clips** — transcribes your narration (locally, with Whisper), measures motion and loudness, finds camera moves, and builds a contact sheet per clip. Cached in a `.repairreel` folder next to the clips, so re-planning is instant. First run downloads the Whisper model (~500 MB for `small.en`).
4. **2. Plan the edit** — Claude returns a title and an ordered list of segments with a one-line reason for each. Use **Notes for the editor** to steer it: "the payoff is in the last clip", "skip the part where I drop the screw", "more of the microscope shots".
5. Look over the plan. Double-click a row to change its in/out points or caption; remove or reorder rows; **Preview segment** plays just that piece. Or hit **2. Plan** again with different notes.
6. **3. Render video** — pick a filename; it renders with a title card, captions on the key steps, and (montage) the music mixed in with the original audio ducked.

**Do all three** runs the whole chain with the current settings.

### Tips for footage that edits well

- Say what you're doing while you do it, even briefly ("okay, port's off"). The planner leans on your narration to understand the story; silent clips are chosen by what's visible.
- One clip per step beats one 40-minute clip: the contact sheet is 9 frames per clip, so a very long clip is seen coarsely. If you have a long recording, cut it in two first, or raise the target length.
- For Shorts, the app crops the landscape frame to 9:16 around where Claude thinks the action is (you can adjust the focus point per segment). Shooting with the work centered helps.
- Repair jargon the transcriber should spell correctly lives in Settings ("PS5, HDMI, retimer, reflow, ...") — add your own.

## Command line

`run.bat` (Windows) or `./run.sh` (macOS) with arguments runs the CLI instead of the window:

```
run.bat analyze  "D:\Captures\ps5 hdmi"
run.bat plan     "D:\Captures\ps5 hdmi" --style story --length 240 --notes "payoff is clip 7"
run.bat render   "D:\Captures\ps5 hdmi" -o "D:\Captures\ps5 hdmi\final.mp4"
run.bat auto     "D:\Captures\ps5 hdmi" --style montage --aspect 9:16 --length 45 --music "C:\Music\track.mp3"
run.bat plan     "D:\Captures\ps5 hdmi" --planner signals      (no API key needed)
```

The plan is a plain JSON file (`.repairreel\plan.json`); edit it in any text editor and render again.

## Settings worth knowing

- **Model**: `auto` picks the newest Sonnet on your account each time; set an explicit model id if you want to pin one.
- **Whisper model**: `small.en` is a good default on CPU; `medium.en` is noticeably better on mumbled narration but slower; `none` skips transcription (fine for montages). Runs on the NVIDIA GPU (setup installs the CUDA libraries it needs).
- **Video encoder**: `auto` uses the hardware encoder when it works — NVIDIA NVENC on Windows, VideoToolbox on a Mac — checked with a test encode each run, falling back to CPU `libx264` otherwise and telling you why in the log. `gpu-check` shows the state of both the transcriber and the encoder.
- **Levels**: montage music 0.9 with the original audio at 0.25 underneath; story music bed 0.12. Adjust in Settings.

## What runs where

Analysis decodes each clip once (GPU decode via NVDEC / VideoToolbox when available), computes motion and scene changes from that single pass, and transcribes on the GPU. Rendering works one segment at a time — GPU decode → scale/crop/caption → GPU encode — into small intermediate files, then joins them without re-encoding the video, so memory use stays flat however many segments there are and the cuts stay frame-exact. What is still CPU work: scaling and cropping frames, drawing captions, the audio mixing, and software decoding of any clip whose codec the GPU can't handle (ffmpeg falls back automatically and the log says so). `gpu-check` reports the decoder, transcriber and encoder in use.

## How it works (for tinkering)

```
repairreel/media.py     ffmpeg/ffprobe helpers: probe, decode audio/frames, scene detection, contact sheets
repairreel/analyze.py   per-clip analysis + cache, Whisper transcription, LLM-friendly clip summaries
repairreel/beats.py     tempo + beat grid from a music file (spectral flux + low-band onsets)
repairreel/planner.py   the Claude prompt and tool schema, the signals-only planner, plan validation
repairreel/render.py    builds one ffmpeg filter graph: trims, scaling/cropping, captions, title, concat, music
repairreel/gui.py       the window;  repairreel/cli.py  the command line
```

The prompt is in `planner.py` (`SYSTEM_PROMPT`, `STORY_RULES`, `MONTAGE_RULES`) — if you want it to favour different things, that's the place. The renderer writes the exact ffmpeg command it ran to a temp folder (path shown in the log on failure).
