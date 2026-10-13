"""Timing engine: turn a dated signal into a best-action window and a deadline (UTC epochs)."""
from __future__ import annotations

from dataclasses import dataclass

DAY = 86400.0


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    deadline: float | None
    decide_by: float | None = None

    def factor(self, now: float) -> float:
        """Timing factor for value: 1.0 inside the window, decays before/after, 0 past the deadline."""
        if self.deadline is not None and now > self.deadline:
            return 0.0
        if self.start <= now <= self.end:
            return 1.0
        if now < self.start:
            lead = (self.start - now) / DAY
            return max(0.4, 1.0 - 0.02 * lead)
        late = (now - self.end) / DAY
        return max(0.1, 1.0 - 0.1 * late)


def role_change(at: float) -> Window:
    return Window(at + 30 * DAY, at + 90 * DAY, at + 120 * DAY)


def cfp(deadline: float, now: float) -> Window:
    return Window(now, max(now, deadline - 3 * DAY), deadline)


def event_trip(start: float, now: float, early_bird: float | None = None) -> Window:
    decide_by = early_bird if early_bird else start - 45 * DAY
    deadline = decide_by if decide_by > now else max(now, start - 3 * DAY)
    return Window(start - 21 * DAY, start - 3 * DAY, deadline, decide_by)


def hiring_post(posted: float) -> Window:
    return Window(posted + 2 * DAY, posted + 21 * DAY, posted + 30 * DAY)


def social_request(posted: float, now: float) -> Window:
    return Window(now, posted + 5 * DAY, posted + 7 * DAY)


def deprecation(now: float, retirement: float) -> Window:
    return Window(now, retirement - 14 * DAY, retirement)


def price_change(now: float) -> Window:
    return Window(now, now + 14 * DAY, None)


def outage(now: float) -> Window:
    return Window(now, now + DAY, None)
