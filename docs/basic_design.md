# 基本設計（外部仕様）

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
  "cache_age_seconds": 2.1,
  "wifi_aps": [{"macAddress": "AA:BB:CC:DD:EE:FF", "signalStrength": -65}],
  "wifi_scanned_at": "2026-05-12T09:55:00+00:00"
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
| `wifi_aps` | WiFiスキャン結果（`[{"macAddress": "...", "signalStrength": -65}, ...]`） |
| `wifi_scanned_at` | 最後にWiFiスキャンした時刻 |

### `GET /temperatures`

接続中の DS18B20 センサー全台と CPU 温度を返す。

**レスポンス例:**
```json
{
  "sensors": [
    {"id": "cpu", "temperature_c": 51.23, "error": null},
    {"id": "28-0123456789ab", "temperature_c": 28.5, "error": null}
  ],
  "read_at": "2026-05-12T10:00:00+00:00"
}
```

### `GET /health`

疎通確認用。常に `{"status": "ok"}` を返す。

---

### Webhook API（voice_assistant）

`WEBHOOK_ENABLED=true` にすると voice_assistant が HTTP サーバを起動し、外部からテキストを送って TTS 読み上げさせることができる。

#### `POST /speak`

**リクエスト（JSON）:**
```bash
curl -X POST http://localhost:8080/speak \
  -H "Content-Type: application/json" \
  -d '{"text": "目的地に到着しました", "title": "ナビ"}'
```

**リクエスト（プレーンテキスト）:**
```bash
curl -X POST http://localhost:8080/speak \
  -H "Content-Type: text/plain" \
  -d "速度注意"
```

**認証あり（`WEBHOOK_TOKEN` が設定されている場合）:**
```bash
curl -X POST http://localhost:8080/speak \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"text": "メッセージ"}'
```

| フィールド | 説明 |
|---|---|
| `text` | 読み上げるテキスト（必須） |
| `title` | 画面に表示するタイトル（省略可。省略時は `text` の先頭50文字を表示） |

| レスポンス | 意味 |
|---|---|
| `200 {"status": "queued"}` | キューに積まれた |
| `400` | `text` が空 |
| `401` | 認証エラー |
| `404` | パスが `/speak` 以外 |

---

## 設定一覧

### サーバ側（`server/.env`）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `RASPI_BASE_URL` | （必須） | ラズパイのベースURL（温度取得に使用） |
| `RASPI_GPS_URL` | （必須） | ラズパイの GPS API URL |
| `SLACK_WEBHOOK_URL` | （必須） | Slack Incoming Webhook URL |
| `GOOGLE_GEOLOCATION_API_KEY` | （任意） | Google Geolocation APIキー（未設定時はWiFi測位スキップ） |
| `POLL_INTERVAL_SECONDS` | `60` | GPS ポーリング間隔（秒） |
| `TEMP_POLL_INTERVAL_SECONDS` | `300` | 温度ポーリング間隔（秒） |
| `GEOLOCATION_INTERVAL_SECONDS` | `300` | Geolocation API 呼び出し間隔（秒） |
| `NOTIFY_COOLDOWN_SECONDS` | `1800` | 同一位置での再通知抑制時間（秒） |
| `NOTIFY_MOVE_THRESHOLD_M` | `200` | 即時再通知する移動距離の閾値（メートル） |
| `REQUEST_TIMEOUT_SECONDS` | `15` | ラズパイへのリクエストタイムアウト（秒） |
| `WEB_PORT` | `8081` | WebUIのポート |

### GPS サーバ（`raspberry/.env`）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `GPSD_HOST` | `localhost` | gpsdのホスト |
| `GPSD_PORT` | `2947` | gpsdのポート |
| `API_HOST` | `0.0.0.0` | APIサーバのバインドアドレス |
| `API_PORT` | `8080` | APIサーバのポート |
| `CACHE_MAX_AGE_SECONDS` | `86400` | GPS未取得時にキャッシュを無効化するまでの秒数 |
| `WIFI_IFACE` | `wlan0` | WiFiスキャンに使うインターフェース名 |
| `WIFI_SCAN_INTERVAL_SECONDS` | `300` | WiFiスキャン間隔（秒） |

### voice_assistant（`raspberry/voice_assistant/.env`）

#### STT / TTS エンジン

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `STT_ENGINE` | `gateway` | 音声認識エンジン（`gateway` または `openai`） |
| `TTS_ENGINE` | `voicevox` | 音声合成エンジン（`voicevox` または `openai`） |

#### OpenAI（`STT_ENGINE=openai` または `TTS_ENGINE=openai` の場合）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `OPENAI_API_KEY` | （必須） | OpenAI API キー |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Whisper モデル名 |
| `OPENAI_TTS_MODEL` | `gpt-4o-mini-tts-2025-12-15` | TTS モデル名 |
| `OPENAI_TTS_VOICE` | `coral` | TTS 音声名 |
| `OPENAI_TTS_SPEED` | `1.1` | 読み上げ速度 |
| `OPENAI_TTS_GAIN_DB` | `9` | 音量ゲイン (dB) |

#### STT Gateway（`STT_ENGINE=gateway` の場合）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `STT_GATEWAY_URL` | `http://localhost:23000` | STT Gateway のベース URL |
| `STT_GATEWAY_LANGUAGE` | `ja` | 認識言語 |

#### VoiceVox（`TTS_ENGINE=voicevox` の場合）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `VOICEVOX_URL` | `http://localhost:50021` | VoiceVox のベース URL |
| `VOICEVOX_SPEAKER` | `2` | スピーカーID |
| `VOICEVOX_SAMPLE_RATE` | `48000` | サンプルレート (Hz) |

#### LLM（OpenClaw）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `OPENCLAW_BASE_URL` | `http://localhost:18789` | OpenClaw のベース URL |
| `OPENCLAW_TOKEN` | （必須） | API トークン |
| `OPENCLAW_AGENT_ID` | （必須） | エージェント ID |
| `OPENCLAW_SESSION_KEY` | （必須） | セッションキー |
| `OPENCLAW_SESSION_n` | （任意） | 複数セッション定義（書式: `名前,agent_id,session_key[,base_url[,token]]`） |

#### ハードウェア

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `PI3_PTT_GPIO` | `17` | PTT スイッチの GPIO ピン番号（BCM） |
| `AUDIO_DEVICE` | `plughw:1,0` | マイク入力デバイス（`arecord -l` で確認） |
| `AUDIO_OUTPUT_DEVICE` | `default` | 音声出力デバイス（`aplay -l` で確認） |
| `AUDIO_OUTPUT_CARD` | `0` | 音声出力カード番号 |
| `AUDIO_OUTPUT_VOLUME` | `90` | 音量（%） |
| `AUDIO_SAMPLE_RATE` | `16000` | マイク録音サンプルレート (Hz) |

#### ディスプレイ

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `PI3_DISPLAY_WIDTH` | `1280` | HDMI ディスプレイ幅 (px) |
| `PI3_DISPLAY_HEIGHT` | `720` | HDMI ディスプレイ高さ (px) |
| `PI3_DISPLAY_FULLSCREEN` | `false` | フルスクリーン表示 |
| `PI3_CHAR_SCALE` | `1.0` | キャラクター画像のスケール |
| `UI_IMAGE_ASSETS_DIR` | `assets/pngtuber_pi3` | キャラクター画像ディレクトリ |

#### GPS 連携・地図

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `GPS_SERVER_URL` | `http://localhost:8080` | gps_server の URL（地図表示用） |
| `MAP_ZOOM` | `15` | 地図ズームレベル（14=広域 15=町丁目 16=建物） |
| `MAP_TILE_CACHE_DIR` | `/tmp/maptiles` | タイルキャッシュ保存先 |

#### Webhook サーバ

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `WEBHOOK_ENABLED` | `false` | Webhook サーバを有効にするか |
| `WEBHOOK_PORT` | `8080` | Webhook サーバのポート |
| `WEBHOOK_TOKEN` | （任意） | Bearer 認証トークン（空なら認証なし） |

#### その他

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `ENABLE_TTS` | `true` | TTS 音声合成を有効にするか |
| `CONVERSATION_HISTORY_LENGTH` | `5` | LLM に送る会話履歴のターン数 |
| `SILENCE_RMS_THRESHOLD` | `200` | 無音と判断する RMS 閾値（下回ると録音をスキップ） |
| `DRY_RUN` | 自動 | `true` にすると API 呼び出しをスキップ（OpenAI キー未設定時に自動で `true` になる） |

---

## セットアップ詳細

### 前提条件

| 環境 | 必要なもの |
|---|---|
| ラズパイ | `gpsd` インストール済み、GPS モジュール接続済み |
| ラズパイ | Tailscale 設定済み、`uv` インストール済み |
| ラズパイ | `wlan0` が利用可能（WiFi測位を使う場合） |
| サーバ | Tailscale 設定済み、`uv` インストール済み |
| Slack | Incoming Webhook URL 取得済み |
| Google Cloud | Geolocation API 有効化・APIキー取得済み（WiFi測位を使う場合） |
| OpenAI / OpenClaw | API キー・エンドポイント取得済み（voice_assistant を使う場合） |

### ラズパイ側セットアップ（GPS サーバ）

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
git clone <このリポジトリ> ~/car-logger-ai
cd ~/car-logger-ai/raspberry

# 依存パッケージをインストールして起動
uv sync
uv run python -m gps_server.main

# 動作確認
curl http://localhost:8080/gps
curl http://localhost:8080/health
```

#### WiFiスキャンの sudo 設定（WiFi測位を使う場合）

`iwlist` はデフォルトで root 権限が必要なため、パスワードなしで実行できるよう設定する。

```bash
echo "pi ALL=(ALL) NOPASSWD: /sbin/iwlist" | sudo tee /etc/sudoers.d/iwlist
sudo chmod 440 /etc/sudoers.d/iwlist

# 動作確認
sudo iwlist wlan0 scan
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

---

### ラズパイ側セットアップ（voice_assistant）

voice_assistant は gps_server と独立したプロセスとして動く。`gps_server` が起動していれば、地図タイルの表示に GPS 情報が使われる。

#### 必要な外部サービス

| サービス | 用途 | 設定変数 |
|---|---|---|
| OpenClaw (必須) | LLM 応答生成 | `OPENCLAW_BASE_URL`, `OPENCLAW_TOKEN`, `OPENCLAW_AGENT_ID` |
| STT Gateway または OpenAI | 音声認識 (STT) | `STT_ENGINE`, `STT_GATEWAY_URL` または `OPENAI_API_KEY` |
| VoiceVox または OpenAI | 音声合成 (TTS) | `TTS_ENGINE`, `VOICEVOX_URL` または `OPENAI_API_KEY` |

#### インストールと起動

```bash
cd ~/car-logger-ai/raspberry/voice_assistant

# 依存パッケージをインストール（ラズパイ Pi3 用のハードウェアドライバも含める）
uv sync --extra pi3

# 環境変数ファイルを作成・編集
cp .env.example .env
nano .env

# 起動（直接）
uv run python main.py
```

#### ハードウェア配線

| 部品 | GPIO ピン（BCM） | 説明 |
|---|---|---|
| PTT スイッチ | `PI3_PTT_GPIO`（デフォルト: 17） | プッシュトゥトークボタン |
| HDMI ディスプレイ | HDMI0 | キャラクター＋地図表示用 |

PTT ボタンの操作方法:

| 操作 | 動作 |
|---|---|
| 押す | 録音開始（マイクに向かって話す） |
| 離す | 録音停止・STT・LLM問い合わせ・TTS再生 |
| 長押し後離す（2秒以上） | キャンセル（応答を中断してアイドルへ戻る） |
| ダブルクリック | セッション切り替え（`OPENCLAW_SESSION_n` を複数設定している場合） |

#### systemdサービスとして登録（自動起動）

```bash
sudo cp voice-assistant.service /etc/systemd/system/
# ユーザ名や配置パスが異なる場合はサービスファイルを編集
sudo nano /etc/systemd/system/voice-assistant.service

sudo systemctl daemon-reload
sudo systemctl enable voice-assistant
sudo systemctl start voice-assistant
sudo systemctl status voice-assistant
```

ログは `/tmp/voice-assistant.log` にも書き出される。

```bash
# systemd ジャーナルで確認
sudo journalctl -u voice-assistant -f

# ファイルで確認
tail -f /tmp/voice-assistant.log
```

---

### サーバ側セットアップ

```bash
git clone <このリポジトリ> /opt/car-logger-ai
cd /opt/car-logger-ai/server

# 環境変数ファイルを作成
cp ../.env.example .env
nano .env  # 下記の必須項目を設定
```

**`.env` の必須設定項目:**

| 変数名 | 説明 | 例 |
|---|---|---|
| `RASPI_BASE_URL` | ラズパイのベースURL | `http://100.x.x.x:8080` |
| `RASPI_GPS_URL` | ラズパイの GPS API URL | `http://100.x.x.x:8080/gps` |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL | `https://hooks.slack.com/...` |
| `GOOGLE_GEOLOCATION_API_KEY` | Google Geolocation APIキー（WiFi測位を使う場合） | `AIzaSy...` |

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

---

## ログの確認

```bash
# ラズパイ側（GPS サーバ）
sudo journalctl -u gps-server -f

# ラズパイ側（voice_assistant）
sudo journalctl -u voice-assistant -f
tail -f /tmp/voice-assistant.log

# サーバ側（GPS監視）
sudo journalctl -u gps-monitor -f

# サーバ側（温度監視）
sudo journalctl -u temp-monitor -f

# サーバ側（WebUI）
sudo journalctl -u gps-web -f
```

---

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

### WiFi測位が表示されない

1. `GOOGLE_GEOLOCATION_API_KEY` が設定されているか確認する
2. ラズパイで `sudo iwlist wlan0 scan` が実行できるか確認する
3. `/etc/sudoers.d/iwlist` の設定が正しいか確認する（「WiFiスキャンの sudo 設定」参照）
4. `server/data/gps_history.db` の `geolocation_log` テーブルにデータがあるか確認する:
   ```bash
   sqlite3 server/data/gps_history.db "SELECT * FROM geolocation_log LIMIT 5;"
   ```

### voice_assistant が起動しない

1. `uv sync --extra pi3` で依存パッケージが正しくインストールされているか確認する
2. `OPENCLAW_BASE_URL` と `OPENCLAW_TOKEN` が設定されているか確認する
3. `arecord -l` でマイクデバイスが見えるか確認し、`AUDIO_DEVICE` を合わせる
4. `DRY_RUN=true` を設定すると API 呼び出しをスキップして動作確認できる

### 音声が録音されない / 無音と判断される

1. `arecord -D <AUDIO_DEVICE> -f S16_LE -r 16000 test.wav` で録音できるか確認する
2. `SILENCE_RMS_THRESHOLD` を下げる（デフォルト `200`）
3. `AUDIO_SAMPLE_RATE` がマイクの対応レートと一致しているか確認する

### TTS が再生されない

1. `aplay -l` で出力デバイスを確認し、`AUDIO_OUTPUT_DEVICE` を正しく設定する（例: `hdmi:CARD=vc4hdmi0`）
2. `AUDIO_OUTPUT_VOLUME` が十分な値か確認する
3. `TTS_ENGINE=voicevox` の場合、`VOICEVOX_URL` にアクセスできるか確認する

### ボタンが反応しない

1. `PI3_PTT_GPIO` のピン番号（BCM）が正しいか確認する
2. ラズパイの GPIO が有効になっているか確認する（`raspi-config` → Interface Options → GPIO）
3. `gpiozero` がインストールされているか確認する（`uv sync --extra pi3`）
