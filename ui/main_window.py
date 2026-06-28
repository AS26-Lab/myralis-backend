from __future__ import annotations

import ctypes
import html
import json
import logging
import os
import queue
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QPixmap, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.audio_manager import AudioManager
from core.conversation_manager import AssistantResult, AssistantState, ConversationManager
from core.deepgram_stt_manager import DeepgramSTTManager
from core.language import CURRENT_LANGUAGE_SETTING_ID, normalize_current_language
from core.runtime_bridge import RuntimeBridge
from core.settings_manager import (
    CUSTOMIZATION_SETTING_IDS,
    PASSIVE_GRAPHICS_SETTING_IDS,
    SettingsManager,
)
from core.websocket_server import (
    add_websocket_connection_status_handler,
    has_websocket_client,
    is_websocket_server_active,
    remove_websocket_connection_status_handler,
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
from ui.window_chrome import apply_native_dark_title_bar


LOGGER = logging.getLogger(__name__)
MIC_LEVEL_SEND_INTERVAL_SECONDS = 0.05
BACKEND_UI_STATUSES = {"connected", "disconnected", "reconnecting", "error"}
USAGE_PROFILES = {"low_usage", "balanced", "high_quality"}
DEFAULT_USAGE_COST_BY_PROFILE_USD: dict[str, float] = {
    "low_usage": 0.015,
    "balanced": 0.050,
    "high_quality": 0.150,
}
DEFAULT_USAGE_STT_COST_USD = 0.004
DEFAULT_USAGE_LISTENING_EMOTION_COST_USD = 0.002
DEFAULT_USAGE_CONSERVATIVE_FACTOR = 0.80
DEFAULT_USAGE_AVERAGE_CONVERSATIONS_PER_HOUR = 120.0


class WindowsDebugHotkeyFilter(QAbstractNativeEventFilter):
    WM_HOTKEY = 0x0312
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10
    VK_D = 0x44
    HOTKEY_ID = 0x4450

    def __init__(self, callback, window_id: int | None = None) -> None:
        super().__init__()
        self._callback = callback
        self._window_id = window_id
        self._registered = False

    def register(self) -> bool:
        if sys.platform != "win32":
            return False
        modifiers = self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT
        hwnd = self._window_id if self._window_id else None
        try:
            ok = bool(
                ctypes.windll.user32.RegisterHotKey(
                    hwnd,
                    self.HOTKEY_ID,
                    modifiers,
                    self.VK_D,
                )
            )
        except Exception:
            LOGGER.exception("Could not register global debug hotkey")
            return False
        if not ok:
            LOGGER.warning("Global debug hotkey Ctrl+Shift+D is already registered")
            return False
        QCoreApplication.instance().installNativeEventFilter(self)
        self._registered = True
        LOGGER.info("Global debug hotkey registered: Ctrl+Shift+D hwnd=%s", hwnd)
        return True

    def unregister(self) -> None:
        if not self._registered or sys.platform != "win32":
            return
        try:
            hwnd = self._window_id if self._window_id else None
            ctypes.windll.user32.UnregisterHotKey(hwnd, self.HOTKEY_ID)
            QCoreApplication.instance().removeNativeEventFilter(self)
        except Exception:
            LOGGER.exception("Could not unregister global debug hotkey")
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
        if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
            LOGGER.info("Global debug hotkey pressed: Ctrl+Shift+D")
            QTimer.singleShot(0, self._callback)
            return True, 0
        return False, 0


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
        deepgram_stt_manager: DeepgramSTTManager,
    ) -> None:
        super().__init__()
        self.settings_manager = settings_manager
        self.audio_manager = audio_manager
        self.conversation_manager = conversation_manager
        self.runtime_bridge = runtime_bridge
        self.deepgram_stt_manager = deepgram_stt_manager
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.websocket_stream_test_thread: threading.Thread | None = None
        self.voice_hotkey_shortcut: QShortcut | None = None
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
        self.current_language = normalize_current_language(
            self.settings_manager.get_setting(
                CURRENT_LANGUAGE_SETTING_ID,
                "spanish",
            )
        )
        self.current_state = AssistantState.IDLE
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

        self.setWindowTitle("PYTHON_AI_ASSISTANT")
        self.resize(1180, 780)
        self._build_ui()
        self._apply_style()
        apply_native_dark_title_bar(self)
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
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(14)

        logo_label = QLabel()
        logo_label.setObjectName("brandLogo")
        logo_label.setFixedSize(276, 76)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_label.setPixmap(self._load_logo_pixmap(276, 76))

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

        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(12, 12)

        self.status_label = QLabel(AssistantState.IDLE.value)
        self.status_label.setObjectName("statusLabel")

        self.mic_level_meter = self._build_level_meter(
            "micLevelMeter", self._t("mic_tooltip")
        )
        self.output_level_meter = self._build_level_meter(
            "outputLevelMeter", self._t("out_tooltip")
        )
        meters_panel = QWidget()
        meters_panel.setObjectName("metersPanel")
        meters_layout = QVBoxLayout(meters_panel)
        meters_layout.setContentsMargins(10, 8, 10, 8)
        meters_layout.setSpacing(7)
        meters_layout.addLayout(self._build_level_meter_row("MIC", self.mic_level_meter))
        meters_layout.addLayout(
            self._build_level_meter_row("OUT", self.output_level_meter)
        )

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self._open_settings)

        self.websocket_button = QPushButton()
        self.websocket_button.setObjectName("websocketButton")
        self.websocket_button.clicked.connect(self._reload_websocket_server)

        header_layout.addWidget(logo_label)
        header_layout.addWidget(title_stack)
        header_layout.addStretch(1)
        header_layout.addWidget(meters_panel)
        header_layout.addWidget(self.status_dot)
        header_layout.addWidget(self.status_label)
        header_layout.addWidget(self.websocket_button)
        header_layout.addWidget(self.settings_button)

        self.chat_panel = ChatPanel()
        self.chat_panel.input_activity_changed.connect(
            self._handle_input_activity_changed
        )
        self.chat_panel.send_requested.connect(self._handle_send_requested)

        root.addWidget(header)
        root.addWidget(self._build_unreal_update_panel())
        workspace = QWidget()
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(12)
        debug_panel = self._build_debug_panel()
        debug_panel.setMinimumWidth(430)
        debug_panel.setMaximumWidth(520)
        workspace_layout.addWidget(debug_panel)
        workspace_layout.addWidget(self.chat_panel, 1)
        root.addWidget(workspace, 1)
        self.setCentralWidget(central)
        self._apply_ui_language(self.current_language)

    def _t(self, key: str) -> str:
        return tr(self.current_language, key)

    def _apply_ui_language(self, language: str | None = None) -> None:
        if language is not None:
            self.current_language = normalize_current_language(language)

        self.setWindowTitle(f"MYRALIS AI - {self._t('technical_console')}")
        self.title_label.setText(self._t("technical_console"))
        self.subtitle_label.setText(self._t("console_subtitle"))
        self.settings_button.setText(self._t("settings"))
        self.websocket_button.setText(self._t("reload_websocket"))
        self.debug_panel_title.setText(self._t("debug_panel"))
        self.debug_log_view.setPlaceholderText(self._t("logs"))
        self.mic_level_meter.setToolTip(self._t("mic_tooltip"))
        self.output_level_meter.setToolTip(self._t("out_tooltip"))
        self.chat_panel.set_language(self.current_language)

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

    def _refresh_unreal_settings_panel(self) -> None:
        self.unreal_update_history_title.setText(self._t("recent_unreal_changes"))
        if not self._unreal_settings_events:
            self.unreal_update_title.setText(self._t("unreal_monitor"))
            self.unreal_update_details.setText(self._t("unreal_waiting"))
            self.unreal_update_history_view.setPlainText(self._t("no_unreal_changes"))
            return

        self._render_unreal_settings_visual_event(self._unreal_settings_events[0])

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
        assets_dir = self.settings_manager.root / "assets"
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

        self.unreal_update_history_view = QPlainTextEdit()
        self.unreal_update_history_view.setObjectName("unrealUpdateHistoryView")
        self.unreal_update_history_view.setReadOnly(True)
        self.unreal_update_history_view.setMaximumBlockCount(8)
        self.unreal_update_history_view.setMinimumHeight(88)
        self.unreal_update_history_view.setMaximumHeight(118)
        self.unreal_update_history_view.setPlainText("")
        layout.addWidget(self.unreal_update_history_view)
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

    def _handle_send_requested(self, text: str) -> None:
        self._start_conversation_from_text(text, source="text")

    def _start_conversation_from_text(self, text: str, *, source: str) -> None:
        clean_text = text.strip()
        if not clean_text:
            return
        if self._conversation_active or (
            self.worker_thread and self.worker_thread.is_alive()
        ):
            return

        self._conversation_active = True
        self.chat_panel.add_user_message(clean_text)
        self.chat_panel.set_input_enabled(False)
        self.conversation_manager.note_user_interaction()
        self._set_state(AssistantState.LISTENING)

        settings_snapshot = self.settings_manager.get_settings()
        LOGGER.info("Conversation started from %s input", source)
        self.worker_thread = threading.Thread(
            target=self._conversation_worker,
            args=(clean_text, settings_snapshot),
            daemon=True,
        )
        self.worker_thread.start()

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
            elif event["type"] == "unreal_settings_event":
                self._record_unreal_settings_visual_event(event["event"])
            elif event["type"] == "ui_language":
                self._apply_ui_language(str(event["language"]))

    def _handle_unreal_json_message_for_ui(self, payload: dict[str, Any]) -> bool:
        message_type = str(payload.get("type", "")).strip()
        if message_type not in {"settings_update", "settings_action"}:
            return self.conversation_manager.handle_unreal_websocket_message(payload)

        before = self.settings_manager.get_settings()
        handled = self.conversation_manager.handle_unreal_websocket_message(payload)
        if not handled:
            return False

        after = self.settings_manager.get_settings()
        before_language = normalize_current_language(
            before.get(CURRENT_LANGUAGE_SETTING_ID, "spanish")
        )
        after_language = normalize_current_language(
            after.get(CURRENT_LANGUAGE_SETTING_ID, "spanish")
        )
        if before_language != after_language:
            self.events.put({"type": "ui_language", "language": after_language})

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
        return True

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
        self.unreal_update_history_view.setPlainText(
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
        rows = (
            (self._t("type"), event.get("type", "-"), "#f4f1ea"),
            (self._t("group"), event.get("group", "-"), "#f4f1ea"),
            (item_label, event.get("item", "-"), "#f4f1ea"),
            (self._t("previous"), event.get("previous", "-"), "#b8bdc8"),
            (self._t("new"), event.get("new", "-"), "#ff4fd8"),
            (self._t("effect"), event.get("effect", "-"), "#f4f1ea"),
            (self._t("time"), event.get("time", "-"), "#b8bdc8"),
            (self._t("source"), event.get("source", "-"), "#b8bdc8"),
        )
        return "<br>".join(
            (
                '<span style="color:#c9a24d;font-weight:800;">'
                f"{html.escape(str(label))}:"
                "</span> "
                f'<span style="color:{color};font-weight:700;">'
                f"{html.escape(str(value))}"
                "</span>"
            )
            for label, value, color in rows
        )

    def _unreal_event_history_line(self, event: dict[str, str]) -> str:
        item_label = self._unreal_event_item_label(event)
        item = event.get("item", "-")
        if event.get("type") == "settings_update":
            change = f"{event.get('previous', '-')} -> {event.get('new', '-')}"
        else:
            change = event.get("effect", "-")
        return (
            f"{event.get('time', '-')} | {event.get('type', '-')} | "
            f"{event.get('group', '-')} | {item_label}: {item} | {change}"
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

    def _handle_input_activity_changed(self, has_text: bool) -> None:
        if self._conversation_active or not self.chat_panel.input_box.isEnabled():
            return
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "text":
            return
        if has_text and self.current_state == AssistantState.IDLE:
            self.conversation_manager.note_user_interaction()
            self._set_state(AssistantState.LISTENING)
        elif not has_text and self.current_state == AssistantState.LISTENING:
            self._set_state(AssistantState.IDLE)

    def _handle_voice_hotkey_pressed(self) -> None:
        if self._conversation_active:
            return
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            return
        if self.current_state == AssistantState.IDLE:
            self.conversation_manager.note_user_interaction()
            self._set_state(AssistantState.LISTENING)

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
            parent=self,
        )
        dialog.settings_changed.connect(self._settings_changed)
        dialog.exec()

    def _start_websocket_server(self, auto: bool = False) -> None:
        started = start_websocket_server()
        if started:
            if not auto:
                self.chat_panel.add_system_message(
                    "WebSocket iniciando en ws://127.0.0.1:8765"
                )
            self._send_backend_ui_snapshot("connected")
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

    def _settings_changed(self) -> None:
        interval = int(
            self.settings_manager.get_setting("app.state_poll_interval_ms", 100)
        )
        self.event_timer.setInterval(interval)
        self._configure_voice_hotkey()
        self._start_input_level_monitor()
        self._sync_deepgram_stt_for_state(self.current_state)

    def _handle_ai_realtime_processing_changed(self, enabled: bool) -> None:
        if enabled:
            self._sync_deepgram_stt_for_state(self.current_state)
            return

        was_listening = self.deepgram_stt_manager.is_listening()
        self.deepgram_stt_manager.stop_listening()
        if was_listening or self.current_state == AssistantState.LISTENING:
            self._start_input_level_monitor()

    def _configure_voice_hotkey(self) -> None:
        hotkey = str(self.settings_manager.get_setting("app.hotkey", "F8")).strip()
        sequence = QKeySequence(hotkey or "F8")
        if self.voice_hotkey_shortcut is None:
            self.voice_hotkey_shortcut = QShortcut(sequence, self)
            self.voice_hotkey_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            self.voice_hotkey_shortcut.activated.connect(
                self._handle_voice_hotkey_pressed
            )
        else:
            self.voice_hotkey_shortcut.setKey(sequence)
        self.voice_hotkey_shortcut.setEnabled(not sequence.isEmpty())

    def _configure_debug_hotkey(self) -> None:
        sequence = QKeySequence("Ctrl+Shift+D")
        if self.debug_hotkey_shortcut is None:
            self.debug_hotkey_shortcut = QShortcut(sequence, self)
            self.debug_hotkey_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            self.debug_hotkey_shortcut.activated.connect(
                self._handle_debug_hotkey_activated
            )
        else:
            self.debug_hotkey_shortcut.setKey(sequence)
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

    def _handle_debug_hotkey_activated(self) -> None:
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
        if self.isVisible() and not self.isMinimized():
            self.hide()
            LOGGER.info("Debug UI hidden")
            return
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
            self.hide()
            LOGGER.info("Debug UI hidden")

    def _refresh_debug_panel(self) -> None:
        if not hasattr(self, "_debug_value_labels"):
            return

        settings = self.settings_manager.get_settings()
        runtime_state = self.runtime_bridge.get_runtime_state()
        profile = self._usage_profile(settings)
        conversations, hours, cost_per_interaction, budget = self._usage_estimates(
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
            f"{profile}: {conversations} conversations / {hours}h "
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
        settings = self.settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            return
        capture_active = (
            self.deepgram_stt_manager.is_listening()
            or self.audio_manager.is_input_level_monitor_active()
        )
        if not capture_active or not has_websocket_client():
            return
        now = time.time()
        if now - self._last_mic_level_send_time < MIC_LEVEL_SEND_INTERVAL_SECONDS:
            return
        self._last_mic_level_send_time = now
        level = max(0.0, min(1.0, float(mic_level)))
        send_json_to_unreal_threadsafe({"type": "mic_level", "level": level})

    def _build_level_meter(self, object_name: str, tooltip: str) -> QProgressBar:
        meter = QProgressBar()
        meter.setObjectName(object_name)
        meter.setRange(0, 100)
        meter.setValue(0)
        meter.setTextVisible(False)
        meter.setFixedSize(58, 7)
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
        label.setFixedWidth(24)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(label)
        layout.addWidget(meter)
        return layout

    def _set_state(self, state: AssistantState) -> None:
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
                        "AI realtime processing disabled; Deepgram STT auto-start skipped"
                    )
                    was_listening = self.deepgram_stt_manager.is_listening()
                    self.deepgram_stt_manager.stop_listening()
                    if was_listening:
                        LOGGER.info("Deepgram STT stopped because AI realtime is disabled")
                    self._start_input_level_monitor()
                    return

                devices = self.settings_manager.get_devices()
                if not self._deepgram_stt_enabled(settings):
                    LOGGER.info("Deepgram STT skipped for current voice settings")
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
            LOGGER.exception("Could not sync Deepgram STT for state=%s", state.value)

    def _deepgram_stt_enabled(self, settings: dict[str, Any]) -> bool:
        if self._interaction_mode(settings) != "voice":
            return False
        if str(settings.get("stt_engine", "deepgram")).strip() != "deepgram":
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

    def _handle_websocket_connection_status(self, connected: bool) -> None:
        self.events.put({"type": "websocket_connection", "connected": connected})

    def _handle_websocket_connection_event(self, connected: bool) -> None:
        self._websocket_connected = connected
        if connected:
            self._send_backend_ui_snapshot("connected")
            self._send_audio_devices()
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

    def _build_backend_ui_payload(self, status: str | None = None) -> dict[str, str]:
        settings = self.settings_manager.get_settings()
        profile = self._usage_profile(settings)
        conversations, hours, cost_per_interaction, budget = self._usage_estimates(
            settings,
            profile,
        )
        clean_status = status or ("connected" if has_websocket_client() else "disconnected")
        if clean_status not in BACKEND_UI_STATUSES:
            clean_status = "error"
        LOGGER.info(
            "backend_ui usage estimates: status=%s profile=%s budget=%.4f "
            "estimated_cost_per_interaction=%.4f conversations=%s hours=%s "
            "source=local_estimates billing_apis=not_integrated",
            clean_status,
            profile,
            budget,
            cost_per_interaction,
            conversations,
            hours,
        )
        return {
            "type": "backend_ui",
            "system_connection_status": clean_status,
            "usage_conversations_estimate": str(conversations),
            "usage_hours_estimate": str(hours),
            "usage_profile": profile,
        }

    def _usage_profile(self, settings: dict[str, Any]) -> str:
        openai_model = str(settings.get("openai_model", "")).strip()
        response_length = str(settings.get("response_length", "")).strip()
        elevenlabs_model = str(settings.get("elevenlabs_model", "")).strip()
        if openai_model == "GPT-5" or response_length == "detailed" or elevenlabs_model == "eleven_v3":
            return "high_quality"
        if (
            openai_model in {"GPT-5.4 nano", "GPT-5 mini"}
            and response_length in {"very_short", "short"}
            and self._interaction_mode(settings) == "text"
        ):
            return "low_usage"
        return "balanced"

    def _usage_estimates(
        self,
        settings: dict[str, Any],
        profile: str,
    ) -> tuple[int, int, float, float]:
        budget = self._usage_budget_remaining()
        average_per_hour = max(
            1.0,
            self._float_from_env(
                "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR",
                DEFAULT_USAGE_AVERAGE_CONVERSATIONS_PER_HOUR,
            ),
        )
        estimated_cost = self._estimated_complete_interaction_cost(settings, profile)
        if budget <= 0:
            return 0, 0, estimated_cost, budget

        conservative_factor = max(
            0.0,
            min(
                1.0,
                self._float_from_env(
                    "USAGE_ESTIMATE_CONSERVATIVE_FACTOR",
                    DEFAULT_USAGE_CONSERVATIVE_FACTOR,
                ),
            ),
        )
        conservative_conversations = int(
            (budget / max(estimated_cost, 0.0001)) * conservative_factor
        )
        conservative_hours = int(conservative_conversations / average_per_hour)
        return (
            max(0, conservative_conversations),
            max(0, conservative_hours),
            estimated_cost,
            budget,
        )

    def _estimated_complete_interaction_cost(
        self,
        settings: dict[str, Any],
        profile: str,
    ) -> float:
        clean_profile = profile if profile in USAGE_PROFILES else "balanced"
        base_cost = self._float_from_env(
            f"USAGE_ESTIMATE_COST_{clean_profile.upper()}_USD",
            DEFAULT_USAGE_COST_BY_PROFILE_USD[clean_profile],
        )

        if self._interaction_mode(settings) == "text":
            return max(0.0001, base_cost)

        stt_cost = 0.0
        if str(settings.get("stt_engine", "deepgram")).strip() == "deepgram":
            deepgram_settings = settings.get("deepgram", {})
            deepgram_enabled = (
                bool(deepgram_settings.get("enabled", True))
                if isinstance(deepgram_settings, dict)
                else True
            )
            if deepgram_enabled:
                stt_cost = self._float_from_env(
                    "USAGE_ESTIMATE_STT_COST_USD",
                    DEFAULT_USAGE_STT_COST_USD,
                )

        emotion_cost = 0.0
        if bool(settings.get("listening_emotion_analysis", True)):
            emotion_cost = self._float_from_env(
                "USAGE_ESTIMATE_LISTENING_EMOTION_COST_USD",
                DEFAULT_USAGE_LISTENING_EMOTION_COST_USD,
            )

        return max(0.0001, base_cost + stt_cost + emotion_cost)

    def _usage_budget_remaining(self) -> float:
        return max(
            0.0,
            self._float_from_env(
                "USAGE_BUDGET_USD",
                self._float_from_env("USAGE_CREDITS_REMAINING", 0.0),
            ),
        )

    def _float_from_env(self, env_key: str, default: float) -> float:
        raw_value = os.getenv(env_key, default)
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return default

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #050505;
            }
            QWidget {
                background: transparent;
            }
            QFrame#header {
                background: #000000;
                border: 1px solid #2a2415;
                border-radius: 8px;
            }
            QLabel#brandLogo {
                background: #000000;
                border: none;
            }
            QFrame#debugPanel {
                background: #0c0c0c;
                border: 1px solid #2b2618;
                border-radius: 8px;
            }
            QFrame#unrealUpdatePanel {
                background: #080808;
                border: 2px solid #c9a24d;
                border-radius: 8px;
            }
            QLabel#appTitle {
                color: #f4f1ea;
                font-size: 15px;
                font-weight: 800;
                letter-spacing: 0px;
            }
            QLabel#appSubtitle {
                color: #9f9a8d;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#unrealUpdateTitle {
                color: #f0c86a;
                font-size: 24px;
                font-weight: 900;
            }
            QLabel#unrealUpdateDetails {
                color: #f4f1ea;
                font-size: 15px;
                line-height: 145%;
            }
            QLabel#unrealUpdateHistoryTitle {
                color: #c9a24d;
                font-size: 12px;
                font-weight: 800;
            }
            QLabel#debugPanelTitle {
                color: #c9a24d;
                font-size: 14px;
                font-weight: 800;
            }
            QLabel#debugFieldLabel {
                color: #a9a59a;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#debugFieldValue {
                color: #f4f1ea;
                font-size: 12px;
            }
            QPlainTextEdit#debugLogView {
                background: #050505;
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
            QPlainTextEdit#unrealUpdateHistoryView {
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
            QLabel#statusLabel {
                color: #f4f1ea;
                font-size: 13px;
                font-weight: 700;
                min-width: 78px;
            }
            QPushButton#settingsButton {
                background: #090909;
                color: #f4f1ea;
                border: 1px solid #c9a24d;
                border-radius: 6px;
                padding: 8px 12px;
                font-weight: 600;
            }
            QPushButton#settingsButton:hover {
                background: #171309;
            }
            QPushButton#websocketButton {
                background: #0d0d0d;
                color: #f4f1ea;
                border: 1px solid #5f4b1f;
                border-radius: 6px;
                padding: 8px 12px;
                font-weight: 600;
            }
            QPushButton#websocketButton:hover {
                background: #171309;
                border-color: #c9a24d;
            }
            QWidget#metersPanel {
                background: #070707;
                border: 1px solid #2b2618;
                border-radius: 8px;
            }
            QLabel#meterLabel {
                color: #c9a24d;
                font-size: 10px;
                font-weight: 700;
            }
            QProgressBar#micLevelMeter,
            QProgressBar#outputLevelMeter {
                background: #020202;
                border: 1px solid #2b2618;
                border-radius: 3px;
            }
            QProgressBar#micLevelMeter::chunk {
                background: #f4f1ea;
                border-radius: 2px;
            }
            QProgressBar#outputLevelMeter::chunk {
                background: #c9a24d;
                border-radius: 2px;
            }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._shutdown_started:
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        apply_native_dark_title_bar(self)

    def shutdown_backend(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        remove_websocket_connection_status_handler(
            self._handle_websocket_connection_status
        )
        if self.debug_hotkey_filter is not None:
            self.debug_hotkey_filter.unregister()
        stop_websocket_server()
        self.deepgram_stt_manager.shutdown()
        self.conversation_manager.shutdown()
        self.audio_manager.stop_input_level_monitor()
