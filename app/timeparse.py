"""Deterministic resolution of natural-language times.

The LLM only extracts the raw phrase ("tomorrow at 9 AM"); this module turns it into a datetime.

How it works:
1. Look for a time-of-day in the phrase ourselves with a few simple regexes
   (dateparser silently fills in "now's" time when none is given, so we can't trust it for that).
2. Ask the user when the time is missing or AM/PM is ambiguous.
3. Let dateparser work out the date, then double-check it kept the time we found.
4. Reject results that are not in the future.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser

# "9am", "9 PM", "9:30 p.m."
_AMPM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b\.?", re.IGNORECASE)
# "09:00", "21:30" (checked after am/pm times are removed)
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# "at 9" - a bare hour (checked after the two above are removed)
_AT_HOUR = re.compile(r"\bat\s+(\d{1,2})\b")
_NOON = re.compile(r"\b(noon|midday)\b")
_MIDNIGHT = re.compile(r"\bmidnight\b")
# "in 2 hours", "in 30 minutes", "in an hour", "in half an hour"
_RELATIVE = re.compile(r"\bin\s+(\d+|an?|half an)\s*(minutes?|mins?|hours?|hrs?)\b")
# dateparser can't handle "next monday at 10am" / "this friday", but does handle "monday at 10am"
_NEXT_WEEKDAY = re.compile(r"\b(next|this|coming)\s+(?=(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b)")

_ASK_TIME = "What time should I set it for?"
_ASK_AGAIN = "Sorry, I couldn't understand that time. Could you say it like 'tomorrow at 9 AM'?"


@dataclass
class TimeResolution:
    due: datetime | None  # aware datetime in the user's zone, or None if clarification is needed
    clarification: str | None = None  # question to ask the user when due is None


def _normalize(phrase: str) -> str:
    text = " ".join(phrase.lower().split())
    text = re.sub(r"\s*o'?clock\b", "", text)  # "9 o'clock" -> "9"
    # Follow-up answers get appended ("tomorrow at 9" + "9 pm"): "at 9 9 pm" -> "at 9 pm"
    text = re.sub(r"\b(\d{1,2}) \1(?=\s*[ap]\.?\s*m\b)", r"\1", text)
    text = _NEXT_WEEKDAY.sub("", text)
    return text


def _find_time(text: str) -> tuple[str, tuple[int, int] | int | None]:
    """Return what kind of time-of-day the phrase contains:
    ("relative", None)       e.g. "in 2 hours"
    ("exact", (hour, min))   e.g. "9 pm", "21:30", "noon"
    ("ambiguous", hour)      e.g. "at 9"  (AM or PM?)
    ("invalid", None)        e.g. "25:00"
    ("none", None)           no time-of-day at all
    """
    if _RELATIVE.search(text):
        return "relative", None

    match = _AMPM.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not (1 <= hour <= 12 and minute < 60):
            return "invalid", None
        hour = hour % 12 + (12 if match.group(3) == "p" else 0)
        return "exact", (hour, minute)

    match = _CLOCK.search(text)
    if match:
        hour_text, minute = match.group(1), int(match.group(2))
        hour = int(hour_text)
        if hour > 23 or minute > 59:
            return "invalid", None
        # "09:00", "00:30", "21:30" are clearly 24-hour; "9:30" or "12:15" could be AM or PM.
        zero_padded = hour_text.startswith("0")
        if 1 <= hour <= 12 and not zero_padded:
            return "ambiguous", hour
        return "exact", (hour, minute)

    if _NOON.search(text):
        return "exact", (12, 0)
    if _MIDNIGHT.search(text):
        return "exact", (0, 0)

    match = _AT_HOUR.search(text)
    if match:
        hour = int(match.group(1))
        if 1 <= hour <= 12:
            return "ambiguous", hour
        if hour == 0 or 13 <= hour <= 23:
            return "exact", (hour, 0)
        return "invalid", None

    return "none", None


def resolve_time(phrase: str, now: datetime, tz: str) -> TimeResolution:
    """Resolve `phrase` relative to `now` (aware) in IANA zone `tz`.

    Asks for clarification (due=None) when: AM/PM is ambiguous for an hour 1-12 without
    am/pm/24h context, no time of day is given, the phrase can't be parsed, or the result is in the past.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone: {tz!r}") from exc

    text = _normalize(phrase)
    if not text:
        return TimeResolution(None, "When should I remind you?")

    kind, value = _find_time(text)
    if kind == "ambiguous":
        return TimeResolution(None, f"Did you mean {value} AM or {value} PM?")
    if kind == "none":
        return TimeResolution(None, _ASK_TIME)
    if kind == "invalid":
        return TimeResolution(None, _ASK_AGAIN)

    local_now = now.astimezone(zone)
    parsed = dateparser.parse(
        text,
        languages=["en"],  # English only: faster and avoids odd matches in other languages
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": tz,
            "RETURN_AS_TIMEZONE_AWARE": True,
            # dateparser expects a naive base in the TIMEZONE above
            "RELATIVE_BASE": local_now.replace(tzinfo=None),
        },
    )
    if parsed is None:
        return TimeResolution(None, _ASK_AGAIN)
    due = parsed.astimezone(zone)

    # dateparser sometimes ignores part of a phrase and keeps "now's" time; make sure it used ours.
    if kind == "exact" and (due.hour, due.minute) != value:
        return TimeResolution(None, _ASK_AGAIN)

    if due <= now:
        return TimeResolution(None, "That time has already passed. When should I remind you?")
    return TimeResolution(due)
