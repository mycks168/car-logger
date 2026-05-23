import os
from dotenv import load_dotenv

load_dotenv()


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_TRANSCRIBE_MODEL = os.environ.get(
    "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"
)
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts-2025-12-15")
OPENAI_TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "coral")
OPENAI_TTS_SPEED = float(os.environ.get("OPENAI_TTS_SPEED", "1.1"))
OPENAI_TTS_GAIN_DB = float(os.environ.get("OPENAI_TTS_GAIN_DB", "9"))
OPENAI_TTS_INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Speak in a warm, sweet, and playful tone with a gentle high pitch. "
    "Sound like an adorable, tiny friend who is genuinely excited to help. "
    "Use natural breathing and smooth pacing — never robotic or monotone. "
    "Let sentences flow into each other without awkward pauses.",
)

WEBHOOK_ENABLED = os.environ.get("WEBHOOK_ENABLED", "false").lower() in ("true", "1", "yes")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# GPIO ピン番号（BCM）
PI3_PTT_GPIO = int(os.environ.get("PI3_PTT_GPIO", "17"))
PI3_AUDIO_SOURCE = os.environ.get("PI3_AUDIO_SOURCE", "")
PI3_DISPLAY_WIDTH = int(os.environ.get("PI3_DISPLAY_WIDTH", "1280"))
PI3_DISPLAY_HEIGHT = int(os.environ.get("PI3_DISPLAY_HEIGHT", "720"))
PI3_DISPLAY_FULLSCREEN = os.environ.get("PI3_DISPLAY_FULLSCREEN", "false").lower() in ("true", "1", "yes")
PI3_CHAR_SCALE = float(os.environ.get("PI3_CHAR_SCALE", "1.0"))

STT_ENGINE = os.environ.get("STT_ENGINE", "gateway")   # "openai" or "gateway"
TTS_ENGINE = os.environ.get("TTS_ENGINE", "voicevox")  # "openai" or "voicevox"

STT_GATEWAY_URL = os.environ.get("STT_GATEWAY_URL", "http://localhost:23000")
STT_GATEWAY_LANGUAGE = os.environ.get("STT_GATEWAY_LANGUAGE", "ja")

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://localhost:50021")
VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "2"))
VOICEVOX_SAMPLE_RATE = int(os.environ.get("VOICEVOX_SAMPLE_RATE", "48000"))

TTS_FILTER_URL = os.environ.get("TTS_FILTER_URL", "")
TTS_FILTER_BEARER_TOKEN = os.environ.get("TTS_FILTER_BEARER_TOKEN", "")
TTS_FILTER_CODE_BLOCK_MODE = os.environ.get("TTS_FILTER_CODE_BLOCK_MODE", "ollama-summary")
TTS_FILTER_OLLAMA_MODEL = os.environ.get("TTS_FILTER_OLLAMA_MODEL", "qwen2.5:0.5b")

OPENCLAW_BASE_URL = os.environ.get("OPENCLAW_BASE_URL", "http://localhost:18789")
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_TOKEN", "")
OPENCLAW_AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "")
OPENCLAW_SESSION_KEY = os.environ.get("OPENCLAW_SESSION_KEY", "")


def _load_sessions() -> list[dict]:
    sessions = []
    i = 1
    while True:
        raw = os.environ.get(f"OPENCLAW_SESSION_{i}", "")
        if not raw:
            break
        parts = raw.split(",", 4)
        if len(parts) >= 3:
            session: dict = {"name": parts[0], "agent_id": parts[1], "session_key": parts[2]}
            if len(parts) >= 4 and parts[3]:
                session["base_url"] = parts[3]
            if len(parts) >= 5 and parts[4]:
                session["token"] = parts[4]
            sessions.append(session)
        i += 1
    if not sessions and (OPENCLAW_AGENT_ID or OPENCLAW_SESSION_KEY):
        sessions = [{"name": "デフォルト", "agent_id": OPENCLAW_AGENT_ID, "session_key": OPENCLAW_SESSION_KEY}]
    return sessions


OPENCLAW_SESSIONS: list[dict] = _load_sessions()

AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "plughw:CARD=Microphone,DEV=0")
AUDIO_OUTPUT_DEVICE = os.environ.get("AUDIO_OUTPUT_DEVICE", "plughw:CARD=vc4hdmi,DEV=0")
# amixer 用カード名（aplay -l の card N: <name> の短縮名）
AUDIO_OUTPUT_CARD = os.environ.get("AUDIO_OUTPUT_CARD", "vc4hdmi")
AUDIO_OUTPUT_VOLUME = int(os.environ.get("AUDIO_OUTPUT_VOLUME", "90"))
AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "16000"))

_dry_run_env = os.environ.get("DRY_RUN", "")
if _dry_run_env:
    DRY_RUN = _dry_run_env.lower() in ("true", "1", "yes")
else:
    DRY_RUN = (STT_ENGINE == "openai" or TTS_ENGINE == "openai") and not OPENAI_API_KEY

ENABLE_TTS = os.environ.get("ENABLE_TTS", "true").lower() in ("true", "1", "yes")
CONVERSATION_HISTORY_LENGTH = int(os.environ.get("CONVERSATION_HISTORY_LENGTH", "5"))
SILENCE_RMS_THRESHOLD = float(os.environ.get("SILENCE_RMS_THRESHOLD", "200"))

UI_IMAGE_ASSETS_DIR = os.environ.get("UI_IMAGE_ASSETS_DIR", "assets/pngtuber_pi3")

# 地図・GPS 設定
GPS_SERVER_URL = os.environ.get("GPS_SERVER_URL", "http://localhost:8080")
MAP_ZOOM = int(os.environ.get("MAP_ZOOM", "15"))
MAP_TILE_CACHE_DIR = os.environ.get("MAP_TILE_CACHE_DIR", "/tmp/maptiles")

# ナビゲーション設定
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "http://localhost:5000")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
NAV_ANNOUNCE_DISTANCE_M = int(os.environ.get("NAV_ANNOUNCE_DISTANCE_M", "300"))
NAV_ARRIVE_DISTANCE_M = int(os.environ.get("NAV_ARRIVE_DISTANCE_M", "50"))
POI_SEARCH_RADIUS_M = int(os.environ.get("POI_SEARCH_RADIUS_M", "2000"))
POI_UPDATE_DISTANCE_M = int(os.environ.get("POI_UPDATE_DISTANCE_M", "500"))


def print_config():
    print(f"STT_ENGINE              = {STT_ENGINE}")
    print(f"TTS_ENGINE              = {TTS_ENGINE}")
    print(f"OPENCLAW_BASE_URL       = {OPENCLAW_BASE_URL}")
    print(f"AUDIO_DEVICE            = {AUDIO_DEVICE}")
    print(f"AUDIO_OUTPUT_DEVICE     = {AUDIO_OUTPUT_DEVICE}")
    print(f"AUDIO_OUTPUT_VOLUME     = {AUDIO_OUTPUT_VOLUME}%")
    print(f"AUDIO_SAMPLE_RATE       = {AUDIO_SAMPLE_RATE}")
    print(f"DRY_RUN                 = {DRY_RUN}")
    print(f"PI3_PTT_GPIO            = {PI3_PTT_GPIO}")
    print(f"PI3_DISPLAY             = {PI3_DISPLAY_WIDTH}x{PI3_DISPLAY_HEIGHT} fullscreen={PI3_DISPLAY_FULLSCREEN}")
    if STT_ENGINE == "gateway":
        print(f"STT_GATEWAY_URL         = {STT_GATEWAY_URL}")
    elif STT_ENGINE == "openai":
        print(f"OPENAI_TRANSCRIBE_MODEL = {OPENAI_TRANSCRIBE_MODEL}")
    if TTS_ENGINE == "voicevox":
        print(f"VOICEVOX_URL            = {VOICEVOX_URL}")
    elif TTS_ENGINE == "openai":
        print(f"OPENAI_TTS_MODEL        = {OPENAI_TTS_MODEL}")
        print(f"OPENAI_TTS_VOICE        = {OPENAI_TTS_VOICE}")
    print(f"OPENAI_API_KEY set      = {bool(OPENAI_API_KEY)}")
    print(f"OPENCLAW_TOKEN set      = {bool(OPENCLAW_TOKEN)}")
    if TTS_FILTER_URL and TTS_FILTER_BEARER_TOKEN:
        print(f"TTS_FILTER_URL          = {TTS_FILTER_URL}")
        print(f"TTS_FILTER_CODE_BLOCK   = {TTS_FILTER_CODE_BLOCK_MODE}")
    print(f"ENABLE_TTS              = {ENABLE_TTS}")
    print(f"CONVERSATION_HISTORY    = {CONVERSATION_HISTORY_LENGTH}")
    print(f"SILENCE_RMS_THRESHOLD   = {SILENCE_RMS_THRESHOLD}")
    print(f"UI_IMAGE_ASSETS_DIR     = {UI_IMAGE_ASSETS_DIR}")
