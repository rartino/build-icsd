"""Shared progress and completion-time prognosis for the two COD build passes."""

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def _duration_text(seconds: float) -> str:
    """Render a positive duration compactly, rounding up to avoid optimistic ETAs."""
    remaining = max(0, math.ceil(seconds))
    days, remaining = divmod(remaining, 24 * 60 * 60)
    hours, remaining = divmod(remaining, 60 * 60)
    minutes, seconds = divmod(remaining, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


@dataclass(frozen=True)
class ProgressSnapshot:
    """One measured progress state and its projected completion."""

    elapsed: float
    rate: float
    remaining_seconds: float | None
    completion_time: datetime | None

    @property
    def prognosis(self) -> str:
        if self.completion_time is None or self.remaining_seconds is None:
            return "ETA estimating"
        completion = self.completion_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        return f"ETA {completion} ({_duration_text(self.remaining_seconds)} remaining)"


@dataclass(frozen=True)
class CompletionPrognosis:
    """Project completion from all work timings observed in the current invocation.

    ``completed_at_start`` keeps resume estimates honest: rows completed by an earlier
    invocation contribute to the remaining-work count, but not to this run's measured rate.
    Each snapshot therefore updates the estimate from the cumulative timing observed so far.
    """

    total: int
    completed_at_start: int = 0
    monotonic_started: float = field(default_factory=time.monotonic)

    def snapshot(
        self,
        completed: int,
        *,
        monotonic_now: float | None = None,
        wall_now: datetime | None = None,
    ) -> ProgressSnapshot:
        if not self.completed_at_start <= completed <= self.total:
            raise ValueError("completed must be between completed_at_start and total")
        if monotonic_now is None:
            monotonic_now = time.monotonic()
        if wall_now is None:
            wall_now = datetime.now().astimezone()
        elapsed = max(monotonic_now - self.monotonic_started, 1e-9)
        completed_this_run = completed - self.completed_at_start
        rate = completed_this_run / elapsed
        if rate == 0:
            return ProgressSnapshot(elapsed, rate, None, None)
        remaining_seconds = (self.total - completed) / rate
        return ProgressSnapshot(
            elapsed,
            rate,
            remaining_seconds,
            wall_now + timedelta(seconds=remaining_seconds),
        )
