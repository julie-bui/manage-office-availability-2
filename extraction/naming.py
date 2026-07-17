"""Resolves an output spreadsheet name for each processed file, and keeps
names unique within a batch.
"""
import re
from datetime import date as _date
from email.utils import parsedate_to_datetime
from pathlib import Path

# Rules tied to a specific, known sender — the rule name itself IS the
# provider name. The generic "Grid/Tabular" rule matches any tabular input
# and isn't tied to a sender, so it doesn't count as a confident identification.
NAMED_RULES = {"Knotel", "MetSpace", "Workplace Plus", "GPE", "BC", "Breezblok"}

ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')
LEADING_REPLY_PREFIX = re.compile(r"^(fw|fwd|re)[:_\-\s]+", re.IGNORECASE)

# Words that can appear as the LAST " - "-separated segment of a real
# filename without being an area/location name — extract_area_hint below
# must not mistake one of these for the actual distinguishing part.
_GENERIC_FILENAME_SEGMENTS = {
    "availability", "update", "updates", "report", "live", "current",
    "listing", "listings", "spaces", "space", "fully managed",
}

_MONTH_NAMES = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MONTH_LOOKUP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
# A "Month Day" or "Day Month" date fragment (e.g. "June 26", "26 June",
# "Dec 3rd") — deliberately NOT just "any segment with a digit in it":
# confirmed real (2026-07), a UNION filename's own trailing segment can be
# a genuine area/subset name that just happens to end in a number ("City
# 2", a second installment of the same "City" export, not a different
# area) — a blanket digit check would wrongly reject that as if it were
# part of the date.
_DATE_FRAGMENT_RE = re.compile(
    rf"^(?:(?:{_MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_NAMES})\.?)$",
    re.IGNORECASE,
)
# Filename date shapes used for External Ref when email/PDF metadata is
# missing (UNION "June 26", Knotel "30_06_2026", "14th July", ISO dates).
_FILENAME_DATE_PATTERNS = (
    re.compile(
        rf"(?P<month>{_MONTH_NAMES})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:\s*,?\s*(?P<year>20\d{{2}}))?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_NAMES})\.?"
        rf"(?:\s*,?\s*(?P<year>20\d{{2}}))?",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<year>20\d{2})[-_./](?P<month>\d{1,2})[-_./](?P<day>\d{1,2})"),
    re.compile(r"(?P<day>\d{1,2})[-_./](?P<month>\d{1,2})[-_./](?P<year>20\d{2})"),
    re.compile(r"(?P<day>\d{1,2})_(?P<month>\d{1,2})_(?P<year>20\d{2})"),
)


def resolve_provider_name(rule_name, filename, llm_source_name=None):
    """rule_name: the name returned by extraction.rules.try_rules(), or None
    if nothing matched (LLM fallback was used). llm_source_name: the source
    name the LLM identified, if the LLM fallback was used."""
    if rule_name in NAMED_RULES:
        return rule_name
    if llm_source_name:
        cleaned = _sanitize(llm_source_name)
        if cleaned:
            return cleaned
    return _name_from_filename(filename)


def extract_date(content):
    """Best-effort YYYY-MM-DD from an email's Date header, or None."""
    date_header = (content or {}).get("date")
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def extract_date_from_filename(filename, fallback_year=None):
    """Best-effort YYYY-MM-DD from a source filename date fragment.

    Confirmed real (2026-07): UNION xlsx uploads carry no email Date and no
    spreadsheet file_date, but the filename itself includes "June 26".
    Without this, External Ref fell back to today's processing date.

    Year resolution when the fragment is month+day only: prefer an explicit
    year elsewhere in the same filename, else fallback_year (typically the
    file's mtime year), else the current calendar year.
    """
    stem = Path(filename or "").stem
    if not stem:
        return None
    stem = LEADING_REPLY_PREFIX.sub("", stem).strip()
    year_hint = fallback_year
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", stem)
    if year_match:
        year_hint = int(year_match.group(1))
    if year_hint is None:
        year_hint = _date.today().year

    for pattern in _FILENAME_DATE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        parts = match.groupdict()
        try:
            day = int(parts["day"])
            raw_month = parts["month"]
            if raw_month.isdigit():
                month = int(raw_month)
            else:
                month = _MONTH_LOOKUP.get(raw_month.lower().rstrip("."))
            if not month:
                continue
            year = int(parts["year"]) if parts.get("year") else int(year_hint)
            return _date(year, month, day).isoformat()
        except (KeyError, TypeError, ValueError):
            continue
    return None


def resolve_source_date(content):
    """Best-effort YYYY-MM-DD for when the source document was actually
    created/sent, in priority order:
      1. An email's Date header (the actual sent date) — extract_date().
      2. PDF/DOCX metadata (creation date, then modified date, whichever
         file_readers could read) — content["file_date"].
      3. A date fragment in the uploaded filename (e.g. UNION "June 26",
         Knotel "30_06_2026") — extract_date_from_filename().
    Returns None if nothing is available — the caller should fall back to
    the processing date as a last resort rather than guessing."""
    content = content or {}
    return (
        extract_date(content)
        or content.get("file_date")
        or extract_date_from_filename(
            content.get("filename") or content.get("source_file_name") or "",
            fallback_year=content.get("file_mtime_year"),
        )
    )


def extract_area_hint(filename, provider_name=None):
    """Best-effort trailing area/location name from the ORIGINAL uploaded
    filename — e.g. "UNION - Availability - June 26 - City.xlsx" ->
    "City", "UNION  - Availability - June 26 - Aldgate & Whitechapel.xlsx"
    -> "Aldgate & Whitechapel". Confirmed real (2026-07): UNION exports
    the same provider/date combination as several separate area-based
    files (City, Aldgate & Whitechapel, Shoreditch, ...), so provider +
    date alone isn't enough to tell them apart in a batch/download list.

    Splits the filename's stem on " - " (tolerant of a stray double space
    before the dash — confirmed present in at least one real filename)
    and takes the LAST segment that looks like a genuine area name, not a
    date fragment ("June 26" — _DATE_FRAGMENT_RE), a generic descriptor
    ("Availability", "Update", ... — _GENERIC_FILENAME_SEGMENTS), or the
    provider name itself. A segment like "City 2" (confirmed real,
    2026-07 — a second installment of the same "City" export, not a
    different area) is deliberately NOT rejected just for ending in a
    digit — only an actual date-shaped fragment is. Requires at least 2
    segments to start with — a filename with no " - " structure at all
    (e.g. "Fw_ MetSpace Availability Update.eml") has no distinct
    trailing part to extract, so this returns None rather than treating
    the WHOLE name as an "area". Returns None if nothing in the filename
    looks like a genuine area hint at all — the caller should fall back
    to Area field consensus (area_from_records below), then a plain
    numeric collision suffix (make_unique_names)."""
    stem = Path(filename).stem
    stem = LEADING_REPLY_PREFIX.sub("", stem).strip()
    segments = [s.strip() for s in re.split(r"\s*-\s*", stem) if s.strip()]
    if len(segments) < 2:
        return None
    for segment in reversed(segments):
        if _DATE_FRAGMENT_RE.match(segment):
            continue
        if segment.lower() in _GENERIC_FILENAME_SEGMENTS:
            continue
        if provider_name and segment.lower() == provider_name.lower():
            continue
        return _sanitize(segment)
    return None


def area_from_records(records):
    """Best-effort area name when every record extracted from this file
    that HAS an Area value agrees on it — a weaker signal than the
    filename itself (a source can genuinely mix multiple areas under one
    umbrella export, varying Area row to row, in which case this
    correctly returns None), so only used when the filename gave no hint
    at all (extract_area_hint above). A record with no Area value at all
    doesn't break consensus — it carries no information either way,
    unlike a genuinely different non-empty value."""
    areas = {(r.get("Area") or "").strip() for r in records}
    areas.discard("")
    if len(areas) == 1:
        return _sanitize(next(iter(areas)))
    return None


def make_unique_names(names):
    """names: list of fully-resolved display names, one per file, in
    order — provider name, plus an optional area disambiguator and the
    source date, already baked in by the caller (see
    extraction.pipeline's own construction, which tries extract_area_hint
    then area_from_records above before falling back to provider name
    alone). Returns a list of final, collision-free names in the same
    order — the first file to claim a name keeps it as-is; any later
    EXACT collision (identical provider, area, and date — every other
    available disambiguating signal already tried and still identical)
    gets an incrementing "(2)", "(3)", ... suffix as the last resort."""
    seen_count = {}
    used = set()
    final = []
    for base in names:
        if base not in used:
            used.add(base)
            final.append(base)
            continue

        seen_count[base] = seen_count.get(base, 1) + 1
        candidate = f"{base} ({seen_count[base]})"
        while candidate in used:
            seen_count[base] += 1
            candidate = f"{base} ({seen_count[base]})"
        used.add(candidate)
        final.append(candidate)
    return final


def _sanitize(name):
    cleaned = ILLEGAL_FILENAME_CHARS.sub("", name).strip()
    return cleaned[:40]


def _name_from_filename(filename):
    stem = Path(filename).stem
    stem = LEADING_REPLY_PREFIX.sub("", stem).strip()

    tokens = re.split(r"[\s_]+", stem)
    first = re.sub(r"[^A-Za-z0-9]", "", tokens[0]) if tokens else ""
    if first and not first.isdigit():
        return first[0].upper() + first[1:]

    # First token wasn't usable (e.g. purely numeric, or empty) — fall back
    # to a cleaned-up version of the whole filename.
    words = re.sub(r"[^A-Za-z0-9]+", " ", stem).split()
    condensed = "".join(w.capitalize() for w in words)[:40]
    return condensed or "Upload"
