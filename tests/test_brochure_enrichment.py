from extraction.brochure import enrich_properties
from extraction import pipeline
from extraction.models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
    ExtractedValue,
    FieldProvenance,
    Property,
)
from extraction.schema import normalize_record


BROCHURE = "https://files.example.test/example-brochure.pdf"


def evidence(field, value, confidence=0.85):
    return field, ExtractedValue(value, "brochure", BROCHURE, "test:brochure", confidence)


def extraction(fields=(), assets=()):
    return BrochureExtraction(BROCHURE, dict(fields), list(assets))


def prop(**values):
    record = normalize_record({"Building": "Example House", "Brochure PDF": BROCHURE, **values})
    return Property.from_record(record, "primary.eml", "Example", "rule:test")


def run(property_, result):
    return enrich_properties(
        [property_],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]


def test_brochure_fills_missing_special_features_with_provenance():
    enriched = run(prop(), extraction([evidence("Special Features", "Roof terrace; Bike storage")]))
    assert enriched.values["Special Features"] == "Roof terrace; Bike storage"
    provenance = enriched.provenance["Special Features"]
    assert provenance.source == "brochure"
    assert provenance.source_document == BROCHURE
    assert provenance.method == "test:brochure"
    assert provenance.confidence == 0.85


def test_brochure_adds_deduplicated_property_images():
    photo = AssetCandidate("https://img.example.test/reception.jpg", BROCHURE, alt_text="Reception")
    duplicate = AssetCandidate("https://img.example.test/reception.jpg?utm_source=email", BROCHURE)
    second = AssetCandidate("https://img.example.test/terrace.png", BROCHURE, alt_text="Terrace")
    enriched = run(prop(), extraction(assets=[photo, duplicate, second]))
    photos = [asset.url for asset in enriched.assets if asset.classification == AssetType.PROPERTY_IMAGE]
    assert photos == ["https://img.example.test/reception.jpg", "https://img.example.test/terrace.png"]
    assert enriched.values["High Res Images"] == photos[0]


def test_brochure_floorplan_is_never_assigned_as_property_photo():
    floorplan = AssetCandidate("https://img.example.test/third-floor-plan.png", BROCHURE, alt_text="Floor plan")
    photo = AssetCandidate("https://img.example.test/reception.jpg", BROCHURE, alt_text="Reception")
    enriched = run(prop(), extraction(assets=[floorplan, photo]))
    assert enriched.values["Floor Plan"] == floorplan.url
    assert enriched.values["High Res Images"] == photo.url
    assert [asset.classification for asset in enriched.assets] == [AssetType.FLOORPLAN, AssetType.PROPERTY_IMAGE]


def test_brochure_does_not_overwrite_stronger_primary_value_and_flags_conflict():
    primary = prop(**{"Special Features": "Primary roof terrace"})
    enriched = run(primary, extraction([evidence("Special Features", "Brochure gym")]))
    assert enriched.values["Special Features"] == "Primary roof terrace"
    assert any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)
    assert enriched.review_required


def test_higher_confidence_brochure_replaces_low_confidence_primary():
    primary = prop(**{"Min. Term": "Unknown"})
    primary.provenance["Min. Term"] = FieldProvenance("primary.eml", "llm", 0.35, "Unknown")
    enriched = run(primary, extraction([evidence("Min. Term", "24 months", 0.9)]))
    assert enriched.values["Min. Term"] == "24 months"
    assert enriched.provenance["Min. Term"].source == "brochure"


def test_duplicate_brochure_text_is_deduplicated():
    enriched = run(prop(), extraction([evidence("Special Features", "Bike storage; Bike storage; Showers")]))
    assert enriched.values["Special Features"] == "Bike storage; Showers"


def test_brochure_failure_does_not_fail_property_extraction():
    property_ = prop(**{"Special Features": "Primary value"})

    def fail(url):
        raise TimeoutError("timed out")

    enriched = enrich_properties([property_], fetcher=fail)[0]
    assert enriched.values["Building"] == "Example House"
    assert enriched.values["Special Features"] == "Primary value"
    assert any(issue.stage == "brochure_enrichment" for issue in enriched.issues)
    assert not enriched.review_required


def test_pipeline_runs_brochure_enrichment_before_final_validation(monkeypatch, tmp_path):
    source = tmp_path / "availability.csv"
    source.write_text(
        "Building,Brochure PDF,Special Features\n"
        f'"Example House, 1 Example Street, EC1A 1AA",{BROCHURE},\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "geocode", lambda *args, **kwargs: (51.5, -0.1, "EC1A 1AA", None))
    monkeypatch.setattr(
        pipeline,
        "try_rules",
        lambda content: (
            "Test",
            [{"Building": "Example House, 1 Example Street, EC1A 1AA", "Brochure PDF": BROCHURE}],
        ),
    )
    result = pipeline.process_files(
        [source],
        brochure_enrichment=True,
        brochure_fetcher=lambda url: (b"brochure", "application/pdf"),
        brochure_extractor=lambda *args: extraction([evidence("Special Features", "Showers")]),
    )[0]
    assert result["status"] == "ok"
    assert result["records"][0]["Special Features"] == "Showers"
    stages = [stage["stage"] for stage in result["processing_report"]["stages"]]
    assert stages.index("BROCHURE_ENRICHMENT") < stages.index("FINAL_VALIDATION")
