"""
Slack Bolt + Socket Mode による人物検知通知・家族学習モジュール。

起動方法: start_socket_mode() をバックグラウンドスレッドで呼ぶ。
通知送信: send_person_alert() を呼ぶ。
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")

_JST = timezone(timedelta(hours=9))
_photos_dir: Path | None = None


def _to_jst(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M:%S JST")
    except Exception:
        return iso_str


def _family_member_options() -> list[dict]:
    """DBから家族メンバーを読んでSlackのoption形式で返す。"""
    from gps_monitor import db as gps_db
    members = gps_db.list_family_members()
    if not members:
        return [{"text": {"type": "plain_text", "text": "（未登録）"}, "value": "unknown"}]
    return [
        {"text": {"type": "plain_text", "text": m["name"]}, "value": m["name"]}
        for m in members
    ]


def _make_bolt_app():
    from slack_bolt import App
    app = App(token=SLACK_BOT_TOKEN)

    @app.action("mark_family")
    def handle_mark_family(ack, body, client):
        """「この人は家族です」ボタン → 家族選択モーダルを開く。"""
        ack()
        photo_id = body["actions"][0]["value"]
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "family_label_modal",
                "private_metadata": photo_id,  # photo_id をモーダルに引き継ぐ
                "title": {"type": "plain_text", "text": "家族として登録"},
                "submit": {"type": "plain_text", "text": "登録する"},
                "close": {"type": "plain_text", "text": "キャンセル"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"写真 {photo_id} の人物を誰として登録しますか？"},
                    },
                    {
                        "type": "input",
                        "block_id": "label_block",
                        "label": {"type": "plain_text", "text": "名前"},
                        "element": {
                            "type": "static_select",
                            "action_id": "label_select",
                            "placeholder": {"type": "plain_text", "text": "選択してください"},
                            "options": _family_member_options(),
                        },
                    },
                ],
            },
        )

    @app.view("family_label_modal")
    def handle_family_label_submit(ack, body, client):
        """モーダル送信 → 顔埋め込みをラベル付きで保存。"""
        ack()
        photo_id = int(body["view"]["private_metadata"])
        label = body["view"]["state"]["values"]["label_block"]["label_select"]["selected_option"]["value"]
        channel = body.get("channel", {}).get("id") or SLACK_CHANNEL

        # 元メッセージのtsはbodyに含まれないため、スレッドへの返信で結果を通知する
        _do_register_face(client, photo_id, label, channel, message_ts=None)

    return app


def _do_register_face(
    client,
    photo_id: int,
    label: str,
    channel: str,
    message_ts: str | None,
) -> None:
    """写真の顔を指定ラベルで家族登録し、Slackに結果を通知する。"""
    from gps_monitor import db as gps_db
    from gps_web.face_recog import detect_faces, embedding_to_bytes

    try:
        photo_path_rel = gps_db.get_photo_path(photo_id)
        if photo_path_rel is None or _photos_dir is None:
            client.chat_postMessage(channel=channel, text=f":x: 写真 {photo_id} が見つかりません")
            return

        photo_bytes = (_photos_dir / photo_path_rel).read_bytes()
        result = detect_faces(photo_bytes)
        if not result.embeddings:
            client.chat_postMessage(channel=channel, text=":x: 顔を再検出できませんでした")
            return

        now = datetime.now(timezone.utc).isoformat()
        for emb in result.embeddings:
            gps_db.insert_family_face(
                created_at=now,
                photo_id=photo_id,
                embedding=embedding_to_bytes(emb),
                label=label,
            )

        gps_db.update_photo_person_info(
            photo_id, person_detected=True, is_family=True, family_label=label
        )

        msg = f":white_check_mark: 写真 {photo_id} の人物を *{label}* として登録しました"
        if message_ts:
            client.chat_update(
                channel=channel,
                ts=message_ts,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": msg}}],
                text=msg,
            )
        else:
            client.chat_postMessage(channel=channel, text=msg)

        logger.info("家族登録完了: photo_id=%d, label=%s, 顔数=%d", photo_id, label, len(result.embeddings))

    except Exception as e:
        logger.error("家族登録処理エラー: %s", e)
        try:
            client.chat_postMessage(channel=channel, text=f":x: 登録中にエラーが発生しました: {e}")
        except Exception:
            pass


def start_socket_mode(photos_dir: Path) -> None:
    """Socket Mode ハンドラーをバックグラウンドスレッドで起動する。"""
    global _photos_dir

    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        logger.info("SLACK_BOT_TOKEN または SLACK_APP_TOKEN 未設定: Socket Mode を無効化します")
        return

    _photos_dir = photos_dir

    def _run():
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        bolt_app = _make_bolt_app()
        handler = SocketModeHandler(bolt_app, SLACK_APP_TOKEN)
        logger.info("Slack Socket Mode を開始しました")
        handler.start()

    threading.Thread(target=_run, daemon=True, name="slack-socket-mode").start()


def send_person_alert(photo_id: int, photo_bytes: bytes, recorded_at: str) -> None:
    """不審者検知をSlackに通知する。写真をアップロードしボタン付きメッセージを送る。"""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        logger.warning("SLACK_BOT_TOKEN または SLACK_CHANNEL 未設定: 人物通知をスキップします")
        return

    try:
        from slack_sdk import WebClient
        client = WebClient(token=SLACK_BOT_TOKEN)

        client.files_upload_v2(
            channel=SLACK_CHANNEL,
            content=photo_bytes,
            filename=f"person_{photo_id}.jpg",
            title=f"検知写真 (ID: {photo_id})",
            initial_comment=(
                f":bust_in_silhouette: *不審者を検知しました*\n"
                f"*検知時刻*: {_to_jst(recorded_at)}\n"
                f"*写真ID*: {photo_id}"
            ),
        )

        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=[
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "この人は家族です"},
                            "style": "primary",
                            "value": str(photo_id),
                            "action_id": "mark_family",
                        }
                    ],
                }
            ],
            text=f"不審者検知 (写真ID: {photo_id})",
        )
        logger.info("Slack人物通知を送信しました: photo_id=%d", photo_id)

    except Exception as e:
        logger.error("Slack人物通知の送信に失敗しました: %s", e)
