import os
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.settings_manager import SettingsManager
from core.usage_estimator import (
    UsageEstimator,
    record_usage_adaptation,
    usage_adaptation_multiplier,
)


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class UsageEstimatorTests(unittest.TestCase):
    def test_usage_adaptation_starts_neutral_until_samples_exist(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }

        multiplier, samples = usage_adaptation_multiplier(settings)

        self.assertEqual(multiplier, 1.0)
        self.assertEqual(samples, 0)

    def test_manual_budget_uses_conservative_miralys_tokens(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4-nano",
            "response_length": "short",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        env = {
            "USAGE_BUDGET_USD": "10",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.70",
            "USAGE_ESTIMATE_COST_LOW_USAGE_USD": "0.01",
            "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR": "100",
        }

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator().build_snapshot(settings)

        self.assertEqual(snapshot.usage_profile, "low_usage")
        self.assertEqual(snapshot.conversations_estimate, 700)
        self.assertEqual(snapshot.hours_estimate, 7.0)
        self.assertEqual(snapshot.miralys_tokens_per_conversation, 10)
        self.assertEqual(snapshot.miralys_tokens_remaining, 7000)
        self.assertEqual(snapshot.miralys_tokens_per_usd, 1000)
        self.assertEqual(snapshot.usage_budget_source, "manual_budget_estimated")

    def test_elevenlabs_subscription_limits_conversations_when_available(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "very_short",
            "elevenlabs_model": "eleven_turbo_v2_5",
            "test_mode": {"enabled": False},
        }
        env = {
            "ELEVENLABS_API_KEY": "test-key",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.50",
            "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR": "60",
        }
        session = Mock()
        session.get.return_value = _FakeResponse(
            {"character_count": 1000, "character_limit": 5000}
        )

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator(session=session).build_snapshot(settings)

        self.assertEqual(snapshot.conversations_estimate, 6)
        self.assertAlmostEqual(snapshot.hours_estimate, 6 / 60)
        self.assertEqual(snapshot.usage_budget_source, "elevenlabs_real")
        self.assertEqual(snapshot.usage_confidence, "real_api")
        self.assertEqual(snapshot.miralys_tokens_per_conversation, 50)
        self.assertEqual(snapshot.miralys_tokens_remaining, 300)
        session.get.assert_called_once()

    def test_miralys_token_balance_can_drive_license_estimates(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        env = {
            "MIRALYS_TOKENS_REMAINING": "10000",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.80",
        }

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator().build_snapshot(settings)

        self.assertEqual(snapshot.usage_profile, "balanced")
        self.assertEqual(snapshot.miralys_tokens_per_conversation, 50)
        self.assertEqual(snapshot.conversations_estimate, 160)
        self.assertEqual(snapshot.miralys_tokens_remaining, 8000)
        self.assertEqual(snapshot.usage_budget_source, "miralys_tokens_estimated")

    def test_test_mode_miralys_tokens_override_env_balance(self) -> None:
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
        env = {
            "MIRALYS_TOKENS_REMAINING": "10000",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.80",
        }

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator().build_snapshot(settings)
            payload = snapshot.to_backend_payload_fields()

        self.assertEqual(snapshot.conversations_estimate, 64)
        self.assertEqual(snapshot.miralys_tokens_remaining, 3200)
        self.assertEqual(snapshot.usage_budget_source, "miralys_tokens_test")
        self.assertEqual(snapshot.usage_percent, 20.0)
        self.assertEqual(payload["tokens_available"], "4000")
        self.assertEqual(payload["usage_percent"], "20.0")

    def test_small_miralys_token_balance_keeps_fractional_hours(self) -> None:
        settings = {
            "interaction_mode": "voice",
            "openai_model": "gpt-5.5",
            "response_length": "detailed",
            "elevenlabs_model": "eleven_v3",
            "test_mode": {
                "miralys_tokens_purchased": 500,
                "miralys_tokens_used": 50,
            },
        }
        env = {
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.70",
            "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR": "120",
        }

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator().build_snapshot(settings)
            payload = snapshot.to_backend_payload_fields()

        self.assertEqual(snapshot.miralys_tokens_per_conversation, 156)
        self.assertEqual(snapshot.conversations_estimate, 2)
        self.assertAlmostEqual(snapshot.hours_estimate, 2 / 120)
        self.assertEqual(snapshot.miralys_tokens_remaining, 312)
        self.assertEqual(payload["usage_percent"], "10.0")

    def test_openai_admin_costs_use_monthly_budget_when_configured(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        env = {
            "OPENAI_ADMIN_API_KEY": "admin-key",
            "OPENAI_MONTHLY_BUDGET_USD": "10",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.50",
        }
        session = Mock()
        session.get.return_value = _FakeResponse(
            {
                "data": [
                    {
                        "results": [
                            {"amount": {"value": 3.0, "currency": "usd"}},
                            {"amount": {"value": 1.0, "currency": "usd"}},
                        ]
                    }
                ]
            }
        )

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator(session=session).build_snapshot(settings)

        self.assertEqual(snapshot.conversations_estimate, 60)
        self.assertEqual(snapshot.miralys_tokens_per_conversation, 50)
        self.assertEqual(snapshot.miralys_tokens_remaining, 3000)
        self.assertEqual(snapshot.usage_budget_source, "openai_costs_real")
        self.assertEqual(snapshot.usage_confidence, "real_api")
        session.get.assert_called_once()

    def test_openai_admin_key_without_budget_does_not_invent_remaining_usage(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
        }
        env = {"OPENAI_ADMIN_API_KEY": "admin-key"}
        session = Mock()

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator(session=session).build_snapshot(settings)

        self.assertEqual(snapshot.conversations_estimate, 0)
        self.assertEqual(snapshot.usage_budget_source, "openai_admin_costs_no_budget")
        self.assertEqual(snapshot.usage_confidence, "missing_budget")
        session.get.assert_not_called()

    def test_test_mode_does_not_count_elevenlabs_subscription_as_limit(self) -> None:
        settings = {
            "interaction_mode": "text",
            "openai_model": "gpt-5.4",
            "response_length": "balanced",
            "elevenlabs_model": "eleven_turbo_v2_5",
            "test_mode": {"enabled": True},
        }
        env = {
            "ELEVENLABS_API_KEY": "test-key",
            "USAGE_ESTIMATE_CONSERVATIVE_FACTOR": "0.50",
        }
        session = Mock()

        with patch.dict(os.environ, env, clear=True):
            snapshot = UsageEstimator(session=session).build_snapshot(settings)

        self.assertEqual(snapshot.conversations_estimate, 0)
        self.assertEqual(snapshot.usage_confidence, "missing_budget")
        session.get.assert_not_called()

    def test_usage_adaptation_recording_lowers_estimated_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = SettingsManager(Path(temp_dir))
            settings = settings_manager.get_settings()
            settings_manager.apply_official_setting_update("interaction_mode", "text")
            settings_manager.apply_official_setting_update("openai_model", "gpt-5.4")
            settings_manager.apply_official_setting_update("response_length", "balanced")
            settings_manager.apply_official_setting_update(
                "elevenlabs_model",
                "eleven_turbo_v2_5",
            )
            settings = settings_manager.get_settings()

            before_multiplier, before_samples = usage_adaptation_multiplier(settings)
            self.assertEqual(before_multiplier, 1.0)
            self.assertEqual(before_samples, 0)

            record_usage_adaptation(
                settings_manager=settings_manager,
                settings=settings,
                user_text="hola",
                assistant_text="ok",
            )

            updated_settings = settings_manager.get_settings()
            after_multiplier, after_samples = usage_adaptation_multiplier(updated_settings)
            snapshot = UsageEstimator(Path(temp_dir)).build_snapshot(updated_settings)

        self.assertGreaterEqual(after_samples, 1)
        self.assertLess(after_multiplier, 1.0)
        self.assertLess(snapshot.estimated_cost_per_interaction_usd, 0.05)


if __name__ == "__main__":
    unittest.main()
