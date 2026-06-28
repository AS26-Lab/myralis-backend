from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    sd = None  # type: ignore[assignment]

try:
    import soundfile as sf
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    sf = None  # type: ignore[assignment]

from core.settings_manager import (
    SettingsManager,
    device_id_from_index,
    device_index_from_id,
)


LOGGER = logging.getLogger(__name__)


class AudioManagerError(RuntimeError):
    """Raised when local audio devices or playback fail."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int

    @property
    def label(self) -> str:
        return f"{self.index} - {self.name}"


@dataclass(frozen=True)
class VoiceCaptureConfig:
    hotkey: str
    input_device_index: int | None
    output_device_index: int | None
    sample_rate: int
    input_volume: float


class AudioManager:
    """Manages audio devices, playback, and future voice capture settings."""

    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager
        self._level_lock = threading.RLock()
        self._input_level = 0.0
        self._output_level = 0.0
        self._level_stream: Any | None = None
        self._level_stream_active = False
        self._level_stream_input_volume = 1.0

    def list_devices(self) -> list[AudioDevice]:
        if sd is None:
            raise AudioManagerError(
                "sounddevice is not installed. Run pip install -r requirements.txt."
            )

        try:
            raw_devices = sd.query_devices()
        except Exception as exc:
            LOGGER.exception("Could not query audio devices")
            raise AudioManagerError(f"Could not query audio devices: {exc}") from exc

        devices: list[AudioDevice] = []
        for index, device in enumerate(raw_devices):
            devices.append(
                AudioDevice(
                    index=index,
                    name=str(device.get("name", f"Device {index}")),
                    max_input_channels=int(device.get("max_input_channels", 0)),
                    max_output_channels=int(device.get("max_output_channels", 0)),
                    default_sample_rate=int(float(device.get("default_samplerate", 16000))),
                )
            )

        self.settings_manager.update_devices_metadata(
            {"last_refresh_utc": datetime.now(timezone.utc).isoformat()}
        )
        return devices

    def list_input_devices(self) -> list[AudioDevice]:
        return [device for device in self.list_devices() if device.max_input_channels > 0]

    def list_output_devices(self) -> list[AudioDevice]:
        return [device for device in self.list_devices() if device.max_output_channels > 0]

    def build_audio_devices_payload(self) -> dict[str, list[str] | str]:
        input_labels = ["Predeterminado"]
        input_ids = ["default"]
        output_labels = ["Predeterminado"]
        output_ids = ["default"]

        for device in self.list_input_devices():
            input_labels.append(device.name)
            input_ids.append(device_id_from_index(device.index))
        for device in self.list_output_devices():
            output_labels.append(device.name)
            output_ids.append(device_id_from_index(device.index))

        return {
            "type": "audio_devices",
            "input_labels": input_labels,
            "input_ids": input_ids,
            "output_labels": output_labels,
            "output_ids": output_ids,
        }

    def save_selected_devices(
        self,
        *,
        input_device_index: int | None,
        input_device_name: str,
        output_device_index: int | None,
        output_device_name: str,
    ) -> None:
        self.settings_manager.update_device_selection(
            input_device_index=input_device_index,
            input_device_name=input_device_name,
            output_device_index=output_device_index,
            output_device_name=output_device_name,
        )

    def save_input_device_option_id(self, option_id: str) -> None:
        index = device_index_from_id(option_id)
        devices = self.settings_manager.get_devices()
        self.save_selected_devices(
            input_device_index=index,
            input_device_name=self._device_name_for_index(index, input_device=True),
            output_device_index=devices.get("output_device_index"),
            output_device_name=str(devices.get("output_device_name", "")),
        )

    def save_output_device_option_id(self, option_id: str) -> None:
        index = device_index_from_id(option_id)
        devices = self.settings_manager.get_devices()
        self.save_selected_devices(
            input_device_index=devices.get("input_device_index"),
            input_device_name=str(devices.get("input_device_name", "")),
            output_device_index=index,
            output_device_name=self._device_name_for_index(index, input_device=False),
        )

    def play_audio(self, audio_path: Path, output_device_index: int | None) -> None:
        if sd is None or sf is None:
            raise AudioManagerError(
                "sounddevice and soundfile are required for playback. "
                "Run pip install -r requirements.txt."
            )
        if not audio_path.exists():
            raise AudioManagerError(f"Audio file does not exist: {audio_path}")

        try:
            data, samplerate = sf.read(str(audio_path), dtype="float32")
            self._play_data_with_level_meter(data, samplerate, output_device_index)
        except Exception as exc:
            LOGGER.exception("Audio playback failed")
            raise AudioManagerError(f"Audio playback failed: {exc}") from exc
        finally:
            self._set_output_level(0.0)

    def start_input_level_monitor(
        self,
        *,
        input_device_index: int | None,
        sample_rate: int,
        input_volume: float = 1.0,
    ) -> bool:
        if sd is None:
            LOGGER.warning("sounddevice is not installed; input level monitor disabled")
            return False

        self.stop_input_level_monitor()
        self._set_input_level(0.0)
        with self._level_lock:
            self._level_stream_input_volume = max(0.0, min(1.0, float(input_volume)))

        try:
            stream = sd.InputStream(
                device=input_device_index,
                channels=1,
                samplerate=sample_rate,
                dtype="float32",
                blocksize=512,
                callback=self._handle_input_level_block,
            )
            stream.start()
        except Exception as exc:
            LOGGER.warning("Could not start input level monitor: %s", exc)
            self._set_input_level(0.0)
            return False

        with self._level_lock:
            self._level_stream = stream
            self._level_stream_active = True
        LOGGER.info("Input level monitor started for device=%s", input_device_index)
        return True

    def stop_input_level_monitor(self) -> None:
        with self._level_lock:
            stream = self._level_stream
            self._level_stream = None
            self._level_stream_active = False
            self._input_level = 0.0

        if stream is None:
            return

        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            LOGGER.warning("Could not stop input level monitor cleanly: %s", exc)

    def get_input_level(self) -> float:
        with self._level_lock:
            return self._input_level

    def set_input_level_from_capture(self, level: float) -> None:
        self._set_input_level(level)

    def is_input_level_monitor_active(self) -> bool:
        with self._level_lock:
            return bool(self._level_stream_active)

    def get_output_level(self) -> float:
        with self._level_lock:
            return self._output_level

    def build_voice_capture_config(self) -> VoiceCaptureConfig:
        settings = self.settings_manager.get_settings()
        devices = self.settings_manager.get_devices()
        return VoiceCaptureConfig(
            hotkey=str(settings["app"]["hotkey"]),
            input_device_index=devices.get("input_device_index"),
            output_device_index=devices.get("output_device_index"),
            sample_rate=int(settings["audio"]["sample_rate"]),
            input_volume=float(settings.get("input_volume", 1.0)),
        )

    def _handle_input_level_block(
        self,
        indata: Any,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        if status:
            LOGGER.debug("Input level monitor status: %s", status)

        try:
            rms = float((indata**2).mean() ** 0.5)
        except Exception:
            rms = 0.0

        with self._level_lock:
            volume = self._level_stream_input_volume
        level = max(0.0, min(1.0, rms * 8.0 * volume))
        with self._level_lock:
            if level < self._input_level:
                level = max(level, self._input_level * 0.82)
            self._input_level = level

    def _set_input_level(self, level: float) -> None:
        with self._level_lock:
            self._input_level = max(0.0, min(1.0, level))

    def _play_data_with_level_meter(
        self,
        data: Any,
        samplerate: int,
        output_device_index: int | None,
    ) -> None:
        if getattr(data, "ndim", 1) == 1:
            data = data.reshape(-1, 1)

        channels = int(data.shape[1])
        position = 0
        playback_done = threading.Event()
        playback_error: list[Exception] = []

        def callback(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal position
            try:
                if status:
                    LOGGER.debug("Output stream status: %s", status)

                remaining = len(data) - position
                frames_to_copy = min(frames, remaining)
                if frames_to_copy > 0:
                    chunk = data[position : position + frames_to_copy]
                    outdata[:frames_to_copy] = chunk
                    self._set_output_level_from_block(chunk)
                    position += frames_to_copy

                if frames_to_copy < frames:
                    outdata[frames_to_copy:] = 0
                    self._set_output_level(0.0)
                    playback_done.set()
                    raise sd.CallbackStop()
            except sd.CallbackStop:
                raise
            except Exception as exc:
                playback_error.append(exc)
                playback_done.set()
                raise sd.CallbackAbort()

        try:
            with sd.OutputStream(
                device=output_device_index,
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                callback=callback,
            ):
                playback_done.wait()
        except Exception as exc:
            playback_error.append(exc)

        if playback_error:
            raise playback_error[0]

    def _set_output_level_from_block(self, block: Any) -> None:
        try:
            rms = float((block**2).mean() ** 0.5)
        except Exception:
            rms = 0.0

        level = max(0.0, min(1.0, rms * 8.0))
        with self._level_lock:
            if level < self._output_level:
                level = max(level, self._output_level * 0.82)
            self._output_level = level

    def _set_output_level(self, level: float) -> None:
        with self._level_lock:
            self._output_level = max(0.0, min(1.0, level))

    def _device_name_for_index(self, index: int | None, *, input_device: bool) -> str:
        if index is None:
            return ""
        try:
            devices = self.list_input_devices() if input_device else self.list_output_devices()
        except AudioManagerError:
            return device_id_from_index(index)
        for device in devices:
            if device.index == index:
                return device.label
        return device_id_from_index(index)
