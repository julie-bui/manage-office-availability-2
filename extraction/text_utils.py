import re

# Soft target for long brochure amenity/description dumps in Special Features.
# Keep short notes (price drops, "Fitted", etc.) untouched; only cap when the
# text exceeds this word count, always ending on a complete sentence when one
# exists at or before the limit.
SPECIAL_FEATURES_MAX_WORDS = 250

_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s+|$)")


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
