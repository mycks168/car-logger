"""
GPS軌跡・温度グラフ表示WebUI。
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gps_monitor import db as gps_db
from temp_monitor import db as temp_db

load_dotenv()

app = FastAPI(title="Car Logger Viewer")

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_TEMPLATES_DIR = Path(__file__).parent / "templates"

WEB_PORT = int(os.getenv("WEB_PORT", "8081"))
_PHOTOS_DIR = Path(__file__).parent.parent / "data" / "photos"

# センサーIDと場所名のマッピング（sensor_map.json が存在すれば読み込む）
_SENSOR_MAP_PATH = Path(__file__).parent.parent / "sensor_map.json"

def _load_sensor_map() -> dict[str, str]:
    if _SENSOR_MAP_PATH.exists():
        try:
            return json.loads(_SENSOR_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


@app.on_event("startup")
def startup() -> None:
    gps_db.init_db()
    temp_db.init_db()
    _PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


# ---- GPS ----

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/track")
def get_track(
    start: str = Query(description="開始日時 (datetime-local / JST)"),
    end: str = Query(description="終了日時 (datetime-local / JST)"),
) -> JSONResponse:
    try:
        jst = timezone(timedelta(hours=9))
        start_dt = datetime.fromisoformat(start).replace(tzinfo=jst).astimezone(timezone.utc)
        end_dt   = datetime.fromisoformat(end).replace(tzinfo=jst).astimezone(timezone.utc)
    except ValueError as e:
        return JSONResponse({"error": f"日時の形式が不正です: {e}"}, status_code=400)

    rows = gps_db.query(start_dt, end_dt)
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
    rows = gps_db.latest(1)
    if not rows:
        return JSONResponse({"point": None})
    return JSONResponse({"point": rows[0]})


@app.get("/api/geolocation")
def get_geolocation(
    start: str = Query(description="開始日時 (datetime-local / JST)"),
    end: str = Query(description="終了日時 (datetime-local / JST)"),
) -> JSONResponse:
    """
    指定した日時範囲のGeolocation履歴を返す。GPS座標との差も含む。
    """
    try:
        jst = timezone(timedelta(hours=9))
        start_dt = datetime.fromisoformat(start).replace(tzinfo=jst).astimezone(timezone.utc)
        end_dt   = datetime.fromisoformat(end).replace(tzinfo=jst).astimezone(timezone.utc)
    except ValueError as e:
        return JSONResponse({"error": f"日時の形式が不正です: {e}"}, status_code=400)

    rows = gps_db.query_geolocation(start_dt, end_dt)
    return JSONResponse({"count": len(rows), "points": rows})


# ---- 温度 ----

@app.get("/temperature", response_class=HTMLResponse)
def temperature_page() -> HTMLResponse:
    html = (_TEMPLATES_DIR / "temperature.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/temperature")
def get_temperature(
    start: str = Query(description="開始日時 (datetime-local / JST)"),
    end: str = Query(description="終了日時 (datetime-local / JST)"),
) -> JSONResponse:
    """
    指定した日時範囲の温度履歴を返す。

    レスポンス形式:
    {
      "sensor_map": {"28-xxx": "車内フロント", ...},
      "sensors": {
        "28-xxx": [{"t": "2026-...", "c": 25.3}, ...]
      }
    }
    """
    try:
        jst = timezone(timedelta(hours=9))
        start_dt = datetime.fromisoformat(start).replace(tzinfo=jst).astimezone(timezone.utc)
        end_dt   = datetime.fromisoformat(end).replace(tzinfo=jst).astimezone(timezone.utc)
    except ValueError as e:
        return JSONResponse({"error": f"日時の形式が不正です: {e}"}, status_code=400)

    rows = temp_db.query(start_dt, end_dt)
    sensor_map = _load_sensor_map()

    # センサーIDごとに時系列データをまとめる
    sensors: dict[str, list[dict]] = {}
    for r in rows:
        sid = r["sensor_id"]
        if sid not in sensors:
            sensors[sid] = []
        sensors[sid].append({"t": r["recorded_at"], "c": r["temp_c"]})

    return JSONResponse({
        "sensor_map": sensor_map,
        "sensors": sensors,
        "count": len(rows),
    })


@app.get("/api/temperature/sensors")
def get_sensor_list() -> JSONResponse:
    """DBに存在するセンサーID一覧とマッピング名を返す。"""
    ids = temp_db.sensor_ids()
    sensor_map = _load_sensor_map()
    return JSONResponse({
        "sensors": [
            {"id": sid, "name": sensor_map.get(sid, sid)}
            for sid in ids
        ]
    })


# ---- ラズパイからの位置Push ----

class _LocationPush(BaseModel):
    lat: float
    lon: float
    alt: float | None = None
    speed_kmh: float | None = None
    has_fix: bool = True
    recorded_at: str


@app.post("/api/location")
def push_location(body: _LocationPush) -> JSONResponse:
    """ラズパイが加速検知時に位置情報をPushするエンドポイント。"""
    gps_db.insert(
        recorded_at=body.recorded_at,
        lat=body.lat,
        lon=body.lon,
        alt=body.alt,
        speed_kmh=body.speed_kmh,
        has_fix=body.has_fix,
    )
    return JSONResponse({"ok": True})


# ---- カメラ写真 ----

@app.post("/api/photo")
async def upload_photo(
    file: UploadFile = File(...),
    lat: str = Form(""),
    lon: str = Form(""),
    alt: str = Form(""),
    recorded_at: str = Form(...),
) -> JSONResponse:
    """ラズパイが定期的に撮影した静止画をアップロードするエンドポイント。"""
    lat_val = float(lat) if lat else None
    lon_val = float(lon) if lon else None
    alt_val = float(alt) if alt else None

    # タイムスタンプ + ランダムサフィックスでファイル名を生成（衝突回避）
    try:
        dt = datetime.fromisoformat(recorded_at)
        ts = dt.strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        ts = "unknown"
    suffix = os.urandom(3).hex()
    filename = f"{ts}_{suffix}.jpg"

    photo_path = _PHOTOS_DIR / filename
    content = await file.read()
    photo_path.write_bytes(content)

    photo_id = gps_db.insert_photo(
        recorded_at=recorded_at,
        lat=lat_val,
        lon=lon_val,
        alt=alt_val,
        photo_path=filename,
    )
    return JSONResponse({"ok": True, "id": photo_id})


@app.get("/api/photos")
def get_photos(
    start: str = Query(description="開始日時 (datetime-local / JST)"),
    end: str = Query(description="終了日時 (datetime-local / JST)"),
) -> JSONResponse:
    """指定日時範囲のカメラ写真一覧（緯度経度付き）を返す。"""
    try:
        jst = timezone(timedelta(hours=9))
        start_dt = datetime.fromisoformat(start).replace(tzinfo=jst).astimezone(timezone.utc)
        end_dt   = datetime.fromisoformat(end).replace(tzinfo=jst).astimezone(timezone.utc)
    except ValueError as e:
        return JSONResponse({"error": f"日時の形式が不正です: {e}"}, status_code=400)

    rows = gps_db.query_photos(start_dt, end_dt)
    return JSONResponse({"count": len(rows), "photos": rows})


@app.get("/api/photo/{photo_id}")
def get_photo(photo_id: int) -> FileResponse:
    """IDに対応する写真ファイルを返す。"""
    path = gps_db.get_photo_path(photo_id)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    full_path = _PHOTOS_DIR / path
    if not full_path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(full_path), media_type="image/jpeg")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gps_web.main:app", host="0.0.0.0", port=WEB_PORT, reload=False)
