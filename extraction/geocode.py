"""Free geocoding via OpenStreetMap Nominatim, with an on-disk cache and
rate limiting per Nominatim's usage policy (max 1 request/sec, and a
descriptive User-Agent identifying the app instead of a default library
one): https://operations.osmfoundation.org/policies/nominatim/
"""
import json
import math
import time
from pathlib import Path

import requests

from .address import extract_postcode

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "manage-office-availability/1.0 (contact: team@spacepoint.co.uk)"
MIN_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT = 10

# Confirmed empirically (2026-07, Crown Estate) that a common London street
# name with no distinguishing context can return a *confident-looking* top
# result that's actually many km away in an unrelated borough — Nominatim
# itself doesn't flag this, it just returns its single best guess as if
# unambiguous: "25 Bury Street, London, UK" -> Edmonton N9 (real address is
# St James's SW1, ~15km away); "1 Vine Street, London, UK" -> Walthamstow
# E17 (real address is Mayfair W1, ~12km away). Requesting a few more
# candidates and checking how far apart they are catches this — confirmed
# on several known-good addresses already in this app's own sources
# (Alfred Place, Clerkenwell Green, Regent Street, Curtain Road, Jermyn
# Street) that their own top few candidates all cluster within a few km of
# each other, so this threshold doesn't risk flagging a genuinely good
# match just for returning more than one plausible nearby result.
CANDIDATE_LIMIT = 3
AMBIGUITY_DISTANCE_KM = 5.0

# A DIFFERENT, closer-in ambiguity confirmed empirically (2026-07, MetSpace
# audit) that the distance check above can't catch at all: a single real
# building can have two separate OSM nodes just a few metres apart — one
# per distinct unit/entrance (e.g. an upstairs office vs. a ground-floor
# restaurant/bar sharing the same building) — each carrying a genuinely
# different postcode. Nominatim doesn't flag this either; it just returns
# whichever one it ranks first, with no signal that the two are actually
# different, unrelated postcodes for the same address. Confirmed on two
# real, independently cross-checked cases: "9-10 Market Place, London, UK"
# returned an "office"-classed node at W1W 8AE as its top result, but every
# real commercial listing for this exact office (Savills, CBRE, Workthere,
# Hubble, Rightmove) gives W1W 8AQ — the second candidate, a "restaurant"-
# classed node only ~1m away; "1 Curtain Road, London, UK" returned an
# "office"-classed node at EC2A 3NY, but the real listing gives EC2A 3JX —
# the second candidate, a "bar"-classed node ~5m away. Both real cases
# had candidates within single-digit metres of each other, so this uses a
# much tighter threshold than AMBIGUITY_DISTANCE_KM specifically to avoid
# flagging the normal, expected case of nearby-but-genuinely-different
# addresses a block or two apart (which legitimately do have different
# postcodes without being "the same building"). "Office"-classed results
# are not preferred over other classes here — that heuristic is exactly
# what produced the wrong answer both times.
SAME_BUILDING_DISTANCE_KM = 0.03

CACHE_PATH = Path(__file__).resolve().parent.parent / ".geocode_cache.json"

_cache = None
_last_request_at = 0.0


def geocode(address, confident=True):
    """Look up (lat, lng, postcode) for a free-text address via Nominatim.

    Returns (lat, lng, postcode, error): lat/lng are floats, or None if no
    confident match was found; postcode is whatever Nominatim's address
    breakdown reports for the match (normalized the same way as
    extraction.address.extract_postcode), or "" if it didn't include one —
    a useful fallback for sources whose own address text has no postcode
    at all. error is a short human-readable reason (never raises — a
    network failure is reported the same way as "no match", so callers can
    always just leave Lat/Lng/postcode blank on error rather than guessing).

    Results are cached on disk keyed by the normalized address string, so
    the same building is never re-geocoded across runs — except as
    described below for confident=False.

    confident: False for a low-confidence, last-resort lookup — currently
    just extraction.pipeline._geocode_records' bare-building-name
    fallback tier, tried only when neither a full address nor a
    web-search-grounded one was available. A bare name is inherently
    unreliable even when Nominatim does return a match (confirmed
    empirically, twice, on real sources — see that function's own
    docstring), so a confident=False call's cache entry is marked
    low_confidence and is never trusted as-is on a future lookup: the
    fetch is always redone (and the cache entry refreshed with whatever
    the fresh attempt found). Confirmed this gap was real, not
    theoretical — testing GPE while Gemini's quota was exhausted made the
    web-search tier fail every time, so every call fell through to this
    bare-name tier, and its cached answer (also wrong, same as before)
    was then trusted permanently, silently re-poisoning the cache the
    exact same way once already fixed. A cache entry written by a
    confident=True call (the default — a full address from the source
    text, a spelled-out house number, or a web-search-found address) is
    never affected by this and is still trusted forever, same as before
    this parameter existed."""
    address = (address or "").strip()
    if not address:
        return None, None, "", "No address to geocode"

    cache = _load_cache()
    key = _cache_key(address)
    if key in cache:
        hit = cache[key]
        if confident or not hit.get("low_confidence"):
            return hit.get("lat"), hit.get("lng"), hit.get("postcode", ""), hit.get("error")
        # else: both this call and the cached entry are low-confidence —
        # don't trust it, fall through to a fresh fetch below.

    lat, lng, postcode, error = _fetch(address)
    cache[key] = {"lat": lat, "lng": lng, "postcode": postcode, "error": error}
    if not confident:
        cache[key]["low_confidence"] = True
    _save_cache(cache)
    return lat, lng, postcode, error


def _fetch(address):
    _throttle()
    try:
        resp = requests.get(
            NOMINATIM_URL,
            # addressdetails=1 gets a structured breakdown (road, postcode,
            # city, ...) alongside the match, so we can fall back to
            # Nominatim's postcode when the source text didn't have one.
            # limit=CANDIDATE_LIMIT (not just 1) so the ambiguity check
            # below has other plausible candidates to compare the top
            # result against.
            params={"q": address, "format": "json", "limit": CANDIDATE_LIMIT, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        return None, None, "", f"Geocoding request failed: {e}"

    if not results:
        return None, None, "", "No geocoding match found for this address"

    try:
        lat = float(results[0]["lat"])
        lng = float(results[0]["lon"])
    except (KeyError, ValueError, TypeError) as e:
        return None, None, "", f"Unexpected geocoding response shape: {e}"

    postcode = extract_postcode((results[0].get("address") or {}).get("postcode", ""))

    other_candidates = []
    for other in results[1:]:
        try:
            other_lat, other_lng = float(other["lat"]), float(other["lon"])
        except (KeyError, ValueError, TypeError):
            continue
        other_postcode = extract_postcode((other.get("address") or {}).get("postcode", ""))
        other_candidates.append((other_lat, other_lng, other_postcode))

    ambiguity_error = _check_ambiguity(lat, lng, postcode, other_candidates)
    if ambiguity_error:
        # Treated the same as "no match" (not a distinct return shape) so
        # it falls through to whatever fallback the caller already has —
        # Needs manual lookup, or a further tier for a bare-name lookup —
        # instead of silently trusting a coincidental match.
        return None, None, "", ambiguity_error

    return lat, lng, postcode, None


def _check_ambiguity(lat, lng, postcode, other_candidates):
    """Pure logic behind _fetch's own ambiguity checks, split out so it can
    be unit-tested directly against synthetic candidate lists rather than
    live Nominatim responses. other_candidates is [(lat, lng, postcode),
    ...] for every result after the top one. Returns an error message
    string if either check trips, else None."""
    for other_lat, other_lng, other_postcode in other_candidates:
        distance = _distance_km(lat, lng, other_lat, other_lng)
        if distance > AMBIGUITY_DISTANCE_KM:
            return (
                f"Ambiguous match: top result is {distance:.1f}km from another plausible "
                f"candidate for this same address — not confident enough to trust"
            )
        if distance <= SAME_BUILDING_DISTANCE_KM:
            # See SAME_BUILDING_DISTANCE_KM's own comment — two candidates
            # this close are almost certainly two units/entrances of the
            # SAME building, so a different postcode between them is a
            # genuine, confirmed real-world conflict (not two candidates
            # merely resolving to the same rounded coordinate), not a
            # coincidence to shrug off.
            if other_postcode and postcode and other_postcode != postcode:
                return (
                    f"Ambiguous match: top result ({postcode}) and another candidate only "
                    f"{distance*1000:.0f}m away ({other_postcode}) disagree on postcode for what's "
                    "almost certainly the same building — not confident enough to trust either"
                )
    return None


def _distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km — just for the ambiguity check above,
    not precise surveying, so a simple haversine is plenty."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _throttle():
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _cache_key(address):
    return " ".join(address.strip().lower().split())


STORAGE_KEY = "cache/geocode_cache.json"


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
    # Same ephemeral-disk risk as extraction.address_lookup's cache — Render's
    # free-tier disk is wiped on cold start/redeploy, so fall back to the
    # same S3-compatible object storage used for batch outputs (top-level
    # storage.py) before giving up and starting from an empty cache.
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
    """Local disk only — see extraction.address_lookup._save_cache for why
    mirroring to S3 here too (once per record, called after every new
    address geocoded) was confirmed to add enough latency to risk
    exceeding gunicorn's default worker timeout on a multi-building batch.
    flush_to_storage below does the mirror once per batch instead."""
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
    """Removes every cached entry whose address key contains `substring`
    (case-insensitive) — from the in-memory cache this process is
    currently using, local disk, and the S3 mirror, in that order. Exists
    so a fix to geocoding/address-lookup logic isn't silently masked by a
    stale answer cached before the fix existed, without needing to
    hand-edit the cache file (locally, or the S3 copy via a bucket
    console) or wait for a redeploy — mutating the already-loaded `_cache`
    global directly (rather than only rewriting disk/S3 and leaving a
    running process's own copy untouched) is what actually clears it for
    the very next request this same worker handles. Returns the list of
    keys actually removed (empty if nothing matched)."""
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
