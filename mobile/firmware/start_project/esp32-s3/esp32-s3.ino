#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>

// ===================== НАСТРОЙКИ WiFi =====================
const char* ssid     = "Galaxy M21";
const char* password = "67676767";

// ===================== BLE UUIDs ==========================
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a8"

// ===================== ПИНЫ СВЯЗИ С ARDUINO ===============
#define ARDUINO_RX_PIN 40 // RX на ESP32 (к TX Arduino)
#define ARDUINO_TX_PIN 39 // TX на ESP32 (к RX Arduino)

HardwareSerial ArduinoSerial(1); // Аппаратный порт UART1 для связи с платой

// ===================== ПИНЫ КАМЕРЫ FREENOVE ================
#define PWDN_GPIO_NUM    -1
#define RESET_GPIO_NUM   -1
#define XCLK_GPIO_NUM    15
#define SIOD_GPIO_NUM    4
#define SIOC_GPIO_NUM    5
#define Y9_GPIO_NUM      16
#define Y8_GPIO_NUM      17
#define Y7_GPIO_NUM      18
#define Y6_GPIO_NUM      12
#define Y5_GPIO_NUM      10
#define Y4_GPIO_NUM      8
#define Y3_GPIO_NUM      9
#define Y2_GPIO_NUM      11
#define VSYNC_GPIO_NUM   6
#define HREF_GPIO_NUM    7
#define PCLK_GPIO_NUM    13

WebServer server(80);

// --- BLE Callbacks ---
class ServerCB : public BLEServerCallbacks {
  void onConnect(BLEServer* s) {
    Serial.println("BLE: пульт подключен");
  }
  void onDisconnect(BLEServer* s) {
    Serial.println("BLE: пульт отключен, снова ждем...");
    BLEDevice::startAdvertising();
  }
};

// Прием команды с пульта и мгновенная пересылка в Arduino
class MyCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *pCharacteristic) {
    String value = pCharacteristic->getValue();
    if (value.length() > 0) {
      Serial.print("BLE -> Arduino: ");
      for (int i = 0; i < value.length(); i++) {
        char c = value[i];
        Serial.print(c);
        ArduinoSerial.write(c); // Отправляем символ в черную плату!
      }
      Serial.println();
    }
  }
};

// --- HTML страница ---
const char INDEX_HTML[] = R"rawliteral(
<!DOCTYPE html><html><head><title>ESP32-S3 FPV Cam</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{text-align:center; background:#111; color:white; font-family:sans-serif; margin:0; padding:10px;}
  img{max-width:100%; height:auto; border-radius:8px; border:2px solid #00ffcc;}
  h2{color:#00ffcc;}
</style>
</head><body>
<h2>ESP32-S3 FPV Robot</h2>
<img src="/stream" id="stream">
<p style="color:#aaa;">Видеопоток онлайн. Управление через BLE пульт.</p>
</body></html>
)rawliteral";

void handleStream() {
  WiFiClient client = server.client();
  client.print("HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n");
  char headBuf[96];

  while (client.connected()) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) break;

    int headLen = snprintf(headBuf, sizeof(headBuf),
                           "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                           (unsigned)fb->len);

    bool ok = client.write((const uint8_t*)headBuf, headLen) == (size_t)headLen;
    if (ok) ok = client.write(fb->buf, fb->len) == fb->len;
    if (ok) client.write((const uint8_t*)"\r\n", 2);

    esp_camera_fb_return(fb);
    if (!ok) break;
    delay(1);
  }
}

void fatalRestart(const char* msg) {
  Serial.println(msg);
  delay(5000);
  ESP.restart();
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- Старт ESP32-S3 Camera + UART Bridge ---");

  // Инициализация порта связи с черной платой Arduino
  ArduinoSerial.begin(9600, SERIAL_8N1, ARDUINO_RX_PIN, ARDUINO_TX_PIN);
  Serial.println("Связь с Arduino по UART (39 TX / 40 RX) готова");

  // 1. Камера
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_7;
  config.ledc_timer   = LEDC_TIMER_3;
  config.pin_d0       = Y2_GPIO_NUM; config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM; config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM; config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM; config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM; config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM; config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM; config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_LATEST;
  config.jpeg_quality = 15;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size   = FRAMESIZE_QVGA;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) fatalRestart("Ошибка запуска камеры");
  Serial.println("Камера OK");
  // === АППАРАТНЫЙ ПЕРЕВОРОТ ИЗОБРАЖЕНИЯ ===
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_vflip(s, 1);   // 1 = перевернуть по вертикали (вверх ногами)
    s->set_hmirror(s, 1); // 1 = зеркально по горизонтали (чтобы право/лево не путались)
  }

  // 2. BLE
  BLEDevice::init("ESP32-S3-Camera");
  BLEServer *pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCB());
  BLEService *pService = pServer->createService(SERVICE_UUID);
  BLECharacteristic *pCharacteristic = pService->createCharacteristic(
    CHARACTERISTIC_UUID,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
  );
  pCharacteristic->setCallbacks(new MyCallbacks());
  pService->start();

  BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  BLEDevice::startAdvertising();
  Serial.println("BLE готов к подключению пульта");

  // 3. WiFi
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.print("Подключение к WiFi");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - t0 > 25000) fatalRestart("\nОшибка WiFi");
  }
  WiFi.setSleep(false);

  Serial.println("\nWiFi подключен!");
  Serial.print("Ссылка на трансляцию: http://");
  Serial.println(WiFi.localIP());

  // 4. Веб-сервер
  server.on("/", []() { server.send(200, "text/html", INDEX_HTML); });
  server.on("/stream", handleStream);
  server.begin();
  Serial.println("Сервер запущен");
}

void loop() {
  server.handleClient();
  delay(1);
}