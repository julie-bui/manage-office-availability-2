from io import BytesIO

import pytest
from PIL import Image

from extraction import brochure, pipeline
from extraction.brochure import LinkedResourceError, enrich_properties, extract_brochure
from extraction.models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
    BrochureResource,
    ExtractedValue,
    Property,
)
from extraction.schema import normalize_record


LINK = "https://links.example.test/property"


def property_record(**overrides):
    values = normalize_record({
        "Building": "Example House, 1 Example Street, London EC1A 1AA",
        "Property Postcode": "EC1A 1AA",
        "Brochure PDF": LINK,
        **overrides,
    })
    return Property.from_record(values, "source.eml", "Any Provider", "rule:test")


def run(prop, resource, extractor=extract_brochure):
    return enrich_properties([prop], fetcher=lambda _: resource, extractor=extractor)[0]


def statuses(prop):
    return [item.status for item in prop.link_diagnostics]


def jpeg_bytes():
    image = Image.effect_noise((640, 480), 80).convert("RGB")
    output = BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


def test_direct_pdf_is_detected_from_payload_despite_wrong_content_type():
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 50), "Example House 1 Example Street EC1A 1AA\nAmenities\nShowers")
    payload = document.tobytes()
    document.close()
    enriched = run(property_record(), BrochureResource(payload, "application/octet-stream", LINK, LINK))
    assert "LINK_RESOURCE_PDF" in statuses(enriched)
    assert "Showers" in enriched.values["Special Features"]


def test_html_property_page_enriches_text_and_multiple_images():
    html = b"""<h1>Example House, 1 Example Street, EC1A 1AA</h1>
      <h2>Amenities</h2><p>Showers and cycle storage</p>
      <img src='/one.jpg' alt='Reception'><img data-src='/two.jpg' alt='Terrace'>"""
    enriched = run(property_record(), BrochureResource(html, "text/html", LINK, LINK))
    assert "LINK_RESOURCE_HTML" in statuses(enriched)
    assert len(enriched.values["_high_res_candidates"]) == 2


def test_html_page_linking_to_pdf_follows_actual_document():
    pdf_url = "https://files.example.test/details"
    calls = []

    def fetch(url):
        calls.append(url)
        if url == LINK:
            return BrochureResource(f"<a href='{pdf_url}'>Download brochure</a>".encode(), "text/html", LINK, LINK)
        return BrochureResource(b"%PDF placeholder", "application/pdf", pdf_url, pdf_url)

    def parse(payload, content_type, source):
        if b"<a" in payload:
            return extract_brochure(payload, content_type, source)
        return BrochureExtraction(source, {"Special Features": ExtractedValue("Roof terrace", "linked_source", source, "test", 0.85)}, identity_text="Example House EC1A 1AA")

    enriched = enrich_properties([property_record()], fetcher=fetch, extractor=parse)[0]
    assert calls == [LINK, pdf_url]
    assert enriched.values["Special Features"] == "Roof terrace"


def test_tracking_redirect_preserves_original_and_final_page_urls():
    final = "https://property.example.test/example-house"
    resource = BrochureResource(b"<h1>Example House EC1A 1AA</h1>", "text/html", final, LINK, (final,))
    enriched = run(property_record(), resource)
    redirect = next(item for item in enriched.link_diagnostics if item.status == "LINK_REDIRECT_RESOLVED")
    assert redirect.original_url == LINK
    assert redirect.final_url == final


def test_tracking_redirect_to_pdf_is_typed_from_content():
    result = BrochureExtraction("https://files.example.test/final", identity_text="Example House EC1A 1AA")
    resource = BrochureResource(b"%PDF placeholder", "application/octet-stream", result.source_document, LINK, (result.source_document,))
    enriched = run(property_record(), resource, extractor=lambda *args: result)
    assert {"LINK_REDIRECT_RESOLVED", "LINK_RESOURCE_PDF", "LINK_ENRICHMENT_SUCCESS"} <= set(statuses(enriched))


def test_direct_image_resource_is_classified_before_assignment():
    enriched = run(property_record(), BrochureResource(jpeg_bytes(), "application/octet-stream", "https://img.example.test/reception", LINK))
    assert "LINK_RESOURCE_IMAGE" in statuses(enriched)
    assert enriched.values["High Res Images"] == "https://img.example.test/reception"


@pytest.mark.parametrize("value", ["mailto:test@example.com", "tel:02070000000", "javascript:alert(1)", "data:image/png;base64,AAAA", "file:///tmp/a.pdf"])
def test_unsupported_uri_schemes_are_ignored(value):
    enriched = enrich_properties([property_record(**{"Brochure PDF": value})])[0]
    assert enriched.values["Brochure PDF"] == value
    assert "LINK_UNSUPPORTED" in statuses(enriched)
    assert not enriched.assets


def test_malformed_url_is_skipped_safely():
    enriched = enrich_properties([property_record(**{"Brochure PDF": "not a valid URL"})])[0]
    assert "LINK_ENRICHMENT_SKIPPED" in statuses(enriched)
    assert enriched.values["Building"].startswith("Example House")


@pytest.mark.parametrize("status", ["LINK_ACCESS_DENIED", "LINK_NOT_FOUND", "LINK_TIMEOUT", "LINK_RATE_LIMITED"])
def test_access_and_network_failures_preserve_original(status):
    original = property_record(**{"Special Features": "Primary value"})
    before = dict(original.values)

    def fail(_):
        raise LinkedResourceError("unavailable", status, LINK)

    enriched = enrich_properties([original], fetcher=fail)[0]
    assert enriched.values == before
    assert status in statuses(enriched)
    assert "LINK_ENRICHMENT_SKIPPED" in statuses(enriched)


def test_javascript_only_page_is_skipped():
    html = b"<html><body><script>renderApplication()</script></body></html>"
    enriched = run(property_record(), BrochureResource(html, "text/html", LINK, LINK))
    assert "LINK_ENRICHMENT_SKIPPED" in statuses(enriched)
    assert not enriched.assets


def test_unrelated_property_page_is_rejected_before_any_merge():
    html = b"""<h1>Other House, 99 Different Road, London W1A 1AA</h1>
      <h2>Amenities</h2><p>Unrelated gym</p><img src='/other.jpg' alt='Office'>"""
    enriched = run(property_record(), BrochureResource(html, "text/html", LINK, LINK))
    assert "LINK_IDENTITY_HARD_CONFLICT" in statuses(enriched)
    assert not enriched.values["Special Features"]
    assert not enriched.assets


def test_strong_primary_value_is_retained_with_review_issue():
    html = b"<h1>Example House EC1A 1AA</h1><h2>Amenities</h2><p>Brochure gym</p>"
    enriched = run(property_record(**{"Special Features": "Primary terrace"}), BrochureResource(html, "text/html", LINK, LINK))
    assert enriched.values["Special Features"] == "Primary terrace"
    assert any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_images_and_floorplans_use_separate_merge_lists():
    html = b"""<h1>Example House EC1A 1AA</h1>
      <img src='/reception.jpg' alt='Reception'><img src='/floor-plan.jpg' alt='Floor plan'>"""
    enriched = run(property_record(), BrochureResource(html, "text/html", LINK, LINK))
    assert enriched.values["High Res Images"].endswith("reception.jpg")
    assert enriched.values["Floor Plan"].endswith("floor-plan.jpg")
    assert enriched.values["Floor Plan"] not in enriched.values["_high_res_candidates"]


def test_unexpected_merge_failure_is_atomic(monkeypatch):
    original = property_record(**{"Special Features": "Primary"})
    before = dict(original.values)
    result = BrochureExtraction(LINK, {"Special Features": object()}, identity_text="Example House EC1A 1AA")
    enriched = run(original, BrochureResource(b"<html></html>", "text/html", LINK, LINK), extractor=lambda *args: result)
    assert enriched.values == before
    assert "LINK_ENRICHMENT_FAILED" in statuses(enriched)


def test_unknown_future_provider_uses_same_linked_source_stage(monkeypatch, tmp_path):
    source = tmp_path / "future-source.html"
    source.write_text("<p>Unknown source</p>", encoding="utf-8")
    monkeypatch.setattr(pipeline, "try_rules", lambda content: (None, None))
    monkeypatch.setattr(pipeline, "extract_with_llm", lambda *args, **kwargs: ([{"Building": "Example House, 1 Example Street, EC1A 1AA", "Brochure PDF": LINK}], "Future Provider"))
    monkeypatch.setattr(pipeline, "geocode", lambda *args, **kwargs: (51.5, -0.1, "EC1A 1AA", None))
    result = pipeline.process_files(
        [source],
        brochure_enrichment=True,
        brochure_fetcher=lambda _: BrochureResource(b"<h1>Example House EC1A 1AA</h1><h2>Amenities</h2><p>Showers</p>", "text/html", LINK, LINK),
    )[0]
    assert result["method"] == "llm"
    assert result["records"][0]["Special Features"] == "Showers"
    assert any(item.status == "LINK_ENRICHMENT_SUCCESS" for item in result["records"][0]["_link_diagnostics"])
