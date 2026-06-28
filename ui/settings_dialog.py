from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QKeySequenceEdit,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.audio_manager import AudioManager, AudioManagerError, AudioDevice
from core.language import DEFAULT_CURRENT_LANGUAGE, normalize_current_language
from core.settings_manager import SettingsManager
from ui.translations import tr
from ui.window_chrome import apply_native_dark_title_bar


OPENAI_MODEL_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "GPT-5.4 mini",
        "label": "Balanceado - GPT-5.4 mini",
        "tooltip": (
            "Sugerido para este asistente: baja latencia con buena calidad "
            "conversacional y respuestas naturales."
        ),
    },
    {
        "id": "GPT-5.4 nano",
        "label": "Rapido - GPT-5.4 nano",
        "tooltip": (
            "Prioriza velocidad y bajo costo. Ideal si la respuesta debe salir "
            "lo antes posible, con menor profundidad."
        ),
    },
    {
        "id": "GPT-5",
        "label": "Calidad - GPT-5",
        "tooltip": (
            "Mayor capacidad para respuestas complejas, normalmente con mas latencia "
            "y costo que las opciones mini/nano."
        ),
    },
    {
        "id": "GPT-5 mini",
        "label": "Economico - GPT-5 mini",
        "tooltip": (
            "Modelo rapido y economico para tareas bien definidas. Buena opcion "
            "si quieres bajar costo sin ir al modo mas minimo."
        ),
    },
)


ELEVENLABS_MODEL_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "eleven_turbo_v2_5",
        "label": "Turbo v2.5 ($) - SUGERIDO calidad/velocidad",
        "tooltip": (
            "Sugerido para este asistente: buena calidad y emocion con menor latencia "
            "que Multilingual v2."
        ),
    },
    {
        "id": "eleven_flash_v2_5",
        "label": "Flash v2.5 ($) - maxima rapidez",
        "tooltip": (
            "Prioriza rapidez y bajo costo. Ideal si la latencia importa mas "
            "que la expresividad fina."
        ),
    },
    {
        "id": "eleven_multilingual_v2",
        "label": "Multilingual v2 ($$) - calidad/emocion",
        "tooltip": (
            "Mayor estabilidad y calidad emocional, especialmente en respuestas "
            "largas, pero con mas latencia."
        ),
    },
    {
        "id": "eleven_v3",
        "label": "Eleven v3 ($$$) - maxima emocion",
        "tooltip": (
            "Modelo mas expresivo y emocional, pero no es la mejor opcion para "
            "conversacion en tiempo real por mayor latencia."
        ),
    },
)


class SettingsDialog(QDialog):
    settings_changed = Signal()

    def __init__(
        self,
        *,
        settings_manager: SettingsManager,
        audio_manager: AudioManager,
        websocket_start_end_callback=None,
        elevenlabs_streaming_callback=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.audio_manager = audio_manager
        self.websocket_start_end_callback = websocket_start_end_callback
        self.elevenlabs_streaming_callback = elevenlabs_streaming_callback
        self.current_language = normalize_current_language(
            settings_manager.get_setting("current_language", DEFAULT_CURRENT_LANGUAGE)
        )
        self._loading = False
        self.setWindowTitle(self._t("configuration"))
        self.setMinimumSize(640, 620)
        self._build_ui()
        self._apply_style()
        apply_native_dark_title_bar(self)
        self._load_values()

    def _t(self, key: str) -> str:
        return tr(self.current_language, key)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        technical_content = QWidget()
        technical_layout = QVBoxLayout(technical_content)
        technical_layout.setContentsMargins(0, 0, 0, 0)
        technical_layout.setSpacing(14)

        app_group = QGroupBox(self._t("application"))
        app_form = QFormLayout(app_group)
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.keySequenceChanged.connect(self._save_settings)
        self.test_mode_check = QCheckBox(self._t("test_mode"))
        self.test_mode_check.toggled.connect(self._save_settings)
        self.test_mode_audio_check = QCheckBox(self._t("test_mode_audio"))
        self.test_mode_audio_check.setToolTip(
            self._t("test_mode_audio_tooltip")
        )
        self.test_mode_audio_check.toggled.connect(self._save_settings)
        test_mode_widget = QWidget()
        test_mode_row = QHBoxLayout(test_mode_widget)
        test_mode_row.setContentsMargins(0, 0, 0, 0)
        test_mode_row.setSpacing(16)
        test_mode_row.addWidget(self.test_mode_check)
        test_mode_row.addWidget(self.test_mode_audio_check)
        test_mode_row.addStretch(1)
        app_form.addRow(self._t("hotkey"), self.hotkey_edit)
        app_form.addRow("", test_mode_widget)

        openai_group = QGroupBox(self._t("openai"))
        openai_form = QFormLayout(openai_group)
        self.openai_model_combo = QComboBox()
        self._populate_openai_model_options()
        self.openai_model_combo.currentIndexChanged.connect(
            self._handle_openai_model_changed
        )
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.valueChanged.connect(self._save_settings)
        self.max_response_words_spin = QSpinBox()
        self.max_response_words_spin.setRange(20, 250)
        self.max_response_words_spin.setSingleStep(5)
        self.max_response_words_spin.setSuffix(self._t("words_suffix"))
        self.max_response_words_spin.setToolTip(
            self._t("max_response_words_tooltip")
        )
        self.max_response_words_spin.valueChanged.connect(self._save_settings)
        self.history_limit_spin = QSpinBox()
        self.history_limit_spin.setRange(2, 30)
        self.history_limit_spin.setSingleStep(1)
        self.history_limit_spin.setSuffix(self._t("messages_suffix"))
        self.history_limit_spin.setToolTip(
            self._t("history_limit_tooltip")
        )
        self.history_limit_spin.valueChanged.connect(self._save_settings)
        openai_form.addRow(self._t("model"), self.openai_model_combo)
        openai_form.addRow(self._t("temperature"), self.temperature_spin)
        openai_form.addRow(self._t("response_length"), self.max_response_words_spin)
        openai_form.addRow(self._t("history_sent"), self.history_limit_spin)

        eleven_group = QGroupBox("ElevenLabs")
        eleven_form = QFormLayout(eleven_group)
        self.voice_id_edit = QLineEdit()
        self.voice_id_edit.textChanged.connect(self._save_settings)
        self.voice_model_combo = QComboBox()
        self._populate_elevenlabs_model_options()
        self.voice_model_combo.currentIndexChanged.connect(
            self._handle_elevenlabs_model_changed
        )
        self.realtime_tts_check = QCheckBox(self._t("realtime_tts"))
        self.realtime_tts_check.setToolTip(
            self._t("realtime_tts_tooltip")
        )
        self.realtime_tts_check.toggled.connect(self._save_settings)
        self.save_response_wav_check = QCheckBox(self._t("save_response_wav"))
        self.save_response_wav_check.setToolTip(
            self._t("save_response_wav_tooltip")
        )
        self.save_response_wav_check.toggled.connect(self._save_settings)
        eleven_form.addRow(self._t("voice_id"), self.voice_id_edit)
        eleven_form.addRow(self._t("voice_model"), self.voice_model_combo)
        eleven_form.addRow("", self.realtime_tts_check)
        eleven_form.addRow("", self.save_response_wav_check)

        deepgram_group = QGroupBox(self._t("deepgram_stt"))
        deepgram_form = QFormLayout(deepgram_group)
        self.deepgram_enabled_check = QCheckBox(self._t("enable_deepgram"))
        self.deepgram_enabled_check.toggled.connect(self._save_settings)
        self.deepgram_api_key_edit = QLineEdit()
        self.deepgram_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.deepgram_api_key_edit.setPlaceholderText("DEEPGRAM_API_KEY")
        self.deepgram_api_key_edit.textChanged.connect(self._save_settings)
        self.deepgram_language_edit = QLineEdit()
        self.deepgram_language_edit.textChanged.connect(self._save_settings)
        self.deepgram_model_edit = QLineEdit()
        self.deepgram_model_edit.textChanged.connect(self._save_settings)
        self.deepgram_interim_check = QCheckBox("Interim results")
        self.deepgram_interim_check.toggled.connect(self._save_settings)
        self.deepgram_endpointing_check = QCheckBox("Endpointing")
        self.deepgram_endpointing_check.toggled.connect(self._save_settings)
        deepgram_form.addRow("", self.deepgram_enabled_check)
        deepgram_form.addRow(self._t("api_key"), self.deepgram_api_key_edit)
        deepgram_form.addRow(self._t("language"), self.deepgram_language_edit)
        deepgram_form.addRow(self._t("model"), self.deepgram_model_edit)
        deepgram_form.addRow("", self.deepgram_interim_check)
        deepgram_form.addRow("", self.deepgram_endpointing_check)

        devices_group = QGroupBox(self._t("devices"))
        devices_form = QFormLayout(devices_group)
        self.input_device_combo = QComboBox()
        self.output_device_combo = QComboBox()
        self.input_device_combo.currentIndexChanged.connect(self._save_devices)
        self.output_device_combo.currentIndexChanged.connect(self._save_devices)

        refresh_row = QHBoxLayout()
        self.refresh_devices_button = QPushButton(self._t("refresh"))
        self.refresh_devices_button.clicked.connect(self._populate_devices)
        self.devices_status_label = QLabel("")
        refresh_row.addWidget(self.refresh_devices_button)
        refresh_row.addWidget(self.devices_status_label, 1)

        devices_form.addRow(self._t("input"), self.input_device_combo)
        devices_form.addRow(self._t("output"), self.output_device_combo)
        devices_form.addRow("", refresh_row)

        diagnostics_group = QGroupBox(self._t("diagnostics"))
        diagnostics_layout = QHBoxLayout(diagnostics_group)
        diagnostics_layout.setContentsMargins(12, 18, 12, 12)
        diagnostics_layout.setSpacing(10)
        self.websocket_test_button = QPushButton(self._t("test_start_end"))
        self.websocket_test_button.clicked.connect(self._run_websocket_start_end_test)
        self.websocket_stream_test_button = QPushButton(self._t("test_elevenlabs"))
        self.websocket_stream_test_button.clicked.connect(
            self._run_elevenlabs_streaming_test
        )
        diagnostics_layout.addWidget(self.websocket_test_button)
        diagnostics_layout.addWidget(self.websocket_stream_test_button)
        diagnostics_layout.addStretch(1)

        self.saved_label = QLabel(self._t("saved"))
        self.saved_label.setObjectName("savedLabel")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText(self._t("close"))
        buttons.rejected.connect(self.reject)

        technical_layout.addWidget(app_group)
        technical_layout.addWidget(openai_group)
        technical_layout.addWidget(eleven_group)
        technical_layout.addWidget(deepgram_group)
        technical_layout.addWidget(devices_group)
        technical_layout.addWidget(diagnostics_group)
        technical_layout.addStretch(1)

        root.addWidget(self._build_scroll_area(technical_content), 1)
        root.addWidget(self.saved_label)
        root.addWidget(buttons)

    def _build_scroll_area(self, widget: QWidget) -> QScrollArea:
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_area.setWidget(widget)
        return scroll_area

    def _load_values(self) -> None:
        self._loading = True
        settings = self.settings_manager.get_settings()
        self.hotkey_edit.setKeySequence(QKeySequence(str(settings["app"]["hotkey"])))
        test_mode_settings = settings.get("test_mode", {})
        self.test_mode_check.setChecked(bool(test_mode_settings.get("enabled", False)))
        self.test_mode_audio_check.setChecked(
            bool(test_mode_settings.get("audio_enabled", False))
        )
        openai_settings = settings.get("openai", {})
        self._select_openai_model(str(openai_settings.get("model", "GPT-5.4 mini")))
        self.temperature_spin.setValue(float(openai_settings.get("temperature", 0.4)))
        self.max_response_words_spin.setValue(
            int(openai_settings.get("max_response_words", 60))
        )
        self.history_limit_spin.setValue(int(openai_settings.get("history_limit", 10)))
        self.voice_id_edit.setText(str(settings["elevenlabs"]["voice_id"]))
        self._select_elevenlabs_model(str(settings["elevenlabs"]["model_id"]))
        elevenlabs_settings = settings.get("elevenlabs", {})
        self.realtime_tts_check.setChecked(
            bool(elevenlabs_settings.get("use_realtime_tts_streaming", True))
        )
        self.save_response_wav_check.setChecked(
            bool(elevenlabs_settings.get("save_response_wav", True))
        )
        deepgram_settings = settings.get("deepgram", {})
        if not isinstance(deepgram_settings, dict):
            deepgram_settings = {}
        self.deepgram_enabled_check.setChecked(
            bool(deepgram_settings.get("enabled", False))
        )
        self.deepgram_api_key_edit.setText(str(deepgram_settings.get("api_key", "")))
        self.deepgram_language_edit.setText(
            str(deepgram_settings.get("language", "es"))
        )
        self.deepgram_model_edit.setText(str(deepgram_settings.get("model", "nova-3")))
        self.deepgram_interim_check.setChecked(
            bool(deepgram_settings.get("interim_results", True))
        )
        self.deepgram_endpointing_check.setChecked(
            bool(deepgram_settings.get("endpointing", True))
        )
        self._populate_devices()
        self._loading = False

    def _populate_devices(self) -> None:
        was_loading = self._loading
        self._loading = True
        devices = self.settings_manager.get_devices()
        self.input_device_combo.clear()
        self.output_device_combo.clear()
        self.input_device_combo.addItem(self._t("system_default"), None)
        self.output_device_combo.addItem(self._t("system_default"), None)

        try:
            input_devices = self.audio_manager.list_input_devices()
            output_devices = self.audio_manager.list_output_devices()
            self._add_devices(self.input_device_combo, input_devices)
            self._add_devices(self.output_device_combo, output_devices)
            self.devices_status_label.setText(self._t("devices_detected"))
        except AudioManagerError as exc:
            self.devices_status_label.setText(str(exc))

        self._select_combo_value(
            self.input_device_combo, devices.get("input_device_index")
        )
        self._select_combo_value(
            self.output_device_combo, devices.get("output_device_index")
        )
        self._loading = was_loading

    def _add_devices(self, combo: QComboBox, devices: list[AudioDevice]) -> None:
        for device in devices:
            combo.addItem(device.label, device.index)

    def _populate_openai_model_options(self) -> None:
        self.openai_model_combo.clear()
        for option in OPENAI_MODEL_OPTIONS:
            self.openai_model_combo.addItem(option["label"], option["id"])
            index = self.openai_model_combo.count() - 1
            self.openai_model_combo.setItemData(
                index,
                option["tooltip"],
                Qt.ItemDataRole.ToolTipRole,
            )

    def _populate_elevenlabs_model_options(self) -> None:
        self.voice_model_combo.clear()
        for option in ELEVENLABS_MODEL_OPTIONS:
            self.voice_model_combo.addItem(option["label"], option["id"])
            index = self.voice_model_combo.count() - 1
            self.voice_model_combo.setItemData(
                index,
                option["tooltip"],
                Qt.ItemDataRole.ToolTipRole,
            )

    def _select_openai_model(self, model_id: str) -> None:
        for index in range(self.openai_model_combo.count()):
            if self.openai_model_combo.itemData(index) == model_id:
                self.openai_model_combo.setCurrentIndex(index)
                self._update_openai_model_tooltip()
                return

        label = f"Personalizado ({model_id})"
        self.openai_model_combo.addItem(label, model_id)
        index = self.openai_model_combo.count() - 1
        self.openai_model_combo.setItemData(
            index,
            "Modelo personalizado. Verifica latencia, costo y compatibilidad antes de usarlo.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.openai_model_combo.setCurrentIndex(index)
        self._update_openai_model_tooltip()

    def _select_elevenlabs_model(self, model_id: str) -> None:
        for index in range(self.voice_model_combo.count()):
            if self.voice_model_combo.itemData(index) == model_id:
                self.voice_model_combo.setCurrentIndex(index)
                self._update_elevenlabs_model_tooltip()
                return

        label = f"Personalizado ({model_id})"
        self.voice_model_combo.addItem(label, model_id)
        index = self.voice_model_combo.count() - 1
        self.voice_model_combo.setItemData(
            index,
            "Modelo personalizado. Verifica latencia, costo y compatibilidad antes de usarlo.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.voice_model_combo.setCurrentIndex(index)
        self._update_elevenlabs_model_tooltip()

    def _select_combo_value(self, combo: QComboBox, value: int | None) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(0)

    def _save_settings(self, *args: object) -> None:
        if self._loading:
            return

        hotkey = self.hotkey_edit.keySequence().toString() or "F8"
        openai_model = str(self.openai_model_combo.currentData() or "GPT-5.4 mini")
        response_words = int(self.max_response_words_spin.value())
        history_limit = int(self.history_limit_spin.value())
        elevenlabs_model = str(
            self.voice_model_combo.currentData() or "eleven_turbo_v2_5"
        )
        realtime_tts = self.realtime_tts_check.isChecked()
        deepgram_enabled = self.deepgram_enabled_check.isChecked()
        self.settings_manager.update_settings(
            {
                "talk_hotkey": hotkey,
                "openai_model": openai_model,
                "response_length": self._response_length_id(response_words),
                "history_level": self._history_level_id(history_limit),
                "elevenlabs_model": elevenlabs_model,
                "tts_realtime": realtime_tts,
                "stt_engine": "deepgram" if deepgram_enabled else "local",
                "app": {"hotkey": hotkey},
                "openai": {
                    "model": openai_model,
                    "temperature": float(self.temperature_spin.value()),
                    "max_response_words": response_words,
                    "history_limit": history_limit,
                    "reasoning_effort": "low",
                },
                "elevenlabs": {
                    "voice_id": self.voice_id_edit.text().strip(),
                    "model_id": elevenlabs_model,
                    "output_format": "pcm_24000",
                    "use_realtime_tts_streaming": realtime_tts,
                    "save_response_wav": self.save_response_wav_check.isChecked(),
                },
                "deepgram": {
                    "enabled": deepgram_enabled,
                    "api_key": self.deepgram_api_key_edit.text().strip(),
                    "language": self.deepgram_language_edit.text().strip() or "es",
                    "model": self.deepgram_model_edit.text().strip() or "nova-3",
                    "sample_rate": 16000,
                    "interim_results": self.deepgram_interim_check.isChecked(),
                    "endpointing": self.deepgram_endpointing_check.isChecked(),
                    "utterance_end_ms": 1000,
                    "vad_events": True,
                    "smart_format": True,
                    "punctuate": True,
                    "audio_block_ms": 50,
                },
                "test_mode": {
                    "enabled": self.test_mode_check.isChecked(),
                    "audio_enabled": self.test_mode_audio_check.isChecked(),
                },
            }
        )
        self.saved_label.setText(self._t("saved"))
        self.settings_changed.emit()

    def _handle_openai_model_changed(self, *args: object) -> None:
        self._update_openai_model_tooltip()
        self._save_settings()

    def _handle_elevenlabs_model_changed(self, *args: object) -> None:
        self._update_elevenlabs_model_tooltip()
        self._save_settings()

    def _update_openai_model_tooltip(self) -> None:
        tooltip = self.openai_model_combo.itemData(
            self.openai_model_combo.currentIndex(),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.openai_model_combo.setToolTip(str(tooltip or ""))

    def _update_elevenlabs_model_tooltip(self) -> None:
        tooltip = self.voice_model_combo.itemData(
            self.voice_model_combo.currentIndex(),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.voice_model_combo.setToolTip(str(tooltip or ""))

    def _response_length_id(self, words: int) -> str:
        options = {
            "very_short": 45,
            "short": 80,
            "balanced": 140,
            "detailed": 220,
        }
        return min(options, key=lambda key: abs(options[key] - int(words)))

    def _history_level_id(self, turns: int) -> str:
        options = {
            "minimal": 4,
            "light": 8,
            "normal": 16,
            "extended": 24,
        }
        return min(options, key=lambda key: abs(options[key] - int(turns)))

    def _save_devices(self, *args: object) -> None:
        if self._loading:
            return

        input_index = self.input_device_combo.currentData()
        output_index = self.output_device_combo.currentData()
        self.audio_manager.save_selected_devices(
            input_device_index=input_index,
            input_device_name=self._device_name(self.input_device_combo),
            output_device_index=output_index,
            output_device_name=self._device_name(self.output_device_combo),
        )
        self.saved_label.setText(self._t("saved"))
        self.settings_changed.emit()

    def _device_name(self, combo: QComboBox) -> str:
        if combo.currentData() is None:
            return ""
        return combo.currentText()

    def _run_websocket_start_end_test(self) -> None:
        if self.websocket_start_end_callback is not None:
            self.websocket_start_end_callback()

    def _run_elevenlabs_streaming_test(self) -> None:
        if self.elevenlabs_streaming_callback is not None:
            self.elevenlabs_streaming_callback()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        apply_native_dark_title_bar(self)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background: #050505;
                color: #f4f1ea;
            }
            QGroupBox {
                background: #0c0c0c;
                border: 1px solid #2b2618;
                border-radius: 8px;
                margin-top: 12px;
                padding: 14px 12px 12px 12px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: #c9a24d;
            }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QKeySequenceEdit {
                min-height: 30px;
                border: 1px solid #2f2f2f;
                border-radius: 6px;
                padding: 4px 8px;
                background: #080808;
                color: #f4f1ea;
                selection-background-color: #c9a24d;
                selection-color: #000000;
            }
            QCheckBox, QLabel {
                color: #f4f1ea;
            }
            QPushButton {
                background: #0b0b0b;
                color: #f4f1ea;
                border: 1px solid #5f4b1f;
                border-radius: 6px;
                padding: 7px 12px;
            }
            QPushButton:hover {
                background: #15120a;
                border-color: #c9a24d;
            }
            QLabel#savedLabel {
                color: #c9a24d;
            }
            """
        )
