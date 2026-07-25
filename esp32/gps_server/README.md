# GPSサーバ (XIAO ESP32C6版)

ラズパイ版 (`raspberry/gps_server/main.py`) のGPS取得・WiFi AP取得・API提供機能を、
Seeed Studio XIAO ESP32C6 + ATGM336H(UART接続GPSモジュール)へ移植したものです。

MPU-6050による加速度Push、USBカメラ撮影、DS18B20温度センサーの機能はこのファームウェアの対象外です。

## 配線 (ATGM336H → XIAO ESP32C6)

| ATGM336H | XIAO ESP32C6 |
|----------|--------------|
| VCC      | 3V3          |
| GND      | GND          |
| TXD      | D7 (RX)      |
| RXD      | D6 (TX)      |

**電源について**: このATGM336Hモジュールは「Power supply: 3.3V-5V」対応品です。基板上のピン定義に
「DC5V」と書かれていても、USB Type-C給電（5V系統）を使う場合の代表値の表記であり、3.3V給電も
公式サポート範囲内です。XIAOの3V3ピンから給電することで、モジュール内部の実装（レギュレータ有無）に
関わらずTXD/RXDの信号レベルが3.3Vになることが保証され、ESP32C6のGPIO（5V非耐圧）を安全に保てます。
**5Vピンからの給電は行わないでください**（TXD/RXDが5Vロジックになる可能性があり、GPIOを破損するリスクがあります）。

## Wi-Fi接続の優先順位

`config.h` の `wifiCredentials[]` に複数のSSID/パスワードを優先順位順（先頭が最優先）で登録できます。
起動時・切断時は先頭の候補から順に接続を試み（1候補あたり `WIFI_CONNECT_TIMEOUT_MS` で次へ移る）、
接続できたものをそのまま使い続けます。

**注意**: 一度接続すると、繋がっている間は優先順位の高い候補が電波圏内に入ってきても
自動では乗り換えません（乗り換え処理を入れると定期的な再接続が発生し、その間API/GPSが
途切れるトレードオフがあるため、現時点では非対応です）。

ただし、**切断を検知した場合は必ず優先順位トップの候補から再試行します**（直前まで
繋がっていた候補から続きを試すのではない）。これにより、例えば車載ルーターがエンジンOFFで
電源を失って切断された後、帰宅すれば自宅Wi-Fiが最優先で再接続されます。

また、接続中も `WIFI_RSSI_CHECK_INTERVAL_SECONDS`（デフォルト15秒）ごとに接続中APの電波強度
(RSSI)を確認し、`WIFI_RSSI_DISCONNECT_THRESHOLD_DBM`（デフォルト-80dBm）以下まで弱くなったら
明示的に切断します。切断後は上記の通り優先順位トップから再試行するため、自宅から離れて
自宅Wi-Fiの電波が実用に耐えないレベルまで弱くなった場合に、いつまでも掴み続けず
自動的に車載ルーター等の次候補へ切り替わります。

### 接続タイムアウトについて（PMF/WPA3対応APでの遅延）

実機検証で、一部のAP（家庭用ルーター/メッシュエクステンダーなど、PMF(Protected Management
Frames)やWPA3に対応した比較的新しい機種で確認）は、電波強度が十分でも接続完了まで
20秒以上かかることがあると判明した。接続試行中は`WiFi.status()`が`WL_IDLE_STATUS`
（＝見かけ上何も進行していないように見える状態）のまま推移し、8秒のような短い
タイムアウトでは実際にはあと数秒〜十数秒で繋がるはずの接続を毎回打ち切ってしまっていた。
この現象はモバイルルーターや車載Wi-Fiのような素のWPA2機器では発生せず、対象APのPMF/WPA3
まわりのハンドシェイクの再試行に時間がかかっていることが原因と推測している
（ESP32側での根本解決は困難なため、`WIFI_CONNECT_TIMEOUT_MS`を余裕を持った値にすることで
対応している）。

そのため `WIFI_CONNECT_TIMEOUT_MS` はデフォルト30秒に設定している。候補数が多いほど
1周（全候補を試し終えるまで）にかかる時間が伸びる（例: 4候補×30秒=最大120秒）ため、
接続に時間がかかるAPを増やす場合はこの点を考慮すること。

### WiFiドライバの自動再初期化（既知の不具合への対策）

実機検証で、圏外の候補が混ざった状態（例: 車載ルーターの電源が入っていない、自宅から
離れている等）で `WiFi.disconnect(true)` + `WiFi.begin()` の接続試行サイクルを繰り返して
いると、ESP-IDFのWiFiドライバが内部的に壊れ（シリアルに `wifi:timeout when WiFi un-init`
等のエラーが出る）、以後は電波が届く候補にすら二度と自動接続できなくなる不具合を確認した。
これが走行中に「GPS情報（Relayへのpush）が取れたり取れなかったりする」症状の原因になって
いる可能性がある。

対策として、どの候補にも `WIFI_DRIVER_RESET_AFTER_MS`（デフォルト300秒）以上接続できない
状態が続いた場合、`WiFi.mode(WIFI_OFF)` → `WIFI_STA` でWiFiドライバごと作り直してから
優先順位トップの候補で再試行する。1周に最大120秒程度かかるようになったため、1周だけで
誤って再初期化しないよう2周分以上の余裕を持たせている。これにより、電波の悪い区間を
長く走った後でも自動復帰できるようにしている。

## mDNS (同一LAN内からのアクセス)

起動時に `MDNS_HOSTNAME`（デフォルト: `sage`）で mDNS を開始するため、同一LAN内の
avahi/Bonjour対応機器（ラズパイOSは標準搭載）からは `http://sage.local:8080/gps` で
IPアドレスを知らなくてもアクセスできます。車載ラズパイから直接ポーリングする場合はこちらを使います。

## 外部Relayへの位置情報Push

XIAO ESP32C6はTailscaleに参加できず、車のWi-Fi環境（自宅/モバイルルーター等）も変わるため、
内部サーバ (`server/gps_monitor`, Tailscale内) から直接Pullすることができません。
そこで `RELAY_PUSH_INTERVAL_SECONDS`（デフォルト30秒）ごとに、`relay/`（別途デプロイする公開サーバ）
の `POST /push/location` へ位置情報をHTTPS Pushします。詳細は `relay/README.md` を参照してください。

`config.h` で以下を設定します。

- `RELAY_HOST` / `RELAY_PORT` / `RELAY_PUSH_PATH`: Relayの接続先
- `RELAY_PUSH_AUTH_TOKEN`: Relayの `PUSH_AUTH_TOKEN` と一致させるBearerトークン
- `RELAY_USE_INSECURE_TLS`: `true`だとTLS証明書検証をスキップ（動作確認用。Bearerトークンで
  書き込み内容の認可自体は保護されるが、通信相手のなりすまし(MITM)は防げないため、本番では
  `false` にして `RELAY_ROOT_CA` にRelayのCA証明書(PEM)を設定することを推奨）

## 状態表示LED

ディスプレイもCLIも無い車載環境で状況を判断できるよう、オンボードLED（`LED_BUILTIN`, GPIO15,
単色）を点滅パターンで状態表示に使う。色は出せないため、点滅の長さ・回数・リズムで区別する。
複数の異常が同時に該当する場合は、該当するパターンを順番に繰り返し表示する。

| 状態 | パターン |
|---|---|
| 正常（WiFi接続 かつ GPS fix取得中） | 短い点滅(150ms)を接続中AP番号の回数 → 1.5秒消灯 → 繰り返し |
| WiFi接続試行中（未接続） | 長めの点滅(500ms)を試行中AP番号の回数 → 1.5秒消灯 → 繰り返し |
| GPSはNMEA受信できているがfix無し | 250ms点滅を2回 → 1秒消灯 → 繰り返し |
| GPSがNMEAを一切受信できていない（WiFi状態によらず） | 80ms間隔の高速点滅を10回 → 1秒消灯 → 繰り返し |

「AP番号」は `config.h` の `wifiCredentials[]` 配列内での順番（1始まり）。例えば正常時に2回点滅なら
2番目に登録したWi-Fiに接続していることを示す。

GPS未認識（NMEA未受信）はWiFiの接続状態に関わらず独立して判定するため、「WiFi接続試行中」と
同時に該当する場合は両方のパターンを順番に繰り返し表示する。

**極性について**: XIAO ESP32C6のオンボードLED(GPIO15)は実機確認の結果 active-HIGH（`HIGH`で点灯）。
`gps_server.ino` 内の `LED_ON`/`LED_OFF` で定義している。

## 必要なライブラリ (Arduino IDE ライブラリマネージャで導入)

- **TinyGPSPlus** (Mikal Hart) — NMEA文の解析
- **ArduinoJson** (Benoit Blanchon) — v7.0以降を使用（`JsonDocument`のAPIを使用しているため）
- `WiFi.h` / `WebServer.h` / `ESPmDNS.h` / `HTTPClient.h` / `WiFiClientSecure.h` — ESP32 Arduinoコアに標準同梱

## ボードマネージャ設定

1. Arduino IDE の「基本設定」→「追加のボードマネージャのURL」に以下を追加:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
2. ボードマネージャで `esp32 by Espressif Systems` をインストール（v3.0.0以降。XIAO ESP32C6サポートに必要）
3. ボード選択: `XIAO_ESP32C6`

## セットアップ手順

1. `config.h.example` を `config.h` にコピーし、Wi-FiのSSID/パスワード等を入力する
   （`config.h` は `.gitignore` 対象のためコミットされません）
2. 上記のライブラリ・ボードをインストール
3. `gps_server.ino` を書き込む
4. シリアルモニタ（115200bps）で起動ログを確認し、Wi-Fi接続完了後に表示されるIPアドレスを控える
5. 同一ネットワークの別端末から `http://<XIAO のIP>:8080/gps` にアクセスして動作確認する

## APIレスポンス例 (`GET /gps`)

ラズパイ版と同一のJSONスキーマです。サーバ側 (`server/gps_monitor`) のポーリング先URLを
XIAO ESP32C6のIPアドレスに変更するだけで、そのまま動作します。

```json
{
  "has_fix": true,
  "gpsd_connected": true,
  "lat": 35.681236,
  "lon": 139.767125,
  "alt": 12.3,
  "speed_kmh": 4.5,
  "last_fix_at": "2026-07-05T12:00:00Z",
  "cache_age_seconds": 0.2,
  "wifi_aps": [
    {"macAddress": "AA:BB:CC:DD:EE:FF", "signalStrength": -55}
  ],
  "wifi_scanned_at": "2026-07-05T11:58:10Z"
}
```

`gpsd_connected` はラズパイ版ではgpsdへの接続有無を表していましたが、
ESP32版ではgpsdが存在しないため「直近10秒以内に有効なNMEA文を受信できているか」に意味を読み替えています
（`config.h` の `GPS_SERIAL_TIMEOUT_MS` で調整可能）。

## 制限事項

- **テストコードなし**: `setup()`/`loop()` は実GPS・実Wi-Fiハードウェアに依存するため、
  Arduino IDE構成では自動テストの対象外としています。JSON生成・時刻フォーマット等は
  関数として分離していますが、ホスト環境での単体テストは導入していません。
- **WiFiスキャン中の瞬断**: `WiFi.scanNetworks()` はSTA接続中でも非同期実行できますが、
  スキャン中に数百ms〜数秒の通信遅延が発生する場合があります（ラズパイ版と同様、
  `WIFI_SCAN_INTERVAL_SECONDS`＝300秒間隔でのみ発生）。
- **fix喪失判定**: `GPS_FIX_TIMEOUT_MS`（デフォルト5秒）新しい位置更新がないと `has_fix=false` に
  なりますが、座標自体は `CACHE_MAX_AGE_SECONDS` が経過するまでキャッシュとして返却され続けます
  （ラズパイ版と同じ挙動）。
