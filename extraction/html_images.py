"""Generic Floor Plan / High Res Images / Brochure PDF enrichment for
.eml/.html sources that have NO dedicated rule-based parser of their own
and go through the LLM fallback instead — the non-PDF counterpart to
extraction.pdf_images/app.py's own _attach_pdf_images (which already
applies generically to any LLM-fallback PDF, gated only by file
extension, not by provider).

Unlike a dedicated rule — written and verified against ONE specific
source's own already-seen HTML structure (e.g. extraction.rules.metspace
trusting mcusercontent.com specifically as MetSpace's own FLOOR PLAN
domain for its usual "WEEKLY AVAILABILITY" template, or
extraction.rules.knotel filtering by its own "X Floor featured image"
alt-text convention) — this has to work reasonably well against an
ARBITRARY, previously-unseen sender's layout, or even a DIFFERENT
template from an already-known sender (confirmed real, 2026-07:
MetSpace's own "Office Of The Week" single-listing spotlight template
goes through this generic path, not its dedicated rule, and — unlike
its usual template — genuinely has BOTH a real content photo and a
floor-plan diagram from the exact same mcusercontent.com domain, so the
domain-based assumption that holds for metspace.py's own template
doesn't hold here). So this favours a defensible, generic heuristic
baseline (alt-text/URL patterns confirmed against a real example, not
assumptions about any one source) over the precision a dedicated rule
can achieve once a provider is seen often enough to justify writing
one — including, for the floor-plan-vs-photo question specifically, an
actual pixel-content check (is_floorplan_image, shared with
extraction.pdf_images) rather than guessing from the URL/alt text
alone, which carries no signal at all for a domain like this one.
"""
import re
from urllib.parse import urlparse

import requests

from . import pdf_images

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

# Confirmed real (2026-07, Workplace Plus): a decorative section-header
# banner image ("Availability", a stylized graphic, not a listing photo)
# had alt text too generic to catch by _NON_CONTENT_ALT_RE, but its own
# filename literally contained "...availability%20logo.png" — checked
# against the URL itself, not just alt text, for exactly this reason.
_NON_CONTENT_URL_RE = re.compile(r"logo", re.IGNORECASE)

_FLOORPLAN_TEXT_RE = re.compile(r"floor\s*plan", re.IGNORECASE)
# Visible labels are often generic ("CLICK HERE", building name, blank) even
# when the href is a real brochure/Drive/property page — MetSpace building-
# name links and Union "CLICK HERE" cells are the confirmed cases.
_BROCHURE_TEXT_RE = re.compile(
    r"brochure|view\s*propert|property\s*details|particulars|click\s*here|"
    r"view\s*listing|website|download|more\s*info|landlord|full\s*details",
    re.IGNORECASE,
)
_DOCUMENT_URL_RE = re.compile(
    r"(?:drive\.google\.com|docs\.google\.com|dropbox\.com|/file/d/|"
    r"\.pdf(?:[?#]|$)|list-manage\.com|usercontent\.google\.com|"
    r"box\.com/s/)",
    re.IGNORECASE,
)

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
    if _NON_CONTENT_URL_RE.search(src):
        return False
    if _NON_CONTENT_DOMAIN_RE.search(src):
        return False
    if not _IMAGE_EXTENSION_RE.search(src):
        return False
    return True


def is_floorplan_link(text):
    return bool(_FLOORPLAN_TEXT_RE.search(text or ""))


def is_brochure_link(text, url=""):
    """True when link text OR the destination URL looks like a property
    brochure / particulars / Drive document — not only when the visible
    label literally says 'Brochure'."""
    if _BROCHURE_TEXT_RE.search(text or ""):
        return True
    lowered = (url or "").lower()
    # Mailchimp footers share list-manage.com with real listing trackers.
    if any(token in lowered for token in ("unsubscribe", "/profile?", "update%20your%20preferences", "preferences")):
        return False
    return bool(_DOCUMENT_URL_RE.search(url or ""))


# Real network fetch, so bounded to a short timeout — a slow/unreachable
# image shouldn't hang or fail the whole batch (see is_floorplan_image_url
# below, which treats a fetch failure as "not a floor plan" rather than
# raising or blocking).
_IMAGE_FETCH_TIMEOUT_SECONDS = 8


def is_floorplan_image_url(url):
    """Best-effort "is this real content image actually a floor-plan
    diagram, not a photo" for a source with no dedicated rule of its own.
    Confirmed real (2026-07, MetSpace's own "Office Of The Week"
    single-listing template, an already-known sender's DIFFERENT
    template going through this generic path instead of its own
    dedicated rule): the URL/alt text alone carries no signal at all here
    — both a real interior photo and a floor-plan diagram came from the
    exact same mcusercontent.com domain, with no distinguishing alt text
    on either. Reuses extraction.pdf_images.is_floorplan_image's own
    pixel-content signal instead (a floor plan renders on an
    overwhelmingly white background, confirmed empirically far whiter
    than any real photo) — an actual fetch of the image bytes, not a URL
    guess. Any fetch failure (network error, timeout, non-2xx, undecodable
    image) is treated as "not a floor plan" — kept as a photo candidate
    rather than dropped, and never allowed to fail or block the batch."""
    try:
        resp = requests.get(url, timeout=_IMAGE_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except Exception:
        return False
    return pdf_images.is_floorplan_image(resp.content)


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
    - 2+ distinct buildings (a multi-listing digest) — segments
      html_items by real-content-image position, one segment per
      listing (see _segment_by_real_image). Confirmed real (2026-07,
      Workplace Plus — the first genuinely multi-building LLM-fallback
      .eml to ever exercise this tier): the ORIGINAL strategy here —
      anchoring on a building's own name appearing in a link's visible
      text — completely failed for this source, since every link's
      visible text is generic ("Brochure", a phone number, or blank),
      never a building name at all, so every anchor search came back
      empty and nothing got enriched at all despite real photos and
      brochure links genuinely existing in the source."""
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
            if is_real_content_image(a, b) and b not in images and b not in (floorplan_url,):
                # Confirmed real (2026-07, MetSpace's own "Office Of The
                # Week" template): a source can genuinely have BOTH a real
                # content photo and a floor-plan diagram from the exact
                # same domain, with identical (or no) alt text — nothing
                # about the URL/alt text tells them apart, unlike a
                # dedicated rule's own domain-based assumption (e.g.
                # extraction.rules.metspace trusting mcusercontent.com to
                # mean "floor plan" for ITS OWN template) which doesn't
                # hold here. Only checked once floor_plan_url is still
                # unclaimed — an actual pixel fetch per image, so no
                # need to re-check once a floor plan's already been found.
                if floorplan_url is None and is_floorplan_image_url(b):
                    floorplan_url = b
                else:
                    images.append(b)
        elif kind == "link":
            if floorplan_url is None and is_floorplan_link(a):
                floorplan_url = b
            elif brochure_url is None and is_brochure_link(a, b):
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
    """Assigns one segment (_segment_by_real_image) per DISTINCT
    building, in order, to however many consecutive records share that
    building — confirmed real (2026-07, Workplace Plus): its own two
    "150 Waterloo Road" floors are two separate records but share the
    ONE photo/brochure segment that building's own listing has (this
    source has one real photo per BUILDING, not per floor), so a naive
    one-record-per-segment mapping would starve every record after the
    first repeat. Relies on the LLM having extracted records in the same
    top-to-bottom order the source itself lists them in — the same
    order-preserving assumption already relied on elsewhere
    (extraction.xlsx_links' own row consumption) — since there's no
    building-name text anywhere in html_items to match against instead
    (see this function's own caller, enrich_records, for why the
    original text-anchored strategy failed here). Stops silently,
    leaving remaining records unenriched, if there turn out to be more
    distinct buildings than segments — safer than guessing wrong."""
    segments = _segment_by_real_image(html_items)
    if not segments:
        return

    seg_idx = -1
    prev_building = None
    for record in records:
        building = record.get("Building") or ""
        if seg_idx == -1 or building != prev_building:
            seg_idx += 1
            prev_building = building
        if seg_idx >= len(segments):
            break
        images, floorplan_url, brochure_url = _collect(segments[seg_idx])
        _apply(record, images, floorplan_url, brochure_url)


def _segment_by_real_image(html_items):
    """Splits html_items into one segment per real content image
    (is_real_content_image) — each segment starts right at a real
    image's own position and runs up to (not including) the NEXT real
    image, so a "Brochure"/"Website"-style link that immediately follows
    a listing's photo falls into that same listing's segment. Everything
    before the FIRST real image (header logo, social icons, sender's own
    signature graphics) is discarded — it can never belong to any real
    listing. Returns [] if the source has no real content images at all
    (nothing to segment by)."""
    boundaries = [i for i, (kind, a, b) in enumerate(html_items) if kind == "image" and is_real_content_image(a, b)]
    if not boundaries:
        return []
    return [
        html_items[start : boundaries[idx + 1] if idx + 1 < len(boundaries) else len(html_items)]
        for idx, start in enumerate(boundaries)
    ]
