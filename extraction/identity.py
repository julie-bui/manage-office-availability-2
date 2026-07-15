"""Provider-neutral property identity evidence and linked-resource gate."""
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import List

from .address import extract_postcode
from .address_resolution import building_numbers_match, parse_address


class IdentityDecision(str, Enum):
    MATCH = "MATCH"
    PROBABLE_MATCH = "PROBABLE_MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    HARD_CONFLICT = "HARD_CONFLICT"


@dataclass(frozen=True)
class IdentityResult:
    decision: IdentityDecision
    reasons: List[str] = field(default_factory=list)
    primary_postcode: str = ""
    linked_postcodes: tuple = ()
    primary_number: str = ""
    primary_street: str = ""


_NUMBERED_STREET_RE = re.compile(
    r"\b(\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?)?)\s+"
    r"([A-Z][A-Z'?.-]*(?:\s+[A-Z][A-Z'?.-]*){0,4}\s+"
    r"(?:STREET|ST|ROAD|RD|LANE|LN|SQUARE|SQ|AVENUE|AVE|PLACE|PL|COURT|CT|"
    r"WAY|YARD|GARDENS|TERRACE|MEWS|CIRCUS))\b",
    re.I,
)


def property_key(values) -> str:
    return " | ".join(
        str(values.get(field) or "").strip()
        for field in ("Building", "Property Address 1", "Property Postcode", "Floor/Unit")
        if values.get(field)
    )


def compare_property_identity(values, linked_text: str, association_confidence: float = 0.0) -> IdentityResult:
    """Classify linked content before any field or asset can be merged."""
    primary_text = " ".join(
        str(values.get(field) or "")
        for field in ("Building", "Property Address 1", "Property Address 2", "Property Postcode")
    )
    primary = parse_address(primary_text)
    primary_postcode = extract_postcode(primary_text)
    linked_text = "\n".join(" ".join(line.split()) for line in str(linked_text or "").splitlines())
    linked_postcodes = tuple(sorted(set(filter(None, (extract_postcode(line) for line in linked_text.splitlines())))))
    normalized_linked_postcodes = {re.sub(r"\s+", "", value.upper()) for value in linked_postcodes}
    normalized_primary_postcode = re.sub(r"\s+", "", primary_postcode.upper())
    linked_streets = []
    for match in _NUMBERED_STREET_RE.finditer(linked_text):
        parsed = parse_address(match.group(0))
        if parsed.building_number and parsed.street:
            linked_streets.append((parsed.building_number, parsed.street))
    reasons = []
    if normalized_primary_postcode and normalized_linked_postcodes and normalized_primary_postcode not in normalized_linked_postcodes:
        return IdentityResult(
            IdentityDecision.HARD_CONFLICT,
            [f"postcode conflict: source {primary_postcode}, linked {', '.join(linked_postcodes)}"],
            primary_postcode, linked_postcodes, primary.building_number, primary.street,
        )
    if primary.building_number and primary.street and linked_streets:
        matching = [
            number for number, street in linked_streets
            if street == primary.street and building_numbers_match(primary.building_number, number)
        ]
        if not matching:
            sample = ", ".join(f"{number} {street}" for number, street in linked_streets[:3])
            return IdentityResult(
                IdentityDecision.HARD_CONFLICT,
                [f"numbered-street conflict: source {primary.building_number} {primary.street}, linked {sample}"],
                primary_postcode, linked_postcodes, primary.building_number, primary.street,
            )
        reasons.append("exact numbered street")
    if normalized_primary_postcode and normalized_primary_postcode in normalized_linked_postcodes:
        reasons.append("exact postcode")
    if reasons:
        return IdentityResult(IdentityDecision.MATCH, reasons, primary_postcode, linked_postcodes, primary.building_number, primary.street)
    primary_name = primary.building_name or re.sub(r"[^a-z0-9]+", " ", primary_text.lower()).strip()
    linked_words = re.sub(r"[^a-z0-9]+", " ", linked_text.lower()).strip()
    meaningful = [word for word in primary_name.split() if len(word) > 3 and word not in {"london", "floor"}]
    if meaningful and sum(word in linked_words for word in meaningful) >= min(2, len(meaningful)):
        return IdentityResult(IdentityDecision.PROBABLE_MATCH, ["building-name agreement"], primary_postcode, linked_postcodes, primary.building_number, primary.street)
    if association_confidence >= 0.8 and not linked_postcodes and not linked_streets:
        return IdentityResult(IdentityDecision.PROBABLE_MATCH, ["strong structural association; no conflicting linked identity"], primary_postcode, linked_postcodes, primary.building_number, primary.street)
    return IdentityResult(IdentityDecision.AMBIGUOUS, ["insufficient property identity evidence"], primary_postcode, linked_postcodes, primary.building_number, primary.street)
