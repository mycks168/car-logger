"""
GPS監視メインループ。

動作概要:
  1. ラズパイの /gps エンドポイントをポーリングする
  2. GPS座標が取得できている間は最終既知位置を更新し続ける
  3. 以下のいずれかの場合にSlackへ通知する:
       a) ラズパイへ接続できない（オフライン）
       b) ラズパイは生きているがGPSが補足できない
  4. 重複通知防止:
       - 前回通知から NOTIFY_COOLDOWN_SECONDS 以内 かつ 移動距離が NOTIFY_MOVE_THRESHOLD_M 未満なら通知しない
       - 移動が検知された場合は即時通知する（トンネルを抜けた後に位置が変わるケースを想定）
  5. アラート状態から復帰した場合はSlackへ復帰通知を送る
"""

import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import NamedTuple

import httpx
from dotenv import load_dotenv

from gps_monitor import db
from gps_monitor.notify import send_alert, send_recovery
from gps_monitor.state import MonitorState, load_state, now_iso, parse_iso, save_state

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません")
    return value


# 設定
RASPI_GPS_URL = _require_env("RASPI_GPS_URL")           # 例: http://100.x.x.x:8080/gps
SLACK_WEBHOOK_URL = _require_env("SLACK_WEBHOOK_URL")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
NOTIFY_COOLDOWN_SECONDS = int(os.getenv("NOTIFY_COOLDOWN_SECONDS", str(30 * 60)))
NOTIFY_MOVE_THRESHOLD_M = float(os.getenv("NOTIFY_MOVE_THRESHOLD_M", "200"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
GOOGLE_GEOLOCATION_API_KEY = os.getenv("GOOGLE_GEOLOCATION_API_KEY", "")
GEOLOCATION_INTERVAL_SECONDS = int(os.getenv("GEOLOCATION_INTERVAL_SECONDS", "300"))

_GEOLOCATION_URL = "https://www.googleapis.com/geolocation/v1/geolocate"


class GpsResponse(NamedTuple):
    has_fix: bool
    gpsd_connected: bool
    lat: float | None
    lon: float | None
    last_fix_at: str | None
    wifi_aps: list[dict]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離をメートルで返す（ハーバーサイン公式）。"""
    r = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _fetch_gps() -> GpsResponse | None:
    """ラズパイからGPS情報を取得する。接続失敗時はNoneを返す。"""
    try:
        resp = httpx.get(RASPI_GPS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        return GpsResponse(
            has_fix=data.get("has_fix", False),
            gpsd_connected=data.get("gpsd_connected", False),
            lat=data.get("lat"),
            lon=data.get("lon"),
            last_fix_at=data.get("last_fix_at"),
            wifi_aps=data.get("wifi_aps", []),
        )
    except Exception as e:
        logger.warning("ラズパイへの接続に失敗しました: %s", e)
        return None


def _call_geolocation(
    wifi_aps: list[dict],
    gps_lat: float | None,
    gps_lon: float | None,
    recorded_at: str,
) -> None:
    """
    Google Geolocation APIを呼び出し、結果をDBに保存する。
    APIキーが未設定、またはWiFi APが0件の場合はスキップする。
    """
    if not GOOGLE_GEOLOCATION_API_KEY:
        logger.debug("GOOGLE_GEOLOCATION_API_KEY が未設定のためGeolocationをスキップします")
        return
    if not wifi_aps:
        logger.warning("WiFi APが0件のためGeolocationをスキップします")
        return

    try:
        resp = httpx.post(
            _GEOLOCATION_URL,
            params={"key": GOOGLE_GEOLOCATION_API_KEY},
            json={"wifiAccessPoints": wifi_aps},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        geo_lat = data["location"]["lat"]
        geo_lon = data["location"]["lng"]
        accuracy_m = data.get("accuracy")

        distance_m = None
        if gps_lat is not None and gps_lon is not None:
            distance_m = round(_haversine_m(gps_lat, gps_lon, geo_lat, geo_lon), 1)
            logger.info(
                "Geolocation取得: lat=%.6f, lon=%.6f, accuracy=%.0fm, GPS差=%.0fm",
                geo_lat, geo_lon, accuracy_m or 0, distance_m,
            )
        else:
            logger.info(
                "Geolocation取得(GPS無し): lat=%.6f, lon=%.6f, accuracy=%.0fm",
                geo_lat, geo_lon, accuracy_m or 0,
            )

        db.insert_geolocation(
            recorded_at=recorded_at,
            lat=geo_lat,
            lon=geo_lon,
            accuracy_m=accuracy_m,
            gps_lat=gps_lat,
            gps_lon=gps_lon,
            distance_m=distance_m,
        )
    except Exception as e:
        logger.error("Geolocation API呼び出し失敗: %s", e)


def _should_notify(state: MonitorState, lat: float, lon: float) -> tuple[bool, str]:
    """
    通知すべきかを判断する。

    Returns:
        (通知すべきか, 理由)
    """
    if state.last_notified_at is None:
        return True, "初回アラート"

    # 移動距離チェック（前回通知位置との比較）
    if state.last_notified_lat is not None and state.last_notified_lon is not None:
        dist = _haversine_m(state.last_notified_lat, state.last_notified_lon, lat, lon)
        if dist >= NOTIFY_MOVE_THRESHOLD_M:
            return True, f"前回通知から {dist:.0f}m 移動を検知"

    # クールダウンチェック
    elapsed = (datetime.now(timezone.utc) - parse_iso(state.last_notified_at)).total_seconds()
    if elapsed >= NOTIFY_COOLDOWN_SECONDS:
        return True, f"前回通知から {elapsed / 60:.0f} 分経過"

    return False, f"クールダウン中（残り {(NOTIFY_COOLDOWN_SECONDS - elapsed) / 60:.0f} 分）"


def _run_once(state: MonitorState) -> MonitorState:
    """1回のポーリングサイクルを実行し、更新されたstateを返す。"""
    gps = _fetch_gps()

    # ラズパイがオフラインの場合
    if gps is None:
        logger.warning("ラズパイがオフラインです")
        if state.last_known_lat is None:
            logger.warning("最終既知位置がないため通知をスキップします")
            return state

        should, reason = _should_notify(state, state.last_known_lat, state.last_known_lon)
        if should:
            ok = send_alert(
                SLACK_WEBHOOK_URL,
                reason=f"ラズパイへの接続が失われました（{reason}）",
                lat=state.last_known_lat,
                lon=state.last_known_lon,
                last_known_at=state.last_known_at or "不明",
            )
            if ok:
                state.last_notified_at = now_iso()
                state.last_notified_lat = state.last_known_lat
                state.last_notified_lon = state.last_known_lon
        else:
            logger.info("通知をスキップ: %s", reason)

        state.is_alerting = True
        return state

    # GPS座標が取れている場合（has_fix または キャッシュ座標）
    available_lat = gps.lat
    available_lon = gps.lon

    if available_lat is not None and available_lon is not None:
        # 最終既知位置を更新
        state.last_known_lat = available_lat
        state.last_known_lon = available_lon
        state.last_known_at = gps.last_fix_at or now_iso()

    # 5分ごとにGeolocation APIを呼び出す（GPS取得成功・失敗問わず）
    now = datetime.now(timezone.utc)
    geo_elapsed = (
        (now - parse_iso(state.last_geolocation_at)).total_seconds()
        if state.last_geolocation_at else GEOLOCATION_INTERVAL_SECONDS
    )
    if geo_elapsed >= GEOLOCATION_INTERVAL_SECONDS and gps is not None:
        _call_geolocation(
            wifi_aps=gps.wifi_aps,
            gps_lat=available_lat,
            gps_lon=available_lon,
            recorded_at=now.isoformat(),
        )
        state.last_geolocation_at = now.isoformat()

    if gps.has_fix:
        logger.info("GPS取得成功: lat=%.6f, lon=%.6f", available_lat, available_lon)
        db.insert(
            recorded_at=state.last_known_at or now_iso(),
            lat=available_lat,
            lon=available_lon,
            alt=None,
            speed_kmh=None,
            has_fix=True,
        )
        # アラートから復帰した場合
        if state.is_alerting:
            send_recovery(SLACK_WEBHOOK_URL, available_lat, available_lon)
            state.is_alerting = False
            state.last_notified_at = None
            state.last_notified_lat = None
            state.last_notified_lon = None
        return state

    # GPS補足不可（ラズパイは生きている）
    logger.warning("GPS補足不可 (gpsd_connected=%s)", gps.gpsd_connected)
    if state.last_known_lat is None:
        logger.warning("最終既知位置がないため通知をスキップします")
        state.is_alerting = True
        return state

    should, reason = _should_notify(state, state.last_known_lat, state.last_known_lon)
    if should:
        detail = "GPS信号が失われました" if gps.gpsd_connected else "gpsdとの接続が失われました"
        ok = send_alert(
            SLACK_WEBHOOK_URL,
            reason=f"{detail}（{reason}）",
            lat=state.last_known_lat,
            lon=state.last_known_lon,
            last_known_at=state.last_known_at or "不明",
        )
        if ok:
            state.last_notified_at = now_iso()
            state.last_notified_lat = state.last_known_lat
            state.last_notified_lon = state.last_known_lon
    else:
        logger.info("通知をスキップ: %s", reason)

    state.is_alerting = True
    return state


def main() -> None:
    db.init_db()
    logger.info(
        "GPS監視を開始します (ポーリング間隔: %d秒, クールダウン: %d秒, 移動閾値: %.0fm)",
        POLL_INTERVAL_SECONDS,
        NOTIFY_COOLDOWN_SECONDS,
        NOTIFY_MOVE_THRESHOLD_M,
    )
    while True:
        state = load_state()
        state = _run_once(state)
        save_state(state)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
