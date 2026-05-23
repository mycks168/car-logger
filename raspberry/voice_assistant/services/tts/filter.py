"""TTS フィルター: VOICEVOX/OpenAI TTS に渡す前にテキストを正規化する。"""

import requests

import config


def apply_tts_filter(text: str) -> str:
    """tts-filter API でテキストを正規化して返す。

    TTS_FILTER_URL または TTS_FILTER_BEARER_TOKEN が未設定の場合、
    またはAPIが失敗した場合は元のテキストをそのまま返す。
    """
    url = (config.TTS_FILTER_URL or "").rstrip("/")
    token = config.TTS_FILTER_BEARER_TOKEN
    if not url or not token:
        return text

    try:
        resp = requests.post(
            f"{url}/normalize",
            json={
                "text": text,
                "code_block_mode": config.TTS_FILTER_CODE_BLOCK_MODE,
                "ollama_model": config.TTS_FILTER_OLLAMA_MODEL,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except Exception as e:
        print(f"[tts_filter] API 呼び出し失敗（元テキストで続行）: {e}")
        return text

    if resp.status_code != 200:
        print(f"[tts_filter] API エラー {resp.status_code}（元テキストで続行）: {resp.text[:200]}")
        return text

    return resp.json().get("normalized", text)
