#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "azure_root_ca.h"
#include "secrets.h"

namespace {

constexpr uint32_t REFRESH_MS = 15UL * 60UL * 1000UL;
constexpr uint32_t WIFI_RETRY_MS = 15000;
constexpr uint32_t TOUCH_REDRAW_MS = 35;

constexpr int SCREEN_WIDTH = 320;
constexpr int SCREEN_HEIGHT = 240;
constexpr int HEADER_HEIGHT = 34;
constexpr int SUMMARY_HEIGHT = 22;
constexpr int LIST_TOP = HEADER_HEIGHT + SUMMARY_HEIGHT;
constexpr int LIST_HEIGHT = SCREEN_HEIGHT - LIST_TOP;
constexpr int ROW_HEIGHT = 48;
constexpr int MAX_ITEMS = 20;

// Original ESP32-2432S028R XPT2046 touch wiring.
constexpr int TOUCH_IRQ = 36;
constexpr int TOUCH_MOSI = 32;
constexpr int TOUCH_MISO = 39;
constexpr int TOUCH_CLK = 25;
constexpr int TOUCH_CS = 33;
constexpr int TOUCH_X_MIN = 200;
constexpr int TOUCH_X_MAX = 3700;
constexpr int TOUCH_Y_MIN = 240;
constexpr int TOUCH_Y_MAX = 3800;
constexpr int TOUCH_PRESSURE_MIN = 180;

constexpr uint16_t COLOR_BG = 0x0861;
constexpr uint16_t COLOR_HEADER = 0x10C3;
constexpr uint16_t COLOR_SUMMARY = 0x18E4;
constexpr uint16_t COLOR_ROW = 0x10A3;
constexpr uint16_t COLOR_ROW_ALT = 0x18E4;
constexpr uint16_t COLOR_TEXT = 0xFFFF;
constexpr uint16_t COLOR_MUTED = 0x9D13;
constexpr uint16_t COLOR_DIVIDER = 0x2946;
constexpr uint16_t COLOR_URGENT = 0xFB2D;
constexpr uint16_t COLOR_PERSONAL = 0xFC10;
constexpr uint16_t COLOR_OK = 0x4ED2;

constexpr uint16_t rgb565(uint8_t red, uint8_t green, uint8_t blue) {
  return ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3);
}

// One high-contrast color per possible visible course. Colors are allocated
// without replacement, so separate courses cannot collide.
constexpr uint16_t COURSE_COLORS[] = {
    rgb565(30, 136, 229),  rgb565(255, 179, 0),  rgb565(142, 36, 170),
    rgb565(67, 160, 71),   rgb565(216, 27, 96),  rgb565(0, 172, 193),
    rgb565(244, 81, 30),   rgb565(57, 73, 171),  rgb565(124, 179, 66),
    rgb565(171, 71, 188),  rgb565(0, 137, 123),  rgb565(229, 57, 53),
    rgb565(3, 155, 229),   rgb565(251, 140, 0),  rgb565(94, 53, 177),
    rgb565(0, 166, 90),    rgb565(236, 64, 122), rgb565(38, 166, 154),
    rgb565(249, 168, 37),  rgb565(84, 110, 122),
};
constexpr int COURSE_COLOR_COUNT = sizeof(COURSE_COLORS) / sizeof(COURSE_COLORS[0]);

struct DashboardItem {
  String title;
  String context;
  String due;
  String source;
  bool urgent = false;
};

TFT_eSPI tft;

DashboardItem dashboardItems[MAX_ITEMS];
String courseNames[MAX_ITEMS];
uint16_t assignedCourseColors[MAX_ITEMS] = {};
int courseCount = 0;
int itemCount = 0;
int canvasCount = 0;
int personalCount = 0;
int scrollOffset = 0;
String updatedLabel = "just now";

uint32_t lastRefresh = 0;
uint32_t lastWifiAttempt = 0;
uint32_t lastTouchRedraw = 0;
bool hasRenderedData = false;
bool touchActive = false;
int touchStartY = 0;
int scrollStart = 0;

String ellipsize(const String &value, size_t maxChars) {
  if (value.length() <= maxChars) return value;
  if (maxChars < 4) return value.substring(0, maxChars);
  return value.substring(0, maxChars - 3) + "...";
}

uint16_t blend565(uint16_t foreground, uint16_t background, uint8_t amount) {
  const uint8_t inverse = 255 - amount;
  const uint8_t fgR = (foreground >> 11) & 0x1F;
  const uint8_t fgG = (foreground >> 5) & 0x3F;
  const uint8_t fgB = foreground & 0x1F;
  const uint8_t bgR = (background >> 11) & 0x1F;
  const uint8_t bgG = (background >> 5) & 0x3F;
  const uint8_t bgB = background & 0x1F;
  const uint8_t r = (fgR * amount + bgR * inverse) / 255;
  const uint8_t g = (fgG * amount + bgG * inverse) / 255;
  const uint8_t b = (fgB * amount + bgB * inverse) / 255;
  return (r << 11) | (g << 5) | b;
}

uint32_t contextHash(const String &value) {
  uint32_t hash = 2166136261u;
  for (size_t i = 0; i < value.length(); ++i) {
    hash ^= static_cast<uint8_t>(value[i]);
    hash *= 16777619u;
  }
  return hash;
}

String normalizedCourseName(const String &value) {
  String normalized = value;
  normalized.trim();
  normalized.toUpperCase();
  return normalized;
}

void buildCourseColorMap() {
  courseCount = 0;

  for (int itemIndex = 0; itemIndex < itemCount; ++itemIndex) {
    const DashboardItem &item = dashboardItems[itemIndex];
    if (item.source == "personal") continue;

    const String course = normalizedCourseName(item.context);
    bool alreadyAdded = false;
    for (int courseIndex = 0; courseIndex < courseCount; ++courseIndex) {
      if (courseNames[courseIndex] == course) {
        alreadyAdded = true;
        break;
      }
    }
    if (!alreadyAdded && courseCount < MAX_ITEMS) courseNames[courseCount++] = course;
  }

  // Sorting makes allocation stable even if Canvas changes assignment order.
  for (int left = 0; left < courseCount - 1; ++left) {
    for (int right = left + 1; right < courseCount; ++right) {
      if (courseNames[right].compareTo(courseNames[left]) < 0) {
        const String swap = courseNames[left];
        courseNames[left] = courseNames[right];
        courseNames[right] = swap;
      }
    }
  }

  bool colorUsed[COURSE_COLOR_COUNT] = {};
  for (int courseIndex = 0; courseIndex < courseCount; ++courseIndex) {
    const int preferred = contextHash(courseNames[courseIndex]) % COURSE_COLOR_COUNT;
    for (int probe = 0; probe < COURSE_COLOR_COUNT; ++probe) {
      const int colorIndex = (preferred + probe) % COURSE_COLOR_COUNT;
      if (colorUsed[colorIndex]) continue;
      assignedCourseColors[courseIndex] = COURSE_COLORS[colorIndex];
      colorUsed[colorIndex] = true;
      break;
    }
  }
}

uint16_t itemColor(const DashboardItem &item) {
  if (item.source == "personal") return COLOR_PERSONAL;
  const String course = normalizedCourseName(item.context);
  for (int courseIndex = 0; courseIndex < courseCount; ++courseIndex) {
    if (courseNames[courseIndex] == course) return assignedCourseColors[courseIndex];
  }
  return COLOR_MUTED;
}

int maxScrollOffset() {
  return max(0, itemCount * ROW_HEIGHT - LIST_HEIGHT);
}

void clampScrollOffset() {
  scrollOffset = constrain(scrollOffset, 0, maxScrollOffset());
}

void drawHeader(bool online) {
  tft.fillRect(0, 0, SCREEN_WIDTH, HEADER_HEIGHT, COLOR_HEADER);
  tft.setTextDatum(ML_DATUM);
  tft.setTextFont(4);
  tft.setTextColor(COLOR_TEXT, COLOR_HEADER);
  tft.drawString("FOCUS", 9, HEADER_HEIGHT / 2);

  tft.setTextFont(2);
  tft.setTextColor(COLOR_MUTED, COLOR_HEADER);
  tft.drawString(String(canvasCount + personalCount) + " DUE", 103, HEADER_HEIGHT / 2);

  tft.fillCircle(250, HEADER_HEIGHT / 2, 4, online ? COLOR_OK : COLOR_URGENT);
  tft.setTextDatum(MR_DATUM);
  tft.setTextFont(1);
  tft.setTextColor(COLOR_MUTED, COLOR_HEADER);
  tft.drawString(online ? "ONLINE" : "OFFLINE", 310, 12);
  tft.drawString(ellipsize(updatedLabel, 12), 310, 24);
}

void drawSummary() {
  tft.fillRect(0, HEADER_HEIGHT, SCREEN_WIDTH, SUMMARY_HEIGHT, COLOR_SUMMARY);
  tft.drawFastHLine(0, LIST_TOP - 1, SCREEN_WIDTH, COLOR_DIVIDER);
  tft.setTextDatum(ML_DATUM);
  tft.setTextFont(1);
  tft.setTextColor(COLOR_MUTED, COLOR_SUMMARY);
  tft.drawString(String(canvasCount) + " SCHOOL  /  " + String(personalCount) + " PERSONAL", 9,
                 HEADER_HEIGHT + SUMMARY_HEIGHT / 2);
  if (maxScrollOffset() > 0) {
    tft.setTextDatum(MR_DATUM);
    tft.setTextColor(COLOR_TEXT, COLOR_SUMMARY);
    tft.drawString("SWIPE", 310, HEADER_HEIGHT + SUMMARY_HEIGHT / 2);
  }
}

void drawEmptyList() {
  tft.fillRect(0, 0, SCREEN_WIDTH, LIST_HEIGHT, COLOR_BG);
  tft.setTextDatum(MC_DATUM);
  tft.setTextFont(4);
  tft.setTextColor(COLOR_OK, COLOR_BG);
  tft.drawString("All clear", SCREEN_WIDTH / 2, 66);
  tft.setTextFont(2);
  tft.setTextColor(COLOR_MUTED, COLOR_BG);
  tft.drawString("Nothing due soon", SCREEN_WIDTH / 2, 96);
}

void drawScrollBar() {
  const int contentHeight = itemCount * ROW_HEIGHT;
  if (contentHeight <= LIST_HEIGHT) return;

  constexpr int trackX = SCREEN_WIDTH - 4;
  constexpr int trackTop = 5;
  constexpr int trackHeight = LIST_HEIGHT - 10;
  const int thumbHeight = max(24, trackHeight * LIST_HEIGHT / contentHeight);
  const int thumbTravel = trackHeight - thumbHeight;
  const int thumbY = trackTop + thumbTravel * scrollOffset / maxScrollOffset();
  tft.fillRoundRect(trackX, trackTop, 3, trackHeight, 2, COLOR_DIVIDER);
  tft.fillRoundRect(trackX, thumbY, 3, thumbHeight, 2, COLOR_MUTED);
}

void drawAssignmentList() {
  clampScrollOffset();
  tft.setViewport(0, LIST_TOP, SCREEN_WIDTH, LIST_HEIGHT);
  tft.fillRect(0, 0, SCREEN_WIDTH, LIST_HEIGHT, COLOR_BG);

  if (itemCount == 0) {
    drawEmptyList();
    tft.resetViewport();
    return;
  }

  const int firstItem = scrollOffset / ROW_HEIGHT;
  const int firstY = -(scrollOffset % ROW_HEIGHT);
  for (int index = firstItem, y = firstY; index < itemCount && y < LIST_HEIGHT;
       ++index, y += ROW_HEIGHT) {
    const DashboardItem &item = dashboardItems[index];
    const uint16_t accent = itemColor(item);
    const uint16_t base = index % 2 == 0 ? COLOR_ROW : COLOR_ROW_ALT;
    const uint16_t rowColor = blend565(accent, base, 76);

    tft.fillRect(0, y, SCREEN_WIDTH, ROW_HEIGHT - 1, rowColor);
    tft.fillRect(0, y, 8, ROW_HEIGHT - 1, accent);
    tft.drawFastHLine(8, y + ROW_HEIGHT - 1, SCREEN_WIDTH - 8, COLOR_DIVIDER);

    tft.setTextDatum(TL_DATUM);
    tft.setTextFont(1);
    tft.setTextColor(accent, rowColor);
    String course = item.context;
    course.toUpperCase();
    tft.drawString(ellipsize(course, 25), 12, y + 5);

    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(item.urgent ? COLOR_URGENT : COLOR_MUTED, rowColor);
    tft.drawString(ellipsize(item.due, 18), 310, y + 5);

    tft.setTextDatum(TL_DATUM);
    tft.setTextFont(2);
    tft.setTextColor(COLOR_TEXT, rowColor);
    tft.drawString(ellipsize(item.title, 36), 12, y + 20);
  }

  drawScrollBar();
  tft.resetViewport();
}

void drawDashboard() {
  tft.fillScreen(COLOR_BG);
  drawHeader(WiFi.status() == WL_CONNECTED);
  drawSummary();
  drawAssignmentList();
  hasRenderedData = true;
}

void drawMessage(const String &title, const String &detail, uint16_t accent) {
  tft.fillScreen(COLOR_BG);
  drawHeader(WiFi.status() == WL_CONNECTED);
  tft.fillRoundRect(12, 54, 296, 150, 10, COLOR_SUMMARY);
  tft.fillRoundRect(12, 54, 5, 150, 3, accent);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COLOR_TEXT, COLOR_SUMMARY);
  tft.setTextFont(4);
  tft.drawString(title, 29, 72);
  tft.setTextColor(COLOR_MUTED, COLOR_SUMMARY);
  tft.setTextFont(2);
  tft.setTextWrap(true, false);
  tft.drawString(detail, 29, 112);
  tft.setTextWrap(false, false);
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  lastWifiAttempt = millis();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  drawMessage("Connecting", "Joining Wi-Fi...", COLOR_PERSONAL);
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 12000) delay(250);
}

void loadDashboard(JsonDocument &doc) {
  JsonArray items = doc["items"].as<JsonArray>();
  canvasCount = doc["counts"]["canvas"] | 0;
  personalCount = doc["counts"]["personal"] | 0;
  updatedLabel = String(doc["updated_label"] | "just now");
  itemCount = min(static_cast<int>(items.size()), MAX_ITEMS);

  for (int index = 0; index < itemCount; ++index) {
    JsonObject item = items[index];
    DashboardItem &stored = dashboardItems[index];
    stored.source = String(item["source"] | "personal");
    stored.title = String(item["title"] | "Untitled");
    stored.context = String(item["context"] |
                            (stored.source == "canvas" ? "Canvas" : "Personal"));
    stored.due = String(item["due_label"] | "No due date");
    stored.urgent = item["urgent"] | false;
  }
  buildCourseColorMap();
  clampScrollOffset();
}

bool fetchDashboard() {
  if (WiFi.status() != WL_CONNECTED) return false;

  WiFiClientSecure secureClient;
  HTTPClient http;
  http.setConnectTimeout(10000);
  http.setTimeout(20000);

  const String dashboardUrl = DASHBOARD_URL;
  bool started = false;
  if (dashboardUrl.startsWith("https://")) {
#ifdef DASHBOARD_ROOT_CA
    secureClient.setCACert(DASHBOARD_ROOT_CA);
#else
    secureClient.setCACert(AZURE_ROOT_CA);
#endif
    started = http.begin(secureClient, dashboardUrl);
  } else {
#if defined(ALLOW_INSECURE_HTTP) && ALLOW_INSECURE_HTTP
    WiFiClient plainClient;
    started = http.begin(plainClient, dashboardUrl);
#else
    Serial.println("Refusing non-HTTPS dashboard URL");
    return false;
#endif
  }
  if (!started) return false;
  http.addHeader("Accept", "application/json");
  http.addHeader("Accept-Encoding", "identity");
  http.addHeader("X-API-Key", DASHBOARD_API_KEY);
  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    Serial.printf("Dashboard request failed: HTTP %d\n", status);
    http.end();
    return false;
  }

  // Azure Functions can send the response with chunked transfer encoding.
  // HTTPClient::getString() decodes the transfer before ArduinoJson sees it;
  // parsing getStream() directly can expose the chunk framing as invalid JSON.
  const String payload = http.getString();
  http.end();

  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    Serial.printf("JSON parse failed: %s\n", error.c_str());
    Serial.printf("Response length: %u bytes\n", payload.length());
    return false;
  }

  loadDashboard(doc);
  drawDashboard();
  return true;
}

uint8_t touchTransfer8(uint8_t output) {
  uint8_t input = 0;
  for (int bit = 0; bit < 8; ++bit) {
    digitalWrite(TOUCH_MOSI, (output & 0x80) ? HIGH : LOW);
    output <<= 1;
    digitalWrite(TOUCH_CLK, HIGH);
    delayMicroseconds(1);
    input = (input << 1) | digitalRead(TOUCH_MISO);
    digitalWrite(TOUCH_CLK, LOW);
    delayMicroseconds(1);
  }
  return input;
}

uint16_t touchTransfer16(uint16_t output) {
  const uint16_t high = touchTransfer8(output >> 8);
  return (high << 8) | touchTransfer8(output & 0xFF);
}

int16_t closestPairAverage(int16_t a, int16_t b, int16_t c) {
  const int ab = abs(a - b);
  const int ac = abs(a - c);
  const int bc = abs(b - c);
  if (ab <= ac && ab <= bc) return (a + b) / 2;
  if (ac <= ab && ac <= bc) return (a + c) / 2;
  return (b + c) / 2;
}

bool readRawTouch(int &rawX, int &rawY) {
  if (digitalRead(TOUCH_IRQ) != LOW) return false;

  int16_t samples[6] = {};
  digitalWrite(TOUCH_CS, LOW);
  touchTransfer8(0xB1);  // Z1
  const int z1 = touchTransfer16(0xC1) >> 3;
  int pressure = z1 + 4095;
  const int z2 = touchTransfer16(0x91) >> 3;
  pressure -= z2;

  if (pressure >= TOUCH_PRESSURE_MIN) {
    touchTransfer16(0x91);  // discard the first noisy coordinate
    samples[0] = touchTransfer16(0xD1) >> 3;
    samples[1] = touchTransfer16(0x91) >> 3;
    samples[2] = touchTransfer16(0xD1) >> 3;
    samples[3] = touchTransfer16(0x91) >> 3;
  }
  samples[4] = touchTransfer16(0xD0) >> 3;
  samples[5] = touchTransfer16(0x00) >> 3;
  digitalWrite(TOUCH_CS, HIGH);

  if (pressure < TOUCH_PRESSURE_MIN) return false;
  rawX = closestPairAverage(samples[0], samples[2], samples[4]);
  rawY = closestPairAverage(samples[1], samples[3], samples[5]);
  return true;
}

bool readTouch(int &screenX, int &screenY) {
  int rawX = 0;
  int rawY = 0;
  if (!readRawTouch(rawX, rawY)) return false;
  screenX = constrain(map(rawX, TOUCH_X_MIN, TOUCH_X_MAX, 0, SCREEN_WIDTH - 1), 0,
                      SCREEN_WIDTH - 1);
  screenY = constrain(map(rawY, TOUCH_Y_MIN, TOUCH_Y_MAX, 0, SCREEN_HEIGHT - 1), 0,
                      SCREEN_HEIGHT - 1);
  return true;
}

void handleTouch() {
  int x = 0;
  int y = 0;
  const bool pressed = readTouch(x, y);

  if (!pressed) {
    touchActive = false;
    return;
  }

  if (!touchActive) {
    if (y < LIST_TOP || maxScrollOffset() == 0) return;
    touchActive = true;
    touchStartY = y;
    scrollStart = scrollOffset;
    return;
  }

  const int nextOffset = constrain(scrollStart + touchStartY - y, 0, maxScrollOffset());
  if (nextOffset == scrollOffset || millis() - lastTouchRedraw < TOUCH_REDRAW_MS) return;
  scrollOffset = nextOffset;
  lastTouchRedraw = millis();
  drawAssignmentList();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, TFT_BACKLIGHT_ON);

  tft.init();
  tft.setRotation(1);
  tft.fillScreen(COLOR_BG);

  pinMode(TOUCH_IRQ, INPUT);
  pinMode(TOUCH_MISO, INPUT);
  pinMode(TOUCH_MOSI, OUTPUT);
  pinMode(TOUCH_CLK, OUTPUT);
  pinMode(TOUCH_CS, OUTPUT);
  digitalWrite(TOUCH_CLK, LOW);
  digitalWrite(TOUCH_CS, HIGH);

  connectWifi();
  if (WiFi.status() == WL_CONNECTED) {
    if (!fetchDashboard()) {
      drawMessage("Bridge unavailable", "Check the dashboard URL, API key, and bridge.",
                  COLOR_URGENT);
    }
  } else {
    drawMessage("No Wi-Fi", "Check the network name and password in include/secrets.h.",
                COLOR_URGENT);
  }
  lastRefresh = millis();
}

void loop() {
  handleTouch();

  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiAttempt >= WIFI_RETRY_MS) {
    connectWifi();
  }
  if (millis() - lastRefresh >= REFRESH_MS) {
    lastRefresh = millis();
    if (!fetchDashboard() && !hasRenderedData) {
      drawMessage("Waiting for data", "The last refresh failed. Retrying automatically.",
                  COLOR_URGENT);
    }
  }
  delay(12);
}
