"""
GPS履歴をSQLiteに保存・取得するモジュール。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "gps_history.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS gps_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,  -- ISO 8601 UTC
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    alt       REAL,
    speed_kmh REAL,
    has_fix   INTEGER NOT NULL  -- 0=キャッシュ値, 1=リアルタイムfix
);
CREATE INDEX IF NOT EXISTS idx_recorded_at ON gps_log (recorded_at);

CREATE TABLE IF NOT EXISTS geolocation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT NOT NULL,  -- ISO 8601 UTC
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    accuracy_m   REAL,           -- Google APIが返す誤差半径（メートル）
    gps_lat      REAL,           -- 同時刻のGPS座標（比較用・nullの場合はGPS取得不可）
    gps_lon      REAL,
    distance_m   REAL            -- GPS座標との距離（メートル）
);
CREATE INDEX IF NOT EXISTS idx_geo_recorded_at ON geolocation_log (recorded_at);
"""


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(_CREATE_TABLE)


def insert(
    recorded_at: str,
    lat: float,
    lon: float,
    alt: float | None,
    speed_kmh: float | None,
    has_fix: bool,
) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO gps_log (recorded_at, lat, lon, alt, speed_kmh, has_fix) VALUES (?,?,?,?,?,?)",
            (recorded_at, lat, lon, alt, speed_kmh, 1 if has_fix else 0),
        )


def query(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲のGPS履歴を時刻昇順で返す。"""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT recorded_at, lat, lon, alt, speed_kmh, has_fix
            FROM gps_log
            WHERE recorded_at >= ? AND recorded_at <= ?
            ORDER BY recorded_at ASC
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def latest(n: int = 1) -> list[dict]:
    """最新n件を返す。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT recorded_at, lat, lon, alt, speed_kmh, has_fix FROM gps_log ORDER BY recorded_at DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_geolocation(
    recorded_at: str,
    lat: float,
    lon: float,
    accuracy_m: float | None,
    gps_lat: float | None,
    gps_lon: float | None,
    distance_m: float | None,
) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO geolocation_log
               (recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m)
               VALUES (?,?,?,?,?,?,?)""",
            (recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m),
        )


def query_geolocation(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲のGeolocation履歴を時刻昇順で返す。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m
               FROM geolocation_log
               WHERE recorded_at >= ? AND recorded_at <= ?
               ORDER BY recorded_at ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]
