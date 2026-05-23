---
name: map-orientation
description: 地図の向きをノースアップ / ヘッディングアップで切り替える。「ヘッディングアップにして」「ノースアップにして」「地図の向きを変えて」などと言ったときに実行する。
---

地図の向きをトグル切り替えする（ノースアップ ↔ ヘッディングアップ）。

## 手順

```bash
curl -s -X POST $RASPI_WEBHOOK_URL/map/orientation \
  ${WEBHOOK_TOKEN:+-H "Authorization: Bearer $WEBHOOK_TOKEN"}
```

レスポンスが `{"status": "ok"}` であれば成功。

## 結果の伝え方

- 成功したら「ヘッディングアップに切り替えました」または「ノースアップに切り替えました」と伝える。
  ラズパイ側が TTS で読み上げるため、どちらに切り替わったかはユーザーの耳に届く。
- ユーザーが「ヘッディングアップ」と明示したなら、すでにその状態の可能性もあるがトグルするのみ。

## 注意

- `WEBHOOK_TOKEN` が設定されている場合は `Authorization: Bearer $WEBHOOK_TOKEN` ヘッダーを追加する
