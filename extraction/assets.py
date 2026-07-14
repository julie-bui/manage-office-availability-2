"""Deterministic asset discovery, normalization, classification and assignment."""
import re
from pathlib import PurePosixPath
from typing import Iterable, List, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import AssetCandidate, AssetType

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx"}
_TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "mc_cid", "mc_eid"}
_FLOORPLAN_RE = re.compile(r"floor[\s_-]*plan|floorplan|layout", re.I)
_BROCHURE_RE = re.compile(r"brochure|particulars|marketing[\s_-]*details", re.I)
_LOGO_RE = re.compile(r"logo|brandmark|signature|avatar|headshot", re.I)
_MAP_RE = re.compile(r"map|location[\s_-]*plan|streetview", re.I)
_DECORATIVE_RE = re.compile(r"pixel|tracking|spacer|social|icon|divider|transparent", re.I)


def normalize_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in _TRACKING_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, urlencode(query), ""))


def deduplicate_candidates(candidates: Iterable[AssetCandidate]) -> List[AssetCandidate]:
    unique = []
    seen = set()
    for candidate in candidates:
        normalized = normalize_url(candidate.url)
        if not normalized or normalized in seen:
            continue
        candidate.url = normalized
        seen.add(normalized)
        unique.append(candidate)
    return unique


def classify_candidate(candidate: AssetCandidate) -> AssetCandidate:
    parsed = urlparse(candidate.url)
    filename = candidate.filename or PurePosixPath(parsed.path).name
    context = " ".join(filter(None, [filename, candidate.anchor_text, candidate.alt_text])).lower()
    extension = PurePosixPath(parsed.path).suffix.lower()
    mime = (candidate.mime_type or "").lower()

    if _DECORATIVE_RE.search(context):
        classification, confidence = AssetType.DECORATIVE, 0.98
    elif _LOGO_RE.search(context):
        classification, confidence = AssetType.LOGO, 0.98
    elif _MAP_RE.search(context):
        classification, confidence = AssetType.MAP, 0.92
    elif _FLOORPLAN_RE.search(context):
        classification, confidence = AssetType.FLOORPLAN, 0.95
    elif _BROCHURE_RE.search(context) and extension not in _IMAGE_EXTENSIONS:
        classification, confidence = AssetType.BROCHURE, 0.95
    elif extension in _DOCUMENT_EXTENSIONS or mime == "application/pdf":
        classification, confidence = AssetType.BROCHURE, 0.78
    elif extension in _IMAGE_EXTENSIONS or mime.startswith("image/"):
        classification, confidence = AssetType.PROPERTY_IMAGE, 0.72
    else:
        classification, confidence = AssetType.UNKNOWN, 0.0

    candidate.classification = classification
    candidate.confidence = confidence
    return candidate


def classify_candidates(candidates: Iterable[AssetCandidate]) -> List[AssetCandidate]:
    return [classify_candidate(item) for item in deduplicate_candidates(candidates)]


def candidates_from_html_items(items: Sequence[tuple], source: str) -> List[AssetCandidate]:
    candidates = []
    for kind, text, url in items or []:
        candidates.append(
            AssetCandidate(
                url=url,
                source=source,
                anchor_text=text if kind == "link" else None,
                alt_text=text if kind == "image" else None,
                mime_type="image/*" if kind == "image" else None,
            )
        )
    return classify_candidates(candidates)
