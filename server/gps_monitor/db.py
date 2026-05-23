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

CREATE TABLE IF NOT EXISTS camera_photos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at      TEXT NOT NULL,  -- ISO 8601 UTC
    lat              REAL,           -- 撮影時のGPS緯度（取得できない場合はNULL）
    lon              REAL,
    alt              REAL,
    photo_path       TEXT NOT NULL,  -- server/data/photos/ 以下の相対パス
    person_detected  INTEGER DEFAULT NULL,  -- NULL=未検知, 0=人物なし, 1=人物あり
    is_family        INTEGER DEFAULT NULL   -- NULL=未判定, 0=不明人物, 1=家族
);
CREATE INDEX IF NOT EXISTS idx_photo_recorded_at ON camera_photos (recorded_at);

CREATE TABLE IF NOT EXISTS family_faces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,  -- ISO 8601 UTC
    photo_id    INTEGER,        -- 元写真ID（NULL=手動追加）
    embedding   BLOB NOT NULL,  -- 顔埋め込みベクトル（numpy配列をbytesでシリアライズ）
    label       TEXT,           -- 誰の顔か（family_members.name と対応）
    note        TEXT
);

CREATE TABLE IF NOT EXISTS family_members (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE  -- 例: "私", "妻", "息子", "娘"
);
"""

# 既存DBへのカラム追加（既に存在する場合はスキップ）
_MIGRATIONS = [
    "ALTER TABLE camera_photos ADD COLUMN person_detected INTEGER DEFAULT NULL",
    "ALTER TABLE camera_photos ADD COLUMN is_family INTEGER DEFAULT NULL",
    "ALTER TABLE camera_photos ADD COLUMN family_label TEXT DEFAULT NULL",
    "ALTER TABLE family_faces ADD COLUMN label TEXT DEFAULT NULL",
]


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
        for sql in _MIGRATIONS:
            try:
                con.execute(sql)
            except sqlite3.OperationalError:
                pass  # カラムが既に存在する場合はスキップ


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


def insert_photo(
    recorded_at: str,
    lat: float | None,
    lon: float | None,
    alt: float | None,
    photo_path: str,
) -> int:
    """カメラ写真を記録しIDを返す。"""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO camera_photos (recorded_at, lat, lon, alt, photo_path) VALUES (?,?,?,?,?)",
            (recorded_at, lat, lon, alt, photo_path),
        )
        return cur.lastrowid


def query_photos(start: datetime, end: datetime) -> list[dict]:
    """指定した日時範囲のカメラ写真一覧を時刻昇順で返す。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT id, recorded_at, lat, lon, alt
               FROM camera_photos
               WHERE recorded_at >= ? AND recorded_at <= ?
               ORDER BY recorded_at ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def get_photo_path(photo_id: int) -> str | None:
    """IDに対応するphoto_pathを返す。存在しない場合はNone。"""
    with _conn() as con:
        row = con.execute(
            "SELECT photo_path FROM camera_photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    return row["photo_path"] if row else None


def update_photo_person_info(
    photo_id: int,
    person_detected: bool,
    is_family: bool,
    family_label: str | None = None,
) -> None:
    """人物検知結果をcamera_photosに記録する。"""
    with _conn() as con:
        con.execute(
            "UPDATE camera_photos SET person_detected=?, is_family=?, family_label=? WHERE id=?",
            (1 if person_detected else 0, 1 if is_family else 0, family_label, photo_id),
        )


def insert_family_face(
    created_at: str,
    photo_id: int | None,
    embedding: bytes,
    label: str | None = None,
    note: str | None = None,
) -> int:
    """家族の顔埋め込みを登録しIDを返す。"""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO family_faces (created_at, photo_id, embedding, label, note) VALUES (?,?,?,?,?)",
            (created_at, photo_id, embedding, label, note),
        )
        return cur.lastrowid


def list_family_embeddings() -> list[dict]:
    """登録済みの家族顔埋め込み一覧を返す（embedding と label を含む）。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT embedding, label FROM family_faces ORDER BY created_at ASC",
        ).fetchall()
    return [dict(r) for r in rows]


# ---- 家族メンバー管理 ----

def list_family_members() -> list[dict]:
    """登録済み家族メンバーの一覧を返す。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT id, name FROM family_members ORDER BY id ASC",
        ).fetchall()
    return [dict(r) for r in rows]


def insert_family_member(name: str) -> int:
    """家族メンバーを追加しIDを返す。同名が既にある場合は IntegrityError が発生する。"""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO family_members (name) VALUES (?)",
            (name,),
        )
        return cur.lastrowid


def delete_family_member(member_id: int) -> None:
    """家族メンバーを削除する。"""
    with _conn() as con:
        con.execute("DELETE FROM family_members WHERE id=?", (member_id,))
