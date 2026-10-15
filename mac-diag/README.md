# Mac Diagnostics

USB Drive Writer profile (`group: Mac`). The USB Drive Writer formats a stick
to **exFAT** (readable by every Mac since 10.6.5) and copies everything in this
folder onto it. Take the stick to the customer's Mac, open it in Finder and
double-click **`Start Dashboard.command`** — Terminal opens, the live dashboard
appears in the Mac's browser, and every other tool runs from its Tools page.

```
MACDIAG/
  Start Dashboard.command   <- the one thing a tech double-clicks
  README.md
  Tools/
    Dashboard/              the dashboard itself (Run-Dashboard.command, collector, api, www/);
                            QC-Reports/ = saved hardware-test checklists (not committed)
    MacDiag/  MalwareTriage/  AppleDiagnostics/  InstallerMaker/  CleanUp/  Files/   (each has a Run-*.command)
    _shared/                library sourced by every tool
    _bin/                   smartctl (smartmontools 7.4, GPL-2, universal) + COPYING
    Set-GeminiKey.command   writes a key into every tool folder by hand
```

The Mac twin of `windows-diag`: same layout (one app per subfolder, `ABOUT.txt`
in each, `gemini.key` injected by BenchTool), same report style
(`<App>-Reports/<App>_<Mac>_<time>.txt` + `.json`), same AI persona pattern.
Everything is plain **bash 3.2** (the `/bin/bash` every macOS ships) — no
Python, no Homebrew, no third-party binaries. JSON work runs through
`osascript -l JavaScript` (`_shared/*.js`).

## Apps

| App | Folder | What it does |
| --- | --- | --- |
| **Dashboard** | `Dashboard/` | Live bench dashboard in the Mac's browser (127.0.0.1:8765, Apple's own httpd or a bash+nc fallback): LED meters + graphs for CPU/GPU/memory/network/disk/battery/thermals, trade-in check, processes, system info, startup items, users, services, disks, one-click **Run** for the tools below with live log + last verdict, report viewer. `Run-Dashboard.command` |
| MacDiag | `MacDiag/` | Read-only health check: identity, **trade-in blockers** (Activation Lock, Find My, MDM/DEP, iCloud), memory pressure, throttling + fan/temperature, storage + real SMART counters via the bundled smartctl (wear %, spare blocks, media errors, TB written), battery health, kernel panics **classified by type + third-party kexts named**, shutdown-cause decode, startup items, security posture, network hijacks, backups. Scored findings → Gemini (ACMT persona) verdict + next bench tests + parts + customer note. `Run-MacDiag.command` |
| Malware Triage | `MalwareTriage/` | "Is this Mac infected / hijacked?" Read-only sweep of persistence (launchd, login/background items, cron, hooks, plug-ins, extensions), browsers (extensions + policy hijacks), network (hosts/DNS/proxy/root certs), processes (codesign + location + live connections), users, PUPs, recent drops. Gemini (removal-tech persona) verdict + removal plan. `-Collect` adds a sysdiagnose. `Run-MalwareTriage.command` |
| Clean Up | `CleanUp/` | Junk + purgeable-space cleaner behind the dashboard's **Clean** page (`cleanup.sh` engine + `Run-CleanUp.command` Terminal front end): scan → sizes + paths per category → tick → **move to `~/.Trash/BenchTool-Cleanup-<time>/`** (recoverable) → report with before/after free space; Time Machine snapshots, large old files, maintenance ops. Customer-data categories (Trash, iPhone backups, Downloads, leftovers) are never pre-ticked; emptying the Trash is a separate double-confirmed step. |
| Files | `Files/` | Space-Lens-style disk map behind the dashboard's **Files** page (`files.sh` engine + `Run-Files.command` "what fills the disk" report): index once (every folder size + every file ≥ 1 MB), browse by breadcrumb / list / bubble map, Customer-data chips, Find by name/kind/size. On the selection: **Back up…** to a mounted volume (rsync keeping paths, per-file size verify, optional SHA-256, manifest, BACKUP VERIFIED / NOT VERIFIED report), **Move to Trash** (`~/.Trash/BenchTool-Files-<time>/`), **Delete permanently** (double confirm; macOS / all-users / Photos-library / Trash roots refused). Everything runs through the collector's job queue with the dashboard's admin rights. |
| Apple Diagnostics / AHT | `AppleDiagnostics/` | Prints the right key sequence for THIS Mac (Intel D / Option-D, Apple silicon Cmd-D; on macOS 26+ Cmd-D opens **Repair Assistant** — Mac Resource Inspector / Display Anomalies / Keyboard-Trackpad-Touch ID / Audio, and the Parts & Service calibration + Genuine/Used/Unknown/Unverified labels are explained), decodes reference codes (`adp_codes.tsv`), and for 2006–2013 Macs finds the AHT image by board-id (`aht_codes.tsv`, 78 boards), downloads it (Apple → GitHub mirror), and builds a bootable AHT USB. `Run-AppleDiagnostics.command` |
| Installer Maker | `InstallerMaker/` | MultiMac-style: one or several macOS installers on one USB (GPT partition per version, `createinstallmedia`), full-installer download via `softwareupdate`, and installer → `.iso` for VMs. Runs on any Mac. `Run-InstallerMaker.command` |

`Tools/_shared/` holds the library the apps source (`benchmac.sh`: logging, findings,
sudo keep-alive, watchdog, Gemini ladder, report writers) and the three JXA
helpers. `Set-GeminiKey.command` writes a key into every app folder by hand.
`Tools/_bin/` carries the one third-party binary on the stick: **smartctl**
(smartmontools 7.4, GPL-2, unmodified universal build from
CodingMarkus/smartmontools_macos_release, licence in `COPYING`). exFAT has no
exec bit, so `bm_smartctl` copies it to the run's temp folder, `chmod +x`,
clears xattrs and ad-hoc signs it if macOS refuses; it only runs with the admin
password. Never link against it - the scripts call it as a separate process.

## Setting the Gemini API key

MacDiag and Malware Triage read `gemini.key` from their own folder (fallback:
`Tools/gemini.key`). Two things put it there: **BenchTool Settings → Import API
Key** writes it into every `Tools/*` folder on the Pi (`tools/keyimport.py`,
same as windows-diag), and **USB Drive Writer injects the bench's current key at
write time** (`benchtool-usbwriter` copies `gemini.key` next to every
`Run-*.command` on the stick + `Tools/gemini.key`), so a stick written after the
key was imported always carries it. No key = scan, verdict and report still work, only
the AI section is skipped. Model ladder: `gemini-3.5-flash` → 2.5 → 2.0
(`GEMINI_MODEL` env overrides).

## Running on a customer Mac — gotchas

- **Quarantine / Gatekeeper**: files the Pi writes have no quarantine flag, so
  `.command` files open normally. If one was copied through a browser first,
  right-click → Open once.
- **Removable-volume prompt** (Sonoma+): Terminal asks to access files on the
  stick — Allow.
- **Full Disk Access**: without it Safari/Mail/quarantine-DB reads are skipped;
  the apps say so and carry on.
- **Admin password**: asked once per run (`sudo -v` + keep-alive). Return to
  skip; coverage drops (fan/temp, `sfltool`, `profiles -all`, `wdutil`).
- exFAT has no permission bits — every file is executable on macOS, which is
  exactly why `.command` double-click works from the stick. Keep it exFAT.

## Adding an app

1. New subfolder under `Tools/`, `Run-<Name>.command` with `#!/bin/bash`, source
   `../_shared/benchmac.sh`, call `bm_init "<Name>" "1.0"`, raise findings with
   `bm_finding HIGH|MED|INFO "Category" "Title" "Detail"`, finish with
   `bm_write_reports "$META" "$AI"`.
2. `ABOUT.txt` (what / how to run / how to read it).
3. If it uses Gemini, read the key via `bm_gemini` — keyimport picks the folder
   up automatically.
4. `chmod +x` the `.command` in git (irrelevant on the stick, nice in the repo),
   re-sync to the Pi, re-write the stick from USB Drive Writer → Mac → Mac
   Diagnostics.

`Start Dashboard.command` is the front door: everything live at a glance, the
other four tools launch from its Tools page (each in its own Terminal window),
their live log and last verdict show on that page, and Reports lists every report
on the stick. Apple's httpd is sandboxed away from removable volumes, so the
dashboard's collector mirrors tool logs + reports into the run folder for it.

> Keep the whole profile under the target stick's capacity. Cached AHT images
> (`AppleDiagnostics/AHT-Images/`), installers (`InstallerMaker/Installers/`),
> ISOs and reports are git-ignored and stay on the stick / the Pi.

## Setup mode (a Mac still at Setup Assistant)

Trade-in Macs arrive erased — no user, no admin password, and Terminal at
Setup Assistant is not allowed to read a USB stick. The BenchTool keystroke
dongle handles that: one tap at the Country screen opens Terminal, joins the
Mac to the shop Wi-Fi, pulls this profile from the bench into `/tmp` and runs
`Start Dashboard.command -NoSudo -Setup -Bench http://<bench>:8377/x/<app>`.
The Dashboard then opens on the **Intake** page — one GO / CHECK / NO-GO
verdict from Activation Lock, MDM/DEP, Find My, battery health, cycles and the
permanent-failure flag, disk SMART and charger wattage — with the hardware
**Tests** page and the QC checklist next to it. **Send to bench** (and saving
the checklist) posts the results to the bench, where the Keystroke Flasher
page lists them and records a bench session. Pages that need an admin
password or a real user account are hidden in setup mode; `-Setup` is also
auto-detected when the dashboard runs as `_mbsetupuser`.
