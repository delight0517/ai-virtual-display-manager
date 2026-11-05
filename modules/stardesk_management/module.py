"""Privacy-bounded, read-only StarDesk host and display observer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

API_VERSION = 1
MODULE_ID = "stardesk_management"
VERSION = "0.1.7"
ACTIONS = ("status",)

_PNP_INVENTORY_SCRIPT = r"""
$monitors = @(Get-PnpDevice -Class Monitor -PresentOnly -ErrorAction Stop | ForEach-Object {
  $parent = $null
  try {
    $property = Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop
    $parent = [string]$property.Data
  } catch {}
  [pscustomobject]@{InstanceId=[string]$_.InstanceId; Parent=$parent}
})
$adapters = @(Get-PnpDevice -Class Display -PresentOnly -ErrorAction Stop | ForEach-Object {
  $provider = $null; $manufacturer = $null
  try {
    $props = @(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_DriverProvider','DEVPKEY_Device_Manufacturer' -ErrorAction Stop)
    foreach($p in $props) {
      if($p.KeyName -eq 'DEVPKEY_Device_DriverProvider'){$provider=[string]$p.Data}
      if($p.KeyName -eq 'DEVPKEY_Device_Manufacturer'){$manufacturer=[string]$p.Data}
    }
  } catch {}
  [pscustomobject]@{InstanceId=[string]$_.InstanceId; FriendlyName=[string]$_.FriendlyName; Provider=$provider; Manufacturer=$manufacturer; Status=[string]$_.Status}
})
[pscustomobject]@{monitors=$monitors;adapters=$adapters} | ConvertTo-Json -Depth 4 -Compress
"""

_STATUS_SCRIPT = r"""
$names = @('StarDesk','StarDeskServer','StarDeskService')
$processes = @(Get-Process -Name $names -ErrorAction SilentlyContinue |
  ForEach-Object {
    $session = $null
    try { $session = [int]$_.SessionId } catch {}
    [pscustomobject]@{ ProcessName=$_.ProcessName; Id=[uint32]$_.Id; SessionId=$session }
  })
$service = Get-CimInstance Win32_Service -Filter "Name='StarDeskService'" -ErrorAction SilentlyContinue |
  Select-Object -First 1 State,StartMode,ProcessId
[pscustomobject]@{processes=$processes;service=$service} | ConvertTo-Json -Depth 3 -Compress
"""


class StarDeskManagementModule:
    def __init__(self, *, process_service_reader=None, topology_reader=None,
                 client_connection_reader=None, transport_reader=None,
                 pnp_inventory_reader=None):
        self._process_service_reader = process_service_reader or self._read_process_service
        self._client_connection_reader = client_connection_reader or self._read_client_connection_count
        self._topology_reader = topology_reader
        self._transport_reader = transport_reader or self._read_server_transport
        self._pnp_inventory_reader = pnp_inventory_reader or self._read_pnp_inventory

    def init(self):
        if not callable(self._process_service_reader):
            raise TypeError("StarDesk process/service reader must be callable")

    def bind_dependencies(self, *, topology_reader):
        if not callable(topology_reader):
            raise TypeError("StarDesk topology reader must be callable")
        self._topology_reader = topology_reader

    @staticmethod
    def _read_pnp_inventory():
        """Return present monitor/adapter identities only; never changes devices."""
        if os.name != "nt":
            raise OSError("WINDOWS_ONLY")
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PNP_INVENTORY_SCRIPT],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        if proc.returncode:
            raise RuntimeError("PNP_INVENTORY_QUERY_FAILED")
        value = json.loads(proc.stdout.strip() or "null")
        if not isinstance(value, dict):
            raise ValueError("PNP_INVENTORY_RESPONSE_INVALID")
        monitors, adapters = value.get("monitors"), value.get("adapters")
        if not isinstance(monitors, list) or not isinstance(adapters, list) or len(monitors) > 128 or len(adapters) > 64:
            raise ValueError("PNP_INVENTORY_RESPONSE_INVALID")
        return value

    @staticmethod
    def _adapter_identity(value):
        return str(value or "").strip().casefold()

    @staticmethod
    def _pnp_monitor_key(instance_id):
        parts = [part.strip().casefold() for part in str(instance_id or "").split("\\")]
        return "#".join(parts[:3]) if len(parts) >= 3 and parts[0] == "display" and all(parts[:3]) else ""

    @staticmethod
    def _display_path_monitor_key(path):
        value = str(path or "").strip()
        if value.startswith("\\\\?\\"):
            value = value[4:]
        value = value.split("#{", 1)[0].strip("\\")
        parts = [part.strip().casefold() for part in value.split("#")]
        return "#".join(parts[:3]) if len(parts) >= 3 and parts[0] == "display" and all(parts[:3]) else ""

    @classmethod
    def _star_desk_adapter_state(cls, topology, inventory):
        unknown = {"state": "unknown", "present": None,
                   "active_output_attached": None, "active_output_count": None,
                   "evidence": "pnp_inventory_unavailable_or_invalid"}
        if not isinstance(inventory, dict):
            return unknown
        adapters, monitors = inventory.get("adapters"), inventory.get("monitors")
        if not isinstance(adapters, list) or not isinstance(monitors, list):
            return unknown
        matches = [item for item in adapters if isinstance(item, dict)
                   and cls._adapter_identity(item.get("FriendlyName")) == "stardesk virtual display adapter"
                   and cls._adapter_identity(item.get("Provider")) == "gearup portal pte. ltd."
                   and cls._adapter_identity(item.get("Manufacturer")) == "gearup portal pte. ltd."
                   and isinstance(item.get("InstanceId"), str) and item.get("InstanceId")]
        if len(matches) > 1:
            return {**unknown, "evidence": "ambiguous_signed_adapter_identity"}
        if not matches:
            return {"state": "observed", "present": False,
                    "active_output_attached": False if isinstance(topology, dict) and topology.get("topology_verified") is True else None,
                    "active_output_count": 0 if isinstance(topology, dict) and topology.get("topology_verified") is True else None,
                    "evidence": "no_present_adapter_matching_exact_name_and_provider"}
        adapter_id = cls._adapter_identity(matches[0].get("InstanceId"))
        monitors_by_key = {}
        for item in monitors:
            if not isinstance(item, dict):
                return {"state": "unknown", "present": True,
                        "active_output_attached": None, "active_output_count": None,
                        "evidence": "invalid_monitor_inventory_entry"}
            key = cls._pnp_monitor_key(item.get("InstanceId"))
            if key:
                monitors_by_key.setdefault(key, []).append(item)
        if (not isinstance(topology, dict) or topology.get("topology_verified") is not True
                or not isinstance(topology.get("displays"), list)):
            return {"state": "observed", "present": True,
                    "active_output_attached": None, "active_output_count": None,
                    "evidence": "adapter_present_topology_unverified"}
        count = 0
        for display in topology["displays"][:32]:
            if (not isinstance(display, dict) or display.get("topology_verified") is not True
                    or display.get("monitor_identity_verified") is not True):
                return {"state": "unknown", "present": True,
                        "active_output_attached": None, "active_output_count": None,
                        "evidence": "active_display_identity_unverified"}
            key = cls._display_path_monitor_key(display.get("monitor_path"))
            matches = monitors_by_key.get(key, [])
            if len(matches) != 1:
                return {"state": "unknown", "present": True,
                        "active_output_attached": None, "active_output_count": None,
                        "evidence": "ambiguous_monitor_path_mapping"}
            parent = cls._adapter_identity(matches[0].get("Parent"))
            topology_parent = cls._adapter_identity(display.get("monitor_parent"))
            if not parent or parent != topology_parent:
                return {"state": "unknown", "present": True,
                        "active_output_attached": None, "active_output_count": None,
                        "evidence": "monitor_parent_changed_during_query"}
            if parent == adapter_id:
                count += 1
        return {"state": "observed", "present": True,
                "active_output_attached": count > 0, "active_output_count": count,
                "evidence": "exact_pnp_parent_match"}

    @staticmethod
    def _read_server_transport(server_pids):
        """Return bounded counts for StarDeskServer-owned sockets only."""
        if os.name != "nt":
            raise OSError("WINDOWS_ONLY")
        pids = [p for p in server_pids[:8] if type(p) is int and 0 < p <= 4_294_967_295]
        if not pids:
            return {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0}
        pid_list = ",".join(str(pid) for pid in pids)
        script = f"""
$tcpCount=0; $udpCount=0; $states=@{{}}; $ok=$true
foreach($p in @({pid_list})) {{
  try {{
    $rows=@(Get-NetTCPConnection -OwningProcess $p -ErrorAction Stop | Select-Object -First 128 State)
    $tcpCount += $rows.Count
    foreach($r in $rows) {{ $state=[string]$r.State; if([string]::IsNullOrWhiteSpace($state)){{$state='Unknown'}}; if(-not $states.ContainsKey($state)){{$states[$state]=0}}; $states[$state]++ }}
    try {{
      $udpRows=@(Get-NetUDPEndpoint -OwningProcess $p -ErrorAction Stop | Select-Object -First 128 OwningProcess)
    }} catch {{
      # Some Windows builds throw NotFound when a PID owns no UDP endpoints.
      # The PID-filtered CIM query correctly returns an empty result in that case.
      $udpRows=@(Get-CimInstance -Namespace root/StandardCimv2 -ClassName MSFT_NetUDPEndpoint -Filter "OwningProcess = $p" -ErrorAction Stop | Select-Object -First 128 OwningProcess)
    }}
    $udpCount += $udpRows.Count
  }} catch {{ $ok=$false }}
}}
[pscustomobject]@{{ok=$ok;tcp_total=$tcpCount;tcp_state_counts=$states;udp_bound_endpoint_count=$udpCount}} | ConvertTo-Json -Compress
"""
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if proc.returncode:
            raise RuntimeError("STAR_DESK_TRANSPORT_QUERY_FAILED")
        value = json.loads(proc.stdout.strip() or "null")
        counts = value.get("tcp_state_counts") if isinstance(value, dict) else None
        total, udp = value.get("tcp_total"), value.get("udp_bound_endpoint_count") if isinstance(value, dict) else (None, None)
        if (not isinstance(value, dict) or value.get("ok") is not True or not isinstance(counts, dict)
                or len(counts) > 16 or type(total) is not int or not 0 <= total <= 1024
                or type(udp) is not int or not 0 <= udp <= 1024
                or any(type(v) is not int or not 0 <= v <= 1024 for v in counts.values())):
            raise ValueError("STAR_DESK_TRANSPORT_RESPONSE_INVALID")
        return {"state": "observed", "tcp_total": total, "tcp_state_counts": counts, "udp_bound_endpoint_count": udp}

    @staticmethod
    def _read_client_connection_count():
        """Read only StarDesk connections initiated by this Windows client."""
        if os.name != "nt":
            raise OSError("WINDOWS_ONLY")
        install_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        executable = install_root / "StarDesk" / "bin" / "stardeskmgr.exe"
        if not executable.is_file():
            raise FileNotFoundError("STAR_DESK_MANAGER_CLI_NOT_FOUND")
        proc = subprocess.run(
            [str(executable), "device", "status"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        if proc.returncode:
            raise RuntimeError("STAR_DESK_CLIENT_STATUS_FAILED")
        try:
            value = json.loads(proc.stdout.strip() or "null")
        except (TypeError, ValueError):
            raise ValueError("STAR_DESK_CLIENT_STATUS_INVALID") from None
        data = value.get("data") if isinstance(value, dict) and value.get("success") is True else None
        devices = data.get("connected_devices") if isinstance(data, dict) else None
        if not isinstance(devices, list) or len(devices) > 128:
            raise ValueError("STAR_DESK_CLIENT_STATUS_INVALID")
        # Discard device identifiers at the boundary; VDM only needs the count.
        return len(devices)

    @staticmethod
    def _read_process_service():
        if os.name != "nt":
            raise OSError("WINDOWS_ONLY")
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _STATUS_SCRIPT],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        if proc.returncode:
            raise RuntimeError("STAR_DESK_PROCESS_QUERY_FAILED")
        value = json.loads(proc.stdout.strip() or "null")
        if not isinstance(value, dict):
            raise ValueError("STAR_DESK_PROCESS_RESPONSE_INVALID")
        return value

    def action(self, name, _payload=None):
        if name != "status":
            raise ValueError("COMMAND_UNSUPPORTED")
        try:
            local = self._process_service_reader()
            processes = local.get("processes", []) if isinstance(local, dict) else []
            safe_processes = []
            for item in processes[:8] if isinstance(processes, list) else []:
                if not isinstance(item, dict):
                    continue
                process_name, pid, session_id = (item.get("ProcessName"), item.get("Id"),
                                                   item.get("SessionId"))
                if (process_name in {"StarDesk", "StarDeskServer", "StarDeskService"}
                        and type(pid) is int and 0 < pid <= 4_294_967_295):
                    session_valid = type(session_id) is int and 0 <= session_id <= 65_535
                    safe_processes.append({
                        "name": process_name, "running": True,
                        "windows_session_id": session_id if session_valid else None,
                        "session_role": ("session_0_service" if session_id == 0 else
                                         "non_service_session" if session_valid else "unknown"),
                    })
            def unique_process_session(process_name):
                session_ids = {
                    item.get("windows_session_id") for item in safe_processes
                    if item.get("name") == process_name
                    and type(item.get("windows_session_id")) is int
                }
                return next(iter(session_ids)) if len(session_ids) == 1 else None

            ui_session = unique_process_session("StarDesk")
            server_session = unique_process_session("StarDeskServer")
            session_alignment = (
                "same_windows_session" if ui_session is not None and server_session is not None
                and ui_session == server_session else
                "different_windows_sessions" if ui_session is not None and server_session is not None
                else "unknown"
            )
            service = local.get("service") if isinstance(local, dict) else None
            safe_service = None
            if isinstance(service, dict):
                state, start_mode = service.get("State"), service.get("StartMode")
                service_pid = service.get("ProcessId")
                safe_service = {
                    "state": state if state in {"Running", "Stopped", "Paused"} else "unknown",
                    "start_mode": start_mode if start_mode in {"Auto", "Manual", "Disabled", "Boot", "System"} else "unknown",
                    "process_id_present": type(service_pid) is int and service_pid > 0,
                }
            process_query_valid = isinstance(local, dict) and isinstance(local.get("processes"), list)
            observed_process_names = sorted({
                item["name"] for item in safe_processes
                if item.get("running") is True
            })
            service_state = safe_service["state"] if safe_service else "unknown"
            if observed_process_names or service_state == "Running":
                runtime_observation_state = "observed_active"
            elif process_query_valid and not observed_process_names and service_state == "Stopped":
                runtime_observation_state = "observed_inactive"
            else:
                runtime_observation_state = "unknown"
            runtime_observation = {
                "state": runtime_observation_state,
                "process_query": "complete" if process_query_valid else "unknown",
                "observed_processes": observed_process_names,
                "service_state": service_state,
                "scope": "local Windows process/service presence only; not remote-session or input evidence",
            }
            topology = self._topology_reader() if self._topology_reader else None
            try:
                pnp_inventory = self._pnp_inventory_reader()
            except Exception:
                pnp_inventory = None
            stardesk_adapter = self._star_desk_adapter_state(topology, pnp_inventory)
            server_pids = [item["Id"] for item in (local.get("processes", []) if isinstance(local, dict) else [])
                           if isinstance(item, dict) and item.get("ProcessName") == "StarDeskServer"
                           and type(item.get("Id")) is int][:8]
            try:
                transport = self._transport_reader(server_pids)
                if not isinstance(transport, dict) or transport.get("state") != "observed":
                    raise ValueError("STAR_DESK_TRANSPORT_RESPONSE_INVALID")
                states = transport.get("tcp_state_counts")
                total, udp = transport.get("tcp_total"), transport.get("udp_bound_endpoint_count")
                if (not isinstance(states, dict) or len(states) > 16 or type(total) is not int
                        or not 0 <= total <= 1024 or type(udp) is not int or not 0 <= udp <= 1024
                        or any(not isinstance(k, str) or len(k) > 24 or type(v) is not int or not 0 <= v <= 1024 for k, v in states.items())):
                    raise ValueError("STAR_DESK_TRANSPORT_RESPONSE_INVALID")
                server_transport = {"state": "observed", "tcp_total": total, "tcp_state_counts": states,
                    "udp_bound_endpoint_count": udp, "scope": "StarDeskServer-owned socket counts only; not proof of inbound remote session or input delivery"}
            except Exception:
                server_transport = {"state": "unknown", "tcp_total": None, "tcp_state_counts": {},
                    "udp_bound_endpoint_count": None, "scope": "StarDeskServer-owned socket counts only; not proof of inbound remote session or input delivery"}
            outbound = {"direction": "outbound_from_this_pc", "state": "unknown",
                        "connected_count": None, "read_only": True}
            try:
                connected_count = self._client_connection_reader()
                if type(connected_count) is int and 0 <= connected_count <= 128:
                    outbound["state"] = "connected" if connected_count else "none"
                    outbound["connected_count"] = connected_count
            except Exception:
                # A failed StarDesk CLI query is unknown, never evidence of no connection.
                pass
            displays = topology.get("displays") if isinstance(topology, dict) else None
            verified = isinstance(topology, dict) and topology.get("topology_verified") is True
            virtual = []
            if verified and isinstance(displays, list):
                for display in displays[:32]:
                    if not isinstance(display, dict) or display.get("virtual") is not True:
                        continue
                    # These are local Windows endpoints. Their StarDesk/Mac display-index
                    # mapping and product ownership are not exposed by the host observer.
                    def safe_text(value, limit=128):
                        return value[:limit] if isinstance(value, str) and value else None

                    width, height, refresh = (display.get("width"), display.get("height"),
                                               display.get("refresh_rate"))
                    mode = ([width, height, refresh]
                            if all(type(v) is int and 0 < v <= 16_384 for v in (width, height))
                            and type(refresh) is int and 0 < refresh <= 1_000 else None)
                    clone_count = display.get("clone_source_count")
                    virtual.append({
                        "virtual": True,
                        "display_name": safe_text(display.get("display_name"), 32),
                        "adapter_name": safe_text(display.get("adapter")),
                        "target_name": safe_text(display.get("target_name")),
                        "mode": mode,
                        "cloned": (clone_count > 1 if type(clone_count) is int and clone_count > 0
                                   else display.get("cloned") if type(display.get("cloned")) is bool
                                   else None),
                        "identity_verified": display.get("topology_verified") is True
                                             and display.get("monitor_identity_verified") is True,
                        "starDesk_owner": "unknown",
                        "stardesk_display_index": "unknown",
                    })
            return {
                "state": "ready",
                "source": "local Windows process/service and VDM display topology",
                "client_outbound_connection": outbound,
                "server_transport_activity": server_transport,
                "remote_host_session": "unknown",
                "remote_input_mode": "unknown",
                "ui_server_session_alignment": {
                    "state": session_alignment,
                    "ui_process_session_id": ui_session,
                    "server_process_session_id": server_session,
                    "scope": "local Windows process placement only; does not identify an inbound Mac session",
                },
                "processes": safe_processes,
                "service": safe_service,
                "local_runtime_observation": runtime_observation,
                "active_virtual_outputs": virtual,
                "active_virtual_output_count": len(virtual) if verified and isinstance(displays, list) else None,
                "stardesk_virtual_adapter": stardesk_adapter,
                "display_topology": "verified" if verified else "unverified",
                "warning": "StarDesk server process presence does not prove an inbound remote session; this observer cannot read Mac-side input mode.",
                "read_only": True,
            }
        except Exception as exc:
            return {"state": "unavailable", "error_code": str(exc)[:80],
                    "remote_host_session": "unknown", "remote_input_mode": "unknown",
                    "read_only": True}

    def refresh(self):
        return {"module_id": MODULE_ID, "version": VERSION,
                "read_only": True, "available": True}

    def snapshot(self):
        return {"read_only": True}

    def restore(self, state):
        if state not in (None, {"read_only": True}):
            raise ValueError("StarDesk observer has no mutable state")

    def dispose(self):
        return None

    def reload_blocked(self):
        return False


def create(_context=None):
    return StarDeskManagementModule()


