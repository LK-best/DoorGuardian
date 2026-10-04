import sys
import os
import datetime
import csv
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QComboBox, QLineEdit,
    QProgressBar, QTableWidget, QTableWidgetItem, QTextEdit,
    QGroupBox, QFormLayout, QSpinBox, QMessageBox, QHeaderView,
    QFileDialog  # <-- Добавлен QFileDialog
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QIcon
import serial
import serial.tools.list_ports
from pathlib import Path

def resource_path(relative_path: str) -> Path:
    """
    Возвращает путь к ресурсу:
    — при обычном запуске Python;
    — внутри EXE, собранного PyInstaller.
    """
    if hasattr(sys, "_MEIPASS"):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).resolve().parent

    return base_path / relative_path

class DoorGuardianApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DoorGuardian - Панель управления АРМ")
        self.resize(900, 650)
        
        # 1. Иконка приложения (используем методы Path)
        icon_path = resource_path("assets/icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        else:
            print(f"Иконка не найдена: {icon_path}")

        # 2. Загружаем стили из папки styles
        self.load_qss()
        
        self.serial_port = None
        
        # Главный виджет
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # 3. Верхняя панель подключения COM-порта
        main_layout.addLayout(self.create_connection_bar())

        # 4. Вкладки (Tabs)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.create_dashboard_tab(), "🛡️ Мониторинг")
        self.tabs.addTab(self.create_settings_tab(), "⚙️ Настройки")
        self.tabs.addTab(self.create_diagnostics_tab(), "🛠️ Диагностика")
        self.tabs.addTab(self.create_log_tab(), "📋 Журнал событий")
        
        main_layout.addWidget(self.tabs)

        # Таймер опроса Serial
        self.read_timer = QTimer()
        self.read_timer.setInterval(100)
        self.read_timer.timeout.connect(self.read_serial_data)

    def load_qss(self):
        """Загружает оформление из файла styles/dark.qss."""
        # Убрано self., так как resource_path - глобальная функция
        qss_path = resource_path("styles/dark.qss")
    
        try:
            stylesheet = qss_path.read_text(encoding="utf-8")
            self.setStyleSheet(stylesheet)
            print(f"Стили загружены: {qss_path}")
        except FileNotFoundError:
            print(f"Файл стилей не найден: {qss_path}")
        except OSError as error:
            print(f"Не удалось загрузить стили: {error}")

    # ================== ПАНЕЛЬ ПОДКЛЮЧЕНИЯ ==================
    def create_connection_bar(self):
        layout = QHBoxLayout()
        
        layout.addWidget(QLabel("COM Порт:"))
        self.combo_ports = QComboBox()
        self.refresh_ports()
        layout.addWidget(self.combo_ports)

        btn_refresh = QPushButton("🔄")
        btn_refresh.setFixedWidth(40)
        btn_refresh.clicked.connect(self.refresh_ports)
        layout.addWidget(btn_refresh)

        self.btn_connect = QPushButton("Подключиться")
        self.btn_connect.setStyleSheet("background-color: #a6e3a1; color: #11111b;")
        self.btn_connect.clicked.connect(self.toggle_connection)
        layout.addWidget(self.btn_connect)

        self.lbl_status = QLabel("Статус: Отключено")
        self.lbl_status.setStyleSheet("color: #f38ba8; font-weight: bold;")
        layout.addWidget(self.lbl_status)
        layout.addStretch()

        return layout

    def refresh_ports(self):
        self.combo_ports.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.combo_ports.addItem(p.device)

    def toggle_connection(self):
        if self.serial_port and self.serial_port.is_open:
            self.read_timer.stop()
            self.serial_port.close()
            self.btn_connect.setText("Подключиться")
            self.btn_connect.setStyleSheet("background-color: #a6e3a1; color: #11111b;")
            self.lbl_status.setText("Статус: Отключено")
            self.lbl_status.setStyleSheet("color: #f38ba8; font-weight: bold;")
        else:
            port = self.combo_ports.currentText()
            if not port:
                QMessageBox.warning(self, "Ошибка", "Выберите COM-порт!")
                return
            try:
                self.serial_port = serial.Serial(port, 9600, timeout=0.1)
                self.read_timer.start()
                self.btn_connect.setText("Отключиться")
                self.btn_connect.setStyleSheet("background-color: #f38ba8; color: #11111b;")
                self.lbl_status.setText(f"Подключено: {port}")
                self.lbl_status.setStyleSheet("color: #a6e3a1; font-weight: bold;")
                self.add_log_event("Связь", "Успешное подключение к контроллеру")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось открыть порт:\n{e}")

    # ================== ВКЛАДКА 1: МОНИТОРИНГ ==================
    def create_dashboard_tab(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)

        # Левая колонка - Статусы
        left_box = QGroupBox("Состояние системы")
        left_layout = QVBoxLayout(left_box)

        self.lbl_door_status = QLabel("ДВЕРЬ ЗАКРЫТА")
        self.lbl_door_status.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        self.lbl_door_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_door_status.setStyleSheet("background-color: #2e3c32; color: #a6e3a1; border-radius: 10px; padding: 20px;")
        left_layout.addWidget(self.lbl_door_status)

        self.lbl_state = QLabel("Режим: Снято с охраны")
        self.lbl_state.setFont(QFont("Segoe UI", 12))
        left_layout.addWidget(self.lbl_state)

        # Аккумулятор
        left_layout.addWidget(QLabel("Заряд аккумулятора (18650):"))
        self.battery_bar = QProgressBar()
        self.battery_bar.setValue(85)
        self.battery_bar.setFormat("85% (3.95 V)")
        left_layout.addWidget(self.battery_bar)

        layout.addWidget(left_box)

        # Правая колонка - Управление
        right_box = QGroupBox("Быстрое управление")
        right_layout = QVBoxLayout(right_box)

        self.btn_arm = QPushButton("ПОСТАВИТЬ НА ОХРАНУ")
        self.btn_arm.setFixedHeight(60)
        self.btn_arm.setStyleSheet("background-color: #89b4fa; color: #11111b; font-size: 14px;")
        self.btn_arm.clicked.connect(lambda: self.send_command("CMD:ARM"))
        right_layout.addWidget(self.btn_arm)

        self.btn_disarm = QPushButton("СНЯТЬ С ОХРАНЫ")
        self.btn_disarm.setFixedHeight(60)
        self.btn_disarm.setStyleSheet("background-color: #f38ba8; color: #11111b; font-size: 14px;")
        self.btn_disarm.clicked.connect(lambda: self.send_command("CMD:DISARM"))
        right_layout.addWidget(self.btn_disarm)

        layout.addWidget(right_box)
        return widget

    # ================== ВКЛАДКА 2: НАСТРОЙКИ ==================
    def create_settings_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        box = QGroupBox("Параметры оповещения (Запись в EEPROM)")
        form = QFormLayout(box)

        self.txt_phone = QLineEdit("+79775590755")
        form.addRow("Номер телефона для SMS/Звонков:", self.txt_phone)

        self.spin_timer1 = QSpinBox()
        self.spin_timer1.setValue(15)
        self.spin_timer1.setSuffix(" сек")
        form.addRow("Таймаут включения звука:", self.spin_timer1)

        self.spin_timer2 = QSpinBox()
        self.spin_timer2.setValue(60)
        self.spin_timer2.setSuffix(" сек")
        form.addRow("Таймаут отправки SMS:", self.spin_timer2)

        btn_save = QPushButton("💾 Сохранить настройки в устройство")
        btn_save.setStyleSheet("background-color: #f9e2af; color: #11111b;")
        btn_save.clicked.connect(self.save_settings)
        form.addRow(btn_save)

        layout.addWidget(box)
        layout.addStretch()
        return widget

    def save_settings(self):
        phone = self.txt_phone.text()
        t1 = self.spin_timer1.value()
        cmd = f"SET:PHONE={phone},T1={t1}"
        self.send_command(cmd)
        QMessageBox.information(self, "Успех", "Настройки отправлены в микроконтроллер!")
        self.add_log_event("Конфигурация", f"Обновлен номер: {phone}")

    # ================== ВКЛАДКА 3: ДИАГНОСТИКА ==================
    def create_diagnostics_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        box_tests = QGroupBox("Тестирование исполнительных органов")
        test_layout = QHBoxLayout(box_tests)

        btn_test_led = QPushButton("Тест LED")
        btn_test_led.clicked.connect(lambda: self.send_command("TEST:LED"))
        test_layout.addWidget(btn_test_led)

        btn_test_buzzer = QPushButton("Тест Зуммера")
        btn_test_buzzer.clicked.connect(lambda: self.send_command("TEST:BUZZER"))
        test_layout.addWidget(btn_test_buzzer)

        btn_test_gsm = QPushButton("Проверка GSM (AT)")
        btn_test_gsm.clicked.connect(lambda: self.send_command("TEST:GSM"))
        test_layout.addWidget(btn_test_gsm)

        layout.addWidget(box_tests)

        # Консоль сырых данных
        box_console = QGroupBox("RAW Serial Terminal (Монитор порта)")
        console_layout = QVBoxLayout(box_console)
        self.txt_console = QTextEdit()
        self.txt_console.setReadOnly(True)
        self.txt_console.setStyleSheet("font-family: 'Consolas', monospace; color: #a6e3a1;")
        console_layout.addWidget(self.txt_console)

        layout.addWidget(box_console)
        return widget

    # ================== ВКЛАДКА 4: ЖУРНАЛ СОБЫТИЙ ==================
    def create_log_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self.table_log = QTableWidget(0, 3)
        self.table_log.setHorizontalHeaderLabels(["Дата и Время", "Тип события", "Детали"])
        self.table_log.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table_log)

        btn_export = QPushButton("📊 Экспорт журнала в CSV")
        btn_export.clicked.connect(self.export_csv)
        layout.addWidget(btn_export)

        return widget

    def add_log_event(self, event_type, details):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = self.table_log.rowCount()
        self.table_log.insertRow(row)
        self.table_log.setItem(row, 0, QTableWidgetItem(now))
        self.table_log.setItem(row, 1, QTableWidgetItem(event_type))
        self.table_log.setItem(row, 2, QTableWidgetItem(details))

    def export_csv(self):
        # Заменено QMessageBox на QFileDialog и добавлено имя по умолчанию
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить CSV",
            "door_guardian_log.csv",
            "CSV Files (*.csv)"
        )
        
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Type", "Details"])
                for row in range(self.table_log.rowCount()):
                    writer.writerow([
                        self.table_log.item(row, 0).text(),
                        self.table_log.item(row, 1).text(),
                        self.table_log.item(row, 2).text()
                    ])
            QMessageBox.information(self, "Успех", "Журнал экспортирован!")

    # ================== РАБОТА С SERIAL ==================
    def send_command(self, cmd):
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.write((cmd + "\n").encode())
            self.txt_console.append(f"TX > {cmd}")
        else:
            QMessageBox.warning(self, "Ошибка", "Устройство не подключено!")

    def read_serial_data(self):
        if self.serial_port and self.serial_port.is_open:
            while self.serial_port.in_waiting:
                line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    self.txt_console.append(f"RX < {line}")
                    self.parse_telemetry(line)

    def parse_telemetry(self, data):
        # Пример строки: STAT:DOOR=1,STATE=2,BAT=3.95
        if data.startswith("STAT:"):
            try:
                content = data.replace("STAT:", "")
                params = dict(item.split("=") for item in content.split(","))

                # Обновляем дверь
                if "DOOR" in params:
                    if params["DOOR"] == "1":
                        self.lbl_door_status.setText("⚠️ ДВЕРЬ ОТКРЫТА!")
                        self.lbl_door_status.setStyleSheet("background-color: #452a2a; color: #f38ba8; border-radius: 10px; padding: 20px;")
                    else:
                        self.lbl_door_status.setText("ДВЕРЬ ЗАКРЫТА")
                        self.lbl_door_status.setStyleSheet("background-color: #2e3c32; color: #a6e3a1; border-radius: 10px; padding: 20px;")

                # Обновляем АКБ
                if "BAT" in params:
                    v = float(params["BAT"])
                    pct = int(max(0, min(100, (v - 3.0) / (4.2 - 3.0) * 100)))
                    self.battery_bar.setValue(pct)
                    self.battery_bar.setFormat(f"{pct}% ({v:.2f} V)")

            except Exception as e:
                pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DoorGuardianApp()
    window.show()
    sys.exit(app.exec())