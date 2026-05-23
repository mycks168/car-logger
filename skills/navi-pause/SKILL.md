---
name: navi-pause
description: カーナビの案内を一時停止または再開する。「案内を止めて（でも経路は消さないで）」「再開して」と言ったときに実行する。
---

ラズパイ3のカーナビ案内を一時停止 / 再開する（呼ぶたびにトグルする）。

## 手順

```bash
curl -s -X POST $RASPI_WEBHOOK_URL/navigate/pause
```

レスポンスは `{"status": "toggled"}` のみ。現在の状態はラズパイ側の音声で通知される（「一時停止しました」または「再開します」）。

ユーザーには「案内を一時停止しました」または「案内を再開します」と伝える。直前の文脈から判断してよい。

## 注意

- `WEBHOOK_TOKEN` が設定されている場合は `Authorization: Bearer $WEBHOOK_TOKEN` ヘッダーを追加する
- 案内を完全に終了したい場合は `/navi-stop` スキルを使う
