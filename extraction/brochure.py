"""Provider-neutral brochure extraction and confidence-aware enrichment.

Brochures are secondary evidence.  Failure is isolated per brochure and a
strong primary value is never silently replaced.
"""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import gc
import hashlib
import ipaddress
import json
from io import BytesIO
import os
import re
import threading
import time
from typing import Callable, Iterable, List
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

from .assets import classify_candidate, classify_candidates, is_blank_or_empty_image, normalize_url
from .address import extract_postcode
from .html_images import is_image_like_url
from .identity import IdentityDecision, compare_property_identity, property_key
from .models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
    BrochureResource,
    ExtractedValue,
    FieldProvenance,
    LinkDiagnostic,
    Property,
    Severity,
    ValidationIssue,
)
from .text_utils import cap_special_features

# Raised from 20MB after confirming real UNION Box shared brochures arrive
# as ~22MB PDFs via app.box.com/shared/static/{id}.pdf — the previous
# hard cap skipped those downloads and left High Res blank despite a
# real public PDF existing behind the "CLICK HERE" cell.
MAX_BROCHURE_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 6
PRIMARY_STRONG_CONFIDENCE = 0.8
BROCHURE_RELIABLE_CONFIDENCE = 0.7


def _env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value > 0 else default


def _detect_host_ram_mb() -> float:
    """Best-effort container/host RAM limit in MiB (cgroup, then psutil)."""
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
            if not raw or raw == "max":
                continue
            value = int(raw)
            # cgroup v1 "unlimited" sentinel is a huge number.
            if 0 < value < (1 << 62):
                return value / (1024 * 1024)
        except Exception:
            continue
    try:
        import psutil

        total = psutil.virtual_memory().total
        if total > 0:
            return total / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def _default_fetch_workers(host_ram_mb: float) -> int:
    # Confirmed real (Railway GPE, 2026-07): 6 parallel workers + nested
    # landlord PDFs SIGKILL'd a ~512MB–1GB worker. Scale with container RAM.
    if host_ram_mb >= 4000:
        return 4
    if host_ram_mb >= 1800:
        return 2
    return 1


def _default_parallel_rss_mb(host_ram_mb: float) -> float:
    if host_ram_mb >= 4000:
        return 2400.0
    if host_ram_mb >= 1800:
        return max(250.0, host_ram_mb * 0.35)
    if host_ram_mb > 0:
        # 512MB–1.5GB Railway: serialize before the first large Box PDF.
        return max(120.0, host_ram_mb * 0.28)
    # Unknown host → assume ~1GB Railway, serialize early.
    return 180.0


def _default_rss_ceiling_mb(host_ram_mb: float) -> float:
    if host_ram_mb >= 6000:
        return 6144.0
    if host_ram_mb >= 1800:
        return max(400.0, host_ram_mb * 0.72)
    if host_ram_mb > 0:
        # Leave headroom before the container SIGKILL line on ~1GB Railway.
        # Confirmed real (2026-07, UNION): full Box PDF decode after
        # rule:UNION at ~100 MiB still jumped past a high ceiling and died.
        return max(260.0, host_ram_mb * 0.52)
    # Unknown host → stop well before a typical 1GB hard-kill.
    return 420.0


# Serialize pdfplumber/fitz decodes whenever RSS is under 1.5GB (typical
# Railway) so two ~22MB UNION Box PDFs never decode at once.
_PDF_DECODE_LOCK = threading.Lock()
_PDF_DECODE_SERIALIZE_BELOW_RSS_MB = 1536.0


_HOST_RAM_MB = _detect_host_ram_mb()
# Memory-aware defaults; override with ENRICHMENT_FETCH_WORKERS /
# ENRICHMENT_PARALLEL_RSS_MB / ENRICHMENT_RSS_CEILING_MB.
_ENRICHMENT_FETCH_WORKERS = _env_positive_int(
    "ENRICHMENT_FETCH_WORKERS", _default_fetch_workers(_HOST_RAM_MB)
)
_ENRICHMENT_PARALLEL_RSS_MB = _env_positive_float(
    "ENRICHMENT_PARALLEL_RSS_MB", _default_parallel_rss_mb(_HOST_RAM_MB)
)
# Soft caps on embedded bitmaps retained per brochure PDF. Matches
# app.MAX_HIGH_RES_IMAGES so RSS does not hold uncapped MetSpace Drive
# embeds until materialise. Floor plans capped separately.
_SOFT_MAX_EMBEDDED_PHOTOS = 8
_SOFT_MAX_EMBEDDED_FLOORPLANS = 4
_MIN_HIGH_RES_TARGET = 5
# Light extract under extreme deadline pressure only: a few first-page
# photos beat blank High Res. Full galleries run whenever budget/RSS allow.
_LIGHT_MAX_PHOTOS = 3
_LIGHT_MAX_PAGES = 5
# Skip *optional* nested PDFs on property HTML pages that already expose a
# confident photo gallery (Knotel/GPE). Never used to skip Drive/Box/Dropbox
# download targets — those viewer shells often have ≥2 chrome images and
# are the ONLY photo/floorplan source for MetSpace / Workplace Plus.
_NESTED_PDF_SKIP_WHEN_PAGE_PHOTOS = 3
# Soft stop before container OOM — scaled to detected RAM, not a fixed 6GiB
# that never trips on a 512MB–1GB Railway service.
_RSS_ENRICHMENT_CEILING_MB = _env_positive_float(
    "ENRICHMENT_RSS_CEILING_MB", _default_rss_ceiling_mb(_HOST_RAM_MB)
)
# When many unique hosted PDFs remain, stop deepening already-complete
# galleries so blank High Res rows still get a fetch attempt.
_SKIP_COMPLETE_WHEN_UNIQUE_GE = 24
# Only force light first-page extracts when a huge unique-URL backlog remains
# *and* little wall-clock is left — prefer full embeds on paid Railway.
_FORCE_LIGHT_UNIQUE_REMAINING = 80
_HTML_ONLY_REMAINING_SECONDS = 12.0
_LIGHT_PDF_REMAINING_SECONDS = 35.0
_SKIP_WAVE_REMAINING_SECONDS = 5.0
# Box/Drive static PDFs are often 15-25MB; allow a long transfer when the
# enrichment deadline still has headroom.
_HOSTED_PDF_FETCH_TIMEOUT_SECONDS = 60.0

_SECTION_FIELDS = {
    "description": "Special Features",
    "specification": "Special Features",
    "amenities": "Special Features",
    "features": "Special Features",
    "sustainability": "Special Features",
    "epc": "Special Features",
    "lease terms": "Min. Term",
    "minimum term": "Min. Term",
    "min term": "Min. Term",
    "term": "Min. Term",
    # Availability notes describe state of space, never Floor/Unit identity.
    "availability": "State of Space",
    "available": "State of Space",
    "state of space": "State of Space",
    "condition": "State of Space",
    "contacts": "Contacts",
    "contact": "Contacts",
    "get in touch": "Contacts",
    "enquire": "Contacts",
    "enquiries": "Contacts",
    "service charge": "Special Features",
    "business rates": "Special Features",
    "rates": "Special Features",
    "rent": "Special Features",
    "pricing": "Special Features",
}
_HEADING_RE = re.compile(
    r"^(description|specification|amenities|features|sustainability|epc|"
    r"lease terms|minimum term|min term|term|availability|available|"
    r"state of space|condition|contacts|contact|get in touch|enquire|enquiries|"
    r"service charge|business rates|rates|rent|pricing)\s*:?(.*)$",
    re.I,
)
_SIZE_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)\b", re.I)
_DESKS_RE = re.compile(
    r"\b(?:up\s+to\s+)?(\d+)\s*(?:-|–|to)\s*(\d+)\s*desks?\b|\b(?:up\s+to\s+)?(\d+)\s*desks?\b",
    re.I,
)
_MIN_TERM_RE = re.compile(
    r"(?:min(?:imum)?\.?\s*term|lease\s*term|term(?:\s+certain)?)"
    r"[:\s]+(?:from\s+)?(\d+\s*(?:months?|years?|yrs?))|"
    r"\b(\d+)\s*(months?|years?|yrs?)\s*(?:min(?:imum)?(?:\s+term)?|term)\b|"
    r"\b(?:from\s+)?(\d+\s*(?:months?|years?|yrs?))\s*(?:minimum|min\.?\s*term)\b",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(
    r"(?:\+44\s?\(?0?\)?[\d\s()-]{8,}|0\d[\d\s()-]{8,}|\+\d[\d\s()-]{8,})",
)
_CONTACT_NAME_RE = re.compile(
    r"(?:contact|enquire|enquir(?:y|ies)|speak\s+to|get\s+in\s+touch(?:\s+with)?)"
    r"[:\s]+([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})",
    re.I,
)
_FLOOR_TOKEN_RE = re.compile(
    r"\b(?:(?P<nth>\d+)(?:st|nd|rd|th)|(?P<label>ground|basement|lower\s+ground|mezzanine|lg|g))"
    r"(?:\s*(?:floor|fl|unit))?\b",
    re.I,
)
_FLOOR_NEAR_SIZE_RE = re.compile(
    r"(?P<floor>(?:\d+(?:st|nd|rd|th)|ground|basement|lower\s+ground|mezzanine|lg)\s*(?:floor|fl|unit)?)"
    r".{0,120}?"
    r"(?P<size>[\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)"
    r"|"
    r"(?P<size2>[\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)"
    r".{0,120}?"
    r"(?P<floor2>(?:\d+(?:st|nd|rd|th)|ground|basement|lower\s+ground|mezzanine|lg)\s*(?:floor|fl|unit)?)",
    re.I | re.S,
)
_FLOOR_NEAR_DESKS_RE = re.compile(
    r"(?P<floor>(?:\d+(?:st|nd|rd|th)|ground|basement|lower\s+ground|mezzanine|lg)\s*(?:floor|fl|unit)?)"
    r".{0,120}?"
    r"(?:up\s+to\s+)?(?:(?P<d1>\d+)\s*(?:-|–|to)\s*(?P<d2>\d+)|(?P<d3>\d+))\s*desks?"
    r"|"
    r"(?:up\s+to\s+)?(?:(?P<d4>\d+)\s*(?:-|–|to)\s*(?P<d5>\d+)|(?P<d6>\d+))\s*desks?"
    r".{0,120}?"
    r"(?P<floor2>(?:\d+(?:st|nd|rd|th)|ground|basement|lower\s+ground|mezzanine|lg)\s*(?:floor|fl|unit)?)",
    re.I | re.S,
)
_PRICE_PCM_RE = re.compile(
    r"(?:£|gbp)\s*([\d,]+(?:\.\d+)?)\s*(?:pcm|per\s*month|/month|p\.?m\.?)\b",
    re.I,
)
_PRICE_PSF_RE = re.compile(
    r"(?:£|gbp)\s*([\d,]+(?:\.\d+)?)\s*(?:psf|per\s*sq\.?\s*ft|/sq\.?\s*ft)\b",
    re.I,
)
# Building-level brochure fills (safe when identity matched).
_SAFE_BROCHURE_FIELDS = {
    "Special Features",
    "Contacts",
    "Min. Term",
    "State of Space",
    "Property Postcode",
}
# Require same floor/unit evidence before applying to a row.
_UNIT_SPECIFIC_FIELDS = {
    "Size (sq ft)",
    "Desks (max)",
    "Marketing Price (Based on Min Term) PCM",
    "Marketing Price (Based on Min Term) PSF",
}
# Never take address wording from a brochure — primary file owns identity text.
_ADDRESS_LOCKED_FIELDS = {
    "Building",
    "Property Address 1",
    "Property Address 2",
    "Floor/Unit",
}


class LinkedResourceError(Exception):
    def __init__(self, message, status="LINK_ENRICHMENT_FAILED", final_url=None):
        super().__init__(message)
        self.status = status
        self.final_url = final_url


def fetch_brochure(url: str, timeout: float = 6.0, deadline: float = None) -> BrochureResource:
    current = url
    redirects = []
    # Confirmed real (2026-07, UNION): app.box.com/s/{id} is a JS viewer shell
    # with no usable photos. The same share id downloads as a public PDF at
    # /shared/static/{id}.pdf — jump straight there so enrichment does not
    # burn a round-trip (and ~20s) on the useless HTML page per listing.
    box_shared = _box_shared_name(current)
    if box_shared and "/shared/static/" not in (urlparse(current).path or ""):
        current = f"https://app.box.com/shared/static/{box_shared}.pdf"
        redirects.append(current)
    embedded_target = _embedded_http_target(url)
    if embedded_target and normalize_url(embedded_target) != normalize_url(url):
        current = embedded_target
        redirects.append(embedded_target)
    for _ in range(MAX_REDIRECTS + 1):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                raise LinkedResourceError(
                    "Linked enrichment skipped to preserve the request deadline",
                    "LINK_ENRICHMENT_SKIPPED", current,
                )
            request_timeout = min(timeout, remaining)
        else:
            request_timeout = timeout
        _validate_remote_url(current)
        try:
            # Box landlord PDFs are often 15-25MB; give the static PDF path a
            # longer per-request timeout than a normal HTML property page.
            if "/shared/static/" in (urlparse(current).path or "") or _box_shared_name(current):
                if deadline is not None:
                    # Cap under the remaining enrichment window so a late
                    # UNION wave cannot overrun batch_deadline and erase
                    # earlier files' galleries at finalize time.
                    effective_timeout = min(
                        _HOSTED_PDF_FETCH_TIMEOUT_SECONDS,
                        max(0.5, deadline - time.monotonic()),
                    )
                else:
                    effective_timeout = max(float(timeout), _HOSTED_PDF_FETCH_TIMEOUT_SECONDS)
            else:
                effective_timeout = request_timeout
            # Stream one body at a time so response.content and a second
            # join buffer never coexist for large UNION Box PDFs.
            response = requests.get(
                current,
                timeout=effective_timeout,
                headers={"User-Agent": "OfficeAvailability/1.0"},
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout as exc:
            raise LinkedResourceError("Linked resource timed out", "LINK_TIMEOUT", current) from exc
        except requests.RequestException as exc:
            raise LinkedResourceError(f"Linked resource request failed: {exc}", "LINK_ENRICHMENT_FAILED", current) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise LinkedResourceError("Linked-resource redirect did not provide a destination", "LINK_ENRICHMENT_FAILED", current)
            destination = urljoin(current, location)
            if destination in redirects or destination == current:
                raise LinkedResourceError("Linked-resource redirect loop detected", "LINK_ENRICHMENT_FAILED", current)
            redirects.append(destination)
            current = destination
            continue
        if response.status_code in {401, 403}:
            response.close()
            raise LinkedResourceError("Linked resource denied access", "LINK_ACCESS_DENIED", current)
        if response.status_code in {404, 410}:
            response.close()
            raise LinkedResourceError("Linked resource was not found", "LINK_NOT_FOUND", current)
        if response.status_code == 429:
            response.close()
            raise LinkedResourceError("Linked resource rate limited enrichment", "LINK_RATE_LIMITED", current)
        if response.status_code >= 400:
            code = response.status_code
            response.close()
            raise LinkedResourceError(f"Linked resource returned HTTP {code}", "LINK_ENRICHMENT_FAILED", current)
        break
    else:
        raise LinkedResourceError("Linked resource exceeded the redirect limit", "LINK_ENRICHMENT_FAILED", current)
    content_type = response.headers.get("Content-Type", "")
    final_url = response.url or current
    chunks = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_BROCHURE_BYTES:
                raise LinkedResourceError("Linked resource exceeds the 30MB enrichment limit", "LINK_ENRICHMENT_SKIPPED", current)
            chunks.append(chunk)
        payload = b"".join(chunks)
    finally:
        response.close()
        chunks.clear()
        del chunks
    if len(payload) > MAX_BROCHURE_BYTES:
        raise LinkedResourceError("Linked resource exceeds the 30MB enrichment limit", "LINK_ENRICHMENT_SKIPPED", current)
    return BrochureResource(payload, content_type, final_url, url, tuple(redirects))



def _embedded_http_target(url: str) -> str:
    """Resolve an explicit public HTTP(S) destination embedded by trackers.

    Some marketing platforms return a JavaScript-only shell instead of an HTTP
    redirect while carrying the real destination in a JSON query value. Only
    explicit URL-shaped values/TargetUrl keys are accepted; the result still
    passes the normal public-host and redirect safety checks before fetching.
    """
    def decode(value):
        previous = str(value or "")
        for _ in range(4):
            current = unquote(previous)
            if current == previous:
                break
            previous = current
        return previous

    def find_target(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in {"targeturl", "target_url", "destination", "redirecturl"}:
                    candidate = decode(nested)
                    if normalize_url(candidate):
                        return candidate
                found = find_target(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = find_target(nested)
                if found:
                    return found
        return ""

    for values in parse_qs(urlparse(url).query).values():
        for raw in values:
            decoded = decode(raw)
            if normalize_url(decoded):
                return decoded
            try:
                structured = json.loads(decoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            target = find_target(structured)
            if target:
                return target
    return ""

def _validate_remote_url(url):
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        raise LinkedResourceError("Linked destination is not a public HTTP(S) host", "LINK_UNSUPPORTED", url)
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise LinkedResourceError("Linked destination is not a public HTTP(S) host", "LINK_UNSUPPORTED", url)


def extract_brochure(
    payload: bytes,
    content_type: str,
    source_document: str,
    *,
    max_photos: int = None,
    stop_after_floorplans: int = None,
    prefer_photos: bool = False,
    max_pages: int = None,
) -> BrochureExtraction:
    """Best-effort extraction based on actual response content, not suffix.

    max_photos / stop_after_floorplans let callers that already have an HTML
    gallery (GPE property pages) pull only a floor-plan bitmap from a nested
    landlord PDF without decoding every marketing photo page.

    prefer_photos / max_pages are the light path for high unique-URL batches
    (UNION Box): get at least one High Res bitmap quickly when Floor Plan is
    already seeded with a viewer URL and full-gallery decode would blow the
    deadline.
    """
    resource_type = _resource_type(payload, content_type)
    if resource_type == "html":
        return _extract_html(payload, source_document)
    if resource_type == "image":
        return _extract_direct_image(payload, content_type, source_document)
    if resource_type != "pdf":
        raise LinkedResourceError("Linked resource type is unsupported", "LINK_UNSUPPORTED", source_document)

    def _decode_pdf() -> BrochureExtraction:
        import pdfplumber

        text_parts = []
        links = []
        page_limit = None if max_pages is None else max(1, int(max_pages))
        with pdfplumber.open(BytesIO(payload)) as pdf:
            pages = pdf.pages if page_limit is None else pdf.pages[:page_limit]
            for page_number, page in enumerate(pages, start=1):
                text_parts.append(page.extract_text() or "")
                for annotation in page.annots or []:
                    uri = (annotation.get("data") or {}).get("URI") or annotation.get("uri")
                    if uri:
                        links.append(AssetCandidate(uri, source_document, page_number=page_number))
        text = "\n".join(text_parts)
        fields = _extract_fields(text, source_document)
        return BrochureExtraction(
            source_document,
            fields,
            classify_candidates(links)
            + _extract_pdf_visuals(
                payload,
                source_document,
                text_parts,
                max_photos=max_photos,
                stop_after_floorplans=stop_after_floorplans,
                prefer_photos=prefer_photos,
                max_pages=max_pages,
            ),
            identity_text=text,
        )

    # Hard cap concurrent PDF decodes to 1 under 1.5GB RSS / small hosts.
    if _pdf_decode_must_serialize():
        with _PDF_DECODE_LOCK:
            return _decode_pdf()
    return _decode_pdf()


def _resource_type(payload: bytes, content_type: str) -> str:
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    head = payload[:512].lstrip().lower()
    if payload.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"<!doctype html", b"<html")) or b"<html" in head or kind in {"text/html", "application/xhtml+xml"}:
        return "html"
    if kind.startswith("image/") or payload.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF")):
        return "image"
    return "unsupported"


def _extract_direct_image(payload: bytes, content_type: str, source_document: str) -> BrochureExtraction:
    try:
        from PIL import Image
        from . import pdf_images
        with Image.open(BytesIO(payload)) as bitmap:
            detected_format = (bitmap.format or "").lower()
            bitmap.verify()
    except Exception as exc:
        raise LinkedResourceError("Linked image is corrupt or unsupported", "LINK_UNSUPPORTED", source_document) from exc
    detected_mime = "image/jpeg" if detected_format in {"jpg", "jpeg"} else f"image/{detected_format or 'unknown'}"
    candidate = classify_candidate(AssetCandidate(source_document, source_document, mime_type=detected_mime, original_url=source_document, final_url=source_document, filename=urlparse(source_document).path.rsplit("/", 1)[-1], discovery_method="direct_resource", association_confidence=1.0, content_hash=hashlib.sha256(payload).hexdigest()))
    if candidate.classification == AssetType.PROPERTY_IMAGE and pdf_images.is_floorplan_image(payload):
        candidate.classification = AssetType.FLOORPLAN
        candidate.confidence = 0.9
    return BrochureExtraction(source_document, assets=[candidate])


def _extract_html(payload: bytes, source_document: str) -> BrochureExtraction:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(payload, "lxml")
    # Resolve Drive/docs download targets BEFORE stripping <script> — the
    # public PDF mime hint for MetSpace-style Drive viewers lives there.
    hosted_documents = _hosted_document_candidates(soup, source_document, raw_html=payload)
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    candidates = []
    content_root = soup.find("main") or soup.find("article") or soup.body or soup
    semantic_property_root = getattr(content_root, "name", None) in {"main", "article"}
    for image in content_root.find_all("img"):
        # Marketing platforms commonly keep the real image in a lazy-load
        # attribute or expose several responsive variants in srcset.  Keep
        # every distinct full asset URL; the shared asset layer performs
        # exact URL/content deduplication later.
        image_urls = []
        for attribute in ("src", "data-src", "data-lazy-src", "data-original", "data-image"):
            if image.get(attribute):
                image_urls.append(image.get(attribute))
        for attribute in ("srcset", "data-srcset", "data-lazy-srcset"):
            image_urls.extend(_srcset_urls(image.get(attribute) or ""))
        for raw_url in image_urls:
            src = urljoin(source_document, raw_url)
            if src:
                container = image.find_parent(["article", "section", "figure", "li", "div"])
                context = container.get_text(" ", strip=True)[:600] if container else ""
                candidates.append(AssetCandidate(src, source_document, mime_type="image/*", original_url=raw_url, final_url=src, alt_text=image.get("alt"), filename=urlparse(src).path.rsplit("/", 1)[-1], surrounding_text=context, html_container=getattr(container, "name", None), discovery_method="html_img", association_confidence=0.85 if semantic_property_root or (container and container.name in {"article", "section", "figure"}) else 0.55))
    for source in content_root.find_all("source"):
        for raw_url in _srcset_urls(source.get("srcset") or source.get("data-srcset") or ""):
            src = urljoin(source_document, raw_url)
            candidates.append(AssetCandidate(src, source_document, mime_type=source.get("type") or "image/*", original_url=raw_url, final_url=src, filename=urlparse(src).path.rsplit("/", 1)[-1], discovery_method="html_srcset", association_confidence=0.85 if semantic_property_root else 0.7))
    for meta in soup.find_all("meta"):
        if (meta.get("property") or meta.get("name") or "").lower() in {"og:image", "twitter:image", "twitter:image:src"}:
            src = urljoin(source_document, meta.get("content") or "")
            if src:
                candidates.append(AssetCandidate(src, source_document, mime_type="image/*", original_url=meta.get("content"), final_url=src, anchor_text="page preview image", filename=urlparse(src).path.rsplit("/", 1)[-1], discovery_method="html_metadata"))
    if hosted_documents:
        # Viewer thumbnails are document previews, not independent property
        # photographs. The downloaded document below supplies the real,
        # hashable page assets and avoids duplicating its cover/first page.
        for candidate in candidates:
            if candidate.anchor_text == "page preview image":
                candidate.classification = AssetType.DECORATIVE
                candidate.confidence = 0.95
    candidates.extend(hosted_documents)
    for link in content_root.find_all("a", href=True):
        href = urljoin(source_document, link.get("href"))
        container = link.find_parent(["article", "section", "figure", "li", "div"])
        context = container.get_text(" ", strip=True)[:600] if container else ""
        candidates.append(AssetCandidate(href, source_document, original_url=link.get("href"), final_url=href, anchor_text=link.get_text(" ", strip=True), filename=urlparse(href).path.rsplit("/", 1)[-1], surrounding_text=context, html_container=getattr(container, "name", None), discovery_method="html_link", association_confidence=0.85 if container and container.name in {"article", "section", "figure"} else 0.55))
        # Download/navigation labels are asset metadata, not property
        # description text (e.g. "Download brochure" under Amenities).
        link.decompose()
    text = soup.get_text("\n", strip=True)
    extraction = BrochureExtraction(source_document, _extract_fields(text, source_document), classify_candidates(candidates), identity_text=text)
    visible = " ".join(text.split()).lower()
    if not visible and not extraction.assets:
        raise LinkedResourceError("Linked HTML page requires JavaScript or contains no usable content", "LINK_ENRICHMENT_SKIPPED", source_document)
    if any(token in visible[:1500] for token in ("sign in to continue", "log in to continue", "access denied", "enable javascript")) and not any(a.classification in {AssetType.BROCHURE, AssetType.PROPERTY_IMAGE, AssetType.FLOORPLAN} for a in extraction.assets):
        raise LinkedResourceError("Linked HTML page is inaccessible or requires login/JavaScript", "LINK_ACCESS_DENIED", source_document)
    return extraction


def _srcset_urls(value: str) -> List[str]:
    """Return every URL from an HTML srcset without choosing one rendition."""
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


def _is_box_host(host: str) -> bool:
    host = (host or "").lower()
    if "dropbox" in host:
        return False
    return host == "box.com" or host.endswith(".box.com")


def _is_viewer_floorplan_url(url: str) -> bool:
    """True when Floor Plan still points at a document/viewer rather than a bitmap.

    Box/Drive/Dropbox/.pdf placeholders are temporary enrichment seeds only;
    they must not count as a finished Floor Plan cell value.
    """
    text = str(url or "").strip()
    if not text or "/api/download/" in text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if _is_box_host(host):
        return True
    if host in {"drive.google.com", "docs.google.com"} or host.endswith("drive.usercontent.google.com"):
        return True
    if "dropbox.com" in host or host.endswith("dropboxusercontent.com"):
        return True
    if path.endswith(".pdf") or "export=download" in query:
        return True
    if any(token in host for token in ("canva.com", "canva.link", "pitch.com")):
        return True
    return False


def _hosted_document_candidates(soup, source_document: str, raw_html: bytes = None) -> List[AssetCandidate]:
    """Resolve public document-viewer pages to their downloadable document.

    This is based on the hosting platform, never the property provider.  A
    Google Drive viewer deliberately exposes only a single preview bitmap in
    its HTML; the actual public PDF is required for multi-page media discovery.

    Confirmed real (2026-07, MetSpace Mailchimp → Drive): listing titles are
    like "9-10 Market Place - 2nd Floor - Google Drive" with NO ".pdf" in the
    title, and the `"docs-dm":"application/pdf"` hint lives only inside a
    <script> block. _extract_html strips script/style before this runs, so the
    old title/docs-dm gate silently returned no download candidate and High
    Res stayed blank despite a public PDF existing. Always expose the
    usercontent download URL for /file/d/{id} viewers; nested retrieve then
    keeps only real PDF/image payloads.

    Confirmed real (2026-07, UNION Box "CLICK HERE" cells): app.box.com/s/{id}
    returns a JS shell with no usable images, but the same share id downloads
    as a real PDF from app.box.com/shared/static/{id}.pdf. Without that
    rewrite, brochure enrichment "succeeds" on decorative Box chrome and
    High Res stays blank even though a public brochure PDF exists.
    """
    parsed = urlparse(source_document)
    host = (parsed.hostname or "").lower()
    if host in {"drive.google.com", "docs.google.com"}:
        match = re.search(r"/file/d/([\w-]+)", parsed.path)
        if not match:
            return []
        file_id = match.group(1)
        url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
        return [AssetCandidate(url, source_document, mime_type="application/pdf", filename=f"{file_id}.pdf", anchor_text="Download brochure")]
    if "dropbox.com" in host or host.endswith("dropboxusercontent.com"):
        # Public Dropbox share → direct download (?dl=1) for nested retrieve.
        if "/s/" in (parsed.path or "") or "/scl/" in (parsed.path or "") or host.endswith("dropboxusercontent.com"):
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query["dl"] = "1"
            url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), ""))
            return [AssetCandidate(url, source_document, mime_type="application/pdf", filename="dropbox-brochure.pdf", anchor_text="Download brochure")]
    if _is_box_host(host):
        shared = _box_shared_name(source_document)
        if not shared:
            return []
        url = f"https://app.box.com/shared/static/{shared}.pdf"
        return [AssetCandidate(url, source_document, mime_type="application/pdf", filename=f"{shared}.pdf", anchor_text="Download brochure")]
    return []


def _box_shared_name(url: str) -> str:
    parsed = urlparse(url)
    if not _is_box_host(parsed.hostname or ""):
        return ""
    match = re.search(r"/s/([A-Za-z0-9]+)", parsed.path or "")
    return match.group(1) if match else ""


def _is_hosted_document_download(url: str) -> bool:
    """True for Drive/Box/Dropbox downloads that must be followed even when
    the viewer HTML already shows chrome images.

    Plain `.pdf` hrefs on property sites are optional (Knotel) and may be
    skipped when the HTML page already has a photo gallery.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host.endswith("drive.usercontent.google.com") or host in {"drive.google.com", "docs.google.com"}:
        return True
    if "dropbox.com" in host or host.endswith("dropboxusercontent.com"):
        return True
    if _is_box_host(host):
        return True
    return False


def _extract_pdf_visuals(
    payload: bytes,
    source_document: str,
    pages_text: List[str],
    *,
    max_photos: int = None,
    stop_after_floorplans: int = None,
    prefer_photos: bool = False,
    max_pages: int = None,
) -> List[AssetCandidate]:
    """Extract embedded PDF visuals conservatively for later hosting."""
    try:
        import fitz
        from PIL import Image
        from . import pdf_images
    except ImportError:
        return []
    try:
        document = fitz.open(stream=payload, filetype="pdf")
    except Exception:
        return []
    extracted = []
    counts = Counter()
    seen_digests = set()
    photo_kept = 0
    floorplan_kept = 0
    # Soft-cap photos tighter for large Box/Drive payloads on small hosts.
    photo_cap = _SOFT_MAX_EMBEDDED_PHOTOS if max_photos is None else max(0, int(max_photos))
    floorplan_cap = (
        _SOFT_MAX_EMBEDDED_FLOORPLANS
        if stop_after_floorplans is None
        else max(0, int(stop_after_floorplans))
    )
    if len(payload) >= 8 * 1024 * 1024 or _is_hosted_document_download(source_document):
        if max_photos is None:
            photo_cap = min(photo_cap, 5)
    page_cap = len(document) if max_pages is None else min(len(document), max(1, int(max_pages)))
    # Scan pages so floor plans later in UNION/Workplace Plus brochures
    # are not skipped after early photo pages (unless light max_pages).
    # Exact content hashes prevent the same bitmap being kept twice;
    # blank/near-solid slides are dropped. Soft-cap property-photo and
    # floor-plan bitmaps (RSS bound).
    try:
        rss_tight = False
        for page_number in range(page_cap):
            page = document[page_number]
            if stop_after_floorplans is not None and floorplan_kept >= floorplan_cap and photo_kept >= photo_cap:
                break
            if photo_kept >= photo_cap and (prefer_photos or floorplan_kept >= floorplan_cap):
                break
            if _rss_mb() >= _RSS_ENRICHMENT_CEILING_MB:
                rss_tight = True
            for image in page.get_images(full=True):
                try:
                    base = document.extract_image(image[0])
                    content = base.get("image") or b""
                    if len(content) < pdf_images.MIN_IMAGE_BYTES:
                        continue
                    digest = hashlib.sha256(content).hexdigest()
                    counts[digest] += 1
                    if digest in seen_digests:
                        continue
                    seen_digests.add(digest)
                    with Image.open(BytesIO(content)) as bitmap:
                        width, height = bitmap.size
                    page_text = pages_text[page_number] if page_number < len(pages_text) else ""
                    is_floorplan = pdf_images.is_floorplan_page(page_text) or pdf_images.is_floorplan_image(content)
                    # Near-solid placeholder slides (confirmed MetSpace Drive
                    # PDFs) must never become PROPERTY_IMAGE later via
                    # classify_candidates' association_confidence path.
                    if is_floorplan:
                        if floorplan_kept >= floorplan_cap:
                            continue
                        # When Floor Plan is already seeded (viewer URL) or
                        # RSS is tight, prefer High Res photos over another
                        # plan bitmap — blank High Res is the user-visible gap.
                        if prefer_photos and photo_kept < max(1, min(photo_cap, _LIGHT_MAX_PHOTOS)):
                            continue
                        if rss_tight and prefer_photos:
                            continue
                        if rss_tight and not prefer_photos and photo_kept < 1:
                            # Still missing High Res — skip more plans.
                            continue
                        classification = AssetType.FLOORPLAN
                        floorplan_kept += 1
                    elif width < 300 or height < 200 or is_blank_or_empty_image(content):
                        classification = AssetType.DECORATIVE
                        # Drop bytes immediately — merge never hosts decorative.
                        content = None
                    else:
                        if photo_kept >= photo_cap:
                            continue
                        if rss_tight and photo_kept >= max(1, min(photo_cap, _LIGHT_MAX_PHOTOS)):
                            continue
                        # PROPERTY_IMAGE directly — leaving UNKNOWN let
                        # classify_candidates promote near-white floor-plan
                        # diagrams that barely missed FLOORPLAN_WHITE_FRACTION
                        # into High Res (confirmed MetSpace Drive galleries).
                        classification = AssetType.PROPERTY_IMAGE
                        photo_kept += 1
                    extracted.append(
                        AssetCandidate(
                            "", source_document, mime_type=f"image/{base.get('ext', 'png')}",
                            filename=f"asset-p{page_number + 1}-{digest[:10]}.{base.get('ext', 'png')}",
                            page_number=page_number + 1, classification=classification,
                            confidence=(
                                0.9 if classification == AssetType.FLOORPLAN
                                else 0.86 if classification == AssetType.DECORATIVE
                                else 0.82
                            ),
                            surrounding_text=page_text[:800], discovery_method="pdf_embedded_image",
                            association_confidence=0.85 if classification == AssetType.PROPERTY_IMAGE else 0.0,
                            width=width, height=height, content=content, content_hash=digest,
                            extension=base.get("ext", "png"),
                        )
                    )
                except Exception:
                    continue
        # Soft first-page render when embeds yielded no property photo —
        # cheaper than leaving High Res blank for text-heavy landlord PDFs.
        if photo_kept < 1 and photo_cap > 0 and len(document) > 0:
            try:
                page = document[0]
                # ~108 dpi — enough for a usable High Res link without a
                # full marketing-gallery decode spike.
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                content = pix.tobytes("jpeg")
                if len(content) >= pdf_images.MIN_IMAGE_BYTES and not is_blank_or_empty_image(content):
                    digest = hashlib.sha256(content).hexdigest()
                    if digest not in seen_digests:
                        seen_digests.add(digest)
                        counts[digest] += 1
                        page_text = pages_text[0] if pages_text else ""
                        is_floorplan = (
                            pdf_images.is_floorplan_page(page_text)
                            or pdf_images.is_floorplan_image(content)
                        )
                        if is_floorplan and floorplan_kept < floorplan_cap and not prefer_photos:
                            classification = AssetType.FLOORPLAN
                            floorplan_kept += 1
                        elif not is_floorplan:
                            classification = AssetType.PROPERTY_IMAGE
                            photo_kept += 1
                        else:
                            classification = None
                        if classification is not None:
                            extracted.append(
                                AssetCandidate(
                                    "", source_document, mime_type="image/jpeg",
                                    filename=f"asset-p1-render-{digest[:10]}.jpg",
                                    page_number=1, classification=classification,
                                    confidence=0.78 if classification == AssetType.PROPERTY_IMAGE else 0.88,
                                    surrounding_text=page_text[:800],
                                    discovery_method="pdf_page_render",
                                    association_confidence=0.8 if classification == AssetType.PROPERTY_IMAGE else 0.0,
                                    width=pix.width, height=pix.height, content=content,
                                    content_hash=digest, extension="jpg",
                                )
                            )
            except Exception:
                pass
    finally:
        document.close()
        try:
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass
    for candidate in extracted:
        candidate.occurrence_count = counts[candidate.content_hash]
    return classify_candidates(extracted)


def _extract_fields(text: str, source_document: str):
    """Deterministic building-level fields from brochure HTML/PDF text.

    Size / desks / prices are resolved later in `_merge` against the row's
    Floor/Unit so multi-unit brochures cannot copy one floor's numbers onto
    another. Address identity fields are never emitted here.
    """
    fields = {}
    sections = {}
    current = None
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        match = _HEADING_RE.match(line)
        if match:
            current = match.group(1).lower()
            if match.group(2).strip():
                sections.setdefault(current, []).append(match.group(2).strip())
        elif current and len(line) < 500:
            sections.setdefault(current, []).append(line)
    feature_parts = []
    for heading, parts in sections.items():
        value = _dedupe_text("; ".join(parts))
        field = _SECTION_FIELDS[heading]
        if field == "Special Features":
            feature_parts.append(value)
        elif field == "Min. Term" and value:
            term = _normalize_min_term(value) or value
            fields[field] = _evidence(term, source_document, 0.72)
        elif field == "Contacts" and value:
            contact = _normalize_contact_text(value)
            if contact:
                fields[field] = _evidence(contact, source_document, 0.72)
        elif field == "State of Space" and value:
            fields[field] = _evidence(value, source_document, 0.72)
        elif value and field not in _ADDRESS_LOCKED_FIELDS:
            fields[field] = _evidence(value, source_document, 0.72)
    if feature_parts:
        features = cap_special_features(_dedupe_text("; ".join(feature_parts)))
        fields["Special Features"] = _evidence(features, source_document, 0.74)

    if "Min. Term" not in fields:
        term = _find_min_term(text)
        if term:
            fields["Min. Term"] = _evidence(term, source_document, 0.78)
    if "Contacts" not in fields:
        contact = _find_contacts(text)
        if contact:
            fields["Contacts"] = _evidence(contact, source_document, 0.76)
    if "State of Space" not in fields:
        state = _find_state_of_space(text)
        if state:
            fields["State of Space"] = _evidence(state, source_document, 0.74)

    postcodes = sorted(set(filter(None, (extract_postcode(line) for line in text.splitlines()))))
    # One brochure belongs to one Property at this stage.  A single
    # unambiguous postcode is reliable secondary evidence; multiple
    # postcodes are deliberately left unresolved for conflict review.
    if len(postcodes) == 1:
        fields["Property Postcode"] = _evidence(postcodes[0], source_document, 0.84)
    # Drop anything that must never come from a brochure.
    for locked in _ADDRESS_LOCKED_FIELDS:
        fields.pop(locked, None)
    return fields


def _evidence(value, source_document, confidence):
    return ExtractedValue(value, "brochure", source_document, "deterministic:brochure", confidence)


def _floor_token(value: str) -> str:
    """Normalize '7th' / '7th Floor' / 'Ground' to a stable comparison token."""
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return ""
    match = _FLOOR_TOKEN_RE.search(text)
    if not match:
        return re.sub(r"[^a-z0-9]+", "", text)
    if match.group("nth"):
        return match.group("nth")
    label = (match.group("label") or "").lower()
    label = re.sub(r"\s+", " ", label)
    if label in {"g", "ground"}:
        return "ground"
    if label in {"lg", "lower ground"}:
        return "lower ground"
    return label


def _brochure_floor_tokens(text: str) -> set:
    return {
        _floor_token(match.group(0))
        for match in _FLOOR_TOKEN_RE.finditer(text or "")
        if _floor_token(match.group(0))
    }


def _normalize_min_term(value: str) -> str:
    match = _MIN_TERM_RE.search(value or "")
    if not match:
        loose = re.search(r"\b(\d+)\s*(months?|years?|yrs?)\b", value or "", re.I)
        if not loose:
            return ""
        return f"{loose.group(1)} {loose.group(2).lower()}"
    parts = [g for g in match.groups() if g]
    if len(parts) >= 2 and str(parts[0]).isdigit() and re.match(r"months?|years?|yrs?", str(parts[1]), re.I):
        unit = str(parts[1]).lower().replace("yrs", "years").replace("yr", "year")
        return f"{parts[0]} {unit}"
    if parts:
        cleaned = " ".join(str(parts[0]).split()).lower()
        cleaned = cleaned.replace("yrs", "years").replace("yr", "year")
        if re.match(r"^\d+\s*(months?|years?)$", cleaned):
            return cleaned
        if cleaned.isdigit():
            return cleaned
        return cleaned
    return ""


def _find_min_term(text: str) -> str:
    match = _MIN_TERM_RE.search(text or "")
    if not match:
        return ""
    return _normalize_min_term(match.group(0))


def _normalize_contact_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 400:
        return ""
    emails = _EMAIL_RE.findall(text)
    phones = [re.sub(r"\s+", " ", p).strip() for p in _PHONE_RE.findall(text)]
    name_match = _CONTACT_NAME_RE.search(text)
    name = name_match.group(1).strip() if name_match else ""
    # Prefer structured contact signals; ignore nav chrome like "Contact us".
    if not emails and not phones and not name:
        if re.search(r"@|\+?\d[\d\s()-]{7,}\d", text):
            return text
        return ""
    parts = []
    if name and name.lower() not in {"us", "me", "team", "here"}:
        parts.append(name)
    parts.extend(emails)
    for phone in phones:
        if phone not in parts:
            parts.append(phone)
    return _dedupe_text("; ".join(parts)) if parts else ""


def _find_contacts(text: str) -> str:
    emails = list(dict.fromkeys(_EMAIL_RE.findall(text or "")))
    phones = []
    for phone in _PHONE_RE.findall(text or ""):
        cleaned = re.sub(r"\s+", " ", phone).strip()
        if cleaned not in phones:
            phones.append(cleaned)
    names = []
    for match in _CONTACT_NAME_RE.finditer(text or ""):
        name = match.group(1).strip()
        if name.lower() in {"us", "me", "team", "here", "more"}:
            continue
        if name not in names:
            names.append(name)
    if not emails and not phones:
        return ""
    # A brochure with many unrelated emails is ambiguous — leave blank.
    if len(emails) > 3:
        return ""
    parts = names[:2] + emails[:2] + phones[:2]
    return _dedupe_text("; ".join(parts))


def _find_state_of_space(text: str) -> str:
    match = re.search(
        r"\b(fully\s+fitted|cat\s*[ab]\s*(?:fit\s*out|fitted)?|plug\s*(?:and|&)\s*play|"
        r"available\s+(?:now|immediately)|immediate\s+availability|vacant\s+possession|"
        r"coming\s+soon|under\s+offer)\b",
        text or "",
        re.I,
    )
    if match:
        return " ".join(match.group(0).split())
    return ""


def _parse_desks_match(match):
    if not match:
        return None
    groups = match.groupdict() if hasattr(match, "groupdict") else {}
    if groups:
        if groups.get("d2") or groups.get("d5"):
            return float(groups.get("d2") or groups.get("d5"))
        for key in ("d3", "d6", "d1", "d4"):
            if groups.get(key):
                return float(groups[key])
    if match.lastindex:
        nums = [g for g in match.groups() if g and str(g).isdigit()]
        if len(nums) >= 2:
            return float(nums[1])
        if nums:
            return float(nums[0])
    return None


def _extract_unit_fields(text: str, floor_unit: str, source_document: str) -> dict:
    """Size / desks / prices only when clearly tied to this row's floor/unit."""
    fields = {}
    text = text or ""
    floor_token = _floor_token(floor_unit)
    brochure_floors = _brochure_floor_tokens(text)
    multi_floor = len(brochure_floors) > 1

    # --- Size ---
    size_by_floor = {}
    for match in _FLOOR_NEAR_SIZE_RE.finditer(text):
        floor_raw = match.group("floor") or match.group("floor2") or ""
        size_raw = match.group("size") or match.group("size2") or ""
        token = _floor_token(floor_raw)
        if token and size_raw:
            size_by_floor[token] = float(size_raw.replace(",", ""))
    all_sizes = [float(m.group(1).replace(",", "")) for m in _SIZE_RE.finditer(text)]
    size_value = None
    size_confidence = 0.0
    if floor_token and floor_token in size_by_floor:
        size_value = size_by_floor[floor_token]
        size_confidence = 0.82
    elif not multi_floor and len(set(all_sizes)) == 1:
        # Single unambiguous size — safe when brochure isn't multi-floor, or
        # the row's floor (if any) agrees with the sole brochure floor.
        if not floor_token or not brochure_floors or floor_token in brochure_floors:
            size_value = all_sizes[0]
            size_confidence = 0.76 if not brochure_floors else 0.8
    if size_value is not None:
        fields["Size (sq ft)"] = _evidence(size_value, source_document, size_confidence)

    # --- Desks ---
    desks_by_floor = {}
    for match in _FLOOR_NEAR_DESKS_RE.finditer(text):
        floor_raw = match.group("floor") or match.group("floor2") or ""
        token = _floor_token(floor_raw)
        desks = _parse_desks_match(match)
        if token and desks is not None:
            desks_by_floor[token] = desks
    all_desk_matches = list(_DESKS_RE.finditer(text))
    all_desks = [d for d in (_parse_desks_match(m) for m in all_desk_matches) if d is not None]
    desks_value = None
    desks_confidence = 0.0
    if floor_token and floor_token in desks_by_floor:
        desks_value = desks_by_floor[floor_token]
        desks_confidence = 0.8
    elif not multi_floor and len(set(all_desks)) == 1:
        if not floor_token or not brochure_floors or floor_token in brochure_floors:
            desks_value = all_desks[0]
            desks_confidence = 0.74 if not brochure_floors else 0.78
    if desks_value is not None:
        fields["Desks (max)"] = _evidence(desks_value, source_document, desks_confidence)

    # --- Prices: only with floor agreement; never cross floors blindly ---
    pcm_matches = list(_PRICE_PCM_RE.finditer(text))
    psf_matches = list(_PRICE_PSF_RE.finditer(text))
    if floor_token and multi_floor:
        for match in pcm_matches:
            window = text[max(0, match.start() - 100) : match.end() + 100]
            window_floors = _brochure_floor_tokens(window)
            if floor_token in window_floors and len(window_floors) == 1:
                fields["Marketing Price (Based on Min Term) PCM"] = _evidence(
                    float(match.group(1).replace(",", "")), source_document, 0.75
                )
                break
        for match in psf_matches:
            window = text[max(0, match.start() - 100) : match.end() + 100]
            window_floors = _brochure_floor_tokens(window)
            if floor_token in window_floors and len(window_floors) == 1:
                fields["Marketing Price (Based on Min Term) PSF"] = _evidence(
                    float(match.group(1).replace(",", "")), source_document, 0.75
                )
                break
    elif not multi_floor and (
        not brochure_floors or not floor_token or floor_token in brochure_floors
    ):
        if len(pcm_matches) == 1:
            fields["Marketing Price (Based on Min Term) PCM"] = _evidence(
                float(pcm_matches[0].group(1).replace(",", "")), source_document, 0.72
            )
        if len(psf_matches) == 1:
            fields["Marketing Price (Based on Min Term) PSF"] = _evidence(
                float(psf_matches[0].group(1).replace(",", "")), source_document, 0.72
            )

    return fields


def _source_photo_count(prop: Property) -> int:
    values = prop.values
    candidates = list(values.get("_source_high_res_candidates") or [])
    candidates.extend(values.get("_high_res_candidates") or [])
    existing = str(values.get("High Res Images") or "").strip()
    if existing and ".html" not in existing.lower():
        candidates.insert(0, existing)
    # Brochure/PDF seed URLs are usable click-through links, not photos —
    # still count as priority-0 blanks so enrichment upgrades them.
    return len({
        str(url).split("?", 1)[0]
        for url in candidates
        if url and not _is_brochure_media_seed_url(url)
    })


def _photo_enrichment_priority(prop: Property) -> int:
    """Lower runs first. Blank High Res (MetSpace) before under-target
    galleries (Knotel bottom rows stuck at 1 featured image) before
    listings that already meet the 5-photo target."""
    count = _source_photo_count(prop)
    if count <= 0:
        return 0
    if count < _MIN_HIGH_RES_TARGET:
        return 1
    return 2


def _is_brochure_media_seed_url(url: str) -> bool:
    """True for brochure/document seeds used as High Res fallbacks.

    These keep the cell clickable when full PDF photo extraction cannot
    finish every unique URL, but they are not property photographs.
    Provider-neutral: Box/Drive/Dropbox/.pdf plus any non-image http(s)
    brochure or property page (Knotel/GPE listing pages, spreadsheet
    document links). Image-like CMS/CDN URLs stay photos, never seeds.
    """
    text = str(url or "").strip()
    if not text or "/api/download/" in text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if _is_box_host(host):
        return True
    if host in {"drive.google.com", "docs.google.com"} or host.endswith("drive.usercontent.google.com"):
        return True
    if "dropbox.com" in host or host.endswith("dropboxusercontent.com"):
        return True
    if path.endswith(".pdf"):
        return True
    scheme = (parsed.scheme or "").lower()
    if scheme in {"http", "https"} and not is_image_like_url(text):
        return True
    return False


def _usable_high_res_seed_url(url: str) -> str:
    """Prefer a direct public document URL over a JS viewer shell."""
    text = str(url or "").strip()
    if not text:
        return ""
    box = _box_shared_name(text)
    if box:
        return f"https://app.box.com/shared/static/{box}.pdf"
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host in {"drive.google.com", "docs.google.com"}:
        match = re.search(r"/file/d/([\w-]+)", parsed.path or "")
        if match:
            return f"https://drive.usercontent.google.com/download?id={match.group(1)}&export=download"
    if "dropbox.com" in host or host.endswith("dropboxusercontent.com"):
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["dl"] = "1"
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), ""))
    return normalize_url(text) or text


def _is_js_only_brochure_viewer(url: str) -> bool:
    """True for pitch/canva presentation shells — never Brochure PDF targets."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return any(token in host for token in ("canva.com", "canva.link", "pitch.com"))


def _is_direct_document_pdf(url: str) -> bool:
    """True for a fetchable PDF/document URL (not an HTML property page)."""
    text = str(url or "").strip()
    if not text or _is_js_only_brochure_viewer(text):
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if (parsed.scheme or "").lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if path.endswith(".pdf"):
        return True
    if host.endswith("drive.usercontent.google.com") and "export=download" in query:
        return True
    if ("dropbox.com" in host or host.endswith("dropboxusercontent.com")) and "dl=1" in query:
        return True
    return False


def _brochure_pdf_cell_rank(url: str) -> int:
    """Prefer actual PDF > resolvable viewer > property HTML page > nothing/JS."""
    text = str(url or "").strip()
    if not text:
        return 0
    try:
        parsed = urlparse(text)
    except ValueError:
        return 0
    if (parsed.scheme or "").lower() not in {"http", "https"}:
        return 0
    if _is_js_only_brochure_viewer(text):
        return 0
    if _is_direct_document_pdf(text):
        return 3
    resolved = _usable_high_res_seed_url(text)
    if resolved and _is_direct_document_pdf(resolved):
        return 2
    return 1


def _as_brochure_document_url(url: str) -> str:
    """Return a direct document URL when `url` is or resolves to one."""
    text = str(url or "").strip()
    if not text or _is_js_only_brochure_viewer(text):
        return ""
    if _is_direct_document_pdf(text):
        return normalize_url(text) or text
    resolved = _usable_high_res_seed_url(text)
    if resolved and _is_direct_document_pdf(resolved):
        return normalize_url(resolved) or resolved
    return ""


def _best_brochure_document_url(extraction: BrochureExtraction, seed_url: str = "") -> str:
    """Best real document PDF discovered during linked-source enrichment."""
    candidates = []

    def add(url):
        document = _as_brochure_document_url(url)
        if document and document not in candidates:
            candidates.append(document)

    add(seed_url)
    add(extraction.source_document)
    for asset in extraction.assets:
        if asset.classification == AssetType.BROCHURE:
            add(asset.url)
    for diagnostic in extraction.diagnostics:
        if diagnostic.resource_type == "pdf":
            add(diagnostic.final_url)
            add(diagnostic.original_url)
    return candidates[0] if candidates else ""


def _promote_brochure_pdf_cell(prop: Property, extraction: BrochureExtraction) -> None:
    """Set Brochure PDF to a real document URL when enrichment discovers one.

    Property/listing HTML pages remain valid seeds and are stashed on
    `_brochure_source_page` so gallery enrichment can still use them. Never
    blank the cell, and never promote pitch/canva JS viewers.
    """
    current = str(prop.values.get("Brochure PDF") or "").strip()
    best = _best_brochure_document_url(extraction, current)
    if not best:
        return
    if normalize_url(best) == normalize_url(current):
        return
    if _brochure_pdf_cell_rank(best) <= _brochure_pdf_cell_rank(current):
        return
    if current and _brochure_pdf_cell_rank(current) > 0:
        prop.values.setdefault("_brochure_source_page", current)
    _set_value(
        prop,
        "Brochure PDF",
        ExtractedValue(
            best,
            "brochure",
            extraction.source_document or best,
            "brochure:resolved_document",
            0.9,
        ),
    )


def _seed_high_res_fallback(prop: Property, brochure_url: str) -> None:
    """No-op: brochure/document URLs must not become High Res cell values.

    High Res / Floor Plan final cells are real extracted images or hosted
    gallery HTML only. Brochure PDF keeps the document URL. Enrichment still
    fetches aggressively; when budget/RSS skips a unique PDF the High Res
    cell stays blank rather than showing a Box/Drive/.pdf click-through.
    """
    return


def _floor_plan_already_seeded(prop: Property) -> bool:
    plan = str(prop.values.get("Floor Plan") or "").strip()
    return bool(plan)


def _rss_mb() -> float:
    try:
        import os
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _pdf_decode_must_serialize(rss: float = None) -> bool:
    """Hard-cap concurrent PDF decodes to 1 under 1.5GB RSS / small hosts."""
    if _HOST_RAM_MB and _HOST_RAM_MB < _PDF_DECODE_SERIALIZE_BELOW_RSS_MB:
        return True
    current = _rss_mb() if rss is None else rss
    return current < _PDF_DECODE_SERIALIZE_BELOW_RSS_MB


def _enrichment_memory_constrained(rss: float = None, hosted: bool = False) -> bool:
    """True when Box/PDF enrichment should stay serial + light-first-page.

    Modest RSS after rule:UNION (~100 MiB) is already "tight" on a 512MB–1GB
    Railway worker — waiting for the soft ceiling lets one full 22MB decode
    SIGKILL the process.
    """
    current = _rss_mb() if rss is None else rss
    if _HOST_RAM_MB and _HOST_RAM_MB < 1536:
        return True
    if current >= _ENRICHMENT_PARALLEL_RSS_MB:
        return True
    if hosted and (not _HOST_RAM_MB or _HOST_RAM_MB < 2048) and current >= 50:
        return True
    return False


def _memlog_enrichment(checkpoint: str, filename: str = "") -> None:
    try:
        from . import memlog

        memlog.log(checkpoint, filename)
    except Exception:
        pass


def _usable_floor_plan_value(values) -> str:
    plan = str(values.get("Floor Plan") or "").strip()
    if not plan or _is_viewer_floorplan_url(plan):
        return ""
    return plan


def _share_underfilled_building_photos(properties: List[Property]) -> None:
    """Copy enriched photo candidates across floors of the same building.

    Knotel/GPE/Workplace Plus often emit one row per floor. Enrichment may
    only finish the first few unique brochure URLs before the deadline —
    without this, the bottom half of the sheet stays on a single featured
    email image even though a sibling floor already has a full gallery.
    Also copies a real (non-viewer) Floor Plan when siblings still have a
    Box/Drive viewer placeholder or blank cell.
    """
    by_building = defaultdict(list)
    for prop in properties:
        building = str(prop.values.get("Building") or "").strip().lower()
        if building:
            by_building[building].append(prop)
    for props in by_building.values():
        if len(props) < 2:
            continue
        donor = max(
            props,
            key=lambda item: (
                _source_photo_count(item),
                len(item.values.get("_brochure_embedded_assets") or []),
                1 if _usable_floor_plan_value(item.values) else 0,
            ),
        )
        donor_count = _source_photo_count(donor)
        donor_embeds = donor.values.get("_brochure_embedded_assets") or []
        donor_candidates = list(donor.values.get("_high_res_candidates") or [])
        donor_image = str(donor.values.get("High Res Images") or "").strip()
        if donor_image and ".html" not in donor_image.lower():
            donor_candidates = list(dict.fromkeys([donor_image] + donor_candidates))
        donor_plan = _usable_floor_plan_value(donor.values)
        if donor_count < 1 and not donor_embeds and not donor_plan:
            continue
        # Share whenever a sibling is under-filled or missing a real plan —
        # even a single enriched photo / embed beats a blank cell.
        if (
            donor_count < 1
            and not donor_embeds
            and len(donor_candidates) < 1
            and not donor_plan
        ):
            continue
        for prop in props:
            if prop is donor:
                continue
            if _source_photo_count(prop) < _MIN_HIGH_RES_TARGET:
                if donor_embeds and not prop.values.get("_brochure_embedded_assets"):
                    prop.values["_brochure_embedded_assets"] = donor_embeds
                if donor_candidates:
                    existing = list(prop.values.get("_high_res_candidates") or [])
                    existing_image = str(prop.values.get("High Res Images") or "").strip()
                    if existing_image and ".html" not in existing_image.lower():
                        existing.insert(0, existing_image)
                    prop.values["_high_res_candidates"] = list(
                        dict.fromkeys(existing + donor_candidates)
                    )[:_SOFT_MAX_EMBEDDED_PHOTOS]
            sibling_plan = str(prop.values.get("Floor Plan") or "").strip()
            if donor_plan and (not sibling_plan or _is_viewer_floorplan_url(sibling_plan)):
                prop.values["Floor Plan"] = donor_plan


def enrich_properties(
    properties: Iterable[Property],
    fetcher: Callable = fetch_brochure,
    extractor: Callable[[bytes, str, str], BrochureExtraction] = extract_brochure,
    deadline: float = None,
) -> List[Property]:
    properties = list(properties)
    # Group by brochure URL first so each unique linked document is fetched
    # once, then applied to every listing that shares it (Workplace Plus
    # London has 231 rows / ~140 unique Drive PDFs — serial fetches under a
    # shared deadline blanked High Res for the majority). Props keep the
    # caller's original order in the returned list.
    by_url = defaultdict(list)
    for index, prop in enumerate(properties):
        seed_urls = []
        primary = str(prop.values.get("Brochure PDF") or "").strip()
        source_page = str(prop.values.get("_brochure_source_page") or "").strip()
        if primary:
            seed_urls.append(primary)
        # When Brochure PDF was promoted to a real document, still enrich from
        # the original property page so HTML galleries remain available.
        if source_page and source_page not in seed_urls:
            seed_urls.append(source_page)
        for extra in prop.values.get("_extra_brochure_urls") or []:
            text = str(extra or "").strip()
            if text and text not in seed_urls:
                seed_urls.append(text)
        for raw_url in seed_urls:
            brochure_url = normalize_url(raw_url)
            if raw_url and not brochure_url:
                status = "LINK_UNSUPPORTED" if urlparse(raw_url).scheme and urlparse(raw_url).scheme.lower() not in {"http", "https"} else "LINK_ENRICHMENT_SKIPPED"
                _record_diagnostic(prop, status, raw_url, detail="Only valid public HTTP(S) linked resources are supported.")
                _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", raw_url, detail="Primary extraction preserved.")
                continue
            if not brochure_url:
                continue
            by_url[brochure_url].append((index, prop, raw_url))

    # Unique URLs ordered by photo-need: blank High Res first (MetSpace /
    # Union / spreadsheet Drive rows), then single featured images
    # (Knotel), then already-complete galleries last.
    unique_urls = sorted(
        by_url.keys(),
        key=lambda url: min(_photo_enrichment_priority(prop) for _, prop, _ in by_url[url]),
    )

    def _skip_url(brochure_url, detail="Bounded batch enrichment budget was exhausted."):
        for _, prop, _ in by_url[brochure_url]:
            _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", brochure_url, detail=detail)
            prop.add_issue(ValidationIssue("Brochure PDF", "Linked-source enrichment was skipped because the bounded batch enrichment budget was exhausted.", Severity.INFO, brochure_url, "Primary extraction remains valid; process this file alone for another enrichment attempt.", "linked_source_enrichment"))
            _seed_high_res_fallback(prop, brochure_url)

    def _skip_remaining_for_memory(start_index, exclude=()):
        rss_now = _rss_mb()
        detail = (
            f"Skipped to keep process RSS under {_RSS_ENRICHMENT_CEILING_MB:.0f} MiB "
            f"(now {rss_now:.0f} MiB)."
        )
        skipped = 0
        for brochure_url in pending[start_index:]:
            if brochure_url in exclude:
                continue
            for _, prop, _ in by_url[brochure_url]:
                _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", brochure_url, detail=detail)
                prop.add_issue(
                    ValidationIssue(
                        "Brochure PDF",
                        "Linked-source enrichment was skipped to protect worker memory.",
                        Severity.INFO,
                        brochure_url,
                        "Primary extraction remains valid; process this file alone on a larger instance if photos are missing.",
                        "linked_source_enrichment",
                    )
                )
                _seed_high_res_fallback(prop, brochure_url)
            skipped += 1
        _memlog_enrichment(
            f"brochure enrichment RSS skip "
            f"(ceiling={_RSS_ENRICHMENT_CEILING_MB:.0f} MiB, now={rss_now:.0f} MiB, "
            f"skipped_urls={skipped})"
        )
        return skipped

    pending = []
    skip_complete = len(unique_urls) >= _SKIP_COMPLETE_WHEN_UNIQUE_GE
    for brochure_url in unique_urls:
        members = by_url[brochure_url]
        if skip_complete and all(_photo_enrichment_priority(prop) >= 2 for _, prop, _ in members):
            # Don't burn remaining unique-URL budget deepening galleries
            # that already meet the High Res target.
            continue
        if deadline is not None and time.monotonic() >= deadline:
            _skip_url(brochure_url)
            continue
        pending.append(brochure_url)

    if not pending:
        for brochure_url in unique_urls:
            for _, prop, _ in by_url[brochure_url]:
                _seed_high_res_fallback(prop, brochure_url)
        _share_underfilled_building_photos(properties)
        return properties

    # Fetch in small waves and apply immediately. Holding every UNION Box
    # PDF (~22MB) plus extracted page images in one giant cache (the old
    # "fetch all, then merge" approach) blew past Render's free-tier RSS
    # ceiling before spreadsheet write. Scale concurrency from detected
    # RAM / current RSS — GPE HTML pages with nested landlord PDFs are as
    # heavy as Box/Drive static downloads and must serialize under pressure.
    hosted_pending = any(
        "/shared/static/" in (urlparse(url).path or "")
        or _box_shared_name(url)
        or (urlparse(url).hostname or "").endswith("google.com")
        or (urlparse(url).hostname or "").endswith("googleusercontent.com")
        or "dropbox" in ((urlparse(url).hostname or "").lower())
        for url in pending
    )
    rss = _rss_mb()
    memory_constrained = _enrichment_memory_constrained(rss, hosted_pending)
    workers = min(_ENRICHMENT_FETCH_WORKERS, len(pending))
    # Always serialize when RSS is already high — not only for hosted PDFs.
    # Confirmed real (Railway GPE): parallel HTML→nested-PDF waves OOM'd
    # while hosted_pending was False, so the old guard never fired.
    # Confirmed real (Railway UNION, 2026-07): after rule:UNION at ~100 MiB
    # RSS, serial + light Box extracts are required on ~1GB hosts.
    if memory_constrained or rss >= _ENRICHMENT_PARALLEL_RSS_MB:
        workers = 1
    elif hosted_pending and _HOST_RAM_MB and _HOST_RAM_MB < 1800:
        workers = min(workers, 1)
    wave_size = max(1, workers)
    _memlog_enrichment(
        f"brochure enrichment start ({len(pending)} unique URLs, "
        f"workers={workers}, hosted={int(hosted_pending)}, "
        f"light={int(memory_constrained and hosted_pending)})"
    )

    def _apply_result(brochure_url, extraction):
        shared_embeds = None
        for member_index, (_, prop, _raw_url) in enumerate(by_url[brochure_url]):
            if isinstance(extraction, Exception):
                exc = extraction
                status = getattr(exc, "status", "LINK_ENRICHMENT_FAILED")
                _record_diagnostic(prop, status, brochure_url, getattr(exc, "final_url", None), detail=str(exc))
                _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", brochure_url, getattr(exc, "final_url", None), detail="Primary extraction preserved unchanged.")
                prop.add_issue(
                    ValidationIssue(
                        "Brochure PDF",
                        f"Linked-source enrichment was skipped: {exc}",
                        Severity.INFO,
                        brochure_url,
                        "Primary extraction remains valid; review the linked source manually if needed.",
                        "brochure_enrichment",
                    )
                )
                _seed_high_res_fallback(prop, brochure_url)
                continue
            try:
                identity = compare_property_identity(prop.values, extraction.identity_text, association_confidence=1.0)
                _record_diagnostic(
                    prop,
                    f"LINK_IDENTITY_{identity.decision.value}",
                    brochure_url,
                    extraction.source_document,
                    detail="; ".join(identity.reasons),
                    property_identity=property_key(prop.values),
                    identity_result=identity.decision.value,
                )
                prop.link_diagnostics.extend(extraction.diagnostics)
                if identity.decision in {IdentityDecision.AMBIGUOUS, IdentityDecision.HARD_CONFLICT}:
                    label = "conflicts with" if identity.decision == IdentityDecision.HARD_CONFLICT else "could not be confidently matched to"
                    prop.add_issue(ValidationIssue("Brochure PDF", f"Linked property content {label} this property and was not merged.", Severity.WARNING, extraction.source_document, "Confirm that the linked property source belongs to this record.", "linked_source_identity"))
                    continue
                staged = deepcopy(prop)
                _merge(staged, extraction)
                # One brochure URL can span many floors (UNION, Workplace Plus
                # sheets). Keep heavy embedded bitmaps once and reuse the same
                # candidate objects so 6 floors of HYLO don't each retain a
                # private copy of the same 22MB PDF's photos.
                embeds = staged.values.get("_brochure_embedded_assets") or []
                if embeds:
                    if shared_embeds is None:
                        shared_embeds = embeds
                    staged.values["_brochure_embedded_assets"] = shared_embeds
                # Drop bitmap bytes from the general assets list; materialise
                # only needs `_brochure_embedded_assets`.
                for asset in staged.assets:
                    if asset.content and (not shared_embeds or asset not in shared_embeds):
                        asset.content = None
                prop.values = staged.values
                prop.provenance = staged.provenance
                prop.assets = staged.assets
                prop.issues = staged.issues
                prop.review_required = staged.review_required
                _record_diagnostic(prop, "LINK_ENRICHMENT_SUCCESS", brochure_url, extraction.source_document, _diagnostic_resource_type(extraction))
                if _source_photo_count(prop) < 1 and not prop.values.get("_brochure_embedded_assets"):
                    _seed_high_res_fallback(prop, brochure_url)
            except Exception as exc:
                status = getattr(exc, "status", "LINK_ENRICHMENT_FAILED")
                _record_diagnostic(prop, status, brochure_url, getattr(exc, "final_url", None), detail=str(exc))
                _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", brochure_url, getattr(exc, "final_url", None), detail="Primary extraction preserved unchanged.")
                prop.add_issue(
                    ValidationIssue(
                        "Brochure PDF",
                        f"Linked-source enrichment was skipped: {exc}",
                        Severity.INFO,
                        brochure_url,
                        "Primary extraction remains valid; review the linked source manually if needed.",
                        "brochure_enrichment",
                    )
                )
                _seed_high_res_fallback(prop, brochure_url)
        # Release extraction-owned bitmaps that were not retained on
        # shared_embeds (decorative / capped photos / unused assets).
        if not isinstance(extraction, Exception):
            kept = set(id(a) for a in (shared_embeds or []))
            for asset in extraction.assets:
                if id(asset) not in kept:
                    asset.content = None
            extraction.identity_text = ""

    for wave_start in range(0, len(pending), wave_size):
        # Stop before starting a wave that cannot finish inside this file's
        # fair share. Drive/Box fetches use up to ~12s; keep a margin so the
        # last wave does not wipe Knotel/Workplace Plus enrichment time.
        # When only a little time remains, still attempt light/HTML retrieval
        # for remaining property pages instead of hard-skipping — nested
        # landlord PDFs are what burn the budget.
        remaining = None if deadline is None else deadline - time.monotonic()
        # Paid Railway: prefer full embed extraction. Only cut over to
        # HTML-only / light first-page extracts when the file's fair share
        # is nearly gone — not as soon as a modest unique-URL backlog exists.
        html_only = remaining is not None and remaining < _HTML_ONLY_REMAINING_SECONDS
        light_pdf = remaining is not None and remaining < _LIGHT_PDF_REMAINING_SECONDS
        if (
            len(pending) - wave_start >= _FORCE_LIGHT_UNIQUE_REMAINING
            and hosted_pending
            and remaining is not None
            and remaining < _LIGHT_PDF_REMAINING_SECONDS * 2
        ):
            light_pdf = True
        rss = _rss_mb()
        # Force light first-page Box/Drive extracts under modest RSS / small
        # hosts — full pdfplumber+fitz on a 22MB UNION PDF SIGKILL'd ~1GB
        # Railway after rule:UNION (confirmed 2026-07).
        if _enrichment_memory_constrained(rss, hosted_pending) and hosted_pending:
            light_pdf = True
        if deadline is not None and remaining <= _SKIP_WAVE_REMAINING_SECONDS:
            for brochure_url in pending[wave_start:]:
                _skip_url(brochure_url)
            break
        if rss >= _RSS_ENRICHMENT_CEILING_MB:
            _skip_remaining_for_memory(wave_start)
            break
        # Shrink wave when RSS is climbing so several large PDFs do not land together.
        effective_wave = wave_size
        if _enrichment_memory_constrained(rss, hosted_pending) or rss >= _ENRICHMENT_PARALLEL_RSS_MB:
            effective_wave = 1
        elif hosted_pending and _HOST_RAM_MB and _HOST_RAM_MB < 1800:
            effective_wave = 1
        wave = pending[wave_start : wave_start + effective_wave]
        prefer_photos = any(
            _floor_plan_already_seeded(prop) for brochure_url in wave for _, prop, _ in by_url[brochure_url]
        )
        # Under memory pressure, skip optional nested deepening even on HTML
        # seeds — Box/Drive must_follow downloads still run lightly.
        skip_nested = html_only or (
            _enrichment_memory_constrained(rss, hosted_pending) and not hosted_pending
        )

        def _run_retrieve(brochure_url):
            return _retrieve(
                brochure_url,
                fetcher,
                extractor,
                deadline,
                skip_nested_documents=skip_nested,
                light_pdf=light_pdf,
                prefer_photos=prefer_photos or light_pdf,
            )

        stop_for_memory = False
        if len(wave) == 1:
            brochure_url = wave[0]
            try:
                result = _run_retrieve(brochure_url)
            except Exception as exc:
                result = exc
            _apply_result(brochure_url, result)
            gc.collect()
            # One Box PDF can push RSS near the kill line; prefer partial
            # media over SIGKILL for the rest of the unique-URL backlog.
            if _rss_mb() >= _RSS_ENRICHMENT_CEILING_MB:
                _skip_remaining_for_memory(wave_start + 1)
                break
            continue
        # Wait for the wave to finish (requests timeouts already respect
        # `deadline`). Abandoned wait=False workers kept downloading UNION /
        # Drive PDFs into the next file's enrichment window and starved
        # Knotel's lighter HTML gallery fetches (confirmed 2026-07 4-file batch).
        applied = set()
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {
                pool.submit(_run_retrieve, brochure_url): brochure_url
                for brochure_url in wave
            }
            for future in as_completed(futures):
                brochure_url = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = exc
                _apply_result(brochure_url, result)
                applied.add(brochure_url)
                gc.collect()
                if _rss_mb() >= _RSS_ENRICHMENT_CEILING_MB:
                    stop_for_memory = True
                    for pending_future in futures:
                        pending_future.cancel()
                    break
        if stop_for_memory:
            _skip_remaining_for_memory(wave_start, exclude=applied)
            break
    # Brochure document URLs stay on Brochure PDF only — never seed High Res.
    _share_underfilled_building_photos(properties)
    _memlog_enrichment(f"brochure enrichment end ({len(pending)} unique URLs attempted)")
    return properties


def _fetch_resource(fetcher, url, deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise LinkedResourceError(
            "Linked enrichment skipped to preserve the request deadline",
            "LINK_ENRICHMENT_SKIPPED", url,
        )
    if fetcher is fetch_brochure:
        return fetcher(url, deadline=deadline)
    return fetcher(url)


def _retrieve(
    url,
    fetcher,
    extractor,
    deadline=None,
    skip_nested_documents=False,
    light_pdf=False,
    prefer_photos=False,
):
    resource = _coerce_resource(_fetch_resource(fetcher, url, deadline), url)
    resource_type = _resource_type(resource.payload, resource.content_type)
    payload = resource.payload
    content_type = resource.content_type
    final_url = resource.final_url
    extract_kwargs = {}
    if light_pdf and resource_type == "pdf" and extractor is extract_brochure:
        extract_kwargs = {
            "max_photos": _LIGHT_MAX_PHOTOS,
            "stop_after_floorplans": 1 if not prefer_photos else 0,
            "prefer_photos": prefer_photos,
            "max_pages": _LIGHT_MAX_PAGES,
        }
        combined = extract_brochure(payload, content_type, final_url, **extract_kwargs)
    elif prefer_photos and resource_type == "pdf" and extractor is extract_brochure:
        combined = extract_brochure(
            payload,
            content_type,
            final_url,
            prefer_photos=True,
            max_photos=_SOFT_MAX_EMBEDDED_PHOTOS,
        )
    else:
        combined = extractor(payload, content_type, final_url)
    # Drop the raw PDF/HTML body immediately — extract_brochure already
    # holds whatever embedded bitmaps it kept. Keeping both copies of a
    # ~22MB UNION Box PDF was a confirmed free-tier SIGKILL path.
    del payload
    combined.diagnostics.extend(_resolution_diagnostics(resource, resource_type))
    # HTML/property pages commonly expose the actual downloadable brochure
    # as a PDF link. Always follow Drive/Box/Dropbox download targets
    # (MetSpace/Workplace Plus: viewer chrome has ≥2 imgs but zero real
    # photos). Optionally skip other nested PDFs when a Knotel-style
    # property page already exposes a confident HTML photo gallery.
    def _confident_page_photo_count(assets):
        """Count real listing photos only — not Drive/Box chrome or icons.

        Confirmed real (2026-07, GPE): decorative/preview images on the
        property HTML page were counted as a "full gallery", which skipped
        the nested "Download brochure" PDF that actually held the photos.
        Require image-like URLs or embedded bitmaps plus association.
        """
        count = 0
        for asset in assets:
            if asset.classification != AssetType.PROPERTY_IMAGE:
                continue
            if (asset.association_confidence or 0) < 0.8:
                continue
            if asset.content:
                count += 1
                continue
            url = (asset.url or "").lower()
            if not url:
                continue
            if any(
                token in url
                for token in (
                    ".jpg", ".jpeg", ".png", ".webp", "/assets/", "/media/",
                    "/digitalassets", "/image", "/photo", "/gallery/",
                )
            ):
                count += 1
        return count

    def _page_gallery_url_count(assets):
        """Image-like HTTP URLs on the page usable as High Res candidates.

        Broader than _confident_page_photo_count: empty-alt GPE /media/
        thumbs still count so we can skip a slow nested PDF when the
        website gallery alone can fill the 5–8 target. Non-image chrome
        URLs (even if mis-labeled PROPERTY_IMAGE) do not count.
        """
        seen = set()
        skip = {
            AssetType.FLOORPLAN,
            AssetType.LOGO,
            AssetType.MAP,
            AssetType.TRACKING_OR_DECORATIVE,
            AssetType.DOCUMENT_PREVIEW,
            AssetType.BROCHURE,
        }
        for asset in assets:
            if asset.content or asset.classification in skip:
                continue
            url = normalize_url(asset.url or "")
            if not url or url in seen:
                continue
            low = url.lower()
            if any(
                token in low
                for token in (
                    ".jpg", ".jpeg", ".png", ".webp", "/assets/", "/media/",
                    "/digitalassets", "/image", "/photo", "/gallery/",
                )
            ):
                seen.add(url)
        return len(seen)

    page_photos = _confident_page_photo_count(combined.assets)
    # Broader gallery signal for the nested-PDF skip decision: image-like
    # HTTP URLs on the property page (GPE /media/, Knotel /assets/) even
    # when alt text is empty. Prefer these over burning the enrichment
    # window on a multi-MB "Download brochure" PDF per building.
    page_gallery_urls = _page_gallery_url_count(combined.assets)
    has_floorplan_seed = any(
        a.classification == AssetType.FLOORPLAN and (a.url or a.content)
        for a in combined.assets
    )
    brochure_assets = [
        a for a in combined.assets
        if a.classification == AssetType.BROCHURE and a.url and a.url != resource.final_url
    ]
    # Always follow Drive/Box/Dropbox. Follow explicit "Download brochure"
    # PDFs when the HTML gallery is under target (photos) OR when the page
    # has no floor-plan asset yet (GPE plans live inside the landlord PDF).
    html_gallery_ready = (
        page_gallery_urls >= _MIN_HIGH_RES_TARGET
        or page_photos >= _NESTED_PDF_SKIP_WHEN_PAGE_PHOTOS
    )
    need_nested_photos = not html_gallery_ready
    need_nested_plans = not has_floorplan_seed and not prefer_photos
    must_follow = []
    optional_docs = []
    for asset in brochure_assets:
        label = f"{asset.anchor_text or ''} {asset.filename or ''}".lower()
        is_download_brochure = (
            ("brochure" in label and ("download" in label or label.strip().endswith(".pdf")))
            or bool(re.search(r"download\s+(?:the\s+)?(?:brochure|pdf)", label))
            or bool(re.search(r"(?:^|[-_/])brochure(?:[-_.]|\.pdf|$)", label))
        )
        if _is_hosted_document_download(asset.url):
            must_follow.append(asset.url)
        elif is_download_brochure and (need_nested_photos or need_nested_plans):
            must_follow.append(asset.url)
        else:
            optional_docs.append(asset.url)
    documents = list(dict.fromkeys(must_follow))[:2]
    if need_nested_photos:
        for doc_url in optional_docs:
            if doc_url not in documents:
                documents.append(doc_url)
            if len(documents) >= 2:
                break
    # Caller may request HTML-only (tight deadline): still expose page
    # gallery URLs without nested landlord-PDF fetches. Drive/Box viewers
    # still need their download target — those are the only photo source.
    if skip_nested_documents:
        documents = [u for u in documents if _is_hosted_document_download(u)][:1]
    rss_now = _rss_mb()
    memory_tight = (
        rss_now >= _ENRICHMENT_PARALLEL_RSS_MB
        or _enrichment_memory_constrained(rss_now, hosted=any(_is_hosted_document_download(u) for u in documents))
    )
    # When HTML already supplies High Res, prefer those photos and only
    # lightly pull nested PDFs for a floor plan. Decoding marketing photos
    # again from large landlord PDFs spikes RSS on moderate Railway
    # (SIGKILL / "Perhaps out of memory?"). Under memory pressure, skip
    # nested landlord PDFs entirely once the page gallery + plan exist.
    if html_gallery_ready and not need_nested_photos:
        nested_max_photos = 0
        nested_stop_plans = 1 if need_nested_plans else 0
        if memory_tight and not need_nested_plans:
            documents = [u for u in documents if _is_hosted_document_download(u)]
        elif memory_tight and need_nested_plans:
            # One light nested PDF only — enough for a plan bitmap.
            hosted = [u for u in documents if _is_hosted_document_download(u)]
            nested = [u for u in documents if u not in hosted][:1]
            documents = hosted[:1] + nested
        else:
            # Cap concurrent nested landlord PDFs per property page.
            hosted = [u for u in documents if _is_hosted_document_download(u)]
            nested = [u for u in documents if u not in hosted][:1]
            documents = hosted[:1] + nested
    else:
        nested_max_photos = None if need_nested_photos else 0
        nested_stop_plans = None if need_nested_photos else 1
        if memory_tight and need_nested_photos:
            # Memory-tight HTML pages: at most one nested PDF, light decode.
            documents = documents[:1]
    if light_pdf and need_nested_photos:
        nested_max_photos = _LIGHT_MAX_PHOTOS
    # Prefer light page scan when we only need a floor plan from nested PDF,
    # or when RSS/host headroom is already modest.
    nested_light_pages = light_pdf or memory_tight or (html_gallery_ready and not need_nested_photos)
    for document_url in documents:
        if deadline is not None and time.monotonic() >= deadline:
            combined.warnings.append(
                "Linked brochure document was skipped to preserve the request deadline."
            )
            break
        try:
            nested = _coerce_resource(_fetch_resource(fetcher, document_url, deadline), document_url)
            if extractor is extract_brochure and (
                nested_max_photos is not None or light_pdf or prefer_photos or nested_light_pages
            ):
                nested_extraction = extract_brochure(
                    nested.payload,
                    nested.content_type,
                    nested.final_url,
                    max_photos=0 if nested_max_photos == 0 else (nested_max_photos if nested_max_photos is not None else _LIGHT_MAX_PHOTOS if light_pdf else None),
                    stop_after_floorplans=(
                        nested_stop_plans or 1
                        if nested_max_photos == 0
                        else (
                            nested_stop_plans
                            if nested_max_photos is not None and not need_nested_photos
                            else (0 if prefer_photos and light_pdf else 1 if light_pdf else None)
                        )
                    ),
                    prefer_photos=prefer_photos or light_pdf,
                    max_pages=_LIGHT_MAX_PAGES if nested_light_pages else None,
                )
            else:
                nested_extraction = extractor(nested.payload, nested.content_type, nested.final_url)
            nested_extraction.diagnostics.extend(_resolution_diagnostics(nested, _resource_type(nested.payload, nested.content_type)))
            # Drop the raw nested body once extract has kept its embeds —
            # several GPE landlord PDFs alive across a wave was a confirmed
            # Railway SIGKILL path.
            del nested
            for field, value in nested_extraction.fields.items():
                combined.fields.setdefault(field, value)
            # Reuse nested PDF/HTML text already extracted in this pass for
            # unit-scoped field matching — no extra fetch.
            if nested_extraction.identity_text:
                combined.identity_text = (
                    f"{combined.identity_text}\n{nested_extraction.identity_text}"
                    if combined.identity_text
                    else nested_extraction.identity_text
                )
            combined.assets.extend(nested_extraction.assets)
            combined.warnings.extend(nested_extraction.warnings)
            combined.diagnostics.extend(nested_extraction.diagnostics)
        except Exception as exc:
            combined.warnings.append(f"Linked brochure document could not be enriched: {exc}")
    combined.assets = classify_candidates(combined.assets)
    counts = Counter(candidate.classification.value for candidate in combined.assets)
    combined.diagnostics.append(
        LinkDiagnostic(
            "IMAGE_CANDIDATES_CLASSIFIED",
            original_url=url,
            final_url=combined.source_document,
            detail=", ".join(f"{kind}={count}" for kind, count in sorted(counts.items())) or "no asset candidates",
        )
    )
    return combined


def _coerce_resource(value, requested_url):
    if isinstance(value, BrochureResource):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        return BrochureResource(value[0], value[1], value[2])
    if isinstance(value, tuple) and len(value) == 2:
        return BrochureResource(value[0], value[1], requested_url)
    raise TypeError("Brochure fetcher must return BrochureResource or a 2/3-item tuple")


def _resolution_diagnostics(resource: BrochureResource, resource_type: str) -> List[LinkDiagnostic]:
    original = resource.original_url or resource.final_url
    diagnostics = [LinkDiagnostic("LINK_RESOLVED", original, resource.final_url, resource_type)]
    if resource.redirects or normalize_url(original) != normalize_url(resource.final_url):
        diagnostics.append(LinkDiagnostic("LINK_REDIRECT_RESOLVED", original, resource.final_url, resource_type, f"{len(resource.redirects)} redirect(s)"))
    status = {"pdf": "LINK_RESOURCE_PDF", "html": "LINK_RESOURCE_HTML", "image": "LINK_RESOURCE_IMAGE"}.get(resource_type, "LINK_UNSUPPORTED")
    diagnostics.append(LinkDiagnostic(status, original, resource.final_url, resource_type))
    return diagnostics


def _record_diagnostic(prop, status, original_url, final_url=None, resource_type=None, detail="", property_identity="", identity_result="", source_context=""):
    prop.link_diagnostics.append(LinkDiagnostic(status, original_url, final_url, resource_type, detail, property_identity, identity_result, source_context))


def _diagnostic_resource_type(extraction):
    for diagnostic in reversed(extraction.diagnostics):
        if diagnostic.resource_type:
            return diagnostic.resource_type
    return None


def _merge(prop: Property, extraction: BrochureExtraction) -> None:
    for warning in extraction.warnings:
        prop.add_issue(ValidationIssue("Brochure PDF", warning, Severity.INFO, extraction.source_document, "Primary extraction and any successfully extracted brochure-page evidence were preserved.", "brochure_enrichment"))
    for candidate in classify_candidates(extraction.assets):
        _add_asset(prop, candidate)
    # Prefer a real nested/hosted PDF in Brochure PDF over the HTML property
    # page that seeded enrichment (GPE Download brochure, Drive/Box static).
    _promote_brochure_pdf_cell(prop, extraction)
    embedded = [a for a in prop.assets if a.content and a.classification in {AssetType.PROPERTY_IMAGE, AssetType.FLOORPLAN}]
    if embedded:
        # Soft-cap photos (floor plans uncapped). Exact content-hash dedupe
        # already happened in classify_candidates / _extract_pdf_visuals;
        # materialise writes bytes to disk then clears content.
        photos = [a for a in embedded if a.classification == AssetType.PROPERTY_IMAGE][:_SOFT_MAX_EMBEDDED_PHOTOS]
        plans = [a for a in embedded if a.classification == AssetType.FLOORPLAN][:_SOFT_MAX_EMBEDDED_FLOORPLANS]
        prop.values["_brochure_embedded_assets"] = photos + plans
        # Drop bytes from assets not kept for materialise.
        kept = set(id(a) for a in photos + plans)
        for asset in prop.assets:
            if asset.content and id(asset) not in kept:
                asset.content = None
    # Cap URL-side candidates to the High Res band (5–8).
    _MAX_PROPERTY_IMAGES = _SOFT_MAX_EMBEDDED_PHOTOS
    property_images = [
        a.url for a in prop.assets if a.classification == AssetType.PROPERTY_IMAGE and a.url
    ][:_MAX_PROPERTY_IMAGES]
    floorplans = [a.url for a in prop.assets if a.classification == AssetType.FLOORPLAN and a.url]
    existing_images = str(prop.values.get("High Res Images") or "")
    # Brochure/PDF seeds are click-through fallbacks, not photos — real
    # property images must replace them (UNION High Res coverage path).
    if _is_brochure_media_seed_url(existing_images):
        existing_images = ""
        prop.values["High Res Images"] = ""
    if not existing_images and property_images:
        _apply(prop, "High Res Images", _evidence(property_images[0], extraction.source_document, 0.82))
        # The web layer turns 2+ externally hosted candidates into its
        # existing gallery page; direct/library callers still receive the
        # safe first photo in the public spreadsheet field above.
        prop.values["_high_res_candidates"] = property_images
    elif property_images:
        # The primary parser may provide an extensionless CDN/tracking URL,
        # so extension checks are not a safe way to decide whether it is an
        # image.  It is already in the image field and therefore joins the
        # brochure photos before exact-identity deduplication.  Generated
        # HTML galleries are excluded because they are containers, not
        # candidate images.
        existing_candidates = [
            c for c in list(prop.values.get("_high_res_candidates") or [])
            if c and not _is_brochure_media_seed_url(c)
        ]
        if existing_images and not re.search(r"\.html(?:[?#]|$)", existing_images, re.I):
            existing_candidates.insert(0, existing_images)
        prop.values["_high_res_candidates"] = list(
            dict.fromkeys(existing_candidates + property_images)
        )[:_MAX_PROPERTY_IMAGES]
    # Embedded brochure photos (no HTTP URL) still clear a seed cell once
    # materialise writes them — mark via candidates list already handled
    # in _seed only when embeds are absent.
    if prop.values.get("_brochure_embedded_assets") and _is_brochure_media_seed_url(
        str(prop.values.get("High Res Images") or "")
    ):
        prop.values["High Res Images"] = ""
    existing_plan = str(prop.values.get("Floor Plan") or "")
    if floorplans:
        plan_evidence = _evidence(floorplans[0], extraction.source_document, 0.9)
        if not existing_plan:
            _apply(prop, "Floor Plan", plan_evidence)
        elif _is_viewer_floorplan_url(existing_plan):
            # Primary xlsx_links often pre-fills Box/Drive viewer URLs at
            # confidence 1.0 — still replace them with a real plan asset URL.
            _set_value(prop, "Floor Plan", plan_evidence)
    # Building-level text fills (contacts, features, term, postcode, …).
    for field, evidence in extraction.fields.items():
        if field in _UNIT_SPECIFIC_FIELDS or field in _ADDRESS_LOCKED_FIELDS:
            continue
        if field not in _SAFE_BROCHURE_FIELDS:
            continue
        _apply(prop, field, evidence)
    # Size / desks / prices: only when the same floor/unit is clearly labeled.
    unit_fields = _extract_unit_fields(
        extraction.identity_text,
        str(prop.values.get("Floor/Unit") or ""),
        extraction.source_document,
    )
    for field, evidence in unit_fields.items():
        _apply(prop, field, evidence)


def _apply(prop: Property, field: str, evidence: ExtractedValue) -> None:
    incoming = evidence.value
    existing = prop.values.get(field)
    existing_provenance = prop.provenance.get(field)
    existing_confidence = existing_provenance.confidence if existing_provenance else (1.0 if existing not in (None, "") else 0.0)
    # Primary file owns address / floor identity wording. Brochure may only
    # fill Property Postcode when blank (handled as a normal empty-cell fill).
    if field in _ADDRESS_LOCKED_FIELDS:
        if existing not in (None, "") and not _equivalent(existing, incoming):
            prop.add_issue(
                ValidationIssue(
                    field,
                    "Brochure address/floor text was ignored; primary source retained.",
                    Severity.WARNING,
                    f"Primary: {existing} | Brochure: {incoming} | Source: {evidence.source_document}",
                    "Primary Building / Property Address 1 / Floor/Unit are never replaced from a brochure.",
                    "brochure_conflict_resolution",
                )
            )
        return
    if existing in (None, ""):
        if evidence.confidence >= BROCHURE_RELIABLE_CONFIDENCE:
            _set_value(prop, field, evidence)
        return
    if _equivalent(existing, incoming):
        return
    if existing_confidence < PRIMARY_STRONG_CONFIDENCE and evidence.confidence > existing_confidence:
        _set_value(prop, field, evidence)
        return
    prop.add_issue(
        ValidationIssue(
            field,
            "Brochure value conflicts with the retained primary-source value.",
            Severity.WARNING,
            f"Primary: {existing} | Brochure: {incoming} | Source: {evidence.source_document}",
            "Compare the primary source and brochure before upload.",
            "brochure_conflict_resolution",
        )
    )


def _set_value(prop: Property, field: str, evidence: ExtractedValue) -> None:
    value = _dedupe_text(evidence.value) if isinstance(evidence.value, str) and ";" in evidence.value else evidence.value
    if field == "Special Features" and isinstance(value, str):
        value = cap_special_features(value)
    prop.values[field] = value
    prop.provenance[field] = FieldProvenance(
        source=evidence.source,
        method=evidence.extraction_method,
        confidence=evidence.confidence,
        original_value=value,
        source_document=evidence.source_document,
    )


def _add_asset(prop: Property, candidate: AssetCandidate) -> None:
    normalized = normalize_url(candidate.url)
    if normalized and all(normalize_url(item.url) != normalized for item in prop.assets):
        candidate.url = normalized
        prop.assets.append(candidate)
    elif candidate.content_hash and all(item.content_hash != candidate.content_hash for item in prop.assets):
        prop.assets.append(candidate)


def _equivalent(left, right) -> bool:
    try:
        a, b = float(left), float(right)
        return abs(a - b) <= max(1.0, abs(a) * 0.02)
    except (TypeError, ValueError):
        clean = lambda value: re.sub(r"\W+", " ", str(value).lower()).strip()
        return clean(left) == clean(right)


def _dedupe_text(value: str) -> str:
    seen, parts = set(), []
    for part in (item.strip() for item in value.split(";") if item.strip()):
        key = re.sub(r"\W+", " ", part.lower()).strip()
        if key not in seen:
            seen.add(key)
            parts.append(part)
    return "; ".join(parts)
