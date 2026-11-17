#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Turret Cloud Relay Bridge (Турель + DoorGuardian Wi-Fi)
Порт: 18081 (локальный, наружу смотрит nginx на 18080)
Путь: /root/turret_bridge/server.py

Что исправлено относительно прежней версии (эндпоинты и форматы НЕ менялись):
  * HTTP/1.1 keep-alive: ESP32-S3 больше не делает новый TCP-коннект на каждый кадр.
    Для этого КАЖДЫЙ ответ теперь имеет Content-Length (иначе клиент на keep-alive зависает).
  * /stream отдаёт только НОВЫЕ кадры (по счётчику), без повторов одного и того же кадра,
    а медленный зритель просто пропускает кадры вместо накопления задержки.
  * TCP_NODELAY, ограниченный буфер отправки на стриме, таймауты на сокетах.
  * Кадр читается ровно по Content-Length, проверяется JPEG-сигнатура и лимит размера.
  * Очередь PTZ-команд: только U/D/L/R/C/S, не длиннее 16 символов (старые отбрасываются).
  * Дверь: до первого отчёта статус UNKNOWN (а не выдуманный ARMED), в /door_status
    добавлен ключ AGE (мс с последнего отчёта), -1 если отчётов ещё не было.
  * Тихая обработка обрывов соединений (нет трейсбеков в логе).
  * Веб-пульт (HTML) оставлен без изменений.
"""

import socket
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import os

PORT = int(
    os.environ.get("TURRET_PORT", "18081")
)  # за nginx: 18081, наружу торчит nginx:18080
MAX_FRAME_BYTES = 512 * 1024  # защита от мусора/переполнения памяти
MAX_PENDING_CMDS = 16  # длина очереди PTZ-команд
VALID_PTZ = set("UDLRCS")
VALID_DOOR_CMDS = ("ARM", "DISARM")

# ==========================================
# ГЛОБАЛЬНЫЕ ДАННЫЕ
# ==========================================
# 1. Турель
latest_jpeg = None
frame_seq = 0  # номер последнего кадра (внутренний счётчик сервера)
last_upload_seq = 0  # X-Seq последнего принятого кадра от камеры
last_frame_at = 0.0  # monotonic время последнего принятого кадра
latest_telemetry = "DIST=-1;LIGHT=0;PAN=165;TILT=125"
pending_commands = ""
cmd_lock = threading.Lock()
data_lock = threading.Lock()
frame_condition = threading.Condition(data_lock)

# 2. DoorGuardian (Дверь)
door_status = "UNKNOWN"  # "OPEN" / "CLOSED" / "UNKNOWN"
door_state = "UNKNOWN"  # "ARMED" / "DISARMED" / "ALARM" / "ARMING_xs" / "UNKNOWN"
door_last_report = 0.0  # time.monotonic() последнего отчёта
door_pending_cmd = ""  # "ARM" / "DISARM"
door_lock = threading.Lock()


def normalize_telemetry(t):
    """Принимает 'DIST=..;LIGHT=..;PAN=..;TILT=..' или короткий 'D:..;L:..;P:..;T:..'.
    Возвращает формат DIST=..., который понимают веб-пульт и Flet, либо None."""
    t = (t or "").strip()[:200]
    if t.startswith("DIST="):
        return t
    if t.startswith("D:"):
        m = {}
        for part in t.split(";"):
            k, _, v = part.partition(":")
            m[k.strip()] = v.strip()
        try:
            return "DIST=%d;LIGHT=%d;PAN=%d;TILT=%d" % (
                int(m["D"]),
                int(m["L"]),
                int(m["P"]),
                int(m["T"]),
            )
        except (KeyError, ValueError):
            return None
    return None


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def handle_error(self, request, client_address):
        # Обрывы связи на мобильной сети — норма, не засоряем лог трейсбеками
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


class TurretHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive (ВАЖНО для скорости камеры)
    timeout = 15  # простаивающее соединение/зависший клиент закрываются сами
    disable_nagle_algorithm = True  # TCP_NODELAY

    def log_message(self, format, *args):
        pass

    # ---------- общий ответ: ВСЕГДА с Content-Length ----------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    # ==========================================
    # POST
    # ==========================================
    def do_POST(self):
        global latest_jpeg, latest_telemetry, pending_commands, frame_seq
        global last_upload_seq, last_frame_at

        path = urlparse(self.path).path
        if path != "/api/upload":
            self.close_connection = (
                True  # тело не прочитано — соединение дальше непригодно
            )
            self._send(404, b"Not Found")
            return

        # Приём кадра и телеметрии от ESP32-S3
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_FRAME_BYTES:
            self.close_connection = True
            self._send(400, b"Bad Content-Length")
            return

        frame_data = self.rfile.read(content_length)  # ровно N байт
        if len(frame_data) != content_length:
            self.close_connection = True
            return
        if frame_data[:2] != b"\xff\xd8":  # не JPEG
            self._send(400, b"Not a JPEG")
            return

        telem = normalize_telemetry(self.headers.get("X-Telemetry"))
        try:
            seq = int(self.headers.get("X-Seq", ""))
        except ValueError:
            seq = None

        # Камера шлёт кадры по нескольким параллельным соединениям, поэтому они могут
        # приходить не по порядку. Старый кадр (seq меньше уже принятого) не показываем,
        # иначе видео "дёргается" назад. После перезагрузки платы seq начинается с 1:
        # это распознаётся по давности последнего кадра или по большому провалу номера.
        now = time.monotonic()
        with frame_condition:
            stale = (
                seq is not None
                and seq <= last_upload_seq
                and last_upload_seq - seq < 1000
                and now - last_frame_at < 3.0
            )
            if not stale:
                latest_jpeg = frame_data
                frame_seq += 1
                if seq is not None:
                    last_upload_seq = seq
                last_frame_at = now
                frame_condition.notify_all()
            if telem:
                latest_telemetry = telem

        # Отдаем накопившиеся PTZ команды пачкой
        cmd_to_send = ""
        with cmd_lock:
            if pending_commands:
                cmd_to_send = pending_commands
                pending_commands = ""

        self._send(
            200, cmd_to_send.encode("utf-8"), "text/plain", {"Connection": "keep-alive"}
        )

    # ==========================================
    # GET
    # ==========================================
    def do_GET(self):
        global pending_commands, door_status, door_state
        global door_pending_cmd, door_last_report

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # ----------------------------------------------------
        # ЭНДПОИНТЫ ТУРЕЛИ
        # ----------------------------------------------------
        if path == "/cmd":
            c = "".join(ch for ch in query.get("c", [""])[0] if ch in VALID_PTZ)
            if c:
                with cmd_lock:
                    # хвост очереди: устаревшие команды отбрасываются, чтобы турель не "доигрывала" минуту
                    pending_commands = (pending_commands + c)[-MAX_PENDING_CMDS:]
                ip = (
                    self.headers.get("X-Real-IP") or self.client_address[0]
                )  # за nginx client_address всегда 127.0.0.1
                print(f"[CMD ПРИНЯТА] от {ip} -> {c}", flush=True)
            self._send(200, b"OK")
            return

        elif path == "/snapshot":
            with data_lock:
                frame = latest_jpeg
            if frame is None:
                self._send(503, b"Waiting for camera...", extra={"Retry-After": "1"})
                return
            self._send(200, frame, "image/jpeg")
            return

        elif path == "/stream":
            self.close_connection = True  # поток бесконечный, Content-Length нет
            try:
                # небольшой буфер отправки: если зритель медленный — задержка не копится в ядре
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024
                )
            except OSError:
                pass
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            last_seq = 0
            while True:
                with frame_condition:
                    got = frame_condition.wait_for(
                        lambda: frame_seq != last_seq, timeout=5.0
                    )
                    frame = latest_jpeg
                    last_seq = frame_seq
                if not got or not frame:
                    continue  # новых кадров нет — ничего не шлём (обрыв клиента поймаем на следующей записи)
                # Одна запись = один sendall, без дробления на мелкие пакеты
                head = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                )
                self.wfile.write(
                    head + frame + b"\r\n"
                )  # при обрыве исключение закроет поток

        elif path == "/telemetry":
            with data_lock:
                telem = latest_telemetry
            self._send(200, telem.encode("utf-8"))
            return

        # ----------------------------------------------------
        # ЭНДПОИНТЫ DOOR GUARDIAN (ДВЕРЬ)
        # ----------------------------------------------------
        # 1. DoorGuardian ESP32 присылает свой статус: /api/door_report?door=OPEN&state=ALARM
        elif path == "/api/door_report":
            d_param = query.get("door", [""])[0]
            s_param = query.get("state", [""])[0]

            with door_lock:
                if d_param in ("OPEN", "CLOSED"):
                    door_status = d_param
                if (
                    s_param
                    and len(s_param) <= 16
                    and s_param.replace("_", "").isalnum()
                ):
                    door_state = s_param
                door_last_report = time.monotonic()

                # Забираем ожидающую команду для датчика двери
                cmd_out = door_pending_cmd
                door_pending_cmd = ""

            self._send(200, cmd_out.encode("utf-8"))
            return

        # 2. Пульт / Web отправляет команду двери: /api/door_cmd?c=ARM или DISARM
        elif path == "/api/door_cmd":
            c = query.get("c", [""])[0].upper()
            if c in VALID_DOOR_CMDS:
                with door_lock:
                    door_pending_cmd = c
                self._send(200, b"OK")
            else:
                self._send(400, b"BAD CMD")
            return

        # 3. Пульт Flet читает статус двери: /door_status
        elif path == "/door_status":
            with door_lock:
                age = (
                    int((time.monotonic() - door_last_report) * 1000)
                    if door_last_report
                    else -1
                )
                status_str = f"DOOR={door_status};STATE={door_state};AGE={age}"
            self._send(200, status_str.encode("utf-8"))
            return

        # ----------------------------------------------------
        # ВЕБ-ИНТЕРФЕЙС (/)
        # ----------------------------------------------------
        elif path in ["/", "/index.html"]:
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            return
        else:
            self._send(404, b"Not Found")


INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Охранный Комплекс: Турель & DoorGuardian</title>
    <style>
        body { background: #121212; color: #e0e0e0; font-family: 'Segoe UI', Tahoma, sans-serif; text-align: center; margin: 0; padding: 15px; }
        h2 { margin-bottom: 10px; color: #4CAF50; }
        .container { display: flex; flex-wrap: wrap; justify-content: center; gap: 20px; }
        .panel { background: #1e1e1e; padding: 15px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); min-width: 320px; }
        img { width: 460px; max-width: 95vw; height: auto; border-radius: 8px; border: 2px solid #333; background: #000; }
        .grid { display: grid; grid-template-columns: repeat(3, 80px); grid-gap: 10px; justify-content: center; margin-top: 15px; }
        button { background: #2a2a2a; color: #fff; border: 1px solid #444; border-radius: 8px; font-size: 18px; font-weight: bold; height: 55px; cursor: pointer; user-select: none; }
        button:active { background: #4CAF50; transform: scale(0.95); }
        .action-btn { font-size: 14px; height: 45px; padding: 0 15px; margin: 5px; }
        .telem { font-size: 15px; margin: 10px 0; text-align: left; line-height: 1.8; }
        .badge { color: #81C784; font-weight: bold; }
        .alarm { color: #ff5252 !important; font-weight: bold; }
    </style>
</head>
<body>
    <h2>ЕДИНЫЙ ПУЛЬТ ОХРАНЫ</h2>
    <div class="container">
        <!-- Видео и Телеметрия Турели -->
        <div class="panel">
            <img src="/stream" alt="Камера offline">
            <div class="telem">
                <div>Дистанция: <span id="dist" class="badge">--</span> см</div>
                <div>Освещенность: <span id="light" class="badge">--</span></div>
                <div>Угол Pan (D10): <span id="pan" class="badge">--</span>°</div>
                <div>Угол Tilt (D11): <span id="tilt" class="badge">--</span>°</div>
            </div>
        </div>

        <!-- Управление PTZ и Охрана Двери -->
        <div class="panel">
            <h3>Управление Турелью (PTZ)</h3>
            <div class="grid">
                <div></div>
                <button onmousedown="startCmd('U')" onmouseup="stopCmd()" ontouchstart="startCmd('U'); event.preventDefault();" ontouchend="stopCmd()">▲</button>
                <div></div>
                <button onmousedown="startCmd('L')" onmouseup="stopCmd()" ontouchstart="startCmd('L'); event.preventDefault();" ontouchend="stopCmd()">◄</button>
                <button onclick="sendOnce('C')">•</button>
                <button onmousedown="startCmd('R')" onmouseup="stopCmd()" ontouchstart="startCmd('R'); event.preventDefault();" ontouchend="stopCmd()">►</button>
                <div></div>
                <button onmousedown="startCmd('D')" onmouseup="stopCmd()" ontouchstart="startCmd('D'); event.preventDefault();" ontouchend="stopCmd()">▼</button>
                <div></div>
            </div>

            <hr style="border: 0; border-top: 1px solid #333; margin: 20px 0;">

            <h3>Охрана Двери (DoorGuardian Wi-Fi)</h3>
            <div class="telem">
                <div>Геркон: <span id="door_status" class="badge">--</span></div>
                <div>Режим: <span id="door_state" class="badge">--</span></div>
            </div>
            <div>
                <button class="action-btn" onclick="sendDoor('ARM')">🔒 На Охрану</button>
                <button class="action-btn" onclick="sendDoor('DISARM')">🔓 Снять / Сброс</button>
            </div>
        </div>
    </div>

    <script>
        let holdTimer = null;
        function sendOnce(c) { fetch('/cmd?c=' + c).catch(()=>{}); }
        function sendDoor(c) { fetch('/api/door_cmd?c=' + c).catch(()=>{}); }
        function startCmd(c) {
            sendOnce(c);
            if (!holdTimer) holdTimer = setInterval(() => sendOnce(c), 90);
        }
        function stopCmd() {
            if (holdTimer) { clearInterval(holdTimer); holdTimer = null; }
        }

        window.addEventListener('keydown', (e) => {
            if (e.repeat) return;
            if (e.key === 'ArrowUp') startCmd('D');
            else if (e.key === 'ArrowDown') startCmd('U');
            else if (e.key === 'ArrowLeft') startCmd('L');
            else if (e.key === 'ArrowRight') startCmd('R');
            else if (e.key === ' ') sendOnce('C');
        });
        window.addEventListener('keyup', (e) => {
            if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) stopCmd();
        });

        // Опрос телеметрии турели
        setInterval(() => {
            fetch('/telemetry').then(r => r.text()).then(t => {
                let parts = t.split(';');
                parts.forEach(p => {
                    let [k, v] = p.split('=');
                    if (k === 'DIST') document.getElementById('dist').innerText = v;
                    if (k === 'LIGHT') document.getElementById('light').innerText = v;
                    if (k === 'PAN') document.getElementById('pan').innerText = v;
                    if (k === 'TILT') document.getElementById('tilt').innerText = v;
                });
            }).catch(()=>{});
        }, 300);

        // Опрос статуса двери
        setInterval(() => {
            fetch('/door_status').then(r => r.text()).then(t => {
                let parts = t.split(';');
                let dSpan = document.getElementById('door_status');
                let sSpan = document.getElementById('door_state');
                parts.forEach(p => {
                    let [k, v] = p.split('=');
                    if (k === 'DOOR') {
                        dSpan.innerText = (v === 'OPEN') ? 'ОТКРЫТА!' : 'ЗАКРЫТА';
                        dSpan.className = (v === 'OPEN') ? 'alarm' : 'badge';
                    }
                    if (k === 'STATE') {
                        sSpan.innerText = v;
                        sSpan.className = (v === 'ALARM') ? 'alarm' : 'badge';
                    }
                });
            }).catch(()=>{});
        }, 500);
    </script>
</body>
</html>""".encode("utf-8")


def run():
    server = ThreadedHTTPServer(("0.0.0.0", PORT), TurretHandler)
    print(f"=== Turret & DoorGuardian Relay запущен на порту {PORT} ===", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
