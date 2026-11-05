"""Attested AI Account Router target for VDM's private desktop only."""

from pathlib import Path
import hashlib
import json

API_VERSION = 1
VERSION = "0.1.1"
MODULE_ID = "router_task_target"
PRIVATE_PROFILE_TOKEN = "{VDM_TASK_PROFILE}"
ROUTER_WINDOW_CLASS_PREFIX = "WindowsForms10.Window.8.app.0."


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_target(runtime_module, registry_path):
    """Accept only the exact registered Router image attested for private QA."""
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8-sig"))
        rows = [row for row in registry["apps"]
                if isinstance(row, dict) and row.get("id") == "ai-account-router"]
        if len(rows) != 1:
            return None, "ROUTER_REGISTRY_AMBIGUOUS"
        windows = rows[0].get("windows")
        if not isinstance(windows, dict):
            return None, "ROUTER_PRIVATE_QA_NOT_ATTESTED"
        image_path = windows.get("privateQaNativePath")
        image_hash = windows.get("privateQaNativeSha256")
        source_path = windows.get("source")
        if (rows[0].get("version") != "1.3.0"
                or windows.get("privateQaModeSupported") is not True
                or windows.get("privateQaNativeVerified") is not True
                or not isinstance(image_path, str)
                or not Path(image_path).is_absolute()
                or not isinstance(source_path, str)
                or not Path(source_path).is_absolute()
                or not isinstance(image_hash, str)):
            return None, "ROUTER_PRIVATE_QA_NOT_ATTESTED"
        source_root = Path(source_path).resolve(strict=True)
        image = Path(image_path).resolve(strict=True)
        if (not source_root.is_dir() or not image.is_file()
                or not image.is_relative_to(source_root)
                or image.name.casefold() != "ai account router.exe"
                or _hash(image).casefold() != image_hash.casefold()):
            return None, "ROUTER_PRIVATE_QA_IMAGE_MISMATCH"
        descriptor = runtime_module.TargetLaunchDescriptor(
            executable_path=str(image), executable_sha256=image_hash.lower(),
            arguments=(f"--qa-private-profile={PRIVATE_PROFILE_TOKEN}",),
            working_directory=str(image.parent),
            window_class_names=(ROUTER_WINDOW_CLASS_PREFIX + "*",),
            actions=(), cleanup_timeout_seconds=10, maximum_session_seconds=900,
        )
        return runtime_module.TargetSpec(
            app_id="ai_account_router_qa", action_ids=descriptor.action_ids,
            launch=descriptor, minimum_output_count=0, maximum_output_count=0,
            output_profiles=frozenset({"private"}), isolation_mode="private_desktop",
        ), None
    except (OSError, KeyError, TypeError, ValueError):
        return None, "ROUTER_PRIVATE_QA_UNAVAILABLE"
