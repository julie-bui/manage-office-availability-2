import re

# Special Features soft cap (Kitt-aligned): ~50 words, ending on a complete
# sentence or complete `;`-separated amenity phrase — never mid-word/phrase.
SPECIAL_FEATURES_MAX_WORDS = 50
SPECIAL_FEATURES_AMENITY_MAX_WORDS = 50
# Amenity-list item count when cleaning brochure / semicolon dumps.
SPECIAL_FEATURES_AMENITY_MAX_ITEMS = 12
# Primary sheet notes at or under this word count are preferred over brochure essays.
SPECIAL_FEATURES_SHORT_MAX_WORDS = 40

# State of Space: mirror clean/cap treatment; soft prose backstop stays ~250
# while messy OCR prefers compact fit-out status tags (Kitt template style).
STATE_OF_SPACE_MAX_WORDS = 250
STATE_OF_SPACE_SHORT_MAX_WORDS = 40
PROSE_FIELD_MAX_WORDS = SPECIAL_FEATURES_MAX_WORDS

_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s+|$)")
# Compact fit-out / availability status phrases (Kitt State of Space style).
_STATE_OF_SPACE_STATUS_RE = re.compile(
    r"(?i)\b("
    r"fully\s+fitted|partially\s+fitted|"
    r"fit[\s\-]*outs?\s+underway|fitouts?\s+underway|"
    r"cat\s*[ab]\s*[-–—]?\s*(?:custom\s+)?fit\s*outs?(?:\s+opportunity)?|"
    r"cat\s*[ab]\s*(?:fit\s*out|fitted)|"
    r"custom\s+fit\s*out\s+opportunity|"
    r"plug\s*(?:and|&)\s*play|"
    r"available\s+(?:now|immediately)|immediate(?:\s+availability)?|"
    r"vacant\s+possession|coming\s+soon|under\s+offer"
    r")\b"
)

# Semicolon after a conjunction/preposition → mid-phrase PDF break.
_CONJ_SEMI_RE = re.compile(
    r"\b(and|or|with|to|for|of|in|on|at|by|from|into|over|under|the)\s*;\s*",
    re.IGNORECASE,
)
# Lowercase continuation across a semicolon: "bright; efficient".
# Require both sides to be real words (3+ letters) so vertical OCR
# "d; n; 2; h; t; u; o; S" is not glued into nonsense.
_LOWER_CONT_SEMI_RE = re.compile(r"(?<=[a-z]{3})\s*;\s*(?=[a-z]{3})")
_TRAILING_PUNCT_RE = re.compile(r"[,.;:!?·•]+$")
_PAGE_JUNK_RE = re.compile(
    r"(?i)^(page\s*\d+|continued|cont\.?|see over|www\.|http\S+)$"
)
_CAMEL_BOUNDARY_RE = re.compile(
    r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=\D)(?=\d)|(?<=\d)(?=[A-Za-z])"
)
_SHEET_HEADER_RE = re.compile(
    r"(?i)^(current\s*floor\s*type|current\s*spec|square\s*footage|floor\s*size|"
    r"size\s*sq\.?\s*ft|minimum\s*term|monthly\s*rate|price\s*p/?sq\.?\s*ft|"
    r"desks?\s*\(?max\)?|marketing\s*price|floor\s*/?\s*unit)$"
)
# Short amenity / desk tokens that must not be stripped as PDF junk.
_KEEP_SHORT_TOKENS = {
    "ac",
    "av",
    "cat",
    "epc",
    "hr",
    "hvac",
    "it",
    "led",
    "m&e",
    "mr",
    "wc",
    "wifi",
}
# Common English words that appear reversed in multi-column/vertical PDF OCR.
_COMMON_ENGLISH_WORDS = frozenset(
    {
        "space",
        "union",
        "from",
        "south",
        "north",
        "east",
        "west",
        "floor",
        "office",
        "kitchen",
        "fitted",
        "furnished",
        "available",
        "building",
        "street",
        "square",
        "house",
        "suite",
        "desk",
        "desks",
        "meeting",
        "rooms",
        "storage",
        "shower",
        "showers",
        "terrace",
        "reception",
        "lighting",
        "air",
        "conditioning",
        "raised",
        "floors",
        "bike",
        "cycle",
        "fibre",
        "fiber",
        "connectivity",
        "modern",
        "refurbishment",
        "natural",
        "light",
        "efficient",
        "floorplate",
        "agile",
        "work",
        "stations",
        "fully",
        "and",
        "the",
        "with",
        "for",
        "this",
        "that",
        "area",
        "unit",
        "ground",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "london",
        "city",
        "spec",
        "type",
        "current",
        "footage",
        "size",
        "term",
        "rate",
        "price",
        "brochure",
        "contact",
        "features",
        "amenities",
        "description",
        "specification",
    }
)


def titlecase_area(s):
    """Like str.title(), but doesn't capitalize the letter right after an
    apostrophe (str.title() turns "ST JAMES'S" into "St James'S")."""
    words = []
    for w in s.split(" "):
        if "'" in w and len(w) > 1:
            words.append(w[0].upper() + w[1:].lower())
        else:
            words.append(w.capitalize())
    return " ".join(words)


def cap_prose_field(text, max_words=PROSE_FIELD_MAX_WORDS):
    """Cap long prose to about *max_words* on a complete boundary.

    Short values pass through unchanged. Prefer the last complete sentence
    *or* the last complete ``;``-separated amenity item at or before
    *max_words* (whichever keeps more content). If neither boundary fits,
    fall back to a soft word cap with an ellipsis (never mid-word).
    """
    if text is None:
        return ""
    text = str(text).strip()
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return text

    candidates = []

    best_end = None
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if len(text[:end].split()) <= max_words:
            best_end = end
        else:
            break
    if best_end is not None:
        candidates.append(text[:best_end].rstrip())

    amenity_prefix = _cap_at_amenity_item_boundary(text, max_words)
    if amenity_prefix:
        candidates.append(amenity_prefix)

    if candidates:
        # Keep the richest complete prefix under the limit.
        return max(candidates, key=lambda s: (len(s.split()), len(s)))

    # No complete boundary under the limit — soft word cut, never mid-word.
    capped = " ".join(words[:max_words]).rstrip(".,;:!? ")
    return f"{capped}..."


def _cap_at_amenity_item_boundary(text, max_words):
    """Return the longest ``; ``-joined prefix of complete amenity items ≤ max_words."""
    if ";" not in text:
        return None
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) < 2:
        return None
    kept = []
    for part in parts:
        trial = "; ".join(kept + [part])
        if len(trial.split()) <= max_words:
            kept.append(part)
        else:
            break
    if not kept or len(kept) >= len(parts):
        # Nothing dropped at an item boundary (first item alone may exceed).
        return None
    return "; ".join(kept)


def cap_special_features(text, max_words=SPECIAL_FEATURES_MAX_WORDS):
    """Cap Special Features; see ``cap_prose_field``."""
    return cap_prose_field(text, max_words=max_words)


def cap_state_of_space(text, max_words=STATE_OF_SPACE_MAX_WORDS):
    """Cap State of Space; see ``cap_prose_field``."""
    return cap_prose_field(text, max_words=max_words)


def is_useful_primary_prose_field(text, *, max_words=SPECIAL_FEATURES_SHORT_MAX_WORDS):
    """True when a short primary/source-sheet value should win over brochure essays."""
    text = "" if text is None else str(text).strip()
    if not text:
        return False
    if looks_like_ocr_layout_noise(text):
        return False
    return len(text.split()) <= max_words


def is_useful_primary_special_features(text):
    """True when a short primary Special Features value should win over brochure essays."""
    return is_useful_primary_prose_field(text, max_words=SPECIAL_FEATURES_SHORT_MAX_WORDS)


def is_useful_primary_state_of_space(text):
    """True when a short primary State of Space value should win over brochure essays."""
    return is_useful_primary_prose_field(text, max_words=STATE_OF_SPACE_SHORT_MAX_WORDS)


def looks_like_long_or_messy_features(text):
    """True for brochure essays / amenity dumps that should not replace a short primary."""
    text = "" if text is None else str(text).strip()
    if not text:
        return False
    if looks_like_ocr_layout_noise(text):
        return True
    if len(text.split()) > SPECIAL_FEATURES_SHORT_MAX_WORDS:
        return True
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) > SPECIAL_FEATURES_AMENITY_MAX_ITEMS:
        return True
    return _looks_like_amenity_dump(text)


def looks_like_ocr_layout_noise(text):
    """True when PDF multi-column / vertical-label OCR dominates the fill."""
    text = "" if text is None else str(text).strip()
    if not text:
        return False
    parts = [p.strip() for p in re.split(r"[;\n•·]+", text) if p.strip()]
    if not parts:
        return False

    single_char = sum(1 for p in parts if _is_single_char_part(p))
    if single_char >= 4:
        return True
    # Long run of 1-char segments (vertical label exploded across `;`).
    run = 0
    for part in parts:
        if _is_single_char_part(part):
            run += 1
            if run >= 4:
                return True
        else:
            run = 0

    tokens = re.findall(r"[A-Za-z0-9&]+", text)
    if not tokens:
        return False
    reversed_hits = sum(1 for t in tokens if _is_reversed_english_token(t))
    consonant_junk = sum(1 for t in tokens if _is_consonant_garbage_token(t))
    header_hits = sum(1 for p in parts if _is_sheet_header_part(p))
    code_hits = sum(1 for t in tokens if _is_code_like_token(t) and not t.isdigit())

    signal = reversed_hits + consonant_junk + header_hits + (1 if single_char >= 2 else 0)
    if reversed_hits >= 2 or (reversed_hits >= 1 and single_char >= 2):
        return True
    if signal >= 3 and len(parts) >= 5:
        return True
    junkish = reversed_hits + consonant_junk + header_hits + code_hits + single_char
    if len(tokens) >= 6 and junkish / len(tokens) >= 0.45:
        return True
    return False


def clean_prose_or_amenity_field(text, *, force_amenity_list=False, max_words=SPECIAL_FEATURES_MAX_WORDS):
    """Clean a prose/amenity field into a short `; `-joined list when messy.

    Short useful notes (price drops, desk extras, "Fitted", "Immediate") pass
    through. Semicolon amenity dumps and forced brochure fills are
    artifact-cleaned, truncated to ~12 items / ~50 words, then boundary-capped
    as a backstop. Brochure OCR layout noise (reversed words, exploded
    vertical labels, glued sheet headers) is dropped; fills that are mostly
    garbage go blank. Sentence prose without amenity separators is left for
    ``cap_prose_field``.
    """
    if text is None:
        return ""
    text = " ".join(str(text).split()).strip()
    if not text:
        return ""

    if force_amenity_list or _looks_like_amenity_dump(text) or looks_like_ocr_layout_noise(text):
        cleaned = _amenity_list_from_text(text)
        return cap_prose_field(cleaned, max_words=max_words)

    return cap_prose_field(text, max_words=max_words)


def clean_special_features(text, *, force_amenity_list=False):
    """Clean Special Features into short amenity/spec blurbs (Kitt style)."""
    return clean_prose_or_amenity_field(
        text, force_amenity_list=force_amenity_list, max_words=SPECIAL_FEATURES_MAX_WORDS
    )


def extract_state_of_space_status(text):
    """Return a compact fit-out/availability status if one appears in *text*.

    Normalizes common phrases to Kitt template wording when recognized.
    """
    text = "" if text is None else str(text).strip()
    if not text:
        return ""
    match = _STATE_OF_SPACE_STATUS_RE.search(text)
    if not match:
        return ""
    raw = " ".join(match.group(0).split())
    lower = raw.lower()
    if re.search(r"fully\s+fitted", lower):
        return "Fully Fitted"
    if re.search(r"partially\s+fitted", lower):
        return "Partially Fitted"
    if "underway" in lower:
        return "Fitout Underway"
    if re.search(r"cat\s*a", lower):
        if "custom" in lower or "opportunity" in lower:
            return "CAT A - Custom Fit Out Opportunity"
        if re.search(r"fitted|fit\s*out", lower):
            return "CAT A"
        return "CAT A"
    if re.search(r"cat\s*b", lower):
        if "custom" in lower or "opportunity" in lower:
            return "CAT B - Custom Fit Out Opportunity"
        if re.search(r"fitted|fit\s*out", lower):
            return "CAT B"
        return "CAT B"
    if re.search(r"plug\s*(?:and|&)\s*play", lower):
        return "Plug and Play"
    if re.search(r"available\s+now", lower):
        return "Available now"
    if re.search(r"available\s+immediately|immediate(?:\s+availability)?", lower):
        return "Immediate"
    if re.search(r"vacant\s+possession", lower):
        return "Vacant possession"
    if re.search(r"coming\s+soon", lower):
        return "Coming soon"
    if re.search(r"under\s+offer", lower):
        return "Under offer"
    return raw[0].upper() + raw[1:] if raw else ""


def clean_state_of_space(text, *, force_amenity_list=False):
    """Clean State of Space into a short availability/condition note.

    Prefers compact status tags (Fully Fitted, Fitout Underway, CAT A …)
    over amenity-list essays. OCR noise and long brochure dumps without a
    status phrase are blanked rather than shipped as amenity lists. Short
    primary notes pass through; remaining prose is soft-capped (~250 words)
    on a complete sentence boundary.
    """
    if text is None:
        return ""
    text = " ".join(str(text).split()).strip()
    if not text:
        return ""

    # Multi-column / vertical OCR is not salvageable into a status tag.
    if looks_like_ocr_layout_noise(text):
        return ""

    status = extract_state_of_space_status(text)
    messy = force_amenity_list or _looks_like_amenity_dump(text)
    if messy:
        # Status tag wins; otherwise do not dump amenities into this column.
        return status
    # Short notes (Immediate, Cat A, Fully Fitted) and light condition prose.
    return cap_prose_field(text, max_words=STATE_OF_SPACE_MAX_WORDS)


def _looks_like_amenity_dump(text):
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) < 3:
        return False
    junk = sum(1 for p in parts if _is_junk_amenity_part(p))
    if junk >= 2:
        return True
    # Many short semicolon segments → amenity-style dump, not prose.
    avg = sum(len(p.split()) for p in parts) / len(parts)
    return avg <= 15 and len(parts) >= 4


def _fix_mid_phrase_semicolons(text):
    text = _CONJ_SEMI_RE.sub(r"\1 ", text)
    text = _LOWER_CONT_SEMI_RE.sub(" ", text)
    return text


def _amenity_list_from_text(text):
    # Multi-column / vertical PDF OCR is not recoverable into amenities.
    if looks_like_ocr_layout_noise(text):
        return ""
    text = _fix_mid_phrase_semicolons(text)
    raw_parts = []
    for chunk in re.split(r"[;\n•·]+", text):
        chunk = _TRAILING_PUNCT_RE.sub("", chunk.strip()).strip()
        if chunk:
            raw_parts.append(chunk)

    # Drop exploded vertical-label runs (single letters/digits in a row).
    raw_parts = _drop_single_char_runs(raw_parts)

    items = []
    seen = set()
    word_count = 0
    for part in raw_parts:
        pieces = _expand_glued_amenity_part(part)
        if not pieces:
            continue
        for piece in pieces:
            piece = _normalize_amenity_part(piece)
            if not piece or _is_junk_amenity_part(piece):
                continue
            key = re.sub(r"\W+", " ", piece.lower()).strip()
            if not key or key in seen:
                continue
            part_words = piece.split()
            if word_count + len(part_words) > SPECIAL_FEATURES_AMENITY_MAX_WORDS and items:
                break
            seen.add(key)
            items.append(piece)
            word_count += len(part_words)
            if len(items) >= SPECIAL_FEATURES_AMENITY_MAX_ITEMS:
                break
        if len(items) >= SPECIAL_FEATURES_AMENITY_MAX_ITEMS:
            break
    return "; ".join(items)


def _drop_single_char_runs(parts):
    """Remove consecutive single-character OCR fragments (vertical labels)."""
    kept = []
    i = 0
    while i < len(parts):
        if _is_single_char_part(parts[i]):
            j = i
            while j < len(parts) and _is_single_char_part(parts[j]):
                j += 1
            # Prefer drop; never reconstruct vertical labels into amenities.
            if j - i >= 2:
                i = j
                continue
            # Lone single-char part is still junk; skip it.
            i = j
            continue
        kept.append(parts[i])
        i += 1
    return kept


def _is_single_char_part(part):
    s = _TRAILING_PUNCT_RE.sub("", (part or "").strip()).strip()
    if not s:
        return True
    # One letter/digit, optionally with trivial punctuation.
    return bool(re.fullmatch(r"[A-Za-z0-9]", s))


def _expand_glued_amenity_part(part):
    """Split camelCase / digit glue; drop sheet-header fragments."""
    s = _TRAILING_PUNCT_RE.sub("", (part or "").strip()).strip()
    if not s:
        return []
    # Reject reversed OCR tokens before camel-splitting ("noinU" → noin+U).
    if _is_reversed_english_token(s) or (
        " " not in s and _is_junk_token(s) and not _looks_like_real_amenity_phrase(s)
    ):
        return []

    tokens = s.split()
    needs_unglue = any(
        re.search(r"[a-z][A-Z]|\d[A-Za-z]|[A-Za-z]\d", tok)
        or _is_compact_sheet_header(tok)
        or re.fullmatch(r"\d+desks?", tok, re.I)
        for tok in tokens
    )
    if not needs_unglue:
        if _is_sheet_header_part(s):
            return []
        return [s]

    kept_words = []
    for raw in tokens:
        if _is_reversed_english_token(raw) or _is_compact_sheet_header(raw) or _is_sheet_header_part(raw):
            continue
        if re.fullmatch(r"\d+desks?", raw, re.I):
            continue
        # "noinU" style: lowercase stem + single capital — check reverse on full token.
        if re.fullmatch(r"[a-z]{3,}[A-Z]", raw) and _is_reversed_english_token(raw):
            continue
        pieces = _CAMEL_BOUNDARY_RE.sub(" ", raw).split() or [raw]
        spaced = " ".join(pieces)
        if _is_sheet_header_part(spaced) or _is_header_word_run([p.lower() for p in pieces]):
            continue
        if re.fullmatch(r"\d+\s*desks?", spaced, re.I):
            continue
        kept_words.extend(pieces if len(pieces) > 1 else [raw])

    if not kept_words:
        return []
    joined = " ".join(kept_words).strip()
    if not joined or _is_sheet_header_part(joined):
        return []
    return [joined]


def _is_compact_sheet_header(token):
    compact = re.sub(r"[^a-z0-9]+", "", (token or "").lower())
    return compact in {
        "currentfloortype",
        "currentspec",
        "squarefootage",
        "floorsize",
        "sizesqft",
        "minimumterm",
        "monthlyrate",
        "pricepsqft",
        "pricepsqf",
        "desksmax",
        "marketingprice",
        "floorunit",
    }


def _is_header_word_run(pieces):
    header_words = {
        "current",
        "floor",
        "type",
        "spec",
        "square",
        "footage",
        "size",
        "minimum",
        "term",
        "monthly",
        "rate",
        "price",
        "desks",
        "desk",
        "marketing",
        "unit",
    }
    lowered = [p.lower() for p in pieces]
    return len(lowered) >= 2 and all(w in header_words for w in lowered)


def _looks_like_real_amenity_phrase(text):
    """True for multi-word fitted/amenity phrases rather than column headers."""
    words = [w for w in re.findall(r"[A-Za-z]+", text.lower()) if w]
    if len(words) >= 3:
        return True
    amenity_cues = {
        "fitted",
        "furnished",
        "agile",
        "workstations",
        "stations",
        "kitchen",
        "kitchenette",
        "terrace",
        "shower",
        "showers",
        "bike",
        "cycle",
        "storage",
        "meeting",
        "reception",
        "conditioning",
        "lighting",
        "fibre",
        "fiber",
        "wifi",
        "hvac",
        "collab",
        "breakout",
        "lounge",
    }
    return any(w in amenity_cues or w.rstrip("s") in amenity_cues for w in words)


def _normalize_amenity_part(part):
    part = _TRAILING_PUNCT_RE.sub("", part.strip()).strip()
    part = re.sub(r"\s+", " ", part)
    if not part:
        return ""
    letters = [c for c in part if c.isalpha()]
    if letters and len(letters) >= 4 and all(c.isupper() for c in letters):
        part = part[0].upper() + part[1:].lower()
    return part


def _is_junk_amenity_part(part):
    s = _TRAILING_PUNCT_RE.sub("", (part or "").strip()).strip()
    if not s:
        return True
    if _PAGE_JUNK_RE.match(s):
        return True
    if _is_sheet_header_part(s):
        return True
    if re.fullmatch(r"[\d\s./\-]+", s):
        return True
    tokens = re.findall(r"[A-Za-z0-9&]+", s)
    if not tokens:
        return True
    if all(_is_junk_token(t) for t in tokens):
        return True
    # Reversed / consonant-garbage tokens dominate the part.
    garbage = sum(
        1 for t in tokens if _is_reversed_english_token(t) or _is_consonant_garbage_token(t)
    )
    if garbage and garbage >= max(1, (len(tokens) + 1) // 2):
        return True
    return False


def _is_sheet_header_part(part):
    s = re.sub(r"\s+", " ", (part or "").strip())
    if not s:
        return False
    if _is_compact_sheet_header(s):
        return True
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", s)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    if _SHEET_HEADER_RE.match(spaced) or _SHEET_HEADER_RE.match(s):
        return True
    words = [w for w in re.findall(r"[A-Za-z]+", spaced.lower()) if w]
    if words and _is_header_word_run(words) and not _looks_like_real_amenity_phrase(spaced):
        return True
    return False


def _is_junk_token(token):
    t = token.lower()
    if t in _KEEP_SHORT_TOKENS:
        return False
    if t.isdigit():
        return True
    if _is_code_like_token(token):
        return True
    if _is_reversed_english_token(token):
        return True
    if _is_consonant_garbage_token(token):
        return True
    if t.isalpha() and len(t) <= 2:
        return True
    # Lone 3-letter ALL-CAPS stubs from PDF wraps (e.g. "ING"), not acronyms.
    if token.isalpha() and token.isupper() and len(token) == 3 and t not in _KEEP_SHORT_TOKENS:
        return True
    return False


def _is_reversed_english_token(token):
    """True when the token looks like a reversed common English word."""
    t = re.sub(r"[^a-z]", "", (token or "").lower())
    if len(t) < 4:
        return False
    if t in _COMMON_ENGLISH_WORDS:
        return False
    reversed_t = t[::-1]
    if reversed_t in _COMMON_ENGLISH_WORDS:
        return True
    # Title-ish reverse of a common word ("noinU" → union).
    return False


def _is_consonant_garbage_token(token):
    """High-consonant alpha junk that is not a known short amenity token."""
    t = re.sub(r"[^a-z]", "", (token or "").lower())
    if len(t) < 4 or t in _KEEP_SHORT_TOKENS or t in _COMMON_ENGLISH_WORDS:
        return False
    if t[::-1] in _COMMON_ENGLISH_WORDS:
        return True
    vowels = sum(1 for c in t if c in "aeiou")
    if vowels == 0:
        return True
    if vowels / len(t) <= 0.2 and len(t) >= 5:
        return True
    # Odd consonant sandwich unlikely in English amenities (e.g. "ecaps" already
    # caught as reverse; this catches leftovers like "thguo").
    if re.search(r"[bcdfghjklmnpqrstvwxyz]{4,}", t) and vowels / len(t) < 0.35:
        return True
    return False


def _is_code_like_token(token):
    """Pure numbers / short alnum codes (dr3, 624) unless clearly EPC-related."""
    t = (token or "").strip()
    if not t:
        return False
    lower = t.lower()
    if lower in _KEEP_SHORT_TOKENS:
        return False
    # EPC ratings are kept when attached to EPC (handled as multi-token parts).
    if lower in {"epc"} or re.fullmatch(r"epc[a-g]", lower):
        return False
    if t.isdigit():
        return True
    # Short mixed codes only (dr3, a12, 12b) — not word+digit stems like one0.
    if re.fullmatch(r"[a-z]{1,2}\d{1,3}", lower) or re.fullmatch(r"\d{1,3}[a-z]{1,2}", lower):
        return True
    return False
