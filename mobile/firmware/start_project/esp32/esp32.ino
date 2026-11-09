#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// =========================================================================
// ПИНЫ (датчик света убран, он теперь на голове: Arduino A0/A5)
// =========================================================================
#define REED_PIN        4     // Геркон (к GND, INPUT_PULLUP)
#define BTN_PIN         21    // Кнопка сброса/теста тревоги (к GND, INPUT_PULLUP)
#define LED_PIN         16    // Светодиод тревоги
#define BUZZER_PIN      5     // Пассивный пьезодинамик

// =========================================================================
// BLE UUID (согласованы с приложением Flet)
// =========================================================================
#define SERVICE_UUID           "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID_TX "beb5483e-36e1-4688-b7f5-ea07361b26a8" // NOTIFY
#define CHARACTERISTIC_UUID_RX "6d68efe5-04b6-4a85-abc4-c2670b7bf7fd" // WRITE

// =========================================================================
// НОТЫ И МЕЛОДИЯ FNAF
// =========================================================================
#define NOTE_C4  262
#define NOTE_D4  294
#define NOTE_E4  330
#define NOTE_F4  349
#define NOTE_G4  392
#define NOTE_A4  440
#define NOTE_AS4 466
#define NOTE_C5  523
#define NOTE_D5  587

const int fnafMelody[] = {
  NOTE_F4, NOTE_C5, NOTE_AS4, NOTE_A4, NOTE_G4, NOTE_F4,
  NOTE_C4, NOTE_F4, NOTE_A4, NOTE_G4, NOTE_F4, NOTE_E4, NOTE_D4, NOTE_C4,
  NOTE_F4, NOTE_C5, NOTE_AS4, NOTE_A4, NOTE_G4, NOTE_F4,
  NOTE_C5, NOTE_D5, NOTE_C5, NOTE_AS4, NOTE_A4, NOTE_G4, NOTE_F4
};

const int fnafDurations[] = {
  4, 4, 8, 8, 4, 4,
  4, 4, 8, 8, 4, 8, 8, 2,
  4, 4, 8, 8, 4, 4,
  4, 4, 8, 8, 4, 4, 2
};

const int totalNotes = sizeof(fnafMelody) / sizeof(fnafMelody[0]);

// =========================================================================
// ГЛОБАЛЬНЫЕ СОСТОЯНИЯ
// =========================================================================
BLEServer* pServer = nullptr;
BLECharacteristic* pTxCharacteristic = nullptr;
BLECharacteristic* pRxCharacteristic = nullptr;

volatile bool deviceConnected = false;
bool oldDeviceConnected = false;

volatile bool alarmActive = false;
volatile bool doorOpen = false;
bool buttonPressed = false;
volatile bool telemetryPending = false;   // запрос на внеочередную отправку

unsigned long lastTelemetryTime = 0;
const unsigned long TELEMETRY_INTERVAL = 500;

unsigned long lastReedChange = 0;
const unsigned long REED_DEBOUNCE_MS = 50;

TaskHandle_t AlarmTaskHandle = NULL;

// =========================================================================
// BLE CALLBACKS
// =========================================================================
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* s) override {
    deviceConnected = true;
    telemetryPending = true;               // сразу отдать текущее состояние
    Serial.println("[BLE] Клиент подключился (Flet App)");
  }
  void onDisconnect(BLEServer* s) override {
    deviceConnected = false;
    Serial.println("[BLE] Клиент отключился");
  }
};

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    String rx = c->getValue().c_str();
    rx.trim();
    if (rx.length() == 0) return;

    Serial.print("[BLE RX] Команда: ");
    Serial.println(rx);

    if (rx == "RESET" || rx == "ALARM_OFF") {
      alarmActive = false;
      Serial.println("[ALARM] Тревога сброшена из приложения");
    } else if (rx == "ALARM_ON") {
      alarmActive = true;
      Serial.println("[ALARM] Тревога включена из приложения");
    }
    telemetryPending = true;               // отправим из loop(), не из колбэка
  }
};

// =========================================================================
// ФОНОВАЯ ЗАДАЧА: СИРЕНА FNAF + LED
// =========================================================================
void AlarmTask(void* pvParameters) {
  for (;;) {
    if (alarmActive) {
      for (int i = 0; i < totalNotes; i++) {
        if (!alarmActive) break;

        int noteDuration = 1000 / fnafDurations[i];
        int pause = noteDuration * 30 / 100;

        digitalWrite(LED_PIN, HIGH);
        tone(BUZZER_PIN, fnafMelody[i]);
        vTaskDelay(pdMS_TO_TICKS(noteDuration));

        noTone(BUZZER_PIN);
        digitalWrite(LED_PIN, LOW);
        vTaskDelay(pdMS_TO_TICKS(pause));
      }
      noTone(BUZZER_PIN);
      digitalWrite(LED_PIN, LOW);
      vTaskDelay(pdMS_TO_TICKS(500));
    } else {
      noTone(BUZZER_PIN);
      digitalWrite(LED_PIN, LOW);
      vTaskDelay(pdMS_TO_TICKS(50));
    }
  }
}

// =========================================================================
// ОТПРАВКА ТЕЛЕМЕТРИИ: DOOR=..;ALARM=..;BTN=..
// =========================================================================
void sendTelemetry() {
  if (!deviceConnected) return;

  char buffer[48];
  snprintf(buffer, sizeof(buffer), "DOOR=%d;ALARM=%d;BTN=%d\n",
           doorOpen ? 1 : 0,
           alarmActive ? 1 : 0,
           buttonPressed ? 1 : 0);

  pTxCharacteristic->setValue((uint8_t*)buffer, strlen(buffer));
  pTxCharacteristic->notify();
}

// =========================================================================
// SETUP
// =========================================================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== DoorGuardian ESP32 (без датчика света) ===");

  pinMode(REED_PIN, INPUT_PULLUP);
  pinMode(BTN_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // Начальное состояние двери, чтобы не было ложной тревоги при старте
  doorOpen = (digitalRead(REED_PIN) == HIGH);

  xTaskCreatePinnedToCore(AlarmTask, "AlarmTask", 4096, NULL, 1, &AlarmTaskHandle, 1);

  BLEDevice::init("DoorGuardian");
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService* pService = pServer->createService(SERVICE_UUID);

  pTxCharacteristic = pService->createCharacteristic(
      CHARACTERISTIC_UUID_TX, BLECharacteristic::PROPERTY_NOTIFY);
  pTxCharacteristic->addDescriptor(new BLE2902());

  pRxCharacteristic = pService->createCharacteristic(
      CHARACTERISTIC_UUID_RX,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  pRxCharacteristic->setCallbacks(new RxCallbacks());

  pService->start();

  BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  pAdvertising->setMinPreferred(0x06);
  pAdvertising->setMinPreferred(0x12);
  BLEDevice::startAdvertising();

  Serial.println("[BLE] В эфире как 'DoorGuardian'");
  Serial.print("[SYSTEM] Дверь при старте: ");
  Serial.println(doorOpen ? "ОТКРЫТА" : "закрыта");
}

// =========================================================================
// LOOP
// =========================================================================
void loop() {
  unsigned long now = millis();

  // 1. Геркон с антидребезгом (HIGH = магнит отошёл = дверь открыта)
  bool reedOpen = (digitalRead(REED_PIN) == HIGH);
  if (reedOpen != doorOpen && (now - lastReedChange) > REED_DEBOUNCE_MS) {
    lastReedChange = now;
    doorOpen = reedOpen;
    if (doorOpen) {
      Serial.println("[EVENT] ДВЕРЬ ОТКРЫТА!");
      alarmActive = true;
    } else {
      Serial.println("[EVENT] Дверь закрыта");
    }
    sendTelemetry();                        // мгновенно в приложение
  }

  // 2. Кнопка: сброс тревоги / тестовая тревога
  if (digitalRead(BTN_PIN) == LOW) {
    if (!buttonPressed) {
      buttonPressed = true;
      alarmActive = !alarmActive;
      Serial.println(alarmActive ? "[BUTTON] Тестовая тревога"
                                 : "[BUTTON] Тревога сброшена");
      sendTelemetry();
    }
  } else if (buttonPressed) {
    buttonPressed = false;
    sendTelemetry();
  }

  // 3. Внеочередная отправка (после подключения или BLE-команды)
  if (telemetryPending) {
    telemetryPending = false;
    sendTelemetry();
  }

  // 4. Периодическая телеметрия
  if (now - lastTelemetryTime >= TELEMETRY_INTERVAL) {
    lastTelemetryTime = now;
    sendTelemetry();
  }

  // 5. Перезапуск рекламы после отключения
  if (!deviceConnected && oldDeviceConnected) {
    delay(500);
    pServer->startAdvertising();
    Serial.println("[BLE] Перезапуск рекламы...");
    oldDeviceConnected = false;
  }
  if (deviceConnected && !oldDeviceConnected) {
    oldDeviceConnected = true;
  }

  delay(20);
}