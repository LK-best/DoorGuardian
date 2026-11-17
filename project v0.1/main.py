import csv
import datetime
import inspect
import os
import threading
import time
import urllib.parse
import urllib.request
import flet as ft

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
DEFAULT_VPS_HOST = "194.87.237.144"  # Вставьте ваш реальный IP VPS
DEFAULT_VPS_PORT = 18080
CSV_LOG_FILE = "security_events.csv"


class TurretOperatorApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.page.title = "Охранный комплекс: Турель & DoorGuardian"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.padding = 15
        self.page.window_width = 1240
        self.page.window_height = 860

        # Сетевые параметры
        self.vps_host = DEFAULT_VPS_HOST
        self.vps_port = DEFAULT_VPS_PORT
        self.phone_prefix = "+7"
        self.phone_number = "9991234567"
        self.cmd_repeat_interval = 0.08  # 80 мс при зажатии

        # Удержание кнопок
        self.holding_command = None
        self.hold_thread = None
        self.is_running = True

        # Состояние двери
        self.door_status = "НЕИЗВЕСТНО"
        self.guardian_state = "DISARMED"

        # Телеметрия башни
        self.telem_dist = -1
        self.telem_light = 0
        self.telem_pan = 165
        self.telem_tilt = 125

        self.init_csv()
        self.build_ui()
        self.start_background_tasks()

    def init_csv(self):
        if not os.path.exists(CSV_LOG_FILE):
            with open(CSV_LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "EventType", "Details"])

    def log_event(self, event_type: str, details: str):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(CSV_LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([now_str, event_type, details])
        except Exception as e:
            print(f"Ошибка CSV: {e}")

        if hasattr(self, "log_list_view"):
            self.log_list_view.controls.insert(
                0,
                ft.Text(f"[{now_str}] [{event_type}] {details}", size=12, color=ft.colors.ON_SURFACE_VARIANT)
            )
            self.page.update()

    # ==========================================
    # СЕТЕВЫЕ ЗАПРОСЫ И ЗАЖАТИЕ КНОПОК
    # ==========================================
    def send_ptz_cmd(self, cmd_char: str):
        url = f"http://{self.vps_host}:{self.vps_port}/cmd?c={cmd_char}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FletClient/2.0"})
            with urllib.request.urlopen(req, timeout=0.15) as resp:
                pass
        except Exception:
            pass

    def start_hold(self, cmd_char: str):
        self.holding_command = cmd_char
        if self.hold_thread is None or not self.hold_thread.is_alive():
            self.hold_thread = threading.Thread(target=self._hold_worker, daemon=True)
            self.hold_thread.start()

    def stop_hold(self):
        self.holding_command = None

    def _hold_worker(self):
        while self.holding_command and self.is_running:
            self.send_ptz_cmd(self.holding_command)
            time.sleep(self.cmd_repeat_interval)

    def send_door_cmd(self, cmd_text: str):
        url = f"http://{self.vps_host}:{self.vps_port}/api/door_cmd?c={cmd_text}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FletClient/2.0"})
            with urllib.request.urlopen(req, timeout=0.3) as resp:
                self.log_event("DOOR_CMD", f"Команда охране двери: {cmd_text}")
        except Exception:
            pass

    # ==========================================
    # ИНТЕРФЕЙС
    # ==========================================
    def build_ui(self):
        self.title_row = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Text("ЦЕНТР ОПЕРАТИВНОГО НАБЛЮДЕНИЯ", size=22, weight=ft.FontWeight.BOLD),
                ft.Row(
                    controls=[
                        ft.IconButton(ft.icons.SETTINGS, tooltip="Настройки связи и SMS", on_click=self.open_settings),
                        ft.IconButton(ft.icons.REFRESH, tooltip="Сбросить в центр (C)", on_click=lambda _: self.send_ptz_cmd("C"))
                    ]
                )
            ]
        )

        # Видеопанель
        self.video_image = ft.Image(
            src=f"http://{self.vps_host}:{self.vps_port}/snapshot",
            width=540,
            height=400,
            fit=ft.ImageFit.CONTAIN,
            border_radius=8
        )
        self.video_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("ВИДЕОПОТОК С ТУРЕЛИ (ESP32-S3)", weight=ft.FontWeight.BOLD),
                        self.video_image
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER
                ),
                padding=10
            )
        )

        # Конструктор кнопки с поддержкой зажатия (GestureDetector)
        def make_hold_button(label: str, cmd: str, width: int = 110, height: int = 50):
            container = ft.Container(
                content=ft.Text(label, weight=ft.FontWeight.BOLD, size=14),
                alignment=ft.alignment.center,
                width=width,
                height=height,
                bgcolor=ft.colors.SURFACE_VARIANT,
                border_radius=8,
                border=ft.border.all(1, ft.colors.OUTLINE)
            )
            return ft.GestureDetector(
                on_tap_down=lambda _: self.start_hold(cmd),
                on_tap_up=lambda _: self.stop_hold(),
                content=container
            )

        btn_up = make_hold_button("▲ ВВЕРХ", "U", width=120)
        btn_down = make_hold_button("▼ ВНИЗ", "D", width=120)
        btn_left = make_hold_button("◄ ВЛЕВО", "L", width=105)
        btn_right = make_hold_button("ВПРАВО ►", "R", width=105)
        btn_center = ft.ElevatedButton("• ЦЕНТР •", width=105, height=50, on_click=lambda _: self.send_ptz_cmd("C"))

        self.ptz_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("УПРАВЛЕНИЕ ПРИВОДАМИ (PTZ С ЗАЖАТИЕМ)", weight=ft.FontWeight.BOLD),
                        ft.Row([btn_up], alignment=ft.MainAxisAlignment.CENTER),
                        ft.Row([btn_left, btn_center, btn_right], alignment=ft.MainAxisAlignment.CENTER),
                        ft.Row([btn_down], alignment=ft.MainAxisAlignment.CENTER),
                        ft.Text("Удерживайте кнопку мыши или используйте стрелки на клавиатуре", size=11, color=ft.colors.GREY_400)
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10
                ),
                padding=15
            )
        )

        # Телеметрия башни
        self.txt_dist = ft.Text("Дистанция: -- см", size=15)
        self.txt_light = ft.Text("Освещенность: --", size=15)
        self.txt_pan = ft.Text("Угол Pan (D10): 165°", size=15)
        self.txt_tilt = ft.Text("Угол Tilt (D11): 125°", size=15)

        self.telem_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("ДАТЧИКИ ТУРЕЛИ (ARDUINO)", weight=ft.FontWeight.BOLD),
                        self.txt_dist,
                        self.txt_light,
                        self.txt_pan,
                        self.txt_tilt
                    ],
                    spacing=8
                ),
                padding=15
            )
        )

        # Охрана двери
        self.txt_door_mode = ft.Text("Связь с дверью: Облако (Wi-Fi)", size=13, color=ft.colors.LIGHT_GREEN)
        self.txt_door_state = ft.Text("Геркон: НЕИЗВЕСТНО", size=15, weight=ft.FontWeight.BOLD)
        self.txt_alarm_state = ft.Text("Режим: Ожидание", size=15)

        self.btn_arm = ft.ElevatedButton("ПОСТАВИТЬ НА ОХРАНУ (15 СЕК)", icon=ft.icons.LOCK, on_click=lambda _: self.send_door_cmd("ARM"))
        self.btn_disarm = ft.ElevatedButton("СНЯТЬ С ОХРАНЫ / СБРОС", icon=ft.icons.LOCK_OPEN, on_click=lambda _: self.send_door_cmd("DISARM"))

        self.guardian_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("ОХРАНА ДВЕРИ (DOOR GUARDIAN WI-FI)", weight=ft.FontWeight.BOLD),
                        self.txt_door_mode,
                        self.txt_door_state,
                        self.txt_alarm_state,
                        ft.Row([self.btn_arm, self.btn_disarm], alignment=ft.MainAxisAlignment.START)
                    ],
                    spacing=8
                ),
                padding=15
            )
        )

        # SMS
        self.txt_sms_target = ft.Text(f"Получатель: {self.phone_prefix} {self.phone_number}", size=13)
        self.sms_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("СИСТЕМА SMS-ОПОВЕЩЕНИЯ", weight=ft.FontWeight.BOLD),
                        self.txt_sms_target,
                        ft.ElevatedButton("Тестовое SMS тревоги", icon=ft.icons.SMS, on_click=self.send_test_sms)
                    ],
                    spacing=8
                ),
                padding=15
            )
        )

        # Лог CSV
        self.log_list_view = ft.ListView(expand=True, spacing=4, auto_scroll=True)
        self.logs_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("ЖУРНАЛ БЕЗОПАСНОСТИ (CSV)", weight=ft.FontWeight.BOLD),
                        ft.Container(self.log_list_view, height=140)
                    ]
                ),
                padding=15
            )
        )

        left_col = ft.Column([self.video_card, self.logs_card], expand=3)
        right_col = ft.Column([self.ptz_card, self.telem_card, self.guardian_card, self.sms_card], expand=2)

        self.page.add(
            self.title_row,
            ft.Row([left_col, right_col], vertical_alignment=ft.CrossAxisAlignment.START)
        )

        self.page.on_keyboard_event = self.on_keyboard

    # ==========================================
    # НАСТРОЙКИ (OPEN_SETTINGS)
    # ==========================================
    def open_settings(self, _):
        tb_vps_ip = ft.TextField(label="IP сервера VPS", value=str(self.vps_host))
        tb_vps_port = ft.TextField(label="Порт VPS", value=str(self.vps_port))
        tb_prefix = ft.TextField(label="Префикс телефона", value=str(self.phone_prefix), width=100)
        tb_phone = ft.TextField(label="Номер телефона", value=str(self.phone_number), expand=True)

        slider_delay = ft.Slider(
            min=0.03, max=0.25, divisions=22,
            value=self.cmd_repeat_interval,
            label="{value} сек",
            tooltip="Интервал отправки при удержании"
        )

        def save_settings(e):
            self.vps_host = tb_vps_ip.value.strip()
            try:
                self.vps_port = int(tb_vps_port.value.strip())
            except ValueError:
                self.vps_port = 18080

            self.phone_prefix = tb_prefix.value.strip()
            self.phone_number = tb_phone.value.strip()
            self.cmd_repeat_interval = float(slider_delay.value)

            self.txt_sms_target.value = f"Получатель: {self.phone_prefix} {self.phone_number}"
            self.log_event("CONFIG", f"VPS: {self.vps_host}:{self.vps_port}")

            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            title=ft.Text("Конфигурация комплекса"),
            content=ft.Column([
                tb_vps_ip, tb_vps_port,
                ft.Row([tb_prefix, tb_phone]),
                ft.Text("Скорость повтора команд при удержании:"),
                slider_delay
            ], tight=True, width=420),
            actions=[
                ft.TextButton("Отмена", on_click=lambda _: setattr(dialog, "open", False) or self.page.update()),
                ft.ElevatedButton("Сохранить", on_click=save_settings)
            ]
        )
        self.page.dialog = dialog
        dialog.open = True
        self.page.update()

    def on_keyboard(self, e: ft.KeyboardEvent):
        key = e.key
        if key == "Arrow Up": self.send_ptz_cmd("U")
        elif key == "Arrow Down": self.send_ptz_cmd("D")
        elif key == "Arrow Left": self.send_ptz_cmd("L")
        elif key == "Arrow Right": self.send_ptz_cmd("R")
        elif key in [" ", "Space"]: self.send_ptz_cmd("C")

    def send_test_sms(self, _):
        full_phone = f"{self.phone_prefix}{self.phone_number}"
        self.log_event("SMS_DISPATCH", f"Отправка тестового тревожного SMS на {full_phone}: ТРЕВОГА! Взлом двери!")
        self.page.snack_bar = ft.SnackBar(ft.Text(f"SMS отправлено на {full_phone}!"), open=True)
        self.page.update()

    # ==========================================
    # ФОНОВЫЙ ОПРОС
    # ==========================================
    def start_background_tasks(self):
        threading.Thread(target=self._snapshot_worker, daemon=True).start()
        threading.Thread(target=self._telemetry_worker, daemon=True).start()
        threading.Thread(target=self._door_http_worker, daemon=True).start()

    def _snapshot_worker(self):
        while self.is_running:
            try:
                t_stamp = int(time.time() * 1000)
                self.video_image.src = f"http://{self.vps_host}:{self.vps_port}/snapshot?t={t_stamp}"
                self.page.update()
            except Exception:
                pass
            time.sleep(0.07)

    def _telemetry_worker(self):
        while self.is_running:
            url = f"http://{self.vps_host}:{self.vps_port}/telemetry"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FletClient/2.0"})
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore").strip()
                    self.parse_turret_telemetry(raw)
            except Exception:
                pass
            time.sleep(0.3)

    def parse_turret_telemetry(self, raw: str):
        if not raw or "DIST=" not in raw: return
        parts = raw.split(";")
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                if k == "DIST": self.telem_dist = int(v) if v.lstrip("-").isdigit() else -1
                elif k == "LIGHT": self.telem_light = int(v) if v.isdigit() else 0
                elif k == "PAN": self.telem_pan = int(v) if v.isdigit() else 165
                elif k == "TILT": self.telem_tilt = int(v) if v.isdigit() else 125

        self.txt_dist.value = f"Дистанция: {self.telem_dist} см" if self.telem_dist >= 0 else "Дистанция: > 400 см"
        self.txt_light.value = f"Освещенность: {self.telem_light}"
        self.txt_pan.value = f"Угол Pan (D10): {self.telem_pan}°"
        self.txt_tilt.value = f"Угол Tilt (D11): {self.telem_tilt}°"
        self.page.update()

    def _door_http_worker(self):
        while self.is_running:
            url = f"http://{self.vps_host}:{self.vps_port}/door_status"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FletClient/2.0"})
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore").strip()
                    self.parse_door_telemetry(raw)
            except Exception:
                pass
            time.sleep(0.5)

    def parse_door_telemetry(self, raw: str):
        if not raw or "DOOR=" not in raw: return
        parts = raw.split(";")
        door_open = False
        state = "UNKNOWN"

        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                if k == "DOOR": door_open = (v == "OPEN")
                elif k == "STATE": state = v

        prev_door = self.door_status
        self.door_status = "ОТКРЫТА" if door_open else "ЗАКРЫТА"
        self.guardian_state = state

        if door_open:
            self.txt_door_state.value = "Геркон: ВНИМАНИЕ! ДВЕРЬ ОТКРЫТА!"
            self.txt_door_state.color = ft.colors.RED_ACCENT
        else:
            self.txt_door_state.value = "Геркон: Дверь закрыта"
            self.txt_door_state.color = ft.colors.GREEN_ACCENT

        if state.startswith("ARMING"):
            sec_left = state.split("_")[1] if "_" in state else "..."
            self.txt_alarm_state.value = f"Режим: ЗАДЕРЖКА НА ВЫХОД ({sec_left})"
            self.txt_alarm_state.color = ft.colors.AMBER_ACCENT
        elif state == "ALARM":
            self.txt_alarm_state.value = "Режим: ТРЕВОГА! СИРЕНА FNAF!"
            self.txt_alarm_state.color = ft.colors.RED_ACCENT
            if prev_door != "ОТКРЫТА":
                self.log_event("ALARM", "Сработала тревога открытия двери! Звучит сирена FNAF.")
        elif state == "ARMED":
            self.txt_alarm_state.value = "Режим: НА ОХРАНЕ"
            self.txt_alarm_state.color = ft.colors.LIGHT_BLUE_ACCENT
        else:
            self.txt_alarm_state.value = "Режим: СНЯТ С ОХРАНЫ"
            self.txt_alarm_state.color = ft.colors.GREY_400

        self.page.update()


def main(page: ft.Page):
    TurretOperatorApp(page)


def launch_app():
    if hasattr(ft, "app") and callable(ft.app) and not inspect.ismodule(ft.app):
        ft.app(target=main)
        return
    if hasattr(ft, "app") and hasattr(ft.app, "app") and callable(ft.app.app):
        ft.app.app(target=main)
        return
    try:
        from flet.app import app as fapp
        if callable(fapp) and not inspect.ismodule(fapp):
            fapp(target=main)
            return
    except Exception:
        pass
    if hasattr(ft, "run") and callable(ft.run):
        ft.run(target=main)
        return


if __name__ == "__main__":
    launch_app()