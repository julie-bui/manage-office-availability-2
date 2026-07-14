"""A true wall-clock deadline for a blocking call, independent of whatever
internal timeout semantics the callee's own HTTP client implements.

Confirmed via a real Render crash (2026-07) that setting google-genai's
own http_options.timeout wasn't enough on its own: verified empirically
(a 1ms http_options.timeout against the real API failed almost
instantly, so the mechanism itself works) that httpx's `timeout` bounds
the gap *between* chunks of data arriving, not the total call duration —
a large response (Crown Estate's extraction call asks for up to 24,000
output tokens, vs. a trivial test call) can keep trickling data slowly
enough that no single inter-chunk gap ever exceeds the configured
timeout, while the call's *total* duration still runs far longer than
it, exceeding gunicorn's own worker timeout with no clean exception ever
raised — the exact "Perhaps out of memory?" SIGKILL pattern confirmed by
that crash's traceback (still blocked in a low-level socket read).
"""
import concurrent.futures


def call_with_timeout(fn, timeout_seconds, *args, **kwargs):
    """Runs fn(*args, **kwargs) in a background thread and enforces a hard
    timeout_seconds deadline on the TOTAL call, raising TimeoutError if it
    isn't done in time — regardless of whether the callee's own HTTP
    client would have kept waiting longer. There's no safe way to force-
    kill a thread blocked in a C-level socket read, so a timed-out call
    may keep running in the background afterward; explicitly using
    shutdown(wait=False) (not a `with` block, which would default to
    wait=True and block on exactly the same hang this exists to avoid)
    lets this function return immediately either way rather than waiting
    for that orphaned thread to ever finish."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    finally:
        executor.shutdown(wait=False)
