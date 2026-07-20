"""Canonical typed contracts shared by extraction pipeline stages.

Legacy provider parsers may still return dictionaries, but records cross stage
boundaries through these models. This keeps the established spreadsheet schema
compatible while making provenance, validation and diagnostics explicit.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .text_utils import (
    clean_special_features,
    clean_state_of_space,
    reclassify_special_features_and_state_of_space,
)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AssetType(str, Enum):
    BROCHURE = "brochure"
    FLOORPLAN = "floorplan"
    PROPERTY_IMAGE = "property_image"
    LOGO = "logo"
    MAP = "map"
    DECORATIVE = "decorative"
    DOCUMENT_PREVIEW = "document_preview"
    TRACKING_OR_DECORATIVE = "tracking_or_decorative"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FieldProvenance:
    source: str
    method: str
    confidence: float = 1.0
    original_value: Any = None
    source_document: Optional[str] = None


@dataclass(frozen=True)
class ExtractedValue:
    """A typed value offered by a secondary source such as a brochure."""

    value: Any
    source: str
    source_document: str
    extraction_method: str
    confidence: float


@dataclass
class BrochureExtraction:
    source_document: str
    fields: Dict[str, ExtractedValue] = field(default_factory=dict)
    assets: List["AssetCandidate"] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    identity_text: str = ""
    diagnostics: List["LinkDiagnostic"] = field(default_factory=list)


@dataclass(frozen=True)
class BrochureResource:
    payload: bytes
    content_type: str
    final_url: str
    original_url: Optional[str] = None
    redirects: tuple = ()


@dataclass(frozen=True)
class LinkDiagnostic:
    status: str
    original_url: str = ""
    final_url: Optional[str] = None
    resource_type: Optional[str] = None
    detail: str = ""
    property_key: str = ""
    identity_result: str = ""
    source_context: str = ""


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    severity: Severity = Severity.WARNING
    value: Any = None
    action: str = "Review the source document and correct this value if needed."
    stage: str = "validation"


@dataclass
class AssetCandidate:
    url: str
    source: str
    mime_type: Optional[str] = None
    original_url: Optional[str] = None
    final_url: Optional[str] = None
    source_file: Optional[str] = None
    provider: Optional[str] = None
    source_section: Optional[str] = None
    source_block: Optional[str] = None
    nearest_property: Optional[str] = None
    source_row: Optional[int] = None
    source_cell: Optional[str] = None
    html_container: Optional[str] = None
    filename: Optional[str] = None
    anchor_text: Optional[str] = None
    alt_text: Optional[str] = None
    page_number: Optional[int] = None
    classification: AssetType = AssetType.UNKNOWN
    confidence: float = 0.0
    surrounding_text: Optional[str] = None
    discovery_method: Optional[str] = None
    associated_property_key: Optional[str] = None
    association_confidence: float = 0.0
    width: Optional[int] = None
    height: Optional[int] = None
    occurrence_count: int = 1
    validation_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    content: Optional[bytes] = None
    content_hash: Optional[str] = None
    extension: Optional[str] = None


@dataclass
class RawDocument:
    source_file_name: str
    content: Dict[str, Any]
    source_file_url: Optional[str] = None
    provider: Optional[str] = None


@dataclass
class Property:
    """Canonical property wrapper without changing the public XLSX schema."""

    source_file_name: str
    provider: str
    values: Dict[str, Any]
    source_file_url: Optional[str] = None
    source_url_expected: bool = False
    provenance: Dict[str, FieldProvenance] = field(default_factory=dict)
    assets: List[AssetCandidate] = field(default_factory=list)
    issues: List[ValidationIssue] = field(default_factory=list)
    link_diagnostics: List[LinkDiagnostic] = field(default_factory=list)
    review_required: bool = False

    @classmethod
    def from_record(
        cls,
        record: Dict[str, Any],
        source_file_name: str,
        provider: str,
        method: str,
        source_file_url: Optional[str] = None,
        source_url_expected: bool = False,
    ) -> "Property":
        provenance = {
            key: FieldProvenance(source=source_file_name, method=method, original_value=value)
            for key, value in record.items()
            if value not in (None, "") and not key.startswith("_")
        }
        return cls(
            source_file_name=source_file_name,
            source_file_url=source_file_url,
            source_url_expected=source_url_expected,
            provider=provider,
            values=dict(record),
            provenance=provenance,
        )

    def set_source_reference(self, source_file_name: str, source_file_url: Optional[str]) -> None:
        self.source_file_name = source_file_name
        self.source_file_url = source_file_url
        self.source_url_expected = True

    def add_issue(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)
        if issue.severity in (Severity.WARNING, Severity.ERROR):
            self.review_required = True

    def to_record(self) -> Dict[str, Any]:
        record = dict(self.values)
        # Brochure enrichment can fill Special Features / State of Space after
        # normalize_record; clean + cap again at export so every row benefits.
        if "Special Features" in record or "State of Space" in record:
            sf = record.get("Special Features")
            sos = record.get("State of Space")
            sf = clean_special_features(sf) if "Special Features" in record else sf
            sos = clean_state_of_space(sos) if "State of Space" in record else sos
            sf, sos = reclassify_special_features_and_state_of_space(sf, sos)
            if "Special Features" in record:
                record["Special Features"] = sf
            if "State of Space" in record:
                record["State of Space"] = sos
        record["_source_file"] = self.source_file_name
        record["_source_file_name"] = self.source_file_name
        record["_source_file_url"] = self.source_file_url
        # Link to File is intentionally not a public spreadsheet column;
        # provenance stays on the underscore-prefixed fields above.
        record["_provenance"] = self.provenance
        record["_validation_issues"] = list(self.issues)
        record["_link_diagnostics"] = list(self.link_diagnostics)
        record["_review_required"] = self.review_required
        return record


@dataclass
class StageResult:
    stage: str
    status: str
    message: str = ""
    item_count: int = 0


@dataclass
class ProcessingReport:
    source_file: str
    stages: List[StageResult] = field(default_factory=list)
    issues: List[ValidationIssue] = field(default_factory=list)

    def record(self, stage: str, status: str, message: str = "", item_count: int = 0) -> None:
        self.stages.append(StageResult(stage, status, message, item_count))

    @property
    def review_required(self) -> bool:
        return any(issue.severity in (Severity.WARNING, Severity.ERROR) for issue in self.issues)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_file": self.source_file,
            "review_required": self.review_required,
            "stages": [vars(stage) for stage in self.stages],
            "issues": [
                {**vars(issue), "severity": issue.severity.value}
                for issue in self.issues
            ],
        }
