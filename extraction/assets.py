"""Deterministic asset discovery, normalization, classification and assignment."""
import re
import hashlib
from io import BytesIO
from pathlib import PurePosixPath
from typing import Callable, Dict, Iterable, List, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .models import AssetCandidate, AssetType

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx"}
_TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "mc_cid", "mc_eid"}
_RENDITION_KEYS = {"width", "height", "format", "quality", "fit", "crop", "dpr", "v"}
_FLOORPLAN_RE = re.compile(r"floor[\s_-]*plan|floorplan|layout", re.I)
_BROCHURE_RE = re.compile(r"brochure|particulars|marketing[\s_-]*details|download\s*(?:pdf|details|document)", re.I)
_LOGO_RE = re.compile(r"logo|brandmark|signature|avatar|headshot", re.I)
_MAP_RE = re.compile(r"map|location[\s_-]*plan|streetview", re.I)
_DECORATIVE_RE = re.compile(r"pixel|tracking|spacer|social|icon|divider|transparent", re.I)
_DOCUMENT_PREVIEW_RE = re.compile(r"preview|thumbnail|thumb|document[-_ ]?(?:cover|page)|og[-_ ]?image", re.I)
_PROPERTY_RE = re.compile(
    r"reception|office|workspace|interior|exterior|terrace|meeting|boardroom|"
    r"breakout|kitchen|building|facade|fa?ade|gallery|hero|desk|lounge|property",
    re.I,
)
_UNSTABLE_RE = re.compile(r"(?:^|[?&])(expires?|signature|token|x-amz-expires)=", re.I)
MIN_PROPERTY_IMAGE_WIDTH = 320
MIN_PROPERTY_IMAGE_HEIGHT = 200
MAX_VALIDATION_BYTES = 8 * 1024 * 1024


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
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in _TRACKING_KEYS | _RENDITION_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, urlencode(query), ""))


def deduplicate_candidates(candidates: Iterable[AssetCandidate]) -> List[AssetCandidate]:
    unique = []
    seen = set()
    for candidate in candidates:
        normalized = normalize_url(candidate.url)
        content_key = candidate.content_hash or (hashlib.sha256(candidate.content).hexdigest() if candidate.content else "")
        key = normalized or (f"embedded:{content_key}" if content_key else "")
        if not key or key in seen:
            continue
        candidate.url = normalized
        candidate.content_hash = content_key or None
        seen.add(key)
        unique.append(candidate)
    return unique


def classify_candidate(candidate: AssetCandidate) -> AssetCandidate:
    if candidate.classification != AssetType.UNKNOWN and candidate.confidence > 0:
        return candidate
    parsed = urlparse(candidate.url)
    filename = candidate.filename or PurePosixPath(parsed.path).name
    context = " ".join(
        filter(
            None,
            [
                filename,
                candidate.anchor_text,
                candidate.alt_text,
                candidate.surrounding_text,
                candidate.source_section,
                candidate.html_container,
            ],
        )
    ).lower()
    extension = PurePosixPath(parsed.path).suffix.lower()
    mime = (candidate.mime_type or "").lower()

    if candidate.width is not None and candidate.height is not None and (
        candidate.width <= 8 or candidate.height <= 8
    ):
        classification, confidence = AssetType.TRACKING_OR_DECORATIVE, 1.0
    elif _DECORATIVE_RE.search(context):
        classification, confidence = AssetType.DECORATIVE, 0.98
    elif _LOGO_RE.search(context):
        classification, confidence = AssetType.LOGO, 0.98
    elif _DOCUMENT_PREVIEW_RE.search(context):
        classification, confidence = AssetType.DOCUMENT_PREVIEW, 0.94
    elif _MAP_RE.search(context):
        classification, confidence = AssetType.MAP, 0.92
    elif _FLOORPLAN_RE.search(context):
        classification, confidence = AssetType.FLOORPLAN, 0.95
    elif _BROCHURE_RE.search(context) and extension not in _IMAGE_EXTENSIONS:
        classification, confidence = AssetType.BROCHURE, 0.95
    elif extension in _DOCUMENT_EXTENSIONS or mime == "application/pdf":
        if _BROCHURE_RE.search(context):
            classification, confidence = AssetType.BROCHURE, 0.84
        else:
            classification, confidence = AssetType.UNKNOWN, 0.2
    elif extension in _IMAGE_EXTENSIONS or mime.startswith("image/"):
        # Repetition alone does not make a strongly property-associated
        # brochure image decorative. The same photo is often reused on a
        # cover and an availability page; it still needs to reach the gallery.
        if (
            candidate.occurrence_count > 1
            and candidate.association_confidence < 0.8
            and not _PROPERTY_RE.search(
                " ".join(filter(None, [candidate.alt_text, candidate.anchor_text, filename]))
            )
        ):
            classification, confidence = AssetType.DECORATIVE, 0.9
        elif (
            candidate.width is not None
            and candidate.height is not None
            and (candidate.width < MIN_PROPERTY_IMAGE_WIDTH or candidate.height < MIN_PROPERTY_IMAGE_HEIGHT)
        ):
            classification, confidence = AssetType.DECORATIVE, 0.86
        elif _PROPERTY_RE.search(context) or candidate.association_confidence >= 0.8:
            classification, confidence = AssetType.PROPERTY_IMAGE, 0.82
        else:
            classification, confidence = AssetType.UNKNOWN, 0.25
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


def merge_candidate_urls(*groups: Iterable[str]) -> List[str]:
    """Stable union: preserve all distinct canonical URLs, never overwrite."""
    result, seen = [], set()
    for group in groups:
        for raw in group or []:
            normalized = normalize_url(str(raw or ""))
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return result


def validate_image_url(
    url: str,
    timeout: float = 3.0,
    requester: Callable = requests.get,
    cache: Dict[str, dict] = None,
) -> dict:
    """Bounded, cached validation used once per canonical external image URL."""
    normalized = normalize_url(url)
    cache = cache if cache is not None else {}
    if not normalized:
        return {"ok": False, "status": "INVALID_URL", "url": url}
    if normalized in cache:
        return cache[normalized]
    try:
        response = requester(
            normalized,
            timeout=timeout,
            headers={"User-Agent": "OfficeAvailability/1.0", "Range": f"bytes=0-{MAX_VALIDATION_BYTES - 1}"},
            allow_redirects=True,
            stream=True,
        )
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if hasattr(response, "iter_content"):
            chunks, total = [], 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_VALIDATION_BYTES:
                    break
            payload = b"".join(chunks)
        else:
            payload = response.content
        if len(payload) > MAX_VALIDATION_BYTES:
            result = {"ok": False, "status": "IMAGE_TOO_LARGE", "url": normalized}
        elif not content_type.startswith("image/"):
            result = {"ok": False, "status": "NOT_AN_IMAGE", "url": normalized, "content_type": content_type}
        else:
            from PIL import Image
            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
                image.verify()
            final_url = normalize_url(response.url or normalized)
            result = {
                "ok": width >= MIN_PROPERTY_IMAGE_WIDTH and height >= MIN_PROPERTY_IMAGE_HEIGHT,
                "status": "VALID_IMAGE" if width >= MIN_PROPERTY_IMAGE_WIDTH and height >= MIN_PROPERTY_IMAGE_HEIGHT else "IMAGE_TOO_SMALL",
                "url": final_url or normalized,
                "width": width,
                "height": height,
                "content_type": content_type,
                "unstable": bool(_UNSTABLE_RE.search(response.url or normalized)),
            }
    except Exception as exc:
        result = {"ok": False, "status": "LINK_EXPIRED_OR_INACCESSIBLE", "url": normalized, "detail": str(exc)}
    cache[normalized] = result
    return result
