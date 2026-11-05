# Compatibility and verification status

This document describes observed verification, not a promise that every PC or AI application will work.

## Verified build environment

- Windows 11, build 26300
- Python 3.13.13, run from a Windows virtual environment
- PyInstaller 6.22.3
- The public-candidate Windows CI uses the complete dependency set recorded in `requirements-build-lock.txt`; candidate 0.1.103 passed CI in [GitHub Actions](https://github.com/delight0517/ai-virtual-display-manager/actions/runs/42059008002).
- VDM source candidate 0.1.104; `manager_cli` 0.1.21
- Source suite: 562 tests passed on the development host
- Windows app and CLI: candidate 0.1.104 built locally; CLI help and hidden app self-check returned exit code 0
- The self-check read host display metadata. It did not start an AI target, capture pixels, or change display topology.

## Integration status

Candidate 0.1.103 recovered the active virtual-output pool after verified endpoint renumbering. Candidate 0.1.104 is now installed on the development host; its Router QA target rendered on a fresh VDM private desktop with zero virtual outputs, and stop verified desktop removal, input-desktop preservation, and temp-profile removal. No account or provider operation was performed. These checks do not verify Only Chat integration, virtual-monitor window placement, other PCs or drivers, or stable-release reliability.

## Support limits

- Windows only. macOS has no verified VDM display provider in this candidate.
- The external MikeTheTech Virtual Display Driver is a prerequisite for virtual-output features. VDM does not install, sign, or redistribute it.
- Task sessions use a fixed target allowlist. This is not a general launcher for arbitrary AI applications.
- Only the build host and the listed software versions have been exercised. Other Windows releases, GPU/display hardware, driver versions, and application versions have not been verified.
- A virtual display is a presentation surface; it is not an operating-system security boundary.
- Passing tests and a hidden self-check do not guarantee future error-free operation on every system.
