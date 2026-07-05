/*
 * GPSサーバ (XIAO ESP32C6版)
 *
 * ラズパイ版 raspberry/gps_server/main.py のGPS取得・WiFi AP取得・API提供機能を
 * XIAO ESP32C6 + ATGM336H(UART接続GPSモジュール)へ移植したもの。
 * /gps エンドポイントのJSONスキーマはラズパイ版と互換に維持している
 * （MPU-6050によるPush通知・USBカメラ撮影・DS18B20温度センサー機能は対象外）。
 *
 * 必要ライブラリ: TinyGPSPlus, ArduinoJson (v7以降)
 * ボード: XIAO_ESP32C6 (esp32 by Espressif Systems, v3.0.0以降)
 *
 * 配線 (ATGM336H -> XIAO ESP32C6):
 *   VCC  -> 3V3
 *   GND  -> GND
 *   TXD  -> D7 (RX)
 *   RXD  -> D6 (TX)
 */

#include <math.h>
#include <time.h>

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <TinyGPSPlus.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

struct WifiCredential {
  const char* ssid;
  const char* password;
};

#include "config.h"  // wifiCredentials[] 等を定義

const int wifiCredentialCount = sizeof(wifiCredentials) / sizeof(wifiCredentials[0]);

TinyGPSPlus gps;
HardwareSerial gpsSerial(1);
WebServer server(API_PORT);
WiFiClientSecure relayClient;

struct GpsState {
  double lat = NAN;
  double lon = NAN;
  double alt = NAN;
  double speedKmh = NAN;
  bool hasFix = false;
  time_t lastFixAt = 0;             // 0 = 一度もfixしていない
  unsigned long lastFixMillis = 0;
  bool serialActive = false;        // ラズパイ版のgpsd_connected相当（NMEA受信中か）
  unsigned long lastSentenceMillis = 0;
};

struct WifiApEntry {
  String macAddress;
  int32_t signalStrength = 0;
};

GpsState gpsState;
WifiApEntry wifiAps[MAX_WIFI_APS];
int wifiApCount = 0;
time_t wifiScannedAt = 0;
unsigned long lastWifiScanStartMillis = 0;
unsigned int lastPassedChecksumCount = 0;

// time_t(UTC)を "YYYY-MM-DDTHH:MM:SSZ" 形式のISO8601文字列に変換する
String formatIso8601(time_t t) {
  struct tm tmStruct;
  gmtime_r(&t, &tmStruct);
  char buf[25];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tmStruct);
  return String(buf);
}

int wifiCredentialIndex = 0;
bool wifiConnectInProgress = false;
unsigned long wifiConnectAttemptMillis = 0;

// 優先順位リストの現在位置の候補へ接続を開始する（非ブロッキング。WiFi.begin自体は即座に返る）
void startWifiCandidate() {
  const WifiCredential& cred = wifiCredentials[wifiCredentialIndex];
  Serial.printf(
      "Wi-Fi接続試行 (優先順位 %d/%d): %s\n",
      wifiCredentialIndex + 1, wifiCredentialCount, cred.ssid);
  WiFi.begin(cred.ssid, cred.password);
  wifiConnectAttemptMillis = millis();
  wifiConnectInProgress = true;
}

// Wi-Fi接続状態を監視し、切断中は優先順位順に候補を巡回して再接続を試みる（非ブロッキング）。
// setup()・loop()の両方から毎回呼び出すことで、初回接続と再接続を同じロジックで扱う。
void ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiConnectInProgress = false;
    return;
  }

  if (!wifiConnectInProgress) {
    startWifiCandidate();
    return;
  }

  // 接続試行中: タイムアウトしたら次の優先順位の候補へ移る
  if (millis() - wifiConnectAttemptMillis >= WIFI_CONNECT_TIMEOUT_MS) {
    wifiCredentialIndex = (wifiCredentialIndex + 1) % wifiCredentialCount;
    startWifiCandidate();
  }
}

// mDNSを開始する。同一LAN内から http://<MDNS_HOSTNAME>.local:API_PORT/ でアクセス可能にする
void startMdns() {
  if (MDNS.begin(MDNS_HOSTNAME)) {
    MDNS.addService("http", "tcp", API_PORT);
    Serial.printf("mDNS開始: http://%s.local:%d/\n", MDNS_HOSTNAME, API_PORT);
  } else {
    Serial.println("mDNS開始に失敗しました");
  }
}

// NTPで時刻同期する（last_fix_at等のタイムスタンプ生成に使用）
void syncTime() {
  configTime(NTP_GMT_OFFSET_SEC, NTP_DAYLIGHT_OFFSET_SEC, NTP_SERVER);
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo, 10000)) {
    Serial.println("NTP時刻同期に失敗しました（後続のタイムスタンプがずれる可能性があります）");
    return;
  }
  Serial.println("NTP時刻同期完了");
}

// GPS UARTから受信可能なバイトをすべて読み、gpsStateを更新する
void readGpsSerial() {
  while (gpsSerial.available() > 0) {
    gps.encode(gpsSerial.read());
  }

  // NMEA文を継続受信できているか（ラズパイ版のgpsd_connectedに相当する項目）
  unsigned int passed = gps.passedChecksum();
  if (passed != lastPassedChecksumCount) {
    lastPassedChecksumCount = passed;
    gpsState.lastSentenceMillis = millis();
  }
  gpsState.serialActive = (millis() - gpsState.lastSentenceMillis) < GPS_SERIAL_TIMEOUT_MS;

  if (gps.location.isUpdated() && gps.location.isValid()) {
    gpsState.lat = gps.location.lat();
    gpsState.lon = gps.location.lng();
    gpsState.alt = gps.altitude.isValid() ? gps.altitude.meters() : NAN;
    gpsState.speedKmh = gps.speed.isValid() ? gps.speed.kmph() : NAN;
    gpsState.hasFix = true;
    gpsState.lastFixMillis = millis();
    time_t now;
    time(&now);
    gpsState.lastFixAt = now;
  } else if (gpsState.hasFix && (millis() - gpsState.lastFixMillis) > GPS_FIX_TIMEOUT_MS) {
    // 一定時間新しい位置更新がなければfix喪失とみなす（座標自体はキャッシュとして保持する）
    gpsState.hasFix = false;
  }
}

// 非同期WiFiスキャンを開始・完了判定し、結果をwifiAps配列へ反映する
void updateWifiScan() {
  int16_t status = WiFi.scanComplete();

  if (status == WIFI_SCAN_RUNNING) {
    return;
  }

  if (status >= 0) {
    wifiApCount = min((int)status, MAX_WIFI_APS);
    for (int i = 0; i < wifiApCount; i++) {
      wifiAps[i].macAddress = WiFi.BSSIDstr(i);
      wifiAps[i].macAddress.toUpperCase();
      wifiAps[i].signalStrength = WiFi.RSSI(i);
    }
    WiFi.scanDelete();
    time(&wifiScannedAt);
    Serial.printf("WiFiスキャン完了: %d APを検出\n", wifiApCount);
  }

  bool intervalElapsed =
      (millis() - lastWifiScanStartMillis) >= (unsigned long)WIFI_SCAN_INTERVAL_SECONDS * 1000UL;
  if (lastWifiScanStartMillis == 0 || intervalElapsed) {
    WiFi.scanNetworks(true /* async */);
    lastWifiScanStartMillis = millis();
  }
}

// 現在の位置情報をJSONに組み立て、外部Relayサーバへ1回Pushする
void pushLocationToRelay() {
  if (isnan(gpsState.lat) || isnan(gpsState.lon)) {
    return;  // 一度もfixしていない場合はPushする内容がない
  }

  if (RELAY_USE_INSECURE_TLS) {
    relayClient.setInsecure();
  } else {
    relayClient.setCACert(RELAY_ROOT_CA);
  }

  HTTPClient https;
  String url = String("https://") + RELAY_HOST + ":" + String(RELAY_PORT) + RELAY_PUSH_PATH;
  if (!https.begin(relayClient, url)) {
    Serial.println("Relay Push: HTTPS接続の初期化に失敗しました");
    return;
  }
  https.addHeader("Content-Type", "application/json");
  https.addHeader("Authorization", String("Bearer ") + RELAY_PUSH_AUTH_TOKEN);

  JsonDocument doc;
  doc["lat"] = gpsState.lat;
  doc["lon"] = gpsState.lon;
  if (isnan(gpsState.alt)) doc["alt"] = nullptr; else doc["alt"] = gpsState.alt;
  if (isnan(gpsState.speedKmh)) {
    doc["speed_kmh"] = nullptr;
  } else {
    doc["speed_kmh"] = round(gpsState.speedKmh * 10) / 10.0;
  }
  doc["has_fix"] = gpsState.hasFix;
  doc["recorded_at"] = formatIso8601(gpsState.lastFixAt);

  JsonArray aps = doc["wifi_aps"].to<JsonArray>();
  for (int i = 0; i < wifiApCount; i++) {
    JsonObject ap = aps.add<JsonObject>();
    ap["macAddress"] = wifiAps[i].macAddress;
    ap["signalStrength"] = wifiAps[i].signalStrength;
  }
  if (wifiScannedAt != 0) {
    doc["wifi_scanned_at"] = formatIso8601(wifiScannedAt);
  } else {
    doc["wifi_scanned_at"] = nullptr;
  }

  String body;
  serializeJson(doc, body);

  int statusCode = https.POST(body);
  if (statusCode > 0) {
    Serial.printf("Relay Push完了: HTTP %d\n", statusCode);
  } else {
    Serial.printf("Relay Push失敗: %s\n", https.errorToString(statusCode).c_str());
  }
  https.end();
}

// RELAY_PUSH_INTERVAL_SECONDSごとにpushLocationToRelay()を呼び出す（非ブロッキングのタイマー判定）
void updateRelayPush() {
  if (!RELAY_PUSH_ENABLED || WiFi.status() != WL_CONNECTED) {
    return;
  }

  static unsigned long lastPushMillis = 0;
  if (lastPushMillis != 0 &&
      millis() - lastPushMillis < (unsigned long)RELAY_PUSH_INTERVAL_SECONDS * 1000UL) {
    return;
  }
  lastPushMillis = millis();
  pushLocationToRelay();
}

// GET /gps ハンドラ。現在のGPS状態とWiFiスキャン結果をJSONで返す
void handleGps() {
  time_t now;
  time(&now);

  double lat = gpsState.lat;
  double lon = gpsState.lon;
  double cacheAgeSeconds = -1;

  if (gpsState.lastFixAt != 0) {
    cacheAgeSeconds = difftime(now, gpsState.lastFixAt);
    if (cacheAgeSeconds > CACHE_MAX_AGE_SECONDS) {
      Serial.println("GPSキャッシュが期限切れのため無効化します");
      lat = NAN;
      lon = NAN;
    }
  }

  JsonDocument doc;
  doc["has_fix"] = gpsState.hasFix;
  doc["gpsd_connected"] = gpsState.serialActive;

  if (isnan(lat)) doc["lat"] = nullptr; else doc["lat"] = lat;
  if (isnan(lon)) doc["lon"] = nullptr; else doc["lon"] = lon;
  if (isnan(gpsState.alt)) doc["alt"] = nullptr; else doc["alt"] = gpsState.alt;
  if (isnan(gpsState.speedKmh)) {
    doc["speed_kmh"] = nullptr;
  } else {
    doc["speed_kmh"] = round(gpsState.speedKmh * 10) / 10.0;
  }
  if (gpsState.lastFixAt != 0) {
    doc["last_fix_at"] = formatIso8601(gpsState.lastFixAt);
  } else {
    doc["last_fix_at"] = nullptr;
  }

  if (cacheAgeSeconds >= 0) {
    doc["cache_age_seconds"] = round(cacheAgeSeconds * 10) / 10.0;
  } else {
    doc["cache_age_seconds"] = nullptr;
  }

  JsonArray aps = doc["wifi_aps"].to<JsonArray>();
  for (int i = 0; i < wifiApCount; i++) {
    JsonObject ap = aps.add<JsonObject>();
    ap["macAddress"] = wifiAps[i].macAddress;
    ap["signalStrength"] = wifiAps[i].signalStrength;
  }
  if (wifiScannedAt != 0) {
    doc["wifi_scanned_at"] = formatIso8601(wifiScannedAt);
  } else {
    doc["wifi_scanned_at"] = nullptr;
  }

  String output;
  serializeJson(doc, output);
  server.send(200, "application/json", output);
}

// GET /health ハンドラ
void handleHealth() {
  JsonDocument doc;
  doc["status"] = "ok";
  String output;
  serializeJson(doc, output);
  server.send(200, "application/json", output);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("=== GPS Server (XIAO ESP32C6) 起動 ===");

  gpsSerial.begin(GPS_BAUD_RATE, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.println("GPS UART初期化完了");

  WiFi.mode(WIFI_STA);
  while (WiFi.status() != WL_CONNECTED) {
    ensureWifiConnected();
    delay(100);
  }
  Serial.print("Wi-Fi接続完了: SSID=");
  Serial.print(WiFi.SSID());
  Serial.print(" IP=");
  Serial.println(WiFi.localIP());

  startMdns();
  syncTime();

  server.on("/gps", handleGps);
  server.on("/health", handleHealth);
  server.begin();
  Serial.printf("APIサーバ起動: ポート%d\n", API_PORT);
}

void loop() {
  ensureWifiConnected();
  readGpsSerial();
  updateWifiScan();
  updateRelayPush();
  server.handleClient();
}
