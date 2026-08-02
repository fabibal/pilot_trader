#!/usr/bin/env python3
"""Unit tests for monitor._first_sentence — the stand-in both digests write into
a CURRENT VIEW whose one-line headline (shift_note) came back empty.

The Consensus panel renders that field alone, so the fallback has to be real
prose from the stance itself, never a placeholder and never a blank cell.

Run:  .venv/bin/python -m pytest tests/test_first_sentence.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402


def test_takes_only_the_first_sentence():
    """stance_summary is 2-4 sentences; the headline cell wants one."""
    s = ("A Bitcoin szűk sávban konszolidálódik. Az elemző a 70 ezer dolláros "
         "szintet figyeli. Addig kivár.")
    assert monitor._first_sentence(s) == "A Bitcoin szűk sávban konszolidálódik."


def test_collapses_whitespace_and_newlines():
    """The LLM's own line breaks must not survive into a single-line cell."""
    assert monitor._first_sentence("Első  mondat\nfolytatás. Második.") == \
        "Első mondat folytatás."


def test_unterminated_text_returns_whole_string():
    """No sentence-ending punctuation -> keep everything rather than nothing."""
    assert monitor._first_sentence("Semleges kivárás 70k alatt") == \
        "Semleges kivárás 70k alatt"


def test_question_and_exclamation_end_a_sentence():
    assert monitor._first_sentence("Kitörés jön? Talán.") == "Kitörés jön?"
    assert monitor._first_sentence("Vigyázat! Korrekció.") == "Vigyázat!"


def test_empty_and_none_are_safe():
    """A synthesis with no stance_summary either is broken anyway -- the caller
    just gets an empty note rather than a crash."""
    assert monitor._first_sentence("") == ""
    assert monitor._first_sentence(None) == ""
    assert monitor._first_sentence("   \n ") == ""
