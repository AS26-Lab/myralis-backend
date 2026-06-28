from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.elevenlabs_manager import TTSResult
from core.openai_manager import AIResponse
from core.settings_manager import SettingsManager


LOGGER = logging.getLogger(__name__)


class TestModeManager:
    """Caches one OpenAI text response and one ElevenLabs WAV during development."""

    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager
        self.response_cache_path = settings_manager.logs_output_dir / "test_mode_response.json"
        self.audio_cache_path = settings_manager.audio_output_dir / "test_mode_response.wav"

    def is_enabled(self, settings: dict[str, Any] | None = None) -> bool:
        active_settings = settings or self.settings_manager.get_settings()
        return bool(active_settings.get("test_mode", {}).get("enabled", False))

    def is_audio_enabled(self, settings: dict[str, Any] | None = None) -> bool:
        active_settings = settings or self.settings_manager.get_settings()
        return bool(active_settings.get("test_mode", {}).get("audio_enabled", False))

    def load_response(self) -> AIResponse | None:
        if not self.response_cache_path.exists():
            return None
        try:
            payload = json.loads(self.response_cache_path.read_text(encoding="utf-8"))
            text = str(payload.get("text", "")).strip()
            if not text:
                return None
            return AIResponse(
                text=text,
                emotion=str(payload.get("emotion", "neutral")),
                raw_text=str(payload.get("raw_text", text)),
                model=str(payload.get("model", "")),
                from_cache=True,
            )
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Ignoring invalid test mode response cache: %s", exc)
            return None

    def save_response(self, response: AIResponse) -> None:
        payload = {
            "text": response.text,
            "emotion": response.emotion,
            "raw_text": response.raw_text,
            "model": response.model,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.response_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.response_cache_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_audio(self) -> TTSResult | None:
        if not self.audio_cache_path.exists():
            return None
        return TTSResult(audio_path=self.audio_cache_path, from_cache=True)

    def save_audio(self, audio_path: Path) -> TTSResult:
        self.audio_cache_path.parent.mkdir(parents=True, exist_ok=True)
        if audio_path.resolve() != self.audio_cache_path.resolve():
            shutil.copyfile(audio_path, self.audio_cache_path)
        return TTSResult(audio_path=self.audio_cache_path, from_cache=True)

    def cache_summary(self) -> dict[str, Any]:
        return {
            "text_cached": self.response_cache_path.exists(),
            "audio_cached": self.audio_cache_path.exists(),
            "response_cache_path": str(self.response_cache_path),
            "audio_cache_path": str(self.audio_cache_path),
        }

    def clear_cache(self) -> None:
        for path in (self.response_cache_path, self.audio_cache_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                LOGGER.warning("Could not remove test mode cache file %s: %s", path, exc)
