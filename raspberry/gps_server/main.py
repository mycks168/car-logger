"""
GPSサーバ - ラズパイ上で動作し、gpsdからGPS座標を取得してHTTP APIで提供する。
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from gps3 import agps3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# 設定
GPSD_HOST = os.getenv("GPSD_HOST", "localhost")
GPSD_PORT = int(os.getenv("GPSD_PORT", "2947"))
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8080"))
# GPS未取得が続いた場合に古いキャッシュを使い続けない上限（秒）
CACHE_MAX_AGE_SECONDS = int(os.getenv("CACHE_MAX_AGE_SECONDS", str(60 * 60 * 24)))


@dataclass
class GpsState:
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    speed: float | None = None
    # GPS衛星を補足できているか
    has_fix: bool = False
    # 最後にfixできた時刻
    last_fix_at: datetime | None = None
    # gpsdとの接続状態
    gpsd_connected: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


state = GpsState()
app = FastAPI(title="GPS Server")


def _gpsd_watcher() -> None:
    """バックグラウンドスレッドでgpsdを監視し、stateを更新する。"""
    while True:
        try:
            gps_socket = agps3.GPSDSocket()
            data_stream = agps3.DataStream()
            gps_socket.connect(GPSD_HOST, GPSD_PORT)
            gps_socket.watch()

            with state.lock:
                state.gpsd_connected = True
            logger.info("gpsd に接続しました")

            for new_data in gps_socket:
                if new_data:
                    data_stream.unpack(new_data)
                    lat = getattr(data_stream, "lat", "n/a")
                    lon = getattr(data_stream, "lon", "n/a")
                    mode = getattr(data_stream, "mode", 0)

                    with state.lock:
                        # mode 2=2Dfix, 3=3Dfix
                        if mode and mode not in ("n/a", 0, 1) and lat not in ("n/a", None) and lon not in ("n/a", None):
                            state.lat = float(lat)
                            state.lon = float(lon)
                            raw_alt = getattr(data_stream, "alt", "n/a")
                            state.alt = float(raw_alt) if raw_alt not in ("n/a", None) else None
                            raw_speed = getattr(data_stream, "speed", "n/a")
                            state.speed = float(raw_speed) if raw_speed not in ("n/a", None) else None
                            state.has_fix = True
                            state.last_fix_at = datetime.now(timezone.utc)
                        else:
                            state.has_fix = False

        except Exception as e:
            logger.warning("gpsd 接続エラー: %s。5秒後に再接続します。", e)
            with state.lock:
                state.gpsd_connected = False
                state.has_fix = False
            time.sleep(5)


@app.on_event("startup")
def startup_event() -> None:
    thread = threading.Thread(target=_gpsd_watcher, daemon=True)
    thread.start()
    logger.info("GPS 監視スレッドを開始しました")


@app.get("/gps")
def get_gps() -> JSONResponse:
    """
    現在のGPS状態を返す。

    - has_fix=true: 現在GPS補足中
    - has_fix=false, last_fix_at あり: GPS補足不可だが最終既知位置あり
    - has_fix=false, last_fix_at なし: 一度もGPSを補足できていない
    """
    with state.lock:
        gpsd_connected = state.gpsd_connected
        has_fix = state.has_fix
        lat = state.lat
        lon = state.lon
        alt = state.alt
        speed = state.speed
        last_fix_at = state.last_fix_at

    # キャッシュが古すぎる場合は「位置情報なし」と同様に扱う
    cache_age_seconds: float | None = None
    if last_fix_at is not None:
        cache_age_seconds = (datetime.now(timezone.utc) - last_fix_at).total_seconds()
        if cache_age_seconds > CACHE_MAX_AGE_SECONDS:
            logger.warning("GPS キャッシュが %d 秒以上古いため無効化します", CACHE_MAX_AGE_SECONDS)
            lat = None
            lon = None

    return JSONResponse({
        "has_fix": has_fix,
        "gpsd_connected": gpsd_connected,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "speed_kmh": round(speed * 3.6, 1) if speed is not None else None,
        "last_fix_at": last_fix_at.isoformat() if last_fix_at else None,
        "cache_age_seconds": round(cache_age_seconds, 1) if cache_age_seconds is not None else None,
    })


@app.get("/temperatures")
def get_temperatures() -> JSONResponse:
    """
    接続されている全DS18B20センサーの温度を返す。

    /sys/bus/w1/devices/28-*/w1_slave を読んで値を取得する。
    読み取りに失敗したセンサーは error フィールドを返す。
    """
    import glob

    W1_BASE = "/sys/bus/w1/devices"
    sensor_dirs = glob.glob(f"{W1_BASE}/28-*")

    sensors = []
    for path in sorted(sensor_dirs):
        sensor_id = path.split("/")[-1]
        slave_file = f"{path}/w1_slave"
        try:
            raw = open(slave_file).read()
            # w1_slave の形式:
            # 50 05 4b 46 7f ff 0c 10 1c : crc=1c YES
            # 50 05 4b 46 7f ff 0c 10 1c t=21250
            if "YES" not in raw:
                sensors.append({"id": sensor_id, "temperature_c": None, "error": "CRC error"})
                continue
            t_line = [l for l in raw.splitlines() if "t=" in l]
            if not t_line:
                sensors.append({"id": sensor_id, "temperature_c": None, "error": "t= not found"})
                continue
            temp_raw = int(t_line[0].split("t=")[1].strip())
            sensors.append({"id": sensor_id, "temperature_c": round(temp_raw / 1000, 2), "error": None})
        except Exception as e:
            sensors.append({"id": sensor_id, "temperature_c": None, "error": str(e)})

    return JSONResponse({
        "sensors": sensors,
        "read_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gps_server.main:app", host=API_HOST, port=API_PORT, reload=False)
