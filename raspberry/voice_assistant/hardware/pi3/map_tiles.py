"""地図タイル管理: OpenStreetMap タイルの取得・キャッシュ・GPS ポーリング。"""

import io
import logging
import math
import os
import threading

import requests

log = logging.getLogger("voice-assistant.map")

_TILE_SIZE = 256
_USER_AGENT = "voice-assistant/1.0 (Raspberry Pi in-car)"
_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# ズームレベルの許容範囲
_ZOOM_MIN = 10
_ZOOM_MAX = 19


def _lat_lon_to_tile_frac(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """(lat, lon) → タイル座標（小数）を返す。"""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _lat_lon_to_screen(
    lat: float, lon: float,
    center_lat: float, center_lon: float,
    zoom: int, screen_w: int, screen_h: int,
) -> tuple[int, int] | None:
    """緯度経度をスクリーン座標に変換する。範囲外なら None。"""
    cx_frac, cy_frac = _lat_lon_to_tile_frac(center_lat, center_lon, zoom)
    px_frac, py_frac = _lat_lon_to_tile_frac(lat, lon, zoom)
    dx = (px_frac - cx_frac) * _TILE_SIZE
    dy = (py_frac - cy_frac) * _TILE_SIZE
    sx = int(screen_w // 2 + dx)
    sy = int(screen_h // 2 + dy)
    margin = 60
    if -margin <= sx <= screen_w + margin and -margin <= sy <= screen_h + margin:
        return sx, sy
    return None


class MapManager:
    """GPS ポーリング + OSM タイル取得・キャッシュを管理する。"""

    def __init__(self, gps_url: str, zoom: int, cache_dir: str):
        self._gps_url = gps_url
        self._zoom = zoom
        self._cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

        self._gps_lock = threading.Lock()
        self._lat: float | None = None
        self._lon: float | None = None
        self._speed: float | None = None
        self._has_fix: bool = False

        self._tile_lock = threading.Lock()
        self._mem_cache: dict[tuple, bytes] = {}

        # render_map のキャッシュ（GPS が動いていない間は再合成しない）
        self._render_cache = None
        self._render_cache_key: tuple | None = None

        # ナビ・POI データ（外部から set する）
        self._nav_lock = threading.Lock()
        self._route_coords: list[tuple[float, float]] = []   # (lat, lon) リスト
        self._dest: tuple[float, float] | None = None         # (lat, lon)
        self._dest_name: str = ""
        self._pois: list = []                                 # POI オブジェクトのリスト
        self._nav_active: bool = False

        self._stop = threading.Event()
        self._gps_thread = threading.Thread(target=self._poll_gps, daemon=True, name="gps-poller")

    def start(self):
        self._gps_thread.start()
        log.info("MapManager 開始 (zoom=%d, cache=%s)", self._zoom, self._cache_dir)

    def stop(self):
        self._stop.set()

    def get_gps(self) -> tuple[float | None, float | None, float | None, bool]:
        """(lat, lon, speed_kmh, has_fix) を返す。"""
        with self._gps_lock:
            return self._lat, self._lon, self._speed, self._has_fix

    def set_zoom(self, zoom: int):
        """ズームレベルを変更する。"""
        new_zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, zoom))
        if new_zoom != self._zoom:
            self._zoom = new_zoom
            self.invalidate_cache()
            log.info("ズームレベル変更: %d", new_zoom)

    def change_zoom(self, delta: int):
        """ズームレベルを相対的に変更する。"""
        self.set_zoom(self._zoom + delta)

    @property
    def zoom(self) -> int:
        return self._zoom

    def set_route(
        self,
        route_coords: list[tuple[float, float]],
        dest: tuple[float, float] | None,
        dest_name: str = "",
    ):
        """経路座標・目的地を設定する。route_coords が空なら経路を消去。"""
        with self._nav_lock:
            self._route_coords = route_coords
            self._dest = dest
            self._dest_name = dest_name
            self._nav_active = bool(route_coords)
        self.invalidate_cache()

    def clear_route(self):
        with self._nav_lock:
            self._route_coords = []
            self._dest = None
            self._dest_name = ""
            self._nav_active = False
        self.invalidate_cache()

    def set_pois(self, pois: list):
        """POI リストを更新する。"""
        with self._nav_lock:
            self._pois = pois
        self.invalidate_cache()

    def get_tile(self, z: int, x: int, y: int) -> bytes | None:
        """タイル PNG を返す。メモリ → ディスク → ネットの順で探す。"""
        key = (z, x, y)
        with self._tile_lock:
            if key in self._mem_cache:
                return self._mem_cache[key]

        disk_path = os.path.join(self._cache_dir, str(z), str(x), f"{y}.png")
        if os.path.isfile(disk_path):
            try:
                with open(disk_path, "rb") as f:
                    data = f.read()
                with self._tile_lock:
                    self._mem_cache[key] = data
                return data
            except OSError:
                pass

        url = _TILE_URL.format(z=z, x=x, y=y)
        try:
            resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=5)
            if resp.status_code == 200:
                data = resp.content
                os.makedirs(os.path.dirname(disk_path), exist_ok=True)
                with open(disk_path, "wb") as f:
                    f.write(data)
                with self._tile_lock:
                    self._mem_cache[key] = data
                log.debug("タイル取得 z=%d x=%d y=%d", z, x, y)
                return data
        except Exception as e:
            log.debug("タイル取得失敗 z=%d x=%d y=%d: %s", z, x, y, e)
        return None

    def render_map(self, screen_w: int, screen_h: int, pygame) -> "pygame.Surface | None":
        """現在位置を中心とした地図 Surface を返す。位置不明なら None。"""
        lat, lon, _, _ = self.get_gps()
        if lat is None or lon is None:
            return None

        zoom = self._zoom
        with self._nav_lock:
            route_coords = list(self._route_coords)
            dest = self._dest
            dest_name = self._dest_name
            pois = list(self._pois)

        cache_key = (round(lat, 5), round(lon, 5), screen_w, screen_h, zoom,
                     len(route_coords), dest)
        if self._render_cache_key == cache_key and self._render_cache is not None:
            return self._render_cache

        cx_frac, cy_frac = _lat_lon_to_tile_frac(lat, lon, zoom)
        cx_tile = int(cx_frac)
        cy_tile = int(cy_frac)
        cx_pix = int((cx_frac - cx_tile) * _TILE_SIZE)
        cy_pix = int((cy_frac - cy_tile) * _TILE_SIZE)

        tiles_x = screen_w // _TILE_SIZE + 2
        tiles_y = screen_h // _TILE_SIZE + 2
        half_x = tiles_x // 2
        half_y = tiles_y // 2

        surf = pygame.Surface((screen_w, screen_h))
        surf.fill((180, 180, 180))

        any_tile = False
        for dy in range(-half_y, half_y + 1):
            for dx in range(-half_x, half_x + 1):
                tx = cx_tile + dx
                ty = cy_tile + dy
                blit_x = screen_w // 2 - cx_pix + dx * _TILE_SIZE
                blit_y = screen_h // 2 - cy_pix + dy * _TILE_SIZE

                tile_data = self.get_tile(zoom, tx, ty)
                if tile_data:
                    try:
                        tile_surf = pygame.image.load(io.BytesIO(tile_data))
                        surf.blit(tile_surf, (blit_x, blit_y))
                        any_tile = True
                    except Exception:
                        pass

        if not any_tile:
            return None

        # ── 経路ポリライン描画 ───────────────────────────────────────────────────
        if route_coords:
            screen_points = []
            for rlat, rlon in route_coords:
                pt = _lat_lon_to_screen(rlat, rlon, lat, lon, zoom, screen_w, screen_h)
                if pt:
                    screen_points.append(pt)
            if len(screen_points) >= 2:
                # 外枠（白）
                pygame.draw.lines(surf, (255, 255, 255), False, screen_points, 8)
                # 経路本体（青）
                pygame.draw.lines(surf, (30, 120, 255), False, screen_points, 5)

        # ── 目的地マーカー ───────────────────────────────────────────────────────
        if dest:
            pt = _lat_lon_to_screen(dest[0], dest[1], lat, lon, zoom, screen_w, screen_h)
            if pt:
                dx, dy = pt
                pygame.draw.circle(surf, (255, 60, 60), (dx, dy), 14)
                pygame.draw.circle(surf, (255, 255, 255), (dx, dy), 10)
                pygame.draw.circle(surf, (255, 60, 60), (dx, dy), 6)

        # ── POI マーカー ─────────────────────────────────────────────────────────
        for poi in pois:
            pt = _lat_lon_to_screen(poi.lat, poi.lon, lat, lon, zoom, screen_w, screen_h)
            if pt:
                px, py = pt
                pygame.draw.circle(surf, poi.color, (px, py), 8)
                pygame.draw.circle(surf, (255, 255, 255), (px, py), 8, 2)

        self._render_cache = surf
        self._render_cache_key = cache_key
        return surf

    def invalidate_cache(self):
        """レンダーキャッシュを破棄する。"""
        self._render_cache = None
        self._render_cache_key = None

    def _poll_gps(self):
        while not self._stop.is_set():
            try:
                resp = requests.get(self._gps_url, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    new_lat = data.get("lat")
                    new_lon = data.get("lon")
                    with self._gps_lock:
                        if new_lat != self._lat or new_lon != self._lon:
                            self.invalidate_cache()
                        self._lat = new_lat
                        self._lon = new_lon
                        self._speed = data.get("speed_kmh")
                        self._has_fix = data.get("has_fix", False)
            except Exception as e:
                log.debug("GPS ポーリング失敗: %s", e)
            self._stop.wait(timeout=5)
