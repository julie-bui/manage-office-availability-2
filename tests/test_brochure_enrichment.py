from extraction.brochure import enrich_properties, extract_brochure
from extraction import pipeline
from extraction.models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
    ExtractedValue,
    FieldProvenance,
    Property,
    BrochureResource,
)
from extraction.schema import normalize_record
from spreadsheet import write_xlsx
from openpyxl import load_workbook
import app as app_module
from io import BytesIO


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


def test_multiple_images_from_same_brochure_remain_gallery_candidates():
    images = [
        AssetCandidate(f"https://img.example.test/office-{number}.jpg", BROCHURE, alt_text=f"Office {number}")
        for number in range(1, 6)
    ]
    enriched = run(prop(), extraction(assets=images))
    assert enriched.values["_high_res_candidates"] == [image.url for image in images]


def test_existing_and_brochure_images_merge_even_for_extensionless_cdn_url():
    existing = "https://cdn.example.test/image/hero-token"
    added = AssetCandidate("https://img.example.test/terrace.jpg", BROCHURE, alt_text="Terrace")
    enriched = run(prop(**{"High Res Images": existing}), extraction(assets=[added]))
    assert enriched.values["_high_res_candidates"] == [existing, added.url]


def test_brochure_adds_photos_to_existing_direct_property_image():
    existing = "https://img.example.test/existing.jpg"
    added = AssetCandidate("https://img.example.test/terrace.jpg", BROCHURE, alt_text="Terrace")
    enriched = run(prop(**{"High Res Images": existing}), extraction(assets=[added]))
    assert enriched.values["High Res Images"] == existing
    assert enriched.values["_high_res_candidates"] == [existing, added.url]


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


def test_brochure_conflict_is_written_to_qa_review(tmp_path):
    enriched = run(prop(**{"Special Features": "Primary roof terrace"}), extraction([evidence("Special Features", "Brochure gym")]))
    output = tmp_path / "conflict.xlsx"
    write_xlsx(output, [enriched.to_record()])
    qa = load_workbook(output)["QA Review"]
    assert any("Brochure value conflicts" in str(cell.value) for cell in qa["D"])


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


def test_html_brochure_extracts_and_classifies_links_and_images():
    html = b"""<html><body><h2>Amenities</h2><p>Showers; Bike storage</p>
      <img src='/reception.jpg' alt='Reception'>
      <img src='/third-floor-plan.png' alt='Floor plan'>
      <img src='/company-logo.png' alt='Company logo'>
      <img src='/social-icon.png' alt='Social icon'>
      <a href='/downloads/brochure.pdf'>Download brochure</a></body></html>"""
    result = extract_brochure(html, "text/html", "https://property.example.test/listing/1")
    classes = {asset.filename: asset.classification for asset in result.assets}
    assert result.fields["Special Features"].value == "Showers; Bike storage"
    assert classes["reception.jpg"] == AssetType.PROPERTY_IMAGE
    assert classes["third-floor-plan.png"] == AssetType.FLOORPLAN
    assert classes["company-logo.png"] == AssetType.LOGO
    assert classes["social-icon.png"] == AssetType.DECORATIVE
    assert classes["brochure.pdf"] == AssetType.BROCHURE


def test_html_brochure_discovers_lazy_responsive_and_preview_images():
    html = b"""<html><head><meta property='og:image' content='/preview.jpg'></head><body>
      <img data-src='/reception.jpg' data-srcset='/reception-small.jpg 480w, /reception-large.jpg 1200w' alt='Reception'>
      <picture><source srcset='/terrace.webp 1x, /terrace@2x.webp 2x'></picture>
      </body></html>"""
    result = extract_brochure(html, "text/html", "https://property.example.test/listing/1")
    urls = {asset.url for asset in result.assets if asset.classification == AssetType.PROPERTY_IMAGE}
    assert urls == {
        "https://property.example.test/reception.jpg",
        "https://property.example.test/reception-small.jpg",
        "https://property.example.test/reception-large.jpg",
        "https://property.example.test/terrace.webp",
        "https://property.example.test/terrace@2x.webp",
    }
    assert next(asset for asset in result.assets if asset.url.endswith("preview.jpg")).classification == AssetType.DOCUMENT_PREVIEW


def test_public_google_drive_pdf_viewer_exposes_downloadable_brochure_candidate():
    viewer = b"""<html><head><title>Example House Brochure.pdf - Google Drive</title></head>
      <body><script>window.config={\"docs-dm\":\"application/pdf\"}</script></body></html>"""
    result = extract_brochure(viewer, "text/html", "https://drive.google.com/file/d/public_file_id/view?usp=sharing")
    brochure_assets = [asset for asset in result.assets if asset.classification == AssetType.BROCHURE]
    assert [asset.url for asset in brochure_assets] == [
        "https://drive.usercontent.google.com/download?id=public_file_id&export=download"
    ]


def test_direct_pdf_brochure_extracts_text_and_embedded_photo():
    import fitz
    from PIL import Image

    bitmap = Image.effect_noise((500, 400), 80).convert("RGB")
    image_bytes = BytesIO()
    bitmap.save(image_bytes, format="JPEG", quality=90)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 50), "Amenities\nShowers and cycle storage")
    page.insert_image(fitz.Rect(50, 90, 450, 390), stream=image_bytes.getvalue())
    payload = document.tobytes()
    document.close()

    result = extract_brochure(payload, "application/pdf", BROCHURE)
    assert "Showers and cycle storage" in result.fields["Special Features"].value
    assert any(asset.classification == AssetType.PROPERTY_IMAGE and asset.content for asset in result.assets)


def test_redirect_final_page_and_downloadable_pdf_are_followed():
    calls = []
    page_url = "https://property.example.test/final-listing"
    pdf_url = "https://property.example.test/files/details.pdf"

    def fetch(url):
        calls.append(url)
        if url == BROCHURE:
            return BrochureResource(f"<a href='{pdf_url}'>Download brochure</a>".encode(), "text/html", page_url)
        return BrochureResource(b"%PDF fake", "application/pdf", pdf_url)

    def parse(payload, content_type, source):
        if "html" in content_type:
            return extract_brochure(payload, content_type, source)
        return extraction([evidence("Special Features", "From downloadable PDF")])

    enriched = enrich_properties([prop()], fetcher=fetch, extractor=parse)[0]
    assert calls == [BROCHURE, pdf_url]
    assert enriched.values["Special Features"] == "From downloadable PDF"


def test_failed_linked_download_preserves_html_page_enrichment():
    page = b"<h2>Amenities</h2><p>Showers</p><a href='/broken.pdf'>Download brochure</a>"

    def fetch(url):
        if url == BROCHURE:
            return BrochureResource(page, "text/html", "https://property.example.test/listing")
        raise TimeoutError("download timed out")

    enriched = enrich_properties([prop()], fetcher=fetch)[0]
    assert enriched.values["Special Features"] == "Showers"
    assert any("Linked brochure document" in issue.message for issue in enriched.issues)
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


def test_unknown_provider_llm_record_uses_same_brochure_stage(monkeypatch, tmp_path):
    source = tmp_path / "future-provider.html"
    source.write_text("<html><body>Unknown office listing</body></html>", encoding="utf-8")
    monkeypatch.setattr(pipeline, "try_rules", lambda content: (None, None))
    monkeypatch.setattr(
        pipeline,
        "extract_with_llm",
        lambda text, source_hint=None: ([{"Building": "1 Future Street, EC1A 1AA", "Brochure PDF": BROCHURE}], "Future Provider"),
    )
    monkeypatch.setattr(pipeline, "geocode", lambda *args, **kwargs: (51.5, -0.1, "EC1A 1AA", None))
    result = pipeline.process_files(
        [source],
        brochure_enrichment=True,
        brochure_fetcher=lambda url: (b"brochure", "application/pdf"),
        brochure_extractor=lambda *args: extraction([evidence("Special Features", "Future-ready")]),
    )[0]
    assert result["method"] == "llm"
    assert result["records"][0]["Special Features"] == "Future-ready"


def test_embedded_brochure_assets_are_hosted_by_classification(tmp_path):
    photo = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=b"photo-bytes", content_hash="photo", extension="jpg")
    floorplan = AssetCandidate("", BROCHURE, classification=AssetType.FLOORPLAN, confidence=0.9, content=b"plan-bytes", content_hash="plan", extension="png")
    logo = AssetCandidate("", BROCHURE, classification=AssetType.LOGO, confidence=0.99, content=b"logo-bytes", content_hash="logo", extension="png")
    record = {"Building": "Example House", "Floor Plan": "", "High Res Images": "", "_brochure_embedded_assets": [photo, floorplan, logo]}
    with app_module.app.test_request_context("/process", base_url="https://app.example.test"):
        jobs = app_module._materialize_brochure_assets([record], tmp_path, "batch", "Example")
    assert len(jobs) == 2
    assert ".png?" in record["Floor Plan"]
    assert len(record["_high_res_candidates"]) == 1
    assert "logo" not in " ".join(path.name for _, path in jobs)


def test_gallery_generator_excludes_broken_url_and_keeps_valid_candidates(tmp_path):
    candidates = [f"https://img.example.test/{number}.jpg" for number in range(1, 6)]
    candidates[2] = "https://img.example.test/broken.jpg"
    record = {"Building": "Five Photo House", "High Res Images": candidates[0], "_high_res_candidates": candidates}
    def validate(url, cache=None):
        return {"ok": "broken" not in url, "url": url, "status": "LINK_EXPIRED_OR_INACCESSIBLE" if "broken" in url else "VALID_IMAGE"}
    with app_module.app.test_request_context("/process", base_url="https://app.example.test"):
        jobs = app_module._finalize_high_res_images([record], tmp_path, "batch", "Example", image_validator=validate)
    assert len(jobs) == 1
    gallery = jobs[0][1].read_text(encoding="utf-8")
    assert gallery.count("<img ") == 4
    assert candidates[2] not in gallery
    assert all(url in gallery for url in candidates if url != candidates[2])
    assert any(item.status == "LINK_EXPIRED_OR_INACCESSIBLE" for item in record["_link_diagnostics"])


def test_materialized_brochure_photos_extend_existing_candidates_and_keep_floorplan_separate(tmp_path):
    photo_a = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=b"photo-a", content_hash="photo-a", extension="jpg")
    photo_b = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=b"photo-b", content_hash="photo-b", extension="jpg")
    duplicate = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=b"photo-a", content_hash="photo-a", extension="jpg")
    floorplan = AssetCandidate("", BROCHURE, classification=AssetType.FLOORPLAN, confidence=0.9, content=b"plan", content_hash="plan", extension="png")
    record = {
        "Building": "Example House",
        "Floor Plan": "",
        "High Res Images": "https://cdn.example.test/source-image",
        "_high_res_candidates": ["https://cdn.example.test/source-image"],
        "_brochure_embedded_assets": [photo_a, photo_b, duplicate, floorplan],
    }
    with app_module.app.test_request_context("/process", base_url="https://app.example.test"):
        jobs = app_module._materialize_brochure_assets([record], tmp_path, "batch", "Example")
        app_module._finalize_high_res_images([record], tmp_path, "batch", "Example", image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE"})
    assert len(jobs) == 3
    assert ".png?" in record["Floor Plan"]
    gallery_path = next(tmp_path.glob("*_photos*.html"))
    gallery = gallery_path.read_text(encoding="utf-8")
    assert gallery.count("<img ") == 3
    assert record["Floor Plan"] not in gallery


def test_brochure_postcode_survives_disagreeing_geocoder(monkeypatch):
    enriched = run(prop(), extraction([evidence("Property Postcode", "SE1 9HH", 0.9)]))
    monkeypatch.setattr(pipeline, "geocode", lambda *args, **kwargs: (51.5, -0.1, "SE1 0DG", None))
    pipeline._geocode_records([enriched.values], "future-provider.pdf", "Unknown Provider", float("inf"))
    assert enriched.values["Property Postcode"] == "SE1 9HH"
    assert enriched.provenance["Property Postcode"].source == "brochure"
