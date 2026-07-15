"""Shared, provider-neutral address identity matching and resolution."""
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Callable, Iterable, List, Optional

from .address import extract_postcode


class AddressResolutionStatus(str, Enum):
    RESOLVED_FROM_SOURCE = "RESOLVED_FROM_SOURCE"
    RESOLVED_FROM_BROCHURE = "RESOLVED_FROM_BROCHURE"
    RESOLVED_FROM_PROPERTY_PAGE = "RESOLVED_FROM_PROPERTY_PAGE"
    RESOLVED_FROM_VALIDATED_LOOKUP = "RESOLVED_FROM_VALIDATED_LOOKUP"
    CONFLICTING_CANDIDATES = "CONFLICTING_CANDIDATES"
    NO_VALID_CANDIDATE = "NO_VALID_CANDIDATE"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass(frozen=True)
class AddressComponents:
    building_number: str = ""
    building_name: str = ""
    street: str = ""
    locality: str = ""
    postcode: str = ""


@dataclass
class AddressCandidate:
    address: str
    source: str
    components: AddressComponents = field(default_factory=AddressComponents)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    raw: Any = None

    def __post_init__(self):
        if self.components == AddressComponents():
            self.components = parse_address(self.address)


@dataclass
class CandidateAssessment:
    candidate: AddressCandidate
    score: float
    rejected: bool = False
    reasons: List[str] = field(default_factory=list)


@dataclass
class AddressResolution:
    original_address: str
    original_postcode: str
    status: AddressResolutionStatus
    selected_candidate: Optional[AddressCandidate] = None
    confidence: float = 0.0
    query_variants: List[str] = field(default_factory=list)
    candidates_considered: List[CandidateAssessment] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    final_address_source: str = ""
    final_postcode_source: str = ""
    resolution_reason: Optional[AddressResolutionStatus] = None


_NUMBER_RE = re.compile(r"\b(\d+[A-Za-z]?(?:\s*-\s*\d+[A-Za-z]?)?)\b")
_STREET_RE = re.compile(
    r"(?:\b\d+[A-Za-z]?(?:\s*-\s*\d+[A-Za-z]?)?\s+)?"
    r"([A-Za-z][A-Za-z' .-]*?\b(?:road|rd|street|st|lane|ln|place|pl|square|sq|"
    r"avenue|ave|yard|way|bridge|close|court|ct|gardens|terrace|mews|circus))\b",
    re.I,
)
_LOCALITY_RE = re.compile(r"\b(london|city of london|southwark|westminster|camden|islington)\b", re.I)
_STREET_WORDS = {"road": "road", "rd": "road", "street": "street", "st": "street", "place": "place", "pl": "place", "avenue": "avenue", "ave": "avenue", "lane": "lane", "ln": "lane", "square": "square", "sq": "square", "court": "court", "ct": "court"}


def parse_address(value: str) -> AddressComponents:
    text = " ".join(str(value or "").replace("\n", " ").split())
    postcode = extract_postcode(text)
    identity_text = re.sub(re.escape(postcode), "", text, flags=re.I) if postcode else text
    number_match = _NUMBER_RE.search(identity_text)
    number = number_match.group(1).replace(" ", "").upper() if number_match else ""
    street_match = _STREET_RE.search(text)
    street = _normalize_street(street_match.group(1) if street_match else "")
    locality_match = _LOCALITY_RE.search(text)
    locality = locality_match.group(1).lower() if locality_match else ""
    first_segment = text.split(",", 1)[0].strip()
    building_name = ""
    if street_match and first_segment and not _STREET_RE.search(first_segment):
        building_name = _normalize_words(first_segment)
    return AddressComponents(number, building_name, street, locality, postcode)


def generate_query_variants(address: str, postcode: str = "", building_name: str = "", locality: str = "London") -> List[str]:
    components = parse_address(address)
    number_street = " ".join(filter(None, [components.building_number, components.street])).strip()
    values = [address, number_street, f"{number_street}, {locality}" if number_street else ""]
    if postcode:
        values.extend([f"{address}, {postcode}", f"{number_street}, {postcode}" if number_street else ""])
    if building_name and components.street:
        values.extend([f"{building_name}, {components.street}", f"{building_name}, {components.street}, {postcode}" if postcode else ""])
    return _dedupe(values)


def assess_candidate(requested: AddressComponents, candidate: AddressCandidate) -> CandidateAssessment:
    actual = candidate.components
    reasons, score = [], 0.0
    if requested.building_number:
        if not actual.building_number:
            return CandidateAssessment(candidate, 0, True, ["candidate missing requested building number"])
        if not building_numbers_match(requested.building_number, actual.building_number):
            return CandidateAssessment(candidate, 0, True, [f"building number conflict: requested {requested.building_number}, candidate {actual.building_number}"])
        score += 45
    if requested.street:
        if not actual.street:
            return CandidateAssessment(candidate, 0, True, ["candidate missing requested street"])
        if requested.street != actual.street:
            return CandidateAssessment(candidate, 0, True, [f"street conflict: requested {requested.street}, candidate {actual.street}"])
        score += 40
    if requested.building_name and actual.building_name and requested.building_name == actual.building_name:
        score += 8
    if requested.locality and actual.locality and requested.locality == actual.locality:
        score += 4
    if requested.postcode and actual.postcode:
        score += 10 if requested.postcode == actual.postcode else -5
    return CandidateAssessment(candidate, score, False, reasons)


def rank_candidates(requested_address: str, candidates: Iterable[AddressCandidate]) -> List[CandidateAssessment]:
    requested = parse_address(requested_address)
    return sorted((assess_candidate(requested, candidate) for candidate in candidates), key=lambda item: item.score, reverse=True)


def resolve_address(
    original_address: str,
    original_postcode: str = "",
    trusted_evidence: Iterable[AddressCandidate] = (),
    candidate_provider: Optional[Callable[[str], Iterable[AddressCandidate]]] = None,
) -> AddressResolution:
    source_postcode = extract_postcode(original_postcode) or extract_postcode(original_address)
    if source_postcode:
        selected = AddressCandidate(original_address, "source_file")
        selected.components = AddressComponents(**{**vars(selected.components), "postcode": source_postcode})
        return AddressResolution(original_address, source_postcode, AddressResolutionStatus.RESOLVED_FROM_SOURCE, selected, 1.0, evidence_sources=["source_file"], final_address_source="source_file", final_postcode_source="source_file")

    queries = generate_query_variants(original_address)
    candidates = list(trusted_evidence)
    for query in queries:
        if candidate_provider:
            candidates.extend(candidate_provider(query) or [])
    candidates = _dedupe_candidates(candidates)
    assessments = rank_candidates(original_address, candidates)
    valid = [item for item in assessments if not item.rejected and item.score >= 80 and item.candidate.components.postcode]
    if not valid:
        reason = AddressResolutionStatus.NO_VALID_CANDIDATE
        return AddressResolution(
            original_address,
            "",
            AddressResolutionStatus.MANUAL_REVIEW_REQUIRED,
            query_variants=queries,
            candidates_considered=assessments,
            resolution_reason=reason,
        )

    strongest = valid[0]
    same_identity = [item for item in valid if item.candidate.components.postcode == strongest.candidate.components.postcode]
    conflicting = [item for item in valid if item.candidate.components.postcode != strongest.candidate.components.postcode and item.score >= strongest.score - 5]
    if conflicting:
        return AddressResolution(original_address, "", AddressResolutionStatus.CONFLICTING_CANDIDATES, query_variants=queries, candidates_considered=assessments)
    sources = _dedupe([item.candidate.source for item in same_identity])
    source = strongest.candidate.source
    if source == "brochure":
        status = AddressResolutionStatus.RESOLVED_FROM_BROCHURE
    elif source in {"property_page", "agent_listing", "landlord_page"}:
        status = AddressResolutionStatus.RESOLVED_FROM_PROPERTY_PAGE
    else:
        status = AddressResolutionStatus.RESOLVED_FROM_VALIDATED_LOOKUP
    confidence = min(1.0, strongest.score / 100 + max(0, len(sources) - 1) * 0.08)
    return AddressResolution(original_address, "", status, strongest.candidate, confidence, queries, assessments, sources, source, source)


def building_numbers_match(requested: str, candidate: str) -> bool:
    requested = requested.upper().replace(" ", "")
    candidate = candidate.upper().replace(" ", "")
    if requested == candidate:
        return True
    range_match = re.fullmatch(r"(\d+)-(\d+)", candidate)
    if range_match and requested.isdigit():
        return int(range_match.group(1)) <= int(requested) <= int(range_match.group(2))
    requested_range = re.fullmatch(r"(\d+)-(\d+)", requested)
    return bool(requested_range and candidate.isdigit() and int(requested_range.group(1)) <= int(candidate) <= int(requested_range.group(2)))


def _normalize_words(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _normalize_street(value):
    words = _normalize_words(value).split()
    return " ".join(_STREET_WORDS.get(word, word) for word in words)


def _dedupe(values):
    seen, result = set(), []
    for value in values:
        key = " ".join(str(value or "").lower().split())
        if key and key not in seen:
            seen.add(key)
            result.append(str(value).strip())
    return result


def _dedupe_candidates(candidates):
    seen, result = set(), []
    for candidate in candidates:
        key = (candidate.source, _normalize_words(candidate.address))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result
