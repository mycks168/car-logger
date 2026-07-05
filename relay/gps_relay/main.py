"""
GPS Push/Pull 中継サーバ。

車載のXIAO ESP32C6はTailscaleに参加できず、かつWi-Fi環境（自宅/モバイルルーター等）が
変化するため、内部サーバ(gps_monitor, Tailscale内)から直接Pullすることができない。
そこで公開サーバ上でこのRelayを動かし、以下の2方向のやり取りを仲介する。

  ESP32 --(HTTPS Push, PUSH_AUTH_TOKEN)--> Relay --(HTTP Pull, PULL_AUTH_TOKEN)--> gps_monitor

GET /gps のレスポンスはラズパイ版 gps_server の /gps と互換のスキーマを維持し、
gps_monitor 側のポーリング・アラートロジックを変更せずに済むようにしている。
ただし gpsd_connected は「gpsdへの接続有無」ではなく「直近 PUSH_TIMEOUT_SECONDS 秒以内に
ESP32からPushを受信できているか」を表す点が異なる。
"""

import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

load_dotenv()


def _require_env(name: str) -> str:
    """必須環境変数を取得する。未設定ならRuntimeErrorを送出する。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません")
    return value


PUSH_AUTH_TOKEN = _require_env("PUSH_AUTH_TOKEN")
PULL_AUTH_TOKEN = _require_env("PULL_AUTH_TOKEN")
CACHE_MAX_AGE_SECONDS = int(os.getenv("CACHE_MAX_AGE_SECONDS", str(60 * 60 * 24)))
PUSH_TIMEOUT_SECONDS = int(os.getenv("PUSH_TIMEOUT_SECONDS", "180"))

_bearer = HTTPBearer(auto_error=False)


@dataclass
class RelayState:
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    speed_kmh: float | None = None
    has_fix: bool = False
    last_fix_at: datetime | None = None
    last_push_at: datetime | None = None
    wifi_aps: list[dict] = field(default_factory=list)
    wifi_scanned_at: datetime | None = None


_state = RelayState()
_state_lock = threading.Lock()

app = FastAPI(title="GPS Relay")


class LocationPush(BaseModel):
    lat: float
    lon: float
    alt: float | None = None
    speed_kmh: float | None = None
    has_fix: bool = True
    recorded_at: str
    wifi_aps: list[dict] = []
    wifi_scanned_at: str | None = None


def _parse_iso(value: str) -> datetime:
    """ISO8601文字列(末尾Z許容)をtimezone-awareなdatetimeに変換する。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _check_token(expected: str, credentials: HTTPAuthorizationCredentials | None) -> None:
    """Bearerトークンを定数時間比較で検証する。不一致・未指定なら401を送出する。"""
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401, detail="認証に失敗しました")


def require_push_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    _check_token(PUSH_AUTH_TOKEN, credentials)


def require_pull_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    _check_token(PULL_AUTH_TOKEN, credentials)


@app.post("/push/location")
def push_location(body: LocationPush, _: None = Depends(require_push_auth)) -> JSONResponse:
    """ESP32からの位置情報Pushを受け付け、最新状態を更新する。"""
    now = datetime.now(timezone.utc)
    with _state_lock:
        _state.lat = body.lat
        _state.lon = body.lon
        _state.alt = body.alt
        _state.speed_kmh = body.speed_kmh
        _state.has_fix = body.has_fix
        _state.last_push_at = now
        if body.has_fix:
            _state.last_fix_at = _parse_iso(body.recorded_at) if body.recorded_at else now
        if body.wifi_scanned_at is not None:
            _state.wifi_aps = body.wifi_aps
            _state.wifi_scanned_at = _parse_iso(body.wifi_scanned_at)
    return JSONResponse({"ok": True})


@app.get("/gps")
def get_gps(_: None = Depends(require_pull_auth)) -> JSONResponse:
    """
    現在のキャッシュ状態をラズパイ版 /gps 互換のJSONで返す。

    - gpsd_connected: 直近 PUSH_TIMEOUT_SECONDS 秒以内にESP32からPushを受信できているか
    - has_fix: 直近のPush内容がfix有りだった、かつPush自体が新しい場合のみtrue
    """
    now = datetime.now(timezone.utc)
    with _state_lock:
        lat = _state.lat
        lon = _state.lon
        alt = _state.alt
        speed_kmh = _state.speed_kmh
        has_fix = _state.has_fix
        last_fix_at = _state.last_fix_at
        last_push_at = _state.last_push_at
        wifi_aps = list(_state.wifi_aps)
        wifi_scanned_at = _state.wifi_scanned_at

    push_age_seconds = (now - last_push_at).total_seconds() if last_push_at else None
    gpsd_connected = push_age_seconds is not None and push_age_seconds < PUSH_TIMEOUT_SECONDS

    cache_age_seconds = None
    if last_fix_at is not None:
        cache_age_seconds = (now - last_fix_at).total_seconds()
        if cache_age_seconds > CACHE_MAX_AGE_SECONDS:
            lat = None
            lon = None

    return JSONResponse({
        "has_fix": has_fix and gpsd_connected,
        "gpsd_connected": gpsd_connected,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "speed_kmh": speed_kmh,
        "last_fix_at": last_fix_at.isoformat() if last_fix_at else None,
        "cache_age_seconds": round(cache_age_seconds, 1) if cache_age_seconds is not None else None,
        "wifi_aps": wifi_aps,
        "wifi_scanned_at": wifi_scanned_at.isoformat() if wifi_scanned_at else None,
        "last_push_at": last_push_at.isoformat() if last_push_at else None,
        "push_age_seconds": round(push_age_seconds, 1) if push_age_seconds is not None else None,
    })


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "gps_relay.main:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8090")),
        reload=False,
    )
