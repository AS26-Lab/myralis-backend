from __future__ import annotations

import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    np = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    sd = None  # type: ignore[assignment]

try:
    import soundfile as sf
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    sf = None  # type: ignore[assignment]

try:  # pragma: no cover - Windows only.
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms.
    winreg = None  # type: ignore[assignment]

from core.settings_manager import (
    SettingsManager,
    device_id_from_index,
    device_index_from_id,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_PLAYBACK_TAIL_SETTLE_SECONDS = 0.08
DEFAULT_UI_BEEP_FREQUENCY_HZ = 880.0
DEFAULT_UI_BEEP_DURATION_SECONDS = 0.09
DEFAULT_UI_BEEP_FADE_SECONDS = 0.012
DEFAULT_UI_BEEP_SAMPLE_RATE = 24000
WINDOWS_MMDEVICES_CAPTURE_PATH = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
)
WINDOWS_DEVICE_FRIENDLY_NAME_KEY = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
WINDOWS_DEVICE_STATE_ACTIVE = 0x00000001


class AudioManagerError(RuntimeError):
    """Raised when local audio devices or playback fail."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int
    hostapi_name: str = ""

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
        self._level_stream_sample_rate = 0

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

        hostapis_by_index = self._hostapis_by_index()

        devices: list[AudioDevice] = []
        for index, device in enumerate(raw_devices):
            hostapi_index = int(device.get("hostapi", -1))
            devices.append(
                AudioDevice(
                    index=index,
                    name=str(device.get("name", f"Device {index}")),
                    max_input_channels=int(device.get("max_input_channels", 0)),
                    max_output_channels=int(device.get("max_output_channels", 0)),
                    default_sample_rate=int(float(device.get("default_samplerate", 16000))),
                    hostapi_name=str(hostapis_by_index.get(hostapi_index, "")),
                )
            )

        self.settings_manager.update_devices_metadata(
            {"last_refresh_utc": datetime.now(timezone.utc).isoformat()}
        )
        return devices

    def list_input_devices(self) -> list[AudioDevice]:
        devices = self._filter_devices_to_preferred_hostapi(self.list_devices())
        active_windows_names = self._windows_active_capture_device_names()
        seen_names: dict[str, AudioDevice] = {}

        for device in devices:
            if device.max_input_channels <= 0:
                continue
            if active_windows_names and not self._matches_active_windows_capture(
                device.name,
                active_windows_names,
            ):
                continue

            device_key = self._normalized_device_key(device.name)
            current_device = seen_names.get(device_key)
            if current_device is None or self._is_preferred_input_device(device, current_device):
                seen_names[device_key] = device

        filtered_devices: list[AudioDevice] = []
        for device in devices:
            if device.max_input_channels <= 0:
                continue

            device_key = self._normalized_device_key(device.name)
            if seen_names.get(device_key) == device:
                filtered_devices.append(device)

        return filtered_devices

    def list_output_devices(self) -> list[AudioDevice]:
        devices = self._filter_devices_to_preferred_hostapi(self.list_devices())
        return [device for device in devices if device.max_output_channels > 0]

    def build_audio_devices_payload(self) -> dict[str, list[str] | str]:
        input_labels = ["Predeterminado"]
        input_ids = ["default"]
        output_labels = ["Predeterminado"]
        output_ids = ["default"]
        devices = self.settings_manager.get_devices()
        saved_input_device_id = str(
            devices.get("saved_input_device_id")
            or device_id_from_index(devices.get("input_device_index"))
            or "default"
        )

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
            "saved_input_device_id": saved_input_device_id,
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
            time.sleep(DEFAULT_PLAYBACK_TAIL_SETTLE_SECONDS)
        except Exception as exc:
            LOGGER.exception("Audio playback failed")
            raise AudioManagerError(f"Audio playback failed: {exc}") from exc
        finally:
            self._set_output_level(0.0)

    def play_ui_beep(self, volume: float) -> bool:
        if sd is None or np is None:
            LOGGER.debug("UI beep unavailable: sounddevice or numpy missing")
            return False

        clean_volume = max(0.0, min(1.0, float(volume)))
        if clean_volume <= 0.0:
            return False

        threading.Thread(
            target=self._play_ui_beep_worker,
            args=(clean_volume,),
            name="UIBeep",
            daemon=True,
        ).start()
        return True

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

        stream = None
        last_error: Exception | None = None
        for candidate_sample_rate in self._input_sample_rate_candidates(
            input_device_index,
            sample_rate,
        ):
            try:
                stream = sd.InputStream(
                    device=input_device_index,
                    channels=1,
                    samplerate=candidate_sample_rate,
                    dtype="float32",
                    blocksize=512,
                    callback=self._handle_input_level_block,
                )
                stream.start()
                with self._level_lock:
                    self._level_stream_sample_rate = candidate_sample_rate
                if candidate_sample_rate != sample_rate:
                    LOGGER.info(
                        "Input level monitor fallback sample_rate=%s for device=%s",
                        candidate_sample_rate,
                        input_device_index,
                    )
                break
            except Exception as exc:
                last_error = exc
                stream = None
                LOGGER.debug(
                    "Could not start input level monitor with sample_rate=%s: %s",
                    candidate_sample_rate,
                    exc,
                )

        if stream is None:
            LOGGER.warning("Could not start input level monitor: %s", last_error)
            self._set_input_level(0.0)
            return False

        with self._level_lock:
            self._level_stream = stream
            self._level_stream_active = True
        LOGGER.info(
            "Input level monitor started for device=%s sample_rate=%s",
            input_device_index,
            self._level_stream_sample_rate,
        )
        return True

    def stop_input_level_monitor(self) -> None:
        with self._level_lock:
            stream = self._level_stream
            self._level_stream = None
            self._level_stream_active = False
            self._input_level = 0.0
            self._level_stream_sample_rate = 0

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

    def _play_ui_beep_worker(self, volume: float) -> None:
        try:
            sample_rate = DEFAULT_UI_BEEP_SAMPLE_RATE
            duration = DEFAULT_UI_BEEP_DURATION_SECONDS
            fade = min(DEFAULT_UI_BEEP_FADE_SECONDS, duration / 2.0)
            total_samples = max(1, int(sample_rate * duration))
            fade_samples = max(1, int(sample_rate * fade))
            t = np.arange(total_samples, dtype=np.float32) / float(sample_rate)
            waveform = np.sin(2.0 * math.pi * DEFAULT_UI_BEEP_FREQUENCY_HZ * t)
            envelope = np.ones(total_samples, dtype=np.float32)
            fade_in = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
            fade_out = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
            envelope[:fade_samples] = fade_in
            envelope[-fade_samples:] = np.minimum(envelope[-fade_samples:], fade_out)
            audio = (waveform * envelope * (0.18 * volume)).astype(np.float32)
            sd.play(audio, sample_rate, blocking=False)
            sd.wait()
        except Exception:
            LOGGER.debug("Could not play UI beep", exc_info=True)

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

    def _normalized_device_key(self, name: str) -> str:
        return " ".join(str(name).strip().lower().split())

    def _hostapis_by_index(self) -> dict[int, str]:
        if sd is None or not hasattr(sd, "query_hostapis"):
            return {}
        try:
            raw_hostapis = sd.query_hostapis()
        except Exception:
            LOGGER.debug("Could not query audio host APIs", exc_info=True)
            return {}

        hostapis_by_index: dict[int, str] = {}
        for index, hostapi in enumerate(raw_hostapis):
            hostapis_by_index[index] = str(hostapi.get("name", ""))
        return hostapis_by_index

    def _input_sample_rate_candidates(
        self,
        input_device_index: int | None,
        requested_sample_rate: int,
    ) -> list[int]:
        candidates: list[int] = []

        def add_candidate(value: Any) -> None:
            try:
                rate = int(float(value))
            except (TypeError, ValueError):
                return
            if rate <= 0 or rate in candidates:
                return
            candidates.append(rate)

        add_candidate(requested_sample_rate)
        add_candidate(self._device_default_sample_rate(input_device_index))
        for rate in (48000, 44100, 32000, 24000, 22050, 16000, 11025, 8000):
            add_candidate(rate)
        return candidates

    def _device_default_sample_rate(self, input_device_index: int | None) -> int | None:
        if sd is None or input_device_index is None:
            return None

        try:
            raw_devices = sd.query_devices()
        except Exception:
            LOGGER.debug("Could not query input device sample rate", exc_info=True)
            return None

        if not isinstance(raw_devices, list):
            return None
        if input_device_index < 0 or input_device_index >= len(raw_devices):
            return None

        device = raw_devices[input_device_index]
        if not isinstance(device, dict):
            return None
        try:
            return int(float(device.get("default_samplerate", 0)))
        except (TypeError, ValueError):
            return None

    def _preferred_hostapi_names(self) -> set[str]:
        if sys.platform != "win32":
            return set()
        return {"Windows WASAPI"}

    def _filter_devices_to_preferred_hostapi(
        self,
        devices: list[AudioDevice],
    ) -> list[AudioDevice]:
        preferred_names = self._preferred_hostapi_names()
        if not preferred_names:
            return devices
        filtered = [
            device
            for device in devices
            if device.hostapi_name in preferred_names
        ]
        return filtered if filtered else devices

    def _windows_active_capture_device_names(self) -> set[str]:
        if sys.platform != "win32" or winreg is None:
            return set()

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                WINDOWS_MMDEVICES_CAPTURE_PATH,
            ) as capture_root:
                return self._read_active_capture_names_from_registry(capture_root)
        except OSError:
            LOGGER.debug("Could not read Windows capture device registry", exc_info=True)
            return set()

    def _read_active_capture_names_from_registry(self, capture_root: Any) -> set[str]:
        active_names: set[str] = set()
        subkey_count = winreg.QueryInfoKey(capture_root)[0]

        for index in range(subkey_count):
            try:
                device_key_name = winreg.EnumKey(capture_root, index)
            except OSError:
                continue

            try:
                with winreg.OpenKey(capture_root, device_key_name) as device_key:
                    device_state, _ = winreg.QueryValueEx(device_key, "DeviceState")
                    if not self._is_active_windows_device_state(int(device_state)):
                        continue

                    with winreg.OpenKey(device_key, "Properties") as properties_key:
                        friendly_name, _ = winreg.QueryValueEx(
                            properties_key,
                            WINDOWS_DEVICE_FRIENDLY_NAME_KEY,
                        )
                    clean_name = self._normalized_device_key(str(friendly_name))
                    if clean_name:
                        active_names.add(clean_name)
            except OSError:
                continue

        return active_names

    def _is_active_windows_device_state(self, device_state: int) -> bool:
        return bool(int(device_state) & WINDOWS_DEVICE_STATE_ACTIVE)

    def _matches_active_windows_capture(
        self,
        device_name: str,
        active_windows_names: set[str],
    ) -> bool:
        if not active_windows_names:
            return True

        clean_device_name = self._normalized_device_key(device_name)
        if clean_device_name in active_windows_names:
            return True

        for active_name in active_windows_names:
            if clean_device_name in active_name or active_name in clean_device_name:
                return True
        return False

    def _is_preferred_input_device(
        self,
        candidate: AudioDevice,
        current: AudioDevice,
    ) -> bool:
        candidate_score = (
            int(candidate.max_input_channels > 0),
            candidate.max_input_channels,
            candidate.default_sample_rate,
            -candidate.index,
        )
        current_score = (
            int(current.max_input_channels > 0),
            current.max_input_channels,
            current.default_sample_rate,
            -current.index,
        )
        return candidate_score > current_score
