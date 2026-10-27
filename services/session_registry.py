"""Per-session connection state for the QR pairing screen.

/frame calls mark_frame() for every valid frame it receives (before OCR), and
GET /session/{id}/status reads status(). State lives in process memory only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

CONNECTED_WITHIN_SEC = 6.0      # 3x the camera page's 2 s send interval
SESSION_TTL_SEC = 60 * 60       # Forget sessions with no frame for this long


@dataclass
class _Session:
    last_frame_mono: float
    last_frame_at: datetime
    frames_received: int


class SessionRegistry:
    def __init__(
        self,
        connected_within_sec: float = CONNECTED_WITHIN_SEC,
        ttl_sec: float = SESSION_TTL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.connected_within_sec = connected_within_sec
        self.ttl_sec = ttl_sec
        self._clock = clock
        self._sessions: Dict[str, _Session] = {}

    def mark_frame(self, session_id: str) -> None:
        now = self._clock()
        self._prune(now)
        session = self._sessions.get(session_id)
        if session is None:
            session = _Session(now, datetime.now(timezone.utc), 0)
            self._sessions[session_id] = session
        session.last_frame_mono = now
        session.last_frame_at = datetime.now(timezone.utc)
        session.frames_received += 1

    def status(self, session_id: str) -> Dict[str, Any]:
        session: Optional[_Session] = self._sessions.get(session_id)
        if session is None:
            return {
                "session_id": session_id,
                "state": "waiting",
                "last_frame_at": None,
                "seconds_since_last": None,
                "frames_received": 0,
            }
        age = self._clock() - session.last_frame_mono
        return {
            "session_id": session_id,
            "state": "connected" if age <= self.connected_within_sec else "disconnected",
            "last_frame_at": session.last_frame_at.isoformat(),
            "seconds_since_last": round(age, 1),
            "frames_received": session.frames_received,
        }

    def _prune(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if now - s.last_frame_mono > self.ttl_sec]
        for sid in expired:
            del self._sessions[sid]


_registry = SessionRegistry()


def get_session_registry() -> SessionRegistry:
    return _registry
