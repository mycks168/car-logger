#!/usr/bin/env python3
"""
SQLite → PostgreSQL マイグレーションツール。

複数の SQLite ファイル（worktree で分断されたDBなど）を指定し、
PostgreSQL に統合する。重複データはスキップ（ON CONFLICT DO NOTHING）。

使用例:
    uv run tools/migrate_sqlite_to_pg.py \\
        server/data/gps_history.db \\
        ../car-logger-ai/server/data/gps_history.db

    # 温度DBも含める場合
    uv run tools/migrate_sqlite_to_pg.py \\
        server/data/gps_history.db \\
        server/data/temp_history.db \\
        ../car-logger-ai/server/data/gps_history.db \\
        ../car-logger-ai/server/data/temp_history.db

事前準備:
    1. server/.env に DATABASE_URL を設定
    2. PostgreSQL にデータベースを作成 (createdb car_logger など)
    3. アプリを一度起動してテーブルを作成するか、--init-schema オプションを使う
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# server/ ディレクトリを sys.path に追加してアプリモジュールを使えるようにする
_REPO_ROOT = Path(__file__).parent.parent
_SERVER_DIR = _REPO_ROOT / "server"
sys.path.insert(0, str(_SERVER_DIR))

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(_SERVER_DIR / ".env")


def _pg_conn(url: str):
    return psycopg2.connect(url)


def _sqlite_conn(path: Path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


# ---------- テーブル作成 ----------

_GPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS gps_log (
    id          SERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    alt         DOUBLE PRECISION,
    speed_kmh   DOUBLE PRECISION,
    has_fix     BOOLEAN NOT NULL,
    UNIQUE (recorded_at, lat, lon)
);
CREATE INDEX IF NOT EXISTS idx_gps_recorded_at ON gps_log (recorded_at);

CREATE TABLE IF NOT EXISTS geolocation_log (
    id          SERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL UNIQUE,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    accuracy_m  DOUBLE PRECISION,
    gps_lat     DOUBLE PRECISION,
    gps_lon     DOUBLE PRECISION,
    distance_m  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_geo_recorded_at ON geolocation_log (recorded_at);

CREATE TABLE IF NOT EXISTS camera_photos (
    id          SERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    alt         DOUBLE PRECISION,
    photo_path  TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_photo_recorded_at ON camera_photos (recorded_at);
"""

_TEMP_SCHEMA = """
CREATE TABLE IF NOT EXISTS temp_log (
    id          SERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL,
    sensor_id   TEXT NOT NULL,
    temp_c      DOUBLE PRECISION NOT NULL,
    UNIQUE (recorded_at, sensor_id)
);
CREATE INDEX IF NOT EXISTS idx_temp_recorded_at ON temp_log (recorded_at);
CREATE INDEX IF NOT EXISTS idx_temp_sensor ON temp_log (sensor_id, recorded_at);
"""


def init_schema(pg: psycopg2.extensions.connection) -> None:
    """PostgreSQL にスキーマを作成する。"""
    with pg.cursor() as cur:
        for stmt in _GPS_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        for stmt in _TEMP_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    pg.commit()
    print("スキーマを作成しました。")


# ---------- GPS DB 移行 ----------

def _has_table(sqlite: sqlite3.Connection, table: str) -> bool:
    row = sqlite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate_gps_log(sqlite: sqlite3.Connection, pg: psycopg2.extensions.connection) -> dict:
    if not _has_table(sqlite, "gps_log"):
        return {"gps_log": {"total": 0, "inserted": 0, "skipped": 0}}

    rows = sqlite.execute(
        "SELECT recorded_at, lat, lon, alt, speed_kmh, has_fix FROM gps_log ORDER BY recorded_at"
    ).fetchall()

    inserted = skipped = 0
    with pg.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO gps_log (recorded_at, lat, lon, alt, speed_kmh, has_fix)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (recorded_at, lat, lon) DO NOTHING
                """,
                (r["recorded_at"], r["lat"], r["lon"], r["alt"],
                 r["speed_kmh"], bool(r["has_fix"])),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    return {"gps_log": {"total": len(rows), "inserted": inserted, "skipped": skipped}}


def migrate_geolocation_log(sqlite: sqlite3.Connection, pg: psycopg2.extensions.connection) -> dict:
    if not _has_table(sqlite, "geolocation_log"):
        return {"geolocation_log": {"total": 0, "inserted": 0, "skipped": 0}}

    rows = sqlite.execute(
        "SELECT recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m FROM geolocation_log ORDER BY recorded_at"
    ).fetchall()

    inserted = skipped = 0
    with pg.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO geolocation_log
                    (recorded_at, lat, lon, accuracy_m, gps_lat, gps_lon, distance_m)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (recorded_at) DO NOTHING
                """,
                (r["recorded_at"], r["lat"], r["lon"], r["accuracy_m"],
                 r["gps_lat"], r["gps_lon"], r["distance_m"]),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    return {"geolocation_log": {"total": len(rows), "inserted": inserted, "skipped": skipped}}


def migrate_camera_photos(sqlite: sqlite3.Connection, pg: psycopg2.extensions.connection) -> dict:
    if not _has_table(sqlite, "camera_photos"):
        return {"camera_photos": {"total": 0, "inserted": 0, "skipped": 0}}

    rows = sqlite.execute(
        "SELECT recorded_at, lat, lon, alt, photo_path FROM camera_photos ORDER BY recorded_at"
    ).fetchall()

    inserted = skipped = 0
    with pg.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO camera_photos (recorded_at, lat, lon, alt, photo_path)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (photo_path) DO NOTHING
                """,
                (r["recorded_at"], r["lat"], r["lon"], r["alt"], r["photo_path"]),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    return {"camera_photos": {"total": len(rows), "inserted": inserted, "skipped": skipped}}


def migrate_temp_log(sqlite: sqlite3.Connection, pg: psycopg2.extensions.connection) -> dict:
    if not _has_table(sqlite, "temp_log"):
        return {"temp_log": {"total": 0, "inserted": 0, "skipped": 0}}

    rows = sqlite.execute(
        "SELECT recorded_at, sensor_id, temp_c FROM temp_log ORDER BY recorded_at"
    ).fetchall()

    inserted = skipped = 0
    with pg.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO temp_log (recorded_at, sensor_id, temp_c)
                VALUES (%s, %s, %s)
                ON CONFLICT (recorded_at, sensor_id) DO NOTHING
                """,
                (r["recorded_at"], r["sensor_id"], r["temp_c"]),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    return {"temp_log": {"total": len(rows), "inserted": inserted, "skipped": skipped}}


# ---------- メイン ----------

def migrate_file(sqlite_path: Path, pg: psycopg2.extensions.connection) -> None:
    print(f"\n--- {sqlite_path} ---")
    sqlite = _sqlite_conn(sqlite_path)
    try:
        tables = {
            r[0]
            for r in sqlite.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        print(f"  テーブル: {', '.join(sorted(tables)) or '(なし)'}")

        stats: dict[str, dict] = {}
        if "gps_log" in tables or "geolocation_log" in tables or "camera_photos" in tables:
            stats.update(migrate_gps_log(sqlite, pg))
            stats.update(migrate_geolocation_log(sqlite, pg))
            stats.update(migrate_camera_photos(sqlite, pg))
        if "temp_log" in tables:
            stats.update(migrate_temp_log(sqlite, pg))

        for table, s in stats.items():
            if s["total"]:
                print(
                    f"  {table}: 合計={s['total']}, "
                    f"挿入={s['inserted']}, スキップ={s['skipped']}"
                )
    finally:
        sqlite.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SQLite → PostgreSQL マイグレーションツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "sqlite_files",
        nargs="+",
        metavar="SQLITE_FILE",
        help="移行元の SQLite ファイル（複数指定可）",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="PostgreSQL 接続URL（省略時は DATABASE_URL 環境変数を使用）",
    )
    parser.add_argument(
        "--init-schema",
        action="store_true",
        help="移行前に PostgreSQL のスキーマを作成する",
    )
    args = parser.parse_args()

    if not args.database_url:
        print(
            "エラー: DATABASE_URL が設定されていません。\n"
            "--database-url オプションか、環境変数 DATABASE_URL を設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"PostgreSQL に接続: {args.database_url.split('@')[-1]}")  # パスワードを隠して表示
    pg = _pg_conn(args.database_url)

    try:
        if args.init_schema:
            init_schema(pg)

        for path_str in args.sqlite_files:
            path = Path(path_str)
            if not path.exists():
                print(f"警告: {path} が見つかりません。スキップします。", file=sys.stderr)
                continue
            migrate_file(path, pg)

        print("\n移行完了。")
    finally:
        pg.close()


if __name__ == "__main__":
    main()
