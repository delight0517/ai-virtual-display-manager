"""Explicitly route only supported AI app windows to a verified MTT output."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import time
from ctypes import wintypes
from core.win32_dpi import physical_pixel_context

API_VERSION = 1
MODULE_ID = "app_window_router"
VERSION = "0.1.20"
ACTIONS = ("list_windows", "move_window", "restore_window", "count_virtual_output_windows",
           "physical_guard_start", "physical_guard_stop", "physical_guard_status")

SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_NOOWNERZORDER = 0x0200
SWP_NOSENDCHANGING = 0x0400
SWP_FLAGS = SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING | 0x4000  # SWP_ASYNCWINDOWPOS: never wait on a busy user UI thread
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOT = 2
GW_OWNER = 4
ERROR_ACCESS_DENIED = 5
HWND_NOT_FOUND = 1400
def classify_app_path(path: str) -> str | None:
    """Return an app label only for the two deliberately supported installs."""
    normalized = os.path.normcase(os.path.abspath(str(path or ""))).replace("/", "\\")
    folded = normalized.casefold()
    if (folded.endswith("\\only chat.exe")
            and "\\appdata\\local\\programs\\onlychat-electron-" in folded):
        return "Only Chat"
    if (folded.endswith("\\app\\chatgpt.exe")
            and "\\program files\\windowsapps\\openai.codex_" in folded):
        return "Codex"
    return None


def _identity(pid: int, created: str, hwnd: int, class_name: str, path: str) -> str:
    material = f"{int(pid)}|{created}|{int(hwnd)}|{class_name}|{os.path.normcase(path).casefold()}"
    return hashlib.sha256(material.encode("utf-8", "strict")).hexdigest()


def _is_unowned_root_window(user32, hwnd: int) -> bool:
    get_ancestor = user32.GetAncestor
    get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
    get_ancestor.restype = wintypes.HWND
    get_window = user32.GetWindow
    get_window.argtypes = [wintypes.HWND, wintypes.UINT]
    get_window.restype = wintypes.HWND
    return get_ancestor(hwnd, GA_ROOT) == hwnd and not get_window(hwnd, GW_OWNER)


class _WinPoint(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _WinRect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _WindowPlacement(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", _WinPoint),
                ("ptMaxPosition", _WinPoint), ("rcNormalPosition", _WinRect)]


class Win32WindowAPI:
    """Small, read-only-enumeration and nonactivating-placement Win32 adapter."""

    RECT = _WinRect

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    WINDOWPLACEMENT = _WindowPlacement

    def __init__(self, user32=None, kernel32=None):
        if user32 is None:
            if not hasattr(ctypes, "WinDLL"):
                raise OSError("AI window routing is available on Windows only")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = user32
        self.kernel32 = kernel32
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindowPlacement.argtypes = [
            wintypes.HWND, ctypes.POINTER(self.WINDOWPLACEMENT)]
        self.user32.GetWindowPlacement.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(self.FILETIME), ctypes.POINTER(self.FILETIME),
            ctypes.POINTER(self.FILETIME), ctypes.POINTER(self.FILETIME)]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def enumerate_windows(self):
        with physical_pixel_context(self.user32):
            return self._enumerate_windows_physical(False)

    def enumerate_all_windows(self):
        """Enumerate visible top-level window metadata, without titles or content."""
        with physical_pixel_context(self.user32):
            return self._enumerate_windows_physical(True)

    def _enumerate_windows_physical(self, include_all=False):
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        foreground = self.user32.GetForegroundWindow()
        enum_windows = self.user32.EnumWindows
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        enum_windows.argtypes = [callback_type, wintypes.LPARAM]
        enum_windows.restype = wintypes.BOOL
        is_visible = self.user32.IsWindowVisible
        is_visible.argtypes = [wintypes.HWND]
        is_visible.restype = wintypes.BOOL
        is_iconic = self.user32.IsIconic
        is_iconic.argtypes = [wintypes.HWND]
        is_iconic.restype = wintypes.BOOL
        get_pid = self.user32.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD
        get_rect = self.user32.GetWindowRect
        get_rect.argtypes = [wintypes.HWND, ctypes.POINTER(self.RECT)]
        get_rect.restype = wintypes.BOOL
        get_class = self.user32.GetClassNameW
        get_class.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        get_class.restype = ctypes.c_int
        rows = []

        def unknown(hwnd, pid=0):
            if include_all:
                rows.append({"pid": int(pid or 0), "hwnd": int(hwnd), "bounds": None,
                             "window_state": "unknown", "bounds_space": "unknown"})

        def collect(hwnd, _lparam):
            try:
                if not is_visible(hwnd):
                    return True
                minimized = bool(is_iconic(hwnd))
                pid = wintypes.DWORD()
                if not get_pid(hwnd, ctypes.byref(pid)) or not pid.value:
                    unknown(hwnd)
                    return True
                process = self.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value,
                )
                if not process:
                    unknown(hwnd, pid.value)
                    return True
                try:
                    path_buf = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(path_buf))
                    if not self.kernel32.QueryFullProcessImageNameW(
                            process, 0, path_buf, ctypes.byref(length)):
                        unknown(hwnd, pid.value)
                        return True
                    path = path_buf.value
                    app = classify_app_path(path)
                    if not include_all and not app:
                        return True
                    created, exited, kernel, user = (self.FILETIME(), self.FILETIME(),
                                                     self.FILETIME(), self.FILETIME())
                    if not self.kernel32.GetProcessTimes(
                            process, ctypes.byref(created), ctypes.byref(exited),
                            ctypes.byref(kernel), ctypes.byref(user)):
                        unknown(hwnd, pid.value)
                        return True
                    class_buf = ctypes.create_unicode_buffer(256)
                    class_length = get_class(hwnd, class_buf, len(class_buf))
                    class_name = class_buf.value[:class_length]
                    if not include_all and not _is_unowned_root_window(self.user32, hwnd):
                        return True
                    if include_all and self.user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
                        return True
                    # Keep the manual AI-app route narrowly class/path allowlisted.
                    if not include_all and class_name != "Chrome_WidgetWin_1":
                        return True
                    if minimized:
                        placement = self.WINDOWPLACEMENT()
                        placement.length = ctypes.sizeof(placement)
                        if not self.user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                            unknown(hwnd, pid.value)
                            return True
                        rect = placement.rcNormalPosition
                        window_state = "minimized"
                    else:
                        rect = self.RECT()
                        if not get_rect(hwnd, ctypes.byref(rect)):
                            unknown(hwnd, pid.value)
                            return True
                        window_state = "normal"
                    bounds = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
                    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                        unknown(hwnd, pid.value)
                        return True
                    restore_evidence = None
                    if minimized and include_all:
                        from modules.app_window_router.restore_bounds import resolve_restore_bounds
                        restore_evidence = resolve_restore_bounds(self.user32, int(hwnd), bounds)
                    creation = (int(created.high) << 32) | int(created.low)
                    rows.append({"window_id": _identity(pid.value, str(creation), hwnd,
                                                         class_name, path),
                                 "app": app or "Application", "pid": int(pid.value), "hwnd": int(hwnd),
                                 "process_created": str(creation), "class_name": class_name,
                                 "exe_path": path, "bounds": bounds,
                                 "foreground": int(hwnd) == int(foreground or 0),
                                 "window_state": window_state,
                                 # WINDOWPLACEMENT.rcNormalPosition is in workspace
                                 # coordinates for ordinary top-level windows. Until
                                 # we can prove its monitor-specific conversion, never
                                 # compare it with screen-space display rectangles.
                                 "bounds_space": "workspace" if minimized else "screen",
                                 "restore_bounds_evidence": restore_evidence})
                finally:
                    self.kernel32.CloseHandle(process)
            except Exception:
                # One inaccessible or redrawn unrelated window must not abort the scan.
                unknown(hwnd)
                return True
            return True

        callback = callback_type(collect)
        ctypes.set_last_error(0)
        if not enum_windows(callback, 0):
            error = ctypes.get_last_error()
            if error != 0:
                raise OSError(error, "EnumWindows failed")
        return rows

    def move_without_activation(self, hwnd: int, bounds: list[int]) -> bool:
        with physical_pixel_context(self.user32):
            self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                                 ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_int, wintypes.UINT]
            self.user32.SetWindowPos.restype = wintypes.BOOL
            x, y, width, height = map(int, bounds)
            return bool(self.user32.SetWindowPos(
                wintypes.HWND(int(hwnd)), None, x, y, width, height, SWP_FLAGS,
            ))

    def move_exact_without_activation(self, expected: dict, bounds: list[int]) -> bool:
        """Revalidate PID/process identity/HWND/bounds immediately before placement."""
        if expected.get("window_state") == "minimized":
            return False
        enumerate_current = (self.enumerate_all_windows
                             if expected.get("app") == "Application"
                             else self.enumerate_windows)
        current = [row for row in enumerate_current()
                   if row.get("hwnd") == expected.get("hwnd")
                   and row.get("window_id") == expected.get("window_id")
                   and row.get("bounds") == expected.get("bounds")
                   and row.get("window_state") == expected.get("window_state")]
        if len(current) != 1:
            return False
        left, top, right, bottom = map(int, bounds)
        return self.move_without_activation(int(current[0]["hwnd"]),
                                            [left, top, right - left, bottom - top])

    def restore_exact(self, expected: dict) -> bool:
        """Restore only the same freshly revalidated minimized top-level window."""
        if expected.get("window_state") != "minimized":
            return False
        current = [row for row in self.enumerate_windows()
                   if row.get("window_id") == expected.get("window_id")
                   and row.get("hwnd") == expected.get("hwnd")
                   and row.get("pid") == expected.get("pid")
                   and row.get("process_created") == expected.get("process_created")
                   and row.get("class_name") == expected.get("class_name")
                   and os.path.normcase(str(row.get("exe_path", ""))).casefold()
                       == os.path.normcase(str(expected.get("exe_path", ""))).casefold()
                   and row.get("window_state") == "minimized"]
        if len(current) != 1:
            return False
        show = self.user32.ShowWindowAsync
        show.argtypes = [wintypes.HWND, ctypes.c_int]
        show.restype = wintypes.BOOL
        if not show(wintypes.HWND(int(current[0]["hwnd"])), SW_RESTORE):
            return False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            after = [row for row in self.enumerate_windows()
                     if row.get("window_id") == expected.get("window_id")
                     and row.get("hwnd") == expected.get("hwnd")
                     and row.get("pid") == expected.get("pid")
                     and row.get("process_created") == expected.get("process_created")]
            if len(after) != 1:
                return False
            if after[0].get("window_state") != "minimized":
                return True
            time.sleep(0.05)
        return False


def current_displays():
    from core.module_host import ModuleHost
    from core.paths import APP_ROOT
    host = ModuleHost(APP_ROOT)
    host.load("display_topology")
    try:
        return host.refresh("display_topology")
    finally:
        host.dispose_all()


def current_guard_topology(*, nonblocking=False):
    """Join active display paths with present PnP monitor identity; fail closed."""
    from core.module_host import ModuleHost
    from core.paths import APP_ROOT
    host = ModuleHost(APP_ROOT)
    host.load("display_topology")
    host.load("mtt_vdd", APP_ROOT / "modules" / "mtt_vdd" / "module.py")
    try:
        topology = host.refresh("display_topology")
        status = host._loaded["mtt_vdd"].instance.refresh(nonblocking=nonblocking)
        topology["monitor_identity_age_ms"] = status.get("status_age_ms")
        topology["monitor_identity_cache_hit"] = status.get("status_cache_hit")
        topology["monitor_identity_refresh_state"] = status.get("status_refresh_state", "fresh")
        if nonblocking and topology["monitor_identity_refresh_state"] != "fresh":
            # An expired PnP snapshot can describe a monitor that was removed or
            # replaced while the background query is running. Keep the display
            # paths for diagnostics, but never let stale identity authorize a move.
            topology["topology_verified"] = False
            topology["monitor_identity_error_type"] = status.get("status_refresh_error_type")
            for display in topology.get("displays", []):
                display["monitor_identity_verified"] = False
                display["virtual"] = None
                display["monitor_parent"] = ""
            return topology
        monitors = status.get("monitors", [])
        by_key = {}
        for monitor in monitors:
            key = _pnp_monitor_key(monitor.get("InstanceId", ""))
            if key:
                by_key.setdefault(key, []).append(monitor)
        device_instance_id = str(status.get("device", {}).get("instance_id", ""))
        topology["mtt_device_instance_id"] = device_instance_id
        for display in topology.get("displays", []):
            key = _display_path_monitor_key(display.get("monitor_path", ""))
            matches = by_key.get(key, [])
            monitor = matches[0] if len(matches) == 1 else None
            display["monitor_identity_verified"] = bool(monitor)
            display["virtual"] = monitor.get("Virtual") if monitor else None
            display["monitor_parent"] = monitor.get("Parent", "") if monitor else ""
        return topology
    finally:
        host.dispose_all()


def _pnp_monitor_key(instance_id: str) -> str:
    parts = [part.strip().casefold() for part in str(instance_id or "").split("\\")]
    if len(parts) < 3 or parts[0] != "display" or not all(parts[:3]):
        return ""
    return "#".join(parts[:3])


def _display_path_monitor_key(path: str) -> str:
    value = str(path or "").strip()
    if value.startswith("\\\\?\\"):
        value = value[4:]
    value = value.split("#{", 1)[0].strip("\\")
    parts = [part.strip().casefold() for part in value.split("#")]
    if len(parts) < 3 or parts[0] != "display" or not all(parts[:3]):
        return ""
    return "#".join(parts[:3])


def verified_mtt_outputs(topology: dict) -> list[dict]:
    if not isinstance(topology, dict) or not topology.get("topology_verified"):
        return []
    displays = topology.get("displays") or []
    mtt_instance = str(topology.get("mtt_device_instance_id", "")).strip().casefold()
    if not mtt_instance:
        return []
    outputs = [dict(display) for display in displays
               if display.get("virtual") is True
               and display.get("monitor_identity_verified") is True
               and str(display.get("monitor_parent", "")).strip().casefold() == mtt_instance
               and display.get("attached") is True
               and display.get("topology_verified") is True
               and display.get("verified") is True
               and display.get("cloned") is False
               and display.get("width") and display.get("height")
               and display.get("x") is not None and display.get("y") is not None]
    if not outputs:
        return []
    # Multiple MTT outputs are supported when every active source and monitor
    # path resolves to one distinct endpoint. Ambiguous duplicate paths fail closed.
    names = [str(output.get("display_name", "")).casefold() for output in outputs]
    paths = [str(output.get("monitor_path", "")).casefold() for output in outputs]
    if (any(not value for value in names + paths)
            or len(set(names)) != len(names) or len(set(paths)) != len(paths)):
        return []
    return outputs


def _display_for_window(window: dict, displays: list[dict]) -> str:
    if window.get("window_state") == "minimized" or window.get("bounds_space") == "workspace":
        return ""
    matches = [d for d in displays
               if _bounds_fully_within_output(window["bounds"], d)]
    return str(matches[0].get("display_name", "")) if len(matches) == 1 else ""


def _bounds_fully_within_output(bounds: list[int], output: dict) -> bool:
    left, top, right, bottom = map(int, bounds)
    x, y = int(output["x"]), int(output["y"])
    return (left >= x and top >= y
            and right <= x + int(output["width"])
            and bottom <= y + int(output["height"]))


def _same_output_identity(current: dict, expected: dict) -> bool:
    for key in ("display_name", "adapter_device_id"):
        current_value = str(current.get(key, "")).strip().casefold()
        expected_value = str(expected.get(key, "")).strip().casefold()
        if not current_value or not expected_value or current_value != expected_value:
            return False
    for key in ("target_name", "monitor_path"):
        current_value = str(current.get(key, "")).strip().casefold()
        expected_value = str(expected.get(key, "")).strip().casefold()
        if bool(current_value) != bool(expected_value):
            return False
        if current_value and current_value != expected_value:
            return False
    return True


class AppWindowRouter:
    def __init__(self, window_api=None, display_provider=None, guard_settings_store_factory=None):
        self.window_api = window_api or Win32WindowAPI()
        # Every routing/activity operation joins active paths to present PnP
        # monitor identity before classifying a path as an MTT virtual output.
        self.display_provider = display_provider or current_guard_topology
        # Only the long-running physical-window guard uses the async PnP path.
        # One-shot routing and display mutations continue to demand a fresh read.
        self.guard_display_provider = (display_provider if display_provider is not None
                                       else lambda: current_guard_topology(nonblocking=True))
        self.guard_settings_store_factory = guard_settings_store_factory
        self.guard = None
        self.cursor_guard = None
        self.diagnostic_recorder = None
        self._last_guard_preference = None

    def bind_diagnostics(self, recorder):
        if not callable(recorder):
            raise TypeError("diagnostic recorder must be callable")
        self.diagnostic_recorder = recorder

    def _record_diagnostic(self, event):
        if not callable(self.diagnostic_recorder):
            return
        try:
            self.diagnostic_recorder(event)
        except Exception as exc:
            self._diagnostic_error = type(exc).__name__

    def _record_guard_preference(self, enabled):
        enabled = bool(enabled)
        if self._last_guard_preference is enabled:
            return
        self._last_guard_preference = enabled
        self._record_diagnostic({"event": "guard_state",
                                 "setting": "physical_window_guard_enabled",
                                 "enabled": enabled, "result": "unknown"})

    def init(self):
        pass

    def dispose(self):
        if self.cursor_guard:
            try:
                self.cursor_guard.stop()
            except Exception:
                pass
            self.cursor_guard = None
        if self.guard:
            state = self.guard.stop()
            if state.get("enabled"):
                raise RuntimeError("Physical-window guard stop is unverified; module remains active")

    def snapshot(self):
        return {"last_selected_window_id": ""}

    def restore(self, _state):
        pass

    def refresh(self):
        # Loading or hidden self-check must not enumerate user windows.
        return {"feature": "allowlisted_ai_window_placement",
                "window_enumeration": "on_explicit_dialog_open_only"}

    def _inventory(self, require_virtual_output: bool = True):
        topology = self.display_provider()
        if require_virtual_output and not topology.get("topology_verified"):
            raise RuntimeError("화면 출력 경로를 확인하지 못했습니다. 창은 이동하지 않았습니다.")
        outputs = verified_mtt_outputs(topology)
        if require_virtual_output and not outputs:
            raise RuntimeError("복제되지 않은 MTT 가상 출력 하나를 확인하지 못했습니다. 창은 이동하지 않았습니다.")
        windows = self.window_api.enumerate_windows()
        displays = topology.get("displays", []) if topology.get("topology_verified") else []
        public_windows = []
        for win in windows:
            public_windows.append({"window_id": win["window_id"], "app": win["app"],
                                  "pid": win["pid"], "bounds": list(win["bounds"]),
                                  "window_state": win.get("window_state", "normal"),
                                  "display_name": _display_for_window(win, displays)})
        public_outputs = [{"display_name": output["display_name"],
                           "target_name": output.get("target_name") or "MTT Virtual Display",
                           "bounds": [int(output["x"]), int(output["y"]),
                                      int(output["width"]), int(output["height"])],
                           "width": int(output["width"]), "height": int(output["height"])}
                          for output in outputs]
        return topology, outputs, windows, public_windows, public_outputs

    def action(self, name: str, payload: dict):
        if name == "list_windows":
            _topology, _outputs, _windows, public_windows, public_outputs = self._inventory(
                require_virtual_output=False)
            return {"windows": public_windows, "virtual_outputs": public_outputs,
                    "movement_requires_confirmation": True,
                    "restoration_requires_confirmation": True}
        if name == "move_window":
            return self.move_window(payload)
        if name == "restore_window":
            return self.restore_window(payload)
        if name == "count_virtual_output_windows":
            return self.count_virtual_output_windows()
        if name in {"physical_guard_start", "physical_guard_stop", "physical_guard_status"}:
            return self.physical_guard_action(name, payload)
        raise ValueError("Unknown window-routing action")

    def count_virtual_output_windows(self):
        """Count visible, non-minimized windows for idle cleanup without identity/content."""
        topology = self.display_provider()
        if not topology.get("topology_verified"):
            raise RuntimeError("Display topology is not verified; virtual outputs stay active")
        outputs = verified_mtt_outputs(topology)
        if not outputs:
            raise RuntimeError("No uncloned MTT output is verified; virtual outputs stay active")
        windows = self.window_api.enumerate_all_windows()
        manager_pid = os.getpid()
        count = 0
        for window in windows:
            if int(window.get("pid", 0)) == manager_pid:
                continue
            if (window.get("class_name") in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"}
                    and os.path.normcase(str(window.get("exe_path", ""))) == os.path.normcase(
                        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe"))):
                continue
            bounds = window.get("bounds")
            # A minimized window is not visible output use. Active AI task leases
            # protect task-owned windows; minimized ordinary windows must not hold
            # the shared output pool open indefinitely. Unknown non-minimized
            # workspace geometry remains fail-closed below.
            if window.get("window_state") == "minimized":
                continue
            if window.get("bounds_space") == "workspace":
                count += 1
                continue
            try:
                left, top, right, bottom = map(int, bounds)
                if right <= left or bottom <= top:
                    raise ValueError("Invalid activity bounds")
            except (TypeError, ValueError, KeyError):
                count += 1
                continue
            if any(left < int(output["x"]) + int(output["width"])
                   and right > int(output["x"])
                   and top < int(output["y"]) + int(output["height"])
                   and bottom > int(output["y"]) for output in outputs):
                count += 1
        return {"active_window_count": count, "outputs_checked": len(outputs),
                "evidence": "bounds_only_no_titles_or_pixels"}

    def physical_guard_action(self, name, payload):
        from modules.app_window_router.guard_settings import GuardSettingsStore
        from modules.app_window_router.physical_guard import PhysicalWindowGuard
        from modules.app_window_router.cursor_guard import MouseCursorGuard
        store = (self.guard_settings_store_factory() if self.guard_settings_store_factory
                 else GuardSettingsStore())
        if name == "physical_guard_status":
            preference, error = store.read()
            self._record_guard_preference(preference)
            return {"preference_enabled": preference, "settings_error": error,
                    "runtime": self.guard.status() if self.guard else
                    {"enabled": False, "state": "off", "moved_count": 0, "error": ""}}
        if name == "physical_guard_start":
            if payload.get("confirmed") is not True:
                raise PermissionError("Explicit in-app confirmation is required")
            from modules.app_window_router.physical_guard import _diagnostic_topology, topology_ready
            try:
                topology = self.guard_display_provider()
            except Exception as exc:
                self._record_diagnostic({"event": "topology_state", "topology": "unknown",
                                         "output_count": 0, "result": "unavailable",
                                         "reason": "topology_provider_exception",
                                         "provider_error_type": type(exc).__name__
                                         if type(exc).__name__ in {"OSError", "TimeoutError", "RuntimeError", "ValueError"}
                                         else "Other"})
                self._record_diagnostic({"event": "guard_start_result", "result": "failed",
                                         "reason": "topology_provider_exception"})
                topology = {"topology_verified": False, "displays": [],
                            "provider_failure": True,
                            "provider_error_type": type(exc).__name__
                            if type(exc).__name__ in {"OSError", "TimeoutError", "RuntimeError", "ValueError"}
                            else "Other"}
            self._record_diagnostic({"event": "topology_state", **_diagnostic_topology(topology)})
            if not topology_ready(topology):
                self._record_diagnostic({"event": "guard_start_result", "result": "failed",
                                         "reason": "no_physical_output" if topology.get("topology_verified")
                                         else "topology_unverified"})
                # Arming is safe without a connected physical screen: the
                # worker waits without moving anything until identity verifies.
            try:
                if self.guard is None:
                    api = self.window_api
                    if not callable(getattr(api, "enumerate_all_windows", None)):
                        raise RuntimeError("Window metadata enumeration is unavailable")
                    self.guard = PhysicalWindowGuard(api, self.guard_display_provider,
                                                     diagnostic_recorder=self.diagnostic_recorder)
                state = self.guard.start(recover_existing=True)
                # Window recovery must not overwrite games' ClipCursor ownership.
            except Exception:
                self._record_diagnostic({"event": "guard_start_result", "result": "failed",
                                         "reason": "api_failed"})
                try:
                    store.write(False)
                except Exception:
                    pass
                if self.cursor_guard:
                    try:
                        self.cursor_guard.stop()
                    except Exception:
                        pass
                if self.guard and self.guard.status().get("enabled"):
                    self.guard.stop()
                raise
            if state.get("state") != "running":
                store.write(False)
                if self.cursor_guard:
                    try:
                        self.cursor_guard.stop()
                    except Exception:
                        pass
                raise RuntimeError("Physical-window guard did not start")
            store.write(True)
            self._record_guard_preference(True)
            self._record_diagnostic({"event": "guard_start_result", "result": "succeeded"})
            return {"preference_enabled": True, "runtime": state}
        preference, settings_error = store.read()
        if preference and not settings_error:
            try:
                store.write(False)  # Persist disabled before signaling the worker.
                self._record_guard_preference(False)
            except Exception as exc:
                settings_error = f"{type(exc).__name__}: preference could not be saved"
        if self.cursor_guard:
            try:
                self.cursor_guard.stop()
            except Exception:
                pass
            self.cursor_guard = None
        state = self.guard.stop() if self.guard else {
            "enabled": False, "state": "off", "moved_count": 0, "error": ""}
        self._record_diagnostic({"event": "guard_start_result",
                                 "result": "failed" if state.get("enabled") else "closed",
                                 "reason": "timeout" if state.get("enabled") else None})
        return {"preference_enabled": bool(preference and settings_error),
                "settings_error": settings_error, "runtime": state}

    def move_window(self, payload: dict):
        if payload.get("confirmed") is not True:
            raise PermissionError("Explicit in-app confirmation is required")
        selected_id = str(payload.get("window_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", selected_id):
            raise ValueError("Invalid or stale window selection")
        requested_display = str(payload.get("display_name") or "").casefold()
        if not requested_display:
            raise ValueError("A verified virtual output must be selected")
        topology, outputs, windows, _public_windows, _public_outputs = self._inventory()
        candidates = [win for win in windows if win.get("window_id") == selected_id]
        if len(candidates) != 1:
            raise RuntimeError("선택한 AI 창이 바뀌었거나 닫혔습니다. 목록을 새로 고치세요.")
        target = candidates[0]
        if not classify_app_path(target.get("exe_path", "")):
            raise PermissionError("This window is outside the supported AI app allowlist")
        matches = [display for display in outputs
                   if str(display.get("display_name", "")).casefold() == requested_display]
        if len(matches) != 1:
            raise RuntimeError("선택한 MTT 가상 출력이 더 이상 유일하게 검증되지 않습니다.")
        display = matches[0]
        if target.get("window_state") == "minimized":
            raise RuntimeError("최소화된 창의 복원 위치는 모니터 좌표로 확인되지 않습니다. 앱 창을 직접 복원한 뒤 다시 선택하세요.")
        left, top, right, bottom = map(int, target["bounds"])
        width, height = right - left, bottom - top
        display_left, display_top = int(display["x"]), int(display["y"])
        display_width, display_height = int(display["width"]), int(display["height"])
        margin = 32
        if width > display_width - margin * 2 or height > display_height - margin * 2:
            raise RuntimeError("선택한 창이 가상 출력 크기보다 큽니다. 크기는 변경하지 않았습니다.")
        destination = [display_left + margin, display_top + margin,
                       display_left + margin + width, display_top + margin + height]
        original = list(target["bounds"])
        move_exact = getattr(self.window_api, "move_exact_without_activation", None)
        if not callable(move_exact) or not move_exact(target, destination):
            raise RuntimeError("이동 직전 창 identity/위치가 바뀌었습니다. 창을 이동하지 않았습니다.")
        try:
            # Re-enumerate both the process identity and display path after placement.
            after_topology, after_outputs, after_windows, *_ = self._inventory()
            after = [win for win in after_windows if win.get("window_id") == selected_id]
            if len(after) != 1:
                raise RuntimeError("창 위치를 확인하는 동안 앱 창이 바뀌었습니다.")
            actual = list(map(int, after[0]["bounds"]))
            if actual != destination or not after_topology.get("topology_verified"):
                raise RuntimeError("이동 후 창 좌표 또는 화면 분리 상태가 달라 이동을 확인하지 못했습니다.")
            if len(after_outputs) != 1 or not _same_output_identity(after_outputs[0], display):
                raise RuntimeError("이동 후 가상 출력 경로를 다시 확인하지 못했습니다.")
        except Exception:
            # Never fall back to saved physical-screen coordinates. Restore only if
            # the exact original rectangle remains inside the same verified MTT path.
            try:
                current_topology = self.display_provider()
                current_outputs = verified_mtt_outputs(current_topology)
                exact_outputs = [item for item in current_outputs
                                 if _same_output_identity(item, display)]
                if (len(exact_outputs) == 1
                        and _bounds_fully_within_output(original, exact_outputs[0])):
                    current = [win for win in self.window_api.enumerate_windows()
                               if win.get("window_id") == selected_id
                               and win.get("hwnd") == target["hwnd"]]
                    if len(current) == 1:
                        self.window_api.move_without_activation(target["hwnd"], original)
            except Exception:
                # A failed verification is not permission to move to another output.
                pass
            raise
        return {"ok": True, "app": target["app"], "pid": target["pid"],
                "display_name": display["display_name"], "bounds": actual,
                "activated": False, "data_or_content_read": False}

    def restore_window(self, payload: dict):
        if payload.get("confirmed") is not True:
            raise PermissionError("Explicit in-app confirmation is required")
        selected_id = str(payload.get("window_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", selected_id):
            raise ValueError("Invalid or stale window selection")
        _topology, _outputs, windows, _public_windows, _public_outputs = self._inventory(
            require_virtual_output=False)
        candidates = [win for win in windows if win.get("window_id") == selected_id]
        if len(candidates) != 1:
            raise RuntimeError("선택한 AI 창이 바뀌었거나 닫혔습니다. 목록을 새로 고치세요.")
        target = candidates[0]
        if not classify_app_path(target.get("exe_path", "")):
            raise PermissionError("This window is outside the supported AI app allowlist")
        if target.get("window_state") != "minimized":
            raise RuntimeError("선택한 창이 더 이상 최소화 상태가 아닙니다. 목록을 새로 고치세요.")
        restore = getattr(self.window_api, "restore_exact", None)
        if not callable(restore) or not restore(target):
            raise RuntimeError("같은 AI 창이 정상 상태로 복원됐는지 확인하지 못했습니다.")
        after = [row for row in self.window_api.enumerate_windows()
                 if row.get("window_id") == selected_id
                 and row.get("hwnd") == target.get("hwnd")
                 and row.get("pid") == target.get("pid")]
        if len(after) != 1 or after[0].get("window_state") == "minimized":
            raise RuntimeError("복원 후 창 identity 또는 표시 상태를 확인하지 못했습니다.")
        topology = self.display_provider()
        displays = topology.get("displays", []) if topology.get("topology_verified") else []
        return {"ok": True, "app": target["app"], "pid": target["pid"],
                "window_state": after[0].get("window_state"),
                "display_name": _display_for_window(after[0], displays),
                "activated": True, "moved": False, "resized": False,
                "data_or_content_read": False}


def create():
    return AppWindowRouter()



