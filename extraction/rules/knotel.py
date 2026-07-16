"""Rule-based parser for Knotel's "Availability" email layout.

Layout (as plain text, one item per line):
    AREA HEADER (all caps, e.g. "CITY", "WEST END")
    Building name                      <- only present for the first floor
    Full address, London POSTCODE      <- of a given building
    Available[ from <date>]
    London
    Floor descriptor
    Full address, London POSTCODE      (repeated)
    [View property] [Download Floorplan] [View Brochure] [View Listing]  (buttons, order varies)
    Seats
    <n>
    Size
    <n> sqft
    Price (monthly)
    £<n> pcm
    Price (per sqft)
    £<n> per sqft

A building with multiple floors repeats from "Available" onward without
repeating the name/address lines.

Contacts is a fixed, whole-email value (_contact_block) — Knotel gives no
individual broker name, only a shared team contact in the intro
paragraph. Special Features carries a per-floor price-drop note when the
intro also flags one (_price_drop_notes) — otherwise blank. Brochure PDF prefers a real, working knotel.com link ("View property"/
"View Listing") over "View Brochure" itself (_best_brochure_link) —
confirmed real (2026-07) that "View Brochure" often points at pitch.com,
a JS-rendered viewer. When View Brochure is a high-trust HTTP(S) URL it
is still kept on `_extra_brochure_urls` so enrichment can fetch it.
Distinct from Link to File, which app.py always overwrites with the
persisted source file link regardless of what this rule sets.
"""
import re

from extraction.html_images import is_low_trust_link_domain
from extraction.assets import normalize_url
from extraction.text_utils import titlecase_area

AREA_RE = re.compile(r"^[A-Z][A-Z ]+$")
POSTCODE_HINT_RE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", re.IGNORECASE)
LINK_LABELS = ("View property", "Download Floorplan", "View Brochure", "View Listing", "View Floorplan")
_LINK_LABELS_LOWER = {label.lower() for label in LINK_LABELS}

FROM_LINE_RE = re.compile(r"^(.+?)\s*<[^>]+>$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
PHONE_RE = re.compile(r"\b0\d{3,4}[\s-]?\d{3}[\s-]?\d{3,4}\b")
GET_IN_TOUCH_RE = re.compile(r"get in touch", re.IGNORECASE)

PRICE_DROP_HEADER_RE = re.compile(r"^\*\*\s*PRICE DROP AT (.+?)\s*\*\*$")
PRICE_DROP_LINE_RE = re.compile(r"^(.+?)\s*[–-]\s*now\s+(£[\d,.]+\s*(?:psf|pcm))\s*$", re.IGNORECASE)


def detect(content):
    blob = ((content.get("sender") or "") + " " + (content.get("text") or "")[:3000]).lower()
    if "knotel" in blob:
        return True
    return any("knotel.com" in href.lower() for _, href in content.get("links", []))


def _combine_name_and_address(name, address):
    """Knotel's own marketing name for a building (e.g. "Hallmark", "15
    Hatfields") often differs from its real registered address (e.g. "The
    Hallmark Building, 106 Fenchurch St, London EC3M 5JE", "Chadwick
    Court, London SE1 8DJ") — sometimes the name is already embedded in
    the address text ("Classic House" is literally the start of "Classic
    House, 174-180 Martha's Buildings..."), sometimes not at all ("Gilray
    House" never appears in "146-150 City Rd, London EC1V 2RL"). The real
    address is the only thing with the postcode this app's geocoding
    needs — extraction.schema.normalize_record reads Property Address 1/
    Postcode straight from this Building field — so it's always kept;
    the marketing name is only prepended when the address doesn't already
    mention it, so the output stays recognizable without a redundant
    duplicate name."""
    if not address:
        return name
    if name and name.lower() not in address.lower():
        return f"{name}, {address}"
    return address


def _contact_block(lines):
    """Knotel's own emails give no individual broker name for any listing
    at all — just a shared team contact mentioned in the intro
    paragraph, e.g. "feel free to get in touch via londonbrokers@
    knotel.com or on 0204 571 4271" — the same "company/team, not a
    named person, acting as the contact" pattern already handled for
    GPE/Crown Estate's sole-agent case (extraction.llm_fallback's own
    prompt rules).

    Anchored specifically on the line containing "get in touch" (rather
    than scanning the whole intro block) so this can't instead pick up
    the recipient's own forwarding signature at the very top of every
    forwarded copy (a different phone number, "0203 369 9800", sits
    there) or the forwarded email's own "From:"/"To:" header addresses
    (mail@brokers.knotel.com / lthomasson@spacepoint.co.uk) — neither of
    those is the contact this app should show.

    The sender's own display name (e.g. "Knotel Brokers") comes from the
    forwarded email's own "From:" header line — confirmed present, in
    the exact same "From:" / "Name <email>" shape, in both example
    emails; real text Outlook itself wrote when this was forwarded, not
    fabricated."""
    sender_name = ""
    try:
        idx = lines.index("From:")
        m = FROM_LINE_RE.match(lines[idx + 1])
        if m:
            sender_name = m.group(1).strip()
    except (ValueError, IndexError):
        pass

    touch_line = next((l for l in lines if GET_IN_TOUCH_RE.search(l)), "")
    email_match = EMAIL_RE.search(touch_line)
    phone_match = PHONE_RE.search(touch_line)

    parts = [p for p in (sender_name, email_match and email_match.group(), phone_match and phone_match.group()) if p]
    return ", ".join(parts)


def _price_drop_notes(lines):
    """Knotel's intro sometimes flags a recent price reduction for
    specific floors of one building, e.g.:
        ** PRICE DROP AT 15 HATFIELDS **
        We've reduced the pricing at 15 Hatfields:
        1st Floor – now £120 psf
        3rd Floor – now £115 psf
    Genuinely relevant context for those exact rows (confirmed the
    price already shown in each floor's own "Price (per sqft)" matches
    the promo note exactly, in the one real example seen so far) — not
    a sale signal (this rule never sets Sale Price at all, and Knotel is
    lettings-only; a price-drop mention isn't evidence of a sale, same
    "promotional pricing != for sale" distinction already confirmed for
    GPE).

    Returns {building_name_lower: {floor_label: note}}. Building-
    agnostic — keyed off whatever name follows "PRICE DROP AT", not a
    hardcoded "15 Hatfields" — so a future email promoting a different
    building's floors is picked up the same way. Scans a bounded window
    after the header (skipping the intervening "We've reduced..." lead-
    in sentence) and stops early at the next area header, since that
    means the actual listings have started."""
    notes = {}
    i = 0
    n = len(lines)
    while i < n:
        m = PRICE_DROP_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        building_name = m.group(1).strip()
        floor_notes = {}
        j = i + 1
        window_limit = min(n, i + 12)
        while j < window_limit:
            if AREA_RE.match(lines[j]) and len(lines[j].split()) <= 3:
                break
            line_m = PRICE_DROP_LINE_RE.match(lines[j])
            if line_m:
                floor_label, price = line_m.group(1).strip(), line_m.group(2).strip()
                floor_notes[floor_label] = f"Price drop: now {price}"
            j += 1
        if floor_notes:
            notes[building_name.lower()] = floor_notes
        i = j
    return notes


def _price_drop_note_for(price_drop_notes, building, floor):
    building_key = next((k for k in price_drop_notes if k in building.lower()), None)
    if not building_key:
        return ""
    floor_label = next((label for label in price_drop_notes[building_key] if label.lower() in floor.lower()), None)
    return price_drop_notes[building_key][floor_label] if floor_label else ""


def parse(content):
    lines = [l.strip() for l in content["text"].split("\n") if l.strip()]
    link_groups = _group_items(content.get("html_items", []))
    contact = _contact_block(lines)
    price_drop_notes = _price_drop_notes(lines)

    records = []
    current_area = ""
    current_building = ""
    group_idx = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Area header: short, all-caps, letters/spaces only, and not itself
        # an address/label line.
        if AREA_RE.match(line) and len(line.split()) <= 3 and not POSTCODE_HINT_RE.search(line):
            # Guard against matching things like "SEATS" etc. — area headers
            # are followed by a building name then an address line.
            if i + 2 < n and POSTCODE_HINT_RE.search(lines[i + 2]) or (i + 1 < n and POSTCODE_HINT_RE.search(lines[i + 1])):
                current_area = titlecase_area(line)
                i += 1
                continue

        if line == "Available" or line.startswith("Available from"):
            # Optionally preceded (immediately, before this Available block)
            # by a fresh "building name" + "address" pair. Confirmed
            # (2026-07, a real Render regression against a live Knotel
            # email) that gating this on POSTCODE_HINT_RE matching the
            # address line — the previous approach — silently fails
            # whenever a building's own address only carries a partial/
            # outward-only postcode with no inward part at all, e.g.
            # "Market Exchange" at "8 Macklin Street, Covent Garden WC2":
            # current_building then never updates away from whatever
            # building preceded it ("33 Soho" in that real case), and the
            # real address+postcode text is discarded entirely — the
            # bare building name from lines[i - 2] was all that ever got
            # kept, which is exactly why every Knotel record's postcode
            # was silently coming out blank. Instead, rely on the
            # layout's own structural invariant (see this module's
            # docstring): a genuinely fresh building's address line
            # repeats verbatim 3 lines later (after "London" and the
            # floor descriptor), whereas a later floor of the *same*
            # building has no fresh name/address before "Available" at
            # all — lines[i - 1] there is the previous floor's own last
            # price line, which never equals lines[i + 3]. This holds
            # regardless of whether the postcode is full, partial, or
            # embedded oddly, so it can't be fooled by a postcode-shape
            # false negative the way the old gate was.
            if (
                i >= 2
                and i + 3 < n
                and lines[i - 1] == lines[i + 3]
                and not lines[i - 2].startswith("Available")
            ):
                current_building = _combine_name_and_address(lines[i - 2], lines[i - 1])

            floor = lines[i + 2] if i + 2 < n else ""

            # Scan forward (bounded) for the four labeled values.
            window = lines[i : i + 25]
            seats = _value_after(window, "Seats")
            size = _value_after(window, "Size")
            price_monthly = _value_after(window, "Price (monthly)")
            price_psf = _value_after(window, "Price (per sqft)")

            group = link_groups[group_idx] if group_idx < len(link_groups) else {}
            group_idx += 1
            brochure_primary = _best_brochure_link(group)
            brochure_extras = _extra_brochure_urls(group, brochure_primary)

            records.append(
                {
                    "Area": current_area,
                    "Building": current_building,
                    "Floor/Unit": floor,
                    "Size (sq ft)": _strip_units(size, "sqft"),
                    "Desks (max)": seats,
                    "Marketing Price (Based on Min Term) PCM": _strip_units(price_monthly, "pcm"),
                    "Marketing Price (Based on Min Term) PSF": _strip_units(price_psf, "per sqft"),
                    "Special Features": _price_drop_note_for(price_drop_notes, current_building, floor),
                    "Contacts": contact,
                    "Brochure PDF": brochure_primary,
                    "Floor Plan": group.get("floorplan", ""),
                    # Featured email photo plus later knotel.com page photos
                    # land in _high_res_candidates so app.py can build a
                    # multi-image gallery (same path as GPE), not a single
                    # direct High Res URL that enrichment never expands.
                    "High Res Images": group.get("highres", ""),
                    "_high_res_candidates": (
                        [group["highres"]] if group.get("highres") else []
                    ),
                    # High-trust View Brochure / View Listing URLs that were
                    # not chosen as the primary seed — enrichment fetches
                    # these too so under-filled cells still get PDF embeds
                    # when the property page alone is thin.
                    **({"_extra_brochure_urls": brochure_extras} if brochure_extras else {}),
                }
            )
        i += 1

    return records


def _best_brochure_link(group):
    """Prefer knotel.com property/listing pages over View Brochure.

    View Brochure often points at pitch.com (low-trust JS viewer) — skip
    those unless nothing else exists. When View Brochure is a real
    HTTP(S) document host, `_extra_brochure_urls` still seeds enrichment.
    """
    for key in ("property", "listing", "brochure"):
        url = group.get(key)
        if url and not is_low_trust_link_domain(url):
            return url
    for key in ("property", "listing", "brochure"):
        if group.get(key):
            return group[key]
    return ""


def _extra_brochure_urls(group, primary):
    """Additional high-trust brochure/property URLs for linked enrichment.

    Prefer property page as Brochure PDF, but also keep a fetchable View
    Brochure (non-pitch) so galleries are not stuck on one featured image
    when the property page is thin or times out.
    """
    extras = []
    seen = {normalize_url(primary)} if primary else set()
    for key in ("property", "listing", "brochure"):
        url = group.get(key)
        if not url or is_low_trust_link_domain(url):
            continue
        normalized = normalize_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        extras.append(url)
    return extras


def _value_after(window, label):
    for idx, l in enumerate(window):
        if l == label and idx + 1 < len(window):
            return window[idx + 1]
    return ""


def _strip_units(value, *units):
    if not value:
        return ""
    v = value
    for u in units:
        v = re.sub(re.escape(u), "", v, flags=re.IGNORECASE)
    return v.replace("£", "").strip()


def _group_items(items):
    """Chunk the ordered (kind, text_or_alt, href_or_src) stream — from
    extraction.file_readers' html_items — into one dict per listing, keyed
    by which button labels (and photo, see below) were found. A new group
    starts at "View property" (the black primary button), which every
    listing has first.

    Confirmed empirically (2026-07) that each listing's own card is laid
    out in this literal DOM order: an <img alt="... featured image">
    (a real, listing-specific photo — not a logo/icon), then floor/address
    text, then this same button row. So the most recently seen "featured
    image" src, at the moment a new group starts, belongs to that group —
    reset immediately after so it can't leak into a later listing that
    has no photo of its own."""
    groups = []
    current = None
    pending_image = None
    for kind, a, b in items:
        if kind == "image":
            if "featured image" in a.lower():
                # The <img> tag's own src carries Directus's on-the-fly
                # transform params (?width=600&height=300&quality=70&
                # format=jpeg&fit=cover&v=...) - that's a deliberately
                # shrunk/compressed thumbnail sized for the email, not the
                # original. Confirmed empirically this also caused
                # "Illegal asset transformation" errors on some assets, and
                # that dropping the query string entirely (same idea as
                # Floor Plan's own "?download=" link, no resize params at
                # all) serves the real, full-resolution original instead.
                pending_image = b.split("?", 1)[0]
            continue

        text, href = a, b
        # Confirmed (2026-07, a real Knotel email) two distinct source-HTML
        # quirks that would otherwise silently drop a real, genuine
        # brochure link:
        #  - inconsistent button casing ("View brochure" instead of "View
        #    Brochure") for one listing (33 Soho) — matched case-
        #    insensitively below instead of via exact membership in
        #    LINK_LABELS.
        #  - text and href reversed entirely for another listing (23 Great
        #    Titchfield Street): href held the literal label text ("View
        #    Brochure") while the tag's own visible text held the real
        #    destination URL. Swapped back before the rest of this
        #    function's label-based logic runs, so it's treated exactly
        #    like every normal, correctly-formed button.
        if href.lower() in _LINK_LABELS_LOWER and text.lower().startswith(("http://", "https://")):
            text, href = href, text
        if text.lower() not in _LINK_LABELS_LOWER:
            continue
        if text.lower() == "view property":
            current = {}
            groups.append(current)
            if pending_image:
                current["highres"] = pending_image
                pending_image = None
        if current is None:
            current = {}
            groups.append(current)
        low = text.lower()
        if "brochure" in low:
            current["brochure"] = href
        elif "floorplan" in low:
            current["floorplan"] = href
        elif "listing" in low:
            current["listing"] = href
        elif "property" in low:
            current["property"] = href
    return groups
