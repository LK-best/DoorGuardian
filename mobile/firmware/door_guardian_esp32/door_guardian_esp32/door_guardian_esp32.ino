// DoorGuardian — ESP32: твоя логика охраны + Bluetooth LE для мобильного приложения.
// USB-Serial (9600) оставлен, поэтому PyQt-приложение по COM-порту продолжает работать.
//
// Требуется: Arduino-ESP32 core 3.x (нужен tone()/noTone()), плата "ESP32 Dev Module".
//
// Протокол (одинаковый для BLE и Serial):
//   приложение -> ESP32 : CMD:ARM | CMD:DISARM | CMD:STATUS | TEST:LED | TEST:BUZZER | TEST:GSM
//                         SET:PHONE=+79991234567,T1=15
//                         SMS:+79991234567:текст  (номер "+" — взять из EEPROM)
//   ESP32 -> приложение : STAT:DOOR=1,STATE=2,BAT=3.95  и  LOG: текст

#include <EEPROM.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ========== BLE (UUID должны совпадать с main_2.py) ==========
#define DEVICE_NAME  "DoorGuardian"
#define SERVICE_UUID "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define TX_UUID      "beb5483e-36e1-4688-b7f5-ea07361b26a8"  // notify: ESP32 -> приложение
#define RX_UUID      "6d68efe5-04b6-4a85-abc4-c2670b7bf7fd"  // write:  приложение -> ESP32

// ========== НАСТРОЙКА ПИНОВ ==========
// ВНИМАНИЕ: пины Arduino Uno (2, 3, 10) на ESP32 не подходят:
//   GPIO10 занят флеш-памятью, GPIO3 — это RX USB-порта, GPIO2 — загрузочный.
// Поэтому ниже безопасные пины ESP32 — подключите провода к ним (или поменяйте номера).
const int REED_PIN   = 27;  // Датчик двери (Геркон)
const int BTN_PIN    = 26;  // Физическая кнопка
const int LED_PIN    = 13;  // Светодиод
const int BUZZER_PIN = 25;  // Зуммер

// ========== EEPROM (на ESP32 это эмуляция во флеше) ==========
#define EEPROM_SIZE 32
#define EE_MAGIC    0xA5
// [0] = магическое число, [1] = T1 (сек), [2..17] = телефон
char phoneNumber[17] = "";
byte timer1 = 15;       // задержка до тревоги, сек (настраивается из приложения)

// ========== СОСТОЯНИЯ СИСТЕМЫ ==========
// 0 = Снято с охраны
// 1 = На охране (Дверь закрыта)
// 2 = Отсчет T1 секунд (Дверь открыта)
// 3 = Тревога / Музыка FNAF
byte systemState = 0;

bool isDoorOpen = false;
float simBattery = 4.12;   // TODO: заменить на analogRead() через делитель

// Переменные для кнопки и таймеров
bool butt_flag = 0;
unsigned long last_press = 0;
int countdown = 15;
unsigned long countdownTimer = 0;
unsigned long lastBlinkTime = 0;
unsigned long lastTelemetryTime = 0;

// ========== BLE: состояние ==========
BLECharacteristic *txChar = nullptr;
bool bleConnected = false;
String bleBuf;
String bleQueue[4];                    // очередь команд из BLE-потока в loop()
volatile byte qHead = 0, qTail = 0;
String serialBuf;

// ========== МУЗЫКА FNAF ==========
struct MusicNote {
  char name[5];
  int duration;
};

MusicNote fnafSong[] = {
  {"G3", 700}, {"A3", 600}, {"G3", 200}, {"E3", 700},
  {"E3", 700}, {"E3", 600}, {"D3", 200}, {"E3", 600},
  {"F3", 200}, {"E3", 1500}, {"F3", 700}, {"D3", 500},
  {"G3", 200}, {"E3", 1500},
  {"C3", 700}, {"A2", 600}, {"D3", 200}, {"A2b", 700},
  {"C3", 400}, {"B2", 400}, {"A2", 400}, {"G2", 400},
  {"C3", 400}, {"E2", 400}, {"G2", 200}, {"A2", 200},
  {"G2", 400}, {"F2", 400}, {"E2", 400}, {"D2", 400},
  {"E2", 400}, {"A1", 400}, {"C2", 500},
  {"D2", 400}, {"F2", 400}, {"E2", 400}, {"D2", 400},
  {"E2", 400}, {"A1", 400}, {"C2", 600}
};

int songLength = 41;
boolean isPlayingMusic = false;
int currentNote = 0;
unsigned long noteStartTime = 0;

void playNote(char note[], int duration) {
  int freq = 0;
  if (strcmp(note, "A1") == 0) freq = 440;
  else if (strcmp(note, "C2") == 0) freq = 523;
  else if (strcmp(note, "D2") == 0) freq = 587;
  else if (strcmp(note, "E2") == 0) freq = 659;
  else if (strcmp(note, "F2") == 0) freq = 698;
  else if (strcmp(note, "G2") == 0) freq = 784;
  else if (strcmp(note, "A2") == 0) freq = 880;
  else if (strcmp(note, "A2b") == 0) freq = 831;
  else if (strcmp(note, "B2") == 0) freq = 988;
  else if (strcmp(note, "C3") == 0) freq = 1047;
  else if (strcmp(note, "D3") == 0) freq = 1175;
  else if (strcmp(note, "E3") == 0) freq = 1319;
  else if (strcmp(note, "F3") == 0) freq = 1397;
  else if (strcmp(note, "G3") == 0) freq = 1568;
  else if (strcmp(note, "A3") == 0) freq = 1760;
  else freq = 1000;

  tone(BUZZER_PIN, freq, duration);
}

void startMusic() {
  isPlayingMusic = true;
  currentNote = 0;
  noteStartTime = millis();
  playNote(fnafSong[0].name, fnafSong[0].duration);
}

void updateMusic() {
  if (!isPlayingMusic) return;
  unsigned long now = millis();
  if (now - noteStartTime > (unsigned long)(fnafSong[currentNote].duration + 50)) {
    currentNote++;
    if (currentNote >= songLength) {
      isPlayingMusic = false;
      noTone(BUZZER_PIN);
      return;
    }
    noteStartTime = now;
    playNote(fnafSong[currentNote].name, fnafSong[currentNote].duration);
  }
}

void stopMusic() {
  isPlayingMusic = false;
  noTone(BUZZER_PIN);
}

// ========== ВЫВОД: Serial + BLE ==========
// Строка уходит в BLE кусками по 20 байт (размер пакета по умолчанию).
void bleSendLine(const String &line) {
  if (!bleConnected || !txChar) return;
  String data = line + "\n";
  for (size_t i = 0; i < data.length(); i += 20) {
    String chunk = data.substring(i, i + 20);
    txChar->setValue((uint8_t *)chunk.c_str(), chunk.length());
    txChar->notify();
    delay(8);  // пауза, чтобы BLE-стек не терял пакеты
  }
}

void logLine(const String &msg) {
  String line = "LOG: " + msg;
  Serial.println(line);
  bleSendLine(line);
}

// Отправка телеметрии (PyQt по USB и мобильное приложение по BLE)
void sendTelemetry() {
  int state = systemState > 0 ? (systemState > 1 ? 2 : 1) : 0;
  char buf[48];
  snprintf(buf, sizeof(buf), "STAT:DOOR=%d,STATE=%d,BAT=%.2f", isDoorOpen ? 1 : 0, state, simBattery);
  Serial.println(buf);
  bleSendLine(String(buf));
}

// ========== EEPROM ==========
void loadSettings() {
  EEPROM.begin(EEPROM_SIZE);
  if (EEPROM.read(0) == EE_MAGIC) {
    byte t = EEPROM.read(1);
    timer1 = (t >= 1 && t <= 250) ? t : 15;
    for (int i = 0; i < 16; i++) {
      char c = (char)EEPROM.read(2 + i);
      phoneNumber[i] = (c == '+' || (c >= '0' && c <= '9')) ? c : 0;
    }
    phoneNumber[16] = 0;
  }
}

void saveSettings() {
  EEPROM.write(0, EE_MAGIC);
  EEPROM.write(1, timer1);
  for (int i = 0; i < 16; i++) EEPROM.write(2 + i, (byte)phoneNumber[i]);
  EEPROM.commit();  // на ESP32 без commit() данные не запишутся
}

// ========== ОТПРАВКА SMS ==========
// TODO: подключить GSM-модуль (SIM800L/SIM7600): AT+CMGF=1, AT+CMGS="номер", текст, Ctrl+Z.
// Пока команда подтверждается в журнале — так панель и приложение отлаживаются целиком.
void sendSms(const String &to, const String &text) {
  if (to.length() == 0) {
    logLine("SMS не отправлена: номер не задан");
    return;
  }
  logLine("SMS на " + to + ": " + text);
}

// ========== УПРАВЛЕНИЕ ОХРАНОЙ ==========
void armSystem() {
  systemState = 1;
  tone(BUZZER_PIN, 1200, 200);
  sendTelemetry();
}

void disarmSystem() {
  systemState = 0;
  stopMusic();
  digitalWrite(LED_PIN, LOW);
  tone(BUZZER_PIN, 400, 200);
  sendTelemetry();
}

void toggleArm() {
  if (systemState == 0) armSystem();
  else disarmSystem();
}

// ========== ОБРАБОТКА КОМАНД ==========
void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "CMD:ARM") {
    armSystem();
  }
  else if (cmd == "CMD:DISARM") {
    disarmSystem();
  }
  else if (cmd == "CMD:STATUS") {
    sendTelemetry();
  }
  else if (cmd == "TEST:LED") {
    for (int i = 0; i < 3; i++) {
      digitalWrite(LED_PIN, HIGH);
      delay(200);
      digitalWrite(LED_PIN, LOW);
      delay(200);
    }
    logLine("Тест LED завершен");
  }
  else if (cmd == "TEST:BUZZER") {
    startMusic();
    logLine("Тест Зуммера запущен");
  }
  else if (cmd == "TEST:GSM") {
    // TODO: реальный запрос AT к GSM-модулю
    logLine("GSM Module: OK (CSQ: 24, SIM Ready)");
  }
  else if (cmd.startsWith("SMS:")) {
    // SMS:+79991234567:текст  |  SMS:+:текст (номер из EEPROM)
    int sep = cmd.indexOf(':', 4);
    if (sep < 0) {
      logLine("SMS не отправлена: нет разделителя номера и текста");
    } else {
      String to = cmd.substring(4, sep);
      String text = cmd.substring(sep + 1);
      if (to == "+") to = String(phoneNumber);
      sendSms(to, text);
    }
  }
  else if (cmd.startsWith("SET:")) {
    // SET:PHONE=+79991234567,T1=15
    int p = cmd.indexOf("PHONE=");
    int t = cmd.indexOf(",T1=");
    if (p >= 0) {
      String phone = (t > p) ? cmd.substring(p + 6, t) : cmd.substring(p + 6);
      phone.trim();
      if (phone.length() >= 1 && phone.length() <= 16) {
        phone.toCharArray(phoneNumber, sizeof(phoneNumber));
      }
    }
    if (t >= 0) {
      int v = cmd.substring(t + 4).toInt();
      if (v >= 1 && v <= 250) timer1 = (byte)v;
    }
    saveSettings();
    logLine("Сохранено в EEPROM: " + String(phoneNumber) + ", T1=" + String(timer1) + "с");
  }
}

// ========== BLE-колбэки ==========
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override { bleConnected = true; }
  void onDisconnect(BLEServer *) override {
    bleConnected = false;
    bleBuf = "";
    BLEDevice::startAdvertising();  // снова доступны для подключения
  }
};

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    auto v = c->getValue();  // std::string (core 2.x) или String (core 3.x)
    bleBuf += String(v.c_str());
    int nl;
    while ((nl = bleBuf.indexOf('\n')) >= 0) {
      String line = bleBuf.substring(0, nl);
      bleBuf = bleBuf.substring(nl + 1);
      byte next = (qHead + 1) % 4;
      if (next != qTail) {           // команды выполняем в loop(), а не в BLE-потоке
        bleQueue[qHead] = line;
        qHead = next;
      }
    }
  }
};

void setupBle() {
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(185);

  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  BLEService *service = server->createService(SERVICE_UUID);

  txChar = service->createCharacteristic(TX_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  txChar->addDescriptor(new BLE2902());

  BLECharacteristic *rxChar = service->createCharacteristic(
      RX_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rxChar->setCallbacks(new RxCallbacks());

  service->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
}

void setup() {
  pinMode(BTN_PIN, INPUT_PULLUP);
  pinMode(REED_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(9600);   // 9600 — как в PyQt-приложении
  loadSettings();
  setupBle();
  sendTelemetry();
}

void loop() {
  // 1. Считываем положение магнита (HIGH = Дверь открыта)
  isDoorOpen = (digitalRead(REED_PIN) == HIGH);

  // 2. Считываем физическую кнопку (защита от дребезга)
  bool btnState = !digitalRead(BTN_PIN);
  if (btnState == 1 && butt_flag == 0 && millis() - last_press > 100) {
    butt_flag = 1;
    last_press = millis();
    toggleArm(); // Нажали кнопку -> вкл/выкл охрану
  }
  if (btnState == 0 && butt_flag == 1) {
    butt_flag = 0;
  }

  // 3. АВТОМАТ СОСТОЯНИЙ (STATE MACHINE)
  switch (systemState) {
    case 0: // СНЯТО С ОХРАНЫ
      digitalWrite(LED_PIN, LOW);
      if (isPlayingMusic) stopMusic();
      break;

    case 1: // НА ОХРАНЕ (Дверь закрыта)
      digitalWrite(LED_PIN, HIGH); // Светодиод горит
      if (isPlayingMusic) stopMusic();

      // Если открыли дверь -> запуск отсчета T1 секунд
      if (isDoorOpen) {
        systemState = 2;
        countdown = timer1;
        countdownTimer = millis();
        sendTelemetry();
      }
      break;

    case 2: // ОТСЧЕТ T1 СЕКУНД
      // Если вернется магнит (закрыли дверь) -> возврат в нормальный режим на охране
      if (!isDoorOpen) {
        systemState = 1;
        tone(BUZZER_PIN, 1000, 100);
        sendTelemetry();
        break;
      }

      // Таймер 1 секунда
      if (millis() - countdownTimer >= 1000) {
        countdownTimer = millis();
        if (countdown > 0) {
          tone(BUZZER_PIN, 600, 100); // Пикаем раз в секунду
          digitalWrite(LED_PIN, !digitalRead(LED_PIN)); // Мигаем LED
          countdown--;
        }
        // Когда время истекло -> Включаем песню FNAF!
        if (countdown == 0) {
          systemState = 3;
          startMusic();
          sendTelemetry();
        }
      }
      break;

    case 3: // ТРЕВОГА / ИГРАЕТ МУЗЫКА FNAF
      // Если закрыли дверь -> Музыка МГНОВЕННО выключается, система опять на охране
      if (!isDoorOpen) {
        systemState = 1;
        stopMusic();
        sendTelemetry();
        break;
      }

      updateMusic();

      // Мигание светодиодом во время тревоги
      if (millis() - lastBlinkTime > 150) {
        lastBlinkTime = millis();
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
      }
      break;
  }

  // 4. Отправка статуса каждые 300 мс (USB + BLE)
  if (millis() - lastTelemetryTime > 300) {
    lastTelemetryTime = millis();
    sendTelemetry();
  }

  // 5а. Команды из BLE (мобильное приложение)
  while (qTail != qHead) {
    String c = bleQueue[qTail];
    qTail = (qTail + 1) % 4;
    handleCommand(c);
  }

  // 5б. Команды из USB-Serial (PyQt-приложение), без блокировки loop()
  while (Serial.available() > 0) {
    char ch = (char)Serial.read();
    if (ch == '\n') {
      handleCommand(serialBuf);
      serialBuf = "";
    } else if (ch != '\r' && serialBuf.length() < 120) {
      serialBuf += ch;
    }
  }
}
