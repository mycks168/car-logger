"""Webhook HTTP サーバー。POST /speak でテキストを受け取り TTS キューに積む。

エンドポイント:
    POST /speak
    Content-Type: application/json
    {"text": "喋る内容", "title": "画面表示タイトル（省略可）"}

    または plain text:
    Content-Type: text/plain
    喋る内容

認証 (WEBHOOK_TOKEN が設定されている場合):
    Authorization: Bearer <token>

レスポンス:
    200 {"status": "queued"}   キューに積んだ
    400                        テキストが空
    401                        認証エラー
    404                        パス不正
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import config

log = logging.getLogger("voice-assistant")


class WebhookServer:
    def __init__(self, on_message):
        """
        on_message(text: str, title: str | None) が会話と非同期に呼ばれる。
        スレッドセーフなキューに積む処理を渡すこと。
        """
        self._on_message = on_message
        self._server = HTTPServer(("", config.WEBHOOK_PORT), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        log.info("webhook server listening on :%d", config.WEBHOOK_PORT)

    def stop(self):
        self._server.shutdown()

    def _make_handler(self):
        on_message = self._on_message

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("webhook: " + fmt, *args)

            def do_POST(self):
                if config.WEBHOOK_TOKEN:
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {config.WEBHOOK_TOKEN}":
                        self._respond(401, b"Unauthorized")
                        return

                if self.path != "/speak":
                    self._respond(404, b"Not Found")
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                ct = self.headers.get("Content-Type", "")

                text = ""
                title = None
                if "application/json" in ct:
                    try:
                        data = json.loads(body)
                        text = data.get("text", "")
                        title = data.get("title") or None
                    except json.JSONDecodeError:
                        self._respond(400, b"Invalid JSON")
                        return
                else:
                    text = body.strip()

                if not text:
                    self._respond(400, b"text is empty")
                    return

                log.info("webhook: queued text=%r", text[:80])
                on_message(text, title)
                self._respond(200, b'{"status":"queued"}', "application/json")

            def _respond(self, code: int, body: bytes, ct: str = "text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler
