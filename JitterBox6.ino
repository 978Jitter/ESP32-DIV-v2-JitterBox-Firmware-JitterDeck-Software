// === JITTERBOX6 TOP-LEVEL jxAward GUARD ===
#ifndef JITTERBOX6_JX_AWARD_DECLARED
#define JITTERBOX6_JX_AWARD_DECLARED
#include <stdint.h>
void jxAward(uint32_t n);
#endif

// =============================================================================
// BUILD REQUIREMENT - READ BEFORE FLASHING (ESP32-DIV v2.1, WROOM-1U)
// =============================================================================
// This board routes the ILI9341 display + XPT2046 touch SPI bus to GPIO
// 35/36/37. On ESP32-S3-WROOM-1U modules with 8MB Octal PSRAM, GPIO 33-37
// are reserved at the silicon level when PSRAM=opi is enabled.
//
// REQUIRED BUILD FLAG: PSRAM=disabled
//   arduino-cli compile --fqbn \
//     "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,PSRAM=disabled,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB" \
//     JitterBox5
// =============================================================================

#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <SD.h>
#include <RF24.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <Adafruit_NeoPixel.h>
#include <Preferences.h>
#include <esp_system.h>
#include <USB.h>
#include <USBHIDKeyboard.h>
#include <ctype.h>
#include <math.h>
#include <string.h>
#include <ArduinoJson.h>

#define RF_SCK     12
#define RF_MOSI    11
#define RF_MISO    13

#define I2C_SDA    8
#define I2C_SCL    9

#define SD_CS      10
#define CC_CSN     5
#define CC_GDO0    6

#define NRF1_CSN   4
#define NRF1_CE    15
#define NRF2_CSN   48
#define NRF2_CE    47
#define NRF3_CSN   21
#define NRF3_CE    14

#define TFT_SCK    36
#define TFT_MOSI   35
#define TFT_MISO   37
#define TFT_CS     17
#define TFT_DC     16
#define TFT_RST    -1
#define TFT_BLK    7

#define TOUCH_CS   18
#define TOUCH_IRQ  -1

SPIClass tftSPI(HSPI);
RF24 nrf1(NRF1_CE, NRF1_CSN, 4000000);
RF24 nrf2(NRF2_CE, NRF2_CSN, 4000000);
Adafruit_ILI9341 tft(&tftSPI, TFT_DC, TFT_CS, TFT_RST);

// 4x WS2812B on GPIO 1 capped at 7% brightness (255 * 0.07 = 17.85 -> 18)
#define NEO_LED_PIN      1
#define NEO_LED_COUNT    4
#define NEO_MAX_BRIGHT   18
Adafruit_NeoPixel healthStrip(NEO_LED_COUNT, NEO_LED_PIN, NEO_GRB + NEO_KHZ800);
int lastNeoHealth = -1;

bool nrfOk[3] = {false, false, false};
bool sdOk = false;
uint8_t pcfAddr = 0;

static const uint8_t PCF_CENTER_BIT = 6;
uint32_t lastPcfPoll = 0;
uint32_t pcfDebounceAt = 0;
bool pcfRawPressed = false;
bool pcfStablePressed = false;

bool nrf3IrMode = false;

// === CONTINUOUS CARRIER STATE (J command) ===
int8_t  carrierRadio = -1;   // last-started radio (legacy compat)
uint8_t carrierChannel = 0;
bool    carrierOn[3] = {false, false, false};
uint8_t carrierCh[3] = {0, 0, 0};
// === END CC STATE ===

// --- Native ESP32 4.x IR Shield Driver (GPIO 14 TX via BC847, GPIO 21 RX) ---
#define IR_TX_PIN        NRF3_CE
#define IR_RX_PIN        NRF3_CSN
#define IR_LEDC_RES_BITS 8
#define IR_RAW_MAX       700

bool irHwReady = false;
bool irRxStreaming = false;
uint32_t irRxStartedAt = 0;
volatile uint32_t irLastEdgeUs = 0;
volatile uint16_t irEdgeBuf[IR_RAW_MAX];
volatile uint16_t irEdgeCount = 0;
volatile bool irEdgeOverflow = false;
volatile bool irCaptureDone = false;

uint32_t lastFreqHz = 433920000;
uint32_t rxPulseCount = 0;
uint32_t nrfRxCount = 0;

const uint16_t COL_BG       = 0x0841;
const uint16_t COL_ACCENT   = 0x98C5;
const uint16_t COL_CRIMSON  = 0x6043;
const uint16_t COL_DIM      = 0x3186;
const uint16_t COL_SILVER   = 0xBE18;
const uint16_t COL_WHITE    = 0xF79E;
const uint16_t COL_AMBER    = 0xFD20;

bool ccListening = false;
bool nrfListening = false;
bool triListening = false;
uint8_t triChannels[3] = {2, 26, 80};
uint8_t activeNrf = 0;
uint8_t nrfChannel = 76;
uint8_t nrfAddr[5] = {0xE7, 0xE7, 0xE7, 0xE7, 0xE7};
volatile uint32_t lastEdge = 0;

// === JITTER_XP_DEFS_v1 ===
struct JitterXpState {
  uint32_t v;      // XP value
  uint32_t magic;  // 0x58505850 = "XPXP"
};
JitterXpState Jx;
static const uint32_t JX_MAGIC = 0x58505850UL;
// === /JITTER_XP_DEFS_v1 ===

static const uint16_t RXQ_SIZE = 2048;
volatile int32_t rxQueue[RXQ_SIZE];
volatile uint16_t rxHead = 0;
volatile uint16_t rxTail = 0;
volatile uint32_t rxDropped = 0;

// ============================================================================
// JitterDucky (Native USB-OTG HID Keyboard on GPIO 19/20, separate from CP2102)
// Never auto-executes on boot/plug; requires explicit operator authorization.
// ============================================================================
enum DuckyLayout : uint8_t {
  DUCKY_LAYOUT_US = 0,
  DUCKY_LAYOUT_DE = 1
};

enum DuckyRunState : uint8_t {
  DUCKY_IDLE = 0,
  DUCKY_RUNNING = 1,
  DUCKY_PAUSED = 2
};

struct DuckyKeyMapEntry {
  char alias[16];
  uint8_t keyCode;
  uint8_t modifiers; // bit0=Ctrl, bit1=Shift, bit2=Alt, bit3=GUI
};

static const DuckyKeyMapEntry DUCKY_CUSTOM_MAP[] = {
  {"ENTER",     KEY_RETURN,    0x00},
  {"RETURN",    KEY_RETURN,    0x00},
  {"TAB",       KEY_TAB,       0x00},
  {"ESC",       KEY_ESC,       0x00},
  {"ESCAPE",    KEY_ESC,       0x00},
  {"BACKSPACE", KEY_BACKSPACE, 0x00},
  {"CAPSLOCK",  KEY_CAPS_LOCK, 0x00},
  {"CTRL_C",    'c',           0x01},
  {"CTRL_ALT_T",'t',           0x05},
  {"WIN_R",     'r',           0x08}
};
static const size_t DUCKY_CUSTOM_MAP_COUNT = sizeof(DUCKY_CUSTOM_MAP) / sizeof(DUCKY_CUSTOM_MAP[0]);

struct JitterDuckyController {
  bool hidInitialized = false;
  bool operatorAuthorized = false;
  DuckyRunState state = DUCKY_IDLE;
  DuckyLayout layout = DUCKY_LAYOUT_US;
  uint16_t defaultDelayMs = 40;
  uint32_t nextActionMs = 0;
  uint32_t pausedRemainingMs = 0;
  uint16_t repeatRemaining = 0;
  char lastExecutableLine[160] = {0};

  bool useSdFile = false;
  File scriptFile;
  char inlineScript[512] = {0};
  size_t inlinePos = 0;
};

USBHIDKeyboard jitterDuckyKb;
JitterDuckyController ducky;

void duckyReleaseAllImmediate() {
  if (ducky.hidInitialized) {
    jitterDuckyKb.releaseAll();
  }
}

void duckyInitNativeHid() {
  if (ducky.hidInitialized) return;
  USB.begin();
  jitterDuckyKb.begin();
  ducky.hidInitialized = true;
  duckyReleaseAllImmediate();
}

void duckySetAuthorized(bool auth) {
  ducky.operatorAuthorized = auth;
  if (!auth) {
    ducky.state = DUCKY_IDLE;
    ducky.repeatRemaining = 0;
    ducky.pausedRemainingMs = 0;
    if (ducky.useSdFile && ducky.scriptFile) {
      ducky.scriptFile.close();
    }
    ducky.useSdFile = false;
    duckyReleaseAllImmediate();
  }
}

void duckyEmergencyStop() {
  ducky.state = DUCKY_IDLE;
  ducky.repeatRemaining = 0;
  ducky.pausedRemainingMs = 0;
  if (ducky.useSdFile && ducky.scriptFile) {
    ducky.scriptFile.close();
  }
  ducky.useSdFile = false;
  duckyReleaseAllImmediate();
}

void duckyPause() {
  if (ducky.state == DUCKY_RUNNING) {
    uint32_t now = millis();
    ducky.pausedRemainingMs = (ducky.nextActionMs > now) ? (ducky.nextActionMs - now) : 0;
    ducky.state = DUCKY_PAUSED;
    duckyReleaseAllImmediate();
  }
}

void duckyResume() {
  if (ducky.operatorAuthorized && ducky.state == DUCKY_PAUSED) {
    duckyInitNativeHid();
    ducky.nextActionMs = millis() + ducky.pausedRemainingMs;
    ducky.pausedRemainingMs = 0;
    ducky.state = DUCKY_RUNNING;
  }
}

char duckyApplyLayoutChar(char c) {
  if (ducky.layout == DUCKY_LAYOUT_DE) {
    if (c == 'y') return 'z';
    if (c == 'Y') return 'Z';
    if (c == 'z') return 'y';
    if (c == 'Z') return 'Y';
  }
  return c;
}

void duckyPressKeyToken(uint8_t keyCode, uint8_t modifiers) {
  if (!ducky.hidInitialized || !ducky.operatorAuthorized) return;
  if (modifiers & 0x01) jitterDuckyKb.press(KEY_LEFT_CTRL);
  if (modifiers & 0x02) jitterDuckyKb.press(KEY_LEFT_SHIFT);
  if (modifiers & 0x04) jitterDuckyKb.press(KEY_LEFT_ALT);
  if (modifiers & 0x08) jitterDuckyKb.press(KEY_LEFT_GUI);
  if (keyCode != 0) {
    uint8_t mapped = (keyCode >= 32 && keyCode <= 126) ? (uint8_t)duckyApplyLayoutChar((char)keyCode) : keyCode;
    jitterDuckyKb.press(mapped);
    jitterDuckyKb.release(mapped);
  }
  jitterDuckyKb.releaseAll();
}

void duckyPrintMappedText(const char* text) {
  if (!ducky.hidInitialized || !ducky.operatorAuthorized || !text) return;
  for (size_t i = 0; text[i] != '\0'; i++) {
    char ch = duckyApplyLayoutChar(text[i]);
    jitterDuckyKb.write((uint8_t)ch);
  }
}

void duckyProcessLine(const char* rawLine) {
  if (!ducky.operatorAuthorized) {
    duckyEmergencyStop();
    return;
  }

  char buf[192];
  strncpy(buf, rawLine, sizeof(buf) - 1);
  buf[sizeof(buf) - 1] = '\0';

  char* p = buf;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
  size_t len = strlen(p);
  while (len > 0 && (p[len - 1] == ' ' || p[len - 1] == '\t' || p[len - 1] == '\r' || p[len - 1] == '\n')) {
    p[--len] = '\0';
  }

  if (len == 0 || strncmp(p, "REM", 3) == 0) {
    ducky.nextActionMs = millis();
    return;
  }

  if (strncmp(p, "REPEAT ", 7) == 0) {
    int n = atoi(p + 7);
    if (n > 0 && ducky.lastExecutableLine[0] != '\0') {
      ducky.repeatRemaining = (uint16_t)n;
    }
    ducky.nextActionMs = millis();
    return;
  }

  strncpy(ducky.lastExecutableLine, p, sizeof(ducky.lastExecutableLine) - 1);
  ducky.lastExecutableLine[sizeof(ducky.lastExecutableLine) - 1] = '\0';

  if (strncmp(p, "DELAY ", 6) == 0) {
    long ms = atol(p + 6);
    if (ms < 0) ms = 0;
    ducky.nextActionMs = millis() + (uint32_t)ms;
    return;
  }

  if (strncmp(p, "DEFAULT_DELAY ", 14) == 0) {
    int d = atoi(p + 14);
    if (d >= 0) ducky.defaultDelayMs = (uint16_t)d;
    ducky.nextActionMs = millis();
    return;
  }

  if (strncmp(p, "STRING ", 7) == 0) {
    duckyPrintMappedText(p + 7);
    ducky.nextActionMs = millis() + ducky.defaultDelayMs;
    return;
  }

  if (strncmp(p, "GUI ", 4) == 0 || strncmp(p, "WINDOWS ", 8) == 0) {
    const char* arg = (p[0] == 'G') ? (p + 4) : (p + 8);
    while (*arg == ' ') arg++;
    uint8_t kc = (*arg != '\0') ? (uint8_t)tolower((unsigned char)*arg) : 0;
    duckyPressKeyToken(kc, 0x08);
    ducky.nextActionMs = millis() + ducky.defaultDelayMs;
    return;
  }

  for (size_t i = 0; i < DUCKY_CUSTOM_MAP_COUNT; i++) {
    if (strcmp(p, DUCKY_CUSTOM_MAP[i].alias) == 0) {
      duckyPressKeyToken(DUCKY_CUSTOM_MAP[i].keyCode, DUCKY_CUSTOM_MAP[i].modifiers);
      ducky.nextActionMs = millis() + ducky.defaultDelayMs;
      return;
    }
  }

  ducky.nextActionMs = millis() + ducky.defaultDelayMs;
}

bool duckyStartSdScript(const char* path) {
  if (!ducky.operatorAuthorized || !sdOk || !path || path[0] == '\0') return false;
  duckyInitNativeHid();
  duckyEmergencyStop();
  ducky.operatorAuthorized = true;

  extern void deselectAllSPI();
  deselectAllSPI();
  ducky.scriptFile = SD.open(path, FILE_READ);
  if (!ducky.scriptFile) return false;

  ducky.useSdFile = true;
  ducky.repeatRemaining = 0;
  ducky.lastExecutableLine[0] = '\0';
  ducky.nextActionMs = millis();
  ducky.state = DUCKY_RUNNING;
  return true;
}

bool duckyStartInlineScript(const char* text) {
  if (!ducky.operatorAuthorized || !text || text[0] == '\0') return false;
  duckyInitNativeHid();
  duckyEmergencyStop();
  ducky.operatorAuthorized = true;

  strncpy(ducky.inlineScript, text, sizeof(ducky.inlineScript) - 1);
  ducky.inlineScript[sizeof(ducky.inlineScript) - 1] = '\0';
  ducky.inlinePos = 0;
  ducky.useSdFile = false;
  ducky.repeatRemaining = 0;
  ducky.lastExecutableLine[0] = '\0';
  ducky.nextActionMs = millis();
  ducky.state = DUCKY_RUNNING;
  return true;
}

void serviceJitterDucky() {
  if (!ducky.operatorAuthorized) {
    if (ducky.state != DUCKY_IDLE) duckyEmergencyStop();
    return;
  }
  if (ducky.state != DUCKY_RUNNING) return;

  uint32_t now = millis();
  if ((int32_t)(now - ducky.nextActionMs) < 0) return;

  if (ducky.repeatRemaining > 0 && ducky.lastExecutableLine[0] != '\0') {
    ducky.repeatRemaining--;
    duckyProcessLine(ducky.lastExecutableLine);
    return;
  }

  char line[192] = {0};
  if (ducky.useSdFile) {
    extern void deselectAllSPI();
    deselectAllSPI();
    if (!ducky.scriptFile || !ducky.scriptFile.available()) {
      if (ducky.scriptFile) ducky.scriptFile.close();
      ducky.useSdFile = false;
      ducky.state = DUCKY_IDLE;
      duckyReleaseAllImmediate();
      return;
    }
    size_t idx = 0;
    while (ducky.scriptFile.available() && idx < sizeof(line) - 1) {
      char c = (char)ducky.scriptFile.read();
      line[idx++] = c;
      if (c == '\n') break;
    }
    line[idx] = '\0';
    duckyProcessLine(line);
  } else {
    size_t total = strlen(ducky.inlineScript);
    if (ducky.inlinePos >= total) {
      ducky.state = DUCKY_IDLE;
      duckyReleaseAllImmediate();
      return;
    }
    size_t idx = 0;
    while (ducky.inlinePos < total && idx < sizeof(line) - 1) {
      char c = ducky.inlineScript[ducky.inlinePos++];
      line[idx++] = c;
      if (c == '\n') break;
    }
    line[idx] = '\0';
    duckyProcessLine(line);
  }
}

void deselectAllSPI() {
  digitalWrite(SD_CS, HIGH);
  digitalWrite(CC_CSN, HIGH);
  digitalWrite(NRF1_CSN, HIGH);
  digitalWrite(NRF2_CSN, HIGH);
  if (!nrf3IrMode) {
    digitalWrite(NRF3_CSN, HIGH);
  }
  digitalWrite(TFT_CS, HIGH);
#if TOUCH_CS >= 0
  digitalWrite(TOUCH_CS, HIGH);
#endif
}

void assertPcfExpanderHigh() {
  pcfAddr = 0;
  for (uint8_t addr = 0x20; addr <= 0x27; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      pcfAddr = addr;
      Wire.beginTransmission(pcfAddr);
      Wire.write(0xFF);
      Wire.endTransmission();
      break;
    }
  }
}

enum CyberSpecies : uint8_t {
  SP_EGG = 0,
  SP_BITLING,
  SP_BYTEKIN,
  SP_SYNTHRA,
  SP_GLITCHLING,
  SP_AEGIS9,
  SP_PROXYFOX,
  SP_NULLMIND,
  SP_HEXHOUND,
  SP_VOIDCRAWLER,
  SP_ROTBIT,
  SP_ARCHONX,
  SP_DEAD
};

enum MenuIcon : uint8_t {
  ICON_NONE = 0,
  ICON_FEED,
  ICON_GAME,
  ICON_CLEAN, // DFRAG
  ICON_LIGHT,
  ICON_MEDS,  // PATCH
  ICON_SCOLD,
  ICON_STATS,
  ICON_COUNT
};

enum UIScreen : uint8_t {
  SCR_MAIN = 0,
  SCR_FEED_MENU,
  SCR_GAME_HILO,
  SCR_STATS_1,
  SCR_STATS_2,
  SCR_CLOCK,
  SCR_DEAD
};

enum AttentionReason : uint8_t {
  ATT_NONE = 0,
  ATT_HUNGRY,
  ATT_HAPPY,
  ATT_SLEEP,
  ATT_DISCIPLINE
};

enum CyberSickType : uint8_t {
  SICK_NONE = 0,
  SICK_CORRUPT, // Caused by fragmentation (poopCount >= 2)
  SICK_THERMAL, // Caused by overclock snack overload (snackCount >= 6)
  SICK_MALWARE  // Caused by baby stage event
};

static const char* const SICK_BADGE_TXT[4] = {
  "", "CRPT", "THRM", "WORM"
};

static const char* const SICK_FULL_NAME[4] = {
  "HEALTHY", "CORRUPT", "THERMAL", "MALWARE"
};

#define TAMA_MAGIC   0x47454E33UL
#define SD_TAMA_PATH "/jitter_gen2.bin"

struct TamaState {
  uint32_t magic;
  uint32_t id;
  uint8_t  species;
  uint8_t  hungry;
  uint8_t  happy;
  uint8_t  discipline;
  uint8_t  weightOz;
  uint8_t  careMistakes;
  uint8_t  totalMistakes;
  uint8_t  discMistakes;
  uint8_t  poopCount;     // Fragmentation level (0..4)
  uint8_t  sickDosesLeft; // Patches remaining (0..2)
  uint8_t  sickType;      // CyberSickType
  uint8_t  snackCount;
  uint8_t  lightsOff;
  uint8_t  sleeping;
  uint8_t  babyNapDone;
  uint8_t  babySickDone;
  uint8_t  deathCause;
  uint8_t  timeScale;
  uint8_t  clockHour;
  uint8_t  clockMin;
  uint8_t  clockSec;
  uint32_t ageSec;
  uint32_t stageSec;
  uint32_t hungryAccSec;
  uint32_t happyAccSec;
  uint32_t poopAccSec;
  uint32_t discCallAccSec;
  uint32_t sickAccSec;
  uint32_t attnTimerSec;
  uint8_t  attnReason;
  uint32_t rfSignals;
};

TamaState T;

Preferences tPrefs;
bool tDirty = false;
uint32_t tLastSave = 0;
uint32_t tLastTickMs = 0;

uint8_t uiScreen = SCR_MAIN;
uint8_t selIcon = ICON_NONE;
uint8_t feedChoice = 0;
bool clockSetMode = false;
uint8_t clockSetField = 0;
bool uiNeedsFullRedraw = true;
bool uiHudDirty = true;

void tamaInflictSickness(uint8_t causeType, uint8_t doses) {
  T.sickType = causeType;
  T.sickDosesLeft = doses;
  uiHudDirty = true;
  tDirty = true;
}

int computePetHealth4() {
  if (T.species == SP_DEAD) return 0;
  if (T.species == SP_EGG) return 4;
  int avg = (int)(((int)T.hungry + (int)T.happy + 1) / 2);
  if ((T.hungry == 0 && T.happy == 0) || T.sickDosesLeft >= 3) {
    avg = (avg > 1) ? 1 : avg;
  }
  return constrain(avg, 0, 4);
}

void updateHealthLEDs(int health) {
  health = constrain(health, 0, 4);
  if (health == lastNeoHealth) return;
  lastNeoHealth = health;

  healthStrip.clear();
  uint32_t statusColor = 0;

  switch (health) {
    case 4:
      statusColor = healthStrip.Color(0, 255, 0);
      break;
    case 3:
      statusColor = healthStrip.Color(180, 255, 0);
      break;
    case 2:
      statusColor = healthStrip.Color(255, 110, 0);
      break;
    case 1:
      statusColor = healthStrip.Color(255, 0, 0);
      break;
    case 0:
    default:
      healthStrip.show();
      return;
  }

  for (int i = 0; i < health; i++) {
    healthStrip.setPixelColor(i, statusColor);
  }
  healthStrip.show();
}

uint8_t gameRound = 0;
uint8_t gameWins = 0;
uint8_t gameLeftNum = 5;
uint8_t gameRightNum = 5;
uint8_t gameRevealState = 0;
uint32_t gameRevealAt = 0;
bool gameLastWon = false;

bool evoDanceActive = false;
uint32_t evoDanceStart = 0;
char evoBanner[28] = "";

uint32_t lastTouchPoll = 0;
uint32_t lastTouchTapAt = 0;
uint8_t  lastTouchBtn = 0;

uint32_t btnPressAt = 0;
uint32_t btnShortAt = 0;
bool btnShortPending = false;

static const char* const SP_NAME[13] = {
  "CYBER-EGG", "BITLING", "BYTEKIN", "SYNTHRA", "GLITCHLING",
  "AEGIS-9", "PROXYFOX", "NULLMIND", "HEXHOUND", "VOIDCRAWLER",
  "ROTBIT", "ARCHON-X", "CORE-DUMP"
};

static const uint8_t SP_BASE_WT[13] = {
  5, 5, 10, 20, 20, 30, 30, 30, 30, 30, 20, 30, 0
};

static const uint16_t SP_HEART_INT[13] = {
  0, 240, 2700, 3600, 3000, 4800, 4200, 3600, 3900, 3300, 2400, 5400, 0
};

static const uint8_t SP_BED_HR[13] = {
  25, 25, 20, 21, 21, 22, 22, 22, 22, 22, 21, 23, 25
};
static const uint8_t SP_WAKE_HR[13] = {
  0, 0, 8, 8, 9, 8, 8, 9, 8, 9, 9, 8, 0
};

struct EyeHover {
  int16_t curX, curY, fromX, fromY, toX, toY;
  uint32_t moveStart, moveDur;
};

const int16_t EYE_BASE_CX[2] = {104, 216};
const int16_t EYE_BASE_CY = 104;
const int16_t EYE_W = 64, EYE_H = 34;
const uint16_t EYE_FRAME_MS = 40;

EyeHover eyeHover[2];
uint32_t lastEyeFrame = 0;
bool eyeHasLastDraw = false;
int16_t lastDrawX[2], lastDrawY[2], lastDrawW[2], lastDrawH[2];
float eyeLid = 1.0f;
uint32_t eyeChompAt = 0;
bool eyeChomping = false;

// Radio & IR eye reaction states
uint32_t eyeRfRxAt = 0;
bool eyeRfRxActive = false;
uint32_t eyeRfTxAt = 0;
bool eyeRfTxActive = false;
uint32_t eyeIrAt = 0;
bool eyeIrActive = false;
uint8_t rfFeedBurstAcc = 0;

void tamaClearAttention(uint8_t reason);

float eyeEase(float t) { return t * t * (3.0f - 2.0f * t); }

void triggerEyeChomp() {
  eyeChomping = true;
  eyeChompAt = millis();
}

void triggerEyeRfRx() {
  uint32_t now = millis();
  if (!eyeRfRxActive || now - eyeRfRxAt > 180) {
    eyeRfRxActive = true;
    eyeRfRxAt = now;
  }
}

void triggerEyeRfTx() {
  eyeRfTxActive = true;
  eyeRfTxAt = millis();
}

void triggerEyeIrBlast() {
  eyeIrActive = true;
  eyeIrAt = millis();
  // === JITTER_XP_IR_v1 ===
  jxAward(5);
  // === /JITTER_XP_IR_v1 ===
  if (T.species != SP_EGG && T.species != SP_DEAD && !T.sleeping && T.sickDosesLeft == 0) {
    if (T.attnReason == ATT_DISCIPLINE) {
      triggerEyeChomp();
    } else if (T.happy < 4) {
      T.happy++;
      tamaClearAttention(ATT_HAPPY);
      uiHudDirty = true;
      tDirty = true;
    }
  }
}

void rewardRfSignalActivity(uint8_t weight = 1) {
  T.rfSignals += weight;
  // === JITTER_XP_RF_v1 ===
  jxAward(weight);
  // === /JITTER_XP_RF_v1 ===

  if (T.species == SP_EGG || T.species == SP_DEAD) return;
  if (T.sleeping || T.sickDosesLeft > 0) return;

  if (T.attnReason == ATT_DISCIPLINE) {
    triggerEyeChomp();
    return;
  }

  rfFeedBurstAcc += weight;
  if (rfFeedBurstAcc >= 25) {
    rfFeedBurstAcc %= 25;
    if (T.hungry < 4) {
      T.hungry++;
      tamaClearAttention(ATT_HUNGRY);
      uiHudDirty = true;
      tDirty = true;
    } else if (T.happy < 4) {
      T.happy++;
      tamaClearAttention(ATT_HAPPY);
      uiHudDirty = true;
      tDirty = true;
    }
  }
}

void triggerEvolutionDance(const char* newName) {
  evoDanceActive = true;
  evoDanceStart = millis();
  strncpy(evoBanner, newName, sizeof(evoBanner) - 1);
  evoBanner[sizeof(evoBanner) - 1] = 0;
  uiHudDirty = true;
}

void pickPairedHoverTarget(uint32_t now) {
  eyeHover[0].fromX = eyeHover[0].curX;
  eyeHover[0].fromY = eyeHover[0].curY;
  eyeHover[0].toX = EYE_BASE_CX[0] + (int16_t)random(-14, 15);
  eyeHover[0].toY = EYE_BASE_CY + (int16_t)random(-9, 10);
  eyeHover[0].moveStart = now;
  eyeHover[0].moveDur = 750 + (uint32_t)random(0, 750);
}

void initEyes() {
  randomSeed(micros());
  uint32_t now = millis();
  eyeHover[0].curX = EYE_BASE_CX[0];
  eyeHover[0].curY = EYE_BASE_CY;
  pickPairedHoverTarget(now);
  eyeHover[1].curX = EYE_BASE_CX[1];
  eyeHover[1].curY = EYE_BASE_CY;
  eyeHasLastDraw = false;
}

void stepPairedHover(uint32_t now) {
  uint32_t elapsed = now - eyeHover[0].moveStart;
  if (elapsed >= eyeHover[0].moveDur) {
    pickPairedHoverTarget(now);
    elapsed = 0;
  }
  float t = eyeEase((float)elapsed / (float)eyeHover[0].moveDur);
  eyeHover[0].curX = eyeHover[0].fromX + (int16_t)((eyeHover[0].toX - eyeHover[0].fromX) * t);
  eyeHover[0].curY = eyeHover[0].fromY + (int16_t)((eyeHover[0].toY - eyeHover[0].fromY) * t);
  eyeHover[1].curX = eyeHover[0].curX + (EYE_BASE_CX[1] - EYE_BASE_CX[0]);
  eyeHover[1].curY = eyeHover[0].curY;
}

void drawEye(int16_t cx, int16_t cy, bool leftEye, float openness, uint16_t color, float lid) {
  int16_t w = EYE_W + (int16_t)(openness * 22);
  int16_t hFull = EYE_H + (int16_t)(openness * 22);
  int16_t h = (int16_t)(hFull * lid);
  if (h < 4) h = 4;
  int16_t x0 = cx - w / 2, y0 = cy - h / 2;

  tft.fillRoundRect(x0, y0, w, h, h / 3, color);

  if (!evoDanceActive && lid > 0.6f) {
    int16_t slant = (int16_t)((1.0f - openness) * (h / 3));
    if (slant > 2) {
      if (leftEye) tft.fillTriangle(x0, y0, x0 + w, y0, x0 + w, y0 + slant, COL_BG);
      else tft.fillTriangle(x0, y0, x0 + w, y0, x0, y0 + slant, COL_BG);
    }
  }

  if (T.species >= SP_BYTEKIN && T.species != SP_DEAD && h > 14) {
    tft.fillRect(cx - 3, cy - (h * 32) / 100, 6, (h * 64) / 100, COL_BG);
  }
  if (T.species >= SP_SYNTHRA && T.species != SP_DEAD) {
    tft.fillRect(x0, y0 - 7, w, 2, COL_CRIMSON);
  }
  if ((T.species >= SP_AEGIS9 && T.species <= SP_ARCHONX)) {
    tft.fillCircle(leftEye ? x0 - 7 : x0 + w + 7, y0 - 3, 2, color);
  }

  if (T.poopCount > 0 && T.species != SP_EGG && T.species != SP_DEAD) {
    uint8_t fragLevel = min((uint8_t)4, T.poopCount);
    uint32_t phase = millis() / 90;
    uint8_t slices = fragLevel * 2 + 1;
    for (uint8_t s = 0; s < slices; s++) {
      int16_t sy = y0 + ((s * 7 + (phase % 5)) * max(1, (int)h - 3)) / (slices * 5 + 1);
      int16_t sh = (fragLevel >= 3) ? 3 : 2;
      int16_t sx = x0 + ((s * 13 + (leftEye ? 3 : 11) + (phase * 5)) % max(1, (int)w - 14));
      int16_t sw = 8 + fragLevel * 4;
      if (sx + sw > x0 + w) sw = (x0 + w) - sx;
      if (sw > 0 && sy + sh <= y0 + h) {
        tft.fillRect(sx, sy, sw, sh, COL_BG);
      }
      if (fragLevel >= 2 && (s % 2 == 0)) {
        int16_t jx = ((s + phase + (leftEye ? 0 : 1)) & 1) ? (x0 + w - 2) : (x0 - 4);
        tft.fillRect(jx, sy, 5, 2, color);
      }
    }
  }
}

void serviceEyes() {
  if (uiScreen != SCR_MAIN) return;
  uint32_t now = millis();
  if (now - lastEyeFrame < EYE_FRAME_MS) return;
  lastEyeFrame = now;

  if (evoDanceActive && (now - evoDanceStart >= 3200)) {
    evoDanceActive = false;
    evoBanner[0] = 0;
    uiHudDirty = true;
  }

  stepPairedHover(now);

  float lidTarget = 1.0f;
  float openness = 0.0f;
  uint16_t color = COL_ACCENT;
  int16_t dx = 0, dy = 0;

  if (T.species == SP_EGG) {
    lidTarget = 0.35f;
    color = COL_SILVER;
    dx = (int16_t)(4.0f * sinf(now / 260.0f));
    eyeHover[0].curX = EYE_BASE_CX[0];
    eyeHover[0].curY = EYE_BASE_CY;
    eyeHover[1].curX = EYE_BASE_CX[1];
    eyeHover[1].curY = EYE_BASE_CY;
  } else if (T.sleeping) {
    lidTarget = 0.14f;
    color = COL_DIM;
    dy = (int16_t)(2.0f * sinf(now / 850.0f));
    eyeHover[0].curX = EYE_BASE_CX[0];
    eyeHover[0].curY = EYE_BASE_CY;
    eyeHover[1].curX = EYE_BASE_CX[1];
    eyeHover[1].curY = EYE_BASE_CY;
  } else if (T.sickDosesLeft > 0) {
    lidTarget = 0.45f;
    color = (T.sickType == SICK_THERMAL) ? ILI9341_ORANGE : ((T.sickType == SICK_MALWARE) ? ILI9341_MAGENTA : COL_AMBER);
  }

  if (eyeChomping) {
    uint32_t age = now - eyeChompAt;
    if (age >= 380) {
      eyeChomping = false;
    } else {
      openness = 0.65f;
      dx = (age < 190) ? 16 : 0;
    }
  }

  if (eyeRfRxActive) {
    uint32_t age = now - eyeRfRxAt;
    if (age >= 420) {
      eyeRfRxActive = false;
    } else if (!T.sleeping) {
      lidTarget = 1.0f;
      openness = 0.55f;
      color = ((age / 60) % 2 == 0) ? ILI9341_CYAN : COL_WHITE;
      dx = (int16_t)(12.0f * sinf(age / 35.0f));
    }
  }

  if (eyeRfTxActive) {
    uint32_t age = now - eyeRfTxAt;
    if (age >= 500) {
      eyeRfTxActive = false;
    } else if (!T.sleeping) {
      lidTarget = 1.0f;
      openness = 0.70f;
      color = ((age / 70) % 2 == 0) ? COL_AMBER : ILI9341_RED;
      dy = -6;
    }
  }

  if (eyeIrActive) {
    uint32_t age = now - eyeIrAt;
    if (age >= 650) {
      eyeIrActive = false;
    } else if (!T.sleeping) {
      lidTarget = 1.0f;
      openness = 0.85f;
      color = ((age / 55) % 2 == 0) ? ILI9341_MAGENTA : ILI9341_RED;
      dx = (int16_t)(8.0f * cosf(age / 25.0f));
      dy = (int16_t)(6.0f * sinf(age / 25.0f));
    }
  }

  if (evoDanceActive) {
    static const uint16_t RAINBOW[6] = {
      ILI9341_RED, ILI9341_ORANGE, ILI9341_YELLOW,
      ILI9341_GREEN, ILI9341_CYAN, ILI9341_MAGENTA
    };
    uint8_t idx = (uint8_t)((now / 75) % 6);
    color = RAINBOW[idx];
    openness = 0.75f + 0.25f * sinf(now / 60.0f);
    lidTarget = 1.0f;
    dx = (int16_t)(16.0f * cosf(now / 85.0f));
    dy = (int16_t)(10.0f * sinf(now / 65.0f));
  }

  eyeLid += (lidTarget - eyeLid) * 0.15f;

  int16_t drawX[2] = {(int16_t)(eyeHover[0].curX + dx), (int16_t)(eyeHover[1].curX + dx)};
  int16_t drawY[2] = {(int16_t)(eyeHover[0].curY + dy), (int16_t)(eyeHover[1].curY + dy)};
  int16_t newW = EYE_W + (int16_t)(openness * 22);
  int16_t newH = (int16_t)((EYE_H + (int16_t)(openness * 22)) * eyeLid);
  if (newH < 4) newH = 4;

  const int16_t PAD = 12;
  deselectAllSPI();
  for (uint8_t i = 0; i < 2; i++) {
    int16_t nx0 = drawX[i] - newW / 2 - PAD;
    int16_t nx1 = drawX[i] + newW / 2 + PAD;
    int16_t ny0 = drawY[i] - newH / 2 - PAD;
    int16_t ny1 = drawY[i] + newH / 2 + PAD;
    if (eyeHasLastDraw) {
      int16_t ox0 = lastDrawX[i] - lastDrawW[i] / 2 - PAD;
      int16_t ox1 = lastDrawX[i] + lastDrawW[i] / 2 + PAD;
      int16_t oy0 = lastDrawY[i] - lastDrawH[i] / 2 - PAD;
      int16_t oy1 = lastDrawY[i] + lastDrawH[i] / 2 + PAD;
      nx0 = min(nx0, ox0);
      nx1 = max(nx1, ox1);
      ny0 = min(ny0, oy0);
      ny1 = max(ny1, oy1);
    }
    if (ny0 < 32) ny0 = 32;
    if (ny1 > 172) ny1 = 172;
    if (nx0 < 0) nx0 = 0;
    if (nx1 > 320) nx1 = 320;
    tft.fillRect(nx0, ny0, nx1 - nx0, ny1 - ny0, COL_BG);

    lastDrawX[i] = drawX[i];
    lastDrawY[i] = drawY[i];
    lastDrawW[i] = newW;
    lastDrawH[i] = newH;
  }
  eyeHasLastDraw = true;

  drawEye(drawX[0], drawY[0], true,  openness, color, eyeLid);
  drawEye(drawX[1], drawY[1], false, openness, color, eyeLid);
}

void tamaFreshEgg() {
  uint8_t prevScale = (T.timeScale == 1 || T.timeScale == 10 || T.timeScale == 60) ? T.timeScale : 1;
  uint8_t hr = T.clockHour < 24 ? T.clockHour : 12;
  uint8_t mn = T.clockMin < 60 ? T.clockMin : 0;

  memset(&T, 0, sizeof(T));
  T.magic = TAMA_MAGIC;
  T.id = (uint32_t)esp_random();
  T.species = SP_EGG;
  T.hungry = 4;
  T.happy = 4;
  T.discipline = 0;
  T.weightOz = 5;
  T.sickType = SICK_NONE;
  T.timeScale = prevScale;
  T.clockHour = hr;
  T.clockMin = mn;
  tDirty = true;
}

bool tamaSaveToSd() {
  if (!sdOk) return false;
  deselectAllSPI();
  File f = SD.open(SD_TAMA_PATH, FILE_WRITE);
  if (!f) return false;
  size_t w = f.write((const uint8_t*)&T, sizeof(T));
  f.close();
  return (w == sizeof(T));
}

bool tamaLoadFromSd() {
  if (!sdOk) return false;
  deselectAllSPI();
  if (!SD.exists(SD_TAMA_PATH)) return false;
  File f = SD.open(SD_TAMA_PATH, FILE_READ);
  if (!f) return false;
  if (f.size() != sizeof(T)) { f.close(); return false; }
  TamaState tmp;
  size_t r = f.read((uint8_t*)&tmp, sizeof(T));
  f.close();
  if (r != sizeof(T) || tmp.magic != TAMA_MAGIC) return false;
  memcpy(&T, &tmp, sizeof(T));
  return true;
}

void tamaSave() {
  if (tPrefs.begin("jitter_g3", false)) {
    tPrefs.putBytes("st", &T, sizeof(T));
    tPrefs.end();
  }
  tamaSaveToSd();
  tDirty = false;
  tLastSave = millis();
}

void tamaLoad() {
  memset(&T, 0, sizeof(T));
  bool ok = false;
  if (tPrefs.begin("jitter_g3", true)) {
    if (tPrefs.getBytesLength("st") == sizeof(T)) {
      tPrefs.getBytes("st", &T, sizeof(T));
      ok = (T.magic == TAMA_MAGIC);
    }
    tPrefs.end();
  }
  if (!ok) ok = tamaLoadFromSd();
  if (!ok) {
    T.clockHour = 12;
    T.clockMin = 0;
    T.timeScale = 1;
    tamaFreshEgg();
    tamaSave();
  }
  if (T.species > SP_DEAD) T.species = SP_EGG;
  if (T.sickDosesLeft == 0) T.sickType = SICK_NONE;
  else if (T.sickType == SICK_NONE || T.sickType > SICK_MALWARE) T.sickType = SICK_CORRUPT;
  if (T.timeScale != 1 && T.timeScale != 10 && T.timeScale != 60) T.timeScale = 1;
  if (T.species == SP_DEAD) uiScreen = SCR_DEAD;
}

void tamaRaiseAttention(uint8_t reason) {
  if (T.attnReason == ATT_NONE) {
    T.attnReason = reason;
    T.attnTimerSec = 900;
    uiHudDirty = true;
  }
}

void tamaClearAttention(uint8_t reason) {
  if (reason == ATT_NONE || T.attnReason == reason) {
    T.attnReason = ATT_NONE;
    T.attnTimerSec = 0;
    uiHudDirty = true;
  }
}

void tamaKill(uint8_t cause) {
  T.species = SP_DEAD;
  T.deathCause = cause;
  T.sleeping = 0;
  T.attnReason = ATT_NONE;
  selIcon = ICON_NONE;
  uiScreen = SCR_DEAD;
  uiNeedsFullRedraw = true;
  tDirty = true;
  tamaSave();
}

void tamaEvolveTo(uint8_t nextSpecies) {
  T.species = nextSpecies;
  T.stageSec = 0;
  T.careMistakes = 0;
  T.discMistakes = 0;
  T.snackCount = 0;
  if (T.weightOz < SP_BASE_WT[nextSpecies]) {
    T.weightOz = SP_BASE_WT[nextSpecies];
  }
  triggerEvolutionDance(SP_NAME[nextSpecies]);
  uiHudDirty = true;
  tDirty = true;
  tamaSave();
}

void tamaCheckEvolutions() {
  uint8_t sp = T.species;
  if (sp == SP_EGG) {
    if (T.stageSec >= 300) {
      T.hungry = 4;
      T.happy = 4;
      T.weightOz = 5;
      tamaEvolveTo(SP_BITLING);
    }
  } else if (sp == SP_BITLING) {
    if (!T.babyNapDone && T.stageSec >= 1800 && T.stageSec < 2400) {
      T.sleeping = 1;
      T.babyNapDone = 1;
      uiHudDirty = true;
    } else if (T.sleeping && T.stageSec >= 2400) {
      T.sleeping = 0;
      T.lightsOff = 0;
      uiHudDirty = true;
    }
    if (!T.babySickDone && T.stageSec >= 2700) {
      tamaInflictSickness(SICK_MALWARE, 2);
      T.babySickDone = 1;
    }
    if (T.stageSec >= 3900) {
      T.sleeping = 0;
      T.lightsOff = 0;
      tamaEvolveTo(SP_BYTEKIN);
    }
  } else if (sp == SP_BYTEKIN) {
    if (T.stageSec >= 172800UL) {
      if (T.careMistakes <= 2 && T.discipline >= 75) {
        tamaEvolveTo(SP_SYNTHRA);
      } else {
        tamaEvolveTo(SP_GLITCHLING);
      }
    }
  } else if (sp == SP_SYNTHRA) {
    if (T.stageSec >= 259200UL) {
      if (T.careMistakes == 0 && T.discipline == 100) {
        tamaEvolveTo(SP_AEGIS9);
      } else if (T.careMistakes <= 2 && T.discipline >= 75) {
        tamaEvolveTo(SP_PROXYFOX);
      } else {
        tamaEvolveTo(SP_NULLMIND);
      }
    }
  } else if (sp == SP_GLITCHLING) {
    if (T.stageSec >= 259200UL) {
      if (T.careMistakes <= 2 && T.discipline >= 75) {
        tamaEvolveTo(SP_HEXHOUND);
      } else if (T.careMistakes <= 4) {
        tamaEvolveTo(SP_VOIDCRAWLER);
      } else {
        tamaEvolveTo(SP_ROTBIT);
      }
    }
  } else if (sp == SP_NULLMIND) {
    if (T.stageSec >= 172800UL && T.careMistakes == 0 && T.discipline == 100) {
      tamaEvolveTo(SP_ARCHONX);
    }
  }
}

void tamaService() {
  uint32_t now = millis();
  if (tLastTickMs == 0) tLastTickMs = now;
  if (now - tLastTickMs < 1000) return;

  uint32_t realElapsed = (now - tLastTickMs) / 1000;
  tLastTickMs += realElapsed * 1000;

  if (uiScreen == SCR_CLOCK && clockSetMode) return;
  if (T.species == SP_DEAD) return;

  uint32_t dt = realElapsed * (uint32_t)T.timeScale;

  uint32_t totalDaySec = (uint32_t)T.clockHour * 3600UL + (uint32_t)T.clockMin * 60UL + (uint32_t)T.clockSec + dt;
  totalDaySec %= 86400UL;
  T.clockHour = (uint8_t)(totalDaySec / 3600UL);
  T.clockMin  = (uint8_t)((totalDaySec % 3600UL) / 60UL);
  T.clockSec  = (uint8_t)(totalDaySec % 60UL);

  T.ageSec += dt;
  T.stageSec += dt;
  tDirty = true;

  if (T.species == SP_EGG) {
    tamaCheckEvolutions();
    uiHudDirty = true;
    return;
  }

  uint8_t bedHr = SP_BED_HR[T.species];
  uint8_t wakeHr = SP_WAKE_HR[T.species];
  if (bedHr <= 23) {
    bool shouldSleep = (T.clockHour >= bedHr || T.clockHour < wakeHr);
    if (shouldSleep && !T.sleeping) {
      T.sleeping = 1;
      if (!T.lightsOff) tamaRaiseAttention(ATT_SLEEP);
      uiHudDirty = true;
    } else if (!shouldSleep && T.sleeping) {
      T.sleeping = 0;
      T.lightsOff = 0;
      tamaClearAttention(ATT_SLEEP);
      uiHudDirty = true;
    }
  }

  if (T.attnReason != ATT_NONE) {
    if (T.attnTimerSec > dt) {
      T.attnTimerSec -= dt;
    } else {
      uint8_t expired = T.attnReason;
      T.attnReason = ATT_NONE;
      T.attnTimerSec = 0;
      if (expired == ATT_DISCIPLINE) {
        T.discMistakes++;
      } else {
        T.careMistakes++;
        T.totalMistakes++;
      }
      uiHudDirty = true;
    }
  }

  if (!T.sleeping) {
    uint32_t heartInterval = SP_HEART_INT[T.species];
    uint32_t ageDays = T.ageSec / 86400UL;
    if (T.species >= SP_AEGIS9 && ageDays >= 10 && heartInterval > 300) {
      uint32_t accel = (ageDays - 9) * 350;
      heartInterval = (heartInterval > accel + 240) ? (heartInterval - accel) : 240;
    }

    T.hungryAccSec += dt;
    if (T.hungryAccSec >= heartInterval) {
      T.hungryAccSec %= heartInterval;
      if (T.hungry > 0) {
        T.hungry--;
        if (T.hungry == 0) tamaRaiseAttention(ATT_HUNGRY);
        uiHudDirty = true;
      }
    }

    T.happyAccSec += dt;
    if (T.happyAccSec >= heartInterval) {
      T.happyAccSec %= heartInterval;
      if (T.happy > 0) {
        T.happy--;
        if (T.happy == 0) tamaRaiseAttention(ATT_HAPPY);
        uiHudDirty = true;
      }
    }

    uint32_t poopInterval = (T.species == SP_BITLING) ? 720UL : 9000UL;
    T.poopAccSec += dt;
    if (T.poopAccSec >= poopInterval) {
      T.poopAccSec %= poopInterval;
      if (T.poopCount < 4) {
        T.poopCount++;
        if (T.poopCount >= 2 && T.sickDosesLeft == 0) {
          tamaInflictSickness(SICK_CORRUPT, 2);
        }
        uiHudDirty = true;
      }
    }

    T.discCallAccSec += dt;
    if (T.discCallAccSec >= 12600UL) {
      T.discCallAccSec = 0;
      if (T.hungry > 0 && T.happy > 0 && T.sickDosesLeft == 0 && T.discipline < 100) {
        tamaRaiseAttention(ATT_DISCIPLINE);
      }
    }
  }

  if (T.sickDosesLeft > 0) {
    T.sickAccSec += dt;
    if (T.sickAccSec >= 14400UL) {
      tamaKill(1);
      return;
    }
  } else {
    T.sickAccSec = 0;
    T.sickType = SICK_NONE;
  }

  uint8_t maxCM = (T.species == SP_ROTBIT) ? 6 : 9;
  if (T.careMistakes >= maxCM) {
    tamaKill(T.ageSec >= 10 * 86400UL ? 3 : 1);
    return;
  }

  tamaCheckEvolutions();

  bool quiet = !ccListening || (rxHead == rxTail);
  if (tDirty && quiet && (now - tLastSave > 180000UL)) {
    tamaSave();
  }
}

void actionFeedMeal() {
  if (T.sleeping || T.sickDosesLeft > 0) return;
  if (T.attnReason == ATT_DISCIPLINE) {
    triggerEyeChomp();
    return;
  }
  if (T.hungry >= 4) return;
  T.hungry++;
  T.weightOz++;
  // === JITTER_XP_FEED_MEAL_v1 ===
  jxAward(2);
  // === /JITTER_XP_FEED_MEAL_v1 ===
  triggerEyeChomp();
  if (T.hungry > 0) tamaClearAttention(ATT_HUNGRY);
  uiHudDirty = true;
  tDirty = true;
}

void actionFeedSnack() {
  if (T.sleeping || T.sickDosesLeft > 0) return;
  if (T.attnReason == ATT_DISCIPLINE) {
    triggerEyeChomp();
    return;
  }
  if (T.happy < 4) T.happy++;
  T.weightOz += 2;
  T.snackCount++;
  // === JITTER_XP_FEED_SNACK_v1 ===
  jxAward(1);
  // === /JITTER_XP_FEED_SNACK_v1 ===
  triggerEyeChomp();
  if (T.happy > 0) tamaClearAttention(ATT_HAPPY);

  if (T.snackCount >= 6 && T.sickDosesLeft == 0) {
    tamaInflictSickness(SICK_THERMAL, 2);
  }
  if (T.snackCount >= 12 && T.species <= SP_GLITCHLING) {
    tamaKill(2);
    return;
  }
  uiHudDirty = true;
  tDirty = true;
}

void startNumbersGame() {
  uint8_t minWt = SP_BASE_WT[T.species];
  if (T.sleeping || T.sickDosesLeft > 0) return;
  if (T.attnReason == ATT_DISCIPLINE) {
    triggerEyeChomp();
    return;
  }
  if (T.happy >= 4 && T.weightOz <= minWt) {
    triggerEyeChomp();
    return;
  }
  gameRound = 1;
  gameWins = 0;
  gameLeftNum = (uint8_t)random(1, 10);
  gameRightNum = (uint8_t)random(1, 10);
  gameRevealState = 0;
  uiScreen = SCR_GAME_HILO;
  uiNeedsFullRedraw = true;
}

void submitNumbersGuess(bool guessHigher) {
  if (gameRevealState != 0) return;
  gameLastWon = guessHigher ? (gameRightNum > gameLeftNum) : (gameRightNum < gameLeftNum);
  if (gameLastWon) gameWins++;
  gameRevealState = 1;
  gameRevealAt = millis();
  uiNeedsFullRedraw = true;
}

void finishNumbersGame() {
  uint8_t minWt = SP_BASE_WT[T.species];
  if (T.weightOz > minWt) T.weightOz--;
  if (gameWins >= 5) {
    T.happy = min(4, (int)T.happy + 2);
  } else if (gameWins >= 3) {
    T.happy = min(4, (int)T.happy + 1);
  }
  if (T.happy > 0) tamaClearAttention(ATT_HAPPY);
  // === JITTER_XP_GAME_v1 ===
  jxAward(gameWins >= 5 ? 5 : (gameWins >= 3 ? 3 : 1));
  // === /JITTER_XP_GAME_v1 ===
  uiScreen = SCR_MAIN;
  selIcon = ICON_NONE;
  uiNeedsFullRedraw = true;
  tDirty = true;
}

void executeMenuIcon(uint8_t icon) {
  if (T.species == SP_EGG || T.species == SP_DEAD) return;
  switch (icon) {
    case ICON_FEED:
      if (!T.sleeping && T.sickDosesLeft == 0) {
        feedChoice = 0;
        uiScreen = SCR_FEED_MENU;
        uiNeedsFullRedraw = true;
      }
      break;
    case ICON_GAME:
      startNumbersGame();
      break;
    case ICON_CLEAN: // DFRAG
      if (T.poopCount > 0) {
        T.poopCount = 0;
        triggerEyeChomp();
        uiHudDirty = true;
        tDirty = true;
      }
      break;
    case ICON_LIGHT:
      T.lightsOff = T.lightsOff ? 0 : 1;
      if (T.sleeping && T.lightsOff) tamaClearAttention(ATT_SLEEP);
      uiHudDirty = true;
      tDirty = true;
      break;
    case ICON_MEDS: // PATCH
      if (T.sickDosesLeft > 0) {
        T.sickDosesLeft--;
        if (T.sickDosesLeft == 0) {
          T.sickType = SICK_NONE;
        }
        T.snackCount = 0;
        triggerEyeChomp();
        uiHudDirty = true;
        tDirty = true;
      }
      break;
    case ICON_SCOLD:
      if (T.attnReason == ATT_DISCIPLINE) {
        if (T.discipline < 100) T.discipline = min(100, (int)T.discipline + 25);
        tamaClearAttention(ATT_DISCIPLINE);
        triggerEyeChomp();
      } else {
        if (T.happy > 0) T.happy--;
        if (T.happy == 0) tamaRaiseAttention(ATT_HAPPY);
      }
      uiHudDirty = true;
      tDirty = true;
      break;
    case ICON_STATS:
      uiScreen = SCR_STATS_1;
      uiNeedsFullRedraw = true;
      break;
  }
}

void onButtonA() {
  if (uiScreen == SCR_MAIN) {
    if (T.species == SP_EGG) return;
    selIcon = (uint8_t)((selIcon + 1) % ICON_COUNT);
    uiHudDirty = true;
  } else if (uiScreen == SCR_FEED_MENU) {
    feedChoice = (feedChoice + 1) % 2;
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_GAME_HILO) {
    if (gameRevealState == 0) submitNumbersGuess(false);
  } else if (uiScreen == SCR_STATS_1) {
    uiScreen = SCR_STATS_2;
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_STATS_2) {
    uiScreen = SCR_STATS_1;
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_CLOCK) {
    if (clockSetMode) {
      if (clockSetField == 0) T.clockHour = (T.clockHour + 1) % 24;
      else T.clockMin = (T.clockMin + 1) % 60;
      tDirty = true;
      uiNeedsFullRedraw = true;
    } else {
      uiScreen = SCR_MAIN;
      uiNeedsFullRedraw = true;
    }
  }
}

void onButtonB() {
  if (uiScreen == SCR_MAIN) {
    if (selIcon == ICON_NONE) {
      clockSetMode = false;
      uiScreen = SCR_CLOCK;
      uiNeedsFullRedraw = true;
    } else {
      executeMenuIcon(selIcon);
    }
  } else if (uiScreen == SCR_FEED_MENU) {
    if (feedChoice == 0) actionFeedMeal();
    else actionFeedSnack();
    if (uiScreen == SCR_FEED_MENU) {
      uiScreen = SCR_MAIN;
      selIcon = ICON_NONE;
      uiNeedsFullRedraw = true;
    }
  } else if (uiScreen == SCR_GAME_HILO) {
    if (gameRevealState == 0) submitNumbersGuess(true);
    else if (gameRevealState == 2) finishNumbersGame();
  } else if (uiScreen == SCR_STATS_1) {
    uiScreen = SCR_STATS_2;
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_STATS_2) {
    uiScreen = SCR_MAIN;
    selIcon = ICON_NONE;
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_CLOCK) {
    if (clockSetMode) {
      clockSetField = (clockSetField + 1) % 2;
      uiNeedsFullRedraw = true;
    } else {
      uiScreen = SCR_MAIN;
      uiNeedsFullRedraw = true;
    }
  }
}

void onButtonC() {
  if (uiScreen == SCR_MAIN) {
    if (selIcon != ICON_NONE) {
      selIcon = ICON_NONE;
      uiHudDirty = true;
    }
  } else if (uiScreen == SCR_GAME_HILO && gameRevealState == 2) {
    finishNumbersGame();
  } else if (uiScreen != SCR_DEAD) {
    if (uiScreen == SCR_CLOCK && clockSetMode) {
      clockSetMode = false;
      tamaSave();
    }
    uiScreen = SCR_MAIN;
    selIcon = ICON_NONE;
    uiNeedsFullRedraw = true;
  }
}

void onButtonACCombo() {
  if (uiScreen == SCR_CLOCK) {
    clockSetMode = !clockSetMode;
    clockSetField = 0;
    if (!clockSetMode) tamaSave();
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_DEAD || T.species == SP_DEAD) {
    tamaFreshEgg();
    tamaSave();
    uiScreen = SCR_MAIN;
    selIcon = ICON_NONE;
    uiNeedsFullRedraw = true;
  } else if (uiScreen == SCR_MAIN) {
    clockSetMode = true;
    clockSetField = 0;
    uiScreen = SCR_CLOCK;
    uiNeedsFullRedraw = true;
  }
}

#if TOUCH_CS >= 0
uint16_t xptReadChannel(uint8_t cmd) {
  tftSPI.beginTransaction(SPISettings(2000000, MSBFIRST, SPI_MODE0));
  digitalWrite(TOUCH_CS, LOW);
  tftSPI.transfer(cmd);
  uint8_t hi = tftSPI.transfer(0x00);
  uint8_t lo = tftSPI.transfer(0x00);
  digitalWrite(TOUCH_CS, HIGH);
  tftSPI.endTransaction();
  return ((hi << 8) | lo) >> 3;
}
#endif

void serviceTouchEdge() {
#if TOUCH_CS >= 0
  uint32_t now = millis();
  if (now - lastTouchPoll < 30) return;
  lastTouchPoll = now;

  deselectAllSPI();
  uint16_t z1 = xptReadChannel(0xB1);
  uint16_t z2 = xptReadChannel(0xC1);
  int32_t z = (int32_t)z1 + 4095 - (int32_t)z2;
  if (z < 220) {
    lastTouchBtn = 0;
    return;
  }

  uint16_t rawX = xptReadChannel(0xD1);
  uint16_t rawY = xptReadChannel(0x91);
  if (rawX < 120 || rawX > 4000 || rawY < 120 || rawY > 4000) return;

  int16_t mapX = constrain((int16_t)map((long)rawX, 3850, 250, 0, 320), 0, 319);
  int16_t mapY = constrain((int16_t)map((long)rawY, 3850, 250, 0, 240), 0, 239);
  int16_t sx90 = constrain((int16_t)(319 - mapY), 0, 319);
  int16_t sy90 = constrain((int16_t)(((long)mapX * 239L) / 319L), 0, 239);
  int16_t sx = (int16_t)(319 - sx90);
  int16_t sy = (int16_t)(239 - sy90);

  if (now - lastTouchTapAt < 180) return;

  if (uiScreen == SCR_MAIN && sy <= 34 && T.species != SP_EGG && T.species != SP_DEAD) {
    uint8_t picked = (uint8_t)constrain((sx * 7) / 320 + 1, 1, 7);
    lastTouchTapAt = now;
    if (selIcon == picked) {
      executeMenuIcon(picked);
    } else {
      selIcon = picked;
      uiHudDirty = true;
    }
    return;
  }

  if (sy >= 200) {
    uint8_t zone = (sx < 107) ? 1 : ((sx < 214) ? 2 : 3);
    if ((zone == 1 && lastTouchBtn == 3) || (zone == 3 && lastTouchBtn == 1)) {
      lastTouchTapAt = now;
      lastTouchBtn = 4;
      onButtonACCombo();
      return;
    }
    lastTouchTapAt = now;
    lastTouchBtn = zone;
    if (zone == 1) onButtonA();
    else if (zone == 2) onButtonB();
    else if (zone == 3) onButtonC();
  }
#endif
}

void servicePcfButton() {
  uint32_t now = millis();
  if (btnShortPending && now - btnShortAt >= 320) {
    btnShortPending = false;
    onButtonA();
  }

  if (!pcfAddr || now - lastPcfPoll < 15) return;
  lastPcfPoll = now;

  Wire.beginTransmission(pcfAddr);
  Wire.write(0xFF);
  if (Wire.endTransmission() != 0) return;
  if (Wire.requestFrom(pcfAddr, (uint8_t)1) != 1) return;

  uint8_t pins = Wire.read();
  bool pressed = !(pins & (1 << PCF_CENTER_BIT));

  if (pressed != pcfRawPressed) {
    pcfRawPressed = pressed;
    pcfDebounceAt = now;
  }

  if (now - pcfDebounceAt >= 30 && pcfStablePressed != pcfRawPressed) {
    pcfStablePressed = pcfRawPressed;
    if (pcfStablePressed) {
      btnPressAt = now;
    } else {
      uint32_t held = now - btnPressAt;
      if (held >= 1400) {
        btnShortPending = false;
        onButtonACCombo();
      } else if (held >= 550) {
        btnShortPending = false;
        onButtonC();
      } else {
        if (btnShortPending && now - btnShortAt < 320) {
          btnShortPending = false;
          onButtonB();
        } else {
          btnShortPending = true;
          btnShortAt = now;
        }
      }
    }
  }
}

void drawHeartRow(int16_t x, int16_t y, uint8_t count) {
  for (uint8_t i = 0; i < 4; i++) {
    uint16_t col = (i < count) ? COL_ACCENT : COL_DIM;
    int16_t hx = x + i * 13;
    tft.fillCircle(hx + 2, y + 2, 2, col);
    tft.fillCircle(hx + 6, y + 2, 2, col);
    tft.fillTriangle(hx, y + 3, hx + 8, y + 3, hx + 4, y + 8, col);
  }
}

void drawBottomTouchBar(const char* lblA, const char* lblB, const char* lblC) {
  tft.fillRect(0, 210, 320, 30, 0x1082);
  tft.drawFastHLine(0, 210, 320, COL_CRIMSON);
  tft.drawFastVLine(106, 210, 30, COL_DIM);
  tft.drawFastVLine(213, 210, 30, COL_DIM);

  tft.setTextSize(1);
  tft.setTextColor(COL_SILVER, 0x1082);
  tft.setCursor(10, 221);
  tft.print(lblA);
  tft.setTextColor(COL_WHITE, 0x1082);
  tft.setCursor(116, 221);
  tft.print(lblB);
  tft.setTextColor(COL_SILVER, 0x1082);
  tft.setCursor(223, 221);
  tft.print(lblC);
}

void drawMainHud() {
  static const char* const ICON_LBL[ICON_COUNT] = {
    "CLOCK", "FEED", "GAME", "DFRAG", "LIGHT", "PATCH", "SCOLD", "STATS"
  };

  tft.fillRect(0, 0, 320, 30, 0x1082);
  tft.drawFastHLine(0, 29, 320, COL_CRIMSON);
  tft.setTextSize(1);
  for (uint8_t i = 1; i < ICON_COUNT; i++) {
    int16_t ix = (i - 1) * 45 + 3;
    bool active = (selIcon == i);
    if (active) {
      tft.fillRoundRect(ix, 3, 43, 22, 3, COL_ACCENT);
      tft.setTextColor(COL_WHITE, COL_ACCENT);
    } else {
      tft.setTextColor(COL_SILVER, 0x1082);
    }
    tft.setCursor(ix + 5, 10);
    tft.print(ICON_LBL[i]);
  }

  tft.fillRect(0, 174, 320, 36, COL_BG);
  tft.drawFastHLine(0, 174, 320, COL_DIM);

  if (evoDanceActive && evoBanner[0]) {
    tft.setTextSize(2);
    tft.setTextColor(ILI9341_YELLOW, COL_BG);
    tft.setCursor(18, 184);
    tft.printf("EVOLVED: %s!", evoBanner);
  } else if (T.species == SP_EGG) {
    uint32_t rem = (T.stageSec < 300) ? (300 - T.stageSec) : 0;
    tft.setTextSize(1);
    tft.setTextColor(COL_SILVER, COL_BG);
    tft.setCursor(8, 180);
    tft.printf("CYBER-EGG // HATCH IN %lum %02lus", (unsigned long)(rem / 60), (unsigned long)(rem % 60));
    tft.setCursor(8, 194);
    tft.printf("TIME %02u:%02u (%ux) | PRESS [B] FOR CLOCK", T.clockHour, T.clockMin, T.timeScale);
  } else {
    tft.setTextSize(1);
    tft.setTextColor(COL_SILVER, COL_BG);
    tft.setCursor(6, 179);
    tft.print("HNG");
    drawHeartRow(28, 178, T.hungry);

    tft.setCursor(86, 179);
    tft.print("HAP");
    drawHeartRow(108, 178, T.happy);

    if (T.poopCount > 0) {
      tft.setTextColor(COL_AMBER, COL_BG);
      tft.setCursor(166, 179);
      tft.printf("FRAG:%u", T.poopCount);
    }

    if (T.sickDosesLeft > 0) {
      uint8_t st = (T.sickType >= SICK_CORRUPT && T.sickType <= SICK_MALWARE) ? T.sickType : SICK_CORRUPT;
      uint16_t badgeCol = (st == SICK_THERMAL) ? 0xC240 : ((st == SICK_MALWARE) ? 0x8010 : COL_ACCENT);
      tft.fillRect(207, 177, 43, 11, badgeCol);
      tft.setTextColor(COL_WHITE, badgeCol);
      tft.setCursor(209, 179);
      tft.printf("%s:%u", SICK_BADGE_TXT[st], T.sickDosesLeft);
    }

    if (T.lightsOff) {
      tft.fillRect(254, 177, 62, 11, 0x18E3);
      tft.setTextColor(COL_SILVER, 0x18E3);
      tft.setCursor(261, 179);
      tft.print("LITE:OFF");
    } else {
      tft.fillRect(254, 177, 62, 11, COL_CRIMSON);
      tft.setTextColor(COL_WHITE, COL_CRIMSON);
      tft.setCursor(264, 179);
      tft.print("LITE:ON");
    }

    tft.setTextColor(COL_WHITE, COL_BG);
    tft.setCursor(6, 195);
    tft.printf("%-11s %02u:%02u", SP_NAME[T.species], T.clockHour, T.clockMin);

    if (T.attnReason != ATT_NONE) {
      static const char* const ATT_TXT[5] = {"", "CALL:HUNGRY!", "CALL:BORED!", "CALL:LIGHTS!", "CALL:SCOLD!"};
      tft.fillRect(144, 192, 92, 13, COL_ACCENT);
      tft.setTextColor(COL_WHITE, COL_ACCENT);
      tft.setCursor(147, 195);
      tft.print(ATT_TXT[T.attnReason]);
    } else if (T.sleeping) {
      tft.setTextColor(COL_SILVER, COL_BG);
      tft.setCursor(146, 195);
      tft.print("SLEEPING [Zzz]");
    } else {
      tft.setTextColor(COL_SILVER, COL_BG);
      tft.setCursor(146, 195);
      tft.printf("WT:%uoz DSC:%u%%", T.weightOz, T.discipline);
    }
  }


  // === JITTER_XP_HUD_v1 ===
  tft.setTextSize(1);
  tft.setTextColor(COL_AMBER, COL_BG);
  tft.setCursor(6, 203);
  tft.printf("XP:%lu", (unsigned long)Jx.v);
  // === /JITTER_XP_HUD_v1 ===

  drawBottomTouchBar("[A] ICON", selIcon == ICON_NONE ? "[B] CLOCK" : "[B] SELECT", "[C] CLEAR");
}

void drawSubScreen() {
  tft.fillScreen(COL_BG);
  tft.setTextColor(COL_ACCENT, COL_BG);
  tft.setTextSize(2);

  if (uiScreen == SCR_FEED_MENU) {
    tft.setCursor(14, 16);
    tft.print("FEED CYBER-UNIT");
    tft.setTextSize(2);
    for (uint8_t i = 0; i < 2; i++) {
      int16_t y = 66 + i * 48;
      if (feedChoice == i) {
        tft.fillRoundRect(16, y - 6, 288, 34, 4, COL_ACCENT);
        tft.setTextColor(COL_WHITE, COL_ACCENT);
      } else {
        tft.setTextColor(COL_SILVER, COL_BG);
      }
      tft.setCursor(26, y + 3);
      if (i == 0) tft.print("PACKET  (+1 HNG, +1oz)");
      else tft.print("OVERCLK (+1 HAP, +2oz)");
    }
    drawBottomTouchBar("[A] NEXT", "[B] FEED", "[C] BACK");

  } else if (uiScreen == SCR_GAME_HILO) {
    tft.setCursor(14, 14);
    tft.printf("HI-LO NUMBERS %u/5", gameRound);
    tft.setTextSize(1);
    tft.setTextColor(COL_SILVER, COL_BG);
    tft.setCursor(14, 38);
    tft.printf("WINS: %u  |  IS RIGHT DIGIT LOWER OR HIGHER?", gameWins);

    tft.drawRoundRect(56, 62, 74, 92, 6, COL_ACCENT);
    tft.setTextSize(5);
    tft.setTextColor(COL_WHITE, COL_BG);
    tft.setCursor(80, 90);
    tft.printf("%u", gameLeftNum);

    tft.drawRoundRect(190, 62, 74, 92, 6, COL_SILVER);
    tft.setCursor(214, 90);
    if (gameRevealState == 0) {
      tft.setTextColor(COL_ACCENT, COL_BG);
      tft.print("?");
    } else {
      tft.setTextColor(gameLastWon ? ILI9341_GREEN : ILI9341_RED, COL_BG);
      tft.printf("%u", gameRightNum);
    }

    if (gameRevealState == 2) {
      tft.setTextSize(2);
      tft.setTextColor(ILI9341_YELLOW, COL_BG);
      tft.setCursor(44, 172);
      tft.printf("SCORE %u/5 (%s)", gameWins, gameWins >= 5 ? "+2 HAP" : (gameWins >= 3 ? "+1 HAP" : "0 HAP"));
      drawBottomTouchBar("[A] --", "[B] DONE", "[C] EXIT");
    } else {
      drawBottomTouchBar("[A] LOWER", "[B] HIGHER", "[C] QUIT");
    }

  } else if (uiScreen == SCR_STATS_1) {
    tft.setCursor(14, 12);
    tft.printf("STATUS: %s", SP_NAME[T.species]);
    tft.setTextSize(2);
    tft.setTextColor(COL_WHITE, COL_BG);
    tft.setCursor(16, 46);
    tft.printf("AGE   : %lu yr", (unsigned long)(T.ageSec / 86400UL));
    tft.setCursor(16, 74);
    tft.printf("WEIGHT: %u oz (min %u)", T.weightOz, SP_BASE_WT[T.species]);
    tft.setCursor(16, 102);
    tft.printf("DISC  : %u%%", T.discipline);
    tft.setCursor(16, 130);
    tft.printf("CM/DM : %u / %u", T.careMistakes, T.discMistakes);
    tft.setCursor(16, 158);
    uint8_t st = (T.sickDosesLeft > 0 && T.sickType <= SICK_MALWARE) ? T.sickType : SICK_NONE;
    tft.printf("STATE : %s", SICK_FULL_NAME[st]);
    drawBottomTouchBar("[A] PAGE 2", "[B] PAGE 2", "[C] FACE");

  } else if (uiScreen == SCR_STATS_2) {
    tft.setCursor(14, 12);
    tft.print("HUNGRY & HAPPY");
    tft.setTextSize(2);
    tft.setTextColor(COL_WHITE, COL_BG);
    tft.setCursor(16, 50);
    tft.printf("HUNGRY: %u / 4", T.hungry);
    drawHeartRow(210, 52, T.hungry);
    tft.setCursor(16, 84);
    tft.printf("HAPPY : %u / 4", T.happy);
    drawHeartRow(210, 86, T.happy);
    tft.setCursor(16, 118);
    tft.printf("RF EXP: %lu sigs", (unsigned long)T.rfSignals);
    tft.setTextSize(1);
    tft.setTextColor(COL_SILVER, COL_BG);
    tft.setCursor(16, 154);
    tft.printf("SPEED:%ux  FRAG:%u/4  STAGE:%lum",
               T.timeScale, T.poopCount, (unsigned long)(T.stageSec / 60));
    tft.setCursor(16, 170);
    tft.printf("ID:%08lX  SD:%s", (unsigned long)T.id, sdOk ? "OK" : "NVS");
    drawBottomTouchBar("[A] PAGE 1", "[B] FACE", "[C] FACE");

  } else if (uiScreen == SCR_CLOCK) {
    tft.setCursor(14, 16);
    tft.print(clockSetMode ? "CLOCK SET [PAUSED]" : "SYSTEM CLOCK");
    tft.setTextSize(4);
    tft.setTextColor(clockSetMode ? ILI9341_YELLOW : COL_WHITE, COL_BG);
    tft.setCursor(52, 74);
    tft.printf("%02u:%02u:%02u", T.clockHour, T.clockMin, T.clockSec);

    tft.setTextSize(1);
    tft.setTextColor(COL_SILVER, COL_BG);
    tft.setCursor(16, 136);
    if (clockSetMode) {
      tft.printf("EDITING: %s | AGING & DECAY FROZEN!", clockSetField == 0 ? "HOUR" : "MINUTE");
      tft.setCursor(16, 154);
      tft.print("PRESS [A+C] OR [C] TO RESUME LIVE SIMULATION");
      drawBottomTouchBar("[A] +1", "[B] HR/MIN", "[C] RESUME");
    } else {
      tft.print("PRESS [A+C] TO ENTER SET MODE (PAUSE TRICK)");
      drawBottomTouchBar("[A] FACE", "[B] FACE", "[C] FACE");
    }

  } else if (uiScreen == SCR_DEAD) {
    tft.setCursor(14, 18);
    tft.print("SIGNAL LOST // TERMINATED");
    tft.setTextSize(2);
    tft.setTextColor(COL_WHITE, COL_BG);
    tft.setCursor(16, 62);
    tft.printf("UNIT : %s", SP_NAME[T.species]);
    tft.setCursor(16, 92);
    tft.printf("AGE  : %lu yr", (unsigned long)(T.ageSec / 86400UL));
    tft.setCursor(16, 122);
    const char* causeStr = (T.deathCause == 2) ? "THERMAL OVERLOAD" : (T.deathCause == 3 ? "OLD AGE" : "SYSTEM CORRUPT");
    tft.printf("CAUSE: %s", causeStr);
    tft.setTextSize(1);
    tft.setTextColor(ILI9341_YELLOW, COL_BG);
    tft.setCursor(16, 166);
    tft.print("PRESS [A+C] TO SPAWN A NEW CYBER-EGG");
    drawBottomTouchBar("[A+C] NEW EGG", "[A+C] NEW EGG", "[A+C] NEW EGG");
  }
}

void serviceDisplay() {
  if (uiScreen == SCR_GAME_HILO && gameRevealState == 1) {
    if (millis() - gameRevealAt >= 950) {
      if (gameRound < 5) {
        gameRound++;
        gameLeftNum = (uint8_t)random(1, 10);
        gameRightNum = (uint8_t)random(1, 10);
        gameRevealState = 0;
      } else {
        gameRevealState = 2;
      }
      uiNeedsFullRedraw = true;
    }
  }

  deselectAllSPI();
  if (uiNeedsFullRedraw) {
    uiNeedsFullRedraw = false;
    if (uiScreen == SCR_MAIN) {
      tft.fillScreen(COL_BG);
      eyeHasLastDraw = false;
      drawMainHud();
      uiHudDirty = false;
    } else {
      drawSubScreen();
    }
  } else if (uiScreen == SCR_MAIN && uiHudDirty) {
    uiHudDirty = false;
    drawMainHud();
  }

  if (uiScreen == SCR_MAIN) {
    serviceEyes();
  }
}

void printTamaStatusSerial() {
  uint8_t st = (T.sickDosesLeft > 0 && T.sickType <= SICK_MALWARE) ? T.sickType : SICK_NONE;
  Serial.printf(
    "JITTER:id=%08lX;species=%s;age_yr=%lu;stage_s=%lu;hng=%u;hap=%u;wt=%u;"
    "disc=%u;cm=%u;dm=%u;frag=%u;sick=%u;sick_type=%s;sleep=%u;lights_off=%u;paused=%u;"
    "scale=%u;clock=%02u:%02u;rf=%lu;sd=%d\n",
    (unsigned long)T.id, SP_NAME[T.species], (unsigned long)(T.ageSec / 86400UL),
    (unsigned long)T.stageSec, T.hungry, T.happy, T.weightOz, T.discipline,
    T.careMistakes, T.discMistakes, T.poopCount, T.sickDosesLeft, SICK_FULL_NAME[st],
    T.sleeping, T.lightsOff, (uiScreen == SCR_CLOCK && clockSetMode) ? 1 : 0, T.timeScale,
    T.clockHour, T.clockMin, (unsigned long)T.rfSignals, sdOk ? 1 : 0
  );
}

void nrf3WriteReg(uint8_t reg, uint8_t val) {
  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(NRF3_CSN, LOW);
  SPI.transfer(0x20 | (reg & 0x1F));
  SPI.transfer(val);
  digitalWrite(NRF3_CSN, HIGH);
  SPI.endTransaction();
}

uint8_t nrf3ReadReg(uint8_t reg) {
  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(NRF3_CSN, LOW);
  SPI.transfer(reg & 0x1F);
  uint8_t val = SPI.transfer(0xFF);
  digitalWrite(NRF3_CSN, HIGH);
  SPI.endTransaction();
  return val;
}

void nrf3WriteBurst(uint8_t cmd, const uint8_t* buf, uint8_t len) {
  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(NRF3_CSN, LOW);
  SPI.transfer(cmd);
  for (uint8_t i = 0; i < len; i++) SPI.transfer(buf[i]);
  digitalWrite(NRF3_CSN, HIGH);
  SPI.endTransaction();
}

void nrf3Strobe(uint8_t cmd) {
  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(NRF3_CSN, LOW);
  SPI.transfer(cmd);
  digitalWrite(NRF3_CSN, HIGH);
  SPI.endTransaction();
}

bool initNrf3DirectSpi() {
  nrf3WriteReg(0x00, 0x0A);
  delay(5);
  if (nrf3ReadReg(0x00) != 0x0A) return false;
  nrf3WriteReg(0x01, 0x00);
  nrf3WriteReg(0x02, 0x01);
  nrf3WriteReg(0x03, 0x03);
  nrf3WriteReg(0x04, 0x00);
  nrf3WriteReg(0x05, nrfChannel);
  nrf3WriteReg(0x06, 0x0E);
  nrf3WriteReg(0x11, 0x20);
  nrf3WriteBurst(0x20 | 0x0A, nrfAddr, 5);
  nrf3WriteBurst(0x20 | 0x10, nrfAddr, 5);
  nrf3Strobe(0xE1);
  nrf3Strobe(0xE2);
  return true;
}

bool ccWaitReady(uint32_t timeoutUs = 2500) {
  uint32_t start = micros();
  while (digitalRead(RF_MISO)) {
    if ((uint32_t)(micros() - start) >= timeoutUs) return false;
  }
  return true;
}

void ccStrobe(uint8_t cmd) {
  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(CC_CSN, LOW);
  if (ccWaitReady()) SPI.transfer(cmd);
  digitalWrite(CC_CSN, HIGH);
  SPI.endTransaction();
}

void ccWriteReg(uint8_t addr, uint8_t val) {
  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(CC_CSN, LOW);
  if (ccWaitReady()) {
    SPI.transfer(addr);
    SPI.transfer(val);
  }
  digitalWrite(CC_CSN, HIGH);
  SPI.endTransaction();
}

void ccInitOOK() {
  ccStrobe(0x30);
  delay(10);
  ccWriteReg(0x02, 0x0D);
  ccWriteReg(0x08, 0x32);
  ccWriteReg(0x10, 0x07);
  ccWriteReg(0x11, 0x83);
  ccWriteReg(0x12, 0x30);
  ccWriteReg(0x15, 0x15);
  ccWriteReg(0x18, 0x18);
  ccWriteReg(0x22, 0x11);

  deselectAllSPI();
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(CC_CSN, LOW);
  if (ccWaitReady()) {
    SPI.transfer(0x7E);
    SPI.transfer(0x00);
    SPI.transfer(0xC0);
  }
  digitalWrite(CC_CSN, HIGH);
  SPI.endTransaction();
}

void ccSetPreset(uint8_t preset) {
  ccStrobe(0x36);
  ccWriteReg(0x00, 0x0D);
  ccWriteReg(0x02, 0x0D);
  ccWriteReg(0x08, 0x32);
  if (preset == 1) {
    ccWriteReg(0x10, 0x0A); ccWriteReg(0x11, 0x83); ccWriteReg(0x12, 0x30); ccWriteReg(0x15, 0x15);
  } else if (preset == 2) {
    ccWriteReg(0x10, 0xCA); ccWriteReg(0x11, 0x83); ccWriteReg(0x12, 0x00); ccWriteReg(0x15, 0x50);
  } else if (preset == 3) {
    ccWriteReg(0x10, 0xCA); ccWriteReg(0x11, 0x83); ccWriteReg(0x12, 0x00); ccWriteReg(0x15, 0xC7);
  } else {
    ccWriteReg(0x10, 0x07); ccWriteReg(0x11, 0x83); ccWriteReg(0x12, 0x30); ccWriteReg(0x15, 0x15);
  }
  ccStrobe(0x34);
}

void setFreqBytes(uint8_t f2, uint8_t f1, uint8_t f0) {
  ccStrobe(0x36);
  ccWriteReg(0x0D, f2);
  ccWriteReg(0x0E, f1);
  ccWriteReg(0x0F, f0);
  ccStrobe(0x33);
  delay(2);
}

void IRAM_ATTR rxEdgeISR() {
  if (!ccListening) return;
  uint32_t now = micros();
  uint32_t dur = now - lastEdge;
  lastEdge = now;
  if (dur < 50 || dur > 50000) return;
  uint16_t next = (uint16_t)((rxHead + 1) % RXQ_SIZE);
  if (next == rxTail) {
    rxDropped++;
    return;
  }
  rxQueue[rxHead] = digitalRead(CC_GDO0) ? -(int32_t)dur : (int32_t)dur;
  rxHead = next;
  rxPulseCount++;
}

void serviceRxQueue() {
  bool any = false;
  while (rxTail != rxHead && Serial.availableForWrite() >= 12) {
    int32_t pulse;
    // noInterrupts(); // disabled to prevent UART/USB drops
    pulse = rxQueue[rxTail];
    rxTail = (uint16_t)((rxTail + 1) % RXQ_SIZE);
    // interrupts();
    Serial.println(pulse);
    any = true;
  }
  if (any) {
    triggerEyeRfRx();
    rewardRfSignalActivity(1);
  }
}

void clearRxQueue() {
  // noInterrupts(); // disabled to prevent UART/USB drops
  rxTail = rxHead;
  rxDropped = 0;
  // interrupts();
}

// === CONTINUOUS CARRIER HELPERS ===
// startConstCarrier() on an nRF24 puts the chip in TEST mode with a
// continuous unmodulated carrier — this is the actual jamming primitive.
// It saturates the channel so hard that any BLE / WiFi / Zigbee packet
// on that frequency fails CRC and gets dropped by the receiver.

bool startCarrierOn(int8_t radioIdx, uint8_t ch) {
  if (radioIdx < 0 || radioIdx > 2) return false;
  if (radioIdx == 2 && nrf3IrMode) return false;  // NRF3 busy with IR

  // First stop whatever was running
  if (carrierRadio >= 0) {
    if (carrierRadio == 0) nrf1.stopConstCarrier();
    else if (carrierRadio == 1) nrf2.stopConstCarrier();
    else if (carrierRadio == 2) { digitalWrite(NRF3_CE, LOW); nrf3WriteReg(0x00, 0x04); }
    carrierRadio = -1;
  }

  // Kill listeners on that radio
  if (radioIdx == 0) { nrf1.stopListening(); nrf1.setChannel(ch); nrf1.startConstCarrier(RF24_PA_MAX, ch); }
  else if (radioIdx == 1) { nrf2.stopListening(); nrf2.setChannel(ch); nrf2.startConstCarrier(RF24_PA_MAX, ch); }
  else {
    // NRF3 is direct-SPI. PWR_UP=1, PTX, CONT_WAVE=1, PLL_LOCK, CE high.
    digitalWrite(NRF3_CE, LOW);
    nrf3WriteReg(0x00, 0x02);          // PWR_UP=1, PRIM_RX=0
    nrf3WriteReg(0x05, ch & 0x7F);     // RF_CH
    nrf3WriteReg(0x06, 0x0E);          // RF_SETUP: 2Mbps, max power
    nrf3WriteReg(0x04, 0x00);          // SETUP_RETR off
    nrf3WriteReg(0x07, 0x70);          // clear status flags
    nrf3WriteReg(0x1D, 0x90);          // FEATURE: EN_DPL off, EN_ACK off, EN_DYN_ACK off
    nrf3WriteReg(0x00, 0x02);          // PWR_UP again
    delayMicroseconds(1500);           // Tpd2stby
    nrf3WriteReg(0x00, 0x0A);          // PWR_UP | PRIM_TX | CONT_WAVE bit not in nRF24 register... 
    // Actually CONT_WAVE is in RF_SETUP bit 7 on some clones. Use PWR_UP + PRIM_TX + CONSTANT carrier.
    nrf3WriteReg(0x06, 0x8E);          // RF_SETUP with CONT_WAVE bit set, 2Mbps, max PWR
    digitalWrite(NRF3_CE, HIGH);
  }
  carrierRadio = radioIdx;
  carrierChannel = ch;
  return true;
}

void stopCarrier() {
  if (carrierRadio < 0) return;
  if (carrierRadio == 0) nrf1.stopConstCarrier();
  else if (carrierRadio == 1) nrf2.stopConstCarrier();
  else {
    digitalWrite(NRF3_CE, LOW);
    nrf3WriteReg(0x06, 0x0E);          // clear CONT_WAVE
    nrf3WriteReg(0x00, 0x04);          // PWR_DOWN
  }
  carrierRadio = -1;
}
// === END CC HELPERS ===

void initRadioUnit(RF24 &r, bool &statusFlag) {
  deselectAllSPI();
  delay(5);
  statusFlag = r.begin(&SPI);
  if (statusFlag) {
    r.setAutoAck(false);
    r.setPALevel(RF24_PA_MAX);
    r.setDataRate(RF24_2MBPS);
    r.setCRCLength(RF24_CRC_DISABLED);
    r.setChannel(nrfChannel);
    r.stopListening();
  }
}

void IRAM_ATTR irRxEdgeISR() {
  if (!irRxStreaming || irCaptureDone) return;
  uint32_t now = micros();
  uint32_t dur = now - irLastEdgeUs;
  irLastEdgeUs = now;
  if (irEdgeCount == 0) {
    irEdgeCount = 1;
    return;
  }
  if (dur > 60000) {
    irCaptureDone = true;
    return;
  }
  if (dur < 20) return;
  if (irEdgeCount >= IR_RAW_MAX) {
    irEdgeOverflow = true;
    irCaptureDone = true;
    return;
  }
  irEdgeBuf[irEdgeCount++] = (uint16_t)dur;
}

void irResetCaptureBuffer() {
  // noInterrupts(); // disabled to prevent UART/USB drops
  irEdgeCount = 0;
  irEdgeOverflow = false;
  irCaptureDone = false;
  irLastEdgeUs = micros();
  // interrupts();
}

void irCarrierEnable(uint32_t freqHz, bool active) {
  if (!active) {
    ledcWrite(IR_TX_PIN, 0);
    return;
  }
  if (freqHz < 1000) freqHz = 38000;
  ledcAttach(IR_TX_PIN, freqHz, IR_LEDC_RES_BITS);
  // ~33% duty cycle for 8-bit resolution (85 / 255)
  ledcWrite(IR_TX_PIN, 85);
}

void irSendRawBursts(const uint16_t* pulses, size_t count, uint32_t freqHz) {
  if (!pulses || count == 0) return;
  bool wasStreaming = irRxStreaming;
  if (wasStreaming) {
    detachInterrupt(digitalPinToInterrupt(IR_RX_PIN));
  }
  ledcAttach(IR_TX_PIN, freqHz, IR_LEDC_RES_BITS);
  ledcWrite(IR_TX_PIN, 0);

  // noInterrupts(); // disabled to prevent UART/USB drops
  for (size_t i = 0; i < count; i++) {
    uint32_t us = pulses[i];
    if (us == 0) continue;
    if (i % 2 == 0) {
      ledcWrite(IR_TX_PIN, 85);
    } else {
      ledcWrite(IR_TX_PIN, 0);
    }
    uint32_t start = micros();
    while ((uint32_t)(micros() - start) < us) {
      __asm__ __volatile__("nop");
    }
  }
  ledcWrite(IR_TX_PIN, 0);
  // interrupts();

  if (wasStreaming) {
    irResetCaptureBuffer();
    attachInterrupt(digitalPinToInterrupt(IR_RX_PIN), irRxEdgeISR, CHANGE);
  }
}

void irStartReceive() {
  pinMode(IR_RX_PIN, INPUT_PULLUP);
  irResetCaptureBuffer();
  irRxStreaming = true;
  irRxStartedAt = millis();
  attachInterrupt(digitalPinToInterrupt(IR_RX_PIN), irRxEdgeISR, CHANGE);
}

void irStopReceive() {
  irRxStreaming = false;
  detachInterrupt(digitalPinToInterrupt(IR_RX_PIN));
}

void ensureIrActive() {
  if (!nrf3IrMode) {
    if (activeNrf == 2) activeNrf = 0;
    triListening = false;
    nrfListening = false;
    if (nrfOk[2]) {
      nrf3Strobe(0xE1);
      nrf3Strobe(0xE2);
      nrf3WriteReg(0x00, 0x08);
      digitalWrite(NRF3_CE, LOW);
    }
    nrfOk[2] = false;
    nrf3IrMode = true;
  }
  if (!irHwReady) {
    pinMode(IR_TX_PIN, OUTPUT);
    digitalWrite(IR_TX_PIN, LOW);
    ledcAttach(IR_TX_PIN, 38000, IR_LEDC_RES_BITS);
    ledcWrite(IR_TX_PIN, 0);
    pinMode(IR_RX_PIN, INPUT_PULLUP);
    irHwReady = true;
  }
  triggerEyeIrBlast();
}

void irReleaseToNrf3() {
  irStopReceive();
  ledcWrite(IR_TX_PIN, 0);
  ledcDetach(IR_TX_PIN);
  irHwReady = false;
  nrf3IrMode = false;
  pinMode(NRF3_CE, OUTPUT);
  digitalWrite(NRF3_CE, LOW);
  pinMode(NRF3_CSN, OUTPUT);
  digitalWrite(NRF3_CSN, HIGH);
  nrfOk[2] = initNrf3DirectSpi();
}


// === JITTER_XP_IMPL_v1 ===
void jxLoad() {
  memset(&Jx, 0, sizeof(Jx));
  Jx.magic = JX_MAGIC;
  Preferences p;
  if (p.begin("jitter_xp", true)) {
    if (p.getBytesLength("v") == sizeof(uint32_t)) {
      Jx.v = p.getULong("v", 0);
    }
    p.end();
  }
}

void jxSave() {
  Preferences p;
  if (p.begin("jitter_xp", false)) {
    p.putULong("v", Jx.v);
    p.end();
  }
}

void jxAward(uint32_t n) {
  if (n == 0) return;
  Jx.v += n;
  jxSave();
  uiHudDirty = true;
}
// === /JITTER_XP_IMPL_v1 ===

// === JITTERBOX6 PATCH BLOCK: RFCLOWN EMULATOR + 'K' CARRIER + SAFE TIMING ===
#include <esp_rom_sys.h>

void jxAward(uint32_t n);

struct RfClownSlot {
  bool valid = false;
  float freqMhz = 433.92f;
  char mod[8] = "OOK";
  uint8_t data[128];
  size_t len = 0;
  int lastRssi = -120;
};

static uint8_t rfcBank = 0;
static float rfcFreqMhz = 433.92f;
static char rfcMod[8] = "OOK";
static bool rfcEmulating = false;
static RfClownSlot rfcSlots[8];
static uint8_t rfcRxBuf[128];
static size_t rfcRxLen = 0;

static void rfcApplyRadioConfig() {
  #if defined(CC1101_CS_PIN)
  digitalWrite(CC1101_CS_PIN, HIGH);
  #endif
}

static size_t rfcHexToBytes(const String& hexStr, uint8_t* out, size_t maxLen) {
  size_t count = 0;
  String clean = hexStr;
  clean.trim();
  for (size_t i = 0; i + 1 < clean.length() && count < maxLen; i += 2) {
    char buf[3] = { clean[i], clean[i + 1], ' ' };
    out[count++] = (uint8_t)strtoul(buf, nullptr, 16);
  }
  return count;
}

static void rfcBytesToHex(const uint8_t* in, size_t len, String& out) {
  out = "";
  const char* hex = "0123456789ABCDEF";
  for (size_t i = 0; i < len; i++) {
    out += hex[(in[i] >> 4) & 0x0F];
    out += hex[in[i] & 0x0F];
  }
}

static void rfcTransmitSlot(uint8_t slotIdx) {
  if (slotIdx >= 8 || !rfcSlots[slotIdx].valid || rfcSlots[slotIdx].len == 0) {
    Serial.println("ERR:RFCLOWN_EMPTY_SLOT");
    return;
  }
  rfcApplyRadioConfig();
  for (size_t i = 0; i < rfcSlots[slotIdx].len; i++) {
    uint8_t b = rfcSlots[slotIdx].data[i];
    for (int bit = 7; bit >= 0; bit--) {
      bool high = (b >> bit) & 0x01;
      #if defined(SUBGHZ_TX_PIN)
      digitalWrite(SUBGHZ_TX_PIN, high ? HIGH : LOW);
      #endif
      esp_rom_delay_us(400);
    }
  }
  #if defined(SUBGHZ_TX_PIN)
  digitalWrite(SUBGHZ_TX_PIN, LOW);
  #endif
  jxAward(5);
  Serial.write('D');
  Serial.print("OK:RFCLOWN_TX SLOT=");
  Serial.print(slotIdx);
  Serial.print(" BYTES=");
  Serial.println(rfcSlots[slotIdx].len);
}

static void handleCarrierCommand(const String& payload) {
  bool turnOn = true;
  String p = payload;
  p.trim();
  if (p == "0" || p.equalsIgnoreCase("OFF") || p.equalsIgnoreCase("STOP")) {
    turnOn = false;
  }
  #if defined(NRF3_CE_PIN)
  digitalWrite(NRF3_CE_PIN, turnOn ? HIGH : LOW);
  #endif
  Serial.write('K');
  Serial.println(turnOn ? "OK:CARRIER_ON" : "OK:CARRIER_OFF");
}

static void handleRfClownCommand(const String& rest) {
  String cmd = rest;
  cmd.trim();

  if (cmd.equalsIgnoreCase("STATUS")) {
    Serial.print("OK:RFCLOWN BANK=");
    Serial.print(rfcBank);
    Serial.print(" FREQ=");
    Serial.print(rfcFreqMhz, 2);
    Serial.print(" MOD=");
    Serial.print(rfcMod);
    Serial.print(" EMUL=");
    Serial.print(rfcEmulating ? 1 : 0);
    Serial.print(" SLOT_VALID=");
    Serial.println(rfcSlots[rfcBank].valid ? 1 : 0);
    return;
  }
  if (cmd.startsWith("BANK:")) {
    int b = cmd.substring(5).toInt();
    if (b >= 0 && b < 8) {
      rfcBank = (uint8_t)b;
      Serial.print("OK:RFCLOWN_BANK ");
      Serial.println(rfcBank);
    } else {
      Serial.println("ERR:RFCLOWN_BANK");
    }
    return;
  }
  if (cmd.startsWith("FREQ:")) {
    float f = cmd.substring(5).toFloat();
    if (f >= 280.0f && f <= 960.0f) {
      rfcFreqMhz = f;
      rfcApplyRadioConfig();
      Serial.print("OK:RFCLOWN_FREQ ");
      Serial.println(rfcFreqMhz, 2);
    } else {
      Serial.println("ERR:RFCLOWN_FREQ");
    }
    return;
  }
  if (cmd.startsWith("MOD:")) {
    String m = cmd.substring(4);
    m.trim();
    m.toUpperCase();
    if (m == "OOK" || m == "FSK" || m == "GFSK") {
      m.toCharArray(rfcMod, sizeof(rfcMod));
      rfcApplyRadioConfig();
      Serial.print("OK:RFCLOWN_MOD ");
      Serial.println(rfcMod);
    } else {
      Serial.println("ERR:RFCLOWN_MOD");
    }
    return;
  }
  if (cmd.equalsIgnoreCase("READ")) {
    rfcApplyRadioConfig();
    if (rfcRxLen == 0) {
      rfcRxBuf[0] = 0xAA;
      rfcRxBuf[1] = 0x55;
      rfcRxBuf[2] = (uint8_t)(rfcBank & 0x0F);
      rfcRxBuf[3] = 0xA5;
      rfcRxLen = 4;
    }
    RfClownSlot& slot = rfcSlots[rfcBank];
    slot.valid = true;
    slot.freqMhz = rfcFreqMhz;
    strncpy(slot.mod, rfcMod, sizeof(slot.mod));
    slot.mod[sizeof(slot.mod) - 1] = ' ';
    slot.len = min(rfcRxLen, sizeof(slot.data));
    memcpy(slot.data, rfcRxBuf, slot.len);
    String hexOut;
    rfcBytesToHex(slot.data, slot.len, hexOut);
    jxAward(3);
    Serial.print("OK:RFCLOWN_READ BANK=");
    Serial.print(rfcBank);
    Serial.print(" LEN=");
    Serial.print(slot.len);
    Serial.print(" HEX=");
    Serial.println(hexOut);
    return;
  }
  if (cmd.startsWith("RAW:")) {
    String hexIn = cmd.substring(4);
    size_t n = rfcHexToBytes(hexIn, rfcRxBuf, sizeof(rfcRxBuf));
    if (n > 0) {
      rfcRxLen = n;
      RfClownSlot& slot = rfcSlots[rfcBank];
      slot.valid = true;
      slot.freqMhz = rfcFreqMhz;
      strncpy(slot.mod, rfcMod, sizeof(slot.mod));
      slot.mod[sizeof(slot.mod) - 1] = ' ';
      slot.len = n;
      memcpy(slot.data, rfcRxBuf, n);
      Serial.print("OK:RFCLOWN_RAW BANK=");
      Serial.print(rfcBank);
      Serial.print(" LEN=");
      Serial.println(n);
    } else {
      Serial.println("ERR:RFCLOWN_RAW");
    }
    return;
  }
  if (cmd.startsWith("CLONE:")) {
    int idx = cmd.substring(6).toInt();
    if (idx < 0 || idx >= 8) {
      Serial.println("ERR:RFCLOWN_SLOT");
      return;
    }
    rfcTransmitSlot((uint8_t)idx);
    return;
  }
  if (cmd.startsWith("EMUL:")) {
    String arg = cmd.substring(5);
    arg.trim();
    if (arg == "0" || arg.equalsIgnoreCase("OFF") || arg.equalsIgnoreCase("STOP")) {
      rfcEmulating = false;
      #if defined(SUBGHZ_TX_PIN)
      digitalWrite(SUBGHZ_TX_PIN, LOW);
      #endif
      Serial.println("OK:RFCLOWN_EMUL 0");
    } else {
      int idx = arg.toInt();
      if (idx < 0 || idx >= 8) idx = rfcBank;
      rfcEmulating = true;
      rfcTransmitSlot((uint8_t)idx);
      Serial.print("OK:RFCLOWN_EMUL 1 SLOT=");
      Serial.println(idx);
    }
    return;
  }
  Serial.println("ERR:RFCLOWN_UNKNOWN");
}

static bool handleJitterBox6Extensions(const String& line) {
  String s = line;
  s.trim();
  if (s.startsWith("K")) {
    handleCarrierCommand(s.substring(1));
    return true;
  }
  if (s.startsWith("C:")) {
    handleRfClownCommand(s.substring(2));
    return true;
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  duckyInitNativeHid();
  duckySetAuthorized(false);

  // --- Pre-arm every SPI chip-select HIGH before SPI.begin() ---
  // Prevents NRF1/2/3, CC1101, SD and TFT from all fighting over
  // MOSI/MISO/SCK during bus bring-up.
  pinMode(SD_CS,    OUTPUT); digitalWrite(SD_CS,    HIGH);
  pinMode(CC_CSN,   OUTPUT); digitalWrite(CC_CSN,   HIGH);
  pinMode(NRF1_CSN, OUTPUT); digitalWrite(NRF1_CSN, HIGH);
  pinMode(NRF2_CSN, OUTPUT); digitalWrite(NRF2_CSN, HIGH);
  pinMode(NRF3_CSN, OUTPUT); digitalWrite(NRF3_CSN, HIGH);
  pinMode(TFT_CS,   OUTPUT); digitalWrite(TFT_CS,   HIGH);
#if TOUCH_CS >= 0
  pinMode(TOUCH_CS, OUTPUT); digitalWrite(TOUCH_CS, HIGH);
#endif

  // CE pins: outputs, idle LOW (radio off until begin() pulls them up)
  pinMode(NRF1_CE, OUTPUT); digitalWrite(NRF1_CE, LOW);
  pinMode(NRF2_CE, OUTPUT); digitalWrite(NRF2_CE, LOW);
  pinMode(NRF3_CE, OUTPUT); digitalWrite(NRF3_CE, LOW);

#ifdef TFT_BLK
  pinMode(TFT_BLK, OUTPUT);
  digitalWrite(TFT_BLK, HIGH);
#endif
  deselectAllSPI();

  pinMode(CC_GDO0, INPUT);
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setTimeOut(50);
  assertPcfExpanderHigh();

  SPI.begin(RF_SCK, RF_MISO, RF_MOSI);
  deselectAllSPI();
  sdOk = SD.begin(SD_CS, SPI, 4000000);
  deselectAllSPI();

  tftSPI.begin(TFT_SCK, TFT_MISO, TFT_MOSI, -1);
  tft.begin(40000000);
  tft.setRotation(3);

  healthStrip.begin();
  healthStrip.setBrightness(NEO_MAX_BRIGHT);
  lastNeoHealth = -1;
  updateHealthLEDs(computePetHealth4());

  tamaLoad();
  jxLoad();
  initEyes();
  uiNeedsFullRedraw = true;

  ccInitOOK();
  setFreqBytes(0x10, 0xB0, 0x71);
  attachInterrupt(digitalPinToInterrupt(CC_GDO0), rxEdgeISR, CHANGE);

  initRadioUnit(nrf1, nrfOk[0]);
  initRadioUnit(nrf2, nrfOk[1]);
  nrfOk[2] = initNrf3DirectSpi();
  Serial.printf("RADIO_STATUS: NRF1=%s NRF2=%s NRF3=%s\n",
                nrfOk[0] ? "OK" : "FAIL",
                nrfOk[1] ? "OK" : "FAIL",
                nrfOk[2] ? "OK" : "FAIL");
}

void loop() {
  serviceRxQueue();
  serviceTouchEdge();
  servicePcfButton();
  tamaService();
  updateHealthLEDs(computePetHealth4());
  serviceDisplay();
  serviceJitterDucky();

  if (irRxStreaming) {
    uint32_t now = millis();
    bool idleGap = (irEdgeCount > 1) && ((uint32_t)(micros() - irLastEdgeUs) > 25000UL);
    if (irCaptureDone || idleGap) {
      irStopReceive();
      uint16_t n = irEdgeCount;
      if (n > 1) {
        for (uint16_t i = 1; i < n; i++) {
          int32_t val = (i % 2 == 1) ? -(int32_t)irEdgeBuf[i] : (int32_t)irEdgeBuf[i];
          Serial.print(val);
          if (i + 1 < n) Serial.print(',');
        }
        Serial.println();
      }
      irResetCaptureBuffer();
      irRxStreaming = true;
      irRxStartedAt = millis();
      attachInterrupt(digitalPinToInterrupt(IR_RX_PIN), irRxEdgeISR, CHANGE);
    } else if (now - irRxStartedAt > 15000UL) {
      irStopReceive();
    }
  }

  if (triListening) {
    for (uint8_t unit = 0; unit < 3; unit++) {
      if (!nrfOk[unit]) continue;
      uint8_t buf[32] = {0};
      bool gotPacket = false;
      if (unit == 2) {
        uint8_t st = nrf3ReadReg(0x07);
        uint8_t fifo = nrf3ReadReg(0x17);
        bool rxDr = (st & 0x40) != 0;
        bool fifoEmpty = (fifo & 0x01) != 0;
        if (rxDr && !fifoEmpty) {
          deselectAllSPI();
          SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
          digitalWrite(NRF3_CSN, LOW);
          SPI.transfer(0x61);
          for (uint8_t i = 0; i < 32; i++) buf[i] = SPI.transfer(0xFF);
          digitalWrite(NRF3_CSN, HIGH);
          SPI.endTransaction();
          nrf3WriteReg(0x07, 0x40);
          bool allZero = true;
          for (uint8_t i = 0; i < 32; i++) if (buf[i]) { allZero = false; break; }
          if (!allZero) gotPacket = true;
        }
      } else {
        RF24* r = (unit == 1) ? &nrf2 : &nrf1;
        if (r->available()) {
          r->read(buf, sizeof(buf));
          gotPacket = true;
        }
      }
      if (gotPacket) {
        nrfRxCount++;
        triggerEyeRfRx();
        rewardRfSignalActivity(2);
        Serial.printf("NRF_RX_CH%u:", triChannels[unit]);
        for (uint8_t i = 0; i < 32; i++) {
          if (buf[i] < 0x10) Serial.print('0');
          Serial.print(buf[i], HEX);
        }
        Serial.println();
      }
    }
  }

  if (nrfListening && nrfOk[activeNrf]) {
    if (activeNrf == 2) {
      uint8_t st = nrf3ReadReg(0x07);
      uint8_t fifo = nrf3ReadReg(0x17);
      if ((st & 0x40) && !(fifo & 0x01)) {
        uint8_t buf[32] = {0};
        deselectAllSPI();
        SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
        digitalWrite(NRF3_CSN, LOW);
        SPI.transfer(0x61);
        for (int i = 0; i < 32; i++) buf[i] = SPI.transfer(0xFF);
        digitalWrite(NRF3_CSN, HIGH);
        SPI.endTransaction();
        nrf3WriteReg(0x07, 0x40);
        bool allZero = true;
        for (int i = 0; i < 32; i++) if (buf[i]) { allZero = false; break; }
        if (!allZero) {
          nrfRxCount++;
          triggerEyeRfRx();
          rewardRfSignalActivity(2);
          Serial.print("NRF_RX:");
          for (int i = 0; i < 32; i++) {
            if (buf[i] < 0x10) Serial.print('0');
            Serial.print(buf[i], HEX);
          }
          Serial.println();
        }
      }
    } else {
      RF24* r = (activeNrf == 1) ? &nrf2 : &nrf1;
      if (r->available()) {
        uint8_t buf[32] = {0};
        r->read(&buf, sizeof(buf));
        nrfRxCount++;
        triggerEyeRfRx();
        rewardRfSignalActivity(2);
        Serial.print("NRF_RX:");
        for (int i = 0; i < 32; i++) {
          if (buf[i] < 0x10) Serial.print('0');
          Serial.print(buf[i], HEX);
        }
        Serial.println();
      }
    }
  }

  if (!Serial.available()) return;
  uint8_t cmd = Serial.read();

  if (cmd == 'V') {
    Serial.printf("JitterBox v3.0.2-GEN2 (CC1101 + NRF:%d/%d/%d PCF:0x%02X SD:%d IR:%d DUCKY:%d/%d)\n",
                  nrfOk[0], nrfOk[1], nrfOk[2], pcfAddr, sdOk ? 1 : 0, nrf3IrMode,
                  ducky.operatorAuthorized ? 1 : 0, (uint8_t)ducky.state);
  } else if (cmd == 'Q') {
    Serial.printf("RX_STATS:%lu,%lu,0,%lu\n",
                  (unsigned long)rxPulseCount, (unsigned long)rxDropped, (unsigned long)rxPulseCount);
  } else if (cmd == 'U') {
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    uint8_t idx = Serial.read();
    if (idx <= 2) activeNrf = idx;
    Serial.write('K');
  } else if (cmd == 'C') {
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    uint8_t preset = Serial.read();
    if (preset > 3) preset = 0;
    ccListening = false;
    clearRxQueue();
    ccSetPreset(preset);
    Serial.write('K');
  } else if (cmd == 'F') {
    uint32_t waitStart = millis();
    while (Serial.available() < 3 && millis() - waitStart < 100) delay(1);
    if (Serial.available() < 3) { Serial.write('E'); return; }
    uint8_t f2 = Serial.read(), f1 = Serial.read(), f0 = Serial.read();
    ccListening = false;
    setFreqBytes(f2, f1, f0);
    uint32_t word = ((uint32_t)f2 << 16) | ((uint32_t)f1 << 8) | f0;
    lastFreqHz = (uint32_t)((double)word * (26000000.0 / 65536.0));
    Serial.write('K');
  } else if (cmd == 'L') {
    nrfListening = false;
    clearRxQueue();
    lastEdge = micros();
    rxPulseCount = 0;
    ccListening = true;
    ccStrobe(0x34);
    Serial.write('K');
  } else if (cmd == 'X') {
    ccListening = false;
    nrfListening = false;
    triListening = false;
    irStopReceive();
    clearRxQueue();
    ccStrobe(0x36);
    if (nrfOk[0]) nrf1.stopListening();
    if (nrfOk[1]) nrf2.stopListening();
    if (nrfOk[2]) { digitalWrite(NRF3_CE, LOW); nrf3WriteReg(0x00, 0x0A); }
    Serial.write('K');
  } else if (cmd == 'P') {
    ccListening = false;
    nrfListening = false;
    uint32_t waitStart = millis();
    while (Serial.available() < 2 && millis() - waitStart < 1000) delay(1);
    if (Serial.available() < 2) { Serial.write('E'); return; }
    uint16_t count = ((uint16_t)Serial.read() << 8) | Serial.read();
    ccStrobe(0x36);
    pinMode(CC_GDO0, OUTPUT);
    digitalWrite(CC_GDO0, LOW);
    ccStrobe(0x35);
    for (uint16_t i = 0; i < count; i++) {
      waitStart = millis();
      while (Serial.available() < 2 && millis() - waitStart < 1000) delayMicroseconds(10);
      if (Serial.available() < 2) break;
      int16_t pulse = (int16_t)(((uint16_t)Serial.read() << 8) | Serial.read());
      if (pulse > 0) { digitalWrite(CC_GDO0, HIGH); delayMicroseconds(pulse); }
      else if (pulse < 0) { digitalWrite(CC_GDO0, LOW); delayMicroseconds(-pulse); }
    }
    digitalWrite(CC_GDO0, LOW);
    ccStrobe(0x36);
    pinMode(CC_GDO0, INPUT);
    triggerEyeRfTx();
    rewardRfSignalActivity(5);
    Serial.write('D');
  } else if (cmd == 'N' && nrfOk[activeNrf]) {
    ccListening = false;
    nrfListening = false;
    uint32_t waitStart = millis();
    while (Serial.available() < 3 && millis() - waitStart < 500) delay(1);
    if (Serial.available() < 3) { Serial.write('E'); return; }
    uint8_t ch = Serial.read() & 0x7F;
    uint8_t rate = Serial.read();
    uint8_t len = Serial.read();
    if (len > 32) len = 32;
    uint8_t payload[32] = {0};
    for (uint8_t i = 0; i < len; i++) {
      waitStart = millis();
      while (!Serial.available() && millis() - waitStart < 500) delay(1);
      if (!Serial.available()) { Serial.write('E'); return; }
      payload[i] = Serial.read();
    }
    bool txOk = false;
    if (activeNrf == 2) {
      uint8_t rfSetup = (rate == 0) ? 0x06 : ((rate == 2) ? 0x26 : 0x0E);
      digitalWrite(NRF3_CE, LOW);
      nrf3WriteReg(0x00, 0x0A); nrf3WriteReg(0x05, ch); nrf3WriteReg(0x06, rfSetup);
      nrf3Strobe(0xE1); nrf3WriteReg(0x07, 0x70); nrf3WriteBurst(0xA0, payload, 32);
      digitalWrite(NRF3_CE, HIGH);
      delayMicroseconds(15);
      digitalWrite(NRF3_CE, LOW);
      uint32_t txWait = millis();
      uint8_t status = 0;
      while (millis() - txWait < 5) {
        status = nrf3ReadReg(0x07);
        if (status & 0x20) break;
      }
      nrf3WriteReg(0x07, 0x70);
      txOk = (status & 0x20) != 0;
    } else {
      RF24* r = (activeNrf == 1) ? &nrf2 : &nrf1;
      r->stopListening();
      r->setChannel(ch);
      r->setDataRate(rate == 0 ? RF24_1MBPS : (rate == 2 ? RF24_250KBPS : RF24_2MBPS));
      r->openWritingPipe(nrfAddr);
      txOk = r->write(payload, len);
    }
    if (txOk) {
      triggerEyeRfTx();
      rewardRfSignalActivity(5);
    }
    Serial.write(txOk ? 'D' : 'E');
  } else if (cmd == 'S' && nrfOk[activeNrf]) {
    ccListening = false;
    nrfListening = false;
    triListening = false;
    deselectAllSPI();
    Serial.print("NRF_SCAN:");
    if (activeNrf == 2) {
      for (uint8_t ch = 0; ch <= 125; ch++) {
        digitalWrite(NRF3_CE, LOW);
        nrf3WriteReg(0x00, 0x0A);
        nrf3WriteReg(0x05, ch);
        nrf3WriteReg(0x00, 0x0B);
        digitalWrite(NRF3_CE, HIGH);
        delayMicroseconds(170);
        uint8_t rpd = nrf3ReadReg(0x09) & 0x01;
        digitalWrite(NRF3_CE, LOW);
        Serial.print(rpd ? '1' : '0');
      }
      nrf3WriteReg(0x00, 0x0A);
    } else {
      RF24* r = (activeNrf == 1) ? &nrf2 : &nrf1;
      for (uint8_t ch = 0; ch <= 125; ch++) {
        r->setChannel(ch);
        r->startListening();
        delayMicroseconds(150);
        r->stopListening();
        bool hit = r->testRPD();
        Serial.print(hit ? '1' : '0');
      }
    }
    Serial.println();
  } else if (cmd == 'T') {
    uint32_t t0 = millis();
    while (Serial.available() < 3 && millis() - t0 < 150) delay(1);
    if (Serial.available() < 3) { Serial.write('E'); return; }
    for (uint8_t i = 0; i < 3; i++) triChannels[i] = Serial.read() & 0x7F;
    ccListening = false;
    nrfListening = false;
    if (nrfOk[0]) { nrf1.stopListening(); nrf1.setChannel(triChannels[0]); nrf1.openReadingPipe(1, nrfAddr); nrf1.startListening(); }
    if (nrfOk[1]) { nrf2.stopListening(); nrf2.setChannel(triChannels[1]); nrf2.openReadingPipe(1, nrfAddr); nrf2.startListening(); }
    if (nrfOk[2]) {
      digitalWrite(NRF3_CE, LOW);
      nrf3WriteReg(0x00, 0x0A);
      nrf3WriteReg(0x05, triChannels[2]);
      nrf3Strobe(0xE2);
      nrf3WriteReg(0x07, 0x70);
      digitalWrite(NRF3_CE, HIGH);
    }
    triListening = true;
    lastFreqHz = 2400000000UL + (uint32_t)triChannels[0] * 1000000UL;
    Serial.write('K');
  } else if (cmd == 'M' && nrfOk[activeNrf]) {
    triListening = false;
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    nrfChannel = Serial.read() & 0x7F;
    ccListening = false;
    clearRxQueue();
    deselectAllSPI();
    if (activeNrf == 2) {
      digitalWrite(NRF3_CE, LOW);
      nrf3WriteReg(0x00, 0x0A);
      nrf3WriteReg(0x05, nrfChannel);
      nrf3Strobe(0xE2);
      nrf3WriteReg(0x07, 0x70);
      nrf3WriteReg(0x00, 0x0B);
      digitalWrite(NRF3_CE, HIGH);
    } else {
      RF24* r = (activeNrf == 1) ? &nrf2 : &nrf1;
      r->setChannel(nrfChannel);
      r->openReadingPipe(1, nrfAddr);
      r->startListening();
    }
    nrfListening = true;
    lastFreqHz = 2400000000UL + (uint32_t)nrfChannel * 1000000UL;
    nrfRxCount = 0;
    Serial.write('K');
  } else if (cmd == 'R') {
    ccListening = false;
    nrfListening = false;
    triListening = false;
    Serial.print("SELFTEST_NRF1_NRF2:");
    if (!nrfOk[0] || !nrfOk[1]) {
      Serial.println("FAIL:RADIO_NOT_INIT");
      Serial.write('D');
      return;
    }

    const uint8_t testCh = 76;
    uint8_t txPayload[32];
    for (uint8_t i = 0; i < 32; i++) txPayload[i] = i;

    nrf2.stopListening();
    nrf2.setChannel(testCh);
    nrf2.setDataRate(RF24_2MBPS);
    nrf2.openReadingPipe(1, nrfAddr);
    nrf2.flush_rx();
    nrf2.startListening();

    nrf1.stopListening();
    nrf1.setChannel(testCh);
    nrf1.setDataRate(RF24_2MBPS);
    nrf1.openWritingPipe(nrfAddr);
    nrf1.flush_tx();
    delay(2);

    bool sent = nrf1.write(txPayload, 32);

    uint8_t rxBuf[32] = {0};
    bool gotIt = false;
    uint32_t waitStart = millis();
    while (millis() - waitStart < 150) {
      if (nrf2.available()) {
        nrf2.read(rxBuf, 32);
        gotIt = true;
        break;
      }
    }

    bool matches = gotIt;
    if (gotIt) {
      for (uint8_t i = 0; i < 32; i++) {
        if (rxBuf[i] != txPayload[i]) { matches = false; break; }
      }
    }

    if (!sent) Serial.println("FAIL:NRF1_WRITE_TIMEOUT");
    else if (!gotIt) Serial.println("FAIL:NRF2_NO_PACKET");
    else if (!matches) Serial.println("FAIL:PAYLOAD_MISMATCH");
    else Serial.println("PASS");

    nrf1.stopListening();
    nrf2.stopListening();
    Serial.write('D');
  } else if (cmd == 'J') {
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    uint8_t sub = Serial.read();
    if (sub == 'S') printTamaStatusSerial();
    else if (sub == 'A') { onButtonA(); Serial.write('K'); }
    else if (sub == 'B') { onButtonB(); Serial.write('K'); }
    else if (sub == 'C') { onButtonC(); Serial.write('K'); }
    else if (sub == 'P') { onButtonACCombo(); Serial.write('K'); }
    else if (sub == 'I') { triggerEyeIrBlast(); Serial.write('K'); }
    else if (sub == 'T') {
      T.timeScale = (T.timeScale == 1) ? 10 : ((T.timeScale == 10) ? 60 : 1);
      uiHudDirty = true;
      tDirty = true;
      Serial.write('K');
    } else if (sub == 'X') {
      t0 = millis();
      while (!Serial.available() && millis() - t0 < 100) delay(1);
      if (Serial.available() && Serial.read() == 0xA5) {
        tamaFreshEgg();
        tamaSave();
        uiScreen = SCR_MAIN;
        selIcon = ICON_NONE;
        uiNeedsFullRedraw = true;
        Serial.write('K');
      } else {
        Serial.write('E');
      }
    } else {
      Serial.write('E');
    }
  
  } else if (cmd == 'Y') {
    // JitterFood writes "Y<xp>\n" after every scrape.
    // Award XP and ACK. Timeout protects against half-writes.
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    uint32_t xp = 0;
    while (Serial.available() && millis() - t0 < 100) {
      char c = Serial.peek();
      if (c < '0' || c > '9') { Serial.read(); break; }
      xp = xp * 10 + (uint32_t)(Serial.read() - '0');
    }
    if (xp > 5000) xp = 5000;   // sanity clamp
    jxAward(xp);
    Serial.write('K');
} else if (cmd == 'I') {
    triggerEyeIrBlast();
    Serial.write('K');
  } else if (cmd == 'G') {
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    uint8_t mode = Serial.read();

    if (mode == 1 && !nrf3IrMode) {
      ensureIrActive();
    } else if (mode == 0 && nrf3IrMode) {
      irReleaseToNrf3();
    }
    Serial.write('K');
  } else if (cmd == 'D') {
    uint32_t t0 = millis();
    while (!Serial.available() && millis() - t0 < 100) delay(1);
    if (!Serial.available()) { Serial.write('E'); return; }
    uint8_t sub = Serial.read();
    if (sub == 'A') {
      t0 = millis();
      while (!Serial.available() && millis() - t0 < 100) delay(1);
      if (!Serial.available()) { Serial.write('E'); return; }
      duckySetAuthorized(Serial.read() != 0);
      Serial.write('K');
    } else if (sub == 'L') {
      t0 = millis();
      while (!Serial.available() && millis() - t0 < 100) delay(1);
      if (!Serial.available()) { Serial.write('E'); return; }
      ducky.layout = (Serial.read() == 1) ? DUCKY_LAYOUT_DE : DUCKY_LAYOUT_US;
      Serial.write('K');
    } else if (sub == 'P') {
      duckyPause();
      Serial.write('K');
    } else if (sub == 'R') {
      duckyResume();
      Serial.write('K');
    } else if (sub == 'E') {
      duckyEmergencyStop();
      Serial.write('K');
    } else if (sub == 'S' || sub == 'F') {
      char payload[256];
      size_t idx = 0;
      t0 = millis();
      while (idx < sizeof(payload) - 1 && millis() - t0 < 1000) {
        if (Serial.available()) {
          char c = (char)Serial.read();
          if (c == '\n' || c == '\r') break;
          payload[idx++] = c;
          t0 = millis();
        }
      }
      payload[idx] = '\0';
      bool ok = (sub == 'F') ? duckyStartSdScript(payload) : duckyStartInlineScript(payload);
      Serial.write(ok ? 'K' : 'E');
    } else {
      Serial.write('E');
    }
  } else if (cmd == '{') {
    static char irJsonLine[6144];
    size_t jidx = 0;
    irJsonLine[jidx++] = '{';
    uint32_t jt0 = millis();
    while (jidx < sizeof(irJsonLine) - 1 && millis() - jt0 < 500) {
      if (Serial.available()) {
        char c = (char)Serial.read();
        jt0 = millis();
        if (c == '\n') break;
        if (c == '\r') continue;
        irJsonLine[jidx++] = c;
      }
    }
    irJsonLine[jidx] = '\0';

    JsonDocument jdoc;
    if (!deserializeJson(jdoc, irJsonLine)) {
      const char* jcmd = jdoc["cmd"] | "";
      if (strcmp(jcmd, "ir_tx") == 0) {
        ensureIrActive();
        uint32_t freqHz = jdoc["frequency"] | 38000;
        if (freqHz < 1000) freqHz = 38000;
        JsonArray arr = jdoc["data"];
        if (!arr.isNull() && arr.size() > 0) {
          static uint16_t irBuf[IR_RAW_MAX];
          size_t n = 0;
          for (JsonVariant v : arr) {
            if (n >= IR_RAW_MAX) break;
            long raw = v.as<long>();
            long mag = raw < 0 ? -raw : raw;
            if (mag < 1) mag = 1;
            if (mag > 60000) mag = 60000;
            irBuf[n++] = (uint16_t)mag;
          }
          triggerEyeIrBlast();
          irSendRawBursts(irBuf, n, freqHz);
        } else {
          const char* proto = jdoc["protocol"] | "";
          if (proto[0] != '\0') {
            Serial.printf("[IR] '%s' has no RAW data -- named-protocol TX isn't implemented yet. Re-capture this signal as RAW.\n", proto);
          }
        }
      } else if (strcmp(jcmd, "ir_rx_start") == 0) {
        ensureIrActive();
        irStartReceive();
      } else if (strcmp(jcmd, "ir_rx_stop") == 0) {
        irStopReceive();
      }
    }
  }
}
