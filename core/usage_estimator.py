from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.runtime_paths import get_runtime_paths
from core.settings_manager import normalize_openai_model_id, openai_model_profile

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled by env fallback.
    load_dotenv = None  # type: ignore[assignment]

try:
    import requests
except ImportError:  # pragma: no cover - handled by local estimate fallback.
    requests = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)

USAGE_PROFILES = {"low_usage", "balanced", "high_quality"}
MIRALYS_TOKENS_PER_USD = 1000
DEFAULT_USAGE_COST_BY_PROFILE_USD: dict[str, float] = {
    "low_usage": 0.015,
    "balanced": 0.050,
    "high_quality": 0.150,
}
DEFAULT_USAGE_STT_COST_USD = 0.004
DEFAULT_USAGE_LISTENING_EMOTION_COST_USD = 0.002
DEFAULT_USAGE_CONSERVATIVE_FACTOR = 0.95
DEFAULT_USAGE_AVERAGE_CONVERSATIONS_PER_HOUR = 100.0
DEFAULT_USAGE_TTS_CHARS_PER_WORD = 7.0
DEFAULT_USAGE_TTS_CHARS_OVERHEAD = 80.0
DEFAULT_USAGE_ADAPTATION_ALPHA = 0.25
DEFAULT_USAGE_ADAPTATION_MIN_MULTIPLIER = 0.70
DEFAULT_USAGE_ADAPTATION_MAX_MULTIPLIER = 1.35
PROVIDER_CACHE_TTL_SECONDS = 300.0

RESPONSE_LENGTH_MAX_WORDS: dict[str, int] = {
    "very_short": 36,
    "short": 64,
    "balanced": 112,
    "detailed": 176,
}


@dataclass(frozen=True)
class ProviderBudget:
    name: str
    source: str
    remaining_units: float
    conversations: int
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class UsageSnapshot:
    usage_profile: str
    conversations_estimate: int
    hours_estimate: float
    usage_percent: float
    usage_coins_used: int
    usage_coins_purchased: int
    estimated_cost_per_interaction_usd: float
    budget_remaining_usd_estimate: float
    conservative_factor: float
    miralys_tokens_available_raw: int
    miralys_tokens_remaining: int
    miralys_tokens_per_conversation: int
    miralys_tokens_per_usd: int
    adaptive_cost_multiplier: float
    adaptive_samples: int
    usage_budget_source: str
    usage_confidence: str

    def to_backend_payload_fields(self) -> dict[str, str | int | float]:
        # Keep internal field names stable; the payload uses user-facing names
        # that are easier to read in the backend/UI bridge.
        return {
            "tokens_available": str(
                self.miralys_tokens_available_raw or self.miralys_tokens_remaining
            ),
            "usage_percent": f"{self.usage_percent:.1f}",
            "usage_coins_used": str(self.usage_coins_used),
            "usage_coins_purchased": str(self.usage_coins_purchased),
            "usage_profile": self.usage_profile,
            "usage_budget_source": self.usage_budget_source,
            "usage_confidence": self.usage_confidence,
            "usage_estimated_cost_per_interaction_usd": (
                f"{self.estimated_cost_per_interaction_usd:.4f}"
            ),
            "usage_budget_remaining_usd_estimate": (
                f"{self.budget_remaining_usd_estimate:.2f}"
            ),
            "usage_conservative_factor": f"{self.conservative_factor:.2f}",
            "usage_adaptive_cost_multiplier": f"{self.adaptive_cost_multiplier:.2f}",
            "usage_adaptive_samples": str(self.adaptive_samples),
            "miralys_tokens_per_conversation": (
                str(self.miralys_tokens_per_conversation)
            ),
            "miralys_tokens_per_usd": str(self.miralys_tokens_per_usd),
        }


class UsageEstimator:
    """Builds conservative usage estimates for backend_ui snapshots."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        session: Any | None = None,
        now: Any | None = None,
    ) -> None:
        self.root = root
        self._session = session if session is not None else (
            requests.Session() if requests is not None else None
        )
        self._now = now if now is not None else time.time
        self._env_loaded = False
        self._provider_cache: dict[str, tuple[float, Any]] = {}

    def build_snapshot(self, settings: dict[str, Any]) -> UsageSnapshot:
        self._load_env_once()
        profile = usage_profile(settings)
        adaptive_multiplier, adaptive_samples = usage_adaptation_multiplier(settings)
        estimated_cost = estimated_complete_interaction_cost(
            settings,
            profile,
            adaptive_multiplier=adaptive_multiplier,
        )
        cost_tokens = max(
            1,
            int(math.ceil(estimated_cost * MIRALYS_TOKENS_PER_USD)),
        )
        conservative_factor = conservative_factor_from_env()
        average_per_hour = average_conversations_per_hour_from_env()
        purchased_tokens = test_miralys_tokens_purchased(settings)
        used_tokens = test_miralys_tokens_used(settings)

        budgets = self._provider_budgets(
            settings=settings,
            estimated_cost=estimated_cost,
            conservative_factor=conservative_factor,
        )
        available_budgets = [budget for budget in budgets if budget.available]

        if available_budgets:
            limiting_budget = min(
                available_budgets,
                key=lambda budget: budget.conversations,
            )
            conversations = max(0, limiting_budget.conversations)
            source = "+".join(budget.source for budget in available_budgets)
            has_estimated_budget = any(
                "estimated" in budget.source for budget in available_budgets
            )
            has_real_budget = any(
                "real" in budget.source for budget in available_budgets
            )
            if has_estimated_budget and has_real_budget:
                confidence = "mixed"
            elif has_real_budget:
                confidence = "real_api"
            else:
                confidence = "estimated"
        else:
            conversations = 0
            source = self._missing_source_label()
            confidence = "missing_budget"

        hours = conversations / average_per_hour
        raw_tokens_remaining = max(
            0,
            self._raw_tokens_available(settings),
        )
        tokens_remaining = max(0, conversations * cost_tokens)
        if raw_tokens_remaining <= 0:
            raw_tokens_remaining = tokens_remaining
        budget_usd_estimate = tokens_remaining / MIRALYS_TOKENS_PER_USD
        usage_percent = self._usage_percent_from_tokens(
            purchased_tokens=purchased_tokens,
            used_tokens=used_tokens,
            remaining_tokens=raw_tokens_remaining,
        )

        return UsageSnapshot(
            usage_profile=profile,
            conversations_estimate=conversations,
            hours_estimate=max(0.0, hours),
            usage_percent=usage_percent,
            usage_coins_used=used_tokens,
            usage_coins_purchased=purchased_tokens,
            estimated_cost_per_interaction_usd=estimated_cost,
            budget_remaining_usd_estimate=budget_usd_estimate,
            conservative_factor=conservative_factor,
            miralys_tokens_available_raw=raw_tokens_remaining,
            miralys_tokens_remaining=tokens_remaining,
            miralys_tokens_per_conversation=cost_tokens,
            miralys_tokens_per_usd=MIRALYS_TOKENS_PER_USD,
            adaptive_cost_multiplier=adaptive_multiplier,
            adaptive_samples=adaptive_samples,
            usage_budget_source=source,
            usage_confidence=confidence,
        )

    def _provider_budgets(
        self,
        *,
        settings: dict[str, Any],
        estimated_cost: float,
        conservative_factor: float,
    ) -> list[ProviderBudget]:
        budgets: list[ProviderBudget] = []

        test_mode_has_tokens = (
            test_miralys_tokens_purchased(settings) > 0
            or test_miralys_tokens_used(settings) > 0
            or test_miralys_tokens_remaining(settings) > 0
        )
        test_miralys_tokens = self._raw_tokens_available(settings)
        if test_mode_has_tokens and test_miralys_tokens > 0:
            cost_tokens = max(
                1,
                int(math.ceil(estimated_cost * MIRALYS_TOKENS_PER_USD)),
            )
            usable_tokens = test_miralys_tokens * conservative_factor
            budgets.append(
                ProviderBudget(
                    name="miralys_tokens_test",
                    source="miralys_tokens_test",
                    remaining_units=usable_tokens,
                    conversations=int(usable_tokens / cost_tokens),
                    available=True,
                    detail="test_mode.miralys_tokens_remaining",
                )
            )
        else:
            miralys_tokens = miralys_tokens_remaining_from_env()
            if miralys_tokens > 0:
                cost_tokens = max(
                    1,
                    int(math.ceil(estimated_cost * MIRALYS_TOKENS_PER_USD)),
                )
                usable_tokens = miralys_tokens * conservative_factor
                budgets.append(
                    ProviderBudget(
                        name="miralys_tokens",
                        source="miralys_tokens_estimated",
                        remaining_units=usable_tokens,
                        conversations=int(usable_tokens / cost_tokens),
                        available=True,
                        detail="MIRALYS_TOKENS_REMAINING or MIRALYS_TOKEN_BALANCE",
                    )
                )

        manual_budget = usage_budget_remaining_from_env()
        if manual_budget > 0:
            usable_budget = manual_budget * conservative_factor
            budgets.append(
                ProviderBudget(
                    name="manual_usd",
                    source="manual_budget_estimated",
                    remaining_units=usable_budget,
                    conversations=int(
                        usable_budget / max(estimated_cost, 0.0001)
                    ),
                    available=True,
                    detail="USAGE_BUDGET_USD or USAGE_CREDITS_REMAINING",
                )
            )

        openai_budget = self._openai_admin_budget(
            estimated_cost,
            conservative_factor,
        )
        if openai_budget is not None:
            budgets.append(openai_budget)

        elevenlabs_budget = self._elevenlabs_budget(settings, conservative_factor)
        if elevenlabs_budget is not None:
            budgets.append(elevenlabs_budget)

        return budgets

    def _raw_tokens_available(self, settings: dict[str, Any]) -> int:
        purchased_tokens = test_miralys_tokens_purchased(settings)
        used_tokens = test_miralys_tokens_used(settings)
        if purchased_tokens > 0:
            return max(0, int(purchased_tokens) - int(used_tokens))

        test_miralys_tokens = test_miralys_tokens_remaining(settings)
        if test_miralys_tokens > 0:
            return int(test_miralys_tokens)

        miralys_tokens = miralys_tokens_remaining_from_env()
        if miralys_tokens > 0:
            return int(miralys_tokens)

        return 0

    def _usage_percent_from_tokens(
        self,
        *,
        purchased_tokens: int,
        used_tokens: int,
        remaining_tokens: int,
    ) -> float:
        if purchased_tokens > 0:
            used = max(0, min(int(purchased_tokens), int(used_tokens)))
            return max(0.0, min(100.0, (used / float(purchased_tokens)) * 100.0))

        if remaining_tokens > 0:
            return 0.0

        return 0.0

    def _openai_admin_budget(
        self,
        estimated_cost: float,
        conservative_factor: float,
    ) -> ProviderBudget | None:
        admin_key = os.getenv("OPENAI_ADMIN_API_KEY", "").strip()
        budget = openai_monthly_budget_from_env()
        if not admin_key or budget <= 0 or self._session is None:
            return None

        current_month_cost = self._cached_provider_call(
            "openai_current_month_cost",
            lambda: self._fetch_openai_current_month_cost(admin_key),
        )
        if current_month_cost is None:
            return None

        remaining_budget = max(0.0, budget - float(current_month_cost))
        usable_budget = remaining_budget * conservative_factor
        return ProviderBudget(
            name="openai_monthly_budget",
            source="openai_costs_real",
            remaining_units=usable_budget,
            conversations=int(usable_budget / max(estimated_cost, 0.0001)),
            available=True,
            detail="OPENAI_MONTHLY_BUDGET_USD minus organization costs",
        )

    def _fetch_openai_current_month_cost(self, admin_key: str) -> float | None:
        endpoint = "https://api.openai.com/v1/organization/costs"
        try:
            response = self._session.get(
                endpoint,
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Accept": "application/json",
                },
                params={
                    "start_time": current_month_start_unix(),
                    "limit": 31,
                },
                timeout=6,
            )
        except Exception:
            LOGGER.info("OpenAI costs lookup failed", exc_info=True)
            return None

        if response.status_code >= 400:
            LOGGER.info(
                "OpenAI costs lookup returned status=%s",
                response.status_code,
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            LOGGER.info("OpenAI costs lookup returned non-JSON")
            return None
        return sum_openai_costs(payload)

    def _elevenlabs_budget(
        self,
        settings: dict[str, Any],
        conservative_factor: float,
    ) -> ProviderBudget | None:
        if not self._tts_expected(settings):
            return None

        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key or self._session is None:
            return None

        subscription = self._cached_provider_call(
            "elevenlabs_subscription",
            lambda: self._fetch_elevenlabs_subscription(api_key),
        )
        if not isinstance(subscription, dict):
            return None

        character_count = _float_or_none(subscription.get("character_count"))
        character_limit = _float_or_none(subscription.get("character_limit"))
        if character_count is None or character_limit is None or character_limit <= 0:
            return None

        remaining_chars = max(0.0, character_limit - character_count)
        usable_chars = remaining_chars * conservative_factor
        chars_per_conversation = estimate_tts_chars_per_conversation(settings)
        conversations = int(usable_chars / max(chars_per_conversation, 1.0))
        return ProviderBudget(
            name="elevenlabs_chars",
            source="elevenlabs_real",
            remaining_units=usable_chars,
            conversations=max(0, conversations),
            available=True,
            detail="subscription character_limit-character_count",
        )

    def _fetch_elevenlabs_subscription(self, api_key: str) -> dict[str, Any] | None:
        base_url = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
        endpoint = f"{base_url}/v1/user/subscription"
        try:
            response = self._session.get(
                endpoint,
                headers={
                    "xi-api-key": api_key,
                    "Accept": "application/json",
                },
                timeout=4,
            )
        except Exception:
            LOGGER.info("ElevenLabs subscription lookup failed", exc_info=True)
            return None

        if response.status_code >= 400:
            LOGGER.info(
                "ElevenLabs subscription lookup returned status=%s",
                response.status_code,
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            LOGGER.info("ElevenLabs subscription lookup returned non-JSON")
            return None
        return payload if isinstance(payload, dict) else None

    def _cached_provider_call(self, key: str, factory: Any) -> Any:
        now = float(self._now())
        cached = self._provider_cache.get(key)
        if cached is not None:
            timestamp, value = cached
            if now - timestamp < PROVIDER_CACHE_TTL_SECONDS:
                return value

        value = factory()
        self._provider_cache[key] = (now, value)
        return value

    def _load_env_once(self) -> None:
        if self._env_loaded:
            return
        if load_dotenv is not None and self.root is not None:
            runtime_paths = get_runtime_paths(self.root)
            external_env = runtime_paths.external_config_root / ".env"
            if external_env.exists():
                load_dotenv(external_env, override=False)
            elif (self.root / ".env").exists():
                load_dotenv(self.root / ".env", override=False)
        self._env_loaded = True

    def _missing_source_label(self) -> str:
        sources: list[str] = []
        if os.getenv("OPENAI_ADMIN_API_KEY", "").strip():
            if openai_monthly_budget_from_env() > 0:
                sources.append("openai_admin_costs_unavailable")
            else:
                sources.append("openai_admin_costs_no_budget")
        if os.getenv("OPENAI_API_KEY", "").strip():
            sources.append("openai_key_present_no_billing")
        if os.getenv("ELEVENLABS_API_KEY", "").strip():
            sources.append("elevenlabs_unavailable")
        if os.getenv("DEEPGRAM_API_KEY", "").strip():
            sources.append("deepgram_estimated")
        if not sources:
            return "local_estimate_missing_budget"
        return "+".join(sources)

    def _tts_expected(self, settings: dict[str, Any]) -> bool:
        test_mode = settings.get("test_mode", {})
        if isinstance(test_mode, dict) and bool(test_mode.get("enabled", False)):
            return False
        return True


def usage_profile(settings: dict[str, Any]) -> str:
    openai_model = (
        normalize_openai_model_id(
            settings.get("openai_model", ""),
            default="gpt-5.4-mini",
        )
        or "gpt-5.4-mini"
    )
    response_length = str(settings.get("response_length", "")).strip()
    elevenlabs_model = str(settings.get("elevenlabs_model", "")).strip()
    model_profile = openai_model_profile(openai_model)
    if model_profile == "quality" or response_length == "detailed" or elevenlabs_model == "eleven_v3":
        return "high_quality"
    if (
        model_profile in {"fast", "economy"}
        and response_length in {"very_short", "short"}
        and interaction_mode(settings) == "text"
    ):
        return "low_usage"
    return "balanced"


def estimate_tts_chars_per_conversation(settings: dict[str, Any]) -> float:
    response_length = str(settings.get("response_length", "balanced")).strip()
    words = RESPONSE_LENGTH_MAX_WORDS.get(
        response_length,
        RESPONSE_LENGTH_MAX_WORDS["balanced"],
    )
    adaptive_multiplier, _ = usage_adaptation_multiplier(settings)
    words = max(1.0, words * adaptive_multiplier)
    chars_per_word = float_from_env(
        "USAGE_ESTIMATE_TTS_CHARS_PER_WORD",
        DEFAULT_USAGE_TTS_CHARS_PER_WORD,
    )
    overhead = float_from_env(
        "USAGE_ESTIMATE_TTS_CHARS_OVERHEAD",
        DEFAULT_USAGE_TTS_CHARS_OVERHEAD,
    )
    return max(1.0, (words * max(1.0, chars_per_word)) + max(0.0, overhead))


def estimated_complete_interaction_cost(
    settings: dict[str, Any],
    profile: str,
    *,
    adaptive_multiplier: float | None = None,
) -> float:
    clean_profile = profile if profile in USAGE_PROFILES else "balanced"
    base_cost = float_from_env(
        f"USAGE_ESTIMATE_COST_{clean_profile.upper()}_USD",
        DEFAULT_USAGE_COST_BY_PROFILE_USD[clean_profile],
    )
    clean_multiplier = adaptive_multiplier
    if clean_multiplier is None:
        clean_multiplier, _ = usage_adaptation_multiplier(settings)
    clean_multiplier = max(
        DEFAULT_USAGE_ADAPTATION_MIN_MULTIPLIER,
        min(DEFAULT_USAGE_ADAPTATION_MAX_MULTIPLIER, float(clean_multiplier)),
    )
    base_cost *= clean_multiplier

    if interaction_mode(settings) == "text":
        return max(0.0001, base_cost)

    stt_cost = 0.0
    if str(settings.get("stt_engine", "deepgram")).strip() == "deepgram":
        deepgram_settings = settings.get("deepgram", {})
        deepgram_enabled = (
            bool(deepgram_settings.get("enabled", True))
            if isinstance(deepgram_settings, dict)
            else True
        )
        if deepgram_enabled:
            stt_cost = float_from_env(
                "USAGE_ESTIMATE_STT_COST_USD",
                DEFAULT_USAGE_STT_COST_USD,
            )

    emotion_cost = 0.0
    if bool(settings.get("listening_emotion_analysis", True)):
        emotion_cost = float_from_env(
            "USAGE_ESTIMATE_LISTENING_EMOTION_COST_USD",
            DEFAULT_USAGE_LISTENING_EMOTION_COST_USD,
        )

    return max(0.0001, base_cost + stt_cost + emotion_cost)


def usage_budget_remaining_from_env() -> float:
    return max(
        0.0,
        float_from_env(
            "USAGE_BUDGET_USD",
            float_from_env("USAGE_CREDITS_REMAINING", 0.0),
        ),
    )


def openai_monthly_budget_from_env() -> float:
    return max(
        0.0,
        float_from_env(
            "OPENAI_MONTHLY_BUDGET_USD",
            float_from_env("OPENAI_BUDGET_USD", 0.0),
        ),
    )


def miralys_tokens_remaining_from_env() -> float:
    return max(
        0.0,
        float_from_env(
            "MIRALYS_TOKENS_REMAINING",
            float_from_env("MIRALYS_TOKEN_BALANCE", 0.0),
        ),
    )


def test_miralys_tokens_remaining(settings: dict[str, Any]) -> float:
    test_mode = settings.get("test_mode", {})
    if not isinstance(test_mode, dict):
        return 0.0
    return max(0.0, _float_or_none(test_mode.get("miralys_tokens_remaining")) or 0.0)


def test_miralys_tokens_purchased(settings: dict[str, Any]) -> int:
    test_mode = settings.get("test_mode", {})
    if not isinstance(test_mode, dict):
        return 0
    purchased = _float_or_none(test_mode.get("miralys_tokens_purchased"))
    if purchased is not None and purchased > 0:
        return max(0, int(purchased))
    remaining = _float_or_none(test_mode.get("miralys_tokens_remaining"))
    if remaining is not None and remaining > 0:
        return max(0, int(remaining))
    return 0


def test_miralys_tokens_used(settings: dict[str, Any]) -> int:
    test_mode = settings.get("test_mode", {})
    if not isinstance(test_mode, dict):
        return 0
    used = _float_or_none(test_mode.get("miralys_tokens_used"))
    if used is None:
        return 0
    return max(0, int(used))


def record_test_miralys_token_usage(
    *,
    settings_manager: Any,
    settings: dict[str, Any],
    coins_used: int,
) -> None:
    if settings_manager is None:
        return

    test_mode = settings.get("test_mode", {})
    if not isinstance(test_mode, dict) or not bool(test_mode.get("enabled", False)):
        return

    purchased = test_miralys_tokens_purchased(settings)
    if purchased <= 0:
        return

    current_used = test_miralys_tokens_used(settings)
    new_used = min(purchased, max(0, current_used) + max(0, int(coins_used)))
    new_test_mode = dict(test_mode)
    new_test_mode["miralys_tokens_purchased"] = int(purchased)
    new_test_mode["miralys_tokens_used"] = int(new_used)
    new_test_mode["miralys_tokens_remaining"] = max(0, int(purchased) - int(new_used))
    try:
        settings_manager.set_setting("test_mode", new_test_mode)
    except Exception:
        LOGGER.exception("Could not persist test mode token usage")


def usage_adaptation_multiplier(settings: dict[str, Any]) -> tuple[float, int]:
    adaptation = settings.get("usage_adaptation", {})
    if not isinstance(adaptation, dict):
        return 1.0, 0
    if not bool(adaptation.get("enabled", True)):
        return 1.0, 0

    profile = _usage_adaptation_profile(adaptation, interaction_mode(settings))
    if profile is None:
        return 1.0, 0

    multiplier = _float_or_none(profile.get("ema_cost_multiplier"))
    samples = int(_float_or_none(profile.get("samples")) or 0)
    if multiplier is None:
        return 1.0, samples

    return (
        max(
            DEFAULT_USAGE_ADAPTATION_MIN_MULTIPLIER,
            min(DEFAULT_USAGE_ADAPTATION_MAX_MULTIPLIER, float(multiplier)),
        ),
        max(0, samples),
    )


def record_usage_adaptation(
    *,
    settings_manager: Any,
    settings: dict[str, Any],
    user_text: str,
    assistant_text: str,
) -> None:
    if settings_manager is None:
        return

    adaptation = settings.get("usage_adaptation", {})
    if not isinstance(adaptation, dict) or not bool(adaptation.get("enabled", True)):
        return

    mode = interaction_mode(settings)
    profile = _usage_adaptation_profile(adaptation, mode)
    if profile is None:
        profile = _default_usage_adaptation_profile(mode)

    alpha = _bounded_float(
        adaptation.get("alpha", DEFAULT_USAGE_ADAPTATION_ALPHA),
        DEFAULT_USAGE_ADAPTATION_ALPHA,
        minimum=0.05,
        maximum=0.50,
    )
    user_words = _count_words(user_text)
    assistant_words = _count_words(assistant_text)
    turn_words = max(1, user_words + assistant_words)
    expected_turn_words = _expected_turn_words_for_settings(settings)
    sample_multiplier = turn_words / max(1.0, expected_turn_words)
    sample_multiplier = max(
        DEFAULT_USAGE_ADAPTATION_MIN_MULTIPLIER,
        min(DEFAULT_USAGE_ADAPTATION_MAX_MULTIPLIER, sample_multiplier),
    )

    updated = dict(profile)
    updated["samples"] = int(_float_or_none(profile.get("samples")) or 0) + 1
    updated["ema_user_words"] = _ema(
        user_words,
        _float_or_none(profile.get("ema_user_words")) or float(user_words),
        alpha,
    )
    updated["ema_assistant_words"] = _ema(
        assistant_words,
        _float_or_none(profile.get("ema_assistant_words")) or float(assistant_words),
        alpha,
    )
    updated["ema_turn_words"] = _ema(
        turn_words,
        _float_or_none(profile.get("ema_turn_words")) or float(turn_words),
        alpha,
    )
    updated["ema_cost_multiplier"] = _ema(
        sample_multiplier,
        _float_or_none(profile.get("ema_cost_multiplier")) or 1.0,
        alpha,
    )
    updated["last_update_utc"] = datetime.now(timezone.utc).isoformat()

    profiles = adaptation.get("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
    profiles[mode] = updated
    new_adaptation = dict(adaptation)
    new_adaptation["profiles"] = profiles
    try:
        settings_manager.set_setting("usage_adaptation", new_adaptation)
    except Exception:
        LOGGER.exception("Could not persist usage adaptation state")


def _usage_adaptation_profile(
    adaptation: dict[str, Any],
    mode: str,
) -> dict[str, Any] | None:
    profiles = adaptation.get("profiles", {})
    if not isinstance(profiles, dict):
        return None
    profile = profiles.get(mode)
    return profile if isinstance(profile, dict) else None


def _default_usage_adaptation_profile(mode: str) -> dict[str, Any]:
    if mode == "voice":
        return {
            "samples": 0,
            "ema_user_words": 10.0,
            "ema_assistant_words": 32.0,
            "ema_turn_words": 42.0,
            "ema_cost_multiplier": 1.0,
            "last_update_utc": None,
        }
    return {
        "samples": 0,
        "ema_user_words": 8.0,
        "ema_assistant_words": 28.0,
        "ema_turn_words": 36.0,
        "ema_cost_multiplier": 1.0,
        "last_update_utc": None,
    }


def _expected_turn_words_for_settings(settings: dict[str, Any]) -> float:
    response_length = str(settings.get("response_length", "balanced")).strip()
    max_words = RESPONSE_LENGTH_MAX_WORDS.get(
        response_length,
        RESPONSE_LENGTH_MAX_WORDS["balanced"],
    )
    base_user_words = 10.0 if interaction_mode(settings) == "voice" else 8.0
    expected_assistant_words = max(12.0, max_words * 0.5)
    return base_user_words + expected_assistant_words


def _count_words(text: str) -> int:
    return len([word for word in str(text).split() if word.strip()])


def _ema(value: float, previous: float, alpha: float) -> float:
    clean_alpha = max(0.0, min(1.0, float(alpha)))
    clean_previous = max(0.0, float(previous))
    clean_value = max(0.0, float(value))
    return (clean_alpha * clean_value) + ((1.0 - clean_alpha) * clean_previous)


def _bounded_float(
    value: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        parsed = float(default)
    return max(minimum, min(maximum, parsed))


def conservative_factor_from_env() -> float:
    return max(
        0.0,
        min(
            1.0,
            float_from_env(
                "USAGE_ESTIMATE_CONSERVATIVE_FACTOR",
                DEFAULT_USAGE_CONSERVATIVE_FACTOR,
            ),
        ),
    )


def average_conversations_per_hour_from_env() -> float:
    return max(
        1.0,
        float_from_env(
            "USAGE_AVERAGE_CONVERSATIONS_PER_HOUR",
            DEFAULT_USAGE_AVERAGE_CONVERSATIONS_PER_HOUR,
        ),
    )


def format_usage_hours(hours: float) -> str:
    return f"{max(0.0, hours):.2f}".rstrip("0").rstrip(".")


def format_usage_minutes(minutes: float) -> str:
    return str(int(max(0.0, minutes) + 0.5))


def interaction_mode(settings: dict[str, Any]) -> str:
    app_settings = settings.get("app", {})
    value = settings.get(
        "interaction_mode",
        app_settings.get("interaction_mode", "voice")
        if isinstance(app_settings, dict)
        else "voice",
    )
    clean = str(value).strip()
    return clean if clean in {"voice", "text"} else "voice"


def float_from_env(env_key: str, default: float) -> float:
    raw_value = os.getenv(env_key, default)
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return default


def current_month_start_unix() -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def sum_openai_costs(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None

    total = 0.0
    for bucket in payload.get("data", []):
        if not isinstance(bucket, dict):
            continue
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            amount = result.get("amount", {})
            if not isinstance(amount, dict):
                continue
            value = _float_or_none(amount.get("value"))
            if value is not None:
                total += value
    return total


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
