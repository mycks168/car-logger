# GPS Relay

XIAO ESP32C6（車載）はTailscaleに参加できず、また車のWi-Fi環境（自宅/モバイルルーター等）が
変化するため、内部サーバ (`server/gps_monitor`, Tailscale内) から直接Pullすることができない。
このRelayは公開インターネット上に配置し、ESP32からのPushを中継することでこの問題を解決する。

```
ESP32 --(HTTPS POST /push/location, PUSH_AUTH_TOKEN)--> Relay
Relay --(HTTP GET /gps, PULL_AUTH_TOKEN)--> 内部サーバ(gps_monitor)
```

`GET /gps` のレスポンスはラズパイ版 `raspberry/gps_server` の `/gps` と互換のスキーマを維持しており、
`gps_monitor` 側は `RASPI_GPS_URL` をこのRelayのURLに向けるだけで動作する
（`gpsd_connected` は「gpsdへの接続有無」ではなく「直近 `PUSH_TIMEOUT_SECONDS` 秒以内にESP32から
Pushを受信できているか」を表す点のみ意味が異なる）。

## エンドポイント

| メソッド | パス | 認証 | 用途 |
|---|---|---|---|
| POST | `/push/location` | `Authorization: Bearer <PUSH_AUTH_TOKEN>` | ESP32からの位置情報Push |
| GET | `/gps` | `Authorization: Bearer <PULL_AUTH_TOKEN>` | 内部サーバからのPull |
| GET | `/health` | なし | ヘルスチェック |

Push/Pullでトークンを分けているのは最小権限のため（Push用トークンは書き込みのみ、
Pull用トークンは読み取りのみ可能）。

## セットアップ

1. `.env.example` を `.env` にコピーし、`PUSH_AUTH_TOKEN` / `PULL_AUTH_TOKEN` に十分に長いランダム文字列を設定する
   （例: `openssl rand -hex 32`）
2. `uv sync` で依存関係をインストール
3. `uv run python -m gps_relay.main` で起動（デフォルトは `0.0.0.0:8090`）

## 公開時の注意（重要）

このRelayは**インターネットに公開する前提**のサービスなので、以下を必ず満たすこと。

- **HTTPS必須**: ESP32側はTLS証明書検証を行う設定にできるが、そのためには信頼されたCA証明書
  （Let's Encrypt等）でこのRelayがHTTPS提供されている必要がある。リバースプロキシ
  （Caddy/nginx等）でTLS終端し、このアプリは内部でHTTP（例: 8090番ポート）のみ待ち受ける構成を推奨する。
- **公開するのはこのRelayのポートのみ**にすること。`server/gps_web`（地図WebUI・写真アップロード）は
  認証機構を持たないため、誤って公開すると車両位置や写真が誰でも閲覧・投稿可能になってしまう。
  Relayとgps_webは別ポート・別サービスとして扱い、gps_webは引き続きTailscale内限定にすること。
- トークンは十分な長さのランダム文字列にし、`.env`は絶対にコミットしないこと（`.gitignore`の
  `.env`パターンで自動的に除外される）。

## 制限事項

- 状態はメモリ上のみに保持しており、プロセス再起動でリセットされる（ESP32が
  `RELAY_PUSH_INTERVAL_SECONDS`ごとに再Pushするため、通常は数十秒以内に復元される）。
- 複数のESP32から同時にPushされることは想定していない（最新の1件のみを保持する単一状態）。
