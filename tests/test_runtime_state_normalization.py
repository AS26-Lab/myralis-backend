import threading
import tempfile
import unittest
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.conversation_manager import (
    AssistantState,
    ConversationManager,
    normalize_mood,
    normalize_runtime_audio_mode,
    normalize_runtime_state,
)
from core.audio_manager import AudioManager
from core.mood import (
    ALLOWED_RUNTIME_MOODS,
    EMOTION_STRENGTH_BY_MOOD,
    get_emotion_strength_for_mood,
    get_random_debug_mood,
)
from core.websocket_server import UnrealWebSocketServer
from core.settings_manager import SettingsManager


class _PaidOpenAIManager:
    def generate_response(self, **_: object) -> object:
        raise AssertionError("OpenAI must not be called in debug flow")


class _PaidElevenLabsManager:
    def generate_wav(self, **_: object) -> object:
        raise AssertionError("ElevenLabs WAV must not be called in debug flow")

    def stream_elevenlabs_tts_to_unreal(self, **_: object) -> object:
        raise AssertionError("ElevenLabs streaming must not be called in debug flow")


class _WavElevenLabsManager:
    def __init__(self, audio_path: Path) -> None:
        self.audio_path = audio_path
        self.generate_calls: list[dict[str, object]] = []

    def generate_wav(self, **kwargs: object) -> object:
        self.generate_calls.append(kwargs)
        return SimpleNamespace(audio_path=self.audio_path)

    def stream_elevenlabs_tts_to_unreal(self, **_: object) -> object:
        raise AssertionError("Realtime streaming must not be called in WAV mode")


class _RealtimeElevenLabsManager:
    def __init__(self) -> None:
        self.stream_calls: list[dict[str, object]] = []

    def generate_wav(self, **_: object) -> object:
        raise AssertionError("WAV generation must not be called in realtime mode")

    def stream_elevenlabs_tts_to_unreal(self, **kwargs: object) -> object:
        self.stream_calls.append(kwargs)
        on_audio_start = kwargs.get("on_audio_start")
        if callable(on_audio_start):
            on_audio_start()
        return SimpleNamespace(audio_path=None)


class _FakeAudioManager:
    settings_manager = SimpleNamespace(get_devices=lambda: {})

    def play_audio(self, *_: object) -> None:
        raise AssertionError("Local audio playback must not be called in debug flow")


class _FakeTestModeManager:
    def is_enabled(self, settings: dict[str, object] | None = None) -> bool:
        return False

    def is_audio_enabled(self, settings: dict[str, object] | None = None) -> bool:
        return False


class _FakeRuntimeBridge:
    def __init__(self, response_audio_path: Path) -> None:
        self.config = SimpleNamespace(response_audio_path=response_audio_path)
        self.states: list[tuple[str | None, str | None]] = []

    def set_runtime_state(
        self,
        state: str | None = None,
        mood: str | None = None,
    ) -> None:
        self.states.append((state, mood))

    def set_state(self, state: str) -> None:
        self.states.append((state, None))

    def publish_response_audio(self, audio_path: Path) -> Path:
        destination = self.config.response_audio_path
        if audio_path.resolve() != destination.resolve():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audio_path, destination)
        return destination


class RuntimeStateNormalizationTests(unittest.TestCase):
    def _build_manager(self, response_audio_path: Path) -> ConversationManager:
        manager = ConversationManager(
            openai_manager=_PaidOpenAIManager(),
            elevenlabs_manager=_PaidElevenLabsManager(),
            audio_manager=_FakeAudioManager(),
            test_mode_manager=_FakeTestModeManager(),
            runtime_bridge=_FakeRuntimeBridge(response_audio_path),
        )
        manager.post_talking_idle_delay_seconds = 0.0
        return manager

    def test_mood_case_normalization_for_unreal_switches(self) -> None:
        self.assertEqual(normalize_mood("happy"), "Happy")
        self.assertEqual(normalize_mood("anger"), "Anger")
        self.assertEqual(normalize_mood(" Surprise "), "Surprise")

    def test_unknown_moods_fall_back_to_neutral(self) -> None:
        self.assertEqual(normalize_mood(None), "Neutral")
        self.assertEqual(normalize_mood(""), "Neutral")
        self.assertEqual(normalize_mood("Angry"), "Anger")
        self.assertEqual(normalize_mood("Focused"), "Neutral")

    def test_emotion_strength_is_float_for_allowed_moods(self) -> None:
        self.assertEqual(set(EMOTION_STRENGTH_BY_MOOD), ALLOWED_RUNTIME_MOODS)
        self.assertEqual(get_emotion_strength_for_mood("Happy"), 0.65)
        self.assertEqual(get_emotion_strength_for_mood("unknown"), 0.30)
        self.assertIsInstance(get_emotion_strength_for_mood("Happy"), float)

    def test_state_aliases_for_unreal_switches(self) -> None:
        self.assertEqual(normalize_runtime_state("TALKING"), "talking")

    def test_debug_audio_mode_is_not_allowed_for_runtime_state(self) -> None:
        self.assertEqual(normalize_runtime_audio_mode("debug"), "none")

    def test_runtime_state_websocket_payload_includes_float_emotion_strength(
        self,
    ) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager._use_websocket_runtime_state = True
        manager._active_audio_mode = "realtime"
        manager._active_response_id = 123

        with patch(
            "core.conversation_manager.send_json_to_unreal_blocking"
        ) as send_json:
            manager._send_runtime_state_over_websocket("TALKING", "happy")

        payload = send_json.call_args.args[0]
        self.assertEqual(payload["type"], "runtime_state")
        self.assertEqual(payload["state"], "talking")
        self.assertEqual(payload["mood"], "Happy")
        self.assertEqual(payload["audio_mode"], "realtime")
        self.assertEqual(payload["response_id"], 123)
        self.assertEqual(payload["emotion_strength"], 0.65)
        self.assertIsInstance(payload["emotion_strength"], float)

    def test_wav_runtime_state_includes_wav_location_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "current_response.wav"
            wav_path.write_bytes(b"RIFF")
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager._use_websocket_runtime_state = True
            manager._active_audio_mode = "wav"
            manager._active_response_id = 124
            manager.runtime_bridge = SimpleNamespace(
                config=SimpleNamespace(response_audio_path=wav_path)
            )

            with patch(
                "core.conversation_manager.send_json_to_unreal_blocking"
            ) as send_json:
                manager._send_runtime_state_over_websocket("TALKING", "Neutral")

        payload = send_json.call_args.args[0]
        self.assertEqual(payload["audio_mode"], "wav")
        self.assertEqual(payload["wav_location"], wav_path.as_posix())

    def test_ai_realtime_processing_default_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._build_manager(Path(temp_dir) / "current_response.wav")
            try:
                self.assertTrue(manager.is_ai_realtime_processing_enabled())
            finally:
                manager.shutdown()

    def test_unreal_message_false_cannot_enable_debug_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._build_manager(Path(temp_dir) / "current_response.wav")
            try:
                handled = manager.handle_unreal_websocket_message(
                    {"type": "ai_realtime_processing", "enabled": False}
                )
                self.assertFalse(handled)
                self.assertTrue(manager.is_ai_realtime_processing_enabled())
            finally:
                manager.shutdown()

    def test_unreal_message_true_updates_flag_to_true(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._build_manager(Path(temp_dir) / "current_response.wav")
            try:
                manager.set_ai_realtime_processing_enabled(False)
                handled = manager.handle_unreal_websocket_message(
                    {"type": "ai_realtime_processing", "enabled": True}
                )
                self.assertTrue(handled)
                self.assertTrue(manager.is_ai_realtime_processing_enabled())
            finally:
                manager.shutdown()

    def test_unreal_message_string_false_cannot_enable_debug_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._build_manager(Path(temp_dir) / "current_response.wav")
            try:
                handled = manager.handle_unreal_websocket_message(
                    {"type": "ai_realtime_processing", "enabled": "false"}
                )
                self.assertFalse(handled)
                self.assertTrue(manager.is_ai_realtime_processing_enabled())
            finally:
                manager.shutdown()

    def test_unreal_message_string_true_updates_flag_to_true(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._build_manager(Path(temp_dir) / "current_response.wav")
            try:
                manager.set_ai_realtime_processing_enabled(False)
                handled = manager.handle_unreal_websocket_message(
                    {"type": "ai_realtime_processing", "enabled": "true"}
                )
                self.assertTrue(handled)
                self.assertTrue(manager.is_ai_realtime_processing_enabled())
            finally:
                manager.shutdown()

    def test_websocket_incoming_json_reaches_registered_handler(self) -> None:
        server = UnrealWebSocketServer()
        received: list[dict[str, object]] = []
        server.set_incoming_json_handler(received.append)

        server._handle_incoming_message(
            '{"type":"ai_realtime_processing","enabled":false}'
        )

        self.assertEqual(
            received,
            [{"type": "ai_realtime_processing", "enabled": False}],
        )

    def test_random_debug_mood_is_allowed(self) -> None:
        mood = get_random_debug_mood()
        self.assertIn(mood, ALLOWED_RUNTIME_MOODS)

    def test_disabled_ai_flow_uses_fake_mood_and_runtime_strength(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._build_manager(Path(temp_dir) / "current_response.wav")
            manager.set_ai_realtime_processing_enabled(False)
            sent_payloads: list[dict[str, object]] = []

            def capture_payload(
                payload: dict[str, object],
                timeout: float = 1.0,
            ) -> bool:
                _ = timeout
                sent_payloads.append(payload)
                return True

            settings = {
                "app": {"use_websocket_runtime_state": True},
                "test_mode": {"enabled": False},
                "elevenlabs": {
                    "websocket_audio_chunk_ms": 200,
                    "websocket_audio_realtime_pacing": True,
                },
            }

            try:
                with patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    side_effect=capture_payload,
                ), patch("core.conversation_manager.stream_wav_to_unreal") as stream_wav:
                    result = manager.process_user_message("hola", settings)
            finally:
                manager.shutdown()

        self.assertIn(result.mood, ALLOWED_RUNTIME_MOODS)
        self.assertIsInstance(get_emotion_strength_for_mood(result.mood), float)
        self.assertFalse(stream_wav.called)

        runtime_payloads = [
            payload
            for payload in sent_payloads
            if payload.get("type") == "runtime_state"
        ]
        self.assertTrue(runtime_payloads)
        self.assertTrue(
            all(payload["audio_mode"] == "none" for payload in runtime_payloads)
        )
        self.assertIn("emotion_strength", runtime_payloads[0])
        self.assertIsInstance(runtime_payloads[0]["emotion_strength"], float)

    def test_wav_mode_sends_location_without_streaming_start_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            generated_wav = temp_path / "generated.wav"
            generated_wav.write_bytes(b"RIFF")
            response_wav = temp_path / "current_response.wav"
            manager = ConversationManager(
                openai_manager=_PaidOpenAIManager(),
                elevenlabs_manager=_WavElevenLabsManager(generated_wav),
                audio_manager=_FakeAudioManager(),
                test_mode_manager=_FakeTestModeManager(),
                runtime_bridge=_FakeRuntimeBridge(response_wav),
            )
            manager.post_talking_idle_delay_seconds = 0.0
            manager._active_response_id = 77
            manager._use_websocket_runtime_state = True
            settings = {
                "app": {"use_websocket_runtime_state": True},
                "elevenlabs": {
                    "voice_id": "voice",
                    "model_id": "eleven_turbo_v2_5",
                    "output_format": "pcm_24000",
                    "voice_speed": "normal",
                },
            }
            sent_payloads: list[dict[str, object]] = []
            wav_exists_before_cleanup = False

            def capture_payload(
                payload: dict[str, object],
                timeout: float = 1.0,
            ) -> bool:
                _ = timeout
                sent_payloads.append(payload)
                return True

            try:
                with patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    side_effect=capture_payload,
                ), patch("core.conversation_manager.stream_wav_to_unreal") as stream_wav:
                    audio_path, _used_cache, talking_started = (
                manager._generate_legacy_wav_and_playback(
                    response_text="hola",
                    mood="Neutral",
                    settings=settings,
                    test_mode_enabled=False,
                    state_callback=None,
                    thinking_started_at=0.0,
                )
                    )
            finally:
                manager.shutdown()

        self.assertTrue(talking_started)
        self.assertEqual(audio_path, response_wav)
        self.assertEqual(
            manager.elevenlabs_manager.generate_calls[0]["output_format"],
            "pcm_16000",
        )
        self.assertFalse(stream_wav.called)
        talking_payloads = [
            payload
            for payload in sent_payloads
            if payload.get("type") == "runtime_state"
            and payload.get("state") == "talking"
        ]
        self.assertTrue(talking_payloads)
        self.assertEqual(talking_payloads[0]["audio_mode"], "wav")
        self.assertEqual(talking_payloads[0]["wav_location"], response_wav.as_posix())

    def test_tts_realtime_true_uses_realtime_audio_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            realtime_manager = _RealtimeElevenLabsManager()
            manager = ConversationManager(
                openai_manager=_PaidOpenAIManager(),
                elevenlabs_manager=realtime_manager,
                audio_manager=_FakeAudioManager(),
                test_mode_manager=_FakeTestModeManager(),
                runtime_bridge=_FakeRuntimeBridge(
                    Path(temp_dir) / "current_response.wav"
                ),
            )
            manager.post_talking_idle_delay_seconds = 0.0
            manager._active_response_id = 123
            settings = {
                "app": {"use_websocket_runtime_state": True},
                "elevenlabs": {
                    "voice_id": "voice",
                    "model_id": "eleven_turbo_v2_5",
                    "output_format": "pcm_24000",
                    "use_realtime_tts_streaming": True,
                    "save_response_wav": False,
                    "websocket_audio_chunk_ms": 200,
                    "websocket_audio_realtime_pacing": True,
                    "voice_speed": "normal",
                },
            }
            sent_payloads: list[dict[str, object]] = []

            def capture_payload(
                payload: dict[str, object],
                timeout: float = 1.0,
            ) -> bool:
                _ = timeout
                sent_payloads.append(payload)
                return True

            try:
                with patch("core.conversation_manager.has_websocket_client", return_value=True), patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    side_effect=capture_payload,
                ):
                    audio_path, _used_cache, errors = manager._handle_tts_and_playback(
                        response_text="hola",
                        mood="Neutral",
                        settings=settings,
                        test_mode_enabled=False,
                        state_callback=None,
                        thinking_started_at=0.0,
                    )
            finally:
                manager.shutdown()

        self.assertIsNone(audio_path)
        self.assertEqual(errors, [])
        self.assertEqual(len(realtime_manager.stream_calls), 1)
        stream_call = realtime_manager.stream_calls[0]
        self.assertEqual(stream_call["output_format"], "pcm_24000")
        self.assertEqual(stream_call["websocket_audio_chunk_ms"], 200)
        self.assertIs(stream_call["websocket_audio_realtime_pacing"], True)
        talking_payloads = [
            payload
            for payload in sent_payloads
            if payload.get("type") == "runtime_state"
            and payload.get("state") == "talking"
        ]
        self.assertTrue(talking_payloads)
        self.assertEqual(talking_payloads[0]["audio_mode"], "realtime")
        self.assertEqual(talking_payloads[0]["emotion_strength"], 0.30)
        self.assertNotIn("wav_location", talking_payloads[0])
        self.assertNotIn("wav_path", talking_payloads[0])

    def test_tts_realtime_false_uses_wav_location_after_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            generated_wav = temp_path / "generated.wav"
            generated_wav.write_bytes(b"RIFF")
            response_wav = temp_path / "current_response.wav"
            manager = ConversationManager(
                openai_manager=_PaidOpenAIManager(),
                elevenlabs_manager=_WavElevenLabsManager(generated_wav),
                audio_manager=_FakeAudioManager(),
                test_mode_manager=_FakeTestModeManager(),
                runtime_bridge=_FakeRuntimeBridge(response_wav),
            )
            manager.post_talking_idle_delay_seconds = 0.0
            manager._active_response_id = 124
            settings = {
                "app": {"use_websocket_runtime_state": True},
                "elevenlabs": {
                    "voice_id": "voice",
                    "model_id": "eleven_turbo_v2_5",
                    "output_format": "pcm_24000",
                    "use_realtime_tts_streaming": False,
                    "voice_speed": "normal",
                },
            }
            sent_payloads: list[dict[str, object]] = []

            def capture_payload(
                payload: dict[str, object],
                timeout: float = 1.0,
            ) -> bool:
                _ = timeout
                sent_payloads.append(payload)
                return True

            try:
                with patch("core.conversation_manager.has_websocket_client", return_value=True), patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    side_effect=capture_payload,
                ):
                    audio_path, _used_cache, errors = manager._handle_tts_and_playback(
                        response_text="hola",
                        mood="Neutral",
                        settings=settings,
                        test_mode_enabled=False,
                        state_callback=None,
                        thinking_started_at=0.0,
                    )
                    wav_exists_before_cleanup = response_wav.exists()
            finally:
                manager.shutdown()

        self.assertEqual(errors, [])
        self.assertEqual(audio_path, response_wav)
        self.assertTrue(wav_exists_before_cleanup)
        talking_payloads = [
            payload
            for payload in sent_payloads
            if payload.get("type") == "runtime_state"
            and payload.get("state") == "talking"
        ]
        self.assertTrue(talking_payloads)
        self.assertEqual(talking_payloads[0]["audio_mode"], "wav")
        self.assertEqual(talking_payloads[0]["emotion_strength"], 0.30)
        self.assertEqual(talking_payloads[0]["wav_location"], response_wav.as_posix())
        self.assertNotIn("wav_path", talking_payloads[0])

    def test_wav_runtime_state_is_not_sent_before_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager._use_websocket_runtime_state = True
            manager._active_audio_mode = "wav"
            manager._active_response_id = 125
            manager.runtime_bridge = SimpleNamespace(
                config=SimpleNamespace(
                    response_audio_path=Path(temp_dir) / "missing.wav"
                )
            )

            with patch(
                "core.conversation_manager.send_json_to_unreal_blocking"
            ) as send_json, self.assertLogs("core.conversation_manager", level="WARNING"):
                manager._send_runtime_state_over_websocket("TALKING", "Neutral")

        self.assertFalse(send_json.called)

    def test_audio_finished_event_transitions_to_idle_after_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            manager = self._build_manager(temp_path / "current_response.wav")
            states: list[AssistantState] = []
            manager._current_state = AssistantState.TALKING
            manager._active_response_id = 42
            manager._active_state_callback = states.append
            manager.current_mood = "Neutral"
            manager.runtime_bridge = _FakeRuntimeBridge(
                temp_path / "current_response.wav"
            )
            manager._use_websocket_runtime_state = True

            try:
                with patch("core.conversation_manager.time.sleep", return_value=None), patch(
                    "core.conversation_manager.threading.Thread",
                    side_effect=lambda **kwargs: SimpleNamespace(
                        start=lambda: kwargs["target"](*kwargs["args"])
                    ),
                ), patch(
                    "core.conversation_manager.send_json_to_unreal_blocking",
                    return_value=True,
                ):
                    handled = manager.handle_unreal_websocket_message(
                        {"type": "settings_action", "action": "audio_finished"}
                    )
            finally:
                manager.shutdown()

        self.assertTrue(handled)
        self.assertIn(AssistantState.IDLE, states)
        self.assertEqual(manager.runtime_bridge.states[-1][0], AssistantState.IDLE.value)

    def test_settings_update_and_reset_settings_defaults_from_unreal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = SettingsManager(Path(temp_dir))
            audio_manager = AudioManager(settings_manager)
            manager = ConversationManager(
                openai_manager=_PaidOpenAIManager(),
                elevenlabs_manager=_PaidElevenLabsManager(),
                audio_manager=audio_manager,
                test_mode_manager=_FakeTestModeManager(),
                runtime_bridge=_FakeRuntimeBridge(
                    Path(temp_dir) / "current_response.wav"
                ),
            )
            try:
                handled = manager.handle_unreal_websocket_message(
                    {
                        "type": "settings_update",
                        "setting": "tts_realtime",
                        "value": "False",
                    }
                )
                self.assertTrue(handled)
                settings = settings_manager.get_settings()
                self.assertFalse(settings["tts_realtime"])
                self.assertFalse(settings["elevenlabs"]["use_realtime_tts_streaming"])

                handled = manager.handle_unreal_websocket_message(
                    {
                        "type": "settings_update",
                        "setting": "response_length",
                        "value": "detailed",
                    }
                )
                self.assertTrue(handled)
                settings = settings_manager.get_settings()
                self.assertEqual(settings["openai"]["max_response_words"], 176)

                handled = manager.handle_unreal_websocket_message(
                    {"type": "settings_action", "action": "reset_settings_defaults"}
                )
                self.assertTrue(handled)
                settings = settings_manager.get_settings()
                self.assertFalse(settings["tts_realtime"])
                self.assertEqual(settings["openai_model"], "gpt-5.4-mini")
                self.assertEqual(settings["response_length"], "short")
            finally:
                manager.shutdown()


def _pcm16_samples(value: int, count: int) -> bytes:
    return b"".join(
        int(value).to_bytes(2, "little", signed=True)
        for _ in range(count)
    )


if __name__ == "__main__":
    unittest.main()
