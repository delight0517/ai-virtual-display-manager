"""Console client for the canonical AI Virtual Display Manager operator pipe."""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import uuid
from pathlib import Path

PIPE_PATH = r"\\.\pipe\AIVDM_ManagerControl.v1"
CONTROL_PIPE_PATH = r"\\.\pipe\AIVDM_TaskControl.v1"
UI_CONTROL_PIPE_PATH = r"\\.\pipe\AIVDM_UiControl.v1"
TASK_CONTROL_COMMANDS = frozenset({"task.status", "task.stop"})
UI_CONTROL_COMMANDS = frozenset({"ui.status", "ui.show", "ui.hide", "ui.stop"})
OPERATOR_PIPE_WAIT_TIMEOUT_MS = 8_000
CONTROL_PIPE_WAIT_TIMEOUT_MS = 2_000
MAX_REQUEST_BYTES = 16_384
MAX_REPLY_BYTES = 6 * 1024 * 1024
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3


def send_request(command, arguments, *, timeout_ms=2_000, api=None, pipe_path=None):
    if sys.platform != "win32" and api is None:
        raise OSError("VDM operator CLI is available on Windows only")
    api = api or ctypes.WinDLL("kernel32", use_last_error=True)
    pipe_path = pipe_path or pipe_for_command(command)
    api.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong]
    api.WaitNamedPipeW.restype = ctypes.c_int
    api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    api.CreateFileW.restype = ctypes.c_void_p
    api.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                              ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    api.WriteFile.restype = ctypes.c_int
    api.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                             ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    api.ReadFile.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    ready = False
    while time.monotonic() <= deadline:
        remaining_ms = max(1, min(100, int((deadline - time.monotonic()) * 1000)))
        if api.WaitNamedPipeW(pipe_path, remaining_ms):
            ready = True
            break
        error = ctypes.get_last_error()
        if error not in {2, 121, 231}:  # not found, semaphore timeout, pipe busy
            break
        time.sleep(0.01)
    if not ready:
        raise OSError(ctypes.get_last_error(), "VDM operator pipe is unavailable")
    handle = api.CreateFileW(pipe_path, GENERIC_READ | GENERIC_WRITE, 0, None,
                             OPEN_EXISTING, 0, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), "Could not connect to the VDM operator pipe")
    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()),
               "issued_at_ms": int(time.time() * 1000),
               "command": command, "arguments": arguments}
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        api.CloseHandle(handle)
        raise ValueError("VDM operator request exceeds the size limit")
    try:
        buffer = ctypes.create_string_buffer(encoded)
        written = ctypes.c_ulong()
        if not api.WriteFile(handle, buffer, len(encoded), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "Could not send the VDM operator request")
        if written.value != len(encoded):
            raise OSError("VDM operator request was not fully written")
        data = bytearray()
        while len(data) <= MAX_REPLY_BYTES:
            chunk = ctypes.create_string_buffer(64 * 1024)
            read = ctypes.c_ulong()
            if not api.ReadFile(handle, chunk, len(chunk), ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error(), "Could not read the VDM operator response")
            if not read.value:
                break
            data.extend(chunk.raw[:read.value])
            if b"\n" in data:
                break
        if len(data) > MAX_REPLY_BYTES or data.count(b"\n") != 1 or not data.endswith(b"\n"):
            raise ValueError("VDM operator response is malformed or exceeds the size limit")
        response = json.loads(data[:-1].decode("utf-8", errors="strict"))
        if (not isinstance(response, dict) or response.get("protocol_version") != 1
                or response.get("request_id") != request["request_id"]
                or type(response.get("ok")) is not bool):
            raise ValueError("VDM operator response does not match the request")
        return response
    finally:
        api.CloseHandle(handle)


def pipe_for_command(command):
    """Keep time-sensitive task and UI control off the general operator pipe."""
    if command in TASK_CONTROL_COMMANDS:
        return CONTROL_PIPE_PATH
    if command in UI_CONTROL_COMMANDS:
        return UI_CONTROL_PIPE_PATH
    return PIPE_PATH


def pipe_wait_timeout_ms(command):
    """Give general queries time to wait behind a slow operator request."""
    if command in TASK_CONTROL_COMMANDS or command in UI_CONTROL_COMMANDS:
        return CONTROL_PIPE_WAIT_TIMEOUT_MS
    return OPERATOR_PIPE_WAIT_TIMEOUT_MS


def build_parser():
    parser = argparse.ArgumentParser(prog="vdmctl", description="Read and operate the local VDM manager.")
    sub = parser.add_subparsers(dest="group", required=True)
    sub.add_parser("status", help="Read live module, display, pool, guard and task status (JSON)")
    ui = sub.add_parser("ui", help="Control the optional window or stop the background engine")
    ui_sub = ui.add_subparsers(dest="action", required=True)
    for action in ("status", "show", "hide", "stop"):
        ui_sub.add_parser(action)
    logs = sub.add_parser("logs", help="Read retained VDM diagnostic records")
    log_options = logs.add_mutually_exclusive_group()
    log_options.add_argument("--limit", type=int, default=100, help="1..5000 most recent records")
    log_options.add_argument("--all", action="store_true", help="Read all retained VDM event records")
    sub.add_parser("screens", help="Read live display topology and pool state (no pixels)")
    sub.add_parser("windows", help="Read window placement metadata (no titles or pixels)")
    stardesk = sub.add_parser("stardesk", help="Read local StarDesk status; no session or input control")
    stardesk_sub = stardesk.add_subparsers(dest="action", required=True)
    stardesk_sub.add_parser("status", help="Read service/process and verified virtual-output status")
    outputs = sub.add_parser("outputs", help="Preview and manage VDM-owned virtual outputs")
    output_sub = outputs.add_subparsers(dest="action", required=True)
    preview = output_sub.add_parser("preview", help="Prepare an output-pool impact preview")
    preview.add_argument("--count", type=int, required=True, help="new outputs to add (2..8)")
    preview.add_argument("--profile", choices=("balanced", "high", "ultra"), default="high")
    apply = output_sub.add_parser("apply", help="Apply an unexpired preview after explicit confirmation")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--confirm", required=True)
    output_sub.add_parser("return-preview", help="Preview verified return of the manager-owned pool")
    return_cmd = output_sub.add_parser("return", help="Return only the verified manager-owned output pool")
    return_cmd.add_argument("--plan", required=True)
    return_cmd.add_argument("--confirm", required=True)
    task = sub.add_parser("task", help="Control manager-owned isolated QA sessions")
    task_sub = task.add_subparsers(dest="action", required=True)
    task_status = task_sub.add_parser("status", help="Read task target and lease readiness")
    task_status.add_argument("--owner", help="Stable AI/chat owner ID")
    start = task_sub.add_parser("start", help="Start an attested app on a private desktop")
    start.add_argument("--target", choices=("only_chat_qa", "pomodoro", "ai_account_router_qa"),
                       default="only_chat_qa")
    start.add_argument("--owner", help="Stable AI/chat owner ID; requires an isolated app profile")
    start.add_argument("--outputs", type=int, choices=(0,), default=0,
                       help="Private workspace never attaches host VDD outputs (always0)")
    start.add_argument("--profile", choices=("private",), default="private")
    start.add_argument("--confirm", action="store_true", required=True,
                       help="Explicitly authorize this isolated QA session start")
    observe = task_sub.add_parser("observe", help="Observe a manager-owned task session")
    observe.add_argument("--session", required=True)
    observe.add_argument("--owner", help="Owner ID used to start this session")
    stop = task_sub.add_parser("stop", help="Stop a task session and verify cleanup")
    stop.add_argument("--session", required=True)
    stop.add_argument("--owner", help="Owner ID used to start this session")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.group == "status":
            command, payload = "status", {}
        elif args.group == "ui":
            command, payload = f"ui.{args.action}", {}
        elif args.group == "logs":
            command, payload = "logs", {"limit": 5000 if args.all else args.limit}
        elif args.group in {"screens", "windows"}:
            command, payload = args.group, {}
        elif args.group == "stardesk":
            command, payload = "stardesk.status", {}
        elif args.group == "task":
            command = f"task.{args.action}"
            if args.action == "status":
                payload = {}
            elif args.action == "start":
                payload = {"confirmed": args.confirm, "output_count": args.outputs,
                           "profile": args.profile}
                if args.target != "only_chat_qa":
                    payload["target_app_id"] = args.target
            else:
                payload = {"session_id": args.session}
        elif args.action == "preview":
            command, payload = "outputs.preview", {"count": args.count, "profile": args.profile}
        elif args.action == "apply":
            command, payload = "outputs.apply", {"plan_id": args.plan, "confirm": args.confirm}
        elif args.action == "return-preview":
            command, payload = "outputs.return_preview", {}
        else:
            command, payload = "outputs.return", {"plan_id": args.plan, "confirm": args.confirm}
        if args.group == "task" and args.owner is not None:
            payload["owner_id"] = args.owner
        response = send_request(command, payload,
                                timeout_ms=pipe_wait_timeout_ms(command))
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error_code": type(exc).__name__,
                          "message": str(exc)[:300]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
