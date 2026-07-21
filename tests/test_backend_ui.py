import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ui.main_window import MainWindow


class _BackendUIHarness:
    _build_backend_ui_payload = MainWindow._build_backend_ui_payload
    _build_out_of_credits_payload = MainWindow._build_out_of_credits_payload
    _current_out_of_credits_state = MainWindow._current_out_of_credits_state
    _actual_out_of_credits_state = MainWindow._actual_out_of_credits_state
    _estimated_generation_cost_usd = MainWindow._estimated_generation_cost_usd
    _send_backend_ui_snapshot = MainWindow._send_backend_ui_snapshot
    _send_out_of_credits_state = MainWindow._send_out_of_credits_state
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
            "openai_model": "gpt-5.4",
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

        self.assertEqual(payload["type"], "backend_ui")
        self.assertEqual(payload["system_connection_status"], "connected")
        self.assertIs(payload["debug_mode"], False)
        self.assertEqual(payload["tokens_available"], "11990")
        self.assertEqual(payload["usage_percent"], "0.0")
        self.assertEqual(payload["usage_profile"], "balanced")
        self.assertEqual(payload["usage_budget_source"], "manual_budget_estimated")
        self.assertEqual(payload["usage_confidence"], "estimated")
        self.assertEqual(payload["usage_estimated_cost_per_interaction_usd"], "0.0550")
        self.assertEqual(payload["miralys_tokens_per_conversation"], "55")
        self.assertEqual(payload["miralys_tokens_per_usd"], "1000")
        self.assertTrue(
            any("source=manual_budget_estimated" in line for line in logs.output)
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
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
            "deepgram": {"enabled": True},
        }

        with patch.dict(os.environ, env, clear=True):
            payload = _BackendUIHarness(settings)._build_backend_ui_payload("connected")

        self.assertEqual(payload["tokens_available"], "12000")
        self.assertEqual(payload["usage_percent"], "0.0")
        self.assertEqual(payload["usage_profile"], "balanced")
        self.assertEqual(payload["miralys_tokens_per_conversation"], "40")

    def test_backend_ui_payload_includes_unreal_debug_mode_true(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        window = _BackendUIHarness(settings)
        window._unreal_debug_mode = True

        with patch.dict(os.environ, {}, clear=True):
            payload = window._build_backend_ui_payload("connected")

        self.assertIs(payload["debug_mode"], True)

    def test_backend_ui_without_budget_does_not_invent_credit_data(self) -> None:
        settings = {
            "interaction_mode": "voice",
            "openai_model": "gpt-5.5",
            "response_length": "detailed",
            "elevenlabs_model": "eleven_v3",
        }

        with patch.dict(os.environ, {}, clear=True):
            payload = _BackendUIHarness(settings)._build_backend_ui_payload("bad")

        self.assertEqual(payload["system_connection_status"], "error")
        self.assertEqual(payload["tokens_available"], "0")
        self.assertEqual(payload["usage_percent"], "0.0")
        self.assertEqual(payload["usage_profile"], "high_quality")
        self.assertEqual(payload["usage_confidence"], "missing_budget")

    def test_send_backend_ui_snapshot_sends_backend_ui_only(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4-nano",
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
        self.assertIs(payload["debug_mode"], False)
        self.assertEqual(payload["system_connection_status"], "reconnecting")
        self.assertEqual(payload["usage_profile"], "low_usage")

    def test_backend_ui_uses_test_miralys_tokens_from_settings(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
            "test_mode": {
                "miralys_tokens_purchased": 5000,
                "miralys_tokens_used": 1000,
            },
        }
        window = _BackendUIHarness(settings)

        with patch.dict(
            os.environ,
            {"USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.80"},
            clear=True,
        ):
            payload = window._build_backend_ui_payload("connected")

        self.assertEqual(payload["usage_budget_source"], "miralys_tokens_test")
        self.assertEqual(payload["tokens_available"], "4000")
        self.assertEqual(payload["miralys_tokens_per_conversation"], "50")
        self.assertEqual(payload["usage_percent"], "20.0")

    def test_out_of_credits_payload_defaults_to_license_balance(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        harness = _BackendUIHarness(settings)
        harness._license_validation_result = SimpleNamespace(credits_balance=12.5)
        harness._out_of_credits_ready = True

        payload = harness._build_out_of_credits_payload()

        self.assertEqual(payload["type"], "out_of_credits")
        self.assertEqual(payload["is_out_of_credits"], "False")

        harness._license_validation_result = SimpleNamespace(credits_balance=0.0)
        payload = harness._build_out_of_credits_payload()
        self.assertEqual(payload["is_out_of_credits"], "True")

        harness._license_validation_result = SimpleNamespace(credits_balance=0.01)
        payload = harness._build_out_of_credits_payload()
        self.assertEqual(payload["is_out_of_credits"], "True")

    def test_out_of_credits_override_forces_payload_and_dedupes(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        harness = _BackendUIHarness(settings)
        harness._license_validation_result = SimpleNamespace(credits_balance=25.0)
        harness._out_of_credits_ready = True
        harness._last_out_of_credits_sent = None
        harness._out_of_credits_override = True

        with patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            harness._send_out_of_credits_state(force=True)
            harness._send_out_of_credits_state()

        self.assertEqual(send_json.call_count, 1)
        payload = send_json.call_args.args[0]
        self.assertEqual(payload["type"], "out_of_credits")
        self.assertEqual(payload["is_out_of_credits"], "True")

    def test_out_of_credits_is_silenced_before_finish_loading(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        harness = _BackendUIHarness(settings)
        harness._license_validation_result = SimpleNamespace(credits_balance=0.0)
        harness._out_of_credits_ready = False

        with patch("ui.main_window.send_json_to_unreal_threadsafe") as send_json:
            harness._send_out_of_credits_state()

        send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
