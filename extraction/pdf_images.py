"""Extracts real, embedded per-listing images from a PDF for sources with
no rule-based parser (LLM fallback) — e.g. BC, Crown Estate — as opposed
to Kitt's, which already gets Floor Plan/High Res Images from its own
table columns via extraction.rules.grid, or Knotel, which gets a Floor
Plan link from its email's own "Download Floorplan" button (extraction.
rules.knotel) — neither of those needs this module.

Confirmed empirically (2026-07) on the Crown Estate example that a
source's decorative/repeated assets (a "MANAGED - ALL INCLUSIVE RATES"
banner, a small footer logo) are byte-identical across many pages, while
a genuine per-listing photo is page-unique — so boilerplate is detected
by content hash, not by size/position, which varies too much to be a
reliable signal on its own.
"""
import hashlib
import re
from collections import defaultdict

_LEADING_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z\s'-]*)")

# A real per-listing photo should be page-unique (or shared by only the
# handful of floors on the same building's page). Anything recurring on
# more pages than that is decorative branding repeated throughout the
# document, not a listing's own image.
BOILERPLATE_MAX_PAGES = 2

# A tiny logo/icon fragment can be page-unique too (confirmed empirically
# on a BC brochure: a single 675-byte partial-logo image on one page,
# evading the boilerplate-by-repetition check above) — every genuine
# photo seen so far is tens of KB at minimum, so this threshold has a
# wide, safe margin rather than sitting right at the boundary.
MIN_IMAGE_BYTES = 3000

# A link annotation's own rect can differ slightly from the image's own
# placement rect for the same visual region (confirmed empirically on
# Crown Estate) — this is a generous-but-not-promiscuous IoU
# (intersection-over-union) threshold for treating the two as "the same
# spot", not an exact-equality check. Deliberately IoU, not "fraction of
# the smaller rect": confirmed empirically (BC's Clerkenwell Green
# brochure) that a small "watch our video"/"book a tour" link icon can
# sit entirely INSIDE a much larger unrelated photo — a fraction-of-
# smaller-rect check scores that as 100% overlap (the tiny link is fully
# contained), wrongly treating an incidental icon as if it were placed to
# cover the whole photo. IoU penalizes exactly this kind of size
# mismatch, since the union includes all the photo's area the tiny link
# doesn't cover.
_LINK_OVERLAP_THRESHOLD = 0.5

# viewings.ehouse.co.uk wraps a Matterport tour behind a login-gated
# viewer, even though the tour itself is already hosted publicly on
# Matterport's own domain under the exact same space ID — confirmed
# empirically (2026-07) for all 3 such links in the Crown Estate example:
# each https://viewings.ehouse.co.uk/#/matterport/show/<ID> link's own
# <ID>, fetched directly as https://my.matterport.com/show/?m=<ID>,
# returned the correct tour (matching page title, e.g. "The Linen Hall -
# Room 411 - Matterport 3D Showcase") with no login required, while the
# ehouse.co.uk URL itself hangs on a client-side "Loading..." screen for
# an anonymous/unauthenticated visitor (its "#/..." fragment is parsed by
# client-side JS, which then makes its own API call that a bare,
# unauthenticated link click can't get past). Rewritten here to the
# direct URL, which points at the identical real tour without the
# unnecessary login wall.
_EHOUSE_MATTERPORT_RE = re.compile(r"^https?://viewings\.ehouse\.co\.uk/#/matterport/show/([A-Za-z0-9]+)/?$", re.IGNORECASE)


def _normalize_floorplan_url(uri):
    if not uri:
        return uri
    m = _EHOUSE_MATTERPORT_RE.match(uri.strip())
    return f"https://my.matterport.com/show/?m={m.group(1)}" if m else uri


def _rect_overlap_fraction(a, b):
    """Intersection-over-union of two (x0, y0, x1, y1) rects — 0.0 if they
    don't overlap at all, 1.0 if they're identical."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _link_uri_for_rect(links, rect):
    """Returns the URI of a page's own link annotation covering `rect`
    (above _LINK_OVERLAP_THRESHOLD), or None.

    Confirmed empirically (Crown Estate, 2026-07) that each real
    per-listing image on a page can carry its own link annotation
    pointing to an external 3D-tour/floor-plan viewer (my.matterport.com,
    or a white-label viewer domain hosting the same kind of tour) — a
    real, source-labeled Floor Plan signal sitting on TOP of the image
    itself, not something visible in the image's own pixel content or
    embedded bytes at all, so extraction.pdf_images.is_floorplan_image's
    pixel-based check can never see it. Confirmed distinct per image
    (not one shared link for a whole page): each listing's own image had
    its own distinct tour ID.

    When more than one link annotation qualifies for the same image
    (confirmed empirically on "5 Swallow Place, 3rd Floor Suite 3.1":
    two overlapping link boxes on the same photo, one tightly fit to it
    pointing to a stale/superseded tour ID, one looser but pointing to
    the actual current tour — verified by directly opening both), this
    takes whichever qualifying link appears LAST in the page's own
    annotation list, not whichever has the tightest geometric fit. PDF
    annotations are appended in edit order, so a later annotation is
    more likely to be a correction layered on top of an older one left
    in place, rather than a tighter pixel fit necessarily being the
    intended link."""
    best_uri = None
    for link in links:
        uri = link.get("uri")
        lrect = link.get("from")
        if not uri or lrect is None:
            continue
        overlap = _rect_overlap_fraction(rect, (lrect.x0, lrect.y0, lrect.x1, lrect.y1))
        if overlap > _LINK_OVERLAP_THRESHOLD:
            best_uri = uri
    return _normalize_floorplan_url(best_uri)


def scan_pages(path):
    """Pass 1 of the memory-bounded extraction split (see load_page_images
    for pass 2): identifies which pages have real, non-boilerplate images
    and which content-hashes those are, WITHOUT retaining any image's raw
    bytes past the moment it's hashed.

    Confirmed empirically (2026-07) on a large Render free-tier PDF (Crown
    Estate, 4.3MB) that the original single-pass version of this module —
    decode every image on every page, then keep every one of them (deduped
    by hash, but still the full set of distinct real images in the whole
    document) alive in memory for as long as the caller took to walk every
    record and upload each match — could hold much more of a document's
    embedded photography in memory at once than any single page ever
    needs, on a document with many/large real photos (a 20-30MB brochure
    with high-resolution per-listing photography is a real, plausible
    document even though this specific example's images totaled a modest
    few MB). Splitting into a cheap hash-only scan (this function) plus an
    on-demand, one-page-at-a-time loader (load_page_images) bounds peak
    memory to roughly one page's own images, regardless of how large or
    image-heavy the document as a whole is.

    Returns {page_num (0-indexed): [hash, ...]} for pages with at least
    one real (non-boilerplate, non-tiny) image, in first-seen order — {}
    if PyMuPDF isn't installed or the PDF can't be opened/has no images."""
    try:
        import fitz
    except ImportError:
        return {}

    try:
        doc = fitz.open(path)
    except Exception:
        return {}

    hash_pages = defaultdict(set)
    page_hashes = defaultdict(list)

    try:
        for page_num in range(len(doc)):
            for img in doc[page_num].get_images(full=True):
                try:
                    base = doc.extract_image(img[0])
                except Exception:
                    continue
                data = base.get("image")
                if not data or len(data) < MIN_IMAGE_BYTES:
                    continue
                h = hashlib.sha256(data).hexdigest()
                hash_pages[h].add(page_num)
                page_hashes[page_num].append(h)
                # `data`/`base` go out of scope at the end of this
                # iteration — deliberately never stashed anywhere past
                # hashing; that's the whole point of this pass.
    finally:
        doc.close()
        try:
            # See load_page_images' own finally block for why this is
            # here — MuPDF's process-global store isn't freed just
            # because this Document closed.
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass

    boilerplate = {h for h, pages in hash_pages.items() if len(pages) > BOILERPLATE_MAX_PAGES}

    result = {}
    for page_num, hashes in page_hashes.items():
        real = list(dict.fromkeys(h for h in hashes if h not in boilerplate))
        if real:
            result[page_num] = real
    return result


def load_page_images(path, page_num, allowed_hashes):
    """Pass 2: re-opens the PDF just to decode the real images on ONE
    specific page, filtered to `allowed_hashes` (that page's own entry
    from scan_pages' result, above) so a page that also contains a
    boilerplate banner doesn't return it a second way. Called once per
    (page, record-match) from the caller's own loop, immediately after
    which the caller uploads and discards the bytes — so at most one
    page's real images are ever resident at once, never the whole
    document's.

    Deliberately reopens the file per call rather than keeping one
    fitz.Document handle open across many calls: PyMuPDF's own per-page
    decode cost is small (confirmed empirically: ~0.1s for this module's
    entire single-pass extraction across a whole 20-page brochure), so
    trading a little redundant CPU for never holding more than one page's
    images at a time is the right side of that tradeoff here. Returns
    [(image_bytes, ext, floorplan_url), ...] in first-seen order — []
    if PyMuPDF isn't installed, the PDF can't be reopened, or the page
    index is out of range; never raises. floorplan_url (see
    _link_uri_for_rect) is the URI of a link annotation covering this
    image's own position, if any — None otherwise."""
    try:
        import fitz
    except ImportError:
        return []

    try:
        doc = fitz.open(path)
    except Exception:
        return []

    try:
        if page_num < 0 or page_num >= len(doc):
            return []
        page = doc[page_num]
        links = page.get_links()
        allowed = set(allowed_hashes)
        seen = set()
        result = []
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue
            data = base.get("image")
            if not data or len(data) < MIN_IMAGE_BYTES:
                continue
            h = hashlib.sha256(data).hexdigest()
            if h not in allowed or h in seen:
                continue
            seen.add(h)
            rects = page.get_image_rects(xref)
            floorplan_url = None
            if rects:
                rect = rects[0]
                floorplan_url = _link_uri_for_rect(links, (rect.x0, rect.y0, rect.x1, rect.y1))
            result.append((data, base.get("ext", "png"), floorplan_url))
        return result
    finally:
        doc.close()
        # MuPDF keeps a process-global store (fonts/images) that speeds up
        # repeated rendering of the SAME document across calls — not
        # something Python's own refcounting/gc touches, and not released
        # just because this Document was closed. This function is called
        # once per (page, record-match), so a document with many
        # buildings/pages can trigger dozens of open/close cycles within
        # a single request; shrinking the store back down after each one
        # keeps that process-global cache from quietly ratcheting up
        # across those calls (confirmed no measurable difference on this
        # app's own current example PDFs, whose images are small — kept
        # anyway as cheap, standard PyMuPDF hygiene for memory-constrained
        # deployments, since a future source with larger/more embedded
        # photos is exactly the case this would matter for).
        try:
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass


def extract_page_images(path):
    """Returns {page_num (0-indexed): [(image_bytes, ext, floorplan_url), ...]}
    — real, non-boilerplate, non-tiny images only, deduped within a page,
    in first-seen order (see load_page_images for floorplan_url). Returns
    {} if PyMuPDF isn't installed or the PDF can't be opened/has no
    images — never raises, this is always an optional enrichment, not
    something that should fail extraction.

    A convenience wrapper around scan_pages + load_page_images that
    materializes every page's images at once, same as this function's
    original (single-pass) implementation — kept for callers that
    genuinely want the whole document's images together (e.g. this
    module's own test coverage) and don't need the peak-memory bound
    app.py's real request path cares about; see _attach_pdf_images in
    app.py for the page-by-page caller that actually needs that bound."""
    page_hashes = scan_pages(path)
    return {page_num: load_page_images(path, page_num, hashes) for page_num, hashes in page_hashes.items()}


def match_listings_to_images(path, page_hashes, records_by_page):
    """Pairs each real image on a page to the SPECIFIC listing it belongs
    to, by position — not to every record whose building name happens to
    appear anywhere on that page.

    Confirmed necessary empirically (Crown Estate, 2026-07): this
    source's pages routinely hold 2-6 distinct listings sharing one page
    (a 2-column or 3-column grid), each with its own real photos. The
    previous approach — find every page a building's name appears on,
    then attach every real image found on that whole page — silently
    merged unrelated buildings' photos into one shared gallery (e.g. "7
    Swallow Place" and "Charles House, 5-11 Regent Street", genuinely
    different buildings sharing a page, ending up with the identical
    photo set) whenever a page held more than one listing.

    page_hashes: {page_num: [hash, ...]} from scan_pages — the real,
    non-boilerplate images already known for each page.
    records_by_page: {page_num: [(record_index, building_name, floor_unit), ...]}
    in original extraction order, for records whose find_matching_pages()
    result included that page. floor_unit is that record's own extracted
    "Floor/Unit" value (may be "" if unknown) — see step 2 below for why
    this is needed alongside building_name.

    Returns {record_index: [(page_num, image_bytes, ext, floorplan_url), ...]}
    — page_num included (not just implied by the records_by_page key the
    caller already has) so a caller can still tell which source page a
    given image came from, e.g. for is_floorplan_page's own per-page text
    check. floorplan_url (see _link_uri_for_rect) is the URI of a link
    annotation covering this image's own position, if any — None
    otherwise.

    Algorithm, per page: locate each candidate listing's own heading text
    block (matching _building_candidates the same way find_matching_pages
    does, consuming text blocks in top-to-bottom/left-to-right reading
    order); then assign each real image to the nearest heading directly
    above it that horizontally overlaps its own column — the same
    heading a human reader would associate that image with, not just
    "some heading somewhere on this page." A page with only one listing
    degenerates to the old whole-page behavior automatically, since
    there's only one heading for every image to be nearest to.

    When two or more pending records share byte-identical heading text
    (e.g. two floors of the same building repeat on one page), reading
    order alone doesn't reliably say which LLM-extracted record a given
    occurrence belongs to — confirmed empirically wrong (Crown Estate,
    2026-07): "215-221 Regent Street"'s "3rd Floor South" occurrence sits
    ABOVE its "5th Floor" occurrence on the page, but the LLM's own
    listings array extracted "5th Floor" first — assuming record order
    matches page reading order paired every image with the wrong floor.
    _floor_label_near reads the page's own "Floor | <value>" text printed
    next to each heading occurrence and matches it against each
    candidate record's own floor_unit to disambiguate; falls back to
    reading order (the previous behavior) only when that comes back
    inconclusive, so this is a pure accuracy improvement over the
    single-occurrence and order-agrees-with-layout cases, which were
    already correct."""
    try:
        import fitz
    except ImportError:
        return {}
    try:
        doc = fitz.open(path)
    except Exception:
        return {}

    result = {}
    try:
        for page_num, record_entries in records_by_page.items():
            if page_num < 0 or page_num >= len(doc) or not record_entries:
                continue
            allowed = set(page_hashes.get(page_num, []))
            if not allowed:
                continue
            page = doc[page_num]
            links = page.get_links()

            # 1. Each real image's own position(s) + hash on this page.
            image_positions = []  # (bbox, image_bytes, ext, floorplan_url)
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    base = doc.extract_image(xref)
                except Exception:
                    continue
                data = base.get("image")
                if not data or len(data) < MIN_IMAGE_BYTES:
                    continue
                h = hashlib.sha256(data).hexdigest()
                if h not in allowed:
                    continue
                ext = base.get("ext", "png")
                for rect in page.get_image_rects(xref):
                    floorplan_url = _link_uri_for_rect(links, (rect.x0, rect.y0, rect.x1, rect.y1))
                    image_positions.append((rect, data, ext, floorplan_url))
            if not image_positions:
                continue

            # 2. Each listing's own heading block, consumed in reading
            # order so repeated identical headings (e.g. two floors of
            # "Princes House, 38 Jermyn Street") each claim a distinct
            # occurrence rather than all matching the first one found —
            # disambiguated by floor text (see _floor_label_near) when
            # more than one pending record shares this heading's building
            # name, since reading order alone isn't reliable there (see
            # this function's own docstring).
            text_blocks = sorted(
                (b for b in page.get_text("blocks") if (b[4] or "").strip()),
                key=lambda b: (round(b[1] / 10) * 10, b[0]),
            )
            pending = list(record_entries)
            headings = []  # (record_index, (x0, y0, x1, y1))
            for x0, y0, x1, y1, text, *_rest in text_blocks:
                if not pending:
                    break
                lowered = text.lower()
                matches = [
                    i
                    for i, (record_index, building_name, floor_unit) in enumerate(pending)
                    if any(c in lowered for c in _building_candidates(building_name))
                ]
                if not matches:
                    continue
                chosen = matches[0]
                if len(matches) > 1:
                    floor_label = _floor_label_near(text_blocks, (x0, y0, x1, y1))
                    if floor_label:
                        floor_label_lower = floor_label.lower()
                        floor_matches = [
                            i for i in matches if pending[i][2] and pending[i][2].lower() in floor_label_lower
                        ]
                        if len(floor_matches) == 1:
                            chosen = floor_matches[0]
                record_index, _building_name, _floor_unit = pending.pop(chosen)
                headings.append((record_index, (x0, y0, x1, y1)))
            if not headings:
                continue

            # 3. Each real image -> nearest heading above it, in the same
            # horizontal band (x-overlap), not just anywhere on the page.
            for (ix0, iy0, ix1, iy1), data, ext, floorplan_url in image_positions:
                best_index, best_y1 = None, None
                for record_index, (hx0, hy0, hx1, hy1) in headings:
                    overlaps_x = hx0 < ix1 and hx1 > ix0
                    if not overlaps_x or hy1 > iy0:
                        continue
                    if best_y1 is None or hy1 > best_y1:
                        best_y1, best_index = hy1, record_index
                if best_index is not None:
                    result.setdefault(best_index, []).append((page_num, data, ext, floorplan_url))
    finally:
        doc.close()
        try:
            # See load_page_images' own finally block for why this is here.
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass

    return result


_FLOORPLAN_TEXT_RE = re.compile(r"floor[\s-]?plan", re.IGNORECASE)


def find_matching_pages(building_name, pages_text):
    """Best-effort: every page (0-indexed) whose extracted text contains
    `building_name` (case-insensitive), using whichever candidate tier
    below is the first to match at least one page — never a mix of
    tiers. pages_text is the list of per-page text strings from
    extraction.file_readers' _read_pdf. Relies on these documents' own
    layout convention — a listing's own building name appears as a
    heading on every page describing it — confirmed empirically on the
    Crown Estate example (one building per page or page-pair) and on a
    single-listing brochure (BC's own "2-7 Clerkenwell Green" brochure,
    where the name recurs across all 10 pages); not a guarantee for
    every possible PDF layout.

    Tries the building name as given first, then two narrower fallbacks —
    since an LLM-extracted "Building" often joins a name and its street
    address (e.g. "Linen Hall, 162-168 Regent Street" or "Linen Hall
    162-168 Regent Street") while the source PDF's own text has them as
    separate lines/tokens, sometimes with the name repeated first (a
    two-column page layout printing "LINEN HALL LINEN HALL" before the
    address starts) — which breaks a match on the full joined string
    even after normalizing punctuation. Confirmed empirically on Linen
    Hall/Princes House/Crown House/Maddox House/Kendal House: only the
    bare name portion (stopping at the first digit or comma) reliably
    matches in these cases.
      1. the building name as given
      2. the part before the first comma, if any
      3. the leading run of letters/spaces (stops at the first digit,
         comma, parenthesis, or other punctuation — e.g. "Princes House"
         from both "Princes House 38 Jermyn Street" and "Princes House
         (38 Jermyn Street)", since the LLM's exact punctuation isn't
         deterministic call to call)
    A building name that's mostly numeric (e.g. "1 Vine Street") has no
    useful (3), so it relies on (1)/(2) instead — already confirmed
    working for those via the full-string match."""
    for candidate in _building_candidates(building_name):
        if not pages_text:
            continue
        matches = [i for i, text in enumerate(pages_text) if candidate in (text or "").lower()]
        if matches:
            return matches
    return []


def find_all_matching_pages(building_name, pages_text):
    """Like find_matching_pages, but the UNION of every candidate tier's
    own matches, instead of stopping at the first tier that matches
    anything.

    Needed specifically when several records share byte-identical
    Building text and their real occurrences are spread across pages
    with different levels of text detail — confirmed empirically (Crown
    Estate, 2026-07): "1 Vine Street, W1" matches one page's own raw text
    exactly (comma and area code both present), but a DIFFERENT floor of
    the exact same building sits on a page whose own layout doesn't
    repeat the area code, so only the bare "1 Vine Street" (a narrower
    candidate tier) matches there. find_matching_pages' single-first-tier
    behavior finds the first page and stops, silently missing the
    second occurrence's page entirely — fine for a single record (an
    over-broad narrower tier risks matching an unrelated page), but wrong
    for grouping several same-text records across their real occurrences,
    where every candidate tier's pages are genuinely relevant."""
    pages = set()
    for candidate in _building_candidates(building_name):
        if not pages_text:
            continue
        pages.update(i for i, text in enumerate(pages_text) if candidate in (text or "").lower())
    return sorted(pages)


def count_heading_occurrences(path, page_nums, building_name):
    """Returns {page_num: count} — how many of a page's own text blocks
    match building_name's own _building_candidates, for each page in
    page_nums.

    Needed alongside find_matching_pages, not instead of it: several
    records can share byte-identical Building text when the same
    building spans several floors (e.g. Crown Estate's "Princes House,
    38 Jermyn Street" across 4 pages, 2 floors per page) — and
    find_matching_pages naturally returns EVERY one of those pages for
    EVERY one of those records, since it only answers "where does this
    name appear at all", not "which specific occurrence is this
    particular record". Naively registering every such record on every
    matching page (in a caller building a page->records map) lets several
    pages' images all pile onto whichever records happen to come first,
    while later floors get no images at all — confirmed exactly this
    empirically (Crown Estate, 2026-07): only the first 2 of 7 "Princes
    House" floors ended up with any image, the other 5 got none, even
    though every one of those 4 pages had its own real, distinct photos.

    This lets a caller distribute several same-name records across their
    real distinct page occurrences in order (2 records to a page with 2
    headings, 1 to a page with only 1, etc.) instead of assuming
    ambiguously that every one of them belongs to every matching page."""
    try:
        import fitz
    except ImportError:
        return {}
    try:
        doc = fitz.open(path)
    except Exception:
        return {}

    candidates = _building_candidates(building_name)
    counts = {}
    try:
        for page_num in page_nums:
            if page_num < 0 or page_num >= len(doc):
                continue
            count = 0
            for block in doc[page_num].get_text("blocks"):
                text = (block[4] or "").strip()
                if text and any(c in text.lower() for c in candidates):
                    count += 1
            counts[page_num] = count
    finally:
        doc.close()
        try:
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass

    return counts


def _floor_label_near(text_blocks, heading_rect, max_dy=200):
    """Finds the "Floor | <value>" text block (as printed directly on the
    page, e.g. "Floor | 3rd Floor South") associated with a specific
    heading occurrence at heading_rect, for disambiguating which pending
    record a repeated heading belongs to (see match_listings_to_images).

    These documents print a listing's own Floor sub-field either directly
    below its heading at the same x position, or to the right of it at
    roughly the same y (both layouts seen in the Crown Estate example) —
    so this picks whichever "Floor"-prefixed block is closest by a
    distance that tolerates either, among blocks not positioned above or
    well to the left of the heading (which would belong to a different
    listing's column instead)."""
    hx0, hy0, _hx1, _hy1 = heading_rect
    best_text, best_dist = None, None
    for x0, y0, _x1, _y1, text, *_rest in text_blocks:
        stripped = (text or "").strip()
        if not stripped.lower().startswith("floor"):
            continue
        if y0 < hy0 - 5 or y0 - hy0 > max_dy or x0 < hx0 - 5:
            continue
        dist = (y0 - hy0) + max(0, x0 - hx0) * 0.1
        if best_dist is None or dist < best_dist:
            best_dist, best_text = dist, stripped
    return best_text


def _building_candidates(building_name):
    """Shared with match_listings_to_images below: the same three
    name-matching tiers as find_matching_pages' own docstring, lowercased
    and de-duplicated, in priority order — the *name* portion of a
    building string, not the street-address portion (contrast
    extraction.pipeline's own comma-split, which wants the opposite half
    for geocoding)."""
    candidates = [building_name or ""]
    if "," in (building_name or ""):
        candidates.append(building_name.split(",", 1)[0])
    m = _LEADING_NAME_RE.match(building_name or "")
    if m and m.group(1).strip():
        candidates.append(m.group(1))
    seen = set()
    result = []
    for c in candidates:
        name = c.strip().lower()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def is_floorplan_page(page_text):
    """True if a page's own text calls out its content as a floor plan
    (e.g. BC's brochure literally has a page headed "Example Floorplan").
    A real, source-labeled signal, not a guess from pixel content — but
    confirmed empirically (Breezblok's John Stow House brochure) that not
    every source labels its floor-plan page this way, so this alone isn't
    enough; see is_floorplan_image for a per-image fallback."""
    return bool(_FLOORPLAN_TEXT_RE.search(page_text or ""))


# A CAD-rendered floor-plan diagram is drawn on a plain white/pale
# background, unlike either a real photo or a decorative logo/icon
# graphic — confirmed empirically across BC's and Breezblok's own real
# floor-plan images (69.7% and 84.9% of pixels near-white respectively)
# versus every real photo tested (never above ~17%) and Breezblok's own
# decorative header/footer illustration (34.5% — low unique-color count
# like a floor plan, but nowhere near as white). 0.5 sits with a wide
# margin on both sides of that gap.
# 0.5 missed some MetSpace Drive floor-plan pages (gray/cream boards) that
# then leaked into High Res. 0.38 still sits well above real office photos
# tested previously (~17% white) while catching lighter plan diagrams.
FLOORPLAN_WHITE_FRACTION = 0.38
# Only need a rough estimate, so large images are downsampled first —
# this keeps the check cheap even for a multi-megapixel source image.
_MAX_SAMPLE_PIXELS = 400 * 400
_SAMPLE_WIDTH = 200


def _white_fraction(image_bytes):
    """Fraction of an image's pixels that are near-white (all three RGB
    channels above 235). Returns 0.0 (never mistaken for a floor plan) if
    Pillow can't decode it, rather than raising — this is only ever an
    extra signal on top of the existing hash/size-based filtering, never
    something that should fail extraction."""
    try:
        from PIL import Image
    except ImportError:
        return 0.0
    try:
        import io

        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return 0.0

    w, h = im.size
    if w * h > _MAX_SAMPLE_PIXELS and w > 0:
        im = im.resize((_SAMPLE_WIDTH, max(1, int(_SAMPLE_WIDTH * h / w))))

    total = 0
    white = 0
    for r, g, b in im.getdata():
        total += 1
        if r > 235 and g > 235 and b > 235:
            white += 1
    return (white / total) if total else 0.0


def is_floorplan_image(image_bytes):
    """True if an image's own pixel content looks like a floor-plan
    diagram rather than a photo or decorative graphic (see
    FLOORPLAN_WHITE_FRACTION). Deliberately a per-image check, not a
    per-page one: confirmed on Breezblok's John Stow House brochure that
    a floor-plan diagram and a real desk photo can share the same PDF
    page, so classifying by page alone would wrongly keep or exclude
    both together."""
    return _white_fraction(image_bytes) > FLOORPLAN_WHITE_FRACTION


def build_gallery_html(title, image_urls):
    """A minimal, self-contained HTML page listing several image URLs
    stacked together — used when a listing has more than one real photo,
    since a spreadsheet cell can only hold one hyperlink. No JS, no
    external assets — just <img> tags pointing at each image's own
    already-hosted download URL."""
    import html as _html

    safe_title = _html.escape(title or "Photos")
    imgs_html = "\n".join(
        f'<img src="{_html.escape(url)}" alt="Photo {i + 1}" '
        f'style="max-width:100%; height:auto; display:block; margin-bottom:16px; border-radius:6px;">'
        for i, url in enumerate(image_urls)
    )
    return (
        "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{safe_title} — Photos</title>\n"
        "<style>\n"
        "  body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; "
        "background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }\n"
        "  h1 { font-size:18px; margin:0 0 16px; }\n"
        "  .gallery { max-width:900px; margin:0 auto; }\n"
        "</style>\n</head>\n<body>\n<div class=\"gallery\">\n"
        f"<h1>{safe_title}</h1>\n{imgs_html}\n</div>\n</body>\n</html>"
    )
