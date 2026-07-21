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
        ), patch(
            "core.audio_manager.AudioManager._windows_active_capture_device_names",
            return_value=set(),
        ):
            audio_manager = AudioManager(SettingsManager(Path(temp_dir)))
            payload = audio_manager.build_audio_devices_payload()

        self.assertEqual(payload["type"], "audio_devices")
        self.assertEqual(
            payload["input_labels"],
            ["Predeterminado", "Micrófono USB", "Headset Combo"],
        )
        self.assertEqual(payload["input_ids"], ["default", "device_0", "device_2"])
        self.assertEqual(payload["saved_input_device_id"], "default")
        self.assertEqual(
            payload["output_labels"],
            ["Predeterminado", "Altavoces Realtek", "Headset Combo"],
        )
        self.assertEqual(payload["output_ids"], ["default", "device_1", "device_2"])

    def test_audio_devices_payload_collapses_duplicate_input_names(self) -> None:
        raw_devices = [
            {
                "name": "Microfono Realtek",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 16000,
            },
            {
                "name": "Microfono Realtek",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
            },
            {
                "name": "Speakers",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.audio_manager.sd",
            SimpleNamespace(query_devices=lambda: raw_devices),
        ), patch(
            "core.audio_manager.AudioManager._windows_active_capture_device_names",
            return_value=set(),
        ):
            audio_manager = AudioManager(SettingsManager(Path(temp_dir)))
            payload = audio_manager.build_audio_devices_payload()

        self.assertEqual(
            payload["input_labels"],
            ["Predeterminado", "Microfono Realtek"],
        )
        self.assertEqual(payload["input_ids"], ["default", "device_1"])
        self.assertEqual(payload["saved_input_device_id"], "default")

    def test_windows_active_capture_names_gate_input_devices(self) -> None:
        raw_devices = [
            {
                "name": "Mic Realtek",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 44100,
            },
            {
                "name": "Mic USB",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
            },
            {
                "name": "Mic Virtual",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 16000,
            },
            {
                "name": "Speakers",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.audio_manager.sd",
            SimpleNamespace(query_devices=lambda: raw_devices),
        ), patch(
            "core.audio_manager.sys.platform",
            "win32",
        ), patch(
            "core.audio_manager.AudioManager._windows_active_capture_device_names",
            return_value={"mic realtek", "mic usb"},
        ):
            audio_manager = AudioManager(SettingsManager(Path(temp_dir)))
            payload = audio_manager.build_audio_devices_payload()

        self.assertEqual(
            payload["input_labels"],
            ["Predeterminado", "Mic Realtek", "Mic USB"],
        )
        self.assertEqual(payload["input_ids"], ["default", "device_0", "device_1"])
        self.assertEqual(payload["saved_input_device_id"], "default")

    def test_windows_device_state_is_active_uses_bitmask(self) -> None:
        self.assertTrue(AudioManager._is_active_windows_device_state(None, 0x1))
        self.assertTrue(AudioManager._is_active_windows_device_state(None, 0x10000001))
        self.assertFalse(AudioManager._is_active_windows_device_state(None, 0x4))

    def test_windows_prefers_wasapi_hostapi_for_input_devices(self) -> None:
        raw_devices = [
            {
                "name": "Mic Realtek",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 44100,
                "hostapi": 0,
            },
            {
                "name": "Mic Realtek",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
                "hostapi": 1,
            },
        ]
        raw_hostapis = [
            {"name": "Windows MME"},
            {"name": "Windows WASAPI"},
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.audio_manager.sd",
            SimpleNamespace(
                query_devices=lambda: raw_devices,
                query_hostapis=lambda: raw_hostapis,
            ),
        ), patch(
            "core.audio_manager.sys.platform",
            "win32",
        ), patch(
            "core.audio_manager.AudioManager._windows_active_capture_device_names",
            return_value={"mic realtek"},
        ):
            audio_manager = AudioManager(SettingsManager(Path(temp_dir)))
            payload = audio_manager.build_audio_devices_payload()

        self.assertEqual(payload["input_labels"], ["Predeterminado", "Mic Realtek"])
        self.assertEqual(payload["input_ids"], ["default", "device_1"])
        self.assertEqual(payload["saved_input_device_id"], "default")

    def test_saved_input_device_id_is_persisted_in_devices(self) -> None:
        raw_devices = [
            {
                "name": "Mic USB",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
                "hostapi": 1,
            }
        ]
        raw_hostapis = [
            {"name": "Windows MME"},
            {"name": "Windows WASAPI"},
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.audio_manager.sd",
            SimpleNamespace(
                query_devices=lambda: raw_devices,
                query_hostapis=lambda: raw_hostapis,
            ),
        ), patch(
            "core.audio_manager.sys.platform",
            "win32",
        ), patch(
            "core.audio_manager.AudioManager._windows_active_capture_device_names",
            return_value={"mic usb"},
        ):
            settings_manager = SettingsManager(Path(temp_dir))
            audio_manager = AudioManager(settings_manager)
            audio_manager.save_input_device_option_id("device_0")
            devices = settings_manager.get_devices()

        self.assertEqual(devices["saved_input_device_id"], "device_0")

    def test_input_level_monitor_falls_back_to_device_default_sample_rate(self) -> None:
        raw_devices = [
            {
                "name": "Mic Realtek",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
            }
        ]

        class FakeStream:
            def __init__(self, samplerate: int) -> None:
                self.samplerate = samplerate
                self.started = False

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def close(self) -> None:
                self.started = False

        def input_stream_factory(**kwargs: object) -> FakeStream:
            samplerate = int(kwargs["samplerate"])
            if samplerate == 16000:
                raise ValueError("Invalid sample rate")
            if samplerate != 48000:
                raise AssertionError(f"Unexpected sample rate: {samplerate}")
            return FakeStream(samplerate)

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.audio_manager.sd",
            SimpleNamespace(
                query_devices=lambda: raw_devices,
                InputStream=input_stream_factory,
            ),
        ), patch(
            "core.audio_manager.AudioManager._windows_active_capture_device_names",
            return_value=set(),
        ):
            audio_manager = AudioManager(SettingsManager(Path(temp_dir)))
            started = audio_manager.start_input_level_monitor(
                input_device_index=0,
                sample_rate=16000,
                input_volume=0.75,
            )

        self.assertTrue(started)
        self.assertEqual(audio_manager._level_stream_sample_rate, 48000)
        self.assertTrue(audio_manager.is_input_level_monitor_active())


class MicLevelPayloadTests(unittest.TestCase):
    def _window_stub(
        self,
        *,
        interaction_mode: str = "voice",
        deepgram_listening: bool = False,
        input_monitor_active: bool = True,
        send_mic_level: bool = False,
        mic_level_show: bool = False,
        force_send_mic_level: bool = False,
        settings_is_open: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            settings_manager=SimpleNamespace(
                get_settings=lambda: {"interaction_mode": interaction_mode}
            ),
            conversation_manager=SimpleNamespace(
                should_send_mic_level=lambda: send_mic_level,
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
            _mic_level_show=mic_level_show,
            _force_send_mic_level=force_send_mic_level,
            _settings_is_open=settings_is_open,
        )

    def test_mic_level_is_not_sent_when_settings_open_but_hidden(self) -> None:
        window = self._window_stub(send_mic_level=True, settings_is_open=True)

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.time.time",
            return_value=1.0,
        ), patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            MainWindow._send_mic_level_if_active(window, 2.4)

        self.assertFalse(send_json.called)
        self.assertEqual(window._last_mic_level_send_time, 0.0)

    def test_mic_level_show_flag_is_sent_and_level_is_clamped(self) -> None:
        window = self._window_stub(
            send_mic_level=True,
            mic_level_show=True,
            settings_is_open=True,
        )

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.time.time",
            return_value=1.0,
        ), patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            MainWindow._send_mic_level_if_active(window, 2.4)

        send_json.assert_called_once_with(
            {"type": "mic_level", "show": True, "level": 1.0}
        )
        self.assertEqual(window._last_mic_level_send_time, 1.0)

    def test_mic_level_is_not_sent_by_default(self) -> None:
        window = self._window_stub()

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.send_json_to_unreal_threadsafe"
        ) as send_json:
            MainWindow._send_mic_level_if_active(window, 0.5)

        self.assertFalse(send_json.called)

    def test_mic_level_force_toggle_overrides_normal_rules(self) -> None:
        window = self._window_stub(
            force_send_mic_level=True,
            interaction_mode="text",
            deepgram_listening=False,
            input_monitor_active=False,
            settings_is_open=False,
            mic_level_show=False,
        )

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.time.time",
            return_value=1.0,
        ), patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            MainWindow._send_mic_level_if_active(window, 0.42)

        send_json.assert_called_once_with(
            {"type": "mic_level", "show": False, "level": 0.42}
        )
        self.assertEqual(window._last_mic_level_send_time, 1.0)

    def test_mic_level_is_not_sent_in_text_mode(self) -> None:
        window = self._window_stub(
            interaction_mode="text",
            send_mic_level=True,
            settings_is_open=True,
        )

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.send_json_to_unreal_threadsafe"
        ) as send_json:
            MainWindow._send_mic_level_if_active(window, 0.5)

        self.assertFalse(send_json.called)

    def test_mic_level_is_not_sent_without_active_capture(self) -> None:
        window = self._window_stub(
            deepgram_listening=False,
            input_monitor_active=False,
            send_mic_level=True,
            settings_is_open=True,
        )

        with patch("ui.main_window.has_websocket_client", return_value=True), patch(
            "ui.main_window.send_json_to_unreal_threadsafe"
        ) as send_json:
            MainWindow._send_mic_level_if_active(window, 0.5)

        self.assertFalse(send_json.called)

    def test_mic_level_is_throttled_to_about_twenty_hz(self) -> None:
        window = self._window_stub(
            send_mic_level=True,
            mic_level_show=True,
            settings_is_open=True,
        )
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

        send_json.assert_called_once_with(
            {"type": "mic_level", "show": True, "level": 0.42}
        )


if __name__ == "__main__":
    unittest.main()
