"""Unit tests for Special Features amenity cleanup and 50-word boundary cap."""
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


def test_special_features_max_is_one_hundred_words():
    assert SPECIAL_FEATURES_MAX_WORDS == 100
    assert SPECIAL_FEATURES_AMENITY_MAX_WORDS == 100


def test_short_special_features_unchanged():
    assert cap_special_features("Fitted") == "Fitted"
    assert cap_special_features("Price drop: now £120 psf") == "Price drop: now £120 psf"
    assert cap_special_features("") == ""
    assert cap_special_features(None) == ""
    assert clean_special_features("Fitted") == "Fitted"
    assert clean_special_features("Price drop: now £120 psf") == "Price drop: now £120 psf"
    assert clean_special_features("30 + 3 MR + Collab") == "30 + 3 MR + Collab"


def test_cap_stops_at_last_sentence_boundary_at_or_before_max_words():
    # Two sentences under 100 words, third pushes over — keep first two.
    first = _words(40, "alpha") + "."
    second = _words(40, "beta") + "."
    third = _words(40, "gamma") + "."
    text = f"{first} {second} {third}"
    assert len(text.split()) > SPECIAL_FEATURES_MAX_WORDS

    capped = cap_special_features(text)
    assert capped == f"{first} {second}"
    assert capped.endswith(".")
    assert len(capped.split()) <= SPECIAL_FEATURES_MAX_WORDS
    assert "gamma0" not in capped
    assert capped[-1] == "."


def test_cap_stops_at_last_complete_amenity_item():
    # Semicolon amenities: keep whole items under 100 words, never mid-phrase.
    items = [
        _words(30, "one"),
        _words(30, "two"),
        _words(30, "three"),
        _words(30, "four"),
    ]
    text = "; ".join(items)
    assert len(text.split()) > SPECIAL_FEATURES_MAX_WORDS
    capped = cap_special_features(text)
    assert capped == "; ".join(items[:3])
    assert len(capped.split()) <= SPECIAL_FEATURES_MAX_WORDS
    assert "four0" not in capped
    assert not capped.endswith("...")


def test_cap_prefers_richer_complete_boundary():
    # Sentence boundary keeps more words than truncating mid-dump would.
    first = _words(40, "alpha") + "."
    second = _words(40, "beta") + "."
    third = _words(40, "gamma") + "."
    text = f"{first} {second} {third}"
    capped = cap_special_features(text)
    assert capped == f"{first} {second}"
    assert len(capped.split()) == 80


def test_cap_falls_back_to_word_limit_with_ellipsis_without_sentence():
    text = _words(SPECIAL_FEATURES_MAX_WORDS + 40, "token")
    capped = cap_special_features(text)
    assert capped.endswith("...")
    body = capped[: -len("...")]
    assert len(body.split()) == SPECIAL_FEATURES_MAX_WORDS
    assert body.split()[-1].startswith("token")


def test_normalize_record_caps_special_features():
    first = _words(40, "one") + "."
    second = _words(40, "two") + "."
    third = _words(40, "three") + "."
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
    assert "bright and; efficient" not in cleaned
    assert "bright and efficient" in cleaned
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
    # Pure OCR junk blanks; a buried real amenity phrase may still salvage.
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
    # The one real amenity phrase in the user OCR sample should survive when
    # the description fallback runs (Kitts Special Features stay non-empty).
    assert "fitted" in cleaned.lower() or cleaned == ""
    assert "fitted" in force.lower() or force == ""


def test_description_fallback_when_amenity_cleaner_near_empty():
    # Aggressive list cleaning must not wipe a real Kitts-style description.
    prose = (
        "Stunning period property in the heart of Mayfair, communal roof terrace. "
        "Fit out to be completed in June 2026"
    )
    cleaned = clean_special_features(prose, force_amenity_list=True)
    assert len(cleaned.split()) >= 8
    assert "Mayfair" in cleaned
    assert "terrace" in cleaned.lower()


def test_kitts_style_description_prose_passes_through():
    blurb = "In built meeting room and kitchenette. Excellent value in stunning location"
    assert clean_special_features(blurb) == blurb
    assert len(clean_special_features(blurb).split()) <= SPECIAL_FEATURES_MAX_WORDS


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
    assert len(cleaned.split()) <= SPECIAL_FEATURES_AMENITY_MAX_WORDS


def test_useful_primary_preferred_over_long_brochure_essay_without_conflict():
    primary = normalize_record(
        {"Building": "Example House", "Brochure PDF": BROCHURE, "Special Features": "Fitted; Bike store"}
    )
    # Status tag moves to State of Space; amenity remainder stays in Special Features.
    assert primary["State of Space"] == "Fitted"
    assert primary["Special Features"] == "Bike store"
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
    assert enriched.values["Special Features"] == "Bike store"
    assert enriched.values["State of Space"] == "Fitted"
    assert not any(issue.stage == "brochure_conflict_resolution" for issue in enriched.issues)


def test_useful_primary_preferred_over_ocr_layout_noise_fill():
    primary = normalize_record(
        {"Building": "Example House", "Brochure PDF": BROCHURE, "Special Features": "Fitted"}
    )
    assert primary["State of Space"] == "Fitted"
    assert primary["Special Features"] == ""
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
    # Pure status left SF blank — OCR junk tokens must not ship; SoS keeps Fitted.
    features = enriched.values.get("Special Features") or ""
    for junk in ("ecaps", "noinU", "morf", "Currentfloortype", "dr3", "d; n; 2"):
        assert junk not in features
    assert enriched.values["State of Space"] == "Fitted"
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
    features = enriched.values.get("Special Features") or ""
    # Reversed/vertical OCR tokens must never ship; a buried real amenity
    # phrase may salvage into a short Kitts-style description.
    for junk in ("ecaps", "noinU", "morf", "Currentfloortype", "dr3", "d; n; 2"):
        assert junk not in features
    if features:
        assert len(features.split()) >= 4
        assert "fitted" in features.lower() or "kitchen" in features.lower()


def test_pure_ocr_tokens_still_blank_special_features():
    assert clean_special_features("ecaps; noinU; morf", force_amenity_list=True) == ""
    assert clean_special_features("d; n; 2; h; t; u; o; S", force_amenity_list=True) == ""


def test_is_useful_primary_special_features():
    assert is_useful_primary_special_features("Fitted")
    assert is_useful_primary_special_features("Price drop: now £120 psf")
    assert not is_useful_primary_special_features("")
    assert not is_useful_primary_special_features(_words(100, "essay"))
    assert not is_useful_primary_special_features(USER_OCR_MESSY)
