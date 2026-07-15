from pathlib import Path
from io import BytesIO

from PIL import Image

from extraction.assets import classify_candidate, merge_candidate_urls, normalize_url, validate_image_url
from extraction.identity import IdentityDecision, compare_property_identity
from extraction.brochure import _embedded_http_target, enrich_properties
from extraction.models import AssetCandidate, AssetType, BrochureExtraction, BrochureResource, Property
from extraction.schema import normalize_record
from extraction.file_readers import read_file
from extraction.rules.workplace_plus import _listing_assets
from extraction.rules import try_rules
import app as app_module


def values(**overrides):
    result = {
        "Building": "Example House, 1 Example Street",
        "Property Address 1": "1 Example Street",
        "Property Postcode": "EC1A 1AA",
        "Floor/Unit": "Third Floor",
    }
    result.update(overrides)
    return result


def test_identity_gate_has_four_explicit_outcomes():
    exact = compare_property_identity(values(), "Example House, 1 Example Street, EC1A 1AA")
    probable = compare_property_identity(values(**{"Property Postcode": "", "Property Address 1": ""}), "Welcome to Example House")
    ambiguous = compare_property_identity(values(), "Premium fitted workspace")
    conflict = compare_property_identity(values(), "Other House, 99 Other Road, W1A 1AA")
    assert exact.decision == IdentityDecision.MATCH
    assert "exact postcode" in exact.reasons
    assert probable.decision == IdentityDecision.PROBABLE_MATCH
    assert ambiguous.decision == IdentityDecision.AMBIGUOUS
    assert conflict.decision == IdentityDecision.HARD_CONFLICT
    assert "postcode conflict" in conflict.reasons[0]


def test_structural_confidence_never_overrides_explicit_conflict():
    generic = compare_property_identity(values(), "Download brochure", association_confidence=1.0)
    conflict = compare_property_identity(values(), "99 Other Road W1A 1AA", association_confidence=1.0)
    assert generic.decision == IdentityDecision.PROBABLE_MATCH
    assert conflict.decision == IdentityDecision.HARD_CONFLICT


def test_property_image_classification_requires_positive_evidence():
    generic = classify_candidate(AssetCandidate("https://cdn.test/123.jpg", "html"))
    reception = classify_candidate(AssetCandidate("https://cdn.test/123.jpg", "html", alt_text="Reception"))
    logo = classify_candidate(AssetCandidate("https://cdn.test/logo.jpg", "html", alt_text="Company logo"))
    preview = classify_candidate(AssetCandidate("https://cdn.test/preview.jpg", "html", anchor_text="page preview image"))
    floorplan = classify_candidate(AssetCandidate("https://cdn.test/plan.jpg", "html", alt_text="Third floor plan"))
    repeated = classify_candidate(AssetCandidate("https://cdn.test/123.jpg", "html", occurrence_count=5))
    small = classify_candidate(AssetCandidate("https://cdn.test/office.jpg", "html", width=120, height=80))
    repeated_associated = classify_candidate(AssetCandidate("https://cdn.test/123.jpg", "pdf", occurrence_count=5, association_confidence=0.85))
    asserted = classify_candidate(AssetCandidate("https://cdn.test/123", "html", mime_type="image/jpeg", association_confidence=0.9))
    assert generic.classification == AssetType.UNKNOWN
    assert reception.classification == AssetType.PROPERTY_IMAGE
    assert logo.classification == AssetType.LOGO
    assert preview.classification == AssetType.DOCUMENT_PREVIEW
    assert floorplan.classification == AssetType.FLOORPLAN
    assert repeated.classification == AssetType.DECORATIVE
    assert small.classification == AssetType.DECORATIVE
    assert asserted.classification == AssetType.PROPERTY_IMAGE
    assert repeated_associated.classification == AssetType.PROPERTY_IMAGE


def test_url_union_is_stable_canonical_and_safe():
    merged = merge_candidate_urls(
        ["https://EXAMPLE.test/a.jpg?utm_source=x", "javascript:alert(1)"],
        ["https://example.test/a.jpg", "https://example.test/b.jpg"],
    )
    assert merged == ["https://example.test/a.jpg", "https://example.test/b.jpg"]
    assert normalize_url("mailto:test@example.com") == ""
    assert normalize_url("https://example.test/a.jpg#fragment") == "https://example.test/a.jpg"


class FakeResponse:
    def __init__(self, payload, content_type="image/jpeg", url="https://img.test/final.jpg", status=200):
        self.content = payload
        self.headers = {"Content-Type": content_type}
        self.url = url
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def image_bytes(size=(640, 480)):
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, "JPEG")
    return output.getvalue()


def test_image_validation_is_bounded_cached_and_typed():
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(image_bytes())
    cache = {}
    first = validate_image_url("https://img.test/start.jpg", requester=request, cache=cache)
    second = validate_image_url("https://img.test/start.jpg", requester=request, cache=cache)
    assert first["ok"] is True
    assert first["status"] == "VALID_IMAGE"
    assert first["url"] == "https://img.test/final.jpg"
    assert second == first
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 3.0
    assert calls[0][1]["headers"]["Range"].startswith("bytes=0-")


def test_image_validation_rejects_html_small_and_inaccessible():
    html = validate_image_url("https://img.test/html", requester=lambda *a, **k: FakeResponse(b"<html>", "text/html"), cache={})
    small = validate_image_url("https://img.test/small", requester=lambda *a, **k: FakeResponse(image_bytes((100, 80))), cache={})
    failed = validate_image_url("https://img.test/fail", requester=lambda *a, **k: (_ for _ in ()).throw(TimeoutError("down")), cache={})
    assert html["ok"] is False and html["status"] == "NOT_AN_IMAGE"
    assert small["ok"] is False and small["status"] == "IMAGE_TOO_SMALL"
    assert failed["ok"] is False and failed["status"] == "LINK_EXPIRED_OR_INACCESSIBLE"


def test_workplace_assets_are_joined_by_same_card_tracking_url():
    items = [
        ("link", "", "https://track.test/a"),
        ("image", "", "https://gallery.eocampaign1.com/x%2F019-a.jpg"),
        ("link", "Brochure", "https://track.test/a"),
        ("link", "", "https://track.test/b"),
        ("image", "", "https://gallery.eocampaign1.com/x%2F019-b.jpg"),
        ("link", "Brochure", "https://track.test/c"),
    ]
    pairs = _listing_assets(items)
    assert pairs == [("https://track.test/a", "https://gallery.eocampaign1.com/x%2F019-a.jpg")]


def test_real_workplace_fixture_keeps_durweston_in_its_own_card():
    content = read_file("Fw_ Workplace Plus - Availability 14th July (1).eml")
    pairs = _listing_assets(content["html_items"])
    assert len(pairs) == 3
    assert len({brochure for brochure, _photo in pairs}) == 3
    assert all(photo.endswith(".jpg") for _brochure, photo in pairs)



def test_exact_durweston_risborough_conflict_merges_nothing():
    link = "https://track.workplace.test/durweston"
    record = normalize_record({
        "Building": "8 Durweston Street, W1H 1EW",
        "Property Postcode": "W1H 1EW",
        "Brochure PDF": link,
    })
    prop = Property.from_record(record, "workplace.eml", "Workplace Plus", "rule:Workplace Plus")
    extraction = BrochureExtraction(
        link,
        assets=[classify_candidate(AssetCandidate("https://img.test/risborough.jpg", link, alt_text="Office"))],
        identity_text="17-21 Risborough Street, London SE1 0HG",
    )
    result = enrich_properties(
        [prop],
        fetcher=lambda _: BrochureResource(b"linked", "text/html", link, link),
        extractor=lambda *_: extraction,
    )[0]
    assert any(item.status == "LINK_IDENTITY_HARD_CONFLICT" for item in result.link_diagnostics)
    assert not result.assets
    assert not result.values["High Res Images"]
    assert not result.values["Floor Plan"]



def test_gpe_property_page_links_enter_shared_linked_source_field():
    path = next(Path(".").glob("Fw_ The latest GPE Fully Managed availability*.eml"))
    rule, records = try_rules(read_file(path))
    assert rule == "GPE"
    assert len(records) == 15
    assert all(record.get("Brochure PDF", "").startswith("http") for record in records)
    by_building = {}
    for record in records:
        by_building.setdefault(record["Building"], record["Brochure PDF"])
        assert record["Brochure PDF"] == by_building[record["Building"]]


def test_truthful_blank_image_diagnostic(tmp_path):
    record = {"Building": "No Photo House", "High Res Images": ""}
    with app_module.app.test_request_context("/process", base_url="https://app.test"):
        jobs = app_module._finalize_high_res_images([record], tmp_path, "batch", "Example", image_validator=lambda *_a, **_k: None)
    assert jobs == []
    assert record["High Res Images"] == ""
    assert [item.status for item in record["_link_diagnostics"]] == ["NO_IMAGES_DISCOVERED"]



def test_json_tracking_parameter_resolves_explicit_https_target_only():
    tracked = "https://tracker.test/go?payload=%7B%22TargetUrl%22%3A%22https%253A%252F%252Fproperty.test%252Fone%22%7D"
    assert _embedded_http_target(tracked) == "https://property.test/one"
    assert _embedded_http_target("https://tracker.test/go?payload=%7B%22TargetUrl%22%3A%22javascript%253Aalert%25281%2529%22%7D") == ""



def test_responsive_renditions_of_same_image_deduplicate_but_distinct_paths_survive():
    urls = merge_candidate_urls([
        "https://img.test/media/office.jpg?width=310&height=175&format=webp&v=1",
        "https://img.test/media/office.jpg?width=1160&format=webp&v=1",
        "https://img.test/media/terrace.jpg?width=1160&v=1",
    ])
    assert urls == ["https://img.test/media/office.jpg", "https://img.test/media/terrace.jpg"]
