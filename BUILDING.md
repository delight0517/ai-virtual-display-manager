# Building on Windows

This guide builds the source candidate locally. It does not install the app, change a display, or enable the external driver.

## Requirements

- Windows 11 x64
- Python 3.13.13 x64 and the Python launcher (`py`)
- PowerShell

From the source root, create an isolated environment and install the exact tested dependency set:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements-build-lock.txt
```

The version command should report Python 3.13.13. The lock file includes direct and transitive Python build dependencies. `requirements-build.txt` lists only direct dependencies for maintainers who intentionally update the build environment.

## Tests and packages

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "packaging\AI Virtual Display Manager.spec"
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "packaging\vdmctl.spec"
```

The app is an onedir package under `dist\AI Virtual Display Manager\`; the operator CLI is `dist\vdmctl.exe`. Both builds include `THIRD_PARTY_NOTICES.md` and the corresponding license texts in `licenses\`.

## Read-only smoke checks

```powershell
.\dist\vdmctl.exe --help
.\dist\AI Virtual Display Manager\AI Virtual Display Manager.exe --self-check .\self-check-report.json
```

The hidden self-check reads local device/display metadata and exercises an ephemeral local task-runtime fixture. It does not launch a consumer app, capture desktop pixels, bind the production task pipe, or change display topology. A successful self-check is not evidence that a consumer integration works on another computer.

## Release boundary

This source candidate has not been installed or published. No project license has been chosen yet. Do not redistribute it until the owner adds the selected project license and reviews all upstream terms in `THIRD_PARTY_NOTICES.md`. The external MTT VDD driver is not bundled; obtain and install it separately from its upstream project.



