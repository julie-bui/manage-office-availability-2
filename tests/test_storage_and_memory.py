"""S3 env credential hygiene and enrichment memory-guard helpers."""
import os

import pytest

import storage
from extraction import brochure
from extraction.assets import classify_candidate
from extraction.models import (
    AssetCandidate,
    AssetType,
    BrochureExtraction,
    BrochureResource,
    Property,
)
from extraction.schema import normalize_record


def test_s3_env_vars_strip_whitespace_and_newlines(monkeypatch):
    """Leading newlines in Railway env paste break SigV4 Authorization headers."""
    monkeypatch.setenv("S3_BUCKET", "  my-bucket\n")
    monkeypatch.setenv("S3_ENDPOINT_URL", "\nhttps://example.r2.cloudflarestorage.com\n")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "\n00561058d3cb9a10000000002")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", " secret-with-spaces\n")
    monkeypatch.setenv("S3_REGION", " auto\n")
    storage.reload_from_env()
    assert storage._BUCKET == "my-bucket"
    assert storage._ENDPOINT_URL == "https://example.r2.cloudflarestorage.com"
    assert storage._ACCESS_KEY == "00561058d3cb9a10000000002"
    assert storage._SECRET_KEY == "secret-with-spaces"
    assert storage._REGION == "auto"
    assert storage.enabled()
    for key in (
        "S3_BUCKET",
        "S3_ENDPOINT_URL",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_REGION",
    ):
        monkeypatch.delenv(key, raising=False)
    storage.reload_from_env()
    assert not storage.enabled()


def test_storage_env_helper_strips():
    assert storage._env("MISSING_S3_VAR_XYZ", "default") == "default"
    os.environ["MISSING_S3_VAR_XYZ"] = "\n  value \n"
    try:
        assert storage._env("MISSING_S3_VAR_XYZ") == "value"
    finally:
        del os.environ["MISSING_S3_VAR_XYZ"]


@pytest.mark.parametrize(
    "ram_mb,expected_workers",
    [
        (0, 4),
        (512, 2),
        (1024, 2),
        (2048, 3),
        (4096, 4),
        (8192, 6),
    ],
)
def test_enrichment_high_memory_workers_scale_with_ram(ram_mb, expected_workers):
    assert brochure._default_fetch_workers(ram_mb, high_memory=True) == expected_workers


@pytest.mark.parametrize(
    "ram_mb,expected_workers",
    [
        (0, 1),
        (512, 1),
        (1024, 1),
        (2048, 2),
        (4096, 4),
    ],
)
def test_enrichment_safe_path_workers_remain_serial_on_1gb(ram_mb, expected_workers):
    assert brochure._default_fetch_workers(ram_mb, high_memory=False) == expected_workers


@pytest.mark.parametrize(
    "ram_mb,expected",
    [
        (0, True),
        (512, False),
        (1024, False),
        (1200, False),
        (2047, False),
        (2048, True),
        (4096, True),
    ],
)
def test_resolve_high_memory_auto_from_host_ram(ram_mb, expected, monkeypatch):
    monkeypatch.delenv("ENRICHMENT_HIGH_MEMORY", raising=False)
    assert brochure._resolve_high_memory(ram_mb) is expected


def test_resolve_high_memory_env_override(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_HIGH_MEMORY", "1")
    assert brochure._resolve_high_memory(1024.0) is True
    monkeypatch.setenv("ENRICHMENT_HIGH_MEMORY", "0")
    assert brochure._resolve_high_memory(4096.0) is False


@pytest.mark.parametrize(
    "ram_mb",
    [0, 512, 1024, 2048, 4096],
)
def test_enrichment_rss_ceiling_stays_below_host_ram(ram_mb):
    ceiling = brochure._default_rss_ceiling_mb(ram_mb, high_memory=True)
    parallel = brochure._default_parallel_rss_mb(ram_mb, high_memory=True)
    if ram_mb > 0:
        assert ceiling <= ram_mb
        assert parallel < ceiling
        if ram_mb >= 2048:
            assert ceiling >= ram_mb - 120
            assert parallel >= 1000
    else:
        assert ceiling >= 3000
        assert parallel < ceiling
        assert parallel >= 2000


def test_enrichment_memory_constrained_dual_path(monkeypatch):
    """≥2GB: only near parallel threshold. ≤1.2GB safe path: always constrained."""
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    assert not brochure._enrichment_memory_constrained(100.0, hosted=True)
    assert not brochure._enrichment_memory_constrained(180.0, hosted=True)
    assert brochure._enrichment_memory_constrained(2800.0, hosted=True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", False)
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    assert brochure._enrichment_memory_constrained(40.0, hosted=False)
    assert brochure._skip_heavy_pdf_embeds() is True


def test_enrichment_skips_remaining_urls_when_rss_near_ceiling(monkeypatch):
    """Approaching OOM must skip remaining brochure URLs rather than SIGKILL."""
    calls = {"n": 0}

    def fake_rss():
        calls["n"] += 1
        if calls["n"] <= 2:
            return 50.0
        return brochure._RSS_ENRICHMENT_CEILING_MB + 80

    monkeypatch.setattr(brochure, "_rss_mb", fake_rss)
    monkeypatch.setattr(brochure, "_enrichment_memory_constrained", lambda *a, **k: False)
    monkeypatch.setattr(brochure, "_ENRICHMENT_FETCH_WORKERS", 1)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_skip_heavy_pdf_embeds", lambda: False)

    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        return BrochureResource(b"%PDF-1.4 x", "application/pdf", url, url)

    def extract(payload, content_type, final_url, **kwargs):
        return BrochureExtraction(final_url, identity_text="Building EC1A 1AA")

    props = []
    for i in range(3):
        props.append(
            Property.from_record(
                normalize_record(
                    {
                        "Building": f"Building {i}",
                        "Property Postcode": "EC1A 1AA",
                        "Brochure PDF": f"https://app.box.com/shared/static/union{i}.pdf",
                    }
                ),
                "union.xlsx",
                "UNION",
                "rule:UNION",
            )
        )

    brochure.enrich_properties(props, fetcher=fetch, extractor=extract)
    assert len(fetched) == 1
    skipped = [
        d
        for prop in props
        for d in prop.link_diagnostics
        if d.status == "LINK_ENRICHMENT_SKIPPED" and "RSS" in (d.detail or "")
    ]
    assert skipped
    assert any("protect worker memory" in (i.message or "") for prop in props for i in prop.issues)


def test_small_host_skips_box_pdf_before_fetch(monkeypatch):
    """Railway 1GB plan: never download Union Box PDFs — soft-skip, keep link."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", False)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 180.0)
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        return BrochureResource(b"%PDF-1.4 brochure", "application/pdf", url, url)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://app.box.com/shared/static/abc123.pdf",
            }
        ),
        "union.xlsx",
        "UNION",
        "rule:UNION",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert fetched == []
    assert prop.values.get("Brochure PDF") == "https://app.box.com/shared/static/abc123.pdf"
    skipped = [
        d
        for d in prop.link_diagnostics
        if d.status == "LINK_ENRICHMENT_SKIPPED" and "small host" in (d.detail or "").lower()
    ]
    assert skipped
    assert any("≤1.2GB" in (i.message or "") for i in prop.issues)


def test_high_memory_host_fetches_box_pdf_full_extract(monkeypatch):
    """≥2GB hosts run full Box PDF embed extraction (no light kwargs at ~100 MiB)."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 180.0)
    seen = []

    def fetch(url, deadline=None):
        return BrochureResource(b"%PDF-1.4 brochure", "application/pdf", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        seen.append(dict(kwargs))
        return BrochureExtraction(
            source_document,
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://app.box.com/shared/static/abc123.pdf",
            }
        ),
        "union.xlsx",
        "UNION",
        "rule:UNION",
    )
    brochure.enrich_properties(
        [prop],
        fetcher=fetch,
        extractor=brochure.extract_brochure,
    )
    assert seen
    assert seen[0].get("max_pages") is None
    assert seen[0].get("max_photos") is None


def test_small_host_keeps_html_gallery_skips_nested_pdf(monkeypatch):
    """GPE-style HTML still enriches on 1GB; nested landlord PDFs soft-skip."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", False)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 100.0)
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        if url.endswith(".pdf"):
            return BrochureResource(b"%PDF-1.4 plan", "application/pdf", url, url)
        return BrochureResource(b"<html>listing</html>", "text/html", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        if payload.startswith(b"%PDF"):
            raise AssertionError("nested PDF must not be fetched/decoded on ≤1.2GB")
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"https://cdn.property.test/media/{name}.jpg",
                    source_document,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            source_document,
            assets=photos
            + [
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/brochure.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.BROCHURE,
                        confidence=0.9,
                        anchor_text="Download brochure",
                    )
                ),
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/floorplan.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        alt_text="Floor plan",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://property.test/listing",
            }
        ),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert fetched == ["https://property.test/listing"]
    assert len(prop.values.get("_high_res_candidates") or []) >= 5
    # Soft-skip nested PDFs on 1GB, but still assign the HTML floorplan.pdf link.
    assert prop.values.get("Floor Plan") == "https://cdn.property.test/floorplan.pdf"


def test_small_host_follows_floorplan_modal_for_plan_image(monkeypatch):
    """GPE regression: Umbraco floorplan modals resolve on 1GB soft-skip.

    Portfolio pages expose plans via data-modal-path=/umbraco/surface/floorplan?…
    which returns a tiny HTML fragment with *floorplan*.jpg / *.pdf — not a
    static floorplans.pdf href. Soft-skip must still follow that modal.
    Shared brochure path — not a GPE rule gate.
    """
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", False)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 100.0)

    listing = "https://property.test/portfolio/example-house"
    modal = "https://property.test/umbraco/surface/floorplan?propertyid=1&key=abc"
    plan_jpg = "https://cdn.property.test/media/example-house_2d-floorplan_l6.jpg"
    plan_pdf = "https://cdn.property.test/media/example-house_2d-floorplan_l6.pdf"
    brochure_pdf = "https://cdn.property.test/media/example-house-brochure.pdf"
    fetched = []

    listing_html = f"""
    <html><body><main>
      <img src="https://cdn.property.test/media/a.jpg" alt="office" />
      <img src="https://cdn.property.test/media/b.jpg" alt="office" />
      <img src="https://cdn.property.test/media/c.jpg" alt="office" />
      <img src="https://cdn.property.test/media/d.jpg" alt="office" />
      <img src="https://cdn.property.test/media/e.jpg" alt="office" />
      <a href="{brochure_pdf}">Download brochure</a>
      <a href="#fpModal" data-modal-target="floorplan-modal"
         data-modal-path="/umbraco/surface/floorplan?propertyid=1&amp;key=abc"></a>
    </main></body></html>
    """
    modal_html = f"""
    <div class="modal modal--floorplan" id="floorplan-modal">
      <a href="{plan_pdf}" class="floorplan-download">Download</a>
      <img class="floorplan-modal__image" src="{plan_jpg}"
           alt="Example House 2D Floorplan L6" />
    </div>
    """

    def fetch(url, deadline=None):
        fetched.append(url)
        if "umbraco/surface/floorplan" in url:
            return BrochureResource(modal_html.encode(), "text/html", modal, url)
        if url.endswith(".pdf"):
            raise AssertionError(f"nested PDF must not be fetched on ≤1.2GB: {url}")
        return BrochureResource(listing_html.encode(), "text/html", listing, url)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": listing,
            }
        ),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert listing in fetched
    assert any("umbraco/surface/floorplan" in u for u in fetched)
    assert not any(u.endswith(".pdf") for u in fetched)
    assert prop.values.get("Floor Plan") == plan_jpg
    assert prop.values.get("Brochure PDF") == brochure_pdf


def test_small_host_light_floorplan_discovery_any_provider(monkeypatch):
    """Shared soft-skip path: light floor-plan HTML works for unknown providers.

    LLM-fallback / no dedicated rule — only a Brochure PDF HTML seed. Uses
    hyphenated /floor-plan/ modal path + data-href (not GPE data-modal-path).
    """
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", False)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 100.0)

    listing = "https://landlord.example/properties/north-wing"
    modal = "https://landlord.example/floor-plan/modal?id=42"
    plan_png = "https://cdn.landlord.example/assets/north-wing-floor-plan.png"
    brochure_pdf = "https://cdn.landlord.example/assets/north-wing-brochure.pdf"
    fetched = []

    listing_html = f"""
    <html><body><main>
      <img src="https://cdn.landlord.example/assets/a.jpg" alt="office" />
      <img src="https://cdn.landlord.example/assets/b.jpg" alt="office" />
      <img src="https://cdn.landlord.example/assets/c.jpg" alt="office" />
      <img src="https://cdn.landlord.example/assets/d.jpg" alt="office" />
      <img src="https://cdn.landlord.example/assets/e.jpg" alt="office" />
      <a href="{brochure_pdf}">Download brochure</a>
      <button type="button" class="open-floorplan"
              data-href="/floor-plan/modal?id=42">View plan</button>
    </main></body></html>
    """
    modal_html = f"""
    <section class="plan-viewer">
      <img src="{plan_png}" alt="North Wing floor plan" />
    </section>
    """

    def fetch(url, deadline=None):
        fetched.append(url)
        if "floor-plan/modal" in url:
            return BrochureResource(modal_html.encode(), "text/html", modal, url)
        if url.endswith(".pdf"):
            raise AssertionError(f"nested PDF must not be fetched on ≤1.2GB: {url}")
        return BrochureResource(listing_html.encode(), "text/html", listing, url)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "North Wing, 9 Example Road",
                "Property Postcode": "E1 6AN",
                "Brochure PDF": listing,
            }
        ),
        "unknown-broker.eml",
        "Unknown Broker",
        "llm:fallback",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert listing in fetched
    assert any("floor-plan/modal" in u for u in fetched)
    assert not any(u.endswith(".pdf") for u in fetched)
    assert prop.values.get("Floor Plan") == plan_png
    assert prop.values.get("Brochure PDF") == brochure_pdf
    # High Res stays on gallery images — never the brochure PDF.
    high_res = prop.values.get("_high_res_candidates") or []
    assert len(high_res) >= 5
    assert all(not u.endswith(".pdf") for u in high_res)


def test_small_host_direct_floorplan_image_on_html(monkeypatch):
    """Soft-skip: hosted *floorplan*.jpg on the page sets Floor Plan without PDF fetch."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", False)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 100.0)

    listing = "https://listings.example/space/42"
    plan_jpg = "https://cdn.listings.example/media/level-3_floorplan.jpg"
    brochure_pdf = "https://cdn.listings.example/docs/space-42-brochure.pdf"
    fetched = []

    listing_html = f"""
    <html><body><main>
      <img src="https://cdn.listings.example/media/a.jpg" alt="office" />
      <img src="https://cdn.listings.example/media/b.jpg" alt="office" />
      <img src="https://cdn.listings.example/media/c.jpg" alt="office" />
      <img src="https://cdn.listings.example/media/d.jpg" alt="office" />
      <img src="https://cdn.listings.example/media/e.jpg" alt="office" />
      <img src="{plan_jpg}" alt="Level 3 floor plan" />
      <a href="{brochure_pdf}">Download brochure</a>
      <a href="https://cdn.listings.example/docs/level-3_floorplans.pdf">Floorplans PDF</a>
    </main></body></html>
    """

    def fetch(url, deadline=None):
        fetched.append(url)
        if url.endswith(".pdf"):
            raise AssertionError(f"nested PDF must not be fetched on ≤1.2GB: {url}")
        return BrochureResource(listing_html.encode(), "text/html", listing, url)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Space 42, Example Quay",
                "Property Postcode": "SE1 2AA",
                "Brochure PDF": listing,
            }
        ),
        "grid-availability.xlsx",
        "Grid",
        "llm:fallback",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert fetched == [listing]
    assert prop.values.get("Floor Plan") == plan_jpg
    assert prop.values.get("Brochure PDF") == brochure_pdf


def test_floorplan_pdf_fallback_when_no_bitmap(monkeypatch):
    """When no plan bitmap is extracted, Floor Plan keeps the website floorplan.pdf."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 0.0)

    plan_pdf = "https://cdn.property.test/media/level-2_2d-floorplans_a4.pdf"

    def fetch(url, deadline=None):
        if url.endswith(".pdf"):
            return BrochureResource(b"%PDF-1.4 empty", "application/pdf", url, url)
        return BrochureResource(b"<html>listing</html>", "text/html", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        if payload.startswith(b"%PDF"):
            # Nested extract runs but yields no bitmap plan.
            return BrochureExtraction(source_document, identity_text="Example House, EC1A 1AA")
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"https://cdn.property.test/media/{name}.jpg",
                    source_document,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            source_document,
            assets=photos
            + [
                classify_candidate(
                    AssetCandidate(
                        plan_pdf,
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        alt_text="Floor plan",
                    )
                ),
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/brochure.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.BROCHURE,
                        confidence=0.9,
                        anchor_text="Download brochure",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)
    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://property.test/listing",
            }
        ),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert prop.values.get("Floor Plan") == plan_pdf
    # PDF seed must not count as finished — nested brochure still hunted.
    assert not brochure._floor_plan_already_seeded(prop)


def test_floorplan_bitmap_url_wins_over_pdf(monkeypatch):
    """Hosted plan image replaces / beats a floorplan.pdf seed when both exist."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 0.0)

    plan_pdf = "https://cdn.property.test/media/level-2_2d-floorplans_a4.pdf"
    plan_png = "https://cdn.property.test/media/level-2-floorplan.png"

    def fetch(url, deadline=None):
        return BrochureResource(b"<html>listing</html>", "text/html", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        return BrochureExtraction(
            source_document,
            assets=[
                classify_candidate(
                    AssetCandidate(
                        plan_pdf,
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        alt_text="Floor plan PDF",
                    )
                ),
                classify_candidate(
                    AssetCandidate(
                        plan_png,
                        source_document,
                        mime_type="image/png",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.95,
                        alt_text="Floor plan",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)
    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://property.test/listing",
                "Floor Plan": plan_pdf,
            }
        ),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert prop.values.get("Floor Plan") == plan_png


def test_hosted_box_pdf_light_extract_prefers_photos_when_real_plan_seeded(monkeypatch):
    """On ≥2GB with deadline/light pressure, usable Floor Plan keeps photo-focused light window."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    # Force light via memory-constrained at parallel threshold.
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 2800.0)
    seen = []

    def fetch(url, deadline=None):
        return BrochureResource(b"%PDF-1.4 brochure", "application/pdf", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        seen.append(dict(kwargs))
        return BrochureExtraction(
            source_document,
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://app.box.com/shared/static/abc123.pdf",
                "Floor Plan": "https://cdn.example.test/plans/level-2.png",
            }
        ),
        "union.xlsx",
        "UNION",
        "rule:UNION",
    )
    brochure.enrich_properties(
        [prop],
        fetcher=fetch,
        extractor=brochure.extract_brochure,
    )
    assert seen
    assert seen[0].get("max_pages") == brochure._LIGHT_MAX_PAGES
    assert seen[0].get("prefer_photos") is True
    assert seen[0].get("stop_after_floorplans") == 0


def test_viewer_floor_plan_seed_does_not_skip_plan_hunt(monkeypatch):
    """Box/Drive viewer URLs must not count as a finished Floor Plan seed."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 2800.0)
    seen = []

    def fetch(url, deadline=None):
        return BrochureResource(b"%PDF-1.4 brochure", "application/pdf", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        seen.append(dict(kwargs))
        return BrochureExtraction(
            source_document,
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://drive.google.com/file/d/abc/view",
                "Floor Plan": "https://drive.google.com/file/d/abc/view",
            }
        ),
        "metspace.xlsx",
        "MetSpace",
        "rule:MetSpace",
    )
    brochure.enrich_properties(
        [prop],
        fetcher=fetch,
        extractor=brochure.extract_brochure,
    )
    assert seen
    assert seen[0].get("prefer_photos") is False
    assert seen[0].get("stop_after_floorplans") == 1
    assert seen[0].get("max_pages") == max(brochure._LIGHT_MAX_PAGES, brochure._PLAN_ONLY_MAX_PAGES)


def test_retrieve_prefers_html_gallery_and_limits_nested_pdf_photos(monkeypatch):
    """GPE-style pages with a real /media/ gallery must not re-decode marketing photos."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    fetched = []
    nested_kwargs = []

    def fetch(url, deadline=None):
        fetched.append(url)
        if url.endswith(".pdf"):
            return BrochureResource(b"%PDF-1.4 plan", "application/pdf", url, url)
        return BrochureResource(b"<html>listing</html>", "text/html", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        if payload.startswith(b"%PDF"):
            nested_kwargs.append(dict(kwargs))
            return BrochureExtraction(
                source_document,
                assets=[
                    AssetCandidate(
                        "",
                        source_document,
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        content=b"plan-bytes",
                        content_hash="planhashhtml",
                    )
                ],
                identity_text="Example House, 1 Example Street, EC1A 1AA",
            )
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"https://cdn.property.test/media/{name}.jpg",
                    source_document,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            source_document,
            assets=photos
            + [
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/brochure.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.BROCHURE,
                        confidence=0.9,
                        anchor_text="Download brochure",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 0.0)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://property.test/listing",
            }
        ),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert any(url.endswith(".pdf") for url in fetched)
    assert nested_kwargs
    assert all(kw.get("max_photos") == 0 for kw in nested_kwargs)
    assert all(kw.get("prefer_photos") is False for kw in nested_kwargs)
    assert all(
        kw.get("max_pages") == max(brochure._LIGHT_MAX_PAGES, brochure._PLAN_ONLY_MAX_PAGES)
        for kw in nested_kwargs
    )
    candidates = prop.values.get("_high_res_candidates") or []
    assert len(candidates) >= 5
    assert all("media/" in url for url in candidates[:5])


def test_floorplan_pdf_link_on_page_still_fetches_nested_brochure_for_bitmap(monkeypatch):
    """HTML floorplan.pdf links must not count as a finished plan seed.

    Confirmed real (GPE 2026-07): property pages expose *floorplans*.pdf
    assets. Counting them as has_floorplan_seed skipped the nested brochure
    bitmap extract. PDF links may still be kept as a Floor Plan click-through
    fallback when no bitmap is produced (see test_floorplan_pdf_fallback_*).

    Requires ≥2GB host (high-memory path). On Railway 1GB this nested PDF
    path soft-skips before fetch — Floor Plan still gets the HTML pdf link.
    """
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    fetched = []
    nested_kwargs = []

    def fetch(url, deadline=None):
        fetched.append(url)
        if url.endswith(".pdf"):
            return BrochureResource(b"%PDF-1.4 plan", "application/pdf", url, url)
        return BrochureResource(b"<html>listing</html>", "text/html", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        if payload.startswith(b"%PDF"):
            nested_kwargs.append(dict(kwargs))
            return BrochureExtraction(
                source_document,
                assets=[
                    AssetCandidate(
                        "",
                        source_document,
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        content=b"plan-bytes",
                        content_hash="planhashpdfseed",
                        extension="png",
                    )
                ],
                identity_text="Example House, 1 Example Street, EC1A 1AA",
            )
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"https://cdn.property.test/media/{name}.jpg",
                    source_document,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            source_document,
            assets=photos
            + [
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/media/level-2_2d-floorplans_a4.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                        alt_text="Floor plan",
                    )
                ),
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/brochure.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.BROCHURE,
                        confidence=0.9,
                        anchor_text="Download brochure",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 0.0)

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://property.test/listing",
            }
        ),
        "provider.eml",
        "Provider",
        "rule:Provider",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert any(url.endswith("brochure.pdf") for url in fetched)
    assert nested_kwargs
    assert all(kw.get("max_photos") == 0 for kw in nested_kwargs)
    assert all(kw.get("stop_after_floorplans") == 1 for kw in nested_kwargs)
    embeds = prop.values.get("_brochure_embedded_assets") or []
    assert any(a.classification == AssetType.FLOORPLAN and a.content for a in embeds)


def test_retrieve_skips_nested_landlord_pdf_when_gallery_and_plan_and_rss_tight(monkeypatch):
    """Under RSS pressure, HTML gallery + floor plan seed must not pull nested PDFs."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    fetched = []

    def fetch(url, deadline=None):
        fetched.append(url)
        assert not url.endswith(".pdf")
        return BrochureResource(b"<html>listing</html>", "text/html", url, url)

    def fake_extract(payload, content_type, source_document, **kwargs):
        photos = [
            classify_candidate(
                AssetCandidate(
                    f"https://cdn.property.test/media/{name}.jpg",
                    source_document,
                    alt_text=name,
                    association_confidence=0.85,
                    classification=AssetType.PROPERTY_IMAGE,
                    confidence=0.85,
                )
            )
            for name in ("a", "b", "c", "d", "e")
        ]
        return BrochureExtraction(
            source_document,
            assets=photos
            + [
                classify_candidate(
                    AssetCandidate(
                        f"{source_document}/plan.jpg",
                        source_document,
                        alt_text="Floor plan",
                        classification=AssetType.FLOORPLAN,
                        confidence=0.9,
                    )
                ),
                classify_candidate(
                    AssetCandidate(
                        "https://cdn.property.test/brochure.pdf",
                        source_document,
                        mime_type="application/pdf",
                        classification=AssetType.BROCHURE,
                        confidence=0.9,
                        anchor_text="Download brochure",
                    )
                ),
            ],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    monkeypatch.setattr(brochure, "extract_brochure", fake_extract)
    monkeypatch.setattr(
        brochure, "_rss_mb", lambda: brochure._ENRICHMENT_PARALLEL_RSS_MB + 25
    )

    prop = Property.from_record(
        normalize_record(
            {
                "Building": "Example House, 1 Example Street",
                "Property Postcode": "EC1A 1AA",
                "Brochure PDF": "https://property.test/listing",
            }
        ),
        "gpe.eml",
        "GPE",
        "rule:GPE",
    )
    brochure.enrich_properties([prop], fetcher=fetch, extractor=brochure.extract_brochure)
    assert fetched == ["https://property.test/listing"]
    assert len(prop.values.get("_high_res_candidates") or []) >= 5


def test_enrichment_serializes_when_rss_high_even_for_html_pages(monkeypatch):
    """HTML property pages (not Box/Drive) must still use wave size 1 under RSS pressure."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 4096.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_HIGH_MEMORY", True)
    monkeypatch.setattr(brochure, "_ENRICHMENT_PARALLEL_RSS_MB", 2800.0)
    monkeypatch.setattr(brochure, "_RSS_ENRICHMENT_CEILING_MB", 3900.0)
    monkeypatch.setattr(brochure, "_ENRICHMENT_FETCH_WORKERS", 4)
    monkeypatch.setattr(
        brochure, "_rss_mb", lambda: brochure._ENRICHMENT_PARALLEL_RSS_MB + 50
    )

    props = []
    for i in range(3):
        props.append(
            Property.from_record(
                normalize_record(
                    {
                        "Building": f"Building {i}",
                        "Property Postcode": "EC1A 1AA",
                        "Brochure PDF": f"https://property.test/listing-{i}",
                    }
                ),
                "gpe.eml",
                "GPE",
                "rule:GPE",
            )
        )

    concurrent = []
    submitted_waves = []
    real_executor = brochure.ThreadPoolExecutor

    class CountingExecutor:
        def __init__(self, max_workers=1, *args, **kwargs):
            submitted_waves.append(max_workers)
            self._inner = real_executor(max_workers=max_workers)

        def submit(self, *args, **kwargs):
            return self._inner.submit(*args, **kwargs)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    def fetch(url, deadline=None):
        concurrent.append(url)
        return BrochureResource(b"<html>x</html>", "text/html", url, url)

    def extract(payload, content_type, final_url, **kwargs):
        return BrochureExtraction(final_url, identity_text="Building EC1A 1AA")

    monkeypatch.setattr(brochure, "ThreadPoolExecutor", CountingExecutor)
    brochure.enrich_properties(props, fetcher=fetch, extractor=extract)
    # Wave size 1 takes the serial path (no pool) or never exceeds 1 worker.
    assert all(n <= 1 for n in submitted_waves)
    assert len(concurrent) == 3
