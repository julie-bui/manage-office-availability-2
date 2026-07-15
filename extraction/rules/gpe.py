"""Rule-based parser for GPE's "Fully Managed Availability" email layout.

Layout (plain text lines):
    AREA HEADER (all caps, e.g. "SOHO", "ST JAMES'S")
    Building name
    Description sentence(s)
    - bullet feature
    - bullet feature ...
    Available
    Desk space
    Sq ft
    Price (psf)
    [optional promo note line]
    <floor label>          \
    <desk range> desks      | repeated once per floor/unit in the building
    <sqft>                  |
    <£psf>                 /
    ... (next building, or next area, starts here)

A building can have several floor rows in a row; PCM isn't given directly
here, only PSF — schema.normalize_record derives PCM from PSF * size / 12.
"""
import re

from bs4 import BeautifulSoup, NavigableString, Tag

from extraction.text_utils import titlecase_area

AREA_RE = re.compile(r"^[A-Z][A-Z '\"]+$")
DESKS_LINE_RE = re.compile(r"\bdesks?\b", re.IGNORECASE)
SQFT_LINE_RE = re.compile(r"^[\d,]+(\s*-\s*[\d,]+)?$")
PSF_LINE_RE = re.compile(r"^£[\d,.]+(\s*-\s*£?[\d,.]+)?$")
MARKER = ("Available", "Desk space", "Sq ft", "Price (psf)")
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'.-]+(?: [A-Z][a-zA-Z'.-]+)+$")


def detect(content):
    blob = ((content.get("sender") or "") + " " + (content.get("text") or "")).lower()
    if "gpe.co.uk" in blob or "fully managed" in blob:
        return True
    return False


def parse(content):
    lines = _clean_lines(content["text"])
    # Everything before "CURRENT AVAILABILITY" is marketing/news content
    # (with its own ALL-CAPS-looking headings that would otherwise be
    # mistaken for area names) — drop it.
    try:
        lines = lines[lines.index("CURRENT AVAILABILITY") + 1 :]
    except ValueError:
        pass
    contact = _contact_block(lines)
    records = []
    current_area = ""
    buffer = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i : i + 4] == list(MARKER):
            header = buffer
            buffer = []
            # An area header, when present, sits right before the building
            # name — scan backwards so it wins over any earlier noise (CTA
            # button labels, or for the very first block, marketing copy).
            area_idx = next(
                (idx for idx in range(len(header) - 1, -1, -1) if AREA_RE.match(header[idx]) and len(header[idx].split()) <= 4),
                None,
            )
            if area_idx is not None:
                current_area = titlecase_area(header[area_idx])
                header = header[area_idx + 1 :]
            building = header[0] if header else ""
            features = "; ".join(l.lstrip("- ").strip() for l in header[1:] if l.startswith("-"))

            j = i + 4
            skipped_notes = 0
            found_any = False
            while j + 3 < n and skipped_notes <= 2:
                floor, desks_line, sqft_line, psf_line = lines[j : j + 4]
                if DESKS_LINE_RE.search(desks_line) and SQFT_LINE_RE.match(sqft_line) and PSF_LINE_RE.match(psf_line):
                    records.append(
                        {
                            "Area": current_area,
                            "Building": building,
                            "Floor/Unit": floor,
                            "Size (sq ft)": sqft_line.split("-")[-1].strip().replace(",", ""),
                            "Desks (max)": _max_desks(desks_line),
                            "Marketing Price (Based on Min Term) PSF": psf_line.split("-")[-1].replace("£", "").strip(),
                            "Special Features": features,
                            "Contacts": contact,
                        }
                    )
                    found_any = True
                    j += 4
                    continue
                if not found_any and skipped_notes < 2:
                    skipped_notes += 1
                    j += 1
                    continue
                break
            i = j
            continue
        buffer.append(lines[i])
        i += 1

    building_names = {r.get("Building") for r in records if r.get("Building")}
    photo_by_building = _photo_by_building(content, building_names)
    property_link_by_building = _property_link_by_building(content, building_names)
    for record in records:
        building = record.get("Building")
        candidates = photo_by_building.get(building)
        if candidates:
            record["_high_res_candidates"] = candidates
        property_link = property_link_by_building.get(building)
        if property_link:
            # Brochure PDF is the established linked-property-source column;
            # actual resource typing happens after retrieval and may resolve
            # this HTML property page to a brochure, floorplan, and/or photos.
            record["Brochure PDF"] = property_link

    return records


def _property_link_by_building(content, building_names):
    """Map exact building-name anchors to their own property-page URL."""
    building_by_lower = {name.lower(): name for name in building_names}
    result = {}
    for kind, text, url in content.get("html_items", []):
        if kind != "link" or not url:
            continue
        canonical = building_by_lower.get((text or "").strip().lower())
        if canonical and canonical not in result:
            result[canonical] = url
    return result


def _photo_by_building(content, building_names):
    """Maps each building name to its real marketing photo(s) — genuine
    hosted assets-gbr.mkt.dynamics.com images (GPE's own images have no
    distinguishing alt text, same situation as MetSpace, so this filters
    by source domain instead), keyed by building rather than by
    occurrence: confirmed empirically GPE's email links each building's
    name exactly once in the "CURRENT AVAILABILITY" section, not once
    per floor/unit row the way MetSpace's does — so every floor of the
    same building shares the same candidate photo(s), applied by name
    lookup below rather than by sequential position.

    A building can genuinely have TWO distinct real photos, not one:
    confirmed by actually viewing them (not assumed) that a building
    also featured in the promotional/news blurbs at the top of the email
    (before "CURRENT AVAILABILITY") has its own separate photo there,
    different from its "CURRENT AVAILABILITY" listing-card photo — e.g.
    Thirty One Alfred Place has a ground-floor lounge shot in its promo
    blurb and a rooftop terrace shot in its listing card; Elsley has an
    outdoor courtyard shot in its promo blurb and an interior lounge shot
    in its listing card. Neither photo is tied to a *specific floor* of
    the building (this source has no per-floor photos at all, unlike
    MetSpace/Knotel) — they're both building-level, just from two
    different parts of the same email, so all floors of a building with
    two photos share the same two-item candidate list; app.py turns 2+
    candidates into a small gallery page (see _finalize_high_res_images).

    The two sections use opposite DOM ordering, confirmed by direct
    inspection: the "CURRENT AVAILABILITY" listing-card section is
    LINK-then-IMAGE (a building's own name link, then its photo,
    established after an earlier version of this function had the
    direction backwards and silently shifted every building's photo
    onto the next building's name); the promotional section is
    TEXT-then-IMAGE-then-CTA (a building's name appears in a heading/
    description, its photo follows, then a shared "Find out more"/"Read
    the full story" button — with no per-building link to key off at
    all, since 2-3 buildings can share one CTA). This finds the listing-
    card photo via the established link-adjacency method, and separately
    scans only the promo region (bounded to everything before the first
    listing-card link, so it can't cross into and misread that section)
    for each building's own nearby photo, searching a few positions in
    either direction in a unified text+image sequence (BeautifulSoup's
    own document-order traversal, so mid-word line-wrapping in the raw
    HTML — e.g. "16\\n Dufour's Place" — can't break a plain substring
    search the way it would against the raw HTML string directly)."""
    html_items = content.get("html_items", [])
    photo_by_building = {}

    # Case-insensitive lookup (2026-07 audit — see extraction.rules.knotel's
    # own "View brochure" vs "View Brochure" casing bug for the precedent):
    # a link's own visible text isn't guaranteed to match this rule's
    # extracted Building text byte-for-byte in case, even for the same
    # listing. Resolves back to the CANONICAL Building spelling (the key
    # parse()'s own final photo_by_building.get(record.get("Building"))
    # lookup expects), not whatever case the link text happened to use.
    building_by_lower = {b.lower(): b for b in building_names}

    # --- listing-card section: established link -> image adjacency ---
    pending_building = None
    for kind, a, b in html_items:
        if kind == "link":
            pending_building = a
            continue
        if pending_building and "digitalassets/images" in b:
            canonical = building_by_lower.get(pending_building.lower())
            if canonical and canonical not in photo_by_building:
                photo_by_building[canonical] = [b]
            pending_building = None

    # --- promotional section: bounded text-proximity scan ---
    soup = BeautifulSoup(content.get("html", ""), "lxml")
    sequence = []
    for node in soup.descendants:
        if isinstance(node, Tag) and node.name == "img":
            src = node.get("src", "")
            if "digitalassets/images" in src:
                sequence.append(("image", src))
        elif isinstance(node, Tag) and node.name == "a":
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if text.lower() in building_by_lower:
                sequence.append(("boundary", text))
                break  # the "CURRENT AVAILABILITY" section starts here
        elif isinstance(node, NavigableString):
            text = re.sub(r"\s+", " ", str(node)).strip()
            if text:
                sequence.append(("text", text))

    window = 6  # sequence positions, either direction — generous enough
    # for a promo photo to sit a few text nodes from its heading, tight
    # enough not to reach into an unrelated building's own blurb.
    for name in building_names:
        positions = [i for i, (k, v) in enumerate(sequence) if k == "text" and name.lower() in v.lower()]
        for pos in positions:
            best_dist, best_src = None, None
            for j in range(max(0, pos - window), min(len(sequence), pos + window + 1)):
                if j == pos:
                    continue
                k, v = sequence[j]
                if k == "image":
                    dist = abs(j - pos)
                    if best_dist is None or dist < best_dist:
                        best_dist, best_src = dist, v
            if best_src:
                photo_by_building.setdefault(name, [])
                if best_src not in photo_by_building[name]:
                    photo_by_building[name].append(best_src)

    return photo_by_building


def _clean_lines(text):
    raw = [l.strip() for l in text.split("\n") if l.strip()]
    # A lone "-" bullet marker occasionally lands on its own line, separate
    # from its text (a source formatting quirk) — merge it with the next line.
    cleaned = []
    i = 0
    while i < len(raw):
        if raw[i] == "-" and i + 1 < len(raw):
            cleaned.append("- " + raw[i + 1])
            i += 2
        else:
            cleaned.append(raw[i])
            i += 1
    return cleaned


def _max_desks(desc):
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", desc)
    if m:
        return m.group(2)
    m2 = re.search(r"\d+", desc)
    return m2.group() if m2 else ""


_PHONE_RE = re.compile(r"\+?\d[\d\s()]{7,}\d")


def _contact_block(lines):
    """GPE emails end with a fixed, whole-email "Get in touch" block, not
    per-listing info — same idea as MetSpace's own contact block. Shaped
    as repeating Name / phone-number-line(s) groups (a phone number is
    sometimes hard-wrapped across two lines, e.g. "+44 (0) 7435 9" then
    "39 956" — a source formatting quirk), terminated by "View in
    browser".

    Pulls the phone number into Contacts alongside each name — confirmed
    (2026-07 audit) this was being silently dropped even though the
    source gives a real phone for every contact, the same class of gap
    Knotel's own missing contact info turned out to be. Collects every
    line between one name and the next (rather than assuming exactly one
    phone line) and joins them with NO separator before extracting the
    phone number, so a wrapped number like "+44 (0) 7435 9" / "39 956"
    reassembles into a normal-looking "+44 (0) 7435 939 956" — confirmed
    against the one real wrapped example seen so far, but this join
    convention (no separator) is an assumption, not verified against a
    second wrapped case; double check if a future contact's phone number
    ever looks wrong. names_only (extraction.schema) already strips
    phone numbers back out for Assigned Agents, same as it does for
    Knotel's own combined Contacts string, so this doesn't need any
    special-casing there. No email address appears anywhere in this
    block in the real source — nothing to add for that."""
    try:
        idx = lines.index("Get in touch")
    except ValueError:
        return ""
    end = next((i for i in range(idx + 1, len(lines)) if lines[i] == "View in browser"), len(lines))
    block = lines[idx + 1 : end]

    parts = []
    seen_names = set()
    i, n = 0, len(block)
    while i < n:
        line = block[i]
        if NAME_RE.match(line) and line not in seen_names:
            seen_names.add(line)
            parts.append(line)
            j = i + 1
            fragments = []
            while j < n and not NAME_RE.match(block[j]):
                fragments.append(block[j])
                j += 1
            phone_match = _PHONE_RE.search("".join(fragments))
            if phone_match:
                parts.append(phone_match.group().strip())
            i = j
        else:
            i += 1
    return ", ".join(parts)
