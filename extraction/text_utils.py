import re

# Soft target for long brochure amenity/description dumps in Special Features.
# Keep short notes (price drops, "Fitted", etc.) untouched; only cap when the
# text exceeds this word count, always ending on a complete sentence when one
# exists at or before the limit.
SPECIAL_FEATURES_MAX_WORDS = 250

# Amenity-list target when cleaning brochure / semicolon dumps.
SPECIAL_FEATURES_AMENITY_MAX_ITEMS = 12
SPECIAL_FEATURES_AMENITY_MAX_WORDS = 80
# Primary sheet notes at or under this word count are preferred over brochure essays.
SPECIAL_FEATURES_SHORT_MAX_WORDS = 40

_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s+|$)")

# Semicolon after a conjunction/preposition → mid-phrase PDF break.
_CONJ_SEMI_RE = re.compile(
    r"\b(and|or|with|to|for|of|in|on|at|by|from|into|over|under|the|a|an)\s*;\s*",
    re.IGNORECASE,
)
# Lowercase continuation across a semicolon: "bright; efficient".
_LOWER_CONT_SEMI_RE = re.compile(r"(?<=[a-z])\s*;\s*(?=[a-z])")
_TRAILING_PUNCT_RE = re.compile(r"[,.;:!?·•]+$")
_PAGE_JUNK_RE = re.compile(
    r"(?i)^(page\s*\d+|continued|cont\.?|see over|www\.|http\S+)$"
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


def cap_special_features(text, max_words=SPECIAL_FEATURES_MAX_WORDS):
    """Cap Special Features to about *max_words*, ending on a complete sentence.

    Short values pass through unchanged. For long brochure amenity/description
    dumps, prefer the last sentence boundary at or before *max_words*. If no
    sentence ends under the limit, fall back to a soft word cap with an
    ellipsis (never mid-word).
    """
    if text is None:
        return ""
    text = str(text).strip()
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return text

    best_end = None
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if len(text[:end].split()) <= max_words:
            best_end = end
        else:
            break

    if best_end is not None:
        return text[:best_end].rstrip()

    # No sentence boundary under the limit — soft word cut, never mid-word.
    capped = " ".join(words[:max_words]).rstrip(".,;:!? ")
    return f"{capped}..."


def is_useful_primary_special_features(text):
    """True when a short primary/source-sheet value should win over brochure essays."""
    text = "" if text is None else str(text).strip()
    if not text:
        return False
    return len(text.split()) <= SPECIAL_FEATURES_SHORT_MAX_WORDS


def looks_like_long_or_messy_features(text):
    """True for brochure essays / amenity dumps that should not replace a short primary."""
    text = "" if text is None else str(text).strip()
    if not text:
        return False
    if len(text.split()) > SPECIAL_FEATURES_SHORT_MAX_WORDS:
        return True
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) > SPECIAL_FEATURES_AMENITY_MAX_ITEMS:
        return True
    return _looks_like_amenity_dump(text)


def clean_special_features(text, *, force_amenity_list=False):
    """Clean Special Features into a short `; `-joined amenity list when messy.

    Short useful notes (price drops, desk extras, "Fitted") pass through.
    Semicolon amenity dumps and forced brochure fills are artifact-cleaned,
    truncated to ~8–12 items / ~40–80 words, then sentence-capped as a backstop.
    Sentence prose without amenity separators is left for ``cap_special_features``.
    """
    if text is None:
        return ""
    text = " ".join(str(text).split()).strip()
    if not text:
        return ""

    if force_amenity_list or _looks_like_amenity_dump(text):
        cleaned = _amenity_list_from_text(text)
        return cap_special_features(cleaned)

    return cap_special_features(text)


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
    text = _fix_mid_phrase_semicolons(text)
    raw_parts = []
    for chunk in re.split(r"[;\n•·]+", text):
        chunk = _TRAILING_PUNCT_RE.sub("", chunk.strip()).strip()
        if chunk:
            raw_parts.append(chunk)

    items = []
    seen = set()
    word_count = 0
    for part in raw_parts:
        part = _normalize_amenity_part(part)
        if not part or _is_junk_amenity_part(part):
            continue
        key = re.sub(r"\W+", " ", part.lower()).strip()
        if not key or key in seen:
            continue
        part_words = part.split()
        if word_count + len(part_words) > SPECIAL_FEATURES_AMENITY_MAX_WORDS and items:
            break
        seen.add(key)
        items.append(part)
        word_count += len(part_words)
        if len(items) >= SPECIAL_FEATURES_AMENITY_MAX_ITEMS:
            break
    return "; ".join(items)


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
    if re.fullmatch(r"[\d\s./\-]+", s):
        return True
    tokens = re.findall(r"[A-Za-z0-9&]+", s)
    if not tokens:
        return True
    if all(_is_junk_token(t) for t in tokens):
        return True
    return False


def _is_junk_token(token):
    t = token.lower()
    if t in _KEEP_SHORT_TOKENS:
        return False
    if t.isdigit():
        return True
    if t.isalpha() and len(t) <= 2:
        return True
    # Lone 3-letter ALL-CAPS stubs from PDF wraps (e.g. "ING"), not acronyms.
    if token.isalpha() and token.isupper() and len(token) == 3 and t not in _KEEP_SHORT_TOKENS:
        return True
    return False
