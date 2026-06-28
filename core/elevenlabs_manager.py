from __future__ import annotations

import logging
import os
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    load_dotenv = None  # type: ignore[assignment]

try:
    import requests
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    requests = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)


class ElevenLabsManagerError(RuntimeError):
    """Raised when the ElevenLabs integration cannot complete a request."""


@dataclass(frozen=True)
class TTSResult:
    audio_path: Path
    request_id: str = ""
    character_count: str = ""
    from_cache: bool = False


@dataclass(frozen=True)
class StreamingTTSResult:
    audio_path: Path | None = None
    request_id: str = ""
    character_count: str = ""
    chunks_sent: int = 0
    bytes_sent: int = 0


class ElevenLabsManager:
    """Generates WAV files through the ElevenLabs text-to-speech endpoint."""

    PCM_SAMPLE_RATES = {
        "pcm_8000": 8000,
        "pcm_16000": 16000,
        "pcm_22050": 22050,
        "pcm_24000": 24000,
        "pcm_44100": 44100,
        "pcm_48000": 48000,
    }

    def __init__(self, root: Path, output_dir: Path) -> None:
        self.root = root
        self.output_dir = output_dir
        self._env_loaded = False
        self._api_key = ""
        self._base_url = ""
        self._session = requests.Session() if requests is not None else None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_wav(
        self,
        *,
        text: str,
        voice_id: str,
        model_id: str,
        output_format: str,
        voice_settings: dict[str, Any] | None = None,
        mood: str = "Neutral",
    ) -> TTSResult:
        if not text.strip():
            raise ElevenLabsManagerError("Text is required for ElevenLabs TTS.")
        if not voice_id.strip():
            raise ElevenLabsManagerError("ElevenLabs voice_id is missing in settings.")
        if output_format not in self.PCM_SAMPLE_RATES:
            raise ElevenLabsManagerError(
                "The current build writes WAV files from PCM formats. "
                f"Use one of: {', '.join(self.PCM_SAMPLE_RATES)}."
            )

        api_key = self._get_api_key()
        sample_rate = self.PCM_SAMPLE_RATES[output_format]
        endpoint = self._build_endpoint(voice_id)
        payload: dict[str, Any] = {
            "text": text,
            "model_id": model_id,
        }
        if voice_settings is not None:
            payload["voice_settings"] = voice_settings
        params = {"output_format": output_format}
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }

        LOGGER.info("Sending ElevenLabs TTS request voice_id=%s model_id=%s", voice_id, model_id)
        LOGGER.info("[ElevenLabs] mood=%s settings=%s", mood, voice_settings or {})
        if self._session is None:
            raise ElevenLabsManagerError(
                "requests is not installed. Run pip install -r requirements.txt."
            )

        try:
            response = self._session.post(
                endpoint,
                params=params,
                headers=headers,
                json=payload,
                timeout=90,
            )
        except Exception as exc:
            LOGGER.exception("ElevenLabs request failed")
            raise ElevenLabsManagerError(f"ElevenLabs request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ElevenLabsManagerError(self._format_error_response(response))

        audio_path = self._new_audio_path()
        self._write_pcm_as_wav(audio_path, response.content, sample_rate)
        LOGGER.info("[ElevenLabs] generated WAV: %s", audio_path)
        return TTSResult(
            audio_path=audio_path,
            request_id=response.headers.get("request-id", ""),
            character_count=response.headers.get("x-character-count", ""),
            from_cache=False,
        )

    def stream_elevenlabs_tts_to_unreal(
        self,
        *,
        text: str,
        voice_id: str,
        model_id: str,
        output_format: str = "pcm_24000",
        voice_settings: dict[str, Any] | None = None,
        mood: str = "Neutral",
        optimize_streaming_latency: int | None = None,
        save_response_wav: bool = True,
        response_wav_path: Path | None = None,
        on_audio_start: Callable[[], None] | None = None,
        websocket_audio_chunk_ms: int = 20,
        websocket_audio_realtime_pacing: bool = True,
    ) -> StreamingTTSResult:
        """Stream ElevenLabs PCM chunks directly to Unreal over WebSocket."""
        if not text.strip():
            raise ElevenLabsManagerError("Text is required for ElevenLabs TTS.")
        if not voice_id.strip():
            raise ElevenLabsManagerError("ElevenLabs voice_id is missing in settings.")
        if output_format != "pcm_24000":
            raise ElevenLabsManagerError(
                "Realtime Unreal streaming requires output_format='pcm_24000'."
            )

        if self._session is None:
            raise ElevenLabsManagerError(
                "requests is not installed. Run pip install -r requirements.txt."
            )

        from core.websocket_server import (
            calculate_pcm_bytes_per_chunk,
            send_ws_audio_binary_blocking,
            send_ws_audio_end_blocking,
            send_ws_audio_start_blocking,
        )

        api_key = self._get_api_key()
        endpoint = self._build_stream_endpoint(voice_id)
        payload: dict[str, Any] = {
            "text": text,
            "model_id": model_id,
        }
        if voice_settings is not None:
            payload["voice_settings"] = voice_settings

        params: dict[str, object] = {"output_format": output_format}
        if optimize_streaming_latency is not None:
            params["optimize_streaming_latency"] = optimize_streaming_latency

        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }

        LOGGER.info(
            "Starting ElevenLabs streaming TTS voice_id=%s model_id=%s output_format=%s",
            voice_id,
            model_id,
            output_format,
        )
        LOGGER.info("[ElevenLabs] mood=%s settings=%s", mood, voice_settings or {})

        wav_file: wave.Wave_write | None = None
        temp_wav_path: Path | None = None
        final_wav_path: Path | None = response_wav_path if save_response_wav else None
        ws_started = False
        ws_start_sent = False
        ws_end_sent = False
        ws_active = False
        chunks_sent = 0
        bytes_sent = 0
        stream_completed = False
        pcm_send_buffer = bytearray()
        bytes_per_chunk = calculate_pcm_bytes_per_chunk(
            sample_rate=self.PCM_SAMPLE_RATES[output_format],
            channels=1,
            chunk_ms=websocket_audio_chunk_ms,
        )

        try:
            if save_response_wav:
                if response_wav_path is None:
                    final_wav_path = self._new_audio_path()
                assert final_wav_path is not None
                final_wav_path.parent.mkdir(parents=True, exist_ok=True)
                temp_wav_path = final_wav_path.with_suffix(
                    final_wav_path.suffix + ".streaming.tmp"
                )
                wav_file = wave.open(str(temp_wav_path), "wb")
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.PCM_SAMPLE_RATES[output_format])

            with self._session.post(
                endpoint,
                params=params,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(10, 90),
            ) as response:
                if response.status_code >= 400:
                    raise ElevenLabsManagerError(self._format_error_response(response))

                for chunk in response.iter_content(chunk_size=4096):
                    if not chunk:
                        continue

                    if wav_file is not None:
                        wav_file.writeframes(chunk)

                    if not ws_started:
                        LOGGER.info("ELEVENLABS STREAM START")
                        ws_started = True
                        if on_audio_start is not None:
                            on_audio_start()
                        ws_active = send_ws_audio_start_blocking(
                            realtime_pacing=websocket_audio_realtime_pacing
                        )
                        if ws_active:
                            ws_start_sent = True

                    if ws_active:
                        pcm_send_buffer.extend(chunk)
                        while len(pcm_send_buffer) >= bytes_per_chunk:
                            ws_chunk = bytes(pcm_send_buffer[:bytes_per_chunk])
                            del pcm_send_buffer[:bytes_per_chunk]
                            if send_ws_audio_binary_blocking(
                                ws_chunk,
                                chunk_ms=websocket_audio_chunk_ms,
                                realtime_pacing=websocket_audio_realtime_pacing,
                            ):
                                chunks_sent += 1
                                bytes_sent += len(ws_chunk)
                            else:
                                ws_active = False
                                break

                stream_completed = True
                if ws_active:
                    if pcm_send_buffer:
                        ws_chunk = bytes(pcm_send_buffer)
                        pcm_send_buffer.clear()
                        if send_ws_audio_binary_blocking(
                            ws_chunk,
                            chunk_ms=websocket_audio_chunk_ms,
                            realtime_pacing=websocket_audio_realtime_pacing,
                        ):
                            chunks_sent += 1
                            bytes_sent += len(ws_chunk)
                        else:
                            ws_active = False

                if ws_active:
                    if send_ws_audio_end_blocking():
                        ws_end_sent = True
                    ws_active = False

                if ws_started:
                    LOGGER.info("ELEVENLABS STREAM END")
        finally:
            if ws_start_sent and not ws_end_sent:
                send_ws_audio_end_blocking()

            if wav_file is not None:
                wav_file.close()
                wav_file = None

            if temp_wav_path is not None and final_wav_path is not None:
                try:
                    if stream_completed:
                        temp_wav_path.replace(final_wav_path)
                        LOGGER.info("[ElevenLabs] streamed WAV saved: %s", final_wav_path)
                    else:
                        temp_wav_path.unlink(missing_ok=True)
                except Exception:
                    LOGGER.exception("Could not finalize streamed WAV: %s", final_wav_path)
                    try:
                        temp_wav_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        return StreamingTTSResult(
            audio_path=final_wav_path if save_response_wav else None,
            request_id=response.headers.get("request-id", "") if "response" in locals() else "",
            character_count=response.headers.get("x-character-count", "") if "response" in locals() else "",
            chunks_sent=chunks_sent,
            bytes_sent=bytes_sent,
        )

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        if load_dotenv is None:
            raise ElevenLabsManagerError(
                "python-dotenv is not installed. Run pip install -r requirements.txt."
            )
        self._load_env_once()
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise ElevenLabsManagerError("ELEVENLABS_API_KEY is missing in .env.")
        self._api_key = api_key
        return self._api_key

    def _build_endpoint(self, voice_id: str) -> str:
        if not self._base_url:
            self._load_env_once()
            self._base_url = os.getenv(
                "ELEVENLABS_BASE_URL",
                "https://api.elevenlabs.io",
            ).rstrip("/")
        base_url = self._base_url
        return f"{base_url}/v1/text-to-speech/{voice_id}"

    def _build_stream_endpoint(self, voice_id: str) -> str:
        return f"{self._build_endpoint(voice_id)}/stream"

    def _load_env_once(self) -> None:
        if self._env_loaded:
            return
        if load_dotenv is None:
            raise ElevenLabsManagerError(
                "python-dotenv is not installed. Run pip install -r requirements.txt."
            )
        load_dotenv(self.root / ".env")
        self._env_loaded = True

    def _new_audio_path(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return self.output_dir / f"assistant_tts_{stamp}.wav"

    def _write_pcm_as_wav(self, path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
        if not pcm_bytes:
            raise ElevenLabsManagerError("ElevenLabs returned empty audio data.")
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)

    def _format_error_response(self, response: Any) -> str:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:500]
        return f"ElevenLabs error {response.status_code}: {detail}"
