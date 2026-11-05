"""Status and explicit display-count controls for the installed MTT VDD."""
from __future__ import annotations

import json
import subprocess
import ctypes
import re
import os
import time
from pathlib import Path
from ctypes import wintypes
from core.mtt_status_cache import (STATUS_CACHE_MAX_AGE_SECONDS, get_status,
                                   get_status_nonblocking, invalidate)
from core.vdd_pipe import send, validate_command

API_VERSION = 1
MODULE_ID = "mtt_vdd"
VERSION = "0.1.7"
# Guard identity reads have a bounded longer freshness window because the
# present-monitor PowerShell scan can outlast the 5s general status cache. The
# guard still joins this snapshot to live display paths on every check.
GUARD_MONITOR_IDENTITY_MAX_AGE_SECONDS = 20.0
GUARD_MONITOR_IDENTITY_REFRESH_EARLY_SECONDS = 18.0

_STATUS_SCRIPT = r"""
$dev = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | Where-Object {
  $_.FriendlyName -match 'Virtual Display Driver' -and $_.Manufacturer -match 'MikeTheTech'
} | Select-Object -First 1
if($dev) {
  $props = @(Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_DriverVersion','DEVPKEY_Device_DriverInfPath','DEVPKEY_Device_Manufacturer' -ErrorAction SilentlyContinue)
  $driver = @{ DriverVersion=''; InfName=''; Manufacturer=$dev.Manufacturer }
  foreach($p in $props) { if($p.KeyName -match 'DriverVersion'){$driver.DriverVersion=$p.Data}; if($p.KeyName -match 'DriverInfPath'){$driver.InfName=$p.Data}; if($p.KeyName -match 'Manufacturer'){$driver.Manufacturer=$p.Data} }
  $device = [pscustomobject]@{Status=$dev.Status;Class=$dev.Class;FriendlyName=$dev.FriendlyName;InstanceId=$dev.InstanceId}
} else {
  $driver = @{ DriverVersion=''; InfName=''; Manufacturer=''; DeviceName='' }
  $device = [pscustomobject]@{Status='Missing';Class='Display';FriendlyName='';InstanceId=''}
}
$monitors = @(Get-PnpDevice -Class Monitor -PresentOnly -ErrorAction SilentlyContinue | Select-Object FriendlyName,InstanceId)
[pscustomobject]@{ device=$device; driver=[pscustomobject]$driver; monitors=$monitors } | ConvertTo-Json -Depth 5 -Compress
"""


def classify_monitor_parent(parent, mtt_device_ids=()):
    """Return (virtual, evidence) from the monitor's verified PnP parent.

    Root-enumerated display adapters are software display devices, including
    StarDesk's ``ROOT\\DISPLAY\\0001`` adapter. A failed parent lookup stays
    unknown; it must never be promoted to a physical-monitor claim.
    """
    value = str(parent or "").strip().casefold()
    if not value:
        return None, "parent_unavailable"
    known_mtt = {str(item).strip().casefold() for item in (mtt_device_ids or ())
                 if str(item).strip()}
    if value in known_mtt:
        return True, "verified_mtt_adapter_parent"
    if value.startswith("root\\display\\"):
        return True, "root_enumerated_display_adapter"
    if value.startswith("root\\"):
        return True, "root_enumerated_adapter"
    return False, "non_root_adapter_parent"

class MttVddModule:
    def __init__(self):
        self._last = {"device": {}, "pipe": {}, "monitors": []}
        self._status_observed_at = None
        self._status_cache_hit = False
        self._status_refresh_state = "unknown"
        self._status_refresh_error_type = None
        self._disposed = False

    def init(self):
        self._disposed = False

    def snapshot(self):
        return json.loads(json.dumps(self._last))

    def _status_result(self):
        result = self.snapshot()
        age_ms = (None if self._status_observed_at is None else
                  max(0, int((time.monotonic() - self._status_observed_at) * 1000)))
        result["status_age_ms"] = age_ms
        result["status_cache_hit"] = bool(self._status_cache_hit)
        result["status_refresh_state"] = self._status_refresh_state
        result["status_refresh_error_type"] = self._status_refresh_error_type
        return result

    def restore(self, state):
        if isinstance(state, dict):
            self._last = json.loads(json.dumps(state))

    def _powershell(self, script):
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                              check=False)
        if proc.returncode:
            raise RuntimeError((proc.stderr or "PowerShell query failed").strip()[:500])
        return json.loads(proc.stdout.strip() or "null")

    @staticmethod
    def _device_parent(instance_id):
        if not hasattr(ctypes, "WinDLL") or not instance_id:
            return ""
        cfgmgr = ctypes.WinDLL("CfgMgr32.dll", use_last_error=True)
        locate = cfgmgr.CM_Locate_DevNodeW
        locate.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
        locate.restype = wintypes.ULONG
        get_parent = cfgmgr.CM_Get_Parent
        get_parent.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG]
        get_parent.restype = wintypes.ULONG
        get_id = cfgmgr.CM_Get_Device_IDW
        get_id.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
        get_id.restype = wintypes.ULONG
        devinst, parent = wintypes.DWORD(), wintypes.DWORD()
        if locate(ctypes.byref(devinst), instance_id, 0) or get_parent(ctypes.byref(parent), devinst.value, 0):
            return ""
        buffer = ctypes.create_unicode_buffer(512)
        if get_id(parent.value, buffer, len(buffer), 0):
            return ""
        return buffer.value

    @staticmethod
    def _device_problem(instance_id):
        """Read the current ConfigMgr devnode status and problem code."""
        if not hasattr(ctypes, "WinDLL") or not instance_id:
            return None, None
        cfgmgr = ctypes.WinDLL("CfgMgr32.dll", use_last_error=True)
        locate = cfgmgr.CM_Locate_DevNodeW
        locate.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
        locate.restype = wintypes.ULONG
        get_status = cfgmgr.CM_Get_DevNode_Status
        get_status.argtypes = [ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.ULONG),
                               wintypes.DWORD, wintypes.ULONG]
        get_status.restype = wintypes.ULONG
        devinst, flags, problem = wintypes.DWORD(), wintypes.ULONG(), wintypes.ULONG()
        if locate(ctypes.byref(devinst), instance_id, 0) != 0:
            return None, None
        if get_status(ctypes.byref(flags), ctypes.byref(problem), devinst.value, 0) != 0:
            return None, None
        return int(problem.value) if problem.value else None, int(flags.value)

    def refresh(self, *, force=False, nonblocking=False):
        def load_status():
            return self._read_status()

        if nonblocking:
            (value, self._status_observed_at, self._status_cache_hit,
             self._status_refresh_state, self._status_refresh_error_type) = get_status_nonblocking(
                load_status,
                max_age_seconds=GUARD_MONITOR_IDENTITY_MAX_AGE_SECONDS,
                refresh_early_seconds=GUARD_MONITOR_IDENTITY_REFRESH_EARLY_SECONDS)
            if value is not None:
                self._last = value
            else:
                self._last = {"device": {}, "pipe": {}, "monitors": []}
        else:
            self._last, self._status_observed_at, self._status_cache_hit = get_status(
                load_status, force=bool(force), max_age_seconds=STATUS_CACHE_MAX_AGE_SECONDS)
            self._status_refresh_state = "fresh"
            self._status_refresh_error_type = None
        return self._status_result()

    def _read_status(self):
        raw = self._powershell(_STATUS_SCRIPT) or {}
        devices = raw.get("device") or {}
        signed = raw.get("driver") or {}
        monitors = raw.get("monitors") or []
        if isinstance(monitors, dict): monitors = [monitors]
        device = devices
        driver = signed
        problem_code, devnode_status = self._device_problem(device.get("InstanceId", ""))
        vdd_ids = {device.get("InstanceId", "").casefold()} if device.get("InstanceId") else set()
        for monitor in monitors:
            parent = self._device_parent(monitor.get("InstanceId", ""))
            monitor["Parent"] = parent
            monitor["Virtual"], monitor["VirtualClassification"] = classify_monitor_parent(
                parent, vdd_ids)
        try:
            response = send("PING")
            pipe = {"connected": response == "DELIVERED_NO_REPLY" or response.strip().upper().startswith("PONG"),
                    "reply_received": response != "DELIVERED_NO_REPLY", "response": response[:240]}
        except Exception as exc:
            pipe = {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"device": {"installed": bool(driver), "status": device.get("Status", "Unknown"),
                                  "friendly_name": device.get("FriendlyName", driver.get("DeviceName", "MikeTheTech VDD")),
                                  "instance_id": device.get("InstanceId", driver.get("DeviceID", "")),
                                  "manufacturer": driver.get("Manufacturer", ""),
                                  "driver_version": driver.get("DriverVersion", ""),
                                  "inf": driver.get("InfName", ""),
                                  "problem_code": problem_code,
                                  "devnode_status": devnode_status},
                      "pipe": pipe, "monitors": monitors}

    def command(self, command):
        command = validate_command(command)
        if command != "PING" and not command.startswith("SETDISPLAYCOUNT "):
            raise ValueError("Command is not allowlisted")
        count_change = command.startswith("SETDISPLAYCOUNT ")
        if count_change:
            invalidate()
        try:
            return send(command)
        finally:
            # A refresh may have started between the preflight invalidation and
            # the count change. Invalidate again so its old snapshot is discarded.
            if count_change:
                invalidate()

    def device_action(self, action):
        if action not in {"enable", "disable"}:
            raise ValueError("Device action must be explicitly enable or disable")
        self.refresh(force=True)  # Recheck provider, device identity and status immediately before UAC.
        device = self._last.get("device", {})
        instance_id = device.get("instance_id", "")
        if not device.get("installed") or device.get("manufacturer", "").casefold() != "mikethetech":
            raise RuntimeError("The verified MikeTheTech VDD device is not available")
        if not re.fullmatch(r"ROOT\\(?:DISPLAY|MTTVDD)\\[A-Za-z0-9&_.\\-]+", instance_id, re.IGNORECASE):
            raise RuntimeError("The current device instance ID is outside the MTT VDD allowlist")
        if (action == "enable" and device.get("status", "").casefold() in {"started", "ok"}
                and device.get("problem_code") is None):
            return "MikeTheTech VDD 장치가 이미 활성 상태입니다. 추가 관리자 요청은 하지 않았습니다."
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Elevated device actions are available on Windows only")
        class ShellExecuteInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HANDLE),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        if action == "enable":
            script = Path(__file__).resolve().with_name("enable_device.ps1")
            if not script.is_file():
                raise FileNotFoundError(f"MTT enable helper is missing: {script}")
            executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                      "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            params = (f'-NoLogo -NoProfile -NonInteractive -File "{script}" '
                      f'-InstanceId "{instance_id}"')
            action_name = "Enable-PnpDevice"
        else:
            params = f'/{action}-device "{instance_id}"'
            executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "pnputil.exe")
            action_name = "pnputil"
        request = ShellExecuteInfo()
        request.cbSize = ctypes.sizeof(ShellExecuteInfo)
        request.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
        request.lpVerb = "runas"
        request.lpFile = executable
        request.lpParameters = params
        request.nShow = 0
        invalidate()
        if not shell32.ShellExecuteExW(ctypes.byref(request)):
            error = ctypes.get_last_error()
            if error == 1223:
                raise RuntimeError("Windows UAC 요청이 취소되었습니다. 장치 상태는 변경하지 않았습니다.")
            raise OSError(error, "Windows에서 관리자 권한 PnP 요청을 시작하지 못했습니다.")
        if not request.hProcess:
            raise RuntimeError("Windows가 관리자 작업을 시작했지만 결과 핸들을 제공하지 않았습니다. 결과를 확인할 수 없습니다.")
        try:
            wait_result = kernel32.WaitForSingleObject(request.hProcess, 30_000)
            if wait_result == 0x00000102:  # WAIT_TIMEOUT
                raise TimeoutError("관리자 PnP 작업이 30초 안에 끝나지 않았습니다. 중복 요청은 보내지 않았습니다.")
            if wait_result != 0:
                raise OSError(ctypes.get_last_error(), "관리자 PnP 작업의 완료 상태를 읽지 못했습니다.")
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(request.hProcess, ctypes.byref(exit_code)):
                raise OSError(ctypes.get_last_error(), "관리자 PnP 작업의 종료 코드를 읽지 못했습니다.")
            if exit_code.value != 0:
                raise RuntimeError(f"{action_name} 관리자 작업이 오류 코드 {exit_code.value}(으)로 끝났습니다. 장치 상태를 새로고침하세요.")
        finally:
            kernel32.CloseHandle(request.hProcess)

        self.refresh(force=True)
        updated = self._last.get("device", {})
        problem = updated.get("problem_code")
        status = updated.get("status", "unknown")
        if action == "enable" and (problem == 22 or status.casefold() not in {"started", "ok"}):
            raise RuntimeError(f"Enable-PnpDevice는 종료됐지만 Windows 장치 상태를 활성으로 확인하지 못했습니다(상태: {status}, 문제 코드: {problem}). 상태를 새로고침하세요.")
        if action == "disable" and problem != 22 and status.casefold() not in {"error", "disabled"}:
            raise RuntimeError(f"pnputil은 완료됐지만 비활성 상태를 확인하지 못했습니다(상태: {status}, 문제 코드: {problem}).")
        return f"Windows 관리자 작업이 종료 코드 0으로 완료됐습니다. 장치 상태: {status}, 문제 코드: {problem if problem is not None else '없음'}. 제어 파이프는 {'연결됨' if self._last.get('pipe', {}).get('connected') else '아직 응답 없음'}입니다."

    def dispose(self):
        self._disposed = True


def create():
    return MttVddModule()
