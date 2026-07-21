from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.usage_manager import UsageManager  # noqa: E402


def _print_ok(label: str) -> None:
    print(f"{label}: OK")


def main() -> int:
    manager = UsageManager()
    manager.usage_events_path.unlink(missing_ok=True)

    print("Running usage manager tests...")

    credits = manager.estimate_credits_spent(
        openai_tokens=1000,
        elevenlabs_chars=1000,
        deepgram_seconds=60,
    )
    assert credits == 1.75
    _print_ok("1. estimate_credits_spent")

    event = manager.record_usage_event(
        question_text="¿Cuál es la pieza más importante del museo?",
        answer_text="La pieza más importante es...",
        openai_tokens=1500,
        elevenlabs_chars=1200,
        deepgram_seconds=30,
        model_used="test-model",
        language="es",
        metadata={"test": True},
        email="beta@myralis.ai",
        license_key="BETA-MYRALIS-001",
    )
    assert event.question_length > 0
    assert event.answer_length > 0
    assert event.credits_spent > 0
    assert event.model_used == "test-model"
    _print_ok("2. record_usage_event")

    recent_events = manager.get_recent_usage_events()
    assert len(recent_events) >= 1
    _print_ok("3. get_recent_usage_events")

    analytics = manager.get_basic_analytics()
    assert analytics["total_events"] >= 1
    assert analytics["total_credits_spent"] > 0
    assert analytics["average_question_length"] > 0
    assert analytics["average_answer_length"] > 0
    _print_ok("4. get_basic_analytics")

    print("All usage manager tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
