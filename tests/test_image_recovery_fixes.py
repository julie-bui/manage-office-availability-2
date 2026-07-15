"""Regression coverage for UNION / Box / MetSpace / Knotel image recovery."""
from pathlib import Path

from extraction.brochure import _box_shared_name, _hosted_document_candidates, enrich_properties
from extraction.file_readers import read_file
from extraction.models import AssetCandidate, AssetType, BrochureExtraction, BrochureResource, Property
from extraction.rules import try_rules, union
from extraction.schema import normalize_record
from extraction.assets import classify_candidate


ROOT = Path(__file__).parent.parent
UNION_CITY2 = ROOT / "UNION - Availability - June 26 - City 2.xlsx"


def test_union_rule_parses_without_llm_and_recovers_hidden_box_links():
    content = read_file(UNION_CITY2)
    assert union.detect(content)
    rule, records = try_rules(content)
    assert rule == "UNION"
    assert len(records) >= 80
    first = normalize_record(records[0])
    assert "9a Devonshire Square" in first["Building"]
    assert first["Floor/Unit"] == "3rd"
    assert first["Brochure PDF"] == "https://app.box.com/s/5ln9uri46xhq586qdoskbc37rhrrftr7"
    # Second floor of the same building gets its own row's Box share, not the first.
    cannon = [normalize_record(r) for r in records if "107 Cannon Street" in r["Building"]]
    assert len(cannon) >= 2
    assert cannon[0]["Brochure PDF"].startswith("https://app.box.com/s/")
    assert cannon[0]["Brochure PDF"] == cannon[1]["Brochure PDF"]


def test_box_shared_name_and_hosted_static_pdf_candidate():
    url = "https://app.box.com/s/5ln9uri46xhq586qdoskbc37rhrrftr7"
    assert _box_shared_name(url) == "5ln9uri46xhq586qdoskbc37rhrrftr7"
    candidates = _hosted_document_candidates(None, url)
    assert candidates
    assert candidates[0].url == "https://app.box.com/shared/static/5ln9uri46xhq586qdoskbc37rhrrftr7.pdf"


def test_enrichment_fetches_each_unique_brochure_url_once():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return BrochureResource(
            b"<html>Example House EC1A 1AA</html>",
            "text/html",
            url,
            url,
        )

    def extract(payload, content_type, final_url):
        return BrochureExtraction(
            final_url,
            assets=[classify_candidate(AssetCandidate(f"{final_url}/photo.jpg", final_url, alt_text="Office"))],
            identity_text="Example House, 1 Example Street, EC1A 1AA",
        )

    props = []
    for floor in ("1st", "2nd", "3rd"):
        props.append(
            Property.from_record(
                normalize_record(
                    {
                        "Building": "Example House, 1 Example Street",
                        "Property Postcode": "EC1A 1AA",
                        "Floor/Unit": floor,
                        "Brochure PDF": "https://property.test/shared-brochure",
                    }
                ),
                "sheet.xlsx",
                "UNION",
                "rule:UNION",
            )
        )
    enrich_properties(props, fetcher=fetch, extractor=extract)
    assert fetched == ["https://property.test/shared-brochure"]
    assert all(prop.values.get("High Res Images") for prop in props)
