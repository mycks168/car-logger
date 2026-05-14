"""
GPSサーバ - ラズパイ上で動作し、gpsdからGPS座標を取得してHTTP APIで提供する。
WiFiスキャン結果もキャッシュして /gps レスポンスに含める（Google Geolocation API用）。
"""

import logging
import os
import re
import subprocess
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
CACHE_MAX_AGE_SECONDS = int(os.getenv("CACHE_MAX_AGE_SECONDS", str(60 * 60 * 24)))
WIFI_IFACE = os.getenv("WIFI_IFACE", "wlan0")
# サーバ側のGeolocation呼び出し間隔に合わせて同じ周期でスキャンする
WIFI_SCAN_INTERVAL_SECONDS = int(os.getenv("WIFI_SCAN_INTERVAL_SECONDS", "300"))


@dataclass
class GpsState:
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    speed: float | None = None
    has_fix: bool = False
    last_fix_at: datetime | None = None
    gpsd_connected: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class WifiState:
    aps: list[dict] = field(default_factory=list)
    scanned_at: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


gps_state = GpsState()
wifi_state = WifiState()
app = FastAPI(title="GPS Server")


def _gpsd_watcher() -> None:
    """バックグラウンドスレッドでgpsdを監視し、gps_stateを更新する。"""
    while True:
        try:
            gps_socket = agps3.GPSDSocket()
            data_stream = agps3.DataStream()
            gps_socket.connect(GPSD_HOST, GPSD_PORT)
            gps_socket.watch()

            with gps_state.lock:
                gps_state.gpsd_connected = True
            logger.info("gpsd に接続しました")

            for new_data in gps_socket:
                if new_data:
                    data_stream.unpack(new_data)
                    lat = getattr(data_stream, "lat", "n/a")
                    lon = getattr(data_stream, "lon", "n/a")
                    mode = getattr(data_stream, "mode", 0)

                    with gps_state.lock:
                        if mode and mode not in ("n/a", 0, 1) and lat not in ("n/a", None) and lon not in ("n/a", None):
                            gps_state.lat = float(lat)
                            gps_state.lon = float(lon)
                            raw_alt = getattr(data_stream, "alt", "n/a")
                            gps_state.alt = float(raw_alt) if raw_alt not in ("n/a", None) else None
                            raw_speed = getattr(data_stream, "speed", "n/a")
                            gps_state.speed = float(raw_speed) if raw_speed not in ("n/a", None) else None
                            gps_state.has_fix = True
                            gps_state.last_fix_at = datetime.now(timezone.utc)
                        else:
                            gps_state.has_fix = False

        except Exception as e:
            logger.warning("gpsd 接続エラー: %s。5秒後に再接続します。", e)
            with gps_state.lock:
                gps_state.gpsd_connected = False
                gps_state.has_fix = False
            time.sleep(5)


def _do_wifi_scan() -> list[dict]:
    """iwlist でWiFiスキャンを実行し、APリストを返す。"""
    try:
        result = subprocess.run(
            ["sudo", "iwlist", WIFI_IFACE, "scan"],
            capture_output=True, text=True, timeout=15,
        )
        aps = []
        current_mac = None
        for line in result.stdout.splitlines():
            mac_m = re.search(r"Address: ([0-9A-Fa-f:]{17})", line)
            if mac_m:
                current_mac = mac_m.group(1).upper()
            sig_m = re.search(r"Signal level=(-\d+)", line)
            if sig_m and current_mac:
                aps.append({
                    "macAddress": current_mac,
                    "signalStrength": int(sig_m.group(1)),
                })
                current_mac = None
        logger.info("WiFiスキャン完了: %d APを検出", len(aps))
        return aps
    except Exception as e:
        logger.warning("WiFiスキャン失敗: %s", e)
        return []


def _wifi_scanner() -> None:
    """バックグラウンドスレッドで定期的にWiFiスキャンを行い、wifi_stateを更新する。"""
    while True:
        aps = _do_wifi_scan()
        with wifi_state.lock:
            wifi_state.aps = aps
            wifi_state.scanned_at = datetime.now(timezone.utc)
        time.sleep(WIFI_SCAN_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event() -> None:
    threading.Thread(target=_gpsd_watcher, daemon=True).start()
    threading.Thread(target=_wifi_scanner, daemon=True).start()
    logger.info("GPS・WiFi監視スレッドを開始しました")


@app.get("/gps")
def get_gps() -> JSONResponse:
    """
    現在のGPS状態を返す。wifi_aps にはキャッシュされたWiFiスキャン結果を含める。

    - has_fix=true: 現在GPS補足中
    - has_fix=false, last_fix_at あり: GPS補足不可だが最終既知位置あり
    - has_fix=false, last_fix_at なし: 一度もGPSを補足できていない
    """
    with gps_state.lock:
        gpsd_connected = gps_state.gpsd_connected
        has_fix = gps_state.has_fix
        lat = gps_state.lat
        lon = gps_state.lon
        alt = gps_state.alt
        speed = gps_state.speed
        last_fix_at = gps_state.last_fix_at

    with wifi_state.lock:
        wifi_aps = list(wifi_state.aps)
        wifi_scanned_at = wifi_state.scanned_at

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
        "wifi_aps": wifi_aps,
        "wifi_scanned_at": wifi_scanned_at.isoformat() if wifi_scanned_at else None,
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

    # CPU温度
    try:
        cpu_raw = int(open("/sys/class/thermal/thermal_zone0/temp").read().strip())
        sensors.append({"id": "cpu", "temperature_c": round(cpu_raw / 1000, 2), "error": None})
    except Exception as e:
        sensors.append({"id": "cpu", "temperature_c": None, "error": str(e)})

    # DS18B20センサー
    for path in sorted(sensor_dirs):
        sensor_id = path.split("/")[-1]
        slave_file = f"{path}/w1_slave"
        try:
            raw = open(slave_file).read()
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
