import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.conversation_manager import ConversationManager
from core.personality import (
    DEFAULT_PERSONALITY_PROMPT,
    build_customization_personality_prompt,
    parse_personality_traits,
)
from core.settings_manager import SettingsManager


class PersonalityTraitsCustomizationTests(unittest.TestCase):
    def test_empty_personality_traits_produces_empty_list(self) -> None:
        with self.assertLogs("core.personality", level="INFO") as logs:
            traits = parse_personality_traits("")

        self.assertEqual(traits, [])
        self.assertIn("Personality traits parsed: []", "\n".join(logs.output))

    def test_valid_and_invalid_traits_are_parsed_without_crashing(self) -> None:
        with self.assertLogs("core.personality", level="INFO") as logs:
            traits = parse_personality_traits(
                " alegre, empatica, hacker, , graciosa, cariñosa "
            )

        output = "\n".join(logs.output)
        self.assertEqual(traits, ["alegre", "empatica", "graciosa", "cariñosa"])
        self.assertIn("Invalid personality trait ignored: hacker", output)
        self.assertIn(
            "Personality traits parsed: ['alegre', 'empatica', 'graciosa', 'cariñosa']",
            output,
        )

    def test_prompt_includes_valid_trait_instructions_and_safety_limits(self) -> None:
        with self.assertLogs("core.personality", level="INFO") as logs:
            prompt = build_customization_personality_prompt(
                {
                    "personality_traits": (
                        "alegre, directa, graciosa, sarcastica, coqueta, enojona, invalida"
                    )
                }
            )

        self.assertIn("Customization / Personality:", prompt)
        self.assertIn("tono positivo", prompt)
        self.assertIn("reduce rodeos", prompt)
        self.assertIn("humor ligero", prompt)
        self.assertIn("sarcasmo moderado", prompt)
        self.assertIn("sin sexualizar", prompt)
        self.assertIn("sin insultar", prompt)
        self.assertIn("no reemplazan el prompt base", prompt)
        self.assertNotIn("invalida:", prompt)
        self.assertIn(
            "Invalid personality trait ignored: invalida",
            "\n".join(logs.output),
        )

    def test_settings_update_saves_personality_traits_as_customization(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            settings_manager = SettingsManager(Path(temp_dir))
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = SimpleNamespace(settings_manager=settings_manager)

            with self.assertLogs(level="INFO") as logs:
                handled = manager.handle_unreal_websocket_message(
                    {
                        "type": "settings_update",
                        "setting": "personality_traits",
                        "value_type": "text",
                        "value": "alegre,empatica,graciosa,cariñosa",
                    }
                )

            self.assertTrue(handled)
            settings = settings_manager.get_settings()
            self.assertEqual(
                settings["customization"]["personality_traits"],
                "alegre,empatica,graciosa,cariñosa",
            )
            output = "\n".join(logs.output)
            self.assertIn(
                "Customization setting updated: personality_traits=alegre,empatica,graciosa,cariñosa",
                output,
            )
            self.assertIn(
                "Personality traits parsed: ['alegre', 'empatica', 'graciosa', 'cariñosa']",
                output,
            )
        finally:
            shutil.rmtree(temp_dir)

    def test_settings_update_saves_custom_personality_prompt_fields(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            settings_manager = SettingsManager(Path(temp_dir))
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = SimpleNamespace(settings_manager=settings_manager)

            handled_toggle = manager.handle_unreal_websocket_message(
                {
                    "type": "settings_update",
                    "setting": "use_custom_personality_prompt",
                    "value_type": "bool",
                    "value": "true",
                }
            )
            handled_prompt = manager.handle_unreal_websocket_message(
                {
                    "type": "settings_update",
                    "setting": "custom_personality_prompt",
                    "value_type": "text",
                    "value": "Eres Myralis con tono tecnico y cercano.",
                }
            )

            self.assertTrue(handled_toggle)
            self.assertTrue(handled_prompt)
            settings = settings_manager.get_settings()
            self.assertTrue(
                settings["customization"]["use_custom_personality_prompt"]
            )
            self.assertEqual(
                settings["customization"]["custom_personality_prompt"],
                "Eres Myralis con tono tecnico y cercano.",
            )
        finally:
            shutil.rmtree(temp_dir)

    def test_conversation_prompt_includes_customization_traits_section(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {"personality_traits": "seria,directa,analitica"},
        )

        self.assertIn("Base prompt.", prompt)
        self.assertIn("Customization / Personality:", prompt)
        self.assertIn(DEFAULT_PERSONALITY_PROMPT, prompt)
        self.assertIn("profesional", prompt)
        self.assertIn("reduce rodeos", prompt)
        self.assertIn("Razona con orden", prompt)
        self.assertIn("Estado emocional actual", prompt)

    def test_custom_personality_prompt_is_mixed_with_traits(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {
                "use_custom_personality_prompt": True,
                "custom_personality_prompt": "Eres Myralis con energia calmada.",
                "personality_traits": "directa,analitica",
            },
        )

        self.assertIn("Base prompt.", prompt)
        self.assertIn("Eres Myralis con energia calmada.", prompt)
        self.assertNotIn(DEFAULT_PERSONALITY_PROMPT, prompt)
        self.assertIn("reduce rodeos", prompt)
        self.assertIn("Razona con orden", prompt)

    def test_default_personality_prompt_is_mixed_with_base_prompt(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        prompt = manager._build_system_prompt_with_mood(
            "Eres Panfila, mujer de 26 anios. Responde solo JSON.",
            "Neutral",
            80,
            {},
        )

        self.assertIn("Eres Panfila", prompt)
        self.assertIn(DEFAULT_PERSONALITY_PROMPT, prompt)
        self.assertNotIn("Nombre:", prompt)
        self.assertNotIn("Edad:", prompt)
        self.assertNotIn("Genero:", prompt)
        self.assertNotIn("Rol:", prompt)
        self.assertNotIn("Base del personaje:", prompt)


if __name__ == "__main__":
    unittest.main()
