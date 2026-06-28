from __future__ import annotations

from typing import Any


CURRENT_LANGUAGE_SETTING_ID = "current_language"
DEFAULT_CURRENT_LANGUAGE = "spanish"
SUPPORTED_CURRENT_LANGUAGE_IDS = (
    "spanish",
    "english",
    "french",
    "portuguese",
)
CURRENT_LANGUAGE_TO_UI_CODE = {
    "spanish": "es",
    "english": "en",
    "french": "fr",
    "portuguese": "pt",
}
UI_CODE_TO_CURRENT_LANGUAGE = {
    "es": "spanish",
    "en": "english",
    "fr": "french",
    "pt": "portuguese",
}


def normalize_current_language(value: Any) -> str:
    clean = str(value or "").strip().casefold().replace("-", "_")
    if clean in SUPPORTED_CURRENT_LANGUAGE_IDS:
        return clean
    if clean in UI_CODE_TO_CURRENT_LANGUAGE:
        return UI_CODE_TO_CURRENT_LANGUAGE[clean]
    return DEFAULT_CURRENT_LANGUAGE


def require_current_language(value: Any) -> str:
    clean = str(value or "").strip().casefold().replace("-", "_")
    if clean not in SUPPORTED_CURRENT_LANGUAGE_IDS:
        raise ValueError(f"Invalid current_language: {value!r}")
    return clean


def current_language_to_ui_code(value: Any) -> str:
    return CURRENT_LANGUAGE_TO_UI_CODE[normalize_current_language(value)]
