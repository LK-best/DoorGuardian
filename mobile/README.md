# Mobile app

## Сборка APK

```bash
cd mobile
pip install "flet[cli]>=1.0.3"
flet build apk
```

Готовый файл — `build/apk/DoorGuardian.apk` (один "толстый" APK со всеми ABI,
ничего дописывать не нужно). Полезные варианты:

```bash
flet build apk --split-per-abi        # отдельные APK на arm64-v8a / armeabi-v7a / x86_64
flet build apk --arch arm64-v8a       # только 64-битные ARM-телефоны
flet build apk --android-legacy-packaging   # меньше сам файл для сайдлоада
```

При первом запуске Flet CLI сам скачает Flutter SDK, JDK 17 и Android SDK
(несколько гигабайт, нужен интернет). Точка входа — `src/main_2.py`, она задана
в `pyproject.toml` (`[tool.flet.app].module`), переименовывать файл не нужно.

На Windows pip часто тянет Linux-зависимости bleak: сборку APK удобнее делать в WSL.
Установка на телефон:

```cmd
adb install -r build\apk\DoorGuardian.apk
```

Ограничение Flet на Android: `bleak` не имеет готового колеса в serious-python,
надёжный канал на телефоне для камеры и датчиков — Wi-Fi/HTTP.

## Run the app

### uv

Run as a desktop app:

```bash
uv run flet run
```

Run as a web app:

```bash
uv run flet run --web
```

For more details on running the app, refer to the [Getting Started Guide](https://flet.dev/docs/).

## Build the app

### Android

```bash
flet build apk -v
```

For more details on building and signing `.apk` or `.aab`, refer to the [Android Packaging Guide](https://flet.dev/docs/publish/android/).

### iOS

```bash
flet build ipa -v
```

For more details on building and signing `.ipa`, refer to the [iOS Packaging Guide](https://flet.dev/docs/publish/ios/).

### macOS

```bash
flet build macos -v
```

For more details on building macOS package, refer to the [macOS Packaging Guide](https://flet.dev/docs/publish/macos/).

### Linux

```bash
flet build linux -v
```

For more details on building Linux package, refer to the [Linux Packaging Guide](https://flet.dev/docs/publish/linux/).

### Windows

```bash
flet build windows -v
```

For more details on building Windows package, refer to the [Windows Packaging Guide](https://flet.dev/docs/publish/windows/).

### Web

```bash
flet build web -v
```

For more details on building Web app, refer to the [Web Packaging Guide](https://flet.dev/docs/publish/web/).
