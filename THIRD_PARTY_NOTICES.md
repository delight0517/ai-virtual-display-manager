# Third-party notices

This file covers third-party components included in Windows build outputs. It does not grant or select a license for AI Virtual Display Manager itself. The VDM license and copyright holder remain unresolved; do not redistribute the VDM source or binary until that is decided.

| Component | Version | License / notice |
|---|---:|---|
| CPython | 3.13.13 | Python Software Foundation License; see `licenses/Python-PSF.txt` |
| Tcl/Tk | Python 3.13.13 bundled runtime | Tcl/Tk terms; see `licenses/Tcl-Tk-License-Terms.txt` |
| comtypes | 1.4.16 | MIT; see `licenses/comtypes-MIT.txt` |
| pywinauto | 0.6.9 | BSD 3-Clause; see `licenses/pywinauto-BSD-3-Clause.txt` |
| pywin32 | 311 | PSF package metadata and BSD-style Mark Hammond terms; see `licenses/pywin32-BSD-3-Clause.txt` and package metadata |
| pywin32-ctypes | 0.2.3 | BSD 3-Clause; see `licenses/pywin32-ctypes-BSD-3-Clause.txt` |
| six | 1.17.0 | MIT; see `licenses/six-MIT.txt` |
| setuptools | 84.2.1 | MIT; see `licenses/setuptools-MIT.txt` |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception; see `licenses/PyInstaller-GPL-2.0-or-later-Bootloader-Exception.txt` |
| PyInstaller Community Hooks runtime hooks | 2026.9 | Apache-2.0 for the two bundled runtime hooks (`pyi_rth_pywintypes.py`, `pyi_rth_pythoncom.py`); full upstream license details at `licenses/pyinstaller-hooks-contrib-LICENSE.txt` |

## Build-only dependencies

These packages are used by the Windows build environment and are not included as runtime components in the app/CLI packages:

| Component | Version | License / notice |
|---|---:|---|
| altgraph | 0.17.5 | MIT; see `licenses/altgraph-MIT.txt` |
| packaging | 26.3 | Apache-2.0 or BSD-2-Clause; see `licenses/packaging-Apache-2.0.txt` and `licenses/packaging-BSD-2-Clause.txt` |
| pefile | 2024.8.26 | MIT; see `licenses/pefile-MIT.txt` |

The other packages in `requirements-build-lock.txt` are listed above because their code or data is present in the build outputs. The community-hooks license file includes terms for standard hooks used only during build and Apache-2.0 terms for the runtime hooks included in the app.

PyInstaller is a build tool. The MTT Virtual Display Driver is not bundled, installed, signed, or redistributed by VDM; obtain it separately from its upstream distributor and follow its own license and installation instructions.
