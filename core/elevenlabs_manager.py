from __future__ import annotations

import logging
import os
from array import array
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.runtime_paths import get_runtime_paths

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    load_dotenv = None  # type: ignore[assignment]

try:
    import requests
except ImportError:  # pragma: no cover - handled at runtime for clearer UX.
    requests = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
DEFAULT_REALTIME_WEBSOCKET_AUDIO_CHUNK_MS = 200
DEFAULT_REALTIME_STARTUP_SILENCE_CHUNKS = 2
DEFAULT_REALTIME_PCM16_FADE_IN_MS = 15
DEFAULT_REALTIME_PCM16_FADE_OUT_MS = 20
DEFAULT_REALTIME_PCM16_TAIL_SILENCE_MS = 120
DEFAULT_PCM16_SILENCE_WINDOW_MS = 20
DEFAULT_PCM16_SILENCE_THRESHOLD_DBFS = -60.0
PCM16_BYTES_PER_SAMPLE = 2
PCM16_MAX_AMPLITUDE = 32767


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


@dataclass(frozen=True)
class WavTailAnalysis:
    total_frames: int
    frame_rate: int
    trailing_silence_frames: int
    trailing_silence_seconds: float
    effective_audio_frames: int
    effective_audio_seconds: float
    analyzed: bool
    reason: str = ""


def apply_pcm16_linear_fade_in(
    pcm_chunk: bytes,
    *,
    sample_rate: int,
    fade_ms: int,
) -> bytes:
    sample_count = len(pcm_chunk) // PCM16_BYTES_PER_SAMPLE
    fade_sample_count = min(sample_count, int(sample_rate * fade_ms / 1000))
    if fade_sample_count <= 0:
        return pcm_chunk

    faded = bytearray(pcm_chunk)
    denominator = max(1, fade_sample_count - 1)
    for sample_index in range(fade_sample_count):
        offset = sample_index * PCM16_BYTES_PER_SAMPLE
        sample = int.from_bytes(
            faded[offset : offset + PCM16_BYTES_PER_SAMPLE],
            "little",
            signed=True,
        )
        factor = sample_index / denominator
        faded_sample = int(sample * factor)
        faded[offset : offset + PCM16_BYTES_PER_SAMPLE] = faded_sample.to_bytes(
            PCM16_BYTES_PER_SAMPLE,
            "little",
            signed=True,
        )
    return bytes(faded)


def apply_pcm16_linear_fade_out(
    pcm_chunk: bytes,
    *,
    sample_rate: int,
    fade_ms: int,
) -> bytes:
    sample_count = len(pcm_chunk) // PCM16_BYTES_PER_SAMPLE
    fade_sample_count = min(sample_count, int(sample_rate * fade_ms / 1000))
    if fade_sample_count <= 0:
        return pcm_chunk

    faded = bytearray(pcm_chunk)
    denominator = max(1, fade_sample_count - 1)
    start_index = sample_count - fade_sample_count
    for sample_index in range(fade_sample_count):
        offset = (start_index + sample_index) * PCM16_BYTES_PER_SAMPLE
        sample = int.from_bytes(
            faded[offset : offset + PCM16_BYTES_PER_SAMPLE],
            "little",
            signed=True,
        )
        factor = (denominator - sample_index) / denominator
        faded_sample = int(sample * factor)
        faded[offset : offset + PCM16_BYTES_PER_SAMPLE] = faded_sample.to_bytes(
            PCM16_BYTES_PER_SAMPLE,
            "little",
            signed=True,
        )
    return bytes(faded)


def append_pcm16_silence(
    pcm_chunk: bytes,
    *,
    sample_rate: int,
    tail_silence_ms: int,
) -> bytes:
    tail_samples = max(0, int(sample_rate * tail_silence_ms / 1000))
    if tail_samples <= 0:
        return pcm_chunk
    return pcm_chunk + (b"\x00" * tail_samples * PCM16_BYTES_PER_SAMPLE)


def analyze_pcm16_wav_tail(
    path: Path,
    *,
    silence_window_ms: int = DEFAULT_PCM16_SILENCE_WINDOW_MS,
    silence_threshold_dbfs: float = DEFAULT_PCM16_SILENCE_THRESHOLD_DBFS,
) -> WavTailAnalysis:
    if not path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {path}")

    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            total_frames = wav_file.getnframes()
            comptype = wav_file.getcomptype()
            pcm_bytes = wav_file.readframes(total_frames)
    except (EOFError, wave.Error, OSError, ValueError):
        return WavTailAnalysis(
            total_frames=0,
            frame_rate=0,
            trailing_silence_frames=0,
            trailing_silence_seconds=0.0,
            effective_audio_frames=0,
            effective_audio_seconds=0.0,
            analyzed=False,
            reason="invalid_wav",
        )

    total_seconds = float(total_frames / frame_rate) if frame_rate > 0 else 0.0
    if (
        comptype != "NONE"
        or channels != 1
        or sample_width != PCM16_BYTES_PER_SAMPLE
        or frame_rate <= 0
    ):
        return WavTailAnalysis(
            total_frames=total_frames,
            frame_rate=frame_rate,
            trailing_silence_frames=0,
            trailing_silence_seconds=0.0,
            effective_audio_frames=total_frames,
            effective_audio_seconds=total_seconds,
            analyzed=False,
            reason="non_pcm16_mono",
        )

    silence_window_frames = max(1, int(frame_rate * silence_window_ms / 1000))
    silence_threshold = int(
        PCM16_MAX_AMPLITUDE * (10.0 ** (silence_threshold_dbfs / 20.0))
    )
    silence_threshold = max(0, min(PCM16_MAX_AMPLITUDE, silence_threshold))

    samples = array("h")
    samples.frombytes(pcm_bytes)

    sample_count = len(samples)
    if sample_count == 0:
        return WavTailAnalysis(
            total_frames=0,
            frame_rate=frame_rate,
            trailing_silence_frames=0,
            trailing_silence_seconds=0.0,
            effective_audio_frames=0,
            effective_audio_seconds=0.0,
            analyzed=True,
            reason="empty",
        )

    trailing_silence_frames = 0
    cursor = sample_count
    while cursor > 0:
        window_start = max(0, cursor - silence_window_frames)
        window = samples[window_start:cursor]
        if not window:
            break

        window_peak = max(abs(int(sample)) for sample in window)
        if window_peak <= silence_threshold:
            trailing_silence_frames += len(window)
            cursor = window_start
            continue

        last_audible_index = len(window) - 1
        while last_audible_index >= 0 and abs(int(window[last_audible_index])) <= silence_threshold:
            last_audible_index -= 1
        trailing_silence_frames += len(window) - (last_audible_index + 1)
        break

    trailing_silence_frames = min(trailing_silence_frames, total_frames)
    effective_audio_frames = max(0, total_frames - trailing_silence_frames)
    return WavTailAnalysis(
        total_frames=total_frames,
        frame_rate=frame_rate,
        trailing_silence_frames=trailing_silence_frames,
        trailing_silence_seconds=trailing_silence_frames / float(frame_rate),
        effective_audio_frames=effective_audio_frames,
        effective_audio_seconds=effective_audio_frames / float(frame_rate),
        analyzed=True,
        reason="",
    )


def finalize_pcm16_wav_tail(
    path: Path,
    *,
    sample_rate: int,
    fade_out_ms: int = DEFAULT_REALTIME_PCM16_FADE_OUT_MS,
    tail_silence_ms: int = DEFAULT_REALTIME_PCM16_TAIL_SILENCE_MS,
) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {path}")

    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_rate = wav_file.getframerate()
        comptype = wav_file.getcomptype()
        pcm_bytes = wav_file.readframes(wav_file.getnframes())

    if (
        comptype != "NONE"
        or channels != 1
        or sample_width != PCM16_BYTES_PER_SAMPLE
        or frame_rate != sample_rate
    ):
        LOGGER.info(
            "Skipping WAV tail smoothing for non-PCM16 mono file: %s",
            path,
        )
        return

    softened = apply_pcm16_linear_fade_out(
        pcm_bytes,
        sample_rate=sample_rate,
        fade_ms=fade_out_ms,
    )
    softened = append_pcm16_silence(
        softened,
        sample_rate=sample_rate,
        tail_silence_ms=tail_silence_ms,
    )

    temp_path = path.with_suffix(path.suffix + ".tail.tmp")
    with wave.open(str(temp_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(PCM16_BYTES_PER_SAMPLE)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(softened)
    temp_path.replace(path)
    analysis = analyze_pcm16_wav_tail(path)
    LOGGER.info(
        "[ElevenLabs] WAV tail finalized: tail_silence_ms=%.1f effective_audio_ms=%.1f",
        analysis.trailing_silence_seconds * 1000.0,
        analysis.effective_audio_seconds * 1000.0,
    )


class ElevenLabsManager:
    """Generates WAV files through the ElevenLabs text-to-speech endpoint."""

    ELEVEN_V3_MODEL_ID = "eleven_v3"

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
        finalize_pcm16_wav_tail(
            audio_path,
            sample_rate=sample_rate,
        )
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
        startup_silence_chunks: int = DEFAULT_REALTIME_STARTUP_SILENCE_CHUNKS,
        fade_in_ms: int = DEFAULT_REALTIME_PCM16_FADE_IN_MS,
        websocket_audio_chunk_ms: int = DEFAULT_REALTIME_WEBSOCKET_AUDIO_CHUNK_MS,
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

        startup_silence_chunks = max(0, int(startup_silence_chunks))
        fade_in_ms = max(0, int(fade_in_ms))

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
        if (
            optimize_streaming_latency is not None
            and model_id != self.ELEVEN_V3_MODEL_ID
        ):
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
        first_real_audio_chunk_sent = False
        bytes_per_chunk = calculate_pcm_bytes_per_chunk(
            sample_rate=self.PCM_SAMPLE_RATES[output_format],
            channels=1,
            chunk_ms=websocket_audio_chunk_ms,
        )

        def send_realtime_ws_chunk(
            ws_chunk: bytes,
            *,
            apply_fade_in: bool,
        ) -> bool:
            nonlocal bytes_sent, chunks_sent, first_real_audio_chunk_sent
            chunk_to_send = ws_chunk
            if apply_fade_in and not first_real_audio_chunk_sent:
                chunk_to_send = apply_pcm16_linear_fade_in(
                    ws_chunk,
                    sample_rate=self.PCM_SAMPLE_RATES[output_format],
                    fade_ms=fade_in_ms,
                )
                first_real_audio_chunk_sent = True

            if send_ws_audio_binary_blocking(
                chunk_to_send,
                chunk_ms=websocket_audio_chunk_ms,
                realtime_pacing=websocket_audio_realtime_pacing,
            ):
                chunks_sent += 1
                bytes_sent += len(chunk_to_send)
                return True
            return False

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
                            if startup_silence_chunks > 0:
                                silent_chunk = b"\x00" * bytes_per_chunk
                                for _ in range(startup_silence_chunks):
                                    if not send_realtime_ws_chunk(
                                        silent_chunk,
                                        apply_fade_in=False,
                                    ):
                                        ws_active = False
                                        break
                                if ws_active:
                                    LOGGER.info(
                                        "WS AUDIO startup silence sent: %s chunks x %s bytes",
                                        startup_silence_chunks,
                                        len(silent_chunk),
                                    )

                    if ws_active:
                        pcm_send_buffer.extend(chunk)
                        while len(pcm_send_buffer) >= bytes_per_chunk:
                            ws_chunk = bytes(pcm_send_buffer[:bytes_per_chunk])
                            del pcm_send_buffer[:bytes_per_chunk]
                            if not send_realtime_ws_chunk(
                                ws_chunk,
                                apply_fade_in=True,
                            ):
                                ws_active = False
                                break

                stream_completed = True
                if ws_active:
                    if pcm_send_buffer:
                        ws_chunk = bytes(pcm_send_buffer)
                        pcm_send_buffer.clear()
                        if not send_realtime_ws_chunk(
                            ws_chunk,
                            apply_fade_in=True,
                        ):
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
                        finalize_pcm16_wav_tail(
                            temp_wav_path,
                            sample_rate=self.PCM_SAMPLE_RATES[output_format],
                        )
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
        runtime_paths = get_runtime_paths(self.root)
        external_env = runtime_paths.external_config_root / ".env"
        if external_env.exists():
            load_dotenv(external_env)
        elif (self.root / ".env").exists():
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
