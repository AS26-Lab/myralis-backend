import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.audio_manager import AudioManager
from core.settings_manager import SettingsManager
from ui.main_window import MIC_LEVEL_SEND_INTERVAL_SECONDS, MainWindow


class AudioDevicesPayloadTests(unittest.TestCase):
    def test_audio_devices_payload_uses_labels_and_option_ids(self) -> None:
        raw_devices = [
            {
                "name": "Micrófono USB",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 16000,
            },
            {
                "name": "Altavoces Realtek",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "Headset Combo",
                "max_input_channels": 1,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.audio_manager.sd",
            SimpleNamespace(query_devices=lambda: raw_devices),
        ):
            audio_manager = AudioManager(SettingsManager(Path(temp_dir)))
            payload = audio_manager.build_audio_devices_payload()

        self.assertEqual(payload["type"], "audio_devices")
        self.assertEqual(
            payload["input_labels"],
            ["Predeterminado", "Micrófono USB", "Headset Combo"],
        )
        self.assertEqual(payload["input_ids"], ["default", "device_0", "device_2"])
        self.assertEqual(
            payload["output_labels"],
            ["Predeterminado", "Altavoces Realtek", "Headset Combo"],
        )
        self.assertEqual(payload["output_ids"], ["default", "device_1", "device_2"])


class MicLevelPayloadTests(unittest.TestCase):
    def _window_stub(
        self,
        *,
        interaction_mode: str = "voice",
        deepgram_listening: bool = False,
        input_monitor_active: bool = True,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            settings_manager=SimpleNamespace(
                get_settings=lambda: {"interaction_mode": interaction_mode}
            ),
            deepgram_stt_manager=SimpleNamespace(
                is_listening=lambda: deepgram_listening
            ),
            audio_manager=SimpleNamespace(
                is_input_level_monitor_active=lambda: input_monitor_active
            ),
            _interaction_mode=lambda settings: settings.get(
                "interaction_mode",
                "voice",
            ),
            _last_mic_level_send_time=0.0,
        )

    def test_mic_level_is_sent_only_with_voice_capture_and_is_clamped(self) -> None:
        window = self._window_stub()

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.time.time",
            return_value=1.0,
        ), patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            MainWindow._send_mic_level_if_active(window, 2.4)

        send_json.assert_called_once_with({"type": "mic_level", "level": 1.0})
        self.assertEqual(window._last_mic_level_send_time, 1.0)

    def test_mic_level_is_not_sent_in_text_mode(self) -> None:
        window = self._window_stub(interaction_mode="text")

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.send_json_to_unreal_threadsafe"
        ) as send_json:
            MainWindow._send_mic_level_if_active(window, 0.5)

        self.assertFalse(send_json.called)

    def test_mic_level_is_not_sent_without_active_capture(self) -> None:
        window = self._window_stub(
            deepgram_listening=False,
            input_monitor_active=False,
        )

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.send_json_to_unreal_threadsafe"
        ) as send_json:
            MainWindow._send_mic_level_if_active(window, 0.5)

        self.assertFalse(send_json.called)

    def test_mic_level_is_throttled_to_about_twenty_hz(self) -> None:
        window = self._window_stub()
        window._last_mic_level_send_time = 10.0

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.time.time",
            return_value=10.0 + MIC_LEVEL_SEND_INTERVAL_SECONDS - 0.001,
        ), patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            MainWindow._send_mic_level_if_active(window, 0.5)

        self.assertFalse(send_json.called)

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.time.time",
            return_value=10.0 + MIC_LEVEL_SEND_INTERVAL_SECONDS,
        ), patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            MainWindow._send_mic_level_if_active(window, 0.42)

        send_json.assert_called_once_with({"type": "mic_level", "level": 0.42})


if __name__ == "__main__":
    unittest.main()
