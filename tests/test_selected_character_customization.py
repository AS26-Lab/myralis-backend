import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.conversation_manager import ConversationManager
from core.settings_manager import SettingsManager


class _FakeAudioManager:
    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager


class SelectedCharacterCustomizationTests(unittest.TestCase):
    def test_selected_character_update_is_stored_only(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            settings_manager = SettingsManager(Path(temp_dir))
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = _FakeAudioManager(settings_manager)

            with self.assertLogs("core.conversation_manager", level="INFO") as logs, patch(
                "core.conversation_manager.send_json_to_unreal_blocking"
            ) as send_runtime_state:
                handled = manager.handle_unreal_websocket_message(
                    {
                        "type": "settings_update",
                        "setting": "selected_character",
                        "value_type": "string",
                        "value": "char_panfila",
                    }
                )

            self.assertTrue(handled)
            settings = settings_manager.get_settings()
            self.assertEqual(
                settings["customization"]["selected_character"],
                "char_panfila",
            )
            self.assertIn(
                "selected_character stored only: char_panfila",
                "\n".join(logs.output),
            )
            self.assertFalse(send_runtime_state.called)
        finally:
            shutil.rmtree(temp_dir)

    def test_selected_character_does_not_change_final_voice_id(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        base_settings = {
            "elevenlabs": {"voice_id": "legacy-voice"},
            "customization": {
                "voice_id": "selected-voice",
                "use_custom_voice": False,
                "custom_voice_id": "manual-voice",
            },
        }
        character_settings = {
            "elevenlabs": {"voice_id": "legacy-voice"},
            "customization": {
                "voice_id": "selected-voice",
                "use_custom_voice": False,
                "custom_voice_id": "manual-voice",
                "selected_character": "char_panfila",
            },
        }

        base_voice_id = manager._resolve_final_voice_id(base_settings)
        character_voice_id = manager._resolve_final_voice_id(character_settings)

        self.assertEqual(character_voice_id, base_voice_id)

    def test_selected_character_does_not_change_prompt(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)

        base_prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {},
        )
        character_prompt = manager._build_system_prompt_with_mood(
            "Base prompt.",
            "Neutral",
            80,
            {"selected_character": "char_panfila"},
        )

        self.assertEqual(character_prompt, base_prompt)
        self.assertNotIn("char_panfila", character_prompt)

    def test_selected_character_does_not_change_models_or_personality(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            settings_manager = SettingsManager(Path(temp_dir))
            before = settings_manager.get_settings()
            manager = ConversationManager.__new__(ConversationManager)
            manager._lock = threading.RLock()
            manager.audio_manager = _FakeAudioManager(settings_manager)

            handled = manager.handle_unreal_websocket_message(
                {
                    "type": "settings_update",
                    "setting": "selected_character",
                    "value_type": "string",
                    "value": "char_panfila",
                }
            )

            self.assertTrue(handled)
            after = settings_manager.get_settings()
            self.assertNotIn("personality", before)
            self.assertNotIn("personality", after)
            self.assertEqual(after["openai_model"], before["openai_model"])
            self.assertEqual(after["elevenlabs_model"], before["elevenlabs_model"])
            self.assertEqual(after["openai"], before["openai"])
            self.assertEqual(after["elevenlabs"], before["elevenlabs"])
        finally:
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    unittest.main()
