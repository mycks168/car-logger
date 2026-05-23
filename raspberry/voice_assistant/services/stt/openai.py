import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

_http_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[502, 503, 504],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        _http_session.mount("http://", adapter)
        _http_session.mount("https://", adapter)
    return _http_session


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file using OpenAI Audio Transcriptions API.

    In dry-run mode (no OPENAI_API_KEY), prompts for typed input instead.
    """
    if config.DRY_RUN:
        print("[transcribe] DRY RUN — type your message:")
        try:
            return input("> ").strip()
        except EOFError:
            return ""

    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"WAV file not found: {wav_path}")

    file_size = os.path.getsize(wav_path)
    if file_size < 100:
        raise ValueError(f"WAV file too small ({file_size} bytes), likely empty recording")

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}

    with open(wav_path, "rb") as f:
        try:
            resp = _get_session().post(
                url,
                headers=headers,
                files={"file": ("utterance.wav", f, "audio/wav")},
                data={
                    "model": config.OPENAI_TRANSCRIBE_MODEL,
                    "response_format": "text",
                },
                timeout=30,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise RuntimeError(f"Transcription request failed: {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(
            f"Transcription failed ({resp.status_code}): {resp.text[:300]}"
        )

    transcript = resp.text.strip()
    print(f"[transcribe] result: {transcript[:120]}")
    return transcript
