"""
GPS履歴をPostgreSQLに保存・取得するモジュール。

接続先は環境変数 DATABASE_URL で指定する。
例: postgresql://user:pass@localhost:5432/car_logger
"""

import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ["DATABASE_URL"]
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, url)
    return _pool


@contextmanager
def _conn():
    pool = _get_pool()
    con = pool.getconn()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        pool.putconn(con)


def init_db() -> None:
    with _conn() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gps_log (
                id          SERIAL PRIMARY KEY,
                recorded_at TIMESTAMPTZ NOT NULL,
                lat         DOUBLE PRECISION NOT NULL,
                lon         DOUBLE PRECISION NOT NULL,
                alt         DOUBLE PRECISION,
                speed_kmh   DOUBLE PRECISION,
                has_fix     BOOLEAN NOT NULL,
                UNIQUE (recorded_at, lat, lon)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gps_recorded_at ON gps_log (recorded_at)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geolocation_log (
                id          SERIAL PRIMARY KEY,
                recorded_at TIMESTAMPTZ NOT NULL UNIQUE,
                lat         DOUBLE PRECISION NOT NULL,
                lon         DOUBLE PRECISION NOT NULL,
                accuracy_m  DOUBLE PRECISION,
                gps_lat     DOUBLE PRECISION,
                gps_lon     DOUBLE PRECISION,
                distance_m  DOUBLE PRECISION
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_recorded_at ON geolocation_log (recorded_at)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS camera_photos (
                id          SERIAL PRIMARY KEY,
                recorded_at TIMESTAMPTZ NOT NULL,
                lat         DOUBLE PRECISION,
                lon         DOUBLE PRECISION,
                alt         DOUBLE PRECISION,
                photo_path  TEXT NOT NULL UNIQUE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photo_recorded_at ON camera_photos (recorded_at)")


def _to_iso(dt: datetime | None) -> str | None:
    """datetimeをISO 8601文字列に変換する。"""
    if dt is None:
        return None
    return dt.isoformat()


def insert(
    recorded_at: str,
    lat: float,
    lon: float,
    alt: float | None,
    speed_kmh: float | None,
    has_fix: bool,
) -> None:
    with _conn() as con:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO gps_log (recorded_at, lat, lon, alt, speed_kmh, has_fix)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (recorded_at, lat, lon) DO NOTHING
            """,
            (recorded_at, lat, lon, alt, speed_kmh, has_fix),
        )


def query(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲のGPS履歴を時刻昇順で返す。"""
    with _conn() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT recorded_at, lat, lon, alt, speed_kmh, has_fix
            FROM gps_log
            WHERE recorded_at >= %s AND recorded_at <= %s
            ORDER BY recorded_at ASC
            """,
            (start.isoformat(), end.isoformat()),
        )
        rows = cur.fetchall()
    return [
        {**dict(r), "recorded_at": _to_iso(r["recorded_at"])}
        for r in rows
    ]


def latest(n: int = 1) -> list[dict]:
    """最新n件を返す。"""
    with _conn() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT recorded_at, lat, lon, alt, speed_kmh, has_fix FROM gps_log ORDER BY recorded_at DESC LIMIT %s",
            (n,),
        )
        rows = cur.fetchall()
    return [
        {**dict(r), "recorded_at": _to_iso(r["recorded_at"])}
        for r in rows
    ]


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
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO geolocation_log
                (recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (recorded_at) DO NOTHING
            """,
            (recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m),
        )


def query_geolocation(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲のGeolocation履歴を時刻昇順で返す。"""
    with _conn() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m
            FROM geolocation_log
            WHERE recorded_at >= %s AND recorded_at <= %s
            ORDER BY recorded_at ASC
            """,
            (start.isoformat(), end.isoformat()),
        )
        rows = cur.fetchall()
    return [
        {**dict(r), "recorded_at": _to_iso(r["recorded_at"])}
        for r in rows
    ]


def insert_photo(
    recorded_at: str,
    lat: float | None,
    lon: float | None,
    alt: float | None,
    photo_path: str,
) -> int:
    """カメラ写真を記録しIDを返す。"""
    with _conn() as con:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO camera_photos (recorded_at, lat, lon, alt, photo_path)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (photo_path) DO NOTHING
            RETURNING id
            """,
            (recorded_at, lat, lon, alt, photo_path),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        # 既に存在する場合はIDを取得して返す
        cur.execute("SELECT id FROM camera_photos WHERE photo_path = %s", (photo_path,))
        return cur.fetchone()[0]


def query_photos(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲のカメラ写真一覧を時刻昇順で返す。"""
    with _conn() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, recorded_at, lat, lon, alt
            FROM camera_photos
            WHERE recorded_at >= %s AND recorded_at <= %s
            ORDER BY recorded_at ASC
            """,
            (start.isoformat(), end.isoformat()),
        )
        rows = cur.fetchall()
    return [
        {**dict(r), "recorded_at": _to_iso(r["recorded_at"])}
        for r in rows
    ]


def get_photo_path(photo_id: int) -> str | None:
    """IDに対応するphoto_pathを返す。存在しない場合はNone。"""
    with _conn() as con:
        cur = con.cursor()
        cur.execute("SELECT photo_path FROM camera_photos WHERE id = %s", (photo_id,))
        row = cur.fetchone()
    return row[0] if row else None
