from pathlib import Path

from openpyxl import load_workbook

from extraction.address import extract_postcode
from extraction.assets import classify_candidates, normalize_url
from extraction.models import AssetCandidate, AssetType, Property
from extraction.pipeline import _address_retry_candidates
from extraction.schema import COLUMNS, normalize_record
from extraction.validation import validate_property
from spreadsheet import QA_COLUMNS, write_xlsx


def test_postcode_extraction_and_normalisation():
    assert extract_postcode("18 Bevis Marks, London ec3a7jb") == "EC3A 7JB"
    assert extract_postcode("No postcode supplied") == ""


def test_address_retry_candidates_are_ordered_and_unique():
    assert _address_retry_candidates("Princes House, 38 Jermyn Street, SW1Y", None) == [
        "Princes House, 38 Jermyn Street",
        "38 Jermyn Street, SW1Y",
        "38 Jermyn Street",
    ]


def test_asset_classification_and_tracking_parameter_deduplication():
    candidates = classify_candidates(
        [
            AssetCandidate("https://EXAMPLE.com/file.pdf?utm_source=email", "html", anchor_text="View brochure"),
            AssetCandidate("https://example.com/file.pdf", "html", anchor_text="Duplicate brochure"),
            AssetCandidate("https://example.com/third-floor-plan.png", "html", alt_text="Floor plan"),
            AssetCandidate("https://example.com/reception.jpg", "html", alt_text="Reception"),
            AssetCandidate("https://example.com/company-logo.png", "html", alt_text="Company logo"),
            AssetCandidate("https://example.com/open-tracking.gif", "html", alt_text="tracking pixel"),
        ]
    )
    assert [item.classification for item in candidates] == [
        AssetType.BROCHURE,
        AssetType.FLOORPLAN,
        AssetType.PROPERTY_IMAGE,
        AssetType.LOGO,
        AssetType.TRACKING_OR_DECORATIVE,
    ]
    assert normalize_url("javascript:alert(1)") == ""


def test_validation_generates_issues_without_destroying_values():
    record = normalize_record(
        {
            "Building": "Example House",
            "Brochure PDF": "https://example.com/photo.jpg",
            "Floor Plan": "https://example.com/shared.jpg",
            "High Res Images": "https://example.com/shared.jpg",
        }
    )
    prop = validate_property(Property.from_record(record, "input.eml", "Example", "rule:test"))
    assert prop.values["Brochure PDF"] == "https://example.com/photo.jpg"
    assert prop.review_required
    assert {issue.field for issue in prop.issues} >= {"Brochure PDF", "High Res Images"}


def test_missing_optional_data_is_not_an_error():
    record = normalize_record({"Building": "18 Bevis Marks, London EC3A 7JB"})
    prop = validate_property(Property.from_record(record, "input.pdf", "Example", "rule:test"))
    assert not any(issue.field in {"Brochure PDF", "Floor Plan", "High Res Images"} for issue in prop.issues)


def test_spreadsheet_named_mapping_hyperlinks_and_qa_sheet(tmp_path: Path):
    record = normalize_record({"Building": "Example House", "Brochure PDF": "https://example.com/brochure.pdf"})
    prop = validate_property(Property.from_record(record, "source.eml", "Example", "rule:test"))
    path = tmp_path / "result.xlsx"
    write_xlsx(path, [prop.to_record()])
    workbook = load_workbook(path)
    listings = workbook["Listings"]
    headers = [cell.value for cell in listings[1]]
    assert headers == COLUMNS
    brochure_cell = listings.cell(2, COLUMNS.index("Brochure PDF") + 1)
    assert brochure_cell.hyperlink.target == "https://example.com/brochure.pdf"
    assert [cell.value for cell in workbook["QA Review"][1]] == QA_COLUMNS


def test_source_reference_survives_model_validation_and_spreadsheet_export(tmp_path: Path):
    source_url = "https://files.example.test/batches/42/source.eml"
    record = normalize_record(
        {
            "Building": "Example House",
            "Brochure PDF": "https://assets.example.test/brochure.pdf",
            "Floor Plan": "https://assets.example.test/floorplan.png",
            "High Res Images": "https://assets.example.test/reception.jpg",
        }
    )
    prop = validate_property(
        Property.from_record(record, "source.eml", "Example", "rule:test", source_url, True)
    )
    exported = prop.to_record()
    assert prop.source_file_name == "source.eml"
    assert prop.source_file_url == source_url
    assert exported["Link to File"] == source_url
    assert exported["_source_file_name"] == "source.eml"
    assert exported["Link to File"] not in {
        exported["Brochure PDF"], exported["Floor Plan"], exported["High Res Images"]
    }

    path = tmp_path / "source-link.xlsx"
    write_xlsx(path, [exported])
    cell = load_workbook(path)["Listings"].cell(2, COLUMNS.index("Link to File") + 1)
    assert cell.value == "source.eml"
    assert cell.hyperlink.target == source_url


def test_missing_source_url_only_warns_when_one_was_expected():
    record = normalize_record({"Building": "Example House"})
    local = validate_property(Property.from_record(record, "local.pdf", "Example", "rule:test"))
    expected = validate_property(
        Property.from_record(record, "hosted.pdf", "Example", "rule:test", source_url_expected=True)
    )
    assert not any(issue.field == "Link to File" for issue in local.issues)
    assert any(issue.field == "Link to File" for issue in expected.issues)
