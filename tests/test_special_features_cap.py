"""Unit tests for Special Features sentence-boundary truncation."""
from extraction.schema import normalize_record
from extraction.text_utils import SPECIAL_FEATURES_MAX_WORDS, cap_special_features


def _words(n, stem="word"):
    return " ".join(f"{stem}{i}" for i in range(n))


def test_short_special_features_unchanged():
    assert cap_special_features("Fitted") == "Fitted"
    assert cap_special_features("Price drop: now £120 psf") == "Price drop: now £120 psf"
    assert cap_special_features("") == ""
    assert cap_special_features(None) == ""


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
