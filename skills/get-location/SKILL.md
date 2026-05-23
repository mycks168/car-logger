---
name: get-location
description: 現在の車の位置情報を取得する。「今どこ？」「現在地は？」「今の位置を教えて」などと言ったときに実行する。
---

ラズパイ3から現在の GPS 位置情報を取得してユーザーに伝える。

## 手順

```bash
curl -s $RASPI_WEBHOOK_URL/location
```

**レスポンス例:**
```json
{
  "has_fix": true,
  "lat": 35.6812,
  "lon": 139.7671,
  "speed_kmh": 42.3
}
```

## 結果の伝え方

- `has_fix` が `true` の場合: 緯度・経度と速度をユーザーに伝える。また Nominatim で逆ジオコーディングして住所・地名に変換して伝えると親切。

  ```bash
  curl -s "https://nominatim.openstreetmap.org/reverse?lat=<lat>&lon=<lon>&format=json&accept-language=ja" \
    -H "User-Agent: car-logger-ai/1.0"
  ```

  レスポンスの `display_name` または `address` を使って「〇〇付近を走行中です」と伝える。

- `has_fix` が `false` の場合: 「現在 GPS を補足できていません」と伝える。

- `speed_kmh` が取得できている場合: 速度も合わせて伝える（例:「時速42キロで走行中です」）。

## 注意

- `WEBHOOK_TOKEN` が設定されている場合は `Authorization: Bearer $WEBHOOK_TOKEN` ヘッダーを追加する
- Nominatim の逆ジオコーディングは任意。lat/lon をそのまま伝えてもよい
