"""Read-only active output and clone-path inventory using Windows display APIs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

API_VERSION = 1
MODULE_ID = "display_topology"
VERSION = "0.1.3"
ACTIONS = ("apply_mtt_mode",)

QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
ERROR_INSUFFICIENT_BUFFER = 122


def is_primary_display(state_flags):
    """Return whether EnumDisplayDevices marks this attached adapter primary."""
    return bool(int(state_flags) & DISPLAY_DEVICE_PRIMARY_DEVICE)


class _DisplayDeviceW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class _PointL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _DevModeW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPosition", _PointL), ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD), ("dmColor", wintypes.SHORT),
                ("dmDuplex", wintypes.SHORT), ("dmYResolution", wintypes.SHORT),
                ("dmTTOption", wintypes.SHORT), ("dmCollate", wintypes.SHORT),
                ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD),
                ("dmPanningHeight", wintypes.DWORD)]


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _Rational(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.DWORD), ("Denominator", wintypes.DWORD)]


class _PathSourceInfo(ctypes.Structure):
    _fields_ = [("adapterId", _Luid), ("id", wintypes.DWORD),
                ("modeInfoIdx", wintypes.DWORD), ("statusFlags", wintypes.DWORD)]


class _PathTargetInfo(ctypes.Structure):
    _fields_ = [("adapterId", _Luid), ("id", wintypes.DWORD),
                ("modeInfoIdx", wintypes.DWORD), ("outputTechnology", wintypes.DWORD),
                ("rotation", wintypes.DWORD), ("scaling", wintypes.DWORD),
                ("refreshRate", _Rational), ("scanLineOrdering", wintypes.DWORD),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.DWORD)]


class _PathInfo(ctypes.Structure):
    _fields_ = [("sourceInfo", _PathSourceInfo), ("targetInfo", _PathTargetInfo),
                ("flags", wintypes.DWORD)]


class _DeviceInfoHeader(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("size", wintypes.DWORD),
                ("adapterId", _Luid), ("id", wintypes.DWORD)]


class _SourceDeviceName(ctypes.Structure):
    _fields_ = [("header", _DeviceInfoHeader), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class _TargetDeviceName(ctypes.Structure):
    _fields_ = [("header", _DeviceInfoHeader), ("flags", wintypes.DWORD),
                ("outputTechnology", wintypes.DWORD), ("edidManufactureId", wintypes.WORD),
                ("edidProductCodeId", wintypes.WORD), ("connectorInstance", wintypes.DWORD),
                ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


class DisplayTopologyModule:
    def __init__(self):
        self._last = {"displays": [], "topology_verified": False, "error": "Not queried"}

    def init(self):
        pass

    def snapshot(self):
        return {"displays": [dict(d) for d in self._last["displays"]],
                "topology_verified": self._last["topology_verified"], "error": self._last["error"]}

    def restore(self, state):
        if isinstance(state, dict) and "displays" in state:
            self._last = state

    def _enumerate_adapters(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_devices = user32.EnumDisplayDevicesW
        enum_devices.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DisplayDeviceW), wintypes.DWORD]
        enum_devices.restype = wintypes.BOOL
        enum_modes = user32.EnumDisplaySettingsW
        enum_modes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DevModeW)]
        enum_modes.restype = wintypes.BOOL
        result = []
        index = 0
        while True:
            adapter = _DisplayDeviceW()
            adapter.cb = ctypes.sizeof(adapter)
            if not enum_devices(None, index, ctypes.byref(adapter), 0):
                break
            index += 1
            if not adapter.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                continue
            mode = _DevModeW()
            mode.dmSize = ctypes.sizeof(mode)
            has_mode = bool(enum_modes(adapter.DeviceName, 0xFFFFFFFF, ctypes.byref(mode)))
            supported_modes = []
            for mode_index in range(256):
                candidate = _DevModeW()
                candidate.dmSize = ctypes.sizeof(candidate)
                if not enum_modes(adapter.DeviceName, mode_index, ctypes.byref(candidate)):
                    break
                dims = (int(candidate.dmPelsWidth), int(candidate.dmPelsHeight),
                        int(candidate.dmDisplayFrequency))
                if (0 < dims[0] <= 16384 and 0 < dims[1] <= 16384
                        and 1 <= dims[2] <= 1000 and dims not in supported_modes):
                    supported_modes.append(dims)
            monitor = _DisplayDeviceW()
            monitor.cb = ctypes.sizeof(monitor)
            enum_devices(adapter.DeviceName, 0, ctypes.byref(monitor), 0)
            result.append({"name": monitor.DeviceString or adapter.DeviceName,
                           "display_name": adapter.DeviceName, "adapter": adapter.DeviceString,
                           "monitor_device_id": monitor.DeviceID, "adapter_device_id": adapter.DeviceID,
                           "width": mode.dmPelsWidth if has_mode else None,
                           "height": mode.dmPelsHeight if has_mode else None,
                           "available_modes": supported_modes,
                           "x": mode.dmPosition.x if has_mode else None,
                           "y": mode.dmPosition.y if has_mode else None,
                           "refresh_rate": mode.dmDisplayFrequency if has_mode else None,
                           "primary": is_primary_display(adapter.StateFlags),
                           "attached": True,
                           "mirroring_driver": bool(adapter.StateFlags & DISPLAY_DEVICE_MIRRORING_DRIVER),
                           "virtual": None, "clone_source_count": None,
                           "topology_verified": False, "monitor_path": "", "target_name": ""})
        return result

    @staticmethod
    def _mode_target(displays, *, display_name, monitor_device_id, monitor_path,
                     monitor_parent, mtt_instance_id, mode, topology_verified):
        if topology_verified is not True:
            raise RuntimeError("Active display topology is not verified")
        if not all(isinstance(value, str) and value.strip() for value in
                   (display_name, monitor_device_id, monitor_path, monitor_parent, mtt_instance_id)):
            raise ValueError("Exact virtual-output identity fields are required")
        if monitor_parent.strip().casefold() != mtt_instance_id.strip().casefold():
            raise PermissionError("The selected monitor is not a child of the verified MTT device")
        matches = [row for row in displays if row.get("display_name", "").casefold() == display_name.casefold()]
        if len(matches) != 1:
            raise RuntimeError("The selected display source is missing or ambiguous")
        row = matches[0]
        if (str(row.get("monitor_device_id", "")).casefold() != monitor_device_id.casefold()
                or str(row.get("monitor_path", "")).casefold() != monitor_path.casefold()
                or row.get("topology_verified") is not True or row.get("cloned") is not False):
            raise RuntimeError("The active virtual-output endpoint changed or is cloned")
        if not isinstance(mode, (list, tuple)) or len(mode) != 3:
            raise ValueError("Requested display mode is invalid")
        try:
            requested = tuple(int(value) for value in mode)
        except (TypeError, ValueError):
            raise ValueError("Requested display mode is invalid") from None
        if (any(isinstance(value, bool) for value in mode)
                or requested not in {tuple(item) for item in row.get("available_modes", [])}):
            raise ValueError("The exact display mode is not currently supported by this endpoint")
        return row, requested

    def apply_mtt_mode(self, *, display_name, monitor_device_id, monitor_path,
                       monitor_parent, mtt_instance_id, mode, confirmed=False):
        """Set only resolution/refresh on an exact, uncloned MTT endpoint."""
        if confirmed is not True:
            raise PermissionError("An explicit in-app confirmation is required")
        snapshot = self.refresh()
        row, requested = self._mode_target(
            snapshot["displays"], display_name=display_name,
            monitor_device_id=monitor_device_id, monitor_path=monitor_path,
            monitor_parent=monitor_parent, mtt_instance_id=mtt_instance_id,
            mode=mode, topology_verified=snapshot["topology_verified"],
        )
        original = (int(row["width"]), int(row["height"]), int(row["refresh_rate"]))
        if requested == original:
            return {"state": "unchanged", "display_name": display_name, "mode": list(original)}
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows display-mode changes are available on Windows only")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        change = user32.ChangeDisplaySettingsExW
        change.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_DevModeW), wintypes.HWND,
                           wintypes.DWORD, ctypes.c_void_p]
        change.restype = wintypes.LONG
        dm_pels_width, dm_pels_height, dm_display_frequency = 0x00080000, 0x00100000, 0x00400000
        cds_test = 0x00000002

        def make_mode(values):
            devmode = _DevModeW()
            devmode.dmSize = ctypes.sizeof(devmode)
            devmode.dmFields = dm_pels_width | dm_pels_height | dm_display_frequency
            devmode.dmPelsWidth, devmode.dmPelsHeight, devmode.dmDisplayFrequency = values
            return devmode

        test_mode = make_mode(requested)
        if change(display_name, ctypes.byref(test_mode), None, cds_test, None) != 0:
            raise RuntimeError("Windows rejected the requested virtual-output mode during test")
        applied_mode = make_mode(requested)
        if change(display_name, ctypes.byref(applied_mode), None, 0, None) != 0:
            raise RuntimeError("Windows could not apply the requested virtual-output mode")
        updated = self.refresh()
        refreshed, _ = self._mode_target(
            updated["displays"], display_name=display_name,
            monitor_device_id=monitor_device_id, monitor_path=monitor_path,
            monitor_parent=monitor_parent, mtt_instance_id=mtt_instance_id,
            mode=requested, topology_verified=updated["topology_verified"],
        )
        actual = (int(refreshed["width"]), int(refreshed["height"]), int(refreshed["refresh_rate"]))
        if actual != requested:
            rollback = make_mode(original)
            change(display_name, ctypes.byref(rollback), None, 0, None)
            raise RuntimeError("Applied mode did not verify; the previous mode was restored where possible")
        return {"state": "applied", "display_name": display_name,
                "monitor_path": monitor_path, "previous_mode": list(original),
                "mode": list(requested)}

    def _query_active_paths(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_sizes = user32.GetDisplayConfigBufferSizes
        get_sizes.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        get_sizes.restype = wintypes.LONG
        query = user32.QueryDisplayConfig
        query.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_PathInfo),
                          ctypes.POINTER(wintypes.UINT), ctypes.c_void_p, ctypes.c_void_p]
        query.restype = wintypes.LONG
        get_info = user32.DisplayConfigGetDeviceInfo
        get_info.argtypes = [ctypes.POINTER(_DeviceInfoHeader)]
        get_info.restype = wintypes.LONG

        for _ in range(3):
            path_count, mode_count = wintypes.UINT(), wintypes.UINT()
            status = get_sizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count))
            if status:
                raise OSError(status, "GetDisplayConfigBufferSizes failed")
            paths = (_PathInfo * max(1, path_count.value))()
            modes = (ctypes.c_byte * max(1, mode_count.value * 64))()
            actual_paths, actual_modes = wintypes.UINT(path_count.value), wintypes.UINT(mode_count.value)
            status = query(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(actual_paths), paths,
                           ctypes.byref(actual_modes), ctypes.cast(modes, ctypes.c_void_p), None)
            if status == ERROR_INSUFFICIENT_BUFFER:
                continue
            if status:
                raise OSError(status, "QueryDisplayConfig failed")
            active = list(paths[:actual_paths.value])
            source_counts = {}
            for path in active:
                src = path.sourceInfo
                key = (src.adapterId.LowPart, src.adapterId.HighPart, src.id)
                source_counts[key] = source_counts.get(key, 0) + 1
            results = []
            for path in active:
                src, target = path.sourceInfo, path.targetInfo
                key = (src.adapterId.LowPart, src.adapterId.HighPart, src.id)
                src_name = _SourceDeviceName()
                src_name.header = _DeviceInfoHeader(1, ctypes.sizeof(src_name), src.adapterId, src.id)
                source_ok = get_info(ctypes.byref(src_name.header)) == 0
                target_name = _TargetDeviceName()
                target_name.header = _DeviceInfoHeader(2, ctypes.sizeof(target_name), target.adapterId, target.id)
                target_ok = get_info(ctypes.byref(target_name.header)) == 0
                results.append({"display_name": src_name.viewGdiDeviceName if source_ok else "",
                                "clone_source_count": source_counts[key],
                                "cloned": source_counts[key] > 1,
                                "monitor_path": target_name.monitorDevicePath if target_ok else "",
                                "target_name": target_name.monitorFriendlyDeviceName if target_ok else "",
                                "verified": bool(source_ok and target_ok)})
            return results
        raise OSError(ERROR_INSUFFICIENT_BUFFER, "Display topology kept changing during read")

    def refresh(self):
        if not hasattr(ctypes, "WinDLL"):
            self._last = {"displays": [], "topology_verified": False, "error": "Windows APIs unavailable"}
            return self.snapshot()
        displays = self._enumerate_adapters()
        try:
            paths = self._query_active_paths()
            by_source = {p["display_name"].casefold(): p for p in paths if p["display_name"]}
            display_sources = {d["display_name"].casefold() for d in displays}
            verified = (bool(paths) and all(p["verified"] for p in paths)
                        and {p["display_name"].casefold() for p in paths if p["display_name"]} <= display_sources)
            for display in displays:
                path = by_source.get(display["display_name"].casefold())
                if path:
                    display.update(path)
                    display["topology_verified"] = path["verified"]
            self._last = {"displays": displays, "topology_verified": verified, "error": "" if verified else "Some active display paths could not be matched"}
        except Exception as exc:
            self._last = {"displays": displays, "topology_verified": False,
                          "error": f"{type(exc).__name__}: {exc}"}
        return self.snapshot()

    def action(self, name, payload):
        if name != "apply_mtt_mode":
            raise ValueError("Unknown display-topology action")
        return self.apply_mtt_mode(
            display_name=payload.get("display_name"),
            monitor_device_id=payload.get("monitor_device_id"),
            monitor_path=payload.get("monitor_path"),
            monitor_parent=payload.get("monitor_parent"),
            mtt_instance_id=payload.get("mtt_instance_id"),
            mode=payload.get("mode"),
            confirmed=payload.get("confirmed") is True,
        )

    def dispose(self):
        pass


def create():
    return DisplayTopologyModule()
