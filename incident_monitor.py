from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass


INSTABILITY_PHRASES = (
    "lag", "giat", "ping cao", "ms cao", "mat ket noi", "disconnect", "timeout",
    "khong vao duoc", "khong ket noi duoc", "server bi sap", "server sap roi", "server die",
    "server offline", "crash", "rollback", "mat do", "loi server", "server loi",
    "treo server",
)
CONFLICT_PHRASES = (
    "lua dao", "scam", "dung hack", "xai hack", "bi hack", "to cao", "chui", "xuc pham", "cai nhau",
    "cai lon", "danh nhau", "au da", "toxic", "gay war", "de doa", "an cap",
    "pha hoai", "gia danh",
)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold()).replace("đ", "d")
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class IncidentSignal:
    category: str
    channel_id: int
    unique_reporters: int


class IncidentMonitor:
    """Detect aggregate incident signals without retaining message content."""

    def __init__(
        self,
        window_seconds: int = 180,
        cooldown_seconds: int = 900,
        max_alerts_per_hour: int = 3,
    ) -> None:
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_alerts_per_hour = max_alerts_per_hour
        self.windows: dict[tuple[int, int, str], deque[tuple[float, int]]] = defaultdict(deque)
        self.cooldowns: dict[tuple[int, int, str], float] = {}
        self.alerts: dict[int, deque[float]] = defaultdict(deque)

    @staticmethod
    def classify(text: str) -> set[str]:
        normalized = normalize_text(text)
        contains = lambda phrase: re.search(
            rf"(?<!\w){re.escape(phrase)}(?!\w)", normalized
        ) is not None
        if any(contains(phrase) for phrase in CONFLICT_PHRASES):
            return {"conflict"}
        if any(contains(phrase) for phrase in INSTABILITY_PHRASES):
            return {"instability"}
        return set()

    def _cleanup(self, now: float) -> None:
        for key, window in list(self.windows.items()):
            while window and window[0][0] <= now - self.window_seconds:
                window.popleft()
            if not window:
                self.windows.pop(key, None)
        for key, expires_at in list(self.cooldowns.items()):
            if expires_at <= now:
                self.cooldowns.pop(key, None)
        for guild_id, events in list(self.alerts.items()):
            while events and events[0] <= now - 3600:
                events.popleft()
            if not events:
                self.alerts.pop(guild_id, None)

    def observe(
        self,
        channel_id: int,
        user_id: int,
        text: str,
        *,
        guild_id: int = 0,
    ) -> list[IncidentSignal]:
        if not text:
            return []
        text = text[:4000]
        now = time.monotonic()
        self._cleanup(now)
        signals: list[IncidentSignal] = []
        for category in self.classify(text):
            key = (guild_id, channel_id, category)
            if self.cooldowns.get(key, 0) > now:
                continue
            window = self.windows[key]
            window.append((now, user_id))
            unique_users = {entry[1] for entry in window}
            if len(window) < 3 or len(unique_users) < 2:
                continue

            guild_alerts = self.alerts[guild_id]
            if len(guild_alerts) >= self.max_alerts_per_hour:
                self.cooldowns[key] = now + self.cooldown_seconds
                window.clear()
                continue

            self.cooldowns[key] = now + self.cooldown_seconds
            guild_alerts.append(now)
            signals.append(IncidentSignal(category, channel_id, len(unique_users)))
            window.clear()
        return signals
