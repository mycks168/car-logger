"""Raspberry Pi 3 向け pygame ディスプレイ。
HDMI 画面にキャラクター画像とテキストを表示する。
"""
import json
import os
import random
import threading
import time
from datetime import datetime

import config
from hardware.pi3.board import Pi3Board
from hardware.pi3.map_tiles import MapManager

_ASSET_DIR = getattr(config, "UI_IMAGE_ASSETS_DIR", "assets/pngtuber_pi3")

_BG_COLOR = (18, 18, 28)
_TEXT_COLOR = (220, 220, 220)
_DIM_COLOR = (100, 100, 110)
_STATUS_COLOR = (255, 230, 100)
_RESPONSE_BG = (28, 28, 40)

_TICK_MS = float(os.environ.get("PI3_TICK_MS", "50"))

_STATE_MAP = {
    "idle":      "idle",
    "listening": "listen",
    "thinking":  "idle",
    "talking":   "talk",
    "done":      "idle",
}

_FPS = int(os.environ.get("PI3_FPS", "15"))


def _find_japanese_font() -> str | None:
    env_path = os.environ.get("PI3_FONT_PATH", "")
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf",
        "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _parse_frame_list(asset_dir: str, frames: list) -> list[tuple]:
    seq = []
    for entry in frames:
        if isinstance(entry, str):
            img_path = os.path.join(asset_dir, entry)
            duration_ms = _TICK_MS
        else:
            img_path = os.path.join(asset_dir, entry["image"])
            if "duration_ms" in entry:
                duration_ms = float(entry["duration_ms"])
            else:
                duration_ms = entry.get("duration_ticks", 1) * _TICK_MS
        seq.append((img_path, duration_ms))
    return seq


def _load_char_frames(asset_dir: str) -> dict[str, dict]:
    json_path = os.path.join(asset_dir, "character.json")
    if not os.path.isfile(json_path):
        return {}
    try:
        with open(json_path) as f:
            data = json.load(f)
    except Exception:
        return {}

    blink_cfg = data.get("_blink", {})
    result: dict[str, dict] = {}

    for state, value in data.items():
        if state.startswith("_"):
            continue
        if isinstance(value, list):
            loop = _parse_frame_list(asset_dir, value)
            if loop:
                result[state] = {"intro": [], "loop": loop, "blink": None, "blink_loop": []}
        elif isinstance(value, dict):
            intro = _parse_frame_list(asset_dir, value.get("intro", []))
            loop = _parse_frame_list(asset_dir, value.get("loop", []))
            blink_name = value.get("blink")
            blink_path = os.path.join(asset_dir, blink_name) if blink_name else None
            blink_loop = [
                os.path.join(asset_dir, p)
                for p in value.get("blink_loop", [])
            ]
            if intro or loop:
                result[state] = {
                    "intro": intro, "loop": loop,
                    "blink": blink_path, "blink_loop": blink_loop,
                }

    result["_blink"] = blink_cfg

    lipsync_shape_dirs = {
        0: "talk_close_warp_frames",
        1: "talk_open_warp_frames",
        2: "talk_open_warp_frames",
        3: "talk_full_warp_frames",
    }
    required_dirs = set(lipsync_shape_dirs.values())
    if all(os.path.isdir(os.path.join(asset_dir, d)) for d in required_dirs):
        sample_dir = os.path.join(asset_dir, "talk_open_warp_frames")
        n_frames = len([f for f in os.listdir(sample_dir)
                        if f.startswith("frame_") and f.endswith(".png")])
        blink_dir = "blink_talk_warp_frames"
        if not os.path.isdir(os.path.join(asset_dir, blink_dir)):
            blink_dir = None
        result["_lipsync"] = {
            "dirs": lipsync_shape_dirs,
            "n_frames": n_frames,
            "blink_dir": blink_dir,
            "breath_ms": 125,
        }

    return result


class Display:
    """Pi3 向けディスプレイ: GPIO PTT + pygame HDMI 表示。"""

    def __init__(self, backlight: int = 100):
        self.board = Pi3Board()
        self.show_response_during_tts = True
        self._sleeping = False
        self._lock = threading.Lock()
        self._response_buf = ""
        self._status_text = ""
        self._status_subtitle = ""
        self._status_color = _TEXT_COLOR
        self._char_state = "idle"
        self._spinner_text = ""
        self._spinner_active = False
        self._stop_event = threading.Event()
        self._tts = None
        self._session_name = ""

        # ナビ情報（ナビエンジンから更新される）
        self._nav_lock = threading.Lock()
        self._nav_active = False
        self._nav_paused = False
        self._nav_next_instruction = ""
        self._nav_next_distance_m = 0.0
        self._nav_dest_name = ""
        self._nav_total_dist_m = 0.0
        self._nav_total_dur_s = 0.0

        self._map = MapManager(
            gps_url=config.GPS_SERVER_URL + "/gps",
            zoom=config.MAP_ZOOM,
            cache_dir=config.MAP_TILE_CACHE_DIR,
        )
        self._map.start()

        self._thread = threading.Thread(target=self._run_pygame, daemon=True)
        self._thread.start()

    def _run_pygame(self):
        import logging
        import pygame

        log = logging.getLogger("voice-assistant")

        sdl_driver = os.environ.get("PI3_SDL_DRIVER", "")
        if sdl_driver:
            os.environ["SDL_VIDEODRIVER"] = sdl_driver
        elif not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "kms")

        try:
            pygame.init()
        except Exception as e:
            log.error("pygame init failed: %s", e)
            return

        # audio サブシステムは aplay を使うので不要。PipeWire への接続を即時解放する
        pygame.mixer.quit()

        if not pygame.display.get_init():
            log.error("pygame display init failed")
            pygame.quit()
            return

        w = config.PI3_DISPLAY_WIDTH
        h = config.PI3_DISPLAY_HEIGHT
        flags = pygame.FULLSCREEN if config.PI3_DISPLAY_FULLSCREEN else 0
        try:
            screen = pygame.display.set_mode((w, h), flags)
        except Exception as e:
            log.error("pygame set_mode failed: %s", e)
            return

        pygame.display.set_caption("voice-assistant")
        pygame.mouse.set_visible(False)
        log.info("pygame display initialized (%dx%d fullscreen=%s)", w, h, config.PI3_DISPLAY_FULLSCREEN)

        font_path = _find_japanese_font()
        log.info("font: %s", font_path or "(not found)")

        def _font(size):
            if font_path:
                try:
                    return pygame.font.Font(font_path, size)
                except Exception:
                    pass
            for name in ("notosanscjkjp", "notosanscjk", "ipagothic", "takao"):
                f = pygame.font.SysFont(name, size)
                if f:
                    return f
            return pygame.font.SysFont(None, int(size * 1.3))

        font_large = _font(34)
        font_medium = _font(22)
        font_small = _font(16)
        font_response = _font(16)

        char_frames = _load_char_frames(_ASSET_DIR)
        log.info("character states loaded: %s", list(char_frames.keys()))

        img_cache: dict[str, "pygame.Surface"] = {}
        scaled_cache: dict[tuple, "pygame.Surface"] = {}

        def get_image(path: str):
            if path in img_cache:
                return img_cache[path]
            if not os.path.isfile(path):
                return None
            try:
                surf = pygame.image.load(path).convert_alpha()
                img_cache[path] = surf
                return surf
            except Exception as e:
                log.warning("image load failed %s: %s", path, e)
                return None

        def get_scaled(path: str, nw: int, nh: int):
            """スケール済み画像をキャッシュして返す。毎フレームの smoothscale を排除する。"""
            key = (path, nw, nh)
            if key in scaled_cache:
                return scaled_cache[key]
            surf = get_image(path)
            if surf is None:
                return None
            scaled = pygame.transform.smoothscale(surf, (nw, nh))
            scaled_cache[key] = scaled
            return scaled

        # アバターは右下隅に画面高さの 1/3 で表示
        AVATAR_H = h // 3
        BAR_H = 54  # 上部バーの高さ

        frame_idx = 0
        frame_elapsed = 0.0
        last_state = ""
        anim_phase = "loop"
        breath_idx = 0
        breath_elapsed = 0.0
        clock = pygame.time.Clock()

        blink_cfg = char_frames.get("_blink", {})
        blink_interval_min = blink_cfg.get("interval_min_s", 2.0)
        blink_interval_max = blink_cfg.get("interval_max_s", 5.0)
        blink_close_ms = blink_cfg.get("close_ms", 100)
        blink_fade_ms = blink_cfg.get("fade_ms", 60)
        next_blink_at = time.monotonic() + random.uniform(blink_interval_min, blink_interval_max)
        blink_start = None

        while not self._stop_event.is_set():
            dt_ms = clock.tick(_FPS)
            now = time.monotonic()

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._stop_event.set()
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self._stop_event.set()

            screen.fill(_BG_COLOR)

            with self._lock:
                app_state = self._char_state
                status_text = self._status_text
                status_sub = self._status_subtitle
                status_col = self._status_color
                response = self._response_buf
                spinner = self._spinner_text if self._spinner_active else ""
                sleeping = self._sleeping
                tts_player = self._tts
                session_name = self._session_name

            with self._nav_lock:
                nav_active = self._nav_active
                nav_paused = self._nav_paused
                nav_next_instr = self._nav_next_instruction
                nav_next_dist = self._nav_next_distance_m
                nav_dest = self._nav_dest_name
                nav_total_dist = self._nav_total_dist_m
                nav_total_dur = self._nav_total_dur_s

            # ── 1. 地図背景（常時） ───────────────────────────────────────────
            map_surf = self._map.render_map(w, h, pygame)
            if map_surf:
                screen.blit(map_surf, (0, 0))
            else:
                screen.fill(_DASH_BG)
                hint = font_small.render("GPS 位置を取得中...", True, _DASH_HINT)
                screen.blit(hint, hint.get_rect(centerx=w // 2, centery=h // 2))

            # ── 2. 上部バー（常時） ───────────────────────────────────────────
            bar = pygame.Surface((w, BAR_H), pygame.SRCALPHA)
            bar.fill((0, 0, 0, 190))
            screen.blit(bar, (0, 0))

            now_dt = datetime.now()
            time_s = font_large.render(now_dt.strftime("%H:%M"), True, (255, 255, 255))
            screen.blit(time_s, (16, (BAR_H - time_s.get_height()) // 2))
            x_bar = 16 + time_s.get_width() + 20

            _, _, speed, has_fix = self._map.get_gps()
            gps_color = (100, 230, 100) if has_fix else (230, 100, 100)
            if has_fix and speed is not None:
                gps_str = f"▲ {speed:.0f} km/h"
            elif has_fix:
                gps_str = "▲ GPS"
            else:
                gps_str = "× GPS"
            gps_s = font_medium.render(gps_str, True, gps_color)
            screen.blit(gps_s, (x_bar, (BAR_H - gps_s.get_height()) // 2))
            x_bar += gps_s.get_width() + 24

            sysinfo = _get_sysinfo()
            temp = sysinfo.get("temp")
            if temp is not None:
                temp_color = _DASH_HOT if temp >= 70 else (220, 220, 220)
                temp_s = font_medium.render(f"{temp:.0f}°C", True, temp_color)
                screen.blit(temp_s, (x_bar, (BAR_H - temp_s.get_height()) // 2))

            if session_name:
                ses_s = font_medium.render(session_name, True, _DASH_SESSION)
                screen.blit(ses_s, (w - ses_s.get_width() - 16, (BAR_H - ses_s.get_height()) // 2))

            # ── 3. 自車位置マーカー（GPS取得時、画面中央） ───────────────────
            if map_surf:
                cx, cy = w // 2, h // 2
                pygame.draw.circle(screen, (255, 255, 255), (cx, cy), 14)
                pygame.draw.circle(screen, (30, 120, 255), (cx, cy), 11)
                pygame.draw.circle(screen, (255, 255, 255), (cx, cy), 4)

            # ── 4. アバターアニメーション（右下隅、常時） ────────────────────
            json_state = _STATE_MAP.get(app_state, "idle")
            if json_state not in char_frames:
                json_state = next((k for k in char_frames if not k.startswith("_")), None)

            blit_x = blit_y = nw = nh = 0
            if json_state:
                state_data = char_frames[json_state]
                intro_seq = state_data["intro"]
                loop_seq = state_data["loop"]

                if app_state != last_state:
                    frame_idx = 0
                    frame_elapsed = 0.0
                    breath_idx = 0
                    breath_elapsed = 0.0
                    last_state = app_state
                    anim_phase = "intro" if intro_seq else "loop"

                lipsync = char_frames.get("_lipsync")
                use_lipsync = (json_state == "talk" and lipsync is not None
                               and tts_player is not None
                               and tts_player.is_speaking.is_set())

                img_path = None
                blink_path_for_frame = None

                if use_lipsync:
                    breath_elapsed += dt_ms
                    while breath_elapsed >= lipsync["breath_ms"]:
                        breath_elapsed -= lipsync["breath_ms"]
                        breath_idx = (breath_idx + 1) % lipsync["n_frames"]
                    mouth_shape = max(0, tts_player.get_mouth_shape())
                    mouth_dir = lipsync["dirs"][mouth_shape]
                    img_path = os.path.join(_ASSET_DIR, mouth_dir, f"frame_{breath_idx:04d}.png")
                    if lipsync["blink_dir"]:
                        blink_path_for_frame = os.path.join(
                            _ASSET_DIR, lipsync["blink_dir"], f"frame_{breath_idx:04d}.png")
                else:
                    frames_seq = intro_seq if anim_phase == "intro" else loop_seq
                    if not frames_seq:
                        frames_seq = loop_seq or intro_seq
                    if frames_seq:
                        frame_elapsed += dt_ms
                        _, dur = frames_seq[frame_idx]
                        while frame_elapsed >= dur:
                            frame_elapsed -= dur
                            ni = frame_idx + 1
                            if ni >= len(frames_seq):
                                if anim_phase == "intro" and loop_seq:
                                    anim_phase = "loop"
                                    frames_seq = loop_seq
                                    frame_idx = 0
                                else:
                                    frame_idx = 0 if len(frames_seq) > 1 else frame_idx
                            else:
                                frame_idx = ni
                            if frame_idx < len(frames_seq):
                                _, dur = frames_seq[frame_idx]
                        img_path, _ = frames_seq[frame_idx]
                        blink_lp = state_data.get("blink_loop", [])
                        if blink_lp:
                            blink_path_for_frame = blink_lp[frame_idx % len(blink_lp)]
                        else:
                            blink_path_for_frame = state_data.get("blink")

                if img_path:
                    surf = get_image(img_path)
                    if surf:
                        iw, ih = surf.get_size()
                        scale = (AVATAR_H / ih) * config.PI3_CHAR_SCALE
                        nw, nh = int(iw * scale), int(ih * scale)
                        blit_x = w - nw - 12
                        blit_y = h - nh - 12
                        scaled = get_scaled(img_path, nw, nh)
                        if scaled:
                            screen.blit(scaled, (blit_x, blit_y))

                        # まばたきトリガー
                        now_t = time.monotonic()
                        if blink_start is None and now_t >= next_blink_at:
                            blink_lp = state_data.get("blink_loop", [])
                            has_blink = (blink_lp or (blink_path_for_frame
                                         and os.path.isfile(str(blink_path_for_frame))))
                            if has_blink:
                                blink_start = now_t
                                next_blink_at = now_t + (blink_close_ms / 1000) + random.uniform(
                                    blink_interval_min, blink_interval_max)

                        # まばたきオーバーレイ
                        if blink_start is not None and blink_path_for_frame and nw and nh:
                            bs = get_scaled(blink_path_for_frame, nw, nh)
                            if bs:
                                elapsed_ms = (time.monotonic() - blink_start) * 1000
                                total_ms = blink_fade_ms * 2 + blink_close_ms
                                if elapsed_ms > total_ms:
                                    blink_start = None
                                else:
                                    if elapsed_ms < blink_fade_ms:
                                        alpha = int(elapsed_ms / blink_fade_ms * 255)
                                    elif elapsed_ms < blink_fade_ms + blink_close_ms:
                                        alpha = 255
                                    else:
                                        alpha = int((1 - (elapsed_ms - blink_fade_ms - blink_close_ms)
                                                    / blink_fade_ms) * 255)
                                    bsc = bs.copy()
                                    bsc.set_alpha(max(0, min(255, alpha)))
                                    screen.blit(bsc, (blit_x, blit_y))

            # ── 5. ナビパネル（案内中のみ、左下） ───────────────────────────────
            if nav_active and not sleeping:
                _draw_nav_panel(
                    screen, w, h, pygame,
                    font_large, font_medium, font_small,
                    nav_next_instr, nav_next_dist,
                    nav_dest, nav_total_dist, nav_total_dur,
                    nav_paused,
                )

            # ── 6. テキストオーバーレイ（スリープ時は非表示） ────────────────
            if not sleeping:
                PAD = 16
                # 応答テキスト（画面下部、アバターの左側まで）
                if response:
                    resp_w = w - (w - blit_x + 8) - PAD * 2 if blit_x > 0 else w - PAD * 2
                    lines = _wrap_text(response, font_response, resp_w - 16)
                    line_h = 24
                    max_lines = (h - BAR_H - 60) // line_h
                    if len(lines) > max_lines:
                        lines = lines[-max_lines:]
                    resp_h = len(lines) * line_h + 16
                    resp_y = h - resp_h - 12
                    bg = pygame.Surface((resp_w, resp_h), pygame.SRCALPHA)
                    bg.fill((0, 0, 0, 180))
                    screen.blit(bg, (PAD, resp_y))
                    for i, line in enumerate(lines):
                        ls = font_response.render(line, True, _TEXT_COLOR)
                        screen.blit(ls, (PAD + 8, resp_y + 8 + i * line_h))

                # ステータス / スピナー（バー直下）
                if spinner or status_text:
                    label = (spinner + "." * (int(now * 2) % 4)) if spinner else status_text
                    col = _STATUS_COLOR if spinner else tuple(status_col)
                    ls = font_large.render(label, True, col)
                    bg_w, bg_h = ls.get_width() + 24, ls.get_height() + 12
                    bg = pygame.Surface((bg_w, bg_h), pygame.SRCALPHA)
                    bg.fill((0, 0, 0, 170))
                    screen.blit(bg, (PAD - 8, BAR_H + 8))
                    screen.blit(ls, (PAD + 4, BAR_H + 14))
                    if status_sub and not spinner:
                        sub_s = font_small.render(status_sub, True, _DIM_COLOR)
                        screen.blit(sub_s, (PAD + 4, BAR_H + 14 + ls.get_height() + 4))

            pygame.display.flip()

        pygame.quit()

    # ── DisplayBackend インターフェース ───────────────────────────────────────

    @property
    def is_sleeping(self) -> bool:
        return self._sleeping

    def sleep(self):
        with self._lock:
            self._sleeping = True

    def wake(self):
        with self._lock:
            self._sleeping = False

    def cleanup(self):
        self._stop_event.set()
        self._map.stop()

    def set_backlight(self, level: int):
        pass

    def set_led(self, r: int, g: int, b: int):
        pass

    def stop_character(self):
        with self._lock:
            self._char_state = "idle"

    def start_character(self, state: str, tts=None):
        with self._lock:
            self._char_state = state
            self._tts = tts
            if state != "listening":
                self._response_buf = ""
            self._status_text = ""
            self._status_subtitle = ""
            self._spinner_active = False

    def set_character_state(self, state: str):
        with self._lock:
            self._char_state = state

    def set_status(
        self,
        text: str,
        *,
        color: tuple[int, int, int] | None = None,
        subtitle: str | None = None,
        accent_color: tuple[int, int, int] | None = None,
    ):
        with self._lock:
            self._status_text = text
            self._status_subtitle = subtitle or ""
            self._status_color = color or _TEXT_COLOR
            self._spinner_active = False

    def append_response(self, delta: str):
        with self._lock:
            self._response_buf += delta
            self._status_text = ""
            self._spinner_active = False

    def set_response_text(self, text: str):
        with self._lock:
            self._response_buf = text
            self._status_text = ""
            self._spinner_active = False

    def flush_response(self):
        pass

    def start_spinner(self, text: str = ""):
        with self._lock:
            self._spinner_text = text
            self._spinner_active = True
            self._status_text = ""

    def stop_spinner(self):
        with self._lock:
            self._spinner_active = False

    def set_session_name(self, name: str):
        with self._lock:
            self._session_name = name

    def update_nav(
        self,
        active: bool,
        paused: bool = False,
        next_instruction: str = "",
        next_distance_m: float = 0.0,
        dest_name: str = "",
        total_dist_m: float = 0.0,
        total_dur_s: float = 0.0,
    ):
        """ナビ情報を更新する（ナビエンジンから呼ばれる）。"""
        with self._nav_lock:
            self._nav_active = active
            self._nav_paused = paused
            self._nav_next_instruction = next_instruction
            self._nav_next_distance_m = next_distance_m
            self._nav_dest_name = dest_name
            self._nav_total_dist_m = total_dist_m
            self._nav_total_dur_s = total_dur_s

    @property
    def map_manager(self) -> MapManager:
        """MapManager への参照（ナビエンジンが経路をセットするために使用）。"""
        return self._map


def _draw_nav_panel(
    screen, w: int, h: int, pygame,
    font_large, font_medium, font_small,
    next_instr: str, next_dist_m: float,
    dest_name: str, total_dist_m: float, total_dur_s: float,
    paused: bool,
) -> None:
    """ナビ情報パネルを画面左下に描画する。"""
    PAD = 12
    PANEL_W = 340
    PANEL_H = 110
    panel_x = PAD
    panel_y = h - PANEL_H - PAD

    bg = pygame.Surface((PANEL_W, PANEL_H), pygame.SRCALPHA)
    bg.fill((0, 0, 0, 210))
    screen.blit(bg, (panel_x, panel_y))

    # 一時停止中の表示
    if paused:
        pause_s = font_large.render("⏸ 案内一時停止中", True, (255, 200, 60))
        screen.blit(pause_s, (panel_x + 10, panel_y + (PANEL_H - pause_s.get_height()) // 2))
        return

    y_cur = panel_y + 8

    # 次の案内（方向 + 距離）
    if next_dist_m > 0 and next_instr:
        if next_dist_m >= 1000:
            dist_str = f"{next_dist_m / 1000:.1f}km先"
        else:
            dist_str = f"{int(next_dist_m)}m先"
        instr_s = font_large.render(f"↗ {dist_str} {next_instr}", True, (255, 255, 255))
        screen.blit(instr_s, (panel_x + 8, y_cur))
        y_cur += instr_s.get_height() + 4

    # 目的地名
    if dest_name:
        dest_s = font_small.render(f"目的地: {dest_name}", True, (200, 200, 200))
        screen.blit(dest_s, (panel_x + 8, y_cur))
        y_cur += dest_s.get_height() + 2

    # 残距離・残時間
    if total_dist_m > 0:
        km_str = f"{total_dist_m / 1000:.1f}km"
        min_str = f"{int(total_dur_s / 60)}分"
        remain_s = font_small.render(f"残り {km_str}  約{min_str}", True, (160, 220, 160))
        screen.blit(remain_s, (panel_x + 8, y_cur))


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    lines = []
    for paragraph in text.splitlines():
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            test = current + ch
            if font.size(test)[0] <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


# ── スリープ時地図ダッシュボード ──────────────────────────────────────────────

_DASH_BG      = (12, 12, 20)
_DASH_HOT     = (255, 140, 60)
_DASH_SESSION = (180, 150, 255)
_DASH_HINT    = (80, 80, 100)

_sysinfo_cache: dict = {}
_sysinfo_cache_at: float = 0.0
_SYSINFO_TTL = 5.0


def _get_sysinfo() -> dict:
    global _sysinfo_cache, _sysinfo_cache_at
    now = time.monotonic()
    if now - _sysinfo_cache_at < _SYSINFO_TTL:
        return _sysinfo_cache
    from hardware.system import _read_cpu_temp, _wifi_mode, _eth1_connected
    _sysinfo_cache = {
        "temp": _read_cpu_temp(),
        "wifi_mode": _wifi_mode(),
        "eth1": _eth1_connected(),
    }
    _sysinfo_cache_at = now
    return _sysinfo_cache


def _draw_map_dashboard(
    screen, w: int, h: int, pygame,
    map_manager, font_large, font_medium, font_small,
    session_name: str, char_frames: dict, get_image, get_scaled,
) -> None:
    """地図を背景に、上部バー＋自車マーカー＋右下アバターを描画する。"""
    import pygame as pg  # type: ignore

    # ── 地図背景 ──────────────────────────────────────────────────────────────
    map_surf = map_manager.render_map(w, h, pg)
    if map_surf:
        screen.blit(map_surf, (0, 0))
    else:
        screen.fill(_DASH_BG)
        hint = font_small.render("GPS 位置を取得中...", True, _DASH_HINT)
        screen.blit(hint, hint.get_rect(centerx=w // 2, centery=h // 2))

    # ── 上部バー（半透明） ────────────────────────────────────────────────────
    BAR_H = 54
    bar = pg.Surface((w, BAR_H), pg.SRCALPHA)
    bar.fill((0, 0, 0, 190))
    screen.blit(bar, (0, 0))

    # 時刻
    now_dt = datetime.now()
    time_str = now_dt.strftime("%H:%M")
    surf = font_large.render(time_str, True, (255, 255, 255))
    screen.blit(surf, (16, (BAR_H - surf.get_height()) // 2))
    x_cursor = 16 + surf.get_width() + 20

    # GPS 状態・速度
    _, _, speed, has_fix = map_manager.get_gps()
    gps_color = (100, 230, 100) if has_fix else (230, 100, 100)
    if has_fix and speed is not None:
        gps_str = f"▲ {speed:.0f} km/h"
    elif has_fix:
        gps_str = "▲ GPS"
    else:
        gps_str = "× GPS"
    surf = font_medium.render(gps_str, True, gps_color)
    screen.blit(surf, (x_cursor, (BAR_H - surf.get_height()) // 2))
    x_cursor += surf.get_width() + 24

    # CPU 温度
    sysinfo = _get_sysinfo()
    temp = sysinfo.get("temp")
    if temp is not None:
        temp_color = _DASH_HOT if temp >= 70 else (220, 220, 220)
        surf = font_medium.render(f"{temp:.0f}°C", True, temp_color)
        screen.blit(surf, (x_cursor, (BAR_H - surf.get_height()) // 2))
        x_cursor += surf.get_width() + 24

    # USBテザリング (eth1)
    eth1_ok = sysinfo.get("eth1", False)
    eth1_color = (80, 220, 80) if eth1_ok else (160, 60, 60)
    surf = font_small.render("USB●" if eth1_ok else "USB×", True, eth1_color)
    screen.blit(surf, (x_cursor, (BAR_H - surf.get_height()) // 2))
    x_cursor += surf.get_width() + 16

    # WiFi モード
    wifi_mode = sysinfo.get("wifi_mode", "none")
    if wifi_mode == "client":
        wifi_label, wifi_color = "WiFi-C", (80, 200, 255)
    elif wifi_mode == "ap":
        wifi_label, wifi_color = "WiFi-AP", (255, 200, 60)
    else:
        wifi_label, wifi_color = "WiFi×", (160, 60, 60)
    surf = font_small.render(wifi_label, True, wifi_color)
    screen.blit(surf, (x_cursor, (BAR_H - surf.get_height()) // 2))

    # セッション名（右端）
    if session_name:
        surf = font_medium.render(session_name, True, _DASH_SESSION)
        screen.blit(surf, (w - surf.get_width() - 16, (BAR_H - surf.get_height()) // 2))

    # ── 自車位置マーカー（画面中央） ─────────────────────────────────────────
    if map_surf:
        cx, cy = w // 2, h // 2
        pg.draw.circle(screen, (255, 255, 255), (cx, cy), 14)
        pg.draw.circle(screen, (30, 120, 255), (cx, cy), 11)
        pg.draw.circle(screen, (255, 255, 255), (cx, cy), 4)

    # ── アバター（右下隅、小さく） ────────────────────────────────────────────
    AVATAR_H = h // 4
    idle_data = char_frames.get("idle") or char_frames.get(next(
        (k for k in char_frames if not k.startswith("_")), None
    ))
    if idle_data:
        seq = idle_data.get("loop") or idle_data.get("intro")
        if seq:
            img_path, _ = seq[0]
            surf = get_image(img_path)
            if surf:
                iw, ih = surf.get_size()
                scale = AVATAR_H / ih
                aw, ah = int(iw * scale), int(ih * scale)
                avatar_surf = get_scaled(img_path, aw, ah)
                if avatar_surf:
                    screen.blit(avatar_surf, (w - aw - 12, h - ah - 12))
