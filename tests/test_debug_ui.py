import ctypes
import unittest
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from core.conversation_manager import AssistantState, ConversationManager
from ui.main_window import WindowsDebugHotkeyFilter
from ui.main_window import WindowsGlobalHotkeyHook
from ui.main_window import MainWindow


class _UnrealIndicatorHarness:
    _build_unreal_settings_visual_event = (
        MainWindow._build_unreal_settings_visual_event
    )
    _format_unreal_value = MainWindow._format_unreal_value
    _setting_value_from_snapshot = MainWindow._setting_value_from_snapshot
    _unreal_action_effect = MainWindow._unreal_action_effect
    _unreal_action_group = MainWindow._unreal_action_group
    _unreal_setting_group = MainWindow._unreal_setting_group
    _unreal_update_effect = MainWindow._unreal_update_effect


class DebugUITests(unittest.TestCase):
    def test_global_debug_hotkey_is_only_ctrl_shift_d_without_alt(self) -> None:
        self.assertEqual(WindowsDebugHotkeyFilter.VK_D, 0x44)
        modifiers = (
            WindowsDebugHotkeyFilter.MOD_CONTROL
            | WindowsDebugHotkeyFilter.MOD_SHIFT
            | WindowsDebugHotkeyFilter.MOD_NOREPEAT
        )
        self.assertEqual(modifiers & WindowsDebugHotkeyFilter.MOD_CONTROL, 0x0002)
        self.assertEqual(modifiers & WindowsDebugHotkeyFilter.MOD_SHIFT, 0x0004)
        self.assertEqual(modifiers & 0x0001, 0)
        self.assertEqual(
            WindowsDebugHotkeyFilter.DEBUG_HOTKEYS,
            (
                (
                    "Ctrl+Shift+D",
                    WindowsDebugHotkeyFilter.HOTKEY_ID,
                    WindowsDebugHotkeyFilter.VK_D,
                ),
            ),
        )

    def test_global_debug_hotkey_registers_against_window_handle(self) -> None:
        callback = Mock()
        hotkey_filter = WindowsDebugHotkeyFilter(callback, window_id=12345)
        user32 = SimpleNamespace(
            RegisterHotKey=Mock(return_value=1),
            UnregisterHotKey=Mock(return_value=1),
        )
        windll = SimpleNamespace(user32=user32)
        app = Mock()

        with patch("ui.main_window.sys.platform", "win32"), patch(
            "ui.main_window.ctypes.windll",
            windll,
            create=True,
        ), patch("ui.main_window.QCoreApplication.instance", return_value=app):
            self.assertTrue(hotkey_filter.register())
            hotkey_filter.unregister()

        modifiers = (
            WindowsDebugHotkeyFilter.MOD_CONTROL
            | WindowsDebugHotkeyFilter.MOD_SHIFT
            | WindowsDebugHotkeyFilter.MOD_NOREPEAT
        )
        user32.RegisterHotKey.assert_has_calls(
            [
                call(
                    12345,
                    WindowsDebugHotkeyFilter.HOTKEY_ID,
                    modifiers,
                    WindowsDebugHotkeyFilter.VK_D,
                ),
            ]
        )
        self.assertEqual(user32.RegisterHotKey.call_count, 1)
        user32.UnregisterHotKey.assert_has_calls(
            [
                call(12345, WindowsDebugHotkeyFilter.HOTKEY_ID),
            ],
            any_order=True,
        )
        self.assertEqual(user32.UnregisterHotKey.call_count, 1)

    def test_voice_hotkey_hook_emits_press_and_release(self) -> None:
        pressed = Mock()
        released = Mock()
        hook = WindowsGlobalHotkeyHook(pressed, released, hotkey_text="F8")
        hook._virtual_key = WindowsGlobalHotkeyHook._virtual_key_for_name(hook, "F8")
        hook._modifiers = 0
        hook._modifiers_satisfied = Mock(return_value=True)
        user32 = SimpleNamespace(CallNextHookEx=Mock(return_value=0))
        windll = SimpleNamespace(user32=user32)
        key = WindowsGlobalHotkeyHook.KBDLLHOOKSTRUCT()
        key.vkCode = hook._virtual_key or 0

        with patch("ui.main_window.ctypes.windll", windll, create=True), patch(
            "ui.main_window.QTimer.singleShot"
        ) as single_shot:
            hook._handle_keyboard_event(
                0,
                WindowsGlobalHotkeyHook.WM_KEYDOWN,
                ctypes.addressof(key),
            )
            hook._handle_keyboard_event(
                0,
                WindowsGlobalHotkeyHook.WM_KEYUP,
                ctypes.addressof(key),
            )

        self.assertEqual(single_shot.call_count, 2)
        self.assertEqual(single_shot.call_args_list[0].args[1], pressed)
        self.assertEqual(single_shot.call_args_list[1].args[1], released)

    def test_voice_hotkey_registers_globally_on_windows(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.settings_manager = SimpleNamespace(
            get_setting=Mock(return_value="F8")
        )
        window.voice_hotkey_filter = None
        window.voice_hotkey_hook = None
        window.winId = Mock(return_value=12345)
        window._unregister_voice_hotkey = Mock()

        fake_hook = Mock()
        fake_hook.register.return_value = True

        with patch("ui.main_window.sys.platform", "win32"), patch(
            "ui.main_window.WindowsGlobalHotkeyHook",
            return_value=fake_hook,
        ), patch("ui.main_window.QShortcut") as qshortcut:
            MainWindow._configure_voice_hotkey(window)

        window._unregister_voice_hotkey.assert_called_once_with()
        fake_hook.register.assert_called_once_with()
        qshortcut.assert_not_called()
        self.assertIs(window.voice_hotkey_hook, fake_hook)
        self.assertIsNone(window.voice_hotkey_filter)

    def test_voice_hotkey_does_not_use_local_shortcut_when_global_registration_fails(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.settings_manager = SimpleNamespace(
            get_setting=Mock(return_value="F8")
        )
        window.voice_hotkey_filter = None
        window.voice_hotkey_hook = None
        window.winId = Mock(return_value=12345)
        window._unregister_voice_hotkey = Mock()

        fake_hook = Mock()
        fake_hook.register.return_value = False

        with patch("ui.main_window.sys.platform", "win32"), patch(
            "ui.main_window.WindowsGlobalHotkeyHook",
            return_value=fake_hook,
        ), patch("ui.main_window.QShortcut") as qshortcut:
            MainWindow._configure_voice_hotkey(window)

        window._unregister_voice_hotkey.assert_called_once_with()
        fake_hook.register.assert_called_once_with()
        qshortcut.assert_not_called()
        self.assertIsNone(window.voice_hotkey_hook)
        self.assertIsNone(window.voice_hotkey_filter)

    def test_voice_hotkey_release_transitions_to_thinking(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.current_state = AssistantState.LISTENING
        window.conversation_manager = SimpleNamespace(
            emit_external_state=Mock()
        )
        window._set_state = Mock()

        MainWindow._handle_voice_hotkey_released(window)

        window.conversation_manager.emit_external_state.assert_called_once_with(
            AssistantState.THINKING
        )
        window._set_state.assert_called_once_with(AssistantState.THINKING)

    def test_debug_hotkey_toggles_ui_without_enabling_debug_mode(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._last_debug_hotkey_toggle_time = 0.0
        window._set_debug_mode_enabled = Mock()
        window.toggle_debug_ui = Mock()

        with patch("ui.main_window.time.monotonic", return_value=10.0):
            MainWindow._handle_debug_hotkey_activated(window)

        window._set_debug_mode_enabled.assert_not_called()
        window.toggle_debug_ui.assert_called_once_with()

    def test_session_reset_turns_debug_mode_off(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.conversation_manager = SimpleNamespace(
            is_ai_realtime_processing_enabled=Mock(return_value=False),
            set_ai_realtime_processing_enabled=Mock(),
        )
        window._refresh_debug_mode_controls = Mock()

        MainWindow._reset_debug_mode_to_normal(window)

        window.conversation_manager.set_ai_realtime_processing_enabled.assert_called_once_with(
            True,
            source="debug_ui_session_reset",
        )
        window._refresh_debug_mode_controls.assert_not_called()

    def test_show_debug_ui_resets_debug_mode_before_opening(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._reset_debug_mode_to_normal = Mock()
        window._refresh_debug_panel = Mock()
        window.isVisible = Mock(return_value=False)
        window.showNormal = Mock()
        window.raise_ = Mock()
        window.activateWindow = Mock()

        with patch("ui.main_window.sys.platform", "linux"):
            MainWindow.show_debug_ui(window)

        window._reset_debug_mode_to_normal.assert_called_once_with()
        window._refresh_debug_panel.assert_called_once_with()
        window.showNormal.assert_called_once_with()

    def test_voice_hotkey_plays_beep_when_listening_starts(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = False
        window.current_state = AssistantState.IDLE
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "voice"}),
            get_setting=Mock(side_effect=lambda key, default=None: 0.25 if key == "ui_volume" else default),
            get_devices=Mock(return_value={"input_device_index": 3}),
        )
        window.conversation_manager = SimpleNamespace(
            note_user_interaction=Mock(),
            emit_external_state=Mock(),
        )
        window.deepgram_stt_manager = SimpleNamespace(
            is_listening=Mock(return_value=False),
            start_listening=Mock(return_value=True),
        )
        window._set_state = Mock()
        window._play_listening_beep = Mock()

        MainWindow._handle_voice_hotkey_pressed(window)

        window.conversation_manager.note_user_interaction.assert_called_once_with()
        window.conversation_manager.emit_external_state.assert_any_call(
            AssistantState.LISTENING
        )
        window._set_state.assert_called_once_with(
            AssistantState.LISTENING,
            sync_voice_state=False,
        )
        window.deepgram_stt_manager.start_listening.assert_called_once()
        window._play_listening_beep.assert_called_once_with()

    def test_voice_hotkey_forces_listening_even_when_not_idle(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = False
        window.current_state = AssistantState.THINKING
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "voice"}),
            get_setting=Mock(side_effect=lambda key, default=None: 0.25 if key == "ui_volume" else default),
            get_devices=Mock(return_value={"input_device_index": 3}),
        )
        window.conversation_manager = SimpleNamespace(
            note_user_interaction=Mock(),
            emit_external_state=Mock(),
        )
        window.deepgram_stt_manager = SimpleNamespace(
            is_listening=Mock(return_value=False),
            start_listening=Mock(return_value=True),
        )
        window._set_state = Mock()
        window._play_listening_beep = Mock()

        MainWindow._handle_voice_hotkey_pressed(window)

        window.conversation_manager.note_user_interaction.assert_called_once_with()
        window.conversation_manager.emit_external_state.assert_any_call(
            AssistantState.LISTENING
        )
        window._set_state.assert_called_once_with(
            AssistantState.LISTENING,
            sync_voice_state=False,
        )
        window.deepgram_stt_manager.start_listening.assert_called_once()
        window._play_listening_beep.assert_called_once_with()

    def test_voice_hotkey_uses_local_stt_when_settings_are_local(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = False
        window.current_state = AssistantState.IDLE
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(
                return_value={
                    "interaction_mode": "voice",
                    "stt_engine": "local",
                    "deepgram": {"enabled": False, "api_key": ""},
                }
            ),
            get_setting=Mock(
                side_effect=lambda key, default=None: 0.25 if key == "ui_volume" else default
            ),
            get_devices=Mock(return_value={"input_device_index": 3}),
        )
        window.conversation_manager = SimpleNamespace(
            note_user_interaction=Mock(),
            emit_external_state=Mock(),
        )
        deepgram_manager = SimpleNamespace(
            is_listening=Mock(return_value=False),
            start_listening=Mock(return_value=True),
        )
        window.deepgram_stt_manager = deepgram_manager
        window._set_state = Mock()
        window._play_listening_beep = Mock()

        MainWindow._handle_voice_hotkey_pressed(window)

        started_settings = deepgram_manager.start_listening.call_args.kwargs["settings"]
        self.assertEqual(started_settings["stt_engine"], "local")
        self.assertFalse(started_settings["deepgram"]["enabled"])
        self.assertEqual(started_settings["deepgram"]["api_key"], "")
        window.conversation_manager.emit_external_state.assert_any_call(
            AssistantState.LISTENING
        )

    def test_play_listening_beep_uses_ui_volume(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.settings_manager = SimpleNamespace(
            get_setting=Mock(return_value=0.32)
        )
        window.audio_manager = SimpleNamespace(play_ui_beep=Mock(return_value=True))

        MainWindow._play_listening_beep(window)

        window.audio_manager.play_ui_beep.assert_called_once_with(0.32)

    def test_mic_level_outbound_events_are_hidden_by_default(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._outbound_unreal_events = []
        window._show_mic_level_outbound_events = False

        MainWindow._record_unreal_outbound_event(
            window,
            {"type": "mic_level", "payload": {"type": "mic_level", "level": 0.5}},
        )
        MainWindow._record_unreal_outbound_event(
            window,
            {"type": "runtime_state", "payload": {"type": "runtime_state"}},
        )

        self.assertEqual(len(window._outbound_unreal_events), 1)
        self.assertEqual(window._outbound_unreal_events[0]["type"], "runtime_state")

    def test_mic_level_outbound_events_can_be_enabled_and_purged(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._outbound_unreal_events = []
        window._show_mic_level_outbound_events = True

        MainWindow._record_unreal_outbound_event(
            window,
            {"type": "mic_level", "payload": {"type": "mic_level", "level": 0.5}},
        )

        self.assertEqual(len(window._outbound_unreal_events), 1)
        self.assertEqual(window._outbound_unreal_events[0]["type"], "mic_level")

        MainWindow._handle_mic_level_outbound_toggle_clicked(window, False)

        self.assertEqual(window._outbound_unreal_events, [])

    def test_unreal_mic_level_show_message_updates_flag_only(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._mic_level_show = False
        window.conversation_manager = SimpleNamespace(
            handle_unreal_websocket_message=Mock(return_value=False)
        )

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "mic_level", "show": "true"},
        )

        self.assertTrue(handled)
        self.assertTrue(window._mic_level_show)
        window.conversation_manager.handle_unreal_websocket_message.assert_not_called()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "mic_level", "show": False},
        )

        self.assertTrue(handled)
        self.assertFalse(window._mic_level_show)

    def test_unreal_mic_level_action_messages_update_flag(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._mic_level_show = False
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"current_language": "spanish"})
        )
        window.conversation_manager = SimpleNamespace(
            handle_unreal_websocket_message=Mock(return_value=True)
        )
        window.events = queue.Queue()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "settings_action", "action": "mic_level_is_showing"},
        )

        self.assertTrue(handled)
        self.assertTrue(window._mic_level_show)
        window.conversation_manager.handle_unreal_websocket_message.assert_called_once()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "settings_action", "action": "mic_level_is_not_showing"},
        )

        self.assertTrue(handled)
        self.assertFalse(window._mic_level_show)

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "settings_action", "action": "mic_level_not_showing"},
        )

        self.assertTrue(handled)
        self.assertFalse(window._mic_level_show)

    def test_unreal_input_device_update_restarts_python_mic(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.events = queue.Queue()
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"current_language": "spanish"})
        )
        window.conversation_manager = SimpleNamespace(
            handle_unreal_websocket_message=Mock(return_value=True)
        )
        window._start_input_level_monitor = Mock()
        window._sync_deepgram_stt_for_state = Mock()
        window.current_state = AssistantState.IDLE

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "settings_update", "setting": "input_device", "value": "device_3"},
        )

        self.assertTrue(handled)
        window._start_input_level_monitor.assert_called_once_with()
        window._sync_deepgram_stt_for_state.assert_called_once_with(
            AssistantState.IDLE
        )

    def test_unreal_debug_mode_button_updates_flag_and_sends_backend_ui(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._unreal_debug_mode = False
        window.unreal_debug_mode_toggle_button = SimpleNamespace(setChecked=Mock())
        window._send_backend_ui_snapshot = Mock()

        MainWindow._handle_unreal_debug_mode_toggle_clicked(window, True)

        self.assertTrue(window._unreal_debug_mode)
        window.unreal_debug_mode_toggle_button.setChecked.assert_called_once_with(True)
        window._send_backend_ui_snapshot.assert_called_once_with()

    def test_unreal_text_input_started_sets_listening_and_emits_runtime_state(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = False
        window.current_state = AssistantState.IDLE
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "text"})
        )
        window.conversation_manager = SimpleNamespace(note_user_interaction=Mock())
        window._set_state = Mock()
        window._emit_unreal_text_input_runtime_state = Mock()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "text_input_state", "state": "started"},
        )

        self.assertTrue(handled)
        window.conversation_manager.note_user_interaction.assert_called_once_with()
        window._set_state.assert_called_once_with(AssistantState.LISTENING)
        window._emit_unreal_text_input_runtime_state.assert_called_once_with(
            AssistantState.LISTENING
        )

    def test_unreal_text_input_timeout_sets_idle_and_emits_runtime_state(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = False
        window.current_state = AssistantState.LISTENING
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "text"})
        )
        window.conversation_manager = SimpleNamespace(note_user_interaction=Mock())
        window._set_state = Mock()
        window._emit_unreal_text_input_runtime_state = Mock()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "text_input_state", "state": "time out"},
        )

        self.assertTrue(handled)
        window.conversation_manager.note_user_interaction.assert_not_called()
        window._set_state.assert_called_once_with(AssistantState.IDLE)
        window._emit_unreal_text_input_runtime_state.assert_called_once_with(
            AssistantState.IDLE
        )

    def test_unreal_text_input_state_does_not_interrupt_active_conversation(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = True
        window.current_state = AssistantState.THINKING
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "text"})
        )
        window.conversation_manager = SimpleNamespace(note_user_interaction=Mock())
        window._set_state = Mock()
        window._emit_unreal_text_input_runtime_state = Mock()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "text_input_state", "state": "timeout"},
        )

        self.assertTrue(handled)
        window.conversation_manager.note_user_interaction.assert_not_called()
        window._set_state.assert_not_called()
        window._emit_unreal_text_input_runtime_state.assert_not_called()

    def test_unreal_text_message_is_queued_for_conversation_ui_thread(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.events = queue.Queue()
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "text"})
        )
        window.conversation_manager = SimpleNamespace(
            handle_unreal_websocket_message=Mock(return_value=False)
        )
        window._start_conversation_from_text = Mock()

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {"type": "user_text", "message": "hola"},
        )

        self.assertTrue(handled)
        window._start_conversation_from_text.assert_not_called()
        self.assertEqual(
            window.events.get_nowait(),
            {"type": "unreal_text_message", "text": "hola"},
        )
        window.conversation_manager.handle_unreal_websocket_message.assert_not_called()

    def test_unreal_text_message_event_starts_conversation_on_ui_thread(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.events = queue.Queue()
        window.events.put({"type": "unreal_text_message", "text": "hola"})
        window._start_conversation_from_text = Mock(return_value=True)

        MainWindow._process_worker_events(window)

        window._start_conversation_from_text.assert_called_once_with(
            "hola",
            source="unreal_text",
        )

    def test_start_conversation_from_text_writes_received_message_to_chat(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = False
        window.worker_thread = None
        window.chat_panel = SimpleNamespace(
            add_user_message=Mock(),
            set_input_enabled=Mock(),
        )
        window.conversation_manager = SimpleNamespace(note_user_interaction=Mock())
        window.settings_manager = SimpleNamespace(get_settings=Mock(return_value={}))
        window._set_state = Mock()
        fake_thread = SimpleNamespace(start=Mock())

        with patch("ui.main_window.threading.Thread", return_value=fake_thread):
            started = MainWindow._start_conversation_from_text(
                window,
                "  hola  ",
                source="unreal_text",
            )

        self.assertTrue(started)
        window.chat_panel.add_user_message.assert_called_once_with("hola")
        window.chat_panel.set_input_enabled.assert_called_once_with(False)
        window.conversation_manager.note_user_interaction.assert_called_once_with()
        window._set_state.assert_called_once_with(AssistantState.THINKING)
        fake_thread.start.assert_called_once_with()

    def test_busy_conversation_still_writes_received_message_to_chat(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._conversation_active = True
        window.worker_thread = None
        window.chat_panel = SimpleNamespace(
            add_user_message=Mock(),
            set_input_enabled=Mock(),
        )
        window.conversation_manager = SimpleNamespace(note_user_interaction=Mock())
        window._set_state = Mock()

        started = MainWindow._start_conversation_from_text(
            window,
            "hola",
            source="voice",
        )

        self.assertFalse(started)
        window.chat_panel.add_user_message.assert_called_once_with("hola")
        window.chat_panel.set_input_enabled.assert_not_called()
        window.conversation_manager.note_user_interaction.assert_not_called()
        window._set_state.assert_not_called()

    def test_conversation_mode_indicator_reflects_settings(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.current_language = "spanish"
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(return_value={"interaction_mode": "voice"})
        )
        window.conversation_mode_title = SimpleNamespace(setText=Mock())
        window.conversation_mode_label = SimpleNamespace(setText=Mock())
        window.conversation_mode_dot = SimpleNamespace(setStyleSheet=Mock())

        MainWindow._refresh_conversation_mode_indicator(window)

        window.conversation_mode_title.setText.assert_called_once_with("MODO ENTRADA")
        window.conversation_mode_label.setText.assert_called_once_with("MODO VOZ")
        self.assertIn(
            "#0ea5e9",
            window.conversation_mode_dot.setStyleSheet.call_args.args[0],
        )

    def test_unreal_backend_ui_show_hide_actions_are_forwarded(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        actions: list[str] = []
        manager._lock = threading.RLock()
        manager._backend_ui_action_handler = actions.append

        self.assertTrue(
            manager.handle_unreal_websocket_message(
                {"type": "backend_ui", "action": "show_python_ui"}
            )
        )
        self.assertTrue(
            manager.handle_unreal_websocket_message(
                {"type": "backend_ui", "action": "hide_python_ui"}
            )
        )
        self.assertFalse(
            manager.handle_unreal_websocket_message(
                {"type": "backend_ui", "action": "toggle_python_ui"}
            )
        )
        self.assertEqual(actions, ["show_python_ui", "hide_python_ui"])

    def test_exit_myralis_backend_ui_action_schedules_shutdown(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window._shutdown_started = False
        window._exit_myralis_requested = False

        app = Mock()
        with patch("ui.main_window.QCoreApplication.instance", return_value=app), patch(
            "ui.main_window.QTimer.singleShot"
        ) as single_shot:
            MainWindow._handle_backend_ui_action(window, "exit_myralis")

            single_shot.assert_called_once()
            self.assertEqual(single_shot.call_args.args[0], 10000)
            single_shot.call_args.args[1]()
            app.quit.assert_called_once_with()

    def test_runtime_state_outgoing_message_also_queues_visual_state_update(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.events = queue.Queue()

        MainWindow._handle_websocket_outgoing_message(
            window,
            {
                "transport": "json",
                "type": "runtime_state",
                "payload": {"state": "thinking", "mood": "Neutral"},
                "client_count": 1,
            },
        )

        queued_events = []
        while not window.events.empty():
            queued_events.append(window.events.get_nowait())

        self.assertIn({"type": "state", "state": AssistantState.THINKING}, queued_events)
        self.assertIn(
            {
                "type": "websocket_outgoing",
                "event": {
                    "transport": "json",
                    "type": "runtime_state",
                    "payload": {"state": "thinking", "mood": "Neutral"},
                    "client_count": 1,
                },
            },
            queued_events,
        )

    def test_unreal_user_text_payload_is_logged_exactly(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager._unreal_turn_lock = SimpleNamespace(acquire=Mock(return_value=False))
        manager.audio_manager = SimpleNamespace(
            settings_manager=SimpleNamespace(
                get_settings=Mock(return_value={"interaction_mode": "text"})
            )
        )

        payload = {
            "type": "user_text",
            "interaction_mode": "text",
            "message": "hola",
        }

        with self.assertLogs("core.conversation_manager", level="INFO") as logs:
            handled = manager.handle_unreal_websocket_message(payload)

        self.assertFalse(handled)
        self.assertIn(
            'Received Unreal text payload: {"type":"user_text","interaction_mode":"text","message":"hola"}',
            "\n".join(logs.output),
        )

    def test_runtime_lip_sync_test_emits_listening_thinking_and_streams_phrase(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._begin_runtime_response = Mock()
        manager._emit_runtime_state = Mock()
        manager._stream_realtime_tts_to_unreal = Mock(return_value=(None, True))

        settings = {"elevenlabs": {"model_id": "eleven_turbo_v2_5"}}
        audio_path = ConversationManager.test_runtime_lip_sync(manager, settings)

        self.assertIsNone(audio_path)
        manager._begin_runtime_response.assert_called_once_with(settings)
        self.assertEqual(
            [call.args[1] for call in manager._emit_runtime_state.call_args_list],
            [AssistantState.LISTENING, AssistantState.THINKING],
        )
        manager._stream_realtime_tts_to_unreal.assert_called_once()
        self.assertEqual(
            manager._stream_realtime_tts_to_unreal.call_args.kwargs["response_text"],
            "Prueba de lip sync en tiempo real para Unreal.",
        )
        self.assertEqual(
            manager._stream_realtime_tts_to_unreal.call_args.kwargs["mood"],
            "Happy",
        )

    def test_unreal_settings_update_queues_backend_ui_refresh(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.events = queue.Queue()
        window.settings_manager = SimpleNamespace(
            get_settings=Mock(
                side_effect=[
                    {"response_length": "balanced"},
                    {"response_length": "detailed"},
                ]
            )
        )
        window.conversation_manager = SimpleNamespace(
            handle_unreal_websocket_message=Mock(return_value=True)
        )

        handled = MainWindow._handle_unreal_json_message_for_ui(
            window,
            {
                "type": "settings_update",
                "setting": "response_length",
                "value": "detailed",
            },
        )

        self.assertTrue(handled)
        queued_events = []
        while not window.events.empty():
            queued_events.append(window.events.get_nowait())
        self.assertIn(
            {"type": "backend_ui_action", "action": "settings_changed"},
            queued_events,
        )

    def test_unreal_settings_update_visual_event_for_general_setting(self) -> None:
        event = _UnrealIndicatorHarness()._build_unreal_settings_visual_event(
            {"type": "settings_update", "setting": "response_length", "value": "detailed"},
            {"response_length": "balanced"},
            {"response_length": "detailed"},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["title"], "UNREAL UPDATE RECEIVED")
        self.assertEqual(event["type"], "settings_update")
        self.assertEqual(event["group"], "Settings")
        self.assertEqual(event["item"], "response_length")
        self.assertEqual(event["previous"], "balanced")
        self.assertEqual(event["new"], "detailed")
        self.assertEqual(event["source"], "Unreal WebSocket")

    def test_unreal_customization_update_visual_event(self) -> None:
        event = _UnrealIndicatorHarness()._build_unreal_settings_visual_event(
            {
                "type": "settings_update",
                "setting": "personality_traits",
                "value": "alegre,empatica,graciosa",
            },
            {"customization": {"personality_traits": "alegre,empatica"}},
            {"customization": {"personality_traits": "alegre,empatica,graciosa"}},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["group"], "Customization")
        self.assertEqual(event["previous"], "alegre,empatica")
        self.assertEqual(event["new"], "alegre,empatica,graciosa")
        self.assertEqual(event["effect"], "Prompt personality updated")

    def test_unreal_current_language_update_visual_event_is_settings(self) -> None:
        event = _UnrealIndicatorHarness()._build_unreal_settings_visual_event(
            {
                "type": "settings_update",
                "setting": "current_language",
                "value": "english",
            },
            {"current_language": "spanish"},
            {"current_language": "english"},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["group"], "Settings")
        self.assertEqual(event["item"], "current_language")
        self.assertEqual(event["previous"], "spanish")
        self.assertEqual(event["new"], "english")
        self.assertEqual(event["effect"], "Python UI language updated")

    def test_unreal_passive_graphics_update_visual_event_is_settings(self) -> None:
        event = _UnrealIndicatorHarness()._build_unreal_settings_visual_event(
            {
                "type": "settings_update",
                "setting": "display_mode",
                "value": "fullscreen",
            },
            {"display_mode": "borderless"},
            {"display_mode": "fullscreen"},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["group"], "Settings")
        self.assertEqual(event["item"], "display_mode")
        self.assertEqual(event["previous"], "borderless")
        self.assertEqual(event["new"], "fullscreen")
        self.assertEqual(event["effect"], "Stored only. Graphics handled by Unreal.")

    def test_unreal_selected_character_visual_event_is_stored_only(self) -> None:
        event = _UnrealIndicatorHarness()._build_unreal_settings_visual_event(
            {"type": "settings_update", "setting": "selected_character", "value": "sofia"},
            {"customization": {"selected_character": "maria"}},
            {"customization": {"selected_character": "sofia"}},
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["group"], "Customization")
        self.assertEqual(event["item"], "selected_character")
        self.assertEqual(event["previous"], "maria")
        self.assertEqual(event["new"], "sofia")
        self.assertEqual(event["effect"], "Stored only. Visual handled by Unreal.")

    def test_unreal_reset_actions_visual_events(self) -> None:
        harness = _UnrealIndicatorHarness()
        settings_event = harness._build_unreal_settings_visual_event(
            {"type": "settings_action", "action": "reset_settings_defaults"},
            {},
            {},
        )
        customization_event = harness._build_unreal_settings_visual_event(
            {"type": "settings_action", "action": "reset_customization_defaults"},
            {},
            {},
        )

        self.assertIsNotNone(settings_event)
        self.assertIsNotNone(customization_event)
        assert settings_event is not None
        assert customization_event is not None
        self.assertEqual(settings_event["title"], "UNREAL ACTION RECEIVED")
        self.assertEqual(settings_event["group"], "Settings")
        self.assertEqual(settings_event["item"], "reset_settings_defaults")
        self.assertEqual(settings_event["effect"], "Settings defaults restored")
        self.assertEqual(customization_event["group"], "Customization")
        self.assertEqual(
            customization_event["item"],
            "reset_customization_defaults",
        )
        self.assertEqual(
            customization_event["effect"],
            "Customization defaults restored",
        )

    def test_unreal_mic_level_action_visual_events_are_explicit(self) -> None:
        harness = _UnrealIndicatorHarness()
        showing_event = harness._build_unreal_settings_visual_event(
            {"type": "settings_action", "action": "mic_level_is_showing"},
            {},
            {},
        )
        hidden_event = harness._build_unreal_settings_visual_event(
            {"type": "settings_action", "action": "mic_level_not_showing"},
            {},
            {},
        )

        self.assertIsNotNone(showing_event)
        self.assertIsNotNone(hidden_event)
        assert showing_event is not None
        assert hidden_event is not None
        self.assertEqual(showing_event["group"], "Settings")
        self.assertEqual(showing_event["item"], "mic_level_is_showing")
        self.assertEqual(showing_event["effect"], "Mic level visibility enabled")
        self.assertEqual(hidden_event["item"], "mic_level_not_showing")
        self.assertEqual(hidden_event["effect"], "Mic level visibility disabled")


if __name__ == "__main__":
    unittest.main()
