from __future__ import annotations

from typing import Final


DEFAULT_DEBUG_STATE: Final[dict[str, bool]] = {
    "global_debug": False,
    "realtime_audio_debug": False,
    "verbose_logging": False,
    "technical_panel_visible": False,
    "websocket_debug": False,
    "audio_debug": False,
    "stt_debug": False,
    "tts_debug": False,
}

