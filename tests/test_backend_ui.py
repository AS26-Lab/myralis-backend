import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ui.main_window import MainWindow


class _BackendUIHarness:
    _build_backend_ui_payload = MainWindow._build_backend_ui_payload
    _send_backend_ui_snapshot = MainWindow._send_backend_ui_snapshot
    _usage_profile = MainWindow._usage_profile
    _usage_estimates = MainWindow._usage_estimates
    _estimated_complete_interaction_cost = (
        MainWindow._estimated_complete_interaction_cost
    )
    _usage_budget_remaining = MainWindow._usage_budget_remaining
    _float_from_env = MainWindow._float_from_env
    _interaction_mode = MainWindow._interaction_mode

    def __init__(self, settings: dict[str, object]) -> None:
        self.settings_manager = SimpleNamespace(get_settings=lambda: settings)


class BackendUITests(unittest.TestCase):
    def test_backend_ui_payload_uses_runtime_fields_and_string_estimates(self) -> None:
        settings = {
            "interaction_mode": "voice",
            "stt_engine": "deepgram",
            "listening_emotion_analysis": True,
            "openai_model": "GPT-5.4 mini",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
            "deepgram": {"enabled": True},
        }
        env = {
            "USAGE_BUDGET_USD": "12",
            "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR": "60",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "1",
            "USAGE_ESTIMATE_COST_BALANCED_USD": "0.04",
            "USAGE_ESTIMATE_STT_COST_USD": "0.01",
            "USAGE_ESTIMATE_LISTENING_EMOTION_COST_USD": "0.005",
        }

        with patch.dict(os.environ, env, clear=True), self.assertLogs(
            "ui.main_window",
            level="INFO",
        ) as logs:
            payload = _BackendUIHarness(settings)._build_backend_ui_payload("connected")

        self.assertEqual(
            payload,
            {
                "type": "backend_ui",
                "system_connection_status": "connected",
                "usage_conversations_estimate": "218",
                "usage_hours_estimate": "3",
                "usage_profile": "balanced",
            },
        )
        self.assertTrue(
            any("source=local_estimates billing_apis=not_integrated" in line for line in logs.output)
        )

    def test_text_mode_estimate_excludes_stt_and_listening_emotion_costs(self) -> None:
        env = {
            "USAGE_BUDGET_USD": "12",
            "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR": "60",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "1",
            "USAGE_ESTIMATE_COST_BALANCED_USD": "0.04",
            "USAGE_ESTIMATE_STT_COST_USD": "0.01",
            "USAGE_ESTIMATE_LISTENING_EMOTION_COST_USD": "0.005",
        }
        settings = {
            "interaction_mode": "text",
            "stt_engine": "deepgram",
            "listening_emotion_analysis": True,
            "openai_model": "GPT-5.4 mini",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
            "deepgram": {"enabled": True},
        }

        with patch.dict(os.environ, env, clear=True):
            payload = _BackendUIHarness(settings)._build_backend_ui_payload("connected")

        self.assertEqual(payload["usage_conversations_estimate"], "300")
        self.assertEqual(payload["usage_hours_estimate"], "5")
        self.assertEqual(payload["usage_profile"], "balanced")

    def test_backend_ui_without_budget_does_not_invent_credit_data(self) -> None:
        settings = {
            "interaction_mode": "voice",
            "openai_model": "GPT-5",
            "response_length": "detailed",
            "elevenlabs_model": "eleven_v3",
        }

        with patch.dict(os.environ, {}, clear=True):
            payload = _BackendUIHarness(settings)._build_backend_ui_payload("bad")

        self.assertEqual(payload["system_connection_status"], "error")
        self.assertEqual(payload["usage_conversations_estimate"], "0")
        self.assertEqual(payload["usage_hours_estimate"], "0")
        self.assertEqual(payload["usage_profile"], "high_quality")

    def test_send_backend_ui_snapshot_sends_backend_ui_only(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "GPT-5.4 nano",
            "response_length": "short",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        env = {
            "USAGE_BUDGET_USD": "1",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "1",
            "USAGE_ESTIMATE_COST_LOW_USAGE_USD": "0.01",
        }
        window = _BackendUIHarness(settings)

        with patch.dict(os.environ, env, clear=True), patch(
            "ui.main_window.send_json_to_unreal_threadsafe"
        ) as send_json:
            window._send_backend_ui_snapshot("reconnecting")

        send_json.assert_called_once()
        payload = send_json.call_args.args[0]
        self.assertEqual(payload["type"], "backend_ui")
        self.assertNotEqual(payload["type"], "settings_update")
        self.assertNotEqual(payload["type"], "runtime_state")
        self.assertEqual(payload["system_connection_status"], "reconnecting")
        self.assertEqual(payload["usage_profile"], "low_usage")


if __name__ == "__main__":
    unittest.main()
