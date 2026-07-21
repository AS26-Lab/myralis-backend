import tempfile
import wave
import unittest
from pathlib import Path
from unittest.mock import patch

from core.elevenlabs_manager import (
    ElevenLabsManager,
    analyze_pcm16_wav_tail,
    finalize_pcm16_wav_tail,
)


class _StreamingResponse:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.status_code = 200
        self.headers = {"request-id": "request-id", "x-character-count": "12"}
        self._chunks = chunks or [b"\x00\x00" * 16]
        self.content = b"".join(self._chunks)

    def __enter__(self) -> "_StreamingResponse":
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def iter_content(self, chunk_size: int) -> list[bytes]:
        _ = chunk_size
        return self._chunks


class _RecordingSession:
    def __init__(self, response: _StreamingResponse | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or _StreamingResponse()

    def post(self, endpoint: str, **kwargs: object) -> _StreamingResponse:
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self.response


class ElevenLabsManagerTests(unittest.TestCase):
    def _manager(self, temp_dir: str) -> tuple[ElevenLabsManager, _RecordingSession]:
        session = _RecordingSession()
        manager = ElevenLabsManager(Path(temp_dir), Path(temp_dir) / "audio")
        manager._api_key = "test-key"
        manager._base_url = "https://example.test"
        manager._session = session
        return manager, session

    def _stream(self, manager: ElevenLabsManager, model_id: str) -> None:
        with patch(
            "core.websocket_server.send_ws_audio_start_blocking",
            return_value=False,
        ):
            manager.stream_elevenlabs_tts_to_unreal(
                text="hola",
                voice_id="voice",
                model_id=model_id,
                output_format="pcm_24000",
                optimize_streaming_latency=2,
                save_response_wav=False,
            )

    def test_streaming_omits_optimize_latency_for_eleven_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, session = self._manager(temp_dir)

            self._stream(manager, "eleven_v3")

        self.assertEqual(session.calls[0]["params"], {"output_format": "pcm_24000"})

    def test_streaming_keeps_optimize_latency_for_supported_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, session = self._manager(temp_dir)

            self._stream(manager, "eleven_turbo_v2_5")

        self.assertEqual(
            session.calls[0]["params"],
            {"output_format": "pcm_24000", "optimize_streaming_latency": 2},
        )

    def test_streaming_sends_startup_silence_then_faded_first_audio_chunk(self) -> None:
        bytes_per_200_ms_chunk = 9600
        real_chunk = _pcm16_samples(1000, bytes_per_200_ms_chunk // 2)
        response = _StreamingResponse([real_chunk])
        events: list[object] = []
        binary_calls: list[dict[str, object]] = []

        def start_ws(**_: object) -> bool:
            events.append("START")
            return True

        def send_binary(data: bytes, **kwargs: object) -> bool:
            events.append("BINARY")
            binary_calls.append({"data": data, **kwargs})
            return True

        def end_ws(**_: object) -> bool:
            events.append("END")
            return True

        with tempfile.TemporaryDirectory() as temp_dir:
            manager, session = self._manager(temp_dir)
            session.response = response

            with patch(
                "core.websocket_server.send_ws_audio_start_blocking",
                side_effect=start_ws,
            ), patch(
                "core.websocket_server.send_ws_audio_binary_blocking",
                side_effect=send_binary,
            ), patch(
                "core.websocket_server.send_ws_audio_end_blocking",
                side_effect=end_ws,
            ):
                result = manager.stream_elevenlabs_tts_to_unreal(
                    text="hola",
                    voice_id="voice",
                    model_id="eleven_turbo_v2_5",
                    output_format="pcm_24000",
                    save_response_wav=False,
                    startup_silence_chunks=2,
                    fade_in_ms=15,
                    websocket_audio_chunk_ms=200,
                    websocket_audio_realtime_pacing=True,
                )

        self.assertEqual(events, ["START", "BINARY", "BINARY", "BINARY", "END"])
        self.assertEqual(len(binary_calls), 3)
        self.assertEqual(binary_calls[0]["data"], b"\x00" * bytes_per_200_ms_chunk)
        self.assertEqual(binary_calls[1]["data"], b"\x00" * bytes_per_200_ms_chunk)
        self.assertEqual(binary_calls[2]["data"] != b"\x00" * bytes_per_200_ms_chunk, True)
        self.assertEqual(binary_calls[0]["chunk_ms"], 200)
        self.assertIs(binary_calls[0]["realtime_pacing"], True)

        faded_chunk = bytes(binary_calls[2]["data"])
        self.assertEqual(len(faded_chunk), bytes_per_200_ms_chunk)
        self.assertEqual(_pcm16_sample_at(faded_chunk, 0), 0)
        self.assertEqual(_pcm16_sample_at(faded_chunk, 180), int(1000 * 180 / 359))
        self.assertEqual(_pcm16_sample_at(faded_chunk, 359), 1000)
        self.assertEqual(_pcm16_sample_at(faded_chunk, 360), 1000)
        self.assertEqual(result.chunks_sent, 3)
        self.assertEqual(result.bytes_sent, bytes_per_200_ms_chunk * 3)

    def test_generate_wav_smooths_the_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, session = self._manager(temp_dir)
            session.response = _StreamingResponse([_pcm16_samples(1000, 1000)])

            result = manager.generate_wav(
                text="hola",
                voice_id="voice",
                model_id="eleven_turbo_v2_5",
                output_format="pcm_24000",
            )

            with wave.open(str(result.audio_path), "rb") as wav_file:
                frames = wav_file.getnframes()
                pcm = wav_file.readframes(frames)

        self.assertEqual(frames, 3880)
        self.assertEqual(_pcm16_sample_at(pcm, 999), 0)
        self.assertEqual(_pcm16_sample_at(pcm, 1000), 0)
        self.assertEqual(_pcm16_sample_at(pcm, frames - 1), 0)

    def test_finalize_pcm16_wav_tail_adds_silence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "test.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                wav_file.writeframes(_pcm16_samples(1000, 1000))

            finalize_pcm16_wav_tail(wav_path, sample_rate=24000)

            with wave.open(str(wav_path), "rb") as wav_file:
                frames = wav_file.getnframes()
                pcm = wav_file.readframes(frames)

        self.assertEqual(frames, 3880)
        self.assertEqual(_pcm16_sample_at(pcm, 999), 0)
        self.assertEqual(_pcm16_sample_at(pcm, frames - 1), 0)

    def test_analyze_pcm16_wav_tail_detects_effective_audio_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "test.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                wav_file.writeframes(_pcm16_samples(1000, 1000))
                wav_file.writeframes(_pcm16_samples(0, 240))

            analysis = analyze_pcm16_wav_tail(wav_path)

        self.assertTrue(analysis.analyzed)
        self.assertEqual(analysis.total_frames, 1240)
        self.assertEqual(analysis.trailing_silence_frames, 240)
        self.assertAlmostEqual(analysis.trailing_silence_seconds, 0.01, places=3)
        self.assertEqual(analysis.effective_audio_frames, 1000)
        self.assertAlmostEqual(analysis.effective_audio_seconds, 1000 / 24000, places=5)


def _pcm16_samples(value: int, count: int) -> bytes:
    return b"".join(
        int(value).to_bytes(2, "little", signed=True)
        for _ in range(count)
    )


def _pcm16_sample_at(data: bytes, sample_index: int) -> int:
    offset = sample_index * 2
    return int.from_bytes(data[offset : offset + 2], "little", signed=True)


if __name__ == "__main__":
    unittest.main()
