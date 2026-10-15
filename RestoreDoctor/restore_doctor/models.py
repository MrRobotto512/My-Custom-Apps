"""Device identification and per-family facts the rules and the DFU helper use."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .kb import get_kb


@dataclass(frozen=True)
class DeviceModel:
    product: str          # e.g. iPhone14,5
    name: str             # e.g. iPhone 13
    board: str = ""       # e.g. d17ap
    cpid: int = 0
    bdid: int = 0

    @property
    def is_iphone(self) -> bool:
        return self.product.startswith("iPhone")

    @property
    def generation(self) -> tuple[int, int]:
        m = re.match(r"[A-Za-z]+(\d+),(\d+)", self.product)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_by_product(product: str) -> DeviceModel | None:
    for d in get_kb().devices:
        if d["product"] == product:
            return DeviceModel(d["product"], d["name"], d["board"], d["cpid"], d["bdid"])
    return None


def find_by_chip(cpid: int, bdid: int) -> DeviceModel | None:
    for d in get_kb().devices:
        if d["cpid"] == cpid and d["bdid"] == bdid:
            return DeviceModel(d["product"], d["name"], d["board"], d["cpid"], d["bdid"])
    return None


# --- family facts -----------------------------------------------------------
# Product identifiers grouped by hardware family. Kept explicit rather than
# clever, so a tech can read and correct them.

_HOME_BUTTON_SE = {"iPhone8,4", "iPhone12,8", "iPhone14,6"}          # SE 1, 2, 3
_IPHONE_8 = {"iPhone10,1", "iPhone10,2", "iPhone10,4", "iPhone10,5"}    # 8, 8 Plus
_IPHONE_7 = {"iPhone9,1", "iPhone9,2", "iPhone9,3", "iPhone9,4"}
_USB_C_EARLY = {"iPhone15,4", "iPhone15,5"}                            # iPhone 15, 15 Plus


def has_home_button(m: DeviceModel) -> bool:
    if not m.is_iphone:
        return False
    major = m.generation[0]
    return m.product in _HOME_BUTTON_SE or m.product in _IPHONE_8 or major <= 9


def has_face_id(m: DeviceModel) -> bool:
    return m.is_iphone and not has_home_button(m) and m.generation[0] >= 10


def is_iphone_7(m: DeviceModel) -> bool:
    return m.product in _IPHONE_7


def has_usb_c(m: DeviceModel) -> bool:
    return m.is_iphone and (m.product in _USB_C_EARLY or m.generation[0] >= 16)


def is_stacked_board(m: DeviceModel) -> bool | None:
    """Two-layer 'sandwich' logic board: iPhone X through the iPhone 16 family
    (SE models excluded). Unknown (None) for newer families until confirmed."""
    if not m.is_iphone or m.product in _HOME_BUTTON_SE:
        return False
    major = m.generation[0]
    if m.product in ("iPhone10,3", "iPhone10,6") or 11 <= major <= 17:
        return True
    if major >= 18:
        return None
    return False


def usb_chip_name(m: DeviceModel | None) -> str:
    """USB data chip naming: Tristar before iPhone 8, Hydra on 8 to 14 (Rossmann
    Group), a USB-C port controller from the iPhone 15 on."""
    if m is None or not m.is_iphone:
        return "the USB data chip"
    if has_usb_c(m):
        return "the USB-C port controller"
    if m.generation[0] <= 9:
        return "Tristar"
    return "Hydra"


def model_notes(m: DeviceModel | None, part: str) -> list[str]:
    """Extra lines for a suspect part on this model."""
    if m is None:
        return []
    notes: list[str] = []
    if part == "baseband":
        if is_iphone_7(m):
            notes.append("iPhone 7/7 Plus: baseband PMU faults are a known cause of restore failures (REWA).")
    if part in ("i2c_peripheral", "faceid") and has_face_id(m):
        notes.append("Face ID model: an I2C short from earpiece/front-sensor flex corrosion is a common cause of "
                     "4013-style failures; try with that flex disconnected (Rossmann Group).")
    if part == "usb_port_ic":
        notes.append(f"USB data chip on this model: {usb_chip_name(m)}.")
    return notes


# --- DFU / Recovery button sequences ----------------------------------------

def dfu_steps(m: DeviceModel | None) -> list[str]:
    if m is not None and m.is_iphone and is_iphone_7(m):
        return [
            "Plug the phone into this PC.",
            "Hold Side and Volume Down together for 8 seconds.",
            "Let go of Side, keep holding Volume Down for about 5 more seconds.",
            "Screen stays black = DFU. Apple logo or a cable picture = start again.",
        ]
    if m is not None and m.is_iphone and has_home_button(m) and m.product not in _IPHONE_8 \
            and m.product not in ("iPhone12,8", "iPhone14,6"):
        return [
            "Plug the phone into this PC.",
            "Hold Home and Side (or Top) together for 8 seconds.",
            "Let go of Side, keep holding Home for about 5 more seconds.",
            "Screen stays black = DFU. Apple logo or a cable picture = start again.",
        ]
    return [
        "Plug the phone into this PC.",
        "Press and release Volume Up, then press and release Volume Down.",
        "Hold Side until the screen goes black.",
        "Keep holding Side and also hold Volume Down for 5 seconds.",
        "Let go of Side, keep holding Volume Down for about 10 more seconds.",
        "Screen stays black = DFU. Apple logo or a cable picture = start again.",
    ]


def recovery_steps(m: DeviceModel | None) -> list[str]:
    if m is not None and m.is_iphone and is_iphone_7(m):
        return ["Plug the phone in.", "Hold Side and Volume Down until the computer-and-cable screen appears."]
    if m is not None and m.is_iphone and has_home_button(m) and m.product not in _IPHONE_8 \
            and m.product not in ("iPhone12,8", "iPhone14,6"):
        return ["Plug the phone in.", "Hold Home and Side (or Top) until the computer-and-cable screen appears."]
    return [
        "Plug the phone in.",
        "Press and release Volume Up, then Volume Down.",
        "Hold Side until the computer-and-cable screen appears.",
    ]
