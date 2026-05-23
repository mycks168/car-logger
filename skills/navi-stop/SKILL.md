---
name: navi-stop
description: カーナビの案内を終了する。ユーザーが「案内終了」「ナビを止めて」と言ったときに実行する。
---

ラズパイ3のカーナビ案内を停止する。

## 手順

```bash
curl -s -X POST http://<RASPI_IP>:<WEBHOOK_PORT>/navigate/stop
```

成功（レスポンスが `{"status": "stopped"}`）したら「案内を終了しました」と伝える。

## 注意

- `WEBHOOK_TOKEN` が設定されている場合は `Authorization: Bearer <token>` ヘッダーを追加する
- 案内中でなくても呼んでよい（エラーにはならない）
