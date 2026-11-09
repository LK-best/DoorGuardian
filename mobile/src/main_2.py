import asyncio
import base64
import csv
import io
import os
import time
from datetime import datetime

import aiohttp
import flet as ft
from bleak import BleakClient, BleakScanner

# =========================================================================
# КОНФИГУРАЦИЯ СИСТЕМЫ ПО УМОЛЧАНИЮ
# =========================================================================
DEFAULT_CAMERA_IP = "192.168.1.100"
BLE_SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
BLE_CHAR_TX_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"  # NOTIFY
BLE_CHAR_RX_UUID = "6d68efe5-04b6-4a85-abc4-c2670b7bf7fd"  # WRITE

DEFAULT_PHONE_PREFIX = "+7"
DEFAULT_PHONE_NUMBER = "9991234567"
DEFAULT_ALARM_DELAY_SEC = 3


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
        "ptz_holding": False,
        "ptz_active_cmd": None,
    }

    ble_client: BleakClient = None
    event_log_data = []

    # =========================================================================
    # ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ЛОГИРОВАНИЯ И ЭКСПОРТА
    # =========================================================================
    def add_log_event(event_type: str, details: str):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        event_log_data.insert(0, {
            "time": now_str,
            "type": event_type,
            "details": details,
            "distance": f"{state['head_distance']} см",
            "light": f"{state['head_light']}"
        })
        # Обновление таблицы в UI
        rows = []
        for item in event_log_data[:25]:
            color = ft.colors.RED_400 if "ТРЕВОГА" in item["type"] else ft.colors.WHITE
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(item["time"], size=12)),
                ft.DataCell(ft.Text(item["type"], color=color, weight=ft.FontWeight.BOLD, size=12)),
                ft.DataCell(ft.Text(item["details"], size=12)),
                ft.DataCell(ft.Text(item["distance"], size=12)),
                ft.DataCell(ft.Text(item["light"], size=12)),
            ]))
        log_table.rows = rows
        page.update()

    def export_csv(e):
        if not event_log_data:
            page.snack_bar = ft.SnackBar(ft.Text("Журнал событий пуст!"), bgcolor=ft.colors.ORANGE_800)
            page.snack_bar.open = True
            page.update()
            return

        filename = f"security_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, mode="w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["time", "type", "details", "distance", "light"])
            writer.writeheader()
            writer.writerows(event_log_data)

        page.snack_bar = ft.SnackBar(
            ft.Text(f"Журнал успешно экспортирован в файл: {filename}"),
            bgcolor=ft.colors.GREEN_800
        )
        page.snack_bar.open = True
        page.update()

    # =========================================================================
    # СЕТЕВЫЕ ЗАПРОСЫ К ESP32-S3 (PTZ & ТЕЛЕМЕТРИЯ)
    # =========================================================================
    async def send_ptz_command(cmd: str):
        url = f"http://{state['camera_ip']}/cmd?c={cmd}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=0.6) as resp:
                    if resp.status == 200:
                        pass
        except Exception:
            pass

    async def ptz_hold_worker():
        """Непрерывное перемещение сервоприводов при удержании кнопки."""
        while state["ptz_holding"] and state["ptz_active_cmd"]:
            await send_ptz_command(state["ptz_active_cmd"])
            await asyncio.sleep(0.12)

    def on_ptz_down(cmd: str):
        def handler(e):
            state["ptz_holding"] = True
            state["ptz_active_cmd"] = cmd
            asyncio.create_task(ptz_hold_worker())

        return handler

    def on_ptz_up(e):
        state["ptz_holding"] = False
        state["ptz_active_cmd"] = None

    async def telemetry_poll_task():
        """Периодический опрос датчиков турели (дистанция, свет, углы)."""
        while True:
            if state["camera_streaming"] or state["camera_ip"]:
                url = f"http://{state['camera_ip']}/telemetry"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=1.0) as resp:
                            if resp.status == 200:
                                text = await resp.text()
                                # Разбор формата DIST=XX;LIGHT=YY;PAN=ZZ;TILT=WW
                                parts = text.strip().split(";")
                                for p in parts:
                                    if "=" in p:
                                        k, v = p.split("=", 1)
                                        if k == "DIST":
                                            state["head_distance"] = int(v)
                                        elif k == "LIGHT":
                                            state["head_light"] = int(v)
                                        elif k == "PAN":
                                            state["head_pan"] = int(v)
                                        elif k == "TILT":
                                            state["head_tilt"] = int(v)

                                # Обновление плашек в UI
                                dist_text = f"{state['head_distance']} см" if state['head_distance'] > 0 else "---"
                                txt_head_dist.value = dist_text
                                txt_head_light.value = f"{state['head_light']} ADC"
                                txt_head_pos.value = f"Pan: {state['head_pan']}° | Tilt: {state['head_tilt']}°"
                                page.update()
                except Exception:
                    pass
            await asyncio.sleep(0.4)

    # =========================================================================
    # ВИДЕОПОТОК (MJPEG / SNAPSHOT STREAMER)
    # =========================================================================
    async def video_stream_task():
        """Потоковый граббер кадров с камеры ESP32-S3."""
        while True:
            if state["camera_streaming"]:
                url = f"http://{state['camera_ip']}/snapshot"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=1.2) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                b64 = base64.b64encode(data).decode("utf-8")
                                camera_view.src_base64 = b64
                                camera_view.visible = True
                                camera_placeholder.visible = False
                                page.update()
                except Exception:
                    pass
            await asyncio.sleep(0.06)

    def toggle_camera_stream(e):
        state["camera_streaming"] = not state["camera_streaming"]
        btn_stream.text = "Остановить видео" if state["camera_streaming"] else "Запустить видео"
        btn_stream.icon = ft.icons.VIDEOCAM_OFF if state["camera_streaming"] else ft.icons.VIDEOCAM
        btn_stream.bgcolor = ft.colors.RED_700 if state["camera_streaming"] else ft.colors.BLUE_700
        if not state["camera_streaming"]:
            camera_view.visible = False
            camera_placeholder.visible = True
        page.update()

    # =========================================================================
    # BLUETOOTH LOW ENERGY (BLE) КЛИЕНТ
    # =========================================================================
    def ble_notification_handler(sender, data: bytearray):
        raw = data.decode("utf-8", errors="ignore").strip()
        # Парсинг: DOOR=1;ALARM=1;BTN=0
        pairs = raw.split(";")
        old_door = state["door_open"]
        for pair in pairs:
            if "=" in pair:
                key, val = pair.split("=", 1)
                val = val.strip()
                if key == "DOOR":
                    state["door_open"] = (val == "1")
                elif key == "ALARM":
                    state["alarm_active"] = (val == "1")
                elif key == "BTN":
                    if val == "1":
                        add_log_event("КНОПКА", "Физическая кнопка двери нажата")

        # СОБЫТИЕ ТРЕВОГИ: Дверь открыта (DOOR=1)
        if state["door_open"] and not old_door:
            add_log_event("ТРЕВОГА ДВЕРИ", "Геркон разомкнут! Открытие двери!")
            card_door_status.bgcolor = ft.colors.RED_900
            txt_door_status.value = "ДВЕРЬ ОТКРЫТА (ТРЕВОГА!)"
            txt_door_status.color = ft.colors.RED_100

            # 1. Автоматическое центрирование турели на дверь
            asyncio.create_task(send_ptz_command("C"))
            # 2. Автоматическое включение видеопотока
            if not state["camera_streaming"]:
                state["camera_streaming"] = True
                btn_stream.text = "Остановить видео"
                btn_stream.icon = ft.icons.VIDEOCAM_OFF
                btn_stream.bgcolor = ft.colors.RED_700

        elif not state["door_open"] and old_door:
            add_log_event("НОРМА", "Дверь закрыта (геркон замкнут)")
            card_door_status.bgcolor = ft.colors.GREEN_900
            txt_door_status.value = "ДВЕРЬ ЗАКРЫТА"
            txt_door_status.color = ft.colors.GREEN_100

        # Статус сирены
        badge_alarm.text = "СИРЕНА: ВКЛЮЧЕНА" if state["alarm_active"] else "СИРЕНА: ВЫКЛ"
        badge_alarm.bgcolor = ft.colors.RED_600 if state["alarm_active"] else ft.colors.GREY_800
        page.update()

    async def ble_connect_task(e=None):
        nonlocal ble_client
        btn_ble_connect.disabled = True
        btn_ble_connect.text = "Поиск DoorGuardian..."
        page.update()

        try:
            device = await BleakScanner.find_by_name("DoorGuardian", timeout=6.0)
            if not device:
                add_log_event("ОШИБКА BLE", "Устройство DoorGuardian не найдено в эфире")
                btn_ble_connect.text = "Подключить BLE"
                btn_ble_connect.disabled = False
                page.update()
                return

            ble_client = BleakClient(device)
            await ble_client.connect()
            state["ble_connected"] = True

            await ble_client.start_notify(BLE_CHAR_TX_UUID, ble_notification_handler)

            add_log_event("СВЯЗЬ", "BLE подключен к DoorGuardian")
            btn_ble_connect.text = "BLE Подключен"
            btn_ble_connect.bgcolor = ft.colors.GREEN_700
            btn_ble_connect.disabled = False
            page.update()
        except Exception as ex:
            add_log_event("ОШИБКА BLE", f"Сбой подключения: {str(ex)}")
            btn_ble_connect.text = "Повторить BLE"
            btn_ble_connect.bgcolor = ft.colors.RED_800
            btn_ble_connect.disabled = False
            state["ble_connected"] = False
            page.update()

    async def send_ble_command(cmd: str):
        if ble_client and ble_client.is_connected:
            try:
                await ble_client.write_gatt_char(BLE_CHAR_RX_UUID, cmd.encode("utf-8"))
            except Exception as ex:
                add_log_event("ОШИБКА BLE", f"Не удалось передать команду: {str(ex)}")

    async def reset_alarm_click(e):
        await send_ble_command("RESET")
        state["alarm_active"] = False
        badge_alarm.text = "СИРЕНА: ВЫКЛ"
        badge_alarm.bgcolor = ft.colors.GREY_800
        add_log_event("СБРОС", "Команда сброса тревоги отправлена")
        page.update()

    # =========================================================================
    # ДИАЛОГ НАСТРОЕК (SETTINGS DIALOG)
    # =========================================================================
    tf_settings_ip = ft.TextField(label="IP-адрес ESP32-S3 Камеры", value=state["camera_ip"])
    tf_settings_prefix = ft.TextField(label="Префикс страны", value=state["phone_prefix"], width=100)
    tf_settings_phone = ft.TextField(label="Номер телефона для SMS", value=state["phone_number"], expand=True)
    slider_delay = ft.Slider(min=1, max=15, divisions=14, label="{value} сек", value=state["alarm_delay"])

    def save_settings(e):
        state["camera_ip"] = tf_settings_ip.value.strip()
        state["phone_prefix"] = tf_settings_prefix.value.strip()
        state["phone_number"] = tf_settings_phone.value.strip()
        state["alarm_delay"] = int(slider_delay.value)
        settings_dialog.open = False
        add_log_event("НАСТРОЙКИ", f"Конфигурация обновлена (IP: {state['camera_ip']})")
        page.update()

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
            ft.TextButton("Отмена", on_click=lambda e: setattr(settings_dialog, "open", False) or page.update()),
            ft.ElevatedButton("Сохранить", on_click=save_settings, bgcolor=ft.colors.GREEN_700),
        ],
    )
    page.dialog = settings_dialog

    def open_settings(e):
        tf_settings_ip.value = state["camera_ip"]
        tf_settings_prefix.value = state["phone_prefix"]
        tf_settings_phone.value = state["phone_number"]
        slider_delay.value = state["alarm_delay"]
        settings_dialog.open = True
        page.update()

    # =========================================================================
    # ВИДЖЕТЫ ИНТЕРФЕЙСА (UI LAYOUT)
    # =========================================================================

    # 1. Шапка
    header_row = ft.Row([
        ft.Row([
            ft.Icon(ft.icons.SECURITY, size=32, color=ft.colors.BLUE_400),
            ft.Text("DoorGuardian & FPV Turret Core", size=22, weight=ft.FontWeight.BOLD),
        ]),
        ft.Row([
            ft.IconButton(ft.icons.SETTINGS, on_click=open_settings, tooltip="Настройки"),
            ft.ElevatedButton("Экспорт CSV", icon=ft.icons.DOWNLOAD, on_click=export_csv),
        ])
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    # 2. Карточка статуса двери и сирены
    txt_door_status = ft.Text("ДВЕРЬ: ОЖИДАНИЕ СВЯЗИ", size=18, weight=ft.FontWeight.BOLD)
    card_door_status = ft.Container(
        content=ft.Row([
            txt_door_status,
            ft.Row([
                badge_alarm := ft.Container(
                    content=ft.Text("СИРЕНА: ВЫКЛ", weight=ft.FontWeight.BOLD),
                    bgcolor=ft.colors.GREY_800,
                    padding=8,
                    border_radius=6
                ),
                btn_ble_connect := ft.ElevatedButton("Подключить BLE", icon=ft.icons.BLUETOOTH,
                                                     on_click=ble_connect_task),
                ft.ElevatedButton("Сброс сирены", icon=ft.icons.ALARM_OFF, bgcolor=ft.colors.ORANGE_800,
                                  on_click=reset_alarm_click),
            ], spacing=8)
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        bgcolor=ft.colors.GREY_900,
        padding=16,
        border_radius=10,
    )

    # 3. Карточка видеопотока FPV и Телеметрии
    camera_placeholder = ft.Container(
        content=ft.Column([
            ft.Icon(ft.icons.VIDEOCAM_OFF, size=64, color=ft.colors.GREY_600),
            ft.Text("Видеопоток выключен", color=ft.colors.GREY_400),
        ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        width=480,
        height=320,
        bgcolor=ft.colors.BLACK45,
        border_radius=8,
        alignment=ft.alignment.center
    )
    camera_view = ft.Image(width=480, height=320, fit=ft.ImageFit.CONTAIN, visible=False, border_radius=8)

    txt_head_dist = ft.Text("---", size=16, weight=ft.FontWeight.BOLD)
    txt_head_light = ft.Text("---", size=16, weight=ft.FontWeight.BOLD)
    txt_head_pos = ft.Text("Pan: 165° | Tilt: 125°", size=16, weight=ft.FontWeight.BOLD)

    telemetry_column = ft.Column([
        ft.Text("Датчики Турели (Голова)", size=16, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_200),
        ft.Row([ft.Text("Дистанция (HC-SR04):", width=180), txt_head_dist]),
        ft.Row([ft.Text("Освещенность (A0):", width=180), txt_head_light]),
        ft.Row([ft.Text("Углы сервоприводов:", width=180), txt_head_pos]),
        ft.Divider(),
        btn_stream := ft.ElevatedButton(
            "Запустить видео",
            icon=ft.icons.VIDEOCAM,
            bgcolor=ft.colors.BLUE_700,
            on_click=toggle_camera_stream
        ),
    ], spacing=10)

    fpv_card = ft.Container(
        content=ft.Row([
            ft.Column([
                ft.Stack([camera_placeholder, camera_view]),
            ]),
            telemetry_column
        ], spacing=20, alignment=ft.MainAxisAlignment.START),
        bgcolor=ft.colors.GREY_900,
        padding=16,
        border_radius=10,
    )

    # 4. Пульт управления PTZ (Инверсия Tilt + Hold)
    def ptz_btn(icon, cmd, tooltip):
        btn = ft.Container(
            content=ft.Icon(icon, size=28, color=ft.colors.WHITE),
            width=54,
            height=54,
            bgcolor=ft.colors.BLUE_GREY_800,
            border_radius=8,
            alignment=ft.alignment.center,
            tooltip=tooltip,
            on_click=lambda e: asyncio.create_task(send_ptz_command(cmd)),
        )
        return btn

    ptz_pad = ft.Container(
        content=ft.Column([
            ft.Text("Управление Турелью (PTZ)", size=16, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_200),
            ft.Row([
                ft.Container(width=54),
                ptz_btn(ft.icons.ARROW_UPWARD, "U", "Наклон вверх (D11 уменьшить)"),
                ft.Container(width=54),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([
                ptz_btn(ft.icons.ARROW_BACK, "L", "Поворот влево (D10)"),
                ptz_btn(ft.icons.CENTER_FOCUS_STRONG, "C", "Центрировать (165°/125°)"),
                ptz_btn(ft.icons.ARROW_FORWARD, "R", "Поворот вправо (D10)"),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Row([
                ft.Container(width=54),
                ptz_btn(ft.icons.ARROW_DOWNWARD, "D", "Наклон вниз (D11 увеличить)"),
                ft.Container(width=54),
            ], alignment=ft.MainAxisAlignment.CENTER),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        bgcolor=ft.colors.GREY_900,
        padding=16,
        border_radius=10,
        width=280
    )

    # 5. Карточка SMS-модуля
    tf_test_sms = ft.TextField(label="Текст тревожного SMS", value="ВНИМАНИЕ! Сработала сигнализация двери!",
                               multiline=True, min_lines=2)
    sms_card = ft.Container(
        content=ft.Column([
            ft.Text("Модуль SMS-оповещений", size=16, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_200),
            tf_test_sms,
            ft.ElevatedButton(
                "Отправить тестовое SMS",
                icon=ft.icons.SMS,
                bgcolor=ft.colors.INDIGO_700,
                on_click=lambda e: add_log_event("SMS",
                                                 f"Отправка на {state['phone_prefix']}{state['phone_number']}: {tf_test_sms.value}")
            )
        ], spacing=10),
        bgcolor=ft.colors.GREY_900,
        padding=16,
        border_radius=10,
        expand=True
    )

    # 6. Журнал событий (Таблица)
    log_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Время", weight=ft.FontWeight.BOLD)),
            ft.DataColumn(ft.Text("Событие", weight=ft.FontWeight.BOLD)),
            ft.DataColumn(ft.Text("Детали", weight=ft.FontWeight.BOLD)),
            ft.DataColumn(ft.Text("Дистанция", weight=ft.FontWeight.BOLD)),
            ft.DataColumn(ft.Text("Свет", weight=ft.FontWeight.BOLD)),
        ],
        rows=[],
        border=ft.border.all(1, ft.colors.GREY_800),
        heading_row_color=ft.colors.GREY_800,
    )

    log_card = ft.Container(
        content=ft.Column([
            ft.Text("Журнал безопасности и событий в реальном времени", size=16, weight=ft.FontWeight.BOLD,
                    color=ft.colors.BLUE_200),
            log_table,
        ], scroll=ft.ScrollMode.ADAPTIVE),
        bgcolor=ft.colors.GREY_900,
        padding=16,
        border_radius=10,
    )

    # Сборка страницы
    page.add(
        header_row,
        card_door_status,
        fpv_card,
        ft.Row([ptz_pad, sms_card], alignment=ft.MainAxisAlignment.START,
               vertical_alignment=ft.CrossAxisAlignment.START),
        log_card
    )

    # Старт фоновых асинхронных задач
    asyncio.create_task(video_stream_task())
    asyncio.create_task(telemetry_poll_task())
    add_log_event("СИСТЕМА", "Центральный пульт инициализирован. Готов к работе.")


if __name__ == "__main__":
    ft.app(target=main)