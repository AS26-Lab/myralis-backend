from __future__ import annotations

import os
import logging
import sys
import time

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.audio_manager import AudioManager
from core.authorization import BackendAuthorizationContext
from core.backend_identity import OFFICIAL_BACKEND_PROJECT_NAME
from core.conversation_manager import ConversationManager
from core.elevenlabs_manager import ElevenLabsManager
from core.health_server import start_health_server, stop_health_server
from core.license_manager import LicenseManager, LicenseValidationResult
from core.openai_manager import OpenAIManager
from core.runtime_bridge import RuntimeBridge, RuntimeConfig, configure_runtime_bridge
from core.runtime_paths import get_runtime_paths
from core.settings_manager import SettingsManager
from core.test_mode_manager import TestModeManager
from core.stt_manager import VoiceSTTManager
from core.websocket_server import is_websocket_server_active
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


def _license_enforcement_enabled() -> bool:
    """Temporary license gate switch.

    This is a conservative bridge for the current development stage. A later
    launcher will collect email and license_key explicitly. In production, this
    gate should be enabled.
    """

    return os.getenv("MYRALIS_LICENSE_ENFORCEMENT", "0").strip() == "1"


def build_window(settings_manager: SettingsManager | None = None) -> MainWindow:
    settings_manager = settings_manager or SettingsManager()
    configure_logging(settings_manager)

    runtime_bridge = RuntimeBridge(RuntimeConfig.from_root(settings_manager.root))
    configure_runtime_bridge(runtime_bridge)
    audio_manager = AudioManager(settings_manager)
    openai_manager = OpenAIManager(settings_manager.root)
    elevenlabs_manager = ElevenLabsManager(
        settings_manager.root,
        settings_manager.audio_output_dir,
    )
    voice_stt_manager = VoiceSTTManager(settings_manager.root)
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
        deepgram_stt_manager=voice_stt_manager,
    )


def main() -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )

    settings_manager = SettingsManager()
    license_manager = LicenseManager(settings_manager.root)
    license_validation_result: LicenseValidationResult = (
        license_manager.validate_saved_license()
    )
    enforcement_enabled = _license_enforcement_enabled()

    if license_validation_result.can_launch:
        logging.info(
            "License validated: plan=%s role=%s reason=%s credits=%.2f key=%s",
            license_validation_result.plan_name,
            license_validation_result.role,
            license_validation_result.reason,
            license_validation_result.credits_balance,
            BackendAuthorizationContext.from_license_result(
                license_validation_result
            ).license_key_masked,
        )
    else:
        logging.warning(
            "Saved license validation failed: reason=%s enforcement=%s",
            license_validation_result.reason,
            "enabled" if enforcement_enabled else "disabled",
        )
        if enforcement_enabled:
            logging.warning(
                "MYRALIS_LICENSE_ENFORCEMENT=1 and no valid saved license was found. "
                "Exiting before creating the app runtime."
            )
            return 0

    app = QApplication(sys.argv)
    app.setApplicationName(OFFICIAL_BACKEND_PROJECT_NAME)
    app.setOrganizationName("LocalAssistant")
    runtime_paths = get_runtime_paths(settings_manager.root)
    if runtime_paths.icon_path.exists():
        app.setWindowIcon(QIcon(str(runtime_paths.icon_path)))
    else:
        logging.warning(
            "Icono no encontrado; se generará la build con el icono predeterminado. "
            "Coloca el archivo en %s",
            runtime_paths.icon_path,
        )

    authorization_context = BackendAuthorizationContext.from_license_result(
        license_validation_result
    )

    # Temporary gate result retained here so later stages can pass it into a
    # launcher/UI flow without re-validating immediately.
    app.license_validation_result = license_validation_result  # type: ignore[attr-defined]
    app.backend_authorization_context = authorization_context  # type: ignore[attr-defined]

    window = build_window(settings_manager)
    if runtime_paths.icon_path.exists():
        window.setWindowIcon(QIcon(str(runtime_paths.icon_path)))
    # Launcher health must not reuse the WebSocket port. 8765 remains the
    # Unreal WebSocket server, so the launcher should point to 8766/health.
    _wait_for_websocket_ready(timeout_seconds=10.0)
    app.aboutToQuit.connect(window.shutdown_backend)
    app.aboutToQuit.connect(stop_health_server)
    start_health_server(
        is_websocket_server_active,
        lambda: authorization_context.authorized_session,
    )
    window.hide()
    return app.exec()


def _wait_for_websocket_ready(*, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if is_websocket_server_active():
            return True
        time.sleep(0.05)
    logging.warning("WebSocket no estuvo listo dentro de %.1f segundos", timeout_seconds)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
