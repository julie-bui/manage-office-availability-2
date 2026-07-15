"""Provider-neutral brochure extraction and confidence-aware enrichment.

Brochures are secondary evidence.  Failure is isolated per brochure and a
strong primary value is never silently replaced.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import ipaddress
import json
from io import BytesIO
import re
import time
from typing import Callable, Iterable, List
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

from .assets import classify_candidate, classify_candidates, normalize_url
from .address import extract_postcode
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

MAX_BROCHURE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 6
PRIMARY_STRONG_CONFIDENCE = 0.8
BROCHURE_RELIABLE_CONFIDENCE = 0.7

_SECTION_FIELDS = {
    "description": "Special Features",
    "specification": "Special Features",
    "amenities": "Special Features",
    "features": "Special Features",
    "sustainability": "Special Features",
    "epc": "Special Features",
    "lease terms": "Min. Term",
    "availability": "Floor/Unit",
    "service charge": "Special Features",
    "business rates": "Special Features",
    "rates": "Special Features",
    "rent": "Special Features",
    "pricing": "Special Features",
}
_HEADING_RE = re.compile(
    r"^(description|specification|amenities|features|sustainability|epc|lease terms|availability|service charge|business rates|rates|rent|pricing)\s*:?(.*)$",
    re.I,
)
_SIZE_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)\b", re.I)


class LinkedResourceError(Exception):
    def __init__(self, message, status="LINK_ENRICHMENT_FAILED", final_url=None):
        super().__init__(message)
        self.status = status
        self.final_url = final_url


def fetch_brochure(url: str, timeout: float = 6.0, deadline: float = None) -> BrochureResource:
    current = url
    redirects = []
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
            response = requests.get(current, timeout=request_timeout, headers={"User-Agent": "OfficeAvailability/1.0"}, allow_redirects=False)
        except requests.Timeout as exc:
            raise LinkedResourceError("Linked resource timed out", "LINK_TIMEOUT", current) from exc
        except requests.RequestException as exc:
            raise LinkedResourceError(f"Linked resource request failed: {exc}", "LINK_ENRICHMENT_FAILED", current) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise LinkedResourceError("Linked-resource redirect did not provide a destination", "LINK_ENRICHMENT_FAILED", current)
            destination = urljoin(current, location)
            if destination in redirects or destination == current:
                raise LinkedResourceError("Linked-resource redirect loop detected", "LINK_ENRICHMENT_FAILED", current)
            redirects.append(destination)
            current = destination
            continue
        if response.status_code in {401, 403}:
            raise LinkedResourceError("Linked resource denied access", "LINK_ACCESS_DENIED", current)
        if response.status_code in {404, 410}:
            raise LinkedResourceError("Linked resource was not found", "LINK_NOT_FOUND", current)
        if response.status_code == 429:
            raise LinkedResourceError("Linked resource rate limited enrichment", "LINK_RATE_LIMITED", current)
        if response.status_code >= 400:
            raise LinkedResourceError(f"Linked resource returned HTTP {response.status_code}", "LINK_ENRICHMENT_FAILED", current)
        break
    else:
        raise LinkedResourceError("Linked resource exceeded the redirect limit", "LINK_ENRICHMENT_FAILED", current)
    payload = response.content
    if len(payload) > MAX_BROCHURE_BYTES:
        raise LinkedResourceError("Linked resource exceeds the 20MB enrichment limit", "LINK_ENRICHMENT_SKIPPED", current)
    return BrochureResource(payload, response.headers.get("Content-Type", ""), response.url or current, url, tuple(redirects))



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


def extract_brochure(payload: bytes, content_type: str, source_document: str) -> BrochureExtraction:
    """Best-effort extraction based on actual response content, not suffix."""
    resource_type = _resource_type(payload, content_type)
    if resource_type == "html":
        return _extract_html(payload, source_document)
    if resource_type == "image":
        return _extract_direct_image(payload, content_type, source_document)
    if resource_type != "pdf":
        raise LinkedResourceError("Linked resource type is unsupported", "LINK_UNSUPPORTED", source_document)
    import pdfplumber

    text_parts = []
    links = []
    with pdfplumber.open(BytesIO(payload)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text_parts.append(page.extract_text() or "")
            for annotation in page.annots or []:
                uri = (annotation.get("data") or {}).get("URI") or annotation.get("uri")
                if uri:
                    links.append(AssetCandidate(uri, source_document, page_number=page_number))
    text = "\n".join(text_parts)
    fields = _extract_fields(text, source_document)
    return BrochureExtraction(source_document, fields, classify_candidates(links) + _extract_pdf_visuals(payload, source_document, text_parts), identity_text=text)


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
    """
    parsed = urlparse(source_document)
    if parsed.hostname not in {"drive.google.com", "docs.google.com"}:
        return []
    match = re.search(r"/file/d/([\w-]+)", parsed.path)
    if not match:
        return []
    file_id = match.group(1)
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
    return [AssetCandidate(url, source_document, mime_type="application/pdf", filename=f"{file_id}.pdf", anchor_text="Download brochure")]


def _extract_pdf_visuals(payload: bytes, source_document: str, pages_text: List[str]) -> List[AssetCandidate]:
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
    try:
        for page_number, page in enumerate(document):
            for image in page.get_images(full=True):
                try:
                    base = document.extract_image(image[0])
                    content = base.get("image") or b""
                    if len(content) < pdf_images.MIN_IMAGE_BYTES:
                        continue
                    digest = hashlib.sha256(content).hexdigest()
                    counts[digest] += 1
                    with Image.open(BytesIO(content)) as bitmap:
                        width, height = bitmap.size
                    page_text = pages_text[page_number] if page_number < len(pages_text) else ""
                    is_floorplan = pdf_images.is_floorplan_page(page_text) or pdf_images.is_floorplan_image(content)
                    classification = AssetType.FLOORPLAN if is_floorplan else (AssetType.DECORATIVE if width < 300 or height < 200 else AssetType.UNKNOWN)
                    extracted.append(
                        AssetCandidate(
                            "", source_document, mime_type=f"image/{base.get('ext', 'png')}",
                            filename=f"asset-p{page_number + 1}-{digest[:10]}.{base.get('ext', 'png')}",
                            page_number=page_number + 1, classification=classification,
                            confidence=0.9 if classification == AssetType.FLOORPLAN else (0.86 if classification == AssetType.DECORATIVE else 0.0),
                            surrounding_text=page_text[:800], discovery_method="pdf_embedded_image",
                            association_confidence=0.85 if classification == AssetType.UNKNOWN else 0.0,
                            width=width, height=height, content=content, content_hash=digest,
                            extension=base.get("ext", "png"),
                        )
                    )
                except Exception:
                    continue
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
        elif value:
            fields[field] = _evidence(value, source_document, 0.72)
    if feature_parts:
        fields["Special Features"] = _evidence(_dedupe_text("; ".join(feature_parts)), source_document, 0.74)
    size = _SIZE_RE.search(text)
    if size:
        fields["Size (sq ft)"] = _evidence(float(size.group(1).replace(",", "")), source_document, 0.76)
    postcodes = sorted(set(filter(None, (extract_postcode(line) for line in text.splitlines()))))
    # One brochure belongs to one Property at this stage.  A single
    # unambiguous postcode is reliable secondary evidence; multiple
    # postcodes are deliberately left unresolved for conflict review.
    if len(postcodes) == 1:
        fields["Property Postcode"] = _evidence(postcodes[0], source_document, 0.84)
    return fields


def _evidence(value, source_document, confidence):
    return ExtractedValue(value, "brochure", source_document, "deterministic:brochure", confidence)


def _source_photo_count(prop: Property) -> int:
    values = prop.values
    candidates = list(values.get("_source_high_res_candidates") or [])
    candidates.extend(values.get("_high_res_candidates") or [])
    existing = str(values.get("High Res Images") or "").strip()
    if existing and ".html" not in existing.lower():
        candidates.insert(0, existing)
    return len({str(url).split("?", 1)[0] for url in candidates if url})


def _photo_enrichment_priority(prop: Property) -> int:
    """Lower runs first. Blank High Res (MetSpace) before single featured
    images (Knotel) before galleries that already meet the 2+ target."""
    count = _source_photo_count(prop)
    if count <= 0:
        return 0
    if count < 2:
        return 1
    return 2


def enrich_properties(
    properties: Iterable[Property],
    fetcher: Callable = fetch_brochure,
    extractor: Callable[[bytes, str, str], BrochureExtraction] = extract_brochure,
    deadline: float = None,
) -> List[Property]:
    properties = list(properties)
    # Visit listings with no/few source photos first so a shared enrichment
    # deadline is spent where High Res would otherwise stay blank or stuck
    # on one featured image. Props are mutated in place; the returned list
    # keeps the caller's original order.
    fetch_order = [
        prop
        for _, prop in sorted(
            enumerate(properties),
            key=lambda item: (_photo_enrichment_priority(item[1]), item[0]),
        )
    ]
    cache = {}
    for prop in fetch_order:
        raw_url = str(prop.values.get("Brochure PDF") or "").strip()
        brochure_url = normalize_url(raw_url)
        if raw_url and not brochure_url:
            status = "LINK_UNSUPPORTED" if urlparse(raw_url).scheme and urlparse(raw_url).scheme.lower() not in {"http", "https"} else "LINK_ENRICHMENT_SKIPPED"
            _record_diagnostic(prop, status, raw_url, detail="Only valid public HTTP(S) linked resources are supported.")
            _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", raw_url, detail="Primary extraction preserved.")
            continue
        if not brochure_url:
            continue
        # Confirmed real (2026-07, GPE "16 Dufour's Place"): a building
        # repeated across several rows (one per floor) shares the exact
        # same Brochure PDF URL, fetched/extracted once and cached. This
        # deadline check used to run before the cache lookup below, so
        # once the enrichment budget for the whole file was used up by the
        # FIRST row's own real network fetch, every later row sharing that
        # already-cached URL got skipped too -- even though reusing a cache
        # hit costs nothing and there was no real budget reason to skip it.
        # Only a genuinely new fetch (not yet in cache) is gated on the
        # deadline now.
        if brochure_url not in cache and deadline is not None and time.monotonic() >= deadline:
            _record_diagnostic(prop, "LINK_ENRICHMENT_SKIPPED", brochure_url, detail="Bounded batch enrichment budget was exhausted.")
            prop.add_issue(ValidationIssue("Brochure PDF", "Linked-source enrichment was skipped because the bounded batch enrichment budget was exhausted.", Severity.INFO, brochure_url, "Primary extraction remains valid; process this file alone for another enrichment attempt.", "linked_source_enrichment"))
            continue
        try:
            if brochure_url not in cache:
                cache[brochure_url] = _retrieve(brochure_url, fetcher, extractor, deadline)
            extraction = cache[brochure_url]
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
            # Merge into a copy, then publish the result as one atomic unit.
            # An unexpected merge error cannot leave half-applied enrichment.
            staged = deepcopy(prop)
            _merge(staged, extraction)
            prop.values = staged.values
            prop.provenance = staged.provenance
            prop.assets = staged.assets
            prop.issues = staged.issues
            prop.review_required = staged.review_required
            _record_diagnostic(prop, "LINK_ENRICHMENT_SUCCESS", brochure_url, extraction.source_document, _diagnostic_resource_type(extraction))
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


def _retrieve(url, fetcher, extractor, deadline=None):
    resource = _coerce_resource(_fetch_resource(fetcher, url, deadline), url)
    resource_type = _resource_type(resource.payload, resource.content_type)
    combined = extractor(resource.payload, resource.content_type, resource.final_url)
    combined.diagnostics.extend(_resolution_diagnostics(resource, resource_type))
    # HTML/property pages commonly expose the actual downloadable brochure
    # as a PDF link. Follow at most two unique document assets, once each.
    documents = [a.url for a in combined.assets if a.classification == AssetType.BROCHURE and a.url != resource.final_url][:2]
    for document_url in documents:
        if deadline is not None and time.monotonic() >= deadline:
            combined.warnings.append(
                "Linked brochure document was skipped to preserve the request deadline."
            )
            break
        try:
            nested = _coerce_resource(_fetch_resource(fetcher, document_url, deadline), document_url)
            nested_extraction = extractor(nested.payload, nested.content_type, nested.final_url)
            nested_extraction.diagnostics.extend(_resolution_diagnostics(nested, _resource_type(nested.payload, nested.content_type)))
            for field, value in nested_extraction.fields.items():
                combined.fields.setdefault(field, value)
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
    embedded = [a for a in prop.assets if a.content and a.classification in {AssetType.PROPERTY_IMAGE, AssetType.FLOORPLAN}]
    if embedded:
        prop.values["_brochure_embedded_assets"] = embedded
    # Keep at most five property photos per listing so brochure dumps cannot
    # flood High Res; the web layer targets 2-5 when photos exist.
    _MAX_PROPERTY_IMAGES = 5
    property_images = [
        a.url for a in prop.assets if a.classification == AssetType.PROPERTY_IMAGE and a.url
    ][:_MAX_PROPERTY_IMAGES]
    floorplans = [a.url for a in prop.assets if a.classification == AssetType.FLOORPLAN and a.url]
    existing_images = str(prop.values.get("High Res Images") or "")
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
        existing_candidates = list(prop.values.get("_high_res_candidates") or [])
        if existing_images and not re.search(r"\.html(?:[?#]|$)", existing_images, re.I):
            existing_candidates.insert(0, existing_images)
        prop.values["_high_res_candidates"] = list(
            dict.fromkeys(existing_candidates + property_images)
        )[:_MAX_PROPERTY_IMAGES]
    if not prop.values.get("Floor Plan") and floorplans:
        _apply(prop, "Floor Plan", _evidence(floorplans[0], extraction.source_document, 0.9))
    for field, evidence in extraction.fields.items():
        _apply(prop, field, evidence)


def _apply(prop: Property, field: str, evidence: ExtractedValue) -> None:
    incoming = evidence.value
    existing = prop.values.get(field)
    existing_provenance = prop.provenance.get(field)
    existing_confidence = existing_provenance.confidence if existing_provenance else (1.0 if existing not in (None, "") else 0.0)
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
