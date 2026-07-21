from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from core.stt_manager import VoiceSTTManager


class _FakeBackend:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.start_calls: list[dict[str, object]] = []
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.listening = False
        self.input_level = 0.0

    def set_transcript_callbacks(self, **kwargs: object) -> None:
        self.callbacks = kwargs

    def start_listening(self, **kwargs: object) -> bool:
        self.start_calls.append(kwargs)
        self.listening = True
        return True

    def stop_listening(self) -> None:
        self.stop_calls += 1
        self.listening = False

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.listening = False

    def is_listening(self) -> bool:
        return self.listening

    def get_input_level(self) -> float:
        return self.input_level


class VoiceSTTManagerTests(unittest.TestCase):
    def test_routes_start_to_local_backend_when_local_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            deepgram = _FakeBackend()
            local = _FakeBackend()
            manager = VoiceSTTManager(
                Path(temp_dir),
                deepgram_manager=deepgram,
                local_manager=local,
            )

            started = manager.start_listening(
                settings={"stt_engine": "local"},
                input_device_index=7,
            )

        self.assertTrue(started)
        self.assertEqual(local.start_calls, [{"settings": {"stt_engine": "local"}, "input_device_index": 7}])
        self.assertEqual(deepgram.start_calls, [])

    def test_routes_start_to_deepgram_backend_when_deepgram_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            deepgram = _FakeBackend()
            local = _FakeBackend()
            manager = VoiceSTTManager(
                Path(temp_dir),
                deepgram_manager=deepgram,
                local_manager=local,
            )

            started = manager.start_listening(
                settings={"stt_engine": "deepgram"},
                input_device_index=3,
            )

        self.assertTrue(started)
        self.assertEqual(deepgram.start_calls, [{"settings": {"stt_engine": "deepgram"}, "input_device_index": 3}])
        self.assertEqual(local.start_calls, [])

    def test_callbacks_and_stop_are_forwarded_to_both_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            deepgram = _FakeBackend()
            local = _FakeBackend()
            manager = VoiceSTTManager(
                Path(temp_dir),
                deepgram_manager=deepgram,
                local_manager=local,
            )
            on_partial = Mock()
            on_final = Mock()

            manager.set_transcript_callbacks(on_partial=on_partial, on_final=on_final)
            manager.stop_listening()

            self.assertIs(deepgram.callbacks["on_partial"], on_partial)
            self.assertIs(local.callbacks["on_final"], on_final)
            self.assertEqual(deepgram.stop_calls, 1)
            self.assertEqual(local.stop_calls, 1)
            self.assertFalse(manager.is_listening())


if __name__ == "__main__":
    unittest.main()
