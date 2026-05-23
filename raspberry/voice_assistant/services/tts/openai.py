"""OpenAI TTS playback with pre-fetching for gapless sentence transitions."""

import math
import queue
import struct
import subprocess
import threading
import time

import requests

import config
from services.tts.filter import apply_tts_filter

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

_SENTINEL = object()
_MOUTH_WINDOW_MS = 80


class TTSPlayer:
    """Two-thread pipeline: fetcher downloads WAV ahead, player plays without gaps."""

    def __init__(self):
        self._submit_q: queue.Queue[str | object] = queue.Queue()
        self._play_q: queue.Queue[tuple[str, bytes] | object] = queue.Queue(maxsize=2)
        self._cancel = threading.Event()
        self._done = threading.Event()

        self._full_text = ""
        self._mouth_timeline: list[float] = []
        self._playback_start: float = 0.0
        self._playback_duration: float = 0.0
        self.is_speaking = threading.Event()
        self._aplay_proc: subprocess.Popen | None = None

        self._volume_set = False

        self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True)
        self._player = threading.Thread(target=self._play_loop, daemon=True)
        self._fetcher.start()
        self._player.start()

    @property
    def current_text(self) -> str:
        """Return a short trailing fragment matching what the voice is saying right now."""
        if not self.is_speaking.is_set() or self._playback_duration <= 0:
            return ""
        text = self._full_text
        if not text:
            return ""
        words = text.split()
        if not words:
            return ""
        # Offset to account for audio pipeline latency so text doesn't lead the voice
        elapsed = time.monotonic() - self._playback_start - 0.25
        if elapsed < 0:
            return ""
        progress = min(1.0, elapsed / self._playback_duration)
        word_idx = min(int(progress * len(words)), len(words) - 1)
        # Trailing window: show the word being spoken and the few before it
        end = word_idx + 1
        start = max(0, end - 4)
        return " ".join(words[start:end])

    def get_mouth_openness(self) -> float:
        """Return mouth openness 0.0-1.0 based on current audio RMS timeline, -1.0 if not playing."""
        if not self.is_speaking.is_set() or not self._mouth_timeline:
            return -1.0
        elapsed = time.monotonic() - self._playback_start
        frame_idx = int(elapsed * 1000 / _MOUTH_WINDOW_MS)
        if 0 <= frame_idx < len(self._mouth_timeline):
            return self._mouth_timeline[frame_idx]
        return -1.0

    def get_mouth_shape(self) -> int:
        """Return mouth shape 0–3 based on current audio RMS timeline, -1 if not playing."""
        openness = self.get_mouth_openness()
        if openness < 0.0:
            return -1
        if openness < 0.1:
            return 0
        if openness < 0.4:
            return 1
        if openness < 0.7:
            return 2
        return 3

    def submit(self, text: str) -> None:
        t = (text or "").strip()
        if not t or config.DRY_RUN:
            return
        self._submit_q.put(t)

    def flush(self) -> None:
        """Block until all queued sentences have been played."""
        self._done.clear()
        self._submit_q.put(_SENTINEL)
        self._done.wait(timeout=120)

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._aplay_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)  # 音声出力が完全に止まるまで待つ
            except (OSError, subprocess.TimeoutExpired):
                pass
        for q in (self._submit_q, self._play_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self._submit_q.put(_SENTINEL)
        self._full_text = ""
        self._mouth_timeline = []
        self.is_speaking.clear()

    # ── Fetcher thread: download WAVs ahead of playback ──────────

    def _fetch_loop(self) -> None:
        while True:
            try:
                item = self._submit_q.get()
            except Exception:
                break
            if item is _SENTINEL:
                self._play_q.put(_SENTINEL)
                continue
            if self._cancel.is_set():
                self._play_q.put(_SENTINEL)
                continue
            original_text = str(item).strip()
            if not original_text:
                continue
            filtered_text = apply_tts_filter(original_text)
            if not filtered_text:
                continue
            wav_data = self._fetch_wav(filtered_text)
            if self._cancel.is_set():
                self._play_q.put(_SENTINEL)
                continue
            if wav_data:
                # 表示は変換前テキスト、音声は変換後テキストを使用
                self._play_q.put((original_text, wav_data))
            else:
                print(f"[tts] skipping sentence (fetch failed): {original_text[:40]}")

    def _fetch_wav(self, text: str) -> bytes | None:
        url = "https://api.openai.com/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": config.OPENAI_TTS_MODEL,
            "voice": config.OPENAI_TTS_VOICE,
            "input": text,
            "response_format": "wav",
            "speed": max(0.25, min(4.0, config.OPENAI_TTS_SPEED)),
        }
        if hasattr(config, "OPENAI_TTS_INSTRUCTIONS") and config.OPENAI_TTS_INSTRUCTIONS:
            payload["instructions"] = config.OPENAI_TTS_INSTRUCTIONS
        try:
            resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=30)
        except Exception as e:
            print(f"[tts] request failed: {e}")
            return None
        if resp.status_code != 200:
            print(f"[tts] API error {resp.status_code}: {resp.text[:200]}")
            return None

        wav_data = b"".join(resp.iter_content(chunk_size=4096))

        gain_db = config.OPENAI_TTS_GAIN_DB
        if gain_db > 0:
            try:
                r = subprocess.run(
                    ["sox", "-t", "wav", "-", "-t", "wav", "-", "gain", str(gain_db)],
                    input=wav_data, capture_output=True, timeout=30, check=False,
                )
                if r.returncode == 0 and r.stdout:
                    wav_data = r.stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        return wav_data

    # ── Player thread: play pre-fetched WAVs back to back ────────

    def _play_loop(self) -> None:
        while True:
            try:
                item = self._play_q.get()
            except Exception:
                break
            if item is _SENTINEL:
                self._cancel.clear()
                self._done.set()
                self._full_text = ""
                self._mouth_timeline = []
                self.is_speaking.clear()
                continue
            if self._cancel.is_set():
                self._cancel.clear()
                self._done.set()
                self._full_text = ""
                self._mouth_timeline = []
                self.is_speaking.clear()
                continue
            text, wav_data = item
            self._full_text = text
            self._play_wav(wav_data)
        self._full_text = ""
        self.is_speaking.clear()

    def _play_wav(self, wav_data: bytes) -> None:
        if not self._volume_set:
            for card in ("0", "1"):
                for control in ("Master", "PCM", "Headphone", "Speaker"):
                    subprocess.run(
                        ["amixer", "-q", "-c", card, "set", control, "100%"],
                        capture_output=True, check=False,
                    )
            self._volume_set = True

        self._mouth_timeline = _analyze_mouth(wav_data)
        self._playback_duration = len(self._mouth_timeline) * _MOUTH_WINDOW_MS / 1000.0
        self._playback_start = time.monotonic()
        self.is_speaking.set()

        try:
            proc = subprocess.Popen(
                ["aplay", "-q", "-D", config.AUDIO_OUTPUT_DEVICE, "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._aplay_proc = proc
            proc.stdin.write(wav_data)
            proc.stdin.close()
            proc.wait(timeout=60)
        except FileNotFoundError:
            print("[tts] aplay not found — install alsa-utils")
        except Exception as e:
            print(f"[tts] playback error: {e}")
        finally:
            self.is_speaking.clear()
            self._mouth_timeline = []


def _analyze_mouth(wav_data: bytes) -> list[float]:
    """Parse WAV and compute mouth shape (0.0-1.0) per 80ms window."""
    header_size = 44
    if len(wav_data) <= header_size:
        return []
    try:
        channels = struct.unpack_from("<H", wav_data, 22)[0]
        sample_rate = struct.unpack_from("<I", wav_data, 24)[0]
        bits_per_sample = struct.unpack_from("<H", wav_data, 34)[0]
    except struct.error:
        return []
    if bits_per_sample != 16:
        return []

    raw = wav_data[header_size:]
    samples_per_window = int(sample_rate * _MOUTH_WINDOW_MS / 1000) * channels
    bytes_per_window = samples_per_window * 2

    def _rms_to_openness(rms: float) -> float:
        if rms < 300:
            return 0.0
        return min(1.0, (rms - 300) / 3700.0)

    shapes: list[float] = []

    if _HAS_NUMPY:
        arr = np.frombuffer(raw[:len(raw) - len(raw) % 2], dtype=np.int16)
        for offset in range(0, len(arr) - samples_per_window + 1, samples_per_window):
            chunk = arr[offset:offset + samples_per_window].astype(np.float64)
            rms = float(np.sqrt(np.mean(chunk * chunk)))
            shapes.append(_rms_to_openness(rms))
    else:
        for offset in range(0, len(raw) - bytes_per_window + 1, bytes_per_window):
            chunk = raw[offset:offset + bytes_per_window]
            n = len(chunk) // 2
            if n == 0:
                shapes.append(0.0)
                continue
            total = 0.0
            for j in range(n):
                sample = struct.unpack_from("<h", chunk, j * 2)[0]
                total += sample * sample
            rms = math.sqrt(total / n)
            shapes.append(_rms_to_openness(rms))

    return shapes
