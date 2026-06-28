from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.mood import DEFAULT_MOOD, normalize_mood


LOGGER = logging.getLogger(__name__)

VALID_STATES = {"IDLE", "LISTENING", "THINKING", "TALKING"}
_DEFAULT_BRIDGE: RuntimeBridge | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    runtime_dir: Path
    state_path: Path
    state_txt_path: Path
    response_audio_path: Path

    @classmethod
    def from_root(cls, root: Path) -> RuntimeConfig:
        runtime_dir = root / "output" / "runtime"
        return cls(
            runtime_dir=runtime_dir,
            state_path=runtime_dir / "runtime_state.json",
            state_txt_path=runtime_dir / "state.txt",
            response_audio_path=runtime_dir / "current_response.wav",
        )


class RuntimeBridge:
    """Maintains shared runtime files consumed by Unreal Engine."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._state = "IDLE"
        self._mood = DEFAULT_MOOD
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.set_runtime_state(state="IDLE", mood=DEFAULT_MOOD)

    def set_state(self, state: str) -> None:
        self.set_runtime_state(state=state)

    def set_mood(self, mood: str) -> None:
        self.set_runtime_state(mood=mood)

    def set_runtime_state(
        self,
        state: str | None = None,
        mood: str | None = None,
    ) -> None:
        with self._lock:
            clean_state = self._state
            if state is not None:
                clean_state = state.strip().upper()
                if clean_state not in VALID_STATES:
                    raise ValueError(f"Invalid runtime state: {state}")

            clean_mood = self._mood
            if mood is not None:
                clean_mood = normalize_mood(mood)
                if clean_mood == DEFAULT_MOOD and mood.strip().lower() != DEFAULT_MOOD.lower():
                    LOGGER.warning(
                        "[RuntimeBridge] invalid mood=%s, falling back to %s",
                        mood,
                        DEFAULT_MOOD,
                    )

            self._state = clean_state
            self._mood = clean_mood

            payload = {
                "state": clean_state,
                "mood": clean_mood,
                "timestamp": int(time.time()),
            }
            self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._write_json_atomic(self.config.state_path, payload)
            self._write_state_txt(clean_state)
            LOGGER.info("[RuntimeBridge] state=%s mood=%s", clean_state, clean_mood)

    def get_runtime_state(self) -> dict[str, str]:
        with self._lock:
            return {
                "state": self._state,
                "mood": self._mood,
            }

    def publish_response_audio(self, audio_path: Path) -> Path:
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file does not exist: {audio_path}")

        with self._lock:
            self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
            destination = self.config.response_audio_path
            if audio_path.resolve() == destination.resolve():
                return destination

            temp_path = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(audio_path, temp_path)
            temp_path.replace(destination)
            LOGGER.info("Published runtime audio: %s", destination)
            return destination

    def _write_json_atomic(self, path: Path, payload: dict[str, object]) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _write_state_txt(self, state: str) -> None:
        temp_path = self.config.state_txt_path.with_suffix(
            self.config.state_txt_path.suffix + ".tmp"
        )
        temp_path.write_text(state + "\n", encoding="utf-8")
        temp_path.replace(self.config.state_txt_path)


def configure_runtime_bridge(runtime_bridge: RuntimeBridge) -> None:
    global _DEFAULT_BRIDGE
    _DEFAULT_BRIDGE = runtime_bridge


def set_state(state: str) -> None:
    if _DEFAULT_BRIDGE is None:
        raise RuntimeError("RuntimeBridge has not been configured.")
    _DEFAULT_BRIDGE.set_state(state)


def set_mood(mood: str) -> None:
    if _DEFAULT_BRIDGE is None:
        raise RuntimeError("RuntimeBridge has not been configured.")
    _DEFAULT_BRIDGE.set_mood(mood)


def set_runtime_state(
    state: str | None = None,
    mood: str | None = None,
) -> None:
    if _DEFAULT_BRIDGE is None:
        raise RuntimeError("RuntimeBridge has not been configured.")
    _DEFAULT_BRIDGE.set_runtime_state(state=state, mood=mood)
