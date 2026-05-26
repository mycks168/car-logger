"""
温度履歴をPostgreSQLに保存・取得するモジュール。

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


def _to_iso(dt: datetime | None) -> str | None:
    """datetimeをISO 8601文字列に変換する。"""
    if dt is None:
        return None
    return dt.isoformat()


def init_db() -> None:
    with _conn() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS temp_log (
                id          SERIAL PRIMARY KEY,
                recorded_at TIMESTAMPTZ NOT NULL,
                sensor_id   TEXT NOT NULL,
                temp_c      DOUBLE PRECISION NOT NULL,
                UNIQUE (recorded_at, sensor_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_temp_recorded_at ON temp_log (recorded_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_temp_sensor ON temp_log (sensor_id, recorded_at)")


def insert_many(recorded_at: str, readings: list[tuple[str, float]]) -> None:
    """複数センサーの読み取り値を一括挿入する。readings は [(sensor_id, temp_c), ...]"""
    with _conn() as con:
        cur = con.cursor()
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO temp_log (recorded_at, sensor_id, temp_c)
            VALUES %s
            ON CONFLICT (recorded_at, sensor_id) DO NOTHING
            """,
            [(recorded_at, sid, temp) for sid, temp in readings],
        )


def query(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲の温度履歴を時刻昇順で返す。"""
    with _conn() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT recorded_at, sensor_id, temp_c
            FROM temp_log
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


def sensor_ids() -> list[str]:
    """DBに存在するセンサーIDの一覧を返す。"""
    with _conn() as con:
        cur = con.cursor()
        cur.execute("SELECT DISTINCT sensor_id FROM temp_log ORDER BY sensor_id")
        rows = cur.fetchall()
    return [r[0] for r in rows]
