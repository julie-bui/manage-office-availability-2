from pathlib import Path
from io import BytesIO
import time

import pytest
import requests
from PIL import Image

from extraction import brochure as brochure_module, pipeline
from extraction.assets import (
    classify_candidate,
    evaluate_image_bytes,
    image_content_hash,
    is_blank_or_empty_image,
    merge_candidate_urls,
    normalize_url,
    validate_image_url,
)
from extraction.html_images import _IMAGE_FETCH_TIMEOUT_SECONDS
from extraction.identity import IdentityDecision, compare_property_identity
from extraction.brochure import _embedded_http_target, enrich_properties
from extraction.models import AssetCandidate, AssetType, BrochureExtraction, BrochureResource, Property
from extraction.schema import normalize_record
from extraction.file_readers import read_file
from extraction.rules.workplace_plus import ADDRESS_RE, _listing_assets
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
    """Colourful synthetic photo — solid white would correctly be rejected
    as IMAGE_BLANK_OR_EMPTY by the shared blank-slide detector."""
    output = BytesIO()
    image = Image.new("RGB", size, (20, 20, 20))
    pixels = image.load()
    width, height = size
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 3) % 256, (y * 5) % 256, (x + y) % 256)
    image.save(output, "JPEG")
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
    statuses = [item.status for item in record["_link_diagnostics"]]
    assert "DIRECT_IMAGE_ASSIGNED" in statuses
    assert "IMAGE_COUNT_BELOW_TARGET" in statuses

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

    def reject_external(url, cache=None, deadline=None):
        if url == source:
            return {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": "source-bytes"}
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
    # Non-image-like optional URL must still be skipped under deadline;
    # already-discovered image-like CDN assets are kept elsewhere.
    external = "https://linked.test/property-page"
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


def test_gallery_creation_failure_falls_back_to_first_image(tmp_path, monkeypatch):
    urls = ["https://img.test/a.jpg", "https://img.test/b.jpg"]
    record = {
        "Building": "Two Photo House",
        "_source_high_res_candidates": urls,
        "_high_res_candidates": urls,
    }

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app_module.pdf_images, "build_gallery_html", boom)
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE"},
        )
    assert jobs == []
    assert record["High Res Images"] == urls[0]
    statuses = [item.status for item in record["_link_diagnostics"]]
    assert "GALLERY_CREATION_FAILED" in statuses
    assert "DIRECT_IMAGE_ASSIGNED" in statuses
    assert any("fell back" in (item.detail or "") for item in record["_link_diagnostics"])


def test_enrichment_prioritizes_listings_without_source_photos():
    """Budget should prefer blank High Res rows over deepening existing photos."""
    fetched = []

    def fetch(url):
        fetched.append(url)
        return BrochureResource(b"<html>Example House, 1 Example Street, EC1A 1AA</html>", "text/html", url, url)

    def extract(payload, content_type, final_url):
        return BrochureExtraction(
            final_url,
            assets=[classify_candidate(AssetCandidate(f"{final_url}/photo.jpg", final_url, alt_text="Office"))],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    already_has_photo = Property.from_record(
        normalize_record({
            "Building": "Example House, 1 Example Street",
            "Property Postcode": "EC1A 1AA",
            "High Res Images": "https://source.test/card.jpg",
            "_source_high_res_candidates": ["https://source.test/card.jpg"],
            "Brochure PDF": "https://property.test/has-photo",
        }),
        "mixed.eml", "Mixed", "rule:test",
    )
    needs_photo = Property.from_record(
        normalize_record({
            "Building": "Example House, 1 Example Street",
            "Property Postcode": "EC1A 1AA",
            "Brochure PDF": "https://property.test/needs-photo",
        }),
        "mixed.eml", "Mixed", "rule:test",
    )

    result = enrich_properties(
        [already_has_photo, needs_photo],
        fetcher=fetch,
        extractor=extract,
    )
    assert fetched[0] == "https://property.test/needs-photo"
    assert fetched[1] == "https://property.test/has-photo"
    # Original caller order is preserved in the returned list.
    assert result[0] is already_has_photo
    assert result[1] is needs_photo


def test_linked_enrichment_deadline_uses_batch_headroom(monkeypatch):
    """Solo uploads with free batch time must not be hard-capped at 15s."""
    path = next(Path(".").glob("Fw_ The latest GPE Fully Managed availability*.eml"))
    captured = {}

    def fake_enrich(properties, **kwargs):
        captured["deadline"] = kwargs.get("deadline")
        return list(properties)

    monkeypatch.setattr(pipeline.brochure, "enrich_properties", fake_enrich)
    monkeypatch.setattr(pipeline, "_geocode_records", lambda *_args: (False, False))

    batch_deadline = time.monotonic() + 80
    result = pipeline.process_files(
        [path],
        deadline=batch_deadline,
        brochure_enrichment=True,
    )[0]
    assert result["status"] == "ok"
    assert captured["deadline"] == pytest.approx(batch_deadline - pipeline.ENRICHMENT_FINALIZE_RESERVE_SECONDS, abs=0.5)
    # Solo files still receive nearly the full remaining enrichment headroom
    # (deadline - finalize reserve), not an arbitrary 15s hard cap.
    assert captured["deadline"] > batch_deadline - pipeline.ENRICHMENT_FINALIZE_RESERVE_SECONDS - 1


def test_batch_file_index_preserves_fair_share_when_finishing_one_at_a_time(monkeypatch):
    """app.py finishes each file before the next; process_files still shares time."""
    path = next(Path(".").glob("Fw_ Knotel Availability _ 30_06_2026.eml"))
    started = time.monotonic()
    captured = []

    def fake_enrich(properties, **kwargs):
        captured.append({"deadline": kwargs.get("deadline"), "started": time.monotonic()})
        return list(properties)

    monkeypatch.setattr(pipeline.brochure, "enrich_properties", fake_enrich)
    monkeypatch.setattr(pipeline, "_geocode_records", lambda *_args: (False, False))

    batch_deadline = started + 80
    pipeline.process_files(
        [path],
        deadline=batch_deadline,
        brochure_enrichment=True,
        batch_total_files=4,
        batch_file_index=1,
    )
    assert len(captured) == 1
    pool_end = batch_deadline - pipeline.ENRICHMENT_FINALIZE_RESERVE_SECONDS
    # File index 1 of 4 → base share rem/3 at call start. Sheets with many
    # unique brochure URLs may receive a larger weighted slice, but must still
    # leave a meaningful remainder for later files.
    base_deadline = captured[0]["started"] + (pool_end - captured[0]["started"]) / 3
    assert captured[0]["deadline"] + 0.01 >= base_deadline
    assert captured[0]["deadline"] <= pool_end + 0.5
    assert (pool_end - captured[0]["deadline"]) > 5


def test_batch_enrichment_splits_deadline_across_files(monkeypatch):
    """Each file takes a fair share of remaining enrichment time when it starts.

    Confirmed real (2026-07): MetSpace+Knotel together left Knotel on its
    single email featured photo because MetSpace consumed the shared budget.
    Absolute-from-batch-start windows also failed — MetSpace overrun left
    Knotel with almost no time while later files still got galleries.
    """
    paths = [
        next(Path(".").glob("Fw_ MetSpace Availability Update.eml")),
        next(Path(".").glob("Fw_ Knotel Availability _ 30_06_2026.eml")),
    ]
    captured = []

    def fake_enrich(properties, **kwargs):
        captured.append({"deadline": kwargs.get("deadline"), "started": time.monotonic()})
        # Simulate MetSpace overrun past a naive absolute midpoint so the
        # second file's fair-share deadline must be computed from "now".
        if len(captured) == 1:
            time.sleep(0.35)
        return list(properties)

    monkeypatch.setattr(pipeline.brochure, "enrich_properties", fake_enrich)
    monkeypatch.setattr(pipeline, "_geocode_records", lambda *_args: (False, False))

    batch_start = time.monotonic()
    batch_deadline = batch_start + 80
    results = pipeline.process_files(paths, deadline=batch_deadline, brochure_enrichment=True)
    assert [r["status"] for r in results] == ["ok", "ok"]
    assert len(captured) == 2
    pool_end = batch_deadline - pipeline.ENRICHMENT_FINALIZE_RESERVE_SECONDS
    # File 0: half of remaining pool at start.
    assert captured[0]["deadline"] == pytest.approx(
        captured[0]["started"] + (pool_end - captured[0]["started"]) / 2,
        abs=0.5,
    )
    # File 1 starts after MetSpace delay and still receives nearly all
    # remaining pool time (1 file left → full remainder).
    assert captured[1]["deadline"] == pytest.approx(pool_end, abs=1.0)
    assert captured[1]["deadline"] - captured[1]["started"] > 30


def test_enrichment_wave_hard_stops_at_deadline():
    """New brochure waves must not start inside the pre-deadline margin."""
    started = time.monotonic()
    deadline = started + 0.3
    calls = []

    def slow_fetch(url, deadline=None):
        calls.append(url)
        time.sleep(2.0)
        return BrochureResource(b"<html></html>", "text/html", url, url)

    def extract(payload, content_type, final_url):
        return BrochureExtraction(final_url, assets=[], identity_text="Example House EC1A 1AA")

    props = [
        Property.from_record(
            normalize_record({
                "Building": "Example House",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": f"https://property.test/{i}",
            }),
            "batch.eml",
            "Knotel",
            "rule:Knotel",
        )
        for i in range(6)
    ]
    enrich_properties(props, fetcher=slow_fetch, extractor=extract, deadline=deadline)
    # 3s pre-deadline margin with a 0.3s budget → no wave starts.
    assert calls == []
    assert time.monotonic() - started < 0.8
    # HTML property-page seeds are kept on Brochure PDF but are no longer
    # copied into High Res (prefer blank over a non-image click-through).
    assert all(prop.values.get("Brochure PDF") for prop in props)
    assert all(not prop.values.get("High Res Images") for prop in props)


def test_retrieve_skips_nested_pdf_when_page_has_gallery_and_floorplan():
    """Knotel HTML galleries with a plan seed should not fetch nested brochures."""
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        assert "brochure.pdf" not in url
        return BrochureResource(b"<html>Example House EC1A 1AA</html>", "text/html", url, url)

    def extract(payload, content_type, final_url):
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"{final_url}/{name}.jpg",
                    final_url,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            final_url,
            assets=photos + [
                classify_candidate(
                    AssetCandidate(
                        f"{final_url}/plan.jpg",
                        final_url,
                        alt_text="Floor plan",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                    )
                ),
                classify_candidate(AssetCandidate(f"{final_url}/brochure.pdf", final_url, anchor_text="Download brochure")),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    prop = Property.from_record(
        normalize_record({
            "Building": "Example House, 1 Example Street",
            "Property Postcode": "EC1A 1AA",
            "Brochure PDF": "https://property.test/listing",
        }),
        "knotel.eml",
        "Knotel",
        "rule:Knotel",
    )
    enrich_properties([prop], fetcher=fetch, extractor=extract)
    assert fetched == ["https://property.test/listing"]
    assert len(prop.values.get("_high_res_candidates") or []) >= 2


def test_retrieve_follows_nested_pdf_for_floorplan_when_gallery_full():
    """GPE: HTML gallery is enough for High Res, but plans live in Download brochure."""
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        if url.endswith(".pdf"):
            return BrochureResource(b"%PDF-1.4 plan", "application/pdf", url, url)
        return BrochureResource(b"<html>Example House EC1A 1AA</html>", "text/html", url, url)

    def extract(payload, content_type, final_url):
        if payload.startswith(b"%PDF"):
            return BrochureExtraction(
                final_url,
                assets=[
                    AssetCandidate(
                        "",
                        final_url,
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        content=b"plan-bytes",
                        content_hash="planhash1",
                        association_confidence=0.0,
                    )
                ],
                identity_text="Example House, 1 Example Street, EC1A 1AA",
            )
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"https://cdn.property.test/{name}.jpg",
                    final_url,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            final_url,
            assets=photos + [
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/brochure.pdf",
                        final_url,
                        mime_type="application/pdf",
                        classification=AssetType.BROCHURE,
                        confidence=0.9,
                        anchor_text="Download brochure",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    prop = Property.from_record(
        normalize_record({
            "Building": "Example House, 1 Example Street",
            "Property Postcode": "EC1A 1AA",
            "Brochure PDF": "https://property.test/listing",
        }),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    enrich_properties([prop], fetcher=fetch, extractor=extract)
    assert any(url.endswith(".pdf") for url in fetched)
    assert prop.values.get("Floor Plan") or any(
        a.classification == AssetType.FLOORPLAN
        for a in (prop.values.get("_brochure_embedded_assets") or [])
    )


def test_retrieve_always_follows_drive_download_despite_viewer_chrome():
    """MetSpace/WP Drive shells have chrome images — must still fetch the PDF."""
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        if "usercontent" in url:
            return BrochureResource(b"%PDF-1.4 fake", "application/pdf", url, url)
        return BrochureResource(b"<html>Drive viewer</html>", "text/html", url, url)

    def extract(payload, content_type, final_url):
        if payload.startswith(b"%PDF"):
            return BrochureExtraction(
                final_url,
                assets=[
                    AssetCandidate(
                        "", final_url, classification=AssetType.PROPERTY_IMAGE, confidence=0.82,
                        content=_photo_jpeg(1), content_hash="drivephoto1", extension="jpg",
                        width=640, height=400, association_confidence=0.85,
                    ),
                    AssetCandidate(
                        "", final_url, classification=AssetType.FLOORPLAN, confidence=0.9,
                        content=_photo_jpeg(2), content_hash="driveplan1", extension="jpg",
                        width=800, height=600,
                    ),
                ],
                identity_text="9-10 Market Place EC1A 1AA",
            )
        drive_pdf = "https://drive.usercontent.google.com/download?id=abc&export=download"
        return BrochureExtraction(
            final_url,
            assets=[
                classify_candidate(AssetCandidate(f"{final_url}/chrome1.png", final_url, alt_text="")),
                classify_candidate(AssetCandidate(f"{final_url}/chrome2.png", final_url, alt_text="")),
                AssetCandidate(
                    drive_pdf, final_url, mime_type="application/pdf",
                    classification=AssetType.BROCHURE, confidence=0.9,
                    anchor_text="Download brochure",
                ),
            ],
            identity_text="9-10 Market Place EC1A 1AA",
        )

    prop = Property.from_record(
        normalize_record({
            "Building": "9-10 Market Place",
            "Property Postcode": "W1W 8AQ",
            "Brochure PDF": "https://drive.google.com/file/d/abc/view",
        }),
        "metspace.eml",
        "MetSpace",
        "rule:MetSpace",
    )
    enrich_properties([prop], fetcher=fetch, extractor=extract)
    assert any("usercontent" in url for url in fetched)
    embeds = prop.values.get("_brochure_embedded_assets") or []
    assert any(a.classification == AssetType.PROPERTY_IMAGE for a in embeds)
    assert any(a.classification == AssetType.FLOORPLAN for a in embeds)


def test_workplace_plus_address_accepts_manchester_and_other_cities():
    assert ADDRESS_RE.match("12 Dummy Street, Manchester, M1 2AB")
    assert ADDRESS_RE.match("77 Gracechurch Street, EC3V 0AS")
    assert ADDRESS_RE.match("150 Waterloo Road, London, SE1 8SB")
    assert not ADDRESS_RE.match("Not an address line")


def test_image_coverage_warns_when_non_exempt_file_has_no_photos():
    warning = app_module._image_coverage_warning(
        [{"Building": "A", "High Res Images": ""}, {"Building": "B", "High Res Images": ""}],
        "rule:Workplace Plus",
    )
    assert "No High Res Images" in warning
    assert app_module._image_coverage_warning(
        [{"Building": "A", "High Res Images": ""}],
        "rule:BC",
    ) == ""


def test_finalize_keeps_discovered_photos_when_batch_deadline_elapsed(tmp_path):
    """Under deadline, keep up to MIN_HIGH_RES unhashed photos; extras skip."""
    urls = [
        f"https://knotel.directus.app/assets/{i:08d}-bbbb-cccc-dddd-eeeeeeeeeeee"
        for i in range(8)
    ]
    record = {
        "Building": "Classic House",
        "_source_high_res_candidates": [urls[0]],
        "_high_res_candidates": urls,
        "High Res Images": urls[0],
    }
    past = time.monotonic() - 1
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Knotel",
            image_validator=lambda *_a, **_k: {"ok": False, "status": "SHOULD_NOT_RUN"},
            deadline=past,
        )
    assert record["_high_res_image_count"] == app_module.MIN_HIGH_RES_IMAGES
    assert jobs  # gallery HTML for 5+ images
    assert any(item.status == "IMAGE_UNHASHED_SKIPPED" for item in record["_link_diagnostics"])


def test_finalize_caps_high_res_gallery_at_eight(tmp_path):
    urls = [f"https://img.test/{i}.jpg" for i in range(12)]
    record = {
        "Building": "Many Photo House",
        "_source_high_res_candidates": urls,
        "_high_res_candidates": urls,
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert len(jobs) == 1
    gallery = jobs[0][1].read_text(encoding="utf-8")
    assert gallery.count("<img") == 8
    assert record["_high_res_image_count"] == 8
    assert any(item.status == "IMAGE_CANDIDATES_CAPPED" for item in record["_link_diagnostics"])


def test_finalize_excludes_floor_plan_url_from_high_res(tmp_path):
    plan = "https://cdn.test/floorplan.jpg"
    photo = "https://cdn.test/office.jpg"
    record = {
        "Building": "Plan Leak House",
        "Floor Plan": plan,
        "_high_res_candidates": [plan, photo],
        "_source_high_res_candidates": [plan, photo],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert record["High Res Images"] == photo
    assert any(item.status == "IMAGE_IS_FLOORPLAN" for item in record["_link_diagnostics"])


def _solid_jpeg(color):
    buffer = BytesIO()
    Image.new("RGB", (640, 400), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _photo_jpeg(seed=0):
    buffer = BytesIO()
    image = Image.new("RGB", (640, 400), (20, 20, 20))
    pixels = image.load()
    for y in range(400):
        for x in range(640):
            pixels[x, y] = ((x * 3 + seed) % 256, (y * 5 + seed) % 256, (x + y + seed) % 256)
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_blank_near_solid_image_is_rejected():
    assert is_blank_or_empty_image(_solid_jpeg((10, 12, 14)))
    assert not is_blank_or_empty_image(_photo_jpeg(3))


def test_sparse_large_png_wash_is_rejected_as_blank():
    """MetSpace Drive sparse 4000×2250 washes (~0.007 bpp) must not enter High Res."""
    image = Image.new("RGB", (2666, 1499), (240, 240, 238))
    pixels = image.load()
    for x in range(0, 2666, 80):
        for y in range(100, 200):
            pixels[x, y] = (40, 40, 40)
    buf = BytesIO()
    image.save(buf, format="PNG", optimize=True)
    payload = buf.getvalue()
    assert len(payload) / (2666 * 1499) < 0.02
    assert is_blank_or_empty_image(payload) or evaluate_image_bytes(payload)["status"] in {
        "IMAGE_BLANK_OR_EMPTY",
        "IMAGE_IS_FLOORPLAN",
    }
    assert evaluate_image_bytes(payload).get("ok") is False


def test_materialize_replays_shared_embeds_for_sibling_floors(tmp_path):
    photo = _photo_jpeg(11)
    digest = image_content_hash(photo)
    shared = [
        AssetCandidate(
            "", "brochure", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
            content=photo, content_hash=digest, extension="jpg", width=640, height=400,
        ),
        AssetCandidate(
            "", "brochure", classification=AssetType.FLOORPLAN, confidence=0.9,
            content=_photo_jpeg(12), content_hash="planhash99", extension="jpg", width=800, height=600,
        ),
    ]
    # Force floorplan-looking bytes for the plan candidate
    plan = Image.new("RGB", (800, 600), (250, 250, 250))
    px = plan.load()
    for x in range(0, 800, 40):
        for y in range(600):
            px[x, y] = (30, 30, 30)
    pbuf = BytesIO()
    plan.save(pbuf, format="JPEG")
    shared[1].content = pbuf.getvalue()
    shared[1].content_hash = image_content_hash(shared[1].content)

    records = [
        {"Building": "Shared House", "Floor/Unit": "1st", "Floor Plan": "https://drive.google.com/file/d/x/view",
         "_brochure_embedded_assets": shared},
        {"Building": "Shared House", "Floor/Unit": "2nd", "Floor Plan": "https://drive.google.com/file/d/x/view",
         "_brochure_embedded_assets": shared},
    ]
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._materialize_brochure_assets(records, tmp_path, "batch", "Example")
    assert len(records[0].get("_high_res_candidates") or []) >= 1
    assert len(records[1].get("_high_res_candidates") or []) >= 1
    assert "/api/download/" in (records[0].get("Floor Plan") or "")
    assert records[0]["Floor Plan"] == records[1]["Floor Plan"]


def test_evaluate_image_bytes_rejects_floorplan_diagram():
    plan = Image.new("RGB", (800, 600), (250, 250, 250))
    pixels = plan.load()
    for x in range(0, 800, 40):
        for y in range(600):
            pixels[x, y] = (40, 40, 40)
    buf = BytesIO()
    plan.save(buf, format="JPEG")
    result = evaluate_image_bytes(buf.getvalue(), url="https://cdn.test/plan.jpg")
    assert result["ok"] is False
    assert result["status"] == "IMAGE_IS_FLOORPLAN"
    assert evaluate_image_bytes(_photo_jpeg(4), url="https://cdn.test/office.jpg")["ok"]


def test_finalize_excludes_floorplan_named_url(tmp_path):
    record = {
        "Building": "Named Plan House",
        "Floor Plan": "",
        "_high_res_candidates": [
            "https://cdn.test/media/floorplan-level-2.png",
            "https://cdn.test/media/office.jpg",
        ],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert record["High Res Images"] == "https://cdn.test/media/office.jpg"
    assert "floorplan" in (record.get("Floor Plan") or "").lower()


def test_share_underfilled_building_photos_copies_sibling_gallery():
    donor_urls = [f"https://img.test/{i}.jpg" for i in range(6)]
    donor = Property.from_record(
        {
            **values(Building="Shared Tower", **{"Floor/Unit": "1st"}),
            "High Res Images": donor_urls[0],
            "_high_res_candidates": donor_urls,
        },
        "src.eml",
        "Knotel",
        "rule:Knotel",
    )
    sibling = Property.from_record(
        {
            **values(Building="Shared Tower", **{"Floor/Unit": "5th"}),
            "High Res Images": "https://img.test/featured-only.jpg",
            "_high_res_candidates": ["https://img.test/featured-only.jpg"],
        },
        "src.eml",
        "Knotel",
        "rule:Knotel",
    )
    brochure_module._share_underfilled_building_photos([donor, sibling])
    merged = sibling.values.get("_high_res_candidates") or []
    assert len(merged) >= 5
    assert donor_urls[1] in merged


def test_soft_accept_does_not_pad_gallery_with_dead_urls(tmp_path):
    good = "https://cdn.test/real.jpg"
    dead = "https://cdn.test/gone.jpg"
    record = {
        "Building": "Pad House",
        "_source_high_res_candidates": [good, dead],
        "_high_res_candidates": [good, dead],
    }

    def validate(url, cache=None, deadline=None):
        if url == good:
            return {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": "goodhash"}
        return {"ok": False, "url": url, "status": "LINK_EXPIRED_OR_INACCESSIBLE"}

    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example", image_validator=validate
        )
    assert record["High Res Images"] == good
    assert record["_high_res_image_count"] == 1


def test_materialize_reclassifies_floorplan_bitmap_out_of_high_res(tmp_path):
    # Near-white diagram should be treated as Floor Plan, not High Res.
    plan = Image.new("RGB", (800, 600), (250, 250, 250))
    pixels = plan.load()
    for x in range(0, 800, 40):
        for y in range(600):
            pixels[x, y] = (40, 40, 40)
    buf = BytesIO()
    plan.save(buf, format="JPEG")
    plan_bytes = buf.getvalue()
    photo_bytes = _photo_jpeg(9)
    record = {
        "Building": "Mixed Brochure House",
        "Floor Plan": "",
        "_brochure_embedded_assets": [
            AssetCandidate(
                "", "brochure", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
                content=plan_bytes, content_hash="planhash01", extension="jpg", width=800, height=600,
            ),
            AssetCandidate(
                "", "brochure", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
                content=photo_bytes, content_hash="photohash01", extension="jpg", width=640, height=400,
            ),
        ],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._materialize_brochure_assets([record], tmp_path, "batch", "Example")
    assert ".jpg" in (record.get("Floor Plan") or "")
    assert len(record.get("_high_res_candidates") or []) == 1
    assert any(item.status == "IMAGE_IS_FLOORPLAN" for item in record["_link_diagnostics"])


def test_finalize_dedupes_identical_bytes_under_different_urls(tmp_path):
    """Knotel-style: same Directus photo served under several asset UUIDs."""
    shared = _photo_jpeg(1)
    other = _photo_jpeg(2)
    third = _photo_jpeg(3)
    payloads = {
        "https://cdn.test/asset-a.jpg": shared,
        "https://cdn.test/asset-b.jpg": shared,
        "https://cdn.test/asset-c.jpg": shared,
        "https://cdn.test/asset-d.jpg": other,
        "https://cdn.test/asset-e.jpg": third,
    }
    urls = list(payloads)

    def validate(url, cache=None, deadline=None):
        payload = payloads[url]
        return {
            "ok": True,
            "url": url,
            "status": "VALID_IMAGE",
            "content_hash": image_content_hash(payload),
        }

    record = {
        "Building": "Knotel House",
        "_source_high_res_candidates": urls[:1],
        "_high_res_candidates": urls,
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Knotel", image_validator=validate
        )
    assert len(jobs) == 1
    gallery = jobs[0][1].read_text(encoding="utf-8")
    assert gallery.count("<img") == 3
    assert record["_high_res_image_count"] == 3
    assert sum(1 for item in record["_link_diagnostics"] if item.status == "IMAGE_DUPLICATE_CONTENT") == 2


def test_materialize_skips_blank_photos_keeps_floorplan_first(tmp_path):
    blank = AssetCandidate(
        "", "brochure.pdf", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
        content=_solid_jpeg((8, 8, 8)), content_hash="blank", extension="jpg", width=640, height=400,
    )
    photo = AssetCandidate(
        "", "brochure.pdf", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
        content=_photo_jpeg(9), content_hash="photo", extension="jpg", width=640, height=400,
    )
    floorplan = AssetCandidate(
        "", "brochure.pdf", classification=AssetType.FLOORPLAN, confidence=0.9,
        content=_solid_jpeg((250, 250, 250)), content_hash="plan", extension="jpg", width=640, height=400,
    )
    record = {
        "Building": "First Cell House",
        "Floor Plan": "https://app.box.com/s/vieweronlyshare",
        "High Res Images": "",
        "_brochure_embedded_assets": [blank, photo, floorplan],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._materialize_brochure_assets([record], tmp_path, "batch", "Example")
        app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert record.get("Floor Plan")
    assert "plan" in record["Floor Plan"]
    assert "box.com" not in record["Floor Plan"]
    assert record.get("_high_res_image_count") == 1
    assert any(item.status == "IMAGE_BLANK_OR_EMPTY" for item in record["_link_diagnostics"])
    assert len(jobs) == 2  # photo + floorplan only
    assert blank.content is None
    assert photo.content is None
    assert floorplan.content is None


def test_gallery_uses_absolute_download_urls_not_data_uris(tmp_path):
    """Galleries link to /api/download siblings; sync upload keeps them durable
    without base64-inlining every JPEG into HTML (Render free-tier OOM)."""
    photo_a = _photo_jpeg(1)
    photo_b = _photo_jpeg(2)
    path_a = tmp_path / "Example_brochure_r1_aaaa.jpg"
    path_b = tmp_path / "Example_brochure_r1_bbbb.jpg"
    path_a.write_bytes(photo_a)
    path_b.write_bytes(photo_b)
    url_a = "https://service.test/api/download/batch/Example_brochure_r1_aaaa.jpg"
    url_b = "https://service.test/api/download/batch/Example_brochure_r1_bbbb.jpg"
    record = {
        "Building": "Market Place",
        "_source_high_res_candidates": [url_a, url_b],
        "_high_res_candidates": [url_a, url_b],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None: {
                "ok": True, "url": url, "status": "VALID_IMAGE",
                "content_hash": image_content_hash(photo_a if "aaaa" in url else photo_b),
            },
        )
    assert len(jobs) == 1
    gallery = jobs[0][1].read_text(encoding="utf-8")
    assert gallery.count("data:image/") == 0
    assert gallery.count("<img") == 2
    assert "api/download/batch/Example_brochure_r1_aaaa.jpg" in gallery
    assert "api/download/batch/Example_brochure_r1_bbbb.jpg" in gallery


def test_gallery_img_srcs_include_access_token_when_set(tmp_path, monkeypatch):
    """Hosted batch imgs in gallery HTML must carry ?token= for bare browser loads."""
    monkeypatch.setattr(app_module, "ACCESS_TOKEN", "secret-token")
    photo_a = _photo_jpeg(1)
    photo_b = _photo_jpeg(2)
    (tmp_path / "Example_brochure_r1_aaaa.jpg").write_bytes(photo_a)
    (tmp_path / "Example_brochure_r1_bbbb.jpg").write_bytes(photo_b)
    # Deliberately omit ?token= — the bug case when candidates lack it.
    url_a = "https://service.test/api/download/batch/Example_brochure_r1_aaaa.jpg"
    url_b = "https://service.test/api/download/batch/Example_brochure_r1_bbbb.jpg"
    cdn = "https://cdn.example/photos/lobby.jpg"
    record = {
        "Building": "Market Place",
        "_source_high_res_candidates": [url_a, url_b, cdn],
        "_high_res_candidates": [url_a, url_b, cdn],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "Example",
            image_validator=lambda url, cache=None, **_kw: {
                "ok": True, "url": url, "status": "VALID_IMAGE",
                "content_hash": image_content_hash(
                    photo_a if "aaaa" in url else photo_b if "bbbb" in url else url.encode()
                ),
            },
        )
    assert len(jobs) == 1
    gallery = jobs[0][1].read_text(encoding="utf-8")
    assert gallery.count("<img") == 3
    assert gallery.count("token=secret-token") == 2
    assert "cdn.example/photos/lobby.jpg" in gallery
    assert "cdn.example" in gallery and "token=secret-token" not in gallery.split("cdn.example")[1].split('"')[0]
    assert app_module._ensure_download_access_token(url_a).endswith("?token=secret-token")
    assert app_module._ensure_download_access_token(cdn) == cdn


def test_materialize_and_finalize_queue_gallery_and_sibling_uploads(tmp_path):
    """MetSpace-style embeds: upload jobs must include photo siblings + gallery HTML."""
    photo_a = AssetCandidate(
        "", "brochure.pdf", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
        content=_photo_jpeg(1), content_hash="photoa1234", extension="jpg", width=640, height=400,
    )
    photo_b = AssetCandidate(
        "", "brochure.pdf", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
        content=_photo_jpeg(2), content_hash="photob5678", extension="jpg", width=640, height=400,
    )
    record = {
        "Building": "Market Place",
        "Floor Plan": "",
        "High Res Images": "",
        "_brochure_embedded_assets": [photo_a, photo_b],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        jobs = app_module._materialize_brochure_assets([record], tmp_path, "batch", "MetSpace")
        jobs.extend(
            app_module._finalize_high_res_images(
                [record], tmp_path, "batch", "MetSpace",
                image_validator=lambda url, cache=None: {
                    "ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url,
                },
            )
        )
    keys = [key for key, _path in jobs]
    assert sum(1 for key in keys if "brochure" in key) == 2
    assert any("photos" in key and key.endswith(".html") for key in keys)
    assert "photos" in (record.get("High Res Images") or "")
    assert "data:image/" not in jobs[-1][1].read_text(encoding="utf-8")


def test_process_uploads_jobs_synchronously_before_return(monkeypatch, tmp_path):
    """Gallery/sibling uploads must finish before /api/process returns (MetSpace 404 fix)."""
    upload_calls = []
    thread_targets = []
    response_seen_after_upload = []

    real_jsonify = app_module.jsonify

    def tracking_jsonify(*args, **kwargs):
        response_seen_after_upload.append(list(upload_calls))
        return real_jsonify(*args, **kwargs)

    class _FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            thread_targets.append(target)

        def start(self):
            return None

    gallery = tmp_path / "g.html"
    photo = tmp_path / "p.jpg"
    gallery.write_text("<html><img src='/api/download/b/p.jpg'></html>", encoding="utf-8")
    photo.write_bytes(_photo_jpeg(1))
    jobs = [("batch/g.html", gallery), ("batch/p.jpg", photo)]

    monkeypatch.setattr(app_module.threading, "Thread", _FakeThread)
    monkeypatch.setattr(app_module, "jsonify", tracking_jsonify)
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app_module, "_cleanup_old_batches", lambda: None)
    monkeypatch.setattr(app_module, "_upload_all", lambda items: upload_calls.extend(items))
    monkeypatch.setattr(
        app_module,
        "process_files",
        lambda *_a, **_k: [{
            "filename": "metspace.eml",
            "status": "ok",
            "method": "rule:MetSpace",
            "record_count": 1,
            "error": None,
            "display_name": "MetSpace",
            "records": [{"Building": "A", "High Res Images": ""}],
            "properties": [],
            "email_html": None,
            "pages_text": None,
            "html_items": None,
        }],
    )
    monkeypatch.setattr(app_module, "_finish_ok_result", lambda *_a, **_k: list(jobs))
    monkeypatch.setattr(app_module, "ACCESS_TOKEN", "test-token")

    with app_module.app.test_client() as client:
        resp = client.post(
            "/api/process",
            data={"files": (BytesIO(b"From: t\n\nbody"), "metspace.eml")},
            headers={"X-Access-Token": "test-token"},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200
    assert upload_calls == jobs
    assert response_seen_after_upload and response_seen_after_upload[0] == jobs
    assert app_module._upload_all not in thread_targets
    assert app_module._flush_caches in thread_targets


def test_dense_spreadsheet_triggers_chunking_before_row_threshold():
    from extraction.spreadsheet_chunks import is_large_spreadsheet

    # Under the old 80-row threshold, but verbose enough to need chunking
    # (Workplace Plus London-style denseness).
    rows = [[f"Building {i}", "Floor", "100 desks", "£10,000", "feature " * 80] for i in range(35)]
    assert is_large_spreadsheet({"tables": [rows]})
    assert not is_large_spreadsheet({"tables": [[["Building", "Floor"], ["1 Small Street", "1st"]]]})


def test_next_image_wrapper_normalizes_to_underlying_asset():
    wrapped = (
        "https://knotel.com/_next/image?url="
        "https%3A%2F%2Fknotel.directus.app%2Fassets%2Fabc123&w=1080&q=75"
    )
    assert normalize_url(wrapped) == "https://knotel.directus.app/assets/abc123"
    assert merge_candidate_urls([
        wrapped,
        "https://knotel.com/_next/image?url=https%3A%2F%2Fknotel.directus.app%2Fassets%2Fabc123&w=256&q=75",
        "https://knotel.directus.app/assets/other",
    ]) == [
        "https://knotel.directus.app/assets/abc123",
        "https://knotel.directus.app/assets/other",
    ]


def test_brochure_link_detection_accepts_hidden_labels_and_document_urls():
    from extraction.html_images import is_brochure_link, is_image_like_url

    assert is_brochure_link("CLICK HERE", "https://app.box.com/s/abc")
    assert is_brochure_link("9-10 Market Place", "https://us.list-manage.com/track")
    assert is_brochure_link("", "https://drive.google.com/file/d/abc/view")
    assert not is_brochure_link("unsubscribe", "https://example.com/unsubscribe")
    assert not is_brochure_link(
        "unsubscribe",
        "https://metspace.us13.list-manage.com/unsubscribe?u=1&id=2",
    )
    assert is_image_like_url("https://anything.example/assets/opaque-id")
    assert app_module._accept_image_url_under_deadline(
        "https://anything.example/assets/opaque-id"
    )


def _cream_floorplan_jpeg():
    """MetSpace-style cream plan board: mostly pale with sparse CAD lines."""
    img = Image.new("RGB", (900, 700), (245, 240, 230))
    pixels = img.load()
    for x in range(0, 900, 35):
        for y in range(700):
            pixels[x, y] = (60, 60, 55)
    for y in range(0, 700, 40):
        for x in range(900):
            pixels[x, y] = (55, 55, 50)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_metspace_cream_floorplan_never_enters_high_res(tmp_path):
    """Cream Drive plan boards must land in Floor Plan, never High Res."""
    from extraction.pdf_images import is_floorplan_image

    plan_bytes = _cream_floorplan_jpeg()
    assert is_floorplan_image(plan_bytes)
    assert evaluate_image_bytes(plan_bytes, url="https://cdn.test/plan.jpg")["status"] == "IMAGE_IS_FLOORPLAN"

    photo_bytes = _photo_jpeg(3)
    record = {
        "Building": "9-10 Market Place",
        "Floor Plan": "https://drive.google.com/file/d/viewer/view",
        "_brochure_embedded_assets": [
            AssetCandidate(
                "", "drive.pdf", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
                content=plan_bytes, content_hash="creamplan01", extension="jpg",
                width=900, height=700,
            ),
            AssetCandidate(
                "", "drive.pdf", classification=AssetType.PROPERTY_IMAGE, confidence=0.8,
                content=photo_bytes, content_hash="creamphoto01", extension="jpg",
                width=640, height=400,
            ),
        ],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._materialize_brochure_assets([record], tmp_path, "batch", "MetSpace")
        app_module._finalize_high_res_images(
            [record], tmp_path, "batch", "MetSpace",
            image_validator=lambda url, cache=None, deadline=None: {
                "ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url,
            },
        )
    assert "/api/download/" in (record.get("Floor Plan") or "")
    assert "drive.google.com" not in (record.get("Floor Plan") or "")
    assert record.get("_high_res_image_count") == 1
    hr = record.get("High Res Images") or ""
    assert hr
    assert "creamplan" not in hr
    assert any(item.status == "IMAGE_IS_FLOORPLAN" for item in record["_link_diagnostics"])


def test_gpe_nested_download_brochure_fills_multi_image_gallery():
    """Property-page chrome must not block following Download brochure PDF."""
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        if url.endswith(".pdf"):
            return BrochureResource(b"%PDF-1.4 gpe", "application/pdf", url, url)
        return BrochureResource(b"<html>GPE listing</html>", "text/html", url, url)

    def extract(payload, content_type, final_url):
        if payload.startswith(b"%PDF"):
            photos = [
                AssetCandidate(
                    f"https://cdn.gpe.test/office-{i}.jpg",
                    final_url,
                    alt_text=f"Office {i}",
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                    association_confidence=0.9,
                )
                for i in range(1, 6)
            ]
            return BrochureExtraction(
                final_url,
                assets=photos,
                identity_text="Elsley House, 24-30 Great Titchfield Street, W1W 8BF",
            )
        # Five non-image chrome URLs that previously inflated page_photos and
        # skipped the nested PDF — must not count as a confident gallery.
        chrome = [
            AssetCandidate(
                f"https://www.gpe.co.uk/static/hero-token-{i}",
                final_url,
                alt_text="Hero",
                association_confidence=0.85,
                classification=AssetType.PROPERTY_IMAGE,
                confidence=0.85,
            )
            for i in range(5)
        ]
        return BrochureExtraction(
            final_url,
            assets=chrome + [
                AssetCandidate(
                    "https://www.gpe.co.uk/downloads/elsley-brochure.pdf",
                    final_url,
                    mime_type="application/pdf",
                    classification=AssetType.BROCHURE,
                    confidence=0.9,
                    anchor_text="Download brochure",
                )
            ],
            identity_text="Elsley House, 24-30 Great Titchfield Street, W1W 8BF",
        )

    prop = Property.from_record(
        normalize_record({
            "Building": "Elsley House",
            "Property Postcode": "W1W 8BF",
            "High Res Images": "https://cdn.gpe.test/email-hero.jpg",
            "_high_res_candidates": ["https://cdn.gpe.test/email-hero.jpg"],
            "Brochure PDF": "https://www.gpe.co.uk/properties/elsley-house",
        }),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    enrich_properties([prop], fetcher=fetch, extractor=extract)
    assert any(url.endswith(".pdf") for url in fetched)
    candidates = prop.values.get("_high_res_candidates") or []
    assert len(candidates) >= 5
    assert any("office-1.jpg" in url for url in candidates)


def test_share_finalized_media_copies_gallery_and_floorplan_to_sibling():
    donor = {
        "Building": "Shared Tower, 1 Example Street",
        "Floor/Unit": "1st",
        "High Res Images": "https://service.test/api/download/batch/Shared_photos1.html",
        "_high_res_image_count": 6,
        "Floor Plan": "https://service.test/api/download/batch/plan.jpg",
        "_link_diagnostics": [],
    }
    sibling = {
        "Building": "Shared Tower, 1 Example Street",
        "Floor/Unit": "5th",
        "High Res Images": "",
        "_high_res_image_count": 0,
        "Floor Plan": "https://app.box.com/s/vieweronly",
        "_link_diagnostics": [],
    }
    app_module._share_finalized_media_across_buildings([donor, sibling])
    assert sibling["High Res Images"] == donor["High Res Images"]
    assert sibling["_high_res_image_count"] == 6
    assert sibling["Floor Plan"] == donor["Floor Plan"]
    assert any(
        item.status == "IMAGES_SHARED_FROM_SIBLING_FLOOR"
        for item in sibling["_link_diagnostics"]
    )


def test_finalize_soft_accept_under_deadline_rejects_floorplan_named_url(tmp_path):
    record = {
        "Building": "Plan Leak House",
        "_source_high_res_candidates": [
            "https://cdn.test/assets/real-office.jpg",
            "https://cdn.test/media/third-floorplan-diagram.jpg",
        ],
        "_high_res_candidates": [
            "https://cdn.test/assets/real-office.jpg",
            "https://cdn.test/media/third-floorplan-diagram.jpg",
        ],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda url, cache=None, deadline=None: {
                "ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url,
            },
            deadline=time.monotonic() - 1,
        )
    assert "floorplan" not in (record.get("High Res Images") or "").lower()
    assert record.get("_high_res_image_count") == 1
    assert any(item.status == "IMAGE_IS_FLOORPLAN" for item in record["_link_diagnostics"])


def test_enrichment_does_not_seed_high_res_with_box_document_when_budget_skips():
    """Floor Plan / Brochure seeds must not copy document URLs into High Res.

    Provider-neutral: uses synthetic Box share URLs (any spreadsheet with
    hosted document links), not a UNION-specific rule path.
    """
    props = []
    for i in range(15):
        box = f"https://app.box.com/s/share{i:04d}abcd"
        props.append(
            Property.from_record(
                normalize_record({
                    "Building": f"Example House {i}, 1 Example Street",
                    "Property Postcode": "EC1A 1AA",
                    "Floor/Unit": "1st",
                    "Brochure PDF": box,
                    "Floor Plan": box,
                }),
                "availability.xlsx",
                "Example Landlord",
                "llm",
            )
        )

    enrich_properties(props, fetcher=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")), extractor=lambda *a, **k: None, deadline=time.monotonic() - 1)
    for prop in props:
        assert not (prop.values.get("High Res Images") or ""), "High Res must stay blank when enrichment is skipped"
        assert "box.com" in (prop.values.get("Brochure PDF") or "")
        # Parse-time Floor Plan seed may still be present until finalize clears it.
        assert prop.values.get("Floor Plan")


def test_finalize_clears_brochure_high_res_and_floor_plan_document_urls(tmp_path):
    seed = "https://app.box.com/shared/static/examplebrochure.pdf"
    record = {
        "Building": "Example House",
        "Brochure PDF": "https://app.box.com/s/examplebrochure",
        "Floor Plan": "https://app.box.com/s/examplebrochure",
        "High Res Images": seed,
        "_high_res_candidates": [seed],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda *_a, **_k: {"ok": False, "status": "NOT_AN_IMAGE"},
        )
    assert not record.get("High Res Images")
    assert not record.get("Floor Plan")
    assert record["Brochure PDF"] == "https://app.box.com/s/examplebrochure"
    statuses = {item.status for item in record["_link_diagnostics"]}
    assert "HIGH_RES_DOCUMENT_PLACEHOLDER_CLEARED" in statuses or "NO_IMAGES_DISCOVERED" in statuses
    assert "FLOOR_PLAN_DOCUMENT_PLACEHOLDER_CLEARED" in statuses


def test_finalize_keeps_floorplan_pdf_fallback_when_no_bitmap(tmp_path):
    """Dedicated website floorplan.pdf survives finalize when no plan image exists."""
    plan_pdf = "https://cdn.property.test/media/level-2_2d-floorplans_a4.pdf"
    photo = "https://cdn.test/office.jpg"
    record = {
        "Building": "Example House",
        "Brochure PDF": "https://cdn.property.test/brochure.pdf",
        "Floor Plan": plan_pdf,
        "High Res Images": "",
        "_high_res_candidates": [photo],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert record["Floor Plan"] == plan_pdf
    assert record["High Res Images"] == photo
    # High Res must still never keep a brochure PDF seed.
    assert not app_module._is_brochure_media_seed_url(record["High Res Images"])


def test_finalize_replaces_brochure_seed_with_real_photo(tmp_path):
    seed = "https://app.box.com/shared/static/examplebrochure.pdf"
    photo = "https://cdn.test/office.jpg"
    record = {
        "Building": "Example House",
        "Brochure PDF": "https://app.box.com/s/examplebrochure",
        "Floor Plan": "https://app.box.com/s/examplebrochure",
        "High Res Images": seed,
        "_high_res_candidates": [seed, photo],
        "_source_high_res_candidates": [photo],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert record["High Res Images"] == photo
    assert "box.com" not in (record.get("High Res Images") or "")
    # Floor Plan document placeholder cleared when no real plan image exists.
    assert not record.get("Floor Plan")
    assert record["Brochure PDF"] == "https://app.box.com/s/examplebrochure"


def test_finalize_keeps_real_floor_plan_image_and_gallery(tmp_path):
    photo = "https://cdn.test/office.jpg"
    plan = "https://service.test/api/download/batch/plan.png"
    record = {
        "Building": "Example House",
        "Brochure PDF": "https://app.box.com/s/examplebrochure",
        "Floor Plan": plan,
        "High Res Images": "",
        "_high_res_candidates": [photo],
    }
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda url, cache=None: {"ok": True, "url": url, "status": "VALID_IMAGE", "content_hash": url},
        )
    assert record["High Res Images"] == photo
    assert record["Floor Plan"] == plan
    assert record["Brochure PDF"] == "https://app.box.com/s/examplebrochure"


def test_usable_high_res_seed_rewrites_box_and_drive():
    from extraction.brochure import _usable_high_res_seed_url, _is_brochure_media_seed_url

    box = "https://app.box.com/s/abc123share"
    assert _usable_high_res_seed_url(box) == "https://app.box.com/shared/static/abc123share.pdf"
    assert _is_brochure_media_seed_url(_usable_high_res_seed_url(box))
    drive = "https://drive.google.com/file/d/FILEID123/view"
    assert "drive.usercontent.google.com" in _usable_high_res_seed_url(drive)
    assert _is_brochure_media_seed_url("https://cdn.test/office.jpg") is False
    assert _is_brochure_media_seed_url("https://property.example.test/offices/example-house") is True
    assert _is_brochure_media_seed_url("https://cms.example.test/assets/uuid-aaaa-bbbb") is False
