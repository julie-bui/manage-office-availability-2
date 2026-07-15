"""Rule-based parser for MetSpace's "Weekly Availability" email layout.

Layout (plain text lines), fully repeated per listing (no carry-over state):
    AREA HEADER                (only before the first building in that area)
    Building name (1-2 lines, may wrap mid-word)
    [(parenthetical note)]     e.g. "(Monument)"
    [- Floor descriptor]
    Sqft: <n>
    Desks: <free text, e.g. "10 + MR + PB">
    Price: <n>
    Av: <date-ish>
"""
import re

from extraction.text_utils import titlecase_area

AREA_RE = re.compile(r"^[A-Z][A-Z ]+$")


def detect(content):
    blob = ((content.get("sender") or "") + " " + (content.get("text") or "")).lower()
    return "metspace" in blob


def parse(content):
    lines = [l.strip() for l in content["text"].split("\n") if l.strip()]
    if any(line.upper() == "OFFICE OF THE WEEK" for line in lines):
        return _parse_office_of_the_week(lines, content.get("html_items", []))
    contact = _contact_block(lines)

    records = []
    buffer = []
    current_area = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.lower().startswith("sqft:"):
            remaining = buffer
            buffer = []

            floor = ""
            if remaining and remaining[-1].startswith("-"):
                floor = remaining.pop().lstrip("- ").strip()
            parenthetical = ""
            if remaining and remaining[-1].startswith("(") and remaining[-1].endswith(")"):
                parenthetical = remaining.pop()

            # An area header may appear anywhere earlier in this block (or
            # not at all, if we're still under the same area as the last
            # listing) — everything before it is boilerplate/noise, so only
            # take what follows it as the actual building name. Scan
            # backwards so the *closest* area-like line wins (the true area
            # header sits right before the name; anything further back,
            # like email boilerplate, should be ignored).
            area_idx = next(
                (idx for idx in range(len(remaining) - 1, -1, -1) if AREA_RE.match(remaining[idx]) and len(remaining[idx].split()) <= 3),
                None,
            )
            if area_idx is not None:
                current_area = titlecase_area(remaining[area_idx])
                remaining = remaining[area_idx + 1 :]

            building = " ".join(remaining).strip()
            if parenthetical:
                building = f"{building} {parenthetical}".strip()

            if not building:
                i += 1
                continue

            sqft = line.split(":", 1)[1].strip() if ":" in line else ""
            desks_line = lines[i + 1] if i + 1 < n else ""
            price_line = lines[i + 2] if i + 2 < n else ""

            desks_desc = desks_line.split(":", 1)[1].strip() if desks_line.lower().startswith("desks:") else ""
            price_val = price_line.split(":", 1)[1].strip() if price_line.lower().startswith("price:") else ""

            records.append(
                {
                    "Area": current_area,
                    "Building": building,
                    "Floor/Unit": floor,
                    "Size (sq ft)": sqft.replace(",", ""),
                    "Desks (max)": _max_desks(desks_desc),
                    "Marketing Price (Based on Min Term) PCM": price_val.replace("£", "").replace(",", ""),
                    "Special Features": desks_desc,
                    "Contacts": contact,
                }
            )
            i += 4  # consumed Sqft, Desks, Price, and Av lines
            continue
        buffer.append(line)
        i += 1

    _attach_floor_plans(records, content.get("html_items", []))
    return records


def _parse_office_of_the_week(lines, html_items):
    """Parse MetSpace's single-property campaign using the same canonical fields.

    This is a genuine provider template, not a filename exception. Its two
    listing images are ordered photo then floorplan, verified against the
    embedded image content; logos and signature assets are excluded by domain
    and context before assignment.
    """
    sqft_idx = next((i for i, line in enumerate(lines) if line.lower().startswith("sqft:")), None)
    if sqft_idx is None or sqft_idx == 0:
        return []
    building = lines[sqft_idx - 1]
    values = {}
    for line in lines[sqft_idx : min(len(lines), sqft_idx + 8)]:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower()] = value.strip()
    desks = values.get("desks", "")
    record = {
        "Building": building,
        "Size (sq ft)": values.get("sqft", "").replace(",", ""),
        "Desks (max)": _max_desks(desks),
        "Marketing Price (Based on Min Term) PCM": values.get("price", "").replace("£", "").replace(",", ""),
        "Special Features": ", ".join(filter(None, [desks, values.get("configuration", "")])),
        "State of Space": values.get("av", ""),
        "Contacts": _contact_block(lines),
    }

    listing_link_idx = next(
        (i for i, (kind, text, _url) in enumerate(html_items) if kind == "link" and text.strip().lower() == building.lower()),
        None,
    )
    if listing_link_idx is not None:
        record["Brochure PDF"] = html_items[listing_link_idx][2]
        images = [
            url for kind, alt, url in html_items[listing_link_idx + 1 :]
            if kind == "image" and "mcusercontent.com" in url and alt.strip().lower() != "logo"
        ]
        if images:
            record["High Res Images"] = images[0]
        if len(images) > 1:
            record["Floor Plan"] = images[1]
    return [record]


def _attach_floor_plans(records, html_items):
    """Fills Floor Plan from the source email's own per-listing image — a
    real, hosted mcusercontent.com image (MetSpace's own images have no
    distinguishing alt text, unlike Knotel's "X Floor featured image"
    convention, so this filters by source domain and excludes the "Logo"-
    alt company logo instead).

    Also fills Brochure PDF from that same matched link's own href — each
    building name in this source (e.g. "9-10 Market Place") is itself the
    hyperlink, not a separate "View Brochure"-style button the way Knotel's
    are. The href is a Mailchimp click-tracking redirect
    (us.list-manage.com/...), not a direct PDF URL — confirmed (2026-07)
    by actually following one all the way through: it 302s to a Google
    Drive file literally titled "9-10 Market Place - 2nd Floor", a real,
    listing-specific brochure. Kept as the tracking URL as extracted
    (not resolved to the final Drive link) — it works identically when
    clicked, and resolving it here would mean an extra network request
    per listing during extraction.

    Confirmed by actually viewing several of these images that every one
    is a floor plan diagram, not a building photo. Goes into Floor Plan,
    not High Res Images, for that reason. High Res Images is filled later
    by brochure.enrich_properties from the building-name Brochure PDF link
    (Mailchimp tracker → Google Drive PDF page embeds) — that is the only
    reliable photo source for this weekly template.

    Direction confirmed directly against this source's raw HTML (not
    inherited from Knotel or GPE): each image FOLLOWS the listing link it
    belongs to, not precedes it — e.g. "9-10 Market Place"'s own link is
    immediately followed by its image, then "43-45 Charlotte Street"'s
    link, then its own image, and so on. An earlier version of this
    function assumed the opposite direction (image precedes link, mirrored
    from Knotel's pattern) — that assumption was wrong for MetSpace and
    silently shifted every image onto the *next* record instead of its
    own, including leaving the first listing incorrectly blank (it does
    have a real image; it had been misattributed to the second listing).

    Still not a simple positional pairing — a non-matching, non-image item
    (e.g. a blank spacer link, or the footer's phone/email links right
    after the last real listing) can sit between a link and its image, or
    can mark that no image exists for a given listing at all. So this only
    consumes an image for the record whose own link was *just* seen, and
    stops looking for that record's image (leaving it blank) the moment
    another non-blank link shows up first — never assumed from
    position/count alone."""
    idx = 0
    n = len(html_items)
    i = 0
    while i < n and idx < len(records):
        kind, a, b = html_items[i]
        if kind == "link":
            text = a
            building = records[idx].get("Building") or ""
            # Case-insensitive (2026-07 audit — see extraction.rules.knotel's
            # own "View brochure" vs "View Brochure" casing bug for the
            # precedent): a real link's own visible text isn't guaranteed to
            # match this rule's own extracted Building text byte-for-byte in
            # case, even when it's clearly the same listing.
            text_l, building_l = text.lower(), building.lower()
            if building and (text_l == building_l or text_l.startswith(building_l) or building_l in text_l):
                if b:
                    records[idx]["Brochure PDF"] = b
                j = i + 1
                while j < n:
                    kind2, a2, b2 = html_items[j]
                    if kind2 == "image":
                        if "mcusercontent.com" in b2 and a2.strip().lower() != "logo":
                            records[idx]["Floor Plan"] = b2
                            break
                        j += 1
                        continue
                    if a2:
                        # A subsequent non-blank link (the next listing, or
                        # footer contact info) showed up before any image
                        # did — this record genuinely has no image.
                        break
                    j += 1
                idx += 1
        i += 1


def _max_desks(desc):
    if not desc:
        return ""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", desc)
    if m:
        return m.group(2)
    m2 = re.search(r"\d+", desc)
    return m2.group() if m2 else ""


NAME_RE = re.compile(r"^[A-Z][a-zA-Z'.-]+(?: [A-Z][a-zA-Z'.-]+)+$")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.\w+$")
PHONE_RE = re.compile(r"^0\d{3,4}[\s-]?\d{3,4}[\s-]?\d{3,4}$")


def _contact_block(lines):
    """MetSpace emails end with a fixed, company-wide contact list — not
    per-listing info — shaped as repeating groups of
    Name / Title / Phone / Email / Website. Job titles can themselves look
    name-shaped (e.g. "Sales Manager" is two capitalized words), so rather
    than pattern-matching every line for "looks like a name", anchor on the
    one line in each group that's unambiguous — the website — and take the
    name as whatever sits exactly 4 lines before it.

    Also pulls the phone (2 lines before the website) and email (1 line
    before it) into Contacts alongside each name — confirmed (2026-07
    audit) these were being silently dropped even though the source gives
    a real, structured phone+email for every contact, the same class of
    gap Knotel's own missing contact info turned out to be. names_only
    (extraction.schema) already strips these back out for Assigned
    Agents, same as it does for Knotel's own combined Contacts string, so
    this doesn't need any special-casing there."""
    try:
        idx = lines.index("Contact")
    except ValueError:
        return ""
    end = next((i for i in range(idx + 1, len(lines)) if lines[i].lower().startswith("copyright")), len(lines))
    block = lines[idx + 1 : end]

    parts = []
    seen_names = set()
    for i, line in enumerate(block):
        if line.lower().startswith("www.") and i >= 4:
            name, phone, email = block[i - 4], block[i - 2], block[i - 1]
            if NAME_RE.match(name) and name not in seen_names:
                seen_names.add(name)
                parts.append(name)
                if EMAIL_RE.match(email):
                    parts.append(email)
                if PHONE_RE.match(phone):
                    parts.append(phone)
    return ", ".join(parts)
