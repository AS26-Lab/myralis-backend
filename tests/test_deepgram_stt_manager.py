import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from core.deepgram_stt_manager import (
    DeepgramSTTConfig,
    DeepgramSTTManager,
)


class DeepgramSTTManagerTests(unittest.TestCase):
    def test_config_uses_env_api_key_when_setting_is_empty(self) -> None:
        manager = DeepgramSTTManager(Path.cwd())
        settings = {
            "deepgram": {
                "enabled": True,
                "api_key": "",
                "language": "es",
                "model": "nova-3",
                "interim_results": True,
                "endpointing": True,
            }
        }

        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "env-key"}, clear=False):
            config = manager._config_from_settings(settings)

        self.assertTrue(config.enabled)
        self.assertEqual(config.api_key, "env-key")
        self.assertEqual(config.language, "es")
        self.assertEqual(config.model, "nova-3")

    def test_build_listen_url_uses_pcm16_streaming_parameters(self) -> None:
        manager = DeepgramSTTManager(Path.cwd())
        config = DeepgramSTTConfig(enabled=True, api_key="secret-key")

        url = manager._build_listen_url(config)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "wss")
        self.assertEqual(parsed.netloc, "api.deepgram.com")
        self.assertEqual(parsed.path, "/v1/listen")
        self.assertEqual(query["model"], ["nova-3"])
        self.assertEqual(query["language"], ["es"])
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["sample_rate"], ["16000"])
        self.assertEqual(query["channels"], ["1"])
        self.assertEqual(query["interim_results"], ["true"])
        self.assertEqual(query["endpointing"], ["300"])
        self.assertEqual(query["utterance_end_ms"], ["1000"])
        self.assertNotIn("secret-key", url)

    def test_effective_config_uses_device_compatible_sample_rate(self) -> None:
        manager = DeepgramSTTManager(Path.cwd())
        config = DeepgramSTTConfig(enabled=True, api_key="secret-key")

        def check_input_settings(**kwargs: object) -> None:
            samplerate = int(kwargs["samplerate"])
            if samplerate != 48000:
                raise ValueError("Invalid sample rate")

        with patch(
            "core.deepgram_stt_manager.sd",
            SimpleNamespace(
                query_devices=lambda: [
                    {
                        "name": "Mic Realtek",
                        "max_input_channels": 1,
                        "max_output_channels": 0,
                        "default_samplerate": 48000,
                    }
                ],
                check_input_settings=check_input_settings,
            ),
        ):
            effective = manager._effective_config_for_input_device(config, 0)

        self.assertEqual(effective.sample_rate, 48000)
        url = manager._build_listen_url(effective)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(query["sample_rate"], ["48000"])

    def test_partial_and_final_transcript_callbacks(self) -> None:
        partials: list[str] = []
        finals: list[str] = []
        manager = DeepgramSTTManager(
            Path.cwd(),
            on_partial=partials.append,
            on_final=finals.append,
        )

        manager._handle_deepgram_message(
            json.dumps(
                {
                    "type": "Results",
                    "is_final": False,
                    "channel": {
                        "alternatives": [
                            {
                                "transcript": "hola",
                            }
                        ]
                    },
                }
            )
        )
        manager._handle_deepgram_message(
            json.dumps(
                {
                    "type": "Results",
                    "is_final": True,
                    "speech_final": True,
                    "channel": {
                        "alternatives": [
                            {
                                "transcript": "hola mundo",
                            }
                        ]
                    },
                }
            )
        )

        self.assertEqual(partials, ["hola"])
        self.assertEqual(finals, ["hola mundo"])


if __name__ == "__main__":
    unittest.main()
