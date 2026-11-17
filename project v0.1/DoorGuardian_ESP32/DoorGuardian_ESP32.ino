// DoorGuardian (ESP32 Dev Module) -> VPS relay (server.py)
//
// Что исправлено:
//  * Сеть вынесена в отдельную FreeRTOS-задачу. Раньше HTTP-запрос (до 350 мс + коннект) выполнялся прямо в loop()
//    каждую секунду: сирена "заикалась", а кнопка и геркон опрашивались с провалами.
//  * Охрана работает и БЕЗ Wi-Fi: раньше setup() вис в while(WiFi.status()!=CONNECTED) и без точки доступа
//    дверь вообще не охранялась.
//  * Keep-alive соединение (setReuse) вместо нового TCP на каждый отчёт; таймаут коннекта 1.5 с.
//  * Антидребезг геркона (вибрация двери не даёт ложной тревоги), смена состояния двери уходит на сервер сразу.
//  * Убраны блокирующие delay()/tone(...,duration): писки идут по таймеру.
//  * Исправлено переполнение unsigned при расчёте секунд обратного отсчёта.

#include <WiFi.h>
#include <HTTPClient.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

// ==========================================
// 1. НАСТРОЙКИ СЕТИ И СЕРВЕРА
// ==========================================
const char* ssid     = "Galaxy M21";
const char* password = "67676767";

const char* vps_host = "188.120.230.91";   // IP VPS
const int   vps_port = 18080;

const uint32_t HEARTBEAT_MS = 1000;        // как часто слать статус, когда ничего не меняется

// ==========================================
// 2. ПИНЫ ОБОРУДОВАНИЯ
// ==========================================
#define REED_PIN      4   // Геркон двери (на контакт GND)
#define BUTTON_PIN    21  // Кнопка постановки / сброса (на GND)
#define LED_PIN       16  // Светодиод индикации
#define BUZZER_PIN    5   // Пассивный зуммер

// Если при закрытой двери пишет "ОТКРЫТА", смените HIGH на LOW
#define REED_OPEN_LEVEL HIGH
const uint32_t REED_DEBOUNCE_MS = 80;

enum SecurityState { STATE_DISARMED, STATE_ARMING, STATE_ARMED, STATE_ALARM };

// Эти переменные читает и пишет и loop(), и сетевая задача
volatile SecurityState currentState = STATE_DISARMED;
volatile bool isDoorOpen = false;
volatile uint32_t armingStartTime = 0;
volatile bool needImmediateReport = false;
volatile uint8_t remoteCmd = 0;            // 0 нет, 1 ARM, 2 DISARM (команда от сервера, применяется в loop)

const uint32_t ARMING_DELAY_MS = 15000;
uint32_t lastCountdownBeep = 0;

// ==========================================
// 3. МЕЛОДИЯ FNAF (Куплеты Тореадора)
// ==========================================
#define NOTE_REST 0
#define NOTE_C4   262
#define NOTE_CS4  277
#define NOTE_D4   294
#define NOTE_DS4  311
#define NOTE_E4   330
#define NOTE_F4   349
#define NOTE_FS4  370
#define NOTE_G4   392
#define NOTE_GS4  415
#define NOTE_A4   440
#define NOTE_AS4  466
#define NOTE_B4   494
#define NOTE_C5   523
#define NOTE_CS5  554
#define NOTE_D5   587

const int fnafMelody[] = {
  NOTE_A4, NOTE_A4, NOTE_A4, NOTE_F4, NOTE_D4, NOTE_A4, NOTE_F4, NOTE_D4,
  NOTE_D4, NOTE_F4, NOTE_A4, NOTE_G4, NOTE_F4, NOTE_E4, NOTE_D4,
  NOTE_CS4, NOTE_E4, NOTE_G4, NOTE_AS4, NOTE_A4, NOTE_G4, NOTE_F4, NOTE_E4,
  NOTE_D4, NOTE_F4, NOTE_A4, NOTE_D5, NOTE_CS5, NOTE_B4, NOTE_A4, NOTE_G4,
  NOTE_F4, NOTE_E4, NOTE_D4, NOTE_REST
};

const int noteDurations[] = {
  280, 140, 140, 280, 280, 280, 140, 140,
  280, 280, 420, 140, 140, 140, 280,
  280, 280, 420, 140, 140, 140, 280,
  280, 280, 420, 140, 140, 140, 280,
  280, 280, 600, 200
};
const int melodyLength = sizeof(fnafMelody) / sizeof(fnafMelody[0]);

uint32_t noteStartTime = 0;
int currentNoteIdx = 0;
bool isNotePlaying = false;

// Короткие писки (не блокируют loop)
uint32_t beepEndAt = 0;
uint32_t chirpAt = 0;
uint8_t chirpStep = 0;   // 0 нет, 1 первый писк идёт, 2 ждём второй

void beep(int freq, uint32_t ms) {
  tone(BUZZER_PIN, freq);
  beepEndAt = millis() + ms;
  if (beepEndAt == 0) beepEndAt = 1;
}

// Таймеры
uint32_t lastLedBlinkTime = 0;
bool ledState = false;

// Антидребезг кнопки
bool lastButtonReading = HIGH;
uint32_t lastDebounceTime = 0;
const uint32_t debounceDelay = 50;

// Антидребезг геркона
bool reedCandidate = false;
uint32_t reedCandidateSince = 0;

// ==========================================
// 4. СВЯЗЬ С VPS (отдельная задача)
// ==========================================
String stateString() {
  switch (currentState) {
    case STATE_ARMING: {
      uint32_t elapsed = millis() - armingStartTime;
      int secLeft = elapsed >= ARMING_DELAY_MS ? 0 : (int)((ARMING_DELAY_MS - elapsed) / 1000);
      return "ARMING_" + String(secLeft) + "s";
    }
    case STATE_ARMED:  return "ARMED";
    case STATE_ALARM:  return "ALARM";
    default:           return "DISARMED";
  }
}

void sendReportToVPS(HTTPClient& http) {
  String url = "http://" + String(vps_host) + ":" + String(vps_port) +
               "/api/door_report?door=" + (isDoorOpen ? "OPEN" : "CLOSED") +
               "&state=" + stateString();

  http.begin(url);
  int httpCode = http.GET();

  if (httpCode == HTTP_CODE_OK) {
    String payload = http.getString();
    payload.trim();
    if (payload == "ARM") remoteCmd = 1;
    else if (payload == "DISARM") remoteCmd = 2;
  }
  http.end();   // при setReuse(true) соединение остаётся открытым
}

void networkTask(void* arg) {
  HTTPClient http;
  http.setReuse(true);
  http.setConnectTimeout(1500);
  http.setTimeout(1500);

  uint32_t lastSend = 0;
  uint32_t lastReconnect = 0;

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      if (millis() - lastReconnect > 10000) {
        lastReconnect = millis();
        WiFi.reconnect();
      }
      vTaskDelay(pdMS_TO_TICKS(200));
      continue;
    }

    if (needImmediateReport || millis() - lastSend >= HEARTBEAT_MS) {
      needImmediateReport = false;   // сбрасываем ДО отправки: событие во время запроса не потеряется
      lastSend = millis();
      sendReportToVPS(http);
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ==========================================
// 5. ОБРАБОТКА ДАТЧИКА, ТАЙМЕРА И СИРЕНЫ
// ==========================================
void startArming() {
  currentState = STATE_ARMING;
  armingStartTime = millis();
  lastCountdownBeep = 0;
  needImmediateReport = true;
  Serial.println("[SECURITY] Внимание! 15 секунд на выход из помещения...");
}

void disarm() {
  currentState = STATE_DISARMED;
  noTone(BUZZER_PIN);
  isNotePlaying = false;
  currentNoteIdx = 0;
  beepEndAt = 0;
  chirpStep = 0;
  needImmediateReport = true;
  Serial.println("[SECURITY] Снято с охраны");
}

void applyRemoteCommand() {
  uint8_t cmd = remoteCmd;
  if (cmd == 0) return;
  remoteCmd = 0;
  if (cmd == 1 && currentState == STATE_DISARMED) {
    Serial.println("[VPS] Запуск задержки на постановку");
    startArming();
  } else if (cmd == 2 && currentState != STATE_DISARMED) {
    Serial.println("[VPS] Снято с охраны");
    disarm();
  }
}

void handleDoorSensor() {
  // антидребезг: новое значение должно продержаться REED_DEBOUNCE_MS
  bool raw = (digitalRead(REED_PIN) == REED_OPEN_LEVEL);
  if (raw != reedCandidate) {
    reedCandidate = raw;
    reedCandidateSince = millis();
  }
  if (reedCandidate != isDoorOpen && millis() - reedCandidateSince >= REED_DEBOUNCE_MS) {
    isDoorOpen = reedCandidate;
    needImmediateReport = true;   // открытие/закрытие сразу уходит на сервер
  }

  if (currentState == STATE_ARMING) {
    uint32_t elapsed = millis() - armingStartTime;

    uint32_t interval = (elapsed > 11000) ? 350 : 1000;   // быстрее в последние секунды
    if (millis() - lastCountdownBeep >= interval) {
      lastCountdownBeep = millis();
      beep(1800, 60);
    }

    if (elapsed >= ARMING_DELAY_MS) {
      currentState = STATE_ARMED;
      beep(2400, 120);
      chirpStep = 1;              // второй писк сыграет updateBuzzer()
      needImmediateReport = true;
      Serial.println("[SECURITY] СИСТЕМА НА ОХРАНЕ!");
    }
    return;   // во время отсчета тревога не срабатывает
  }

  if (currentState == STATE_ARMED && isDoorOpen) {
    currentState = STATE_ALARM;
    currentNoteIdx = 0;
    isNotePlaying = false;
    beepEndAt = 0;
    chirpStep = 0;
    needImmediateReport = true;
    Serial.println("[ALERT] ТРЕВОГА! ДВЕРЬ ОТКРЫТА!");
  }
}

void handleButton() {
  int reading = digitalRead(BUTTON_PIN);
  if (reading != lastButtonReading) {
    lastDebounceTime = millis();
  }

  if ((millis() - lastDebounceTime) > debounceDelay) {
    static bool buttonProcessed = false;
    if (reading == LOW && !buttonProcessed) {
      buttonProcessed = true;
      if (currentState != STATE_DISARMED) disarm();
      else startArming();
    } else if (reading == HIGH) {
      buttonProcessed = false;
    }
  }
  lastButtonReading = reading;
}

void updateBuzzer() {
  uint32_t now = millis();

  if (currentState != STATE_ALARM) {
    if (isNotePlaying) {
      noTone(BUZZER_PIN);
      isNotePlaying = false;
      currentNoteIdx = 0;
    }
    // короткие писки
    if (beepEndAt && now >= beepEndAt) {
      noTone(BUZZER_PIN);
      beepEndAt = 0;
      if (chirpStep == 1) { chirpStep = 2; chirpAt = now + 30; }
    }
    if (chirpStep == 2 && now >= chirpAt) {
      chirpStep = 0;
      beep(3000, 200);
    }
    return;
  }

  int noteDur = noteDurations[currentNoteIdx];

  if (!isNotePlaying) {
    int freq = fnafMelody[currentNoteIdx];
    if (freq > 0) tone(BUZZER_PIN, freq);
    else noTone(BUZZER_PIN);
    noteStartTime = now;
    isNotePlaying = true;
  } else if (now - noteStartTime >= (uint32_t)noteDur) {
    noTone(BUZZER_PIN);
    if (now - noteStartTime >= (uint32_t)(noteDur + 35)) {
      isNotePlaying = false;
      currentNoteIdx++;
      if (currentNoteIdx >= melodyLength) currentNoteIdx = 0;
    }
  }
}

void updateLed() {
  uint32_t now = millis();
  switch (currentState) {
    case STATE_DISARMED:
      digitalWrite(LED_PIN, LOW);
      break;
    case STATE_ARMING:
      if (now - lastLedBlinkTime >= 250) {
        lastLedBlinkTime = now;
        ledState = !ledState;
        digitalWrite(LED_PIN, ledState);
      }
      break;
    case STATE_ARMED:
      if (now - lastLedBlinkTime >= 1500) {
        lastLedBlinkTime = now;
        ledState = !ledState;
        digitalWrite(LED_PIN, ledState);
      }
      break;
    case STATE_ALARM:
      if (now - lastLedBlinkTime >= 70) {
        lastLedBlinkTime = now;
        ledState = !ledState;
        digitalWrite(LED_PIN, ledState);
      }
      break;
  }
}

void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);   // отключает brownout-ресет (скрывает проблемы питания!)
  Serial.begin(115200);

  pinMode(REED_PIN, INPUT_PULLUP);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);

  isDoorOpen = (digitalRead(REED_PIN) == REED_OPEN_LEVEL);
  reedCandidate = isDoorOpen;
  beep(1200, 80);

  // Wi-Fi НЕ блокирует запуск: охрана работает и без сети, сеть подтянется сама
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.begin(ssid, password);
  Serial.println("[WIFI] Подключение к " + String(ssid) + " (в фоне)");

  xTaskCreate(networkTask, "net", 8192, NULL, 1, NULL);
}

void loop() {
  applyRemoteCommand();
  handleDoorSensor();
  handleButton();
  updateBuzzer();
  updateLed();

  static bool wasConnected = false;
  bool nowConnected = (WiFi.status() == WL_CONNECTED);
  if (nowConnected && !wasConnected) {
    Serial.println("[WIFI] Готово! IP: " + WiFi.localIP().toString());
    needImmediateReport = true;
  }
  wasConnected = nowConnected;

  delay(2);   // отдаём время системе; на цикл опроса не влияет
}
