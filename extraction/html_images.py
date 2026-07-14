"""Generic Floor Plan / High Res Images / Brochure PDF enrichment for
.eml/.html sources that have NO dedicated rule-based parser of their own
and go through the LLM fallback instead — the non-PDF counterpart to
extraction.pdf_images/app.py's own _attach_pdf_images (which already
applies generically to any LLM-fallback PDF, gated only by file
extension, not by provider).

Unlike a dedicated rule — written and verified against ONE specific
source's own already-seen HTML structure (e.g. extraction.rules.metspace
trusting mcusercontent.com specifically as MetSpace's own real-photo
domain, or extraction.rules.knotel filtering by its own "X Floor featured
image" alt-text convention) — this has to work reasonably well against an
ARBITRARY, previously-unseen sender's layout. So it favours a defensible,
generic heuristic baseline (alt-text/URL patterns confirmed against a
real example, not assumptions about any one source) over the precision a
dedicated rule can achieve once a provider is seen often enough to
justify writing one.
"""
import re
from urllib.parse import urlparse

# Confirmed empirically (2026-07, The Workplace Company — a brand-new
# provider's marketing email, first one seen through this generic path)
# to reliably mark a decorative/non-content image, never a real listing
# photo: a broker's own headshot/company logo/social icon from an
# email-signature generator (wisestamp.com), Mailchimp's own icon assets,
# and open-tracking pixels (no recognizable image extension in the URL
# at all — just an opaque tracking path).
_NON_CONTENT_ALT_RE = re.compile(r"logo|icon|social", re.IGNORECASE)
_NON_CONTENT_DOMAIN_RE = re.compile(r"wisestamp\.com|cdn-images\.mailchimp\.com", re.IGNORECASE)
_IMAGE_EXTENSION_RE = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:[?#]|$)", re.IGNORECASE)

_FLOORPLAN_TEXT_RE = re.compile(r"floor\s*plan", re.IGNORECASE)
_BROCHURE_TEXT_RE = re.compile(r"brochure|view\s*propert|property\s*details|particulars", re.IGNORECASE)

# Confirmed real (2026-07, The Workplace Company): a source can give
# multiple link candidates for the same listing under different labels
# ("Brochure" vs "Website"), and the one literally labeled "Brochure"
# isn't always the usable one — its own "Brochure" column linked to
# Canva, a JS-rendered viewer (fetched directly: a real HTTP GET comes
# back as an HTML viewer page, never actual PDF bytes — the same
# already-confirmed-unusable category as Box.com and Pitch.com, checked
# the same way earlier this project), while its separate "Website"
# column pointed at the company's own domain, a real, working page.
# Domain-based, not label-based, since a source's own "Brochure"-labeled
# column/button can point at any of these — this lets a caller PREFER a
# different candidate link for the same listing when one exists, rather
# than trusting a label alone.
_LOW_TRUST_LINK_DOMAIN_RE = re.compile(r"(?:^|\.)canva\.(?:com|link)$|(?:^|\.)pitch\.com$|(?:^|\.)box\.com$", re.IGNORECASE)

# Positions either side of a building-name occurrence to scan for its own
# images/links when a source has 2+ distinct buildings — same idea and
# same order of magnitude as extraction.rules.gpe's own promotional-image
# proximity window, not independently re-derived here.
PROXIMITY_WINDOW = 8


def is_real_content_image(alt, src):
    """Best-effort "is this a genuine listing photo, not a logo/icon/
    tracking pixel" for a source with no dedicated rule of its own — see
    this module's own docstring for why this can only ever be a
    heuristic here, unlike a dedicated rule's own domain/alt-text
    allow-list tuned against one already-seen real source."""
    if not src:
        return False
    if _NON_CONTENT_ALT_RE.search(alt or ""):
        return False
    if _NON_CONTENT_DOMAIN_RE.search(src):
        return False
    if not _IMAGE_EXTENSION_RE.search(src):
        return False
    return True


def is_floorplan_link(text):
    return bool(_FLOORPLAN_TEXT_RE.search(text or ""))


def is_brochure_link(text):
    return bool(_BROCHURE_TEXT_RE.search(text or ""))


def is_low_trust_link_domain(url):
    """True if `url`'s domain is a known JS-rendered viewer/presentation
    tool (Canva, Pitch.com, Box.com — see _LOW_TRUST_LINK_DOMAIN_RE's own
    comment) rather than a real, directly-fetchable document. A caller
    with multiple candidate links for the same listing should prefer any
    OTHER candidate over one of these, falling back to a low-trust link
    only when it's genuinely the only one available — still better than
    nothing."""
    try:
        host = urlparse(url or "").netloc.lower()
    except ValueError:
        return False
    return bool(_LOW_TRUST_LINK_DOMAIN_RE.search(host))


def enrich_records(records, html_items):
    """Fills _high_res_candidates (resolved into a real High Res Images
    link/gallery by app.py's own _finalize_high_res_images, the same
    generic finishing step extraction.rules.gpe already relies on) plus
    Floor Plan and Brochure PDF directly, for records from an LLM-fallback
    .eml/.html source with no dedicated rule. Mutates records in place;
    never raises — this is always a best-effort enrichment, never
    something that should fail the batch.

    Two-tier, by how many distinct buildings are actually in this batch:

    - A single distinct Building across every record (confirmed the
      common case: a "property of the week"-style single-listing
      marketing email) — every real content image and floor-plan/
      brochure link ANYWHERE in the document belongs to it, there's no
      other listing to misattribute to (same reasoning as app.py's own
      _attach_pdf_images single-record special case).
    - 2+ distinct buildings (a multi-listing digest) — falls back to a
      bounded text-proximity scan around each occurrence of a building's
      own name in the html_items sequence. Real-world accuracy for this
      tier hasn't been verified against an actual multi-building
      LLM-fallback .eml yet, since none has come through this path so
      far — flag it if a future one is ever seen to misattribute."""
    if not records or not html_items:
        return

    buildings = {r.get("Building") for r in records if r.get("Building")}
    if len(buildings) <= 1:
        _enrich_single_building(records, html_items)
    else:
        _enrich_multi_building(records, html_items)


def _collect(html_items):
    """One pass over an html_items slice, returning (image_urls,
    floorplan_url, brochure_url) — shared by both the single- and
    multi-building tiers below so the classification rules only live in
    one place."""
    images = []
    floorplan_url = None
    brochure_url = None
    for kind, a, b in html_items:
        if kind == "image":
            if is_real_content_image(a, b) and b not in images:
                images.append(b)
        elif kind == "link":
            if floorplan_url is None and is_floorplan_link(a):
                floorplan_url = b
            elif brochure_url is None and is_brochure_link(a):
                brochure_url = b
    return images, floorplan_url, brochure_url


def _apply(record, images, floorplan_url, brochure_url):
    if images:
        record["_high_res_candidates"] = list(images)
    if floorplan_url:
        record["Floor Plan"] = floorplan_url
    if brochure_url:
        record["Brochure PDF"] = brochure_url


def _enrich_single_building(records, html_items):
    images, floorplan_url, brochure_url = _collect(html_items)
    for record in records:
        _apply(record, images, floorplan_url, brochure_url)


def _enrich_multi_building(records, html_items):
    n = len(html_items)
    for record in records:
        building = record.get("Building") or ""
        if not building:
            continue
        building_l = building.lower()
        anchor_positions = [
            i for i, (kind, a, _b) in enumerate(html_items) if kind == "link" and a and building_l in a.lower()
        ]
        if not anchor_positions:
            continue
        window_items = []
        for pos in anchor_positions:
            start, end = max(0, pos - PROXIMITY_WINDOW), min(n, pos + PROXIMITY_WINDOW + 1)
            window_items.extend(html_items[start:end])
        images, floorplan_url, brochure_url = _collect(window_items)
        _apply(record, images, floorplan_url, brochure_url)
