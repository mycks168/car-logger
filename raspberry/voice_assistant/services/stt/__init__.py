"""STT エンジン選択。config.STT_ENGINE に応じた transcribe 関数を提供する。"""
import config

if config.STT_ENGINE == "gateway":
    from services.stt.gateway import transcribe
else:
    from services.stt.openai import transcribe

__all__ = ["transcribe"]
