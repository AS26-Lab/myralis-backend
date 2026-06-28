import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.conversation_manager import ConversationManager
from core.settings_manager import SettingsManager


CLEAN_INSTRUCTION = (
    "Do not use profanity, vulgar language, insults, or swear words in responses."
)
CONTEXTUAL_PROFANITY_INSTRUCTION = (
    "Profanity and swear words may be used when appropriate to the conversation and context."
)


class ProfanityFilterCustomizationTests(unittest.TestCase):
    def test_profanity_filter_values_are_normalized(self) -> None:
        cases = (
            (True, True),
            (False, False),
            ("true", True),
            ("false", False),
            ("True", True),
            ("False", False),
            (1, True),
            (0, False),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_manager = SettingsManager(Path(temp_dir))
            for raw_value, expected in cases:
                with self.subTest(raw_value=raw_value):
                    result = settings_manager.apply_settings_update(
                        "profanity_filter",
                        raw_value,
                    )
                    settings = settings_manager.get_settings()

                    self.assertEqual(result.category, "customization")
                    self.assertIs(result.value, expected)
                    self.assertIs(
                        settings["customization"]["profanity_filter"],
                        expected,
                    )

    def test_settings_update_logs_profanity_filter_value(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            settings_manager = SettingsManager(Path(temp_dir))
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = SimpleNamespace(settings_manager=settings_manager)

            with self.assertLogs("core.conversation_manager", level="INFO") as logs:
                handled = manager.handle_unreal_websocket_message(
                    {
                        "type": "settings_update",
                        "setting": "profanity_filter",
                        "value_type": "bool",
                        "value": "false",
                    }
                )

            self.assertTrue(handled)
            self.assertFalse(
                settings_manager.get_settings()["customization"]["profanity_filter"]
            )
            self.assertIn("Profanity filter updated: false", "\n".join(logs.output))
        finally:
            shutil.rmtree(temp_dir)

    def test_prompt_with_profanity_filter_true_adds_clean_restriction(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {"profanity_filter": True},
        )

        self.assertIn("Base prompt.", prompt)
        self.assertIn("Customization / Profanity Filter:", prompt)
        self.assertIn(CLEAN_INSTRUCTION, prompt)
        self.assertNotIn(CONTEXTUAL_PROFANITY_INSTRUCTION, prompt)
        self.assertIn("Estado emocional actual", prompt)

    def test_prompt_with_profanity_filter_false_adds_contextual_permission(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {"profanity_filter": False},
        )

        self.assertIn("Base prompt.", prompt)
        self.assertIn("Customization / Profanity Filter:", prompt)
        self.assertIn(CONTEXTUAL_PROFANITY_INSTRUCTION, prompt)
        self.assertNotIn(CLEAN_INSTRUCTION, prompt)
        self.assertIn("Estado emocional actual", prompt)

    def test_profanity_filter_true_coexists_with_direct_angry_sarcastic_traits(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {
                "personality_traits": "sarcastica,enojona,directa",
                "profanity_filter": True,
            },
        )

        self.assertIn("Customization / Personality:", prompt)
        self.assertIn("sarcasmo moderado", prompt)
        self.assertIn("mas reactiva", prompt)
        self.assertIn("reduce rodeos", prompt)
        self.assertIn("Customization / Profanity Filter:", prompt)
        self.assertIn(CLEAN_INSTRUCTION, prompt)


if __name__ == "__main__":
    unittest.main()
