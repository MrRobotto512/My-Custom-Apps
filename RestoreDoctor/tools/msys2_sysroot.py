"""Resolve and unpack MSYS2 mingw64 packages into a cross-compile sysroot.

usage: python3 msys2_sysroot.py DB DLDIR SYSROOT pkg [pkg...]
Packages are named without the mingw-w64-x86_64- prefix.
"""
import io
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = "https://repo.msys2.org/mingw/mingw64/"
PFX = "mingw-w64-x86_64-"
SKIP = {"gcc-libs", "libwinpthread", "winpthreads", "libwinpthread-git", "crt", "headers"}


def load_db(path: Path) -> dict:
    raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    pkgs, provides = {}, {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        for m in t.getmembers():
            if not m.name.endswith("/desc"):
                continue
            text = t.extractfile(m).read().decode()
            fields, key = {}, None
            for line in text.splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                    fields[key] = []
                elif line and key:
                    fields[key].append(line)
            name = fields["NAME"][0]
            pkgs[name] = fields
            for p in fields.get("PROVIDES", []):
                provides[re.split(r"[<>=]", p)[0]] = name
    return pkgs, provides


def main():
    db, dl, root = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    want = [PFX + n for n in sys.argv[4:]]
    pkgs, provides = load_db(db)
    seen, order = set(), []

    def visit(name):
        name = re.split(r"[<>=]", name)[0]
        if name not in pkgs:
            name = provides.get(name, name)
        if name in seen or name not in pkgs:
            return
        short = name[len(PFX):] if name.startswith(PFX) else name
        if short in SKIP or not name.startswith(PFX):
            seen.add(name)
            return
        seen.add(name)
        for d in pkgs[name].get("DEPENDS", []):
            visit(d)
        order.append(name)

    for w in want:
        visit(w)
    root.mkdir(parents=True, exist_ok=True)
    dl.mkdir(parents=True, exist_ok=True)
    for name in order:
        fn = pkgs[name]["FILENAME"][0]
        dest = dl / fn
        if not dest.exists():
            with urllib.request.urlopen(REPO + fn, timeout=120) as r:
                dest.write_bytes(r.read())
        subprocess.run(["tar", "--zstd" if fn.endswith(".zst") else "-J", "-xf", str(dest), "-C", str(root),
                        "--exclude=.BUILDINFO", "--exclude=.PKGINFO", "--exclude=.MTREE", "--exclude=.INSTALL"],
                       check=True)
        print(f"{name} {pkgs[name]['VERSION'][0]}")


if __name__ == "__main__":
    main()
