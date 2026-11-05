# AI Virtual Display Manager for Windows

AI Virtual Display Manager (VDM) is a Windows utility for inspecting display state and managing an already installed MikeTheTech Virtual Display Driver (MTT VDD). It does not install, sign, or redistribute that third-party driver.

## Current project status

This repository is a public, pre-release source candidate at https://github.com/delight0517/ai-virtual-display-manager. It is **not a stable release**: the most recent code has passed 554 source tests and a bundled hidden self-check, but the new Router private-QA target has not been installed or run in a live isolated session. The installed runtime is older than this candidate. Hardware and driver combinations beyond the development machine have not been verified.

The build candidate is VDM 0.1.99 with `manager_cli` 0.1.19. This project currently targets Windows only. Review [COMPATIBILITY.md](COMPATIBILITY.md) for the exact tested environment and known gaps. Third-party component terms for redistributed build outputs are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). VDM is released under the MIT License. See [LICENSE](LICENSE). Bundled third-party components retain their own terms, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

VDM's task runner currently uses a fixed target allowlist. It is not a generic launcher for arbitrary AI applications. A virtual monitor is a presentation surface, not a security boundary or a second operating-system session. The driver must be installed separately by the user from its upstream source and Windows/UAC prompts remain user controlled.

## Continuous integration

A Windows GitHub Actions workflow is configured to run the unit suite and build both the app and operator CLI on pushes and pull requests. It has not run on GitHub because this candidate has not been published to a repository.

## Build from source

Use Windows 11, Python 3.13.13, and PowerShell. From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements-build-lock.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "packaging\AI Virtual Display Manager.spec"
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "packaging\vdmctl.spec"
```

GitHub Actions uses this same fully pinned build environment; `requirements-build.txt` remains the concise direct-dependency list.

The application and `vdmctl` are built separately. See [BUILDING.md](BUILDING.md) for output locations and read-only smoke checks. Do not copy a candidate over an active installation.

## Safe use

- Read the live display and physical-monitor status before any output operation.
- Use the app's impact preview and explicit confirmation for display changes.
- Never use a physical display as a fallback for isolated AI QA.
- Keep the external MTT VDD driver separate and follow its upstream installation and licensing terms.
- Do not assume that a private desktop, virtual monitor, or application profile protects credentials or network access from an untrusted application.

## License

VDM source is licensed under the MIT License; see [LICENSE](LICENSE). Third-party components retain their own licenses and notices.






