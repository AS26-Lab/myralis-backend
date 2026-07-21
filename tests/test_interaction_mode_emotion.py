import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.conversation_manager import AssistantState, ConversationManager
from core.openai_manager import AIResponse
from core.settings_manager import SettingsManager


class _RecordingOpenAIManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_response(self, **kwargs: object) -> AIResponse:
        self.calls.append(kwargs)
        return AIResponse(
            text="Entiendo la entrada y sigo con la respuesta.",
            emotion="Neutral",
            raw_text="Entiendo la entrada y sigo con la respuesta.",
            model="test",
        )


class _WavElevenLabsManager:
    def __init__(self, audio_path: Path) -> None:
        self.audio_path = audio_path

    def generate_wav(self, **_: object) -> object:
        return SimpleNamespace(audio_path=self.audio_path)

    def stream_elevenlabs_tts_to_unreal(self, **_: object) -> object:
        raise AssertionError("Realtime TTS is outside this test")


class _FakeAudioManager:
    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager

    def play_audio(self, *_: object) -> None:
        raise AssertionError("Local playback must not be used")


class _FakeTestModeManager:
    def is_enabled(self, settings: dict[str, object] | None = None) -> bool:
        return False

    def is_audio_enabled(self, settings: dict[str, object] | None = None) -> bool:
        return False


class _FakeRuntimeBridge:
    def __init__(self, response_audio_path: Path) -> None:
        self.config = SimpleNamespace(response_audio_path=response_audio_path)
        self.states: list[tuple[str | None, str | None]] = []

    def set_state(self, state: str) -> None:
        self.states.append((state, None))

    def set_runtime_state(
        self,
        state: str | None = None,
        mood: str | None = None,
    ) -> None:
        self.states.append((state, mood))

    def publish_response_audio(self, audio_path: Path) -> Path:
        destination = self.config.response_audio_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if audio_path.resolve() != destination.resolve():
            shutil.copyfile(audio_path, destination)
        return destination


class InteractionModeEmotionTests(unittest.TestCase):
    def _settings_manager(self, temp_path: Path, mode: str) -> SettingsManager:
        settings_manager = SettingsManager(temp_path)
        settings_manager.apply_official_setting_update("interaction_mode", mode)
        settings_manager.apply_official_setting_update("tts_realtime", False)
        settings_manager.apply_official_setting_update(
            "listening_emotion_analysis",
            True,
        )
        return settings_manager

    def _manager(
        self,
        temp_path: Path,
        settings_manager: SettingsManager,
        generated_wav: Path,
    ) -> ConversationManager:
        manager = ConversationManager(
            openai_manager=_RecordingOpenAIManager(),
            elevenlabs_manager=_WavElevenLabsManager(generated_wav),
            audio_manager=_FakeAudioManager(settings_manager),
            test_mode_manager=_FakeTestModeManager(),
            runtime_bridge=_FakeRuntimeBridge(temp_path / "current_response.wav"),
        )
        manager.post_talking_idle_delay_seconds = 0.0
        return manager

    def test_text_mode_text_input_enters_thinking_but_skips_emotion_analysis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            generated_wav = temp_path / "generated.wav"
            generated_wav.write_bytes(b"RIFF")
            settings_manager = self._settings_manager(temp_path, "text")
            manager = self._manager(temp_path, settings_manager, generated_wav)
            states: list[AssistantState] = []
            sent_payloads: list[dict[str, object]] = []

            def capture_payload(
                payload: dict[str, object],
                timeout: float = 1.0,
            ) -> bool:
                _ = timeout
                sent_payloads.append(payload)
                return True

            try:
                with self.assertLogs("core.conversation_manager", level="INFO") as logs, patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    side_effect=capture_payload,
                ):
                    result = manager.process_user_message(
                        "texto de prueba",
                        settings_manager.get_settings(),
                        state_callback=states.append,
                    )
                with patch(
                    "core.conversation_manager.time.sleep",
                    return_value=None,
                ), patch(
                    "core.conversation_manager.threading.Thread",
                    side_effect=lambda *args, **kwargs: SimpleNamespace(
                        start=lambda: kwargs["target"](*kwargs.get("args", ()))
                    ),
                ), patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    side_effect=capture_payload,
                ):
                    manager.handle_unreal_websocket_message(
                        {"type": "settings_action", "action": "audio_finished"}
                    )
            finally:
                manager.shutdown()

        self.assertEqual(result.response.text, "Entiendo la entrada y sigo con la respuesta.")
        self.assertIn(AssistantState.THINKING, states)
        self.assertIn(AssistantState.TALKING, states)
        self.assertIn(AssistantState.IDLE, states)
        self.assertTrue(
            any("Listening emotion analysis skipped: text mode" in line for line in logs.output)
        )
        thinking_payloads = [
            payload
            for payload in sent_payloads
            if payload.get("type") == "runtime_state"
            and payload.get("state") == "thinking"
        ]
        self.assertTrue(thinking_payloads)
        self.assertTrue(
            all(payload["audio_mode"] == "none" for payload in thinking_payloads)
        )

    def test_text_mode_partial_stt_does_not_run_listening_emotion_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = self._settings_manager(Path(temp_dir), "text")
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = _FakeAudioManager(settings_manager)
            manager._current_state = AssistantState.LISTENING

            with self.assertLogs("core.conversation_manager", level="INFO") as logs, patch(
                "core.conversation_manager.send_json_to_unreal_blocking"
            ) as send_json:
                manager.handle_partial_stt_transcript(
                    "no tengo claro esto es confuso tengo duda suficiente"
                )

        self.assertFalse(send_json.called)
        self.assertTrue(
            any("Listening emotion analysis skipped: text mode" in line for line in logs.output)
        )

    def test_voice_mode_partial_stt_can_send_listening_emotion_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = self._settings_manager(Path(temp_dir), "voice")
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = _FakeAudioManager(settings_manager)
            manager.runtime_bridge = _FakeRuntimeBridge(Path(temp_dir) / "current_response.wav")
            manager._current_state = AssistantState.LISTENING
            manager.current_mood = "Neutral"
            manager.last_listening_mood = "Neutral"
            manager.last_listening_emotion_strength = 0.30
            manager.last_emotion_analysis_time = 0.0
            manager.last_mood_change_time = 0.0
            manager.emotion_analysis_interval_seconds = 1.5
            manager.min_words_for_emotion_analysis = 6
            manager.min_seconds_between_mood_changes = 2.0
            manager.min_strength_delta_to_send = 0.15
            manager._use_websocket_runtime_state = True
            manager._active_audio_mode = "realtime"
            manager._active_response_id = 123
            sent_payloads: list[dict[str, object]] = []

            def capture_payload(
                payload: dict[str, object],
                timeout: float = 1.0,
            ) -> bool:
                _ = timeout
                sent_payloads.append(payload)
                return True

            with patch("core.conversation_manager.time.time", return_value=10.0), patch(
                "core.conversation_manager.send_json_to_unreal_blocking",
                side_effect=capture_payload,
            ):
                manager.handle_partial_stt_transcript(
                    "no tengo claro esto es confuso tengo duda suficiente"
                )

        self.assertTrue(sent_payloads)
        payload = sent_payloads[0]
        self.assertEqual(payload["type"], "runtime_state")
        self.assertEqual(payload["state"], "listening")
        self.assertEqual(payload["mood"], "Confused")
        self.assertEqual(payload["audio_mode"], "none")
        self.assertEqual(payload["response_id"], 123)
        self.assertEqual(payload["emotion_strength"], 0.55)

    def test_voice_mode_partial_stt_respects_analysis_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = self._settings_manager(Path(temp_dir), "voice")
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = _FakeAudioManager(settings_manager)
            manager.runtime_bridge = _FakeRuntimeBridge(Path(temp_dir) / "current_response.wav")
            manager._current_state = AssistantState.LISTENING
            manager.current_mood = "Neutral"
            manager.last_listening_mood = "Neutral"
            manager.last_listening_emotion_strength = 0.30
            manager.last_emotion_analysis_time = 10.0
            manager.last_mood_change_time = 0.0
            manager.emotion_analysis_interval_seconds = 1.5
            manager.min_words_for_emotion_analysis = 6
            manager.min_seconds_between_mood_changes = 2.0
            manager.min_strength_delta_to_send = 0.15
            manager._use_websocket_runtime_state = True
            manager._active_audio_mode = "realtime"
            manager._active_response_id = 123

            with self.assertLogs("core.conversation_manager", level="INFO") as logs, patch(
                "core.conversation_manager.time.time",
                return_value=11.0,
            ), patch("core.conversation_manager.send_json_to_unreal_blocking") as send_json:
                manager.handle_partial_stt_transcript(
                    "no tengo claro esto es confuso tengo duda suficiente"
                )

        self.assertFalse(send_json.called)
        self.assertTrue(
            any("Listening emotion analysis skipped: cooldown active" in line for line in logs.output)
        )


if __name__ == "__main__":
    unittest.main()
