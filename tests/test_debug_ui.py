import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.conversation_manager import ConversationManager
from ui.main_window import WindowsDebugHotkeyFilter
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
    def test_global_debug_hotkey_is_ctrl_shift_d_without_alt(self) -> None:
        self.assertEqual(WindowsDebugHotkeyFilter.VK_D, 0x44)
        modifiers = (
            WindowsDebugHotkeyFilter.MOD_CONTROL
            | WindowsDebugHotkeyFilter.MOD_SHIFT
            | WindowsDebugHotkeyFilter.MOD_NOREPEAT
        )
        self.assertEqual(modifiers & WindowsDebugHotkeyFilter.MOD_CONTROL, 0x0002)
        self.assertEqual(modifiers & WindowsDebugHotkeyFilter.MOD_SHIFT, 0x0004)
        self.assertEqual(modifiers & 0x0001, 0)

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

        user32.RegisterHotKey.assert_called_once_with(
            12345,
            WindowsDebugHotkeyFilter.HOTKEY_ID,
            (
                WindowsDebugHotkeyFilter.MOD_CONTROL
                | WindowsDebugHotkeyFilter.MOD_SHIFT
                | WindowsDebugHotkeyFilter.MOD_NOREPEAT
            ),
            WindowsDebugHotkeyFilter.VK_D,
        )
        user32.UnregisterHotKey.assert_called_once_with(
            12345,
            WindowsDebugHotkeyFilter.HOTKEY_ID,
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


if __name__ == "__main__":
    unittest.main()
