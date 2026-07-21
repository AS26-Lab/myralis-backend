import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.conversation_manager import ConversationManager
from core.openai_manager import AIResponse
from core.settings_manager import SettingsManager


class _FakeOpenAIManager:
    def generate_response(self, **_: object) -> AIResponse:
        raise AssertionError("OpenAI is outside voice customization tests")


class _RecordingRealtimeElevenLabsManager:
    def __init__(self) -> None:
        self.stream_calls: list[dict[str, object]] = []

    def generate_wav(self, **_: object) -> object:
        raise AssertionError("WAV generation is outside this test")

    def stream_elevenlabs_tts_to_unreal(self, **kwargs: object) -> object:
        self.stream_calls.append(kwargs)
        return SimpleNamespace(audio_path=None)


class _FakeAudioManager:
    def __init__(self, settings_manager: SettingsManager | None = None) -> None:
        self.settings_manager = settings_manager

    def play_audio(self, *_: object) -> None:
        raise AssertionError("Local audio playback is outside this test")


class _FakeTestModeManager:
    def is_enabled(self, settings: dict[str, object] | None = None) -> bool:
        return False

    def is_audio_enabled(self, settings: dict[str, object] | None = None) -> bool:
        return False


class _FakeRuntimeBridge:
    def __init__(self, response_audio_path: Path) -> None:
        self.config = SimpleNamespace(response_audio_path=response_audio_path)

    def set_runtime_state(self, state: str | None = None, mood: str | None = None) -> None:
        _ = state
        _ = mood


class VoiceCustomizationTests(unittest.TestCase):
    def test_voice_updates_are_saved_without_clearing_other_voice_ids(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            settings_manager = SettingsManager(Path(temp_dir))
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = _FakeAudioManager(settings_manager)

            with self.assertLogs("core.conversation_manager", level="INFO") as logs:
                self.assertTrue(
                    manager.handle_unreal_websocket_message(
                        {
                            "type": "settings_update",
                            "setting": "custom_voice_id",
                            "value_type": "string",
                            "value": "manual-voice",
                        }
                    )
                )
                self.assertTrue(
                    manager.handle_unreal_websocket_message(
                        {
                            "type": "settings_update",
                            "setting": "voice_id",
                            "value_type": "string",
                            "value": "selected-voice",
                        }
                    )
                )
                self.assertTrue(
                    manager.handle_unreal_websocket_message(
                        {
                            "type": "settings_update",
                            "setting": "use_custom_voice",
                            "value_type": "bool",
                            "value": True,
                        }
                    )
                )
                self.assertTrue(
                    manager.handle_unreal_websocket_message(
                        {
                            "type": "settings_update",
                            "setting": "use_custom_voice",
                            "value_type": "bool",
                            "value": False,
                        }
                    )
                )

            customization = settings_manager.get_settings()["customization"]
            self.assertEqual(customization["voice_id"], "selected-voice")
            self.assertEqual(customization["custom_voice_id"], "manual-voice")
            self.assertFalse(customization["use_custom_voice"])
            output = "\n".join(logs.output)
            self.assertIn("Custom voice id updated: manual-voice", output)
            self.assertIn("Voice selection updated: voice_id=selected-voice", output)
            self.assertIn("Custom voice enabled: true", output)
            self.assertIn("Custom voice enabled: false", output)
        finally:
            shutil.rmtree(temp_dir)

    def test_final_voice_uses_selected_voice_when_custom_voice_is_disabled(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        settings = {
            "elevenlabs": {"voice_id": "legacy-voice"},
            "customization": {
                "voice_id": "selected-voice",
                "use_custom_voice": False,
                "custom_voice_id": "manual-voice",
            },
        }

        with self.assertLogs("core.conversation_manager", level="INFO") as logs:
            final_voice_id = manager._resolve_final_voice_id(settings)

        self.assertEqual(final_voice_id, "selected-voice")
        self.assertIn("Final voice resolved: selected-voice", "\n".join(logs.output))

    def test_final_voice_uses_custom_voice_when_enabled(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        settings = {
            "elevenlabs": {"voice_id": "legacy-voice"},
            "customization": {
                "voice_id": "selected-voice",
                "use_custom_voice": True,
                "custom_voice_id": "manual-voice",
            },
        }

        final_voice_id = manager._resolve_final_voice_id(settings)

        self.assertEqual(final_voice_id, "manual-voice")

    def test_empty_custom_voice_id_falls_back_safely_when_custom_voice_enabled(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        settings = {
            "elevenlabs": {"voice_id": "legacy-voice"},
            "customization": {
                "voice_id": "selected-voice",
                "use_custom_voice": True,
                "custom_voice_id": "",
            },
        }

        with self.assertLogs("core.conversation_manager", level="INFO") as logs:
            final_voice_id = manager._resolve_final_voice_id(settings)

        output = "\n".join(logs.output)
        self.assertEqual(final_voice_id, "selected-voice")
        self.assertIn("Custom voice enabled but custom_voice_id is invalid", output)
        self.assertIn("Final voice resolved: selected-voice", output)

    def test_invalid_custom_voice_id_falls_back_safely_when_custom_voice_enabled(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        settings = {
            "elevenlabs": {"voice_id": "legacy-voice"},
            "customization": {
                "voice_id": "selected-voice",
                "use_custom_voice": True,
                "custom_voice_id": "Eleven Labs Voicfjfjfge ID",
            },
        }

        with self.assertLogs("core.conversation_manager", level="INFO") as logs:
            final_voice_id = manager._resolve_final_voice_id(settings)

        output = "\n".join(logs.output)
        self.assertEqual(final_voice_id, "selected-voice")
        self.assertIn("Custom voice enabled but custom_voice_id is invalid", output)
        self.assertIn("Final voice resolved: selected-voice", output)

    def test_realtime_tts_receives_final_voice_without_changing_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = SettingsManager(Path(temp_dir))
            elevenlabs_manager = _RecordingRealtimeElevenLabsManager()
            manager = ConversationManager(
                openai_manager=_FakeOpenAIManager(),
                elevenlabs_manager=elevenlabs_manager,
                audio_manager=_FakeAudioManager(settings_manager),
                test_mode_manager=_FakeTestModeManager(),
                runtime_bridge=_FakeRuntimeBridge(Path(temp_dir) / "current_response.wav"),
            )
            manager._use_websocket_runtime_state = False
            manager.post_talking_idle_delay_seconds = 0.0
            settings = {
                "elevenlabs": {
                    "voice_id": "legacy-voice",
                    "model_id": "eleven_turbo_v2_5",
                    "output_format": "pcm_24000",
                    "save_response_wav": False,
                    "websocket_audio_chunk_ms": 200,
                    "websocket_audio_realtime_pacing": True,
                    "voice_speed": "normal",
                },
                "customization": {
                    "voice_id": "selected-voice",
                    "use_custom_voice": True,
                    "custom_voice_id": "manual-voice",
                },
            }

            try:
                manager._stream_realtime_tts_to_unreal(
                    response_text="hola",
                    mood="Neutral",
                    settings=settings,
                    state_callback=None,
                    thinking_started_at=0.0,
                )
            finally:
                manager.shutdown()

        self.assertEqual(len(elevenlabs_manager.stream_calls), 1)
        call = elevenlabs_manager.stream_calls[0]
        self.assertEqual(call["voice_id"], "manual-voice")
        self.assertEqual(call["model_id"], "eleven_turbo_v2_5")


if __name__ == "__main__":
    unittest.main()
