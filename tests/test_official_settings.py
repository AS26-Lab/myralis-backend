import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.conversation_manager import ConversationManager
from core.settings_manager import SettingsManager


class OfficialSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.settings_manager = SettingsManager(Path(self.temp_dir))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir)

    def test_boolean_values_are_normalized(self) -> None:
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
        for raw_value, expected in cases:
            with self.subTest(raw_value=raw_value):
                normalized = self.settings_manager.apply_official_setting_update(
                    "tts_realtime",
                    raw_value,
                )
                self.assertIs(normalized, expected)
                settings = self.settings_manager.get_settings()
                self.assertIs(settings["tts_realtime"], expected)
                self.assertIs(
                    settings["elevenlabs"]["use_realtime_tts_streaming"],
                    expected,
                )

    def test_volume_values_are_clamped(self) -> None:
        self.assertEqual(
            self.settings_manager.apply_official_setting_update("ui_volume", 1.4),
            1.0,
        )
        self.assertEqual(
            self.settings_manager.apply_official_setting_update("input_volume", -0.25),
            0.0,
        )

    def test_official_option_values_are_persisted(self) -> None:
        updates = {
            "interaction_mode": "text",
            "neutral_return_time": 25,
            "stt_engine": "local",
            "openai_model": "GPT-5.4 nano",
            "response_length": "detailed",
            "history_level": "extended",
            "elevenlabs_model": "eleven_v3",
            "voice_speed": "very_fast",
            "input_device": "device_2",
            "output_device": "default",
            "current_language": "english",
            "display_mode": "fullscreen",
            "fps_limit": "unlimited",
            "performance_profile": "ultra",
        }
        for setting_id, value in updates.items():
            self.settings_manager.apply_official_setting_update(setting_id, value)

        settings = self.settings_manager.get_settings()
        for setting_id, value in updates.items():
            self.assertEqual(settings[setting_id], value)
        self.assertEqual(settings["app"]["neutral_return_time"], 25)
        self.assertEqual(settings["openai"]["max_response_words"], 220)
        self.assertEqual(settings["openai"]["history_limit"], 24)
        self.assertEqual(settings["elevenlabs"]["model_id"], "eleven_v3")

    def test_current_language_accepts_only_unreal_value_ids(self) -> None:
        normalized = self.settings_manager.apply_official_setting_update(
            "current_language",
            "portuguese",
        )

        self.assertEqual(normalized, "portuguese")
        self.assertEqual(
            self.settings_manager.get_settings()["current_language"],
            "portuguese",
        )
        with self.assertRaises(ValueError):
            self.settings_manager.apply_official_setting_update(
                "current_language",
                "pt",
            )

    def test_passive_unreal_graphics_settings_are_general_settings(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager.audio_manager = SimpleNamespace(settings_manager=self.settings_manager)

        updates = {
            "display_mode": "windowed",
            "fps_limit": "30",
            "performance_profile": "quality",
        }

        for setting_id, value in updates.items():
            with self.assertLogs("core.conversation_manager", level="INFO") as logs:
                handled = manager.handle_unreal_websocket_message(
                    {
                        "type": "settings_update",
                        "setting": setting_id,
                        "value": value,
                    }
                )
            self.assertTrue(handled)
            self.assertIn(
                "Passive Unreal graphics setting stored",
                "\n".join(logs.output),
            )

        settings = self.settings_manager.get_settings()
        for setting_id, value in updates.items():
            self.assertEqual(settings[setting_id], value)
        self.assertNotIn("display_mode", settings["customization"])
        self.assertNotIn("fps_limit", settings["customization"])
        self.assertNotIn("performance_profile", settings["customization"])

    def test_passive_unreal_graphics_settings_validate_values(self) -> None:
        invalid_updates = {
            "display_mode": "exclusive_fullscreen",
            "fps_limit": 120,
            "performance_profile": "cinematic",
        }

        for setting_id, value in invalid_updates.items():
            with self.subTest(setting_id=setting_id):
                with self.assertRaises(ValueError):
                    self.settings_manager.apply_official_setting_update(
                        setting_id,
                        value,
                    )

    def test_visible_device_labels_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.settings_manager.apply_official_setting_update(
                "output_device",
                "0 - Speakers",
            )

    def test_settings_action_reset_settings_defaults_preserves_customization(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager.audio_manager = SimpleNamespace(settings_manager=self.settings_manager)

        self.settings_manager.apply_official_setting_update("tts_realtime", False)
        self.settings_manager.apply_official_setting_update("response_length", "detailed")
        self.settings_manager.apply_official_setting_update("neutral_return_time", 15)
        self.settings_manager.apply_official_setting_update("current_language", "english")
        self.settings_manager.apply_official_setting_update("display_mode", "fullscreen")
        self.settings_manager.apply_official_setting_update("fps_limit", "unlimited")
        self.settings_manager.apply_official_setting_update("performance_profile", "ultra")
        self.settings_manager.apply_customization_setting_update(
            "selected_character",
            "Panfila",
        )
        self.settings_manager.apply_customization_setting_update(
            "use_custom_voice",
            True,
        )

        handled = manager.handle_unreal_websocket_message(
            {"type": "settings_action", "action": "reset_settings_defaults"}
        )

        self.assertTrue(handled)
        settings = self.settings_manager.get_settings()
        self.assertTrue(settings["tts_realtime"])
        self.assertEqual(settings["interaction_mode"], "voice")
        self.assertEqual(settings["neutral_return_time"], 45)
        self.assertEqual(settings["avatar_voice_volume"], 0.8)
        self.assertEqual(settings["ui_volume"], 0.5)
        self.assertEqual(settings["input_volume"], 0.8)
        self.assertEqual(settings["input_device"], "default")
        self.assertEqual(settings["output_device"], "default")
        self.assertEqual(settings["stt_engine"], "deepgram")
        self.assertTrue(settings["listening_emotion_analysis"])
        self.assertEqual(settings["openai_model"], "GPT-5.4 mini")
        self.assertEqual(settings["response_length"], "balanced")
        self.assertEqual(settings["history_level"], "normal")
        self.assertEqual(settings["elevenlabs_model"], "eleven_turbo_v2_5")
        self.assertEqual(settings["voice_speed"], "normal")
        self.assertEqual(settings["current_language"], "spanish")
        self.assertEqual(settings["display_mode"], "borderless")
        self.assertEqual(settings["fps_limit"], "60")
        self.assertEqual(settings["performance_profile"], "balanced")
        self.assertEqual(settings["elevenlabs"]["use_realtime_tts_streaming"], True)
        self.assertEqual(settings["customization"]["selected_character"], "Panfila")
        self.assertTrue(settings["customization"]["use_custom_voice"])

    def test_customization_update_and_reset_preserves_general_settings(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager.audio_manager = SimpleNamespace(settings_manager=self.settings_manager)

        self.settings_manager.apply_official_setting_update("interaction_mode", "text")
        self.settings_manager.apply_official_setting_update("response_length", "detailed")
        self.settings_manager.apply_official_setting_update("display_mode", "fullscreen")
        self.settings_manager.apply_official_setting_update("fps_limit", "unlimited")
        self.settings_manager.apply_official_setting_update("performance_profile", "ultra")

        handled = manager.handle_unreal_websocket_message(
            {
                "type": "settings_update",
                "setting": "selected_character",
                "value_type": "string",
                "value": "Panfila",
            }
        )
        self.assertTrue(handled)
        handled = manager.handle_unreal_websocket_message(
            {
                "type": "settings_update",
                "setting": "profanity_filter",
                "value_type": "bool",
                "value": "false",
            }
        )
        self.assertTrue(handled)

        settings = self.settings_manager.get_settings()
        self.assertEqual(settings["customization"]["selected_character"], "Panfila")
        self.assertFalse(settings["customization"]["profanity_filter"])

        handled = manager.handle_unreal_websocket_message(
            {"type": "settings_action", "action": "reset_customization_defaults"}
        )

        self.assertTrue(handled)
        settings = self.settings_manager.get_settings()
        self.assertEqual(settings["interaction_mode"], "text")
        self.assertEqual(settings["response_length"], "detailed")
        self.assertEqual(settings["display_mode"], "fullscreen")
        self.assertEqual(settings["fps_limit"], "unlimited")
        self.assertEqual(settings["performance_profile"], "ultra")
        self.assertEqual(settings["customization"]["personality_traits"], "")
        self.assertTrue(settings["customization"]["profanity_filter"])
        self.assertEqual(
            settings["customization"]["voice_id"],
            settings["elevenlabs"]["voice_id"],
        )
        self.assertFalse(settings["customization"]["use_custom_voice"])
        self.assertEqual(settings["customization"]["custom_voice_id"], "")
        self.assertEqual(settings["customization"]["selected_character"], "")
        self.assertEqual(settings["customization"]["selected_personality"], "")
        self.assertEqual(settings["customization"]["voice_style"], "")
        self.assertEqual(settings["customization"]["character_personality"], "")

    def test_runtime_backend_ui_settings_are_not_persisted(self) -> None:
        result = self.settings_manager.apply_settings_update(
            "usage_profile",
            "low_usage",
        )

        self.assertEqual(result.category, "runtime_backend_ui")
        self.assertFalse(result.persisted)
        self.assertNotIn("usage_profile", self.settings_manager.get_settings())

    def test_legacy_personality_settings_are_not_active(self) -> None:
        self.settings_manager.update_settings(
            {
                "personality": {
                    "name": "Panfila",
                    "age": "26",
                    "gender": "Mujer",
                    "role": "Asistente",
                    "background": "Historia vieja",
                    "traits": {"direct": True},
                }
            }
        )
        self.settings_manager.set_setting("personality.name", "Sofia")

        settings = self.settings_manager.get_settings()

        self.assertNotIn("personality", settings)

    def test_unknown_settings_action_is_not_handled(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager.audio_manager = SimpleNamespace(settings_manager=self.settings_manager)

        with self.assertLogs("core.conversation_manager", level="WARNING"):
            handled = manager.handle_unreal_websocket_message(
                {"type": "settings_action", "action": "reconnect_system"}
            )

        self.assertFalse(handled)

    def test_generic_reset_defaults_action_is_not_handled(self) -> None:
        manager = ConversationManager.__new__(ConversationManager)
        manager._lock = threading.RLock()
        manager.audio_manager = SimpleNamespace(settings_manager=self.settings_manager)

        with self.assertLogs("core.conversation_manager", level="WARNING"):
            handled = manager.handle_unreal_websocket_message(
                {"type": "settings_action", "action": "reset_defaults"}
            )

        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
