"""Generate the simulation scenarios in restore_doctor/scenarios/.

These are SYNTHETIC logs written in the engine's real output format
(idevicerestore with --logfile=- -d -P): every message text below is copied
from the engine's source so the parser and rules see what they will see on
the bench. They exist so the app can be exercised without a phone, and as
regression tests for the rules. Replace or extend them with real captured
logs as the shop collects them (File > Import log saves a log as a case).

Run:  python tools/make_scenarios.py
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "restore_doctor" / "scenarios"
VERSION = "idevicerestore 1.0.0-278-g4b3e847 (libirecovery 1.3.1-12-g93c117c, libtatsu 1.0.5-6-ge7d6ad1)"


class Log:
    def __init__(self):
        self.t = 0.0
        self.lines = []      # (t, stream, text)
        self.usb = []        # (t, mode)

    def wait(self, sec):
        self.t += sec
        return self

    def i(self, text, dt=0.05, level="Info"):
        self.t += dt
        self.lines.append((self.t, "err", f"{level}|{text}"))
        return self

    def e(self, text, dt=0.05):
        return self.i(text, dt, "Error")

    def d(self, text, dt=0.05):
        return self.i(text, dt, "Debug")

    def v(self, text, dt=0.05):
        return self.i(text, dt, "Verbose")

    def raw(self, text, dt=0.01):
        """A line printed without a timestamp (continuation, libtatsu)."""
        self.t += dt
        self.lines.append((self.t, "raw", text))
        return self

    def progress(self, step, frac, dt=0.01):
        self.t += dt
        self.lines.append((self.t, "out", f"progress: {step} {frac:.6f}"))
        return self

    def usb_mode(self, mode, dt=0.0):
        self.t += dt
        self.usb.append((round(self.t, 3), mode))
        return self

    def render(self):
        out = []
        for t, stream, text in self.lines:
            if stream == "err":
                level, msg = text.split("|", 1)
                ms = int(round(t * 1000)) + 10 * 3600 * 1000
                hh, rem = divmod(ms, 3600 * 1000)
                mm, rem = divmod(rem, 60 * 1000)
                ss, mss = divmod(rem, 1000)
                label = {"Error": "  <Error>", "Info": "   <Info>", "Debug": "  <Debug>",
                         "Verbose": "<Verbose>", "Warning": "<Warning>"}[level]
                out.append(f"{hh:02d}:{mm:02d}:{ss:02d}.{mss:03d} {label} {msg}")
            else:
                out.append(text)
        return "\n".join(out) + "\n"


ECID_DEC = "7298405312345678"
ECID_HEX = "0019ed4b21a3c94e"


def preamble(log, mode, board, product, erase=False, ios_from=None):
    log.usb_mode({"Recovery": "Recovery", "DFU": "DFU", "Normal": "Normal"}[mode])
    log.i(VERSION).progress(0, 0.0)
    log.i(f"Found device in {mode} mode", 0.4)
    log.i(f"ECID: {ECID_DEC}")
    log.i(f"Identified device as {board}, {product}")
    if ios_from:
        log.i(f"Device Product Version: {ios_from}")
    log.i("Extracting BuildManifest from IPSW", 0.1)
    log.i("IPSW Product Version: 26.0.1", 0.6)
    log.i("IPSW Product Build: 23A355 Major: 23")
    log.i("Device supports Image4: true")
    if erase:
        log.i("Variant: Customer Erase Install (IPSW)")
        log.i("This restore will erase all device data.")
    else:
        log.i("Variant: Customer Upgrade Install (IPSW)")
        log.i("This restore will update the device without erasing user data.")
    log.i("Checking IPSW for required components...")
    log.i("All required components found in IPSW", 0.3)
    log.i("Extracting filesystem from IPSW: 090-31452-117.dmg.aea", 0.1)
    log.progress(1, 0.1, 6.0)
    return log


def signing(log, mode):
    log.i(f"Getting ApNonce in {mode.lower()} mode... ")
    log.i("Trying to fetch new SHSH blob", 0.3)
    log.i(f"Getting SepNonce in {mode.lower()} mode... ")
    log.i("Received SHSH blobs", 1.4)
    log.progress(1, 0.25)
    return log


def boot_chain_from_dfu(log):
    log.i("Sending iBSS (1048576 bytes)...", 0.3)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.2).usb_mode("Gone")
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) connected in Recovery mode", 2.5).usb_mode("Recovery")
    return boot_chain_from_recovery(log)


def boot_chain_from_recovery(log):
    log.progress(1, 0.3)
    log.i("Recovery Mode Environment:")
    log.i("iBoot build-version=iBoot-11881.0.39")
    log.i("iBoot build-style=RELEASE")
    log.i("Sending iBEC (1245184 bytes)...", 0.2)
    log.d("Waiting for device to disconnect...", 1.1)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 0.6).usb_mode("Gone")
    log.d("Waiting for device to reconnect in recovery mode...")
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) connected in Recovery mode", 3.4).usb_mode("Recovery")
    log.progress(1, 0.5).progress(1, 0.7)
    log.i("Sending RestoreRamDisk (118489088 bytes)...", 0.4)
    log.i("Sending RestoreDeviceTree (131072 bytes)...", 4.8)
    log.i("Sending RestoreKernelCache (19431424 bytes)...", 0.3)
    log.progress(1, 0.9, 1.2)
    log.i("Waiting for device to enter restore mode...")
    return log


def restore_os_up(log):
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.0).usb_mode("Gone")
    log.d(f"idevice_event_cb: device {ECID_HEX} (udid: 00008030-001A2B3C4D5E6F70) connected in Restore mode",
          18.0).usb_mode("Restore")
    log.i("Connecting now...", 0.1)
    log.i("Connected to com.apple.mobile.restored, version 15", 0.1)
    log.i("Device 00008030-001A2B3C4D5E6F70 has successfully entered restore mode")
    log.i("Hardware Information:")
    log.i("BoardID: 4")
    log.i("ChipID: 32816")
    log.i(f"UniqueChipID: {ECID_DEC}")
    log.i("ProductionMode: true")
    log.i("About to restore device... ", 0.4)
    return log


def storage_ok(log, verify=True):
    log.i("Waiting for NAND (28)", 0.6)
    log.i("Creating partition map (11)", 2.1)
    log.i("About to send RootTicket...", 0.2)
    log.i("Sending RootTicket now...")
    log.i("Done sending RootTicket")
    log.i("Creating filesystem (12)", 1.0)
    log.i("About to send filesystem...", 0.4)
    log.i("Connected to ASR")
    log.i("Validating the filesystem")
    log.i("Filesystem validated", 2.0)
    log.i("Sending filesystem now...")
    log.i("Restoring image (13)", 0.2)
    for k in range(1, 11):
        log.progress(2, k / 10, 6.0)
    log.i("Done sending filesystem", 0.3)
    if verify:
        log.i("Verifying restore (14)", 0.2)
        for k in range(1, 5):
            log.progress(3, k / 4, 3.0)
    log.i("Checking filesystems (15)", 0.5)
    log.i("Mounting filesystems (16)", 0.5)
    log.i("Installing kernelcache (27)", 0.4)
    log.i("Installing DeviceTree (61)", 0.2)
    return log


def firmware_ok(log, baseband=True, se=True, savage=False):
    log.i("About to send NORData...", 0.4)
    log.i("Found firmware path Firmware/all_flash")
    log.i("Getting firmware manifest from build identity")
    log.i("Sending NORData now...", 0.8)
    log.i("Done sending NORData", 0.4)
    log.i("Flashing firmware (18)", 0.2)
    log.progress(4, 1.0, 4.0)
    if se:
        log.i("Sending SE TSS request...", 0.4)
        log.i("Received SE2,Ticket", 0.9)
        log.i("Updating SE Firmware (59)", 0.3)
    if savage:
        log.i("Sending Savage TSS request...", 0.4)
        log.i("Received Savage ticket", 0.8)
        log.i("Updating Savage (60)", 0.3)
    if baseband:
        log.i("Preparing for baseband update (48)", 1.0)
        log.i("About to send BasebandData...", 0.2)
        log.i("Sending Baseband TSS request...")
        log.i("Received Baseband SHSH blobs", 1.0)
        log.i("Sending BasebandData now...", 0.5)
        log.i("Done sending BasebandData", 2.0)
        log.i("Booting the baseband (49)", 0.3)
        log.i("Updating baseband (19)", 4.0)
        log.i("Updating Baseband in progress...", 2.0)
        log.progress(5, 1.0, 20.0)
        log.i("Updating Baseband completed.", 0.2)
    return log


def finish_ok(log):
    log.i("Sealing System Volume (77)", 0.4)
    log.i("Got status message", 3.0)
    log.i("Status: Restore Finished")
    log.i("Rebooting device (21)", 0.3)
    log.i("DONE", 0.3)
    log.progress(7, 1.0)
    log.usb_mode("Gone", 1.0).usb_mode("Normal", 25.0)
    return log


def failed(log, dt=0.3):
    log.e("Unable to successfully restore device", dt)
    log.e("Unable to restore device", 0.1)
    log.i("RESTORE FAILED", 0.1)
    return log


SCENARIOS = []


def scenario(id_, title, device, product, board, exit_code, notes, attempt=1):
    def deco(fn):
        SCENARIOS.append({"id": id_, "title": title, "device": device, "product": product, "board": board,
                          "exit_code": exit_code, "notes": notes, "fn": fn, "attempt": attempt})
        return fn
    return deco


@scenario("success_update", "iPhone 13 update completes", "iPhone 13", "iPhone14,5", "d17ap", 0,
          "A clean Update restore from Recovery mode.")
def s_success():
    log = preamble(Log(), "Recovery", "d17ap", "iPhone14,5")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    restore_os_up(log)
    storage_ok(log)
    firmware_ok(log)
    return finish_ok(log)


@scenario("nand_disk_failure", "iPhone 11 storage fails (status 6)", "iPhone 11", "iPhone12,1", "n104ap", 1,
          "The restore OS reports a disk failure while creating the partition map.")
def s_nand():
    log = preamble(Log(), "DFU", "n104ap", "iPhone12,1", erase=True)
    signing(log, "DFU")
    boot_chain_from_dfu(log)
    restore_os_up(log)
    log.i("Waiting for NAND (28)", 0.6)
    log.i("Creating partition map (11)", 14.0)
    log.i("Got status message", 9.0)
    log.i("Status: Disk Failure")
    log.i("Log is available:\nAppleANS2: ANS2 not responding, retry 3/3\nlwvm: failed to create partition map: IOReturn 0xe00002bc", 0.0)
    failed(log)
    log.usb_mode("Gone", 2.0).usb_mode("Recovery", 9.0)
    return log


@scenario("baseband_1015", "iPhone 7 Plus baseband update fails (status 1015)", "iPhone 7 Plus", "iPhone9,2",
          "d11ap", 1, "Fails while updating the baseband; iBoot already reported a radio error.")
def s_bb():
    log = preamble(Log(), "Recovery", "d11ap", "iPhone9,2")
    signing(log, "Recovery")
    log.progress(1, 0.3)
    log.i("Recovery Mode Environment:")
    log.i("iBoot build-version=iBoot-11881.0.39")
    log.i("iBoot build-style=RELEASE")
    log.i("radio-error=1")
    log.i("radio-error-string=Baseband failed to boot")
    log.i("Sending iBEC (1245184 bytes)...", 0.2)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.6).usb_mode("Gone")
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) connected in Recovery mode", 3.4).usb_mode("Recovery")
    log.i("Sending RestoreRamDisk (118489088 bytes)...", 0.4)
    log.i("Sending RestoreDeviceTree (131072 bytes)...", 4.8)
    log.i("Sending RestoreKernelCache (19431424 bytes)...", 0.3)
    log.i("Waiting for device to enter restore mode...", 1.0)
    restore_os_up(log)
    storage_ok(log)
    firmware_ok(log, baseband=False, se=False)
    log.i("Preparing for baseband update (48)", 1.0)
    log.i("About to send BasebandData...", 0.2)
    log.i("Sending Baseband TSS request...")
    log.i("Received Baseband SHSH blobs", 1.0)
    log.i("Sending BasebandData now...", 0.5)
    log.i("Done sending BasebandData", 2.0)
    log.i("Booting the baseband (49)", 0.3)
    log.i("Updating baseband (19)", 30.0)
    log.i("Got status message", 45.0)
    log.i("Status: X-Gold Baseband Update Failed. Defective Unit?")
    failed(log)
    log.usb_mode("Gone", 2.0).usb_mode("Recovery", 10.0)
    return log


@scenario("baseband_no_snum", "iPhone 11 baseband does not identify itself", "iPhone 11", "iPhone12,1", "n104ap",
          1, "The modem never reports its serial, so its firmware cannot be signed.")
def s_bb2():
    log = preamble(Log(), "Recovery", "n104ap", "iPhone12,1")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    restore_os_up(log)
    storage_ok(log)
    log.i("About to send NORData...", 0.4)
    log.i("Sending NORData now...", 0.8)
    log.i("Done sending NORData", 0.4)
    log.i("Flashing firmware (18)", 0.2)
    log.i("Preparing for baseband update (48)", 1.0)
    log.i("About to send BasebandData...", 25.0)
    log.raw("ERROR: Unable to find required BbSNUM in parameters")
    log.e("Unable to create Baseband TSS request", 0.0)
    log.e("Unable to fetch Baseband TSS", 0.0)
    log.e("Unable to send BasebandData", 0.0)
    failed(log)
    log.usb_mode("Gone", 2.0).usb_mode("Recovery", 10.0)
    return log


@scenario("faceid_savage", "iPhone 12 Face ID module does not answer", "iPhone 12", "iPhone13,2", "d53gap", 1,
          "The Savage (Face ID) updater cannot get the module's ID.")
def s_savage():
    log = preamble(Log(), "Recovery", "d53gap", "iPhone13,2")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    restore_os_up(log)
    storage_ok(log)
    firmware_ok(log, baseband=False)
    log.i("Updating Savage (60)", 0.4)
    log.raw("ERROR: tss_request_add_savage_tags: Unable to find required Savage,UID in parameters")
    log.e("restore_send_firmware_updater_data: Couldn't get Savage firmware data", 0.0)
    failed(log)
    log.usb_mode("Gone", 2.0).usb_mode("Recovery", 10.0)
    return log


@scenario("restore_os_vanished", "iPhone XS never boots the restore OS", "iPhone XS", "iPhone11,2", "d321ap", 1,
          "After the restore ramdisk is sent the phone drops off USB and never returns.")
def s_vanish():
    log = preamble(Log(), "Recovery", "d321ap", "iPhone11,2")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.0).usb_mode("Gone")
    log.wait(300.0)
    log.e("Device failed to enter restore mode.")
    log.e("Make sure that usbmuxd is running.")
    log.i("RESTORE FAILED", 0.1)
    return log


@scenario("restore_os_pc_side", "Restore mode reached but the PC can't talk to it", "iPhone 14", "iPhone14,7",
          "d27ap", 1, "The USB watch sees the phone in restore mode, but the engine never connects.")
def s_pcside():
    log = preamble(Log(), "Recovery", "d27ap", "iPhone14,7")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.0).usb_mode("Gone")
    log.usb_mode("Restore", 15.0)
    log.wait(285.0)
    log.e("Device failed to enter restore mode.")
    log.e("Make sure that usbmuxd is running.")
    log.i("RESTORE FAILED", 0.1)
    return log


@scenario("write_drop", "iPhone 14 drops off while writing iOS", "iPhone 14", "iPhone14,7", "d27ap", 1,
          "A single drop at 40% while iOS is being written.")
def s_drop():
    log = preamble(Log(), "Recovery", "d27ap", "iPhone14,7")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    restore_os_up(log)
    log.i("Waiting for NAND (28)", 0.6)
    log.i("Creating partition map (11)", 2.1)
    log.i("Creating filesystem (12)", 1.0)
    log.i("About to send filesystem...", 0.4)
    log.i("Connected to ASR")
    log.i("Validating the filesystem")
    log.i("Filesystem validated", 2.0)
    log.i("Sending filesystem now...")
    log.i("Restoring image (13)", 0.2)
    for k in range(1, 5):
        log.progress(2, k / 10, 6.0)
    log.d(f"idevice_event_cb: device {ECID_HEX} (udid: 00008110-001A2B3C4D5E6F70) disconnected", 1.0).usb_mode("Gone")
    log.e("_restore_send_file_data: Failed to send data (-256)", 0.2)
    log.e("Unable to send data to ASR")
    log.e("Unable to send filesystem", 0.0)
    failed(log)
    log.usb_mode("Recovery", 12.0)
    return log


@scenario("write_drop_repeat", "iPhone 14 drops at 40% again (second run)", "iPhone 14", "iPhone14,7", "d27ap", 1,
          "Second attempt on a different cable; the phone hands over the panic from the first crash and drops at "
          "the same point.", attempt=2)
def s_drop2():
    log = preamble(Log(), "Recovery", "d27ap", "iPhone14,7")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.0).usb_mode("Gone")
    log.d(f"idevice_event_cb: device {ECID_HEX} (udid: 00008110-001A2B3C4D5E6F70) connected in Restore mode",
          18.0).usb_mode("Restore")
    log.i("Connecting now...", 0.1)
    log.i("Connected to com.apple.mobile.restored, version 15", 0.1)
    log.i("Device 00008110-001A2B3C4D5E6F70 has successfully entered restore mode")
    log.i("Previous restore exit status: 0x400")
    log.i("Panic log is available:\npanic(cpu 0 caller 0xfffffff02a1c3b44): ANS2 Recoverable Panic - assert failed: "
          "[3412]:nand_read: uncorrectable error\nDebugger message: panic\nOS release type: User\n"
          "Kernel version: Darwin Kernel Version 25.0.0", 0.0)
    log.i("About to restore device... ", 0.4)
    log.i("Waiting for NAND (28)", 0.6)
    log.i("Creating partition map (11)", 2.1)
    log.i("Creating filesystem (12)", 1.0)
    log.i("About to send filesystem...", 0.4)
    log.i("Connected to ASR")
    log.i("Validating the filesystem")
    log.i("Filesystem validated", 2.0)
    log.i("Sending filesystem now...")
    log.i("Restoring image (13)", 0.2)
    for k in range(1, 5):
        log.progress(2, k / 10 + 0.01, 6.0)
    log.d(f"idevice_event_cb: device {ECID_HEX} (udid: 00008110-001A2B3C4D5E6F70) disconnected", 1.0).usb_mode("Gone")
    log.e("_restore_send_file_data: Failed to send data (-256)", 0.2)
    log.e("Unable to send data to ASR")
    failed(log)
    log.usb_mode("Recovery", 12.0)
    return log


@scenario("not_signed", "iOS build no longer signed", "iPhone 13", "iPhone14,5", "d17ap", 1,
          "Apple's signing server refuses the chosen build (status 94).")
def s_unsigned():
    log = preamble(Log(), "Recovery", "d17ap", "iPhone14,5")
    log.i("Getting ApNonce in recovery mode... ")
    log.i("Trying to fetch new SHSH blob", 0.3)
    log.raw("TSS server returned: STATUS=94&MESSAGE=This device isn't eligible for the requested build.", 1.2)
    log.raw("ERROR: TSS request failed (status=94, message=This device isn't eligible for the requested build.)")
    log.e("Unable to get SHSH blobs for this device", 0.0)
    log.e("Unable to proceed without a TSS record.", 0.0)
    return log


@scenario("no_device", "Phone never detected", "Unknown", "", "", 1,
          "No phone answered in any mode within the engine's wait.")
def s_nodev():
    log = Log()
    log.i(VERSION).progress(0, 0.0)
    log.e("Unable to discover device mode. Please make sure a device is attached.", 10.0)
    return log


@scenario("dfu_hop_drop", "Drops during the DFU-to-Recovery hop", "iPhone 8", "iPhone10,4", "d201ap", 1,
          "The phone falls off USB while the first bootloader loads.")
def s_hop():
    log = preamble(Log(), "DFU", "d201ap", "iPhone10,4")
    signing(log, "DFU")
    log.i("Sending iBSS (1048576 bytes)...", 0.3)
    log.d(f"irecv_event_cb: device {ECID_HEX} (udid: N/A) disconnected", 1.2).usb_mode("Gone")
    log.e("Unable to place device into recovery mode from DFU mode", 60.0)
    log.usb_mode("DFU", 2.0)
    return log


@scenario("battery_veridian", "iPhone 15 fails updating battery management", "iPhone 15", "iPhone15,4", "d37ap",
          1, "Drops off during the Veridian (battery management) update.")
def s_battery():
    log = preamble(Log(), "Recovery", "d37ap", "iPhone15,4")
    signing(log, "Recovery")
    boot_chain_from_recovery(log)
    restore_os_up(log)
    storage_ok(log)
    firmware_ok(log, baseband=False)
    log.i("Sending Veridian TSS request...", 0.4)
    log.i("Received Veridian ticket", 0.8)
    log.i("Updating Veridian (66)", 0.3)
    log.d(f"idevice_event_cb: device {ECID_HEX} (udid: 00008120-001A2B3C4D5E6F70) disconnected", 8.0).usb_mode("Gone")
    log.e("Could not read data (-4). Aborting.", 0.1)
    failed(log)
    log.usb_mode("Recovery", 15.0)
    return log


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    for sc in SCENARIOS:
        log = sc["fn"]()
        path = OUT / f"{sc['id']}.log"
        path.write_text(log.render(), encoding="utf-8")
        index.append({k: v for k, v in sc.items() if k != "fn"} | {
            "log": path.name, "usb": log.usb, "duration": round(log.t, 2)})
    (OUT / "scenarios.json").write_text(json.dumps({"synthetic": True, "scenarios": index}, indent=1) + "\n",
                                        encoding="utf-8")
    print(f"wrote {len(index)} scenarios to {OUT}")


if __name__ == "__main__":
    main()
