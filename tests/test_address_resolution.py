from extraction.address_resolution import (
    AddressCandidate,
    AddressResolutionStatus,
    assess_candidate,
    parse_address,
    rank_candidates,
    resolve_address,
)
from extraction import geocode


RIGHT = AddressCandidate("49 Southwark Bridge Road, London SE1 9HH", "nominatim")
WRONG = AddressCandidate("138 Southwark Bridge Road, London SE1 0DG", "nominatim")


def test_southwark_bridge_regression_selects_exact_property():
    resolution = resolve_address("49 Southwark Bridge Road", candidate_provider=lambda query: [WRONG, RIGHT])
    assert resolution.selected_candidate.address == RIGHT.address
    assert resolution.selected_candidate.components.postcode == "SE1 9HH"
    assert resolution.status == AddressResolutionStatus.RESOLVED_FROM_VALIDATED_LOOKUP


def test_wrong_number_on_same_street_is_hard_rejected():
    assessment = assess_candidate(parse_address("49 Southwark Bridge Road"), WRONG)
    assert assessment.rejected
    assert "building number conflict" in assessment.reasons[0]


def test_exact_number_and_street_rank_above_other_candidates():
    other_street = AddressCandidate("49 Blackfriars Road, London SE1 8NZ", "lookup")
    ranked = rank_candidates("49 Southwark Bridge Road", [other_street, RIGHT])
    assert ranked[0].candidate == RIGHT
    assert ranked[1].rejected


def test_valid_source_postcode_is_never_overwritten_by_lookup():
    called = False

    def provider(query):
        nonlocal called
        called = True
        return [WRONG]

    resolution = resolve_address("49 Southwark Bridge Road, SE1 9HH", candidate_provider=provider)
    assert resolution.status == AddressResolutionStatus.RESOLVED_FROM_SOURCE
    assert resolution.selected_candidate.components.postcode == "SE1 9HH"
    assert not called


def test_brochure_can_resolve_missing_postcode():
    brochure = AddressCandidate("49 Southwark Bridge Road, London SE1 9HH", "brochure")
    resolution = resolve_address("49 Southwark Bridge Road", trusted_evidence=[brochure])
    assert resolution.status == AddressResolutionStatus.RESOLVED_FROM_BROCHURE
    assert resolution.final_postcode_source == "brochure"


def test_property_page_can_resolve_missing_postcode():
    page = AddressCandidate("49 Southwark Bridge Road, London SE1 9HH", "property_page")
    resolution = resolve_address("49 Southwark Bridge Road", trusted_evidence=[page])
    assert resolution.status == AddressResolutionStatus.RESOLVED_FROM_PROPERTY_PAGE


def test_multiple_agreeing_sources_increase_confidence():
    one = resolve_address("49 Southwark Bridge Road", trusted_evidence=[RIGHT])
    two = resolve_address(
        "49 Southwark Bridge Road",
        trusted_evidence=[RIGHT, AddressCandidate(RIGHT.address, "agent_listing")],
    )
    assert two.confidence > one.confidence
    assert set(two.evidence_sources) == {"nominatim", "agent_listing"}


def test_geocoder_disagreement_cannot_override_source_data():
    resolution = resolve_address(
        "49 Southwark Bridge Road",
        original_postcode="SE1 9HH",
        candidate_provider=lambda query: [WRONG],
    )
    assert resolution.status == AddressResolutionStatus.RESOLVED_FROM_SOURCE
    assert resolution.selected_candidate.components.postcode == "SE1 9HH"


def test_unresolved_becomes_manual_only_after_all_queries_attempted():
    attempted = []

    def provider(query):
        attempted.append(query)
        return []

    resolution = resolve_address("Unknown Future Building", candidate_provider=provider)
    assert attempted == resolution.query_variants
    assert resolution.status == AddressResolutionStatus.MANUAL_REVIEW_REQUIRED


def test_postcode_digits_are_not_mistaken_for_a_building_number():
    parsed = parse_address("The Shard, London SE1 9SG")
    assert parsed.building_number == ""
    assert parsed.postcode == "SE1 9SG"


def test_all_rejected_candidates_end_in_manual_review_with_reason():
    resolution = resolve_address("49 Southwark Bridge Road", candidate_provider=lambda query: [WRONG])
    assert resolution.status == AddressResolutionStatus.MANUAL_REVIEW_REQUIRED
    assert resolution.resolution_reason == AddressResolutionStatus.NO_VALID_CANDIDATE


def test_unknown_provider_uses_same_shared_resolver():
    resolution = resolve_address("49 Southwark Bridge Road", trusted_evidence=[RIGHT])
    assert resolution.selected_candidate.components.postcode == "SE1 9HH"


def test_nominatim_candidate_adapter_rejects_138_for_requested_49(monkeypatch):
    responses = [
        {"lat": "51.5", "lon": "-0.1", "display_name": WRONG.address, "address": {"house_number": "138", "road": "Southwark Bridge Road", "postcode": "SE1 0DG"}},
        {"lat": "51.51", "lon": "-0.11", "display_name": RIGHT.address, "address": {"house_number": "49", "road": "Southwark Bridge Road", "postcode": "SE1 9HH"}},
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return responses

    monkeypatch.setattr(geocode, "_throttle", lambda: None)
    monkeypatch.setattr(geocode.requests, "get", lambda *args, **kwargs: Response())
    lat, lng, postcode, error = geocode._fetch("49 Southwark Bridge Road, London, UK")
    assert (lat, lng, postcode, error) == (51.51, -0.11, "SE1 9HH", None)
