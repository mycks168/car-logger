"""notify.py のユニットテスト。"""

from unittest.mock import MagicMock, patch

import pytest

from gps_monitor.notify import _to_jst, send_alert, send_recovery


class TestToJst:
    def test_utc_iso_変換(self):
        result = _to_jst("2024-01-15T12:00:00+00:00")
        assert result == "2024-01-15 21:00:00 JST"

    def test_utc_isoformat_変換(self):
        # timezone.utc の isoformat は +00:00 を付ける
        result = _to_jst("2024-01-15T00:00:00+00:00")
        assert result == "2024-01-15 09:00:00 JST"

    def test_naive_datetime_はUTCとみなす(self):
        result = _to_jst("2024-01-15T12:00:00")
        assert result == "2024-01-15 21:00:00 JST"

    def test_不正な文字列はそのまま返す(self):
        result = _to_jst("invalid-timestamp")
        assert result == "invalid-timestamp"

    def test_日付またぎ(self):
        result = _to_jst("2024-01-15T15:30:00+00:00")
        assert result == "2024-01-16 00:30:00 JST"


class TestSendAlert:
    def test_成功時にTrueを返す(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch("gps_monitor.notify.httpx.post", return_value=mock_resp) as mock_post:
            result = send_alert(
                "https://hooks.example.com/webhook",
                reason="テスト",
                lat=35.681236,
                lon=139.767125,
                last_known_at="2024-01-15T12:00:00+00:00",
            )
        assert result is True
        payload = mock_post.call_args.kwargs["json"]
        assert "21:00:00 JST" in payload["text"]

    def test_通信失敗時にFalseを返す(self):
        with patch("gps_monitor.notify.httpx.post", side_effect=Exception("接続失敗")):
            result = send_alert(
                "https://hooks.example.com/webhook",
                reason="テスト",
                lat=35.0,
                lon=139.0,
                last_known_at="2024-01-15T12:00:00+00:00",
            )
        assert result is False


class TestSendRecovery:
    def test_成功時にTrueを返す(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch("gps_monitor.notify.httpx.post", return_value=mock_resp):
            result = send_recovery("https://hooks.example.com/webhook", 35.0, 139.0)
        assert result is True

    def test_通信失敗時にFalseを返す(self):
        with patch("gps_monitor.notify.httpx.post", side_effect=Exception("接続失敗")):
            result = send_recovery("https://hooks.example.com/webhook", 35.0, 139.0)
        assert result is False
