from __future__ import annotations

import json
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.manager_cli.module import ManagerCliModule, REQUEST_COMMANDS
from modules.stardesk_management.module import API_VERSION, VERSION, StarDeskManagementModule
from core.module_host import ModuleHost
from tools.vdmctl import build_parser


class StarDeskManagementTests(unittest.TestCase):
    @patch("modules.stardesk_management.module.subprocess.run")
    @patch("modules.stardesk_management.module.os.name", "nt")
    def test_pnp_inventory_uses_bounded_longer_timeout_and_read_only_queries(self, run):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0,
            stdout=json.dumps({"monitors": [], "adapters": []}), stderr="")
        result = StarDeskManagementModule._read_pnp_inventory()
        self.assertEqual(result, {"monitors": [], "adapters": []})
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        script = run.call_args.args[0][-1]
        self.assertIn("Get-PnpDevice -Class Monitor -PresentOnly", script)
        self.assertIn("Get-PnpDevice -Class Display -PresentOnly", script)
        self.assertNotIn("Enable-PnpDevice", script)
        self.assertNotIn("Disable-PnpDevice", script)

    def test_adapter_presence_is_distinct_from_verified_active_attachment(self):
        inventory = {
            "monitors": [{"InstanceId": "DISPLAY\\SDM0001\\1&ABC&0&UID1",
                          "Parent": "ROOT\\DISPLAY\\0001"}],
            "adapters": [{"InstanceId": "ROOT\\DISPLAY\\0001",
                          "FriendlyName": "StarDesk Virtual Display Adapter",
                          "Provider": "GearUP Portal Pte. Ltd.",
                          "Manufacturer": "GearUP Portal Pte. Ltd.", "Status": "OK"}],
        }
        topology = {"topology_verified": True, "displays": [{
            "display_name": "\\\\.\\DISPLAY7", "virtual": True,
            "topology_verified": True, "monitor_identity_verified": True,
            "monitor_parent": "root\\display\\0001",
            "monitor_path": r"\\?\DISPLAY#SDM0001#1&ABC&0&UID1#{guid}",
        }]}
        result = StarDeskManagementModule._star_desk_adapter_state(topology, inventory)
        self.assertEqual(result, {"state": "observed", "present": True,
            "active_output_attached": True, "active_output_count": 1,
            "evidence": "exact_pnp_parent_match"})

    def test_present_adapter_does_not_imply_active_output(self):
        inventory = {"monitors": [], "adapters": [{
            "InstanceId": "ROOT\\DISPLAY\\0001",
            "FriendlyName": "StarDesk Virtual Display Adapter",
            "Provider": "GearUP Portal Pte. Ltd.",
            "Manufacturer": "GearUP Portal Pte. Ltd.", "Status": "OK",
        }]}
        topology = {"topology_verified": True, "displays": []}
        result = StarDeskManagementModule._star_desk_adapter_state(topology, inventory)
        self.assertTrue(result["present"])
        self.assertFalse(result["active_output_attached"])
        self.assertEqual(result["active_output_count"], 0)

    def test_adapter_identity_and_monitor_mapping_fail_closed(self):
        topology = {"topology_verified": True, "displays": [{
            "topology_verified": True, "monitor_identity_verified": True,
            "monitor_parent": "ROOT\\DISPLAY\\0001",
            "monitor_path": r"\\?\DISPLAY#SDM0001#1&ABC&0&UID1#{guid}",
        }]}
        base = {"monitors": [{"InstanceId": "DISPLAY\\SDM0001\\1&ABC&0&UID1",
                              "Parent": "ROOT\\DISPLAY\\0001"}],
                "adapters": [{"InstanceId": "ROOT\\DISPLAY\\0001",
                              "FriendlyName": "StarDesk Virtual Display Adapter",
                              "Provider": "GearUP Portal Pte. Ltd.",
                              "Manufacturer": "GearUP Portal Pte. Ltd."}]}
        bad_provider = json.loads(json.dumps(base))
        bad_provider["adapters"][0]["Provider"] = "Unknown Publisher"
        self.assertFalse(StarDeskManagementModule._star_desk_adapter_state(
            topology, bad_provider)["present"])
        ambiguous = json.loads(json.dumps(base))
        ambiguous["adapters"].append(dict(ambiguous["adapters"][0], InstanceId="ROOT\\DISPLAY\\0002"))
        self.assertEqual(StarDeskManagementModule._star_desk_adapter_state(
            topology, ambiguous)["evidence"], "ambiguous_signed_adapter_identity")
        duplicate_monitor = json.loads(json.dumps(base))
        duplicate_monitor["monitors"].append(dict(duplicate_monitor["monitors"][0],
            Parent="ROOT\\DISPLAY\\0002"))
        self.assertEqual(StarDeskManagementModule._star_desk_adapter_state(
            topology, duplicate_monitor)["evidence"], "ambiguous_monitor_path_mapping")

    def test_unverified_active_display_prevents_negative_attachment_claim(self):
        inventory = {"monitors": [], "adapters": [{
            "InstanceId": "ROOT\\DISPLAY\\0001",
            "FriendlyName": "StarDesk Virtual Display Adapter",
            "Provider": "GearUP Portal Pte. Ltd.",
            "Manufacturer": "GearUP Portal Pte. Ltd.",
        }]}
        topology = {"topology_verified": True, "displays": [{
            "topology_verified": True, "monitor_identity_verified": False,
        }]}
        result = StarDeskManagementModule._star_desk_adapter_state(topology, inventory)
        self.assertIsNone(result["active_output_attached"])
        self.assertEqual(result["evidence"], "active_display_identity_unverified")

    def test_adapter_inventory_failure_is_unknown_without_failing_status(self):
        def fail():
            raise PermissionError("access denied")
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [], "service": None},
            topology_reader=lambda: {"topology_verified": True, "displays": []},
            client_connection_reader=lambda: 0,
            transport_reader=lambda _pids: {"state": "observed", "tcp_total": 0,
                "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
            pnp_inventory_reader=fail,
        )
        result = module.action("status")
        self.assertEqual(result["stardesk_virtual_adapter"]["state"], "unknown")
        self.assertIsNone(result["stardesk_virtual_adapter"]["present"])
        self.assertEqual(result["remote_input_mode"], "unknown")

    def test_process_presence_never_claims_inbound_session_or_mouse_mode(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {
                "processes": [
                    {"ProcessName": "StarDesk", "Id": 46220, "SessionId": 1},
                    {"ProcessName": "StarDeskServer", "Id": 49364, "SessionId": 1},
                ],
                "service": {"State": "Running", "StartMode": "Auto"},
            },
            client_connection_reader=lambda: 0,
            topology_reader=lambda: {"topology_verified": True, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
        )
        result = module.action("status")
        self.assertEqual(result["remote_host_session"], "unknown")
        self.assertEqual(result["remote_input_mode"], "unknown")
        self.assertEqual(result["processes"], [
            {"name": "StarDesk", "running": True,
             "windows_session_id": 1, "session_role": "non_service_session"},
            {"name": "StarDeskServer", "running": True,
             "windows_session_id": 1, "session_role": "non_service_session"},
        ])
        self.assertEqual(result["service"]["state"], "Running")
        self.assertEqual(result["local_runtime_observation"], {
            "state": "observed_active", "process_query": "complete",
            "observed_processes": ["StarDesk", "StarDeskServer"],
            "service_state": "Running",
            "scope": "local Windows process/service presence only; not remote-session or input evidence",
        })
        self.assertEqual(result["ui_server_session_alignment"], {
            "state": "same_windows_session", "ui_process_session_id": 1,
            "server_process_session_id": 1,
            "scope": "local Windows process placement only; does not identify an inbound Mac session",
        })
        self.assertTrue(result["read_only"])

    def test_ui_server_session_alignment_distinguishes_mismatch_and_ambiguity(self):
        cases = (
            ([{"ProcessName": "StarDesk", "Id": 1, "SessionId": 1},
              {"ProcessName": "StarDeskServer", "Id": 2, "SessionId": 2}],
             "different_windows_sessions", 1, 2),
            ([{"ProcessName": "StarDesk", "Id": 1, "SessionId": 1}],
             "unknown", 1, None),
            ([{"ProcessName": "StarDesk", "Id": 1, "SessionId": 1},
              {"ProcessName": "StarDeskServer", "Id": 2, "SessionId": 1},
              {"ProcessName": "StarDeskServer", "Id": 3, "SessionId": 2}],
             "unknown", 1, None),
        )
        for processes, state, ui_session, server_session in cases:
            with self.subTest(state=state, processes=processes):
                module = StarDeskManagementModule(
                    process_service_reader=lambda p=processes: {"processes": p, "service": None},
                    topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
                )
                alignment = module.action("status")["ui_server_session_alignment"]
                self.assertEqual(alignment["state"], state)
                self.assertEqual(alignment["ui_process_session_id"], ui_session)
                self.assertEqual(alignment["server_process_session_id"], server_session)

    def test_virtual_output_owner_stays_unknown(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [], "service": None},
            client_connection_reader=lambda: 0,
            topology_reader=lambda: {"topology_verified": True, "displays": [
                {"virtual": True, "monitor_identity_verified": True,
                 "topology_verified": True, "display_name": "\\\\.\\DISPLAY36",
                 "adapter": "VDD by MTT", "target_name": "VDD by MTT",
                 "width": 1920, "height": 1080, "refresh_rate": 60,
                 "clone_source_count": 1},
                {"virtual": False, "monitor_identity_verified": True},
            ]},
        )
        result = module.action("status")
        self.assertEqual(result["active_virtual_output_count"], 1)
        endpoint = result["active_virtual_outputs"][0]
        self.assertEqual(endpoint["starDesk_owner"], "unknown")
        self.assertEqual(endpoint["stardesk_display_index"], "unknown")
        self.assertEqual(endpoint["display_name"], "\\\\.\\DISPLAY36")
        self.assertEqual(endpoint["mode"], [1920, 1080, 60])

    def test_session_zero_is_not_mislabeled_as_interactive(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [
                {"ProcessName": "StarDeskService", "Id": 34008, "SessionId": 0}],
                "service": {"State": "Running", "StartMode": "Auto", "ProcessId": 34008}},
            topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
        )
        result = module.action("status")
        self.assertEqual(result["processes"][0]["session_role"], "session_0_service")
        self.assertEqual(result["processes"][0]["windows_session_id"], 0)
        self.assertTrue(result["service"]["process_id_present"])
        self.assertEqual(result["remote_host_session"], "unknown")

    def test_invalid_session_and_mode_values_fail_closed(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [
                {"ProcessName": "StarDesk", "Id": 46220, "SessionId": "1"}],
                "service": None},
            topology_reader=lambda: {"topology_verified": True, "displays": [{
                "virtual": True, "monitor_identity_verified": True,
                "width": 0, "height": 1080, "refresh_rate": 60,
            }]},
        )
        result = module.action("status")
        self.assertIsNone(result["processes"][0]["windows_session_id"])
        self.assertEqual(result["processes"][0]["session_role"], "unknown")
        self.assertIsNone(result["active_virtual_outputs"][0]["mode"])
        self.assertEqual(result["active_virtual_outputs"][0]["stardesk_display_index"], "unknown")

    def test_unverified_topology_does_not_report_zero_as_fact(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [], "service": None},
            client_connection_reader=lambda: 0,
            topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
        )
        result = module.action("status")
        self.assertIsNone(result["active_virtual_output_count"])
        self.assertEqual(result["display_topology"], "unverified")

    def test_client_cli_reports_only_directional_connection_count(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [], "service": None},
            client_connection_reader=lambda: 2,
            topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
        )
        result = module.action("status")
        self.assertEqual(result["client_outbound_connection"], {
            "direction": "outbound_from_this_pc", "state": "connected",
            "connected_count": 2, "read_only": True,
        })
        self.assertEqual(result["remote_host_session"], "unknown")

    def test_client_cli_failure_is_unknown_not_disconnected(self):
        def fail():
            raise RuntimeError("STAR_DESK_CLIENT_STATUS_FAILED")

        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [], "service": None},
            client_connection_reader=fail,
            topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
        )
        result = module.action("status")
        self.assertEqual(result["client_outbound_connection"]["state"], "unknown")
        self.assertIsNone(result["client_outbound_connection"]["connected_count"])

    def test_runtime_observation_distinguishes_inactive_and_unknown(self):
        cases = (
            ({"processes": [], "service": {"State": "Stopped", "StartMode": "Manual"}},
             "observed_inactive", "Stopped"),
            ({"processes": [], "service": None}, "unknown", "unknown"),
            ({"processes": "invalid", "service": {"State": "Stopped"}},
             "unknown", "Stopped"),
        )
        for local, state, service_state in cases:
            with self.subTest(state=state, local=local):
                module = StarDeskManagementModule(
                    process_service_reader=lambda value=local: value,
                    client_connection_reader=lambda: 0,
                    topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 0, "tcp_state_counts": {}, "udp_bound_endpoint_count": 0},
                )
                result = module.action("status")
                self.assertEqual(result["state"], "ready")
                observation = result["local_runtime_observation"]
                self.assertEqual(observation["state"], state)
                self.assertEqual(observation["service_state"], service_state)
                self.assertEqual(result["remote_host_session"], "unknown")
                self.assertEqual(result["remote_input_mode"], "unknown")
                self.assertEqual(observation["process_query"],
                                 "complete" if isinstance(local["processes"], list) else "unknown")

    def test_transport_counts_are_bounded_and_do_not_change_session_claim(self):
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [
                {"ProcessName": "StarDeskServer", "Id": 49364, "SessionId": 1}], "service": None},
            topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=lambda pids: {"state": "observed", "tcp_total": 1,
                "tcp_state_counts": {"Established": 1}, "udp_bound_endpoint_count": 0,
                "remote_address": "192.0.2.9", "remote_port": 443},
        )
        result = module.action("status")
        self.assertEqual(result["server_transport_activity"]["tcp_total"], 1)
        self.assertNotIn("remote_address", result["server_transport_activity"])
        self.assertNotIn("remote_port", result["server_transport_activity"])
        self.assertEqual(result["server_transport_activity"]["scope"],
            "StarDeskServer-owned socket counts only; not proof of inbound remote session or input delivery")
        self.assertEqual(result["remote_host_session"], "unknown")
        self.assertEqual(result["remote_input_mode"], "unknown")

    @patch("modules.stardesk_management.module.subprocess.run")
    def test_transport_query_falls_back_to_pid_filtered_cim_for_empty_udp_owner(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"ok": True, "tcp_total": 14,
                "tcp_state_counts": {"Bound": 2, "CloseWait": 4,
                    "Unknown": 6, "Established": 1, "FinWait1": 1},
                "udp_bound_endpoint_count": 0}), stderr="",
        )

        result = StarDeskManagementModule._read_server_transport([49364])

        script = run.call_args.args[0][-1]
        self.assertIn("Get-CimInstance -Namespace root/StandardCimv2", script)
        self.assertIn('ClassName MSFT_NetUDPEndpoint -Filter "OwningProcess = $p"', script)
        self.assertIn("if([string]::IsNullOrWhiteSpace($state)){$state='Unknown'}", script)
        self.assertEqual(result["tcp_total"], 14)
        self.assertEqual(result["tcp_state_counts"]["Unknown"], 6)
        self.assertEqual(result["udp_bound_endpoint_count"], 0)

    def test_transport_query_failure_is_unknown_without_failing_status(self):
        def fail(_pids):
            raise PermissionError("access denied")
        module = StarDeskManagementModule(
            process_service_reader=lambda: {"processes": [], "service": None},
            topology_reader=lambda: {"topology_verified": False, "displays": []},
            transport_reader=fail,
        )
        result = module.action("status")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["server_transport_activity"]["state"], "unknown")
        self.assertIsNone(result["server_transport_activity"]["tcp_total"])
        self.assertEqual(result["remote_host_session"], "unknown")

    def test_cli_route_is_allowlisted_and_read_only(self):
        parsed = build_parser().parse_args(["stardesk", "status"])
        self.assertEqual((parsed.group, parsed.action), ("stardesk", "status"))
        self.assertIn("stardesk.status", REQUEST_COMMANDS)
        module = ManagerCliModule.__new__(ManagerCliModule)
        module._stardesk_reader = lambda: {"remote_host_session": "unknown", "read_only": True}
        self.assertEqual(module._dispatch_command("stardesk.status", {}),
                         {"remote_host_session": "unknown", "read_only": True})
        with self.assertRaisesRegex(ValueError, "ARGUMENT_INVALID"):
            module._dispatch_command("stardesk.status", {"connect": True})

    def test_manifest_module_version_matches_contract(self):
        manifest = json.loads((ROOT / "module-registry.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["modules"] if item["id"] == "stardesk_management")
        self.assertEqual(entry["version"], VERSION)
        self.assertEqual(entry["apiVersion"], API_VERSION)

    def test_isolated_candidate_loads_and_reloads_with_new_hash(self):
        with tempfile.TemporaryDirectory(prefix="vdm-stardesk-module-") as temp:
            root = Path(temp)
            module_path = root / "modules" / "stardesk_management" / "module.py"
            module_path.parent.mkdir(parents=True)
            module_path.write_bytes((ROOT / "modules" / "stardesk_management" / "module.py").read_bytes())
            registry = {
                "schemaVersion": 1,
                "modules": [{"id": "stardesk_management",
                             "path": "modules/stardesk_management/module.py",
                             "version": VERSION, "apiVersion": API_VERSION}],
            }
            (root / "module-registry.json").write_text(json.dumps(registry), encoding="utf-8")
            host = ModuleHost(root)
            first = host.load("stardesk_management")
            self.assertEqual(first["generation"], 1)
            self.assertTrue(first["sha256"])

            module_path.write_bytes(module_path.read_bytes() + b"\n")
            second = host.reload("stardesk_management")
            self.assertEqual(second["generation"], 2)
            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertEqual(second["version"], VERSION)
            host.dispose_all()


if __name__ == "__main__":
    unittest.main()
