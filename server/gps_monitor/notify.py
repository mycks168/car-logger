"""
Slack通知モジュール。最後に検知した地点のGoogleマップリンク付きでメッセージを送る。
画像検知通知は send_detection() を使い、Bot Token設定時は画像を直接添付する。
"""

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lon}"


def send_alert(
    webhook_url: str,
    reason: str,
    lat: float,
    lon: float,
    last_known_at: str,
) -> bool:
    """
    Slackへアラートを送信する。

    Args:
        webhook_url: Slack Incoming Webhook URL
        reason: アラートの原因説明
        lat, lon: 最後に既知の座標
        last_known_at: 最後にGPSを確認した時刻（ISO 8601）

    Returns:
        送信成功なら True
    """
    maps_url = _maps_url(lat, lon)
    text = (
        f":warning: *車両アラート* :warning:\n"
        f"*原因*: {reason}\n"
        f"*最終確認位置*: `{lat:.6f}, {lon:.6f}`\n"
        f"*最終確認時刻*: {last_known_at}\n"
        f"*地図*: {maps_url}"
    )
    payload = {"text": text}
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Slack通知を送信しました: %s", reason)
        return True
    except Exception as e:
        logger.error("Slack通知の送信に失敗しました: %s", e)
        return False


def send_detection(
    webhook_url: str,
    bot_token: str,
    channel_id: str,
    new_items: str,
    resident_items: str,
    captured_at: str,
    photo_path: Path,
) -> None:
    """
    画像解析の検知結果をSlackに通知する。
    Bot Token + Channel ID が設定されていれば画像添付、未設定ならWebhookでテキストのみ。
    captured_at はラズパイ撮影時刻（ISO 8601）。photo_path はアノテーション済み画像。
    """
    lines = [":rotating_light: *車両カメラ検知*", f"*新規*: {new_items}"]
    if resident_items:
        lines.append(f"*常駐（変化なし）*: {resident_items}")
    lines.append(f"*撮影時刻*: {captured_at}")
    text = "\n".join(lines)
    if bot_token and channel_id:
        _send_with_image(bot_token, channel_id, text, photo_path)
    elif webhook_url:
        try:
            httpx.post(webhook_url, json={"text": text}, timeout=10).raise_for_status()
            logger.info("Slack検知通知を送信しました（テキストのみ）")
        except Exception as e:
            logger.error("Slack通知の送信に失敗しました: %s", e)
    else:
        logger.warning("Slack通知先が未設定のため通知をスキップします")


def _send_with_image(bot_token: str, channel_id: str, text: str, photo_path: Path) -> None:
    """Slack Files API (getUploadURLExternal 方式) で画像付き通知を送る。"""
    headers = {"Authorization": f"Bearer {bot_token}"}
    image_bytes = photo_path.read_bytes()

    try:
        # 1. アップロードURL取得
        r1 = httpx.get(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=headers,
            params={"filename": photo_path.name, "length": len(image_bytes)},
            timeout=10,
        )
        data1 = r1.json()
        if not data1.get("ok"):
            logger.error("Slack upload URL取得失敗: %s", data1.get("error"))
            return

        # 2. ファイルをアップロード
        httpx.post(data1["upload_url"], content=image_bytes, timeout=30)

        # 3. 完了通知（チャンネルへ投稿）
        r3 = httpx.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers=headers,
            json={
                "files": [{"id": data1["file_id"]}],
                "channel_id": channel_id,
                "initial_comment": text,
            },
            timeout=10,
        )
        if not r3.json().get("ok"):
            logger.error("Slack upload完了通知失敗: %s", r3.json().get("error"))
        else:
            logger.info("Slack画像付き通知を送信しました")
    except Exception as e:
        logger.error("Slack画像付き通知失敗: %s", e)


def send_recovery(webhook_url: str, lat: float, lon: float) -> bool:
    """GPS取得が復帰したことを通知する。"""
    maps_url = _maps_url(lat, lon)
    text = (
        f":white_check_mark: *車両アラート解除*\n"
        f"GPS取得が復帰しました。\n"
        f"*現在位置*: `{lat:.6f}, {lon:.6f}`\n"
        f"*地図*: {maps_url}"
    )
    payload = {"text": text}
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Slack復帰通知を送信しました")
        return True
    except Exception as e:
        logger.error("Slack復帰通知の送信に失敗しました: %s", e)
        return False
