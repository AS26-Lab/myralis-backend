from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any


LOGGER = logging.getLogger(__name__)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
BLACK_COLORREF = 0x000000
SOFT_WHITE_COLORREF = 0xEAF1F4


def apply_native_dark_title_bar(widget: Any) -> bool:
    if sys.platform != "win32":
        return False

    try:
        hwnd = int(widget.winId())
        enabled = ctypes.c_int(1)
        applied = False

        for attribute in (
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
        ):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_int(attribute),
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                applied = True
                break

        _set_dwm_color(hwnd, DWMWA_CAPTION_COLOR, BLACK_COLORREF)
        _set_dwm_color(hwnd, DWMWA_TEXT_COLOR, SOFT_WHITE_COLORREF)
        return applied
    except Exception:
        LOGGER.debug("Could not apply native dark title bar", exc_info=True)
        return False


def _set_dwm_color(hwnd: int, attribute: int, colorref: int) -> None:
    color = ctypes.c_int(colorref)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd),
        ctypes.c_int(attribute),
        ctypes.byref(color),
        ctypes.sizeof(color),
    )
