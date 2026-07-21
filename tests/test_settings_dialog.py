import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ui.main_window import MainWindow
from ui.settings_dialog import SettingsDialog


class SettingsDialogTests(unittest.TestCase):
    def test_selected_websocket_audio_chunk_ms_accepts_custom_value(self) -> None:
        dialog = SimpleNamespace(
            websocket_audio_chunk_spin=SimpleNamespace(
                value=Mock(return_value=137)
            )
        )

        self.assertEqual(SettingsDialog._selected_websocket_audio_chunk_ms(dialog), 137)

    def test_clean_websocket_audio_chunk_ms_clamps_to_safe_range(self) -> None:
        self.assertEqual(SettingsDialog._clean_websocket_audio_chunk_ms(0), 1)
        self.assertEqual(SettingsDialog._clean_websocket_audio_chunk_ms(5000), 1000)
        self.assertEqual(SettingsDialog._clean_websocket_audio_chunk_ms("bad"), 200)

    def test_selected_startup_silence_chunks_and_fade_in_ms(self) -> None:
        dialog = SimpleNamespace(
            websocket_audio_start_silence_spin=SimpleNamespace(
                value=Mock(return_value=2)
            ),
            websocket_audio_fade_in_spin=SimpleNamespace(
                value=Mock(return_value=15)
            ),
        )

        self.assertEqual(
            SettingsDialog._selected_websocket_audio_start_silence_chunks(dialog),
            2,
        )
        self.assertEqual(
            SettingsDialog._selected_websocket_audio_fade_in_ms(dialog),
            15,
        )

    def test_clean_startup_silence_and_fade_in_ranges(self) -> None:
        self.assertEqual(
            SettingsDialog._clean_websocket_audio_start_silence_chunks(-1),
            0,
        )
        self.assertEqual(
            SettingsDialog._clean_websocket_audio_start_silence_chunks(99),
            10,
        )
        self.assertEqual(
            SettingsDialog._clean_websocket_audio_fade_in_ms(-1),
            0,
        )
        self.assertEqual(
            SettingsDialog._clean_websocket_audio_fade_in_ms(999),
            250,
        )

    def test_backend_ui_refresh_button_saves_current_tokens_and_emits_signal(self) -> None:
        dialog = SimpleNamespace(
            _loading=False,
            test_miralys_tokens_spin=SimpleNamespace(interpretText=Mock()),
            test_miralys_tokens_used_spin=SimpleNamespace(interpretText=Mock()),
            _save_settings=Mock(),
            backend_ui_refresh_requested=SimpleNamespace(emit=Mock()),
        )

        SettingsDialog._request_backend_ui_refresh(dialog)

        dialog.test_miralys_tokens_spin.interpretText.assert_called_once_with()
        dialog.test_miralys_tokens_used_spin.interpretText.assert_called_once_with()
        dialog._save_settings.assert_called_once_with()
        dialog.backend_ui_refresh_requested.emit.assert_called_once_with()

    def test_out_of_credits_toggle_emits_signal(self) -> None:
        dialog = SimpleNamespace(
            out_of_credits_test_toggled=SimpleNamespace(emit=Mock()),
        )

        SettingsDialog._handle_out_of_credits_test_clicked(dialog, True)

        dialog.out_of_credits_test_toggled.emit.assert_called_once_with(True)

    def test_open_settings_connects_backend_ui_refresh_to_backend_snapshot(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.settings_manager = object()
        window.audio_manager = object()
        window._test_websocket_start_end = Mock()
        window._test_elevenlabs_streaming = Mock()
        window._test_runtime_lip_sync = Mock()
        window._settings_changed = Mock()
        window._send_backend_ui_snapshot = Mock()
        window._handle_out_of_credits_test_toggled = Mock()
        fake_dialog = SimpleNamespace(
            settings_changed=SimpleNamespace(connect=Mock()),
            backend_ui_refresh_requested=SimpleNamespace(connect=Mock()),
            out_of_credits_test_toggled=SimpleNamespace(connect=Mock()),
            exec=Mock(),
        )

        with patch("ui.main_window.SettingsDialog", return_value=fake_dialog):
            MainWindow._open_settings(window)

        fake_dialog.settings_changed.connect.assert_called_once_with(
            window._settings_changed
        )
        fake_dialog.backend_ui_refresh_requested.connect.assert_called_once_with(
            window._send_backend_ui_snapshot
        )
        fake_dialog.out_of_credits_test_toggled.connect.assert_called_once_with(
            window._handle_out_of_credits_test_toggled
        )
        fake_dialog.exec.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
