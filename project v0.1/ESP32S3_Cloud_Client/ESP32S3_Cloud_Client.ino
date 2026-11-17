// ============================================================================
// ESP32-S3 -> VPS: стример с ПАРАЛЛЕЛЬНЫМИ TCP-соединениями
// Плата: ESP32S3 Dev Module | PSRAM: OPI PSRAM (N8R8)
// Core 1: камера + UART (loop)      Core 0: N задач-отправителей
//
// ПОЧЕМУ БЫЛО 1-2 FPS при кадре ~25 КБ:
//   В Arduino-ESP32 окно отправки TCP (lwIP) ~5.7 КБ. Скорость одного соединения
//   не может быть выше  окно / RTT.  При RTT 100-150 мс (мобильная точка доступа ->
//   интернет -> VPS) это ~40-57 КБ/с, то есть 25 КБ * 1.8 fps = как раз то, что ты видишь.
//   Это не вина камеры или очереди. Пересобрать lwIP в Arduino IDE нельзя, поэтому
//   шлём кадры по НЕСКОЛЬКИМ соединениям одновременно: пропускная способность
//   растёт почти пропорционально их числу.
//   Кадры могут прийти не по порядку - сервер (server_v3) отбрасывает устаревшие по X-Seq.
//
// Каждые 5 секунд печатается [STAT]: fps, размер кадра, ВРЕМЯ ОТВЕТА СЕРВЕРА (≈ RTT + передача),
// сколько кадров отброшено, уровень Wi-Fi (RSSI). По ним видно, что реально ограничивает.
// ============================================================================
#include <WiFi.h>
#include "esp_camera.h"
#include "esp_heap_caps.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

// ----------------------------------------------------------------------------
// 1. НАСТРОЙКИ
// ----------------------------------------------------------------------------
const char* WIFI_SSID = "Galaxy M21";
const char* WIFI_PASS = "67676767";

const char* VPS_HOST  = "188.120.230.91";   // IP VPS (поменяй, если другой)
const uint16_t VPS_PORT = 18080;            // порт nginx

#define UPLOAD_CONNECTIONS 5                // 3..6. Больше соединений = выше FPS, но больше нагрузка
const framesize_t FRAME_SIZE = FRAMESIZE_VGA;   // VGA 640x480. Тяжелее: SVGA. Легче: HVGA / QVGA
const int JPEG_QUALITY = 12;                // 10 = лучше и тяжелее (~30 КБ), 15 = легче (~17 КБ)

// ----------------------------------------------------------------------------
// 2. ПИНЫ
// ----------------------------------------------------------------------------
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  15
#define SIOD_GPIO_NUM  4
#define SIOC_GPIO_NUM  5
#define Y9_GPIO_NUM    16
#define Y8_GPIO_NUM    17
#define Y7_GPIO_NUM    18
#define Y6_GPIO_NUM    12
#define Y5_GPIO_NUM    10
#define Y4_GPIO_NUM    8
#define Y3_GPIO_NUM    9
#define Y2_GPIO_NUM    11
#define VSYNC_GPIO_NUM 6
#define HREF_GPIO_NUM  7
#define PCLK_GPIO_NUM  13

#define ARDUINO_RX_PIN 40  // ESP32 RX <- Arduino D1 (TX)  (!) 5 В -> 3.3 В: нужен делитель 1k/2k
#define ARDUINO_TX_PIN 39  // ESP32 TX -> Arduino D0 (RX)
HardwareSerial ArduinoSerial(1);

// ----------------------------------------------------------------------------
// 3. ОБЩИЕ ДАННЫЕ
// ----------------------------------------------------------------------------
struct Frame {
  uint8_t* buf;
  size_t   len;
  uint32_t seq;
};

QueueHandle_t frameQueue;                 // последние кадры (размер 2: свежий вытесняет старый)
SemaphoreHandle_t uartMutex;
portMUX_TYPE statMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE telemMux = portMUX_INITIALIZER_UNLOCKED;

char sharedTelemetry[96] = "DIST=-1;LIGHT=0;PAN=165;TILT=125";
char uartLine[96];
size_t uartLen = 0;

// статистика (под statMux)
uint32_t stSent = 0, stBytes = 0, stReplyMsSum = 0, stReplyMsMax = 0, stFail = 0, stDropped = 0, stCaptured = 0;

// ----------------------------------------------------------------------------
// 4. КАМЕРА
// ----------------------------------------------------------------------------
bool tryCameraInit(const char* label, framesize_t size, int fbCount,
                   camera_fb_location_t loc, camera_grab_mode_t grab) {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;   config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM; config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;   config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = size;
  config.jpeg_quality = JPEG_QUALITY;
  config.fb_count     = fbCount;
  config.fb_location  = loc;
  config.grab_mode    = grab;

  esp_err_t err = esp_camera_init(&config);
  if (err == ESP_OK) { Serial.printf("[CAM] OK: %s\n", label); return true; }
  Serial.printf("[CAM] FAIL (%s): 0x%x\n", label, err);
  esp_camera_deinit();
  return false;
}

bool initCamera() {
  bool ok = false;
  if (psramFound()) {
    // кадр сразу копируется в свой буфер и драйверу возвращается, поэтому хватает 2 буферов
    ok = tryCameraInit("2 буфера, PSRAM", FRAME_SIZE, 2, CAMERA_FB_IN_PSRAM, CAMERA_GRAB_LATEST)
      || tryCameraInit("1 буфер, PSRAM",  FRAME_SIZE, 1, CAMERA_FB_IN_PSRAM, CAMERA_GRAB_WHEN_EMPTY);
  } else {
    Serial.println("[CAM] PSRAM не найдена! Tools -> PSRAM = OPI PSRAM (N8R8) / QSPI (N8R2, N16R2)");
  }
  if (!ok) ok = tryCameraInit("QVGA, DRAM", FRAMESIZE_QVGA, 1, CAMERA_FB_IN_DRAM, CAMERA_GRAB_WHEN_EMPTY);

  if (ok) {
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
      s->set_vflip(s, 1);
      s->set_hmirror(s, 1);
      s->set_brightness(s, 1);
      s->set_contrast(s, 2);
      s->set_sharpness(s, 2);
      s->set_denoise(s, 0);
      s->set_whitebal(s, 1);
      s->set_exposure_ctrl(s, 1);
      s->set_gainceiling(s, (gainceiling_t)GAINCEILING_4X);
    }
  }
  return ok;
}

// ----------------------------------------------------------------------------
// 5. КАДРЫ
// ----------------------------------------------------------------------------
void freeFrame(Frame* f) {
  if (!f) return;
  if (f->buf) free(f->buf);
  free(f);
}

Frame* makeFrame(const camera_fb_t* fb) {
  static uint32_t seq = 0;
  Frame* f = (Frame*)malloc(sizeof(Frame));
  if (!f) return NULL;
  f->buf = (uint8_t*)heap_caps_malloc(fb->len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!f->buf) f->buf = (uint8_t*)malloc(fb->len);
  if (!f->buf) { free(f); return NULL; }
  memcpy(f->buf, fb->buf, fb->len);
  f->len = fb->len;
  f->seq = ++seq;
  return f;
}

// ----------------------------------------------------------------------------
// 6. UART <-> ARDUINO
// ----------------------------------------------------------------------------
void pollArduinoTelemetry() {
  // без readStringUntil: тот блокирует до 1 с на неполной строке и подвешивал захват камеры
  while (ArduinoSerial.available() > 0) {
    char ch = (char)ArduinoSerial.read();
    if (ch == '\n' || ch == '\r') {
      if (uartLen > 3) {
        uartLine[uartLen] = 0;
        // принимаем оба формата: "DIST=..;LIGHT=..;PAN=..;TILT=.." и "D:..;L:..;P:..;T:.."
        if (strncmp(uartLine, "DIST=", 5) == 0 || (strncmp(uartLine, "D:", 2) == 0 && strstr(uartLine, ";P:"))) {
          portENTER_CRITICAL(&telemMux);
          strncpy(sharedTelemetry, uartLine, sizeof(sharedTelemetry) - 1);
          sharedTelemetry[sizeof(sharedTelemetry) - 1] = 0;
          portEXIT_CRITICAL(&telemMux);
        }
      }
      uartLen = 0;
    } else if (uartLen < sizeof(uartLine) - 1) {
      uartLine[uartLen++] = ch;
    } else {
      uartLen = 0;
    }
  }
}

void forwardToArduino(char c) {
  if (xSemaphoreTake(uartMutex, pdMS_TO_TICKS(20)) == pdTRUE) {
    ArduinoSerial.write(c);
    xSemaphoreGive(uartMutex);
  }
}

// ----------------------------------------------------------------------------
// 7. ОТПРАВКА (выполняется в N задачах на Core 0)
// ----------------------------------------------------------------------------
// Читает ответ сервера: статус, заголовки, тело ровно по Content-Length (= пачка PTZ-команд).
bool readServerReply(WiFiClient& client, uint32_t timeoutMs) {
  const uint32_t deadline = millis() + timeoutMs;
  char line[80];
  size_t n = 0;
  int status = 0, contentLen = 0;
  bool first = true, headersDone = false;

  while (!headersDone) {
    if ((int32_t)(millis() - deadline) > 0) return false;
    if (!client.available()) {
      if (!client.connected()) return false;
      vTaskDelay(1);
      continue;
    }
    char c = (char)client.read();
    if (c == '\r') continue;
    if (c != '\n') { if (n < sizeof(line) - 1) line[n++] = c; continue; }
    line[n] = 0;
    if (n == 0) {
      headersDone = true;
    } else if (first) {
      first = false;
      const char* sp = strchr(line, ' ');
      status = sp ? atoi(sp + 1) : 0;
    } else if (strncasecmp(line, "Content-Length:", 15) == 0) {
      contentLen = atoi(line + 15);
    }
    n = 0;
  }
  if (status != 200 || contentLen < 0 || contentLen > 64) return false;

  while (contentLen > 0) {
    if ((int32_t)(millis() - deadline) > 0) return false;
    if (client.available()) {
      char c = (char)client.read();
      contentLen--;
      if (c == 'U' || c == 'D' || c == 'L' || c == 'R' || c == 'C' || c == 'S') forwardToArduino(c);
    } else {
      if (!client.connected()) return false;
      vTaskDelay(1);
    }
  }
  return true;
}

bool sendFrame(WiFiClient& client, const Frame* f) {
  if (!client.connected()) {
    client.stop();
    if (!client.connect(VPS_HOST, VPS_PORT, 3000)) return false;
    client.setNoDelay(true);
    client.setTimeout(5);   // секунды (у WiFiClient это секунды, а не мс!)
  }

  char telem[96];
  portENTER_CRITICAL(&telemMux);
  strncpy(telem, sharedTelemetry, sizeof(telem) - 1);
  telem[sizeof(telem) - 1] = 0;
  portEXIT_CRITICAL(&telemMux);

  char head[320];
  int hl = snprintf(head, sizeof(head),
                    "POST /api/upload HTTP/1.1\r\n"
                    "Host: %s:%u\r\n"
                    "Connection: keep-alive\r\n"
                    "Content-Type: image/jpeg\r\n"
                    "Content-Length: %u\r\n"
                    "X-Seq: %u\r\n"
                    "X-Telemetry: %s\r\n\r\n",
                    VPS_HOST, (unsigned)VPS_PORT, (unsigned)f->len, (unsigned)f->seq, telem);

  const uint32_t t0 = millis();
  if (client.write((const uint8_t*)head, hl) != (size_t)hl) return false;

  size_t sent = 0;
  while (sent < f->len) {
    size_t chunk = f->len - sent;
    if (chunk > 4096) chunk = 4096;
    size_t w = client.write(f->buf + sent, chunk);
    if (w == 0) return false;
    sent += w;
  }

  if (!readServerReply(client, 6000)) return false;

  uint32_t dt = millis() - t0;
  portENTER_CRITICAL(&statMux);
  stSent++;
  stBytes += f->len;
  stReplyMsSum += dt;
  if (dt > stReplyMsMax) stReplyMsMax = dt;
  portEXIT_CRITICAL(&statMux);
  return true;
}

void uploadTask(void* arg) {
  WiFiClient client;
  for (;;) {
    Frame* f = NULL;
    if (xQueueReceive(frameQueue, &f, portMAX_DELAY) != pdTRUE || !f) continue;

    bool ok = false;
    if (WiFi.status() == WL_CONNECTED) {
      ok = sendFrame(client, f);
    }
    freeFrame(f);

    if (!ok) {
      client.stop();
      portENTER_CRITICAL(&statMux);
      stFail++;
      portEXIT_CRITICAL(&statMux);
      vTaskDelay(pdMS_TO_TICKS(WiFi.status() == WL_CONNECTED ? 150 : 500));
    }
  }
}

// ----------------------------------------------------------------------------
// 8. SETUP / LOOP (Core 1)
// ----------------------------------------------------------------------------
void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);   // отключает brownout-ресет (маскирует проблемы питания!)
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n[SYS] reset=%d, PSRAM=%s, свободно %u KB\n", (int)esp_reset_reason(),
                psramFound() ? "есть" : "НЕТ", (unsigned)(ESP.getFreePsram() / 1024));

  uartMutex = xSemaphoreCreateMutex();
  ArduinoSerial.setRxBufferSize(512);
  ArduinoSerial.begin(115200, SERIAL_8N1, ARDUINO_RX_PIN, ARDUINO_TX_PIN);

  if (!initCamera()) {
    Serial.println("[CAM] камера не запустилась, перезагрузка через 5 с");
    delay(5000);
    ESP.restart();
  }

  frameQueue = xQueueCreate(2, sizeof(Frame*));

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[WIFI] Подключение к " + String(WIFI_SSID));
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(200); Serial.print("."); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WIFI] Подключено! IP: " + WiFi.localIP().toString() + ", RSSI " + String(WiFi.RSSI()) + " dBm");
  } else {
    Serial.println("\n[WIFI] Пока нет связи, продолжаю в фоне");
  }

  for (int i = 0; i < UPLOAD_CONNECTIONS; i++) {
    xTaskCreatePinnedToCore(uploadTask, "up", 6144, NULL, 2, NULL, 0);
  }
  Serial.printf("[SYSTEM] Запущено %d параллельных соединений\n", UPLOAD_CONNECTIONS);
}

void loop() {
  static uint32_t statAt = millis();
  static uint8_t camFails = 0;

  pollArduinoTelemetry();

  camera_fb_t* fb = esp_camera_fb_get();   // блокируется до свежего кадра
  if (!fb) {
    if (++camFails >= 50) { Serial.println("[CAM] кадры не приходят, перезагрузка"); ESP.restart(); }
    delay(10);
    return;
  }
  camFails = 0;

  Frame* f = makeFrame(fb);
  esp_camera_fb_return(fb);                // драйвер сразу снимает следующий кадр
  if (f) {
    portENTER_CRITICAL(&statMux);
    stCaptured++;
    portEXIT_CRITICAL(&statMux);

    if (xQueueSend(frameQueue, &f, 0) != pdTRUE) {
      Frame* old = NULL;                   // очередь полна: выкидываем самый старый, ставим свежий
      if (xQueueReceive(frameQueue, &old, 0) == pdTRUE && old) {
        freeFrame(old);
        portENTER_CRITICAL(&statMux);
        stDropped++;
        portEXIT_CRITICAL(&statMux);
      }
      if (xQueueSend(frameQueue, &f, 0) != pdTRUE) freeFrame(f);
    }
  }

  if (millis() - statAt >= 5000) {
    portENTER_CRITICAL(&statMux);
    uint32_t sent = stSent, bytes = stBytes, rsum = stReplyMsSum, rmax = stReplyMsMax,
             fail = stFail, drop = stDropped, cap = stCaptured;
    stSent = stBytes = stReplyMsSum = stReplyMsMax = stFail = stDropped = stCaptured = 0;
    portEXIT_CRITICAL(&statMux);
    float sec = (millis() - statAt) / 1000.0f;
    Serial.printf("[STAT] отправлено %.1f fps (снято %.1f) | кадр %u КБ | ответ сервера ср. %u мс, макс. %u мс | "
                  "пропущено %u, ошибок %u | %.0f КБ/с | RSSI %d dBm | PSRAM %u КБ\n",
                  sent / sec, cap / sec, sent ? (unsigned)(bytes / sent / 1024) : 0,
                  sent ? (unsigned)(rsum / sent) : 0, (unsigned)rmax, (unsigned)drop, (unsigned)fail,
                  bytes / 1024.0f / sec, WiFi.RSSI(), (unsigned)(ESP.getFreePsram() / 1024));
    statAt = millis();
  }
}
