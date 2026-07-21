from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.language import DEFAULT_CURRENT_LANGUAGE, normalize_current_language
from ui.translations import tr


class MessageBubble(QFrame):
    def __init__(self, role: str, text: str, title_text: str | None = None) -> None:
        super().__init__()
        self.setObjectName(f"{role}Bubble")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        title = QLabel(title_text or role.upper())
        title.setObjectName("bubbleTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        body = QLabel(text)
        body.setObjectName("bubbleBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout.addWidget(title)
        layout.addWidget(body)


class ChatPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.current_language = DEFAULT_CURRENT_LANGUAGE
        self._build_ui()
        self._apply_style()
        self.set_language(self.current_language)

    def add_user_message(self, text: str) -> None:
        self._add_message("user", text, align_right=True)

    def add_assistant_message(self, text: str) -> None:
        self._add_message("assistant", text, align_right=False)

    def add_system_message(self, text: str) -> None:
        self._add_message("system", text, align_right=False)

    def set_input_enabled(self, enabled: bool) -> None:
        _ = enabled

    def set_language(self, language: str) -> None:
        self.current_language = normalize_current_language(language)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setObjectName("conversationScroll")

        self.messages_widget = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_widget)
        self.messages_layout.setContentsMargins(18, 18, 18, 18)
        self.messages_layout.setSpacing(10)
        self.messages_layout.addStretch(1)
        self.scroll_area.setWidget(self.messages_widget)

        root_layout.addWidget(self.scroll_area, 1)

    def _add_message(self, role: str, text: str, align_right: bool) -> None:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)

        bubble = MessageBubble(role, text, self._translated_role(role))
        bubble.setMaximumWidth(760)

        if align_right:
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        else:
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)

        self.messages_layout.insertWidget(self.messages_layout.count() - 1, row)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _translated_role(self, role: str) -> str:
        return self._t(f"role_{role}")

    def _t(self, key: str) -> str:
        return tr(self.current_language, key)

    def _scroll_to_bottom(self) -> None:
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QScrollArea#conversationScroll {
                background: #050505;
                border: 1px solid #2b2618;
                border-radius: 8px;
            }
            QScrollArea#conversationScroll QWidget {
                background: #050505;
            }
            QFrame#userBubble {
                background: #0f0e0b;
                border: 1px solid #5f4b1f;
                border-radius: 8px;
            }
            QFrame#assistantBubble {
                background: #0c0c0c;
                border: 1px solid #2b2618;
                border-radius: 8px;
            }
            QFrame#systemBubble {
                background: #171309;
                border: 1px solid #c9a24d;
                border-radius: 8px;
            }
            QLabel#bubbleTitle {
                color: #c9a24d;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#bubbleBody {
                color: #f4f1ea;
                font-size: 14px;
                line-height: 1.35;
            }
            """
        )
