from __future__ import annotations

import json
import logging
import sys
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.runtime_paths import get_project_root as get_runtime_project_root
from core.runtime_paths import get_runtime_paths
from core.language import (
    CURRENT_LANGUAGE_SETTING_ID,
    DEFAULT_CURRENT_LANGUAGE,
    normalize_current_language,
    require_current_language,
)
from core.personality import DEFAULT_PERSONALITY_PROMPT, parse_personality_traits


LOGGER = logging.getLogger(__name__)


OFFICIAL_SETTING_IDS: tuple[str, ...] = (
    "talk_hotkey",
    "interaction_mode",
    "neutral_return_time",
    "ui_volume",
    "avatar_voice_volume",
    "input_volume",
    "input_device",
    "output_device",
    "stt_engine",
    "current_language",
    "display_mode",
    "fps_limit",
    "performance_profile",
    "reset_settings_defaults",
    "reset_customization_defaults",
    "openai_model",
    "response_length",
    "history_level",
    "elevenlabs_model",
    "voice_speed",
    "tts_realtime",
    "listening_emotion_analysis",
    "system_connection_status",
    "usage_conversations_estimate",
    "usage_hours_estimate",
    "usage_profile",
)

CONFIGURABLE_SETTING_IDS: set[str] = {
    "talk_hotkey",
    "interaction_mode",
    "neutral_return_time",
    "ui_volume",
    "avatar_voice_volume",
    "input_volume",
    "input_device",
    "output_device",
    "stt_engine",
    "current_language",
    "display_mode",
    "fps_limit",
    "performance_profile",
    "openai_model",
    "response_length",
    "history_level",
    "elevenlabs_model",
    "voice_speed",
    "tts_realtime",
    "listening_emotion_analysis",
}

RUNTIME_BACKEND_UI_SETTING_IDS: set[str] = {
    "system_connection_status",
    "usage_conversations_estimate",
    "usage_hours_estimate",
    "usage_profile",
}

PASSIVE_GRAPHICS_SETTING_IDS: set[str] = {
    "display_mode",
    "fps_limit",
    "performance_profile",
}

CUSTOMIZATION_SETTING_IDS: set[str] = {
    "personality_traits",
    "use_custom_personality_prompt",
    "custom_personality_prompt",
    "profanity_filter",
    "voice_id",
    "use_custom_voice",
    "custom_voice_id",
    "selected_personality",
    "voice_style",
    "character_personality",
    "selected_character",
}

OFFICIAL_DEFAULT_SETTINGS: dict[str, Any] = {
    "talk_hotkey": "F8",
    "interaction_mode": "voice",
    "neutral_return_time": 45,
    "ui_volume": 0.5,
    "avatar_voice_volume": 0.8,
    "input_volume": 0.8,
    "input_device": "default",
    "output_device": "default",
    "stt_engine": "local",
    "current_language": DEFAULT_CURRENT_LANGUAGE,
    "display_mode": "borderless",
    "fps_limit": "60",
    "performance_profile": "balanced",
    "openai_model": "gpt-5.4-mini",
    "response_length": "short",
    "history_level": "normal",
    "elevenlabs_model": "eleven_turbo_v2_5",
    "voice_speed": "normal",
    "tts_realtime": False,
    "listening_emotion_analysis": True,
}

RESPONSE_LENGTH_MAX_WORDS: dict[str, int] = {
    "very_short": 36,
    "short": 64,
    "balanced": 112,
    "detailed": 176,
}

HISTORY_LEVEL_LAST_TURNS: dict[str, int] = {
    "minimal": 2,
    "light": 4,
    "normal": 8,
    "extended": 12,
}

OPENAI_MODEL_IDS: set[str] = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
}

OPENAI_LEGACY_MODEL_IDS: dict[str, str] = {
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.4-nano": "gpt-5.4-nano",
    "gpt-5": "gpt-5.5",
    "gpt-5-mini": "gpt-5.4-mini",
    "gpt-5.4 mini": "gpt-5.4-mini",
    "gpt-5.4 nano": "gpt-5.4-nano",
    "gpt-5 mini": "gpt-5.4-mini",
    "gpt-5 nano": "gpt-5.4-nano",
    "gpt-5.4mini": "gpt-5.4-mini",
    "gpt-5.4nano": "gpt-5.4-nano",
}

OPENAI_MODEL_PROFILES: dict[str, str] = {
    "gpt-5.5": "quality",
    "gpt-5.4": "balanced",
    "gpt-5.4-mini": "fast",
    "gpt-5.4-nano": "economy",
}

ELEVENLABS_MODEL_IDS: set[str] = {
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
    "eleven_multilingual_v2",
    "eleven_v3",
}

VOICE_SPEED_IDS: set[str] = {"slow", "normal", "fast", "very_fast"}
INTERACTION_MODE_IDS: set[str] = {"voice", "text"}
NEUTRAL_RETURN_TIME_IDS: set[int] = {15, 25, 45, 60}
STT_ENGINE_IDS: set[str] = {"deepgram", "local"}
DISPLAY_MODE_IDS: set[str] = {"windowed", "borderless", "fullscreen"}
FPS_LIMIT_IDS: set[str] = {"30", "60", "unlimited"}
PERFORMANCE_PROFILE_IDS: set[str] = {
    "performance",
    "balanced",
    "quality",
    "ultra",
}


DEFAULT_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"

DEFAULT_CUSTOMIZATION_SETTINGS: dict[str, Any] = {
    "personality_traits": "",
    "use_custom_personality_prompt": False,
    "custom_personality_prompt": DEFAULT_PERSONALITY_PROMPT,
    "profanity_filter": True,
    "voice_id": DEFAULT_ELEVENLABS_VOICE_ID,
    "use_custom_voice": False,
    "custom_voice_id": "",
    "selected_character": "",
    "selected_personality": "",
    "voice_style": "",
    "character_personality": "",
}


@dataclass(frozen=True)
class SettingUpdateResult:
    setting_id: str
    category: str
    value: Any
    persisted: bool


DEFAULT_SETTINGS: dict[str, Any] = {
    **OFFICIAL_DEFAULT_SETTINGS,
    "app": {
        "hotkey": OFFICIAL_DEFAULT_SETTINGS["talk_hotkey"],
        "interaction_mode": OFFICIAL_DEFAULT_SETTINGS["interaction_mode"],
        "neutral_return_time": OFFICIAL_DEFAULT_SETTINGS["neutral_return_time"],
        "state_poll_interval_ms": 100,
        "use_websocket_runtime_state": True,
    },
    "openai": {
        "model": OFFICIAL_DEFAULT_SETTINGS["openai_model"],
        "temperature": 0.4,
        "max_response_words": RESPONSE_LENGTH_MAX_WORDS[
            OFFICIAL_DEFAULT_SETTINGS["response_length"]
        ],
        "history_limit": HISTORY_LEVEL_LAST_TURNS[
            OFFICIAL_DEFAULT_SETTINGS["history_level"]
        ]
        * 2,
        "reasoning_effort": "low",
        "system_prompt": (
            "You are a concise conversational assistant. Return only valid JSON "
            "with this shape: {\"text\":\"assistant reply\",\"emotion\":\"neutral\"}. "
            "Use the user's language."
        ),
    },
    "elevenlabs": {
        "voice_id": DEFAULT_ELEVENLABS_VOICE_ID,
        "model_id": OFFICIAL_DEFAULT_SETTINGS["elevenlabs_model"],
        "output_format": "pcm_16000",
        "use_realtime_tts_streaming": OFFICIAL_DEFAULT_SETTINGS["tts_realtime"],
        "save_response_wav": True,
        "optimize_streaming_latency": 2,
        "websocket_audio_start_silence_chunks": 2,
        "websocket_audio_fade_in_ms": 15,
        "websocket_audio_chunk_ms": 200,
        "websocket_audio_realtime_pacing": True,
        "voice_speed": OFFICIAL_DEFAULT_SETTINGS["voice_speed"],
    },
    "deepgram": {
        "enabled": False,
        "api_key": "",
        "language": "es",
        "model": "nova-3",
        "sample_rate": 16000,
        "interim_results": True,
        "endpointing": True,
        "utterance_end_ms": 1000,
        "vad_events": True,
        "smart_format": True,
        "punctuate": True,
        "audio_block_ms": 50,
    },
    "audio": {
        "sample_rate": 16000,
        "auto_play": False,
        "ui_volume": OFFICIAL_DEFAULT_SETTINGS["ui_volume"],
        "avatar_voice_volume": OFFICIAL_DEFAULT_SETTINGS["avatar_voice_volume"],
        "input_volume": OFFICIAL_DEFAULT_SETTINGS["input_volume"],
    },
    "test_mode": {
        "enabled": False,
        "audio_enabled": False,
        "miralys_tokens_remaining": 0,
        "miralys_tokens_purchased": 0,
        "miralys_tokens_used": 0,
    },
    "usage_adaptation": {
        "enabled": True,
        "alpha": 0.25,
        "profiles": {
            "voice": {
                "samples": 0,
                "ema_user_words": 10.0,
                "ema_assistant_words": 32.0,
                "ema_turn_words": 42.0,
                "ema_cost_multiplier": 1.0,
                "last_update_utc": None,
            },
            "text": {
                "samples": 0,
                "ema_user_words": 8.0,
                "ema_assistant_words": 28.0,
                "ema_turn_words": 36.0,
                "ema_cost_multiplier": 1.0,
                "last_update_utc": None,
            },
        },
    },
}


DEFAULT_DEVICES: dict[str, Any] = {
    "input_device_index": None,
    "input_device_name": "",
    "saved_input_device_id": "default",
    "output_device_index": None,
    "output_device_name": "",
    "last_refresh_utc": None,
}


def get_project_root() -> Path:
    """Return the writable project root in source and PyInstaller builds."""
    return get_runtime_project_root()


class SettingsManager:
    """Loads and persists application settings without a database."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_project_root()
        runtime_paths = get_runtime_paths(self.root)
        self.config_dir = runtime_paths.config_root
        self.output_dir = runtime_paths.output_root
        self.audio_output_dir = self.output_dir / "audio"
        self.logs_output_dir = runtime_paths.logs_root
        self.settings_path = self.config_dir / "settings.json"
        self.devices_path = self.config_dir / "devices.json"
        self._lock = threading.RLock()

        self.ensure_directories()
        self._settings = self._load_or_create(self.settings_path, DEFAULT_SETTINGS)
        self._devices = self._load_or_create(self.devices_path, DEFAULT_DEVICES)
        self._drop_legacy_personality_settings(write_changes=True)
        self._sync_customization_defaults()
        self._sync_official_settings_from_nested()

    def ensure_directories(self) -> None:
        for path in (
            self.config_dir,
            self.output_dir,
            self.audio_output_dir,
            self.logs_output_dir,
            self.root / "assets",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._settings)

    def get_devices(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._devices)

    def get_setting(self, dotted_path: str, default: Any = None) -> Any:
        with self._lock:
            current: Any = self._settings
            for key in dotted_path.split("."):
                if not isinstance(current, dict) or key not in current:
                    return default
                current = current[key]
            return deepcopy(current)

    def set_setting(self, dotted_path: str, value: Any) -> None:
        clean_path = str(dotted_path or "").strip()
        if clean_path == "personality" or clean_path.startswith("personality."):
            LOGGER.info("Ignoring legacy personality setting write: %s", clean_path)
            return
        with self._lock:
            self._set_nested_value(self._settings, clean_path, value)
            self._drop_legacy_personality_settings(write_changes=False)
            self._write_json(self.settings_path, self._settings)

    def update_settings(self, patch: dict[str, Any]) -> None:
        with self._lock:
            clean_patch = deepcopy(patch)
            clean_patch.pop("personality", None)
            self._settings = self._deep_merge(self._settings, clean_patch)
            self._drop_legacy_personality_settings(write_changes=False)
            self._sync_customization_defaults(write_changes=False)
            self._sync_official_from_nested_values(self._settings)
            self._write_json(self.settings_path, self._settings)

    def apply_settings_update(self, setting: str, value: Any) -> SettingUpdateResult:
        setting_id = str(setting or "").strip()
        if setting_id in CONFIGURABLE_SETTING_IDS:
            normalized = self.apply_official_setting_update(setting_id, value)
            return SettingUpdateResult(
                setting_id=setting_id,
                category="settings",
                value=normalized,
                persisted=True,
            )
        if setting_id in CUSTOMIZATION_SETTING_IDS:
            normalized = self.apply_customization_setting_update(setting_id, value)
            return SettingUpdateResult(
                setting_id=setting_id,
                category="customization",
                value=normalized,
                persisted=True,
            )
        if setting_id in RUNTIME_BACKEND_UI_SETTING_IDS:
            return SettingUpdateResult(
                setting_id=setting_id,
                category="runtime_backend_ui",
                value=value,
                persisted=False,
            )
        raise ValueError(f"Unsupported setting id: {setting_id}")

    def apply_official_setting_update(self, setting: str, value: Any) -> Any:
        setting_id = str(setting or "").strip()
        if setting_id not in CONFIGURABLE_SETTING_IDS:
            raise ValueError(f"Unsupported setting id: {setting_id}")

        normalized = normalize_official_setting_value(setting_id, value)
        with self._lock:
            self._settings[setting_id] = normalized
            self._apply_official_setting_to_nested(
                self._settings,
                setting_id,
                normalized,
            )
            self._write_json(self.settings_path, self._settings)
        return normalized

    def apply_customization_setting_update(self, setting: str, value: Any) -> Any:
        setting_id = str(setting or "").strip()
        if setting_id not in CUSTOMIZATION_SETTING_IDS:
            raise ValueError(f"Unsupported customization id: {setting_id}")

        normalized = normalize_customization_setting_value(setting_id, value)
        if setting_id == "personality_traits":
            parse_personality_traits(normalized)
        with self._lock:
            self._sync_customization_defaults(write_changes=False)
            customization = self._settings.setdefault("customization", {})
            if not isinstance(customization, dict):
                customization = {}
                self._settings["customization"] = customization
            customization[setting_id] = normalized
            self._write_json(self.settings_path, self._settings)
        return normalized

    def reset_settings_defaults(self) -> dict[str, Any]:
        with self._lock:
            for setting_id, value in OFFICIAL_DEFAULT_SETTINGS.items():
                clean_value = deepcopy(value)
                self._settings[setting_id] = clean_value
                self._apply_official_setting_to_nested(
                    self._settings,
                    setting_id,
                    clean_value,
                )
            self._write_json(self.settings_path, self._settings)

            self._devices.update(
                {
                    "input_device_index": None,
                    "input_device_name": "",
                    "saved_input_device_id": "default",
                    "output_device_index": None,
                    "output_device_name": "",
                }
            )
            self._write_json(self.devices_path, self._devices)
            return deepcopy(self._settings)

    def reset_official_defaults(self) -> dict[str, Any]:
        return self.reset_settings_defaults()

    def reset_customization_defaults(self) -> dict[str, Any]:
        with self._lock:
            self._settings["customization"] = self._customization_defaults()
            self._write_json(self.settings_path, self._settings)
            return deepcopy(self._settings)

    def update_device_selection(
        self,
        *,
        input_device_index: int | None,
        input_device_name: str,
        output_device_index: int | None,
        output_device_name: str,
    ) -> None:
        with self._lock:
            self._devices.update(
                {
                    "input_device_index": input_device_index,
                    "input_device_name": input_device_name,
                    "saved_input_device_id": device_id_from_index(input_device_index),
                    "output_device_index": output_device_index,
                    "output_device_name": output_device_name,
                }
            )
            self._write_json(self.devices_path, self._devices)
            self._settings["input_device"] = device_id_from_index(input_device_index)
            self._settings["output_device"] = device_id_from_index(output_device_index)
            self._write_json(self.settings_path, self._settings)

    def update_devices_metadata(self, patch: dict[str, Any]) -> None:
        with self._lock:
            self._devices.update(patch)
            self._write_json(self.devices_path, self._devices)

    def _load_or_create(self, path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            data = deepcopy(defaults)
            self._write_json(path, data)
            return data

        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{path.name} must contain a JSON object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            LOGGER.exception("Could not read %s. Recreating defaults.", path)
            loaded = {}

        merged = self._deep_merge(defaults, loaded)
        if merged != loaded:
            self._write_json(path, merged)
        return merged

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _deep_merge(
        self, defaults: dict[str, Any], overrides: dict[str, Any]
    ) -> dict[str, Any]:
        merged = deepcopy(defaults)
        for key, value in overrides.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = self._deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged

    def _set_nested_value(self, data: dict[str, Any], dotted_path: str, value: Any) -> None:
        keys = [key for key in dotted_path.split(".") if key]
        if not keys:
            raise ValueError("A setting path is required")

        current = data
        for key in keys[:-1]:
            next_value = current.setdefault(key, {})
            if not isinstance(next_value, dict):
                next_value = {}
                current[key] = next_value
            current = next_value
        current[keys[-1]] = value

    def _sync_official_settings_from_nested(self) -> None:
        with self._lock:
            before = deepcopy(self._settings)
            self._sync_customization_defaults(write_changes=False)
            self._sync_official_from_nested_values(self._settings)
            self._settings["input_device"] = device_id_from_index(
                self._devices.get("input_device_index")
            )
            self._settings["output_device"] = device_id_from_index(
                self._devices.get("output_device_index")
            )
            for setting_id, value in OFFICIAL_DEFAULT_SETTINGS.items():
                self._apply_official_setting_to_nested(
                    self._settings,
                    setting_id,
                    self._settings.get(setting_id, value),
                )
            if self._settings != before:
                self._write_json(self.settings_path, self._settings)

    def _sync_customization_defaults(self, *, write_changes: bool = True) -> None:
        before = deepcopy(self._settings)
        customization = self._settings.get("customization")
        if not isinstance(customization, dict):
            customization = {}
            self._settings["customization"] = customization

        for key, value in self._customization_defaults().items():
            customization.setdefault(key, deepcopy(value))

        if write_changes and self._settings != before:
            self._write_json(self.settings_path, self._settings)

    def _drop_legacy_personality_settings(self, *, write_changes: bool) -> None:
        if "personality" not in self._settings:
            return
        with self._lock:
            self._settings.pop("personality", None)
            if write_changes:
                self._write_json(self.settings_path, self._settings)

    def _customization_defaults(self) -> dict[str, Any]:
        defaults = deepcopy(DEFAULT_CUSTOMIZATION_SETTINGS)
        elevenlabs_settings = self._settings.get("elevenlabs", {})
        if isinstance(elevenlabs_settings, dict):
            voice_id = str(elevenlabs_settings.get("voice_id", "")).strip()
            if voice_id:
                defaults["voice_id"] = voice_id
        return defaults

    def _sync_official_from_nested_values(self, settings: dict[str, Any]) -> None:
        settings[CURRENT_LANGUAGE_SETTING_ID] = normalize_current_language(
            settings.get(CURRENT_LANGUAGE_SETTING_ID, DEFAULT_CURRENT_LANGUAGE)
        )

        app_settings = settings.get("app", {})
        if isinstance(app_settings, dict):
            settings["talk_hotkey"] = str(
                app_settings.get("hotkey", settings.get("talk_hotkey", "F8"))
            )
            if "interaction_mode" in app_settings:
                settings["interaction_mode"] = _value_or_default(
                    str(app_settings.get("interaction_mode", "")),
                    INTERACTION_MODE_IDS,
                    OFFICIAL_DEFAULT_SETTINGS["interaction_mode"],
                )
            if "neutral_return_time" in app_settings:
                settings["neutral_return_time"] = _int_value_or_default(
                    app_settings.get("neutral_return_time"),
                    NEUTRAL_RETURN_TIME_IDS,
                    OFFICIAL_DEFAULT_SETTINGS["neutral_return_time"],
                )

        openai_settings = settings.get("openai", {})
        if isinstance(openai_settings, dict):
            model = str(openai_settings.get("model", "")).strip()
            normalized_model = normalize_openai_model_id(
                model,
                default=str(
                    settings.get("openai_model", OFFICIAL_DEFAULT_SETTINGS["openai_model"])
                ),
            )
            settings["openai_model"] = normalized_model
            openai_settings["model"] = normalized_model
            max_words = openai_settings.get("max_response_words")
            settings["response_length"] = _nearest_key_for_int(
                max_words,
                RESPONSE_LENGTH_MAX_WORDS,
                str(
                    settings.get(
                        "response_length",
                        OFFICIAL_DEFAULT_SETTINGS["response_length"],
                    )
                ),
            )
            history_limit = openai_settings.get("history_limit")
            settings["history_level"] = _nearest_key_for_int(
                _messages_to_turns(history_limit),
                HISTORY_LEVEL_LAST_TURNS,
                str(settings.get("history_level", "normal")),
            )

        elevenlabs_settings = settings.get("elevenlabs", {})
        if isinstance(elevenlabs_settings, dict):
            model_id = str(elevenlabs_settings.get("model_id", "")).strip()
            if model_id in ELEVENLABS_MODEL_IDS:
                settings["elevenlabs_model"] = model_id
            settings["voice_speed"] = _value_or_default(
                str(elevenlabs_settings.get("voice_speed", settings.get("voice_speed", ""))),
                VOICE_SPEED_IDS,
                OFFICIAL_DEFAULT_SETTINGS["voice_speed"],
            )
            settings["tts_realtime"] = _parse_bool(
                elevenlabs_settings.get(
                    "use_realtime_tts_streaming",
                    settings.get("tts_realtime", True),
                )
            )

        deepgram_settings = settings.get("deepgram", {})
        if isinstance(deepgram_settings, dict):
            settings["stt_engine"] = (
                "deepgram" if _parse_bool(deepgram_settings.get("enabled", True)) else "local"
            )

        audio_settings = settings.get("audio", {})
        if isinstance(audio_settings, dict):
            for setting_id in ("ui_volume", "avatar_voice_volume", "input_volume"):
                if setting_id in audio_settings:
                    settings[setting_id] = _clamp_float(audio_settings[setting_id])

    def _apply_official_setting_to_nested(
        self,
        settings: dict[str, Any],
        setting_id: str,
        value: Any,
    ) -> None:
        app_settings = settings.setdefault("app", {})
        openai_settings = settings.setdefault("openai", {})
        elevenlabs_settings = settings.setdefault("elevenlabs", {})
        deepgram_settings = settings.setdefault("deepgram", {})
        audio_settings = settings.setdefault("audio", {})

        if setting_id == "talk_hotkey":
            app_settings["hotkey"] = str(value)
        elif setting_id == "interaction_mode":
            app_settings["interaction_mode"] = str(value)
        elif setting_id == "neutral_return_time":
            app_settings["neutral_return_time"] = int(value)
        elif setting_id in {"ui_volume", "avatar_voice_volume", "input_volume"}:
            audio_settings[setting_id] = float(value)
        elif setting_id == "stt_engine":
            deepgram_settings["enabled"] = str(value) == "deepgram"
        elif setting_id == CURRENT_LANGUAGE_SETTING_ID:
            settings[CURRENT_LANGUAGE_SETTING_ID] = str(value)
        elif setting_id == "openai_model":
            openai_settings["model"] = str(value)
        elif setting_id == "response_length":
            openai_settings["max_response_words"] = RESPONSE_LENGTH_MAX_WORDS[str(value)]
        elif setting_id == "history_level":
            openai_settings["history_limit"] = HISTORY_LEVEL_LAST_TURNS[str(value)] * 2
        elif setting_id == "elevenlabs_model":
            elevenlabs_settings["model_id"] = str(value)
        elif setting_id == "voice_speed":
            elevenlabs_settings["voice_speed"] = str(value)
        elif setting_id == "tts_realtime":
            elevenlabs_settings["use_realtime_tts_streaming"] = bool(value)
        elif setting_id == "listening_emotion_analysis":
            settings["listening_emotion_analysis"] = bool(value)


def normalize_official_setting_value(setting_id: str, value: Any) -> Any:
    if setting_id in {"tts_realtime", "listening_emotion_analysis"}:
        return _parse_bool(value)
    if setting_id in {"ui_volume", "avatar_voice_volume", "input_volume"}:
        return _clamp_float(value)
    if setting_id in {"input_device", "output_device"}:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError(f"{setting_id} requires a non-empty OptionId")
        if clean != "default" and not clean.startswith("device_"):
            raise ValueError(f"{setting_id} requires an official OptionId")
        return device_id_from_index(device_index_from_id(clean))
    if setting_id == "talk_hotkey":
        clean = str(value or "").strip()
        if not clean:
            raise ValueError("talk_hotkey requires a non-empty value")
        return clean
    if setting_id == "interaction_mode":
        return _require_option(setting_id, value, INTERACTION_MODE_IDS)
    if setting_id == "neutral_return_time":
        return _require_int_option(setting_id, value, NEUTRAL_RETURN_TIME_IDS)
    if setting_id == "stt_engine":
        return _require_option(setting_id, value, STT_ENGINE_IDS)
    if setting_id == CURRENT_LANGUAGE_SETTING_ID:
        return require_current_language(value)
    if setting_id == "display_mode":
        return _require_option(setting_id, value, DISPLAY_MODE_IDS)
    if setting_id == "fps_limit":
        return _require_option(setting_id, value, FPS_LIMIT_IDS)
    if setting_id == "performance_profile":
        return _require_option(setting_id, value, PERFORMANCE_PROFILE_IDS)
    if setting_id == "openai_model":
        clean = normalize_openai_model_id(value)
        if clean is None:
            raise ValueError(f"Invalid openai_model: {value!r}")
        return clean
    if setting_id == "response_length":
        return _require_option(setting_id, value, set(RESPONSE_LENGTH_MAX_WORDS))
    if setting_id == "history_level":
        return _require_option(setting_id, value, set(HISTORY_LEVEL_LAST_TURNS))
    if setting_id == "elevenlabs_model":
        return _require_option(setting_id, value, ELEVENLABS_MODEL_IDS)
    if setting_id == "voice_speed":
        return _require_option(setting_id, value, VOICE_SPEED_IDS)
    raise ValueError(f"Unsupported setting id: {setting_id}")


def normalize_customization_setting_value(setting_id: str, value: Any) -> Any:
    if setting_id in {
        "profanity_filter",
        "use_custom_voice",
        "use_custom_personality_prompt",
    }:
        return _parse_bool(value)
    if setting_id in {
        "personality_traits",
        "custom_personality_prompt",
        "voice_id",
        "custom_voice_id",
        "selected_personality",
        "voice_style",
        "character_personality",
        "selected_character",
    }:
        return str(value or "").strip()
    raise ValueError(f"Unsupported customization id: {setting_id}")


def device_id_from_index(index: int | None) -> str:
    if index is None:
        return "default"
    return f"device_{index}"


def device_index_from_id(device_id: str) -> int | None:
    clean = str(device_id or "").strip()
    if not clean or clean == "default":
        return None
    if clean.startswith("device_"):
        clean = clean[len("device_") :]
    try:
        return int(clean)
    except ValueError as exc:
        raise ValueError(f"Invalid audio device OptionId: {device_id!r}") from exc


def _parse_bool(value: Any) -> bool:
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
    raise ValueError(f"Invalid boolean value: {value!r}")


def _clamp_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid float value: {value!r}") from exc
    return max(0.0, min(1.0, parsed))


def _require_option(setting_id: str, value: Any, allowed: set[str]) -> str:
    clean = str(value or "").strip()
    if clean not in allowed:
        raise ValueError(f"Invalid {setting_id}: {value!r}")
    return clean


def _value_or_default(value: str, allowed: set[str], default: Any) -> str:
    return value if value in allowed else str(default)


def _int_value_or_default(value: Any, allowed: set[int], default: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed in allowed else int(default)


def _require_int_option(setting_id: str, value: Any, allowed: set[int]) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {setting_id}: {value!r}") from exc
    if parsed not in allowed:
        raise ValueError(f"Invalid {setting_id}: {value!r}")
    return parsed


def _nearest_key_for_int(
    value: Any,
    options: dict[str, int],
    default_key: str,
) -> str:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default_key if default_key in options else next(iter(options))
    return min(options, key=lambda key: abs(options[key] - parsed))


def _messages_to_turns(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(1, (parsed + 1) // 2)


def normalize_openai_model_id(
    value: Any,
    *,
    default: str | None = None,
) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return default

    canonical = OPENAI_LEGACY_MODEL_IDS.get(clean.lower(), clean.lower())
    if canonical in OPENAI_MODEL_IDS:
        return canonical
    return default


def openai_model_profile(model_id: Any) -> str:
    canonical = normalize_openai_model_id(
        model_id,
        default=OFFICIAL_DEFAULT_SETTINGS["openai_model"],
    )
    if canonical is None:
        canonical = OFFICIAL_DEFAULT_SETTINGS["openai_model"]
    return OPENAI_MODEL_PROFILES.get(canonical, "balanced")
