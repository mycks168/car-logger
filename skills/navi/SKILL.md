---
name: navi
description: カーナビ案内を開始する。ユーザーが目的地を指定して案内を求めたときに実行する。例：「渋谷駅に案内して」「〇〇まで行きたい」
---

ユーザーが指定した目的地への経路案内をラズパイ3のカーナビに指示する。

## 手順

1. **緯度経度を取得する**

   Nominatim API で目的地名を検索する。

   ```bash
   curl -s "https://nominatim.openstreetmap.org/search?q=<目的地名>&format=json&limit=1&accept-language=ja" \
     -H "User-Agent: car-logger-ai/1.0"
   ```

   レスポンスの `[0].lat` と `[0].lon` を使う。見つからなければユーザーに別の言い方で聞き直す。

2. **ナビ API を呼び出す**

   環境変数 `$RASPI_WEBHOOK_URL`（例: `http://100.x.x.x:8080`）を使って POST する。

   ```bash
   curl -s -X POST $RASPI_WEBHOOK_URL/navigate \
     -H "Content-Type: application/json" \
     -d '{"lat": <緯度>, "lon": <経度>, "name": "<目的地名>"}'
   ```

3. **結果を伝える**

   API が `{"status": "starting"}` を返したら「〇〇への案内を開始します」と伝える。
   エラーの場合は理由を説明してリトライを促す。

## 注意

- Nominatim は日本語の地名に対応している
- `WEBHOOK_TOKEN` が設定されている場合は `Authorization: Bearer $WEBHOOK_TOKEN` ヘッダーを追加する
- GPS が取得できていない場合は OSRM が経路を計算できないため、その旨を伝える
