---
name: map-zoom
description: 地図のズームレベルを変更する。「ズームイン」「地図を拡大」「もう少し広く見せて」「ズームを16にして」などと言ったときに実行する。
---

ラズパイ3の地図表示をズームイン / ズームアウトする。

## 手順

ユーザーの発言から操作を判断して API を呼び分ける。

**ズームイン（拡大）:**
```bash
curl -s -X POST http://<RASPI_IP>:<WEBHOOK_PORT>/map/zoom \
  -H "Content-Type: application/json" \
  -d '{"delta": 1}'
```

**ズームアウト（縮小）:**
```bash
curl -s -X POST http://<RASPI_IP>:<WEBHOOK_PORT>/map/zoom \
  -H "Content-Type: application/json" \
  -d '{"delta": -1}'
```

**ズームレベルを絶対値で指定（「ズームを16にして」など）:**
```bash
curl -s -X POST http://<RASPI_IP>:<WEBHOOK_PORT>/map/zoom \
  -H "Content-Type: application/json" \
  -d '{"level": <数値>}'
```

## ズームレベルの目安

| レベル | 表示範囲 |
|---|---|
| 12 | 市区町村 |
| 14 | 広域道路 |
| 15 | 町丁目（デフォルト） |
| 16 | 建物レベル |
| 17〜18 | 詳細な建物 |

## 注意

- `WEBHOOK_TOKEN` が設定されている場合は `Authorization: Bearer <token>` ヘッダーを追加する
- レベルの範囲は 10〜19。範囲外はラズパイ側でクランプされる
- 「もっと広く」「もっと詳しく」など曖昧な場合は `delta: 1` または `delta: -1` を使う
