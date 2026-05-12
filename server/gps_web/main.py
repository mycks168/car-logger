"""
GPS軌跡表示WebUI。
日時範囲を指定してSQLiteからGPS履歴を取得し、Leaflet.jsで地図表示する。
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gps_monitor import db

load_dotenv()

app = FastAPI(title="GPS Track Viewer")

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_TEMPLATES_DIR = Path(__file__).parent / "templates"

WEB_PORT = int(os.getenv("WEB_PORT", "8081"))


@app.on_event("startup")
def startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/track")
def get_track(
    start: str = Query(description="開始日時 (ISO 8601 / JST)"),
    end: str = Query(description="終了日時 (ISO 8601 / JST)"),
) -> JSONResponse:
    """
    指定した日時範囲のGPS軌跡を返す。

    日時はブラウザのローカル時刻（JST）で受け取り、UTC変換してDBを検索する。
    """
    try:
        # datetime-local の値は "2026-05-12T10:00" 形式で来る（タイムゾーンなし=JST扱い）
        jst = timezone(timedelta(hours=9))
        start_dt = datetime.fromisoformat(start).replace(tzinfo=jst).astimezone(timezone.utc)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=jst).astimezone(timezone.utc)
    except ValueError as e:
        return JSONResponse({"error": f"日時の形式が不正です: {e}"}, status_code=400)

    rows = db.query(start_dt, end_dt)

    # Leaflet.js 用に [[lat, lon], ...] の配列と詳細情報を返す
    points = [
        {
            "lat": r["lat"],
            "lon": r["lon"],
            "alt": r["alt"],
            "speed_kmh": r["speed_kmh"],
            "has_fix": bool(r["has_fix"]),
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]
    return JSONResponse({"count": len(points), "points": points})


@app.get("/api/latest")
def get_latest() -> JSONResponse:
    """最新の1件を返す（地図の初期表示位置に使う）。"""
    rows = db.latest(1)
    if not rows:
        return JSONResponse({"point": None})
    return JSONResponse({"point": rows[0]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gps_web.main:app", host="0.0.0.0", port=WEB_PORT, reload=False)
