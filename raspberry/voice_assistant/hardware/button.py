"""PTT ボタン状態管理。ハードウェア非依存の共通コンポーネント。"""
import threading
import time
from enum import Enum

_DOUBLE_CLICK_MAX_PRESS = 0.35
_DOUBLE_CLICK_MAX_GAP = 0.50


class State(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    STREAMING = "streaming"
    ERROR = "error"


STATE_COLORS = {
    State.IDLE:          (90, 90, 90),
    State.LISTENING:     (0, 80, 255),
    State.TRANSCRIBING:  (255, 200, 0),
    State.THINKING:      (255, 200, 0),
    State.STREAMING:     (0, 200, 50),
    State.ERROR:         (255, 0, 0),
}


class ButtonPTT:
    """PTT ボタンと状態マシンを管理する。ダブルクリックでセッション切り替え。"""

    def __init__(
        self, board,
        on_press_cb=None, on_release_cb=None, on_cancel_cb=None,
        cancel_allowed_cb=None, on_any_press_cb=None,
        on_abort_listening_cb=None, on_double_click_cb=None,
    ):
        self._board = board
        self._on_press = on_press_cb
        self._on_release = on_release_cb
        self._on_cancel = on_cancel_cb
        self._on_any_press = on_any_press_cb
        self._on_abort_listening = on_abort_listening_cb
        self._cancel_allowed = cancel_allowed_cb
        self._on_double_click = on_double_click_cb
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._press_start_time: float = 0.0
        self._last_quick_tap_time: float = 0.0

        board.on_button_press(self._handle_press)
        board.on_button_release(self._handle_release)

    @property
    def state(self) -> State:
        return self._state

    @state.setter
    def state(self, new_state: State):
        with self._lock:
            self._state = new_state
            self._update_led(new_state)

    def _update_led(self, state: State):
        if state == State.IDLE:
            return
        color = STATE_COLORS.get(state, (40, 40, 40))
        try:
            self._board.set_backlight_color(*color)
        except AttributeError:
            pass

    def _handle_press(self):
        self._press_start_time = time.monotonic()
        if self._on_any_press:
            self._on_any_press()
        if self._state == State.LISTENING:
            if self._on_abort_listening:
                self._on_abort_listening()
            self._state = State.IDLE
            self._update_led(State.IDLE)
            return
        if self._state in (State.TRANSCRIBING, State.THINKING):
            if self._cancel_allowed and not self._cancel_allowed():
                return
            self._state = State.IDLE
            self._update_led(State.IDLE)
            if self._on_cancel:
                self._on_cancel()
            return
        if self._state == State.STREAMING:
            if self._on_cancel:
                self._on_cancel()
        if self._state not in (State.IDLE, State.ERROR):
            return
        self._state = State.LISTENING
        self._update_led(State.LISTENING)
        if self._on_press:
            self._on_press()

    def _handle_release(self):
        if self._state != State.LISTENING:
            return
        now = time.monotonic()
        duration = now - self._press_start_time
        if duration < _DOUBLE_CLICK_MAX_PRESS:
            gap = now - self._last_quick_tap_time
            if gap < _DOUBLE_CLICK_MAX_GAP and self._on_double_click:
                self._last_quick_tap_time = 0.0
                self._state = State.IDLE
                self._update_led(State.IDLE)
                if self._on_abort_listening:
                    self._on_abort_listening()
                self._on_double_click()
            else:
                self._last_quick_tap_time = now
                self._state = State.IDLE
                self._update_led(State.IDLE)
                if self._on_abort_listening:
                    self._on_abort_listening()
            return
        if self._on_release:
            self._on_release()
