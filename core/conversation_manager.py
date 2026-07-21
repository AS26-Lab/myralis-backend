from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from core.audio_manager import AudioManager
from core.elevenlabs_manager import ElevenLabsManager
from core.mood import (
    DEFAULT_MOOD,
    ELEVENLABS_MOOD_PROFILES,
    detect_response_mood,
    get_emotion_strength_for_mood,
    get_random_debug_mood,
    normalize_mood as normalize_assistant_mood,
    normalize_runtime_mood as normalize_unreal_mood,
    prepare_text_for_elevenlabs,
)
from core.openai_manager import AIResponse, OpenAIManager
from core.personality import (
    build_customization_personality_prompt,
    build_profanity_filter_prompt,
)
from core.runtime_bridge import RuntimeBridge
from core.settings_manager import (
    DEFAULT_ELEVENLABS_VOICE_ID,
    OFFICIAL_DEFAULT_SETTINGS,
    PASSIVE_GRAPHICS_SETTING_IDS,
)
from core.usage_estimator import (
    UsageEstimator,
    record_test_miralys_token_usage,
    record_usage_adaptation,
)
from core.test_mode_manager import TestModeManager
from core.websocket_server import (
    NO_CLIENT_MESSAGE,
    has_websocket_client,
    send_json_to_unreal_blocking,
    set_unreal_json_message_handler,
    stream_wav_to_unreal,
)


LOGGER = logging.getLogger(__name__)

RUNTIME_STATES: dict[str, str] = {
    "idle": "idle",
    "listening": "listening",
    "thinking": "thinking",
    "talking": "talking",
}

RUNTIME_AUDIO_MODES: set[str] = {"none", "realtime", "wav"}
VOICE_SPEED_MULTIPLIERS: dict[str, float] = {
    "slow": 0.90,
    "normal": 1.00,
    "fast": 1.10,
    "very_fast": 1.20,
}
MIN_THINKING_SECONDS = 5.0
POST_AUDIO_FINISHED_IDLE_DELAY_SECONDS = 0.5


def normalize_runtime_state(value: str | None) -> str:
    if not isinstance(value, str):
        return "idle"
    return RUNTIME_STATES.get(value.strip().lower(), "idle")


def normalize_mood(value: str | None) -> str:
    return normalize_unreal_mood(value)


normalize_runtime_mood = normalize_mood


def normalize_runtime_audio_mode(value: str | None) -> str:
    if not isinstance(value, str):
        return "none"
    clean_value = value.strip().lower()
    if clean_value in RUNTIME_AUDIO_MODES:
        return clean_value
    return "none"


def parse_ai_realtime_processing_enabled(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        clean_value = value.strip().lower()
        if clean_value == "true":
            return True
        if clean_value == "false":
            return False
    return None


def _bool_log(value: bool) -> str:
    return "true" if value else "false"


def _setting_log_value(value: Any) -> str:
    if isinstance(value, bool):
        return _bool_log(value)
    return str(value)


class AssistantState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    TALKING = "TALKING"


class ConversationError(RuntimeError):
    """Raised when a conversation turn cannot be processed."""


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str


@dataclass(frozen=True)
class AssistantResult:
    response: AIResponse
    audio_path: Path | None = None
    mood: str = DEFAULT_MOOD
    used_cached_text: bool = False
    used_cached_audio: bool = False
    errors: list[str] = field(default_factory=list)


StateCallback = Callable[[AssistantState], None]
AIRealtimeProcessingCallback = Callable[[bool], None]
BackendUIActionCallback = Callable[[str], None]


class ConversationManager:
    """Coordinates user text, OpenAI, ElevenLabs, audio playback, and test mode."""

    def __init__(
        self,
        *,
        openai_manager: OpenAIManager,
        elevenlabs_manager: ElevenLabsManager,
        audio_manager: AudioManager,
        test_mode_manager: TestModeManager,
        runtime_bridge: RuntimeBridge,
    ) -> None:
        self.openai_manager = openai_manager
        self.elevenlabs_manager = elevenlabs_manager
        self.audio_manager = audio_manager
        self.test_mode_manager = test_mode_manager
        self.runtime_bridge = runtime_bridge
        self._history: list[ConversationMessage] = []
        self._lock = threading.RLock()
        now = time.time()
        self.current_mood: str = DEFAULT_MOOD
        self.last_mood_update_time: float = now
        self.last_interaction_time: float = now
        self.mood_calm_timeout_seconds: float = 20.0
        self.post_talking_idle_delay_seconds: float = 2.0
        self._current_state = AssistantState.IDLE
        self._response_counter = 0
        self._active_response_id: int | None = None
        self._active_audio_mode = "none"
        self._use_websocket_runtime_state = True
        self.send_mic_level = False
        self.with_ai_realtime_processing = True
        self._backend_ui_action_handler: BackendUIActionCallback | None = None
        self._active_state_callback: StateCallback | None = None
        self._unreal_turn_lock = threading.Lock()
        self.emotion_analysis_interval_seconds = 2.0
        self.min_words_for_emotion_analysis = 6
        self.min_seconds_between_mood_changes = 2.0
        self.min_strength_delta_to_send = 0.15
        self.last_listening_mood = DEFAULT_MOOD
        self.last_listening_emotion_strength = get_emotion_strength_for_mood(DEFAULT_MOOD)
        self.last_emotion_analysis_time = 0.0
        self.last_mood_change_time = 0.0
        self._ai_realtime_processing_listeners: list[AIRealtimeProcessingCallback] = []
        LOGGER.info("AI realtime processing default: true")
        set_unreal_json_message_handler(self.handle_unreal_websocket_message)
        self._calm_stop_event = threading.Event()
        self._calm_thread = threading.Thread(
            target=self._mood_calm_loop,
            name="MoodCalmTimer",
            daemon=True,
        )
        self._calm_thread.start()

    def get_history(self) -> list[ConversationMessage]:
        with self._lock:
            return list(self._history)

    def add_ai_realtime_processing_listener(
        self,
        listener: AIRealtimeProcessingCallback,
    ) -> None:
        with self._lock:
            self._ai_realtime_processing_listeners.append(listener)

    def is_ai_realtime_processing_enabled(self) -> bool:
        with self._lock:
            return bool(self.with_ai_realtime_processing)

    def should_send_mic_level(self) -> bool:
        with self._lock:
            return bool(getattr(self, "send_mic_level", False))

    def disable_mic_level_messages(self, *, source: str = "local") -> None:
        self._set_send_mic_level(False, source=source)

    def set_backend_ui_action_handler(
        self,
        handler: BackendUIActionCallback | None,
    ) -> None:
        with self._lock:
            self._backend_ui_action_handler = handler

    def set_ai_realtime_processing_enabled(
        self,
        enabled: bool,
        *,
        source: str = "local",
    ) -> None:
        clean_enabled = bool(enabled)
        with self._lock:
            self.with_ai_realtime_processing = clean_enabled
            listeners = list(self._ai_realtime_processing_listeners)

        if source.strip().lower() == "unreal":
            LOGGER.info(
                "AI realtime processing updated from Unreal: %s",
                _bool_log(clean_enabled),
            )
        else:
            LOGGER.info(
                "AI realtime processing updated: %s",
                _bool_log(clean_enabled),
            )

        for listener in listeners:
            try:
                listener(clean_enabled)
            except Exception:
                LOGGER.exception("AI realtime processing listener failed")

    def _set_send_mic_level(self, enabled: bool, *, source: str) -> None:
        clean_enabled = bool(enabled)
        with self._lock:
            self.send_mic_level = clean_enabled
        LOGGER.info(
            "Mic level websocket messages %s: source=%s",
            "enabled" if clean_enabled else "disabled",
            source,
        )

    def handle_unreal_websocket_message(self, payload: dict[str, Any]) -> bool:
        message_type = payload.get("type")
        if message_type == "settings_update":
            return self._handle_settings_update(payload)
        if message_type == "settings_action":
            return self._handle_settings_action(payload)
        if message_type == "backend_ui":
            return self._handle_backend_ui_action(payload)
        if message_type in {"user_text", "text_input", "chat_message"}:
            LOGGER.info(
                "Received Unreal text payload: %s",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
            return self._handle_unreal_text_input(payload)
        if message_type != "ai_realtime_processing":
            return False

        enabled = parse_ai_realtime_processing_enabled(payload.get("enabled"))
        if enabled is None:
            LOGGER.warning(
                "Ignoring invalid ai_realtime_processing enabled value: %r",
                payload.get("enabled"),
            )
            return False
        if not enabled:
            LOGGER.info(
                "Ignoring external ai_realtime_processing=false; "
                "debug mode must be enabled manually in the Python UI"
            )
            return False

        self.set_ai_realtime_processing_enabled(enabled, source="Unreal")
        return True

    def _handle_settings_update(self, payload: dict[str, Any]) -> bool:
        setting_id = str(payload.get("setting", "")).strip()
        settings_manager = getattr(self.audio_manager, "settings_manager", None)
        if settings_manager is None:
            LOGGER.warning("Ignoring settings_update without SettingsManager")
            return False

        try:
            result = settings_manager.apply_settings_update(
                setting_id,
                payload.get("value"),
            )
            if result.category == "settings" and setting_id == "input_device":
                self.audio_manager.save_input_device_option_id(str(result.value))
            elif result.category == "settings" and setting_id == "output_device":
                self.audio_manager.save_output_device_option_id(str(result.value))
        except Exception as exc:
            LOGGER.warning(
                "Ignoring invalid settings_update setting=%s value=%r: %s",
                setting_id,
                payload.get("value"),
                exc,
            )
            return False

        if result.category == "settings":
            settings = settings_manager.get_settings()
            self._apply_runtime_settings(settings)
        if not result.persisted:
            LOGGER.info(
                "Runtime/backend_ui setting received without persistence: setting=%s value=%s",
                setting_id,
                _setting_log_value(result.value),
            )
        elif result.category == "customization":
            if setting_id == "profanity_filter":
                LOGGER.info("Profanity filter updated: %s", _bool_log(bool(result.value)))
            elif setting_id == "voice_id":
                LOGGER.info("Voice selection updated: voice_id=%s", result.value)
            elif setting_id == "use_custom_voice":
                LOGGER.info("Custom voice enabled: %s", _bool_log(bool(result.value)))
            elif setting_id == "custom_voice_id":
                LOGGER.info("Custom voice id updated: %s", result.value)
            elif setting_id == "selected_character":
                LOGGER.info("selected_character stored only: %s", result.value)
            elif setting_id == "use_custom_personality_prompt":
                LOGGER.info(
                    "Custom personality prompt enabled: %s",
                    _bool_log(bool(result.value)),
                )
            elif setting_id == "custom_personality_prompt":
                LOGGER.info(
                    "Custom personality prompt updated: %s chars",
                    len(str(result.value)),
                )
            else:
                LOGGER.info(
                    "Customization setting updated: %s=%s",
                    setting_id,
                    _setting_log_value(result.value),
                )
        elif setting_id == "tts_realtime":
            LOGGER.info("TTS realtime setting updated: %s", _bool_log(bool(result.value)))
        elif setting_id in PASSIVE_GRAPHICS_SETTING_IDS:
            LOGGER.info(
                "Passive Unreal graphics setting stored: setting=%s value=%s",
                setting_id,
                _setting_log_value(result.value),
            )
        else:
            LOGGER.info(
                "Official setting updated from Unreal: setting=%s value=%s",
                setting_id,
                _setting_log_value(result.value),
            )
        return True

    def _handle_settings_action(self, payload: dict[str, Any]) -> bool:
        action = str(payload.get("action", "")).strip()
        if action == "settings_is_open":
            self._set_send_mic_level(True, source="settings_is_open")
            return True
        if action == "settings_is_closed":
            self._set_send_mic_level(False, source="settings_is_closed")
            return True
        if action == "exit_myralis":
            LOGGER.info("Received exit_myralis from Unreal")
            self._notify_backend_ui_action("exit_myralis")
            return True
        if action == "finish_loading":
            LOGGER.info("Received finish_loading from Unreal")
            return True
        if action == "audio_finished":
            return self._handle_audio_finished()
        if action == "mic_level_is_showing":
            LOGGER.info("Received mic_level_is_showing from Unreal")
            return True
        if action == "mic_level_is_not_showing":
            LOGGER.info("Received mic_level_is_not_showing from Unreal")
            return True
        if action == "mic_level_not_showing":
            LOGGER.info("Received mic_level_not_showing from Unreal")
            return True

        settings_manager = getattr(self.audio_manager, "settings_manager", None)
        if action == "reset_settings_defaults":
            if settings_manager is None:
                LOGGER.warning("Ignoring reset_settings_defaults without SettingsManager")
                return False
            settings = settings_manager.reset_settings_defaults()
            self._apply_runtime_settings(settings)
            LOGGER.info("General settings reset to defaults from Unreal")
            return True
        if action == "reset_customization_defaults":
            if settings_manager is None:
                LOGGER.warning(
                    "Ignoring reset_customization_defaults without SettingsManager"
                )
                return False
            settings_manager.reset_customization_defaults()
            LOGGER.info("Customization reset to defaults from Unreal")
            return True
        LOGGER.warning("Ignoring unsupported settings_action from Unreal: %s", action)
        return False

    def _handle_backend_ui_action(self, payload: dict[str, Any]) -> bool:
        action = str(payload.get("action", "")).strip()
        if action not in {"show_python_ui", "hide_python_ui"}:
            return False
        self._notify_backend_ui_action(action)
        return True

    def _handle_audio_finished(self) -> bool:
        with self._lock:
            current_state = getattr(self, "_current_state", AssistantState.IDLE)
            active_response_id = getattr(self, "_active_response_id", None)
            state_callback = getattr(self, "_active_state_callback", None)

        if current_state != AssistantState.TALKING:
            LOGGER.info(
                "Ignoring audio_finished because current state is %s",
                current_state.value,
            )
            return True

        threading.Thread(
            target=self._complete_audio_finished_idle_transition,
            args=(active_response_id, state_callback),
            name="AudioFinishedIdle",
            daemon=True,
        ).start()
        LOGGER.info(
            "Received audio_finished from Unreal; scheduling IDLE in %.1fs",
            POST_AUDIO_FINISHED_IDLE_DELAY_SECONDS,
        )
        return True

    def _complete_audio_finished_idle_transition(
        self,
        active_response_id: int | None,
        state_callback: StateCallback | None,
    ) -> None:
        time.sleep(POST_AUDIO_FINISHED_IDLE_DELAY_SECONDS)

        with self._lock:
            if self._current_state != AssistantState.TALKING:
                return
            if self._active_response_id != active_response_id:
                return
            clean_mood = self.current_mood
            self._current_state = AssistantState.IDLE
            self.last_mood_update_time = time.time()

        try:
            self.runtime_bridge.set_runtime_state(
                state=AssistantState.IDLE.value,
                mood=clean_mood,
            )
            self._send_runtime_state_over_websocket(
                AssistantState.IDLE.value,
                clean_mood,
            )
            LOGGER.info(
                "[RuntimeBridge] state=IDLE mood=%s after_audio_finished_delay=%.1f",
                clean_mood,
                POST_AUDIO_FINISHED_IDLE_DELAY_SECONDS,
            )
        except Exception:
            LOGGER.exception("Could not write audio_finished IDLE runtime state")

        if state_callback is not None:
            state_callback(AssistantState.IDLE)
        with self._lock:
            if self._active_state_callback is state_callback:
                self._active_state_callback = None

    def _notify_backend_ui_action(self, action: str) -> None:
        with self._lock:
            handler = self._backend_ui_action_handler
        if handler is None:
            return
        try:
            handler(action)
        except Exception:
            LOGGER.exception("Backend UI action handler failed: %s", action)

    def _handle_unreal_text_input(self, payload: dict[str, Any]) -> bool:
        text = str(
            payload.get("text", payload.get("message", payload.get("value", "")))
        ).strip()
        if not text:
            return False

        settings_manager = getattr(self.audio_manager, "settings_manager", None)
        if settings_manager is None:
            LOGGER.warning("Ignoring Unreal text input without SettingsManager")
            return False
        settings = settings_manager.get_settings()
        if self._interaction_mode(settings) != "text":
            LOGGER.info("Ignoring Unreal text input because interaction_mode=voice")
            return False
        if not self._unreal_turn_lock.acquire(blocking=False):
            LOGGER.warning("Ignoring Unreal text input because a turn is already active")
            return False

        thread = threading.Thread(
            target=self._process_unreal_text_turn,
            args=(text,),
            name="UnrealTextTurn",
            daemon=True,
        )
        thread.start()
        return True

    def _process_unreal_text_turn(self, text: str) -> None:
        try:
            settings_manager = self.audio_manager.settings_manager
            settings = settings_manager.get_settings()
            LOGGER.info("Processing Unreal text input in text mode")
            self.process_user_message(text, settings)
        except Exception:
            LOGGER.exception("Unreal text input turn failed")
        finally:
            self._unreal_turn_lock.release()

    def process_user_message(
        self,
        user_text: str,
        settings: dict[str, Any],
        state_callback: StateCallback | None = None,
    ) -> AssistantResult:
        clean_text = user_text.strip()
        if not clean_text:
            raise ConversationError("Message text is empty.")

        try:
            self._begin_runtime_response(settings)
            with self._lock:
                self._active_state_callback = state_callback
            if not self.is_ai_realtime_processing_enabled():
                LOGGER.info("AI realtime processing disabled; using fake/debug flow")
                LOGGER.info(
                    "AI realtime disabled; skipping OpenAI/ElevenLabs paid generation"
                )
                return self._process_user_message_debug(
                    clean_text,
                    settings,
                    state_callback,
                )

            LOGGER.info("AI realtime processing enabled; using premium AI flow")
            self._prepare_for_user_interaction(settings, state_callback)
            thinking_started_at = time.monotonic()
            self._append_message("user", clean_text)
            openai_settings = settings.get("openai", {})
            history_limit = self._bounded_int(
                openai_settings.get("history_limit", 10),
                default=10,
                minimum=2,
                maximum=40,
            )
            history_payload = self._history_payload(history_limit)
            test_mode_enabled = self.test_mode_manager.is_enabled(settings)
            previous_mood = self._get_current_mood()

            self._emit_state(state_callback, AssistantState.THINKING)
            response, used_cached_text = self._get_ai_response(
                history_payload=history_payload,
                settings=settings,
                test_mode_enabled=test_mode_enabled,
                current_mood=previous_mood,
            )
            detected_mood = detect_response_mood(
                response.text,
                previous_mood=previous_mood,
                user_text=clean_text,
            )
            LOGGER.info(
                "[Mood] previous=%s detected=%s user_changed_mood=%s",
                previous_mood,
                detected_mood,
                previous_mood != detected_mood,
            )
            self._emit_runtime_state(
                state_callback,
                AssistantState.THINKING,
                mood=detected_mood,
            )
            self._append_message("assistant", response.text)
            if not test_mode_enabled:
                settings_manager = getattr(self.audio_manager, "settings_manager", None)
                if settings_manager is not None:
                    record_usage_adaptation(
                        settings_manager=settings_manager,
                        settings=settings,
                        user_text=clean_text,
                        assistant_text=response.text,
                    )

            audio_path, used_cached_audio, errors = self._handle_tts_and_playback(
                response_text=response.text,
                mood=detected_mood,
                settings=settings,
                test_mode_enabled=test_mode_enabled,
                state_callback=state_callback,
                thinking_started_at=thinking_started_at,
            )

            if test_mode_enabled and not used_cached_text:
                settings_manager = getattr(self.audio_manager, "settings_manager", None)
                if settings_manager is not None:
                    root = getattr(settings_manager, "root", None)
                    usage_snapshot = UsageEstimator(root).build_snapshot(settings)
                    record_test_miralys_token_usage(
                        settings_manager=settings_manager,
                        settings=settings,
                        coins_used=usage_snapshot.miralys_tokens_per_conversation,
                    )

            return AssistantResult(
                response=response,
                audio_path=audio_path,
                mood=detected_mood,
                used_cached_text=used_cached_text,
                used_cached_audio=used_cached_audio,
                errors=errors,
            )
        except Exception:
            if self._current_state != AssistantState.IDLE:
                self._emit_state(state_callback, AssistantState.IDLE)
            raise

    def _process_user_message_debug(
        self,
        clean_text: str,
        settings: dict[str, Any],
        state_callback: StateCallback | None,
    ) -> AssistantResult:
        self.note_user_interaction()
        self._set_active_audio_mode("none")
        self._append_message("user", clean_text)

        debug_mood = get_random_debug_mood()
        emotion_strength = get_emotion_strength_for_mood(debug_mood)
        LOGGER.info(
            "DEBUG FAKE MOOD selected: mood=%s emotion_strength=%.2f",
            debug_mood,
            emotion_strength,
        )

        if self._interaction_mode(settings) == "text":
            LOGGER.info("Listening emotion analysis skipped: text mode")

        self._emit_runtime_state(
            state_callback,
            AssistantState.LISTENING,
            mood=debug_mood,
        )
        self._emit_runtime_state(
            state_callback,
            AssistantState.THINKING,
            mood=debug_mood,
        )

        response_text = (
            "Modo debug activo: respuesta local generada sin OpenAI ni ElevenLabs."
        )
        response = AIResponse(
            text=response_text,
            emotion=debug_mood,
            raw_text=response_text,
            model="debug",
        )
        self._append_message("assistant", response.text)

        audio_path, used_debug_audio = self._handle_debug_audio_if_available(
            mood=debug_mood,
            settings=settings,
            state_callback=state_callback,
            thinking_started_at=None,
        )
        return AssistantResult(
            response=response,
            audio_path=audio_path,
            mood=debug_mood,
            used_cached_text=False,
            used_cached_audio=used_debug_audio,
            errors=[],
        )

    def _handle_debug_audio_if_available(
        self,
        *,
        mood: str,
        settings: dict[str, Any],
        state_callback: StateCallback | None,
        thinking_started_at: float | None,
    ) -> tuple[Path | None, bool]:
        LOGGER.info(
            "AI realtime disabled; no paid TTS generated. "
            "Using current_wav/debug audio if available."
        )
        clean_mood = normalize_assistant_mood(mood)
        current_wav = self.runtime_bridge.config.response_audio_path
        audio_path = current_wav if current_wav.exists() else None
        self._set_active_audio_mode("wav" if audio_path is not None else "none")

        self._wait_for_thinking_window(thinking_started_at)
        self._emit_runtime_state(
            state_callback,
            AssistantState.TALKING,
            mood=clean_mood,
            emotion_strength=get_emotion_strength_for_mood(clean_mood),
        )
        if audio_path is not None:
            LOGGER.info("WAV ready; sending runtime_state talking audio_mode=wav")
        else:
            LOGGER.info("No current_wav/debug audio available for disabled AI flow")
        return audio_path, audio_path is not None

    def build_voice_capture_config(self) -> dict[str, Any]:
        config = self.audio_manager.build_voice_capture_config()
        return {
            "hotkey": config.hotkey,
            "input_device_index": config.input_device_index,
            "output_device_index": config.output_device_index,
            "sample_rate": config.sample_rate,
            "input_volume": config.input_volume,
        }

    def handle_partial_stt_transcript(self, text: str) -> None:
        partial_text = text.strip()
        if not partial_text:
            return

        settings_manager = getattr(self.audio_manager, "settings_manager", None)
        if settings_manager is None:
            return
        settings = settings_manager.get_settings()
        if self._interaction_mode(settings) != "voice":
            LOGGER.info("Listening emotion analysis skipped: text mode")
            return
        if not self._listening_emotion_analysis_enabled(settings):
            return
        with self._lock:
            current_state = self._current_state
            previous_mood = self.last_listening_mood or self.current_mood
            previous_strength = self.last_listening_emotion_strength
            last_analysis_time = self.last_emotion_analysis_time
            last_mood_change_time = self.last_mood_change_time
        if current_state != AssistantState.LISTENING:
            return

        word_count = len(partial_text.split())
        if word_count < self.min_words_for_emotion_analysis:
            LOGGER.info("Listening emotion analysis skipped: not enough words")
            return

        now = time.time()
        if now - last_analysis_time < self.emotion_analysis_interval_seconds:
            LOGGER.info("Listening emotion analysis skipped: cooldown active")
            return

        detected_mood = detect_response_mood(
            partial_text,
            previous_mood=previous_mood,
            user_text=partial_text,
        )
        detected_mood = normalize_assistant_mood(detected_mood)
        strength = get_emotion_strength_for_mood(detected_mood)
        LOGGER.info(
            "Listening emotion analysis result: mood=%s strength=%.2f",
            detected_mood,
            strength,
        )

        mood_changed = detected_mood != previous_mood
        if mood_changed and now - last_mood_change_time < self.min_seconds_between_mood_changes:
            LOGGER.info("Listening emotion analysis skipped: cooldown active")
            with self._lock:
                self.last_emotion_analysis_time = now
            return
        if not mood_changed and abs(strength - previous_strength) < self.min_strength_delta_to_send:
            with self._lock:
                self.last_emotion_analysis_time = now
            return

        with self._lock:
            self.last_emotion_analysis_time = now
            self.last_listening_mood = detected_mood
            self.last_listening_emotion_strength = strength
            if mood_changed:
                self.last_mood_change_time = now

        self._emit_runtime_state(
            None,
            AssistantState.LISTENING,
            mood=detected_mood,
            emotion_strength=strength,
        )
        LOGGER.info(
            "Listening emotion update sent: state=listening mood=%s strength=%.2f",
            detected_mood,
            strength,
        )

    def _get_ai_response(
        self,
        *,
        history_payload: list[dict[str, str]],
        settings: dict[str, Any],
        test_mode_enabled: bool,
        current_mood: str,
    ) -> tuple[AIResponse, bool]:
        if test_mode_enabled:
            cached_response = self.test_mode_manager.load_response()
            if cached_response is not None:
                LOGGER.info("Using cached OpenAI response from TEST MODE")
                return cached_response, True

        openai_settings = settings["openai"]
        max_response_words = self._bounded_int(
            openai_settings.get("max_response_words", 60),
            default=60,
            minimum=20,
            maximum=250,
        )
        response = self.openai_manager.generate_response(
            history=history_payload,
            model=str(openai_settings["model"]),
            temperature=float(openai_settings["temperature"]),
            max_response_words=max_response_words,
            reasoning_effort=str(openai_settings.get("reasoning_effort", "low")),
            system_prompt=self._build_system_prompt_with_mood(
                str(openai_settings["system_prompt"]),
                current_mood,
                max_response_words,
                settings.get("customization", {}),
            ),
        )
        if test_mode_enabled:
            self.test_mode_manager.save_response(response)
        return response, False

    def _handle_tts_and_playback(
        self,
        *,
        response_text: str,
        mood: str,
        settings: dict[str, Any],
        test_mode_enabled: bool,
        state_callback: StateCallback | None,
        thinking_started_at: float | None,
    ) -> tuple[Path | None, bool, list[str]]:
        errors: list[str] = []
        used_cached_audio = False
        audio_path: Path | None = None
        talking_started = False
        clean_mood = normalize_assistant_mood(mood)

        try:
            elevenlabs_settings = settings["elevenlabs"]
            use_realtime_streaming = bool(
                elevenlabs_settings.get("use_realtime_tts_streaming", True)
            )

            if use_realtime_streaming and not test_mode_enabled:
                if has_websocket_client():
                    LOGGER.info("Using realtime TTS audio mode")
                    self._set_active_audio_mode("realtime")
                    audio_path, talking_started = self._stream_realtime_tts_to_unreal(
                        response_text=response_text,
                        mood=clean_mood,
                        settings=settings,
                        state_callback=state_callback,
                        thinking_started_at=thinking_started_at,
                    )
                    return audio_path, used_cached_audio, errors
                else:
                    LOGGER.warning(NO_CLIENT_MESSAGE)
                    LOGGER.info("Using WAV complete TTS audio mode")
                    self._set_active_audio_mode("wav")
            else:
                LOGGER.info("Using WAV complete TTS audio mode")
                self._set_active_audio_mode("wav")

            if audio_path is None:
                audio_path, used_cached_audio, talking_started = (
                    self._generate_legacy_wav_and_playback(
                        response_text=response_text,
                        mood=clean_mood,
                        settings=settings,
                        test_mode_enabled=test_mode_enabled,
                        state_callback=state_callback,
                        thinking_started_at=thinking_started_at,
                    )
                )
        except Exception as exc:
            LOGGER.exception("TTS or playback failed")
            errors.append(str(exc))
            if not talking_started:
                self._emit_runtime_state(
                    state_callback,
                    AssistantState.IDLE,
                    mood=clean_mood,
                )

        return audio_path, used_cached_audio, errors

    def test_elevenlabs_streaming(
        self,
        settings: dict[str, Any],
        state_callback: StateCallback | None = None,
    ) -> Path | None:
        self._begin_runtime_response(settings)
        if not self.is_ai_realtime_processing_enabled():
            LOGGER.info(
                "AI realtime disabled; skipping OpenAI/ElevenLabs paid generation"
            )
            debug_mood = get_random_debug_mood()
            emotion_strength = get_emotion_strength_for_mood(debug_mood)
            LOGGER.info(
                "DEBUG FAKE MOOD selected: mood=%s emotion_strength=%.2f",
                debug_mood,
                emotion_strength,
            )
            audio_path, _used_debug_audio = self._handle_debug_audio_if_available(
                mood=debug_mood,
                settings=settings,
                state_callback=state_callback,
                thinking_started_at=time.monotonic(),
            )
            return audio_path

        audio_path, _talking_started = self._stream_realtime_tts_to_unreal(
            response_text="Prueba corta de streaming en tiempo real con Unreal.",
            mood=DEFAULT_MOOD,
            settings=settings,
            state_callback=state_callback,
            thinking_started_at=time.monotonic(),
        )
        return audio_path

    def test_runtime_lip_sync(
        self,
        settings: dict[str, Any],
        state_callback: StateCallback | None = None,
    ) -> Path | None:
        self._begin_runtime_response(settings)
        self._emit_runtime_state(
            state_callback,
            AssistantState.LISTENING,
            mood=DEFAULT_MOOD,
        )
        self._emit_runtime_state(
            state_callback,
            AssistantState.THINKING,
            mood=DEFAULT_MOOD,
        )
        audio_path, _talking_started = self._stream_realtime_tts_to_unreal(
            response_text="Prueba de lip sync en tiempo real para Unreal.",
            mood="Happy",
            settings=settings,
            state_callback=state_callback,
            thinking_started_at=time.monotonic(),
        )
        return audio_path

    def _stream_realtime_tts_to_unreal(
        self,
        *,
        response_text: str,
        mood: str,
        settings: dict[str, Any],
        state_callback: StateCallback | None,
        thinking_started_at: float | None,
    ) -> tuple[Path | None, bool]:
        elevenlabs_settings = settings["elevenlabs"]
        clean_mood = normalize_assistant_mood(mood)
        self._set_active_audio_mode("realtime")
        LOGGER.info("runtime_state audio_mode=realtime")
        tts_text = prepare_text_for_elevenlabs(response_text, clean_mood)
        voice_settings = dict(ELEVENLABS_MOOD_PROFILES[clean_mood])
        self._apply_voice_speed_setting(voice_settings, settings)
        save_response_wav = bool(elevenlabs_settings.get("save_response_wav", True))
        output_format = str(elevenlabs_settings.get("output_format", "pcm_24000"))
        if output_format != "pcm_24000":
            LOGGER.warning(
                "Realtime TTS requires pcm_24000 for Unreal; overriding output_format=%s",
                output_format,
            )
            output_format = "pcm_24000"

        optimize_latency_raw = elevenlabs_settings.get("optimize_streaming_latency")
        optimize_latency: int | None = None
        if optimize_latency_raw is not None:
            optimize_latency = self._bounded_int(
                optimize_latency_raw,
                default=2,
                minimum=0,
                maximum=4,
            )
        websocket_audio_chunk_ms = self._bounded_int(
            elevenlabs_settings.get("websocket_audio_chunk_ms", 200),
            default=200,
            minimum=1,
            maximum=1000,
        )
        websocket_audio_start_silence_chunks = self._bounded_int(
            elevenlabs_settings.get("websocket_audio_start_silence_chunks", 2),
            default=2,
            minimum=0,
            maximum=10,
        )
        websocket_audio_fade_in_ms = self._bounded_int(
            elevenlabs_settings.get("websocket_audio_fade_in_ms", 15),
            default=15,
            minimum=0,
            maximum=250,
        )
        websocket_audio_realtime_pacing = bool(
            elevenlabs_settings.get("websocket_audio_realtime_pacing", True)
        )

        talking_started = False

        def wait_for_thinking_window() -> None:
            self._wait_for_thinking_window(thinking_started_at)

        def mark_talking_started() -> None:
            nonlocal talking_started
            if talking_started:
                return
            wait_for_thinking_window()
            self._emit_runtime_state(
                state_callback,
                AssistantState.TALKING,
                mood=clean_mood,
                emotion_strength=get_emotion_strength_for_mood(clean_mood),
            )
            talking_started = True

        try:
            final_voice_id = self._resolve_final_voice_id(settings)
            result = self.elevenlabs_manager.stream_elevenlabs_tts_to_unreal(
                text=tts_text,
                voice_id=final_voice_id,
                model_id=str(elevenlabs_settings["model_id"]),
                output_format=output_format,
                voice_settings=voice_settings,
                mood=clean_mood,
                optimize_streaming_latency=optimize_latency,
                save_response_wav=save_response_wav,
                response_wav_path=self.runtime_bridge.config.response_audio_path,
                on_audio_start=mark_talking_started,
                startup_silence_chunks=websocket_audio_start_silence_chunks,
                fade_in_ms=websocket_audio_fade_in_ms,
                websocket_audio_chunk_ms=websocket_audio_chunk_ms,
                websocket_audio_realtime_pacing=websocket_audio_realtime_pacing,
            )
        except Exception:
            if not talking_started:
                self._emit_runtime_state(
                    state_callback,
                    AssistantState.IDLE,
                    mood=clean_mood,
                )
            raise

        audio_path = result.audio_path
        if audio_path is not None:
            LOGGER.info("[ElevenLabs] streamed current_response.wav")

        try:
            if audio_path is not None and self.test_mode_manager.is_audio_enabled(settings):
                self.audio_manager.play_audio(audio_path, None)
            else:
                LOGGER.info(
                    "Local Python audio playback muted; realtime audio was sent to Unreal."
                )
        finally:
            if not talking_started:
                self._emit_runtime_state(
                    state_callback,
                    AssistantState.IDLE,
                    mood=clean_mood,
                )

        return audio_path, talking_started

    def _generate_legacy_wav_and_playback(
        self,
        *,
        response_text: str,
        mood: str,
        settings: dict[str, Any],
        test_mode_enabled: bool,
        state_callback: StateCallback | None,
        thinking_started_at: float | None,
    ) -> tuple[Path | None, bool, bool]:
        used_cached_audio = False
        audio_path: Path | None = None
        talking_started = False
        clean_mood = normalize_assistant_mood(mood)
        self._set_active_audio_mode("wav")
        LOGGER.info("runtime_state audio_mode=wav")

        try:
            if test_mode_enabled:
                cached_audio = self.test_mode_manager.load_audio()
                if cached_audio is not None:
                    LOGGER.info("Using cached ElevenLabs audio from TEST MODE")
                    audio_path = cached_audio.audio_path
                    used_cached_audio = True

            if audio_path is None:
                elevenlabs_settings = settings["elevenlabs"]
                tts_text = prepare_text_for_elevenlabs(response_text, clean_mood)
                voice_settings = dict(ELEVENLABS_MOOD_PROFILES[clean_mood])
                self._apply_voice_speed_setting(voice_settings, settings)
                final_voice_id = self._resolve_final_voice_id(settings)
                wav_output_format = "pcm_16000"
                requested_output_format = str(
                    elevenlabs_settings.get("output_format", wav_output_format)
                )
                if requested_output_format != wav_output_format:
                    LOGGER.info(
                        "WAV mode forcing output_format=%s instead of %s",
                        wav_output_format,
                        requested_output_format,
                    )
                tts_result = self.elevenlabs_manager.generate_wav(
                    text=tts_text,
                    voice_id=final_voice_id,
                    model_id=str(elevenlabs_settings["model_id"]),
                    output_format=wav_output_format,
                    voice_settings=voice_settings,
                    mood=clean_mood,
                )
                audio_path = tts_result.audio_path
                if test_mode_enabled:
                    cached_result = self.test_mode_manager.save_audio(audio_path)
                    audio_path = cached_result.audio_path
                    used_cached_audio = True

            if audio_path is not None:
                audio_path = self.runtime_bridge.publish_response_audio(audio_path)
                LOGGER.info("[ElevenLabs] generated current_response.wav")
                if not audio_path.exists():
                    raise FileNotFoundError(f"WAV file is not ready: {audio_path}")
                LOGGER.info("WAV ready; sending runtime_state talking audio_mode=wav")
                self._wait_for_thinking_window(thinking_started_at)
                self._emit_runtime_state(
                    state_callback,
                    AssistantState.TALKING,
                    mood=clean_mood,
                    emotion_strength=get_emotion_strength_for_mood(clean_mood),
                )
                talking_started = True

                if self.test_mode_manager.is_audio_enabled(settings):
                    self.audio_manager.play_audio(audio_path, None)
                else:
                    LOGGER.info(
                        "Local Python audio playback muted; WAV remains available for Unreal."
                    )
        except Exception as exc:
            LOGGER.exception("TTS or playback failed")
            if not talking_started:
                self._emit_runtime_state(
                    state_callback,
                    AssistantState.IDLE,
                    mood=clean_mood,
                )
            raise exc

        return audio_path, used_cached_audio, talking_started

    def _append_message(self, role: str, content: str) -> None:
        with self._lock:
            self._history.append(ConversationMessage(role=role, content=content))

    def _history_payload(self, max_messages: int) -> list[dict[str, str]]:
        clean_limit = max(2, min(40, max_messages))
        with self._lock:
            return [
                {"role": message.role, "content": message.content}
                for message in self._history[-clean_limit:]
            ]

    def note_user_interaction(self) -> None:
        self._apply_calm_timeout_if_due()
        with self._lock:
            self.last_interaction_time = time.time()

    def sync_state(self, state: AssistantState) -> None:
        with self._lock:
            self._current_state = state

    def emit_external_state(self, state: AssistantState) -> None:
        self._emit_state(None, state)

    def shutdown(self) -> None:
        set_unreal_json_message_handler(None)
        self._calm_stop_event.set()
        if self._calm_thread.is_alive():
            self._calm_thread.join(timeout=1.0)

    def _prepare_for_user_interaction(
        self,
        settings: dict[str, Any],
        state_callback: StateCallback | None,
    ) -> None:
        self.note_user_interaction()
        self._reset_listening_emotion_tracking()
        if self._interaction_mode(settings) == "text":
            LOGGER.info("Listening emotion analysis skipped: text mode")
        self._emit_state(state_callback, AssistantState.THINKING)

    def _reset_listening_emotion_tracking(self) -> None:
        with self._lock:
            mood = self.current_mood
            strength = get_emotion_strength_for_mood(mood)
            self.last_listening_mood = mood
            self.last_listening_emotion_strength = strength
            self.last_emotion_analysis_time = 0.0
            self.last_mood_change_time = 0.0

    def _build_system_prompt_with_mood(
        self,
        system_prompt: str,
        mood: str,
        max_response_words: int,
        customization_settings: dict[str, Any] | None = None,
    ) -> str:
        clean_mood = normalize_assistant_mood(mood)
        customization_personality_prompt = build_customization_personality_prompt(
            customization_settings
        )
        profanity_filter_prompt = build_profanity_filter_prompt(customization_settings)
        mood_context = (
            f"Estado emocional actual del asistente: {clean_mood}.\n"
            "Este estado puede influir ligeramente en el tono de la respuesta.\n"
            "No lo menciones explicitamente a menos que sea natural.\n"
            "Puedes cambiar de emocion si el mensaje del usuario lo justifica.\n"
            f"Limita el campo text a maximo {max_response_words} palabras.\n"
            "Normalmente responde en 1 a 3 frases, con naturalidad y utilidad tecnica.\n"
            "El mood influye en el tono, no en la verdad ni en la calidad tecnica.\n"
            "Aunque el mood sea Anger, no uses insultos ni contenido abusivo."
        )
        prompt_parts = [system_prompt.rstrip()]
        if customization_personality_prompt:
            prompt_parts.append(customization_personality_prompt)
        prompt_parts.append(profanity_filter_prompt)
        prompt_parts.append(mood_context)
        return "\n\n".join(prompt_parts)

    def _bounded_int(
        self,
        value: Any,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, parsed))

    def _begin_runtime_response(self, settings: dict[str, Any]) -> None:
        self._apply_runtime_settings(settings)
        elevenlabs_settings = settings.get("elevenlabs", {})
        test_mode_settings = settings.get("test_mode", {})
        use_realtime_tts = bool(
            elevenlabs_settings.get("use_realtime_tts_streaming", True)
        )
        test_mode_enabled = bool(test_mode_settings.get("enabled", False))
        with self._lock:
            self._response_counter += 1
            self._active_response_id = self._response_counter
            if self.with_ai_realtime_processing:
                self._active_audio_mode = (
                    "realtime" if use_realtime_tts and not test_mode_enabled else "wav"
                )
            else:
                self._active_audio_mode = "none"
            self._use_websocket_runtime_state = (
                self._settings_enable_websocket_runtime_state(settings)
            )

    def _apply_runtime_settings(self, settings: dict[str, Any]) -> None:
        neutral_return_time = self._bounded_int(
            settings.get(
                "neutral_return_time",
                settings.get("app", {}).get("neutral_return_time", 45)
                if isinstance(settings.get("app", {}), dict)
                else 45,
            ),
            default=45,
            minimum=1,
            maximum=600,
        )
        with self._lock:
            self.mood_calm_timeout_seconds = float(neutral_return_time)

    def _settings_enable_websocket_runtime_state(
        self,
        settings: dict[str, Any],
    ) -> bool:
        app_settings = settings.get("app", {})
        if isinstance(app_settings, dict) and "use_websocket_runtime_state" in app_settings:
            return bool(app_settings.get("use_websocket_runtime_state", True))
        return bool(settings.get("use_websocket_runtime_state", True))

    def _interaction_mode(self, settings: dict[str, Any]) -> str:
        value = str(
            settings.get(
                "interaction_mode",
                settings.get("app", {}).get("interaction_mode", "voice")
                if isinstance(settings.get("app", {}), dict)
                else "voice",
            )
        ).strip()
        return value if value in {"voice", "text"} else "voice"

    def _listening_emotion_analysis_enabled(self, settings: dict[str, Any]) -> bool:
        return bool(
            settings.get(
                "listening_emotion_analysis",
                OFFICIAL_DEFAULT_SETTINGS["listening_emotion_analysis"],
            )
        )

    def _apply_voice_speed_setting(
        self,
        voice_settings: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        elevenlabs_settings = settings.get("elevenlabs", {})
        speed_id = str(
            settings.get(
                "voice_speed",
                elevenlabs_settings.get("voice_speed", "normal")
                if isinstance(elevenlabs_settings, dict)
                else "normal",
            )
        ).strip()
        multiplier = VOICE_SPEED_MULTIPLIERS.get(speed_id, 1.0)
        base_speed = float(voice_settings.get("speed", 1.0))
        voice_settings["speed"] = max(0.70, min(1.20, base_speed * multiplier))

    def _resolve_final_voice_id(self, settings: dict[str, Any]) -> str:
        elevenlabs_settings = settings.get("elevenlabs", {})
        if not isinstance(elevenlabs_settings, dict):
            elevenlabs_settings = {}
        customization_settings = settings.get("customization", {})
        if not isinstance(customization_settings, dict):
            customization_settings = {}

        selected_voice_id = str(
            customization_settings.get(
                "voice_id",
                elevenlabs_settings.get("voice_id", DEFAULT_ELEVENLABS_VOICE_ID),
            )
        ).strip()
        fallback_voice_id = self._valid_elevenlabs_voice_id(
            selected_voice_id
        ) or self._valid_elevenlabs_voice_id(
            str(elevenlabs_settings.get("voice_id", "")).strip()
        ) or DEFAULT_ELEVENLABS_VOICE_ID
        use_custom_voice = self._bool_setting(
            customization_settings.get("use_custom_voice", False),
            default=False,
        )

        if use_custom_voice:
            custom_voice_id = str(customization_settings.get("custom_voice_id", "")).strip()
            valid_custom_voice_id = self._valid_elevenlabs_voice_id(custom_voice_id)
            if valid_custom_voice_id:
                final_voice_id = valid_custom_voice_id
            else:
                LOGGER.warning(
                    "Custom voice enabled but custom_voice_id is invalid; using fallback voice_id=%s",
                    fallback_voice_id,
                )
                final_voice_id = fallback_voice_id
        else:
            final_voice_id = fallback_voice_id

        LOGGER.info("Final voice resolved: %s", final_voice_id)
        return final_voice_id

    def _valid_elevenlabs_voice_id(self, value: Any) -> str:
        clean_value = str(value or "").strip()
        if not clean_value:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", clean_value):
            return ""
        return clean_value

    def _bool_setting(self, value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            clean = value.strip().lower()
            if clean in {"true", "1"}:
                return True
            if clean in {"false", "0"}:
                return False
        return default

    def _set_active_audio_mode(self, audio_mode: str | None) -> None:
        clean_mode = normalize_runtime_audio_mode(audio_mode)
        with self._lock:
            self._active_audio_mode = clean_mode

    def _send_runtime_state_over_websocket(
        self,
        state: str | None,
        mood: str | None,
        *,
        emotion_strength: float | None = None,
    ) -> None:
        clean_state = normalize_runtime_state(state)
        clean_mood = normalize_runtime_mood(mood)
        clean_emotion_strength = (
            max(0.0, min(1.0, float(emotion_strength)))
            if emotion_strength is not None
            else get_emotion_strength_for_mood(clean_mood)
        )
        with self._lock:
            if not self._use_websocket_runtime_state:
                return
            active_audio_mode = normalize_runtime_audio_mode(self._active_audio_mode)
            response_id = self._active_response_id
        audio_mode = self._audio_mode_for_state(clean_state, active_audio_mode)

        LOGGER.info(
            "WS runtime_state prepared: state=%s mood=%s emotion_strength=%.2f "
            "response_id=%s audio_mode=%s",
            clean_state,
            clean_mood,
            clean_emotion_strength,
            response_id,
            audio_mode,
        )
        LOGGER.info("runtime_state audio_mode=%s", audio_mode)
        payload: dict[str, object] = {
            "type": "runtime_state",
            "state": clean_state,
            "mood": clean_mood,
            "audio_mode": audio_mode,
            "response_id": response_id,
            "emotion_strength": clean_emotion_strength,
        }
        if clean_state == "talking" and audio_mode == "wav":
            current_wav_path = self.runtime_bridge.config.response_audio_path
            if not current_wav_path.exists():
                LOGGER.warning(
                    "Skipping runtime_state talking audio_mode=wav because WAV is not ready: %s",
                    current_wav_path,
                )
                return
            payload["wav_location"] = current_wav_path.as_posix()
        send_json_to_unreal_blocking(payload, timeout=1.0)

    def _audio_mode_for_state(self, state: str, active_audio_mode: str) -> str:
        if state in {"idle", "listening", "thinking"}:
            return "none"
        if state == "talking" and active_audio_mode in {"realtime", "wav"}:
            return active_audio_mode
        return "none"

    def _get_current_mood(self) -> str:
        with self._lock:
            return self.current_mood

    def _emit_runtime_state(
        self,
        state_callback: StateCallback | None,
        state: AssistantState,
        *,
        mood: str,
        emotion_strength: float | None = None,
    ) -> None:
        clean_mood = normalize_assistant_mood(mood)
        with self._lock:
            self._current_state = state
            self.current_mood = clean_mood
            self.last_mood_update_time = time.time()

        try:
            self.runtime_bridge.set_runtime_state(state=state.value, mood=clean_mood)
            self._send_runtime_state_over_websocket(
                state.value,
                clean_mood,
                emotion_strength=emotion_strength,
            )
        except Exception:
            LOGGER.exception(
                "Could not write runtime state %s with mood %s",
                state.value,
                clean_mood,
            )

        if state_callback is not None:
            state_callback(state)

    def _wait_for_thinking_window(self, thinking_started_at: float | None) -> None:
        if thinking_started_at is None:
            return
        elapsed = time.monotonic() - thinking_started_at
        remaining = MIN_THINKING_SECONDS - elapsed
        if remaining > 0:
            LOGGER.info(
                "Delaying TALKING until thinking minimum is met: %.2fs",
                remaining,
            )
            time.sleep(remaining)

    def _mood_calm_loop(self) -> None:
        while not self._calm_stop_event.wait(self._mood_calm_check_interval()):
            self._apply_calm_timeout_if_due()

    def _mood_calm_check_interval(self) -> float:
        with self._lock:
            timeout_seconds = self.mood_calm_timeout_seconds
        return min(0.5, max(0.1, timeout_seconds / 10.0))

    def _apply_calm_timeout_if_due(self) -> None:
        now = time.time()
        with self._lock:
            if self._current_state != AssistantState.IDLE:
                return
            if self.current_mood == DEFAULT_MOOD:
                return

            last_activity_time = max(
                self.last_interaction_time,
                self.last_mood_update_time,
            )
            if now - last_activity_time < self.mood_calm_timeout_seconds:
                return

            self.current_mood = DEFAULT_MOOD
            self.last_mood_update_time = now

        try:
            self.runtime_bridge.set_runtime_state(
                state=AssistantState.IDLE.value,
                mood=DEFAULT_MOOD,
            )
            self._send_runtime_state_over_websocket(
                AssistantState.IDLE.value,
                DEFAULT_MOOD,
            )
            LOGGER.info(
                "[RuntimeBridge] mood_calm_timeout reached, mood=%s",
                DEFAULT_MOOD,
            )
        except Exception:
            LOGGER.exception("Could not write calm timeout runtime state")

    def _emit_state(
        self, state_callback: StateCallback | None, state: AssistantState
    ) -> None:
        with self._lock:
            self._current_state = state

        try:
            self.runtime_bridge.set_state(state.value)
            with self._lock:
                clean_mood = self.current_mood
            self._send_runtime_state_over_websocket(state.value, clean_mood)
        except Exception:
            LOGGER.exception("Could not write runtime state %s", state.value)

        if state_callback is not None:
            state_callback(state)
