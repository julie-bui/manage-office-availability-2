from pathlib import Path
from io import BytesIO
import time

import pytest
import requests
from PIL import Image

from extraction import brochure as brochure_module, pipeline
from extraction.assets import classify_candidate, merge_candidate_urls, normalize_url, validate_image_url
from extraction.html_images import _IMAGE_FETCH_TIMEOUT_SECONDS
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
    assert calls[0][1]["timeout"] == _IMAGE_FETCH_TIMEOUT_SECONDS
    assert calls[0][1]["headers"]["Range"].startswith("bytes=0-")


def test_image_validation_rejects_html_small_and_inaccessible():
    html = validate_image_url("https://img.test/html", requester=lambda *a, **k: FakeResponse(b"<html>", "text/html"), cache={})
    small = validate_image_url("https://img.test/small", requester=lambda *a, **k: FakeResponse(image_bytes((100, 80))), cache={})
    failed = validate_image_url("https://img.test/fail", requester=lambda *a, **k: (_ for _ in ()).throw(TimeoutError("down")), cache={})
    assert html["ok"] is False and html["status"] == "NOT_AN_IMAGE"
    assert small["ok"] is False and small["status"] == "IMAGE_TOO_SMALL"
    assert failed["ok"] is False and failed["status"] == "LINK_EXPIRED_OR_INACCESSIBLE"


def test_timeout_is_retried_once_within_deadline_budget_and_can_succeed():
    """Confirmed real (2026-07, MetSpace mcusercontent.com): a real, live
    image can take longer than one attempt's timeout to transfer (larger
    file, cold CDN cache). A bare requests timeout shouldn't permanently
    blank it if there's deadline budget left for one more try."""
    calls = []
    def flaky_then_ok(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("slow CDN")
        return FakeResponse(image_bytes())
    result = validate_image_url(
        "https://img.test/slow.jpg", requester=flaky_then_ok, cache={},
        deadline=time.monotonic() + 100,
    )
    assert len(calls) == 2
    assert result["ok"] is True
    assert result["status"] == "VALID_IMAGE"


def test_timeout_gets_its_own_status_distinct_from_a_real_dead_link():
    """LINK_TIMED_OUT (a slow/uncertain fetch) must stay distinguishable
    from LINK_EXPIRED_OR_INACCESSIBLE (a real 404/DNS/connection failure)
    on the QA sheet, even after retrying once and still timing out."""
    def always_slow(url, **kwargs):
        raise requests.exceptions.ConnectTimeout("too slow")
    result = validate_image_url(
        "https://img.test/slow.jpg", requester=always_slow, cache={},
        deadline=time.monotonic() + 100,
    )
    assert result["ok"] is False
    assert result["status"] == "LINK_TIMED_OUT"

    dead = validate_image_url(
        "https://img.test/dead.jpg",
        requester=lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("refused")),
        cache={},
    )
    assert dead["status"] == "LINK_EXPIRED_OR_INACCESSIBLE"


def test_timeout_retry_never_risks_the_callers_own_deadline():
    """A caller's own batch/enrichment deadline (see app.py's
    BATCH_DEADLINE_SECONDS and _finalize_high_res_images) must win over a
    retry: no second attempt if it could run past that deadline."""
    calls = []
    def always_slow(url, **kwargs):
        calls.append(url)
        raise requests.exceptions.ReadTimeout("too slow")
    result = validate_image_url(
        "https://img.test/slow.jpg", requester=always_slow, cache={},
        deadline=time.monotonic() - 1,
    )
    assert len(calls) == 1
    assert result["ok"] is False
    assert result["status"] == "LINK_TIMED_OUT"


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



def test_path_style_local_download_skips_loopback_image_validation(tmp_path):
    local = "https://service.test/api/download/batch-123/GPE_brochure_r1_photo.jpx?token=secret"
    record = {"Building": "Example House", "_high_res_candidates": [local]}

    def must_not_fetch(*_args, **_kwargs):
        raise AssertionError("local batch asset must not be fetched over HTTP")

    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch-123", "GPE", image_validator=must_not_fetch
        )
    assert jobs == []
    assert record["High Res Images"] == local
    assert record["_link_diagnostics"][-1].status == "DIRECT_IMAGE_ASSIGNED"

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


def test_source_image_survives_failed_optional_image_validation(tmp_path):
    source = "https://source.test/email-card.jpg"
    external = "https://linked.test/gallery-extra.jpg"
    record = {
        "Building": "Example House",
        "_source_high_res_candidates": [source],
        "_high_res_candidates": [source, external],
    }

    def reject_external(url, cache=None):
        assert url == external
        return {"ok": False, "url": url, "status": "LINK_EXPIRED_OR_INACCESSIBLE"}

    jobs = app_module._finalize_high_res_images(
        [record], tmp_path, "batch", "Example", image_validator=reject_external
    )
    assert jobs == []
    assert record["High Res Images"] == source


def test_source_and_linked_images_create_gallery(tmp_path):
    source = "https://source.test/email-card.jpg"
    external = "https://linked.test/gallery-extra.jpg"
    record = {
        "Building": "Example House",
        "_source_high_res_candidates": [source],
        "_high_res_candidates": [source, external],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {
                "ok": True, "url": url, "status": "VALID_IMAGE"
            },
        )
    assert len(jobs) == 1
    gallery = jobs[0][1].read_text(encoding="utf-8")
    assert source in gallery
    assert external in gallery


def test_deadline_skips_optional_image_but_keeps_source(tmp_path):
    source = "https://source.test/metspace-photo.jpg"
    external = "https://linked.test/extra.jpg"
    record = {
        "_source_high_res_candidates": [source],
        "_high_res_candidates": [source, external],
    }
    jobs = app_module._finalize_high_res_images(
        [record], tmp_path, "batch", "MetSpace",
        image_validator=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("deadline must prevent optional network validation")
        ),
        deadline=time.monotonic() + 1,
    )
    assert jobs == []
    assert record["High Res Images"] == source


def test_gpe_core_extraction_skips_linked_fetch_near_deadline(monkeypatch):
    path = next(Path(".").glob("Fw_ The latest GPE Fully Managed availability*.eml"))
    monkeypatch.setattr(pipeline, "_geocode_records", lambda *_args: (False, True))

    def must_not_fetch(_url):
        raise AssertionError("optional linked fetch must not start near deadline")

    result = pipeline.process_files(
        [path], deadline=time.monotonic() + 19,
        brochure_enrichment=True, brochure_fetcher=must_not_fetch,
    )[0]
    assert result["status"] == "ok"
    assert result["record_count"] == 15
    assert any(record.get("_source_high_res_candidates") for record in result["records"])


def test_brochure_fetch_does_not_start_after_deadline(monkeypatch):
    def must_not_request(*_args, **_kwargs):
        raise AssertionError("expired optional fetch must not reach DNS/network")

    monkeypatch.setattr(brochure_module.requests, "get", must_not_request)
    with pytest.raises(brochure_module.LinkedResourceError) as error:
        brochure_module.fetch_brochure(
            "https://property.test/listing",
            deadline=time.monotonic() - 1,
        )
    assert error.value.status == "LINK_ENRICHMENT_SKIPPED"
