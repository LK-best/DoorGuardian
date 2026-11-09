import asyncio
import base64
import csv
from datetime import datetime

import aiohttp
import flet as ft
from bleak import BleakClient, BleakScanner

# =========================================================================
# КОНФИГУРАЦИЯ СИСТЕМЫ ПО УМОЛЧАНИЮ
# =========================================================================
DEFAULT_CAMERA_IP = "192.168.1.100"
BLE_DEVICE_NAME = "DoorGuardian"
BLE_CHAR_TX_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"  # NOTIFY
BLE_CHAR_RX_UUID = "6d68efe5-04b6-4a85-abc4-c2670b7bf7fd"  # WRITE

DEFAULT_PHONE_PREFIX = "+7"
DEFAULT_PHONE_NUMBER = "9991234567"
DEFAULT_ALARM_DELAY_SEC = 3

PTZ_REPEAT_SEC = 0.12
TELEMETRY_PERIOD_SEC = 0.4
VIDEO_PERIOD_SEC = 0.06
LOG_VISIBLE_ROWS = 25


# =========================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================================
def open_overlay(page: ft.Page, control):
    """Показать диалог или снекбар через page.overlay (работает в Flet 1.0)."""
    if control not in page.overlay:
        page.overlay.append(control)
    control.open = True
    page.update()


def close_overlay(page: ft.Page, control):
    """Скрыть диалог или снекбар."""
    control.open = False
    page.update()

def fmt_distance(cm: int) -> str:
    return f"{cm} см" if cm > 0 else "---"


async def main(page: ft.Page):
    page.title = "Охранный комплекс: DoorGuardian & FPV Turret"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 16
    page.spacing = 16
    page.scroll = ft.ScrollMode.ADAPTIVE

    # --- СОСТОЯНИЕ ПРИЛОЖЕНИЯ ---
    state = {
        "camera_ip": DEFAULT_CAMERA_IP,
        "phone_prefix": DEFAULT_PHONE_PREFIX,
        "phone_number": DEFAULT_PHONE_NUMBER,
        "alarm_delay": DEFAULT_ALARM_DELAY_SEC,
        "door_open": False,
        "alarm_active": False,
        "ble_connected": False,
        "camera_streaming": False,
        "head_distance": -1,
        "head_light": 0,
        "head_pan": 165,
        "head_tilt": 125,
        "ptz_active_cmd": None,
        "ptz_running": False,
    }

    ble_client = None
    http_session = None
    event_log_data = []

    async def get_http() -> aiohttp.ClientSession:
        """Одна HTTP-сессия на всё приложение (переиспользование соединений)."""
        nonlocal http_session
        if http_session is None or http_session.closed:
            http_session = aiohttp.ClientSession()
        return http_session

    # =========================================================================
    # ЛОГИРОВАНИЕ, УВЕДОМЛЕНИЯ, ЭКСПОРТ
    # =========================================================================
    def show_snack(text: str, color):
        # Убираем старые снекбары, чтобы overlay не разрастался
        page.overlay[:] = [c for c in page.overlay if not isinstance(c, ft.SnackBar)]
        open_overlay(page, ft.SnackBar(ft.Text(text), bgcolor=color))

    def build_log_row(item: dict) -> ft.Row:
        is_alarm = "ТРЕВОГА" in item["type"]
        color = ft.Colors.RED_400 if is_alarm else ft.Colors.WHITE
        return ft.Row([
            ft.Text(item["time"], size=12, width=150),
            ft.Text(item["type"], size=12, width=130, color=color, weight=ft.FontWeight.BOLD),
            ft.Text(item["details"], size=12, expand=True),
            ft.Text(item["distance"], size=12, width=90),
            ft.Text(item["light"], size=12, width=60),
        ])

    def add_log_event(event_type: str, details: str):
        event_log_data.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            "details": details,
            "distance": fmt_distance(state["head_distance"]),
            "light": str(state["head_light"]),
        })
        log_list.controls = [build_log_row(item) for item in event_log_data[:LOG_VISIBLE_ROWS]]
        page.update()

    def export_csv(e):
        if not event_log_data:
            show_snack("Журнал событий пуст!", ft.Colors.ORANGE_800)
            return

        filename = f"security_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            # utf-8-sig, чтобы Excel корректно открывал кириллицу
            with open(filename, mode="w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=["time", "type", "details", "distance", "light"])
                writer.writeheader()
                writer.writerows(event_log_data)
        except OSError as ex:
            show_snack(f"Ошибка записи файла: {ex}", ft.Colors.RED_800)
            return

        show_snack(f"Журнал экспортирован в файл: {filename}", ft.Colors.GREEN_800)

    # =========================================================================
    # PTZ: СЕТЕВЫЕ КОМАНДЫ К ESP32-S3
    # =========================================================================
    async def send_ptz_command(cmd: str):
        url = f"http://{state['camera_ip']}/cmd"
        try:
            session = await get_http()
            async with session.get(
                url,
                params={"c": cmd},
                timeout=aiohttp.ClientTimeout(total=0.6),
            ):
                pass
        except Exception:
            pass

    async def ptz_hold_worker():
        """Пока кнопка удерживается, периодически шлём команду. Один воркер на всё."""
        if state["ptz_running"]:
            return
        state["ptz_running"] = True
        try:
            while state["ptz_active_cmd"]:
                await send_ptz_command(state["ptz_active_cmd"])
                await asyncio.sleep(PTZ_REPEAT_SEC)
        finally:
            state["ptz_running"] = False

    def ptz_press(cmd: str):
        def handler(e):
            state["ptz_active_cmd"] = cmd
            page.run_task(ptz_hold_worker)
        return handler

    def ptz_release(e=None):
        state["ptz_active_cmd"] = None

    def ptz_click(cmd: str):
        def handler(e):
            page.run_task(send_ptz_command, cmd)
        return handler

    # =========================================================================
    # ТЕЛЕМЕТРИЯ И ВИДЕОПОТОК
    # =========================================================================
    async def telemetry_poll_task():
        """Опрос датчиков турели: дистанция, свет, углы сервоприводов."""
        while True:
            try:
                session = await get_http()
                url = f"http://{state['camera_ip']}/telemetry"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
                    if resp.status == 200:
                        data = parse_kv_line(await resp.text())
                        for key, state_key in (
                            ("DIST", "head_distance"),
                            ("LIGHT", "head_light"),
                            ("PAN", "head_pan"),
                            ("TILT", "head_tilt"),
                        ):
                            value = parse_int(data.get(key))
                            if value is not None:
                                state[state_key] = value

                        txt_head_dist.value = fmt_distance(state["head_distance"])
                        txt_head_light.value = f"{state['head_light']} ADC"
                        txt_head_pos.value = f"Pan: {state['head_pan']}° | Tilt: {state['head_tilt']}°"
                        page.update()
            except Exception:
                txt_head_dist.value = "нет связи"
                txt_head_light.value = "---"
                page.update()
            await asyncio.sleep(TELEMETRY_PERIOD_SEC)

    async def video_stream_task():
        """Получение кадров с камеры ESP32-S3 (snapshot)."""
        while True:
            if state["camera_streaming"]:
                try:
                    session = await get_http()
                    url = f"http://{state['camera_ip']}/snapshot"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=1.2)) as resp:
                        # Поток могли остановить, пока шёл запрос
                        if resp.status == 200 and state["camera_streaming"]:
                            data = await resp.read()
                            camera_view.src = "data:image/jpeg;base64," + base64.b64encode(data).decode("utf-8")
                            if not camera_view.visible:
                                camera_view.visible = True
                                camera_placeholder.visible = False
                                camera_placeholder.update()
                            camera_view.update()
                except Exception:
                    pass
            await asyncio.sleep(VIDEO_PERIOD_SEC)

    def refresh_stream_button():
        on = state["camera_streaming"]
        btn_stream.text = "Остановить видео" if on else "Запустить видео"
        btn_stream.icon = ft.Icons.VIDEOCAM_OFF if on else ft.Icons.VIDEOCAM
        btn_stream.bgcolor = ft.Colors.RED_700 if on else ft.Colors.BLUE_700

    def start_stream():
        state["camera_streaming"] = True
        txt_placeholder.value = "Подключение к камере..."
        refresh_stream_button()

    def toggle_camera_stream(e):
        if state["camera_streaming"]:
            state["camera_streaming"] = False
            camera_view.visible = False
            camera_placeholder.visible = True
            txt_placeholder.value = "Видеопоток выключен"
            refresh_stream_button()
        else:
            start_stream()
        page.update()

    # =========================================================================
    # ОТОБРАЖЕНИЕ СОСТОЯНИЯ ДВЕРИ И СИРЕНЫ
    # =========================================================================
    def refresh_door_ui():
        if state["door_open"]:
            card_door_status.bgcolor = ft.Colors.RED_900
            txt_door_status.value = "ДВЕРЬ ОТКРЫТА (ТРЕВОГА!)"
            txt_door_status.color = ft.Colors.RED_100
        else:
            card_door_status.bgcolor = ft.Colors.GREEN_900
            txt_door_status.value = "ДВЕРЬ ЗАКРЫТА"
            txt_door_status.color = ft.Colors.GREEN_100

    def refresh_alarm_badge():
        on = state["alarm_active"]
        txt_alarm.value = "СИРЕНА: ВКЛЮЧЕНА" if on else "СИРЕНА: ВЫКЛ"
        badge_alarm.bgcolor = ft.Colors.RED_600 if on else ft.Colors.GREY_800

    # =========================================================================
    # BLUETOOTH LOW ENERGY (BLE)
    # =========================================================================
    def ble_notification_handler(sender, data: bytearray):
        fields = parse_kv_line(bytes(data).decode("utf-8", errors="ignore"))
        was_open = state["door_open"]

        if "DOOR" in fields:
            state["door_open"] = fields["DOOR"] == "1"
        if "ALARM" in fields:
            state["alarm_active"] = fields["ALARM"] == "1"
        if fields.get("BTN") == "1":
            add_log_event("КНОПКА", "Физическая кнопка двери нажата")

        is_open = state["door_open"]
        if is_open and not was_open:
            add_log_event("ТРЕВОГА ДВЕРИ", "Геркон разомкнут! Открытие двери!")
            refresh_door_ui()
            # 1. Центрируем турель на дверь
            page.run_task(send_ptz_command, "C")
            # 2. Включаем видеопоток
            if not state["camera_streaming"]:
                start_stream()
        elif was_open and not is_open:
            add_log_event("НОРМА", "Дверь закрыта (геркон замкнут)")
            refresh_door_ui()

        refresh_alarm_badge()
        page.update()

    def ble_disconnected(_client):
        state["ble_connected"] = False
        btn_ble_connect.text = "Повторить BLE"
        btn_ble_connect.bgcolor = ft.Colors.RED_800
        btn_ble_connect.disabled = False
        add_log_event("ОШИБКА BLE", "Связь с DoorGuardian потеряна")

    async def ble_connect_task(e=None):
        nonlocal ble_client
        if state["ble_connected"]:
            return

        btn_ble_connect.disabled = True
        btn_ble_connect.text = "Поиск DoorGuardian..."
        page.update()

        client = None
        try:
            device = await BleakScanner.find_device_by_name(BLE_DEVICE_NAME, timeout=6.0)
            if device is None:
                raise RuntimeError("устройство не найдено в эфире")

            client = BleakClient(device, disconnected_callback=ble_disconnected)
            await client.connect()
            await client.start_notify(BLE_CHAR_TX_UUID, ble_notification_handler)

            ble_client = client
            state["ble_connected"] = True
            btn_ble_connect.text = "BLE Подключен"
            btn_ble_connect.bgcolor = ft.Colors.GREEN_700
            add_log_event("СВЯЗЬ", "BLE подключен к DoorGuardian")
        except Exception as ex:
            if client is not None and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            state["ble_connected"] = False
            btn_ble_connect.text = "Повторить BLE"
            btn_ble_connect.bgcolor = ft.Colors.RED_800
            add_log_event("ОШИБКА BLE", f"Сбой подключения: {ex}")
        finally:
            btn_ble_connect.disabled = False
            page.update()

    async def send_ble_command(cmd: str):
        if ble_client is None or not ble_client.is_connected:
            add_log_event("ОШИБКА BLE", f"Команда {cmd} не отправлена: BLE не подключен")
            return
        try:
            await ble_client.write_gatt_char(BLE_CHAR_RX_UUID, cmd.encode("utf-8"))
        except Exception as ex:
            add_log_event("ОШИБКА BLE", f"Не удалось передать команду: {ex}")

    async def reset_alarm_click(e):
        await send_ble_command("RESET")
        state["alarm_active"] = False
        refresh_alarm_badge()
        add_log_event("СБРОС", "Команда сброса тревоги отправлена")

    # =========================================================================
    # ДИАЛОГ НАСТРОЕК
    # =========================================================================
    tf_settings_ip = ft.TextField(label="IP-адрес ESP32-S3 Камеры", value=state["camera_ip"])
    tf_settings_prefix = ft.TextField(label="Префикс страны", value=state["phone_prefix"], width=100)
    tf_settings_phone = ft.TextField(label="Номер телефона для SMS", value=state["phone_number"], expand=True)
    slider_delay = ft.Slider(min=1, max=15, divisions=14, label="{value} сек", value=state["alarm_delay"])

    def close_settings(e=None):
        close_overlay(page, e)

    def save_settings(e):
        ip = tf_settings_ip.value.strip()
        if not ip:
            show_snack("IP-адрес камеры не может быть пустым", ft.Colors.RED_800)
            return
        state["camera_ip"] = ip
        state["phone_prefix"] = tf_settings_prefix.value.strip()
        state["phone_number"] = tf_settings_phone.value.strip()
        state["alarm_delay"] = int(slider_delay.value)
        close_settings()
        add_log_event("НАСТРОЙКИ", f"Конфигурация обновлена (IP: {state['camera_ip']})")

    settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Системные настройки комплекса"),
        content=ft.Column([
            tf_settings_ip,
            ft.Row([tf_settings_prefix, tf_settings_phone]),
            ft.Text("Задержка срабатывания тревоги:"),
            slider_delay,
        ], tight=True, spacing=12),
        actions=[
            ft.TextButton("Отмена", on_click=close_settings),
            ft.Button("Сохранить", on_click=save_settings, bgcolor=ft.Colors.GREEN_700),
        ],
    )

    def open_settings(e):
        tf_settings_ip.value = state["camera_ip"]
        tf_settings_prefix.value = state["phone_prefix"]
        tf_settings_phone.value = state["phone_number"]
        slider_delay.value = state["alarm_delay"]
        open_overlay(page, settings_dialog)

    # =========================================================================
    # ВИДЖЕТЫ ИНТЕРФЕЙСА
    # =========================================================================

    # 1. Шапка
    header_row = ft.Row([
        ft.Row([
            ft.Icon(ft.Icons.SECURITY, size=32, color=ft.Colors.BLUE_400),
            ft.Text("DoorGuardian & FPV Turret Core", size=22, weight=ft.FontWeight.BOLD),
        ]),
        ft.Row([
            ft.IconButton(ft.Icons.SETTINGS, on_click=open_settings, tooltip="Настройки"),
            ft.Button("Экспорт CSV", icon=ft.Icons.DOWNLOAD, on_click=export_csv),
        ]),
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True)

    # 2. Карточка статуса двери и сирены
    txt_door_status = ft.Text("ДВЕРЬ: ОЖИДАНИЕ СВЯЗИ", size=18, weight=ft.FontWeight.BOLD)
    txt_alarm = ft.Text("СИРЕНА: ВЫКЛ", weight=ft.FontWeight.BOLD)
    badge_alarm = ft.Container(content=txt_alarm, bgcolor=ft.Colors.GREY_800, padding=8, border_radius=6)
    btn_ble_connect = ft.Button("Подключить BLE", icon=ft.Icons.BLUETOOTH, on_click=ble_connect_task)

    card_door_status = ft.Container(
        content=ft.Row([
            txt_door_status,
            ft.Row([
                badge_alarm,
                btn_ble_connect,
                ft.Button("Сброс сирены", icon=ft.Icons.ALARM_OFF,
                                  bgcolor=ft.Colors.ORANGE_800, on_click=reset_alarm_click),
            ], spacing=8, wrap=True),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
        bgcolor=ft.Colors.GREY_900,
        padding=16,
        border_radius=10,
    )

    # 3. Карточка видеопотока FPV и телеметрии
    txt_placeholder = ft.Text("Видеопоток выключен", color=ft.Colors.GREY_400)
    camera_placeholder = ft.Container(
        content=ft.Column([
            ft.Icon(ft.Icons.VIDEOCAM_OFF, size=64, color=ft.Colors.GREY_600),
            txt_placeholder,
        ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        width=480,
        height=320,
        bgcolor="#73000000",
        border_radius=8,
        alignment=ft.Alignment(0, 0),
    )
    camera_view = ft.Image(src="", width=480, height=320, fit=ft.BoxFit.CONTAIN, visible=False, border_radius=8)

    txt_head_dist = ft.Text("---", size=16, weight=ft.FontWeight.BOLD)
    txt_head_light = ft.Text("---", size=16, weight=ft.FontWeight.BOLD)
    txt_head_pos = ft.Text(f"Pan: {state['head_pan']}° | Tilt: {state['head_tilt']}°",
                           size=16, weight=ft.FontWeight.BOLD)

    btn_stream = ft.Button(
        "Запустить видео",
        icon=ft.Icons.VIDEOCAM,
        bgcolor=ft.Colors.BLUE_700,
        on_click=toggle_camera_stream,
    )

    telemetry_column = ft.Column([
        ft.Text("Датчики Турели (Голова)", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_200),
        ft.Row([ft.Text("Дистанция (HC-SR04):", width=180), txt_head_dist]),
        ft.Row([ft.Text("Освещенность (A0):", width=180), txt_head_light]),
        ft.Row([ft.Text("Углы сервоприводов:", width=180), txt_head_pos]),
        ft.Divider(),
        btn_stream,
    ], spacing=10)

    fpv_card = ft.Container(
        content=ft.Row([
            ft.Stack([camera_placeholder, camera_view]),
            telemetry_column,
        ], spacing=20, wrap=True),
        bgcolor=ft.Colors.GREY_900,
        padding=16,
        border_radius=10,
    )

    # 4. Пульт управления PTZ (удержание для поворотов, клик для центрирования)
    def ptz_btn(icon, cmd: str, tooltip: str, hold: bool = True):
        visual = ft.Container(
            content=ft.Icon(icon, size=28, color=ft.Colors.WHITE),
            width=54,
            height=54,
            bgcolor=ft.Colors.BLUE_GREY_800,
            border_radius=8,
            alignment=ft.Alignment(0, 0),
            tooltip=tooltip,
        )
        if hold:
            return ft.GestureDetector(
                mouse_cursor=ft.MouseCursor.CLICK,
                on_tap_down=ptz_press(cmd),
                on_tap_up=ptz_release,
                on_exit=ptz_release,  # курсор ушёл с кнопки — останавливаем
                content=visual,
            )
        return ft.GestureDetector(
            mouse_cursor=ft.MouseCursor.CLICK,
            on_tap=ptz_click(cmd),
            content=visual,
        )

    ptz_pad = ft.Container(
        content=ft.Column([
            ft.Text("Управление Турелью (PTZ)", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_200),
            ft.Row([
                ft.Container(width=54),
                ptz_btn(ft.Icons.ARROW_UPWARD, "U", "Наклон вверх (удерживайте)"),
                ft.Container(width=54),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([
                ptz_btn(ft.Icons.ARROW_BACK, "L", "Поворот влево (удерживайте)"),
                ptz_btn(ft.Icons.CENTER_FOCUS_STRONG, "C", "Центрировать (165°/125°)", hold=False),
                ptz_btn(ft.Icons.ARROW_FORWARD, "R", "Поворот вправо (удерживайте)"),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([
                ft.Container(width=54),
                ptz_btn(ft.Icons.ARROW_DOWNWARD, "D", "Наклон вниз (удерживайте)"),
                ft.Container(width=54),
            ], alignment=ft.MainAxisAlignment.CENTER),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        bgcolor=ft.Colors.GREY_900,
        padding=16,
        border_radius=10,
        width=280,
    )

    # 5. Карточка SMS-модуля
    tf_test_sms = ft.TextField(
        label="Текст тревожного SMS",
        value="ВНИМАНИЕ! Сработала сигнализация двери!",
        multiline=True,
        min_lines=2,
    )

    def send_test_sms(e):
        # Реальная отправка через GSM-модем здесь не реализована — только журнал
        add_log_event(
            "SMS (ТЕСТ)",
            f"Номер {state['phone_prefix']}{state['phone_number']}: {tf_test_sms.value}",
        )

    sms_card = ft.Container(
        content=ft.Column([
            ft.Text("Модуль SMS-оповещений", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_200),
            tf_test_sms,
            ft.Button(
                "Тестовое SMS (в журнал)",
                icon=ft.Icons.SMS,
                bgcolor=ft.Colors.INDIGO_700,
                on_click=send_test_sms,
            ),
        ], spacing=10),
        bgcolor=ft.Colors.GREY_900,
        padding=16,
        border_radius=10,
        expand=True,
    )

    # 6. Журнал событий
    log_list = ft.Column(spacing=2, height=300, scroll=ft.ScrollMode.ADAPTIVE)

    log_card = ft.Container(
        content=ft.Column([
            ft.Text("Журнал безопасности и событий в реальном времени",
                    size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_200),
            log_list,
        ]),
        bgcolor=ft.Colors.GREY_900,
        padding=16,
        border_radius=10,
    )

    # =========================================================================
    # СБОРКА СТРАНИЦЫ И ЗАПУСК ФОНОВЫХ ЗАДАЧ
    # =========================================================================
    page.add(
        header_row,
        card_door_status,
        fpv_card,
        ft.Row([ptz_pad, sms_card], alignment=ft.MainAxisAlignment.START,
               vertical_alignment=ft.CrossAxisAlignment.START, wrap=True),
        log_card,
    )

    page.run_task(video_stream_task)
    page.run_task(telemetry_poll_task)
    add_log_event("СИСТЕМА", "Центральный пульт инициализирован. Готов к работе.")


if __name__ == "__main__":
    ft.run(main)
