"""AI Virtual Display Manager application entry point."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from concurrent.futures import ThreadPoolExecutor
import json
import base64
import hashlib
from pathlib import Path
from queue import Empty, SimpleQueue
import ctypes
import sys
import time
import uuid
import os
import subprocess
from tkinter import ttk

from core.module_host import ModuleHost
from core.runtime_packet import runtime_packet_self_check
from core.instance_guard import SingleInstanceGuard
from core.paths import APP_ROOT, APP_VERSION
from core.theme import COLORS
from core.png_capture import MAX_PNG_BYTES, PNG_SIGNATURE
from core.ai_keep_awake import get_keep_awake_guard
from core.resident import ResidentController, atomic_json
from core.setup_wizard import launch_wizard_if_needed, FirstRunWizardDialog, check_uac_secure_desktop, apply_uac_secure_desktop_disable
from modules.app_window_router.module import (
    _display_path_monitor_key, _pnp_monitor_key, verified_mtt_outputs,
)
from modules.virtual_output_pool.module import IDLE_SECONDS, PROFILES, mode_identity_key
from modules.virtual_output_pool.state_store import VirtualOutputStateStore


MANAGER_STATUS_WAIT_SECONDS = 1.5
PRIVATE_DESKTOP_FRAME_WAIT_SECONDS = 1.5
PRIVATE_DESKTOP_FRAME_POLL_SECONDS = 0.05
PRIVATE_DESKTOP_FRAME_MAX_ATTEMPTS = 32


def _manager_status_error_code(exc):
    if (isinstance(exc, RuntimeError)
            and str(exc) == "Another manager operation is already active"):
        return "MANAGER_OPERATION_ACTIVE"
    return type(exc).__name__


def manager_window_geometry(screen_width: int, screen_height: int) -> tuple[int, int, int, int]:
    """Fit the manager shell inside the current display while preserving usable size."""
    width = min(980, max(600, int(screen_width) - 32))
    height = min(760, max(460, int(screen_height) - 48))
    return width, height, 16, 16


def only_chat_broker_summary(state: dict) -> str:
    desktop_state = state.get("input_desktop_state", "unknown")
    if desktop_state != "default":
        reason = ("Windows 보안/로그온 데스크톱 전환 감지 · 작업을 중지해야 합니다"
                  if desktop_state == "other" else
                  "Windows 입력 데스크톱 상태를 확인할 수 없어 작업을 중지해야 합니다")
        return (f"Only Chat 브로커: {reason} · 보안 확인을 자동으로 누르지 않습니다. "
                "격리 실행은 사용할 수 없습니다.")
    if state.get("connected"):
        return ("Only Chat 브로커: 프로토콜 연결됨 · Only Chat/Codex 실행 연결이 아직 없어 "
                "AI QA 작업 대여는 거부됩니다.")
    reason = str(state.get("error_code") or "BROKER_UNAVAILABLE")[:80]
    return f"Only Chat 브로커: 연결되지 않음 ({reason}) · 실제 격리 실행은 사용할 수 없습니다."


def task_runtime_broker_summary(state: dict) -> str:
    authenticated = int(state.get("authenticated_request_count", 0) or 0)
    client_activity = (f"Only Chat 인증 요청 수신 {authenticated}회"
                       if authenticated > 0 else "Only Chat 인증 요청 수신 기록 없음")
    readiness_error = state.get("readiness_error") or state.get("error_code")
    if not state.get("pipe_ready"):
        reason = str(readiness_error or "BROKER_UNAVAILABLE")[:80]
        return (f"Only Chat 작업 세션 API: 수신 대기 실패 ({reason}) · {client_activity} · "
                "앱 실행은 거부됩니다.")
    if state.get("backend_available") is not True or state.get("target_count", 0) <= 0:
        reason = str(readiness_error or "ISOLATED_BACKEND_UNAVAILABLE")[:80]
        return ("Only Chat 작업 세션 API: IPC 수신 대기 중 · 격리 실행기/허용 대상 없음 "
                f"({reason}) · {client_activity} · 앱 실행은 거부됩니다.")
    if (state.get("output_lease_required") is True
            and state.get("output_lease_bound") is not True):
        reason = str(readiness_error or
                     "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE")[:80]
        return ("Only Chat 작업 세션 API: IPC 수신 대기 중 · 출력 임대 연결 없음 "
                f"({reason}) · {client_activity} · 앱 실행은 거부됩니다.")
    if readiness_error == "OUTPUT_POOL_NOT_READY":
        return ("Only Chat 작업 세션 API: IPC 수신 대기 중 · 허용 대상에 맞는 "
                "가상 출력 풀이 확인되지 않아 앱 실행을 거부합니다. 먼저 ‘AI 화면 만들기’에서 "
                f"요청 수와 품질을 적용한 뒤 다시 확인하세요. ({readiness_error}) · "
                f"{client_activity}")
    if readiness_error:
        return (f"Only Chat 작업 세션 API: 준비 확인 실패 ({str(readiness_error)[:80]}) · "
                f"{client_activity} · 앱 실행은 거부됩니다.")
    return (f"Only Chat 작업 세션 API: IPC 수신 대기 중 · 허용 대상 "
            f"{int(state['target_count'])}개 · {client_activity} · 세션 요청 가능")


def build_only_chat_qa_target(runtime_module, broker_module, registry_path=None):
    """Build a target only when the installed, isolated-profile runtime is attested."""
    unavailable = (None, "ONLY_CHAT_TARGET_UNAVAILABLE")
    try:
        registry_path = Path(registry_path or broker_module.APP_PATH_REGISTRY)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        rows = registry.get("apps") if isinstance(registry, dict) else None
        matches = [row for row in rows if isinstance(row, dict)
                   and row.get("id") == "only-chat-windows"] if isinstance(rows, list) else []
        if len(matches) != 1:
            return None, "ONLY_CHAT_REGISTRY_UNVERIFIED"
        record = matches[0]
        windows = record.get("windows")
        if not isinstance(windows, dict) or windows.get("installedRuntimeVerified") is not True:
            return None, "ONLY_CHAT_RUNTIME_NOT_ATTESTED"
        expected_archive = windows.get("installedArchiveHash")
        if (not isinstance(expected_archive, str)
                or len(expected_archive) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in expected_archive)):
            return None, "ONLY_CHAT_RUNTIME_NOT_ATTESTED"
        executable = broker_module.registered_only_chat_exe_path(registry_path)
        if executable is None or not Path(executable).is_file():
            return None, "ONLY_CHAT_IMAGE_UNAVAILABLE"
        executable = Path(executable).resolve(strict=True)
        archive = executable.parent / "resources" / "app.asar"
        if not archive.is_file():
            return None, "ONLY_CHAT_ARCHIVE_UNAVAILABLE"
        actual_archive = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual_archive.casefold() != expected_archive.casefold():
            return None, "ONLY_CHAT_RUNTIME_IDENTITY_MISMATCH"
        executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        descriptor = runtime_module.TargetLaunchDescriptor(
            executable_path=str(executable), executable_sha256=executable_hash,
            arguments=("--only-chat-isolated-qa",
                       "--only-chat-qa-profile={VDM_TASK_PROFILE}",
                       "--only-chat-qa-startup-report={VDM_TASK_PROFILE}\\startup-report.json"),
            working_directory=str(executable.parent),
            window_class_names=("Chrome_WidgetWin_1",), actions=(),
            cleanup_timeout_seconds=10, maximum_session_seconds=900,
        )
        target = runtime_module.TargetSpec(
            app_id="only_chat_qa", action_ids=descriptor.action_ids, launch=descriptor,
            minimum_output_count=0, maximum_output_count=0,
            output_profiles=frozenset({"private"}), isolation_mode="private_desktop",
        )
        return target, None
    except Exception:
        return unavailable


def bind_task_runtime_dependencies(loaded_runtime, pool, topology_reader, task_broker,
                                   only_chat_target_source=None):
    """Bind collaborators through the exact dynamically loaded runtime instance.

    ModuleHost deliberately loads each feature under a hash-qualified module name.
    Importing TaskOutputLeaseCoordinator through its ordinary package name creates
    a second Python class identity, so strict isinstance checks correctly reject it.
    Shared runtime collaborators must come from the loaded module instance.
    """
    module = getattr(loaded_runtime, "module", None)
    runtime = getattr(loaded_runtime, "instance", None)
    coordinator_type = getattr(module, "TaskOutputLeaseCoordinator", None)
    if not callable(coordinator_type) or not callable(
            getattr(runtime, "bind_output_lease", None)) or not callable(
            getattr(task_broker, "bind_runtime", None)):
        raise TypeError("Loaded task runtime composition contract is incomplete")
    coordinator = coordinator_type(pool, topology_reader)
    runtime.bind_output_lease(coordinator)
    if only_chat_target_source is not None:
        from modules.task_session_runtime.isolated_target_backend import (
            DescriptorPrivateDesktopBackend, create_native_private_desktop_session,
        )
        target_source_module = only_chat_target_source.module
        target, target_error = build_only_chat_qa_target(
            module, target_source_module)
        from modules.pomodoro_task_target.module import build_target, require_stopped_instance
        pomodoro, pomodoro_error = build_target(module, target_source_module.APP_PATH_REGISTRY)
        from modules.router_task_target.module import build_target as build_router_private_target
        router_target, router_error = build_router_private_target(
            module, target_source_module.APP_PATH_REGISTRY)
        targets = {item.app_id: item for item in
                   (target, pomodoro, router_target) if item is not None}
        def create_target_session(spec, session_id, *, timeout_seconds):
            if spec.app_id == "pomodoro":
                error = require_stopped_instance()
                if error:
                    raise module.BackendStartError(error, target_created=False)
            return create_native_private_desktop_session(
                spec, session_id, timeout_seconds=timeout_seconds, runtime_types=module)
        backend = DescriptorPrivateDesktopBackend(
            session_factory=create_target_session,
            runtime_types=module,
        )
        runtime.configure_backend(
            backend, targets,
            target_registry_error=None if targets else (
                target_error or pomodoro_error or router_error))
    task_broker.bind_runtime(runtime)
    return coordinator


def output_pool_status_line(pool: dict) -> str:
    """Describe automatic idle return without implying unverified topology changes."""
    state = pool.get("state")
    if state in {"active", "idle_countdown"}:
        remaining = max(0, int(pool.get("idle_remaining_seconds", 0)))
        if int(pool.get("leases", 0)) > 0:
            cleanup_text = "AI 작업 lease 유지 중"
        elif remaining < 60:
            cleanup_text = f"유휴 {remaining}초 뒤 정리 대상 · 확인 주기 최대 15초"
        else:
            cleanup_text = f"유휴 {remaining // 60}분 뒤 정리 대상 · 확인 주기 최대 15초"
        return (f"출력 풀: {pool.get('output_count')}개 · {pool.get('profile')} · "
                f"{cleanup_text} · 열린 창/AI 작업 중에는 대기")
    if state == "cleanup_pending":
        return "출력 풀: 자동 복구 진행 중 · 원래 구성을 확인하고 있습니다"
    if state in {"active_unverified", "cleanup_unverified", "recovery_required"}:
        return "출력 풀: 현재 구성/복구를 검증할 수 없어 변경하지 않고 보존 중"
    if state == "off":
        return "출력 풀: 꺼짐"
    return f"출력 풀 상태 확인 필요 · {state or '미확인'}"


def has_verified_physical_primary(topology: dict) -> bool:
    """Require one live, non-cloned, PnP-identified physical primary before VDD writes."""
    if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
        return False
    displays = topology.get("displays")
    if not isinstance(displays, list) or not displays:
        return False
    primaries = [display for display in displays
                 if isinstance(display, dict) and display.get("primary") is True]
    if len(primaries) != 1:
        return False
    display = primaries[0]
    return (display.get("attached") is True
            and display.get("verified") is True
            and display.get("topology_verified") is True
            and display.get("monitor_identity_verified") is True
            and display.get("virtual") is False
            and display.get("cloned") is False
            and display.get("mirroring_driver") is False
            and bool(str(display.get("monitor_path", "")).strip())
            and bool(str(display.get("display_name", "")).strip()))


def output_pool_topology_status_line(pool: dict, topology: dict) -> str:
    """Keep manager-owned pool state distinct from active unmanaged MTT paths."""
    base = output_pool_status_line(pool)
    try:
        active_mtt = len(verified_mtt_outputs(topology))
    except Exception:
        active_mtt = None
    if active_mtt is not None and pool.get("state") == "off" and active_mtt:
        base = (f"출력 풀: 꺼짐 · 관리 세션 밖의 MTT 가상 화면 {active_mtt}개가 활성 상태이며 "
                "소유권이 확인되지 않아 자동 정리하지 않습니다")
    if not has_verified_physical_primary(topology):
        base += " · 검증된 물리 주 화면이 없어 VDD 변경 잠김"
    return base


def vdd_change_control_state(topology: dict, pool: dict, *, driver_installed: bool,
                             pipe_connected: bool) -> dict:
    """Return UI permissions from live topology and manager ownership, fail closed."""
    physical = has_verified_physical_primary(topology)
    try:
        active_mtt = verified_mtt_outputs(topology) if physical or isinstance(topology, dict) else []
    except Exception:
        active_mtt = None
    state = pool.get("state") if isinstance(pool, dict) else None
    pool_off = state == "off"
    pool_active = state in {"active", "idle_countdown", "cleanup_pending",
                            "active_unverified", "cleanup_unverified"}
    return {
        "enable": bool(driver_installed and physical and pool_off),
        "disable": bool(driver_installed and physical and pool_off and active_mtt == []),
        "start": bool(pipe_connected and physical and pool_off),
        "single": bool(driver_installed and pipe_connected and physical and pool_off),
        "stop": bool(pipe_connected and physical and pool_active),
    }


def output_pool_confirmation_message(output_count: int, profile: str,
                                    previous_count: int = 0) -> str:
    mode = PROFILES.get(profile)
    if (mode is None or isinstance(previous_count, bool)
            or not isinstance(previous_count, int) or previous_count < 0
            or previous_count > output_count):
        raise ValueError("Unsupported output quality profile")
    added = output_count - previous_count
    return (f"MTT 가상 출력 {output_count}개를 사용하고, 각 출력에 "
            f"{mode[0]}×{mode[1]} @ {mode[2]} Hz를 적용합니다.\n\n"
            f"현재 {previous_count}개에서 AI 작업용 출력 {added}개를 추가합니다.\n\n"
            "적용 전 출력 수와 지원 모드를 다시 확인합니다. 작업 lease와 가상 화면의 열린 창이 "
            "모두 없는 상태가 5초 이어지면, 다음 최대 15초 주기 확인에서 관리자가 만든 출력만 "
            "기존 구성으로 자동 복구합니다. 창이나 AI 작업이 사용 중이면 유휴 시간이 다시 계산됩니다. "
            "출력 상태를 정확히 확인할 수 "
            "없으면 복구를 시도하지 않고 현재 출력을 보존합니다. 지금 수동으로 일찍 반환할 때는 "
            "별도 확인이 필요합니다. 계속할까요?")


def private_desktop_summary(state: dict) -> str:
    if state.get("supported"):
        probe = "자체 검사 통과" if state.get("probe") == "passed" else "자체 검사 미실행"
        return (f"Windows private desktop {probe}. Only Chat/Codex 실행 연결은 아직 구현되지 않아 "
                "실제 AI 작업 대여는 계속 거부됩니다.")
    return "Windows private desktop 자체 검사를 사용할 수 없습니다."


class ManagerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        from core.background_ui import BackgroundUI
        self._background_ui = BackgroundUI()
        self.root.title("AI Virtual Display Manager")
        width, height, x, y = manager_window_geometry(
            root.winfo_screenwidth(), root.winfo_screenheight(),
        )
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(720, width), min(500, height))
        self.root.configure(bg=COLORS["background"])
        icon_path = APP_ROOT / "assets" / "ai_virtual_display.ico"
        if icon_path.is_file():
            self.root.iconbitmap(str(icon_path))
        self.host = ModuleHost(APP_ROOT)
        self.host.load("mtt_vdd", APP_ROOT / "modules" / "mtt_vdd" / "module.py")
        self.host.load("display_topology")
        self.host.load("app_window_router")
        self.host.load("display_diagnostics")
        diagnostics = self.host._loaded["display_diagnostics"].instance
        self.host._loaded["app_window_router"].instance.bind_diagnostics(
            lambda event: diagnostics.action("record", event))
        self.host.load("only_chat_debug_display")
        self.host.load("only_chat_task_runtime")
        self.host.load("private_desktop")
        self.host.load("task_session_runtime")
        self.host.load("stardesk_management")
        self.host._loaded["stardesk_management"].instance.bind_dependencies(
            topology_reader=self._current_task_topology)
        self.host.load("virtual_output_pool")
        pool = self.host._loaded["virtual_output_pool"].instance
        pool.configure_persistence(VirtualOutputStateStore())
        pool.configure_backend(
            count_reader=self._pool_output_count,
            count_writer=self._pool_set_output_count,
            mode_reader=self._pool_read_modes,
            mode_writer=self._pool_apply_mode,
            mode_restorer=self._pool_restore_modes,
            output_window_count=self._pool_window_count,
        )
        self.host.load("single_output_control")
        self.host._loaded["single_output_control"].instance.bind(
            read_state=self._single_output_state,
            set_count=self._pool_set_output_count,
        )
        self.host.load("task_display_lease")
        self.host._loaded["task_display_lease"].instance.bind_dependencies(
            pool=pool, topology_reader=self._current_task_topology)
        self.host.load("only_chat_output_lease")
        only_chat_lease = self.host._loaded["only_chat_output_lease"].instance
        only_chat_lease.bind_dependencies(pool=pool, topology_reader=self._current_task_topology)
        only_chat_lease.start_listener()
        loaded_task_runtime = self.host._loaded["task_session_runtime"]
        task_broker = self.host._loaded["only_chat_task_runtime"].instance
        bind_task_runtime_dependencies(
            loaded_task_runtime, pool, self._current_task_topology, task_broker,
            only_chat_target_source=self.host._loaded["only_chat_task_runtime"])
        self.host.load("manager_cli")
        self.host._loaded["manager_cli"].instance.bind_dependencies(
            status_reader=self._manager_cli_status,
            topology_reader=self._current_task_topology,
            pool_status=lambda: self.host.invoke("virtual_output_pool", "pool_status"),
            pool_begin=self._pool_begin_with_physical_guard,
            pool_stop=self._return_output_pool,
            task_action=self._task_runtime_action,
            window_reader=self._manager_cli_window_snapshot,
            ui_action=self._background_ui.request,
            stardesk_reader=lambda: self.host.invoke("stardesk_management", "status"),
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vdd-status")
        self._completed = SimpleQueue()
        self._busy = False
        self._closing = False
        self._shutdown_in_progress = False
        self._shutdown_deadline = None
        self._guard_auto_checked = False
        self._guard_policy_hash = hashlib.sha256((APP_ROOT / "modules/app_window_router/physical_guard.py").read_bytes()).hexdigest()
        self._build()
        self.root.after(40, self._drain_completions)
        self.root.after(1000, self._protection_tick)
        self.root.after(15000, self._pool_tick)
        # CLI-first residency: no wizard, topology scan or window on startup.
        self.host._loaded["display_diagnostics"].instance.action(
            "record", {"event": "manager_state", "result": "started"})
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

    def hide_window(self):
        self.root.withdraw()
        self._background_ui.publish(False)

    def show_window(self):
        self.root.deiconify()
        self._background_ui.publish(True)
        self.refresh()

    def _shutdown_notice(self, code, title, detail):
        background_ui = getattr(self, "_background_ui", None)
        if background_ui is None or background_ui.request("status")["visible"]:
            messagebox.showwarning(title, detail, parent=self.root)
        else:
            self._background_ui.fail(code)
            try:
                atomic_json(Path(os.environ["LOCALAPPDATA"]) / "AI Virtual Display Manager"
                            / "shutdown-error.json",
                            {"error_code": code, "detail": detail[:500], "at": time.time()})
            except Exception:
                pass

    def _build(self):
        outer = tk.Frame(self.root, bg=COLORS["background"], padx=28, pady=24)
        outer.pack(fill="both", expand=True)
        header = tk.Frame(outer, bg=COLORS["background"])
        header.pack(fill="x", pady=(0, 22))
        tk.Label(header, text="AI DISPLAY", bg=COLORS["background"], fg=COLORS["accent"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(header, text="Virtual Display Manager", bg=COLORS["background"],
                 fg=COLORS["text"], font=("Segoe UI Semibold", 24)).pack(anchor="w", pady=(3, 2))
        tk.Label(header, text="AI QA를 위한 별도 화면을 확인하고 안전하게 관리합니다.",
                 bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 11)).pack(anchor="w")

        self.banner = tk.Frame(outer, bg=COLORS["panel"], padx=18, pady=15,
                               highlightthickness=1, highlightbackground=COLORS["border"])
        self.banner.pack(fill="x", pady=(0, 18))
        self.status_title = tk.Label(self.banner, text="상태 확인 중", bg=COLORS["panel"],
                                     fg=COLORS["text"], font=("Segoe UI Semibold", 14))
        self.status_title.pack(anchor="w")
        self.status_detail = tk.Label(self.banner, text="Windows 디스플레이와 VDD 연결을 읽고 있습니다.",
                                      bg=COLORS["panel"], fg=COLORS["muted"],
                                      font=("Segoe UI", 10), justify="left", wraplength=850)
        self.status_detail.pack(anchor="w", pady=(5, 0))
        self.module_detail = tk.Label(self.banner, text="", bg=COLORS["panel"], fg=COLORS["muted"],
                                      font=("Segoe UI", 8))
        self.module_detail.pack(anchor="w", pady=(6, 0))
        self.protection_detail = tk.Label(self.banner, text="메인 모니터 보호 상태 확인 중…",
                                         bg=COLORS["panel"], fg=COLORS["text"],
                                         font=("Segoe UI", 10), wraplength=850, justify="left")
        self.protection_detail.pack(anchor="w", pady=(6, 0))
        resident_controls = tk.Frame(self.banner, bg=COLORS["panel"])
        resident_controls.pack(anchor="w", pady=(6, 0))
        self.startup_button = self._button(resident_controls, "자동 시작 설정", self._toggle_startup)
        self.startup_button.pack(side="left")
        self._button(resident_controls, "개발 일시정지 해제", self._resume_developer_pause).pack(side="left", padx=8)

        cards = tk.Frame(outer, bg=COLORS["background"])
        cards.pack(fill="x")
        cards.columnconfigure((0, 1), weight=1, uniform="cards")
        self.driver_card = self._card(cards, 0, "드라이버", "확인 대기 중")
        self.displays_card = self._card(cards, 1, "현재 연결된 화면", "확인 대기 중")

        controls = tk.Frame(outer, bg=COLORS["background"])
        controls.pack(fill="x", pady=(17, 0))
        row1 = tk.Frame(controls, bg=COLORS["background"])
        row1.pack(fill="x")
        row2 = tk.Frame(controls, bg=COLORS["background"])
        row2.pack(fill="x", pady=(8, 0))
        self.refresh_button = self._button(row1, "상태 새로고침", self.refresh)
        self.refresh_button.pack(side="left")
        self.enable_button = self._button(row1, "드라이버 켜기", lambda: self.device_action("enable"))
        self.enable_button.pack(side="left", padx=(9, 0))
        self.disable_button = self._button(row1, "드라이버 끄기", lambda: self.device_action("disable"))
        self.disable_button.pack(side="left", padx=(9, 0))
        self.start_button = self._button(row1, "AI 화면 만들기", self.start_display, primary=True)
        self.start_button.pack(side="left", padx=(9, 0))
        self.single_output_button = self._button(row1, "가상 모니터 1개 만들기",
                                                 self.create_single_output)
        self.single_output_button.pack(side="left", padx=(9, 0))
        self.stop_button = self._button(row1, "가상 화면 끄기", self.stop_display)
        self.stop_button.pack(side="left", padx=(9, 0))
        self.reload_button = self._button(row2, "기능 모듈 다시 읽기", self.reload_module)
        self.reload_button.pack(side="left")
        self.upstream_button = self._button(row2, "공식 VDD 제어 앱", self.open_upstream_control)
        self.upstream_button.pack(side="left", padx=(9, 0))
        self.settings_button = self._button(row2, "Windows 디스플레이 설정", self.open_display_settings)
        self.settings_button.pack(side="right")
        self.router_button = self._button(row2, "AI 앱 창 배치",
                                          self.open_window_router)
        self.router_button.pack(side="left", padx=(9, 0))
        self.private_probe_button = self._button(row2, "격리 데스크톱 검사",
                                                self.run_private_desktop_probe)
        self.private_probe_button.pack(side="left", padx=(9, 0))
        self.physical_guard_button = self._button(row2, "메인 모니터 창 복구",
                                                  self.open_physical_guard)
        self.physical_guard_button.pack(side="left", padx=(9, 0))
        self.setup_wizard_button = self._button(row2, "초기 보호 설정 마법사",
                                                self.open_setup_wizard)
        self.setup_wizard_button.pack(side="left", padx=(9, 0))

        safety = tk.Frame(outer, bg=COLORS["safety"], padx=16, pady=13,
                          highlightthickness=1, highlightbackground=COLORS["safety_border"])
        safety.pack(fill="x", pady=(20, 0))
        tk.Label(safety, text="격리 상태는 별도 확인이 필요합니다", bg=COLORS["safety"],
                 fg=COLORS["safety_text"], font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.safety_detail = tk.Label(safety,
                 text="가상 모니터가 있다는 사실만으로 AI 전용 격리가 보장되지는 않습니다.",
                 bg=COLORS["safety"], fg=COLORS["muted"], font=("Segoe UI", 9),
                 justify="left", wraplength=870)
        self.safety_detail.pack(anchor="w", pady=(4, 0))

        footer = tk.Frame(outer, bg=COLORS["background"])
        footer.pack(fill="x", side="bottom", pady=(16, 0))
        self._button(footer, "서비스 종료", self.close).pack(side="right")
        tk.Label(footer, text=f"관리 앱 {APP_VERSION}  ·  MikeTheTech VDD 제어 어댑터",
                 bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(side="left")

    def _card(self, parent, column, title, detail):
        frame = tk.Frame(parent, bg=COLORS["panel"], padx=17, pady=16,
                         highlightthickness=1, highlightbackground=COLORS["border"])
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
        tk.Label(frame, text=title.upper(), bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        value = tk.Label(frame, text=detail, bg=COLORS["panel"], fg=COLORS["text"],
                         font=("Segoe UI Semibold", 16), justify="left", wraplength=400)
        value.pack(anchor="w", pady=(8, 3))
        body = tk.Label(frame, text="", bg=COLORS["panel"], fg=COLORS["muted"],
                        font=("Segoe UI", 9), justify="left", wraplength=400)
        body.pack(anchor="w")
        return {"frame": frame, "value": value, "body": body}

    def _button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", cursor="hand2",
                         padx=15, pady=10, bd=0, bg=COLORS["accent"] if primary else COLORS["button"],
                         fg="#ffffff" if primary else COLORS["text"], activebackground=COLORS["accent_hover"],
                         activeforeground="#ffffff", font=("Segoe UI Semibold", 10))

    def _verified_virtual_outputs(self):
        topology = self.host.refresh("display_topology")
        # Output-count decisions must use a fresh driver identity snapshot.
        vdd = self.host._loaded["mtt_vdd"].instance.refresh(force=True)
        device = vdd.get("device", {})
        instance_id = str(device.get("instance_id", "")).strip()
        if (not topology.get("topology_verified") or not instance_id
                or str(device.get("manufacturer", "")).casefold() != "mikethetech"
                or str(device.get("status", "")).casefold() not in {"started", "ok"}
                or device.get("problem_code") is not None):
            raise RuntimeError("VDD 장치와 현재 화면 경로를 검증하지 못했습니다.")
        by_key = {}
        for monitor in vdd.get("monitors", []):
            key = _pnp_monitor_key(monitor.get("InstanceId", ""))
            if key:
                by_key.setdefault(key, []).append(monitor)
        for display in topology.get("displays", []):
            matches = by_key.get(_display_path_monitor_key(display.get("monitor_path", "")), [])
            monitor = matches[0] if len(matches) == 1 else None
            display["monitor_identity_verified"] = bool(monitor)
            display["virtual"] = monitor.get("Virtual") if monitor else None
            display["monitor_parent"] = monitor.get("Parent", "") if monitor else ""
        topology["mtt_device_instance_id"] = instance_id
        outputs = verified_mtt_outputs(topology)
        if len(outputs) != sum(1 for d in topology.get("displays", []) if d.get("virtual") is True):
            raise RuntimeError("하나 이상의 가상 출력 경로가 복제되었거나 식별되지 않아 변경하지 않았습니다.")
        return instance_id, outputs

    def _require_physical_primary_for_vdd_change(self):
        """Freshly prove a physical primary before changing VDD count, mode or device state."""
        from modules.app_window_router.module import current_guard_topology
        topology = current_guard_topology()
        if not has_verified_physical_primary(topology):
            raise RuntimeError("PHYSICAL_PRIMARY_UNVERIFIED")
        return topology

    def _pool_begin_with_physical_guard(self, payload):
        self._require_physical_primary_for_vdd_change()
        return self.host.invoke("virtual_output_pool", "pool_begin", payload)

    def _pool_output_count(self):
        _instance_id, outputs = self._verified_virtual_outputs()
        return len(outputs)

    def _single_output_state(self):
        instance_id, outputs = self._verified_virtual_outputs()
        topology = self.host._loaded["display_topology"].instance.snapshot()
        pool = self.host._loaded["virtual_output_pool"].instance.status()
        return {"mtt_device_instance_id": instance_id,
                "topology_verified": topology.get("topology_verified") is True,
                "outputs": outputs,
                "pool_state": pool.get("state")}

    def _pool_set_output_count(self, count):
        current = self._pool_output_count()
        if current == count:
            return "unchanged"
        self._require_physical_primary_for_vdd_change()
        result = self.host._loaded["mtt_vdd"].instance.command(f"SETDISPLAYCOUNT {int(count)}")
        deadline = time.monotonic() + 25.0
        last_count = current
        while time.monotonic() < deadline:
            time.sleep(0.5)
            last_count = self._pool_output_count()
            if last_count == count:
                return result
        raise TimeoutError(f"가상 출력 수를 확인하지 못했습니다. 요청 {count}, 현재 {last_count}.")

    def _pool_read_modes(self):
        _instance_id, outputs = self._verified_virtual_outputs()
        if not outputs:
            return {}
        result = {}
        for display in outputs:
            mode = (display.get("width"), display.get("height"), display.get("refresh_rate"))
            if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in mode):
                raise RuntimeError("활성 가상 출력의 해상도 상태를 확인하지 못했습니다.")
            key = mode_identity_key(display["display_name"], display["monitor_device_id"],
                                    display["monitor_path"])
            result[key] = mode
        return result

    def _pool_supported_mode_evidence(self):
        _instance_id, outputs = self._verified_virtual_outputs()
        if not outputs:
            manager_cli = self.host._loaded["manager_cli"].instance
            evidence = manager_cli.configured_output_mode_evidence()
            return {"modes": [tuple(mode) for mode in evidence["modes"]],
                    "source": evidence["source"], "sha256": evidence["sha256"]}
        common = None
        for display in outputs:
            modes = {tuple(mode) for mode in display.get("available_modes", [])
                     if isinstance(mode, (list, tuple)) and len(mode) == 3}
            common = modes if common is None else common & modes
        return {"modes": sorted(common or set()), "source": "active_mtt_outputs",
                "sha256": None}

    def _pool_apply_mode(self, mode):
        self._require_physical_primary_for_vdd_change()
        instance_id, outputs = self._verified_virtual_outputs()
        topology_module = self.host._loaded["display_topology"].instance
        for display in outputs:
            if tuple(mode) not in {tuple(item) for item in display.get("available_modes", [])}:
                raise RuntimeError(f"{display['display_name']}에서 선택한 해상도를 지원하지 않습니다.")
        applied = []
        for display in outputs:
            applied.append(topology_module.apply_mtt_mode(
                display_name=display["display_name"],
                monitor_device_id=display["monitor_device_id"],
                monitor_path=display["monitor_path"],
                monitor_parent=display["monitor_parent"], mtt_instance_id=instance_id,
                mode=mode, confirmed=True,
            ))
        return all(item.get("state") in {"applied", "unchanged"} for item in applied)

    def _pool_restore_modes(self, modes):
        if not modes:
            return True
        self._require_physical_primary_for_vdd_change()
        instance_id, outputs = self._verified_virtual_outputs()
        by_key = {mode_identity_key(d["display_name"], d["monitor_device_id"], d["monitor_path"]): d
                  for d in outputs}
        if any(key not in by_key for key in modes):
            return False
        topology_module = self.host._loaded["display_topology"].instance
        for key, mode in modes.items():
            display = by_key[key]
            result = topology_module.apply_mtt_mode(
                display_name=display["display_name"],
                monitor_device_id=display["monitor_device_id"],
                monitor_path=display["monitor_path"],
                monitor_parent=display["monitor_parent"], mtt_instance_id=instance_id,
                mode=mode, confirmed=True,
            )
            if result.get("state") not in {"applied", "unchanged"}:
                return False
        return True

    def _pool_window_count(self):
        router = self.host._loaded["app_window_router"].instance
        state = router.count_virtual_output_windows()
        return int(state["active_window_count"])

    def _task_runtime_action(self, action, payload):
        if action in {"status", "stop", "cancel", "interrupt"}:
            return self.host.invoke_concurrent("task_session_runtime", action, payload)
        return self.host.invoke("task_session_runtime", action, payload, wait_timeout=12)

    def _manager_cli_status(self):
        """Read-only status snapshot for the authenticated local operator CLI."""
        deadline = time.monotonic() + MANAGER_STATUS_WAIT_SECONDS

        def remaining_wait():
            return max(0.0, deadline - time.monotonic())

        def refresh(module_id):
            return self.host.refresh(module_id, wait_timeout=remaining_wait())

        def invoke(module_id, action, payload=None):
            return self.host.invoke(module_id, action, payload,
                                    wait_timeout=remaining_wait())

        modules = {}
        for module_id in sorted(self.host._loaded):
            try:
                modules[module_id] = self.host.info(module_id)
            except Exception:
                modules[module_id] = {"state": "unavailable", "error_code": "MODULE_INFO_UNAVAILABLE"}
        status = {"app_version": APP_VERSION, "pid": os.getpid(), "modules": modules,
                  "ui": self._background_ui.request("status")}
        readers = {
            "driver": lambda: refresh("mtt_vdd"),
            "topology": lambda: refresh("display_topology"),
            "pool": lambda: invoke("virtual_output_pool", "pool_status"),
            "task_runtime": lambda: refresh("task_session_runtime"),
            "task_broker": lambda: refresh("only_chat_task_runtime"),
            "output_lease": lambda: refresh("only_chat_output_lease"),
            "manager_cli": lambda: refresh("manager_cli"),
        }
        for key, reader in readers.items():
            try:
                status[key] = reader()
            except Exception as exc:
                status[key] = {"state": "unavailable",
                               "error_code": _manager_status_error_code(exc)}
        try:
            status["physical_guard"] = invoke("app_window_router", "physical_guard_status")
        except Exception as exc:
            status["physical_guard"] = {"state": "unavailable",
                                         "error_code": _manager_status_error_code(exc)}
        return status

    def _manager_cli_window_snapshot(self):
        """Read bounded, content-free window metadata for the operator CLI."""
        return self.host.invoke("app_window_router", "list_windows",
                                wait_timeout=MANAGER_STATUS_WAIT_SECONDS)

    def _pool_tick(self):
        if self._closing or self._shutdown_in_progress:
            return
        if not self._busy and not self.host.active_operations:
            def inspect_pool():
                try:
                    self.host._loaded["task_display_lease"].instance.reap_expired()
                except Exception:
                    # Lease uncertainty is retained; this probe never changes topology.
                    pass
                try:
                    self.host._loaded["only_chat_output_lease"].instance.reap_expired()
                except Exception:
                    # The lease provider retains unverified cleanup for review.
                    pass
                try:
                    return self.host.invoke("virtual_output_pool", "pool_status")
                except Exception:
                    return {"state": "unknown"}

            def apply_pool_status(state):
                if state.get("state") in {"active", "cleanup_pending", "active_unverified",
                                           "idle_countdown", "cleanup_unverified"}:
                    self._run("가상 화면 유휴 자동 복구 상태를 확인하는 중…",
                              lambda: self.host.invoke("virtual_output_pool", "pool_reconcile"),
                              self._pool_reconciled)

            self._run("", inspect_pool, apply_pool_status, quiet=True)
        self.root.after(15000, self._pool_tick)

    @staticmethod
    def _current_task_topology():
        from modules.app_window_router.module import current_guard_topology
        return current_guard_topology()

    def _pool_reconciled(self, result):
        state = result.get("state")
        if state == "off" and result.get("expired"):
            self.status_title.configure(text="원래 가상 출력 구성 확인 완료")
            self.status_detail.configure(text=(f"출력 수 {result.get('restored_count')}개가 이미 복구된 상태임을 확인했습니다. "
                                               "이 확인 동작은 디스플레이 설정을 변경하지 않았습니다."))
        elif state == "cleanup_required":
            self.status_title.configure(text="가상 출력 유지 · 종료 상태 확인 필요")
            self.status_detail.configure(text="가상 출력 상태를 검증할 수 없어 자동 복구하지 않았습니다. 상태를 확인하세요.")
        elif state == "idle_countdown":
            remaining = max(0, int(result.get("idle_remaining_seconds", 0)))
            self.status_title.configure(text="AI 가상 출력 사용 대기 중")
            if remaining == 0:
                message = "활성 작업과 창이 없음을 확인하는 즉시 원래 출력 구성으로 복구합니다."
            elif remaining < 60:
                message = f"활성 작업과 창이 없으면 {remaining}초 뒤 원래 출력 구성으로 복구합니다."
            else:
                minutes = (remaining + 59) // 60
                message = f"활성 작업과 창이 없으면 {minutes}분 뒤 원래 출력 구성으로 복구합니다."
            self.status_detail.configure(text=message)
        elif state in {"cleanup_unverified", "active_unverified"}:
            self.status_title.configure(text="가상 출력 유지 · 정리 확인 필요")
            self.status_detail.configure(text=str(result.get("reason") or "출력 상태를 확인할 수 없습니다."))

    def _output_pool_dialog(self):
        if self._busy or self._closing:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("AI 가상 화면 요청")
        dialog.geometry("600x390")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=COLORS["background"], padx=24, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="AI 작업용 가상 화면", bg=COLORS["background"],
                 fg=COLORS["text"], font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(body, text="필요한 출력 수와 지원되는 해상도를 선택합니다. 실제 적용 전 현재 구성을 다시 확인합니다.",
                 bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 10),
                 wraplength=540, justify="left").pack(anchor="w", pady=(6, 18))
        form = tk.Frame(body, bg=COLORS["background"])
        form.pack(fill="x")
        count_var = tk.IntVar(value=2)
        profile_names = {"balanced": "1080p · 60 Hz", "high": "1440p · 60 Hz", "ultra": "4K · 30 Hz"}
        profile_var = tk.StringVar(value="high")
        tk.Label(form, text="AI 작업용으로 새로 추가할 출력 수 (2–8)", bg=COLORS["background"], fg=COLORS["text"]).grid(row=0, column=0, sticky="w", pady=5)
        count_spin = tk.Spinbox(form, from_=2, to=8, textvariable=count_var, width=8)
        count_spin.grid(row=0, column=1, sticky="w", padx=(16, 0), pady=5)
        tk.Label(form, text="해상도 프로필", bg=COLORS["background"], fg=COLORS["text"]).grid(row=1, column=0, sticky="w", pady=5)
        profile_box = ttk.Combobox(form, state="readonly", width=24,
                                   values=[profile_names[key] for key in ("balanced", "high", "ultra")])
        profile_box.set(profile_names["high"])
        profile_box.grid(row=1, column=1, sticky="w", padx=(16, 0), pady=5)
        status = tk.Label(body, text="먼저 현재 가상 출력과 지원 모드를 확인하세요.",
                          bg=COLORS["panel"], fg=COLORS["muted"], justify="left",
                          wraplength=530, padx=13, pady=12)
        status.pack(fill="x", pady=(14, 0))
        preview = {"count": None, "new_count": None, "previous_count": None, "modes": None}
        controls = tk.Frame(body, bg=COLORS["background"])
        controls.pack(fill="x", side="bottom", pady=(18, 0))
        apply_button = self._button(controls, "가상 화면 만들기", lambda: None, primary=True)
        apply_button.pack(side="left")
        apply_button.configure(state="disabled")
        self._button(controls, "현재 구성 확인", lambda: preview_current()).pack(side="left", padx=(8, 0))
        self._button(controls, "닫기", dialog.destroy).pack(side="right")

        def chosen_profile():
            label = profile_box.get()
            return next((key for key, value in profile_names.items() if value == label), "high")

        def preview_current():
            preview["count"] = None
            preview["new_count"] = None
            preview["previous_count"] = None
            preview["modes"] = None
            preview["mode_evidence"] = None
            apply_button.configure(state="disabled")
            try:
                requested = int(count_var.get())
            except (ValueError, tk.TclError):
                status.configure(text="출력 수를 2–8 사이 정수로 입력하세요.")
                return
            selected = chosen_profile()

            def collect_preview():
                self._require_physical_primary_for_vdd_change()
                current = self._pool_output_count()
                evidence = self._pool_supported_mode_evidence()
                return {"current": current, "modes": evidence["modes"],
                        "mode_evidence": evidence["source"], "requested": requested,
                        "total": current + requested,
                        "profile": selected}

            def show_preview(result):
                expected = PROFILES[result["profile"]]
                if result["total"] > 8:
                    status.configure(text=(f"현재 {result['current']}개에서 {result['requested']}개를 추가하면 "
                                           "드라이버 최대 8개를 넘습니다. 새 출력 수를 낮춰 주세요."))
                    return
                if expected not in result["modes"]:
                    status.configure(text=(f"현재 출력 모두가 {profile_names[result['profile']]} 모드를 지원하지 않아 적용할 수 없습니다. "
                                           "지원 모드를 자동으로 낮추지 않습니다."))
                    return
                preview["count"] = result["total"]
                preview["new_count"] = result["requested"]
                preview["previous_count"] = result["current"]
                preview["modes"] = result["modes"]
                preview["mode_evidence"] = result["mode_evidence"]
                evidence_detail = (
                    "VDD 설정에 선언된 모드이며, 실제 지원 여부는 출력 생성 후 재검증합니다."
                    if result["mode_evidence"] == "configured_vdd_settings" else
                    "현재 활성 MTT 출력의 공통 모드입니다."
                )
                status.configure(text=(f"현재 {result['current']}개 → AI 작업용 {result['requested']}개 추가 "
                                       f"→ 총 {result['total']}개 · {expected[0]}×{expected[1]} @ {expected[2]} Hz\n"
                                       f"{evidence_detail} 복제/출력 identity를 적용 직전 다시 검사합니다."))
                apply_button.configure(state="normal")

            self._run("가상 출력 수와 공통 지원 해상도를 읽는 중…", collect_preview, show_preview)

        def apply_request():
            if preview["count"] is None:
                return
            profile = chosen_profile()
            message = output_pool_confirmation_message(
                preview["count"], profile, preview["previous_count"])
            if preview.get("mode_evidence") == "configured_vdd_settings":
                message += ("\n\n현재 활성 MTT 출력이 없어 VDD 설정 파일의 선언 모드를 사용합니다. "
                            "선언은 실제 지원을 보장하지 않으며, 출력 생성 후 모드를 검증하고 "
                            "검증에 실패하면 풀을 복구합니다.")
            if not messagebox.askyesno("가상 화면 구성 변경", message, parent=dialog):
                return
            payload = {"output_count": preview["count"], "profile": profile,
                       "supported_modes": preview["modes"], "confirmed": True}

            def complete(result):
                dialog.destroy()
                self.status_title.configure(text="AI 가상 화면 준비 완료")
                self.status_detail.configure(text=(f"AI 작업용 {result['plan']['outputs_to_add']}개 추가 · "
                                                   f"총 {result['plan']['output_count']}개 출력 · "
                                                   f"{result['plan']['width']}×{result['plan']['height']} @ "
                                                   f"{result['plan']['refresh_rate']} Hz · 유휴 5초 후, "
                                                   "다음 최대 15초 주기에서 자동 복구"))

            self._run("가상 출력과 정확한 해상도를 적용하고 다시 확인하는 중…",
                      lambda: self.host.invoke("virtual_output_pool", "pool_begin", payload), complete)

        apply_button.configure(command=apply_request)

    def refresh(self):
        def collect():
            result = {}
            for module_id, key in (("mtt_vdd", "vdd"), ("display_topology", "topology"),
                                   ("only_chat_debug_display", "only_chat_broker"),
                                   ("only_chat_task_runtime", "task_runtime_broker"),
                                   ("private_desktop", "private_desktop"),
                                   ("task_display_lease", "task_display_lease"),
                                   ("only_chat_output_lease", "only_chat_output_lease")):
                try:
                    result[key] = self.host.refresh(module_id)
                except Exception as exc:
                    result[key] = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                result["output_pool"] = self.host.invoke("virtual_output_pool", "pool_status")
            except Exception as exc:
                result["output_pool"] = {"state": "unknown", "error": f"{type(exc).__name__}: {exc}"}
            return result
        self._run("드라이버와 Windows 활성 화면 경로를 읽는 중…", collect, self._show_status)

    def _show_status(self, result):
        snap = result.get("vdd", {})
        topology = result.get("topology", {})
        driver = snap.get("device", {})
        displays = topology.get("displays", [])
        monitors = snap.get("monitors", [])
        by_monitor = {}
        for monitor in monitors:
            key = _pnp_monitor_key(monitor.get("InstanceId", ""))
            if key:
                by_monitor.setdefault(key, []).append(monitor)
        for display in displays:
            matches = by_monitor.get(_display_path_monitor_key(display.get("monitor_path", "")), [])
            match = matches[0] if len(matches) == 1 else None
            if match:
                display["monitor_identity_verified"] = True
                display["monitor_parent"] = match.get("Parent", "")
                display["name"] = match.get("FriendlyName") or display["name"]
                display["virtual"] = bool(match.get("Virtual"))
            else:
                display["monitor_identity_verified"] = False
                display["monitor_parent"] = ""
                display["virtual"] = None
        topology["mtt_device_instance_id"] = str(driver.get("instance_id", ""))
        pipe = snap.get("pipe", {})
        status = driver.get("status", "unknown")
        problem_code = driver.get("problem_code")
        if snap.get("error"):
            title, detail = "VDD 상태를 조회하지 못했습니다", snap["error"]
        elif problem_code == 22:
            title = "Windows에서 가상 디스플레이 장치를 꺼 두었습니다"
            detail = "장치 관리자 문제 코드 22입니다. ‘드라이버 켜기’를 누르면 확인 후 Windows 관리자 권한을 요청합니다."
        elif status.lower() in {"started", "ok"} and pipe.get("connected"):
            title = "가상 디스플레이 드라이버가 연결되어 있습니다"
            detail = "VDD 제어 채널 응답을 받았습니다." if pipe.get("reply_received", True) else "VDD 제어 채널에 PING 전송을 완료했습니다. 드라이버가 응답 데이터 없이 연결을 종료했습니다."
        elif driver.get("installed"):
            title, detail = "드라이버 설치됨 · 제어 채널 미응답", f"장치 상태: {status}. 가상 화면 제어를 사용할 수 없습니다."
        else:
            title, detail = "가상 디스플레이 드라이버를 찾지 못했습니다", "드라이버 상태를 확인하고 있습니다. 설치나 변경은 자동으로 하지 않습니다."
        self.status_title.configure(text=title)
        topology_ok = bool(topology.get("topology_verified"))
        topology_state = "활성 화면 경로와 복제 여부 확인 완료" if topology_ok else f"화면 복제 여부 확인 불가: {topology.get('error') or topology.get('topology_verified', '미확인')}"
        pool = result.get("output_pool", {})
        pool_line = output_pool_topology_status_line(pool, topology)
        lease = result.get("task_display_lease", {})
        if lease.get("provider_available") is True:
            lease_line = ("Workbench lease broker ready · выдаётся только при явном пуле, "
                          "активном непродублированном MTT-экране и совпадении процесса")
        else:
            lease_line = ("Workbench lease broker unavailable · "
                          + str(lease.get("error_code") or "not verified"))
        only_chat_lease = result.get("only_chat_output_lease", {})
        if only_chat_lease.get("provider_available") is True:
            only_chat_lease_line = (
                "Only Chat VDM output lease ready · manager-created output only; display scope only")
        else:
            only_chat_lease_line = ("Only Chat VDM output lease unavailable · "
                                    + str(only_chat_lease.get("error_code") or "not verified"))
        age_ms = snap.get("status_age_ms")
        status_age = (f" · PnP 상태 관측 {age_ms}ms 전" if isinstance(age_ms, int)
                      else " · PnP 관측 시각 없음")
        self.status_detail.configure(text=(f"{detail}{status_age}\n{topology_state}\n{pool_line}\n"
                                           f"{lease_line}\n{only_chat_lease_line}"))
        self.driver_card["value"].configure(text="설치됨" if driver.get("installed") else "찾을 수 없음")
        problem_text = f" · Windows 문제 코드: {problem_code}" if problem_code is not None else " · 현재 문제 코드 없음"
        self.driver_card["body"].configure(text=f"{driver.get('friendly_name', 'MikeTheTech VDD')}\n장치 상태: {status}{problem_text}\n파이프: {'연결됨' if pipe.get('connected') else '응답 없음'} · 버전: {driver.get('driver_version') or '확인 불가'}")
        controls = vdd_change_control_state(
            topology, pool, driver_installed=driver.get("installed") is True,
            pipe_connected=pipe.get("connected") is True)
        self.enable_button.configure(state="normal" if controls["enable"] else "disabled")
        self.disable_button.configure(state="normal" if controls["disable"] else "disabled")
        self.start_button.configure(state="normal" if controls["start"] else "disabled")
        self.single_output_button.configure(state="normal" if controls["single"] else "disabled")
        self.stop_button.configure(state="normal" if controls["stop"] else "disabled")
        if displays:
            lines = []
            for d in displays:
                kind = "가상 VDD" if d.get("virtual") is True else "물리" if d.get("virtual") is False else "분류 미확인"
                clone = "복제 경로" if d.get("cloned") else "복제 아님 확인" if d.get("topology_verified") else "복제 여부 미확인"
                lines.append(f"{d['name']} · {d.get('width') or '?'}×{d.get('height') or '?'} · {kind} · {clone}")
            self.displays_card["value"].configure(text=f"{len(displays)}개 활성 출력")
            self.displays_card["body"].configure(text="\n".join(lines))
        else:
            self.displays_card["value"].configure(text="활성 화면 없음")
            self.displays_card["body"].configure(text="Windows 활성 출력 경로가 없습니다.")
        candidate = any(d.get("virtual") is True and not d.get("cloned") and d.get("topology_verified") for d in displays)
        if candidate:
            safety_text = "분리된 가상 출력 후보가 확인됐습니다. 이것만으로 앱/프로세스 격리나 AI 전용 화면이 보장되지는 않습니다."
        elif topology_ok:
            safety_text = "활성 출력 및 복제 상태는 읽었지만, 현재 AI QA용 분리 가상 출력을 확인하지 못했습니다."
        else:
            safety_text = "활성 경로를 전부 확인하지 못해 가상 화면의 물리 화면 분리 여부를 판정할 수 없습니다."
        safety_text += "\n" + only_chat_broker_summary(result.get("only_chat_broker", {}))
        safety_text += "\n" + task_runtime_broker_summary(result.get("task_runtime_broker", {}))
        safety_text += "\n" + private_desktop_summary(result.get("private_desktop", {}))
        self.safety_detail.configure(text=safety_text)
        info = [self.host.module_info(k) for k in
                ("mtt_vdd", "display_topology", "display_diagnostics", "app_window_router", "only_chat_debug_display",
                 "only_chat_task_runtime",
                 "private_desktop", "task_session_runtime", "virtual_output_pool",
                 "single_output_control",
                 "task_display_lease", "only_chat_output_lease")]
        self.module_detail.configure(text=" · ".join(f"{x['module_id']} v{x['version']} sha256 {x['sha256'][:10]} g{x['generation']}" for x in info))
        if not self._guard_auto_checked:
            self._guard_auto_checked = True
            self._run("창 보호 상태를 확인하는 중…", self._ensure_default_guard,
                      self._show_guard_start_result, quiet=True)

    def _ensure_default_guard(self):
        try:
            if resident_controller().status()["developer_paused"]:
                return
            status = self.host.invoke("app_window_router", "physical_guard_status")
            pref = status.get("preference_enabled", False)
            running = status.get("runtime", {}).get("enabled", False)
            if pref and not status.get("settings_error") and not running:
                self.host.invoke("app_window_router", "physical_guard_start", {"confirmed": True})
            return None
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"

    def _show_guard_start_result(self, error):
        if error and not self._closing:
            self.module_detail.configure(text=f"창 보호 시작 실패: {error}")

    def _toggle_startup(self):
        try:
            controller = resident_controller()
            if controller.status()["startup_enabled"]:
                controller.disable()
            else:
                controller.enable()
        except Exception as exc:
            messagebox.showerror("자동 시작 변경 실패", str(exc), parent=self.root)

    def _resume_developer_pause(self):
        try:
            resident_controller().resume()
        except Exception as exc:
            messagebox.showerror("개발 일시정지 복구 실패", str(exc), parent=self.root)

    def _protection_tick(self):
        if self._closing or self._shutdown_in_progress:
            return
        if self._busy or self.host.active_operations:
            report = {"pid": os.getpid(), "app_version": APP_VERSION, "canonical_root": str(APP_ROOT),
                      "updated_at": time.time(), "status": "deferred_manager_operation_active"}
            try:
                atomic_json(Path(os.environ["LOCALAPPDATA"]) / "AI Virtual Display Manager"
                            / "runtime-status.json", report)
            except Exception:
                pass
            self.root.after(2000, self._protection_tick)
            return

        def collect_protection_status():
            report = {"pid": os.getpid(), "app_version": APP_VERSION, "canonical_root": str(APP_ROOT),
                      "updated_at": time.time()}
            try:
                resident = resident_controller().status()
                router = self.host._loaded["app_window_router"].instance
                guard_error = None
                if resident["developer_paused"] and router.guard:
                    router.guard.stop()  # Preserve preference while the dispatcher is paused.
                elif not resident["developer_paused"]:
                    guard_error = self._ensure_default_guard()
                status = self.host.invoke("app_window_router", "physical_guard_status")
                report.update(resident=resident, protection=status,
                              router=self.host.info("app_window_router"),
                              policy_sha256=self._guard_policy_hash)
                labels = {"running": "작동 중", "off": "꺼짐", "waiting_for_physical_monitor": "실제 모니터 연결 대기",
                          "error": "오류 · 재확인 중", "stop_unverified": "중지 확인 필요"}
                runtime = status.get("runtime", {})
                text = (f"메인 모니터 창 복구: {labels.get(runtime.get('state'), runtime.get('state'))} · "
                        f"복구 {runtime.get('moved_count', 0)}개 · 로그인 시 자동 시작: "
                        f"{'켜짐' if resident['startup_enabled'] else '꺼짐'}")
                if resident["developer_paused"]:
                    text += f" · 개발 일시정지: {resident['pause']['task_id']} (복구 대기)"
                if status.get("settings_error") or runtime.get("error"):
                    text += f" · 오류: {status.get('settings_error') or runtime.get('error')}"
                if guard_error:
                    text += f" · 창 보호 시작 실패: {guard_error}"
                report["status_text"] = text
                report["startup_button_text"] = ("자동 시작 끄기" if resident["startup_enabled"]
                                                  else "자동 시작 켜기")
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc) == "Another manager operation is already active":
                    report["status"] = "deferred_manager_operation_active"
                else:
                    report["error"] = f"{type(exc).__name__}: {exc}"
            try:
                atomic_json(Path(os.environ["LOCALAPPDATA"]) / "AI Virtual Display Manager"
                            / "runtime-status.json", report)
            except Exception as exc:
                report["write_error"] = f"{type(exc).__name__}: {exc}"
            return report

        def apply_protection_status(report):
            if self._closing:
                return
            if report.get("status_text"):
                self.protection_detail.configure(text=report["status_text"])
            elif report.get("error"):
                self.protection_detail.configure(text=f"창 복구 상태 확인 실패: {report['error']}")
            elif report.get("write_error"):
                self.protection_detail.configure(text=f"상태 기록 실패: {report['write_error']}")
            if report.get("resident"):
                self.startup_button.configure(
                    text=report.get("startup_button_text", "자동 시작 켜기"))

        self._run("", collect_protection_status, apply_protection_status, quiet=True)
        self.root.after(2000, self._protection_tick)

    def _run(self, working_text, work, done, *, quiet=False):
        if self._busy or self._closing:
            return False
        buttons = (self.refresh_button, self.enable_button, self.disable_button,
                   self.start_button, self.stop_button, self.reload_button,
                   self.router_button, self.private_probe_button, self.physical_guard_button,
                   self.single_output_button)
        button_states = [(button, button.cget("state")) for button in buttons]
        try:
            future = self._executor.submit(work)
        except Exception as exc:
            for button, state in button_states:
                button.configure(state=state)
            if quiet:
                self.protection_detail.configure(
                    text=f"백그라운드 작업을 시작하지 못했습니다: {type(exc).__name__}: {exc}")
            else:
                self.status_title.configure(text="작업을 시작하지 못했습니다")
                self.status_detail.configure(text=f"{type(exc).__name__}: {exc}")
            return False
        self._busy = True
        if not quiet:
            self.status_detail.configure(text=working_text)
        if quiet:
            self._quiet_button_states = button_states
        for button in buttons:
            button.configure(state="disabled")
        future.add_done_callback(lambda f: self._completed.put((f, done, quiet)))
        return True

    def _drain_completions(self):
        if self._closing:
            return
        action = self._background_ui.take()
        if action == "show":
            self.show_window()
        elif action == "hide":
            self.hide_window()
        elif action == "stop":
            if self._busy:
                self._background_ui.fail("MANAGER_OPERATION_BUSY")
            else:
                self.close(verify_no_active_task=True)
            if self._closing:
                return
        self._background_ui.publish(self.root.state() == "normal")
        while True:
            try:
                completion = self._completed.get_nowait()
            except Empty:
                break
            future, done = completion[:2]
            quiet = bool(completion[2]) if len(completion) > 2 else False
            self._finish(future, done, quiet=quiet)
        if not self._closing:
            self.root.after(40 if self._busy or self.root.state() == "normal" else 250,
                            self._drain_completions)

    def _finish(self, future, done, *, quiet=False):
        if self._closing:
            return
        self._busy = False
        try:
            result = future.result()
        except Exception as exc:
            if quiet:
                self.protection_detail.configure(
                    text=f"백그라운드 상태 확인 실패: {type(exc).__name__}: {exc}")
                self._restore_quiet_buttons()
                return
            self.status_title.configure(text="작업을 완료하지 못했습니다")
            self.status_detail.configure(text=f"{type(exc).__name__}: {exc}")
            self.refresh_button.configure(state="normal")
            self.reload_button.configure(state="normal")
            self.router_button.configure(state="normal")
            self.physical_guard_button.configure(state="normal")
            self.enable_button.configure(state="disabled")
            self.disable_button.configure(state="disabled")
            self.start_button.configure(state="disabled")
            self.single_output_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            return
        if quiet:
            self._restore_quiet_buttons()
            done(result)
            return
        self.refresh_button.configure(state="normal")
        self.reload_button.configure(state="normal")
        self.router_button.configure(state="normal")
        self.physical_guard_button.configure(state="normal")
        self.single_output_button.configure(state="normal")
        done(result)

    def _restore_quiet_buttons(self):
        states = getattr(self, "_quiet_button_states", None)
        self._quiet_button_states = None
        if states is not None:
            for button, state in states:
                button.configure(state=state)

    def reload_module(self):
        if self._busy:
            return
        pool = self.host.invoke("virtual_output_pool", "pool_status")
        if pool.get("state") != "off":
            messagebox.showinfo("가상 출력 세션 실행 중",
                                "출력 세션을 직접 반환해 복구할 때까지 모듈을 다시 읽을 수 없습니다.",
                                parent=self.root)
            return
        state = self.host.invoke("app_window_router", "physical_guard_status")
        if state.get("runtime", {}).get("enabled"):
            messagebox.showinfo("창 보호 실행 중", "창 보호를 중지한 뒤 모듈을 다시 읽을 수 있습니다.", parent=self.root)
            return
        self._run("모듈 상태를 저장하고 안전하게 다시 읽는 중…",
                  lambda: self.host.reload("mtt_vdd"), self._module_reloaded)

    def open_setup_wizard(self):
        FirstRunWizardDialog(self.root, on_complete=self.refresh)

    def open_physical_guard(self):
        if self._busy or self._closing:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("메인 모니터 창 복구")
        dialog.geometry("620x350")
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["background"])
        tk.Label(dialog, text="앱 창을 실제 메인 모니터로 복구",
                 bg=COLORS["background"], fg=COLORS["text"],
                 font=("Segoe UI Semibold", 16)).pack(anchor="w", padx=24, pady=(22, 8))
        tk.Label(dialog,
                 text=("가상 화면이나 화면 밖에 있는 일반 앱 창을 검증된 실제 주 모니터로 복구합니다. "
                       "ChatGPT도 포함하며, 모니터 재연결 시 기존 창을 다시 확인합니다. "
                       "명시된 AI 작업 창·Only Chat·터미널은 제외하고, 최소화된 창은 직접 복원하지 않습니다. "
                       "마우스 잠금과 모니터 연결 설정은 바꾸지 않습니다."),
                 bg=COLORS["background"], fg=COLORS["muted"], wraplength=560,
                 justify="left", font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(0, 14))
        status = tk.Label(dialog, text="상태 확인 중…", bg=COLORS["background"],
                          fg=COLORS["text"], justify="left", wraplength=560)
        status.pack(anchor="w", padx=24, pady=(0, 18))
        controls = tk.Frame(dialog, bg=COLORS["background"])
        controls.pack(fill="x", padx=24)

        def show_status(result):
            runtime = result.get("runtime", {})
            state = runtime.get("state", "off")
            if result.get("preference_enabled") and not runtime.get("enabled"):
                state = "이전 실행 흔적 · 현재 감시 꺼짐"
            status.configure(text=(f"감시 상태: {state} · 이번 실행에서 교정한 창: "
                                   f"{runtime.get('moved_count', 0)}개"
                                   + (f" · 오류: {runtime['error']}" if runtime.get("error") else "")
                                   + (f" · 설정 저장: {result['settings_error']}"
                                      if result.get("settings_error") else "")))
            start.configure(state="disabled" if runtime.get("enabled") else "normal")
            stop.configure(state="normal" if runtime.get("enabled") else "disabled")

        def refresh_status():
            self._run("창 보호 상태를 확인하는 중…",
                      lambda: self.host.invoke("app_window_router", "physical_guard_status"),
                      show_status)

        def start_guard():
            confirm = ("일반 앱 창의 위치를 확인하고 가상 화면/화면 밖의 창을 실제 주 모니터로 복구합니다. "
                       "모니터 재연결 때도 다시 확인합니다. 보호를 켤까요?")
            if not messagebox.askyesno("창 보호 시작", confirm, parent=dialog):
                return
            self._run("물리 모니터와 가상 출력을 검증하고 창 보호를 시작하는 중…",
                      lambda: self.host.invoke("app_window_router", "physical_guard_start",
                                                {"confirmed": True}), show_status)

        def stop_guard():
            self._run("창 보호를 중지하고 감시 스레드 종료를 확인하는 중…",
                      lambda: self.host.invoke("app_window_router", "physical_guard_stop"), show_status)

        start = self._button(controls, "보호 켜기", start_guard, primary=True)
        start.pack(side="left")
        stop = self._button(controls, "보호 중지", stop_guard)
        stop.pack(side="left", padx=(10, 0))
        self._button(controls, "상태 새로고침", refresh_status).pack(side="left", padx=(10, 0))
        self._button(controls, "닫기", dialog.destroy).pack(side="right")
        refresh_status()

    def _module_reloaded(self, info):
        self.module_detail.configure(text=f"mtt_vdd · v{info['version']} · sha256 {info['sha256'][:12]} · generation {info['generation']} · reload ok")
        self.refresh()

    def _change_count(self, count):
        if not messagebox.askyesno("화면 구성 변경", "가상 디스플레이 구성을 변경합니다. Windows 화면 배치가 바뀔 수 있습니다. 계속할까요?", parent=self.root):
            return
        self._run("요청한 디스플레이 변경을 처리하는 중…",
                  lambda: self._guarded_manual_count_change(count),
                  self._command_complete)

    def _guarded_manual_count_change(self, count):
        if type(count) is not int or not 0 <= count <= 8:
            raise ValueError("OUTPUT_COUNT_INVALID")
        self._require_physical_primary_for_vdd_change()
        pool = self.host.invoke("virtual_output_pool", "pool_status")
        if pool.get("state") != "off":
            raise RuntimeError("OUTPUT_POOL_OWNS_ACTIVE_SESSION")
        return self.host.command("mtt_vdd", f"SETDISPLAYCOUNT {count}")

    def _command_complete(self, result):
        messagebox.showinfo("VDD 응답", result or "명령을 보냈습니다. 상태를 다시 확인하세요.", parent=self.root)
        self.root.after(600, self.refresh)

    def device_action(self, action):
        try:
            self._require_physical_primary_for_vdd_change()
        except Exception as exc:
            messagebox.showwarning("물리 주 화면 확인 필요",
                                   f"실제 물리 주 화면이 검증되지 않아 VDD 장치를 변경하지 않습니다.\n{exc}",
                                   parent=self.root)
            return
        if action == "disable":
            try:
                pool = self.host.invoke("virtual_output_pool", "pool_status")
            except Exception as exc:
                messagebox.showwarning("가상 출력 상태 미확인",
                                       f"출력 풀 상태를 확인할 수 없어 드라이버를 끄지 않습니다.\n{exc}",
                                       parent=self.root)
                return
            if pool.get("state") not in {"off"}:
                messagebox.showwarning("가상 출력 사용 중",
                                       "출력 세션이 사용 중이거나 상태를 확인할 수 없습니다. 먼저 출력 세션을 안전하게 반환하세요.",
                                       parent=self.root)
                return
            try:
                if self._verified_virtual_outputs()[1]:
                    messagebox.showwarning("가상 출력 사용 중",
                                           "활성 MTT 출력이 남아 있습니다. 풀 상태만 보고 드라이버를 끄지 않습니다.",
                                           parent=self.root)
                    return
            except Exception as exc:
                messagebox.showwarning("가상 출력 상태 미확인",
                                       f"활성 출력 신원을 확인할 수 없어 드라이버를 끄지 않습니다.\n{exc}",
                                       parent=self.root)
                return
        action_label = "켜기" if action == "enable" else "끄기"
        warning = (f"MikeTheTech 가상 디스플레이 장치를 {action_label} 위해 Windows 관리자 권한(UAC)을 요청합니다.\n\n"
                   "디스플레이 구성이 변경되거나 창 배치가 달라질 수 있습니다. 물리 화면을 AI 전용 가상 화면으로 바꾸지는 않으며, 화면 복제·격리는 별도로 검증해야 합니다. 진행할까요?")
        if not messagebox.askyesno("드라이버 상태 변경", warning, parent=self.root):
            return
        self._run("Windows UAC 승인 결과와 드라이버 상태를 기다리는 중…",
                  lambda: self._guarded_device_action(action), self._device_action_complete)

    def _guarded_device_action(self, action):
        self._require_physical_primary_for_vdd_change()
        if action == "disable":
            pool = self.host.invoke("virtual_output_pool", "pool_status")
            if pool.get("state") != "off" or self._verified_virtual_outputs()[1]:
                raise RuntimeError("ACTIVE_VDD_OUTPUTS_REMAIN")
        return self.host.device_action("mtt_vdd", action)

    def _device_action_complete(self, result):
        messagebox.showinfo("드라이버 요청", result, parent=self.root)
        self.root.after(1500, self.refresh)

    def start_display(self):
        self._output_pool_dialog()

    def create_single_output(self):
        if self._busy or self._closing:
            return
        def preview_done(result):
            if result.get("state") == "ready":
                self.status_title.configure(text="DISPLAY10 가상 모니터 확인 완료")
                self.status_detail.configure(text=("MTT 장치에 연결된 단일 비복제 출력을 확인했습니다. "
                                                   "이 출력의 격리 여부는 별도 검증이 필요합니다."))
                return
            prompt = (
                "현재 검증된 MTT 출력은 0개입니다. DISPLAY10 가상 출력 하나만 추가합니다.\n\n"
                "해상도 프로필이나 기존 물리 화면 설정은 변경하지 않습니다. Windows가 실제 출력 이름을 "
                "배정한 뒤 DISPLAY10인지, MTT 장치에 연결됐는지, 복제되지 않았는지 확인합니다. "
                "DISPLAY10이 아니거나 검증이 실패하면 출력을 자동 제거하지 않고 상태를 보존해 멈춥니다.\n\n"
                "계속할까요?")
            if not messagebox.askyesno("DISPLAY10 단일 가상 모니터", prompt, parent=self.root):
                return
            self._run("DISPLAY10 가상 출력 하나를 요청하고 경로를 검증하는 중…",
                      self._create_single_output,
                      self._single_output_complete)
        self._run("현재 MTT 장치, 출력 및 작업 풀 상태를 다시 확인하는 중…",
                  lambda: self.host.invoke("single_output_control", "preview"), preview_done)

    def _create_single_output(self):
        try:
            return self.host.invoke("single_output_control", "create", {"confirmed": True})
        except Exception as exc:
            return {"state": "verification_failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500]}

    def _single_output_complete(self, result):
        if result.get("state") == "ready":
            self.status_title.configure(text="DISPLAY10 단일 가상 모니터 확인 완료")
            self.status_detail.configure(text=("활성 MTT 출력 1개가 DISPLAY10이며 비복제 경로임을 확인했습니다. "
                                               "사용자 앱을 열기 전 별도 격리 상태를 확인하세요."))
            self.root.after(250, self.refresh)
            return
        messagebox.showwarning("단일 출력 검증 실패",
                               str(result.get("error") or result), parent=self.root)
        self.root.after(250, self.refresh)

    def _return_output_pool(self):
        action = "pool_retire_detached" if self._pool_output_count() == 0 else "pool_stop"
        return self.host.invoke("virtual_output_pool", action, {"confirmed": True})

    def stop_display(self):
        pool = self.host.invoke("virtual_output_pool", "pool_status")
        if pool.get("state") != "off":
            message = ("AI 가상 출력 세션을 지금 원래 출력 수와 해상도로 복구합니다. "
                       "가상 출력에 남아 있는 창이나 heartbeat lease가 있으면 안전을 위해 거부됩니다. 계속할까요?")
            if not messagebox.askyesno("가상 출력 세션 반환", message, parent=self.root):
                return

            def complete(result):
                if result.get("state") == "off":
                    self.status_title.configure(text="AI 가상 출력 세션을 반환했습니다")
                    self.status_detail.configure(text="기존 출력 수와 해상도를 복구했습니다.")
                else:
                    messagebox.showinfo("출력이 아직 사용 중입니다",
                                        str(result.get("reason") or "창/lease 또는 출력 상태를 다시 확인하세요."),
                                        parent=self.root)
            self._run("가상 출력 사용 여부를 확인하고 구성을 복구하는 중…",
                      self._return_output_pool,
                      complete)
            return
        self._change_count(0)

    def open_display_settings(self):
        import os
        os.startfile("ms-settings:display")

    def open_upstream_control(self):
        import webbrowser
        webbrowser.open("https://github.com/VirtualDrivers/Virtual-Driver-Control")

    def run_private_desktop_probe(self):
        if self._busy or self._closing:
            return
        prompt = ("관리 앱이 소유한 작은 테스트 창을 별도 Windows 데스크톱에 잠시 만들고, "
                  "화면 전환 없이 캡처·창 메시지를 확인한 뒤 종료합니다. Only Chat/Codex, "
                  "채팅 데이터, 모델 호출은 사용하지 않습니다. 실행할까요?")
        if not messagebox.askyesno("격리 데스크톱 자체 검사", prompt, parent=self.root):
            return
        self._run("별도 Windows 데스크톱에서 자체 검사 창을 확인하는 중…",
                  lambda: self.host.invoke("private_desktop", "probe", {"confirmed": True}),
                  self._private_desktop_probe_complete)

    def _private_desktop_probe_complete(self, result):
        if result.get("passed"):
            summary = ("자체 검사 통과: 테스트 창이 입력 데스크톱에 나타나지 않았고, "
                       "비어 있지 않은 캡처와 메시지 왕복을 확인했습니다. 입력 데스크톱은 유지됐고 "
                       "테스트 프로세스와 데스크톱을 정리했습니다. Only Chat/Codex 통합은 별도입니다.")
            messagebox.showinfo("격리 데스크톱 자체 검사", summary, parent=self.root)
        else:
            messagebox.showwarning("격리 데스크톱 자체 검사 실패",
                                   str(result.get("error_code") or result), parent=self.root)
        self.root.after(200, self.refresh)

    def open_window_router(self):
        """Show allowlisted app windows and require confirmation per restore or move."""
        if self._busy or self._closing:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("허용된 AI 앱 창 배치")
        dialog.geometry("760x500")
        dialog.minsize(680, 420)
        dialog.configure(bg=COLORS["background"])
        tk.Label(dialog, text="Only Chat · Codex 창 복원 및 가상 화면 배치",
                 bg=COLORS["background"], fg=COLORS["text"],
                 font=("Segoe UI Semibold", 16)).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(dialog,
                 text=("앱 제목이나 대화 내용은 읽지 않습니다. 최소화 창 복원은 확인 후 해당 앱 창을 "
                       "기억된 위치에서 열고 활성화합니다. 복원은 이동·크기 변경을 하지 않습니다.\n"
                       "가상 화면 이동은 별도 확인 후에만 실행합니다.\n"
                       "가상 출력 배치는 별도 보안 데스크톱/프로세스 격리를 뜻하지 않습니다."),
                 bg=COLORS["background"], fg=COLORS["muted"], justify="left", wraplength=700,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=22, pady=(0, 14))
        body = tk.Frame(dialog, bg=COLORS["background"])
        body.pack(fill="both", expand=True, padx=22, pady=(0, 10))
        columns = ("app", "pid", "display", "bounds")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse", height=10)
        for key, label, width in (("app", "허용 앱", 150), ("pid", "PID", 75),
                                  ("display", "현재 출력", 170), ("bounds", "창 크기", 190)):
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=(key in {"display", "bounds"}))
        tree.pack(fill="both", expand=True)
        output_row = tk.Frame(dialog, bg=COLORS["background"])
        output_row.pack(fill="x", padx=22, pady=(0, 8))
        tk.Label(output_row, text="대상 가상 출력", bg=COLORS["background"], fg=COLORS["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        output_var = tk.StringVar(value="")
        output_box = ttk.Combobox(output_row, textvariable=output_var, state="readonly", width=48)
        output_box.pack(side="left", padx=(10, 0))
        status = tk.Label(dialog, text="창과 출력 상태를 읽어 주세요.", bg=COLORS["background"],
                          fg=COLORS["muted"], anchor="w", justify="left", wraplength=700,
                          font=("Segoe UI", 9))
        status.pack(fill="x", padx=22, pady=(0, 10))
        rows: dict[str, dict] = {}
        outputs: dict[str, dict] = {}

        def refresh_targets():
            if not dialog.winfo_exists():
                return
            status.configure(text="Only Chat/Codex 창과 MTT 가상 출력을 다시 확인하는 중…")
            def render(result):
                if not dialog.winfo_exists():
                    return
                for item in tree.get_children(""):
                    tree.delete(item)
                rows.clear()
                outputs.clear()
                output_names = []
                for output in result.get("virtual_outputs", []):
                    key = output["display_name"]
                    outputs[key] = output
                    output_names.append(key)
                output_box["values"] = output_names
                output_var.set(output_names[0] if output_names else "")
                for window in result.get("windows", []):
                    rect = window["bounds"]
                    label = f"{rect[2] - rect[0]} × {rect[3] - rect[1]}"
                    app_label = window["app"]
                    if window.get("window_state") == "minimized":
                        app_label += " (최소화)"
                    tree.insert("", "end", iid=window["window_id"], values=(
                        app_label, window["pid"], window.get("display_name") or "출력 확인 불가", label))
                    rows[window["window_id"]] = window
                if not rows:
                    status.configure(text="이 PC에서 허용된 Only Chat/Codex 창을 찾지 못했습니다.")
                elif not outputs:
                    status.configure(text="최소화된 허용 창은 복원할 수 있습니다. 이동하려면 복제되지 않은 활성 MTT 출력이 필요합니다.")
                else:
                    status.configure(text="창을 선택하고 이동을 누르면, 최신 프로세스/창/출력 상태를 다시 검사한 뒤 확인 창을 띄웁니다.")
            self._run("허용된 AI 앱 창과 가상 출력 상태를 읽는 중…",
                      lambda: self.host.invoke("app_window_router", "list_windows"), render)

        def move_selected():
            selection = tree.selection()
            if not selection:
                status.configure(text="먼저 Only Chat 또는 Codex 창을 선택하세요.")
                return
            window_id = selection[0]
            window = rows.get(window_id)
            display_name = output_var.get()
            output = outputs.get(display_name)
            if not window or not output:
                status.configure(text="목록 또는 대상 출력이 바뀌었습니다. 새로 고침해 주세요.")
                return
            if window.get("window_state") == "minimized":
                if window.get("display_name", "").casefold() == display_name.casefold():
                    status.configure(text=(f"{window['app']} 창은 최소화 상태입니다. 복원 위치가 이미 "
                                           f"{display_name} 가상 출력 안에 있습니다. 창을 활성화하지 않았습니다."))
                else:
                    status.configure(text=(f"{window['app']} 창은 최소화 상태입니다. 복원 위치를 자동 변경하지 않습니다. "
                                           "창을 직접 복원한 뒤 다시 선택하세요."))
                return
            rect = window["bounds"]
            target_bounds = output["bounds"]
            message = (f"{window['app']} (PID {window['pid']}) 창 하나를 이동합니다.\n"
                       f"현재 출력: {window.get('display_name') or '확인되지 않음'}\n"
                       f"대상: {output.get('target_name') or 'MTT 가상 출력'} · {display_name}\n"
                       f"창 크기: {rect[2] - rect[0]} × {rect[3] - rect[1]}\n"
                       f"가상 출력 크기: {target_bounds[2]} × {target_bounds[3]}\n\n"
                       "창은 활성화하지 않고 이동합니다. 앱이 이후 실행 위치를 저장할 수 있습니다. 계속할까요?")
            if not messagebox.askyesno("AI 앱 창 이동 확인", message, parent=dialog):
                return
            status.configure(text="창 identity와 가상 출력 연결을 다시 확인한 뒤 이동합니다…")
            def complete(result):
                if not dialog.winfo_exists():
                    return
                status.configure(text=(f"{result['app']} 창이 {result['display_name']}에 배치됐습니다. "
                                       f"새 좌표: {result['bounds']}. 창 활성화: 아니요."))
                refresh_targets()
            self._run("선택한 AI 창을 확인된 MTT 출력에 배치하는 중…",
                      lambda: self.host.invoke("app_window_router", "move_window", {
                          "window_id": window_id, "display_name": display_name, "confirmed": True,
                      }), complete)

        def restore_selected():
            selection = tree.selection()
            if not selection:
                status.configure(text="먼저 최소화된 Only Chat 또는 Codex 창을 선택하세요.")
                return
            window_id = selection[0]
            window = rows.get(window_id)
            if not window or window.get("window_state") != "minimized":
                status.configure(text="선택한 창이 최소화 상태가 아닙니다. 목록을 새로 확인하세요.")
                return
            message = (f"{window['app']} (PID {window['pid']}) 창 하나를 기억된 위치에서 복원합니다.\n\n"
                       "창이 앞으로 활성화되어 화면 내용이 보일 수 있습니다. 이 동작은 창을 이동하거나 "
                       "크기를 바꾸지 않으며, 화면/드라이버 설정도 변경하지 않습니다. 계속할까요?")
            if not messagebox.askyesno("최소화 창 복원 확인", message, parent=dialog):
                return
            status.configure(text="선택한 AI 창 identity를 다시 확인하고 복원 중입니다…")
            def complete(result):
                if not dialog.winfo_exists():
                    return
                location = result.get("display_name") or "현재 위치 확인 불가"
                status.configure(text=f"{result['app']} 창을 복원했습니다 · {location} · 이동/크기 변경 없음")
                refresh_targets()
            self._run("선택한 AI 창을 기억된 위치에서 복원하는 중…",
                      lambda: self.host.invoke("app_window_router", "restore_window", {
                          "window_id": window_id, "confirmed": True,
                      }), complete)

        controls = tk.Frame(dialog, bg=COLORS["background"])
        controls.pack(fill="x", padx=22, pady=(0, 18))
        self._button(controls, "창/출력 새로 확인", refresh_targets).pack(side="left")
        self._button(controls, "최소화 창 복원", restore_selected).pack(side="left", padx=(9, 0))
        self._button(controls, "선택 창을 가상 화면으로 이동", move_selected,
                     primary=True).pack(side="left", padx=(9, 0))
        self._button(controls, "닫기", dialog.destroy).pack(side="right")
        refresh_targets()

    def close(self, *, verify_no_active_task=False):
        if self._busy:
            if not self._shutdown_in_progress:
                messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 닫을 수 있습니다.", parent=self.root)
            return
        if self._closing:
            return
        if not self._shutdown_in_progress:
            self._shutdown_in_progress = True
            self._shutdown_deadline = time.monotonic() + 15.0
        accepted = self._run(
            "관리자 종료 조건을 확인하는 중…",
            lambda: self._close_worker_safe(verify_no_active_task=verify_no_active_task),
            self._finish_close,
        )
        if not accepted and self._shutdown_deadline is not None and not self._busy:
            self._shutdown_in_progress = False
            self._shutdown_deadline = None

    def _close_worker_safe(self, *, verify_no_active_task=False):
        try:
            return self._close_worker(verify_no_active_task=verify_no_active_task)
        except Exception as exc:
            try:
                self.host._loaded["manager_cli"].instance.resume_after_shutdown()
            except Exception:
                pass
            return {"state": "error", "code": "SHUTDOWN_CHECK_FAILED", "title": "관리자 종료 확인 실패",
                    "detail": f"안전 종료 조건을 확인하지 못해 관리자 앱을 열어 둡니다.\n{exc}"}

    def _close_worker(self, *, verify_no_active_task=False):
        cli = self.host._loaded["manager_cli"].instance
        if verify_no_active_task:
            try:
                task = self._task_runtime_action("status", {
                    "protocol_version": 2, "request_id": str(uuid.uuid4())})
                if not isinstance(task, dict) or task.get("active_session_id"):
                    return {"state": "error", "code": "AI_TASK_ACTIVE", "title": "AI 작업 진행 중",
                            "detail": "격리 작업 세션이 종료된 뒤 관리자 앱을 닫을 수 있습니다."}
            except Exception as exc:
                return {"state": "error", "code": "AI_TASK_STATUS_UNAVAILABLE",
                        "title": "AI 작업 상태 미확인", "detail": str(exc)}
        try:
            if not cli.pause_for_shutdown():
                if (self._shutdown_deadline is not None
                        and time.monotonic() < self._shutdown_deadline):
                    return {"state": "cli_pending"}
                return {"state": "error", "code": "CLI_REQUEST_ACTIVE", "title": "CLI 요청 진행 중",
                        "detail": "15초 안에 진행 중인 VDM CLI 요청이 끝나지 않아 안전 종료를 취소했습니다. 관리자 앱은 계속 실행 중입니다."}
        except Exception as exc:
            return {"state": "error", "code": "CLI_STOP_UNVERIFIED", "title": "CLI 연결 종료 확인 실패",
                    "detail": f"CLI 연결을 안전하게 멈추지 못해 관리자 앱을 열어 둡니다.\n{exc}"}
        try:
            pool = self.host.invoke("virtual_output_pool", "pool_status")
        except Exception as exc:
            cli.resume_after_shutdown()
            return {"state": "error", "code": "OUTPUT_STATUS_UNAVAILABLE", "title": "가상 출력 상태 미확인",
                    "detail": f"출력 세션 상태를 확인하지 못해 관리자 앱을 열어 둡니다.\n{exc}"}
        if pool.get("state") != "off":
            cli.resume_after_shutdown()
            return {"state": "error", "code": "OUTPUT_POOL_ACTIVE", "title": "가상 출력 세션 종료 필요",
                    "detail": "가상 출력 세션이 실행 중입니다. 관리자 앱을 닫기 전에 가상 출력 세션 반환 버튼으로 원래 구성을 확인·복구하세요."}
        router = self.host._loaded["app_window_router"].instance
        try:
            was_enabled = bool(router.guard and router.guard.status().get("enabled"))
            runtime = router.guard.stop(timeout=0) if router.guard else {"enabled": False}
            if runtime.get("enabled"):
                if time.monotonic() < self._shutdown_deadline:
                    return {"state": "guard_pending"}
                cli.resume_after_shutdown()
                return {"state": "error", "code": "GUARD_STOP_UNVERIFIED", "title": "창 보호 종료 미확인",
                        "detail": "감시 스레드 종료를 확인하지 못했습니다. 관리자 앱을 열어 둡니다."}
        except Exception as exc:
            cli.resume_after_shutdown()
            return {"state": "error", "code": "GUARD_STOP_FAILED", "title": "창 보호 종료 실패",
                    "detail": str(exc)}
        try:
            self.host._loaded["display_diagnostics"].instance.action(
                "record", {"event": "manager_state", "result": "closed"})
            self.host.dispose_all()
        except Exception as exc:
            try:
                cli.resume_after_shutdown()
                if was_enabled:
                    router.action("physical_guard_start", {"confirmed": True})
            except Exception:
                pass
            return {"state": "error", "code": "DISPOSE_UNVERIFIED", "title": "관리자 종료 실패",
                    "detail": f"정리 작업이 끝나지 않아 창을 유지합니다.\n{exc}"}
        try:
            get_keep_awake_guard().stop()
        except Exception:
            pass
        return {"state": "closed"}

    def _finish_close(self, result):
        if result.get("state") in {"cli_pending", "guard_pending"}:
            self.root.after(250, self.close)
            return
        self._shutdown_in_progress = False
        self._shutdown_deadline = None
        if result.get("state") == "closed":
            self._closing = True
            self._executor.shutdown(wait=False, cancel_futures=True)
            self.root.destroy()
            return
        self._shutdown_notice(result.get("code", "SHUTDOWN_FAILED"),
                              result.get("title", "관리자 종료 실패"),
                              result.get("detail", "상태를 확인하지 못해 관리자 앱을 열어 둡니다."))


def resident_controller():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    frozen_executable = Path(sys.executable) if getattr(sys, "frozen", False) else None
    return ResidentController(APP_ROOT, pythonw, frozen_executable=frozen_executable)


def main():
    startup = "--startup" in sys.argv[1:]
    show_ui = "--show-ui" in sys.argv[1:]
    if startup:
        controller = resident_controller()
        try:
            while controller.status()["developer_paused"]:
                time.sleep(5)  # Background dispatcher only; no windows or input.
            if not controller.startup_allowed():
                return
        except Exception as exc:
            atomic_json(controller.path.with_name("startup-error.json"),
                        {"error": type(exc).__name__, "at": time.time()})
            return
    guard = SingleInstanceGuard.acquire()
    if guard is None:
        if show_ui:
            if getattr(sys, "frozen", False):
                client = Path(sys.executable).with_name("vdmctl.exe")
                command = [str(client), "ui", "show"]
            else:
                interpreter = Path(sys.executable)
                if interpreter.name.casefold() == "pythonw.exe":
                    interpreter = interpreter.with_name("python.exe")
                command = [str(interpreter), "-B", str(APP_ROOT / "tools" / "vdmctl.py"),
                           "ui", "show"]
            if not Path(command[0]).is_file():
                raise FileNotFoundError("VDM companion CLI is missing")
            subprocess.Popen(command, cwd=APP_ROOT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    try:
        root = tk.Tk()
        root.withdraw()
        app = ManagerApp(root)
        if show_ui:
            app.show_window()
        root.mainloop()
    except Exception as exc:
        try:
            if show_ui:
                messagebox.showerror("AI Virtual Display Manager", f"앱 시작 실패: {type(exc).__name__}: {exc}")
            else:
                atomic_json(Path(os.environ["LOCALAPPDATA"]) / "AI Virtual Display Manager"
                            / "startup-error.json",
                            {"error": type(exc).__name__, "at": time.time()})
        except Exception:
            # A partially initialized Tk app may already have destroyed its
            # interpreter; reporting failure must not create a second fatal error.
            pass
        finally:
            if "root" in locals():
                try:
                    root.destroy()
                except tk.TclError:
                    pass
    finally:
        guard.close()


def run_self_check(report_path: str) -> int:
    """Run bundled read-only module checks without creating a desktop window."""
    report = {"app_version": APP_VERSION, "app_root": str(APP_ROOT), "modules": {}}
    exit_code = 0
    caller_argv = list(sys.argv)
    check_argv = list(caller_argv)
    if "--self-check" not in check_argv:
        check_argv.append("--self-check")
    sys.argv[:] = check_argv
    host = None
    broker_server = None
    original_pipe_path = None
    broker_module = None
    original_argv = None
    try:
        report["runtime_packet_codec"] = runtime_packet_self_check()
        if report["runtime_packet_codec"].get("passed") is not True:
            raise RuntimeError("Runtime packet codec self-check failed")
        host = ModuleHost(APP_ROOT)
        for module_id in ("mtt_vdd", "display_topology", "app_window_router",
                          "only_chat_debug_display", "only_chat_task_runtime", "private_desktop",
                          "task_session_runtime", "virtual_output_pool", "task_display_lease",
                          "only_chat_output_lease", "stardesk_management", "manager_cli"):
            info = host.load(module_id)
            state = host.refresh(module_id)
            report["modules"][module_id] = {
                "version": info["version"],
                "sha256": info["sha256"],
                "state": state,
            }

        # Exercise the exact hash-loaded runtime class and broker composition
        # on a disposable endpoint. Never bind the production/global pipe from
        # a hidden self-check, and never add a target/backend in this probe.
        broker_module = host._loaded["only_chat_task_runtime"].module
        original_pipe_path = broker_module.PIPE_PATH
        broker_module.PIPE_PATH = rf"\\.\pipe\AIVDD_SELF_CHECK_{uuid.uuid4().hex}"
        original_argv = list(sys.argv)
        sys.argv[:] = [arg for arg in sys.argv if arg != "--self-check"]
        try:
            coordinator = bind_task_runtime_dependencies(
                host._loaded["task_session_runtime"],
                host._loaded["virtual_output_pool"].instance,
                lambda: {},
                host._loaded["only_chat_task_runtime"].instance,
            )
            broker = host._loaded["only_chat_task_runtime"].instance
            broker_server = broker._server
            state = broker.refresh()
            checks = {
                "runtime_bound": state.get("runtime_bound") is True,
                "pipe_ready": state.get("pipe_ready") is True,
                "output_lease_bound": state.get("output_lease_bound") is True,
                "backend_unavailable": state.get("backend_available") is False,
                "zero_targets": state.get("target_count") == 0,
                "lease_fail_closed": state.get("lease_acquisition") == "fail_closed",
                "production_pipe_not_used": broker_module.PIPE_PATH != original_pipe_path,
                "disposable_pipe_prefix": broker_module.PIPE_PATH.startswith(
                    r"\\.\pipe\AIVDD_SELF_CHECK_"),
                "coordinator_type_matches_loaded_runtime": isinstance(
                    coordinator,
                    host._loaded["task_session_runtime"].module.TaskOutputLeaseCoordinator,
                ),
            }
            report["task_runtime_composition"] = {
                "pipe_path": broker_module.PIPE_PATH,
                "checks": checks,
                "passed": all(checks.values()),
            }
            if not all(checks.values()):
                raise RuntimeError("Task-runtime packaged composition check failed")
        finally:
            sys.argv[:] = original_argv
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    finally:
        if broker_module is not None and original_pipe_path is not None:
            broker_module.PIPE_PATH = original_pipe_path
        if host:
            try:
                host.dispose_all()
            except Exception as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                exit_code = 1
        if broker_server is not None:
            stopped = broker_server._thread is None and not broker_server.ready
            report.setdefault("task_runtime_composition", {})[
                "disposable_pipe_stopped"] = stopped
            if not stopped:
                report["cleanup_error"] = "Disposable task-runtime pipe stop was not verified"
                exit_code = 1
        sys.argv[:] = caller_argv
    output = Path(report_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


def run_private_desktop_probe_self_check(report_path: str) -> int:
    """Run the disposable desktop integration probe without opening the manager shell."""
    report = {"app_version": APP_VERSION, "probe": None}
    exit_code = 1
    host = None
    try:
        host = ModuleHost(APP_ROOT)
        info = host.load("private_desktop")
        report["module"] = {"version": info["version"], "sha256": info["sha256"]}
        report["probe"] = host.invoke("private_desktop", "probe", {"confirmed": True})
        exit_code = 0 if report["probe"].get("passed") is True else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if host:
            try:
                host.dispose_all()
            except Exception as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                exit_code = 1
    output = Path(report_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


def _observe_private_desktop_session_frame(host, session_id: str, *,
                                            different_from_sha256: str | None = None):
    """Wait briefly for a valid frame, or for a changed frame after the test action."""
    deadline = time.monotonic() + PRIVATE_DESKTOP_FRAME_WAIT_SECONDS
    last_observation = None
    last_png = None
    attempts = 0
    while attempts < PRIVATE_DESKTOP_FRAME_MAX_ATTEMPTS:
        attempts += 1
        observed = host.invoke("private_desktop", "observe_session",
                               {"session_id": session_id})
        if not isinstance(observed, dict):
            return {"error_code": "capture_response_invalid"}, None, attempts

        encoded_png = observed.pop("png_bytes", None)
        if isinstance(encoded_png, str) and encoded_png:
            try:
                png = base64.b64decode(encoded_png, validate=True)
            except (ValueError, base64.binascii.Error):
                png = None
            if (png and png.startswith(PNG_SIGNATURE)
                    and len(png) <= MAX_PNG_BYTES):
                digest = hashlib.sha256(png).hexdigest()
                if different_from_sha256 is None or digest != different_from_sha256:
                    return observed, png, attempts
                last_observation, last_png = observed, png
                observed["error_code"] = "capture_content_invalid"
            else:
                observed["error_code"] = "capture_content_invalid"

        error_code = observed.get("error_code")
        if error_code != "capture_content_invalid":
            return observed, None, attempts

        now = time.monotonic()
        if now >= deadline or attempts >= PRIVATE_DESKTOP_FRAME_MAX_ATTEMPTS:
            if last_observation is not None and different_from_sha256 is None:
                return last_observation, last_png, attempts
            return observed, None, attempts
        time.sleep(min(PRIVATE_DESKTOP_FRAME_POLL_SECONDS, deadline - now))

    if last_observation is not None and different_from_sha256 is None:
        return last_observation, last_png, attempts
    return {"error_code": "capture_content_invalid"}, None, attempts


def run_private_desktop_session_self_check(report_path: str) -> int:
    """Exercise start/observe/invoke/observe/stop on the owned test window."""
    report = {"app_version": APP_VERSION, "session": {"passed": False}}
    exit_code = 1
    host = None
    session_id = None
    try:
        host = ModuleHost(APP_ROOT)
        info = host.load("private_desktop")
        report["module"] = {"version": info["version"], "sha256": info["sha256"]}
        started = host.invoke("private_desktop", "start_diagnostic_session", {"confirmed": True})
        session_id = started.get("session_id")
        report["session"]["start"] = {
            "started": started.get("started") is True,
            "session_id_valid": isinstance(session_id, str) and len(session_id) == 36,
            "window_absent_from_input_desktop": started.get("window_absent_from_input_desktop") is True,
            "input_desktop_unchanged": started.get("input_desktop_unchanged") is True,
            "display_stays_disabled": started.get("display_enabled") is False,
        }
        guards = {}
        try:
            host.snapshot("private_desktop")
        except RuntimeError as exc:
            guards["reload_blocked_while_active"] = "reload is deferred" in str(exc)
        try:
            host.invoke("private_desktop", "start_diagnostic_session", {"confirmed": True})
        except RuntimeError:
            guards["second_session_rejected"] = True
        else:
            guards["second_session_rejected"] = False
        try:
            host.invoke("private_desktop", "observe_session", {
                "session_id": "00000000-0000-4000-8000-000000000000"})
        except ValueError:
            guards["stale_session_id_rejected"] = True
        else:
            guards["stale_session_id_rejected"] = False
        appended_self_check_flag = "--self-check" not in sys.argv
        if appended_self_check_flag:
            sys.argv.append("--self-check")
        try:
            host.load("only_chat_debug_display")
            broker_state = host.refresh("only_chat_debug_display")
        finally:
            if appended_self_check_flag:
                sys.argv.remove("--self-check")
        guards["consumer_lease_fail_closed"] = (
            broker_state.get("lease_acquisition") == "fail_closed"
            and broker_state.get("display_enabled") is False)
        report["session"]["guards"] = guards
        before, before_png, before_attempts = _observe_private_desktop_session_frame(
            host, session_id)
        report["session"]["observe_before_capture_attempts"] = before_attempts
        if before_png is None:
            report["session"]["observe_before"] = {
                **before, "capture_ready": False,
                "capture_attempts": before_attempts,
            }
            raise RuntimeError("PRIVATE_DESKTOP_INITIAL_FRAME_UNAVAILABLE")
        before_digest = hashlib.sha256(before_png).hexdigest()
        report["session"]["observe_before"] = {
            **before, "png_signature_valid": before_png.startswith(PNG_SIGNATURE),
            "png_size_bounded": len(before_png) <= MAX_PNG_BYTES,
            "png_hash_matches": before_digest == before.get("png_sha256"),
            "capture_attempts": before_attempts,
        }
        action = host.invoke("private_desktop", "invoke_diagnostic_button", {"session_id": session_id})
        report["session"]["invoke"] = {key: action.get(key) for key in (
            "passed", "window_found", "button_found", "button_invoked", "status_verified",
            "host_foreground_unchanged", "target_absent_from_input_desktop",
            "input_desktop_unchanged")}
        if action.get("error"):
            report["session"]["invoke"]["error"] = action.get("error")
        after, after_png, after_attempts = _observe_private_desktop_session_frame(
            host, session_id, different_from_sha256=before_digest)
        report["session"]["observe_after_capture_attempts"] = after_attempts
        if after_png is None:
            report["session"]["observe_after"] = {
                **after, "capture_ready": False,
                "capture_attempts": after_attempts,
            }
            raise RuntimeError("PRIVATE_DESKTOP_ACTION_FRAME_UNAVAILABLE")
        after_digest = hashlib.sha256(after_png).hexdigest()
        report["session"]["observe_after"] = {
            **after, "png_signature_valid": after_png.startswith(PNG_SIGNATURE),
            "png_size_bounded": len(after_png) <= MAX_PNG_BYTES,
            "png_hash_matches": after_digest == after.get("png_sha256"),
            "frame_changed_after_action": before_digest != after_digest,
            "capture_attempts": after_attempts,
        }
        stopped = host.invoke("private_desktop", "stop_session", {"session_id": session_id})
        report["session"]["stop"] = stopped
        report["session"]["passed"] = all((
            report["session"]["start"]["started"],
            report["session"]["start"]["session_id_valid"],
            report["session"]["start"]["window_absent_from_input_desktop"],
            report["session"]["start"]["input_desktop_unchanged"],
            report["session"]["start"]["display_stays_disabled"],
            report["session"]["guards"].get("reload_blocked_while_active") is True,
            report["session"]["guards"].get("second_session_rejected") is True,
            report["session"]["guards"].get("stale_session_id_rejected") is True,
            report["session"]["guards"].get("consumer_lease_fail_closed") is True,
            report["session"]["observe_before"]["png_signature_valid"],
            report["session"]["observe_before"]["png_size_bounded"],
            report["session"]["observe_before"]["png_hash_matches"],
            report["session"]["invoke"].get("passed") is True,
            report["session"]["invoke"].get("target_absent_from_input_desktop") is True,
            report["session"]["observe_after"]["png_signature_valid"],
            report["session"]["observe_after"]["png_size_bounded"],
            report["session"]["observe_after"]["png_hash_matches"],
            report["session"]["observe_after"]["frame_changed_after_action"],
            stopped.get("stopped") is True,
            stopped.get("desktop_removed") is True,
            stopped.get("input_desktop_unchanged") is True,
        ))
        exit_code = 0 if report["session"]["passed"] else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        if host:
            if session_id:
                try:
                    current = host.refresh("private_desktop")
                    if current.get("diagnostic_session") == "active":
                        host.invoke("private_desktop", "stop_session", {"session_id": session_id})
                except Exception:
                    pass
            try:
                host.dispose_all()
            except Exception as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:180]
                exit_code = 1
    output = Path(report_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


def run_task_runtime_private_desktop_self_check(report_path: str) -> int:
    """Exercise the task dispatcher only against its manager-owned diagnostic target."""
    from modules.task_session_runtime.diagnostic_backend import (
        run_task_runtime_private_desktop_self_check as run_check,
    )
    return run_check(report_path)


def run_private_desktop_probe_window(token: str) -> int:
    """Create an owned Win32 probe with standard UIA-readable child controls."""
    if sys.platform != "win32":
        return 2
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)

    class WndClass(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", wndproc_type),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetSysColorBrush.argtypes = [ctypes.c_int]
    user32.GetSysColorBrush.restype = wintypes.HBRUSH
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

    WM_COMMAND, WM_CLOSE, WM_DESTROY = 0x0111, 0x0010, 0x0002
    BUTTON_ID, BN_CLICKED = 1001, 0
    WS_OVERLAPPEDWINDOW, WS_CHILD, WS_VISIBLE = 0x00CF0000, 0x40000000, 0x10000000
    class_name = f"AIQA_PRIVATE_DESKTOP_CLASS_{token}"
    title = f"AIQA_PRIVATE_DESKTOP_{token}"
    module = kernel32.GetModuleHandleW(None)
    status_hwnd = wintypes.HWND()

    def window_proc(hwnd, message, wparam, lparam):
        if (message == WM_COMMAND and (wparam & 0xFFFF) == BUTTON_ID
                and ((wparam >> 16) & 0xFFFF) == BN_CLICKED and status_hwnd):
            user32.SetWindowTextW(status_hwnd, "Control invoked")
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    callback = wndproc_type(window_proc)
    wndclass = WndClass(0, callback, 0, 0, module, None, None,
                        user32.GetSysColorBrush(5), None, class_name)
    if not user32.RegisterClassW(ctypes.byref(wndclass)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = None
    try:
        hwnd = user32.CreateWindowExW(0, class_name, title, WS_OVERLAPPEDWINDOW,
            120, 120, 360, 190, None, None, module, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.CreateWindowExW(0, "STATIC", "Private desktop diagnostic",
            WS_CHILD | WS_VISIBLE, 20, 18, 300, 26, hwnd, None, module, None)
        status_hwnd = user32.CreateWindowExW(0, "STATIC", "Ready",
            WS_CHILD | WS_VISIBLE, 20, 55, 300, 26, hwnd, None, module, None)
        button = user32.CreateWindowExW(0, "BUTTON", "Invoke test action",
            WS_CHILD | WS_VISIBLE, 20, 100, 170, 34, hwnd, wintypes.HMENU(BUTTON_ID),
            module, None)
        if not status_hwnd or not button:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.ShowWindow(hwnd, 5)
        user32.UpdateWindow(hwnd)
        message = wintypes.MSG()
        while True:
            code = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if code <= 0:
                return 0 if code == 0 else 1
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        if hwnd:
            user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, module)


def run_private_desktop_uia_worker(hwnd: str, report_path: str) -> int:
    """Invoke one named control on the exact self-authored private-desktop window."""
    report = {"passed": False, "window_found": False,
              "button_found": False, "button_invoked": False,
              "status_verified": False, "control_summary": []}
    exit_code = 1
    try:
        from comtypes import CoInitialize, CoUninitialize
        from pywinauto import Desktop

        CoInitialize()
        try:
            window = Desktop(backend="uia").window(handle=int(hwnd))
            window.wait("exists visible enabled", timeout=4)
            report["window_found"] = True
            deadline = time.monotonic() + 3.0
            button = None
            while time.monotonic() < deadline and button is None:
                controls = window.descendants()
                report["control_summary"] = [
                    {"control_type": item.element_info.control_type,
                     "name": item.window_text()[:80]}
                    for item in controls[:24]]
                button = next((item for item in controls
                               if item.element_info.control_type == "Button"
                               and item.window_text() == "Invoke test action"), None)
                if button is None:
                    time.sleep(0.05)
            if button is None:
                raise LookupError("Named diagnostic button is unavailable")
            report["button_found"] = True
            button.invoke()
            report["button_invoked"] = True
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if any(item.window_text() == "Control invoked"
                       for item in window.descendants()):
                    report["status_verified"] = True
                    break
                time.sleep(0.05)
            report["passed"] = report["status_verified"]
            exit_code = 0 if report["passed"] else 1
        finally:
            CoUninitialize()
    except Exception as exc:
        report["error_code"] = f"{type(exc).__name__}: {exc}"[:160]
    output = Path(report_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    return exit_code


def run_private_desktop_uia_action_worker(hwnd: str, descriptor_path: str,
                                         report_path: str) -> int:
    """Run one exact manager-pinned UIA action on the owned private desktop."""
    report = {"performed": False, "postcondition": None}
    exit_code = 1
    try:
        descriptor_file = Path(descriptor_path)
        if not descriptor_file.is_file() or descriptor_file.stat().st_size > 4096:
            raise ValueError("Action descriptor file is unavailable or too large")
        descriptor = json.loads(descriptor_file.read_text(encoding="utf-8"))
        if (not isinstance(descriptor, dict)
                or set(descriptor) != {"control_type", "control_name",
                                      "postcondition_name", "postcondition_value"}
                or descriptor.get("control_type") not in {
                    "Button", "MenuItem", "TabItem", "CheckBox", "RadioButton"}
                or any(not isinstance(descriptor.get(key), str)
                       or not descriptor[key].strip() or len(descriptor[key]) > 256
                       for key in ("control_name", "postcondition_name",
                                   "postcondition_value"))):
            raise ValueError("Action descriptor is invalid")
        from comtypes import CoInitialize, CoUninitialize
        from pywinauto import Desktop

        CoInitialize()
        try:
            window = Desktop(backend="uia").window(handle=int(hwnd))
            window.wait("exists visible enabled", timeout=4)
            matches = [item for item in window.descendants()
                       if item.element_info.control_type == descriptor["control_type"]
                       and item.window_text() == descriptor["control_name"]]
            if len(matches) != 1:
                raise LookupError("Pinned UIA control is missing or ambiguous")
            control = matches[0]
            kind = descriptor["control_type"]
            expected_name = descriptor["postcondition_name"]
            expected_value = descriptor["postcondition_value"]
            if kind in {"Button", "MenuItem", "TabItem"}:
                if expected_name != "Name":
                    raise ValueError("Invoke postcondition must use exact Name")
                control.invoke()
            elif kind == "CheckBox":
                if expected_name != "ToggleState" or expected_value not in {
                        "Off", "On", "Indeterminate"}:
                    raise ValueError("Checkbox postcondition must name ToggleState")
                expected_state = {"Off": 0, "On": 1, "Indeterminate": 2}[expected_value]
                if control.get_toggle_state() != expected_state:
                    control.toggle()
            else:
                if expected_name != "SelectionState" or expected_value not in {
                        "NotSelected", "Selected"}:
                    raise ValueError("Radio postcondition must name SelectionState")
                if expected_value == "Selected":
                    control.select()
            deadline = time.monotonic() + 3.0
            verified = False
            while time.monotonic() < deadline:
                if kind in {"CheckBox", "RadioButton"}:
                    if kind == "CheckBox":
                        actual = {0: "Off", 1: "On", 2: "Indeterminate"}.get(
                            control.get_toggle_state())
                    else:
                        actual = ("Selected" if control.is_selected()
                                  else "NotSelected")
                else:
                    actual = control.window_text()
                if actual == expected_value:
                    verified = True
                    break
                time.sleep(0.05)
            report.update({"performed": verified,
                           "postcondition": ({expected_name: expected_value}
                                             if verified else None),
                           "control_type": kind})
            if not verified:
                raise RuntimeError("Exact UIA postcondition did not appear")
            exit_code = 0
        finally:
            CoUninitialize()
    except Exception as exc:
        report["error_code"] = f"{type(exc).__name__}: {exc}"[:160]
    output = Path(report_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    return exit_code


def _write_hidden_cli_error(message: str) -> None:
    """Write best-effort CLI diagnostics; windowed builds may have no stderr stream."""
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(f"{message}\n")
    except Exception:
        pass


def dispatch_hidden_cli(argv: list[str]) -> int | None:
    """Dispatch private QA commands; malformed reserved flags must never open the UI."""
    if not argv:
        return None

    resident_commands = {"--enable-startup": "enable", "--disable-startup": "disable",
                         "--developer-resume": "resume", "--resident-status": "status"}
    if argv[0] in resident_commands or argv[0] == "--developer-pause":
        try:
            controller = resident_controller()
            if argv[0] == "--developer-pause":
                if len(argv) != 2:
                    raise ValueError("--developer-pause requires exactly one task ID")
                result = controller.pause(argv[1])
                # Recovery survives the editor and sleep; the normal startup
                # dispatcher waits for the absolute expiry without owning UI.
                subprocess.Popen(controller.startup_argv(),
                                 cwd=APP_ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                if len(argv) != 1:
                    raise ValueError("Unexpected resident command arguments")
                result = getattr(controller, resident_commands[argv[0]])()
            stream = getattr(sys, "stdout", None)
            if stream:
                stream.write(json.dumps(result, ensure_ascii=True) + "\n")
            return 0
        except Exception as exc:
            _write_hidden_cli_error(f"Resident control failed: {type(exc).__name__}: {exc}")
            return 1

    commands = {
        "--self-check": (1, run_self_check),
        "--private-desktop-self-check": (1, run_private_desktop_probe_self_check),
        "--private-desktop-session-self-check": (1, run_private_desktop_session_self_check),
        "--task-runtime-private-desktop-self-check":
            (1, run_task_runtime_private_desktop_self_check),
        "--private-desktop-probe": (1, run_private_desktop_probe_window),
        "--private-desktop-uia-worker": (2, run_private_desktop_uia_worker),
        "--private-desktop-uia-action-worker":
            (3, run_private_desktop_uia_action_worker),
    }
    command = argv[0]
    if command not in commands:
        return None

    expected_args, handler = commands[command]
    if len(argv) != expected_args + 1 or any(not arg for arg in argv[1:]):
        _write_hidden_cli_error(
            f"Invalid arguments for {command}; expected {expected_args} argument(s).")
        return 2
    try:
        return handler(*argv[1:])
    except Exception as exc:
        # Windowed PyInstaller builds otherwise show a native unhandled-error dialog.
        # Hidden QA should return a bounded failure and preserve the desktop.
        message = f"{type(exc).__name__}: {exc}"[:500]
        _write_hidden_cli_error(f"Hidden check {command} failed: {message}")
        if expected_args == 1:
            output = Path(argv[1]).expanduser()
            try:
                if not output.exists():
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({
                        "app_version": APP_VERSION,
                        "check": command,
                        "passed": False,
                        "error": message,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    hidden_result = dispatch_hidden_cli(sys.argv[1:])
    if hidden_result is not None:
        raise SystemExit(hidden_result)
    main()
