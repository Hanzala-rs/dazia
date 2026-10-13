from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.timeparse import resolve_time

TZ = "Asia/Karachi"
ZONE = ZoneInfo(TZ)
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=ZONE)  # Wednesday 3 PM


def resolved(phrase, now=NOW):
    result = resolve_time(phrase, now, TZ)
    assert result.due is not None, f"{phrase!r} asked: {result.clarification}"
    assert result.clarification is None
    assert result.due.tzinfo is not None
    assert result.due.utcoffset() == timedelta(hours=5)
    return result.due.replace(tzinfo=None)


def clarification(phrase, now=NOW):
    result = resolve_time(phrase, now, TZ)
    assert result.due is None, f"{phrase!r} unexpectedly resolved to {result.due}"
    assert result.clarification
    return result.clarification


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("tomorrow at 9 AM", datetime(2026, 10, 15, 9, 0)),
        ("tomorrow at 9 a.m.", datetime(2026, 10, 15, 9, 0)),
        ("tomorrow at 9:15 pm", datetime(2026, 10, 15, 21, 15)),
        ("9pm tomorrow", datetime(2026, 10, 15, 21, 0)),
        ("today at 6pm", datetime(2026, 10, 14, 18, 0)),
        ("friday at 17:30", datetime(2026, 10, 16, 17, 30)),
        ("tomorrow at 09:00", datetime(2026, 10, 15, 9, 0)),
        ("next monday at 10am", datetime(2026, 10, 19, 10, 0)),
        ("monday 10am", datetime(2026, 10, 19, 10, 0)),
        ("tomorrow at noon", datetime(2026, 10, 15, 12, 0)),
        ("midnight", datetime(2026, 10, 15, 0, 0)),
        ("tomorrow at 12 am", datetime(2026, 10, 15, 0, 0)),
        ("in 2 hours", datetime(2026, 10, 14, 17, 0)),
        ("in 30 minutes", datetime(2026, 10, 14, 15, 30)),
        ("oct 20 at 5pm", datetime(2026, 10, 20, 17, 0)),
    ],
)
def test_resolves(phrase, expected):
    assert resolved(phrase) == expected


def test_follow_up_answer_appended_to_phrase():
    # The assistant appends the user's answer to the original phrase.
    assert resolved("tomorrow at 9 PM") == datetime(2026, 10, 15, 21, 0)
    assert resolved("tomorrow at 9 9 pm") == datetime(2026, 10, 15, 21, 0)


def test_ambiguous_hour_asks_am_pm():
    assert clarification("tomorrow at 9") == "Did you mean 9 AM or 9 PM?"
    assert clarification("tomorrow at 9 o'clock") == "Did you mean 9 AM or 9 PM?"
    assert "AM or" in clarification("tomorrow at 9:30")


@pytest.mark.parametrize("phrase", ["tomorrow", "friday", "tomorrow morning", "tonight", "in 2 days"])
def test_missing_time_of_day_asks(phrase):
    assert "What time" in clarification(phrase)


def test_past_time_asks():
    assert "already passed" in clarification("today at 1 pm")


@pytest.mark.parametrize("phrase", ["asdkjh qwe at 5pm", "at 25:00", "at 13pm", "  "])
def test_unparseable_asks(phrase):
    clarification(phrase)


def test_result_is_converted_to_target_zone():
    utc_now = NOW.astimezone(ZoneInfo("UTC"))
    assert resolved("tomorrow at 9 AM", now=utc_now) == datetime(2026, 10, 15, 9, 0)


def test_rejects_naive_now_and_bad_zone():
    with pytest.raises(ValueError):
        resolve_time("tomorrow at 9 AM", datetime(2026, 10, 14, 15, 0), TZ)
    with pytest.raises(ValueError):
        resolve_time("tomorrow at 9 AM", NOW, "Not/AZone")
