"""
Slack通知モジュール。最後に検知した地点のGoogleマップリンク付きでメッセージを送る。
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

_JST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)


def _maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lon}"


def _to_jst(iso_str: str) -> str:
    """ISO 8601文字列をJST表示に変換する。"""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M:%S JST")
    except Exception:
        return iso_str


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
        f"*最終確認時刻*: {_to_jst(last_known_at)}\n"
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
