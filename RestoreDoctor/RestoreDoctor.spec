# PyInstaller spec for Restore Doctor (one-folder build).
#   pyinstaller --noconfirm RestoreDoctor.spec
# The engine folder is copied next to RestoreDoctor.exe by tools/build_app.ps1
# (or the GitHub Actions workflow); it is not bundled inside the exe.
import sys
from pathlib import Path

root = Path(SPECPATH)
datas = [
    (str(root / "restore_doctor" / "kb"), "restore_doctor/kb"),
    (str(root / "restore_doctor" / "scenarios"), "restore_doctor/scenarios"),
]

a = Analysis(
    [str(root / "RestoreDoctor.pyw")],
    pathex=[str(root)],
    datas=datas,
    hiddenimports=["tkinter", "tkinter.ttk"],
    excludes=["numpy", "pandas", "matplotlib", "PIL"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RestoreDoctor",
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="RestoreDoctor")
