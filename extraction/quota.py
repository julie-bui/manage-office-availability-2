"""Shared handling for Gemini API error classification, used by both
Gemini-calling modules in this app — extraction.llm_fallback (plain
listing extraction) and extraction.address_lookup (Google Search-
grounded address lookup). Each has its own separate model and its own
separate daily quota (see each module's own docstring for why), but a
given failure shape (a 429 daily-quota error, a 503 "high demand"
overload) looks the same from the API's own error shape either way, and
should be handled the same way regardless of which module hit it.
"""
import time
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — stdlib since Python 3.9
    ZoneInfo = None

PACIFIC = "America/Los_Angeles"
# This app's own audience is UK-based (team@spacepoint.co.uk) — the reset
# time is converted to UK local time rather than assuming whoever reads
# the message already knows what "midnight Pacific" is in their own
# clock, or leaving it in Pacific time only.
LOCAL_DISPLAY_TZ = "Europe/London"


def is_quota_exceeded(exc):
    """True if `exc` (any exception raised by a google-genai call) looks
    like a 429/RESOURCE_EXHAUSTED daily-quota error specifically — never
    a network hiccup, an auth failure, or anything else that should be
    reported/retried differently."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    return code in (429, "429") or "RESOURCE_EXHAUSTED" in text


# Seconds to wait before each automatic retry of a 503 "high demand"
# error — a real, distinct failure mode from the 429 daily-quota case
# above: transient server-side overload that's typically gone within a
# few seconds, not a hard daily ceiling that fails identically on every
# immediate retry. Two retries (three attempts total), with the wait
# increasing rather than constant, gives a brief transient blip a good
# chance to clear on the first retry while still giving a slightly
# longer-lived one a second chance, without piling up enough total delay
# on its own to meaningfully eat into extraction.pipeline's own
# BATCH_DEADLINE_SECONDS budget.
OVERLOAD_RETRY_WAITS_SECONDS = (5, 15)


def is_overloaded_error(exc):
    """True if `exc` looks like Gemini's own transient 503
    UNAVAILABLE/ServerError ("the model is overloaded, please try again
    later") — worth an automatic short-wait retry, unlike a 429 daily-
    quota error (is_quota_exceeded above), which fails identically on
    every immediate retry, or any other real failure that a blind retry
    wouldn't fix either."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    return code in (503, "503") or "UNAVAILABLE" in text or "overloaded" in text.lower()


def call_with_overload_retry(fn, log=print, label=""):
    """Calls fn() (a zero-arg callable — typically a call already wrapped
    in extraction.hard_timeout.call_with_timeout for its own independent
    wall-clock deadline) and automatically retries, waiting
    OVERLOAD_RETRY_WAITS_SECONDS between attempts, ONLY when the failure
    is a transient 503 overload (is_overloaded_error above) — confirmed
    this specific error is usually a short-lived blip on Google's side,
    not a problem with the request itself. Any other exception (a 429
    daily-quota error, a timeout, anything else) is raised immediately
    on the first attempt, exactly as if this wrapper weren't here at
    all — retrying those would either fail identically (429) or isn't
    this function's concern to guess at. Re-raises the final 503 if it's
    still failing after every retry.

    label (e.g. a building name or filename) is included in the retry
    log line so a multi-file/multi-building batch's console output shows
    which specific call is being retried, not just that some retry
    happened somewhere."""
    for attempt, wait in enumerate(OVERLOAD_RETRY_WAITS_SECONDS, start=1):
        try:
            return fn()
        except Exception as e:
            if not is_overloaded_error(e):
                raise
            prefix = f"{label}: " if label else ""
            log(
                f"[quota] {prefix}Gemini reported high demand (503) — retrying in {wait}s "
                f"({attempt}/{len(OVERLOAD_RETRY_WAITS_SECONDS)})"
            )
            time.sleep(wait)
    return fn()


def reset_message(what_hit_it):
    """A user-facing sentence: `what_hit_it` (e.g. "Gemini's daily
    AI-extraction limit", "Gemini's daily address-search limit") plus
    this app's own best-effort note on when it resets. Google resets
    Gemini's free-tier daily quotas at midnight Pacific Time; this
    computes the next one from the real current time (not a hardcoded
    offset — Pacific and UK clocks each observe their own DST on
    different dates, so the gap between them isn't always exactly the
    same number of hours) and converts it to a UK clock-time plus an
    approximate hours-remaining figure. Falls back to a
    timezone-agnostic version of the message if the deployment's Python
    has no IANA tzdata available (see the `tzdata` package in
    requirements.txt for why this shouldn't normally happen on Render) —
    never allowed to itself raise and break the error path it's called
    from."""
    reset_clause = "This resets at midnight Pacific Time"
    if ZoneInfo is not None:
        try:
            pacific_now = datetime.now(ZoneInfo(PACIFIC))
            next_midnight_pacific = (pacific_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            hours_remaining = (next_midnight_pacific - pacific_now).total_seconds() / 3600
            reset_local = next_midnight_pacific.astimezone(ZoneInfo(LOCAL_DISPLAY_TZ))
            hours_display = max(1, round(hours_remaining))
            reset_clause = (
                f"This resets at midnight Pacific Time — "
                f"{reset_local.hour:02d}:{reset_local.minute:02d} UK time, "
                f"in about {hours_display} hour{'s' if hours_display != 1 else ''}"
            )
        except Exception:
            pass
    return f"{what_hit_it} has been reached for today. {reset_clause}."
