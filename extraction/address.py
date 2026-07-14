"""Helpers for deriving a Kato-compatible postcode from freeform address text,
and for normalizing spelled-out building numbers for geocoding."""
import re

UK_POSTCODE_RE = re.compile(r"\b([A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2})\b")


def extract_postcode(text):
    """Return a normalized UK postcode (e.g. "EC3A 7JB") found in `text`, or
    "" if none is confidently present. Never guesses — a near-miss is left
    blank rather than returned as a best effort."""
    if not text:
        return ""
    match = UK_POSTCODE_RE.search(text)
    if not match:
        return ""
    compact = match.group(1).upper().replace(" ", "")
    if len(compact) < 5:
        return ""
    return f"{compact[:-3]} {compact[-3:]}"


_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORD_RE = re.compile(
    r"^(?:"
    r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"
    r")\b",
    re.IGNORECASE,
)


def spelled_number_to_digits(text):
    """If `text` starts with a spelled-out number (e.g. "Thirty One Alfred
    Place"), return it with that leading phrase replaced by digits (e.g.
    "31 Alfred Place") — a fallback for geocoders like Nominatim, which
    can't match a building number spelled out in words. Returns None if
    `text` doesn't start with a recognized number word (one through
    ninety-nine) — never guesses at numbers elsewhere in the string."""
    if not text:
        return None
    stripped = text.strip()
    match = _NUMBER_WORD_RE.match(stripped)
    if not match:
        return None

    words = re.split(r"[\s-]+", match.group(0).strip().lower())
    total = 0
    for word in words:
        total += _TENS.get(word, _ONES.get(word, 0))
    if not total:
        return None

    rest = stripped[match.end():].lstrip()
    return f"{total} {rest}".strip()
