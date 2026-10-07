"""DoorGuardian — настольная панель управления контроллером.

Связь по COM-порту (9600 бод) тем же текстовым протоколом, что и в мобильном
приложении из каталога mobile/:

    панель -> устройство   CMD:ARM | CMD:DISARM | CMD:STATUS
                           TEST:LED | TEST:BUZZER | TEST:GSM
                           SMS:<номер>:<текст>
                           SET:PHONE=+79991234567,T1=15
    устройство -> панель   STAT:DOOR=1,STATE=2,BAT=3.95
                           LOG: текст
"""

import csv
import datetime as dt
import sys
from pathlib import Path

import serial
import serial.tools.list_ports
from PyQt6.QtCore import QRegularExpression, QSettings, Qt, QTimer
from PyQt6.QtGui import QIcon, QRegularExpressionValidator, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

BAUD = 9600
POLL_MS = 100                # как часто забираем данные из порта
BAT_EMPTY, BAT_FULL = 3.0, 4.2
LOG_LIMIT = 300              # строк в журнале событий
CONSOLE_LIMIT = 200          # строк в мониторе порта

TEXT = "#CDD6F4"
MUTED = "#A6ADC8"
ACCENT = "#89B4FA"
OK_FG, OK_BG = "#A6E3A1", "#1C2B26"
ALERT_FG, ALERT_BG = "#F38BA8", "#3A232E"
WARN = "#F9E2AF"

DOOR_COLORS = {
    None: ("#181825", MUTED),      # данных от контроллера ещё не было
    False: (OK_BG, OK_FG),
    True: (ALERT_BG, ALERT_FG),
}
DOOR_PLATE = "border-radius:10px; padding:18px; font-size:16px; font-weight:700;"


def resource_path(name: str) -> Path:
    """Путь к файлу ресурса и при обычном запуске, и внутри сборки PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def battery_percent(voltage: float) -> int:
    """Напряжение банки 18650 -> проценты по паспортным 3.0…4.2 В."""
    fraction = (voltage - BAT_EMPTY) / (BAT_FULL - BAT_EMPTY)
    return max(0, min(100, round(fraction * 100)))


def parse_status(line: str) -> dict[str, str]:
    """'STAT:DOOR=1,STATE=2,BAT=3.95' -> {'DOOR': '1', 'STATE': '2', 'BAT': '3.95'}."""
    if not line.startswith("STAT:"):
        return {}
    params = {}
    for item in line[5:].split(","):
        key, separator, value = item.partition("=")
        if separator:
            params[key.strip().upper()] = value.strip()
    return params


def paint(widget: QWidget, background: str, foreground: str, radius: int = 0) -> None:
    """Красит виджет, у которого цвет меняется в рантайме."""
    style = f"background-color:{background}; color:{foreground};"
    if radius:
        style += f" border-radius:{radius}px;"
    widget.setStyleSheet(style)


class SerialLink:
    """Обёртка над pyserial: держит порт и собирает целые строки протокола.

    Забирать данные нужно только тогда, когда они уже пришли, иначе поток
    интерфейса будет ждать в read().
    """

    def __init__(self) -> None:
        self.port: serial.Serial | None = None
        self.last_error = ""
        self._tail = ""

    @property
    def is_open(self) -> bool:
        return bool(self.port and self.port.is_open)

    def open(self, name: str) -> bool:
        self.close()
        try:
            self.port = serial.Serial(name, BAUD, timeout=0)
        except (serial.SerialException, OSError) as error:
            self.last_error = str(error)
            self.port = None
            return False
        return True

    def close(self) -> None:
        if self.port:
            try:
                self.port.close()
            except (serial.SerialException, OSError):
                pass
        self.port = None
        self._tail = ""

    def send(self, line: str) -> bool:
        if not self.is_open:
            return False
        try:
            self.port.write((line + "\n").encode("utf-8"))
        except (serial.SerialException, OSError) as error:
            self.last_error = str(error)
            self.close()
            return False
        return True

    def receive(self) -> list[str]:
        """Возвращает готовые строки; при обрыве связи закрывает порт."""
        if not self.is_open or not self.port.in_waiting:
            return []
        try:
            chunk = self.port.read(self.port.in_waiting)
        except (serial.SerialException, OSError) as error:
            self.last_error = str(error)
            self.close()
            return []

        # Кириллица из LOG-строк может разорваться на границе чтения,
        # поэтому держим буфер строки до символа перевода строки.
        parts = (self._tail + chunk.decode("utf-8", "ignore")).split("\n")
        self._tail = parts.pop()
        return [line.strip() for line in parts if line.strip()]


class SettingsDialog(QDialog):
    """Код страны для номера и диапазон ползунка задержки — как в мобильном приложении."""

    def __init__(self, parent: QWidget, prefix: str, delay_min: int, delay_max: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setModal(True)

        self.prefix_input = QLineEdit(prefix)
        self.prefix_input.setMaxLength(5)
        self.prefix_input.setPlaceholderText("+7")

        self.min_input = QSpinBox()
        self.min_input.setRange(1, 250)
        self.min_input.setSuffix(" сек")
        self.min_input.setValue(delay_min)

        self.max_input = QSpinBox()
        self.max_input.setRange(1, 250)
        self.max_input.setSuffix(" сек")
        self.max_input.setValue(delay_max)

        self.error_label = QLabel()
        self.error_label.setStyleSheet(f"color:{ALERT_FG};")
        self.error_label.setVisible(False)

        form = QFormLayout(self)
        form.addRow("Код страны:", self.prefix_input)
        form.addRow("Задержка от:", self.min_input)
        form.addRow("Задержка до:", self.max_input)
        form.addRow(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.reject)
        apply_button = QPushButton("Применить")
        apply_button.setDefault(True)
        apply_button.clicked.connect(self.accept)
        buttons.addWidget(cancel_button)
        buttons.addWidget(apply_button)
        form.addRow(buttons)

    def accept(self) -> None:
        prefix = self.prefix_input.text().strip()
        if not QRegularExpression(r"\+\d{1,4}$").match(prefix).hasMatch():
            self._show_error("Код страны в формате +7, +380, +1")
            return
        if self.min_input.value() >= self.max_input.value():
            self._show_error("Верхняя граница должна быть больше нижней")
            return
        super().accept()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)


class DebugWindow(QDialog):
    """Отдельное окно отладки: проверка исполнительных органов, отправка SMS,
    монитор порта с командами TX и всем, что пришло от устройства."""

    def __init__(self, panel: "DoorGuardian") -> None:
        super().__init__(panel)
        self.panel = panel
        self.lines: list[str] = []

        self.setWindowTitle("Отладка")
        self.resize(560, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_hardware_box())
        layout.addWidget(self._build_sms_box())
        layout.addWidget(self._build_console_box(), 1)

    def _build_hardware_box(self) -> QGroupBox:
        box = QGroupBox("Проверка исполнительных органов")
        row = QHBoxLayout(box)

        led_button = QPushButton("Светодиод")
        led_button.setToolTip("TEST:LED — три коротких вспышки")
        led_button.clicked.connect(lambda: self.panel.send_command("TEST:LED"))
        row.addWidget(led_button)

        buzzer_button = QPushButton("Зумер")
        buzzer_button.setToolTip("TEST:BUZZER — проиграть мелодию с зуммера")
        buzzer_button.clicked.connect(lambda: self.panel.send_command("TEST:BUZZER"))
        row.addWidget(buzzer_button)

        gsm_button = QPushButton("Модуль GSM")
        gsm_button.setToolTip("TEST:GSM — ответит ли модуль на AT-запрос")
        gsm_button.clicked.connect(lambda: self.panel.send_command("TEST:GSM"))
        row.addWidget(gsm_button)

        return box

    def _build_sms_box(self) -> QGroupBox:
        box = QGroupBox("Отправка SMS")
        column = QVBoxLayout(box)

        self.sms_number = QLineEdit()
        self.sms_number.setPlaceholderText("+79991234567")
        self.sms_number.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"\+?\d{7,15}"), self)
        )

        self.sms_from_device = QCheckBox("Взять номер из памяти устройства")
        self.sms_from_device.toggled.connect(lambda on: self.sms_number.setDisabled(on))

        self.sms_text = QLineEdit()
        self.sms_text.setPlaceholderText("Текст сообщения")
        self.sms_text.setMaxLength(70)

        row = QHBoxLayout()
        row.addWidget(self.sms_number, 1)
        row.addWidget(self.sms_from_device)

        hint = QLabel("Строка команды: SMS:<номер>:<текст>")
        hint.setStyleSheet(f"color:{MUTED}; font-size:12px;")

        send_button = QPushButton("Отправить")
        send_button.clicked.connect(self.send_sms)

        column.addLayout(row)
        column.addWidget(self.sms_text)
        column.addWidget(hint)
        column.addWidget(send_button)
        return box

    def _build_console_box(self) -> QGroupBox:
        box = QGroupBox("Монитор порта")
        column = QVBoxLayout(box)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet("font-family:'Consolas','Courier New',monospace; font-size:12px;")

        clear_button = QPushButton("Очистить")
        clear_button.clicked.connect(self.clear_console)

        column.addWidget(self.console)
        column.addWidget(clear_button)
        return box

    def send_sms(self) -> None:
        number = "+" if self.sms_from_device.isChecked() else self.sms_number.text().strip()
        text = self.sms_text.text().strip()
        if not number:
            QMessageBox.warning(self, "SMS", "Укажите номер получателя.")
            return
        if not text:
            QMessageBox.warning(self, "SMS", "Введите текст сообщения.")
            return
        if self.panel.send_command(f"SMS:{number}:{text}"):
            self.sms_text.clear()

    def append_line(self, text: str) -> None:
        self.lines.append(text)
        del self.lines[:-CONSOLE_LIMIT]
        self.console.setPlainText("\n".join(self.lines))
        self.console.moveCursor(QTextCursor.MoveOperation.End)

    def clear_console(self) -> None:
        self.lines.clear()
        self.console.clear()


class DoorGuardian(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DoorGuardian")
        self.resize(880, 720)

        icon = resource_path("assets/icon.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        self.style_file = resource_path("styles/dark.qss")
        self.settings = QSettings("DoorGuardian", "ARM")
        self.link = SerialLink()

        self.prefix = self.settings.value("phone_prefix", "+7")
        self.delay_min = self.settings.value("delay_min", 5, type=int)
        self.delay_max = self.settings.value("delay_max", 60, type=int)
        self.delay = self.settings.value("delay", 15, type=int)

        # Последнее, что сообщило устройство. None — данных ещё не было.
        self.door_open: bool | None = None
        self.armed: bool | None = None
        self.events: list[tuple[dt.datetime, str]] = []

        self._load_style()
        self._build_ui()

        self.debug_window = DebugWindow(self)

        self.read_timer = QTimer(self)
        self.read_timer.setInterval(POLL_MS)
        self.read_timer.timeout.connect(self._read_serial)

        self.add_event("Панель запущена")

    # ---------- сборка интерфейса ----------

    def _load_style(self) -> None:
        try:
            self.setStyleSheet(self.style_file.read_text(encoding="utf-8"))
        except OSError as error:
            print(f"Не удалось прочитать {self.style_file}: {error}")

    def _build_ui(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setSpacing(14)

        column.addLayout(self._build_header())
        column.addWidget(self._build_connection_card())
        column.addWidget(self._build_status_card())
        column.addWidget(self._build_battery_card())
        column.addWidget(self._build_settings_card())
        column.addWidget(self._build_log_card())
        column.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()

        title = QLabel("DoorGuardian")
        title.setStyleSheet("font-size:17px; font-weight:700;")
        subtitle = QLabel("Панель управления контроллером")
        subtitle.setStyleSheet(f"color:{MUTED}; font-size:12px;")

        caption = QVBoxLayout()
        caption.setSpacing(0)
        caption.addWidget(title)
        caption.addWidget(subtitle)
        row.addLayout(caption)
        row.addStretch()

        settings_button = QPushButton("Настройки")
        settings_button.clicked.connect(self.open_settings)
        row.addWidget(settings_button)

        debug_button = QPushButton("Отладка")
        debug_button.clicked.connect(self.open_debug)
        row.addWidget(debug_button)

        return row

    def _build_connection_card(self) -> QGroupBox:
        box = QGroupBox("Подключение")
        column = QVBoxLayout(box)

        self.state_badge = QLabel()
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.port_box = QComboBox()
        self.port_box.setMinimumWidth(220)

        refresh_button = QPushButton("Обновить")
        refresh_button.setToolTip("Заново опросить список COM-портов")
        refresh_button.clicked.connect(self.refresh_ports)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("COM-порт:"))
        top_row.addWidget(self.port_box, 1)
        top_row.addWidget(refresh_button)
        top_row.addWidget(self.state_badge)

        self.connect_button = QPushButton("Подключиться")
        self.connect_button.clicked.connect(self.toggle_connection)

        column.addLayout(top_row)
        column.addWidget(self.connect_button)

        self.refresh_ports()
        self._show_connection(False)
        return box

    def _build_status_card(self) -> QGroupBox:
        box = QGroupBox("Состояние")
        column = QVBoxLayout(box)

        self.door_label = QLabel()
        self.door_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.door_label.setStyleSheet("font-size:16px; font-weight:700;")

        self.door_hint = QLabel()
        self.door_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.door_hint.setStyleSheet(f"color:{MUTED}; font-size:13px;")

        self.guard_label = QLabel()
        self.guard_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.arm_button = QPushButton()
        self.arm_button.clicked.connect(self.toggle_guard)

        request_button = QPushButton("Запросить состояние")
        request_button.setToolTip("CMD:STATUS")
        request_button.clicked.connect(lambda: self.send_command("CMD:STATUS"))

        buttons = QHBoxLayout()
        buttons.addWidget(self.arm_button, 1)
        buttons.addWidget(request_button)

        column.addWidget(self.door_label)
        column.addWidget(self.door_hint)
        column.addWidget(self.guard_label)
        column.addLayout(buttons)

        self._show_door(None)
        self._show_guard(None)
        return box

    def _build_battery_card(self) -> QGroupBox:
        box = QGroupBox("Питание")
        column = QVBoxLayout(box)

        self.battery_text = QLabel("Ожидание данных")
        self.battery_text.setStyleSheet(f"color:{MUTED}; font-size:12px;")

        caption = QHBoxLayout()
        caption.addWidget(QLabel("Аккумулятор 18650"))
        caption.addStretch()
        caption.addWidget(self.battery_text)

        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setValue(0)
        self.battery_bar.setFormat("%p%")

        column.addLayout(caption)
        column.addWidget(self.battery_bar)
        return box

    def _build_settings_card(self) -> QGroupBox:
        box = QGroupBox("Оповещения и таймеры")
        column = QVBoxLayout(box)

        self.prefix_label = QLabel(self.prefix)
        self.prefix_label.setStyleSheet(
            f"background-color:#181825; color:{TEXT}; border:1px solid #45475A;"
            " border-radius:10px; padding:7px 10px; font-weight:700;"
        )

        self.phone_input = QLineEdit(self.settings.value("phone", ""))
        self.phone_input.setPlaceholderText("9991234567")
        self.phone_input.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"\+?\d{1,15}"), self)
        )

        phone_row = QHBoxLayout()
        phone_row.addWidget(QLabel("Номер для SMS:"))
        phone_row.addWidget(self.prefix_label)
        phone_row.addWidget(self.phone_input, 1)

        self.delay_value = QLabel()
        self.delay_value.setStyleSheet(f"color:{ACCENT}; font-weight:700;")

        delay_caption = QHBoxLayout()
        delay_caption.addWidget(QLabel("Задержка до тревоги"))
        delay_caption.addStretch()
        delay_caption.addWidget(self.delay_value)

        self.delay_slider = QSlider(Qt.Orientation.Horizontal)
        self.delay_slider.setRange(self.delay_min, self.delay_max)
        self.delay_slider.setValue(max(self.delay_min, min(self.delay, self.delay_max)))
        self.delay_slider.valueChanged.connect(self._show_delay)
        self._show_delay()

        save_button = QPushButton("Сохранить в устройстве")
        paint(save_button, WARN, "#11111B", radius=12)
        save_button.clicked.connect(self.save_settings)

        column.addLayout(phone_row)
        column.addLayout(delay_caption)
        column.addWidget(self.delay_slider)
        column.addWidget(save_button)
        return box

    def _build_log_card(self) -> QGroupBox:
        box = QGroupBox("Журнал событий")
        column = QVBoxLayout(box)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(150)

        export_button = QPushButton("Экспорт в CSV")
        export_button.clicked.connect(self.export_csv)

        column.addWidget(self.log_view)
        column.addWidget(export_button)
        return box

    # ---------- подключение ----------

    def refresh_ports(self) -> None:
        current = self.port_box.currentText()
        self.port_box.clear()
        for port in serial.tools.list_ports.comports():
            self.port_box.addItem(port.device)
        if current and self.port_box.findText(current) >= 0:
            self.port_box.setCurrentText(current)

    def toggle_connection(self) -> None:
        if self.link.is_open:
            self.read_timer.stop()
            self.link.close()
            self._show_connection(False)
            self.add_event("Отключено от устройства")
            return

        port = self.port_box.currentText()
        if not port:
            QMessageBox.warning(self, "COM-порт", "Порт не выбран — обновите список.")
            return
        if not self.link.open(port):
            QMessageBox.critical(
                self, "COM-порт", f"Не удалось открыть {port}:\n{self.link.last_error}"
            )
            return

        self.read_timer.start()
        self._show_connection(True, port)
        self.add_event(f"Подключено к {port}")
        self.send_command("CMD:STATUS")

    def _show_connection(self, connected: bool, port: str = "") -> None:
        if connected:
            self.connect_button.setText("Отключиться")
            paint(self.connect_button, ALERT_FG, "#11111B", radius=12)
            self.state_badge.setText(f"На связи · {port}")
            paint(self.state_badge, OK_BG, OK_FG, radius=12)
        else:
            self.connect_button.setText("Подключиться")
            paint(self.connect_button, OK_FG, "#11111B", radius=12)
            self.state_badge.setText("Нет связи")
            paint(self.state_badge, ALERT_BG, ALERT_FG, radius=12)
        self.state_badge.setMinimumWidth(150)
        self.port_box.setDisabled(connected)

    def send_command(self, command: str) -> bool:
        if not self.link.is_open:
            QMessageBox.warning(self, "Нет связи", "Устройство не подключено.")
            return False
        sent = self.link.send(command)
        self._console(f"TX > {command}")
        if not sent:
            self.add_event(f"Команда не ушла: {self.link.last_error}")
            if not self.link.is_open:
                self._connection_lost(self.link.last_error)
        return sent

    def _read_serial(self) -> None:
        lines = self.link.receive()
        for line in lines:
            self._handle_line(line)
        if not lines and not self.link.is_open:
            self._connection_lost(self.link.last_error)

    def _connection_lost(self, reason: str) -> None:
        self.read_timer.stop()
        self._show_connection(False)
        self.add_event(f"Связь потеряна: {reason or 'порт закрыт'}")

    def _handle_line(self, line: str) -> None:
        self._console(f"RX < {line}")
        if line.startswith("LOG:"):
            self.add_event(line[4:].strip())
            return
        params = parse_status(line)
        if params:
            self._apply_status(params)

    # ---------- телеметрия ----------

    def _apply_status(self, params: dict[str, str]) -> None:
        if "DOOR" in params:
            self._show_door(params["DOOR"] == "1")
        if "STATE" in params:
            try:
                state = int(params["STATE"])
            except ValueError:
                state = None
            self._show_guard(None if state is None else state in (1, 2))
        if "BAT" in params:
            try:
                voltage = float(params["BAT"])
            except ValueError:
                return
            self.battery_bar.setValue(battery_percent(voltage))
            self.battery_bar.setFormat(f"{battery_percent(voltage)}% ({voltage:.2f} В)")
            self.battery_text.setText("Напряжение банки")

    def _show_door(self, is_open: bool | None) -> None:
        if is_open is not None and self.door_open is not None and is_open == self.door_open:
            return
        first_reading = self.door_open is None
        self.door_open = is_open

        background, foreground = DOOR_COLORS[is_open]
        self.door_label.setStyleSheet(
            f"background-color:{background}; color:{foreground};" + DOOR_PLATE
        )
        if is_open is None:
            self.door_label.setText("НЕТ ДАННЫХ")
            self.door_hint.setText("Ожидание телеметрии от контроллера")
            return

        if is_open:
            self.door_label.setText("ДВЕРЬ ОТКРЫТА")
            self.door_hint.setText("Зафиксирован проход")
        else:
            self.door_label.setText("ДВЕРЬ ЗАКРЫТА")
            self.door_hint.setText("Периметр защищён")

        if not first_reading:
            self.add_event("Дверь открыта" if is_open else "Дверь закрыта")

    def _show_guard(self, armed: bool | None) -> None:
        if armed is not None and armed == self.armed:
            return
        first_reading = self.armed is None
        self.armed = armed

        if armed is None:
            self.guard_label.setText("Состояние: неизвестно")
            self.guard_label.setStyleSheet(f"color:{MUTED};")
            self.arm_button.setText("Поставить на охрану")
            paint(self.arm_button, ACCENT, "#11111B", radius=12)
            return

        if armed:
            self.guard_label.setText("Состояние: на охране")
            self.guard_label.setStyleSheet(f"color:{OK_FG};")
            self.arm_button.setText("Снять с охраны")
            paint(self.arm_button, ALERT_FG, "#11111B", radius=12)
        else:
            self.guard_label.setText("Состояние: снято с охраны")
            self.guard_label.setStyleSheet(f"color:{MUTED};")
            self.arm_button.setText("Поставить на охрану")
            paint(self.arm_button, ACCENT, "#11111B", radius=12)

        if not first_reading:
            self.add_event("Система на охране" if armed else "Система снята с охраны")

    def toggle_guard(self) -> None:
        self.send_command("CMD:DISARM" if self.armed else "CMD:ARM")

    # ---------- настройки ----------

    def _show_delay(self) -> None:
        self.delay_value.setText(f"{self.delay_slider.value()} сек")

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.prefix, self.delay_min, self.delay_max)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self.prefix = dialog.prefix_input.text().strip()
        self.delay_min = dialog.min_input.value()
        self.delay_max = dialog.max_input.value()

        self.prefix_label.setText(self.prefix)
        self.delay_slider.setRange(self.delay_min, self.delay_max)
        self._show_delay()

        self.settings.setValue("phone_prefix", self.prefix)
        self.settings.setValue("delay_min", self.delay_min)
        self.settings.setValue("delay_max", self.delay_max)

        self.add_event(
            f"Настройки панели: код {self.prefix}, задержка {self.delay_min}–{self.delay_max} сек"
        )

    def save_settings(self) -> None:
        # Номер можно вставить целиком — код страны из него всё равно уберём.
        digits = self.phone_input.text().strip().lstrip("+")
        country = self.prefix.lstrip("+")
        if digits.startswith(country):
            digits = digits[len(country):]
        number = self.prefix + digits
        if not 7 <= len(number.lstrip("+")) <= 15:
            QMessageBox.warning(
                self,
                "Номер",
                f"В номере {len(number.lstrip('+'))} цифр — нужно от 7 до 15.",
            )
            return

        delay = self.delay_slider.value()
        if not self.send_command(f"SET:PHONE={number},T1={delay}"):
            return

        self.settings.setValue("phone", digits)
        self.settings.setValue("delay", delay)
        self.add_event(f"Настройки отправлены: {number}, задержка {delay} сек")

    # ---------- журнал и отладка ----------

    def add_event(self, text: str) -> None:
        self.events.append((dt.datetime.now(), text))
        del self.events[:-LOG_LIMIT]
        self.log_view.setPlainText(
            "\n".join(f"{moment:%H:%M:%S}  {message}" for moment, message in self.events)
        )
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self._console(f"-- {text}")

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить журнал", "door_guardian_log.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            # utf-8-sig — чтобы Excel правильно показал кириллицу
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(["Дата и время", "Событие"])
                for moment, message in self.events:
                    writer.writerow([moment.strftime("%Y-%m-%d %H:%M:%S"), message])
        except OSError as error:
            QMessageBox.critical(self, "Экспорт", f"Не удалось записать файл:\n{error}")
            return
        self.add_event(f"Журнал сохранён: {Path(path).name}")

    def open_debug(self) -> None:
        self.debug_window.show()
        self.debug_window.raise_()
        self.debug_window.activateWindow()

    def _console(self, text: str) -> None:
        self.debug_window.append_line(text)

    def closeEvent(self, event) -> None:
        self.read_timer.stop()
        self.link.close()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    panel = DoorGuardian()
    panel.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
