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


def _lat_lon_to_tile_frac(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """(lat, lon) → タイル座標（小数）を返す。"""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


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
        # GPS が動いていない限り再合成しない（Pi3 の負荷軽減）
        cache_key = (round(lat, 5), round(lon, 5), screen_w, screen_h, zoom)
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

        self._render_cache = surf
        self._render_cache_key = cache_key
        return surf

    def invalidate_cache(self):
        """GPS 位置が変わったときにレンダーキャッシュを破棄する。"""
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
                        # 位置が変わったらレンダーキャッシュを破棄
                        if new_lat != self._lat or new_lon != self._lon:
                            self.invalidate_cache()
                        self._lat = new_lat
                        self._lon = new_lon
                        self._speed = data.get("speed_kmh")
                        self._has_fix = data.get("has_fix", False)
            except Exception as e:
                log.debug("GPS ポーリング失敗: %s", e)
            self._stop.wait(timeout=5)
