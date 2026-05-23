# car-logger-ai

車両盗難防止用GPSロガー＋車内温度モニタリング＋AI音声アシスタントシステム。ラズパイ3にGPSモジュールとDS18B20温度センサー（最大6台）を接続し、サーバからTailscale経由でデータを定期取得する。GPS情報が取れなくなった場合はSlackへ最終既知位置を地図リンク付きで通知する。WiFiスキャン＋Google Geolocation APIによる位置補完にも対応。さらにPTT（プッシュトゥトーク）ボタン付きAI音声アシスタントをラズパイ上で動作させる。

詳細は [docs/](docs/) を参照:
- [concept.md](docs/concept.md) — システム構成・通知ロジック・ディレクトリ構造
- [basic_design.md](docs/basic_design.md) — APIリファレンス・設定一覧・セットアップ詳細・トラブルシューティング

---

## クイックスタート

### ラズパイ側（GPS サーバ）

```bash
git clone <このリポジトリ> ~/car-logger-ai
cd ~/car-logger-ai/raspberry
uv sync
uv run python -m gps_server.main
```

### ラズパイ側（voice_assistant）

```bash
cd ~/car-logger-ai/raspberry/voice_assistant
sudo apt install python3-lgpio   # 初回のみ
uv venv --system-site-packages
uv sync
cp .env.example .env
nano .env  # 必須項目を設定
uv run python main.py
```

### サーバ側

```bash
cd /opt/car-logger-ai/server
cp ../.env.example .env
nano .env  # 必須項目を設定
uv sync
uv run python -m gps_monitor.main   # GPS監視
uv run python -m temp_monitor.main  # 温度監視（別ターミナル）
uv run python -m gps_web.main       # WebUI（別ターミナル）
# ブラウザで http://localhost:8081
```

セットアップの詳細（gpsd 設定・systemd 登録・WiFi測位の sudo 設定など）は [basic_design.md](docs/basic_design.md) を参照。

---

## クレジット

### 音声合成

本プロジェクトの `raspberry/voice_assistant/assets/sounds/` に含まれる WAV ファイルの一部は、[VOICEVOX](https://voicevox.hiroshiba.jp/) の **四国めたん** の音声を使用して生成しました。

> VOICEVOX:四国めたん

- キャラクター: 四国めたん
- 音声合成エンジン: VOICEVOX
- 利用規約: [VOICEVOX 利用規約](https://voicevox.hiroshiba.jp/term/) / [四国めたん キャラクター利用規約](https://zunko.jp/con_ongen_kiyaku.html)
