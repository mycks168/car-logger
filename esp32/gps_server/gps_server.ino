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
// 直近のスキャンで各候補(config.hのwifiCredentials[]と同じ並び)が検出できたか。
// 圏外と分かっている候補にまでWIFI_CONNECT_TIMEOUT_MSをフルに使わないための判定に使う
bool wifiCandidateVisible[sizeof(wifiCredentials) / sizeof(wifiCredentials[0])];

// XIAO ESP32C6のオンボードLED(GPIO15, 単色)は実機確認によりactive-HIGH（HIGHで点灯）
#define LED_ON HIGH
#define LED_OFF LOW

// LEDで表示する状態の種類。単色LEDのため色ではなく点滅パターン（間隔・回数）で区別する。
enum LedSignal {
  LED_SIGNAL_NORMAL,          // 正常: WiFi接続 かつ GPSfix取得中（短い点滅 x 接続中AP番号）
  LED_SIGNAL_WIFI_CONNECTING, // WiFi未接続（接続試行中）（長めの点滅 x 試行中AP番号）
  LED_SIGNAL_GPS_NO_FIX,      // GPSはNMEA受信できているがfix無し（2回点滅の固定パターン）
  LED_SIGNAL_ALL_DOWN,        // WiFiもGPSも認識できていない（高速連続点滅）
};

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

// WiFi.status()の戻り値(wl_status_t)をログ表示用の文字列に変換する
const char* wifiStatusToString(wl_status_t status) {
  switch (status) {
    case WL_IDLE_STATUS:     return "WL_IDLE_STATUS";
    case WL_NO_SSID_AVAIL:   return "WL_NO_SSID_AVAIL";
    case WL_SCAN_COMPLETED:  return "WL_SCAN_COMPLETED";
    case WL_CONNECTED:       return "WL_CONNECTED";
    case WL_CONNECT_FAILED:  return "WL_CONNECT_FAILED";
    case WL_CONNECTION_LOST: return "WL_CONNECTION_LOST";
    case WL_DISCONNECTED:    return "WL_DISCONNECTED";
    default:                 return "WL_UNKNOWN";
  }
}

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

bool wifiWasConnected = false;
unsigned long lastRssiCheckMillis = 0;
unsigned long wifiRetryCycleStartMillis = 0;

bool wifiPreRetryScanPending = false;
unsigned long wifiPreRetryScanStartMillis = 0;

// 優先順位リストの現在位置の候補へ接続を開始する（WiFi.begin自体は即座に返る）
void startWifiCandidate() {
  const WifiCredential& cred = wifiCredentials[wifiCredentialIndex];
  Serial.printf(
      "Wi-Fi接続試行 (優先順位 %d/%d): %s\n",
      wifiCredentialIndex + 1, wifiCredentialCount, cred.ssid);
  // 前の接続試行が内部的に進行中のままだと WiFi.begin() の設定変更が
  // "sta is connecting, cannot set config" で拒否され続けるため、先に切断する。
  // 切断処理が完了する前に次のbegin()を呼ぶと同じエラーが再発しうるため、
  // 圏外のAP（切断判定に時間がかかる）が混在していても安全なよう短い待機を挟む。
  WiFi.disconnect(true);
  delay(100);
  WiFi.begin(cred.ssid, cred.password);
  wifiConnectAttemptMillis = millis();
  wifiConnectInProgress = true;
}

// 再接続前スキャンの完了を待ち、完了したら各候補の可視性(wifiCandidateVisible)を
// 更新してから最初の候補への接続を開始する。スキャンが極端に長引く／失敗した場合は
// 可視性不明のまま（＝全候補フルタイムアウト扱い）で通常どおり進める。
void processPreRetryScan() {
  int16_t status = WiFi.scanComplete();

  if (status == WIFI_SCAN_RUNNING) {
    if (millis() - wifiPreRetryScanStartMillis < (unsigned long)WIFI_PRE_RETRY_SCAN_TIMEOUT_MS) {
      return;
    }
    Serial.println("再接続前スキャンがタイムアウトしたため可視性判定なしで再試行します");
    for (int c = 0; c < wifiCredentialCount; c++) wifiCandidateVisible[c] = true;
  } else if (status >= 0) {
    for (int c = 0; c < wifiCredentialCount; c++) {
      wifiCandidateVisible[c] = false;
      for (int i = 0; i < (int)status; i++) {
        if (WiFi.SSID(i) == wifiCredentials[c].ssid) {
          wifiCandidateVisible[c] = true;
          break;
        }
      }
    }
    WiFi.scanDelete();
    // 全候補が不可視ならスキャンの誤判定/隠しSSIDの可能性を考慮し、全候補を可視扱いに戻す
    bool anyVisible = false;
    for (int c = 0; c < wifiCredentialCount; c++) {
      if (wifiCandidateVisible[c]) anyVisible = true;
    }
    if (!anyVisible) {
      for (int c = 0; c < wifiCredentialCount; c++) wifiCandidateVisible[c] = true;
    }
  } else {
    // WIFI_SCAN_FAILED等: 可視性不明として全候補フルタイムアウト扱いにする
    for (int c = 0; c < wifiCredentialCount; c++) wifiCandidateVisible[c] = true;
  }

  wifiPreRetryScanPending = false;
  startWifiCandidate();
}

// Wi-Fi接続状態を監視し、切断中は優先順位順に候補を巡回して再接続を試みる（非ブロッキング）。
// setup()・loop()の両方から毎回呼び出すことで、初回接続と再接続を同じロジックで扱う。
void ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiWasConnected) {
      // 接続が確立した直後は電波強度チェックを1周期分猶予する
      // （つながった瞬間に弱いと判定して即切断→再接続を繰り返すのを防ぐ）
      lastRssiCheckMillis = millis();
      // デバッグ用: setup()以外での再接続はこれまで完了ログが出ていなかったため出力する
      Serial.printf("Wi-Fi再接続完了: SSID=%s RSSI=%ddBm IP=%s\n",
                    WiFi.SSID().c_str(), WiFi.RSSI(), WiFi.localIP().toString().c_str());
    }
    wifiConnectInProgress = false;
    wifiWasConnected = true;

    // 接続中のAPの電波強度を定期的に確認し、閾値を下回ったら明示的に切断する。
    // 自宅Wi-Fiの圏外際で弱い電波を掴んだまま居座り続けるのを防ぐのが目的。
    // 切断すると ensureWifiConnected() は次回呼び出し時に優先順位トップから
    // 再試行するため、より強く受信できる候補（自宅Wi-Fiや車載ルーター）へ移れる。
    if (millis() - lastRssiCheckMillis >= (unsigned long)WIFI_RSSI_CHECK_INTERVAL_SECONDS * 1000UL) {
      lastRssiCheckMillis = millis();
      int32_t rssi = WiFi.RSSI();
      if (rssi <= WIFI_RSSI_DISCONNECT_THRESHOLD_DBM) {
        Serial.printf(
            "Wi-Fi電波強度が閾値を下回りました (RSSI=%ddBm <= %ddBm) 切断して再試行します\n",
            rssi, WIFI_RSSI_DISCONNECT_THRESHOLD_DBM);
        WiFi.disconnect(true);
      }
    }
    return;
  }

  if (wifiPreRetryScanPending) {
    processPreRetryScan();
    return;
  }

  if (!wifiConnectInProgress) {
    // 直前まで接続できていた（＝今まさに切断された）場合は、優先順位トップから
    // 試し直す。車載APの電源断（エンジンOFF等）で切れた際に、帰宅後は自宅Wi-Fiを
    // 最優先で再試行してほしいため、切断前の候補から続きを試すのではなく毎回先頭に戻す。
    if (wifiWasConnected) {
      wifiCredentialIndex = 0;
      wifiWasConnected = false;
    }
    // どの候補にも繋がらない状態がいつから続いているかを記録する
    // （WiFiドライバ再初期化の要否判定に使う。下記参照）
    wifiRetryCycleStartMillis = millis();
    // 候補を試す前に一度スキャンし、実際に電波が届いている候補を把握する。
    // 圏外と分かっている候補にまでWIFI_CONNECT_TIMEOUT_MS(30秒)をフルに
    // 使ってしまうのを防ぐ（例: 自宅にいる間はCarapの圏外判定に毎周30秒を浪費していた）。
    WiFi.scanNetworks(true);
    wifiPreRetryScanPending = true;
    wifiPreRetryScanStartMillis = millis();
    return;
  }

  // 接続試行中: タイムアウトしたら次の優先順位の候補へ移る。
  // 直近のスキャンで圏外と分かっている候補は短いタイムアウトで即座に見切る。
  unsigned long timeoutMs = wifiCandidateVisible[wifiCredentialIndex]
      ? (unsigned long)WIFI_CONNECT_TIMEOUT_MS
      : (unsigned long)WIFI_CONNECT_TIMEOUT_INVISIBLE_MS;
  if (millis() - wifiConnectAttemptMillis >= timeoutMs) {
    // 接続できなかった原因の切り分け用（例: WL_NO_SSID_AVAILならSSID発見不可、
    // WL_CONNECT_FAILEDなら認証失敗、WL_IDLE_STATUSのまま進まない場合はモデムスリープ等
    // begin()自体が実質開始できていない状態を疑う）
    Serial.printf("  タイムアウト時のWiFi.status()=%s\n", wifiStatusToString(WiFi.status()));
    // 実機検証で、圏外の候補が混ざった状態でWiFi.disconnect(true)+WiFi.begin()を
    // 高頻度に繰り返すと、ESP-IDFのWiFiドライバ内部状態が壊れ（シリアルに
    // "wifi:timeout when WiFi un-init" 等のエラーが出る）、以後は電波が届く候補に
    // すら二度と接続できなくなる不具合を確認した。長時間どの候補にも繋がらない場合は
    // ドライバごと作り直すことで、車で電波の悪い区間を走った後に自動復帰できなくなる
    // 事態を防ぐ。
    if (millis() - wifiRetryCycleStartMillis >= (unsigned long)WIFI_DRIVER_RESET_AFTER_MS) {
      Serial.println("Wi-Fi接続不能が続いているためWiFiドライバを再初期化します");
      WiFi.mode(WIFI_OFF);
      delay(200);
      WiFi.mode(WIFI_STA);
      WiFi.setSleep(false);
      wifiCredentialIndex = 0;
      wifiRetryCycleStartMillis = millis();
      startWifiCandidate();
      return;
    }
    wifiCredentialIndex = (wifiCredentialIndex + 1) % wifiCredentialCount;
    startWifiCandidate();
  }
}

// 現在の状態から表示すべきLEDシグナルの一覧を組み立てる。複数該当する場合は順番に巡回表示する。
//
// WiFi軸（OK/接続試行中）とGPS軸（fix取得中/NMEA受信中だがfix無し/NMEA未受信=認識不可）は
// 互いに独立に評価する。以前は「GPS認識不可」を「WiFiも未接続」の場合に限定していたため、
// 「WiFiは繋がっているがGPSが全く応答しない」状態がどの条件にも該当せずLEDが消灯したままになる
// 抜け穴があった。GPS未認識はWiFi状態によらず独立して表示する。
int buildLedSignals(LedSignal* out) {
  bool wifiOk = (WiFi.status() == WL_CONNECTED);
  bool gpsSerialOk = gpsState.serialActive;
  bool gpsFixOk = gpsState.hasFix;

  if (wifiOk && gpsFixOk) {
    out[0] = LED_SIGNAL_NORMAL;
    return 1;
  }

  int count = 0;
  if (!wifiOk) {
    out[count++] = LED_SIGNAL_WIFI_CONNECTING;
  }
  if (!gpsSerialOk) {
    out[count++] = LED_SIGNAL_ALL_DOWN;  // GPSがNMEAを一切受信できていない（WiFi状態によらず）
  } else if (!gpsFixOk) {
    out[count++] = LED_SIGNAL_GPS_NO_FIX;
  }
  return count;
}

// オンボードLED(単色)を非ブロッキングで点滅させ、現在の状態を表現する。
// 表示すべきシグナルが複数ある場合は、1つずつ表示してから次のシグナルへ順番に切り替える。
void updateStatusLed() {
  LedSignal signals[3];
  int signalCount = buildLedSignals(signals);

  static int activeIndex = 0;
  static unsigned long patternStartMillis = 0;

  if (signalCount == 0) {
    digitalWrite(LED_BUILTIN, LED_OFF);
    return;
  }
  if (activeIndex >= signalCount) {
    activeIndex = 0;
  }

  int blinkCount;
  unsigned long onMs, offMs, pauseMs;
  switch (signals[activeIndex]) {
    case LED_SIGNAL_NORMAL:
      blinkCount = wifiCredentialIndex + 1;
      onMs = 150; offMs = 150; pauseMs = 1500;
      break;
    case LED_SIGNAL_WIFI_CONNECTING:
      blinkCount = wifiCredentialIndex + 1;
      onMs = 500; offMs = 300; pauseMs = 1500;
      break;
    case LED_SIGNAL_GPS_NO_FIX:
      blinkCount = 2;
      onMs = 250; offMs = 150; pauseMs = 1000;
      break;
    case LED_SIGNAL_ALL_DOWN:
    default:
      blinkCount = 10;
      onMs = 80; offMs = 80; pauseMs = 1000;
      break;
  }

  unsigned long cycleLen = onMs + offMs;
  unsigned long blinkPhaseLen = cycleLen * blinkCount;
  unsigned long elapsed = millis() - patternStartMillis;

  if (elapsed < blinkPhaseLen) {
    unsigned long posInCycle = elapsed % cycleLen;
    digitalWrite(LED_BUILTIN, posInCycle < onMs ? LED_ON : LED_OFF);
  } else if (elapsed < blinkPhaseLen + pauseMs) {
    digitalWrite(LED_BUILTIN, LED_OFF);
  } else {
    patternStartMillis = millis();
    activeIndex = (activeIndex + 1) % signalCount;
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
    Serial.printf("WiFiスキャン完了: %d APを検出\n", wifiApCount);
    // デバッグ用: 現在接続中のSSIDと、登録済み候補(config.h)が実際にどの電波強度で
    // 見えているかを出力する（scanDelete()の前に、SSID名で突き合わせる）
    Serial.printf("  接続中: SSID=%s RSSI=%ddBm\n", WiFi.SSID().c_str(), WiFi.RSSI());
    for (int c = 0; c < wifiCredentialCount; c++) {
      bool found = false;
      for (int i = 0; i < (int)status; i++) {
        if (WiFi.SSID(i) == wifiCredentials[c].ssid) {
          Serial.printf("  候補%d(%s): 検出 RSSI=%ddBm\n", c + 1, wifiCredentials[c].ssid, WiFi.RSSI(i));
          found = true;
          break;
        }
      }
      wifiCandidateVisible[c] = found;
      if (!found) {
        Serial.printf("  候補%d(%s): 圏外\n", c + 1, wifiCredentials[c].ssid);
      }
    }
    WiFi.scanDelete();
    time(&wifiScannedAt);
  }

  // 接続処理中（初回接続・再接続とも）にスキャンを開始すると、STAの接続処理と
  // 競合して接続がタイムアウトしやすくなることを実機で確認したため、
  // 接続が確立している間のみスキャンする。
  bool intervalElapsed =
      (millis() - lastWifiScanStartMillis) >= (unsigned long)WIFI_SCAN_INTERVAL_SECONDS * 1000UL;
  if (WiFi.status() == WL_CONNECTED && (lastWifiScanStartMillis == 0 || intervalElapsed)) {
    WiFi.scanNetworks(true /* async */);
    lastWifiScanStartMillis = millis();
  }
}

// 現在の位置情報をJSONに組み立て、外部Relayサーバへ1回Pushする。
// GPS fixが無い場合も「ESP32・WiFi・Relayへの到達性は生きている」ことを伝えるハートビートとして
// 必ずPushする（座標欄はnullで送る）。盗難保険用途では「機器自体が生きているか」の判別が重要なため。
void pushLocationToRelay() {
  if (RELAY_USE_INSECURE_TLS) {
    relayClient.setInsecure();
  } else {
    relayClient.setCACert(RELAY_ROOT_CA);
  }

  // タイムアウト未設定だとRelayに到達できない場合にloop()が長時間ブロックされ、
  // LED表示も止まってしまうため、接続・応答とも数秒で打ち切るようにする
  relayClient.setTimeout(RELAY_PUSH_TIMEOUT_MS / 1000);

  HTTPClient https;
  https.setConnectTimeout(RELAY_PUSH_TIMEOUT_MS);
  https.setTimeout(RELAY_PUSH_TIMEOUT_MS);
  String url = String("https://") + RELAY_HOST + ":" + String(RELAY_PORT) + RELAY_PUSH_PATH;
  if (!https.begin(relayClient, url)) {
    Serial.println("Relay Push: HTTPS接続の初期化に失敗しました");
    return;
  }
  https.addHeader("Content-Type", "application/json");
  https.addHeader("Authorization", String("Bearer ") + RELAY_PUSH_AUTH_TOKEN);

  JsonDocument doc;
  if (isnan(gpsState.lat)) doc["lat"] = nullptr; else doc["lat"] = gpsState.lat;
  if (isnan(gpsState.lon)) doc["lon"] = nullptr; else doc["lon"] = gpsState.lon;
  if (isnan(gpsState.alt)) doc["alt"] = nullptr; else doc["alt"] = gpsState.alt;
  if (isnan(gpsState.speedKmh)) {
    doc["speed_kmh"] = nullptr;
  } else {
    doc["speed_kmh"] = round(gpsState.speedKmh * 10) / 10.0;
  }
  doc["has_fix"] = gpsState.hasFix;
  // GPSモジュール自体がNMEAを送ってきているか（アンテナ未接続・故障等の切り分け用）
  doc["gps_serial_active"] = gpsState.serialActive;
  time_t recordedAtSource = gpsState.lastFixAt != 0 ? gpsState.lastFixAt : time(nullptr);
  doc["recorded_at"] = formatIso8601(recordedAtSource);

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

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LED_OFF);

  gpsSerial.begin(GPS_BAUD_RATE, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.println("GPS UART初期化完了");

  // 初回スキャン完了前は可視性不明のため、いったん全候補を可視（フルタイムアウト）扱いにする
  for (int c = 0; c < wifiCredentialCount; c++) wifiCandidateVisible[c] = true;

  WiFi.mode(WIFI_STA);
  // WiFiモデムスリープが有効だとbegin()直後の認証フレーム送信が遅延し、
  // 特定候補への接続試行がWL_IDLE_STATUSのまま進まなくなる不具合を実機で確認したため無効化する
  WiFi.setSleep(false);
  while (WiFi.status() != WL_CONNECTED) {
    ensureWifiConnected();
    readGpsSerial();
    updateStatusLed();
    delay(10);
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
  updateStatusLed();
  server.handleClient();
}
