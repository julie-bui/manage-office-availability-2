"""Target schema for the consolidated master spreadsheet.

Derived from the example output (Kitt's Availability PDF): columns, ordering,
and the PCM/PSF relationship used across all three example sources.
"""
import re

from .address import extract_postcode

# Fields a rule parser or the LLM fallback actually extracts from a source
# document — must match the example output.
SOURCE_FIELDS = [
    "Area",
    "Building",
    "Floor/Unit",
    "Size (sq ft)",
    "Desks (max)",
    "Marketing Price (Based on Min Term) PCM",
    "Marketing Price (Based on Min Term) PSF",
    "Brochure PDF",
    "Min. Term",
    "Special Features",
    "State of Space",
    "Legal Structure",
    "Broker Fee",
    "Contacts",
    "Floor Plan",
    "High Res Images",
]

# Extra columns required for Kato bulk-upload compatibility (see the
# "Loader" sheet of kato-disposals-loader-example-LATEST VERSION (3).xlsx).
# These are derived/defaulted in normalize_record, never extracted directly
# from a source document.
KATO_FIELDS = [
    "External Ref",
    "Assigned Agents",
    "Property Address 1",
    "Property Postcode",
    "Lat",
    "Lng",
    "For Sale",
    "To Let",
]

# Column order for the master spreadsheet — Kato columns (External Ref
# first) lead, followed by the extracted source columns.
COLUMNS = KATO_FIELDS + SOURCE_FIELDS

# Fields the LLM fallback must return JSON for — only the ones actually
# present in source documents. The Kato fields are derived afterwards by
# normalize_record, not invented by the LLM (that would risk hallucinated
# postcodes/coordinates).
LLM_FIELDS = list(SOURCE_FIELDS)


def blank_record():
    return {c: "" for c in COLUMNS}


def normalize_record(record):
    """Fill in any missing columns, coerce numeric columns, derive PCM/PSF
    from each other when only one is present (all three example sources
    confirm PSF == PCM * 12 / size_sqft, annualized), and derive the Kato
    bulk-upload columns from the fields actually extracted above.

    Lat/Lng are deliberately left blank here — geocoding is a network call
    and is done as a separate step (see extraction.geocode), keyed off the
    Property Address 1 / Property Postcode this function produces.
    """
    out = blank_record()
    for c in SOURCE_FIELDS:
        v = record.get(c, "")
        out[c] = "" if v is None else v

    # Underscore-prefixed keys are a rule's own staging fields for a later
    # pipeline/app.py step (e.g. gpe.py's "_high_res_candidates", resolved
    # into a real High Res Images URL/gallery by app.py's
    # _finalize_high_res_images after normalization) — not part of the
    # output schema itself, so they're not in SOURCE_FIELDS/blank_record,
    # but need to survive this rebuild rather than being silently dropped.
    for k, v in record.items():
        if k.startswith("_"):
            out[k] = v

    out["Size (sq ft)"] = _to_number(out["Size (sq ft)"])
    out["Desks (max)"] = _to_number(out["Desks (max)"])
    out["Marketing Price (Based on Min Term) PCM"] = _to_number(out["Marketing Price (Based on Min Term) PCM"])
    out["Marketing Price (Based on Min Term) PSF"] = _to_number(out["Marketing Price (Based on Min Term) PSF"])

    size = out["Size (sq ft)"]
    pcm = out["Marketing Price (Based on Min Term) PCM"]
    psf = out["Marketing Price (Based on Min Term) PSF"]

    if size:
        if pcm and not psf:
            out["Marketing Price (Based on Min Term) PSF"] = round(pcm * 12 / size, 2)
        elif psf and not pcm:
            out["Marketing Price (Based on Min Term) PCM"] = round(psf * size / 12, 2)

    # External Ref identifies the whole source batch (same value for every
    # row in a spreadsheet), not an individual listing — it's stamped in
    # by extraction.pipeline.process_files once the provider name and
    # processing date are known, not derivable per-record here.
    out["External Ref"] = ""
    # Required field — a source with no contact/agent info at all (no
    # Contacts value to mirror) would otherwise leave this blank. Distinct
    # from Contacts itself (name + email + phone, e.g. Knotel's "Knotel
    # Brokers, londonbrokers@knotel.com, 0204 571 4271") — this is a
    # name-only subset of that same information (see names_only), never a
    # duplicate of the fuller field.
    out["Assigned Agents"] = names_only(out["Contacts"]) or "Unknown"
    # Property Address 1 is deliberately left as Building here, unchanged
    # from how this has always worked — extraction.pipeline reads THIS
    # value (not a cleaned-up one) for its own geocoding, exactly as
    # before, so that logic (including the retry fallbacks built
    # specifically to handle a combined "Name, Address" string confusing
    # Nominatim) is untouched and stays exactly as reliable as it already
    # is. A separate, later pipeline step (extraction.pipeline.
    # process_files, after geocoding has already run) overwrites this
    # with a clean street-only value via street_address_only, once
    # nothing further needs the fuller text.
    out["Property Address 1"] = out["Building"]
    out["Property Postcode"] = extract_postcode(out["Building"])
    out["Lat"] = ""
    out["Lng"] = ""
    # "Sale Price" isn't a spreadsheet column — it's a raw per-listing
    # signal some sources provide (e.g. BC lists a separate Sale Price
    # alongside its rental price for a handful of listings) that a rule
    # parser (extraction/rules/grid.py) or the LLM fallback may set on the
    # raw record. For Sale reflects whether that signal is a genuine value,
    # not "N/A"/blank — "No" whenever a source has no such signal at all,
    # which is every current source except a few BC listings.
    out["For Sale"] = "Yes" if _has_real_value(record.get("Sale Price")) else "No"
    out["To Let"] = "Yes"

    return out


_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.\w+$")


def _looks_like_phone(s):
    """True for a comma-part that's a phone number in ANY format — UK
    domestic (Knotel's "0204 571 4271"), international with country code/
    parens (GPE's "+44 (0) 7435 939 956"), or anything else shaped like
    one. Deliberately format-agnostic (no fixed digit-grouping pattern)
    after a real miss: an earlier version of this check only recognized
    the UK-domestic shape, so GPE's own international-format numbers
    weren't stripped and leaked straight into Assigned Agents. A phone
    number is the only kind of Contacts part that's ALL digits/spaces/
    punctuation with no letters at all and has a real number of digits in
    it — a name or company never is, so this can't accidentally strip
    either of those."""
    if not s or any(ch.isalpha() for ch in s):
        return False
    return sum(ch.isdigit() for ch in s) >= 7


def names_only(contacts):
    """Strips any email address or phone number out of a comma-separated
    Contacts value, leaving just the name(s)/company — e.g. Knotel's
    "Knotel Brokers, londonbrokers@knotel.com, 0204 571 4271" becomes
    "Knotel Brokers", GPE's "David Korman, +44 (0) 7435 939 956" becomes
    "David Korman". A source whose Contacts is already just names
    (Kitt's "Leah Noray, Ben Danaher", Breezblok's "Sales") passes
    through unchanged — those rules never put an email/phone into
    Contacts in the first place, so there's nothing here to strip.
    General-purpose (not tied to any one rule) so it applies the same
    way regardless of which source produced Contacts, current or
    future."""
    if not contacts:
        return ""
    parts = [p.strip() for p in contacts.split(",")]
    names = [p for p in parts if p and not _EMAIL_RE.match(p) and not _looks_like_phone(p)]
    return ", ".join(names)


_TRAILING_POSTCODE_RE = re.compile(
    r"(?:\bLondon\s+)?[A-Za-z]{1,2}\d[A-Za-z\d]?(?:\s*\d[A-Za-z]{2})?\s*$",
    re.IGNORECASE,
)
_BARE_LONDON_RE = re.compile(r"^london$", re.IGNORECASE)


def _clean_trailing_segment(segment):
    """Strips a trailing UK postcode (full or partial), optionally
    preceded by the literal word "London", from the END of a single
    comma-separated address segment — e.g. "Covent Garden WC2" ->
    "Covent Garden" (a neighbourhood name, not a street, so it still
    isn't the answer on its own — see street_address_only), "London
    EC3M 5JE" -> "" (the whole segment was just city+postcode, nothing
    else). Never touches anything before that trailing postcode-shaped
    text, so a real street name — which doesn't itself end in a bare
    postcode pattern — passes through unchanged."""
    stripped = _TRAILING_POSTCODE_RE.sub("", segment).strip()
    if _BARE_LONDON_RE.match(stripped):
        return ""
    return stripped


def street_address_only(building):
    """"Building Name, Street Number Street Name" derived from a Building
    string that may combine a marketing/building name with the real
    street and a trailing city/postcode — keeping the building name
    (when one genuinely exists) but never the postcode, e.g. "Gilray
    House, 146-150 City Rd, London EC1V 2RL" -> "Gilray House, 146-150
    City Rd"; "Market Exchange, 8 Macklin Street, Covent Garden WC2" ->
    "Market Exchange, 8 Macklin Street"; "John Stow House, 18 Bevis
    Marks, London EC3A 7JB" -> "John Stow House, 18 Bevis Marks". When
    there's no separate name at all, just the street remains: "2 Leonard
    Circus, EC2A 4LW" -> "2 Leonard Circus".

    Splits on commas, strips a trailing postcode/city suffix off the
    last segment (repeating if stripping it away entirely reveals
    another one behind it, e.g. a separate "London" segment), then finds
    whichever remaining segment has a house number: the LAST one if it
    has a digit (the common "Name, Street" shape — everything before it
    is the name, kept), else the FIRST one that does (the less common
    case where the real number sits in an earlier segment than a
    trailing non-numbered qualifier — e.g. Classic House's "174-180
    Martha's Buildings, Old St", where "Old St" is dropped but "Classic
    House" is kept since it precedes the digit-bearing segment; or 6
    Maiden Lane's own "Covent Garden" neighbourhood mention with no
    number of its own, where the digit-bearing segment IS the first one,
    so there's nothing before it to keep as a separate name — "6 Maiden
    Lane" alone, "Covent Garden" dropped). Falls back to the fullest
    available text, never blank, if no segment has a digit at all —
    safer than guessing which single word is "the" street name when
    there's no way to confidently tell.

    Deliberately never called from normalize_record itself — Property
    Address 1 there is still set to the unmodified Building, since
    extraction.pipeline's geocoding is keyed off that full text (see its
    own module docstring for why: a combined "Name, Address" string
    confuses Nominatim, which is exactly what its retry-candidate
    fallbacks exist to work around). This is called once, separately,
    after that geocoding has already run — geocoding accuracy is
    completely unaffected by this function existing at all."""
    text = (building or "").strip()
    if not text:
        return ""

    segments = [s.strip() for s in text.split(",") if s.strip()]
    if not segments:
        return ""

    while segments:
        cleaned_last = _clean_trailing_segment(segments[-1])
        if cleaned_last == segments[-1]:
            break  # nothing postcode/city-shaped left to strip here — stop
        if cleaned_last:
            segments[-1] = cleaned_last
            break
        segments.pop()  # that segment was purely city/postcode — drop it, check what's now last

    if not segments:
        return ""
    if len(segments) == 1:
        return segments[0]

    if any(ch.isdigit() for ch in segments[-1]):
        street_idx = len(segments) - 1
    else:
        street_idx = next((i for i, seg in enumerate(segments) if any(ch.isdigit() for ch in seg)), None)

    if street_idx is None:
        return ", ".join(segments)

    name_segments = segments[:street_idx]
    street = segments[street_idx]
    return f"{', '.join(name_segments)}, {street}" if name_segments else street


_NO_VALUE_TOKENS = {"", "n/a", "na", "-", "none", "tbc", "0"}


def _has_real_value(v):
    if v is None:
        return False
    return str(v).strip().lower() not in _NO_VALUE_TOKENS


def _to_number(v):
    if v == "" or v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    s = s.replace("£", "").replace(",", "").strip()
    if not s:
        return ""
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return ""
