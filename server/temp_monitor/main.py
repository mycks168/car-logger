"""
温度監視メインループ。

ラズパイの /temperatures エンドポイントを定期ポーリングし、
全センサーの温度をSQLiteに保存する。
"""

import json
import logging
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from temp_monitor import db

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


RASPI_BASE_URL = _require_env("RASPI_BASE_URL")   # 例: http://100.x.x.x:8080
TEMP_POLL_INTERVAL_SECONDS = int(os.getenv("TEMP_POLL_INTERVAL_SECONDS", str(5 * 60)))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))

_TEMP_URL = RASPI_BASE_URL.rstrip("/") + "/temperatures"


def _fetch_temperatures() -> list[dict] | None:
    """ラズパイから全センサーの温度を取得する。失敗時はNoneを返す。"""
    try:
        resp = httpx.get(_TEMP_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        return data.get("sensors", []), data.get("read_at")
    except Exception as e:
        logger.warning("温度の取得に失敗しました: %s", e)
        return None


def _run_once() -> None:
    result = _fetch_temperatures()
    if result is None:
        return

    sensors, read_at = result
    readings = [
        (s["id"], s["temperature_c"])
        for s in sensors
        if s["temperature_c"] is not None
    ]

    if not readings:
        logger.warning("有効な温度データがありませんでした")
        return

    db.insert_many(read_at, readings)
    summary = ", ".join(f"{sid.split('-')[-1][:6]}={t}°C" for sid, t in readings)
    logger.info("温度を記録しました (%d件): %s", len(readings), summary)


def main() -> None:
    db.init_db()
    logger.info(
        "温度監視を開始します (ポーリング間隔: %d秒)",
        TEMP_POLL_INTERVAL_SECONDS,
    )
    while True:
        _run_once()
        time.sleep(TEMP_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
