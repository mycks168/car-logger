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


class TestSendPersonAlert:
    """slack_bot.send_person_alert のユニットテスト。"""

    def test_BOT_TOKEN未設定時はスキップ(self):
        import gps_web.slack_bot as m
        with patch.object(m, "SLACK_BOT_TOKEN", ""), \
             patch.object(m, "SLACK_CHANNEL", "C123"):
            # 例外が発生しないこと
            m.send_person_alert(1, b"\xff\xd8\xff", "2024-01-15T12:00:00+00:00")

    def test_CHANNEL未設定時はスキップ(self):
        import gps_web.slack_bot as m
        with patch.object(m, "SLACK_BOT_TOKEN", "xoxb-dummy"), \
             patch.object(m, "SLACK_CHANNEL", ""):
            m.send_person_alert(1, b"\xff\xd8\xff", "2024-01-15T12:00:00+00:00")

    def test_WebClient呼び出しを確認(self):
        import gps_web.slack_bot as m
        mock_client = MagicMock()
        mock_client.files_upload_v2.return_value = {"file": {"shares": {}}}
        with patch.object(m, "SLACK_BOT_TOKEN", "xoxb-dummy"), \
             patch.object(m, "SLACK_CHANNEL", "C123"), \
             patch("slack_sdk.WebClient", return_value=mock_client):
            m.send_person_alert(42, b"\xff\xd8\xff", "2024-01-15T12:00:00+00:00")
        mock_client.files_upload_v2.assert_called_once()
        mock_client.chat_postMessage.assert_called_once()
        # ボタンのvalue に photo_id が含まれること
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        button = blocks[0]["elements"][0]
        assert button["value"] == "42"
        assert button["action_id"] == "mark_family"


class TestFamilyMemberOptions:
    def test_メンバー未登録時はデフォルト選択肢を返す(self):
        import gps_web.slack_bot as m
        from gps_monitor import db as gps_db
        with patch.object(gps_db, "list_family_members", return_value=[]):
            opts = m._family_member_options()
        assert len(opts) == 1
        assert opts[0]["value"] == "unknown"

    def test_登録済みメンバーをオプションに変換する(self):
        import gps_web.slack_bot as m
        from gps_monitor import db as gps_db
        members = [
            {"id": 1, "name": "私"},
            {"id": 2, "name": "妻"},
            {"id": 3, "name": "息子"},
        ]
        with patch.object(gps_db, "list_family_members", return_value=members):
            opts = m._family_member_options()
        assert len(opts) == 3
        assert opts[0]["value"] == "私"
        assert opts[1]["value"] == "妻"
        assert opts[2]["value"] == "息子"
