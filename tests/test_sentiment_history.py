#!/usr/bin/env python3
"""Unit tests for sentiment_history — the append-only CURRENT VIEW log.

Covers the two rules that make the log usable as a timeline: a view over a NEW
window appends a point, and a re-synthesis of the SAME window (what `--force`
produces) replaces the last point instead of inventing a second one.

Run:  .venv/bin/python -m pytest tests/test_sentiment_history.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentiment_history as sh  # noqa: E402


def _view(sentiment="neutral", count=25, from_date="2026-07-23",
          to_date="2026-07-29", shift_note="elmozdult", **extra):
    """A generate_current_view() return value (the fields the log keeps, plus
    the stance_summary prose it deliberately drops)."""
    return {
        "overall_sentiment": sentiment,
        "stance_summary": "hosszu magyar elemzes " * 20,
        "shift_note": shift_note,
        "generated_at": "2026-07-29T09:30:00+00:00",
        "based_on": {"count": count, "from_date": from_date, "to_date": to_date},
        **extra,
    }


def _isolate(tmp_path, monkeypatch):
    """Point the module at a throwaway history file."""
    path = os.path.join(str(tmp_path), "sentiment_history.json")
    monkeypatch.setattr(sh, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sh, "HISTORY_FILE", path)
    return path


def test_record_is_compact(tmp_path, monkeypatch):
    """Only the timeline fields are kept — stance_summary (the bulk of a view)
    is dropped so months of history stay small."""
    path = _isolate(tmp_path, monkeypatch)
    assert sh.append_view("daancrypto", _view()) is True
    with open(path) as f:
        rec, = json.load(f)
    assert set(rec) == {"source", "sentiment", "generated_at", "based_on",
                        "shift_note"}
    assert rec["source"] == "daancrypto"
    assert rec["sentiment"] == "neutral"
    assert rec["based_on"] == {"count": 25, "from_date": "2026-07-23",
                               "to_date": "2026-07-29"}


def test_new_window_appends(tmp_path, monkeypatch):
    """A later run over a moved window is a genuine second data point."""
    _isolate(tmp_path, monkeypatch)
    sh.append_view("daancrypto", _view(sentiment="neutral"))
    sh.append_view("daancrypto", _view(sentiment="bearish", to_date="2026-07-30"))
    history = sh.load_history()
    assert [r["sentiment"] for r in history] == ["neutral", "bearish"]


def test_same_window_replaces(tmp_path, monkeypatch):
    """`--force` re-synthesizes the identical window: that is a corrected
    reading of one point, not a second point."""
    _isolate(tmp_path, monkeypatch)
    sh.append_view("donalt", _view(sentiment="mixed", shift_note="elso"))
    sh.append_view("donalt", _view(sentiment="bullish", shift_note="masodik"))
    history = sh.load_history()
    assert len(history) == 1
    assert history[0]["sentiment"] == "bullish"
    assert history[0]["shift_note"] == "masodik"


def test_replace_only_matches_same_source(tmp_path, monkeypatch):
    """Two feeds summarizing the same date window are independent points."""
    _isolate(tmp_path, monkeypatch)
    sh.append_view("donalt", _view(sentiment="mixed"))
    sh.append_view("dorkchicken", _view(sentiment="bearish"))
    history = sh.load_history()
    assert [(r["source"], r["sentiment"]) for r in history] == [
        ("donalt", "mixed"), ("dorkchicken", "bearish")]


def test_cap_drops_oldest_per_source(tmp_path, monkeypatch):
    """The per-source cap bounds the file; other sources are untouched."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(sh, "MAX_PER_SOURCE", 3)
    sh.append_view("cowen", _view(sentiment="bullish"))
    for day in range(10, 16):
        sh.append_view("joao_wedson", _view(to_date=f"2026-07-{day}",
                                            shift_note=f"day{day}"))
    history = sh.load_history()
    joao = [r for r in history if r["source"] == "joao_wedson"]
    assert [r["shift_note"] for r in joao] == ["day13", "day14", "day15"]
    assert sum(1 for r in history if r["source"] == "cowen") == 1


def test_corrupt_file_does_not_break_the_run(tmp_path, monkeypatch):
    """A truncated log must not take down the digest that produced the view."""
    path = _isolate(tmp_path, monkeypatch)
    with open(path, "w") as f:
        f.write("{not json")
    assert sh.load_history() == []
    assert sh.append_view("cowen", _view()) is True
    assert len(sh.load_history()) == 1
