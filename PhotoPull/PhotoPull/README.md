# PhotoPull

Copies photos and videos off a **trusted iPhone or iPad** to a Windows PC over USB.
No iTunes backup, no iCloud, no jailbreak. It reads the camera roll (`DCIM`) directly
through Apple's own device service, the same way Windows Photos import does, but it
can resume and lets you choose how files are organised.

## What you need on the PC

1. **Apple Mobile Device Service.** Install the **Apple Devices** app (Microsoft Store) *or* iTunes.
   PhotoPull talks to the phone through that service.
2. **Python 3.12** (needed only to build or to run from source). The scripts find it on their own. If it's
   missing, they install it for your user with `winget`. Other Python versions can stay installed.
   PhotoPull uses 3.12 because two parts of the iPhone library (lzfse, pylzss) don't have ready-made Windows
   builds for 3.13 or 3.14 yet. On those versions pip tries to compile them and fails, asking for Visual C++ Build Tools.

## Get it running

- **Make an .exe:** double-click `build_exe.bat`. After a few minutes you get `dist\PhotoPull.exe`
  (one file, about 100 MB, no Python needed on other shop PCs).
- **Or run from source:** double-click `run_from_source.bat`.

## Using it

1. Plug in the device and unlock it. Click **Refresh** if it isn't listed.
2. Pick where to save (default: `Pictures\PhotoPull`).
3. Click **1. Connect & Scan**. If the device hasn't trusted this PC yet, tap **Trust** on the
   device and enter the passcode. A device already trusted by Apple Devices or iTunes on this
   PC connects without a prompt.
4. Choose what to copy, then click **2. Copy to PC**.

Files are saved under `<Save to>\<Device name>\...`.

| Option | What it does |
|---|---|
| Photos / Videos | HEIC, JPG, PNG, DNG/ProRAW, GIF… / MOV, MP4, M4V… |
| Live Photo motion clips | The short `.MOV` next to each Live Photo (same name as the photo) |
| Edit sidecars (.AAE) | The edit instructions Photos keeps. Most people don't need these |
| By year / month | `2024\2024-07\IMG_1234.HEIC` (uses the file's date on the device) |
| Keep phone folders | `100APPLE\IMG_1234.HEIC`, the same as the phone's layout |
| HEIC photos | Keep the HEIC, keep it plus a JPG copy, or convert to JPG only. EXIF and colour profile are kept, and the JPG is saved upright |

**Resume and re-runs:** a file that's already in place at the same size is skipped. Run it again
after a cable drop, a cancel or new photos, and it copies only what's missing. Files are written
as `.part` first, so a half-copied file never looks finished. File dates (modified and created)
are set to the date on the phone.

## Things to know

- **iCloud "Optimize iPhone Storage":** if this is on, many originals are only in iCloud, and the
  phone holds small previews. PhotoPull can only copy what's actually on the device. To get the
  full originals, turn on *Download and Keep Originals* first, or use iCloud for Windows.
- **Edited photos:** iOS stores edits separately. PhotoPull copies the original capture, just as
  Windows Photos import does.
- **Keep the device unlocked** while it scans. If you get "device is locked" or trust errors,
  unlock it, then unplug and replug it.
- A file that fails is listed in `PhotoPull-errors-<time>.txt` in the device folder. Click Copy again to retry.

Built on [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (device protocol) and pillow-heif (HEIC to JPG).
