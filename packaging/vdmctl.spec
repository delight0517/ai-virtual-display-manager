# Standalone CLI build; keep the spec and source layout independent of a local path.
from pathlib import Path
root = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(root / "tools" / "vdmctl.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "THIRD_PARTY_NOTICES.md"), "."),
           (str(root / "licenses"), "licenses")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="vdmctl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)



