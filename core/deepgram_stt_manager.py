from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    load_dotenv = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    sd = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    websockets = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)

DEEPGRAM_LISTEN_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_DEEPGRAM_MODEL = "nova-3"
DEFAULT_DEEPGRAM_LANGUAGE = "es"
DEFAULT_DEEPGRAM_SAMPLE_RATE = 16000
DEFAULT_DEEPGRAM_CHANNELS = 1
DEFAULT_DEEPGRAM_ENDPOINTING_MS = 300
DEFAULT_DEEPGRAM_UTTERANCE_END_MS = 1000
DEFAULT_DEEPGRAM_AUDIO_BLOCK_MS = 50
DEFAULT_DEEPGRAM_QUEUE_MAX_CHUNKS = 120

TranscriptCallback = Callable[[str], None]


class DeepgramSTTManagerError(RuntimeError):
    """Raised for configuration errors before starting Deepgram STT."""


@dataclass(frozen=True)
class DeepgramSTTConfig:
    enabled: bool
    api_key: str
    language: str = DEFAULT_DEEPGRAM_LANGUAGE
    model: str = DEFAULT_DEEPGRAM_MODEL
    sample_rate: int = DEFAULT_DEEPGRAM_SAMPLE_RATE
    channels: int = DEFAULT_DEEPGRAM_CHANNELS
    interim_results: bool = True
    endpointing: bool | int = True
    utterance_end_ms: int = DEFAULT_DEEPGRAM_UTTERANCE_END_MS
    vad_events: bool = True
    smart_format: bool = True
    punctuate: bool = True
    audio_block_ms: int = DEFAULT_DEEPGRAM_AUDIO_BLOCK_MS
    queue_max_chunks: int = DEFAULT_DEEPGRAM_QUEUE_MAX_CHUNKS
    input_volume: float = 1.0


class DeepgramSTTManager:
    """Streams microphone PCM16 to Deepgram and emits transcript callbacks."""

    def __init__(
        self,
        root: Path,
        *,
        on_partial: TranscriptCallback | None = None,
        on_final: TranscriptCallback | None = None,
    ) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._env_loaded = False
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._audio_queue: queue.Queue[bytes | None] | None = None
        self._partial_callback = on_partial
        self._final_callback = on_final
        self._last_partial = ""
        self._input_level = 0.0

    def set_transcript_callbacks(
        self,
        *,
        on_partial: TranscriptCallback | None = None,
        on_final: TranscriptCallback | None = None,
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
            LOGGER.info("Deepgram STT disabled")
            return False

        if not config.api_key:
            LOGGER.error(
                "Deepgram error: DEEPGRAM_API_KEY is missing in .env or settings."
            )
            return False
        if sd is None:
            LOGGER.error(
                "Deepgram error: sounddevice is not installed. "
                "Run pip install -r requirements.txt."
            )
            return False
        if websockets is None:
            LOGGER.error(
                "Deepgram error: websockets is not installed. "
                "Run pip install -r requirements.txt."
            )
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True

            stop_event = threading.Event()
            audio_queue: queue.Queue[bytes | None] = queue.Queue(
                maxsize=config.queue_max_chunks
            )
            thread = threading.Thread(
                target=self._run_session_thread,
                args=(config, input_device_index, stop_event, audio_queue),
                name="DeepgramSTT",
                daemon=True,
            )
            self._stop_event = stop_event
            self._audio_queue = audio_queue
            self._thread = thread
            self._last_partial = ""

        LOGGER.info(
            "Deepgram STT enabled: model=%s language=%s sample_rate=%s "
            "interim_results=%s endpointing=%s",
            config.model,
            config.language,
            config.sample_rate,
            _bool_log(config.interim_results),
            _endpointing_query_value(config.endpointing),
        )
        thread.start()
        return True

    def stop_listening(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            audio_queue = self._audio_queue
            thread = self._thread

        if stop_event is None:
            return

        stop_event.set()
        if audio_queue is not None:
            self._put_stop_marker(audio_queue)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
                self._stop_event = None
                self._audio_queue = None
                self._last_partial = ""
                self._input_level = 0.0

    def shutdown(self) -> None:
        self.stop_listening()

    def is_listening(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def get_input_level(self) -> float:
        with self._lock:
            return self._input_level

    def on_partial_transcript(self, text: str) -> None:
        clean_text = text.strip()
        if not clean_text:
            return

        with self._lock:
            if clean_text == self._last_partial:
                return
            self._last_partial = clean_text
            callback = self._partial_callback

        LOGGER.info("DEEPGRAM PARTIAL: %s", clean_text)
        if callback is not None:
            try:
                callback(clean_text)
            except Exception:
                LOGGER.exception("Deepgram partial transcript callback failed")

    def on_final_transcript(self, text: str) -> None:
        clean_text = text.strip()
        if not clean_text:
            return

        with self._lock:
            self._last_partial = ""
            callback = self._final_callback

        LOGGER.info("DEEPGRAM FINAL: %s", clean_text)
        if callback is not None:
            try:
                callback(clean_text)
            except Exception:
                LOGGER.exception("Deepgram final transcript callback failed")

    def _run_session_thread(
        self,
        config: DeepgramSTTConfig,
        input_device_index: int | None,
        stop_event: threading.Event,
        audio_queue: queue.Queue[bytes | None],
    ) -> None:
        try:
            asyncio.run(
                self._run_streaming_session(
                    config,
                    input_device_index,
                    stop_event,
                    audio_queue,
                )
            )
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._stop_event = None
                    self._audio_queue = None
                    self._last_partial = ""

    async def _run_streaming_session(
        self,
        config: DeepgramSTTConfig,
        input_device_index: int | None,
        stop_event: threading.Event,
        audio_queue: queue.Queue[bytes | None],
    ) -> None:
        url = self._build_listen_url(config)
        headers = {"Authorization": f"Token {config.api_key}"}
        connect_kwargs = _websocket_header_kwargs(headers)
        stream: Any | None = None
        tasks: set[asyncio.Task[Any]] = set()

        try:
            async with websockets.connect(url, **connect_kwargs) as websocket:
                LOGGER.info("Deepgram connection opened")
                stream = self._start_microphone_stream(
                    config,
                    input_device_index,
                    stop_event,
                    audio_queue,
                )
                tasks = {
                    asyncio.create_task(
                        self._send_audio(websocket, audio_queue, stop_event)
                    ),
                    asyncio.create_task(
                        self._receive_transcripts(websocket, stop_event)
                    ),
                    asyncio.create_task(asyncio.to_thread(stop_event.wait)),
                }
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    exception = task.exception()
                    if exception is not None and not stop_event.is_set():
                        raise exception

                stop_event.set()
                self._put_stop_marker(audio_queue)
                await self._close_deepgram_stream(websocket)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:
            LOGGER.exception("Deepgram error: %s", exc)
        finally:
            stop_event.set()
            self._put_stop_marker(audio_queue)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    LOGGER.warning(
                        "Deepgram error: could not close microphone stream: %s",
                        exc,
                    )
            LOGGER.info("Deepgram connection closed")

    async def _send_audio(
        self,
        websocket: Any,
        audio_queue: queue.Queue[bytes | None],
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.is_set():
            chunk = await asyncio.to_thread(audio_queue.get)
            if chunk is None:
                break
            if chunk:
                await websocket.send(chunk)

    async def _receive_transcripts(
        self,
        websocket: Any,
        stop_event: threading.Event,
    ) -> None:
        async for message in websocket:
            if stop_event.is_set():
                return
            self._handle_deepgram_message(message)

    async def _close_deepgram_stream(self, websocket: Any) -> None:
        try:
            await websocket.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            LOGGER.debug("Deepgram CloseStream was not sent", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            LOGGER.debug("Deepgram websocket was already closed", exc_info=True)

    def _start_microphone_stream(
        self,
        config: DeepgramSTTConfig,
        input_device_index: int | None,
        stop_event: threading.Event,
        audio_queue: queue.Queue[bytes | None],
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
                LOGGER.debug("Deepgram microphone status: %s", status)
            if stop_event.is_set():
                return

            try:
                self._set_input_level_from_block(indata, config.input_volume)
                audio_queue.put_nowait(
                    self._pcm16_bytes_with_volume(indata, config.input_volume)
                )
            except queue.Full:
                LOGGER.debug("Deepgram audio queue full; dropping microphone chunk")
            except Exception:
                LOGGER.debug("Deepgram microphone chunk could not be queued", exc_info=True)

        stream = sd.InputStream(
            device=input_device_index,
            channels=config.channels,
            samplerate=config.sample_rate,
            dtype="int16",
            blocksize=blocksize,
            callback=callback,
        )
        stream.start()
        LOGGER.info(
            "Deepgram microphone stream started: device=%s sample_rate=%s "
            "channels=%s block_ms=%s",
            input_device_index,
            config.sample_rate,
            config.channels,
            config.audio_block_ms,
        )
        return stream

    def _handle_deepgram_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                return

        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            LOGGER.debug("Deepgram non-JSON message ignored: %s", message)
            return

        if payload.get("type") == "UtteranceEnd":
            LOGGER.debug("Deepgram utterance end: %s", payload)
            return
        if payload.get("type") not in {None, "Results"}:
            LOGGER.debug("Deepgram event: %s", payload.get("type"))
            return

        transcript = _extract_transcript(payload)
        if not transcript:
            return

        if bool(payload.get("is_final", False)):
            self.on_final_transcript(transcript)
        else:
            self.on_partial_transcript(transcript)

    def _config_from_settings(self, settings: dict[str, Any]) -> DeepgramSTTConfig:
        self._load_env_once()
        deepgram_settings = settings.get("deepgram", {})
        if not isinstance(deepgram_settings, dict):
            deepgram_settings = {}

        enabled = _bool_setting(
            deepgram_settings.get("enabled", settings.get("deepgram_enabled", False))
        )
        api_key = str(
            deepgram_settings.get(
                "api_key",
                settings.get("deepgram_api_key", ""),
            )
            or os.getenv("DEEPGRAM_API_KEY", "")
        ).strip()

        return DeepgramSTTConfig(
            enabled=enabled,
            api_key=api_key,
            language=str(
                deepgram_settings.get(
                    "language",
                    settings.get("deepgram_language", DEFAULT_DEEPGRAM_LANGUAGE),
                )
                or DEFAULT_DEEPGRAM_LANGUAGE
            ).strip()
            or DEFAULT_DEEPGRAM_LANGUAGE,
            model=str(
                deepgram_settings.get(
                    "model",
                    settings.get("deepgram_model", DEFAULT_DEEPGRAM_MODEL),
                )
                or DEFAULT_DEEPGRAM_MODEL
            ).strip()
            or DEFAULT_DEEPGRAM_MODEL,
            sample_rate=_bounded_int(
                deepgram_settings.get(
                    "sample_rate",
                    settings.get("deepgram_sample_rate", DEFAULT_DEEPGRAM_SAMPLE_RATE),
                ),
                default=DEFAULT_DEEPGRAM_SAMPLE_RATE,
                minimum=8000,
                maximum=48000,
            ),
            channels=1,
            interim_results=_bool_setting(
                deepgram_settings.get(
                    "interim_results",
                    settings.get("deepgram_interim_results", True),
                )
            ),
            endpointing=_clean_endpointing(
                deepgram_settings.get(
                    "endpointing",
                    settings.get("deepgram_endpointing", True),
                )
            ),
            utterance_end_ms=_bounded_int(
                deepgram_settings.get("utterance_end_ms", 1000),
                default=DEFAULT_DEEPGRAM_UTTERANCE_END_MS,
                minimum=1000,
                maximum=5000,
            ),
            vad_events=_bool_setting(deepgram_settings.get("vad_events", True)),
            smart_format=_bool_setting(deepgram_settings.get("smart_format", True)),
            punctuate=_bool_setting(deepgram_settings.get("punctuate", True)),
            audio_block_ms=_bounded_int(
                deepgram_settings.get("audio_block_ms", DEFAULT_DEEPGRAM_AUDIO_BLOCK_MS),
                default=DEFAULT_DEEPGRAM_AUDIO_BLOCK_MS,
                minimum=10,
                maximum=250,
            ),
            input_volume=_bounded_float(
                settings.get(
                    "input_volume",
                    settings.get("audio", {}).get("input_volume", 1.0)
                    if isinstance(settings.get("audio", {}), dict)
                    else 1.0,
                ),
                default=1.0,
                minimum=0.0,
                maximum=1.0,
            ),
        )

    def _build_listen_url(self, config: DeepgramSTTConfig) -> str:
        params: dict[str, str | int] = {
            "model": config.model,
            "language": config.language,
            "encoding": "linear16",
            "sample_rate": config.sample_rate,
            "channels": config.channels,
            "interim_results": _bool_query_value(config.interim_results),
            "smart_format": _bool_query_value(config.smart_format),
            "punctuate": _bool_query_value(config.punctuate),
        }
        endpointing = _endpointing_query_value(config.endpointing)
        params["endpointing"] = endpointing
        if config.interim_results:
            params["utterance_end_ms"] = config.utterance_end_ms
            params["vad_events"] = _bool_query_value(config.vad_events)
        return f"{DEEPGRAM_LISTEN_URL}?{urlencode(params)}"

    def _load_env_once(self) -> None:
        if self._env_loaded:
            return
        if load_dotenv is not None:
            load_dotenv(self.root / ".env")
        self._env_loaded = True

    def _put_stop_marker(self, audio_queue: queue.Queue[bytes | None]) -> None:
        try:
            audio_queue.put_nowait(None)
        except queue.Full:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                audio_queue.put_nowait(None)
            except queue.Full:
                pass

    def _set_input_level_from_block(self, indata: Any, input_volume: float) -> None:
        try:
            normalized = indata.astype("float32") / 32768.0
            rms = float((normalized**2).mean() ** 0.5)
        except Exception:
            rms = 0.0

        level = max(0.0, min(1.0, rms * 8.0 * input_volume))
        with self._lock:
            if level < self._input_level:
                level = max(level, self._input_level * 0.82)
            self._input_level = level

    def _pcm16_bytes_with_volume(self, indata: Any, input_volume: float) -> bytes:
        if input_volume >= 0.999:
            return indata.tobytes()
        try:
            scaled = (
                (indata.astype("float32") * input_volume)
                .clip(-32768, 32767)
                .astype("int16")
            )
            return scaled.tobytes()
        except Exception:
            LOGGER.debug("Could not apply input_volume to Deepgram audio", exc_info=True)
            return indata.tobytes()


def _extract_transcript(payload: dict[str, Any]) -> str:
    channel = payload.get("channel", {})
    if not isinstance(channel, dict):
        return ""
    alternatives = channel.get("alternatives", [])
    if not isinstance(alternatives, list) or not alternatives:
        return ""
    first = alternatives[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("transcript", "")).strip()


def _websocket_header_kwargs(headers: dict[str, str]) -> dict[str, Any]:
    signature = inspect.signature(websockets.connect)
    if "additional_headers" in signature.parameters:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


def _bool_setting(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clean_endpointing(value: Any) -> bool | int:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"false", "off", "0"}:
        return False
    if isinstance(value, str) and value.strip().lower() in {"true", "on", "1"}:
        return True
    return _bounded_int(
        value,
        default=DEFAULT_DEEPGRAM_ENDPOINTING_MS,
        minimum=10,
        maximum=5000,
    )


def _endpointing_query_value(value: bool | int) -> str | int:
    if isinstance(value, bool):
        return DEFAULT_DEEPGRAM_ENDPOINTING_MS if value else "false"
    return value


def _bool_query_value(value: bool) -> str:
    return "true" if value else "false"


def _bool_log(value: bool) -> str:
    return "true" if value else "false"


def _bounded_int(
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
