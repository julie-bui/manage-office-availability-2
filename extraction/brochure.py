"""Provider-neutral brochure extraction and confidence-aware enrichment.

Brochures are secondary evidence.  Failure is isolated per brochure and a
strong primary value is never silently replaced.
"""
from io import BytesIO
import re
from typing import Callable, Iterable, List

import requests

from .assets import classify_candidates, normalize_url
from .address import extract_postcode
from .models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
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
}
_HEADING_RE = re.compile(
    r"^(description|specification|amenities|features|sustainability|epc|lease terms|availability)\s*:?(.*)$",
    re.I,
)
_SIZE_RE = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft)\b", re.I)


def fetch_brochure(url: str, timeout: float = 6.0) -> tuple[bytes, str]:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "OfficeAvailability/1.0"})
    response.raise_for_status()
    payload = response.content
    if len(payload) > MAX_BROCHURE_BYTES:
        raise ValueError("Brochure exceeds the 20MB enrichment limit")
    return payload, response.headers.get("Content-Type", "")


def extract_brochure(payload: bytes, content_type: str, source_document: str) -> BrochureExtraction:
    """Best-effort deterministic PDF extraction; unsupported formats fail safely."""
    if "pdf" not in (content_type or "").lower() and not payload.startswith(b"%PDF"):
        raise ValueError("Brochure content is not an extractable PDF")
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
    return BrochureExtraction(source_document, fields, classify_candidates(links))


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
    fetcher: Callable[[str], tuple[bytes, str]] = fetch_brochure,
    extractor: Callable[[bytes, str, str], BrochureExtraction] = extract_brochure,
) -> List[Property]:
    properties = list(properties)
    cache = {}
    for prop in properties:
        brochure_url = normalize_url(str(prop.values.get("Brochure PDF") or ""))
        if not brochure_url:
            continue
        try:
            if brochure_url not in cache:
                payload, content_type = fetcher(brochure_url)
                cache[brochure_url] = extractor(payload, content_type, brochure_url)
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


def _merge(prop: Property, extraction: BrochureExtraction) -> None:
    for candidate in classify_candidates(extraction.assets):
        _add_asset(prop, candidate)
    property_images = [a.url for a in prop.assets if a.classification == AssetType.PROPERTY_IMAGE]
    floorplans = [a.url for a in prop.assets if a.classification == AssetType.FLOORPLAN]
    if not prop.values.get("High Res Images") and property_images:
        _apply(prop, "High Res Images", _evidence(property_images[0], extraction.source_document, 0.82))
        # The web layer turns 2+ externally hosted candidates into its
        # existing gallery page; direct/library callers still receive the
        # safe first photo in the public spreadsheet field above.
        prop.values["_high_res_candidates"] = property_images
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
