from __future__ import annotations

import ctypes
import html
import json
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QEvent, QPoint, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QFontDatabase, QIcon, QKeySequence, QPixmap, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.audio_manager import AudioManager
from core.authorization import BackendAuthorizationContext
from core.backend_identity import OFFICIAL_BACKEND_DISPLAY_NAME
from core.conversation_manager import AssistantResult, AssistantState, ConversationManager
from core.debug_security import DEFAULT_DEBUG_STATE
from core.language import CURRENT_LANGUAGE_SETTING_ID, normalize_current_language
from core.runtime_bridge import RuntimeBridge
from core.runtime_paths import get_runtime_paths
from core.settings_manager import (
    CUSTOMIZATION_SETTING_IDS,
    PASSIVE_GRAPHICS_SETTING_IDS,
    SettingsManager,
)
from core.stt_manager import VoiceSTTManager
from core.usage_estimator import (
    UsageEstimator,
    estimated_complete_interaction_cost,
    float_from_env,
    usage_budget_remaining_from_env,
    usage_profile,
)
from core.websocket_server import (
    add_websocket_connection_status_handler,
    add_websocket_outgoing_message_handler,
    has_websocket_client,
    is_websocket_server_active,
    remove_websocket_connection_status_handler,
    remove_websocket_outgoing_message_handler,
    send_audio_end,
    send_audio_start,
    send_json_to_unreal_threadsafe,
    set_unreal_json_message_handler,
    start_websocket_server,
    stop_websocket_server,
)
from ui.chat_panel import ChatPanel
from ui.settings_dialog import SettingsDialog
from ui.translations import tr

LOGGER = logging.getLogger(__name__)
MIC_LEVEL_SEND_INTERVAL_SECONDS = 0.05
BACKEND_UI_STATUSES = {"connected", "disconnected", "reconnecting", "error"}
MAX_OUTBOUND_UNREAL_EVENTS = 80
SETTING_ID_COLOR = "#f0c86a"
SETTING_VALUE_COLOR = "#8bded4"
SETTING_PREVIOUS_VALUE_COLOR = "#9f9a8d"
SETTING_META_COLOR = "#f4f1ea"
SETTING_MUTED_COLOR = "#6f6a5d"
OUTBOUND_MESSAGE_COLORS: dict[str, str] = {
    "backend_ui": "#38bdf8",
    "runtime_state": "#22c55e",
    "audio_devices": "#f59e0b",
    "out_of_credits": "#fb7185",
    "mic_level": "#a78bfa",
    "audio_start": "#10b981",
    "audio_chunk": "#94a3b8",
    "audio_end": "#ef4444",
    "text": "#eab308",
    "binary": "#cbd5e1",
}


class WindowsDebugHotkeyFilter(QAbstractNativeEventFilter):
    WM_HOTKEY = 0x0312
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10
    VK_D = 0x44
    HOTKEY_ID = 0x4450
    DEBUG_HOTKEYS: tuple[tuple[str, int, int], ...] = (
        ("Ctrl+Shift+D", HOTKEY_ID, VK_D),
    )

    def __init__(self, callback, window_id: int | None = None) -> None:
        super().__init__()
        self._callback = callback
        self._window_id = window_id
        self._registered = False
        self._registered_hotkey_ids: set[int] = set()

    def register(self) -> bool:
        if sys.platform != "win32":
            return False
        modifiers = self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT
        hwnd = self._window_id if self._window_id else None
        registered_labels: list[str] = []
        self._registered_hotkey_ids.clear()
        for label, hotkey_id, virtual_key in self.DEBUG_HOTKEYS:
            try:
                ok = bool(
                    ctypes.windll.user32.RegisterHotKey(
                        hwnd,
                        hotkey_id,
                        modifiers,
                        virtual_key,
                    )
                )
            except Exception:
                LOGGER.exception("Could not register global debug hotkey %s", label)
                continue
            if not ok:
                LOGGER.warning("Global debug hotkey %s is already registered", label)
                continue
            self._registered_hotkey_ids.add(hotkey_id)
            registered_labels.append(label)

        if not registered_labels:
            return False
        QCoreApplication.instance().installNativeEventFilter(self)
        self._registered = True
        LOGGER.info(
            "Global debug hotkeys registered: %s hwnd=%s",
            ", ".join(registered_labels),
            hwnd,
        )
        return True

    def unregister(self) -> None:
        if not self._registered or sys.platform != "win32":
            return
        try:
            hwnd = self._window_id if self._window_id else None
            for hotkey_id in self._registered_hotkey_ids:
                ctypes.windll.user32.UnregisterHotKey(hwnd, hotkey_id)
            QCoreApplication.instance().removeNativeEventFilter(self)
        except Exception:
            LOGGER.exception("Could not unregister global debug hotkey")
        finally:
            self._registered = False
            self._registered_hotkey_ids.clear()

    def nativeEventFilter(self, event_type: bytes | str, message: int) -> tuple[bool, int]:
        event_name = (
            event_type.decode("ascii", errors="ignore")
            if isinstance(event_type, bytes)
            else str(event_type)
        )
        if event_name not in {"windows_generic_MSG", "windows_dispatcher_MSG"}:
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == self.WM_HOTKEY:
            for label, hotkey_id, _virtual_key in self.DEBUG_HOTKEYS:
                if msg.wParam == hotkey_id:
                    LOGGER.info("Global debug hotkey pressed: %s", label)
                    QTimer.singleShot(0, self._callback)
                    return True, 0
        return False, 0


class WindowsGlobalHotkeyFilter(QAbstractNativeEventFilter):
    WM_HOTKEY = 0x0312
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_ALT = 0x0001
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000

    def __init__(
        self,
        callback,
        *,
        hotkey_id: int,
        hotkey_text: str,
        window_id: int | None = None,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._hotkey_id = hotkey_id
        self._hotkey_text = hotkey_text
        self._window_id = window_id
        self._registered = False

    def register(self) -> bool:
        if sys.platform != "win32":
            return False

        parsed = self._parse_hotkey(self._hotkey_text)
        if parsed is None:
            LOGGER.warning("Could not parse global hotkey: %s", self._hotkey_text)
            return False

        modifiers, virtual_key = parsed
        hwnd = self._window_id if self._window_id else None
        try:
            ok = bool(
                ctypes.windll.user32.RegisterHotKey(
                    hwnd,
                    self._hotkey_id,
                    modifiers | self.MOD_NOREPEAT,
                    virtual_key,
                )
            )
        except Exception:
            LOGGER.exception("Could not register global hotkey %s", self._hotkey_text)
            return False
        if not ok:
            LOGGER.warning("Global hotkey already registered: %s", self._hotkey_text)
            return False

        QCoreApplication.instance().installNativeEventFilter(self)
        self._registered = True
        LOGGER.info(
            "Global hotkey registered: %s hwnd=%s",
            self._hotkey_text,
            hwnd,
        )
        return True

    def unregister(self) -> None:
        if not self._registered or sys.platform != "win32":
            return
        try:
            hwnd = self._window_id if self._window_id else None
            ctypes.windll.user32.UnregisterHotKey(hwnd, self._hotkey_id)
            QCoreApplication.instance().removeNativeEventFilter(self)
        except Exception:
            LOGGER.exception("Could not unregister global hotkey %s", self._hotkey_text)
        finally:
            self._registered = False

    def nativeEventFilter(self, event_type: bytes | str, message: int) -> tuple[bool, int]:
        event_name = (
            event_type.decode("ascii", errors="ignore")
            if isinstance(event_type, bytes)
            else str(event_type)
        )
        if event_name not in {"windows_generic_MSG", "windows_dispatcher_MSG"}:
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == self.WM_HOTKEY and msg.wParam == self._hotkey_id:
            LOGGER.info("Global hotkey pressed: %s", self._hotkey_text)
            QTimer.singleShot(0, self._callback)
            return True, 0
        return False, 0

    def _parse_hotkey(self, hotkey_text: str) -> tuple[int, int] | None:
        parts = [part.strip() for part in str(hotkey_text or "").split("+") if part.strip()]
        if not parts:
            return None

        modifiers = 0
        key_part = parts[-1].upper()
        for modifier in parts[:-1]:
            clean = modifier.lower()
            if clean in {"ctrl", "control"}:
                modifiers |= self.MOD_CONTROL
            elif clean == "shift":
                modifiers |= self.MOD_SHIFT
            elif clean == "alt":
                modifiers |= self.MOD_ALT
            elif clean in {"win", "meta", "super", "cmd", "command"}:
                modifiers |= self.MOD_WIN
            else:
                return None

        virtual_key = self._virtual_key_for_name(key_part)
        if virtual_key is None:
            return None
        return modifiers, virtual_key

    def _virtual_key_for_name(self, key_name: str) -> int | None:
        if len(key_name) == 1:
            char = key_name.upper()
            if "0" <= char <= "9" or "A" <= char <= "Z":
                return ord(char)
            return None

        if key_name.startswith("F") and key_name[1:].isdigit():
            fn_number = int(key_name[1:])
            if 1 <= fn_number <= 24:
                return 0x6F + fn_number
            return None

        named_keys = {
            "ESC": 0x1B,
            "TAB": 0x09,
            "SPACE": 0x20,
            "ENTER": 0x0D,
            "RETURN": 0x0D,
            "BACKSPACE": 0x08,
            "INSERT": 0x2D,
            "DELETE": 0x2E,
            "HOME": 0x24,
            "END": 0x23,
            "PAGEUP": 0x21,
            "PAGEDOWN": 0x22,
            "LEFT": 0x25,
            "UP": 0x26,
            "RIGHT": 0x27,
            "DOWN": 0x28,
        }
        return named_keys.get(key_name)


class WindowsGlobalHotkeyHook:
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_ALT = 0x0001
    MOD_WIN = 0x0008
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10
    VK_MENU = 0x12
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _ulong_ptr = getattr(wintypes, "ULONG_PTR", ctypes.c_void_p)
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ulong_ptr),
        ]

    try:
        _LRESULT = ctypes.c_ssize_t
    except AttributeError:  # pragma: no cover - older Python builds
        _LRESULT = ctypes.c_long

    if hasattr(ctypes, "WINFUNCTYPE"):
        HOOKPROC = ctypes.WINFUNCTYPE(
            _LRESULT,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
    else:  # pragma: no cover - non-Windows import safety
        HOOKPROC = None

    def __init__(self, on_pressed, on_released=None, *, hotkey_text: str) -> None:
        self._pressed_callback = on_pressed
        self._released_callback = on_released
        self._hotkey_text = hotkey_text
        self._hook_handle: int | None = None
        self._hook_proc = None
        self._registered = False
        self._modifiers = 0
        self._virtual_key: int | None = None
        self._poll_timer: QTimer | None = None
        self._was_pressed = False

    def register(self) -> bool:
        if sys.platform != "win32":
            return False

        parsed = self._parse_hotkey(self._hotkey_text)
        if parsed is None:
            LOGGER.warning("Could not parse global hotkey hook: %s", self._hotkey_text)
            return False

        self._modifiers, self._virtual_key = parsed
        try:
            if self.HOOKPROC is None:
                return False
            self._hook_proc = self.HOOKPROC(self._handle_keyboard_event)
            user32 = ctypes.windll.user32
            self._hook_handle = user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL,
                self._hook_proc,
                0,
                0,
            )
        except Exception:
            LOGGER.exception(
                "Could not register global hotkey hook %s", self._hotkey_text
            )
            self._hook_handle = None
            self._hook_proc = None
            return False

        if self._hook_handle:
            self._registered = True
            LOGGER.info("Global hotkey hook registered: %s", self._hotkey_text)
            return True

        LOGGER.warning(
            "Global hotkey hook could not be installed, using polling fallback: %s",
            self._hotkey_text,
        )
        self._hook_proc = None
        self._start_polling_fallback()
        return self._registered

    def unregister(self) -> None:
        if not self._registered or sys.platform != "win32":
            return
        try:
            if self._hook_handle:
                ctypes.windll.user32.UnhookWindowsHookEx(self._hook_handle)
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None
        except Exception:
            LOGGER.exception(
                "Could not unregister global hotkey hook %s", self._hotkey_text
            )
        finally:
            self._registered = False
            self._hook_handle = None
            self._hook_proc = None
            self._was_pressed = False

    def _parse_hotkey(self, hotkey_text: str) -> tuple[int, int] | None:
        parts = [part.strip() for part in str(hotkey_text or "").split("+") if part.strip()]
        if not parts:
            return None

        modifiers = 0
        key_part = parts[-1].upper()
        for modifier in parts[:-1]:
            clean = modifier.lower()
            if clean in {"ctrl", "control"}:
                modifiers |= self.MOD_CONTROL
            elif clean == "shift":
                modifiers |= self.MOD_SHIFT
            elif clean == "alt":
                modifiers |= self.MOD_ALT
            elif clean in {"win", "meta", "super", "cmd", "command"}:
                modifiers |= self.MOD_WIN
            else:
                return None

        virtual_key = self._virtual_key_for_name(key_part)
        if virtual_key is None:
            return None
        return modifiers, virtual_key

    def _virtual_key_for_name(self, key_name: str) -> int | None:
        if len(key_name) == 1:
            char = key_name.upper()
            if "0" <= char <= "9" or "A" <= char <= "Z":
                return ord(char)
            return None

        if key_name.startswith("F") and key_name[1:].isdigit():
            fn_number = int(key_name[1:])
            if 1 <= fn_number <= 24:
                return 0x6F + fn_number
            return None

        named_keys = {
            "ESC": 0x1B,
            "TAB": 0x09,
            "SPACE": 0x20,
            "ENTER": 0x0D,
            "RETURN": 0x0D,
            "BACKSPACE": 0x08,
            "INSERT": 0x2D,
            "DELETE": 0x2E,
            "HOME": 0x24,
            "END": 0x23,
            "PAGEUP": 0x21,
            "PAGEDOWN": 0x22,
            "LEFT": 0x25,
            "UP": 0x26,
            "RIGHT": 0x27,
            "DOWN": 0x28,
        }
        return named_keys.get(key_name)

    def _handle_keyboard_event(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code < 0:
            return self._call_next_hook(n_code, w_param, l_param)

        message = int(w_param)
        try:
            keyboard = ctypes.cast(
                l_param, ctypes.POINTER(self.KBDLLHOOKSTRUCT)
            ).contents
        except Exception:
            return self._call_next_hook(n_code, w_param, l_param)

        if message in {self.WM_KEYDOWN, self.WM_SYSKEYDOWN}:
            if (
                keyboard.vkCode == self._virtual_key
                and not self._was_pressed
                and self._modifiers_satisfied()
            ):
                self._was_pressed = True
                LOGGER.info("Global hotkey hook pressed: %s", self._hotkey_text)
                QTimer.singleShot(0, self._pressed_callback)
        elif message in {self.WM_KEYUP, self.WM_SYSKEYUP} and keyboard.vkCode == self._virtual_key:
            if self._was_pressed:
                self._was_pressed = False
                LOGGER.info("Global hotkey hook released: %s", self._hotkey_text)
                if self._released_callback is not None:
                    QTimer.singleShot(0, self._released_callback)

        return self._call_next_hook(n_code, w_param, l_param)

    def _call_next_hook(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            return int(
                ctypes.windll.user32.CallNextHookEx(
                    self._hook_handle or 0,
                    n_code,
                    w_param,
                    l_param,
                )
            )
        except Exception:
            return 0

    def _start_polling_fallback(self) -> None:
        if self._virtual_key is None:
            return
        self._registered = True
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(16)
        self._poll_timer.timeout.connect(self._poll_hotkey_state)
        self._poll_timer.start()

    def _poll_hotkey_state(self) -> None:
        if self._virtual_key is None:
            return
        pressed = self._modifiers_satisfied() and self._is_key_down(self._virtual_key)
        if pressed and not self._was_pressed:
            self._was_pressed = True
            LOGGER.info("Global hotkey polled: %s", self._hotkey_text)
            QTimer.singleShot(0, self._pressed_callback)
        elif not pressed:
            if self._was_pressed:
                LOGGER.info("Global hotkey polled release: %s", self._hotkey_text)
                if self._released_callback is not None:
                    QTimer.singleShot(0, self._released_callback)
            self._was_pressed = False

    def _modifiers_satisfied(self) -> bool:
        if self._virtual_key is None:
            return False
        if self._modifiers & self.MOD_CONTROL:
            if not self._is_key_down(self.VK_CONTROL):
                return False
        if self._modifiers & self.MOD_SHIFT:
            if not self._is_key_down(self.VK_SHIFT):
                return False
        if self._modifiers & self.MOD_ALT:
            if not self._is_key_down(self.VK_MENU):
                return False
        if self._modifiers & self.MOD_WIN:
            if not (self._is_key_down(self.VK_LWIN) or self._is_key_down(self.VK_RWIN)):
                return False
        return True

    def _is_key_down(self, virtual_key: int) -> bool:
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(virtual_key) & 0x8000)
        except Exception:
            return False


class MainWindow(QMainWindow):
    STATE_COLORS = {
        AssistantState.IDLE: "#64748b",
        AssistantState.LISTENING: "#0ea5e9",
        AssistantState.THINKING: "#f59e0b",
        AssistantState.TALKING: "#22c55e",
    }

    def __init__(
        self,
        *,
        settings_manager: SettingsManager,
        audio_manager: AudioManager,
        conversation_manager: ConversationManager,
        runtime_bridge: RuntimeBridge,
        deepgram_stt_manager: VoiceSTTManager,
    ) -> None:
        super().__init__()
        self.settings_manager = settings_manager
        self.audio_manager = audio_manager
        self.conversation_manager = conversation_manager
        self.runtime_bridge = runtime_bridge
        self.deepgram_stt_manager = deepgram_stt_manager
        self.usage_estimator = UsageEstimator(self.settings_manager.root)
        self._runtime_paths = get_runtime_paths(self.settings_manager.root)
        self._runtime_paths.ensure_directories()
        if self._runtime_paths.icon_path.exists():
            self.setWindowIcon(QIcon(str(self._runtime_paths.icon_path)))
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.websocket_stream_test_thread: threading.Thread | None = None
        self.voice_hotkey_shortcut: QShortcut | None = None
        self.voice_hotkey_filter: WindowsGlobalHotkeyFilter | None = None
        self.voice_hotkey_hook: WindowsGlobalHotkeyHook | None = None
        self.debug_hotkey_shortcut: QShortcut | None = None
        self.debug_hotkey_filter: WindowsDebugHotkeyFilter | None = None
        self.debug_hotkey_poll_timer: QTimer | None = None
        self._debug_value_labels: dict[str, QLabel] = {}
        self._debug_field_title_labels: dict[str, QLabel] = {}
        self._input_level_monitor_config: tuple[int | None, int, float] | None = None
        self._conversation_active = False
        self._shutdown_started = False
        self._last_mic_level_send_time = 0.0
        self._last_debug_hotkey_toggle_time = 0.0
        self._debug_hotkey_poll_was_down = False
        self._websocket_connected = False
        self._unreal_settings_events: list[dict[str, str]] = []
        self._outbound_unreal_events: list[dict[str, Any]] = []
        self._show_mic_level_outbound_events = False
        self._mic_level_show = False
        self._force_send_mic_level = False
        self._exit_myralis_requested = False
        self._out_of_credits_override: bool | None = None
        self._out_of_credits_ready = False
        self._last_out_of_credits_sent: str | None = None
        app = QCoreApplication.instance()
        self._license_validation_result = getattr(app, "license_validation_result", None)
        self._authorization_context = getattr(
            app,
            "backend_authorization_context",
            BackendAuthorizationContext.from_license_result(
                self._license_validation_result
            ),
        )
        self._settings_is_open = False
        self._debug_state = dict(DEFAULT_DEBUG_STATE)
        self._unreal_debug_mode = False
        self.current_language = normalize_current_language(
            self.settings_manager.get_setting(
                CURRENT_LANGUAGE_SETTING_ID,
                "spanish",
            )
        )
        self.current_state = AssistantState.IDLE
        self._window_drag_offset: QPoint | None = None
        self._window_drag_widgets: set[QWidget] = set()
        self._ui_font_family = self._preferred_ui_font_family()
        self.conversation_manager.add_ai_realtime_processing_listener(
            lambda enabled: self.events.put(
                {"type": "ai_realtime_processing", "enabled": enabled}
            )
        )
        self.conversation_manager.set_backend_ui_action_handler(
            lambda action: self.events.put(
                {"type": "backend_ui_action", "action": action}
            )
        )
        set_unreal_json_message_handler(self._handle_unreal_json_message_for_ui)
        self.deepgram_stt_manager.set_transcript_callbacks(
            on_partial=self.conversation_manager.handle_partial_stt_transcript,
            on_final=lambda text: self.events.put(
                {"type": "voice_transcript", "text": text}
            ),
        )
        add_websocket_connection_status_handler(self._handle_websocket_connection_status)
        add_websocket_outgoing_message_handler(self._handle_websocket_outgoing_message)

        self.setWindowTitle(OFFICIAL_BACKEND_DISPLAY_NAME)
        self.resize(1180, 780)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._build_ui()
        self._apply_style()
        self._lock_down_debug_state()
        self._set_state(AssistantState.IDLE)
        self._configure_voice_hotkey()
        self._configure_debug_hotkey()
        self._start_input_level_monitor()

        interval = int(
            self.settings_manager.get_setting("app.state_poll_interval_ms", 100)
        )
        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self._process_worker_events)
        self.event_timer.start(interval)

        self.audio_level_timer = QTimer(self)
        self.audio_level_timer.timeout.connect(self._update_audio_level_meters)
        self.audio_level_timer.start(50)
        self._start_websocket_server(auto=True)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("rootWindow")
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(0)

        shell = QFrame()
        shell.setObjectName("debugShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(18, 14, 18, 18)
        shell_layout.setSpacing(14)

        shell_layout.addWidget(self._build_chrome_bar())
        shell_layout.addWidget(self._build_signal_strip())

        self.chat_panel = ChatPanel()

        self.debug_tabs = QTabWidget()
        self.debug_tabs.setObjectName("debugTabs")
        self.debug_tabs.tabBar().setObjectName("debugTabBar")
        self.debug_tabs.addTab(self._build_settings_bridge_tab(), "SETTINGS BRIDGE")
        self.debug_tabs.addTab(self._build_conversation_tab(), "CONVERSATION")
        self.debug_tabs.addTab(self._build_technical_tab(), "TECHNICAL")
        shell_layout.addWidget(self.debug_tabs, 1)

        root.addWidget(shell)
        self.setCentralWidget(central)
        self._apply_ui_language(self.current_language)

    def _build_chrome_bar(self) -> QFrame:
        chrome = QFrame()
        chrome.setObjectName("customTitleBar")
        chrome.setFixedHeight(78)
        chrome_layout = QHBoxLayout(chrome)
        chrome_layout.setContentsMargins(16, 8, 8, 8)
        chrome_layout.setSpacing(14)

        logo_label = QLabel()
        logo_label.setObjectName("brandLogo")
        logo_label.setFixedSize(240, 58)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_label.setPixmap(self._load_logo_pixmap(240, 58))

        title_stack = QWidget()
        title_stack.setObjectName("titleStack")
        title_layout = QVBoxLayout(title_stack)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setObjectName("appTitle")
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("appSubtitle")
        title_layout.addWidget(self.title_label)
        title_layout.addWidget(self.subtitle_label)

        self.minimize_button = QPushButton("−")
        self.minimize_button.setObjectName("windowMinimizeButton")
        self.minimize_button.setFixedSize(34, 30)
        self.minimize_button.clicked.connect(self.showMinimized)

        self.close_button = QPushButton("×")
        self.close_button.setObjectName("windowCloseButton")
        self.close_button.setFixedSize(34, 30)
        self.close_button.clicked.connect(self.close)

        chrome_layout.addWidget(logo_label)
        chrome_layout.addWidget(title_stack)
        chrome_layout.addStretch(1)
        chrome_layout.addWidget(self.minimize_button)
        chrome_layout.addWidget(self.close_button)

        self._register_window_drag_widget(chrome)
        self._register_window_drag_widget(logo_label)
        self._register_window_drag_widget(title_stack)
        self._register_window_drag_widget(self.title_label)
        self._register_window_drag_widget(self.subtitle_label)
        return chrome

    def _build_signal_strip(self) -> QFrame:
        strip = QFrame()
        strip.setObjectName("signalStrip")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(18)

        state_stack = QWidget()
        state_layout = QVBoxLayout(state_stack)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.setSpacing(5)
        state_title = QLabel("SYSTEM")
        state_title.setObjectName("sectionTitle")

        state_row = QWidget()
        state_row_layout = QHBoxLayout(state_row)
        state_row_layout.setContentsMargins(0, 0, 0, 0)
        state_row_layout.setSpacing(8)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(12, 12)
        self.status_label = QLabel(AssistantState.IDLE.value)
        self.status_label.setObjectName("statusLabel")
        state_row_layout.addWidget(self.status_dot)
        state_row_layout.addWidget(self.status_label)
        state_row_layout.addStretch(1)
        state_layout.addWidget(state_title)
        state_layout.addWidget(state_row)

        self.mic_level_meter = self._build_level_meter(
            "micLevelMeter", self._t("mic_tooltip")
        )
        self.output_level_meter = self._build_level_meter(
            "outputLevelMeter", self._t("out_tooltip")
        )
        meters_panel = QWidget()
        meters_panel.setObjectName("metersPanel")
        meters_layout = QVBoxLayout(meters_panel)
        meters_layout.setContentsMargins(0, 0, 0, 0)
        meters_layout.setSpacing(7)
        meters_title = QLabel("AUDIO")
        meters_title.setObjectName("sectionTitle")
        meters_layout.addWidget(meters_title)
        meters_layout.addLayout(self._build_level_meter_row("MIC", self.mic_level_meter))
        meters_layout.addLayout(
            self._build_level_meter_row("OUT", self.output_level_meter)
        )

        debug_mode_panel = QWidget()
        debug_mode_panel.setObjectName("debugModePanel")
        debug_mode_layout = QVBoxLayout(debug_mode_panel)
        debug_mode_layout.setContentsMargins(0, 0, 0, 0)
        debug_mode_layout.setSpacing(5)
        self.debug_mode_title = QLabel()
        self.debug_mode_title.setObjectName("sectionTitle")

        debug_mode_row = QWidget()
        debug_mode_row_layout = QHBoxLayout(debug_mode_row)
        debug_mode_row_layout.setContentsMargins(0, 0, 0, 0)
        debug_mode_row_layout.setSpacing(8)
        self.debug_mode_dot = QLabel()
        self.debug_mode_dot.setObjectName("debugModeDot")
        self.debug_mode_dot.setFixedSize(12, 12)
        self.debug_mode_label = QLabel()
        self.debug_mode_label.setObjectName("debugModeLabel")
        self.debug_mode_toggle_button = QPushButton()
        self.debug_mode_toggle_button.setObjectName("debugModeToggleButton")
        self.debug_mode_toggle_button.setCheckable(True)
        self.debug_mode_toggle_button.clicked.connect(
            self._handle_debug_mode_toggle_clicked
        )
        self.debug_mode_toggle_button.setFixedHeight(34)
        self.unreal_debug_mode_toggle_button = QPushButton()
        self.unreal_debug_mode_toggle_button.setObjectName(
            "unrealDebugModeToggleButton"
        )
        self.unreal_debug_mode_toggle_button.setCheckable(True)
        self.unreal_debug_mode_toggle_button.clicked.connect(
            self._handle_unreal_debug_mode_toggle_clicked
        )
        self.unreal_debug_mode_toggle_button.setFixedHeight(34)
        debug_mode_row_layout.addWidget(self.debug_mode_dot)
        debug_mode_row_layout.addWidget(self.debug_mode_label)
        debug_mode_row_layout.addWidget(self.debug_mode_toggle_button)
        debug_mode_row_layout.addWidget(self.unreal_debug_mode_toggle_button)
        debug_mode_row_layout.addStretch(1)
        debug_mode_layout.addWidget(self.debug_mode_title)
        debug_mode_layout.addWidget(debug_mode_row)

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self._open_settings)
        self.settings_button.setFixedHeight(34)

        self.websocket_button = QPushButton()
        self.websocket_button.setObjectName("websocketButton")
        self.websocket_button.clicked.connect(self._reload_websocket_server)
        self.websocket_button.setFixedHeight(34)

        layout.addWidget(state_stack)
        layout.addWidget(meters_panel)
        layout.addWidget(debug_mode_panel)
        control_panel = QWidget()
        control_panel.setObjectName("controlDeckPanel")
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(6)
        control_title = QLabel("CONTROL")
        control_title.setObjectName("sectionTitle")
        control_row = QWidget()
        control_row_layout = QHBoxLayout(control_row)
        control_row_layout.setContentsMargins(0, 0, 0, 0)
        control_row_layout.setSpacing(8)
        control_row_layout.addWidget(self.websocket_button)
        control_row_layout.addWidget(self.settings_button)
        control_row_layout.addStretch(1)
        control_layout.addWidget(control_title)
        control_layout.addWidget(control_row)
        layout.addWidget(control_panel)
        layout.addStretch(1)
        return strip

    def _build_settings_bridge_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        update_panel = self._build_unreal_update_panel()
        outbound_panel = self._build_unreal_outbound_panel()
        layout.addWidget(update_panel, 1)
        layout.addWidget(outbound_panel, 1)
        return tab

    def _build_conversation_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_conversation_mode_panel())
        layout.addWidget(self.chat_panel, 1)
        return tab

    def _build_conversation_mode_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("conversationModePanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(9)

        self.conversation_mode_title = QLabel()
        self.conversation_mode_title.setObjectName("conversationModeTitle")
        self.conversation_mode_dot = QLabel()
        self.conversation_mode_dot.setObjectName("conversationModeDot")
        self.conversation_mode_dot.setFixedSize(12, 12)
        self.conversation_mode_label = QLabel()
        self.conversation_mode_label.setObjectName("conversationModeLabel")

        layout.addWidget(self.conversation_mode_title)
        layout.addWidget(self.conversation_mode_dot)
        layout.addWidget(self.conversation_mode_label)
        layout.addStretch(1)
        return panel

    def _build_technical_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        debug_panel = self._build_debug_panel()
        layout.addWidget(debug_panel, 1)
        return tab

    def _register_window_drag_widget(self, widget: QWidget) -> None:
        widget.installEventFilter(self)
        self._window_drag_widgets.add(widget)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched in self._window_drag_widgets:
            event_type = event.type()
            if event_type == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
                    self._window_drag_offset = (
                        self._event_global_position(event) - self.frameGeometry().topLeft()
                    )
                    event.accept()
                    return True
            if event_type == QEvent.Type.MouseMove and self._window_drag_offset is not None:
                if event.buttons() & Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
                    self.move(self._event_global_position(event) - self._window_drag_offset)
                    event.accept()
                    return True
            if event_type == QEvent.Type.MouseButtonRelease:
                self._window_drag_offset = None
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _event_global_position(self, event: QEvent) -> QPoint:
        global_position = getattr(event, "globalPosition", None)
        if callable(global_position):
            return global_position().toPoint()
        global_pos = getattr(event, "globalPos", None)
        if callable(global_pos):
            return global_pos()
        return QPoint(0, 0)

    def _preferred_ui_font_family(self) -> str:
        families = set(QFontDatabase.families())
        if "Montserrat" in families:
            return "Montserrat"
        if "Segoe UI" in families:
            return "Segoe UI"
        return "Arial"

    def _t(self, key: str) -> str:
        return tr(self.current_language, key)

    def _apply_ui_language(self, language: str | None = None) -> None:
        if language is not None:
            self.current_language = normalize_current_language(language)

        self.setWindowTitle(f"MYRALIS AI - {self._t('technical_console')}")
        self.title_label.setText("MYRALIS AI")
        self.subtitle_label.setText(self._t("technical_console").upper())
        self.settings_button.setText(self._t("settings").upper())
        self.websocket_button.setText(self._t("reload_websocket").upper())
        self.debug_mode_title.setText(self._t("debug_mode").upper())
        debug_mode_tooltip = self._t("debug_mode_tooltip")
        self.debug_mode_title.setToolTip(debug_mode_tooltip)
        self.debug_mode_dot.setToolTip(debug_mode_tooltip)
        self.debug_mode_label.setToolTip(debug_mode_tooltip)
        self.debug_mode_toggle_button.setToolTip(debug_mode_tooltip)
        self.unreal_debug_mode_toggle_button.setText(
            self._t("debug_mode_unreal").upper()
        )
        self.debug_panel_title.setText(self._t("debug_panel").upper())
        self.debug_log_view.setPlaceholderText(self._t("logs"))
        self.unreal_outbound_title.setText("PYTHON -> UNREAL")
        self.unreal_outbound_subtitle.setText("Mensajes enviados desde Python")
        self._refresh_mic_level_outbound_toggle()
        self._refresh_mic_level_force_toggle()
        if hasattr(self, "debug_tabs"):
            self.debug_tabs.setTabText(0, "SETTINGS BRIDGE")
            self.debug_tabs.setTabText(1, "CONVERSATION")
            self.debug_tabs.setTabText(2, "TECHNICAL")
        self.mic_level_meter.setToolTip(self._t("mic_tooltip"))
        self.output_level_meter.setToolTip(self._t("out_tooltip"))
        self.chat_panel.set_language(self.current_language)
        self._refresh_conversation_mode_indicator()

        debug_label_keys = {
            "websocket": "websocket",
            "runtime": "runtime_state",
            "interaction": "interaction",
            "tts": "tts",
            "devices": "audio_devices",
            "usage": "usage_estimate",
        }
        for field, translation_key in debug_label_keys.items():
            label = self._debug_field_title_labels.get(field)
            if label is not None:
                label.setText(self._t(translation_key))

        self._refresh_unreal_settings_panel()
        self._refresh_unreal_outbound_panel()
        self._refresh_debug_mode_controls()
        self._refresh_unreal_debug_mode_controls()

    def _refresh_conversation_mode_indicator(self) -> None:
        if not hasattr(self, "conversation_mode_label"):
            return

        mode = self._interaction_mode(self.settings_manager.get_settings())
        self.conversation_mode_title.setText(self._t("conversation_input_mode").upper())
        self.conversation_mode_label.setText(
            self._t("conversation_mode_voice")
            if mode == "voice"
            else self._t("conversation_mode_text")
        )
        if mode == "voice":
            dot_color = "#0ea5e9"
            border_color = "#7dd3fc"
        else:
            dot_color = "#f59e0b"
            border_color = "#fcd34d"
        self.conversation_mode_dot.setStyleSheet(
            "background: "
            f"{dot_color}; border-radius: 6px; border: 1px solid {border_color};"
        )

    def _refresh_unreal_settings_panel(self) -> None:
        self.unreal_update_history_title.setText(
            self._t("recent_unreal_changes").upper()
        )
        if not self._unreal_settings_events:
            self.unreal_update_title.setText(self._t("unreal_monitor"))
            self.unreal_update_details.setText(
                self._muted_panel_text(self._t("unreal_waiting"))
            )
            self.unreal_update_history_view.setHtml(
                self._muted_panel_text(self._t("no_unreal_changes"))
            )
            return

        self._render_unreal_settings_visual_event(self._unreal_settings_events[0])

    def _refresh_unreal_outbound_panel(self) -> None:
        if not hasattr(self, "unreal_outbound_view"):
            return

        self._refresh_mic_level_outbound_toggle()
        self._refresh_mic_level_force_toggle()
        self.unreal_outbound_count.setText(
            f"{len(self._outbound_unreal_events)} / {MAX_OUTBOUND_UNREAL_EVENTS}"
        )
        if not self._outbound_unreal_events:
            self.unreal_outbound_view.setHtml(
                self._muted_panel_text("Sin mensajes enviados todavia.")
            )
            return

        self.unreal_outbound_view.setHtml(
            "\n".join(
                self._outbound_event_html(event)
                for event in self._outbound_unreal_events
            )
        )
        scrollbar = self.unreal_outbound_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.minimum())

    def _refresh_mic_level_outbound_toggle(self) -> None:
        if not hasattr(self, "mic_level_outbound_toggle_button"):
            return
        enabled = bool(getattr(self, "_show_mic_level_outbound_events", False))
        self.mic_level_outbound_toggle_button.setChecked(enabled)
        self.mic_level_outbound_toggle_button.setText(
            self._t("hide_mic_level_events")
            if enabled
            else self._t("show_mic_level_events")
        )

    def _refresh_mic_level_force_toggle(self) -> None:
        if not hasattr(self, "mic_level_force_toggle_button"):
            return
        enabled = bool(getattr(self, "_force_send_mic_level", False))
        self.mic_level_force_toggle_button.setChecked(enabled)
        self.mic_level_force_toggle_button.setText(
            self._t("disable_force_mic_level")
            if enabled
            else self._t("enable_force_mic_level")
        )

    def _handle_mic_level_outbound_toggle_clicked(self, checked: bool) -> None:
        self._show_mic_level_outbound_events = bool(checked)
        if not self._show_mic_level_outbound_events:
            self._outbound_unreal_events = [
                event
                for event in self._outbound_unreal_events
                if str(event.get("type", "")).strip() != "mic_level"
            ]
        self._refresh_unreal_outbound_panel()

    def _handle_mic_level_force_toggle_clicked(self, checked: bool) -> None:
        self._force_send_mic_level = bool(checked)
        self._refresh_mic_level_force_toggle()

    def _handle_out_of_credits_test_toggled(self, checked: bool) -> None:
        self._out_of_credits_override = bool(checked)
        self._send_out_of_credits_state()

    def _current_out_of_credits_state(self) -> bool:
        return self._actual_out_of_credits_state() if self._out_of_credits_ready else False

    def _actual_out_of_credits_state(self) -> bool:
        override = getattr(self, "_out_of_credits_override", None)
        if override is not None:
            return bool(override)

        result = getattr(self, "_license_validation_result", None)
        credits_balance = getattr(result, "credits_balance", None)
        if credits_balance is not None:
            try:
                balance = float(credits_balance)
            except (TypeError, ValueError):
                return False
            if balance <= 0.0:
                return True
            return balance < self._estimated_generation_cost_usd()
        return False

    def _estimated_generation_cost_usd(self) -> float:
        settings = self.settings_manager.get_settings()
        profile = self._usage_profile(settings)
        return self._estimated_complete_interaction_cost(settings, profile)

    def _build_out_of_credits_payload(self) -> dict[str, Any]:
        is_out_of_credits = self._current_out_of_credits_state()
        return {
            "type": "out_of_credits",
            "is_out_of_credits": "True" if is_out_of_credits else "False",
        }

    def _send_out_of_credits_state(self, *, force: bool = False) -> None:
        if not self._out_of_credits_ready:
            return
        current = self._current_out_of_credits_state()
        current_payload_value = "True" if current else "False"
        if not force and self._last_out_of_credits_sent is not None:
            if self._last_out_of_credits_sent == current_payload_value:
                return
        self._last_out_of_credits_sent = current_payload_value
        LOGGER.info(
            "out_of_credits payload sent: is_out_of_credits=%s",
            current_payload_value,
        )
        send_json_to_unreal_threadsafe(self._build_out_of_credits_payload())

    def _record_unreal_outbound_event(self, event: dict[str, Any]) -> None:
        message_type = str(event.get("type", "")).strip()
        if (
            message_type == "mic_level"
            and not getattr(self, "_show_mic_level_outbound_events", False)
        ):
            return
        clean_event = dict(event)
        clean_event["time"] = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._outbound_unreal_events.insert(0, clean_event)
        del self._outbound_unreal_events[MAX_OUTBOUND_UNREAL_EVENTS:]
        self._refresh_unreal_outbound_panel()

    def _outbound_event_html(self, event: dict[str, Any]) -> str:
        message_type = str(event.get("type", "unknown") or "unknown")
        transport = str(event.get("transport", "-") or "-")
        color = OUTBOUND_MESSAGE_COLORS.get(message_type, "#f4f1ea")
        time_text = html.escape(str(event.get("time", "--:--:--")))
        type_text = html.escape(message_type)
        transport_text = html.escape(transport)
        detail_html = self._outbound_event_details_html(event)
        client_count = event.get("client_count")
        client_text = ""
        if client_count is not None:
            client_text = (
                ' <span style="color:#9f9a8d;">clientes='
                f"{html.escape(str(client_count))}</span>"
            )
        return (
            '<div style="background:#080808; border-left:4px solid '
            f"{color}; padding:6px 8px; margin:0 0 6px 0; color:#f4f1ea;"
            ' font-family:Consolas, Cascadia Mono, monospace; font-size:11px;">'
            f'<span style="color:#9f9a8d;">{time_text}</span> '
            f'<span style="color:{color}; font-weight:800;">ID: {type_text}</span> '
            f'<span style="color:#c9a24d;">via {transport_text}</span>'
            f"{client_text}<br>"
            f"{detail_html}"
            "</div>"
        )

    def _outbound_event_details_html(self, event: dict[str, Any]) -> str:
        payload = event.get("payload")
        if isinstance(payload, dict):
            return self._payload_kv_html(payload)

        message_type = str(event.get("type", "unknown") or "unknown")
        if message_type == "audio_chunk":
            return self._payload_kv_html(
                {
                    "bytes": event.get("bytes", 0),
                    "chunk_ms": event.get("chunk_ms", "-"),
                    "realtime_pacing": self._format_unreal_value(
                        event.get("realtime_pacing")
                    ),
                }
            )

        if "message" in event:
            return self._payload_kv_html(
                {
                    "message": event.get("message"),
                    "bytes": event.get("bytes", "-"),
                }
            )

        if "bytes" in event:
            return self._payload_kv_html({"bytes": event.get("bytes")})

        try:
            text = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        except TypeError:
            text = str(event)
        return self._muted_panel_text(text)

    def _payload_kv_html(self, payload: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, value in payload.items():
            value_color = self._payload_value_color(str(key))
            parts.append(
                '<span style="white-space:nowrap;">'
                f'<span style="color:#bca75e;font-weight:800;">'
                f"{html.escape(str(key))}</span>"
                f'<span style="color:{SETTING_MUTED_COLOR};">=</span>'
                f'<span style="color:{value_color};font-weight:800;">'
                f"{html.escape(self._format_unreal_value(value))}</span>"
                "</span>"
            )
        return " &nbsp; ".join(parts)

    def _payload_value_color(self, key: str) -> str:
        if key == "setting":
            return SETTING_ID_COLOR
        if key in {"value", "previous", "new", "level"} or key.endswith("_value"):
            return SETTING_VALUE_COLOR
        return SETTING_META_COLOR

    def _load_logo_pixmap(self, width: int, height: int) -> QPixmap:
        logo_path = self._resolve_logo_path()
        if logo_path is None:
            return QPixmap()

        pixmap = QPixmap(str(logo_path))
        if pixmap.isNull():
            LOGGER.warning("Could not load UI logo: %s", logo_path)
            return QPixmap()

        if pixmap.height() > pixmap.width() * 0.45:
            crop_height = max(1, int(pixmap.height() * 0.34))
            crop_y = max(0, int((pixmap.height() - crop_height) / 2))
            pixmap = pixmap.copy(0, crop_y, pixmap.width(), crop_height)

        return pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _resolve_logo_path(self):
        assets_dir = self._runtime_paths.assets_root
        preferred_names = ("LOGO.png", "logo.png", "Logo.png", "LOGO.PNG")
        for name in preferred_names:
            path = assets_dir / name
            if path.exists():
                return path

        try:
            return next(iter(sorted(assets_dir.glob("*.png"))), None)
        except OSError:
            LOGGER.exception("Could not inspect assets directory for UI logo")
            return None

    def _build_unreal_update_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("unrealUpdatePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        self.unreal_update_title = QLabel()
        self.unreal_update_title.setObjectName("unrealUpdateTitle")
        layout.addWidget(self.unreal_update_title)

        self.unreal_update_details = QLabel()
        self.unreal_update_details.setObjectName("unrealUpdateDetails")
        self.unreal_update_details.setWordWrap(True)
        self.unreal_update_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.unreal_update_details)

        self.unreal_update_history_title = QLabel()
        self.unreal_update_history_title.setObjectName("unrealUpdateHistoryTitle")
        layout.addWidget(self.unreal_update_history_title)

        self.unreal_update_history_view = QTextEdit()
        self.unreal_update_history_view.setObjectName("unrealUpdateHistoryView")
        self.unreal_update_history_view.setReadOnly(True)
        self.unreal_update_history_view.setMinimumHeight(88)
        self.unreal_update_history_view.setHtml("")
        layout.addWidget(self.unreal_update_history_view)
        return panel

    def _build_unreal_outbound_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("unrealOutboundPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title_stack = QWidget()
        title_layout = QVBoxLayout(title_stack)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(1)
        self.unreal_outbound_title = QLabel()
        self.unreal_outbound_title.setObjectName("unrealOutboundTitle")
        self.unreal_outbound_subtitle = QLabel()
        self.unreal_outbound_subtitle.setObjectName("unrealOutboundSubtitle")
        title_layout.addWidget(self.unreal_outbound_title)
        title_layout.addWidget(self.unreal_outbound_subtitle)

        self.unreal_outbound_count = QLabel("0")
        self.unreal_outbound_count.setObjectName("unrealOutboundCount")
        self.mic_level_outbound_toggle_button = QPushButton()
        self.mic_level_outbound_toggle_button.setObjectName(
            "micLevelOutboundToggleButton"
        )
        self.mic_level_outbound_toggle_button.setCheckable(True)
        self.mic_level_outbound_toggle_button.clicked.connect(
            self._handle_mic_level_outbound_toggle_clicked
        )
        self.mic_level_force_toggle_button = QPushButton()
        self.mic_level_force_toggle_button.setObjectName("micLevelForceToggleButton")
        self.mic_level_force_toggle_button.setCheckable(True)
        self.mic_level_force_toggle_button.clicked.connect(
            self._handle_mic_level_force_toggle_clicked
        )

        header_layout.addWidget(title_stack)
        header_layout.addStretch(1)
        header_layout.addWidget(self.mic_level_outbound_toggle_button)
        header_layout.addWidget(self.mic_level_force_toggle_button)
        header_layout.addWidget(self.unreal_outbound_count)
        layout.addWidget(header)

        self.unreal_outbound_view = QTextEdit()
        self.unreal_outbound_view.setObjectName("unrealOutboundView")
        self.unreal_outbound_view.setReadOnly(True)
        self.unreal_outbound_view.setMinimumHeight(108)
        self.unreal_outbound_view.setHtml("")
        layout.addWidget(self.unreal_outbound_view)
        return panel

    def _build_debug_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("debugPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.debug_panel_title = QLabel()
        self.debug_panel_title.setObjectName("debugPanelTitle")
        layout.addWidget(self.debug_panel_title)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)

        rows = (
            ("websocket", "WebSocket"),
            ("runtime", "Runtime state"),
            ("interaction", "Interaction"),
            ("tts", "TTS"),
            ("devices", "Audio devices"),
            ("usage", "Usage estimate"),
        )
        for row, (key, label_text) in enumerate(rows):
            label = QLabel(label_text)
            label.setObjectName("debugFieldLabel")
            value = QLabel("-")
            value.setObjectName("debugFieldValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._debug_field_title_labels[key] = label
            self._debug_value_labels[key] = value
            grid.addWidget(label, row, 0)
            grid.addWidget(value, row, 1)

        layout.addLayout(grid)

        self.debug_log_view = QPlainTextEdit()
        self.debug_log_view.setObjectName("debugLogView")
        self.debug_log_view.setReadOnly(True)
        self.debug_log_view.setMaximumBlockCount(240)
        self.debug_log_view.setMinimumHeight(260)
        self.debug_log_view.setPlaceholderText("")
        layout.addWidget(self.debug_log_view, 1)
        return panel

    def _start_conversation_from_text(self, text: str, *, source: str) -> bool:
        clean_text = text.strip()
        if not clean_text:
            return False
        self.chat_panel.add_user_message(clean_text)
        if self._conversation_active or (
            self.worker_thread and self.worker_thread.is_alive()
        ):
            LOGGER.info(
                "Received %s input while a conversation is already active",
                source,
            )
            return False

        self._conversation_active = True
        self.chat_panel.set_input_enabled(False)
        self.conversation_manager.note_user_interaction()
        self._set_state(AssistantState.THINKING)

        settings_snapshot = self.settings_manager.get_settings()
        LOGGER.info("Conversation started from %s input", source)
        self.worker_thread = threading.Thread(
            target=self._conversation_worker,
            args=(clean_text, settings_snapshot),
            daemon=True,
        )
        self.worker_thread.start()
        return True

    def _conversation_worker(self, text: str, settings: dict[str, Any]) -> None:
        try:
            result = self.conversation_manager.process_user_message(
                text,
                settings,
                state_callback=lambda state: self.events.put(
                    {"type": "state", "state": state}
                ),
            )
            self.events.put({"type": "result", "result": result})
        except Exception as exc:
            LOGGER.exception("Conversation worker failed")
            self.events.put({"type": "error", "message": str(exc)})

    def _process_worker_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            if event["type"] == "state":
                self._set_state(event["state"])
            elif event["type"] == "result":
                self._handle_result(event["result"])
            elif event["type"] == "error":
                self._handle_error(event["message"])
            elif event["type"] == "system":
                self.chat_panel.add_system_message(event["message"])
            elif event["type"] == "stream_test_done":
                self.chat_panel.set_input_enabled(True)
                self._conversation_active = False
            elif event["type"] == "ai_realtime_processing":
                self._handle_ai_realtime_processing_changed(bool(event["enabled"]))
            elif event["type"] == "backend_ui_action":
                self._handle_backend_ui_action(str(event["action"]))
            elif event["type"] == "websocket_connection":
                self._handle_websocket_connection_event(bool(event["connected"]))
            elif event["type"] == "voice_transcript":
                self._handle_voice_transcript(str(event["text"]))
            elif event["type"] == "unreal_text_message":
                self._start_conversation_from_text(
                    str(event["text"]),
                    source="unreal_text",
                )
            elif event["type"] == "unreal_settings_event":
                self._record_unreal_settings_visual_event(event["event"])
            elif event["type"] == "websocket_outgoing":
                self._record_unreal_outbound_event(event["event"])
            elif event["type"] == "ui_language":
                self._apply_ui_language(str(event["language"]))

    def _handle_websocket_outgoing_message(self, event: dict[str, Any]) -> None:
        if str(event.get("type", "")).strip() == "runtime_state":
            runtime_payload = event.get("payload")
            if isinstance(runtime_payload, dict):
                state_name = str(runtime_payload.get("state", "")).strip().upper()
                try:
                    state = AssistantState[state_name]
                except KeyError:
                    state = None
                if state is not None:
                    self.events.put({"type": "state", "state": state})
        self.events.put({"type": "websocket_outgoing", "event": dict(event)})

    def _handle_unreal_json_message_for_ui(self, payload: dict[str, Any]) -> bool:
        message_type = str(payload.get("type", "")).strip()
        if message_type == "mic_level" and "show" in payload:
            self._mic_level_show = self._parse_bool(payload.get("show"), default=False)
            LOGGER.info("Mic level show flag updated from Unreal: %s", self._mic_level_show)
            return True
        if message_type == "text_input_state":
            return self._handle_unreal_text_input_state(payload)
        if message_type in {"user_text", "text_input", "chat_message"}:
            return self._handle_unreal_text_message(payload)
        if message_type not in {"settings_update", "settings_action"}:
            return self.conversation_manager.handle_unreal_websocket_message(payload)

        before = self.settings_manager.get_settings()
        handled = self.conversation_manager.handle_unreal_websocket_message(payload)
        if not handled:
            return False

        after = self.settings_manager.get_settings()
        action = str(payload.get("action", "")).strip() if message_type == "settings_action" else ""
        setting_id = str(payload.get("setting", "")).strip() if message_type == "settings_update" else ""
        if action == "settings_is_open":
            self._settings_is_open = True
        elif action == "settings_is_closed":
            self._settings_is_open = False
        before_language = normalize_current_language(
            before.get(CURRENT_LANGUAGE_SETTING_ID, "spanish")
        )
        after_language = normalize_current_language(
            after.get(CURRENT_LANGUAGE_SETTING_ID, "spanish")
        )
        if before_language != after_language:
            self.events.put({"type": "ui_language", "language": after_language})

        if action == "mic_level_is_showing":
            self._mic_level_show = True
            LOGGER.info("Mic level show flag updated from Unreal: %s", self._mic_level_show)
        elif action == "mic_level_is_not_showing":
            self._mic_level_show = False
            LOGGER.info("Mic level show flag updated from Unreal: %s", self._mic_level_show)
        elif action == "mic_level_not_showing":
            self._mic_level_show = False
            LOGGER.info("Mic level show flag updated from Unreal: %s", self._mic_level_show)
        elif action == "finish_loading":
            self._out_of_credits_ready = True
            self._send_out_of_credits_state(force=True)

        if message_type == "settings_update" and setting_id == "input_device":
            self._start_input_level_monitor()
            self._sync_deepgram_stt_for_state(self.current_state)

        visual_event = self._build_unreal_settings_visual_event(
            payload,
            before,
            after,
        )
        if visual_event is not None:
            self.events.put(
                {
                    "type": "unreal_settings_event",
                    "event": visual_event,
                }
            )
        if not self._is_mic_level_action(payload):
            self.events.put({"type": "backend_ui_action", "action": "settings_changed"})
        return True

    def _is_mic_level_action(self, payload: dict[str, Any]) -> bool:
        if str(payload.get("type", "")).strip() != "settings_action":
            return False
        action = str(payload.get("action", "")).strip()
        return action in {
            "settings_is_open",
            "settings_is_closed",
            "mic_level_is_showing",
            "mic_level_is_not_showing",
            "mic_level_not_showing",
        }

    def _handle_unreal_text_input_state(self, payload: dict[str, Any]) -> bool:
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "text":
            LOGGER.info("Ignoring Unreal text_input_state because interaction_mode=voice")
            return False

        state = self._normalize_unreal_text_input_state(payload.get("state"))
        if not state:
            LOGGER.warning(
                "Ignoring invalid Unreal text_input_state: %r",
                payload.get("state"),
            )
            return False

        if self._conversation_active:
            LOGGER.info(
                "Ignoring Unreal text_input_state=%s because a conversation is active",
                state,
            )
            return True

        if state == "started":
            self.conversation_manager.note_user_interaction()
            if self.current_state != AssistantState.LISTENING:
                self._set_state(AssistantState.LISTENING)
            self._emit_unreal_text_input_runtime_state(AssistantState.LISTENING)
            LOGGER.info("Unreal text_input_state started -> LISTENING")
            return True

        if self.current_state != AssistantState.IDLE:
            self._set_state(AssistantState.IDLE)
        self._emit_unreal_text_input_runtime_state(AssistantState.IDLE)
        LOGGER.info("Unreal text_input_state timeout -> IDLE")
        return True

    def _handle_unreal_text_message(self, payload: dict[str, Any]) -> bool:
        LOGGER.info(
            "Received Unreal text payload: %s",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "text":
            LOGGER.info("Ignoring Unreal text input because interaction_mode=voice")
            return False

        text = self._unreal_text_from_payload(payload)
        if not text:
            return False
        self.events.put({"type": "unreal_text_message", "text": text})
        return True

    def _unreal_text_from_payload(self, payload: dict[str, Any]) -> str:
        return str(
            payload.get("text", payload.get("message", payload.get("value", "")))
        ).strip()

    def _normalize_unreal_text_input_state(self, value: Any) -> str:
        clean = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if clean in {"started", "start"}:
            return "started"
        if clean in {"timeout", "time_out", "timed_out"}:
            return "timeout"
        return ""

    def _emit_unreal_text_input_runtime_state(self, state: AssistantState) -> None:
        try:
            self.conversation_manager.emit_external_state(state)
        except Exception:
            LOGGER.exception("Could not emit Unreal text input runtime state")

    def _parse_bool(self, value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        clean = str(value).strip().lower()
        if clean in {"true", "1", "yes", "on"}:
            return True
        if clean in {"false", "0", "no", "off"}:
            return False
        return default

    def _build_unreal_settings_visual_event(
        self,
        payload: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, str] | None:
        message_type = str(payload.get("type", "")).strip()
        timestamp = datetime.now().strftime("%H:%M:%S")
        if message_type == "settings_update":
            setting = str(payload.get("setting", "")).strip()
            group = self._unreal_setting_group(setting)
            previous_value = self._setting_value_from_snapshot(before, setting, group)
            new_value = self._setting_value_from_snapshot(after, setting, group)
            if new_value is None:
                new_value = payload.get("value")
            return {
                "title": "UNREAL UPDATE RECEIVED",
                "title_key": "unreal_update_received",
                "type": "settings_update",
                "group": group,
                "item_label": "Setting",
                "item_label_key": "setting",
                "item": setting or "-",
                "previous": self._format_unreal_value(previous_value),
                "new": self._format_unreal_value(new_value),
                "effect": self._unreal_update_effect(setting, group),
                "time": timestamp,
                "source": "Unreal WebSocket",
            }
        if message_type == "settings_action":
            action = str(payload.get("action", "")).strip()
            return {
                "title": "UNREAL ACTION RECEIVED",
                "title_key": "unreal_action_received",
                "type": "settings_action",
                "group": self._unreal_action_group(action),
                "item_label": "Action",
                "item_label_key": "action",
                "item": action or "-",
                "previous": "-",
                "new": "-",
                "effect": self._unreal_action_effect(action),
                "time": timestamp,
                "source": "Unreal WebSocket",
            }
        return None

    def _record_unreal_settings_visual_event(self, event: dict[str, str]) -> None:
        self._unreal_settings_events.insert(0, dict(event))
        del self._unreal_settings_events[8:]
        self._render_unreal_settings_visual_event(event)

    def _render_unreal_settings_visual_event(self, event: dict[str, str]) -> None:
        self.unreal_update_title.setText(self._unreal_event_title(event))

        self.unreal_update_details.setText(self._unreal_event_details_html(event))
        self.unreal_update_history_view.setHtml(
            "\n".join(
                self._unreal_event_history_line(item)
                for item in self._unreal_settings_events
            )
        )
        scrollbar = self.unreal_update_history_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.minimum())

    def _unreal_event_title(self, event: dict[str, str]) -> str:
        title_key = event.get("title_key", "")
        if title_key:
            return self._t(title_key)
        return event.get("title", self._t("unreal_update_received"))

    def _unreal_event_item_label(self, event: dict[str, str]) -> str:
        item_label_key = event.get("item_label_key", "")
        if item_label_key:
            return self._t(item_label_key)
        return event.get("item_label", self._t("setting"))

    def _unreal_event_details_html(self, event: dict[str, str]) -> str:
        item_label = self._unreal_event_item_label(event)
        item_color = (
            SETTING_ID_COLOR
            if event.get("type") == "settings_update"
            else SETTING_VALUE_COLOR
        )
        rows = (
            (self._t("type"), event.get("type", "-"), SETTING_META_COLOR),
            (self._t("group"), event.get("group", "-"), SETTING_META_COLOR),
            (item_label, event.get("item", "-"), item_color),
            (
                self._t("previous"),
                event.get("previous", "-"),
                SETTING_PREVIOUS_VALUE_COLOR,
            ),
            (self._t("new"), event.get("new", "-"), SETTING_VALUE_COLOR),
            (self._t("effect"), event.get("effect", "-"), SETTING_META_COLOR),
            (self._t("time"), event.get("time", "-"), SETTING_PREVIOUS_VALUE_COLOR),
            (self._t("source"), event.get("source", "-"), SETTING_PREVIOUS_VALUE_COLOR),
        )
        return "<br>".join(
            self._monitor_detail_row_html(label, value, color)
            for label, value, color in rows
        )

    def _monitor_detail_row_html(self, label: str, value: str, color: str) -> str:
        return (
            '<span style="color:#bca75e;font-weight:800;">'
            f"{html.escape(str(label))}:"
            "</span> "
            f'<span style="color:{color};font-weight:800;">'
            f"{html.escape(str(value))}"
            "</span>"
        )

    def _unreal_event_history_line(self, event: dict[str, str]) -> str:
        item_label = self._unreal_event_item_label(event)
        item = event.get("item", "-")
        if event.get("type") == "settings_update":
            change = (
                f'<span style="color:{SETTING_ID_COLOR};font-weight:800;">'
                f"{html.escape(str(item))}</span> "
                f'<span style="color:{SETTING_MUTED_COLOR};">|</span> '
                f'<span style="color:{SETTING_PREVIOUS_VALUE_COLOR};">'
                f"{html.escape(str(event.get('previous', '-')))}</span> "
                f'<span style="color:{SETTING_MUTED_COLOR};">-></span> '
                f'<span style="color:{SETTING_VALUE_COLOR};font-weight:800;">'
                f"{html.escape(str(event.get('new', '-')))}</span>"
            )
        else:
            change = (
                f'<span style="color:{SETTING_VALUE_COLOR};font-weight:800;">'
                f"{html.escape(str(event.get('effect', '-')))}</span>"
            )
        return (
            '<div style="background:#050505; border:1px solid #2b2618; '
            'border-radius:6px; padding:7px 9px; margin:0 0 6px 0;">'
            f'<span style="color:{SETTING_PREVIOUS_VALUE_COLOR};">'
            f"{html.escape(str(event.get('time', '-')))}</span> "
            f'<span style="color:#bca75e;font-weight:800;">'
            f"{html.escape(str(event.get('type', '-')))}</span> "
            f'<span style="color:{SETTING_MUTED_COLOR};">/</span> '
            f'<span style="color:{SETTING_META_COLOR};">'
            f"{html.escape(str(event.get('group', '-')))}</span><br>"
            f'<span style="color:{SETTING_MUTED_COLOR};">'
            f"{html.escape(str(item_label))}:</span> "
            f"{change}</div>"
        )

    def _muted_panel_text(self, text: str) -> str:
        return (
            f'<span style="color:{SETTING_PREVIOUS_VALUE_COLOR};">'
            f"{html.escape(str(text))}</span>"
        )

    def _unreal_setting_group(self, setting: str) -> str:
        return "Customization" if setting in CUSTOMIZATION_SETTING_IDS else "Settings"

    def _unreal_action_group(self, action: str) -> str:
        if action == "reset_customization_defaults":
            return "Customization"
        return "Settings"

    def _setting_value_from_snapshot(
        self,
        snapshot: dict[str, Any],
        setting: str,
        group: str,
    ) -> Any:
        if group == "Customization":
            customization = snapshot.get("customization", {})
            if isinstance(customization, dict):
                return customization.get(setting)
            return None
        return snapshot.get(setting)

    def _unreal_update_effect(self, setting: str, group: str) -> str:
        if setting == "selected_character":
            return "Stored only. Visual handled by Unreal."
        if setting == "personality_traits":
            return "Prompt personality updated"
        if setting in {"use_custom_personality_prompt", "custom_personality_prompt"}:
            return "Prompt personality updated"
        if setting == "profanity_filter":
            return "Prompt profanity preference updated"
        if setting in {"voice_id", "use_custom_voice", "custom_voice_id"}:
            return "Voice customization updated"
        if setting in {"selected_personality", "voice_style", "character_personality"}:
            return "Customization stored"
        if setting == "tts_realtime":
            return "TTS realtime setting updated"
        if setting in {"input_device", "output_device"}:
            return "Audio device setting updated"
        if setting == CURRENT_LANGUAGE_SETTING_ID:
            return "Python UI language updated"
        if setting in PASSIVE_GRAPHICS_SETTING_IDS:
            return "Stored only. Graphics handled by Unreal."
        if group == "Customization":
            return "Customization updated"
        return "Settings value updated"

    def _unreal_action_effect(self, action: str) -> str:
        if action == "reset_settings_defaults":
            return "Settings defaults restored"
        if action == "reset_customization_defaults":
            return "Customization defaults restored"
        if action == "exit_myralis":
            return "Backend shutdown scheduled"
        if action == "settings_is_open":
            return "Mic level messages enabled"
        if action == "settings_is_closed":
            return "Mic level messages disabled"
        if action == "mic_level_is_showing":
            return "Mic level visibility enabled"
        if action == "mic_level_is_not_showing":
            return "Mic level visibility disabled"
        if action == "mic_level_not_showing":
            return "Mic level visibility disabled"
        return "Action received"

    def _format_unreal_value(self, value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, tuple, set)):
            return ",".join(self._format_unreal_value(item) for item in value)
        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except TypeError:
                return str(value)
        text = str(value).strip()
        return text if text else "(empty)"

    def _handle_result(self, result: AssistantResult) -> None:
        self.chat_panel.add_assistant_message(result.response.text)
        if result.used_cached_text or result.used_cached_audio:
            cache_parts = []
            if result.used_cached_text:
                cache_parts.append("texto")
            if result.used_cached_audio:
                cache_parts.append("audio")
            self.chat_panel.add_system_message(
                "TEST MODE reutilizo " + " y ".join(cache_parts) + "."
            )
        for error in result.errors:
            self.chat_panel.add_system_message(f"Audio: {error}")
        self.chat_panel.set_input_enabled(True)
        self._conversation_active = False

    def _handle_error(self, message: str) -> None:
        self.chat_panel.add_system_message(message)
        self._conversation_active = False
        self.chat_panel.set_input_enabled(True)
        self._set_state(AssistantState.IDLE)

    def _handle_voice_hotkey_pressed(self) -> None:
        if self._conversation_active:
            LOGGER.info("Voice hotkey ignored because a conversation is active")
            return
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            LOGGER.info("Voice hotkey ignored because interaction_mode=%s", self._interaction_mode(settings))
            return
        self.conversation_manager.note_user_interaction()
        self.conversation_manager.emit_external_state(AssistantState.LISTENING)
        if self.current_state != AssistantState.LISTENING:
            self._set_state(AssistantState.LISTENING, sync_voice_state=False)
        if not self._start_voice_hotkey_listening(settings):
            LOGGER.warning("Voice hotkey could not start STT listening")
        self._play_listening_beep()

    def _handle_voice_hotkey_released(self) -> None:
        if self.current_state != AssistantState.LISTENING:
            return
        LOGGER.info("Voice hotkey released")
        self.conversation_manager.emit_external_state(AssistantState.THINKING)
        self._set_state(AssistantState.THINKING)

    def _start_voice_hotkey_listening(self, settings: dict[str, Any]) -> bool:
        if self.deepgram_stt_manager.is_listening():
            return True

        devices = self.settings_manager.get_devices()
        started = self.deepgram_stt_manager.start_listening(
            settings=settings,
            input_device_index=devices.get("input_device_index"),
        )
        if started:
            LOGGER.info("Voice hotkey started STT listening")
        return started

    def _play_listening_beep(self) -> None:
        try:
            ui_volume = float(self.settings_manager.get_setting("ui_volume", 0.5))
        except Exception:
            ui_volume = 0.5
        self.audio_manager.play_ui_beep(ui_volume)

    def _handle_voice_transcript(self, text: str) -> None:
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            return
        self.deepgram_stt_manager.stop_listening()
        self._start_conversation_from_text(text, source="voice")

    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            settings_manager=self.settings_manager,
            audio_manager=self.audio_manager,
            websocket_start_end_callback=self._test_websocket_start_end,
            elevenlabs_streaming_callback=self._test_elevenlabs_streaming,
            wav_response_callback=self._test_wav_response,
            runtime_lip_sync_callback=self._test_runtime_lip_sync,
            parent=self,
        )
        dialog.settings_changed.connect(self._settings_changed)
        dialog.backend_ui_refresh_requested.connect(self._send_backend_ui_snapshot)
        dialog.out_of_credits_test_toggled.connect(
            self._handle_out_of_credits_test_toggled
        )
        if hasattr(dialog, "out_of_credits_test_button"):
            dialog.out_of_credits_test_button.setChecked(
                self._current_out_of_credits_state()
            )
        dialog.exec()

    def _start_websocket_server(self, auto: bool = False) -> None:
        self.conversation_manager.disable_mic_level_messages(source="websocket_start")
        started = start_websocket_server()
        if started:
            if not auto:
                self.chat_panel.add_system_message(
                    "WebSocket iniciando en ws://127.0.0.1:8765"
                )
            self._send_backend_ui_snapshot("connected")
            self._send_out_of_credits_state()
        elif is_websocket_server_active():
            if not auto:
                self.chat_panel.add_system_message("WebSocket ya esta activo")
        else:
            if not auto:
                self.chat_panel.add_system_message("No se pudo iniciar WebSocket; revisa logs.")
            self._send_backend_ui_snapshot("error")

    def _reload_websocket_server(self) -> None:
        self._reconnect_system()
        self.chat_panel.add_system_message(self._t("websocket_reloaded"))

    def _test_websocket_start_end(self) -> None:
        send_audio_start()
        QTimer.singleShot(2000, send_audio_end)

    def _test_elevenlabs_streaming(self) -> None:
        if self._conversation_active or (
            self.websocket_stream_test_thread
            and self.websocket_stream_test_thread.is_alive()
        ):
            return

        self._conversation_active = True
        self.chat_panel.set_input_enabled(False)
        self.chat_panel.add_system_message("Probando ElevenLabs Streaming...")
        settings_snapshot = self.settings_manager.get_settings()
        self.websocket_stream_test_thread = threading.Thread(
            target=self._elevenlabs_streaming_test_worker,
            args=(settings_snapshot,),
            daemon=True,
        )
        self.websocket_stream_test_thread.start()

    def _elevenlabs_streaming_test_worker(self, settings: dict[str, Any]) -> None:
        try:
            audio_path = self.conversation_manager.test_elevenlabs_streaming(
                settings,
                state_callback=lambda state: self.events.put(
                    {"type": "state", "state": state}
                ),
            )
            message = "Prueba ElevenLabs Streaming enviada."
            if audio_path is not None:
                message += f" WAV debug: {audio_path}"
            self.events.put({"type": "system", "message": message})
        except Exception as exc:
            LOGGER.exception("ElevenLabs streaming test failed")
            self.events.put({"type": "error", "message": str(exc)})
        finally:
            self.events.put({"type": "stream_test_done"})

    def _test_wav_response(self) -> None:
        if self._conversation_active or (
            self.websocket_stream_test_thread
            and self.websocket_stream_test_thread.is_alive()
        ):
            return

        self._conversation_active = True
        self.chat_panel.set_input_enabled(False)
        self.chat_panel.add_system_message("Probando WAV Response...")
        self.websocket_stream_test_thread = threading.Thread(
            target=self._wav_response_test_worker,
            daemon=True,
        )
        self.websocket_stream_test_thread.start()

    def _wav_response_test_worker(self) -> None:
        try:
            audio_path = self._find_existing_wav_response()
            if audio_path is None:
                raise FileNotFoundError(
                    "No se encontro un WAV existente en output/runtime o output/audio."
                )
            self.audio_manager.play_audio(audio_path, None)
            self.events.put(
                {"type": "system", "message": f"Reproduciendo WAV existente: {audio_path}"}
            )
        except Exception as exc:
            LOGGER.exception("WAV response test failed")
            self.events.put({"type": "error", "message": str(exc)})
        finally:
            self.events.put({"type": "stream_test_done"})

    def _test_runtime_lip_sync(self) -> None:
        if self._conversation_active or (
            self.websocket_stream_test_thread
            and self.websocket_stream_test_thread.is_alive()
        ):
            return

        self._conversation_active = True
        self.chat_panel.set_input_enabled(False)
        self.chat_panel.add_system_message("Probando Runtime Lip Sync...")
        settings_snapshot = self.settings_manager.get_settings()
        self.websocket_stream_test_thread = threading.Thread(
            target=self._runtime_lip_sync_test_worker,
            args=(settings_snapshot,),
            daemon=True,
        )
        self.websocket_stream_test_thread.start()

    def _find_existing_wav_response(self) -> Path | None:
        runtime_wav = self.runtime_bridge.config.response_audio_path
        if runtime_wav.exists():
            return runtime_wav

        test_mode_wav = self.settings_manager.audio_output_dir / "test_mode_response.wav"
        if test_mode_wav.exists():
            return test_mode_wav

        wav_candidates = sorted(
            self.settings_manager.audio_output_dir.glob("*.wav"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if wav_candidates:
            return wav_candidates[0]
        return None

    def _runtime_lip_sync_test_worker(self, settings: dict[str, Any]) -> None:
        try:
            audio_path = self.conversation_manager.test_runtime_lip_sync(
                settings,
                state_callback=lambda state: self.events.put(
                    {"type": "state", "state": state}
                ),
            )
            message = "Prueba Runtime Lip Sync enviada."
            if audio_path is not None:
                message += f" WAV debug: {audio_path}"
            self.events.put({"type": "system", "message": message})
        except Exception:
            LOGGER.exception("Runtime lip sync test failed")
            self.events.put(
                {"type": "error", "message": "Runtime Lip Sync test failed"}
            )
        finally:
            self.events.put({"type": "stream_test_done"})

    def _settings_changed(self) -> None:
        interval = int(
            self.settings_manager.get_setting("app.state_poll_interval_ms", 100)
        )
        self.event_timer.setInterval(interval)
        self._configure_voice_hotkey()
        self._start_input_level_monitor()
        self._sync_deepgram_stt_for_state(self.current_state)
        self._refresh_conversation_mode_indicator()

    def _can_open_technical_panel(self) -> bool:
        context = getattr(self, "_authorization_context", None)
        if context is None:
            return False
        return bool(getattr(context, "can_open_technical_panel", False))

    def _lock_down_debug_state(self) -> None:
        self._debug_state = dict(DEFAULT_DEBUG_STATE)
        self._unreal_debug_mode = False
        self._show_mic_level_outbound_events = False
        self._force_send_mic_level = False
        self._refresh_debug_mode_controls()
        self._refresh_unreal_debug_mode_controls()
        self._refresh_mic_level_outbound_toggle()
        self._refresh_mic_level_force_toggle()
        if self.debug_hotkey_filter is not None and not self._can_open_technical_panel():
            self.debug_hotkey_filter.unregister()
            self.debug_hotkey_filter = None
        if self.debug_hotkey_shortcut is not None and not self._can_open_technical_panel():
            self.debug_hotkey_shortcut.setEnabled(False)
        if self.debug_hotkey_poll_timer is not None and not self._can_open_technical_panel():
            self.debug_hotkey_poll_timer.stop()

    def update_authorization_context(
        self,
        context: BackendAuthorizationContext | None,
    ) -> None:
        self._authorization_context = context or BackendAuthorizationContext()
        if not self._can_open_technical_panel() and self.isVisible():
            self._lock_down_debug_state()
            self.hide()
        elif self._can_open_technical_panel():
            self._configure_debug_hotkey()

    def _is_debug_mode_enabled(self) -> bool:
        return not self.conversation_manager.is_ai_realtime_processing_enabled()

    def _set_debug_mode_enabled(self, enabled: bool) -> None:
        clean_enabled = bool(enabled)
        if self._is_debug_mode_enabled() == clean_enabled:
            self._refresh_debug_mode_controls()
            return
        self.conversation_manager.set_ai_realtime_processing_enabled(
            not clean_enabled,
            source="debug_ui",
        )

    def _handle_debug_mode_toggle_clicked(self, checked: bool) -> None:
        if not self._can_open_technical_panel():
            self._lock_down_debug_state()
            return
        self._set_debug_mode_enabled(bool(checked))

    def _reset_debug_mode_to_normal(self) -> None:
        if self._is_debug_mode_enabled():
            self.conversation_manager.set_ai_realtime_processing_enabled(
                True,
                source="debug_ui_session_reset",
            )
            return
        self._refresh_debug_mode_controls()

    def _handle_unreal_debug_mode_toggle_clicked(self, checked: bool) -> None:
        if not self._can_open_technical_panel():
            self._unreal_debug_mode = False
            self._refresh_unreal_debug_mode_controls()
            return
        self._unreal_debug_mode = bool(checked)
        self._refresh_unreal_debug_mode_controls()
        self._send_backend_ui_snapshot()

    def _refresh_debug_mode_controls(self) -> None:
        if not hasattr(self, "debug_mode_label"):
            return
        debug_enabled = self._is_debug_mode_enabled()
        self.debug_mode_label.setText(
            self._t("debug_mode_on") if debug_enabled else self._t("debug_mode_off")
        )
        self.debug_mode_toggle_button.setText(
            self._t("debug_mode_disable")
            if debug_enabled
            else self._t("debug_mode_enable")
        )
        self.debug_mode_toggle_button.setChecked(debug_enabled)
        if debug_enabled:
            dot_color = "#ef4444"
            border_color = "#fca5a5"
        else:
            dot_color = "#22c55e"
            border_color = "#86efac"
        self.debug_mode_dot.setStyleSheet(
            "background: "
            f"{dot_color}; border-radius: 6px; border: 1px solid {border_color};"
        )

    def _refresh_unreal_debug_mode_controls(self) -> None:
        if not hasattr(self, "unreal_debug_mode_toggle_button"):
            return
        self.unreal_debug_mode_toggle_button.setChecked(
            bool(getattr(self, "_unreal_debug_mode", False))
        )

    def _handle_ai_realtime_processing_changed(self, enabled: bool) -> None:
        self._refresh_debug_mode_controls()
        self._refresh_debug_panel()
        if enabled:
            self._sync_deepgram_stt_for_state(self.current_state)
            return

        was_listening = self.deepgram_stt_manager.is_listening()
        self.deepgram_stt_manager.stop_listening()
        if was_listening or self.current_state == AssistantState.LISTENING:
            self._start_input_level_monitor()

    def _configure_voice_hotkey(self) -> None:
        hotkey = str(self.settings_manager.get_setting("app.hotkey", "F8")).strip()
        self._unregister_voice_hotkey()
        self.voice_hotkey_shortcut = None
        if sys.platform == "win32":
            voice_hook = WindowsGlobalHotkeyHook(
                self._handle_voice_hotkey_pressed,
                self._handle_voice_hotkey_released,
                hotkey_text=hotkey or "F8",
            )
            if voice_hook.register():
                self.voice_hotkey_hook = voice_hook
                self.voice_hotkey_filter = None
                return
        LOGGER.warning("Voice hotkey not registered globally; no local fallback will be used")

    def _unregister_voice_hotkey(self) -> None:
        if self.voice_hotkey_filter is not None:
            self.voice_hotkey_filter.unregister()
            self.voice_hotkey_filter = None
        if self.voice_hotkey_hook is not None:
            self.voice_hotkey_hook.unregister()
            self.voice_hotkey_hook = None
        self.voice_hotkey_shortcut = None

    def _configure_debug_hotkey(self) -> None:
        if not self._can_open_technical_panel():
            self.debug_hotkey_shortcut = None
            if self.debug_hotkey_filter is not None:
                self.debug_hotkey_filter.unregister()
                self.debug_hotkey_filter = None
            if self.debug_hotkey_poll_timer is not None:
                self.debug_hotkey_poll_timer.stop()
                self.debug_hotkey_poll_timer = None
            return
        self.debug_hotkey_shortcut = self._configure_debug_shortcut(
            self.debug_hotkey_shortcut,
            "Ctrl+Shift+D",
        )
        if self.debug_hotkey_filter is None:
            self.debug_hotkey_filter = WindowsDebugHotkeyFilter(
                self._handle_debug_hotkey_activated,
                int(self.winId()),
            )
            self.debug_hotkey_filter.register()
        if sys.platform == "win32" and self.debug_hotkey_poll_timer is None:
            self.debug_hotkey_poll_timer = QTimer(self)
            self.debug_hotkey_poll_timer.timeout.connect(self._poll_debug_hotkey_state)
            self.debug_hotkey_poll_timer.start(60)

    def _configure_debug_shortcut(
        self,
        shortcut: QShortcut | None,
        sequence_text: str,
    ) -> QShortcut:
        sequence = QKeySequence(sequence_text)
        if shortcut is None:
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self._handle_debug_hotkey_activated)
        else:
            shortcut.setKey(sequence)
        shortcut.setEnabled(not sequence.isEmpty())
        return shortcut

    def _handle_debug_hotkey_activated(self) -> None:
        if not self._can_open_technical_panel():
            LOGGER.info("Intento de acceso al panel técnico bloqueado")
            return
        now = time.monotonic()
        if now - self._last_debug_hotkey_toggle_time < 0.35:
            return
        self._last_debug_hotkey_toggle_time = now
        self.toggle_debug_ui()

    def _poll_debug_hotkey_state(self) -> None:
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            ctrl_down = bool(
                user32.GetAsyncKeyState(WindowsDebugHotkeyFilter.VK_CONTROL) & 0x8000
            )
            shift_down = bool(
                user32.GetAsyncKeyState(WindowsDebugHotkeyFilter.VK_SHIFT) & 0x8000
            )
            d_down = bool(
                user32.GetAsyncKeyState(WindowsDebugHotkeyFilter.VK_D) & 0x8000
            )
        except Exception:
            LOGGER.debug("Could not poll debug hotkey state", exc_info=True)
            return

        is_down = ctrl_down and shift_down and d_down
        if is_down and not self._debug_hotkey_poll_was_down:
            LOGGER.info("Debug hotkey poll detected: Ctrl+Shift+D")
            self._handle_debug_hotkey_activated()
        self._debug_hotkey_poll_was_down = is_down

    def toggle_debug_ui(self) -> None:
        if not self._can_open_technical_panel():
            LOGGER.info("Intento de acceso al panel técnico bloqueado")
            return
        if self.isVisible() and not self.isMinimized():
            self._reset_debug_mode_to_normal()
            self.hide()
            LOGGER.info("Debug UI hidden")
            return
        self._reset_debug_mode_to_normal()
        self._refresh_debug_panel()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                LOGGER.debug("Could not force foreground window", exc_info=True)
        LOGGER.info("Debug UI shown")

    def show_debug_ui(self) -> None:
        if not self._can_open_technical_panel():
            LOGGER.info("Intento de acceso al panel técnico bloqueado")
            return
        self._reset_debug_mode_to_normal()
        self._refresh_debug_panel()
        if self.isVisible() and not self.isMinimized():
            self.raise_()
            self.activateWindow()
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                LOGGER.debug("Could not force foreground window", exc_info=True)
        LOGGER.info("Debug UI shown")

    def hide_debug_ui(self) -> None:
        if self.isVisible():
            self._reset_debug_mode_to_normal()
            self.hide()
            LOGGER.info("Debug UI hidden")

    def _refresh_debug_panel(self) -> None:
        if not hasattr(self, "_debug_value_labels") or not self._can_open_technical_panel():
            return

        settings = self.settings_manager.get_settings()
        runtime_state = self.runtime_bridge.get_runtime_state()
        profile = self._usage_profile(settings)
        _tokens_available, _minutes_available, cost_per_interaction, budget, usage_percent = self._usage_estimates(
            settings,
            profile,
        )

        websocket_status = "connected" if has_websocket_client() else "disconnected"
        if is_websocket_server_active() and not has_websocket_client():
            websocket_status = "listening"
        self._set_debug_value("websocket", websocket_status)
        self._set_debug_value(
            "runtime",
            f"{runtime_state.get('state', self.current_state.value)} / "
            f"{runtime_state.get('mood', '-')}",
        )
        self._set_debug_value(
            "interaction",
            f"{self._interaction_mode(settings)} / stt={settings.get('stt_engine', '-')}",
        )
        elevenlabs_settings = settings.get("elevenlabs", {})
        realtime_tts = (
            bool(elevenlabs_settings.get("use_realtime_tts_streaming", True))
            if isinstance(elevenlabs_settings, dict)
            else bool(settings.get("tts_realtime", True))
        )
        self._set_debug_value(
            "tts",
            "realtime" if realtime_tts else "wav",
        )
        self._set_debug_value("devices", self._debug_audio_devices_summary())
        self._set_debug_value(
            "usage",
            f"{profile}: {usage_percent:.1f}% "
            f"(budget={budget:.2f}, estimated_cost={cost_per_interaction:.4f})",
        )
        self._refresh_debug_log_view()

    def _set_debug_value(self, key: str, value: str) -> None:
        label = self._debug_value_labels.get(key)
        if label is not None:
            label.setText(value)

    def _debug_audio_devices_summary(self) -> str:
        devices = self.settings_manager.get_devices()
        input_id = self.settings_manager.get_setting("input_device", "default")
        output_id = self.settings_manager.get_setting("output_device", "default")
        input_name = str(devices.get("input_device_name", "") or input_id)
        output_name = str(devices.get("output_device_name", "") or output_id)
        return f"in={input_name} / out={output_name}"

    def _refresh_debug_log_view(self) -> None:
        log_path = self.settings_manager.logs_output_dir / "app.log"
        if not log_path.exists():
            self.debug_log_view.setPlainText("No log file yet.")
            return
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            self.debug_log_view.setPlainText(f"Could not read logs: {exc}")
            return
        self.debug_log_view.setPlainText("\n".join(lines[-160:]))
        scrollbar = self.debug_log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _start_input_level_monitor(self) -> None:
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            self.audio_manager.stop_input_level_monitor()
            self._input_level_monitor_config = None
            self.mic_level_meter.setValue(0)
            return
        devices = self.settings_manager.get_devices()
        audio_settings = settings.get("audio", {})
        input_volume = float(
            settings.get(
                "input_volume",
                audio_settings.get("input_volume", 1.0)
                if isinstance(audio_settings, dict)
                else 1.0,
            )
        )
        monitor_config = (
            devices.get("input_device_index"),
            int(settings["audio"]["sample_rate"]),
            max(0.0, min(1.0, input_volume)),
        )
        if monitor_config == self._input_level_monitor_config:
            return

        started = self.audio_manager.start_input_level_monitor(
            input_device_index=monitor_config[0],
            sample_rate=monitor_config[1],
            input_volume=monitor_config[2],
        )
        if started:
            self._input_level_monitor_config = monitor_config
        else:
            self._input_level_monitor_config = None
            self.mic_level_meter.setValue(0)

    def _update_audio_level_meters(self) -> None:
        mic_level = self._current_mic_level()
        output_level = self.audio_manager.get_output_level()
        self.mic_level_meter.setValue(max(0, min(100, int(mic_level * 100))))
        self.output_level_meter.setValue(max(0, min(100, int(output_level * 100))))
        self._send_mic_level_if_active(mic_level)

    def _current_mic_level(self) -> float:
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            return 0.0
        if self.deepgram_stt_manager.is_listening():
            return max(0.0, min(1.0, self.deepgram_stt_manager.get_input_level()))
        return max(0.0, min(1.0, self.audio_manager.get_input_level()))

    def _send_mic_level_if_active(self, mic_level: float) -> None:
        force_send = bool(getattr(self, "_force_send_mic_level", False))
        if not force_send and not self.conversation_manager.should_send_mic_level():
            return
        if not has_websocket_client():
            return
        if not force_send:
            if not getattr(self, "_settings_is_open", False) or not getattr(self, "_mic_level_show", False):
                return
            settings = self.settings_manager.get_settings()
            if self._interaction_mode(settings) != "voice":
                return
            capture_active = (
                self.deepgram_stt_manager.is_listening()
                or self.audio_manager.is_input_level_monitor_active()
            )
            if not capture_active:
                return
        now = time.time()
        if not force_send and now - self._last_mic_level_send_time < MIC_LEVEL_SEND_INTERVAL_SECONDS:
            return
        self._last_mic_level_send_time = now
        level = max(0.0, min(1.0, float(mic_level)))
        send_json_to_unreal_threadsafe(
            {
                "type": "mic_level",
                "show": bool(getattr(self, "_mic_level_show", False)),
                "level": level,
            }
        )

    def _build_level_meter(self, object_name: str, tooltip: str) -> QProgressBar:
        meter = QProgressBar()
        meter.setObjectName(object_name)
        meter.setRange(0, 100)
        meter.setValue(0)
        meter.setTextVisible(False)
        meter.setFixedSize(150, 8)
        meter.setToolTip(tooltip)
        return meter

    def _build_level_meter_row(
        self,
        label_text: str,
        meter: QProgressBar,
    ) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        label = QLabel(label_text)
        label.setObjectName("meterLabel")
        label.setFixedWidth(32)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(label)
        layout.addWidget(meter)
        return layout

    def _set_state(self, state: AssistantState, *, sync_voice_state: bool = True) -> None:
        self.current_state = state
        self.conversation_manager.sync_state(state)
        self.status_label.setText(state.value)
        try:
            self.runtime_bridge.set_state(state.value)
        except Exception:
            LOGGER.exception("Could not write runtime state %s", state.value)
        color = self.STATE_COLORS[state]
        self.status_dot.setStyleSheet(
            f"background: {color}; border-radius: 6px; border: 1px solid {color};"
        )
        if sync_voice_state:
            self._sync_deepgram_stt_for_state(state)

    def _sync_deepgram_stt_for_state(self, state: AssistantState) -> None:
        try:
            settings = self.settings_manager.get_settings()
            if self._interaction_mode(settings) != "voice":
                if self.deepgram_stt_manager.is_listening():
                    self.deepgram_stt_manager.stop_listening()
                self.audio_manager.stop_input_level_monitor()
                self._input_level_monitor_config = None
                self.mic_level_meter.setValue(0)
                if state == AssistantState.LISTENING:
                    LOGGER.info("Listening emotion analysis skipped: text mode")
                return
            if state == AssistantState.LISTENING:
                if self._conversation_active:
                    if self.deepgram_stt_manager.is_listening():
                        self.deepgram_stt_manager.stop_listening()
                    self._start_input_level_monitor()
                    return
                if not self.conversation_manager.is_ai_realtime_processing_enabled():
                    LOGGER.info(
                        "AI realtime processing disabled; STT auto-start skipped"
                    )
                    was_listening = self.deepgram_stt_manager.is_listening()
                    self.deepgram_stt_manager.stop_listening()
                    if was_listening:
                        LOGGER.info("STT stopped because AI realtime is disabled")
                    self._start_input_level_monitor()
                    return

                devices = self.settings_manager.get_devices()
                if not self._deepgram_stt_enabled(settings):
                    LOGGER.info("STT skipped for current voice settings")
                    self._start_input_level_monitor()
                    return

                self.audio_manager.stop_input_level_monitor()
                self._input_level_monitor_config = None
                self.mic_level_meter.setValue(0)
                started = self.deepgram_stt_manager.start_listening(
                    settings=settings,
                    input_device_index=devices.get("input_device_index"),
                )
                if not started:
                    self._start_input_level_monitor()
            else:
                was_listening = self.deepgram_stt_manager.is_listening()
                self.deepgram_stt_manager.stop_listening()
                if was_listening:
                    self._start_input_level_monitor()
        except Exception:
            LOGGER.exception("Could not sync STT for state=%s", state.value)

    def _deepgram_stt_enabled(self, settings: dict[str, Any]) -> bool:
        if self._interaction_mode(settings) != "voice":
            return False
        engine = str(settings.get("stt_engine", "deepgram")).strip()
        if engine == "local":
            return True
        if engine != "deepgram":
            return False
        deepgram_settings = settings.get("deepgram", {})
        if isinstance(deepgram_settings, dict):
            return bool(deepgram_settings.get("enabled", False))
        return bool(settings.get("deepgram_enabled", False))

    def _interaction_mode(self, settings: dict[str, Any]) -> str:
        app_settings = settings.get("app", {})
        value = settings.get(
            "interaction_mode",
            app_settings.get("interaction_mode", "voice")
            if isinstance(app_settings, dict)
            else "voice",
        )
        clean = str(value).strip()
        return clean if clean in {"voice", "text"} else "voice"

    def _handle_backend_ui_action(self, action: str) -> None:
        if action == "show_python_ui":
            self.show_debug_ui()
        elif action == "hide_python_ui":
            self.hide_debug_ui()
        elif action == "settings_changed":
            self._settings_changed()
            self._send_audio_devices()
            self._send_backend_ui_snapshot()
        elif action == "reconnect_system":
            self._reconnect_system()
        elif action == "exit_myralis":
            self._schedule_exit_myralis_shutdown()

    def _schedule_exit_myralis_shutdown(self) -> None:
        if self._shutdown_started or self._exit_myralis_requested:
            return
        self._exit_myralis_requested = True
        LOGGER.info("exit_myralis received; shutting down in 10 seconds")
        QTimer.singleShot(10_000, self._complete_exit_myralis_shutdown)

    def _complete_exit_myralis_shutdown(self) -> None:
        app = QCoreApplication.instance()
        if app is None:
            LOGGER.warning("exit_myralis shutdown requested without QCoreApplication")
            return
        app.quit()

    def _handle_websocket_connection_status(self, connected: bool) -> None:
        self.events.put({"type": "websocket_connection", "connected": connected})

    def _handle_websocket_connection_event(self, connected: bool) -> None:
        self._websocket_connected = connected
        self.conversation_manager.disable_mic_level_messages(
            source="websocket_connected" if connected else "websocket_disconnected"
        )
        if not connected:
            self._settings_is_open = False
        if connected:
            self._send_backend_ui_snapshot("connected")
            self._send_audio_devices()
            self._send_out_of_credits_state()
        else:
            self._send_backend_ui_snapshot("disconnected")

    def _reconnect_system(self) -> None:
        self._send_backend_ui_snapshot("reconnecting")
        stop_websocket_server()
        started = start_websocket_server()
        if started or is_websocket_server_active():
            self._send_backend_ui_snapshot("connected")
        else:
            self._send_backend_ui_snapshot("error")

    def _send_audio_devices(self) -> None:
        if not has_websocket_client():
            return
        try:
            payload = self.audio_manager.build_audio_devices_payload()
        except Exception:
            LOGGER.exception("Could not build audio_devices payload")
            return
        send_json_to_unreal_threadsafe(payload)

    def _send_backend_ui_snapshot(self, status: str | None = None) -> None:
        payload = self._build_backend_ui_payload(status)
        send_json_to_unreal_threadsafe(payload)

    def _build_backend_ui_payload(self, status: str | None = None) -> dict[str, Any]:
        settings = self.settings_manager.get_settings()
        estimator = getattr(self, "usage_estimator", None)
        if estimator is None:
            root = getattr(self.settings_manager, "root", None)
            estimator = UsageEstimator(root)
        usage = estimator.build_snapshot(settings)
        clean_status = status or ("connected" if has_websocket_client() else "disconnected")
        if clean_status not in BACKEND_UI_STATUSES:
            clean_status = "error"
        LOGGER.info(
            "backend_ui usage estimates: status=%s profile=%s budget=%.4f "
            "estimated_cost_per_interaction=%.4f tokens_available=%s usage_percent=%.1f "
            "source=%s confidence=%s",
            clean_status,
            usage.usage_profile,
            usage.budget_remaining_usd_estimate,
            usage.estimated_cost_per_interaction_usd,
            usage.miralys_tokens_remaining,
            usage.usage_percent,
            usage.usage_budget_source,
            usage.usage_confidence,
        )
        payload = {
            "type": "backend_ui",
            "system_connection_status": clean_status,
            "debug_mode": bool(getattr(self, "_unreal_debug_mode", False)),
        }
        payload.update(usage.to_backend_payload_fields())
        return payload

    def _usage_profile(self, settings: dict[str, Any]) -> str:
        return usage_profile(settings)

    def _usage_estimates(
        self,
        settings: dict[str, Any],
        profile: str,
    ) -> tuple[int, float, float, float, float]:
        _ = profile
        estimator = getattr(self, "usage_estimator", None)
        if estimator is None:
            root = getattr(self.settings_manager, "root", None)
            estimator = UsageEstimator(root)
        snapshot = estimator.build_snapshot(settings)
        return (
            snapshot.miralys_tokens_available_raw or snapshot.miralys_tokens_remaining,
            snapshot.hours_estimate * 60.0,
            snapshot.estimated_cost_per_interaction_usd,
            snapshot.budget_remaining_usd_estimate,
            snapshot.usage_percent,
        )

    def _estimated_complete_interaction_cost(
        self,
        settings: dict[str, Any],
        profile: str,
    ) -> float:
        return estimated_complete_interaction_cost(settings, profile)

    def _usage_budget_remaining(self) -> float:
        return usage_budget_remaining_from_env()

    def _float_from_env(self, env_key: str, default: float) -> float:
        return float_from_env(env_key, default)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #030303;
            }
            QWidget {
                background: transparent;
                color: #f4f1ea;
                font-family: "__UI_FONT_FAMILY__";
            }
            QWidget#rootWindow {
                background: #030303;
            }
            QFrame#debugShell {
                background: #000000;
                border: 1px solid #2f291a;
                border-radius: 10px;
            }
            QFrame#customTitleBar {
                background: #020202;
                border: 1px solid #201c12;
                border-radius: 10px;
            }
            QLabel#brandLogo {
                background: #000000;
                border: none;
            }
            QWidget#titleStack {
                background: transparent;
            }
            QFrame#signalStrip {
                background: #070706;
                border: 1px solid #312917;
                border-radius: 10px;
            }
            QTabWidget#debugTabs::pane {
                background: #030303;
                border: 1px solid #2b2618;
                border-radius: 10px;
                top: -1px;
                padding: 12px;
            }
            QTabBar#debugTabBar::tab {
                background: #080806;
                color: #9f9a8d;
                border: 1px solid #2b2618;
                border-bottom: none;
                border-top-left-radius: 9px;
                border-top-right-radius: 9px;
                min-width: 150px;
                padding: 11px 20px;
                font-size: 11px;
                font-weight: 800;
            }
            QTabBar#debugTabBar::tab:selected {
                background: #11100c;
                color: #f0c86a;
                border-color: #bca75e;
            }
            QTabBar#debugTabBar::tab:hover {
                color: #f4f1ea;
                border-color: #655528;
            }
            QFrame#debugPanel,
            QFrame#unrealUpdatePanel,
            QFrame#unrealOutboundPanel,
            QFrame#conversationModePanel {
                background: #090907;
                border: 1px solid #3a311c;
                border-radius: 10px;
            }
            QFrame#conversationModePanel {
                border-color: #4b3e1e;
            }
            QFrame#unrealUpdatePanel {
                border-color: #bca75e;
            }
            QFrame#unrealOutboundPanel {
                border-color: #2d6a66;
            }
            QLabel#appTitle {
                color: #f4f1ea;
                font-size: 18px;
                font-weight: 800;
                letter-spacing: 0px;
            }
            QLabel#appSubtitle {
                color: #bca75e;
                font-size: 11px;
                font-weight: 300;
            }
            QLabel#sectionTitle {
                color: #bca75e;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 0px;
            }
            QLabel#unrealUpdateTitle {
                color: #f0c86a;
                font-size: 22px;
                font-weight: 900;
                letter-spacing: 0px;
            }
            QLabel#unrealUpdateDetails {
                color: #f4f1ea;
                font-size: 14px;
                line-height: 150%;
            }
            QLabel#unrealUpdateHistoryTitle {
                color: #bca75e;
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0px;
            }
            QLabel#unrealOutboundTitle {
                color: #8bded4;
                font-size: 16px;
                font-weight: 900;
                letter-spacing: 0px;
            }
            QLabel#unrealOutboundSubtitle {
                color: #9f9a8d;
                font-size: 11px;
                font-weight: 300;
            }
            QLabel#unrealOutboundCount {
                color: #f4f1ea;
                background: #050505;
                border: 1px solid #2d6a66;
                border-radius: 6px;
                padding: 5px 8px;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#conversationModeTitle {
                color: #bca75e;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 0px;
            }
            QLabel#conversationModeLabel {
                color: #f4f1ea;
                font-size: 12px;
                font-weight: 900;
                min-width: 92px;
            }
            QLabel#debugPanelTitle {
                color: #bca75e;
                font-size: 13px;
                font-weight: 800;
                letter-spacing: 0px;
            }
            QLabel#debugFieldLabel {
                color: #a9a59a;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#debugFieldValue {
                color: #f4f1ea;
                font-size: 12px;
            }
            QPlainTextEdit#debugLogView {
                background: #030303;
                color: #d9d5cc;
                border: 1px solid #2b2618;
                border-radius: 6px;
                padding: 8px;
                selection-background-color: #c9a24d;
                selection-color: #000000;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 11px;
            }
            QPlainTextEdit#debugLogView QAbstractScrollArea,
            QPlainTextEdit#debugLogView QWidget {
                background: #050505;
            }
            QTextEdit#unrealUpdateHistoryView {
                background: #030303;
                color: #d9d5cc;
                border: 1px solid #3b321c;
                border-radius: 6px;
                padding: 8px;
                selection-background-color: #c9a24d;
                selection-color: #000000;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 11px;
            }
            QTextEdit#unrealUpdateHistoryView QAbstractScrollArea,
            QTextEdit#unrealUpdateHistoryView QWidget {
                background: #030303;
            }
            QTextEdit#unrealOutboundView {
                background: #030303;
                color: #d9d5cc;
                border: 1px solid #1f5652;
                border-radius: 6px;
                padding: 7px;
                selection-background-color: #8bded4;
                selection-color: #000000;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 11px;
            }
            QTextEdit#unrealOutboundView QAbstractScrollArea,
            QTextEdit#unrealOutboundView QWidget {
                background: #030303;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 13px;
                margin: 8px 4px 8px 4px;
            }
            QScrollBar::handle:vertical {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #5d4d25,
                    stop: 1 #c9a24d
                );
                min-height: 34px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #7a632f,
                    stop: 1 #f0c86a
                );
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
                background: none;
                border: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QLabel#statusLabel {
                color: #f4f1ea;
                font-size: 12px;
                font-weight: 700;
                min-width: 78px;
            }
            QWidget#debugModePanel {
                background: transparent;
                border: none;
            }
            QWidget#controlDeckPanel {
                background: transparent;
                border: none;
                padding-top: 2px;
            }
            QLabel#debugModeLabel {
                color: #f4f1ea;
                font-size: 12px;
                font-weight: 800;
                min-width: 98px;
            }
            QPushButton#settingsButton,
            QPushButton#websocketButton,
            QPushButton#debugModeToggleButton,
            QPushButton#unrealDebugModeToggleButton,
            QPushButton#micLevelOutboundToggleButton,
            QPushButton#micLevelForceToggleButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #13110b,
                    stop: 1 #090806
                );
                color: #f4f1ea;
                border: 1px solid #5d4d25;
                border-radius: 8px;
                padding: 8px 14px;
                font-size: 11px;
                font-weight: 800;
                min-width: 148px;
            }
            QPushButton#settingsButton:hover,
            QPushButton#websocketButton:hover,
            QPushButton#debugModeToggleButton:hover,
            QPushButton#unrealDebugModeToggleButton:hover,
            QPushButton#micLevelOutboundToggleButton:hover,
            QPushButton#micLevelForceToggleButton:hover {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1b170d,
                    stop: 1 #151108
                );
                border-color: #bca75e;
            }
            QPushButton#debugModeToggleButton:checked {
                background: #230909;
                color: #ffe4e6;
                border-color: #ef4444;
            }
            QPushButton#unrealDebugModeToggleButton:checked {
                background: #071f1f;
                color: #ccfbf1;
                border-color: #14b8a6;
            }
            QPushButton#micLevelOutboundToggleButton:checked {
                background: #171027;
                color: #ede9fe;
                border-color: #a78bfa;
            }
            QPushButton#micLevelForceToggleButton:checked {
                background: #0f1f1d;
                color: #ccfbf1;
                border-color: #14b8a6;
            }
            QPushButton#windowMinimizeButton,
            QPushButton#windowCloseButton {
                background: #050505;
                color: #cfc8b7;
                border: 1px solid #312917;
                border-radius: 6px;
                padding: 0px;
                margin: 0px;
                font-size: 17px;
                font-weight: 300;
                text-align: center;
            }
            QPushButton#windowMinimizeButton:hover {
                color: #f0c86a;
                border-color: #bca75e;
                background: #10100c;
            }
            QPushButton#windowCloseButton:hover {
                color: #f4f1ea;
                border-color: #8bded4;
                background: #151108;
            }
            QWidget#metersPanel {
                background: transparent;
                border: none;
            }
            QLabel#meterLabel {
                color: #bca75e;
                font-size: 10px;
                font-weight: 700;
            }
            QProgressBar#micLevelMeter,
            QProgressBar#outputLevelMeter {
                background: #020202;
                border: 1px solid #3b321c;
                border-radius: 4px;
            }
            QProgressBar#micLevelMeter::chunk {
                background: #8bded4;
                border-radius: 3px;
            }
            QProgressBar#outputLevelMeter::chunk {
                background: #f0c86a;
                border-radius: 3px;
            }
            """.replace("__UI_FONT_FAMILY__", self._ui_font_family)
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._shutdown_started:
            event.ignore()
            self._lock_down_debug_state()
            self.hide()
            return
        super().closeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)

    def shutdown_backend(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._lock_down_debug_state()
        remove_websocket_connection_status_handler(
            self._handle_websocket_connection_status
        )
        remove_websocket_outgoing_message_handler(
            self._handle_websocket_outgoing_message
        )
        self._unregister_voice_hotkey()
        if self.debug_hotkey_filter is not None:
            self.debug_hotkey_filter.unregister()
            self.debug_hotkey_filter = None
        if self.debug_hotkey_shortcut is not None:
            self.debug_hotkey_shortcut.setEnabled(False)
        if self.debug_hotkey_poll_timer is not None:
            self.debug_hotkey_poll_timer.stop()
        stop_websocket_server()
        self.deepgram_stt_manager.shutdown()
        self.conversation_manager.shutdown()
        self.audio_manager.stop_input_level_monitor()
