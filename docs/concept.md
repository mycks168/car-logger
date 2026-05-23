# コンセプト

## 概要

car-logger-ai は、車両盗難防止・ドライブ支援を目的としたラズパイベースの総合モニタリングシステムです。GPS ロガー・温度センサー・AI 音声アシスタント・カーナビの4機能を1台のラズパイ3に統合し、Tailscale VPN 経由でサーバと連携します。

## 設計思想

- **常時監視・自動通知**: GPS 補足が途切れたとき（盗難・移動の疑い）に Slack へ自動通知する
- **位置補完**: GPS が取れないトンネル内などでは WiFi スキャン + Google Geolocation API で位置を補完する
- **音声アシスタント**: 運転中でもハンズフリーで AI に話しかけられるよう PTT ボタン方式を採用
- **カーナビ連携**: OpenClaw スキル経由で目的地を指示するとターンバイターン音声案内を行う
- **独立したプロセス構成**: GPS サーバ・音声アシスタント・サーバ側監視はそれぞれ独立したプロセスとして動作し、相互に依存しない

## システム構成

```
[GPS module] + [DS18B20 x6 (1-wire)] + [PTT button]
     │ UART/USB + /sys/bus/w1/devices/ + GPIO
[Raspberry Pi 3]
     ├── gps_server  ─ FastAPI (GPS + 温度 API, port 8080)
     └── voice_assistant ─ PTT AI アシスタント兼カーナビ
           ├── STT: OpenAI Whisper API または STT Gateway
           ├── LLM: OpenClaw (Claude 等)
           ├── TTS: VoiceVox または OpenAI TTS
           ├── 地図表示: OSM タイル + 経路ポリライン + POI（ノースアップ / ヘッディングアップ切替可）
           ├── ナビ: OSRM 経路計算 + ターンバイターン音声案内
           └── Webhook: POST /navigate, /map/zoom, /map/orientation 等で外部から操作
     │ iPhone USBテザリング (インターネット)
     │ Tailscale VPN
[サーバ]
     ├─ GPS監視 → Slack Incoming Webhook
     ├─ GPS履歴 → data/gps_history.db
     ├─ 温度履歴 → data/temp_history.db
     ├─ WiFi測位 → Google Geolocation API → data/gps_history.db
     ├─ WebUI (GPS軌跡 + WiFi測位 + 温度グラフ, port 8081)
     └─ OSRM (ghcr.io/project-osrm/osrm-backend, port 5000) ─ 経路計算
```

| コンポーネント | 役割 |
|---|---|
| `raspberry/gps_server/` | ラズパイ上で動作するGPS + 温度 APIサーバ |
| `raspberry/voice_assistant/` | PTTボタン操作のAI音声アシスタント兼カーナビ |
| `raspberry/voice_assistant/navigation/` | OSRM 経路計算・ターン案内エンジン・POI 検索 |
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

## ディレクトリ構造

```
car-logger-ai/
├── raspberry/                  # ラズパイ側
│   ├── pyproject.toml          # gps_server の依存管理
│   ├── gps-server.service      # systemdユニットファイル（GPS サーバ）
│   ├── gps_server/
│   │   ├── __init__.py
│   │   └── main.py             # FastAPI GPS + 温度 APIサーバ
│   └── voice_assistant/
│       ├── pyproject.toml      # voice_assistant の依存管理
│       ├── voice-assistant.service  # systemdユニットファイル
│       ├── .env.example        # 環境変数サンプル
│       ├── main.py             # エントリポイント
│       ├── config.py           # 全設定値（.env 読み込み）
│       ├── assets/
│       │   ├── pngtuber_pi3/   # キャラクター画像（アイドル・会話アニメーション）
│       │   └── sounds/         # 効果音 WAV（充電・WiFi・バッテリー警告など）
│       ├── core/
│       │   ├── app.py          # メインアシスタントロジック（状態機械）
│       │   ├── session_manager.py  # OpenClaw セッション管理
│       │   └── system_monitor.py  # バッテリー・WiFi 状態監視
│       ├── navigation/
│       │   ├── engine.py       # OSRM 経路計算・ターンバイターン案内エンジン
│       │   └── poi.py          # Overpass API によるガソリンスタンド等 POI 検索
│       ├── hardware/
│       │   ├── audio.py        # 録音（arecord）・音量チェック
│       │   ├── button.py       # PTT ボタン（GPIO 割り込み）
│       │   ├── system.py       # バッテリー・WiFi 状態読み取り
│       │   └── pi3/
│       │       ├── board.py    # ラズパイ Pi3 ボード初期化
│       │       ├── display.py  # pygame キャラクター＋テキスト＋ナビパネル表示・タップゾーン
│       │       └── map_tiles.py  # OSM タイル取得・経路ポリライン・POI 描画・ヘッディングアップ回転
│       └── services/
│           ├── llm/
│           │   └── openclaw.py     # OpenClaw ストリーミング応答
│           ├── stt/
│           │   ├── gateway.py      # STT Gateway 経由の音声認識
│           │   └── openai.py       # OpenAI Whisper API
│           ├── tts/
│           │   ├── voicevox.py     # VoiceVox TTS（非同期キュー再生）
│           │   ├── openai.py       # OpenAI TTS
│           │   └── filter.py       # TTS フィルター（コードブロック変換など）
│           └── webhook.py          # POST /speak /navigate /map/zoom /map/orientation 等 Webhook サーバ
├── server/                     # サーバ側
│   ├── pyproject.toml
│   ├── sensor_map.json.example  # センサーID⇔場所名マッピングのサンプル
│   ├── gps-monitor.service      # systemdユニットファイル（GPS監視）
│   ├── temp-monitor.service     # systemdユニットファイル（温度監視）
│   ├── gps-web.service          # systemdユニットファイル（WebUI）
│   ├── data/
│   │   ├── state.json           # 監視状態（最終既知位置・通知状態など）
│   │   ├── gps_history.db       # GPS位置履歴 + WiFi測位履歴（SQLite）
│   │   └── temp_history.db      # 温度履歴（SQLite）
│   ├── gps_monitor/
│   │   ├── main.py              # GPS ポーリング・通知メインループ
│   │   ├── db.py                # SQLite GPS履歴の保存・取得
│   │   ├── notify.py            # Slack通知
│   │   └── state.py             # 監視状態の永続化
│   ├── temp_monitor/
│   │   ├── main.py              # 温度ポーリング・DBへ保存
│   │   └── db.py                # SQLite 温度履歴の保存・取得
│   └── gps_web/
│       ├── main.py              # FastAPI WebUI（GPS軌跡 + 温度グラフ）
│       └── templates/
│           ├── index.html       # Leaflet.js GPS軌跡UI
│           └── temperature.html # Chart.js 温度グラフUI
├── skills/                     # OpenClaw スキル（voice_assistant から呼び出す）
│   ├── navi/SKILL.md           # カーナビ案内開始（Nominatim + /navigate）
│   ├── navi-stop/SKILL.md      # カーナビ案内停止
│   ├── navi-pause/SKILL.md     # カーナビ案内一時停止 / 再開
│   ├── map-zoom/SKILL.md       # 地図ズームイン / ズームアウト
│   ├── map-orientation/SKILL.md  # 地図向き切替（ノースアップ / ヘッディングアップ）
│   └── get-location/SKILL.md   # 現在地取得（逆ジオコーディング）
├── .env.example                 # サーバ側・GPS サーバ 環境変数サンプル
└── README.md
```
