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
        (0, 1),
        (512, 1),
        (1024, 1),
        (2048, 2),
        (4096, 4),
        (8192, 4),
    ],
)
def test_enrichment_default_workers_scale_with_ram(ram_mb, expected_workers):
    assert brochure._default_fetch_workers(ram_mb) == expected_workers


@pytest.mark.parametrize(
    "ram_mb",
    [0, 512, 1024, 2048],
)
def test_enrichment_rss_ceiling_stays_below_host_ram(ram_mb):
    ceiling = brochure._default_rss_ceiling_mb(ram_mb)
    parallel = brochure._default_parallel_rss_mb(ram_mb)
    if ram_mb > 0:
        assert ceiling <= ram_mb
        assert parallel < ceiling
    else:
        # Unknown host: conservative defaults that trip before a 1GB kill.
        assert ceiling <= 512
        assert parallel < ceiling


def test_enrichment_memory_constrained_for_modest_rss_on_small_host():
    assert brochure._enrichment_memory_constrained(100.0, hosted=True)
    # Large host with low RSS and no hosted PDFs can stay unconstrained.
    # (Function reads module _HOST_RAM_MB; patch it for this assertion.)
    original = brochure._HOST_RAM_MB
    try:
        brochure._HOST_RAM_MB = 4096.0
        assert not brochure._enrichment_memory_constrained(40.0, hosted=False)
        brochure._HOST_RAM_MB = 1024.0
        assert brochure._enrichment_memory_constrained(40.0, hosted=False)
    finally:
        brochure._HOST_RAM_MB = original


def test_enrichment_skips_remaining_urls_when_rss_near_ceiling(monkeypatch):
    """Approaching OOM must skip remaining brochure URLs rather than SIGKILL."""
    calls = {"n": 0}

    def fake_rss():
        calls["n"] += 1
        # Worker sizing + first wave check stay under ceiling; after the first
        # serial materialize, climb above the soft stop.
        if calls["n"] <= 2:
            return 50.0
        return brochure._RSS_ENRICHMENT_CEILING_MB + 80

    monkeypatch.setattr(brochure, "_rss_mb", fake_rss)
    monkeypatch.setattr(brochure, "_enrichment_memory_constrained", lambda *a, **k: False)
    monkeypatch.setattr(brochure, "_ENRICHMENT_FETCH_WORKERS", 1)

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


def test_hosted_box_pdf_forces_light_extract_under_modest_rss(monkeypatch):
    """UNION Box PDFs on ~1GB hosts must use light first-page extract kwargs."""
    monkeypatch.setattr(brochure, "_HOST_RAM_MB", 1024.0)
    monkeypatch.setattr(brochure, "_rss_mb", lambda: 100.0)
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
    # Pass the real extract_brochure name so _retrieve takes the light kwargs path.
    brochure.enrich_properties(
        [prop],
        fetcher=fetch,
        extractor=brochure.extract_brochure,
    )
    assert seen
    assert seen[0].get("max_pages") == brochure._LIGHT_MAX_PAGES
    assert seen[0].get("max_photos") == brochure._LIGHT_MAX_PHOTOS


def test_retrieve_prefers_html_gallery_and_limits_nested_pdf_photos(monkeypatch):
    """GPE-style pages with a real /media/ gallery must not re-decode marketing photos."""
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
    candidates = prop.values.get("_high_res_candidates") or []
    assert len(candidates) >= 5
    assert all("media/" in url for url in candidates[:5])


def test_retrieve_skips_nested_landlord_pdf_when_gallery_and_plan_and_rss_tight(monkeypatch):
    """Under RSS pressure, HTML gallery + floor plan seed must not pull nested PDFs."""
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
