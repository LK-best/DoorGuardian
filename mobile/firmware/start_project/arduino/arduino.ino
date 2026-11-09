#include <Servo.h>
#include <EEPROM.h>
#include <IRremote.hpp>

// --- ПИНЫ ---
#define SERVO_PAN_PIN   10
#define SERVO_TILT_PIN  11
#define TRIG_PIN        12
#define ECHO_PIN        13
#define LIGHT_AO_PIN    A0
#define LIGHT_DO_PIN    A5
#define IR_PIN          A4

// --- КАЛИБРОВКА СЕРВОПРИВОДОВ ---
// Горизонт (Pan): механический сектор 150° - 181°, центр 165°, софтовый лимит 300
#define PAN_MIN         150
#define PAN_MAX         181
#define PAN_CENTER      165
#define PAN_LIMIT_MAX   300

// Вертикаль (Tilt): сектор 85° - 165°, центр 125°
#define TILT_MIN        85
#define TILT_MAX        165
#define TILT_CENTER     125

#define STEP_ANGLE      6

Servo servoPan;
Servo servoTilt;

int currentPan = PAN_CENTER;
int currentTilt = TILT_CENTER;

unsigned long lastTelemetryTime = 0;
const unsigned long TELEMETRY_INTERVAL = 250; // Отправка телеметрии каждые 250 мс

void loadFromEEPROM() {
  EEPROM.get(0, currentPan);
  EEPROM.get(2, currentTilt);
  if (currentPan < PAN_MIN || currentPan > PAN_LIMIT_MAX) currentPan = PAN_CENTER;
  if (currentTilt < TILT_MIN || currentTilt > TILT_MAX) currentTilt = TILT_CENTER;
}

void saveToEEPROM() {
  EEPROM.put(0, currentPan);
  EEPROM.put(2, currentTilt);
}

void updateServos() {
  servoPan.write(currentPan);
  servoTilt.write(currentTilt);
}

long readDistanceCM() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duration = pulseIn(ECHO_PIN, HIGH, 25000); // таймаут 25 мс (~4.3 м)
  if (duration <= 0) return -1;
  return duration / 58.2;
}

void setup() {
  Serial.begin(9600);

  servoPan.attach(SERVO_PAN_PIN);
  servoTilt.attach(SERVO_TILT_PIN);

  loadFromEEPROM();
  updateServos();

  pinMode(TRIG_PIN, OUTPUT);
  digitalWrite(TRIG_PIN, LOW);

  // Инициализация ИК без захвата 13 пина (чтобы не конфликтовал с Echo)
  IrReceiver.begin(IR_PIN, DISABLE_LED_FEEDBACK);
  pinMode(ECHO_PIN, INPUT); // СТРОГО ПОСЛЕ IR!

  pinMode(LIGHT_DO_PIN, INPUT);
}

void loop() {
  // 1. Прием команд от ESP32-S3
  if (Serial.available() > 0) {
    char cmd = Serial.read();
    bool moved = false;

    switch (cmd) {
      case 'U': // Вверх (инверсия: уменьшаем угол наклона)
        if (currentTilt - STEP_ANGLE >= TILT_MIN) {
          currentTilt -= STEP_ANGLE;
          moved = true;
        }
        break;
      case 'D': // Вниз (инверсия: увеличиваем угол наклона)
        if (currentTilt + STEP_ANGLE <= TILT_MAX) {
          currentTilt += STEP_ANGLE;
          moved = true;
        }
        break;
      case 'L': // Влево
        if (currentPan - STEP_ANGLE >= PAN_MIN) {
          currentPan -= STEP_ANGLE;
          moved = true;
        }
        break;
      case 'R': // Вправо
        if (currentPan + STEP_ANGLE <= PAN_MAX) {
          currentPan += STEP_ANGLE;
          moved = true;
        }
        break;
      case 'C': // Центрирование
      case 'S':
        currentPan = PAN_CENTER;
        currentTilt = TILT_CENTER;
        moved = true;
        break;
      default:
        // Все остальные байты (шум, \r, \n) отбрасываются
        break;
    }

    if (moved) {
      updateServos();
      saveToEEPROM();
    }
  }

  // 2. Периодическая посылка телеметрии в ESP32-S3
  unsigned long now = millis();
  if (now - lastTelemetryTime >= TELEMETRY_INTERVAL) {
    lastTelemetryTime = now;

    long dist = readDistanceCM();
    int lightVal = analogRead(LIGHT_AO_PIN);

    // Формат пакета: DIST=XX;LIGHT=YY;PAN=ZZ;TILT=WW\n
    Serial.print("DIST=");
    Serial.print(dist);
    Serial.print(";LIGHT=");
    Serial.print(lightVal);
    Serial.print(";PAN=");
    Serial.print(currentPan);
    Serial.print(";TILT=");
    Serial.print(currentTilt);
    Serial.println();
  }

  // 3. Обработка ИК пульта
  if (IrReceiver.decode()) {
    IrReceiver.resume();
  }
}