#include <Servo.h>
#include <IRremote.hpp>
#include <EEPROM.h>

#define IR_PIN        A4    
#define LIGHT_AO_PIN  A0    
#define LIGHT_DO_PIN  A5    
#define TRIG_PIN      12    
#define ECHO_PIN      13    

#define EEPROM_ADDR_10 0 
#define EEPROM_ADDR_11 2 

Servo servo10; 
Servo servo11; 

int angle10; 
int angle11; 

int dir10 = 0;
int dir11 = 0;
uint16_t activeCommand = 0;

unsigned long lastIRTime = 0;     
unsigned long lastStepTime = 0;   
unsigned long lastSaveTime = 0;   
unsigned long lastSensorPrintTime = 0; 
bool needSave = false;

const int MOVE_INTERVAL = 35; 

void setAngle(Servo &s, int angle) {
  int us = map(angle, 0, 180, 544, 2400);
  s.writeMicroseconds(us);
}

long getDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(4);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duration = pulseIn(ECHO_PIN, HIGH, 30000); 
  if (duration == 0) return -1;
  return duration * 0.034 / 2;
}

// СТРОГАЯ ФИЛЬТРАЦИЯ КОМАНД ОТ ESP32
void processESPCommand(char c) {
  bool changed = false;

  switch (c) {
    case 'U': case 'u': // ВВЕРХ
      angle11 -= 6;
      changed = true;
      break;
    case 'D': case 'd': // ВНИЗ
      angle11 += 6;
      changed = true;
      break;
    case 'R': case 'r': // ВПРАВО
      angle10 += 6;
      changed = true;
      break;
    case 'L': case 'l': // ВЛЕВО
      angle10 -= 6;
      changed = true;
      break;
    case 'C': case 'c': case 'S': case 's': // ЦЕНТР
      angle10 = 165;
      angle11 = 125;
      changed = true;
      break;
    default:
      // Все остальные байты (переводы строк, мусор, шумы) — ИГНОРИРУЕМ!
      break;
  }

  // Применяем углы ТОЛЬКО если была реальная команда
  if (changed) {
    angle10 = constrain(angle10, 150, 181); 
    angle11 = constrain(angle11, 85, 165);

    setAngle(servo10, angle10);
    setAngle(servo11, angle11);

    needSave = true;
    lastSaveTime = millis();
  }
}

void setup() {
  Serial.begin(9600); // Связь с ESP32

  IrReceiver.begin(IR_PIN, DISABLE_LED_FEEDBACK);

  pinMode(LIGHT_AO_PIN, INPUT);
  pinMode(LIGHT_DO_PIN, INPUT);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);

  EEPROM.get(EEPROM_ADDR_10, angle10);
  EEPROM.get(EEPROM_ADDR_11, angle11);

  if (angle10 < 150 || angle10 > 181) angle10 = 165;
  if (angle11 < 85  || angle11 > 165) angle11 = 125;

  servo10.attach(10, 500, 2500); 
  servo11.attach(11, 500, 2500);

  setAngle(servo10, angle10);
  setAngle(servo11, angle11);

  Serial.println("=== СИСТЕМА ГОТОВА: ФИЛЬТР ЛОЖНЫХ КОМАНД ВКЛЮЧЕН ===");
}

void loop() {
  // 1. ПРИЕМ СИМВОЛОВ ОТ ESP32 (БЕЗ ЛОЖНЫХ СРАБАТЫВАНИЙ)
  while (Serial.available()) {
    char cmd = Serial.read();
    processESPCommand(cmd);
  }

  // 2. ИК-ПУЛЬТ
  if (IrReceiver.decode()) {
    lastIRTime = millis();

    if (!(IrReceiver.decodedIRData.flags & IRDATA_FLAGS_IS_REPEAT)) {
      activeCommand = IrReceiver.decodedIRData.command;
    }

    switch (activeCommand) {
      case 0x18: dir11 = -1; break; 
      case 0x52: dir11 = 1;  break; 
      case 0x5A: dir10 = 1;  break; 
      case 0x08: dir10 = -1; break; 
      case 0x1C: case 0x38:
        angle10 = 165; angle11 = 125;
        setAngle(servo10, angle10);
        setAngle(servo11, angle11);
        dir10 = 0; dir11 = 0;
        needSave = true; lastSaveTime = millis();
        break;
    }
    IrReceiver.resume();
  }

  if (millis() - lastIRTime > 250) {
    dir10 = 0; dir11 = 0; activeCommand = 0;
  }

  if ((dir10 != 0 || dir11 != 0) && (millis() - lastStepTime > MOVE_INTERVAL)) {
    lastStepTime = millis();
    angle10 += dir10; angle11 += dir11;
    angle10 = constrain(angle10, 150, 181); 
    angle11 = constrain(angle11, 85, 165);
    setAngle(servo10, angle10);
    setAngle(servo11, angle11);
    needSave = true; lastSaveTime = millis();
  }

  // 3. СОХРАНЕНИЕ В ПАМЯТЬ
  if (needSave && (millis() - lastSaveTime > 1000)) {
    EEPROM.put(EEPROM_ADDR_10, angle10);
    EEPROM.put(EEPROM_ADDR_11, angle11);
    needSave = false;
  }

  // 4. ТЕЛЕМЕТРИЯ (выводим реже, чтобы не засорять шину)
  if (millis() - lastSensorPrintTime > 1000) {
    lastSensorPrintTime = millis();
    long dist = getDistance();
    int lightVal = analogRead(LIGHT_AO_PIN);
    bool lightState = digitalRead(LIGHT_DO_PIN);

    Serial.print("D:"); Serial.print(dist);
    Serial.print(";L:"); Serial.print(lightVal);
    Serial.print(";DO:"); Serial.println(lightState == HIGH ? 1 : 0);
  }
}