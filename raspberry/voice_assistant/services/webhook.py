"""Webhook HTTP サーバー。外部からナビ操作・TTS 読み上げを受け付ける。

エンドポイント:
    POST /speak
        {"text": "喋る内容", "title": "画面表示タイトル（省略可）"}

    POST /navigate
        {"lat": 35.658, "lon": 139.701, "name": "渋谷駅"}
        → 経路案内を開始する

    POST /navigate/stop
        → 案内を停止する

    POST /navigate/pause
        → 案内を一時停止 / 再開トグルする

    POST /map/zoom
        {"delta": 1}  または  {"level": 16}
        → ズームイン / ズームアウト / 絶対値指定

認証 (WEBHOOK_TOKEN が設定されている場合):
    Authorization: Bearer <token>
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

import config

log = logging.getLogger("voice-assistant")


class WebhookServer:
    def __init__(
        self,
        on_message: Callable[[str, str | None], None],
        on_navigate: Callable[[float, float, str], None] | None = None,
        on_navigate_stop: Callable[[], None] | None = None,
        on_navigate_pause: Callable[[], None] | None = None,
        on_map_zoom: Callable[[int | None, int | None], None] | None = None,
    ):
        """
        on_message(text, title)   : /speak — TTS キューに積む
        on_navigate(lat, lon, name): /navigate — 経路案内を開始する
        on_navigate_stop()        : /navigate/stop — 案内停止
        on_navigate_pause()       : /navigate/pause — 一時停止/再開
        on_map_zoom(delta, level) : /map/zoom — ズーム変更（delta か level どちらか non-None）
        """
        self._on_message = on_message
        self._on_navigate = on_navigate or (lambda lat, lon, name: None)
        self._on_navigate_stop = on_navigate_stop or (lambda: None)
        self._on_navigate_pause = on_navigate_pause or (lambda: None)
        self._on_map_zoom = on_map_zoom or (lambda delta, level: None)

        self._server = HTTPServer(("", config.WEBHOOK_PORT), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        log.info("webhook server listening on :%d", config.WEBHOOK_PORT)

    def stop(self):
        self._server.shutdown()

    def _make_handler(self):
        on_message = self._on_message
        on_navigate = self._on_navigate
        on_navigate_stop = self._on_navigate_stop
        on_navigate_pause = self._on_navigate_pause
        on_map_zoom = self._on_map_zoom

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("webhook: " + fmt, *args)

            def _check_auth(self) -> bool:
                if not config.WEBHOOK_TOKEN:
                    return True
                auth = self.headers.get("Authorization", "")
                return auth == f"Bearer {config.WEBHOOK_TOKEN}"

            def _read_json(self) -> dict | None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                try:
                    return json.loads(body) if body.strip() else {}
                except json.JSONDecodeError:
                    return None

            def _respond(self, code: int, body: bytes, ct: str = "text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _respond_json(self, code: int, obj: dict):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self._respond(code, body, "application/json")

            def do_POST(self):
                if not self._check_auth():
                    self._respond(401, b"Unauthorized")
                    return

                path = self.path.rstrip("/")

                # ── POST /speak ──────────────────────────────────────────────
                if path == "/speak":
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
                    log.info("webhook /speak: %r", text[:80])
                    on_message(text, title)
                    self._respond_json(200, {"status": "queued"})

                # ── POST /navigate ───────────────────────────────────────────
                elif path == "/navigate":
                    data = self._read_json()
                    if data is None:
                        self._respond(400, b"Invalid JSON")
                        return
                    lat = data.get("lat")
                    lon = data.get("lon")
                    name = data.get("name", "目的地")
                    if lat is None or lon is None:
                        self._respond(400, b"lat/lon required")
                        return
                    try:
                        lat, lon = float(lat), float(lon)
                    except (TypeError, ValueError):
                        self._respond(400, b"lat/lon must be numbers")
                        return
                    log.info("webhook /navigate: %s (%.5f, %.5f)", name, lat, lon)
                    threading.Thread(
                        target=on_navigate, args=(lat, lon, name), daemon=True
                    ).start()
                    self._respond_json(200, {"status": "starting"})

                # ── POST /navigate/stop ──────────────────────────────────────
                elif path == "/navigate/stop":
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    log.info("webhook /navigate/stop")
                    threading.Thread(target=on_navigate_stop, daemon=True).start()
                    self._respond_json(200, {"status": "stopped"})

                # ── POST /navigate/pause ─────────────────────────────────────
                elif path == "/navigate/pause":
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    log.info("webhook /navigate/pause")
                    threading.Thread(target=on_navigate_pause, daemon=True).start()
                    self._respond_json(200, {"status": "toggled"})

                # ── POST /map/zoom ───────────────────────────────────────────
                elif path == "/map/zoom":
                    data = self._read_json()
                    if data is None:
                        self._respond(400, b"Invalid JSON")
                        return
                    delta = data.get("delta")
                    level = data.get("level")
                    if delta is None and level is None:
                        self._respond(400, b"delta or level required")
                        return
                    try:
                        delta = int(delta) if delta is not None else None
                        level = int(level) if level is not None else None
                    except (TypeError, ValueError):
                        self._respond(400, b"delta/level must be integers")
                        return
                    log.info("webhook /map/zoom: delta=%s level=%s", delta, level)
                    threading.Thread(
                        target=on_map_zoom, args=(delta, level), daemon=True
                    ).start()
                    self._respond_json(200, {"status": "ok"})

                else:
                    self._respond(404, b"Not Found")

        return _Handler
