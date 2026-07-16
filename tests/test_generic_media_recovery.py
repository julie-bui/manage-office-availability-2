"""Generic (provider-agnostic) media recovery regressions.

Synthetic fixtures only — do not depend on UNION/Knotel brand names so
new hosts/layouts stay covered by shared heuristics.
"""
import time

import app as app_module
from extraction.assets import classify_candidate
from extraction.brochure import _hosted_document_candidates
from extraction.html_images import (
    enrich_records,
    is_brochure_link,
    is_enrichment_seed_link,
    is_image_like_url,
    is_real_content_image,
)
from extraction.models import AssetCandidate, AssetType
from extraction.xlsx_links import associate_row_links, enrich_records as enrich_xlsx


def test_xlsx_floor_plan_label_dual_fills_brochure_without_provider_rule():
    """Hidden 'FLOOR PLAN' hyperlinks seed Brochure PDF via shared helper."""
    floorplan, brochure = associate_row_links(
        [("FLOOR PLAN", "https://files.example.test/s/landlord-share")]
    )
    assert floorplan == "https://files.example.test/s/landlord-share"
    assert brochure == "https://files.example.test/s/landlord-share"

    click_floor, click_brochure = associate_row_links(
        [("CLICK HERE", "https://files.example.test/s/click-share")]
    )
    assert click_floor is None
    assert click_brochure == "https://files.example.test/s/click-share"

    records = [
        {"Building": "100 Example Street", "Floor Plan": "", "Brochure PDF": ""},
    ]
    enrich_xlsx(
        records,
        [
            {
                "row_text": "100 Example Street | 3rd | FLOOR PLAN",
                "links": [("FLOOR PLAN", "https://files.example.test/s/row-share")],
            }
        ],
    )
    assert records[0]["Floor Plan"] == "https://files.example.test/s/row-share"
    assert records[0]["Brochure PDF"] == "https://files.example.test/s/row-share"


def test_html_sparse_photo_seeds_brochure_from_listing_cta():
    """One featured photo + View listing CTA → Brochure PDF enrichment seed."""
    html_items = [
        ("image", "Logo", "https://cdn.example.test/brand/logo.png"),
        ("image", "", "https://cdn.example.test/listing/hero.jpg"),
        ("link", "View listing", "https://property.example.test/offices/example-house"),
        ("link", "unsubscribe", "https://mail.example.test/unsubscribe"),
    ]
    records = [{"Building": "Example House", "Brochure PDF": "", "Floor Plan": ""}]
    original = __import__("extraction.html_images", fromlist=["is_floorplan_image_url"]).is_floorplan_image_url
    import extraction.html_images as html_images

    html_images.is_floorplan_image_url = lambda url: False
    try:
        enrich_records(records, html_items)
    finally:
        html_images.is_floorplan_image_url = original

    assert records[0]["_high_res_candidates"] == ["https://cdn.example.test/listing/hero.jpg"]
    assert records[0]["Brochure PDF"] == "https://property.example.test/offices/example-house"


def test_html_building_name_anchor_seeds_brochure_when_label_is_not_brochure():
    html_items = [
        ("image", "", "https://cdn.example.test/photos/one.jpg"),
        ("link", "12 Market Place", "https://drive.example.test/viewer/abc"),
    ]
    records = [{"Building": "12 Market Place"}]
    import extraction.html_images as html_images

    original = html_images.is_floorplan_image_url
    html_images.is_floorplan_image_url = lambda url: False
    try:
        enrich_records(records, html_items)
    finally:
        html_images.is_floorplan_image_url = original
    assert records[0]["Brochure PDF"] == "https://drive.example.test/viewer/abc"


def test_html_floor_plan_document_link_dual_fills_brochure_not_image_url():
    html_items = [
        ("image", "", "https://cdn.example.test/photos/reception.jpg"),
        ("link", "FLOOR PLAN", "https://app.box.com/s/abc123share"),
    ]
    records = [{"Building": "Example Tower"}]
    import extraction.html_images as html_images

    original = html_images.is_floorplan_image_url
    html_images.is_floorplan_image_url = lambda url: False
    try:
        enrich_records(records, html_items)
    finally:
        html_images.is_floorplan_image_url = original
    assert records[0]["Floor Plan"] == "https://app.box.com/s/abc123share"
    assert records[0]["Brochure PDF"] == "https://app.box.com/s/abc123share"


def test_image_like_urls_without_extension_and_unknown_cdn_hosts():
    assert is_image_like_url("https://cms.unknown-cdn.test/assets/uuid-aaaa-bbbb")
    assert is_image_like_url("https://cdn.example.test/digitalassets/images/token")
    assert is_image_like_url("https://cdn.example.test/photo.jpg")
    assert not is_image_like_url("https://tracker.example.test/q/opaque-token")
    assert is_real_content_image("", "https://cms.unknown-cdn.test/assets/uuid-aaaa-bbbb")
    assert not is_real_content_image("Company logo", "https://cms.unknown-cdn.test/assets/logo-uuid")

    classified = classify_candidate(
        AssetCandidate("https://cms.unknown-cdn.test/assets/uuid-aaaa-bbbb", "html", alt_text="Reception")
    )
    assert classified.classification == AssetType.PROPERTY_IMAGE


def test_finalize_accepts_unknown_assets_host_under_deadline(tmp_path):
    url = "https://cms.brand-new-host.test/assets/photo-token-xyz"
    record = {
        "Building": "Example House",
        "_source_high_res_candidates": [url],
        "_high_res_candidates": [url, "https://cms.brand-new-host.test/assets/photo-token-2"],
        "High Res Images": url,
    }
    past = time.monotonic() - 1
    with app_module.app.test_request_context("/process", base_url="https://service.test"):
        app_module._finalize_high_res_images(
            [record],
            tmp_path,
            "batch",
            "Example",
            image_validator=lambda *_a, **_k: {"ok": False, "status": "SHOULD_NOT_RUN"},
            deadline=past,
        )
    # Under deadline, keep up to the High Res minimum of unhashed soft-accepts
    # so multi-photo listings are not collapsed to a single featured image.
    assert record["_high_res_image_count"] == 2
    assert ".html" in (record.get("High Res Images") or "") or record.get("High Res Images") == url


def test_enrichment_seed_and_brochure_helpers_are_label_agnostic():
    assert is_brochure_link("CLICK HERE", "https://app.box.com/s/abc")
    assert is_brochure_link("View property", "https://listing.example.test/office")
    assert is_enrichment_seed_link("12 Market Place", "https://site.example.test/listing", building="12 Market Place")
    assert not is_enrichment_seed_link("unsubscribe", "https://mail.example.test/unsubscribe", building="12 Market Place")


def test_dropbox_hosted_document_gets_direct_download_candidate():
    url = "https://www.dropbox.com/s/abcdefghijklmnop/brochure.pdf?dl=0"
    candidates = _hosted_document_candidates(None, url)
    assert candidates
    assert "dl=1" in candidates[0].url
