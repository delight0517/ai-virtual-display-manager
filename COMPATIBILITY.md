# Compatibility and verification status

This document describes observed verification, not a promise that every PC or AI application will work.

## Verified build environment

- Windows 11, build 26300
- Python 3.13.13, run from a Windows virtual environment
- PyInstaller 6.22.3
- The public-candidate Windows CI uses the complete dependency set recorded in `requirements-build-lock.txt`.
- VDM source candidate 0.1.99; `manager_cli` 0.1.19
- Source suite: 554 tests passed
- Windows app and CLI: built from this staged source tree; hidden app self-check and CLI help returned exit code 0
- The self-check read host display metadata. It did not start an AI target, capture pixels, or change display topology.

## Integration status

The staged candidate was not installed. Live isolated sessions for the Only Chat and Router task targets have not been verified against matching application identities. The candidate has not been used to start a task, change display topology, or capture pixels. These target integrations remain unverified, so passing the source suite and hidden self-check do not establish live-operation reliability.

## Support limits

- Windows only. macOS has no verified VDM display provider in this candidate.
- The external MikeTheTech Virtual Display Driver is a prerequisite for virtual-output features. VDM does not install, sign, or redistribute it.
- Task sessions use a fixed target allowlist. This is not a general launcher for arbitrary AI applications.
- Only the build host and the listed software versions have been exercised. Other Windows releases, GPU/display hardware, driver versions, and application versions have not been verified.
- A virtual display is a presentation surface; it is not an operating-system security boundary.
- Passing tests and a hidden self-check do not guarantee future error-free operation on every system.
