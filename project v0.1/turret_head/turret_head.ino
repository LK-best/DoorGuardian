// ============================================================================
// TURRET HEAD FIRMWARE — ARDUINO UNO + MOTOR SHIELD PRO
// ============================================================================
#include <Servo.h>
#include <EEPROM.h>

#define PAN_PIN   10   // Сервопривод поворота по горизонтали
#define TILT_PIN  11   // Сервопривод наклона по вертикали

#define TRIG_PIN  12   // HC-SR04 Trig
#define ECHO_PIN  13   // HC-SR04 Echo
#define LIGHT_PIN A0   // Аналоговый датчик освещенности

// Строгие лимиты сервоприводов
const int PAN_MIN = 150;
const int PAN_MAX = 181;
const int PAN_CENTER = 165;

const int TILT_MIN = 85;
const int TILT_MAX = 165;
const int TILT_CENTER = 125;

const int STEP_ANGLE = 2; // Шаг одного движения

Servo panServo;
Servo tiltServo;

int currentPan = PAN_CENTER;
int currentTilt = TILT_CENTER;

// Адреса EEPROM
const int EEPROM_PAN_ADDR = 0;
const int EEPROM_TILT_ADDR = 2;

// Таймеры для отложенного сохранения в EEPROM (не изнашивать память)
bool needSaveEEPROM = false;
unsigned long lastMoveTime = 0;
const unsigned long EEPROM_SAVE_DELAY = 2000; // запись через 2 сек после остановки

// Таймер телеметрии (дистанция и свет)
unsigned long lastTelemetryTime = 0;
const unsigned long TELEMETRY_INTERVAL = 150; // каждые 150 мс

// Измерение расстояния (HC-SR04)
int getDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  
  long duration = pulseIn(ECHO_PIN, HIGH, 25000); // таймаут 25 мс (~4 метра)
  if (duration == 0) return 999;
  return (int)(duration * 0.034 / 2);
}

void setup() {
  Serial.begin(115200); // Высокая скорость связи с ESP32-S3

  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(LIGHT_PIN, INPUT);

  panServo.attach(PAN_PIN);
  tiltServo.attach(TILT_PIN);

  // Считывание калибровки из EEPROM (с лимитом проверки 300)
  int savedPan = 0;
  int savedTilt = 0;
  EEPROM.get(EEPROM_PAN_ADDR, savedPan);
  EEPROM.get(EEPROM_TILT_ADDR, savedTilt);

  // Проверка валидности (защитный порог 300)
  if (savedPan >= PAN_MIN && savedPan <= 300 && savedPan <= PAN_MAX) {
    currentPan = savedPan;
  } else {
    currentPan = PAN_CENTER;
  }

  if (savedTilt >= TILT_MIN && savedTilt <= 300 && savedTilt <= TILT_MAX) {
    currentTilt = savedTilt;
  } else {
    currentTilt = TILT_CENTER;
  }

  panServo.write(currentPan);
  tiltServo.write(currentTilt);
}

void loop() {
  // 1. Прием команд от ESP32-S3 по UART
  while (Serial.available() > 0) {
    char cmd = Serial.read();
    bool moved = false;

    switch (cmd) {
      case 'L': // Влево
        if (currentPan + STEP_ANGLE <= PAN_MAX) {
          currentPan += STEP_ANGLE;
          moved = true;
        }
        break;

      case 'R': // Вправо
        if (currentPan - STEP_ANGLE >= PAN_MIN) {
          currentPan -= STEP_ANGLE;
          moved = true;
        }
        break;

      case 'U': // ВВЕРХ: увеличиваем наклон (если у вас раньше опускалось, этот знак исправлен)
        if (currentTilt + STEP_ANGLE <= TILT_MAX) {
          currentTilt += STEP_ANGLE;
          moved = true;
        }
        break;

      case 'D': // ВНИЗ: уменьшаем наклон
        if (currentTilt - STEP_ANGLE >= TILT_MIN) {
          currentTilt -= STEP_ANGLE;
          moved = true;
        }
        break;

      case 'C': // Центрирование
        currentPan = PAN_CENTER;
        currentTilt = TILT_CENTER;
        moved = true;
        break;
    }

    if (moved) {
      panServo.write(currentPan);
      tiltServo.write(currentTilt);
      lastMoveTime = millis();
      needSaveEEPROM = true;
    }
  }

  // 2. Отложенное сохранение углов в EEPROM
  if (needSaveEEPROM && (millis() - lastMoveTime >= EEPROM_SAVE_DELAY)) {
    EEPROM.put(EEPROM_PAN_ADDR, currentPan);
    EEPROM.put(EEPROM_TILT_ADDR, currentTilt);
    needSaveEEPROM = false;
  }

  // 3. Отправка телеметрии обратно в ESP32-S3
  if (millis() - lastTelemetryTime >= TELEMETRY_INTERVAL) {
    lastTelemetryTime = millis();
    int dist = getDistance();
    int light = analogRead(LIGHT_PIN);

    // Формат: D:<дистанция>;L:<свет>;P:<pan>;T:<tilt>
    Serial.print("D:");
    Serial.print(dist);
    Serial.print(";L:");
    Serial.print(light);
    Serial.print(";P:");
    Serial.print(currentPan);
    Serial.print(";T:");
    Serial.println(currentTilt);
  }
}