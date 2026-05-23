"""LLM バックエンド選択。現在は OpenClaw のみ対応。"""
from services.llm.openclaw import stream_response

__all__ = ["stream_response"]
