import flet as ft
import datetime
import csv
import theme  # Импорт вашей темы
import tkinter as tk
from tkinter import filedialog

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
    page.window_height = 750

    # Настройка темы Flet 1.0+ без ошибочных параметров и без бежевого налета M3
    page.theme = ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=theme.PRIMARY,
            surface=theme.CARD,
            surface_tint=ft.Colors.TRANSPARENT,
        )
    )

    demo_state = {"door_open": False, "armed": False}
    log_data = []

    # ==================== ЭКСПОРТ CSV (БЕЗ КРАСНОГО ЭКРАНА) ====================
    def export_csv(e):
        try:
            # Системное окно сохранения Windows (без сбоев Flet FilePicker)
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            
            file_path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV файлы", "*.csv")],
                initialfile="door_log.csv",
                title="Сохранить журнал событий"
            )
            root.destroy()

            if file_path:
                with open(file_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Timestamp", "Event"])
                    writer.writerows(log_data)
                add_event(f"Журнал экспортирован: {file_path}")
        except Exception as err:
            add_event(f"Ошибка экспорта: {err}")

    # ==================== REFS ====================
    door_title_ref = ft.Ref[ft.Text]()
    door_subtitle_ref = ft.Ref[ft.Text]()
    door_icon_ref = ft.Ref[ft.Icon]()
    status_card_ref = ft.Ref[ft.Container]()
    
    arm_btn_text_ref = ft.Ref[ft.Text]()
    arm_btn_icon_ref = ft.Ref[ft.Icon]()
    
    battery_text_ref = ft.Ref[ft.Text]()
    battery_bar_ref = ft.Ref[ft.ProgressBar]()
    
    phone_field_ref = ft.Ref[ft.TextField]()
    sound_delay_ref = ft.Ref[ft.Slider]()
    sound_delay_text_ref = ft.Ref[ft.Text]()
    event_log_ref = ft.Ref[ft.Column]()

    # ==================== ВСПОМОГАТЕЛЬНЫЕ ДЕЙСТВИЯ ====================
    def add_event(message: str):
        if not event_log_ref.current:
            return

        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        log_data.append([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message])
        
        event_log_ref.current.controls.insert(
            0,
            ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Text(current_time, size=11, color=theme.MUTED_TEXT, weight=ft.FontWeight.W_500),
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

    # ==================== ОБРАБОТЧИКИ СОБЫТИЙ ====================
    def toggle_door(e):
        demo_state["door_open"] = not demo_state["door_open"]
        
        if door_title_ref.current and door_subtitle_ref.current and door_icon_ref.current and status_card_ref.current:
            if demo_state["door_open"]:
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

    def toggle_guard(e):
        demo_state["armed"] = not demo_state["armed"]
        if arm_btn_text_ref.current and arm_btn_icon_ref.current:
            if demo_state["armed"]:
                arm_btn_text_ref.current.value = "СНЯТЬ С ОХРАНЫ"
                arm_btn_icon_ref.current.name = ft.Icons.SHIELD_OUTLINED
                add_event("🛡️ Система поставлена на охрану")
            else:
                arm_btn_text_ref.current.value = "ПОСТАВИТЬ НА ОХРАНУ"
                arm_btn_icon_ref.current.name = ft.Icons.SHIELD_MOON
                add_event("🔓 Система снята с охраны")
            page.update()

    def on_slider_change(e):
        if sound_delay_text_ref.current:
            sound_delay_text_ref.current.value = f"{int(e.control.value)} сек"
            page.update()

    def save_settings(e):
        if phone_field_ref.current:
            phone = phone_field_ref.current.value.strip()
            if not phone.startswith("+") or len(phone) < 12:
                phone_field_ref.current.error_text = "Формат: +79991234567"
                page.update()
                return

            phone_field_ref.current.error_text = None
            
            delay_val = 15
            if sound_delay_ref.current:
                delay_val = int(sound_delay_ref.current.value)
                
            add_event(f"⚙️ Сохранено: {phone}, задержка {delay_val}с")
            
            page.overlay.append(
                ft.SnackBar(
                    content=ft.Text("Настройки применены", color=theme.TEXT),
                    bgcolor=theme.CARD,
                    open=True
                )
            )
            page.update()

    # ==================== ЭЛЕМЕНТЫ ИНТЕРФЕЙСА (v2rayTun Style) ====================

    # --- Шапка ---
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
        ft.Container(
            content=ft.Text("v1.0.4 Online", size=11, color=theme.SUCCESS, weight=ft.FontWeight.W_600),
            bgcolor=theme.SUCCESS_BG,
            padding=make_padding(10, 6),
            border_radius=20,
            border=make_border(theme.SUCCESS)
        )
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    # --- Главная карточка статуса ---
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
                    ft.Text(ref=door_title_ref, value="ДВЕРЬ ЗАКРЫТА", size=17, weight=ft.FontWeight.BOLD, color=theme.TEXT),
                    ft.Text(ref=door_subtitle_ref, value="Периметр защищен", size=13, color=theme.MUTED_TEXT),
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

    # --- Главная кнопка активации охраны ---
    arm_btn_icon = ft.Icon(ft.Icons.SHIELD_MOON, color=theme.PRIMARY, size=20, ref=arm_btn_icon_ref)
    
    arm_button = ft.Container(
        content=ft.Row([
            arm_btn_icon,
            ft.Text(ref=arm_btn_text_ref, value="ПОСТАВИТЬ НА ОХРАНУ", weight=ft.FontWeight.BOLD, color=theme.TEXT),
        ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
        bgcolor=theme.CARD,
        border=make_border(theme.PRIMARY),
        border_radius=12,
        padding=16,
        on_click=toggle_guard,
        ink=True
    )

    # --- Виджет заряда аккумулятора ---
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
            ft.ProgressBar(ref=battery_bar_ref, value=0.7, color=theme.WARNING, bgcolor=theme.INPUT_BG, bar_height=6)
        ], spacing=10)
    )

    # --- Виджет настроек (Синтаксис Flet 1.0 с параметром side) ---
    phone_input = ft.TextField(
        ref=phone_field_ref,
        label="Номер телефона для SMS",
        value="+7",
        prefix_icon=ft.Icons.PHONE_ANDROID_ROUNDED,
        bgcolor=theme.INPUT_BG,
        text_size=14,
        max_length=12,
        border=ft.OutlineInputBorder(
            border_radius=10,
            side=ft.BorderSide(1, theme.CARD_BORDER)  # Исправлено: side вместо border_side
        )
    )
    
    settings_card = ft.Container(
        bgcolor=theme.CARD,
        border=make_border(theme.CARD_BORDER),
        border_radius=16,
        padding=16,
        content=ft.Column([
            ft.Text("Оповещения и таймеры", size=14, weight=ft.FontWeight.W_600, color=theme.TEXT),
            phone_input,
            ft.Column([
                ft.Row([
                    ft.Text("Задержка срабатывания", size=13, color=theme.MUTED_TEXT),
                    ft.Text(ref=sound_delay_text_ref, value="15 сек", size=13, weight=ft.FontWeight.BOLD, color=theme.PRIMARY)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Slider(
                    ref=sound_delay_ref,
                    min=5,
                    max=60,
                    divisions=11,
                    value=15,
                    active_color=theme.PRIMARY,
                    inactive_color=theme.INPUT_BG,
                    on_change=on_slider_change
                ),
            ], spacing=2),
            ft.Container(
                content=ft.Text("Сохранить параметры", weight=ft.FontWeight.BOLD, color=theme.BACKGROUND, text_align=ft.TextAlign.CENTER),
                bgcolor=theme.PRIMARY,
                border_radius=10,
                padding=14,
                alignment=ft.Alignment.CENTER,
                on_click=save_settings,
                ink=True
            )
        ], spacing=14)
    )

    # --- Карточка журнала ---
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
    page.add(
        ft.Column(
            controls=[
                header,
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