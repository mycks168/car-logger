"""
温度履歴をSQLiteに保存・取得するモジュール。
GPS履歴とは別のDBファイルに保存する。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "temp_history.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS temp_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,  -- ISO 8601 UTC
    sensor_id   TEXT NOT NULL,
    temp_c      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_temp_recorded_at ON temp_log (recorded_at);
CREATE INDEX IF NOT EXISTS idx_temp_sensor      ON temp_log (sensor_id, recorded_at);
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


def insert_many(recorded_at: str, readings: list[tuple[str, float]]) -> None:
    """複数センサーの読み取り値を一括挿入する。readings は [(sensor_id, temp_c), ...]"""
    with _conn() as con:
        con.executemany(
            "INSERT INTO temp_log (recorded_at, sensor_id, temp_c) VALUES (?,?,?)",
            [(recorded_at, sid, temp) for sid, temp in readings],
        )


def query(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲の温度履歴を時刻昇順で返す。"""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT recorded_at, sensor_id, temp_c
            FROM temp_log
            WHERE recorded_at >= ? AND recorded_at <= ?
            ORDER BY recorded_at ASC
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def sensor_ids() -> list[str]:
    """DBに存在するセンサーIDの一覧を返す。"""
    with _conn() as con:
        rows = con.execute("SELECT DISTINCT sensor_id FROM temp_log ORDER BY sensor_id").fetchall()
    return [r["sensor_id"] for r in rows]
