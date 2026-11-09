#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>

// =========================================================================
// НАСТРОЙКА ПИНОВ КАМЕРЫ (Freenove / Applied Robotics ESP32-S3)
// =========================================================================
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     15
#define SIOD_GPIO_NUM      4
#define SIOC_GPIO_NUM      5

#define Y9_GPIO_NUM       16
#define Y8_GPIO_NUM       17
#define Y7_GPIO_NUM       18
#define Y6_GPIO_NUM       12
#define Y5_GPIO_NUM       10
#define Y4_GPIO_NUM        8
#define Y3_GPIO_NUM        9
#define Y2_GPIO_NUM       11
#define VSYNC_GPIO_NUM     6
#define HREF_GPIO_NUM      7
#define PCLK_GPIO_NUM     13

// =========================================================================
// ПАРАМЕТРЫ WI-FI СЕТИ
// =========================================================================
const char* ssid = "YOUR_WIFI_SSID";         // Замените на ваш SSID
const char* password = "YOUR_WIFI_PASSWORD"; // Замените на ваш пароль

WebServer server(80);
HardwareSerial ArduinoSerial(1); // Аппаратный UART1

String latestTelemetry = "DIST=0;LIGHT=0;PAN=165;TILT=125";
String uartRxBuffer = "";

// =========================================================================
// ОБРАБОТЧИКИ HTTP API
// =========================================================================

// 1. Потоковое вещание MJPEG
void handleStream() {
  WiFiClient client = server.client();
  String response = "HTTP/1.1 200 OK\r\n"
                    "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                    "Access-Control-Allow-Origin: *\r\n\r\n";
  client.print(response);

  while (client.connected()) {
    camera_fb_t * fb = esp_camera_fb_get();
    if (!fb) break;

    client.print("--frame\r\n");
    client.print("Content-Type: image/jpeg\r\n");
    client.print("Content-Length: " + String(fb->len) + "\r\n\r\n");
    client.write(fb->buf, fb->len);
    client.print("\r\n");

    esp_camera_fb_return(fb);
    yield();
  }
}

// 2. Получение одиночного кадра в формате JPEG (для легкого захвата)
void handleSnapshot() {
  camera_fb_t * fb = esp_camera_fb_get();
  if (!fb) {
    server.send(500, "text/plain", "Camera capture failed");
    return;
  }
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.setContentLength(fb->len);
  server.send(200, "image/jpeg", "");
  WiFiClient client = server.client();
  client.write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

// 3. Проброс команд PTZ на Arduino
void handleCmd() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  if (server.hasArg("c")) {
    String c = server.arg("c");
    if (c.length() > 0) {
      ArduinoSerial.print(c[0]); // Отправка одиночного байта в Arduino
    }
    server.send(200, "text/plain", "OK");
  } else {
    server.send(400, "text/plain", "Missing parameter: c");
  }
}

// 4. Отдача последней полученной телеметрии от Arduino
void handleTelemetry() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "text/plain", latestTelemetry);
}

// =========================================================================
// ИНИЦИАЛИЗАЦИЯ
// =========================================================================
void setup() {
  Serial.begin(115200);

  // Инициализация UART1 для связи с Arduino: RX=GPIO 40, TX=GPIO 39
  ArduinoSerial.begin(9600, SERIAL_8N1, 40, 39);

  // Конфигурация камеры
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;     // 640x480
  config.jpeg_quality = 12;              // 10-63 (меньше = четче)
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[CAMERA] Ошибка инициализации камеры: 0x%x\n", err);
    return;
  }

  // Аппаратный разворот изображения (vflip + hmirror)
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_vflip(s, 1);
    s->set_hmirror(s, 1);
  }

  // Подключение к Wi-Fi
  Serial.print("[WIFI] Подключение к ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n[WIFI] Успешно подключено!");
  Serial.print("[WIFI] IP адрес камеры: http://");
  Serial.println(WiFi.localIP());

  // Настройка маршрутов веб-сервера
  server.on("/stream", HTTP_GET, handleStream);
  server.on("/snapshot", HTTP_GET, handleSnapshot);
  server.on("/cmd", HTTP_GET, handleCmd);
  server.on("/telemetry", HTTP_GET, handleTelemetry);

  server.begin();
  Serial.println("[HTTP] Сервер запущен. Эндпоинты: /stream, /snapshot, /cmd, /telemetry");
}

// =========================================================================
// ОСНОВНОЙ ЦИКЛ
// =========================================================================
void loop() {
  server.handleClient();

  // Непрерывное чтение телеметрии из UART от Arduino
  while (ArduinoSerial.available() > 0) {
    char ch = ArduinoSerial.read();
    if (ch == '\n' || ch == '\r') {
      uartRxBuffer.trim();
      if (uartRxBuffer.length() > 0 && uartRxBuffer.startsWith("DIST=")) {
        latestTelemetry = uartRxBuffer;
      }
      uartRxBuffer = "";
    } else {
      if (uartRxBuffer.length() < 120) {
        uartRxBuffer += ch;
      }
    }
  }
}