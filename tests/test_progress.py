from datetime import UTC, datetime

import pytest

from build_icsd.progress import CompletionPrognosis, _duration_text


def test_prognosis_updates_from_all_observed_timing() -> None:
    started = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    prognosis = CompletionPrognosis(total=100, monotonic_started=10.0)

    first = prognosis.snapshot(10, monotonic_now=20.0, wall_now=started)
    assert first.elapsed == 10.0
    assert first.rate == 1.0
    assert first.remaining_seconds == 90.0
    assert first.completion_time == datetime(2026, 8, 23, 12, 1, 30, tzinfo=UTC)
    assert first.prognosis == "ETA 2026-08-23 12:01:30 UTC (1m 30s remaining)"

    updated = prognosis.snapshot(40, monotonic_now=30.0, wall_now=started)
    assert updated.rate == 2.0
    assert updated.remaining_seconds == 30.0
    assert updated.completion_time == datetime(2026, 8, 23, 12, 0, 30, tzinfo=UTC)


def test_resumed_prognosis_times_only_work_from_current_run() -> None:
    prognosis = CompletionPrognosis(total=100, completed_at_start=40, monotonic_started=10.0)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    progress = prognosis.snapshot(50, monotonic_now=20.0, wall_now=now)

    assert progress.rate == 1.0
    assert progress.remaining_seconds == 50.0
    assert progress.completion_time == datetime(2026, 8, 23, 12, 0, 50, tzinfo=UTC)


def test_prognosis_waits_for_first_timing_sample() -> None:
    prognosis = CompletionPrognosis(total=10, completed_at_start=4, monotonic_started=5.0)

    progress = prognosis.snapshot(4, monotonic_now=6.0)

    assert progress.rate == 0.0
    assert progress.remaining_seconds is None
    assert progress.completion_time is None
    assert progress.prognosis == "ETA estimating"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (0.1, "1s"),
        (61, "1m 1s"),
        (3661, "1h 1m 1s"),
        (90061, "1d 1h 1m"),
    ],
)
def test_duration_text(seconds: float, expected: str) -> None:
    assert _duration_text(seconds) == expected


def test_prognosis_rejects_inconsistent_counts() -> None:
    prognosis = CompletionPrognosis(total=10, completed_at_start=4)

    with pytest.raises(ValueError, match="completed must be"):
        prognosis.snapshot(3)
    with pytest.raises(ValueError, match="completed must be"):
        prognosis.snapshot(11)
