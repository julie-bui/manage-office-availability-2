"""Regression coverage for Manchester/Union/Knotel enrichment seeds."""
from extraction.pipeline import _geocode_query
from extraction.rules import knotel, spreadsheet_blocks, union
from extraction.schema import normalize_record


def test_geocode_query_uses_manchester_not_london_for_m_postcode():
    query = _geocode_query(
        {
            "Property Address 1": "80 Mosley Street, M2 3FX",
            "Property Postcode": "M2 3FX",
        }
    )
    assert "Manchester" in query
    assert "London" not in query


def test_geocode_query_still_defaults_bare_streets_to_london():
    query = _geocode_query({"Property Address 1": "28 Bruton Street"})
    assert "London" in query


def test_spreadsheet_blocks_manchester_contacts_and_desk_features():
    content = {
        "filename": "Workplace Plus - Manchester.xlsx",
        "sheet_names": ["MANCHESTER"],
        "tables": [
            [
                ["", "MANCHESTER", "", "", "", "", "", "BROCHURES"],
                ["", "Merchant Exchange, 17-19 Whitworth Street West, M1 5WG", "", "", "", "", "", ""],
                ["", "Unit/Floor", "Sq Ft", "Desks", "Term", "Per Month", "", ""],
                ["", "5th Floor", "4119", "30 + 3 MR + Collab", "3-5 Years", "23300", "", ""],
            ]
        ],
        "row_links": [],
    }
    assert spreadsheet_blocks.detect(content)
    records = spreadsheet_blocks.parse(content)
    assert records
    assert records[0]["Contacts"] == "Workplace Plus, hello@workplaceplus.co.uk"
    assert "MR" in records[0]["Special Features"]
    assert records[0]["Desks (max)"] == 30
    normalized = normalize_record(records[0])
    assert normalized["Contacts"]
    assert normalized["Special Features"]


def test_union_detects_filename_even_without_intro_blurb():
    content = {
        "filename": "UNION - Availability - June 26 - Clerkenwell & Farringdon.xlsx",
        "text": "City Floor Size sq.ft",
        "sheet_names": ["Clerkenwell & Farringdon"],
        "tables": [
            [
                ["", "City", "Floor", "Current Spec", "Size sq.ft", "Minimum Term", "Monthly Rate", "Price p/sq.ft", "Brochure"],
                ["", "Example House", "3rd", "Fitted", "1466", "2 Years", "20157", "165", "CLICK HERE"],
            ]
        ],
    }
    assert union.detect(content)
    records = union.parse(content)
    assert records
    assert records[0]["Building"] == "Example House"


def test_knotel_keeps_high_trust_view_brochure_as_extra_seed():
    group = {
        "property": "https://knotel.com/offices/london/hallmark/u/hallmark-6th-floor",
        "brochure": "https://cdn.knotel.test/brochures/hallmark.pdf",
        "listing": "",
    }
    assert knotel._best_brochure_link(group) == group["property"]
    extras = knotel._extra_brochure_urls(group, group["property"])
    assert group["brochure"] in extras


def test_knotel_ignores_pitch_brochure_as_extra_seed():
    group = {
        "property": "https://knotel.com/offices/london/hallmark/u/hallmark-6th-floor",
        "brochure": "https://pitch.com/v/hallmark-brochure",
    }
    assert knotel._best_brochure_link(group) == group["property"]
    assert knotel._extra_brochure_urls(group, group["property"]) == []


def test_resolve_source_date_reads_filename_month_day():
    from extraction.naming import extract_date_from_filename, resolve_source_date

    assert extract_date_from_filename("UNION - Availability - June 26 - City 2.xlsx", 2026) == "2026-06-26"
    assert extract_date_from_filename("Fw_ Knotel Availability _ 30_06_2026.eml") == "2026-06-30"
    assert extract_date_from_filename("Workplace Plus - Availability 14th July.eml", 2026) == "2026-07-14"
    assert (
        resolve_source_date(
            {
                "filename": "UNION - Availability - June 26 - City 2.xlsx",
                "file_mtime_year": 2026,
            }
        )
        == "2026-06-26"
    )


def test_union_seeds_floor_plan_from_brochure_click_here():
    content = {
        "filename": "UNION - Availability - June 26 - City.xlsx",
        "sheet_names": ["City"],
        "tables": [
            [
                ["", "City", "Floor", "Current Spec", "Size sq.ft", "Minimum Term", "Monthly Rate", "Price p/sq.ft", "Brochure"],
                ["", "Example House", "3rd", "Fitted", "1466", "2 Years", "20157", "165", "CLICK HERE"],
            ]
        ],
        "row_links": [
            {
                "sheet_name": "City",
                "row_text": "Example House | 3rd | Fitted | 1466 | CLICK HERE",
                "links": [("CLICK HERE", "https://app.box.com/s/examplebrochure")],
            }
        ],
    }
    records = union.parse(content)
    assert records[0]["Brochure PDF"] == "https://app.box.com/s/examplebrochure"
    assert records[0]["Floor Plan"] == "https://app.box.com/s/examplebrochure"
