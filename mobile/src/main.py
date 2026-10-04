import flet as ft
import datetime

def main(page: ft.Page):
    page.title = "DoorGuardian"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 16
    page.bgcolor = "#1E1E2E"
    
    # Включаем плавную прокрутку всей страницы для смартфона
    page.scroll = ft.ScrollMode.AUTO

    demo_state = {
        "door_open": False,
        "armed": False,
    }

    # ==================== СТАТУС ДВЕРИ ====================
    door_status = ft.Text(
        "ДВЕРЬ ЗАКРЫТА",
        size=24,
        weight=ft.FontWeight.BOLD,
        color="#A6E3A1",
    )

    mode_status = ft.Text(
        "Система снята с охраны",
        size=15,
        color="#CDD6F4",
    )

    status_container = ft.Container(
        content=door_status,
        bgcolor="#26382E",
        padding=20,
        border_radius=12,
        alignment=ft.Alignment.CENTER,
    )

    # ==================== АККУМУЛЯТОР ====================
    battery_text = ft.Text("Аккумулятор: 85% — 3.95 В", color="#CDD6F4")
    battery_bar = ft.ProgressBar(value=0.85, color="#A6E3A1", bgcolor="#313244")

    # ==================== НАСТРОЙКИ ====================
    phone_field = ft.TextField(
        label="Номер для SMS и звонков",
        value="+70000000000",
        keyboard_type=ft.KeyboardType.PHONE,
    )

    sound_delay = ft.Slider(
        min=5,
        max=60,
        divisions=11,
        value=15,
        label="{value} сек",
    )

    # ==================== ЖУРНАЛ ====================
    event_log = ft.Column(
        controls=[
            ft.Text("Журнал событий", size=18, weight=ft.FontWeight.BOLD),
        ],
        spacing=8,
    )

    def add_event(message: str):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        event_log.controls.append(
            ft.Text(f"{current_time} — {message}", color="#CDD6F4")
        )
        page.update()

    # ==================== ОБРАБОТЧИКИ ====================
    def toggle_door(event):
        demo_state["door_open"] = not demo_state["door_open"]
        if demo_state["door_open"]:
            door_status.value = "⚠️ ДВЕРЬ ОТКРЫТА"
            door_status.color = "#F38BA8"
            status_container.bgcolor = "#452A2A"
            add_event("Дверь открыта")
        else:
            door_status.value = "ДВЕРЬ ЗАКРЫТА"
            door_status.color = "#A6E3A1"
            status_container.bgcolor = "#26382E"
            add_event("Дверь закрыта")
        page.update()

    arm_button_text = ft.Text("Поставить на охрану")

    def toggle_guard(event):
        demo_state["armed"] = not demo_state["armed"]
        if demo_state["armed"]:
            mode_status.value = "Система поставлена на охрану"
            mode_status.color = "#A6E3A1"
            arm_button_text.value = "Снять с охраны"
            add_event("Система поставлена на охрану")
        else:
            mode_status.value = "Система снята с охраны"
            mode_status.color = "#CDD6F4"
            arm_button_text.value = "Поставить на охрану"
            add_event("Система снята с охраны")
        page.update()

    def save_settings(event):
        phone = phone_field.value.strip()
        delay = int(sound_delay.value)
        add_event(f"Сохранено: номер {phone}, задержка {delay} с.")

    # ==================== КНОПКИ ====================
    arm_button = ft.Button(content=arm_button_text, on_click=toggle_guard)
    door_button = ft.Button(content=ft.Text("Изменить состояние двери"), on_click=toggle_door)
    save_button = ft.Button(content=ft.Text("Сохранить настройки"), on_click=save_settings)

    # ==================== СБОРКА ИНТЕРФЕЙСА ====================
    page.add(
        ft.Column(
            controls=[
                ft.Text("DoorGuardian", size=28, weight=ft.FontWeight.BOLD, color="#89B4FA"),
                ft.Text("Мобильная панель управления", color="#A6ADC8"),
                ft.Text("ДЕМОНСТРАЦИОННЫЙ РЕЖИМ", color="#F9E2AF", weight=ft.FontWeight.BOLD),
                status_container,
                mode_status,
                ft.Divider(),
                ft.Text("Состояние питания", size=18, weight=ft.FontWeight.BOLD),
                battery_text,
                battery_bar,
                ft.Divider(),
                ft.Text("Управление", size=18, weight=ft.FontWeight.BOLD),
                arm_button,
                door_button,
                ft.Divider(),
                ft.Text("Настройки оповещения", size=18, weight=ft.FontWeight.BOLD),
                phone_field,
                ft.Text("Задержка включения звука"),
                sound_delay,
                save_button,
                ft.Divider(),
                event_log,
            ],
            spacing=12,
        )
    )

    add_event("Мобильное приложение запущено")

if __name__ == "__main__":
    ft.run(main)