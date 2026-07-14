"""Rule-based parser for Breezblok's single-listing marketing brochure PDF
(e.g. "John Stow House.pdf") — a multi-page document describing ONE
building, with a repeating footer block (Building name/Street/"London"/
Postcode) on most pages and one or more "Proposed space" sections giving
the actual floor/unit details.

Floor Plan/High Res Images aren't handled here — same as the LLM fallback
this rule replaces, those come from app.py's extraction.pdf_images
enrichment (position-based real image extraction), which runs for this
rule too (see app.py's PDF_IMAGE_ENRICHED_METHODS / _attach_pdf_images
call-site) — never invented from this brochure's own text, since a real
BC brochure once had the LLM copy a plain "Example Floorplan" heading
into Floor Plan as an unclickable placeholder; this rule can't repeat
that mistake because it never touches those two fields at all."""
import re

# The repeating footer block seen on most pages, e.g.:
#   John Stow House
#   18 Bevis Marks
#   London
#   EC3A 7JB
# Building name (any capitalized line), then a street line (starts with a
# house number), then the literal "London", then a full UK postcode.
_BUILDING_BLOCK_RE = re.compile(
    r"^([A-Z][A-Za-z0-9 '&\-]+?)\n"
    r"(\d[A-Za-z0-9 '&\-]+)\n"
    r"London\n"
    r"([A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2})",
    re.MULTILINE,
)

_CONTACT_RE = re.compile(r"^Contact:?\s+(.+)$", re.IGNORECASE | re.MULTILINE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s]{7,}\d")

# One listing's own details, e.g.:
#   Proposed space
#   Office 302- 1750 sqft
#   32 Desks and two meeting rooms
#   £18,000 per month + VAT
#   Deposit required to secure the office £36,000
_UNIT_SIZE_RE = re.compile(r"^(.+?)\s*-\s*([\d,]+)\s*sq\s*ft", re.IGNORECASE)
_DESKS_RE = re.compile(r"(\d+)\s*desks?", re.IGNORECASE)
_PRICE_RE = re.compile(r"£?\s*([\d,]+)\s*per\s*month", re.IGNORECASE)
_VAT_RE = re.compile(r"\+\s*VAT", re.IGNORECASE)
_DEPOSIT_RE = re.compile(r"deposit.*", re.IGNORECASE)


def detect(content):
    return "breezblok" in (content.get("text") or "").lower()


def parse(content):
    text = content.get("text") or ""
    building = _building(text)
    if not building:
        return []
    contact = _contact(text)

    lines = text.split("\n")
    records = []
    for i, line in enumerate(lines):
        if line.strip().lower() != "proposed space":
            continue
        block = lines[i + 1 : i + 5]
        record = _parse_unit_block(block)
        if not record:
            continue
        record["Building"] = building
        record["Contacts"] = contact
        records.append(record)
    return records


def _building(text):
    m = _BUILDING_BLOCK_RE.search(text)
    if not m:
        return ""
    name, street, postcode = (g.strip() for g in m.groups())
    return f"{name}, {street}, London {postcode}"


def _contact(text):
    """Returns whatever this brochure's own "Contact: <...>" line says
    (e.g. "Sales") — not just an individual's name. Carries over the same
    fallback the LLM prompt was given for Crown Estate's sole-agent case:
    when a source names no individual person but does give a company/
    team acting as the contact, use that instead of leaving the field
    blank. Here that isn't a fallback branch at all — this always takes
    whatever the source's own Contact line says, whether that's a
    person's name or a team label like "Sales", so the same outcome (a
    real value, not blank, whenever the source has ANY contact signal)
    falls out directly rather than needing a special case. Returns ""
    only when the source has no "Contact:" line at all.

    Also pulls the real email/phone from the two lines immediately
    following (confirmed real: "Contact: Sales" / "Sales@breezblok.
    london" / "Telephone: +44 7500665267") — previously silently
    dropped even though they sit right there in the source, the same
    class of gap Knotel's own missing contact info turned out to be.
    names_only (extraction.schema) already strips these back out for
    Assigned Agents, same as it does for Knotel's own combined Contacts
    string, so this doesn't need any special-casing there."""
    m = _CONTACT_RE.search(text)
    if not m:
        return ""
    parts = [m.group(1).strip()]
    for line in text[m.end() :].split("\n", 3)[1:3]:
        email_match = _EMAIL_RE.search(line)
        if email_match:
            parts.append(email_match.group())
        phone_match = _PHONE_RE.search(line)
        if phone_match:
            parts.append(phone_match.group().strip())
    return ", ".join(parts)


def _parse_unit_block(lines):
    if not lines:
        return None
    unit_line = lines[0].strip() if len(lines) > 0 else ""
    desks_line = lines[1].strip() if len(lines) > 1 else ""
    price_line = lines[2].strip() if len(lines) > 2 else ""
    deposit_line = lines[3].strip() if len(lines) > 3 else ""

    size_match = _UNIT_SIZE_RE.match(unit_line)
    if not size_match:
        return None
    unit = size_match.group(1).strip()
    size = size_match.group(2).replace(",", "")

    desks_match = _DESKS_RE.search(desks_line)
    desks = desks_match.group(1) if desks_match else ""

    price_match = _PRICE_RE.search(price_line)
    price = price_match.group(1).replace(",", "") if price_match else ""

    features = [desks_line] if desks_line else []
    if _VAT_RE.search(price_line):
        features.append("Price excludes VAT")
    deposit_match = _DEPOSIT_RE.search(deposit_line)
    if deposit_match:
        features.append(deposit_match.group(0))

    return {
        "Floor/Unit": unit,
        "Size (sq ft)": size,
        "Desks (max)": desks,
        "Marketing Price (Based on Min Term) PCM": price,
        "Special Features": ". ".join(features),
    }
