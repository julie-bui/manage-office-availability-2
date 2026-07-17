"""Unit tests for Special Features amenity cleanup and sentence-boundary cap."""
from extraction.brochure import enrich_properties
from extraction.models import BrochureExtraction, ExtractedValue, Property
from extraction.schema import normalize_record
from extraction.text_utils import (
    SPECIAL_FEATURES_AMENITY_MAX_ITEMS,
    SPECIAL_FEATURES_AMENITY_MAX_WORDS,
    SPECIAL_FEATURES_MAX_WORDS,
    cap_special_features,
    clean_special_features,
    is_useful_primary_special_features,
    looks_like_ocr_layout_noise,
)


BROCHURE = "https://files.example.test/example-brochure.pdf"

# Confirmed real-style UNION brochure OCR: reversed words, exploded vertical
# label, camelCase sheet headers mixed with one real amenity phrase.
USER_OCR_MESSY = (
    "SPACE; dr3; Currentfloortype 8desks; Fully fitted & "
    "furnished 4 agile work stations; Squarefootage "
    "Kitchen; 624; ecaps; A; noinU; morf; d; n; 2; h; t; u; o; "
    "S"
)


def _words(n, stem="word"):
    return " ".join(f"{stem}{i}" for i in range(n))


def test_short_special_features_unchanged():
    assert cap_special_features("Fitted") == "Fitted"
    assert cap_special_features("Price drop: now £120 psf") == "Price drop: now £120 psf"
    assert cap_special_features("") == ""
    assert cap_special_features(None) == ""
    assert clean_special_features("Fitted") == "Fitted"
    assert clean_special_features("Price drop: now £120 psf") == "Price drop: now £120 psf"
    assert clean_special_features("30 + 3 MR + Collab") == "30 + 3 MR + Collab"


def test_cap_stops_at_last_sentence_boundary_at_or_before_max_words():
    # Three sentences: ~80 + ~80 + ~120 words. Cap should keep the first two
    # (last boundary at/before 250) and drop the third mid-dump.
    first = _words(80, "alpha") + "."
    second = _words(80, "beta") + "."
    third = _words(120, "gamma") + "."
    text = f"{first} {second} {third}"
    assert len(text.split()) > SPECIAL_FEATURES_MAX_WORDS

    capped = cap_special_features(text)
    assert capped == f"{first} {second}"
    assert capped.endswith(".")
    assert len(capped.split()) <= SPECIAL_FEATURES_MAX_WORDS
    assert "gamma0" not in capped
    # Never mid-word / mid-sentence: final token is a full sentence ender.
    assert capped[-1] == "."


def test_cap_falls_back_to_word_limit_with_ellipsis_without_sentence():
    text = _words(SPECIAL_FEATURES_MAX_WORDS + 40, "token")
    capped = cap_special_features(text)
    assert capped.endswith("...")
    body = capped[: -len("...")]
    assert len(body.split()) == SPECIAL_FEATURES_MAX_WORDS
    assert body.split()[-1].startswith("token")


def test_normalize_record_caps_special_features():
    first = _words(100, "one") + "."
    second = _words(100, "two") + "."
    third = _words(100, "three") + "."
    record = normalize_record(
        {
            "Building": "Example House",
            "Special Features": f"{first} {second} {third}",
        }
    )
    features = record["Special Features"]
    assert features == f"{first} {second}"
    assert len(features.split()) <= SPECIAL_FEATURES_MAX_WORDS


def test_clean_messy_brochure_amenity_dump():
    messy = (
        "ING; SUPERB NATURAL LIGHT; bright and; efficient floorplate; "
        "MODERN,; REFURBISHMENT.; S; A; Bike storage; Showers; 0 3; 0 4; "
        "Fibre connectivity; Roof terrace; Meeting rooms; Kitchenette; "
        "Air conditioning; Raised floors; LED lighting; Reception"
    )
    cleaned = clean_special_features(messy)
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    assert "ING" not in parts
    assert "S" not in parts
    assert "A" not in parts
    assert "0 3" not in parts
    assert "0 4" not in parts
    assert "MODERN,;" not in cleaned
    assert "REFURBISHMENT.;" not in cleaned
    # Mid-phrase break rejoined.
    assert "bright and; efficient" not in cleaned
    assert "bright and efficient" in cleaned
    # ALL-CAPS amenity sentence-cased.
    assert "Superb natural light" in cleaned
    assert "Modern" in cleaned
    assert "Refurbishment" in cleaned
    assert len(parts) <= SPECIAL_FEATURES_AMENITY_MAX_ITEMS
    assert len(cleaned.split()) <= SPECIAL_FEATURES_AMENITY_MAX_WORDS
    assert "Bike storage" in cleaned
    assert "Showers" in cleaned


def test_clean_rejects_reversed_and_vertical_ocr_layout_noise():
    assert looks_like_ocr_layout_noise(USER_OCR_MESSY)
    cleaned = clean_special_features(USER_OCR_MESSY)
    force = clean_special_features(USER_OCR_MESSY, force_amenity_list=True)
    # Mostly garbage brochure fill → blank (do not salvage one phrase from OCR soup).
    assert cleaned == ""
    assert force == ""
    for junk in (
        "ecaps",
        "noinU",
        "morf",
        "Currentfloortype",
        "Squarefootage",
        "dr3",
        "624",
        "d; n; 2; h; t; u; o; S",
    ):
        assert junk not in cleaned
        assert junk not in force


def test_clean_drops_reversed_words_and_single_char_runs_in_isolation():
    assert clean_special_features("ecaps; noinU; morf", force_amenity_list=True) == ""
    assert clean_special_features("d; n; 2; h; t; u; o; S", force_amenity_list=True) == ""
    assert "Currentfloortype" not in clean_special_features(
        "Currentfloortype 8desks; Bike storage", force_amenity_list=True
    )
    assert "Bike storage" in clean_special_features(
        "Currentfloortype 8desks; Bike storage", force_amenity_list=True
    )


def test_clean_force_amenity_list_truncates_long_lists():
    items = [f"Feature {i}" for i in range(20)]
    cleaned = clean_special_features("; ".join(items), force_amenity_list=True)
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    assert len(parts) == SPECIAL_FEATURES_AMENITY_MAX_ITEMS
    assert parts[0] == "Feature 0"
    assert parts[-1] == f"Feature {SPECIAL_FEATURES_AMENITY_MAX_ITEMS - 1}"


def test_useful_primary_preferred_over_long_brochure_essay_without_conflict():
    primary = normalize_record(
        {"Building": "Example House", "Brochure PDF": BROCHURE, "Special Features": "Fitted; Bike store"}
    )
    prop = Property.from_record(primary, "primary.eml", "Example", "rule:test")
    long_essay = (
        "ING; SUPERB NATURAL LIGHT; bright and; efficient; S; A; 0 3; 0 4; "
        + "; ".join(f"Amenity phrase number {i}" for i in range(15))
    )
    result = BrochureExtraction(
        BROCHURE,
        {
            "Special Features": ExtractedValue(
                long_essay, "brochure", BROCHURE, "test:brochure", 0.85
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert enriched.values["Special Features"] == "Fitted; Bike store"
    assert not any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_useful_primary_preferred_over_ocr_layout_noise_fill():
    primary = normalize_record(
        {"Building": "Example House", "Brochure PDF": BROCHURE, "Special Features": "Fitted"}
    )
    prop = Property.from_record(primary, "primary.xlsx", "UNION", "rule:UNION")
    result = BrochureExtraction(
        BROCHURE,
        {
            "Special Features": ExtractedValue(
                USER_OCR_MESSY, "brochure", BROCHURE, "test:brochure", 0.85
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert enriched.values["Special Features"] == "Fitted"
    assert not any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_blank_primary_does_not_fill_from_ocr_layout_noise():
    primary = normalize_record(
        {"Building": "Example House", "Brochure PDF": BROCHURE, "Special Features": ""}
    )
    prop = Property.from_record(primary, "primary.xlsx", "UNION", "rule:UNION")
    result = BrochureExtraction(
        BROCHURE,
        {
            "Special Features": ExtractedValue(
                USER_OCR_MESSY, "brochure", BROCHURE, "test:brochure", 0.85
            )
        },
    )
    enriched = enrich_properties(
        [prop],
        fetcher=lambda url: (b"brochure", "application/pdf"),
        extractor=lambda payload, content_type, source: result,
    )[0]
    assert not enriched.values.get("Special Features")


def test_is_useful_primary_special_features():
    assert is_useful_primary_special_features("Fitted")
    assert is_useful_primary_special_features("Price drop: now £120 psf")
    assert not is_useful_primary_special_features("")
    assert not is_useful_primary_special_features(_words(100, "essay"))
    assert not is_useful_primary_special_features(USER_OCR_MESSY)
