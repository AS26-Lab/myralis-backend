from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from core.audio_manager import AudioManager
from core.conversation_manager import ConversationManager
from core.deepgram_stt_manager import DeepgramSTTManager
from core.elevenlabs_manager import ElevenLabsManager
from core.openai_manager import OpenAIManager
from core.runtime_bridge import (
    RuntimeBridge,
    RuntimeConfig,
    configure_runtime_bridge,
)
from core.settings_manager import SettingsManager
from core.test_mode_manager import TestModeManager
from ui.main_window import MainWindow


def configure_logging(settings_manager: SettingsManager) -> None:
    log_path = settings_manager.logs_output_dir / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def build_window() -> MainWindow:
    settings_manager = SettingsManager()
    configure_logging(settings_manager)

    runtime_bridge = RuntimeBridge(RuntimeConfig.from_root(settings_manager.root))
    configure_runtime_bridge(runtime_bridge)
    audio_manager = AudioManager(settings_manager)
    openai_manager = OpenAIManager(settings_manager.root)
    elevenlabs_manager = ElevenLabsManager(
        settings_manager.root,
        settings_manager.audio_output_dir,
    )
    deepgram_stt_manager = DeepgramSTTManager(settings_manager.root)
    test_mode_manager = TestModeManager(settings_manager)
    conversation_manager = ConversationManager(
        openai_manager=openai_manager,
        elevenlabs_manager=elevenlabs_manager,
        audio_manager=audio_manager,
        test_mode_manager=test_mode_manager,
        runtime_bridge=runtime_bridge,
    )

    return MainWindow(
        settings_manager=settings_manager,
        audio_manager=audio_manager,
        conversation_manager=conversation_manager,
        runtime_bridge=runtime_bridge,
        deepgram_stt_manager=deepgram_stt_manager,
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("PYTHON_AI_ASSISTANT")
    app.setOrganizationName("LocalAssistant")

    window = build_window()
    app.aboutToQuit.connect(window.shutdown_backend)
    window.hide()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
