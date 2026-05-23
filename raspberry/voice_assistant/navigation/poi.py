"""Overpass API を使って周辺 POI（ガソリンスタンド等）を検索する。"""

import logging
import threading
import time
from dataclasses import dataclass

import requests

import config

log = logging.getLogger("voice-assistant.poi")

# 検索対象の POI カテゴリ
_POI_QUERIES = {
    "fuel":       ("amenity", "fuel",        "ガソリンスタンド", (255, 160, 30)),
    "convenience":("shop",    "convenience",  "コンビニ",         (30, 160, 255)),
    "parking":    ("amenity", "parking",      "駐車場",           (180, 180, 60)),
    "restaurant": ("amenity", "restaurant",   "レストラン",       (255, 80, 80)),
    "hospital":   ("amenity", "hospital",     "病院",             (255, 60, 120)),
}


@dataclass
class POI:
    lat: float
    lon: float
    name: str
    category: str
    label: str
    color: tuple[int, int, int]


class POIManager:
    """定期的に Overpass API へ問い合わせて POI 一覧を更新する。"""

    def __init__(self, get_gps):
        """get_gps: () → (lat, lon, speed_kmh, has_fix)"""
        self._get_gps = get_gps
        self._lock = threading.Lock()
        self._pois: list[POI] = []
        self._last_lat: float | None = None
        self._last_lon: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._update_loop, daemon=True, name="poi-updater")

    def start(self):
        self._thread.start()
        log.info("POIManager 開始")

    def stop(self):
        self._stop.set()

    def get_pois(self) -> list[POI]:
        with self._lock:
            return list(self._pois)

    def _update_loop(self):
        # 初回は少し待って GPS を取得してから検索
        self._stop.wait(timeout=10.0)
        while not self._stop.is_set():
            lat, lon, _, has_fix = self._get_gps()
            if has_fix and lat is not None and lon is not None:
                moved = self._has_moved(lat, lon)
                if moved:
                    self._fetch_and_update(lat, lon)
                    self._last_lat = lat
                    self._last_lon = lon
            self._stop.wait(timeout=60.0)

    def _has_moved(self, lat: float, lon: float) -> bool:
        """前回取得位置から POI_UPDATE_DISTANCE_M 以上移動したか判定する。"""
        if self._last_lat is None or self._last_lon is None:
            return True
        import math
        R = 6_371_000.0
        dlat = math.radians(lat - self._last_lat)
        dlon = math.radians(lon - self._last_lon)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(self._last_lat))
             * math.cos(math.radians(lat))
             * math.sin(dlon / 2) ** 2)
        dist = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return dist >= config.POI_UPDATE_DISTANCE_M

    def _fetch_and_update(self, lat: float, lon: float):
        """Overpass API で全カテゴリを一括検索する。"""
        r = config.POI_SEARCH_RADIUS_M
        union_parts = "\n".join(
            f'  node["{key}"="{val}"](around:{r},{lat},{lon});'
            for _, (key, val, _, _) in _POI_QUERIES.items()
        )
        query = f"[out:json][timeout:10];\n(\n{union_parts}\n);\nout;"
        try:
            resp = requests.post(
                config.OVERPASS_URL,
                data={"data": query},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.debug("Overpass 取得失敗: %s", e)
            return

        pois: list[POI] = []
        for elem in data.get("elements", []):
            if elem.get("type") != "node":
                continue
            tags = elem.get("tags", {})
            poi_lat = elem["lat"]
            poi_lon = elem["lon"]
            name = tags.get("name", "")

            # カテゴリ判定
            matched = None
            for cat, (key, val, label, color) in _POI_QUERIES.items():
                if tags.get(key) == val:
                    matched = (cat, label, color)
                    break
            if matched is None:
                continue
            cat, label, color = matched
            display_name = name if name else label

            pois.append(POI(
                lat=poi_lat,
                lon=poi_lon,
                name=display_name,
                category=cat,
                label=label,
                color=color,
            ))

        with self._lock:
            self._pois = pois
        log.info("POI 更新: %d 件 (中心: %.4f, %.4f)", len(pois), lat, lon)
