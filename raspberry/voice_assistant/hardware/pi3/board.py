"""Raspberry Pi 3 向け GPIO PTT ボード。gpiozero + lgpio を使用。

必要パッケージ:
    uv sync --extra pi3

設定:
    PI3_PTT_GPIO=17  # BCM ピン番号
"""
import logging
import threading
import config

log = logging.getLogger("voice-assistant")

_STARTUP_IGNORE_SEC = 1.5


class Pi3Board:
    """GPIO ボタンを ButtonPTT インターフェースに合わせるアダプタ。"""

    def __init__(self):
        from gpiozero import Button  # pyright: ignore[reportMissingImports]
        pin = config.PI3_PTT_GPIO
        self._button = Button(pin, pull_up=True, bounce_time=0.05)
        self._press_cb = None
        self._release_cb = None
        self._ready = False
        threading.Timer(_STARTUP_IGNORE_SEC, self._enable).start()
        self._button.when_pressed = self._on_press
        self._button.when_released = self._on_release
        log.info("Pi3Board: GPIO%d で PTT 初期化 (pull_up=True, 起動後 %.1fs 無視)", pin, _STARTUP_IGNORE_SEC)

    def _enable(self):
        self._ready = True
        log.info("Pi3Board: ボタン有効化")

    def on_button_press(self, cb):
        self._press_cb = cb

    def on_button_release(self, cb):
        self._release_cb = cb

    def set_backlight_color(self, r: int, g: int, b: int):
        pass  # Pi3 に LCD バックライトなし

    def set_rgb(self, r: int, g: int, b: int):
        pass  # Pi3 に RGB LED なし

    def _on_press(self):
        if not self._ready:
            log.debug("Pi3Board: 起動直後のイベント無視 (press)")
            return
        if self._press_cb:
            self._press_cb()

    def _on_release(self):
        if not self._ready:
            log.debug("Pi3Board: 起動直後のイベント無視 (release)")
            return
        if self._release_cb:
            self._release_cb()
