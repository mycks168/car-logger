"""TTS エンジン選択。config.TTS_ENGINE に応じた TTSPlayer クラスを提供する。"""
import config

if config.TTS_ENGINE == "voicevox":
    from services.tts.voicevox import TTSPlayer
else:
    from services.tts.openai import TTSPlayer

__all__ = ["TTSPlayer"]
