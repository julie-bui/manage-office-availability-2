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

    click_records = [
        {"Building": "200 Example Street", "Floor Plan": "", "Brochure PDF": ""},
    ]
    enrich_xlsx(
        click_records,
        [
            {
                "row_text": "200 Example Street | 2nd | CLICK HERE",
                "links": [("CLICK HERE", "https://files.example.test/s/click-only")],
            }
        ],
    )
    # Brochure-only rows seed Floor Plan with the same replaceable viewer URL.
    assert click_records[0]["Brochure PDF"] == "https://files.example.test/s/click-only"
    assert click_records[0]["Floor Plan"] == "https://files.example.test/s/click-only"


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


def test_budget_skip_leaves_high_res_blank_not_document_seed():
    """Under enrichment deadline, document PDFs stay on Brochure PDF only.

    Drive/Box/.pdf must not be copied into High Res as a click-through
    fallback — blank High Res beats a brochure document URL in that column.
    """
    from extraction.brochure import enrich_properties
    from extraction.models import Property
    from extraction.schema import normalize_record

    drive = "https://drive.google.com/file/d/FILEIDDRIVE99/view"
    page = "https://property.example.test/offices/example-house"
    props = [
        Property.from_record(
            normalize_record({
                "Building": "Drive House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": drive,
                "Floor Plan": drive,
            }),
            "sheet.xlsx",
            "Example Provider",
            "llm",
        ),
        Property.from_record(
            normalize_record({
                "Building": "Page House, 2 Example Street",
                "Property Postcode": "EC1A 1BB",
                "Brochure PDF": page,
            }),
            "mail.eml",
            "Example Provider",
            "llm",
        ),
    ]
    enrich_properties(
        props,
        fetcher=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")),
        extractor=lambda *a, **k: None,
        deadline=time.monotonic() - 1,
    )
    assert "drive.google.com" in (props[0].values.get("Brochure PDF") or "")
    assert not props[0].values.get("High Res Images")
    assert props[1].values.get("Brochure PDF") == page
    assert not props[1].values.get("High Res Images")


def test_finalize_keeps_featured_photo_and_property_page_fallback(tmp_path):
    """Last-row style listing: featured photo survives deadline; page is seed."""
    featured = "https://cms.example.test/assets/featured-token-xyz"
    page = "https://property.example.test/buildings/example-tower"
    record = {
        "Building": "Example Tower",
        "Brochure PDF": page,
        "High Res Images": featured,
        "_source_high_res_candidates": [featured],
        "_high_res_candidates": [featured, page],
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
    assert record.get("High Res Images") == featured
    assert int(record.get("_high_res_image_count") or 0) >= 1


def test_finalize_blanks_high_res_when_only_document_urls_remain(tmp_path):
    """When featured photo fails validation, do not fall back to Brochure PDF page."""
    featured = "https://cdn.example.test/photo.jpg"
    page = "https://property.example.test/buildings/example-tower"
    record = {
        "Building": "Example Tower",
        "Brochure PDF": page,
        "High Res Images": featured,
        "_high_res_candidates": [featured, page],
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
    assert record.get("Brochure PDF") == page


def test_brochure_media_seed_helpers_are_provider_neutral():
    from extraction.brochure import _is_brochure_media_seed_url as brochure_seed
    from extraction.brochure import _usable_high_res_seed_url

    assert brochure_seed("https://app.box.com/shared/static/x.pdf")
    assert brochure_seed("https://drive.google.com/file/d/ABC/view")
    assert brochure_seed("https://www.dropbox.com/s/abc/file.pdf?dl=0")
    assert brochure_seed("https://files.example.test/packs/brochure.pdf")
    assert brochure_seed("https://listing.example.test/property/12-market")
    assert not brochure_seed("https://cdn.example.test/office.jpg")
    assert not brochure_seed("https://cms.example.test/assets/uuid-photo")
    assert "shared/static" in _usable_high_res_seed_url("https://app.box.com/s/abcshareid")
    assert app_module._is_brochure_media_seed_url("https://listing.example.test/property/12-market")
    assert not app_module._is_brochure_media_seed_url("https://cms.example.test/assets/uuid-photo")
