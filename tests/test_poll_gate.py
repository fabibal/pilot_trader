#!/usr/bin/env python3
"""Unit tests for monitor.slow_fetch_skip — the interval-based poll gate.

Replaces the old exact-hour gate: a slow account fetches if its last fetch was
more than POLL_MIN_INTERVAL_H hours ago, so one missed cron run can't strand it.

Run:  .venv/bin/python -m pytest tests/test_poll_gate.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402

NOW = datetime(2026, 6, 2, 18, 0, 0, tzinfo=timezone.utc)


def _state(account, hours_ago):
    ts = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {account: {"newest_id": "1", "last_fetch": ts}}


def test_non_slow_account_never_skipped():
    """IncomeSharks has no POLL_MIN_INTERVAL_H entry -> polled on every run."""
    skip, age = monitor.slow_fetch_skip("IncomeSharks", _state("IncomeSharks", 0), NOW)
    assert skip is False and age is None


def test_never_fetched_is_not_skipped():
    """No last_fetch (e.g. just added) -> fetch now and seed it."""
    skip, age = monitor.slow_fetch_skip("grkportfolio", {}, NOW)
    assert skip is False and age is None


def test_recent_fetch_is_skipped():
    skip, age = monitor.slow_fetch_skip("grkportfolio", _state("grkportfolio", 4), NOW)
    assert skip is True
    assert round(age, 1) == 4.0


def test_old_fetch_is_due():
    """>11h ago -> fetch, even though it is not an exact 00/12 hour."""
    skip, age = monitor.slow_fetch_skip("grkportfolio", _state("grkportfolio", 12), NOW)
    assert skip is False
    assert round(age, 1) == 12.0


def test_boundary_just_under_interval_skips():
    skip, _ = monitor.slow_fetch_skip("grkportfolio", _state("grkportfolio", 10.99), NOW)
    assert skip is True


def test_boundary_just_over_interval_fetches():
    skip, _ = monitor.slow_fetch_skip("grkportfolio", _state("grkportfolio", 11.01), NOW)
    assert skip is False


def test_corrupt_timestamp_fails_open():
    """An unparseable last_fetch must not strand the account."""
    skip, age = monitor.slow_fetch_skip(
        "grkportfolio", {"grkportfolio": {"last_fetch": "not-a-date"}}, NOW)
    assert skip is False and age is None
