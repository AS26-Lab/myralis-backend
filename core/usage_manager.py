from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.runtime_paths import get_runtime_paths


@dataclass(frozen=True)
class UsageEvent:
    """Represents a local/mock usage event for beta and development.

    This is temporary storage only. The intention is to capture usage locally
    while the licensing and billing flow is being built. In a later stage this
    data should be migrated to Supabase `usage_events`.

    Do not use this file as a secure billing source in production.
    """

    event_type: str
    email: str | None
    license_key: str | None
    question_text: str | None
    answer_text: str | None
    question_length: int
    answer_length: int
    openai_tokens: int
    elevenlabs_chars: int
    deepgram_seconds: float
    credits_spent: float
    model_used: str | None
    language: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class UsageManager:
    """Local usage recorder for beta/dev analytics.

    This manager writes one JSON object per line to a local JSONL file under
    `config/usage_events.jsonl`. It is intentionally lightweight and local-only
    so usage accounting can be tested before the Supabase-backed pipeline is
    introduced.

    The stored data is convenience telemetry, not a secure billing authority.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[1]
        self.config_dir = get_runtime_paths(self.root).config_root
        self.usage_events_path = self.config_dir / "usage_events.jsonl"

    def estimate_credits_spent(
        self,
        openai_tokens: int = 0,
        elevenlabs_chars: int = 0,
        deepgram_seconds: float = 0.0,
    ) -> float:
        """Estimate credits using a simple mock formula.

        Formula:
        - openai_tokens / 1000 * 1.0
        - elevenlabs_chars / 1000 * 0.5
        - deepgram_seconds / 60 * 0.25
        """

        credits = (
            (max(0, int(openai_tokens)) / 1000.0 * 1.0)
            + (max(0, int(elevenlabs_chars)) / 1000.0 * 0.5)
            + (max(0.0, float(deepgram_seconds)) / 60.0 * 0.25)
        )
        return round(float(credits), 4)

    def record_usage_event(
        self,
        *,
        question_text: str | None,
        answer_text: str | None,
        openai_tokens: int = 0,
        elevenlabs_chars: int = 0,
        deepgram_seconds: float = 0.0,
        model_used: str | None,
        language: str | None,
        metadata: dict[str, Any] | None,
        email: str | None,
        license_key: str | None,
        event_type: str = "conversation",
    ) -> UsageEvent:
        """Create and persist a local usage event.

        This is a beta/dev convenience layer. The resulting JSONL file is not
        a secure source of truth for production billing and will eventually be
        replaced with a Supabase-backed usage pipeline.
        """

        clean_question = str(question_text or "")
        clean_answer = str(answer_text or "")
        credits_spent = self.estimate_credits_spent(
            openai_tokens=openai_tokens,
            elevenlabs_chars=elevenlabs_chars,
            deepgram_seconds=deepgram_seconds,
        )
        event = UsageEvent(
            event_type=str(event_type or "").strip() or "conversation",
            email=str(email or "").strip() or None,
            license_key=str(license_key or "").strip() or None,
            question_text=clean_question or None,
            answer_text=clean_answer or None,
            question_length=len(clean_question),
            answer_length=len(clean_answer),
            openai_tokens=max(0, int(openai_tokens)),
            elevenlabs_chars=max(0, int(elevenlabs_chars)),
            deepgram_seconds=max(0.0, float(deepgram_seconds)),
            credits_spent=credits_spent,
            model_used=str(model_used or "").strip() or None,
            language=str(language or "").strip() or None,
            metadata=dict(metadata or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self.config_dir.mkdir(parents=True, exist_ok=True)
        with self.usage_events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._event_to_payload(event), ensure_ascii=False))
            handle.write("\n")

        return event

    def get_recent_usage_events(self, limit: int = 20) -> list[UsageEvent]:
        """Read recent usage events from the local JSONL file.

        Corrupt lines are ignored so development data does not break the app.
        """

        clean_limit = max(0, int(limit))
        if clean_limit == 0 or not self.usage_events_path.exists():
            return []

        events: list[UsageEvent] = []
        try:
            lines = self.usage_events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = self._payload_to_event(payload)
            if event is not None:
                events.append(event)

        if clean_limit >= len(events):
            return events
        return events[-clean_limit:]

    def get_basic_analytics(self) -> dict[str, Any]:
        """Compute simple local analytics from recent usage events.

        This is intentionally minimal and local-only. It is suitable for beta
        and dev inspection, but not for authoritative billing.
        """

        events = self.get_recent_usage_events(limit=10_000)
        total_events = len(events)
        total_credits_spent = round(sum(event.credits_spent for event in events), 4)
        average_question_length = (
            round(sum(event.question_length for event in events) / total_events, 2)
            if total_events
            else 0.0
        )
        average_answer_length = (
            round(sum(event.answer_length for event in events) / total_events, 2)
            if total_events
            else 0.0
        )
        model_counts = Counter(
            event.model_used for event in events if event.model_used
        )
        top_model_used = model_counts.most_common(1)[0][0] if model_counts else None

        return {
            "total_events": total_events,
            "total_credits_spent": total_credits_spent,
            "average_question_length": average_question_length,
            "average_answer_length": average_answer_length,
            "top_model_used": top_model_used,
        }

    def _event_to_payload(self, event: UsageEvent) -> dict[str, Any]:
        return {
            "event_type": event.event_type,
            "email": event.email,
            "license_key": event.license_key,
            "question_text": event.question_text,
            "answer_text": event.answer_text,
            "question_length": event.question_length,
            "answer_length": event.answer_length,
            "openai_tokens": event.openai_tokens,
            "elevenlabs_chars": event.elevenlabs_chars,
            "deepgram_seconds": event.deepgram_seconds,
            "credits_spent": event.credits_spent,
            "model_used": event.model_used,
            "language": event.language,
            "metadata": dict(event.metadata),
            "created_at": event.created_at,
        }

    def _payload_to_event(self, payload: Any) -> UsageEvent | None:
        if not isinstance(payload, dict):
            return None

        try:
            return UsageEvent(
                event_type=str(payload.get("event_type", "conversation")),
                email=self._as_optional_str(payload.get("email")),
                license_key=self._as_optional_str(payload.get("license_key")),
                question_text=self._as_optional_str(payload.get("question_text")),
                answer_text=self._as_optional_str(payload.get("answer_text")),
                question_length=max(0, int(payload.get("question_length", 0))),
                answer_length=max(0, int(payload.get("answer_length", 0))),
                openai_tokens=max(0, int(payload.get("openai_tokens", 0))),
                elevenlabs_chars=max(0, int(payload.get("elevenlabs_chars", 0))),
                deepgram_seconds=max(
                    0.0,
                    float(payload.get("deepgram_seconds", 0.0)),
                ),
                credits_spent=round(float(payload.get("credits_spent", 0.0)), 4),
                model_used=self._as_optional_str(payload.get("model_used")),
                language=self._as_optional_str(payload.get("language")),
                metadata=payload.get("metadata", {})
                if isinstance(payload.get("metadata", {}), dict)
                else {},
                created_at=str(payload.get("created_at", "")),
            )
        except (TypeError, ValueError):
            return None

    def _as_optional_str(self, value: Any) -> str | None:
        clean = str(value or "").strip()
        return clean or None
