"""First-Run Setup & Protection Wizard for AI Virtual Display Manager."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
import winreg

from core.theme import COLORS
from core.ai_keep_awake import get_keep_awake_guard
from modules.app_window_router.guard_settings import GuardSettingsStore

SETUP_KEY = "first_run_setup_completed"

def get_settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local_app_data) / "AI Virtual Display Manager" / "settings.json"

def is_setup_completed() -> bool:
    path = get_settings_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get(SETUP_KEY, False))
    except Exception:
        return False

def mark_setup_completed():
    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data[SETUP_KEY] = True
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def check_uac_secure_desktop() -> bool:
    """Returns True if Secure Desktop is enabled (blackout mode), False if disabled."""
    try:
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            val, _ = winreg.QueryValueEx(k, "PromptOnSecureDesktop")
            return bool(val == 1)
    except Exception:
        return True

def apply_uac_secure_desktop_disable() -> bool:
    """Requests elevation to set PromptOnSecureDesktop = 0."""
    try:
        cmd = 'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System" /v PromptOnSecureDesktop /t REG_DWORD /d 0 /f'
        # Run elevated via ShellExecute
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {cmd}", None, 0)
        return ret > 32
    except Exception:
        return False


class FirstRunWizardDialog:
    def __init__(self, parent: tk.Tk, on_complete=None):
        self.parent = parent
        self.on_complete = on_complete
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("AI Virtual Display Manager 초기 설정 마법사")
        self.dialog.geometry("680x560")
        self.dialog.resizable(False, False)
        self.dialog.configure(bg=COLORS["background"])
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self.var_cursor_guard = tk.BooleanVar(value=True)
        self.var_away_mode = tk.BooleanVar(value=True)
        self.var_uac_fix = tk.BooleanVar(value=True)

        self._build_ui()

    def _build_ui(self):
        container = tk.Frame(self.dialog, bg=COLORS["background"], padx=30, pady=25)
        container.pack(fill="both", expand=True)

        tk.Label(container, text="AI 가상 작업 환경 초기 설정",
                 bg=COLORS["background"], fg=COLORS["text"],
                 font=("Segoe UI Semibold", 18)).pack(anchor="w")
        tk.Label(container,
                 text="AI 에이전트와 사용자가 충돌 없이 원활하게 작업할 수 있도록 핵심 안전 기능을 설정합니다.\n권장 옵션들이 기본 선택되어 있으며, 한 번 설정 후 언제든 변경할 수 있습니다.",
                 bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 10),
                 justify="left").pack(anchor="w", pady=(6, 20))

        # Card 1: Mouse & Folder Guard
        c1 = tk.Frame(container, bg=COLORS["panel"], padx=16, pady=12,
                      highlightthickness=1, highlightbackground=COLORS["border"])
        c1.pack(fill="x", pady=(0, 12))
        cb1 = tk.Checkbutton(c1, text=" [필수 권장] 마우스 커서 및 새 폴더/창 물리 모니터 고정 보호",
                             variable=self.var_cursor_guard, bg=COLORS["panel"], fg=COLORS["text"],
                             selectcolor=COLORS["panel"], activebackground=COLORS["panel"],
                             activeforeground=COLORS["text"], font=("Segoe UI Semibold", 11))
        cb1.pack(anchor="w")
        tk.Label(c1, text="• 마우스 커서가 가상 모니터로 빠져나가지 못하도록 주 모니터 영역에 고정합니다.\n• 새로 열리는 탐색기(폴더)나 앱 창이 가상 화면으로 사라지지 않고 주 화면에 뜹니다.",
                 bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=24, pady=(4, 0))

        # Card 2: Keep-Awake / Away Mode
        c2 = tk.Frame(container, bg=COLORS["panel"], padx=16, pady=12,
                      highlightthickness=1, highlightbackground=COLORS["border"])
        c2.pack(fill="x", pady=(0, 12))
        cb2 = tk.Checkbutton(c2, text=" [무중단 작업] 모니터가 꺼져도 AI 작업 유지 (Away Mode)",
                             variable=self.var_away_mode, bg=COLORS["panel"], fg=COLORS["text"],
                             selectcolor=COLORS["panel"], activebackground=COLORS["panel"],
                             activeforeground=COLORS["text"], font=("Segoe UI Semibold", 11))
        cb2.pack(anchor="w")
        tk.Label(c2, text="• 화면 절전으로 모니터 전원이 꺼지더라도 CPU·네트워크·AI 프로세스가 잠들지 않고 계속 실행됩니다.\n• 밤새 장시간 테스트나 백그라운드 작업을 안전하게 유지합니다.",
                 bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=24, pady=(4, 0))

        # Card 3: UAC Secure Desktop Blackout Fix
        c3 = tk.Frame(container, bg=COLORS["panel"], padx=16, pady=12,
                      highlightthickness=1, highlightbackground=COLORS["border"])
        c3.pack(fill="x", pady=(0, 18))
        cb3 = tk.Checkbutton(c3, text=" [화면 차단 방지] 관리자(UAC) 팝업 시 화면 전체 블랙아웃 해제",
                             variable=self.var_uac_fix, bg=COLORS["panel"], fg=COLORS["text"],
                             selectcolor=COLORS["panel"], activebackground=COLORS["panel"],
                             activeforeground=COLORS["text"], font=("Segoe UI Semibold", 11))
        cb3.pack(anchor="w")
        uac_status_text = "현재 설정: 검은 화면 차단 모드 켜짐" if check_uac_secure_desktop() else "현재 설정: 검은 화면 차단 모드 이미 해제됨 (정상)"
        tk.Label(c3, text=f"• 관리자 권한 요청 시 화면 전체를 검게 가리는 보안 데스크톱을 일반 팝업 창 형태로 전환합니다.\n• AI의 화면 캡처 및 비주얼 인식이 가려지지 않고 유지됩니다. ({uac_status_text})",
                 bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=24, pady=(4, 0))

        # Bottom Actions
        actions = tk.Frame(container, bg=COLORS["background"])
        actions.pack(fill="x", side="bottom")

        btn_apply = tk.Button(actions, text="모든 보호 기능 동의 및 적용 완료", command=self._apply_all,
                              relief="flat", bg=COLORS["accent"], fg="#ffffff",
                              activebackground=COLORS["accent_hover"], activeforeground="#ffffff",
                              font=("Segoe UI Semibold", 11), padx=18, pady=8, cursor="hand2")
        btn_apply.pack(side="right")

    def _apply_all(self):
        # 1. Cursor & Window Guard
        store = GuardSettingsStore()
        store.write(bool(self.var_cursor_guard.get()))

        # 2. Away Mode
        if self.var_away_mode.get():
            get_keep_awake_guard().start()
        else:
            get_keep_awake_guard().stop()

        # 3. UAC Blackout Fix
        if self.var_uac_fix.get() and check_uac_secure_desktop():
            apply_uac_secure_desktop_disable()

        mark_setup_completed()
        messagebox.showinfo("설정 완료",
                            "모든 AI 가상 디스플레이 안전 보호 설정이 성공적으로 적용되었습니다!\n마우스 및 새 창 보호, 무중단 절전 방지가 즉시 활성화됩니다.",
                            parent=self.dialog)
        self.dialog.destroy()
        if callable(self.on_complete):
            self.on_complete()


def launch_wizard_if_needed(parent: tk.Tk, on_complete=None):
    if not is_setup_completed():
        FirstRunWizardDialog(parent, on_complete=on_complete)


__all__ = ["FirstRunWizardDialog", "launch_wizard_if_needed", "is_setup_completed", "check_uac_secure_desktop", "apply_uac_secure_desktop_disable"]
