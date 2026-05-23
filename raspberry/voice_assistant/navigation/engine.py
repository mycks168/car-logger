"""カーナビエンジン。OSRM で経路計算し、ターンバイターン音声案内を行う。"""

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

import config

log = logging.getLogger("voice-assistant.navi")

# ターン種別 → 日本語案内テキスト
_MANEUVER_TEXT = {
    ("turn", "left"):          "左折",
    ("turn", "right"):         "右折",
    ("turn", "slight left"):   "やや左方向",
    ("turn", "slight right"):  "やや右方向",
    ("turn", "sharp left"):    "鋭角に左折",
    ("turn", "sharp right"):   "鋭角に右折",
    ("turn", "uturn"):         "Uターン",
    ("continue", None):        "直進",
    ("new name", None):        "直進",
    ("roundabout", None):      "ロータリーを通過",
    ("rotary", None):          "ロータリーを通過",
    ("fork", "left"):          "左方向へ進む",
    ("fork", "right"):         "右方向へ進む",
    ("merge", "left"):         "左へ合流",
    ("merge", "right"):        "右へ合流",
    ("on ramp", "left"):       "左の入口から進む",
    ("on ramp", "right"):      "右の入口から進む",
    ("off ramp", "left"):      "左の出口へ",
    ("off ramp", "right"):     "右の出口へ",
    ("depart", None):          "出発",
    ("arrive", None):          "目的地に到着",
}


def _maneuver_text(maneuver_type: str, modifier: str | None) -> str:
    key = (maneuver_type, modifier)
    if key in _MANEUVER_TEXT:
        return _MANEUVER_TEXT[key]
    key_none = (maneuver_type, None)
    if key_none in _MANEUVER_TEXT:
        return _MANEUVER_TEXT[key_none]
    return maneuver_type


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離（メートル）をハーバーサイン公式で計算する。"""
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass
class NavStep:
    """OSRM の 1 ステップ（交差点から次の交差点まで）。"""
    lat: float
    lon: float
    maneuver_type: str
    modifier: str | None
    name: str
    distance_m: float
    duration_s: float
    # ステップの経路点（ポリライン描画用）
    geometry: list[tuple[float, float]] = field(default_factory=list)

    @property
    def instruction(self) -> str:
        return _maneuver_text(self.maneuver_type, self.modifier)

    @property
    def is_arrive(self) -> bool:
        return self.maneuver_type == "arrive"


@dataclass
class NavState:
    """現在のナビゲーション状態。スレッドをまたいで参照される。"""
    active: bool = False
    paused: bool = False
    dest_lat: float = 0.0
    dest_lon: float = 0.0
    dest_name: str = ""
    steps: list[NavStep] = field(default_factory=list)
    step_index: int = 0
    route_coords: list[tuple[float, float]] = field(default_factory=list)
    total_distance_m: float = 0.0
    total_duration_s: float = 0.0
    # 案内済みフラグ（300m / 50m）
    announced_far: bool = False
    announced_near: bool = False


class NavigationEngine:
    """OSRM 経路計算 + ターンバイターン案内エンジン。"""

    def __init__(
        self,
        get_gps: Callable[[], tuple[float | None, float | None, float | None, bool]],
        on_speak: Callable[[str], None],
        on_state_change: Callable[[], None] | None = None,
    ):
        """
        get_gps: () → (lat, lon, speed_kmh, has_fix)
        on_speak: TTS に読み上げさせるコールバック
        on_state_change: ナビ状態が変化したとき地図再描画を促すコールバック
        """
        self._get_gps = get_gps
        self._on_speak = on_speak
        self._on_state_change = on_state_change or (lambda: None)

        self._lock = threading.Lock()
        self._state = NavState()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="navi-monitor")
        self._thread.start()

    # ── 公開 API ────────────────────────────────────────────────────────────────

    def start(self, dest_lat: float, dest_lon: float, dest_name: str) -> bool:
        """目的地を設定して経路案内を開始する。失敗したら False を返す。"""
        lat, lon, _, has_fix = self._get_gps()
        if lat is None or lon is None:
            log.warning("GPS 未取得のため経路計算できません")
            return False

        route = self._fetch_route(lat, lon, dest_lat, dest_lon)
        if route is None:
            return False

        steps, route_coords, total_dist, total_dur = route
        with self._lock:
            self._state = NavState(
                active=True,
                paused=False,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
                dest_name=dest_name,
                steps=steps,
                step_index=0,
                route_coords=route_coords,
                total_distance_m=total_dist,
                total_duration_s=total_dur,
                announced_far=False,
                announced_near=False,
            )
        self._on_state_change()
        log.info(
            "案内開始: %s (%.0fm, %.0f分)",
            dest_name, total_dist, total_dur / 60,
        )
        mins = int(total_dur / 60)
        km = total_dist / 1000
        self._on_speak(
            f"{dest_name}への案内を開始します。"
            f"距離は約{km:.1f}キロメートル、"
            f"所要時間は約{mins}分です。"
        )
        return True

    def stop(self):
        """案内を停止して経路を消去する。"""
        with self._lock:
            was_active = self._state.active
            self._state = NavState()
        if was_active:
            self._on_state_change()
            log.info("案内停止")

    def toggle_pause(self) -> bool:
        """一時停止 / 再開をトグルする。現在の状態（True=停止中）を返す。"""
        with self._lock:
            if not self._state.active:
                return False
            self._state.paused = not self._state.paused
            paused = self._state.paused
        log.info("案内 %s", "一時停止" if paused else "再開")
        return paused

    def get_state(self) -> NavState:
        """現在のナビ状態のコピーを返す（スレッドセーフ）。"""
        with self._lock:
            import copy
            return copy.copy(self._state)

    def shutdown(self):
        self._stop.set()

    # ── 内部処理 ────────────────────────────────────────────────────────────────

    def _fetch_route(
        self,
        orig_lat: float, orig_lon: float,
        dest_lat: float, dest_lon: float,
    ) -> tuple[list[NavStep], list[tuple[float, float]], float, float] | None:
        """OSRM に経路を問い合わせて (steps, route_coords, total_m, total_s) を返す。"""
        url = (
            f"{config.OSRM_BASE_URL}/route/v1/driving/"
            f"{orig_lon},{orig_lat};{dest_lon},{dest_lat}"
            "?steps=true&overview=full&geometries=geojson"
        )
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error("OSRM リクエスト失敗: %s", e)
            return None

        if data.get("code") != "Ok" or not data.get("routes"):
            log.error("OSRM 経路なし: %s", data.get("code"))
            return None

        route = data["routes"][0]
        total_dist = route["distance"]
        total_dur = route["duration"]

        # 全体ポリライン（地図描画用）
        route_coords: list[tuple[float, float]] = [
            (c[1], c[0])  # GeoJSON は [lon,lat] → (lat,lon) に変換
            for c in route["geometry"]["coordinates"]
        ]

        steps: list[NavStep] = []
        for leg in route["legs"]:
            for step in leg["steps"]:
                maneuver = step["maneuver"]
                loc = maneuver["location"]  # [lon, lat]
                geom_coords: list[tuple[float, float]] = [
                    (c[1], c[0]) for c in step["geometry"]["coordinates"]
                ]
                steps.append(NavStep(
                    lat=loc[1],
                    lon=loc[0],
                    maneuver_type=maneuver.get("type", ""),
                    modifier=maneuver.get("modifier"),
                    name=step.get("name", ""),
                    distance_m=step["distance"],
                    duration_s=step["duration"],
                    geometry=geom_coords,
                ))

        return steps, route_coords, total_dist, total_dur

    def _monitor_loop(self):
        """1秒ごとに現在位置とステップの距離を確認して案内を発火する。"""
        while not self._stop.wait(timeout=1.0):
            with self._lock:
                state = self._state
                if not state.active or state.paused:
                    continue
                steps = state.steps
                idx = state.step_index

            lat, lon, _, has_fix = self._get_gps()
            if lat is None or lon is None or not has_fix:
                continue

            if idx >= len(steps):
                continue

            # 次のステップ（曲がり角）までの距離
            next_step = steps[idx]
            dist_m = _haversine_m(lat, lon, next_step.lat, next_step.lon)

            with self._lock:
                if not self._state.active or self._state.step_index != idx:
                    continue

                # 目的地到着判定
                if next_step.is_arrive:
                    if dist_m <= config.NAV_ARRIVE_DISTANCE_M:
                        self._state.active = False
                        self._on_speak(f"{self._state.dest_name}に到着しました。案内を終了します。")
                        self._state = NavState()
                        self._on_state_change()
                        log.info("目的地到着")
                    continue

                # ステップ通過判定（50m 以内に近づいたら次へ）
                if dist_m <= 30:
                    self._state.step_index += 1
                    self._state.announced_far = False
                    self._state.announced_near = False
                    self._on_state_change()
                    log.debug("ステップ %d 通過", idx)
                    continue

                # 300m 前案内
                far_thresh = config.NAV_ANNOUNCE_DISTANCE_M
                near_thresh = 50
                if dist_m <= far_thresh and not self._state.announced_far:
                    self._state.announced_far = True
                    road = f"「{next_step.name}」を" if next_step.name else ""
                    self._on_speak(
                        f"約{int(dist_m)}メートル先、"
                        f"{road}{next_step.instruction}してください"
                    )
                    log.info("300m 案内: %s", next_step.instruction)

                # 50m 前案内
                elif dist_m <= near_thresh and not self._state.announced_near:
                    self._state.announced_near = True
                    self._on_speak(f"{next_step.instruction}してください")
                    log.info("50m 案内: %s", next_step.instruction)
