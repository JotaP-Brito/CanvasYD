#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <TFT_eSPI.h>
#include <WiFi.h>

#include "secrets.h"

namespace {

constexpr uint32_t REFRESH_MS = 60000;
constexpr uint32_t WIFI_RETRY_MS = 15000;
constexpr int MAX_VISIBLE_ITEMS = 4;

constexpr uint16_t COLOR_BG = 0x10A2;
constexpr uint16_t COLOR_PANEL = 0x18E3;
constexpr uint16_t COLOR_PANEL_ALT = 0x2124;
constexpr uint16_t COLOR_TEXT = 0xFFFF;
constexpr uint16_t COLOR_MUTED = 0x9D13;
constexpr uint16_t COLOR_CANVAS = 0xF9C7;
constexpr uint16_t COLOR_PERSONAL = 0x4E5F;
constexpr uint16_t COLOR_URGENT = 0xF9E7;
constexpr uint16_t COLOR_OK = 0x56EA;

TFT_eSPI tft;
uint32_t lastRefresh = 0;
uint32_t lastWifiAttempt = 0;
bool hasRenderedData = false;

String ellipsize(const String &value, size_t maxChars) {
  if (value.length() <= maxChars) return value;
  if (maxChars < 4) return value.substring(0, maxChars);
  return value.substring(0, maxChars - 3) + "...";
}

void drawHeader(bool online) {
  tft.fillRect(0, 0, 320, 36, COLOR_PANEL);
  tft.setTextDatum(ML_DATUM);
  tft.setTextColor(COLOR_TEXT, COLOR_PANEL);
  tft.setTextFont(4);
  tft.drawString("FOCUS", 12, 18);
  tft.fillCircle(251, 18, 4, online ? COLOR_OK : COLOR_URGENT);
  tft.setTextDatum(MR_DATUM);
  tft.setTextFont(2);
  tft.setTextColor(COLOR_MUTED, COLOR_PANEL);
  tft.drawString(online ? "ONLINE" : "OFFLINE", 310, 18);
}

void drawMessage(const String &title, const String &detail, uint16_t accent) {
  tft.fillScreen(COLOR_BG);
  drawHeader(WiFi.status() == WL_CONNECTED);
  tft.fillRoundRect(12, 54, 296, 150, 10, COLOR_PANEL);
  tft.fillRoundRect(12, 54, 5, 150, 3, accent);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COLOR_TEXT, COLOR_PANEL);
  tft.setTextFont(4);
  tft.drawString(title, 29, 72);
  tft.setTextColor(COLOR_MUTED, COLOR_PANEL);
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

uint16_t sourceColor(const String &source, bool urgent) {
  if (urgent) return COLOR_URGENT;
  return source == "canvas" ? COLOR_CANVAS : COLOR_PERSONAL;
}

void drawDashboard(JsonDocument &doc) {
  tft.fillScreen(COLOR_BG);
  drawHeader(true);
  JsonArray items = doc["items"].as<JsonArray>();
  const int canvasCount = doc["counts"]["canvas"] | 0;
  const int personalCount = doc["counts"]["personal"] | 0;
  tft.setTextDatum(ML_DATUM);
  tft.setTextFont(2);
  tft.setTextColor(COLOR_MUTED, COLOR_BG);
  tft.drawString(String(canvasCount) + " school  /  " + String(personalCount) + " personal", 12, 51);

  if (items.size() == 0) {
    tft.fillRoundRect(12, 72, 296, 112, 10, COLOR_PANEL);
    tft.setTextDatum(MC_DATUM);
    tft.setTextFont(4);
    tft.setTextColor(COLOR_OK, COLOR_PANEL);
    tft.drawString("All clear", 160, 112);
    tft.setTextFont(2);
    tft.setTextColor(COLOR_MUTED, COLOR_PANEL);
    tft.drawString("Nothing due soon", 160, 145);
  } else {
    const int visible = min(static_cast<int>(items.size()), MAX_VISIBLE_ITEMS);
    for (int i = 0; i < visible; ++i) {
      JsonObject item = items[i];
      const int y = 67 + i * 39;
      const uint16_t rowColor = (i % 2 == 0) ? COLOR_PANEL : COLOR_PANEL_ALT;
      const String source = item["source"] | "personal";
      const bool urgent = item["urgent"] | false;
      const uint16_t accent = sourceColor(source, urgent);
      tft.fillRoundRect(8, y, 304, 35, 6, rowColor);
      tft.fillRoundRect(8, y, 4, 35, 2, accent);
      tft.setTextDatum(TL_DATUM);
      tft.setTextFont(2);
      tft.setTextColor(COLOR_TEXT, rowColor);
      tft.drawString(ellipsize(String(item["title"] | "Untitled"), 29), 20, y + 4);
      String context = item["context"] | (source == "canvas" ? "Canvas" : "Personal");
      String due = item["due_label"] | "No due date";
      tft.setTextColor(COLOR_MUTED, rowColor);
      tft.drawString(ellipsize(context, 20), 20, y + 20);
      tft.setTextDatum(TR_DATUM);
      tft.setTextColor(accent, rowColor);
      tft.drawString(ellipsize(due, 17), 302, y + 20);
    }
  }
  tft.fillRect(0, 225, 320, 15, COLOR_BG);
  tft.setTextDatum(MR_DATUM);
  tft.setTextFont(1);
  tft.setTextColor(COLOR_MUTED, COLOR_BG);
  const char *updated = doc["updated_label"] | "just now";
  tft.drawString(String("UPDATED ") + updated, 310, 233);
  hasRenderedData = true;
}

bool fetchDashboard() {
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(8000);
  if (!http.begin(client, DASHBOARD_URL)) return false;
  http.addHeader("X-API-Key", DASHBOARD_API_KEY);
  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    Serial.printf("Dashboard request failed: HTTP %d\n", status);
    http.end();
    return false;
  }
  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, http.getStream());
  http.end();
  if (error) {
    Serial.printf("JSON parse failed: %s\n", error.c_str());
    return false;
  }
  drawDashboard(doc);
  return true;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, TFT_BACKLIGHT_ON);
  tft.init();
  tft.setRotation(1);
  tft.fillScreen(COLOR_BG);
  connectWifi();
  if (WiFi.status() == WL_CONNECTED) {
    if (!fetchDashboard()) {
      drawMessage("Bridge unavailable", "Check DASHBOARD_URL, API key, and that the bridge is running.", COLOR_URGENT);
    }
  } else {
    drawMessage("No Wi-Fi", "Check the network name and password in include/secrets.h.", COLOR_URGENT);
  }
  lastRefresh = millis();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiAttempt >= WIFI_RETRY_MS) connectWifi();
  if (millis() - lastRefresh >= REFRESH_MS) {
    lastRefresh = millis();
    if (!fetchDashboard() && !hasRenderedData) {
      drawMessage("Waiting for data", "The last refresh failed. Retrying automatically.", COLOR_URGENT);
    }
  }
  delay(50);
}
