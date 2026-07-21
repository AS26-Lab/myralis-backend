from __future__ import annotations

import audioop
import logging
import os
import tempfile
import threading
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    sd = None  # type: ignore[assignment]

from core.deepgram_stt_manager import DeepgramSTTManager
from core.language import current_language_to_ui_code


LOGGER = logging.getLogger(__name__)

LocalTranscriptCallback = Callable[[str], None]

DEFAULT_LOCAL_STT_MODEL = os.getenv("LOCAL_STT_MODEL", "base")
DEFAULT_LOCAL_STT_AUDIO_BLOCK_MS = 50
DEFAULT_LOCAL_STT_SAMPLE_RATE = 16000
DEFAULT_LOCAL_STT_QUEUE_MAX_SECONDS = 30
DEFAULT_LOCAL_STT_BEAM_SIZE = 1
DEFAULT_LOCAL_STT_COMPUTE_TYPE = os.getenv("LOCAL_STT_COMPUTE_TYPE", "int8")


@dataclass(frozen=True)
class LocalSTTConfig:
    enabled: bool
    language: str
    model: str
    sample_rate: int = DEFAULT_LOCAL_STT_SAMPLE_RATE
    channels: int = 1
    audio_block_ms: int = DEFAULT_LOCAL_STT_AUDIO_BLOCK_MS
    input_volume: float = 1.0
    queue_max_seconds: int = DEFAULT_LOCAL_STT_QUEUE_MAX_SECONDS
    beam_size: int = DEFAULT_LOCAL_STT_BEAM_SIZE
    compute_type: str = DEFAULT_LOCAL_STT_COMPUTE_TYPE


class LocalSTTManager:
    """Records microphone audio locally and transcribes it with Whisper."""

    def __init__(
        self,
        root: Path,
        *,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._audio_buffer = bytearray()
        self._partial_callback: LocalTranscriptCallback | None = None
        self._final_callback: LocalTranscriptCallback | None = None
        self._last_partial = ""
        self._input_level = 0.0
        self._model_factory = model_factory
        self._model_cache: dict[str, Any] = {}
        self._transcribing = False

    def set_transcript_callbacks(
        self,
        *,
        on_partial: LocalTranscriptCallback | None = None,
        on_final: LocalTranscriptCallback | None = None,
    ) -> None:
        with self._lock:
            self._partial_callback = on_partial
            self._final_callback = on_final

    def start_listening(
        self,
        *,
        settings: dict[str, Any] | None = None,
        input_device_index: int | None = None,
    ) -> bool:
        config = self._config_from_settings(settings or {})
        if not config.enabled:
            LOGGER.info("Local STT disabled")
            return False
        if sd is None:
            LOGGER.error(
                "Local STT error: sounddevice is not installed. "
                "Run pip install -r requirements.txt."
            )
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_session_thread,
                args=(config, input_device_index, stop_event),
                name="LocalSTT",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            self._audio_buffer = bytearray()
            self._last_partial = ""
            self._input_level = 0.0
            self._transcribing = False

        LOGGER.info(
            "Local STT enabled: model=%s language=%s sample_rate=%s "
            "compute_type=%s block_ms=%s",
            config.model,
            config.language,
            config.sample_rate,
            config.compute_type,
            config.audio_block_ms,
        )
        thread.start()
        return True

    def stop_listening(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread

        if stop_event is None:
            return

        stop_event.set()
        if thread is not None and thread is threading.current_thread():
            return

    def shutdown(self) -> None:
        self.stop_listening()
        thread = None
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def is_listening(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def get_input_level(self) -> float:
        with self._lock:
            return self._input_level

    def _run_session_thread(
        self,
        config: LocalSTTConfig,
        input_device_index: int | None,
        stop_event: threading.Event,
    ) -> None:
        stream: Any | None = None
        partial_thread: threading.Thread | None = None
        try:
            effective_config = self._effective_config_for_input_device(
                config,
                input_device_index,
            )
            stream = self._start_microphone_stream(
                effective_config,
                input_device_index,
                stop_event,
            )
            partial_thread = threading.Thread(
                target=self._run_partial_loop,
                args=(effective_config, stop_event),
                name="LocalSTTPartial",
                daemon=True,
            )
            partial_thread.start()
            LOGGER.info("Local STT microphone stream started")
            stop_event.wait()
        except Exception as exc:
            LOGGER.exception("Local STT session failed: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    LOGGER.warning("Local STT could not close microphone stream: %s", exc)

            if not stop_event.is_set():
                stop_event.set()

            if partial_thread is not None and partial_thread.is_alive():
                partial_thread.join(timeout=2.0)

            audio_bytes = self._drain_audio_buffer()
            transcript = ""
            if audio_bytes:
                try:
                    transcript = self._transcribe_audio_bytes(config, audio_bytes)
                except Exception:
                    LOGGER.exception("Local STT transcription failed")
                    transcript = ""
            self._publish_final_transcript(transcript)

            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._stop_event = None
                    self._audio_buffer = bytearray()
                    self._last_partial = ""
                    self._input_level = 0.0
                    self._transcribing = False

    def _run_partial_loop(
        self,
        config: LocalSTTConfig,
        stop_event: threading.Event,
    ) -> None:
        preview_interval_seconds = 1.5
        preview_window_seconds = 6.0
        preview_window_bytes = int(config.sample_rate * config.channels * 2 * preview_window_seconds)
        min_preview_bytes = int(config.sample_rate * config.channels * 2 * 1.2)

        while not stop_event.wait(preview_interval_seconds):
            audio_bytes = self._drain_audio_buffer(limit_bytes=preview_window_bytes)
            if len(audio_bytes) < min_preview_bytes:
                continue
            try:
                self._transcribe_audio_bytes(config, audio_bytes)
            except Exception:
                LOGGER.debug("Local STT partial transcription failed", exc_info=True)

    def _start_microphone_stream(
        self,
        config: LocalSTTConfig,
        input_device_index: int | None,
        stop_event: threading.Event,
    ) -> Any:
        blocksize = max(
            160,
            int(config.sample_rate * max(10, config.audio_block_ms) / 1000),
        )

        def callback(
            indata: Any,
            frames: int,
            time_info: Any,
            status: Any,
        ) -> None:
            _ = frames, time_info
            if status:
                LOGGER.debug("Local STT microphone status: %s", status)
            if stop_event.is_set():
                return
            try:
                raw = indata.tobytes()
                if config.input_volume < 0.999:
                    raw = self._apply_volume(raw, config.input_volume)
                with self._lock:
                    self._audio_buffer.extend(raw)
                    max_bytes = (
                        config.sample_rate
                        * config.channels
                        * config.queue_max_seconds
                        * 2
                    )
                    if len(self._audio_buffer) > max_bytes:
                        del self._audio_buffer[:-max_bytes]
                    self._set_input_level_from_raw(raw)
            except Exception:
                LOGGER.debug("Local STT audio chunk could not be buffered", exc_info=True)

        stream = sd.InputStream(
            device=input_device_index,
            channels=config.channels,
            samplerate=config.sample_rate,
            dtype="int16",
            blocksize=blocksize,
            callback=callback,
        )
        stream.start()
        return stream

    def _apply_volume(self, raw: bytes, input_volume: float) -> bytes:
        if input_volume >= 0.999:
            return raw
        try:
            audio = audioop.mul(raw, 2, max(0.0, min(1.0, float(input_volume))))
            return audio
        except Exception:
            LOGGER.debug("Local STT could not apply input volume", exc_info=True)
            return raw

    def _publish_final_transcript(self, transcript: str) -> None:
        clean_text = transcript.strip()
        if not clean_text:
            return

        with self._lock:
            callback = self._final_callback
        LOGGER.info("LOCAL STT FINAL: %s", clean_text)
        if callback is not None:
            try:
                callback(clean_text)
            except Exception:
                LOGGER.exception("Local STT final transcript callback failed")

    def _publish_partial_transcript(self, transcript: str) -> None:
        clean_text = transcript.strip()
        if not clean_text:
            return

        with self._lock:
            if clean_text == self._last_partial:
                return
            self._last_partial = clean_text
            callback = self._partial_callback

        LOGGER.info("LOCAL STT PARTIAL: %s", clean_text)
        if callback is not None:
            try:
                callback(clean_text)
            except Exception:
                LOGGER.exception("Local STT partial transcript callback failed")

    def _drain_audio_buffer(self, limit_bytes: int | None = None) -> bytes:
        with self._lock:
            if limit_bytes is None or limit_bytes <= 0 or len(self._audio_buffer) <= limit_bytes:
                return bytes(self._audio_buffer)
            return bytes(self._audio_buffer[-limit_bytes:])

    def _transcribe_audio_bytes(self, config: LocalSTTConfig, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""

        wav_path = self._write_temp_wav(config.sample_rate, audio_bytes)
        try:
            model = self._load_model(config)
            language_code = config.language.strip() or None
            segments, _info = model.transcribe(
                str(wav_path),
                language=language_code,
                task="transcribe",
                beam_size=max(1, config.beam_size),
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True,
            )
            texts: list[str] = []
            for segment in segments:
                text = str(getattr(segment, "text", "")).strip()
                if text:
                    texts.append(text)
                    self._publish_partial_transcript(" ".join(texts))
            return " ".join(texts).strip()
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                LOGGER.debug("Local STT temp WAV could not be removed", exc_info=True)

    def _write_temp_wav(self, sample_rate: int, audio_bytes: bytes) -> Path:
        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_file.close()
        wav_path = Path(temp_file.name)
        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)
        return wav_path

    def _load_model(self, config: LocalSTTConfig) -> Any:
        model_key = f"{config.model}|{config.compute_type}"
        with self._lock:
            cached = self._model_cache.get(model_key)
            if cached is not None:
                return cached

        model = self._create_model(config.model, config.compute_type)
        with self._lock:
            self._model_cache[model_key] = model
        return model

    def _create_model(self, model_name: str, compute_type: str) -> Any:
        if self._model_factory is not None:
            return self._model_factory(model_name)

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on runtime env.
            raise RuntimeError(
                "faster-whisper is not installed. Run pip install -r requirements.txt."
            ) from exc

        return WhisperModel(
            model_name,
            device="cpu",
            compute_type=compute_type,
        )

    def _config_from_settings(self, settings: dict[str, Any]) -> LocalSTTConfig:
        audio_settings = settings.get("audio", {})
        current_language = settings.get("current_language", "spanish")
        return LocalSTTConfig(
            enabled=str(settings.get("stt_engine", "deepgram")).strip() == "local",
            language=current_language_to_ui_code(current_language),
            model=str(os.getenv("LOCAL_STT_MODEL", DEFAULT_LOCAL_STT_MODEL)).strip()
            or DEFAULT_LOCAL_STT_MODEL,
            sample_rate=self._bounded_int(
                settings.get(
                    "audio",
                    {},
                ).get("sample_rate", DEFAULT_LOCAL_STT_SAMPLE_RATE)
                if isinstance(audio_settings, dict)
                else DEFAULT_LOCAL_STT_SAMPLE_RATE,
                default=DEFAULT_LOCAL_STT_SAMPLE_RATE,
                minimum=8000,
                maximum=48000,
            ),
            channels=1,
            audio_block_ms=self._bounded_int(
                settings.get("local_stt", {}).get(
                    "audio_block_ms",
                    DEFAULT_LOCAL_STT_AUDIO_BLOCK_MS,
                )
                if isinstance(settings.get("local_stt", {}), dict)
                else DEFAULT_LOCAL_STT_AUDIO_BLOCK_MS,
                default=DEFAULT_LOCAL_STT_AUDIO_BLOCK_MS,
                minimum=10,
                maximum=250,
            ),
            input_volume=self._bounded_float(
                settings.get(
                    "input_volume",
                    audio_settings.get("input_volume", 1.0)
                    if isinstance(audio_settings, dict)
                    else 1.0,
                ),
                default=1.0,
                minimum=0.0,
                maximum=1.0,
            ),
            queue_max_seconds=self._bounded_int(
                settings.get("local_stt", {}).get(
                    "queue_max_seconds",
                    DEFAULT_LOCAL_STT_QUEUE_MAX_SECONDS,
                )
                if isinstance(settings.get("local_stt", {}), dict)
                else DEFAULT_LOCAL_STT_QUEUE_MAX_SECONDS,
                default=DEFAULT_LOCAL_STT_QUEUE_MAX_SECONDS,
                minimum=5,
                maximum=120,
            ),
            beam_size=self._bounded_int(
                settings.get("local_stt", {}).get(
                    "beam_size",
                    DEFAULT_LOCAL_STT_BEAM_SIZE,
                )
                if isinstance(settings.get("local_stt", {}), dict)
                else DEFAULT_LOCAL_STT_BEAM_SIZE,
                default=DEFAULT_LOCAL_STT_BEAM_SIZE,
                minimum=1,
                maximum=8,
            ),
            compute_type=str(
                settings.get("local_stt", {}).get(
                    "compute_type",
                    DEFAULT_LOCAL_STT_COMPUTE_TYPE,
                )
                if isinstance(settings.get("local_stt", {}), dict)
                else DEFAULT_LOCAL_STT_COMPUTE_TYPE
            ).strip()
            or DEFAULT_LOCAL_STT_COMPUTE_TYPE,
        )

    def _effective_config_for_input_device(
        self,
        config: LocalSTTConfig,
        input_device_index: int | None,
    ) -> LocalSTTConfig:
        if sd is None:
            return config

        for candidate_sample_rate in self._input_sample_rate_candidates(
            input_device_index,
            config.sample_rate,
        ):
            if self._can_open_input_device(
                input_device_index,
                config.channels,
                candidate_sample_rate,
            ):
                if candidate_sample_rate != config.sample_rate:
                    LOGGER.info(
                        "Local STT input fallback sample_rate=%s for device=%s",
                        candidate_sample_rate,
                        input_device_index,
                    )
                return replace(config, sample_rate=candidate_sample_rate)

        return config

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
            LOGGER.debug("Could not query Local STT input device sample rate", exc_info=True)
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

    def _can_open_input_device(
        self,
        input_device_index: int | None,
        channels: int,
        sample_rate: int,
    ) -> bool:
        if sd is None:
            return False
        try:
            sd.check_input_settings(
                device=input_device_index,
                channels=channels,
                samplerate=sample_rate,
            )
            return True
        except Exception:
            LOGGER.debug(
                "Local STT input rejected sample_rate=%s device=%s",
                sample_rate,
                input_device_index,
                exc_info=True,
            )
            return False

    def _set_input_level_from_raw(self, raw: bytes) -> None:
        try:
            rms = float(audioop.rms(raw, 2)) / 32768.0
        except Exception:
            rms = 0.0

        level = max(0.0, min(1.0, rms * 8.0))
        with self._lock:
            if level < self._input_level:
                level = max(level, self._input_level * 0.82)
            self._input_level = level

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
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _bounded_float(
        self,
        value: Any,
        *,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, parsed))


class VoiceSTTManager:
    """Routes voice capture to Deepgram or local STT based on settings."""

    def __init__(
        self,
        root: Path,
        *,
        deepgram_manager: DeepgramSTTManager | None = None,
        local_manager: LocalSTTManager | None = None,
    ) -> None:
        self.deepgram_manager = deepgram_manager or DeepgramSTTManager(root)
        self.local_manager = local_manager or LocalSTTManager(root)

    def set_transcript_callbacks(
        self,
        *,
        on_partial: LocalTranscriptCallback | None = None,
        on_final: LocalTranscriptCallback | None = None,
    ) -> None:
        self.deepgram_manager.set_transcript_callbacks(
            on_partial=on_partial,
            on_final=on_final,
        )
        self.local_manager.set_transcript_callbacks(
            on_partial=on_partial,
            on_final=on_final,
        )

    def start_listening(
        self,
        *,
        settings: dict[str, Any] | None = None,
        input_device_index: int | None = None,
    ) -> bool:
        clean_settings = settings or {}
        if self.is_listening():
            return True

        backend = self._backend_for_settings(clean_settings)
        if backend == "deepgram":
            return self.deepgram_manager.start_listening(
                settings=clean_settings,
                input_device_index=input_device_index,
            )
        if backend == "local":
            return self.local_manager.start_listening(
                settings=clean_settings,
                input_device_index=input_device_index,
            )
        LOGGER.warning("Unknown STT engine: %s", clean_settings.get("stt_engine"))
        return False

    def stop_listening(self) -> None:
        self.deepgram_manager.stop_listening()
        self.local_manager.stop_listening()

    def shutdown(self) -> None:
        self.deepgram_manager.shutdown()
        self.local_manager.shutdown()

    def is_listening(self) -> bool:
        return self.deepgram_manager.is_listening() or self.local_manager.is_listening()

    def get_input_level(self) -> float:
        deepgram_level = self.deepgram_manager.get_input_level()
        local_level = self.local_manager.get_input_level()
        return max(deepgram_level, local_level)

    def _backend_for_settings(self, settings: dict[str, Any]) -> str:
        engine = str(settings.get("stt_engine", "deepgram")).strip().lower()
        if engine in {"deepgram", "local"}:
            return engine
        return "deepgram"
