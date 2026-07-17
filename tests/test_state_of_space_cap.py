"""Unit tests for State of Space cleanup, status tags, and brochure preference."""
from extraction.brochure import enrich_properties
from extraction.models import BrochureExtraction, ExtractedValue, Property
from extraction.schema import normalize_record
from extraction.text_utils import (
    STATE_OF_SPACE_MAX_WORDS,
    cap_prose_field,
    cap_state_of_space,
    clean_state_of_space,
    extract_state_of_space_status,
    is_useful_primary_state_of_space,
    looks_like_ocr_layout_noise,
)


BROCHURE = "https://files.example.test/example-brochure.pdf"

USER_OCR_MESSY = (
    "SPACE; dr3; Currentfloortype 8desks; Fully fitted & "
    "furnished 4 agile work stations; Squarefootage "
    "Kitchen; 624; ecaps; A; noinU; morf; d; n; 2; h; t; u; o; "
    "S"
)


def _words(n, stem="word"):
    return " ".join(f"{stem}{i}" for i in range(n))


def test_state_of_space_max_is_under_fifty_words():
    assert STATE_OF_SPACE_MAX_WORDS == 50


def test_short_state_of_space_unchanged():
    assert cap_state_of_space("Immediate") == "Immediate"
    assert cap_state_of_space("Fully fitted") == "Fully fitted"
    assert cap_state_of_space("") == ""
    assert cap_state_of_space(None) == ""
    assert clean_state_of_space("Immediate") == "Immediate"
    assert clean_state_of_space("Cat A") == "CAT A"
    assert clean_state_of_space("Fully Fitted") == "Fully Fitted"


def test_clean_state_of_space_status_only_blanks_prose_without_tag():
    # Kitts State of Space is tag-or-blank — never keep long condition essays.
    prose = "Bright dual-aspect floor with excellent natural light and a new kitchenette"
    assert extract_state_of_space_status(prose) == ""
    assert clean_state_of_space(prose) == ""
    assert clean_state_of_space("Fully fitted floor ready for occupation") == "Fully Fitted"


def test_extract_state_of_space_status_kitt_phrases():
    assert extract_state_of_space_status("Fully fitted floor with terrace") == "Fully Fitted"
    assert extract_state_of_space_status("Fitout underway — delivery Q3") == "Fitout Underway"
    assert (
        extract_state_of_space_status("CAT A - Custom Fit Out Opportunity")
        == "CAT A - Custom Fit Out Opportunity"
    )
    assert extract_state_of_space_status("Partially fitted desks") == "Partially Fitted"


def test_cap_prose_field_shared_by_state_of_space():
    first = _words(20, "alpha") + "."
    second = _words(20, "beta") + "."
    third = _words(30, "gamma") + "."
    text = f"{first} {second} {third}"
    assert len(text.split()) > STATE_OF_SPACE_MAX_WORDS

    capped = cap_state_of_space(text)
    assert capped == cap_prose_field(text, max_words=STATE_OF_SPACE_MAX_WORDS)
    assert capped == f"{first} {second}"
    assert capped.endswith(".")
    assert len(capped.split()) <= STATE_OF_SPACE_MAX_WORDS
    assert "gamma0" not in capped


def test_cap_state_of_space_ellipsis_without_sentence():
    text = _words(STATE_OF_SPACE_MAX_WORDS + 40, "token")
    capped = cap_state_of_space(text)
    assert capped.endswith("...")
    body = capped[: -len("...")]
    assert len(body.split()) == STATE_OF_SPACE_MAX_WORDS


def test_normalize_record_caps_state_of_space():
    # Long condition prose without a status tag blanks (Kitts tag-or-blank).
    first = _words(20, "one") + "."
    second = _words(20, "two") + "."
    third = _words(30, "three") + "."
    record = normalize_record(
        {
            "Building": "Example House",
            "State of Space": f"{first} {second} {third}",
        }
    )
    assert record["State of Space"] == ""


def test_to_record_caps_state_of_space():
    record = normalize_record({"Building": "Example House"})
    prop = Property.from_record(record, "primary.xlsx", "GPE", "rule:GPE")
    prop.values["State of Space"] = "Fully fitted floor with terrace and meeting rooms"
    exported = prop.to_record()
    assert exported["State of Space"] == "Fully Fitted"


def test_clean_state_of_space_messy_amenity_dump_without_status_is_blank():
    messy = (
        "ING; SUPERB NATURAL LIGHT; bright and; efficient floorplate; "
        "MODERN,; REFURBISHMENT.; S; A; Bike storage; Showers; 0 3; 0 4; "
        "Fibre connectivity; Roof terrace; Meeting rooms; Kitchenette; "
        "Air conditioning; Raised floors; LED lighting; Reception"
    )
    assert clean_state_of_space(messy) == ""
    assert clean_state_of_space(messy, force_amenity_list=True) == ""


def test_clean_state_of_space_messy_prefers_status_tag():
    messy = "Fully fitted; Bike storage; Showers; Meeting rooms; Kitchenette"
    assert clean_state_of_space(messy) == "Fully Fitted"
    assert clean_state_of_space(messy, force_amenity_list=True) == "Fully Fitted"


def test_clean_state_of_space_rejects_ocr_layout_noise():
    assert looks_like_ocr_layout_noise(USER_OCR_MESSY)
    assert clean_state_of_space(USER_OCR_MESSY) == ""
    assert clean_state_of_space(USER_OCR_MESSY, force_amenity_list=True) == ""


def test_useful_primary_state_of_space_preferred_over_long_brochure():
    primary = normalize_record(
        {
            "Building": "Example House",
            "Brochure PDF": BROCHURE,
            "State of Space": "Immediate",
        }
    )
    prop = Property.from_record(primary, "primary.xlsx", "UNION", "rule:UNION")
    long_essay = (
        "ING; SUPERB NATURAL LIGHT; bright and; efficient; S; A; 0 3; 0 4; "
        + "; ".join(f"Amenity phrase number {i}" for i in range(15))
    )
    result = BrochureExtraction(
        BROCHURE,
        {
            "State of Space": ExtractedValue(
                long_essay, "brochure", BROCHURE, "test:brochure", 0.85
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert enriched.values["State of Space"] == "Immediate"
    assert not any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_useful_primary_state_of_space_preferred_over_ocr_noise():
    primary = normalize_record(
        {
            "Building": "Example House",
            "Brochure PDF": BROCHURE,
            "State of Space": "Cat A fitted",
        }
    )
    assert primary["State of Space"] == "CAT A"
    prop = Property.from_record(primary, "primary.xlsx", "UNION", "rule:UNION")
    result = BrochureExtraction(
        BROCHURE,
        {
            "State of Space": ExtractedValue(
                USER_OCR_MESSY, "brochure", BROCHURE, "test:brochure", 0.85
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert enriched.values["State of Space"] == "CAT A"
    assert not any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_useful_primary_state_of_space_normalizes_cat_a():
    assert is_useful_primary_state_of_space("Cat A fitted")
    assert clean_state_of_space("Cat A fitted") == "CAT A"


def test_blank_primary_does_not_fill_state_of_space_from_ocr_noise():
    primary = normalize_record(
        {
            "Building": "Example House",
            "Brochure PDF": BROCHURE,
            "State of Space": "",
        }
    )
    prop = Property.from_record(primary, "primary.xlsx", "UNION", "rule:UNION")
    result = BrochureExtraction(
        BROCHURE,
        {
            "State of Space": ExtractedValue(
                USER_OCR_MESSY, "brochure", BROCHURE, "test:brochure", 0.85
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert not enriched.values.get("State of Space")


def test_blank_primary_fills_status_tag_from_brochure():
    primary = normalize_record(
        {
            "Building": "Example House",
            "Brochure PDF": BROCHURE,
            "State of Space": "",
        }
    )
    prop = Property.from_record(primary, "primary.xlsx", "UNION", "rule:UNION")
    result = BrochureExtraction(
        BROCHURE,
        {
            "State of Space": ExtractedValue(
                "Fully fitted; Bike storage; Showers",
                "brochure",
                BROCHURE,
                "test:brochure",
                0.85,
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert enriched.values["State of Space"] == "Fully Fitted"
    assert "Bike storage" not in enriched.values["State of Space"]


def test_is_useful_primary_state_of_space():
    assert is_useful_primary_state_of_space("Immediate")
    assert is_useful_primary_state_of_space("Fully fitted")
    assert not is_useful_primary_state_of_space("")
    assert not is_useful_primary_state_of_space(_words(100, "essay"))
    assert not is_useful_primary_state_of_space(USER_OCR_MESSY)
