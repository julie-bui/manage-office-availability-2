"""Validation rules that never erase source values or fail a whole batch."""
import re
from typing import Iterable, List
from urllib.parse import urlparse

from .address import extract_postcode
from .assets import normalize_url
from .models import Property, Severity, ValidationIssue

_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}$")
_IMAGE_RE = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:[?#]|$)", re.I)


def validate_url_field(field: str, value: object, forbidden_kind: str = "") -> List[ValidationIssue]:
    if value in (None, ""):
        return []
    urls = value if isinstance(value, (list, tuple)) else [value]
    issues = []
    for url in urls:
        normalized = normalize_url(str(url))
        if not normalized:
            issues.append(ValidationIssue(field, "Value is not a valid HTTP(S) URL.", Severity.ERROR, url))
        elif forbidden_kind == "image" and _IMAGE_RE.search(urlparse(normalized).path):
            issues.append(ValidationIssue(field, "An image URL was placed in a document field.", Severity.ERROR, url))
    return issues


def validate_property(prop: Property) -> Property:
    values = prop.values
    if not prop.source_file_name:
        prop.add_issue(ValidationIssue("Source file", "Source filename was lost before export.", Severity.ERROR, ""))
    if prop.source_file_url:
        source_url_issues = validate_url_field("Source file", prop.source_file_url)
        for issue in source_url_issues:
            prop.add_issue(issue)
    elif prop.source_url_expected:
        prop.add_issue(
            ValidationIssue(
                "Source file",
                "A hosted source URL was expected but is unavailable.",
                Severity.WARNING,
                prop.source_file_name,
                "Verify source persistence/object storage and regenerate the export.",
                "source_validation",
            )
        )
    building = str(values.get("Building") or "").strip()
    address = str(values.get("Property Address 1") or "").strip()
    postcode_value = str(values.get("Property Postcode") or "").strip()
    postcode = extract_postcode(postcode_value)

    if not building:
        prop.add_issue(ValidationIssue("Building", "Building/property name is missing.", Severity.ERROR, building))
    if not address:
        prop.add_issue(ValidationIssue("Property Address 1", "Address is missing.", Severity.ERROR, address))
    if values.get("Size (sq ft)") in (None, ""):
        prop.add_issue(
            ValidationIssue(
                "Size (sq ft)",
                "Size was not present in the source record.",
                Severity.WARNING,
                values.get("Size (sq ft)"),
                "Confirm the size from the brochure or provider before upload.",
            )
        )
    if postcode_value and "manual lookup" not in postcode_value.lower() and not postcode:
        prop.add_issue(ValidationIssue("Property Postcode", "Postcode is not a valid UK postcode.", Severity.ERROR, postcode_value))
    if "manual lookup" in postcode_value.lower():
        prop.add_issue(
            ValidationIssue(
                "Property Postcode",
                "Address enrichment could not determine a trustworthy postcode.",
                Severity.WARNING,
                postcode_value,
                "Confirm the postcode against the brochure or provider website.",
                "address_validation",
            )
        )
    elif postcode and not _POSTCODE_RE.match(postcode):
        prop.add_issue(ValidationIssue("Property Postcode", "Postcode could not be normalized confidently.", Severity.WARNING, postcode_value))
    if "not in source text" in postcode_value.lower():
        prop.add_issue(
            ValidationIssue(
                "Property Postcode",
                "Postcode was derived during enrichment and was not present in the source.",
                Severity.WARNING,
                postcode_value,
                "Verify the postcode against the brochure or provider website.",
                "address_validation",
            )
        )

    resolution = values.get("_address_resolution") or {}
    if resolution.get("status") in {"CONFLICTING_CANDIDATES", "NO_VALID_CANDIDATE", "MANUAL_REVIEW_REQUIRED"}:
        values_summary = "; ".join(
            f"{item.get('address')} ({', '.join(item.get('reasons') or []) or 'score ' + str(item.get('score'))})"
            for item in (resolution.get("candidates_considered") or [])[:3]
        )
        prop.add_issue(
            ValidationIssue(
                "Property Postcode",
                f"Address resolution ended as {resolution.get('status')}; no conflicting property was accepted.",
                Severity.WARNING,
                values_summary or postcode_value,
                "Review the source, brochure, and linked property pages.",
                "address_resolution",
            )
        )

    brochure = values.get("Brochure PDF")
    floorplan = values.get("Floor Plan")
    images = values.get("High Res Images")
    for issue in validate_url_field("Brochure PDF", brochure, forbidden_kind="image"):
        prop.add_issue(issue)
    for issue in validate_url_field("Floor Plan", floorplan):
        prop.add_issue(issue)
    for issue in validate_url_field("High Res Images", images):
        prop.add_issue(issue)

    if brochure and floorplan and normalize_url(str(brochure)) == normalize_url(str(floorplan)):
        prop.add_issue(
            ValidationIssue(
                "Floor Plan",
                "Floor plan URL duplicates the brochure URL.",
                Severity.ERROR,
                floorplan,
                "Floor Plan must be a hosted plan image; Brochure PDF holds the document link.",
                "brochure_enrichment",
            )
        )
    if brochure and images and normalize_url(str(brochure)) == normalize_url(str(images)):
        prop.add_issue(ValidationIssue("High Res Images", "Property image field duplicates the brochure URL.", Severity.ERROR, images))
    if floorplan and images and normalize_url(str(floorplan)) == normalize_url(str(images)):
        prop.add_issue(ValidationIssue("High Res Images", "Property image field duplicates the floorplan URL.", Severity.ERROR, images))

    return prop


def validate_properties(properties: Iterable[Property]) -> List[Property]:
    return [validate_property(prop) for prop in properties]
