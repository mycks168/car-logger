# car-logger

車両盗難防止用GPSロガー＋車内温度モニタリングシステム。ラズパイ3にGPSモジュールとDS18B20温度センサー（最大6台）を接続し、サーバからTailscale経由でデータを定期取得する。GPS情報が取れなくなった場合はSlackへ最終既知位置を地図リンク付きで通知する。

## システム構成

```
[GPS module] + [DS18B20 x6 (1-wire)]
     │ UART/USB + /sys/bus/w1/devices/
[Raspberry Pi 3]
     │ iPhone USBテザリング (インターネット)
     │ Tailscale VPN
[サーバ]
     ├─ GPS監視 → Slack Incoming Webhook
     ├─ GPS履歴 → data/gps_history.db
     ├─ 温度履歴 → data/temp_history.db
     └─ WebUI (GPS軌跡 + 温度グラフ)
```

| コンポーネント | 役割 |
|---|---|
| `raspberry/` | ラズパイ上で動作するGPS + 温度 APIサーバ |
| `server/gps_monitor/` | GPS監視・Slack通知 + GPS履歴をSQLiteへ保存 |
| `server/temp_monitor/` | 温度定期取得・SQLiteへ保存 |
| `server/gps_web/` | GPS軌跡・温度グラフ表示WebUI |

## 通知ロジック

```
GPS取得成功
  └→ 最終既知位置を更新（通知しない）
       アラート中だった場合は復帰通知を送る

GPS取得失敗（ラズパイオフライン or GPS補足不可）
  └→ 最終既知位置が存在する場合:
       ├─ 前回通知から NOTIFY_MOVE_THRESHOLD_M 以上移動 → 即時通知
       ├─ 前回通知から NOTIFY_COOLDOWN_SECONDS 経過   → 再通知
       └─ それ以外（トンネル内など）                   → 通知しない
```

## セットアップ

### 前提条件

| 環境 | 必要なもの |
|---|---|
| ラズパイ | `gpsd` インストール済み、GPS モジュール接続済み |
| ラズパイ | Tailscale 設定済み、`uv` インストール済み |
| サーバ | Tailscale 設定済み、`uv` インストール済み |
| Slack | Incoming Webhook URL 取得済み |

### ラズパイ側セットアップ

```bash
# gpsdのインストール（未インストールの場合）
sudo apt install gpsd gpsd-clients

# GPSデバイスを確認（例: /dev/ttyACM0）
ls /dev/tty*

# gpsdの設定
sudo nano /etc/default/gpsd
```

`/etc/default/gpsd` の設定例:
```
DEVICES="/dev/ttyACM0"
GPSD_OPTIONS="-n"
START_DAEMON="true"
USBAUTO="true"
```

```bash
# gpsdを起動
sudo systemctl enable gpsd
sudo systemctl start gpsd

# 動作確認（GPSのデータが表示されればOK）
cgps -s

# リポジトリをクローン
git clone <このリポジトリ> ~/car-logger
cd ~/car-logger/raspberry

# 環境変数ファイルを作成
cp ../.env.example .env
# .env を編集（ラズパイ側の設定のみ必要に応じて変更）

# 依存パッケージをインストールして起動
uv sync
uv run python -m gps_server.main

# 動作確認
curl http://localhost:8080/gps
curl http://localhost:8080/health
```

#### systemdサービスとして登録（自動起動）

```bash
sudo cp gps-server.service /etc/systemd/system/
# ユーザ名や配置パスが異なる場合はサービスファイルを編集
sudo nano /etc/systemd/system/gps-server.service

sudo systemctl daemon-reload
sudo systemctl enable gps-server
sudo systemctl start gps-server
sudo systemctl status gps-server
```

### サーバ側セットアップ

```bash
git clone <このリポジトリ> /opt/car-logger
cd /opt/car-logger/server

# 環境変数ファイルを作成
cp ../.env.example .env
nano .env  # 下記の必須項目を設定
```

**`.env` の必須設定項目:**

| 変数名 | 説明 | 例 |
|---|---|---|
| `RASPI_GPS_URL` | ラズパイのTailscale IP + ポート | `http://100.x.x.x:8080/gps` |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL | `https://hooks.slack.com/...` |

```bash
# 依存パッケージをインストール
uv sync

# GPS監視プロセスを起動
uv run python -m gps_monitor.main

# 温度監視プロセスを起動（別ターミナルで）
uv run python -m temp_monitor.main

# 地図・温度グラフWebUIを起動（別ターミナルで）
uv run python -m gps_web.main
# ブラウザで http://localhost:8081 を開く
#   / → GPS軌跡
#   /temperature → 温度グラフ
```

#### センサーマッピングの設定

`server/sensor_map.json` を作成してセンサーIDと場所名を対応付ける。

```bash
cp sensor_map.json.example sensor_map.json
nano sensor_map.json
```

センサーIDの確認:
```bash
# ラズパイ上で実行
ls /sys/bus/w1/devices/28-*
# または
curl http://100.x.x.x:8080/temperatures | python3 -m json.tool
```

#### systemdサービスとして登録（自動起動）

```bash
sudo cp gps-monitor.service  /etc/systemd/system/
sudo cp temp-monitor.service /etc/systemd/system/
sudo cp gps-web.service      /etc/systemd/system/
# ユーザ名や配置パスが異なる場合はサービスファイルを編集

sudo systemctl daemon-reload
sudo systemctl enable gps-monitor temp-monitor gps-web
sudo systemctl start  gps-monitor temp-monitor gps-web
sudo systemctl status gps-monitor temp-monitor gps-web
```

## APIリファレンス（ラズパイ側）

### `GET /gps`

現在のGPS状態を返す。

**レスポンス例（GPS補足中）:**
```json
{
  "has_fix": true,
  "gpsd_connected": true,
  "lat": 35.681236,
  "lon": 139.767125,
  "alt": 10.5,
  "speed_kmh": 0.0,
  "last_fix_at": "2026-05-12T10:00:00+00:00",
  "cache_age_seconds": 2.1
}
```

**レスポンス例（GPS補足不可・最終既知位置を返す）:**
```json
{
  "has_fix": false,
  "gpsd_connected": true,
  "lat": 35.681236,
  "lon": 139.767125,
  "alt": null,
  "speed_kmh": null,
  "last_fix_at": "2026-05-12T09:55:00+00:00",
  "cache_age_seconds": 300.0
}
```

| フィールド | 説明 |
|---|---|
| `has_fix` | 現在GPS衛星を補足中かどうか |
| `gpsd_connected` | gpsdデーモンと接続できているか |
| `lat`, `lon` | 最終既知位置（`has_fix=false` の場合もキャッシュを返す） |
| `last_fix_at` | 最後にGPSを補足した時刻 |
| `cache_age_seconds` | `last_fix_at` からの経過秒数 |

### `GET /health`

疎通確認用。常に `{"status": "ok"}` を返す。

## 設定一覧

### サーバ側（`server/.env`）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `RASPI_BASE_URL` | （必須） | ラズパイのベースURL（温度取得に使用） |
| `RASPI_GPS_URL` | （必須） | ラズパイの GPS API URL |
| `SLACK_WEBHOOK_URL` | （必須） | Slack Incoming Webhook URL |
| `POLL_INTERVAL_SECONDS` | `60` | GPS ポーリング間隔（秒） |
| `TEMP_POLL_INTERVAL_SECONDS` | `300` | 温度ポーリング間隔（秒） |
| `NOTIFY_COOLDOWN_SECONDS` | `1800` | 同一位置での再通知抑制時間（秒） |
| `NOTIFY_MOVE_THRESHOLD_M` | `200` | 即時再通知する移動距離の閾値（メートル） |
| `REQUEST_TIMEOUT_SECONDS` | `15` | ラズパイへのリクエストタイムアウト（秒） |
| `WEB_PORT` | `8081` | WebUIのポート |

### ラズパイ側（`raspberry/.env`）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `GPSD_HOST` | `localhost` | gpsdのホスト |
| `GPSD_PORT` | `2947` | gpsdのポート |
| `API_HOST` | `0.0.0.0` | APIサーバのバインドアドレス |
| `API_PORT` | `8080` | APIサーバのポート |
| `CACHE_MAX_AGE_SECONDS` | `86400` | GPS未取得時にキャッシュを無効化するまでの秒数 |

## ログの確認

```bash
# ラズパイ側
sudo journalctl -u gps-server -f

# サーバ側（GPS監視）
sudo journalctl -u gps-monitor -f

# サーバ側（温度監視）
sudo journalctl -u temp-monitor -f

# サーバ側（WebUI）
sudo journalctl -u gps-web -f
```

## ディレクトリ構造

```
car-logger/
├── raspberry/              # ラズパイ側
│   ├── pyproject.toml
│   ├── gps-server.service  # systemdユニットファイル
│   └── gps_server/
│       ├── __init__.py
│       └── main.py         # FastAPI GPS + 温度 APIサーバ
├── server/                 # サーバ側
│   ├── pyproject.toml
│   ├── sensor_map.json.example  # センサーID⇔場所名マッピングのサンプル
│   ├── gps-monitor.service      # systemdユニットファイル（GPS監視）
│   ├── temp-monitor.service     # systemdユニットファイル（温度監視）
│   ├── gps-web.service          # systemdユニットファイル（WebUI）
│   ├── data/
│   │   ├── state.json      # 監視状態（最終既知位置・通知状態）
│   │   ├── gps_history.db  # GPS位置履歴（SQLite）
│   │   └── temp_history.db # 温度履歴（SQLite）
│   ├── gps_monitor/
│   │   ├── __init__.py
│   │   ├── main.py         # GPS ポーリング・通知メインループ
│   │   ├── db.py           # SQLite GPS履歴の保存・取得
│   │   ├── notify.py       # Slack通知
│   │   └── state.py        # 監視状態の永続化
│   ├── temp_monitor/
│   │   ├── __init__.py
│   │   ├── main.py         # 温度ポーリング・DBへ保存
│   │   └── db.py           # SQLite 温度履歴の保存・取得
│   └── gps_web/
│       ├── __init__.py
│       ├── main.py         # FastAPI WebUI（GPS軌跡 + 温度グラフ）
│       └── templates/
│           ├── index.html        # Leaflet.js GPS軌跡UI
│           └── temperature.html  # Chart.js 温度グラフUI
├── .env.example            # 環境変数のサンプル
└── README.md
```

## トラブルシューティング

### GPS座標が取れない

1. `cgps -s` でgpsdが正常に動いているか確認する
2. 屋外など空が見える場所でしばらく待つ（初回補足には数分かかる場合がある）
3. `sudo systemctl status gpsd` でgpsdの状態を確認する

### ラズパイへの接続ができない

1. `tailscale status` でTailscaleのステータスを確認する
2. `curl http://<ラズパイのTailscale IP>:8080/health` で直接疎通確認する
3. ファイアウォールでポート8080が許可されているか確認する

### Slack通知が届かない

1. `server/data/state.json` を確認し、`last_notified_at` を見てクールダウン中でないか確認する
2. Webhook URLが正しいか確認する
3. サーバ側のログでエラーがないか確認する

### 温度センサーが表示されない

1. ラズパイで `ls /sys/bus/w1/devices/28-*` を実行してデバイスが見えるか確認する
2. `curl http://<ラズパイIP>:8080/temperatures` でAPIのレスポンスを確認する
3. `RASPI_BASE_URL` が正しく設定されているか確認する（`RASPI_GPS_URL` とは別の変数）
4. センサー名を設定するには `server/sensor_map.json.example` をコピーして `sensor_map.json` を作成する
