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
    candidates = enriched.values["_high_res_candidates"]
    assert [image.url for image in images] == candidates[: len(images)]
    # Brochure URL may also be stashed as a click-through seed fallback.
    assert all(url in candidates for url in (image.url for image in images))


def test_existing_and_brochure_images_merge_even_for_extensionless_cdn_url():
    existing = "https://cdn.example.test/image/hero.jpg"
    added = AssetCandidate("https://img.example.test/terrace.jpg", BROCHURE, alt_text="Terrace")
    enriched = run(prop(**{"High Res Images": existing}), extraction(assets=[added]))
    candidates = enriched.values["_high_res_candidates"]
    assert existing in candidates
    assert added.url in candidates


def test_brochure_adds_photos_to_existing_direct_property_image():
    existing = "https://img.example.test/existing.jpg"
    added = AssetCandidate("https://img.example.test/terrace.jpg", BROCHURE, alt_text="Terrace")
    enriched = run(prop(**{"High Res Images": existing}), extraction(assets=[added]))
    assert enriched.values["High Res Images"] == existing
    candidates = enriched.values["_high_res_candidates"]
    assert existing in candidates
    assert added.url in candidates


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
    write_xlsx(output, [enriched.to_record()], include_qa_sheet=True)
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


def test_metspace_style_drive_viewer_without_pdf_in_title_still_exposes_download():
    """MetSpace Drive titles omit '.pdf'; mime hint is only inside <script>."""
    viewer = b"""<html><head><title>9-10 Market Place - 2nd Floor - Google Drive</title></head>
      <body><script>window.config={\"docs-dm\":\"application/pdf\"}</script>
      <img src="/preview.png" alt="preview"></body></html>"""
    result = extract_brochure(viewer, "text/html", "https://drive.google.com/file/d/metspace_file_id/view")
    brochure_assets = [asset for asset in result.assets if asset.classification == AssetType.BROCHURE]
    assert [asset.url for asset in brochure_assets] == [
        "https://drive.usercontent.google.com/download?id=metspace_file_id&export=download"
    ]


def test_direct_pdf_brochure_extracts_text_and_embedded_photo():
    import fitz
    from PIL import Image

    bitmap = Image.new("RGB", (500, 400), (20, 20, 20))
    pixels = bitmap.load()
    for y in range(400):
        for x in range(500):
            pixels[x, y] = ((x * 3) % 256, (y * 7) % 256, (x + y) % 256)
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
    assert "plan" in record["Floor Plan"]
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
    from PIL import Image

    def _jpeg(seed):
        buffer = BytesIO()
        image = Image.new("RGB", (640, 400), (10, 10, 10))
        pixels = image.load()
        for y in range(400):
            for x in range(640):
                pixels[x, y] = ((x + seed) % 256, (y + seed) % 256, (x * y + seed) % 256)
        image.save(buffer, format="JPEG")
        return buffer.getvalue()

    bytes_a, bytes_b, bytes_plan = _jpeg(1), _jpeg(2), _jpeg(3)
    photo_a = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=bytes_a, content_hash="photo-a", extension="jpg", width=640, height=400)
    photo_b = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=bytes_b, content_hash="photo-b", extension="jpg", width=640, height=400)
    duplicate = AssetCandidate("", BROCHURE, classification=AssetType.PROPERTY_IMAGE, confidence=0.8, content=bytes_a, content_hash="photo-a", extension="jpg", width=640, height=400)
    floorplan = AssetCandidate("", BROCHURE, classification=AssetType.FLOORPLAN, confidence=0.9, content=bytes_plan, content_hash="plan", extension="png", width=640, height=400)
    record = {
        "Building": "Example House",
        "Floor Plan": "",
        "High Res Images": "https://cdn.example.test/source-image.jpg",
        "_high_res_candidates": ["https://cdn.example.test/source-image.jpg"],
        "_brochure_embedded_assets": [photo_a, photo_b, duplicate, floorplan],
    }
    with app_module.app.test_request_context("/process", base_url="https://app.example.test"):
        jobs = app_module._materialize_brochure_assets([record], tmp_path, "batch", "Example")
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda url, cache=None, deadline=None: {
                "ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url,
            },
        )
    assert len(jobs) == 3
    assert "plan" in record["Floor Plan"]
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


def test_html_brochure_extracts_contacts_term_state_and_postcode():
    html = b"""<html><body>
      <h1>Example House, 1 Example Street, EC1A 1AA</h1>
      <h2>Amenities</h2><p>Showers; Bike storage; Roof terrace</p>
      <h2>Lease terms</h2><p>Minimum term 24 months</p>
      <h2>Availability</h2><p>Fully fitted Cat A space</p>
      <p>Contact: Jane Broker jane@example.test 020 7123 4567</p>
      </body></html>"""
    result = extract_brochure(html, "text/html", "https://property.example.test/listing/1")
    assert "Showers" in result.fields["Special Features"].value
    assert result.fields["Min. Term"].value == "24 months"
    assert "fitted" in result.fields["State of Space"].value.lower() or "Cat A" in result.fields["State of Space"].value
    assert "jane@example.test" in result.fields["Contacts"].value.lower()
    assert result.fields["Property Postcode"].value == "EC1A 1AA"
    assert "Building" not in result.fields
    assert "Property Address 1" not in result.fields
    assert "Floor/Unit" not in result.fields


def test_brochure_fills_blank_safe_fields_with_provenance():
    html = b"""<html><body>
      <h2>Amenities</h2><p>Cycle storage</p>
      <h2>Lease terms</h2><p>12 months</p>
      <p>Contact us at hello@landlord.example 020 8000 1000</p>
      <p>Available immediately - plug and play</p>
      <p>EC2A 4BX</p>
      </body></html>"""
    enriched = enrich_properties(
        [prop()],
        fetcher=lambda url: BrochureResource(html, "text/html", "https://property.example.test/x"),
    )[0]
    assert enriched.values["Special Features"] == "Cycle storage"
    assert enriched.values["Min. Term"] == "12 months"
    assert "hello@landlord.example" in enriched.values["Contacts"]
    assert enriched.values["Property Postcode"] == "EC2A 4BX"
    assert enriched.provenance["Contacts"].source == "brochure"
    assert enriched.provenance["Min. Term"].source == "brochure"


def test_brochure_does_not_overwrite_building_or_address():
    primary = prop(
        **{
            "Building": "Primary House, 9 Primary Street, E1 6AN",
            "Property Postcode": "",
        }
    )
    # normalize_record derives Property Address 1 from Building.
    assert primary.values["Property Address 1"] == primary.values["Building"]
    result = BrochureExtraction(
        BROCHURE,
        {
            "Building": ExtractedValue("Different Brochure Name", "brochure", BROCHURE, "test", 0.95),
            "Property Address 1": ExtractedValue("1 Brochure Road", "brochure", BROCHURE, "test", 0.95),
            "Property Postcode": ExtractedValue("E1 6AN", "brochure", BROCHURE, "test", 0.9),
            "Special Features": ExtractedValue("Terrace", "brochure", BROCHURE, "test", 0.9),
        },
        # Same postcode / street so identity matches; address fields must still
        # stay primary-owned even if the brochure offers different wording.
        identity_text="Primary House, 9 Primary Street, E1 6AN. Terrace.",
    )
    enriched = run(primary, result)
    assert enriched.values["Building"] == "Primary House, 9 Primary Street, E1 6AN"
    assert enriched.values["Property Address 1"] == "Primary House, 9 Primary Street, E1 6AN"
    assert enriched.values["Property Postcode"] == "E1 6AN"
    assert enriched.values["Special Features"] == "Terrace"


def test_brochure_postcode_does_not_overwrite_existing():
    primary = prop()
    primary.values["Property Postcode"] = "EC1A 1AA"
    primary.provenance["Property Postcode"] = FieldProvenance("primary.eml", "rule:test", 1.0, "EC1A 1AA")
    enriched = run(primary, extraction([evidence("Property Postcode", "SE1 9HH", 0.95)]))
    assert enriched.values["Property Postcode"] == "EC1A 1AA"
    assert any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_multi_unit_brochure_applies_size_only_to_matching_floor():
    from extraction.brochure import _extract_fields as real_extract_fields

    text = (
        "Example House EC1A 1AA\n"
        "3rd Floor 2,500 sq ft up to 40 desks\n"
        "5th Floor 4,100 sq ft up to 70 desks\n"
    )
    third = prop(**{"Floor/Unit": "3rd", "Size (sq ft)": "", "Desks (max)": ""})
    fifth = prop(**{"Floor/Unit": "5th", "Size (sq ft)": "", "Desks (max)": ""})
    second = prop(**{"Floor/Unit": "2nd", "Size (sq ft)": "", "Desks (max)": ""})

    def extract(payload, content_type, source):
        return BrochureExtraction(
            source,
            real_extract_fields(text, source),
            identity_text=text,
        )

    enriched = enrich_properties(
        [third, fifth, second],
        fetcher=lambda url: BrochureResource(b"<html><body>x</body></html>", "text/html", BROCHURE),
        extractor=extract,
    )
    assert enriched[0].values["Size (sq ft)"] == 2500.0
    assert enriched[0].values["Desks (max)"] == 40.0
    assert enriched[1].values["Size (sq ft)"] == 4100.0
    assert enriched[1].values["Desks (max)"] == 70.0
    assert enriched[2].values.get("Size (sq ft)") in (None, "")
    assert enriched[2].values.get("Desks (max)") in (None, "")


def test_single_unit_brochure_fills_blank_size_and_desks():
    html = """<html><body>
      <p>Example House, EC1A 1AA</p>
      <p>1,850 sq ft</p>
      <p>Up to 28 desks</p>
      <p>Rent £12,500 pcm</p>
      </body></html>""".encode("utf-8")
    enriched = enrich_properties(
        [prop(**{"Floor/Unit": "1st", "Size (sq ft)": "", "Desks (max)": ""})],
        fetcher=lambda url: BrochureResource(html, "text/html", "https://property.example.test/unit"),
    )[0]
    assert enriched.values["Size (sq ft)"] == 1850.0
    assert enriched.values["Desks (max)"] == 28.0
    # Single-floor brochure (no competing floors) may fill PCM when blank.
    assert enriched.values["Marketing Price (Based on Min Term) PCM"] == 12500.0


def test_multi_unit_brochure_skips_unlabeled_price_copy():
    text = (
        "Example House\n"
        "3rd Floor 2,000 sq ft\n"
        "5th Floor 3,000 sq ft\n"
        "Rent £15,000 pcm\n"
    )
    result = BrochureExtraction(BROCHURE, {}, identity_text=text)
    enriched = run(prop(**{"Floor/Unit": "3rd"}), result)
    assert enriched.values.get("Marketing Price (Based on Min Term) PCM") in (None, "")
    assert enriched.values["Size (sq ft)"] == 2000.0


def test_gpe_style_nested_brochure_promotes_into_brochure_pdf_cell():
    """Property-page seed stays for enrichment; Brochure PDF becomes the nested PDF."""
    page = "https://www.gpe.co.uk/property/16-dufours-place"
    pdf = "https://www.gpe.co.uk/media/lf1dh5wc/gpe-16-dufours-place-brochure.pdf"

    def fetch(url, deadline=None):
        if url == page:
            html = (
                f"<html><body><h1>16 Dufour's Place</h1>"
                f"<a href='{pdf}'>Download brochure</a></body></html>"
            ).encode()
            return BrochureResource(html, "text/html", page, page)
        return BrochureResource(b"%PDF-1.4 brochure", "application/pdf", pdf, pdf)

    record = normalize_record({
        "Building": "16 Dufour's Place",
        "Property Postcode": "W1F 7SP",
        "Brochure PDF": page,
    })
    property_ = Property.from_record(record, "gpe.eml", "GPE", "rule:GPE")
    enriched = enrich_properties([property_], fetcher=fetch)[0]
    assert enriched.values["Brochure PDF"] == pdf
    assert enriched.values.get("_brochure_source_page") == page


def test_generic_hosted_document_promotes_resolved_pdf_into_brochure_pdf_cell():
    """Non-GPE: Drive viewer seed promotes to the resolvable download URL."""
    viewer = "https://drive.google.com/file/d/abc123file/view"
    download = "https://drive.usercontent.google.com/download?id=abc123file&export=download"

    def fetch(url, deadline=None):
        if "usercontent" in url or url == download:
            return BrochureResource(b"%PDF-1.4 drive", "application/pdf", download, download)
        return BrochureResource(b"<html>Drive viewer</html>", "text/html", viewer, viewer)

    record = normalize_record({
        "Building": "9-10 Market Place",
        "Property Postcode": "W1W 8AQ",
        "Brochure PDF": viewer,
    })
    property_ = Property.from_record(record, "metspace.eml", "MetSpace", "rule:MetSpace")
    enriched = enrich_properties([property_], fetcher=fetch)[0]
    assert enriched.values["Brochure PDF"] == download
    assert enriched.values.get("_brochure_source_page") == viewer


def test_html_only_property_page_keeps_brochure_pdf_cell():
    """Knotel-style thin pages with no nested PDF must not blank Brochure PDF."""
    page = "https://www.knotel.com/buildings/example-house"

    def fetch(url, deadline=None):
        html = b"<html><body><h1>Example House EC1A 1AA</h1><p>Available now</p></body></html>"
        return BrochureResource(html, "text/html", page, page)

    record = normalize_record({
        "Building": "Example House",
        "Property Postcode": "EC1A 1AA",
        "Brochure PDF": page,
    })
    property_ = Property.from_record(record, "knotel.eml", "Knotel", "rule:Knotel")
    enriched = enrich_properties([property_], fetcher=fetch)[0]
    assert enriched.values["Brochure PDF"] == page
    assert not enriched.values.get("_brochure_source_page")


def test_pitch_viewer_is_never_promoted_into_brochure_pdf_cell():
    page = "https://property.example.test/listing"
    pitch = "https://pitch.com/v/fake-brochure"

    def fetch(url, deadline=None):
        html = (
            f"<html><body><a href='{pitch}'>Download brochure</a></body></html>"
        ).encode()
        return BrochureResource(html, "text/html", page, page)

    record = normalize_record({
        "Building": "Example House",
        "Property Postcode": "EC1A 1AA",
        "Brochure PDF": page,
    })
    property_ = Property.from_record(record, "source.eml", "Example", "rule:test")
    enriched = enrich_properties([property_], fetcher=fetch)[0]
    assert enriched.values["Brochure PDF"] == page
    assert "pitch.com" not in (enriched.values.get("Brochure PDF") or "")
