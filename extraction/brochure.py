"""Provider-neutral brochure extraction and confidence-aware enrichment.

Brochures are secondary evidence.  Failure is isolated per brochure and a
strong primary value is never silently replaced.
"""
from collections import Counter
import hashlib
import ipaddress
from io import BytesIO
import re
import time
from typing import Callable, Iterable, List
from urllib.parse import urljoin, urlparse

import requests

from .assets import classify_candidate, classify_candidates, normalize_url
from .address import extract_postcode
from .models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
    BrochureResource,
    ExtractedValue,
    FieldProvenance,
    Property,
    Severity,
    ValidationIssue,
)

MAX_BROCHURE_BYTES = 20 * 1024 * 1024
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


def fetch_brochure(url: str, timeout: float = 6.0) -> BrochureResource:
    current = url
    for _ in range(6):
        _validate_remote_url(current)
        response = requests.get(current, timeout=timeout, headers={"User-Agent": "OfficeAvailability/1.0"}, allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise ValueError("Brochure redirect did not provide a destination")
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        break
    else:
        raise ValueError("Brochure exceeded the redirect limit")
    payload = response.content
    if len(payload) > MAX_BROCHURE_BYTES:
        raise ValueError("Brochure exceeds the 20MB enrichment limit")
    return BrochureResource(payload, response.headers.get("Content-Type", ""), response.url or url)


def _validate_remote_url(url):
    parsed = urlparse(normalize_url(url))
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        raise ValueError("Brochure destination is not a public HTTP(S) host")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Brochure destination is not a public HTTP(S) host")


def extract_brochure(payload: bytes, content_type: str, source_document: str) -> BrochureExtraction:
    """Best-effort deterministic PDF or HTML brochure extraction."""
    kind = (content_type or "").lower()
    if "html" in kind or payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return _extract_html(payload, source_document)
    if "pdf" not in kind and not payload.startswith(b"%PDF"):
        raise ValueError("Brochure content is neither extractable PDF nor HTML")
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
    return BrochureExtraction(source_document, fields, classify_candidates(links) + _extract_pdf_visuals(payload, source_document, text_parts))


def _extract_html(payload: bytes, source_document: str) -> BrochureExtraction:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(payload, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    candidates = []
    for image in soup.find_all("img"):
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
                candidates.append(AssetCandidate(src, source_document, mime_type="image/*", alt_text=image.get("alt"), filename=urlparse(src).path.rsplit("/", 1)[-1]))
    for source in soup.find_all("source"):
        for raw_url in _srcset_urls(source.get("srcset") or source.get("data-srcset") or ""):
            src = urljoin(source_document, raw_url)
            candidates.append(AssetCandidate(src, source_document, mime_type=source.get("type") or "image/*", filename=urlparse(src).path.rsplit("/", 1)[-1]))
    for meta in soup.find_all("meta"):
        if (meta.get("property") or meta.get("name") or "").lower() in {"og:image", "twitter:image", "twitter:image:src"}:
            src = urljoin(source_document, meta.get("content") or "")
            if src:
                candidates.append(AssetCandidate(src, source_document, mime_type="image/*", anchor_text="page preview image", filename=urlparse(src).path.rsplit("/", 1)[-1]))
    hosted_documents = _hosted_document_candidates(soup, source_document)
    if hosted_documents:
        # Viewer thumbnails are document previews, not independent property
        # photographs. The downloaded document below supplies the real,
        # hashable page assets and avoids duplicating its cover/first page.
        for candidate in candidates:
            if candidate.anchor_text == "page preview image":
                candidate.classification = AssetType.DECORATIVE
                candidate.confidence = 0.95
    candidates.extend(hosted_documents)
    for link in soup.find_all("a", href=True):
        href = urljoin(source_document, link.get("href"))
        candidates.append(AssetCandidate(href, source_document, anchor_text=link.get_text(" ", strip=True), filename=urlparse(href).path.rsplit("/", 1)[-1]))
        # Download/navigation labels are asset metadata, not property
        # description text (e.g. "Download brochure" under Amenities).
        link.decompose()
    text = soup.get_text("\n", strip=True)
    return BrochureExtraction(source_document, _extract_fields(text, source_document), classify_candidates(candidates))


def _srcset_urls(value: str) -> List[str]:
    """Return every URL from an HTML srcset without choosing one rendition."""
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


def _hosted_document_candidates(soup, source_document: str) -> List[AssetCandidate]:
    """Resolve public document-viewer pages to their downloadable document.

    This is based on the hosting platform, never the property provider.  A
    Google Drive viewer deliberately exposes only a single preview bitmap in
    its HTML; the actual public PDF is required for multi-page media discovery.
    """
    parsed = urlparse(source_document)
    if parsed.hostname not in {"drive.google.com", "docs.google.com"}:
        return []
    match = re.search(r"/file/d/([\w-]+)", parsed.path)
    if not match:
        return []
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = str(soup)
    if ".pdf" not in title.lower() and '"docs-dm":"application/pdf"' not in page_text:
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
                    classification = AssetType.FLOORPLAN if (
                        pdf_images.is_floorplan_page(pages_text[page_number] if page_number < len(pages_text) else "")
                        or pdf_images.is_floorplan_image(content)
                    ) else (AssetType.DECORATIVE if width < 300 or height < 200 else AssetType.PROPERTY_IMAGE)
                    extracted.append(
                        AssetCandidate("", source_document, mime_type=f"image/{base.get('ext', 'png')}", filename=f"brochure-p{page_number + 1}-{digest[:10]}.{base.get('ext', 'png')}", page_number=page_number + 1, classification=classification, confidence=0.9 if classification == AssetType.FLOORPLAN else 0.78, content=content, content_hash=digest, extension=base.get("ext", "png"))
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
        if counts[candidate.content_hash] > 1 and candidate.classification == AssetType.PROPERTY_IMAGE:
            candidate.classification = AssetType.DECORATIVE
            candidate.confidence = 0.8
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


def enrich_properties(
    properties: Iterable[Property],
    fetcher: Callable = fetch_brochure,
    extractor: Callable[[bytes, str, str], BrochureExtraction] = extract_brochure,
    deadline: float = None,
) -> List[Property]:
    properties = list(properties)
    cache = {}
    for prop in properties:
        brochure_url = normalize_url(str(prop.values.get("Brochure PDF") or ""))
        if not brochure_url:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            prop.add_issue(ValidationIssue("Brochure PDF", "Brochure enrichment was skipped because the bounded batch enrichment budget was exhausted.", Severity.INFO, brochure_url, "Primary extraction remains valid; process this file alone for another enrichment attempt.", "brochure_enrichment"))
            continue
        try:
            if brochure_url not in cache:
                cache[brochure_url] = _retrieve(brochure_url, fetcher, extractor)
            _merge(prop, cache[brochure_url])
        except Exception as exc:
            prop.add_issue(
                ValidationIssue(
                    "Brochure PDF",
                    f"Brochure enrichment was skipped: {exc}",
                    Severity.INFO,
                    brochure_url,
                    "Primary extraction remains valid; review the brochure manually if needed.",
                    "brochure_enrichment",
                )
            )
    return properties


def _retrieve(url, fetcher, extractor):
    resource = _coerce_resource(fetcher(url), url)
    combined = extractor(resource.payload, resource.content_type, resource.final_url)
    # HTML/property pages commonly expose the actual downloadable brochure
    # as a PDF link. Follow at most two unique document assets, once each.
    documents = [a.url for a in combined.assets if a.classification == AssetType.BROCHURE and a.url != resource.final_url][:2]
    for document_url in documents:
        try:
            nested = _coerce_resource(fetcher(document_url), document_url)
            nested_extraction = extractor(nested.payload, nested.content_type, nested.final_url)
            for field, value in nested_extraction.fields.items():
                combined.fields.setdefault(field, value)
            combined.assets.extend(nested_extraction.assets)
            combined.warnings.extend(nested_extraction.warnings)
        except Exception as exc:
            combined.warnings.append(f"Linked brochure document could not be enriched: {exc}")
    combined.assets = classify_candidates(combined.assets)
    return combined


def _coerce_resource(value, requested_url):
    if isinstance(value, BrochureResource):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        return BrochureResource(value[0], value[1], value[2])
    if isinstance(value, tuple) and len(value) == 2:
        return BrochureResource(value[0], value[1], requested_url)
    raise TypeError("Brochure fetcher must return BrochureResource or a 2/3-item tuple")


def _merge(prop: Property, extraction: BrochureExtraction) -> None:
    for warning in extraction.warnings:
        prop.add_issue(ValidationIssue("Brochure PDF", warning, Severity.INFO, extraction.source_document, "Primary extraction and any successfully extracted brochure-page evidence were preserved.", "brochure_enrichment"))
    for candidate in classify_candidates(extraction.assets):
        _add_asset(prop, candidate)
    embedded = [a for a in prop.assets if a.content and a.classification in {AssetType.PROPERTY_IMAGE, AssetType.FLOORPLAN}]
    if embedded:
        prop.values["_brochure_embedded_assets"] = embedded
    property_images = [a.url for a in prop.assets if a.classification == AssetType.PROPERTY_IMAGE and a.url]
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
        prop.values["_high_res_candidates"] = list(dict.fromkeys(existing_candidates + property_images))
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
