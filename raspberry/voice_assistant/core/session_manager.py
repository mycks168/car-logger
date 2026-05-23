"""セッション管理: OpenClaw の agent_id/session_key ペアを複数管理する。"""
import logging

log = logging.getLogger("voice-assistant")


class SessionManager:
    """複数の OpenClaw セッションを管理し、順番に切り替える。"""

    def __init__(self, sessions: list[dict]):
        self._sessions = sessions
        self._index = 0
        if sessions:
            log.info(
                "session manager: %d session(s), current=%r",
                len(sessions),
                sessions[0].get("name", ""),
            )

    @property
    def has_multiple(self) -> bool:
        return len(self._sessions) > 1

    @property
    def current(self) -> dict | None:
        if not self._sessions:
            return None
        return self._sessions[self._index]

    @property
    def current_name(self) -> str:
        s = self.current
        return s.get("name", "") if s else ""

    def next(self) -> dict | None:
        """次のセッションに切り替え、そのセッションを返す。セッションが1つ以下なら None。"""
        if len(self._sessions) <= 1:
            return None
        self._index = (self._index + 1) % len(self._sessions)
        session = self._sessions[self._index]
        log.info("session switched to [%d] %r", self._index, session.get("name", ""))
        return session
