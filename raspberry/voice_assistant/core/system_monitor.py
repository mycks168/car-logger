"""システム状態監視モジュール。バッテリー・WiFi などの変化を MonitorEvent として通知する。"""

import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("voice-assistant.monitor")

BATTERY_WARN_THRESHOLDS = [70, 50]
BATTERY_CRITICAL_THRESHOLD = 30

SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "sounds")


def _wav_path(kind: str) -> str:
    return os.path.join(SOUNDS_DIR, f"{kind}.wav")


@dataclass
class MonitorEvent:
    kind: str
    tts_text: str
    display_text: str
    display_color: tuple[int, int, int]
    display_subtitle: str = ""
    severity: str = "info"
    wav_path: str | None = None
    after_cb: Callable[[], None] | None = field(default=None, repr=False)


class BatteryChecker:
    def __init__(self):
        self._last_charging: bool | None = None
        self._notified: set[int] = set()

    def check(self, read_battery_fn: Callable) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        pct, status = read_battery_fn()
        if pct is None:
            return events

        is_charging = (status == "Charging" or status == "Full")

        if self._last_charging is None:
            self._last_charging = is_charging
            return events

        if is_charging != self._last_charging:
            if is_charging:
                events.append(MonitorEvent(
                    kind="charging",
                    tts_text="充電が開始されました。",
                    display_text="充電開始",
                    display_color=(80, 200, 80),
                    display_subtitle=f"バッテリー {pct}%",
                    severity="info",
                    wav_path=_wav_path("charging"),
                ))
                self._notified.clear()
            else:
                events.append(MonitorEvent(
                    kind="discharging",
                    tts_text="電源が切断されました。バッテリー駆動に切り替わります。",
                    display_text="電源切断",
                    display_color=(220, 200, 60),
                    display_subtitle=f"バッテリー {pct}%",
                    severity="info",
                    wav_path=_wav_path("discharging"),
                ))
            self._last_charging = is_charging

        if not is_charging:
            if pct < BATTERY_CRITICAL_THRESHOLD and BATTERY_CRITICAL_THRESHOLD not in self._notified:
                self._notified.add(BATTERY_CRITICAL_THRESHOLD)
                events.append(MonitorEvent(
                    kind="battery_critical",
                    tts_text="バッテリー残量が不足しています。まもなくシャットダウンします。充電してください。",
                    display_text=f"バッテリー残量 {pct}%",
                    display_color=(220, 60, 60),
                    display_subtitle="シャットダウンします",
                    severity="critical",
                    wav_path=_wav_path("battery_critical"),
                    after_cb=_do_shutdown,
                ))
            else:
                for threshold in sorted(BATTERY_WARN_THRESHOLDS, reverse=True):
                    if pct < threshold and threshold not in self._notified:
                        self._notified.add(threshold)
                        color = (220, 140, 40) if threshold == 50 else (200, 200, 60)
                        subtitle = "早めの充電をお勧めします" if threshold == 50 else "充電をご検討ください"
                        events.append(MonitorEvent(
                            kind=f"battery_{threshold}",
                            tts_text=f"バッテリー残量が{threshold}パーセントを切りました。",
                            display_text=f"バッテリー {pct}%",
                            display_color=color,
                            display_subtitle=subtitle,
                            severity="warning",
                            wav_path=_wav_path(f"battery_{threshold}"),
                        ))
                        break

        return events


class WiFiChecker:
    def __init__(self):
        self._last_connected: bool | None = None

    def check(self, connected_fn: Callable) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        connected = connected_fn()

        if self._last_connected is None:
            self._last_connected = connected
            events.append(MonitorEvent(
                kind="wifi_off" if not connected else "wifi_on",
                tts_text="",
                display_text="",
                display_color=(200, 80, 80),
                severity="silent",
            ))
            return events

        if connected != self._last_connected:
            if connected:
                events.append(MonitorEvent(
                    kind="wifi_on",
                    tts_text="WiFiに接続されました。",
                    display_text="WiFi 接続",
                    display_color=(80, 200, 80),
                    display_subtitle="ネットワーク利用可能",
                    severity="info",
                    wav_path=_wav_path("wifi_on"),
                ))
            else:
                events.append(MonitorEvent(
                    kind="wifi_off",
                    tts_text="WiFiが切断されました。",
                    display_text="WiFi 切断",
                    display_color=(200, 80, 80),
                    display_subtitle="ネットワーク利用不可",
                    severity="info",
                    wav_path=_wav_path("wifi_off"),
                ))
            self._last_connected = connected

        return events


def _do_shutdown():
    log.warning("バッテリー残量不足 -- シャットダウンします")
    try:
        subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
    except OSError as e:
        log.error("シャットダウンコマンド実行失敗: %s", e)


class SystemMonitor:
    def __init__(self, on_event: Callable[[MonitorEvent], None], poll_interval: float = 10.0):
        self._on_event = on_event
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="system-monitor")
        self._battery = BatteryChecker()
        self._wifi = WiFiChecker()
        self._extra_checkers: list[tuple[Callable, Callable]] = []

    def start(self):
        self._thread.start()
        log.info("システムモニター開始 (poll_interval=%.1fs)", self._poll_interval)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        log.info("システムモニター停止")

    def _run(self):
        self._stop.wait(timeout=5.0)
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as e:
                log.error("ポーリングエラー: %s", e)
            self._stop.wait(timeout=self._poll_interval)

    def _poll(self):
        from hardware.system import _read_battery, _wifi_connected

        for event in self._battery.check(_read_battery):
            log.info("システムイベント [%s]: %s", event.kind, event.display_text)
            self._on_event(event)

        for event in self._wifi.check(_wifi_connected):
            log.info("システムイベント [%s]: %s", event.kind, event.display_text)
            self._on_event(event)

        for checker_fn, data_fn in self._extra_checkers:
            for event in checker_fn(data_fn):
                log.info("システムイベント [%s]: %s", event.kind, event.display_text)
                self._on_event(event)
