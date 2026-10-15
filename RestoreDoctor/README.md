# Restore Doctor

A Windows bench app that restores iPhones and explains why a restore fails.
It runs the restore itself with the open-source engine **idevicerestore**,
records the exact step the phone dies on, and turns that into ranked suspect
parts with the log lines behind each one and a short list of next bench checks.

Apple's error numbers (9, 4005, 4013, 4014...) only say *that* the phone
disconnected. The step it stopped on says *where*: storage, baseband, Face ID,
battery management, the boot chain, or the PC itself.

Version 0.1 - built and tested in simulation and against the real engine with
no phone attached. Not yet run against a real failing phone.

## Quick start (Windows)

1. Install the **Apple Devices** app (Microsoft Store) or iTunes. Restore Doctor
   reaches phones through Apple's USB driver and Apple Mobile Device Service.
2. Get the engine. Three ways, same result:
   - `tools/cross_build_engine.sh` on Linux (mingw-w64 cross compiler; Windows
     dependencies come prebuilt from MSYS2). This is how `app/engine` was built.
   - `tools/build_engine.sh` in the **MSYS2 MINGW64** shell on Windows.
   - The **Build Windows** GitHub Action (engine and exe together).
3. Run `app\RestoreDoctor.exe` (or `python RestoreDoctor.pyw` from source,
   Python 3.11+). The top bar shows the ready check; click any red chip for the fix.
   Close 3uTools, iTunes or Apple Devices windows that are talking to the phone
   before a restore: two tools driving one phone can break the restore.

To try it with no phone: `RestoreDoctor.exe --sim` (or set `RD_SIM=1`), then
pick a failure from the **Simulation** menu and press **Start restore**.

## How a run works

1. **Ready check** - engine present, Apple Mobile Device Service on port 27015,
   Apple driver installed, Apple's signing server reachable, hosts file clean,
   20 GB free.
2. **Phone** - mode, model (from chip and board IDs), ECID, serial. The DFU
   helper shows the button sequence for the model and confirms when the phone
   lands in DFU.
3. **Firmware** - only builds Apple still signs for this model (api.ipsw.me),
   plus IPSWs in your folder. Downloads resume and are checksum-verified.
4. **Restore** - Update (keeps data) by default; Erase asks twice. The engine
   runs as `idevicerestore -y -d -P --logfile=-`; every line is parsed live into
   seven stages, the named restore step, progress, and signals.
5. **USB watch** - Windows' device list is polled separately, so "the phone
   crashed" and "the PC lost the phone" can be told apart. After a failure the
   app keeps watching for 20 s to see where the phone goes.
6. **Verdict** - setup and firmware problems are reported as such first; then
   the stop point, signatures, device log, checkpoints and any panic the phone
   hands over decide the suspects. A second run that stops at the same point
   raises confidence; a moving stop points at the connection.
7. **Case** - every attempt's raw log, the verdict and an HTML report are kept
   per case. Record the fix that worked; later verdicts for the same model and
   stop point show it.

## Where things live

| Path | What |
| --- | --- |
| `restore_doctor/logparse.py` | Engine output to stages, steps, statuses, signatures |
| `restore_doctor/diagnose.py` | The rules engine |
| `restore_doctor/kb/*.json` | Knowledge base: restore operations, parts and their checks, log signatures, Apple error codes, bench readings, device table |
| `restore_doctor/engine.py` | Ready checks, device probe, the engine process |
| `restore_doctor/usbwatch.py` | USB mode watch (PowerShell on Windows) |
| `restore_doctor/session.py` | Ties a phone, its attempts and the verdict together |
| `restore_doctor/ui/` | Tk interface |
| `restore_doctor/scenarios/` | Synthetic failure logs for simulation and tests |
| `tools/build_engine.sh` | Builds the engine from source (MSYS2 or Linux) |
| `tools/build_app.ps1` | Builds RestoreDoctor.exe and the zip |
| `tools/gen_models.py` | Regenerates the device table from libirecovery |
| `tools/make_scenarios.py` | Regenerates the simulation scenarios |
| `tests/` | Rule regression tests and a headless UI walk-through |

User data goes to `%LOCALAPPDATA%\RestoreDoctor` (cases, IPSW cache, settings).
Override with `RD_DATA_DIR`; point at a different engine with `RD_ENGINE_DIR`.

## Improving the rules

- Add or tune a signature in `kb/signatures.json` (regex, kind, weighted parts).
- Map a restore step to a part in `kb/operations.json`.
- Change the bench steps shown for a part in `kb/components.json`.
- Save real failure logs as scenarios and add the expected verdict to
  `tests/test_rules.py`, so a later rule change can't silently break them.
- Old cases are re-diagnosed from their raw logs, so improved rules apply to
  history too.

## Tests

```
python -m pytest -q tests
xvfb-run python tests/ui_smoke.py write_drop write_drop_repeat --shots shots/   # Linux, headless
```

## Known limits

- Every restore needs Apple's online signature: no offline restores.
- Update mode can't downgrade iOS 17+ and fails on a phone with no data volume.
- Suspects are leads ranked from the log, not a final diagnosis.
- The scenario logs are synthetic (real message text, invented runs) until
  real captures replace them.
