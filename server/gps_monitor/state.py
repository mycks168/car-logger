"""
監視状態の永続化モジュール。JSONファイルで状態を保持する。
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"


@dataclass
class MonitorState:
    # 最後に取得できたGPS座標
    last_known_lat: float | None = None
    last_known_lon: float | None = None
    last_known_at: str | None = None  # ISO 8601

    # 最後にSlack通知した時刻と座標
    last_notified_at: str | None = None
    last_notified_lat: float | None = None
    last_notified_lon: float | None = None

    # 現在アラート中かどうか（復帰を検知するため）
    is_alerting: bool = False

    # 最後にGoogle Geolocation APIを呼び出した時刻
    last_geolocation_at: str | None = None


def load_state() -> MonitorState:
    if not STATE_FILE.exists():
        return MonitorState()
    try:
        data = json.loads(STATE_FILE.read_text())
        return MonitorState(**{k: v for k, v in data.items() if k in MonitorState.__dataclass_fields__})
    except Exception as e:
        logger.warning("状態ファイルの読み込みに失敗しました: %s", e)
        return MonitorState()


def save_state(state: MonitorState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)
