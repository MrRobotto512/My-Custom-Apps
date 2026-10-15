"""Regenerate restore_doctor/kb/devices.json from libirecovery's device table.

Usage:
    python tools/gen_models.py path/to/libirecovery/src/libirecovery.c

libirecovery is the library the engine uses to recognise phones in DFU and
Recovery mode, so its table is the ground truth for "which models can this
engine see". Re-run this whenever the engine is rebuilt for new models.
"""
import json
import re
import sys
from pathlib import Path

ROW = re.compile(
    r'\{\s*"(?P<product>(?:iPhone|iPad)[0-9]+,[0-9]+)",\s*"(?P<board>[a-z0-9]+)",\s*'
    r'(?P<bdid>0x[0-9A-Fa-f]+),\s*(?P<cpid>0x[0-9A-Fa-f]+),\s*"(?P<name>[^"]+)"\s*\}'
)


def main(src: str) -> None:
    text = Path(src).read_text(encoding="utf-8", errors="replace")
    devices = []
    for m in ROW.finditer(text):
        if "99," in m["product"]:  # virtual research device, never a real phone
            continue
        devices.append({
            "product": m["product"],
            "board": m["board"],
            "bdid": int(m["bdid"], 16),
            "cpid": int(m["cpid"], 16),
            "name": m["name"],
        })
    out = Path(__file__).resolve().parent.parent / "restore_doctor" / "kb" / "devices.json"
    out.write_text(json.dumps({"source": "libirecovery device table", "devices": devices}, indent=1) + "\n",
                   encoding="utf-8")
    print(f"wrote {len(devices)} devices to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "libirecovery.c")
