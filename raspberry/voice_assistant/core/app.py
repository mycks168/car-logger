"""アシスタントのメインロジック。"""

import logging
import os
import queue
import re
import subprocess
import threading
import time

import config
from hardware import create_display
from hardware.button import ButtonPTT, State
from hardware.audio import check_audio_level, Recorder
from services.stt import transcribe
from services.tts import TTSPlayer
from services.llm import stream_response
from core.system_monitor import SystemMonitor, MonitorEvent
from core.session_manager import SessionManager
from services.webhook import WebhookServer
from navigation.engine import NavigationEngine
from navigation.poi import POIManager

log = logging.getLogger("voice-assistant")


class Assistant:
    def __init__(self):
        config.print_config()

        self.display = create_display(backlight=100)
        self.recorder = Recorder()
        self.recorder.warmup()
        self.ptt = ButtonPTT(
            self.display.board,
            on_press_cb=self._on_button_press,
            on_release_cb=self._on_button_release,
            on_cancel_cb=self._on_button_cancel,
            cancel_allowed_cb=lambda: (time.monotonic() - self._state_entered_at) >= 2.0,
            on_any_press_cb=self._touch,
            on_abort_listening_cb=self._on_abort_listening,
            on_double_click_cb=self._on_double_click,
        )
        self._session_manager = SessionManager(config.OPENCLAW_SESSIONS)
        session = self._session_manager.current or {}
        self.display.set_session_name(session.get("name", ""))
        self._worker_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._dismiss = threading.Event()
        self._worker_gen = 0
        self._response_hold_timeout = 30
        self._sleep_timeout = 60
        self._last_activity = time.monotonic()
        self._last_idle_refresh = 0.0
        self._state_entered_at = 0.0
        self._tts = TTSPlayer() if config.ENABLE_TTS else None
        self._thinking_proc: subprocess.Popen | None = None
        self._thinking_wavs: list[str] = self._load_thinking_wavs()
        if self._tts:
            self._tts.on_start_cb = lambda: self.display.set_character_state("talking")
        self._conversation_history: list[dict] = []

        self._announce_queue: queue.Queue[MonitorEvent] = queue.Queue()
        self._critical_lock = threading.Lock()
        self._critical_event: MonitorEvent | None = None
        self._led_color: tuple[int, int, int] = (-1, -1, -1)
        self._wifi_connected: bool = True
        self._monitor = SystemMonitor(on_event=self._on_monitor_event)
        self._monitor.start()

        # ナビゲーションエンジン
        self._nav = NavigationEngine(
            get_gps=self.display.map_manager.get_gps,
            on_speak=self._nav_speak,
            on_state_change=self._on_nav_state_change,
        )

        # POI マネージャ
        self._poi = POIManager(get_gps=self.display.map_manager.get_gps)
        self._poi.start()

        self._webhook: WebhookServer | None = None
        if config.WEBHOOK_ENABLED:
            self._webhook = WebhookServer(
                on_message=self._on_webhook_message,
                on_navigate=self._on_webhook_navigate,
                on_navigate_stop=self._on_webhook_navigate_stop,
                on_navigate_pause=self._on_webhook_navigate_pause,
                on_map_zoom=self._on_webhook_map_zoom,
                on_get_location=self._on_webhook_get_location,
            )
            self._webhook.start()

    def _is_stale(self, my_gen: int) -> bool:
        return self._worker_gen != my_gen

    def _touch(self):
        self._last_activity = time.monotonic()
        if self.display.is_sleeping:
            self.display.wake()
            self._go_idle()

    def _on_double_click(self):
        session = self._session_manager.next()
        if session is None:
            return
        self._conversation_history = []
        name = session.get("name", "セッション")
        log.info("session switched: %r", name)
        self.display.set_session_name(name)
        self.display.set_status(
            f"{name}",
            color=(180, 140, 255),
            subtitle="セッション切り替え",
            accent_color=(120, 60, 200),
        )
        if self._tts:
            self._tts.submit(f"{name}に切り替わりました")
            self._tts.flush()
        else:
            time.sleep(1.5)
        self._go_idle()

    def _on_button_cancel(self):
        self._touch()
        self._worker_gen += 1
        self._dismiss.set()
        self._stop_thinking_sound()
        self.display.stop_spinner()
        self.display.stop_character()
        if self._tts:
            self._tts.cancel()
        self._go_idle()
        log.info("button cancel -- back to Ready")

    def _on_abort_listening(self):
        self.recorder.cancel()
        self.display.stop_character()
        self._go_idle()
        log.info("abort listening -- back to Ready")

    def _on_button_press(self):
        self._touch()
        self._dismiss.set()
        log.info("button pressed -- start recording")
        if self._tts:
            self.display.start_character("listening", self._tts)
        else:
            self.display.set_status(
                "Listening...",
                color=(140, 200, 255),
                subtitle="Speak now",
                accent_color=(60, 140, 255),
            )
        try:
            self.recorder.start()
        except Exception as e:
            log.error("recording start failed: %s", e)
            self._show_error(str(e))

    def _on_button_release(self):
        log.info("button released -- processing")
        t = threading.Thread(target=self._process_utterance, daemon=True)
        t.start()
        self._worker_thread = t

    def _process_utterance(self):
        my_gen = self._worker_gen
        try:
            self._process_utterance_inner(my_gen)
        except Exception as e:
            if not self._is_stale(my_gen):
                log.error("error: %s", e)
                self.display.stop_spinner()
                self.display.stop_character()
                self._show_error(str(e)[:80])
        finally:
            self.display.stop_spinner()
            if not self._is_stale(my_gen) and self.ptt.state in (
                State.TRANSCRIBING, State.THINKING, State.STREAMING,
            ):
                self._go_idle()

    def _process_utterance_inner(self, my_gen: int):
        wav_path = self.recorder.stop()

        rms = check_audio_level(wav_path)
        if rms < config.SILENCE_RMS_THRESHOLD:
            log.info("silence detected (RMS=%.0f), skipping", rms)
            if self._is_stale(my_gen):
                return
            self.display.stop_character()
            self.display.set_status(
                "No speech detected",
                color=(160, 160, 160),
                subtitle="Try again",
                accent_color=(80, 80, 80),
            )
            time.sleep(1.5)
            if not self._is_stale(my_gen):
                self._go_idle()
            return

        if self._is_stale(my_gen):
            return

        self._state_entered_at = time.monotonic()
        self.ptt.state = State.TRANSCRIBING
        if self._tts:
            self.display.set_character_state("thinking")
        else:
            self.display.set_status(
                "Transcribing...",
                color=(255, 230, 100),
                subtitle="One moment",
                accent_color=(255, 180, 0),
            )
        t0 = time.monotonic()
        transcript = transcribe(wav_path)
        log.info("transcribe took %.1fs => %r", time.monotonic() - t0, (transcript[:80] if transcript else "(empty)"))

        if not transcript or self._is_stale(my_gen):
            if not self._is_stale(my_gen):
                log.info("empty transcript, returning to idle")
                self._go_idle()
            return

        if self._is_stale(my_gen):
            return
        self._state_entered_at = time.monotonic()
        self.ptt.state = State.THINKING
        if not self._tts:
            self.display.start_spinner("Thinking")
        self._play_thinking_sound()

        self.ptt.state = State.STREAMING
        self._update_led()
        first_token = True
        full_response = ""
        tts_buffer = ""
        stream_t0 = time.monotonic()

        session = self._session_manager.current or {}
        for delta in stream_response(
            transcript,
            history=self._conversation_history,
            agent_id=session.get("agent_id", ""),
            session_key=session.get("session_key", ""),
            base_url=session.get("base_url", ""),
            token=session.get("token", ""),
        ):
            if self._is_stale(my_gen) or self._shutdown.is_set():
                break
            if first_token:
                log.info("first token after %.1fs", time.monotonic() - stream_t0)
                self._stop_thinking_sound()
                if not self._tts:
                    self.display.stop_spinner()
                    self.display.set_response_text("")
                first_token = False
            full_response += delta
            if not self._tts or getattr(self.display, "show_response_during_tts", False):
                self.display.append_response(delta)

            if self._tts:
                tts_buffer += delta
                while True:
                    m = re.search(r"[。！？.!?][\s　]?|\n", tts_buffer)
                    if not m:
                        break
                    cut = m.end()
                    chunk = tts_buffer[:cut].strip()
                    tts_buffer = tts_buffer[cut:]
                    if chunk:
                        self._tts.submit(chunk)

        if self._is_stale(my_gen):
            return

        log.info("stream done in %.1fs, %d chars", time.monotonic() - stream_t0, len(full_response))

        if self._tts:
            if tts_buffer.strip():
                self._tts.submit(tts_buffer.strip())
            self._tts.flush()
            self.display.stop_character()
            self.display.set_response_text(full_response)
        else:
            self.display.flush_response()

        log.info("response complete -- holding on screen")

        self._conversation_history.append({"role": "user", "content": transcript})
        self._conversation_history.append({"role": "assistant", "content": full_response})
        max_msgs = config.CONVERSATION_HISTORY_LENGTH * 2
        if len(self._conversation_history) > max_msgs:
            self._conversation_history = self._conversation_history[-max_msgs:]

        self._dismiss.clear()
        self._dismiss.wait(timeout=self._response_hold_timeout)

        if self._is_stale(my_gen):
            return

        if self._dismiss.is_set():
            log.info("dismissed by button press")
        else:
            log.info("display timeout, returning to idle")

        self._go_idle()

    def _update_led(self) -> None:
        if self.ptt.state == State.STREAMING:
            color = (0, 0, 255)
        elif self._wifi_connected:
            color = (0, 255, 0)
        else:
            color = (255, 0, 0)
        if color != self._led_color:
            self.display.set_led(*color)
            self._led_color = color

    def _go_idle(self):
        self._last_activity = time.monotonic()
        self._last_idle_refresh = time.monotonic()
        self.ptt.state = State.IDLE
        self.display.set_backlight(100)
        self.display.start_character(state="idle")
        self._update_led()

    @staticmethod
    def _load_thinking_wavs() -> list[str]:
        sounds_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "sounds", "thinking")
        if not os.path.isdir(sounds_dir):
            return []
        return sorted(
            os.path.join(sounds_dir, f)
            for f in os.listdir(sounds_dir)
            if f.endswith(".wav")
        )

    def _play_thinking_sound(self) -> None:
        import random
        if not self._thinking_wavs:
            return
        wav_path = random.choice(self._thinking_wavs)
        try:
            self._thinking_proc = subprocess.Popen(
                ["aplay", "-q", "-D", config.AUDIO_OUTPUT_DEVICE, wav_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as e:
            log.warning("thinking sound 再生失敗: %s", e)

    def _stop_thinking_sound(self) -> None:
        proc = self._thinking_proc
        self._thinking_proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _show_error(self, msg: str):
        self.ptt.state = State.ERROR
        self.display.stop_character()
        self.display.set_status(
            msg[:50] + ("..." if len(msg) > 50 else ""),
            color=(255, 120, 120),
            subtitle="Something went wrong",
            accent_color=(200, 0, 0),
        )
        time.sleep(3)
        self._go_idle()

    def _on_webhook_message(self, text: str, title: str | None):
        display_text = (title or text)[:50]
        event = MonitorEvent(
            kind="webhook",
            tts_text=text,
            display_text=display_text,
            display_color=(160, 200, 255),
            display_subtitle="メッセージ" if not title else None,
            severity="info",
        )
        self._announce_queue.put(event)

    def _nav_speak(self, text: str):
        """ナビエンジンから呼ばれる TTS 読み上げ（会話割り込みなし）。"""
        if self._tts:
            self._tts.submit(text)

    def _on_nav_state_change(self):
        """ナビ状態が変わったとき地図と Display を更新する。"""
        state = self._nav.get_state()
        map_mgr = self.display.map_manager

        if state.active:
            # 経路・目的地を地図にセット
            map_mgr.set_route(
                route_coords=state.route_coords,
                dest=(state.dest_lat, state.dest_lon),
                dest_name=state.dest_name,
            )
            # 次のステップ情報を取得
            steps = state.steps
            idx = state.step_index
            if idx < len(steps):
                next_step = steps[idx]
                next_instr = next_step.instruction
                next_dist = 0.0
                lat, lon, _, _ = map_mgr.get_gps()
                if lat is not None:
                    import math
                    dlat = math.radians(next_step.lat - lat)
                    dlon = math.radians(next_step.lon - lon)
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(math.radians(lat))
                         * math.cos(math.radians(next_step.lat))
                         * math.sin(dlon / 2) ** 2)
                    next_dist = 6_371_000 * 2 * math.atan2(
                        math.sqrt(a), math.sqrt(1 - a))
            else:
                next_instr = "目的地"
                next_dist = 0.0

            self.display.update_nav(
                active=True,
                paused=state.paused,
                next_instruction=next_instr,
                next_distance_m=next_dist,
                dest_name=state.dest_name,
                total_dist_m=state.total_distance_m,
                total_dur_s=state.total_duration_s,
            )
        else:
            map_mgr.clear_route()
            self.display.update_nav(active=False)

    def _refresh_nav_display(self):
        """地図再描画なしでナビパネルの数値だけ更新する（run() ループから毎秒呼ぶ）。"""
        state = self._nav.get_state()
        if not state.active:
            return
        import math
        steps = state.steps
        idx = state.step_index
        if idx < len(steps):
            next_step = steps[idx]
            next_instr = next_step.instruction
            lat, lon, _, _ = self.display.map_manager.get_gps()
            if lat is not None and lon is not None:
                dlat = math.radians(next_step.lat - lat)
                dlon = math.radians(next_step.lon - lon)
                a = (math.sin(dlat / 2) ** 2
                     + math.cos(math.radians(lat))
                     * math.cos(math.radians(next_step.lat))
                     * math.sin(dlon / 2) ** 2)
                next_dist = 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            else:
                next_dist = 0.0
        else:
            next_instr = "目的地"
            next_dist = 0.0
        self.display.update_nav(
            active=True,
            paused=state.paused,
            next_instruction=next_instr,
            next_distance_m=next_dist,
            dest_name=state.dest_name,
            total_dist_m=state.total_distance_m,
            total_dur_s=state.total_duration_s,
        )

    def _on_webhook_get_location(self) -> dict:
        lat, lon, speed, has_fix = self.display.map_manager.get_gps()
        return {
            "has_fix": has_fix,
            "lat": lat,
            "lon": lon,
            "speed_kmh": speed,
        }

    def _on_webhook_navigate(self, lat: float, lon: float, name: str):
        ok = self._nav.start(lat, lon, name)
        if not ok:
            self._nav_speak("経路の計算に失敗しました。")

    def _on_webhook_navigate_stop(self):
        self._nav.stop()
        if self._tts:
            self._tts.submit("案内を終了しました。")

    def _on_webhook_navigate_pause(self):
        paused = self._nav.toggle_pause()
        state = self._nav.get_state()
        self.display.update_nav(
            active=state.active,
            paused=paused,
            dest_name=state.dest_name,
            total_dist_m=state.total_distance_m,
            total_dur_s=state.total_duration_s,
        )
        msg = "案内を一時停止しました。" if paused else "案内を再開します。"
        if self._tts:
            self._tts.submit(msg)

    def _on_webhook_map_zoom(self, delta: int | None, level: int | None):
        map_mgr = self.display.map_manager
        if level is not None:
            map_mgr.set_zoom(level)
            if self._tts:
                self._tts.submit(f"ズームレベル{level}にしました。")
        elif delta is not None:
            map_mgr.change_zoom(delta)
            new_zoom = map_mgr.zoom
            direction = "ズームイン" if delta > 0 else "ズームアウト"
            if self._tts:
                self._tts.submit(f"{direction}しました。")

    def _on_monitor_event(self, event: MonitorEvent):
        if event.kind == "wifi_off":
            self._wifi_connected = False
            self._update_led()
        elif event.kind == "wifi_on":
            self._wifi_connected = True
            self._update_led()

        if event.severity == "silent":
            return
        if event.severity == "critical":
            with self._critical_lock:
                self._critical_event = event
        else:
            self._announce_queue.put(event)

    def _play_alert_wav(self, wav_path: str | None) -> bool:
        if not wav_path or not os.path.isfile(wav_path):
            return False
        try:
            subprocess.run(
                ["aplay", "-q", "-D", config.AUDIO_OUTPUT_DEVICE, wav_path],
                timeout=30,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.warning("アラート WAV 再生失敗: %s", e)
            return False

    def _announce_audio(self, event: MonitorEvent):
        if self._play_alert_wav(event.wav_path):
            return
        if self._tts and event.tts_text:
            self._tts.submit(event.tts_text)
            self._tts.flush()
        else:
            time.sleep(2.5)

    def _handle_announce(self, event: MonitorEvent):
        self.display.stop_character()
        self.display.set_status(
            event.display_text,
            color=event.display_color,
            subtitle=event.display_subtitle or None,
            accent_color=tuple(max(0, c - 60) for c in event.display_color),
        )
        self._announce_audio(event)
        if event.after_cb:
            event.after_cb()
        else:
            self._go_idle()

    def _handle_critical_event(self, event: MonitorEvent):
        self._worker_gen += 1
        self._dismiss.set()
        self.display.stop_spinner()
        self.display.stop_character()
        if self._tts:
            self._tts.cancel()
            time.sleep(0.3)
        self.display.set_status(
            event.display_text,
            color=event.display_color,
            subtitle=event.display_subtitle or None,
            accent_color=(180, 0, 0),
        )
        self._announce_audio(event)
        if event.after_cb:
            event.after_cb()
        else:
            self._go_idle()

    def run(self):
        self._go_idle()
        log.info("assistant ready -- press button to talk")
        _last_poi_sync = 0.0

        try:
            while not self._shutdown.is_set():
                self._shutdown.wait(timeout=1.0)
                worker_busy = self._worker_thread is not None and self._worker_thread.is_alive()

                # POI を定期的に地図へ反映（60秒ごと）
                now = time.monotonic()
                if now - _last_poi_sync >= 60.0:
                    self.display.map_manager.set_pois(self._poi.get_pois())
                    _last_poi_sync = now

                # ナビパネル表示を毎秒更新（残り距離・時間を再計算するが地図再描画は不要）
                self._refresh_nav_display()

                with self._critical_lock:
                    crit = self._critical_event
                    self._critical_event = None
                if crit is not None:
                    self._handle_critical_event(crit)
                    continue

                if self.ptt.state == State.IDLE and not worker_busy:
                    try:
                        event = self._announce_queue.get_nowait()
                        self._handle_announce(event)
                        continue
                    except queue.Empty:
                        pass

                if (
                    not self.display.is_sleeping
                    and self.ptt.state == State.IDLE
                    and not worker_busy
                    and time.monotonic() - self._last_activity > self._sleep_timeout
                ):
                    log.info("idle timeout -- sleeping display")
                    self.display.sleep()
        except KeyboardInterrupt:
            log.info("shutting down...")
        finally:
            self.shutdown()

    def shutdown(self):
        self._shutdown.set()
        self._worker_gen += 1
        self._dismiss.set()
        self._stop_thinking_sound()
        self.recorder.cancel()
        if self._tts:
            self._tts.cancel()
        self.display.stop_character()
        self._monitor.stop()
        self._nav.shutdown()
        self._poi.stop()
        if self._webhook:
            self._webhook.stop()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)
        self.display.cleanup()
        log.info("cleanup done")
