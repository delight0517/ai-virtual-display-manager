# PyInstaller onedir candidate. Build from the canonical project root.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve().parent
datas = [
    (str(root / "module-registry.json"), "."),
    (str(root / "THIRD_PARTY_NOTICES.md"), "."),
    (str(root / "licenses"), "licenses"),
    (str(root / "assets"), "assets"),
]
for path in sorted((root / "modules").rglob("*")):
    if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
        datas.append((str(path), str(path.parent.relative_to(root))))
uia_datas, uia_binaries, uia_hiddenimports = [], [], []
for package in ("pywinauto", "comtypes"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    uia_datas.extend(package_datas)
    uia_binaries.extend(package_binaries)
    uia_hiddenimports.extend(package_hiddenimports)

a = Analysis(
    [str(root / "app.py")],
    pathex=[str(root)],
    binaries=uia_binaries,
    datas=datas + uia_datas,
    # Versioned features load from the manifest at runtime; include stdlib
    # imports that static Analysis cannot discover from those modules.
    hiddenimports=["ctypes.wintypes", "core.vdd_pipe", "uuid",
                   "core.png_capture", "core.win32_dpi", "core.runtime_packet",
                   "core.mtt_status_cache",
                   "modules.private_desktop.session",
                   *uia_hiddenimports],
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
    [],
    exclude_binaries=True,
    name="AI Virtual Display Manager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    contents_directory=".",
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "ai_virtual_display.ico"),
)
cli_analysis = Analysis(
    [str(root / "tools" / "vdmctl.py")], pathex=[str(root)],
    binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
cli_pyz = PYZ(cli_analysis.pure)
cli_exe = EXE(
    cli_pyz, cli_analysis.scripts, [], exclude_binaries=True,
    name="vdmctl", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True, contents_directory=".",
)
coll = COLLECT(
    exe,
    cli_exe,
    a.binaries,
    cli_analysis.binaries,
    a.datas,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="AI Virtual Display Manager",
)
