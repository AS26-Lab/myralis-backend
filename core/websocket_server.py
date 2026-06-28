from __future__ import annotations

import asyncio
import json
import logging
import threading
import warnings
import wave
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Coroutine

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    websockets = None  # type: ignore[assignment]

    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        pass

LOGGER = logging.getLogger(__name__)

NO_CLIENT_MESSAGE = "No hay cliente WebSocket conectado"
START_MESSAGE = "START"
END_MESSAGE = "END"
DEFAULT_AUDIO_SAMPLE_RATE = 24000
DEFAULT_AUDIO_CHANNELS = 1
DEFAULT_AUDIO_CHUNK_MS = 20
DEFAULT_AUDIO_REALTIME_PACING = True
START_TO_FIRST_CHUNK_DELAY_SECONDS = 0.02
IncomingJsonHandler = Callable[[dict[str, Any]], None]
ConnectionStatusHandler = Callable[[bool], None]


class UnrealWebSocketServer:
    """Runs a local WebSocket server for Unreal in a dedicated asyncio thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any | None = None
        self._client: Any | None = None
        self._clients: set[Any] = set()
        self._audio_chunk_ms = DEFAULT_AUDIO_CHUNK_MS
        self._audio_realtime_pacing = DEFAULT_AUDIO_REALTIME_PACING
        self._incoming_json_handler: IncomingJsonHandler | None = None
        self._connection_status_handlers: list[ConnectionStatusHandler] = []

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                LOGGER.info("WebSocket ya est\u00e1 activo")
                return False

            if websockets is None:
                LOGGER.error(
                    "No se pudo iniciar WebSocket: falta la dependencia websockets. "
                    "Instala dependencias con pip install -r requirements.txt."
                )
                return False

            self._thread = threading.Thread(
                target=self._run_loop,
                name="UnrealWebSocketServer",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread

        if loop is None or thread is None or not thread.is_alive():
            return

        if loop.is_closed():
            return

        future = asyncio.run_coroutine_threadsafe(self._stop_async(), loop)
        try:
            future.result(timeout=3.0)
        except Exception:
            LOGGER.exception("No se pudo detener el WebSocket limpiamente")

        if thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def is_active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def send_text(self, message: str) -> Future[bool] | None:
        return self._submit(lambda: self._send_text_async(message))

    def send_binary(self, data: bytes) -> Future[bool] | None:
        return self._submit(lambda: self._send_binary_async(data))

    def send_json(self, payload: dict[str, Any]) -> Future[bool] | None:
        return self._submit_json(payload)

    def send_json_blocking(
        self,
        payload: dict[str, Any],
        timeout: float = 1.0,
    ) -> bool:
        future = self.send_json(payload)
        if future is None:
            return False

        try:
            return bool(future.result(timeout=timeout))
        except Exception:
            LOGGER.exception("No se pudo enviar JSON WebSocket")
            return False

    def send_text_blocking(self, message: str, timeout: float = 5.0) -> bool:
        return self._send_blocking(lambda: self._send_text_async(message), timeout)

    def send_binary_blocking(self, data: bytes, timeout: float = 5.0) -> bool:
        return self._send_blocking(lambda: self._send_binary_async(data), timeout)

    def send_audio_start_blocking(
        self,
        *,
        realtime_pacing: bool | None = None,
        timeout: float = 5.0,
    ) -> bool:
        return self._send_blocking(
            lambda: self._send_audio_start_async(realtime_pacing=realtime_pacing),
            timeout,
        )

    def send_audio_binary_blocking(
        self,
        data: bytes,
        *,
        chunk_ms: int | None = None,
        realtime_pacing: bool | None = None,
        timeout: float = 5.0,
    ) -> bool:
        return self._send_blocking(
            lambda: self._send_audio_binary_async(
                data,
                chunk_ms=chunk_ms,
                realtime_pacing=realtime_pacing,
            ),
            timeout,
        )

    def send_audio_end_blocking(self, timeout: float = 5.0) -> bool:
        return self._send_blocking(self._send_audio_end_async, timeout)

    def configure_audio_streaming(
        self,
        *,
        chunk_ms: int | None = None,
        realtime_pacing: bool | None = None,
    ) -> None:
        with self._lock:
            if chunk_ms is not None:
                self._audio_chunk_ms = _clean_audio_chunk_ms(chunk_ms)
            if realtime_pacing is not None:
                self._audio_realtime_pacing = bool(realtime_pacing)

    def has_client(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def set_incoming_json_handler(
        self,
        handler: IncomingJsonHandler | None,
    ) -> None:
        with self._lock:
            self._incoming_json_handler = handler

    def add_connection_status_handler(
        self,
        handler: ConnectionStatusHandler,
    ) -> None:
        with self._lock:
            if handler not in self._connection_status_handlers:
                self._connection_status_handlers.append(handler)

    def remove_connection_status_handler(
        self,
        handler: ConnectionStatusHandler,
    ) -> None:
        with self._lock:
            if handler in self._connection_status_handlers:
                self._connection_status_handlers.remove(handler)

    def stream_wav(
        self,
        wav_path: str,
        *,
        sample_rate: int = 24000,
        channels: int = 1,
        chunk_ms: int | None = None,
        realtime_pacing: bool | None = None,
    ) -> Future[bool] | None:
        return self._submit(
            lambda: self._stream_wav_async(
                wav_path,
                sample_rate=sample_rate,
                channels=channels,
                chunk_ms=chunk_ms,
                realtime_pacing=realtime_pacing,
            )
        )

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        with self._lock:
            self._loop = loop

        try:
            loop.run_until_complete(self._start_async())
            LOGGER.info("WebSocket server activo en ws://%s:%s", self.host, self.port)
            loop.run_forever()
        except Exception:
            LOGGER.exception("No se pudo iniciar WebSocket server")
        finally:
            try:
                loop.run_until_complete(self._cleanup_async())
            except Exception:
                LOGGER.exception("Error limpiando WebSocket server")
            finally:
                loop.close()
                with self._lock:
                    self._loop = None
                    self._server = None
                    self._client = None
                    self._clients.clear()
                    self._thread = None

    async def _start_async(self) -> None:
        if websockets is None:
            raise RuntimeError("websockets is not installed")
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
        )

    async def _stop_async(self) -> None:
        await self._cleanup_async()
        asyncio.get_running_loop().call_soon(asyncio.get_running_loop().stop)

    async def _cleanup_async(self) -> None:
        with self._lock:
            server = self._server
            clients = list(self._clients)
            self._server = None
            self._client = None
            self._clients.clear()

        for client in clients:
            try:
                await client.close()
            except Exception:
                LOGGER.debug("El cliente WebSocket ya estaba cerrado", exc_info=True)

        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle_client(self, websocket: Any, path: str | None = None) -> None:
        _ = path
        with self._lock:
            self._client = websocket
            self._clients.add(websocket)

        LOGGER.info("Unreal conectado al WebSocket")
        self._notify_connection_status(True)
        try:
            async for message in websocket:
                self._handle_incoming_message(message)
        except ConnectionClosed:
            pass
        except Exception:
            LOGGER.exception("Error en conexion WebSocket con Unreal")
        finally:
            with self._lock:
                self._clients.discard(websocket)
                if self._client is websocket:
                    self._client = next(iter(self._clients), None)
            LOGGER.info("Unreal desconectado del WebSocket")
            self._notify_connection_status(False)

    def _notify_connection_status(self, connected: bool) -> None:
        with self._lock:
            handlers = list(self._connection_status_handlers)
        for handler in handlers:
            try:
                handler(connected)
            except Exception:
                LOGGER.exception("WebSocket connection status handler failed")

    def _handle_incoming_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                LOGGER.warning("Ignoring non-UTF8 WebSocket message from Unreal")
                return

        if not isinstance(message, str):
            LOGGER.warning("Ignoring unsupported WebSocket message from Unreal")
            return

        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring non-JSON WebSocket message from Unreal: %s", message)
            return

        if not isinstance(payload, dict):
            LOGGER.warning("Ignoring non-object WebSocket JSON from Unreal")
            return

        with self._lock:
            handler = self._incoming_json_handler

        if handler is None:
            LOGGER.info("WS JSON received from Unreal without handler: type=%s", payload.get("type", ""))
            return

        try:
            handler(payload)
        except Exception:
            LOGGER.exception("Error handling WebSocket JSON from Unreal")

    def _submit(
        self,
        coroutine_factory: Callable[[], Coroutine[Any, Any, bool]],
    ) -> Future[bool] | None:
        with self._lock:
            loop = self._loop

        if loop is None or loop.is_closed() or not loop.is_running():
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return None

        return asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)

    def _submit_json(self, payload: dict[str, Any]) -> Future[bool] | None:
        with self._lock:
            loop = self._loop

        if loop is None or loop.is_closed() or not loop.is_running():
            _log_json_no_client(payload)
            return None

        return asyncio.run_coroutine_threadsafe(
            self._send_json_to_unreal_async(payload),
            loop,
        )

    def _send_blocking(
        self,
        coroutine_factory: Callable[[], Coroutine[Any, Any, bool]],
        timeout: float,
    ) -> bool:
        future = self._submit(coroutine_factory)
        if future is None:
            return False

        try:
            return bool(future.result(timeout=timeout))
        except Exception:
            LOGGER.exception("No se pudo enviar mensaje WebSocket")
            return False

    async def _send_text_async(self, message: str) -> bool:
        client = self._current_client()
        if client is None:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return False
        await self._send_to_client(client, message)
        return True

    async def _send_binary_async(self, data: bytes) -> bool:
        client = self._current_client()
        if client is None:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return False
        await self._send_to_client(client, data)
        return True

    async def _send_json_to_unreal_async(self, payload: dict[str, Any]) -> bool:
        clients = self._current_clients()
        if not clients:
            _log_json_no_client(payload)
            return False

        message = json.dumps(payload, ensure_ascii=False)
        sent_any = False
        for client in clients:
            try:
                await self._send_to_client(client, message)
                sent_any = True
            except ConnectionClosed:
                with self._lock:
                    self._clients.discard(client)
                    if self._client is client:
                        self._client = next(iter(self._clients), None)

        if sent_any:
            _log_json_sent(payload)
        else:
            _log_json_no_client(payload)
        return sent_any

    async def _send_audio_start_async(
        self,
        *,
        realtime_pacing: bool | None = None,
    ) -> bool:
        client = self._current_client()
        if client is None:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return False

        pacing = self._resolve_audio_realtime_pacing(realtime_pacing)
        await self._send_to_client(client, START_MESSAGE)
        LOGGER.info("WS AUDIO START sent")
        if pacing:
            await asyncio.sleep(START_TO_FIRST_CHUNK_DELAY_SECONDS)
        return True

    async def _send_audio_binary_async(
        self,
        data: bytes,
        *,
        chunk_ms: int | None = None,
        realtime_pacing: bool | None = None,
    ) -> bool:
        client = self._current_client()
        if client is None:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return False

        clean_chunk_ms = self._resolve_audio_chunk_ms(chunk_ms)
        pacing = self._resolve_audio_realtime_pacing(realtime_pacing)
        await self._send_audio_chunk_to_client(
            client,
            data,
            chunk_ms=clean_chunk_ms,
            realtime_pacing=pacing,
        )
        return True

    async def _send_audio_end_async(self) -> bool:
        client = self._current_client()
        if client is None:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return False

        await self._send_to_client(client, END_MESSAGE)
        LOGGER.info("WS AUDIO END sent")
        return True

    async def _stream_wav_async(
        self,
        wav_path: str,
        *,
        sample_rate: int,
        channels: int,
        chunk_ms: int | None,
        realtime_pacing: bool | None,
    ) -> bool:
        client = self._current_client()
        if client is None:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            return False

        try:
            pcm_data = _read_or_convert_wav_to_pcm16(
                Path(wav_path),
                sample_rate=sample_rate,
                channels=channels,
            )
        except Exception:
            LOGGER.exception("No se pudo preparar WAV para Unreal: %s", wav_path)
            return False

        clean_chunk_ms = self._resolve_audio_chunk_ms(chunk_ms)
        pacing = self._resolve_audio_realtime_pacing(realtime_pacing)
        bytes_per_chunk = calculate_pcm_bytes_per_chunk(
            sample_rate=sample_rate,
            channels=channels,
            chunk_ms=clean_chunk_ms,
        )

        try:
            await self._send_to_client(client, START_MESSAGE)
            LOGGER.info("WS AUDIO START sent")
            if pacing:
                await asyncio.sleep(START_TO_FIRST_CHUNK_DELAY_SECONDS)
            for index in range(0, len(pcm_data), bytes_per_chunk):
                chunk = pcm_data[index : index + bytes_per_chunk]
                if not chunk:
                    continue
                await self._send_audio_chunk_to_client(
                    client,
                    chunk,
                    chunk_ms=clean_chunk_ms,
                    realtime_pacing=pacing,
                )
            await self._send_to_client(client, END_MESSAGE)
            LOGGER.info("WS AUDIO END sent")
            return True
        except ConnectionClosed:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            with self._lock:
                if self._client is client:
                    self._client = None
            return False
        except Exception:
            LOGGER.exception("Error enviando audio WAV por WebSocket")
            return False

    async def _send_audio_chunk_to_client(
        self,
        client: Any,
        chunk: bytes,
        *,
        chunk_ms: int,
        realtime_pacing: bool,
    ) -> None:
        await self._send_to_client(client, chunk)
        LOGGER.info(
            "WS AUDIO CHUNK sent: %s bytes, chunk_ms=%s, pacing=%s",
            len(chunk),
            chunk_ms,
            _bool_log(realtime_pacing),
        )
        if realtime_pacing:
            await asyncio.sleep(chunk_ms / 1000.0)

    async def _send_to_client(self, client: Any, payload: str | bytes) -> None:
        try:
            await client.send(payload)
        except ConnectionClosed:
            LOGGER.warning(NO_CLIENT_MESSAGE)
            with self._lock:
                if self._client is client:
                    self._client = None
            raise

    def _current_client(self) -> Any | None:
        with self._lock:
            return self._client

    def _current_clients(self) -> list[Any]:
        with self._lock:
            return list(self._clients)

    def _resolve_audio_chunk_ms(self, chunk_ms: int | None) -> int:
        if chunk_ms is not None:
            return _clean_audio_chunk_ms(chunk_ms)
        with self._lock:
            return self._audio_chunk_ms

    def _resolve_audio_realtime_pacing(self, realtime_pacing: bool | None) -> bool:
        if realtime_pacing is not None:
            return bool(realtime_pacing)
        with self._lock:
            return self._audio_realtime_pacing


def _read_or_convert_wav_to_pcm16(
    wav_path: Path,
    *,
    sample_rate: int,
    channels: int,
) -> bytes:
    if not wav_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {wav_path}")

    input_logged = False
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            input_sample_rate = wav_file.getframerate()
            input_channels = wav_file.getnchannels()
            input_sample_width = wav_file.getsampwidth()
            LOGGER.info(
                "WAV INPUT sample_rate=%s channels=%s sample_width=%s",
                input_sample_rate,
                input_channels,
                input_sample_width,
            )
            input_logged = True
            is_pcm16 = (
                wav_file.getcomptype() == "NONE"
                and input_sample_width == 2
                and input_channels == channels
                and input_sample_rate == sample_rate
            )
            if is_pcm16:
                LOGGER.info("SENDING RAW PCM BYTES, no WAV header")
                return wav_file.readframes(wav_file.getnframes())
    except wave.Error:
        LOGGER.info("WAV no PCM detectado; se intentara convertir con pydub")

    AudioSegment = _load_audio_segment_class()
    audio = AudioSegment.from_wav(str(wav_path))
    if not input_logged:
        LOGGER.info(
            "WAV INPUT sample_rate=%s channels=%s sample_width=%s",
            audio.frame_rate,
            audio.channels,
            audio.sample_width,
        )
    audio = audio.set_channels(channels)
    audio = audio.set_frame_rate(sample_rate)
    audio = audio.set_sample_width(2)
    LOGGER.info(
        "WAV convertido para Unreal: sample_rate=%s channels=%s sample_width=16bit",
        sample_rate,
        channels,
    )
    LOGGER.info("SENDING RAW PCM BYTES, no WAV header")
    return bytes(audio.raw_data)


def calculate_pcm_bytes_per_chunk(
    *,
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    channels: int = DEFAULT_AUDIO_CHANNELS,
    chunk_ms: int = DEFAULT_AUDIO_CHUNK_MS,
) -> int:
    bytes_per_sample = 2
    bytes_per_chunk = int(sample_rate * chunk_ms / 1000 * channels * bytes_per_sample)
    if bytes_per_chunk % bytes_per_sample:
        bytes_per_chunk += bytes_per_sample - (bytes_per_chunk % bytes_per_sample)
    return max(bytes_per_sample * channels, bytes_per_chunk)


def _clean_audio_chunk_ms(chunk_ms: int) -> int:
    try:
        parsed = int(chunk_ms)
    except (TypeError, ValueError):
        parsed = DEFAULT_AUDIO_CHUNK_MS
    return max(1, min(1000, parsed))


def _bool_log(value: bool) -> str:
    return "true" if value else "false"


def _log_json_sent(payload: dict[str, Any]) -> None:
    if payload.get("type") == "runtime_state":
        emotion_strength = payload.get("emotion_strength", 0.0)
        if isinstance(emotion_strength, (int, float)):
            emotion_strength_log = f"{float(emotion_strength):.2f}"
        else:
            emotion_strength_log = str(emotion_strength)
        LOGGER.info(
            "WS RUNTIME_STATE sent: state=%s mood=%s emotion_strength=%s "
            "response_id=%s audio_mode=%s",
            payload.get("state", ""),
            payload.get("mood", ""),
            emotion_strength_log,
            payload.get("response_id", ""),
            payload.get("audio_mode", ""),
        )
    else:
        LOGGER.info("WS JSON sent: type=%s", payload.get("type", ""))


def _log_json_no_client(payload: dict[str, Any]) -> None:
    if payload.get("type") == "runtime_state":
        LOGGER.warning("No hay cliente WebSocket conectado para runtime_state")
    else:
        LOGGER.warning(NO_CLIENT_MESSAGE)


def _load_audio_segment_class() -> Any:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Couldn't find ffmpeg or avconv.*",
                category=RuntimeWarning,
            )
            from pydub import AudioSegment
    except ImportError as exc:
        raise RuntimeError(
            "pydub is required to convert WAV files. "
            "Run pip install -r requirements.txt."
        ) from exc
    return AudioSegment


_SERVER = UnrealWebSocketServer()


def start_websocket_server() -> bool:
    return _SERVER.start()


def stop_websocket_server() -> None:
    _SERVER.stop()


def is_websocket_server_active() -> bool:
    return _SERVER.is_active()


def has_websocket_client() -> bool:
    return _SERVER.has_client()


def configure_websocket_audio_streaming(
    *,
    chunk_ms: int | None = None,
    realtime_pacing: bool | None = None,
) -> None:
    _SERVER.configure_audio_streaming(
        chunk_ms=chunk_ms,
        realtime_pacing=realtime_pacing,
    )


def set_unreal_json_message_handler(
    handler: IncomingJsonHandler | None,
) -> None:
    _SERVER.set_incoming_json_handler(handler)


def add_websocket_connection_status_handler(
    handler: ConnectionStatusHandler,
) -> None:
    _SERVER.add_connection_status_handler(handler)


def remove_websocket_connection_status_handler(
    handler: ConnectionStatusHandler,
) -> None:
    _SERVER.remove_connection_status_handler(handler)


def send_ws_text(message: str) -> Future[bool] | None:
    return _SERVER.send_text(message)


def send_ws_binary(data: bytes) -> Future[bool] | None:
    return _SERVER.send_binary(data)


async def send_json_to_unreal(payload: dict[str, Any]) -> bool:
    return await _SERVER._send_json_to_unreal_async(payload)


def send_json_to_unreal_threadsafe(payload: dict[str, Any]) -> Future[bool] | None:
    return _SERVER.send_json(payload)


def send_json_to_unreal_blocking(
    payload: dict[str, Any],
    timeout: float = 1.0,
) -> bool:
    return _SERVER.send_json_blocking(payload, timeout=timeout)


def send_ws_text_blocking(message: str, timeout: float = 5.0) -> bool:
    return _SERVER.send_text_blocking(message, timeout=timeout)


def send_ws_binary_blocking(data: bytes, timeout: float = 5.0) -> bool:
    return _SERVER.send_binary_blocking(data, timeout=timeout)


def send_ws_audio_start_blocking(
    *,
    realtime_pacing: bool | None = None,
    timeout: float = 5.0,
) -> bool:
    return _SERVER.send_audio_start_blocking(
        realtime_pacing=realtime_pacing,
        timeout=timeout,
    )


def send_ws_audio_binary_blocking(
    data: bytes,
    *,
    chunk_ms: int | None = None,
    realtime_pacing: bool | None = None,
    timeout: float = 5.0,
) -> bool:
    return _SERVER.send_audio_binary_blocking(
        data,
        chunk_ms=chunk_ms,
        realtime_pacing=realtime_pacing,
        timeout=timeout,
    )


def send_ws_audio_end_blocking(timeout: float = 5.0) -> bool:
    return _SERVER.send_audio_end_blocking(timeout=timeout)


def send_audio_start(
    sample_rate: int = 24000,
    channels: int = 1,
) -> Future[bool] | None:
    _ = sample_rate, channels
    return send_ws_text(START_MESSAGE)


def send_audio_end() -> Future[bool] | None:
    return send_ws_text(END_MESSAGE)


def stream_wav_to_unreal(
    wav_path: str,
    sample_rate: int = 24000,
    channels: int = 1,
    chunk_ms: int | None = None,
    realtime_pacing: bool | None = None,
) -> Future[bool] | None:
    return _SERVER.stream_wav(
        wav_path,
        sample_rate=sample_rate,
        channels=channels,
        chunk_ms=chunk_ms,
        realtime_pacing=realtime_pacing,
    )
