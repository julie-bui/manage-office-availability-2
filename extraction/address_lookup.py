"""Web-search-grounded address lookup for buildings Nominatim can't find
by name/postcode alone. Uses Gemini's Google Search grounding tool to
actually search the web for a named building and extract a real address
from genuine search results — this is not a Nominatim query variant, it's
a distinct fallback that finds new address information Nominatim never
had to begin with.
"""
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from . import quota
from .hard_timeout import call_with_timeout

# gemini-2.5-flash (used here previously) started returning 404
# NOT_FOUND — "no longer available to new users" — some time between the
# 2026-07 note below and a real Render SIGKILL investigation, also
# 2026-07 (see extraction.pipeline.BATCH_DEADLINE_SECONDS), confirming
# Google's model/quota lineup for grounding really does shift over time,
# as warned. Re-checked every currently-listed flash-tier candidate live
# at that point: gemini-2.5-flash-lite was ALSO already 404 (dead, not
# just deprecated-soon), while gemini-3.1-flash-lite/gemini-3.5-flash/
# gemini-2.0-flash all returned 429 RESOURCE_EXHAUSTED with a "check your
# plan and billing details" message — consistent with the day's shared
# grounding quota already being used up by that point (including by the
# 2.5-flash calls that failed 404 first), not each of those three being
# individually dead; a 429 (not a 400) also confirms the model accepted
# the google_search tool at all, it just had no quota left for it today.
# gemini-3.1-flash-lite is the pick, not just "any surviving candidate":
# it's the one already confirmed live and working on this exact API key
# (extraction.llm_fallback uses it successfully for plain extraction,
# same key/account), and this repo's own llm_fallback.py comment already
# documents it as "explicitly positioned for high-volume, cost-sensitive
# traffic" with "a much more generous free-tier daily quota" than
# gemini-3.5-flash's own 20-requests/day cap — both good reasons for a
# batch job doing many per-building lookups, not just "oldest surviving
# model" reasoning (which the 2.5-flash/2.5-flash-lite cutoff already
# disproves as a reliable signal — that purge hit an entire generation
# regardless of age relative to 3.x). Re-verify this still returns real
# results (not another 429) once a fresh day's grounding quota is
# available — this swap could not be confirmed working end-to-end for
# grounding specifically at the time it was made, only that it returned
# a quota error rather than a dead-model error.
#
# Prior note (2026-07, now superseded): "Confirmed empirically that
# Google Search grounding returns 429 RESOURCE_EXHAUSTED on this
# project's free tier for both gemini-3.1-flash-lite and gemini-2.0-flash
# (used elsewhere in this repo for plain, non-grounded extraction) — only
# gemini-2.5-flash actually has free grounding quota available." That
# note's own gemini-3.1-flash-lite 429 and today's are indistinguishable
# from here (both just "quota exhausted") — can't rule out this model
# having genuinely near-zero free grounding quota specifically, as
# opposed to 2.0-flash; only a fresh-quota re-test will tell for sure.
MODEL = "gemini-3.1-flash-lite"
NOT_FOUND = "NOT_FOUND"

# Confirmed (2026-07) that a single-source grounded answer can still be
# wrong — three real cases (Porters Place, Elsley, Kent House) each
# resolved to a plausible-looking but wrong address before this
# safeguard existed. Requiring at least this many independent sources to
# agree doesn't eliminate the risk (see MIN_INDEPENDENT_SOURCES test
# notes), but it does reject a lot of thin, single-page answers outright.
MIN_INDEPENDENT_SOURCES = 2

# A web search is not perfectly deterministic — the same building name can
# come back with a subtly different address (e.g. a neighboring postcode)
# on different calls. Caching by building name pins whichever address was
# found first, so repeated runs for the same building stay consistent
# instead of drifting between calls.
CACHE_PATH = Path(__file__).resolve().parent.parent / ".address_lookup_cache.json"
_cache = None

# Confirmed (2026-07) that grounding_metadata.grounding_chunks can come back
# completely empty on one call and populated on the next, for the exact same
# building/provider query — see the flakiness note in _search below. Two
# layers of retry handle this: _search itself immediately retries up to
# MAX_EMPTY_METADATA_RETRIES times within a single find_address() call
# (confirmed on real Render logs that the underlying search is usually
# already finding the correct address — City Tower -> 40 Basinghall Street,
# Elsley -> 20/30 Great Titchfield Street W1W 8BF, Kent House -> 17 Market
# Place W1W 8AJ, all genuinely correct — it's specifically the metadata
# that's sometimes missing, not the search itself), so most cases resolve
# within the same run rather than needing several separate future runs.
# This constant is the second, cross-run layer for the rarer case where
# even that immediate retry is still flaky: retrying forever on a building
# that's *consistently* flaky across many separate runs would quietly burn
# through the 20/day free-tier quota one call at a time, every time it's
# reprocessed. This caps it: after this many empty-metadata misses (each a
# full MAX_EMPTY_METADATA_RETRIES-attempt run, not a single call), give up
# and cache a final rejection like any other confident "not found".
MAX_EMPTY_METADATA_MISSES = 3

# How many times _search retries immediately (within one find_address()
# call, no cross-run persistence needed) when a confident answer comes
# back with completely empty grounding metadata — see the flakiness note
# there. Real API calls, so bounded rather than unlimited; 3 in practice
# resolves this most of the time given how often the very next attempt
# for the same query comes back with real citations.
MAX_EMPTY_METADATA_RETRIES = 3

# Hard wall-clock deadline per attempt (extraction.hard_timeout) — see
# extraction.llm_fallback's own CALL_TIMEOUT_SECONDS for why google-genai's
# http_options.timeout isn't enough on its own. Smaller than that one
# since this call can happen up to MAX_EMPTY_METADATA_RETRIES times per
# lookup — worst case ~75s across all attempts before _throttle_rpm below
# existed. With it, per-building worst case is no longer a fixed number:
# it also depends on how much of the shared per-minute budget (see
# RPM_LIMIT) other buildings in the same batch have already used. A batch
# with several ambiguous buildings can now legitimately take much longer
# overall — that's the accepted trade-off for getting real answers
# instead of 429s (see RPM_LIMIT's own comment) — but it does mean a
# large enough batch can still risk gunicorn's 120s request timeout
# (render.yaml). Not solved here; worth revisiting (e.g. an overall
# per-batch deadline that stops attempting further web-search lookups and
# falls back to bare Nominatim once time is running out) if that turns
# out to happen in practice.
CALL_TIMEOUT_SECONDS = 25

# Confirmed (2026-07, live Render logs) that Gemini's free tier enforces a
# separate, much tighter cap for this model on top of the per-day one
# already handled by extraction.quota:
# GenerateRequestsPerMinutePerProjectPerModel-FreeTier, limit 5. Every
# real API call this module makes shares this one project-wide budget —
# the first attempt for one building, an immediate flaky-metadata retry
# (MAX_EMPTY_METADATA_RETRIES above), and the first attempt for the next
# building in the same batch are all indistinguishable to Google's
# limiter. Without throttling, a single ambiguous building needing all
# MAX_EMPTY_METADATA_RETRIES attempts can burn most or all of this budget
# within seconds, so later buildings in the same batch then hit a real
# 429 instead of a genuine "can't find this address" for that building.
# _throttle_rpm enforces this as an actual rolling 60s window rather than
# a fixed delay before every call, so a batch with few or no flaky
# buildings pays no extra latency at all.
RPM_LIMIT = 5
RPM_WINDOW_SECONDS = 60.0
_recent_request_times = []


def _throttle_rpm():
    """Blocks until making another real Gemini call would keep this
    process's own recent call count at or under RPM_LIMIT within any
    trailing RPM_WINDOW_SECONDS window. Module-level state (like
    extraction.geocode._throttle's own _last_request_at) rather than
    per-batch — this app runs as a single gunicorn worker (render.yaml),
    and the limit itself is per-project, not per-request, so state needs
    to persist across separate /api/process calls too, not just within
    one."""
    global _recent_request_times
    now = time.monotonic()
    _recent_request_times = [t for t in _recent_request_times if now - t < RPM_WINDOW_SECONDS]
    if len(_recent_request_times) >= RPM_LIMIT:
        wait = RPM_WINDOW_SECONDS - (now - _recent_request_times[0])
        if wait > 0:
            print(
                f"[address_lookup] at gemini-2.5-flash's free-tier {RPM_LIMIT}/min "
                f"limit — waiting {wait:.1f}s before the next call"
            )
            time.sleep(wait)
        now = time.monotonic()
        _recent_request_times = [t for t in _recent_request_times if now - t < RPM_WINDOW_SECONDS]
    _recent_request_times.append(time.monotonic())


def find_address(building_name, provider_name=None, context_hint="a commercial office building in London, UK"):
    """Best-effort: returns (address_or_none, sources, quota_exhausted).
    address is a plain address string found via a real web search, or
    None if unconfigured, fewer than MIN_INDEPENDENT_SOURCES independent
    sources backed it, the model couldn't confidently find one, or any
    error occurred. sources is the list of distinct source sites (domain,
    or page title when a real domain isn't resolvable from the grounding
    response) the answer was based on — [] whenever address is None.
    quota_exhausted is True specifically when this attempt hit Gemini's
    429 RESOURCE_EXHAUSTED (never for any other kind of failure, and
    never True on a cache hit — only a genuine, just-attempted API call
    can hit a live quota) — extraction.pipeline surfaces this as a
    per-file note so a batch that falls back further than usual (bare
    Nominatim instead of a web-search-found address) is explained rather
    than silently degraded. Never raises — this is always an optional
    last-resort fallback, never something that should fail the batch.

    provider_name (e.g. "GPE", "MetSpace", "Knotel", "Kitts", "BC") is the
    source/broker this listing came from — included in the search so a
    generic building name (e.g. "Elsley") isn't confused with an unrelated
    building of the same name elsewhere. Confirmed empirically this
    matters: a bare-name search for "Elsley" alone found a building in
    Battersea (SW11 5LL), when the real GPE-managed "Elsley" is in
    Fitzrovia (W1W 8BF) — adding "GPE" to the search fixed it.
    """
    building_name = (building_name or "").strip()
    if not building_name:
        return None, [], False

    cache = _load_cache()
    key = f"{building_name.lower()}|{(provider_name or '').strip().lower()}"
    pending_misses = 0
    if key in cache:
        entry = cache[key]
        if isinstance(entry, dict):
            status = entry.get("status")
            if status == "flaky":
                # A prior run got an answer with no grounding chunks at all
                # (API flakiness, not a real rejection — see _search) and
                # hadn't yet hit MAX_EMPTY_METADATA_MISSES. Retry now
                # rather than trusting that non-final result forever.
                pending_misses = entry.get("misses", 0)
            elif status == "final" or entry.get("address") is not None:
                return entry.get("address"), entry.get("sources") or [], False
            else:
                # A pre-fix cache entry: {"address": None, "sources": []}
                # with no "status" at all. The old code cached this
                # shape for BOTH a genuine rejection and the flaky
                # empty-metadata false negative identically — there's no
                # way to tell which one produced any given entry already
                # on disk. Treat it as an unfinished flaky attempt (retry,
                # still bounded by MAX_EMPTY_METADATA_MISSES) rather than
                # trusting a rejection that might just be stale API
                # flakiness from before this fix existed.
                pending_misses = entry.get("misses", 0)
        else:
            # Tolerate the oldest cache format (a bare string or null, from
            # before `sources`/`status` were tracked) rather than crashing.
            return entry, [], False

    address, sources, cacheable, flaky, quota_exhausted = _search(building_name, provider_name, context_hint)
    if cacheable:
        # A genuine, confident answer from the model (found, or a
        # deliberate "not enough sources"/"no address" rejection) — never a
        # transient failure (e.g. a 429 quota error, a network hiccup, no
        # API key configured). Caching those would permanently poison this
        # building/provider pair: it'd keep returning None on every future
        # run even after the quota resets or a key gets added, since the
        # cache is checked before ever calling the API again. Confirmed
        # this was actually happening — a quota error while re-testing GPE
        # got cached as null for Elsley/Kent House/City Tower/Elm Yard,
        # silently blocking retries.
        cache[key] = {"address": address, "sources": sources, "status": "final"}
        _save_cache(cache)
    elif flaky:
        misses = pending_misses + 1
        if misses >= MAX_EMPTY_METADATA_MISSES:
            print(f"[address_lookup] '{building_name}' hit {misses} empty-metadata misses — giving up for good")
            cache[key] = {"address": None, "sources": [], "status": "final"}
        else:
            cache[key] = {"status": "flaky", "misses": misses}
        _save_cache(cache)
    return address, sources, quota_exhausted


def _search(building_name, provider_name, context_hint):
    """Returns (address_or_none, sources, cacheable, flaky, quota_exhausted)
    — cacheable is False whenever the reason for a None result is
    transient (should be retried on a future run), True when the model
    gave a confident, final answer (an address backed by enough sources,
    or a deliberate rejection) worth remembering. quota_exhausted is True
    only for the specific 429/RESOURCE_EXHAUSTED case, never for any
    other kind of failure (network, no key, no chunks) — see
    extraction.quota.is_quota_exceeded.

    Retries within this single call, up to MAX_EMPTY_METADATA_RETRIES
    times, when a confident answer comes back with completely empty
    grounding metadata (see the flakiness note below) — confirmed on real
    Render logs that the search itself is usually already correct in this
    case, it's specifically the metadata that's sometimes missing, so a
    fresh attempt resolves it far more often than not. Each attempt (this
    one and every other real call this module makes, including the first
    attempt for a different building later in the same batch) goes
    through _throttle_rpm first, so "a fresh attempt" here means paced to
    Gemini's shared per-minute free-tier limit, not literally instant —
    see RPM_LIMIT's own comment for why. Only returns flaky=True (letting
    find_address's own cross-run miss-counter take over) once every
    retry within this call has also come back with no metadata."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, [], False, False, False

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, [], False, False, False

    subject = f'a building called "{building_name}"'
    if provider_name:
        subject += f', operated by or listed under "{provider_name}"'

    prompt = (
        f"Search the web to find the real, current full street address (including "
        f"postcode) of {subject}, which is {context_hint}. Base your answer only on "
        f"actual web search results — if search results don't confirm a specific "
        f"address for this specific building, don't guess from prior knowledge alone.\n\n"
        f"Return ONLY the address as plain text on a single line "
        f'(e.g. "12 Example Street, London EC1A 1BB") — no commentary, no markdown, no quotes. '
        f"If a web search does not confidently identify a real address for this specific "
        f'building, return exactly {NOT_FOUND} and nothing else.'
    )

    client = genai.Client(api_key=api_key, vertexai=False)

    def _call():
        return client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                # Unlike gemini-2.5-flash (used here previously, which
                # rejected thinking_level outright), gemini-3.1-flash-lite
                # does support thinking_config — same as extraction.
                # llm_fallback's own equivalent call — so set it here too,
                # for the same reason: keep the token budget on the actual
                # answer, not hidden reasoning, for a lookup this short.
                thinking_config=types.ThinkingConfig(thinking_level="low"),
                # Also set, but confirmed (2026-07, via a real Render
                # crash on extraction.llm_fallback's own equivalent call)
                # NOT sufficient on its own for a call whose response can
                # keep trickling data slowly — see call_with_timeout below
                # for the actual enforcement. Kept as a reasonable inner
                # hint/backup; doesn't hurt.
                http_options=types.HttpOptions(timeout=CALL_TIMEOUT_SECONDS * 1000),
            ),
        )

    for attempt in range(1, MAX_EMPTY_METADATA_RETRIES + 1):
        # Shared per-minute budget, not per-building — see RPM_LIMIT's own
        # comment for why this one call site covers both an immediate
        # retry for this building and the first attempt for the next one.
        _throttle_rpm()
        try:
            # Hard, independent wall-clock deadline per attempt — see
            # extraction.llm_fallback's own call_with_timeout usage for why
            # http_options.timeout alone isn't enough: a worker blocked in
            # a low-level SSL socket read can't be interrupted cleanly by
            # gunicorn's own signal-based timeout, so an unbounded hang
            # here would eventually SIGKILL the whole worker, logged as
            # "Perhaps out of memory?" regardless of the real cause.
            # Wrapped in call_with_overload_retry so a transient 503 "high
            # demand" error gets a couple of automatic short-wait retries
            # before counting as a real failure for this building — see
            # that function's own docstring for why this is deliberately
            # NOT the same handling as a 429 quota error below.
            response = quota.call_with_overload_retry(
                lambda: call_with_timeout(_call, CALL_TIMEOUT_SECONDS), label=building_name
            )
        except Exception as e:
            # A quota error (429), a network failure, etc. — not a real
            # answer, so not cacheable; the caller should be free to retry
            # this exact building/provider pair again next time. Not
            # retried immediately like the empty-metadata case below —
            # a quota error in particular will just fail identically on
            # every immediate retry, wasting calls for nothing.
            print(f"[address_lookup] web search failed for '{building_name}': {type(e).__name__}: {e}")
            return None, [], False, False, quota.is_quota_exceeded(e)

        text = (response.text or "").strip()
        if not text or text.upper().startswith(NOT_FOUND):
            return None, [], True, False, False

        # Guard against the model adding anything beyond the single line
        # asked for despite instructions.
        address = text.splitlines()[0].strip().strip('"')
        sources, grounded = _extract_sources(response)

        if len(sources) >= MIN_INDEPENDENT_SOURCES:
            return address, sources, True, False, False

        if grounded:
            # At least one real chunk came back, just fewer than required
            # — a single thin source is exactly the failure mode that
            # produced three confirmed wrong addresses (Porters Place,
            # Elsley, Kent House) before this check existed — don't accept
            # it just because the model sounded confident. Still a real,
            # cacheable outcome (not a transient error) — the caller falls
            # back to the bare-name Nominatim tier from here. Not retried
            # — this isn't the empty-metadata flakiness case, the model
            # gave a genuine (if too-thin) answer.
            print(
                f"[address_lookup] rejecting '{address}' for '{building_name}' — only "
                f"{len(sources)} independent source(s) cited (need >= {MIN_INDEPENDENT_SOURCES}): {sources}"
            )
            return None, sources, True, False, False

        # Confirmed empirically (2026-07): the same query, called again
        # moments later, can come back with an identical confident address
        # in response.text but with grounding_metadata.grounding_chunks
        # entirely empty/None — even though an earlier call for the exact
        # same building returned real, distinct cited chunks, and Render
        # logs have since confirmed the address itself was correct all
        # along (City Tower -> 40 Basinghall Street, Elsley -> 20/30 Great
        # Titchfield Street W1W 8BF, Kent House -> 17 Market Place W1W
        # 8AJ). This is API-side flakiness in whether grounding metadata
        # is populated, not evidence the answer is unsupported — retry
        # (subject to _throttle_rpm's shared per-minute pacing at the top
        # of this loop, not truly instant anymore) rather than giving up
        # on the first flaky response.
        if attempt < MAX_EMPTY_METADATA_RETRIES:
            print(
                f"[address_lookup] '{building_name}' -> '{address}' but grounding metadata had "
                f"no chunks at all (attempt {attempt}/{MAX_EMPTY_METADATA_RETRIES}) — retrying"
            )
            continue

    # Every immediate retry came back with empty metadata too — treating a
    # deliberate "not enough sources" rejection as cacheable here would
    # permanently poison the cache for this building/provider pair
    # (find_address() checks the cache before ever calling the API again),
    # so one unlucky run blocked it forever, even past a quota reset. Not
    # cacheable here: let find_address's own cross-run miss-counter (and,
    # eventually, a future run) retry instead.
    print(
        f"[address_lookup] '{building_name}' still had no grounding chunks after "
        f"{MAX_EMPTY_METADATA_RETRIES} immediate attempts — not caching, will retry on a future run"
    )
    return None, [], False, True, False


def _extract_sources(response):
    """Returns (sources, grounded). sources is the best-effort list of
    distinct sources the grounding response actually cited, for the
    minimum-sources check above and so a wrong answer is traceable in the
    log/spreadsheet rather than an opaque coordinate. grounded is True
    whenever the response carried at least one raw grounding chunk at
    all — even if none of them resolved to a usable identity below — so
    the caller can tell "grounding ran and cited real sources, just too
    few of them" apart from "grounding metadata was empty this call"
    (see the flakiness note in _search above); those need different
    cacheability treatment. Gemini's grounding chunks give a redirect URL
    through Google's own domain, not the real source site, most of the
    time — so this prefers the chunk's page title (which usually names
    the real site) whenever the URL's own hostname isn't an independent
    domain."""
    try:
        metadata = response.candidates[0].grounding_metadata
        chunks = metadata.grounding_chunks if metadata else None
    except (AttributeError, IndexError):
        return [], False
    if not chunks:
        return [], False

    sources = []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if not web:
            continue
        uri = getattr(web, "uri", "") or ""
        title = getattr(web, "title", "") or ""
        host = urlparse(uri).netloc.lower()
        is_real_domain = host and "google" not in host and "vertexaisearch" not in host
        identity = host if is_real_domain else (title.strip() or uri)
        if identity and identity not in sources:
            sources.append(identity)
    return sources, True


STORAGE_KEY = "cache/address_lookup_cache.json"


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            return _cache
        except (json.JSONDecodeError, OSError):
            pass
    # Local disk is gone (Render's free-tier disk is ephemeral and wiped on
    # every cold start/redeploy — confirmed 2026-07 this was causing
    # buildings to get needlessly re-queried against the 20/day free-tier
    # grounding quota after every restart). Fall back to the same
    # S3-compatible object storage already used for batch outputs
    # (top-level storage.py, not a package under extraction/) — a no-op
    # returning None if unconfigured, so local-only dev behaves exactly
    # as before.
    import storage

    data = storage.fetch(STORAGE_KEY)
    if data is not None:
        try:
            _cache = json.loads(data)
            return _cache
        except json.JSONDecodeError:
            pass
    _cache = {}
    return _cache


def _save_cache(cache):
    """Local disk only — fast, synchronous, called after every single new
    entry. Mirroring to S3 here too would add a real network round-trip on
    every distinct building/address processed (confirmed empirically this
    was slow enough, stacked on top of Nominatim's 1-req/sec throttle and
    Gemini's multi-second grounding calls, to blow past gunicorn's default
    30s worker timeout on a multi-building batch) — see flush_to_storage,
    called once per batch instead, not once per record."""
    global _cache
    _cache = cache
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def flush_to_storage():
    """Best-effort, one-shot mirror of the current on-disk cache to the
    same S3-compatible storage used for batch outputs — call this once
    after a whole batch finishes (extraction.pipeline.process_files),
    not per-record. No-op if unconfigured or nothing has been cached yet
    this process."""
    if not CACHE_PATH.exists():
        return
    import storage

    storage.upload(STORAGE_KEY, CACHE_PATH)


def invalidate(substring):
    """Removes every cached entry whose "building|provider" key contains
    `substring` (case-insensitive) — from the in-memory cache this process
    is currently using, local disk, and the S3 mirror, in that order. See
    extraction.geocode.invalidate for why mutating the already-loaded
    `_cache` global matters, not just rewriting disk/S3: a long-running
    worker process only reads this cache from disk/S3 once, on first use,
    so a fix here is what actually stops the *current* process from
    continuing to serve a stale answer, without waiting for a
    redeploy/restart.
    Returns the list of keys actually removed (empty if nothing matched)."""
    cache = _load_cache()
    needle = (substring or "").strip().lower()
    if not needle:
        return []
    removed = [k for k in cache if needle in k]
    for k in removed:
        del cache[k]
    if removed:
        _save_cache(cache)
        flush_to_storage()
    return removed
