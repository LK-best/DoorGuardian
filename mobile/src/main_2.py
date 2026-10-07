import flet as ft
import datetime
import csv
import io
import codecs
import theme
from bleak import BleakClient, BleakScanner

# ==================== BLE-ПРОФИЛЬ (должен совпадать с прошивкой ESP32) ====================
DEVICE_NAME = "DoorGuardian"
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
TX_CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"  # ESP32 -> приложение (notify)
RX_CHAR_UUID = "6d68efe5-04b6-4a85-abc4-c2670b7bf7fd"  # приложение -> ESP32 (write)


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def make_border(color: str, width: float = 1.0) -> ft.Border:
    side = ft.BorderSide(width, color)
    return ft.Border(top=side, bottom=side, left=side, right=side)


def make_padding(horizontal: float, vertical: float) -> ft.Padding:
    return ft.Padding(horizontal, vertical, horizontal, vertical)


def main(page: ft.Page):
    page.title = "DoorGuardian"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = theme.BACKGROUND
    page.padding = 20
    page.scroll = ft.ScrollMode.ADAPTIVE
    page.window_width = 400
    page.window_height = 800

    page.theme = ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=theme.PRIMARY,
            surface=theme.CARD,
            surface_tint=ft.Colors.TRANSPARENT,
        )
    )

    # ---------- Состояние ----------
    demo_state = {"door_open": False, "armed": False}
    # Настройки, которые меняются через окно «Настройки»
    config = {
        "prefix": "+7",     # неизменяемый префикс номера (меняется только в настройках)
        "delay_min": 5,     # мин. значение ползунка
        "delay_max": 60,    # макс. значение ползунка
    }
    conn = {
        "client": None,
        "buf": "",
        "busy": False,
        # инкрементальный декодер: кириллица из LOG-строк может разорваться между BLE-пакетами
        "decoder": codecs.getincrementaldecoder("utf-8")(errors="ignore"),
    }
    log_data = []

    # ==================== REFS ====================
    door_title_ref = ft.Ref[ft.Text]()
    door_subtitle_ref = ft.Ref[ft.Text]()
    door_icon_ref = ft.Ref[ft.Icon]()
    status_card_ref = ft.Ref[ft.Container]()

    arm_btn_text_ref = ft.Ref[ft.Text]()
    arm_btn_icon_ref = ft.Ref[ft.Icon]()

    battery_text_ref = ft.Ref[ft.Text]()
    battery_bar_ref = ft.Ref[ft.ProgressBar]()

    port_dd_ref = ft.Ref[ft.Dropdown]()
    conn_btn_text_ref = ft.Ref[ft.Text]()
    conn_btn_ref = ft.Ref[ft.Container]()
    conn_badge_text_ref = ft.Ref[ft.Text]()
    conn_badge_ref = ft.Ref[ft.Container]()

    prefix_text_ref = ft.Ref[ft.Text]()
    phone_field_ref = ft.Ref[ft.TextField]()
    sound_delay_ref = ft.Ref[ft.Slider]()
    sound_delay_text_ref = ft.Ref[ft.Text]()
    event_log_ref = ft.Ref[ft.Column]()

    # ==================== ЖУРНАЛ ====================
    def add_event(message: str):
        if not event_log_ref.current:
            return

        now = datetime.datetime.now()
        log_data.append([now.strftime("%Y-%m-%d %H:%M:%S"), message])

        event_log_ref.current.controls.insert(
            0,
            ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Text(now.strftime("%H:%M:%S"), size=11, color=theme.MUTED_TEXT,
                                        weight=ft.FontWeight.W_500),
                        bgcolor=theme.BACKGROUND,
                        padding=make_padding(8, 4),
                        border_radius=6,
                    ),
                    ft.Text(message, size=13, color=theme.TEXT, weight=ft.FontWeight.W_400, expand=True),
                ], alignment=ft.MainAxisAlignment.START),
                padding=make_padding(0, 3),
            )
        )
        if len(event_log_ref.current.controls) > 50:
            event_log_ref.current.controls.pop()

        page.update()

    def show_snack(text: str):
        page.show_dialog(
            ft.SnackBar(content=ft.Text(text, color=theme.TEXT), bgcolor=theme.CARD)
        )

    # ==================== ЭКСПОРТ CSV ====================
    # FilePicker в Flet 1.0 — это сервис: регистрируется в page.services (не в overlay!)
    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    async def export_csv(e):
        try:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Timestamp", "Event"])
            writer.writerows(log_data)
            data = buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel читал кириллицу

            # На телефоне и в вебе файл записывает сам пикер, ему нужны байты.
            # На десктопе пикер возвращает только путь — пишем файл сами.
            is_mobile_or_web = page.web or page.platform in (
                ft.PagePlatform.ANDROID, ft.PagePlatform.IOS
            )

            path = await file_picker.save_file(
                dialog_title="Сохранить журнал событий",
                file_name="door_log.csv",
                allowed_extensions=["csv"],
                src_bytes=data if is_mobile_or_web else None,
            )

            if not is_mobile_or_web and path:
                with open(path, "wb") as f:
                    f.write(data)

            if path or is_mobile_or_web:
                add_event("Журнал экспортирован")
        except Exception as err:
            add_event(f"Ошибка экспорта: {err}")

    # ==================== BLUETOOTH (BLE) ====================
    def is_connected() -> bool:
        client = conn["client"]
        return bool(client and client.is_connected)

    async def _write_async(data: bytes):
        client = conn["client"]
        if not client:
            return
        try:
            # BLE-пакет по умолчанию ~20 байт — режем команду на куски
            for i in range(0, len(data), 20):
                await client.write_gatt_char(RX_CHAR_UUID, data[i:i + 20], response=False)
        except Exception as err:
            add_event(f"Ошибка отправки: {err}")

    def send_command(cmd: str) -> bool:
        """Синхронная обёртка: ставит отправку в очередь event loop Flet."""
        if not is_connected():
            return False
        page.run_task(_write_async, (cmd + "\n").encode())
        return True

    def apply_door_state(is_open: bool):
        demo_state["door_open"] = is_open
        if not (door_title_ref.current and door_subtitle_ref.current
                and door_icon_ref.current and status_card_ref.current):
            return
        if is_open:
            door_title_ref.current.value = "ДВЕРЬ ОТКРЫТА"
            door_subtitle_ref.current.value = "Внимание: Зафиксирован проход"
            door_icon_ref.current.name = ft.Icons.DOOR_SLIDING_OUTLINED
            door_icon_ref.current.color = theme.DANGER
            status_card_ref.current.bgcolor = theme.DANGER_BG
            status_card_ref.current.border = make_border(theme.DANGER)
            add_event("⚠️ Дверь открыта")
        else:
            door_title_ref.current.value = "ДВЕРЬ ЗАКРЫТА"
            door_subtitle_ref.current.value = "Периметр защищен"
            door_icon_ref.current.name = ft.Icons.DOOR_SLIDING
            door_icon_ref.current.color = theme.SUCCESS
            status_card_ref.current.bgcolor = theme.SUCCESS_BG
            status_card_ref.current.border = make_border(theme.SUCCESS)
            add_event("🔒 Дверь закрыта")
        page.update()

    def parse_telemetry(line: str):
        # Пример: STAT:DOOR=1,STATE=2,BAT=3.95
        if line.startswith("LOG:"):
            add_event(line[4:].strip())
            return
        if not line.startswith("STAT:"):
            return
        try:
            params = dict(item.split("=") for item in line[5:].split(","))
            if "DOOR" in params:
                new_open = params["DOOR"] == "1"
                if new_open != demo_state["door_open"]:
                    apply_door_state(new_open)
            if "BAT" in params and battery_bar_ref.current and battery_text_ref.current:
                v = float(params["BAT"])
                pct = max(0, min(100, int((v - 3.0) / (4.2 - 3.0) * 100)))
                battery_bar_ref.current.value = pct / 100
                battery_text_ref.current.value = f"{pct}% ({v:.2f} V)"
                page.update()
        except Exception:
            pass

    def on_notify(_, data: bytearray):
        # Данные приходят кусками по ~20 байт — склеиваем до символа конца строки
        conn["buf"] += conn["decoder"].decode(bytes(data))
        while "\n" in conn["buf"]:
            line, conn["buf"] = conn["buf"].split("\n", 1)
            line = line.strip()
            if line:
                parse_telemetry(line)

    def on_ble_disconnected(_client):
        # Вызывается, если ESP32 пропала сама (выключили, ушла из зоны)
        if conn["client"] is not None:
            conn["client"] = None
            conn["buf"] = ""
            set_conn_ui(False)
            page.update()
            add_event("🔌 Связь потеряна")

    def set_conn_ui(connected: bool, port_name: str = ""):
        if connected:
            conn_btn_text_ref.current.value = "Отключиться"
            conn_btn_ref.current.bgcolor = theme.DANGER
            conn_badge_text_ref.current.value = f"Online · {port_name}"
            conn_badge_text_ref.current.color = theme.SUCCESS
            conn_badge_ref.current.bgcolor = theme.SUCCESS_BG
            conn_badge_ref.current.border = make_border(theme.SUCCESS)
        else:
            conn_btn_text_ref.current.value = "Подключиться"
            conn_btn_ref.current.bgcolor = theme.SUCCESS
            conn_badge_text_ref.current.value = "Offline"
            conn_badge_text_ref.current.color = theme.DANGER
            conn_badge_ref.current.bgcolor = theme.DANGER_BG
            conn_badge_ref.current.border = make_border(theme.DANGER)

    async def disconnect(silent: bool = True, reason: str = "Отключено от контроллера"):
        client = conn["client"]
        conn["client"] = None  # чтобы on_ble_disconnected не дублировал сообщение
        conn["buf"] = ""
        try:
            if client and client.is_connected:
                await client.disconnect()
        except Exception:
            pass
        set_conn_ui(False)
        page.update()
        if not silent:
            add_event(f"🔌 {reason}")

    async def connect(address: str, name: str):
        conn["busy"] = True
        conn_btn_text_ref.current.value = "Подключение..."
        page.update()
        try:
            client = BleakClient(address, disconnected_callback=on_ble_disconnected)
            await client.connect()
            await client.start_notify(TX_CHAR_UUID, on_notify)
            conn["client"] = client
            set_conn_ui(True, name)
            page.update()
            add_event(f"🔌 Подключено к {name}")
            send_command("CMD:STATUS")  # запросить текущее состояние
        except Exception as err:
            set_conn_ui(False)
            page.update()
            add_event(f"Не удалось подключиться: {err}")
            show_snack("Не удалось подключиться")
        finally:
            conn["busy"] = False

    async def on_connect_click(e):
        if conn["busy"]:
            return
        if is_connected():
            await disconnect(silent=False)
            return
        dd = port_dd_ref.current
        address = dd.value
        if not address:
            show_snack("Сначала найдите устройство (кнопка ⟳)")
            return
        name = next((o.text for o in dd.options if o.key == address), address)
        await connect(address, name)

    async def refresh_ports(e=None):
        """Сканирование BLE-устройств поблизости (~5 с)."""
        if conn["busy"]:
            return
        conn["busy"] = True
        add_event("🔍 Поиск Bluetooth-устройств...")
        dd = port_dd_ref.current
        try:
            found = await BleakScanner.discover(timeout=5.0, return_adv=True)
            devices = []
            for address, (dev, adv) in found.items():
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                ours = SERVICE_UUID in uuids or (dev.name or "").startswith(DEVICE_NAME)
                if ours or dev.name:  # безымянные устройства не показываем
                    devices.append((not ours, dev.name or "?", address, ours))
            devices.sort()  # наши устройства — первыми

            dd.options = [
                ft.DropdownOption(
                    key=address,
                    text=f"{'🛡 ' if ours else ''}{name} · {address}",
                )
                for _, name, address, ours in devices
            ]
            keys = [d[2] for d in devices]
            if dd.value not in keys:
                dd.value = keys[0] if keys and devices[0][3] else None
            page.update()
            add_event(f"Найдено устройств: {len(devices)}")
        except Exception as err:
            add_event(f"Ошибка сканирования: {err}")
        finally:
            conn["busy"] = False

    # ==================== ОБРАБОТЧИКИ UI ====================
    def toggle_door(e):
        # Тестовая кнопка: просто переключает отображение
        apply_door_state(not demo_state["door_open"])

    def toggle_guard(e):
        demo_state["armed"] = not demo_state["armed"]
        if demo_state["armed"]:
            arm_btn_text_ref.current.value = "СНЯТЬ С ОХРАНЫ"
            arm_btn_icon_ref.current.name = ft.Icons.SHIELD_OUTLINED
            send_command("CMD:ARM")
            add_event("🛡️ Система поставлена на охрану")
        else:
            arm_btn_text_ref.current.value = "ПОСТАВИТЬ НА ОХРАНУ"
            arm_btn_icon_ref.current.name = ft.Icons.SHIELD_MOON
            send_command("CMD:DISARM")
            add_event("🔓 Система снята с охраны")
        page.update()

    def on_slider_change(e):
        sound_delay_text_ref.current.value = f"{int(e.control.value)} сек"
        page.update()

    def save_settings(e):
        digits = (phone_field_ref.current.value or "").strip()
        prefix_digits = len(config["prefix"]) - 1  # без '+'
        need = 11 if config["prefix"] == "+7" else 7
        need_digits = need - prefix_digits if config["prefix"] == "+7" else 7

        if not digits.isdigit() or len(digits) < need_digits:
            phone_field_ref.current.error_text = f"Минимум {need_digits} цифр"
            page.update()
            return

        phone_field_ref.current.error_text = None
        phone = config["prefix"] + digits
        delay_val = int(sound_delay_ref.current.value)

        send_command(f"SET:PHONE={phone},T1={delay_val}")
        add_event(f"⚙️ Сохранено: {phone}, задержка {delay_val}с")
        show_snack("Настройки применены")

    # ==================== ОКНО НАСТРОЕК ====================
    def apply_prefix_to_ui():
        prefix_text_ref.current.value = config["prefix"]
        # E.164: всего не больше 15 цифр
        phone_field_ref.current.max_length = max(4, 15 - (len(config["prefix"]) - 1))

    def apply_slider_range():
        s = sound_delay_ref.current
        s.min = config["delay_min"]
        s.max = config["delay_max"]
        s.divisions = config["delay_max"] - config["delay_min"]
        s.value = max(s.min, min(s.max, s.value))
        sound_delay_text_ref.current.value = f"{int(s.value)} сек"

    def open_settings(e):
        prefix_f = ft.TextField(
            label="Начало номера (код страны)",
            value=config["prefix"],
            text_size=14,
            bgcolor=theme.INPUT_BG,
            max_length=5,
            border=ft.OutlineInputBorder(border_radius=10, side=ft.BorderSide(1, theme.CARD_BORDER)),
        )
        min_f = ft.TextField(
            label="Мин. задержка, сек",
            value=str(config["delay_min"]),
            text_size=14,
            bgcolor=theme.INPUT_BG,
            input_filter=ft.NumbersOnlyInputFilter(),
            max_length=3,
            expand=True,
            border=ft.OutlineInputBorder(border_radius=10, side=ft.BorderSide(1, theme.CARD_BORDER)),
        )
        max_f = ft.TextField(
            label="Макс. задержка, сек",
            value=str(config["delay_max"]),
            text_size=14,
            bgcolor=theme.INPUT_BG,
            input_filter=ft.NumbersOnlyInputFilter(),
            max_length=3,
            expand=True,
            border=ft.OutlineInputBorder(border_radius=10, side=ft.BorderSide(1, theme.CARD_BORDER)),
        )

        def close_dialog(_=None):
            page.pop_dialog()

        def apply_dialog(_=None):
            pfx = (prefix_f.value or "").strip()
            if not (pfx.startswith("+") and pfx[1:].isdigit() and 1 <= len(pfx[1:]) <= 4):
                prefix_f.error_text = "Формат: +7, +380, +1 ..."
                page.update()
                return
            prefix_f.error_text = None

            try:
                mn, mx = int(min_f.value), int(max_f.value)
            except (TypeError, ValueError):
                min_f.error_text = "Число"
                page.update()
                return
            if mn < 1 or mx <= mn:
                max_f.error_text = "Макс. должно быть больше мин."
                page.update()
                return

            config["prefix"] = pfx
            config["delay_min"], config["delay_max"] = mn, mx
            apply_prefix_to_ui()
            apply_slider_range()
            page.pop_dialog()
            add_event(f"⚙️ Префикс {pfx}, диапазон {mn}–{mx} сек")

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=theme.CARD,
            title=ft.Text("Настройки", color=theme.TEXT, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                width=320,
                content=ft.Column([
                    ft.Text("Начало номера телефона", size=12, color=theme.MUTED_TEXT),
                    prefix_f,
                    ft.Text("Диапазон ползунка задержки", size=12, color=theme.MUTED_TEXT),
                    ft.Row([min_f, max_f], spacing=10),
                ], spacing=10, tight=True),
            ),
            actions=[
                ft.TextButton("Отмена", on_click=close_dialog,
                              style=ft.ButtonStyle(color=theme.MUTED_TEXT)),
                ft.TextButton("Применить", on_click=apply_dialog,
                              style=ft.ButtonStyle(color=theme.PRIMARY)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ==================== ЭЛЕМЕНТЫ ИНТЕРФЕЙСА ====================

    # --- Шапка (с кнопкой настроек) ---
    header = ft.Row([
        ft.Row([
            ft.Container(
                content=ft.Icon(ft.Icons.SECURITY_ROUNDED, color=theme.PRIMARY, size=22),
                bgcolor=theme.CARD,
                padding=10,
                border_radius=12,
                border=make_border(theme.CARD_BORDER)
            ),
            ft.Column([
                ft.Text("DoorGuardian", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT),
                ft.Text("Панель управления Arduino", size=12, color=theme.MUTED_TEXT),
            ], spacing=0)
        ]),
        ft.IconButton(
            icon=ft.Icons.SETTINGS_ROUNDED,
            icon_color=theme.MUTED_TEXT,
            tooltip="Настройки",
            on_click=open_settings,
        )
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    # --- Подключение к COM-порту ---
    port_dropdown = ft.Dropdown(
        ref=port_dd_ref,
        label="Bluetooth-устройство",
        hint_text="Нажмите ⟳ для поиска",
        options=[],
        expand=True,
        text_size=14,
        color=theme.TEXT,
        bgcolor=theme.INPUT_BG,
        border_radius=10,
        border_color=theme.CARD_BORDER,
        focused_border_color=theme.PRIMARY,
    )

    connection_card = ft.Container(
        bgcolor=theme.CARD,
        border=make_border(theme.CARD_BORDER),
        border_radius=16,
        padding=16,
        content=ft.Column([
            ft.Row([
                ft.Row([
                    ft.Icon(ft.Icons.BLUETOOTH_ROUNDED, color=theme.PRIMARY, size=18),
                    ft.Text("Подключение", size=14, weight=ft.FontWeight.W_600, color=theme.TEXT),
                ]),
                ft.Container(
                    ref=conn_badge_ref,
                    content=ft.Text(ref=conn_badge_text_ref, value="Offline", size=11,
                                    color=theme.DANGER, weight=ft.FontWeight.W_600),
                    bgcolor=theme.DANGER_BG,
                    padding=make_padding(10, 6),
                    border_radius=20,
                    border=make_border(theme.DANGER),
                ),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Row([
                port_dropdown,
                ft.IconButton(
                    icon=ft.Icons.REFRESH_ROUNDED,
                    icon_color=theme.PRIMARY,
                    tooltip="Найти устройства",
                    on_click=refresh_ports,
                ),
            ], spacing=4),
            ft.Container(
                ref=conn_btn_ref,
                content=ft.Text(ref=conn_btn_text_ref, value="Подключиться",
                                weight=ft.FontWeight.BOLD, color=theme.BACKGROUND,
                                text_align=ft.TextAlign.CENTER),
                bgcolor=theme.SUCCESS,
                border_radius=10,
                padding=14,
                alignment=ft.Alignment.CENTER,
                on_click=on_connect_click,
                ink=True,
            ),
        ], spacing=12)
    )

    # --- Карточка статуса двери ---
    door_icon = ft.Icon(ft.Icons.DOOR_SLIDING, color=theme.SUCCESS, size=36, ref=door_icon_ref)

    status_card = ft.Container(
        ref=status_card_ref,
        bgcolor=theme.SUCCESS_BG,
        border=make_border(theme.SUCCESS),
        border_radius=16,
        padding=20,
        content=ft.Row([
            ft.Row([
                door_icon,
                ft.Column([
                    ft.Text(ref=door_title_ref, value="ДВЕРЬ ЗАКРЫТА", size=17,
                            weight=ft.FontWeight.BOLD, color=theme.TEXT),
                    ft.Text(ref=door_subtitle_ref, value="Периметр защищен", size=13,
                            color=theme.MUTED_TEXT),
                ], spacing=2)
            ]),
            ft.IconButton(
                icon=ft.Icons.SYNC_ROUNDED,
                icon_color=theme.TEXT,
                tooltip="Тест переключения двери",
                on_click=toggle_door
            )
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    )

    # --- Кнопка охраны ---
    arm_btn_icon = ft.Icon(ft.Icons.SHIELD_MOON, color=theme.PRIMARY, size=20, ref=arm_btn_icon_ref)

    arm_button = ft.Container(
        content=ft.Row([
            arm_btn_icon,
            ft.Text(ref=arm_btn_text_ref, value="ПОСТАВИТЬ НА ОХРАНУ",
                    weight=ft.FontWeight.BOLD, color=theme.TEXT),
        ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
        bgcolor=theme.CARD,
        border=make_border(theme.PRIMARY),
        border_radius=12,
        padding=16,
        on_click=toggle_guard,
        ink=True
    )

    # --- Аккумулятор ---
    battery_card = ft.Container(
        bgcolor=theme.CARD,
        border=make_border(theme.CARD_BORDER),
        border_radius=16,
        padding=16,
        content=ft.Column([
            ft.Row([
                ft.Row([
                    ft.Icon(ft.Icons.BATTERY_CHARGING_FULL_ROUNDED, color=theme.WARNING, size=18),
                    ft.Text("Питание устройства", size=14, weight=ft.FontWeight.W_600, color=theme.TEXT)
                ]),
                ft.Text(ref=battery_text_ref, value="Ожидание данных...", size=12, color=theme.MUTED_TEXT)
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.ProgressBar(ref=battery_bar_ref, value=0.0, color=theme.WARNING,
                           bgcolor=theme.INPUT_BG, bar_height=6)
        ], spacing=10)
    )

    # --- Телефон: неизменяемый префикс + ввод остальных цифр ---
    phone_row = ft.Row([
        ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.PHONE_ANDROID_ROUNDED, size=18, color=theme.MUTED_TEXT),
                ft.Text(ref=prefix_text_ref, value=config["prefix"], size=15,
                        weight=ft.FontWeight.BOLD, color=theme.TEXT),
            ], spacing=6, alignment=ft.MainAxisAlignment.CENTER),
            bgcolor=theme.INPUT_BG,
            border=make_border(theme.CARD_BORDER),
            border_radius=10,
            padding=make_padding(12, 14),
        ),
        ft.TextField(
            ref=phone_field_ref,
            label="Номер телефона для SMS",
            hint_text="9991234567",
            value="",
            expand=True,
            text_size=14,
            bgcolor=theme.INPUT_BG,
            keyboard_type=ft.KeyboardType.PHONE,
            input_filter=ft.NumbersOnlyInputFilter(),
            max_length=10,
            border=ft.OutlineInputBorder(
                border_radius=10,
                side=ft.BorderSide(1, theme.CARD_BORDER)
            )
        ),
    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.START)

    settings_card = ft.Container(
        bgcolor=theme.CARD,
        border=make_border(theme.CARD_BORDER),
        border_radius=16,
        padding=16,
        content=ft.Column([
            ft.Text("Оповещения и таймеры", size=14, weight=ft.FontWeight.W_600, color=theme.TEXT),
            phone_row,
            ft.Column([
                ft.Row([
                    ft.Text("Задержка срабатывания", size=13, color=theme.MUTED_TEXT),
                    ft.Text(ref=sound_delay_text_ref, value="15 сек", size=13,
                            weight=ft.FontWeight.BOLD, color=theme.PRIMARY)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Slider(
                    ref=sound_delay_ref,
                    min=config["delay_min"],
                    max=config["delay_max"],
                    divisions=config["delay_max"] - config["delay_min"],
                    value=15,
                    active_color=theme.PRIMARY,
                    inactive_color=theme.INPUT_BG,
                    on_change=on_slider_change
                ),
            ], spacing=2),
            ft.Container(
                content=ft.Text("Сохранить параметры", weight=ft.FontWeight.BOLD,
                                color=theme.BACKGROUND, text_align=ft.TextAlign.CENTER),
                bgcolor=theme.PRIMARY,
                border_radius=10,
                padding=14,
                alignment=ft.Alignment.CENTER,
                on_click=save_settings,
                ink=True
            )
        ], spacing=14)
    )

    # --- Журнал ---
    event_log = ft.Column(ref=event_log_ref, spacing=4, scroll=ft.ScrollMode.AUTO)

    log_card = ft.Container(
        bgcolor=theme.CARD,
        border=make_border(theme.CARD_BORDER),
        border_radius=16,
        padding=16,
        content=ft.Column([
            ft.Row([
                ft.Row([
                    ft.Icon(ft.Icons.RECEIPT_LONG_ROUNDED, color=theme.MUTED_TEXT, size=18),
                    ft.Text("Журнал событий", size=14, weight=ft.FontWeight.W_600, color=theme.TEXT),
                ]),
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, size=14, color=theme.PRIMARY),
                        ft.Text("CSV Экспорт", size=12, color=theme.PRIMARY, weight=ft.FontWeight.BOLD)
                    ], spacing=4),
                    on_click=export_csv,
                    padding=make_padding(8, 4),
                    border_radius=6,
                    ink=True
                )
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Container(
                content=event_log,
                height=140,
                bgcolor=theme.INPUT_BG,
                border_radius=10,
                padding=10,
                border=make_border(theme.CARD_BORDER, 0.5),
                clip_behavior=ft.ClipBehavior.HARD_EDGE
            )
        ], spacing=10)
    )

    # ==================== СБОРКА СТРАНИЦЫ ====================
    async def on_close(e):
        await disconnect(silent=True)

    page.on_close = on_close

    page.add(
        ft.Column(
            controls=[
                header,
                connection_card,
                status_card,
                arm_button,
                battery_card,
                settings_card,
                log_card
            ],
            spacing=16,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER
        )
    )

    add_event("Приложение успешно запущено")


if __name__ == "__main__":
    ft.run(main)
