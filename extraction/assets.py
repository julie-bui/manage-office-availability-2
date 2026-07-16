"""Deterministic asset discovery, normalization, classification and assignment."""
import re
import hashlib
import time
from io import BytesIO
from pathlib import PurePosixPath
from typing import Callable, Dict, Iterable, List, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .html_images import _IMAGE_FETCH_TIMEOUT_SECONDS
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
# CMS/CDN asset paths often omit a file extension (Directus /assets/{uuid}).
_IMAGE_LIKE_PATH_RE = re.compile(
    r"/assets?/|/media/|/digitalassets?/|/static/(?:img|images|media)/|"
    r"/img/|/photos?/|/gallery/",
    re.I,
)
_UNSTABLE_RE = re.compile(r"(?:^|[?&])(expires?|signature|token|x-amz-expires)=", re.I)
MIN_PROPERTY_IMAGE_WIDTH = 320
MIN_PROPERTY_IMAGE_HEIGHT = 200
MAX_VALIDATION_BYTES = 8 * 1024 * 1024
# Near-solid slides (MetSpace Drive PDFs ship near-black placeholder pages)
# have a dominant colour and very few distinct colours once downsampled.
# Tuned against confirmed blank 4000×2250 Drive embeds (dominant ≥ 55%,
# uniq ≤ 66) vs real interior photos from the same PDFs (uniq ≥ 1500,
# dominant colour ≪ 5%).
_BLANK_SAMPLE_SIZE = (64, 36)
_BLANK_MAX_UNIQUE_COLORS = 80
_BLANK_MAX_LUMINANCE_VARIANCE = 1500.0
_BLANK_DOMINANT_FRACTION = 0.55
# MetSpace Drive PDFs embed 2666×1499 / 4000×2250 PNGs that look blank in
# galleries but fail the dominant-colour blank check (sparse logos/lines on
# a wash). Real photos of that size are ≫0.02 bytes/pixel; these sit ~0.007.
_SPARSE_MIN_PIXELS = 800_000
_SPARSE_MAX_BYTES_PER_PIXEL = 0.02
_SPARSE_MAX_UNIQUE_COLORS = 120


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
    # Confirmed real (2026-07, Knotel property pages): Next.js serves
    # Directus assets via /_next/image?url=<encoded>&w=… — keep the underlying
    # asset URL so gallery dedupe/validation see one real photo, not five
    # resized wrappers of the same image.
    if "/_next/image" in (parsed.path or "").lower():
        from urllib.parse import unquote

        inner = dict(parse_qsl(parsed.query, keep_blank_values=True)).get("url")
        if inner:
            unwrapped = normalize_url(unquote(inner))
            if unwrapped:
                return unwrapped
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
    elif extension in _IMAGE_EXTENSIONS or mime.startswith("image/") or _IMAGE_LIKE_PATH_RE.search(parsed.path or ""):
        # Repetition alone does not make a strongly property-associated
        # brochure image decorative. The same photo is often reused on a
        # cover and an availability page; it still needs to reach the gallery.
        # Extensionless /assets/ (and similar CDN) paths count as images by
        # path evidence — never skip them solely because the host is new.
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


def image_content_hash(payload: bytes) -> str:
    """Exact content identity for gallery dedupe across distinct CDN/asset URLs."""
    return hashlib.sha256(payload or b"").hexdigest()


def is_blank_or_empty_image(payload: bytes) -> bool:
    """True for near-solid / sparse placeholder slides that must never enter High Res.

    Confirmed real (2026-07, MetSpace Drive brochure PDFs): several pages are
    near-black / near-solid 4000×2250 embeds with almost no visual content,
    plus sparse PNG "wash" slides (~0.007 bytes/pixel, ≤120 colours) that
    look blank in galleries but fail a pure dominant-colour check.
    Floor-plan diagrams are intentionally NOT handled here — callers that
    need diagrams use pdf_images.is_floorplan_image / Floor Plan assignment.
    """
    if not payload:
        return True
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
            sample = image.convert("RGB").resize(_BLANK_SAMPLE_SIZE)
            # Pillow 10+ prefers get_flattened_data; keep getdata fallback.
            getter = getattr(sample, "get_flattened_data", None) or sample.getdata
            raw = list(getter())
            if raw and not isinstance(raw[0], tuple):
                pixels = list(zip(raw[0::3], raw[1::3], raw[2::3]))
            else:
                pixels = raw
    except Exception:
        return False
    if not pixels:
        return True
    from collections import Counter

    counts = Counter(pixels)
    unique = len(counts)
    dominant_fraction = counts.most_common(1)[0][1] / len(pixels)
    pixels_total = max(1, int(width or 0) * int(height or 0))
    bytes_per_pixel = len(payload) / pixels_total
    # Huge, extremely compressible embeds = empty marketing slides / washes.
    if (
        pixels_total >= _SPARSE_MIN_PIXELS
        and bytes_per_pixel <= _SPARSE_MAX_BYTES_PER_PIXEL
        and unique <= _SPARSE_MAX_UNIQUE_COLORS
    ):
        return True
    if dominant_fraction >= _BLANK_DOMINANT_FRACTION and unique <= 120:
        return True
    if unique <= _BLANK_MAX_UNIQUE_COLORS:
        luminances = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
        mean = sum(luminances) / len(luminances)
        variance = sum((value - mean) ** 2 for value in luminances) / len(luminances)
        return variance <= _BLANK_MAX_LUMINANCE_VARIANCE
    return False


def evaluate_image_bytes(payload: bytes, url: str = "", content_type: str = "") -> dict:
    """Shared decode/size/blank checks for remote validation and local batch assets."""
    if not payload:
        return {"ok": False, "status": "IMAGE_BLANK_OR_EMPTY", "url": url, "content_hash": image_content_hash(b"")}
    if len(payload) > MAX_VALIDATION_BYTES:
        return {"ok": False, "status": "IMAGE_TOO_LARGE", "url": url}
    content_hash = image_content_hash(payload)
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
            image.load()
    except Exception as exc:
        return {"ok": False, "status": "NOT_AN_IMAGE", "url": url, "detail": str(exc), "content_hash": content_hash}
    if width < MIN_PROPERTY_IMAGE_WIDTH or height < MIN_PROPERTY_IMAGE_HEIGHT:
        return {
            "ok": False,
            "status": "IMAGE_TOO_SMALL",
            "url": url,
            "width": width,
            "height": height,
            "content_hash": content_hash,
            "content_type": content_type,
        }
    # Floor-plan diagrams are intentionally near-white — classify them
    # before the blank-slide rejector so MetSpace Drive plans are not
    # dropped as IMAGE_BLANK_OR_EMPTY (and never reach Floor Plan).
    from . import pdf_images

    if pdf_images.is_floorplan_image(payload):
        return {
            "ok": False,
            "status": "IMAGE_IS_FLOORPLAN",
            "url": url,
            "width": width,
            "height": height,
            "content_hash": content_hash,
            "content_type": content_type,
        }
    if is_blank_or_empty_image(payload):
        return {
            "ok": False,
            "status": "IMAGE_BLANK_OR_EMPTY",
            "url": url,
            "width": width,
            "height": height,
            "content_hash": content_hash,
            "content_type": content_type,
        }
    return {
        "ok": True,
        "status": "VALID_IMAGE",
        "url": url,
        "width": width,
        "height": height,
        "content_hash": content_hash,
        "content_type": content_type,
    }


def _fetch_and_evaluate(url: str, timeout: float, requester: Callable) -> dict:
    """One real fetch attempt. Raises on network/HTTP failure; returns a
    result dict for every outcome that did reach a response (too large, not
    an image, too small, blank, valid)."""
    response = requester(
        url,
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
        return {"ok": False, "status": "IMAGE_TOO_LARGE", "url": url}
    if not content_type.startswith("image/"):
        return {"ok": False, "status": "NOT_AN_IMAGE", "url": url, "content_type": content_type}
    final_url = normalize_url(response.url or url) or url
    result = evaluate_image_bytes(payload, url=final_url, content_type=content_type)
    result["unstable"] = bool(_UNSTABLE_RE.search(response.url or url))
    return result


def validate_image_url(
    url: str,
    timeout: float = _IMAGE_FETCH_TIMEOUT_SECONDS,
    requester: Callable = requests.get,
    cache: Dict[str, dict] = None,
    deadline: float = None,
) -> dict:
    """Bounded, cached validation used once per canonical external image URL.

    Shares its timeout budget with extraction.html_images.is_floorplan_image_url
    (the classification-time fetch of these same URLs) rather than duplicating
    a shorter one of its own — confirmed real (2026-07, MetSpace mcusercontent.com
    photos): a real, live image that classification's own longer timeout
    tolerated was still being rejected here as "expired/inaccessible" for
    needing a few more seconds than this function alone was willing to wait.

    A timeout specifically (as opposed to a real 404/DNS failure/other error)
    gets one retry — but only when `deadline` (the caller's own batch/
    enrichment deadline, a time.monotonic() timestamp) leaves enough budget
    for a full second attempt; never risk overrunning a deadline this app
    already depends on elsewhere just to retry an optional image fetch.
    """
    normalized = normalize_url(url)
    cache = cache if cache is not None else {}
    if not normalized:
        return {"ok": False, "status": "INVALID_URL", "url": url}
    if normalized in cache:
        return cache[normalized]
    try:
        result = _fetch_and_evaluate(normalized, timeout, requester)
    except requests.exceptions.Timeout as exc:
        if deadline is None or time.monotonic() + timeout <= deadline:
            try:
                result = _fetch_and_evaluate(normalized, timeout, requester)
            except requests.exceptions.Timeout as retry_exc:
                result = {"ok": False, "status": "LINK_TIMED_OUT", "url": normalized, "detail": str(retry_exc)}
            except Exception as retry_exc:
                result = {"ok": False, "status": "LINK_EXPIRED_OR_INACCESSIBLE", "url": normalized, "detail": str(retry_exc)}
        else:
            result = {"ok": False, "status": "LINK_TIMED_OUT", "url": normalized, "detail": str(exc)}
    except Exception as exc:
        result = {"ok": False, "status": "LINK_EXPIRED_OR_INACCESSIBLE", "url": normalized, "detail": str(exc)}
    cache[normalized] = result
    return result
