"""Attested installed Pomodoro target for the manager's private desktop.

This is the real installed app/profile, not a synthetic QA profile. The caller
must close its existing instance before starting this target. The private
desktop backend owns only the new process Job and never switches desktops.
"""
from pathlib import Path
import hashlib
import json
import subprocess

API_VERSION = 1
VERSION = "0.1.0"
MODULE_ID = "pomodoro_task_target"


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_stopped_instance(process_reader=None):
    """Never open the real profile while a normal Pomodoro instance uses it."""
    if process_reader is None:
        def process_reader():
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "@(Get-Process -Name vintage_pomodoro -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty Id) | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
            return json.loads(result.stdout.strip() or "[]")
    try:
        processes = process_reader()
        if isinstance(processes, int):
            processes = [processes]
        if not isinstance(processes, list) or any(type(pid) is not int or pid <= 0 for pid in processes):
            return "POMODORO_PROCESS_STATUS_UNAVAILABLE"
        return "POMODORO_ALREADY_RUNNING" if processes else None
    except Exception:
        return "POMODORO_PROCESS_STATUS_UNAVAILABLE"


def build_target(runtime_module, registry_path):
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8-sig"))
        rows = [row for row in registry["apps"] if row.get("id") == "vintage-pomodoro"]
        if len(rows) != 1:
            return None, "POMODORO_REGISTRY_AMBIGUOUS"
        windows = rows[0]["windows"]
        if windows.get("installedVerified") is not True:
            return None, "POMODORO_INSTALL_NOT_VERIFIED"
        install = Path(windows["installed"]).resolve(strict=True)
        image = Path(windows["activeExecutable"]).resolve(strict=True)
        if (image.name.casefold() != "vintage_pomodoro.exe"
                or not image.is_relative_to(install / "versions")):
            return None, "POMODORO_IMAGE_OUTSIDE_INSTALL"
        if _hash(image) != str(windows.get("installedExeSha256", "")).lower():
            return None, "POMODORO_IMAGE_HASH_MISMATCH"
        manifest = json.loads((install / "update-ready.json").read_text(encoding="utf-8-sig"))
        entries = {item["path"]: item["sha256"].lower() for item in manifest["files"]}
        if (manifest["buildId"] != image.parent.name
                or entries.get("vintage_pomodoro.exe") != _hash(image)):
            return None, "POMODORO_INSTALL_MANIFEST_MISMATCH"
        if _hash(image.parent / "data" / "app.so") != entries.get("data/app.so"):
            return None, "POMODORO_ENGINE_HASH_MISMATCH"
        descriptor = runtime_module.TargetLaunchDescriptor(
            executable_path=str(image), executable_sha256=_hash(image),
            arguments=(), working_directory=str(image.parent),
            window_class_names=("FLUTTER_RUNNER_WIN32_WINDOW",), actions=(),
            cleanup_timeout_seconds=10, maximum_session_seconds=900,
        )
        return runtime_module.TargetSpec(
            app_id="pomodoro", action_ids=descriptor.action_ids, launch=descriptor,
            minimum_output_count=0, maximum_output_count=0,
            output_profiles=frozenset({"private"}), isolation_mode="private_desktop",
        ), None
    except (OSError, KeyError, TypeError, ValueError):
        return None, "POMODORO_TARGET_UNAVAILABLE"
