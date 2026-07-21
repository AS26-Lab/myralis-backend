from __future__ import annotations

import sys
import queue
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.authorization import BackendAuthorizationContext
from core.debug_security import DEFAULT_DEBUG_STATE
from ui.main_window import MainWindow


def _build_window(context: BackendAuthorizationContext) -> MainWindow:
    window = MainWindow.__new__(MainWindow)
    window._authorization_context = context
    window._shutdown_started = False
    window._last_debug_hotkey_toggle_time = 0.0
    window._debug_state = dict(DEFAULT_DEBUG_STATE)
    window._unreal_debug_mode = False
    window._show_mic_level_outbound_events = False
    window._force_send_mic_level = False
    window.debug_hotkey_filter = None
    window.debug_hotkey_shortcut = SimpleNamespace(setEnabled=Mock())
    window.debug_hotkey_poll_timer = SimpleNamespace(stop=Mock())
    window.showNormal = Mock()
    window.hide = Mock()
    window.raise_ = Mock()
    window.activateWindow = Mock()
    window.isVisible = Mock(return_value=False)
    window.isMinimized = Mock(return_value=False)
    window._reset_debug_mode_to_normal = Mock()
    window._refresh_debug_panel = Mock()
    window._refresh_debug_mode_controls = Mock()
    window._refresh_unreal_debug_mode_controls = Mock()
    window._refresh_mic_level_outbound_toggle = Mock()
    window._refresh_mic_level_force_toggle = Mock()
    window._lock_down_debug_state = MainWindow._lock_down_debug_state.__get__(window)
    window._can_open_technical_panel = MainWindow._can_open_technical_panel.__get__(window)
    window.toggle_debug_ui = MainWindow.toggle_debug_ui.__get__(window)
    window.show_debug_ui = MainWindow.show_debug_ui.__get__(window)
    window.hide_debug_ui = MainWindow.hide_debug_ui.__get__(window)
    window.update_authorization_context = MainWindow.update_authorization_context.__get__(window)
    window._handle_debug_hotkey_activated = MainWindow._handle_debug_hotkey_activated.__get__(window)
    return window


def _assert_debug_state_off(window: MainWindow) -> None:
    assert window._debug_state == DEFAULT_DEBUG_STATE
    assert window._unreal_debug_mode is False
    assert window._show_mic_level_outbound_events is False
    assert window._force_send_mic_level is False


def main() -> int:
    unauthenticated = BackendAuthorizationContext()
    client = BackendAuthorizationContext(authenticated=True, role="client")
    beta = BackendAuthorizationContext(authenticated=True, role="beta_tester")
    admin = BackendAuthorizationContext(authenticated=True, role="admin", admin_authorized=True)

    window = _build_window(unauthenticated)
    assert window._can_open_technical_panel() is False
    window.show_debug_ui()
    window.showNormal.assert_not_called()
    _assert_debug_state_off(window)
    print("case1: blocked")

    window = _build_window(client)
    assert window._can_open_technical_panel() is False
    window.toggle_debug_ui()
    window.showNormal.assert_not_called()
    _assert_debug_state_off(window)
    print("case2: client blocked")

    window = _build_window(beta)
    assert window._can_open_technical_panel() is False
    window.show_debug_ui()
    window.hide.assert_not_called()
    _assert_debug_state_off(window)
    print("case3: beta blocked")

    window = _build_window(admin)
    assert window._can_open_technical_panel() is True
    window._handle_debug_hotkey_activated()
    window.showNormal.assert_called_once()
    _assert_debug_state_off(window)
    print("case4: admin allowed and debug remains off")

    window.isVisible = Mock(return_value=True)
    window._lock_down_debug_state = Mock(wraps=MainWindow._lock_down_debug_state.__get__(window))
    revoked = BackendAuthorizationContext(authenticated=False, role="client")
    window.update_authorization_context(revoked)
    window._lock_down_debug_state.assert_called()
    window.hide.assert_called()
    _assert_debug_state_off(window)
    print("case5: revoke closes panel and resets debug")

    window = _build_window(client)
    window._debug_state["global_debug"] = True
    window._lock_down_debug_state()
    _assert_debug_state_off(window)
    print("case6: local true ignored")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
