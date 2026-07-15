"""Regression test against the 3 example files this app was built for.
Run with: python tests/test_examples.py
Asserts on rule *names* and minimum record counts (not exact field values —
those are covered by manual review) so this catches "a parser stopped
matching" or "extraction silently dropped most rows" regressions.
"""
import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from extraction.file_readers import read_file
from extraction.rules import try_rules
from extraction.schema import normalize_record, street_address_only, names_only
from extraction import geocode as geocode_module
from extraction import html_images
from extraction.llm_fallback import _build_prompt
from extraction import pdf_images
from extraction import xlsx_links
from extraction import rule_sanity
from extraction import naming as naming_module
from extraction import pipeline as pipeline_module
import app as app_module
import time

EXPECTATIONS = [
    ("Fw_ Knotel Availability _ 30_06_2026.eml", "Knotel", 16),
    ("Fw_ Knotel Availability _ 13_07_2026.eml", "Knotel", 15),
    ("Fw_ MetSpace Availability Update.eml", "MetSpace", 14),
    ("Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml", "GPE", 15),
    # 57, not 19 — a real, previously-shipped regression (2026-07 audit):
    # extraction.rules.grid only ever looked at the FIRST table matching
    # its header keywords, so a long table split across multiple
    # page-tables by pdfplumber (confirmed: this exact PDF has 3, each
    # repeating the same header row) silently dropped every row on every
    # table after the first — 38 of 57 real listings, across 2 entirely-
    # ignored tables. An exact count (not just ">= 19", which the old,
    # already-broken 19 would have kept passing) is what actually catches
    # this class of bug — see main()'s own exact-match check below.
    ("Kitt's Availability (External) - Live Availability.pdf", "Grid/Tabular", 57),
    ("BC Current Availability.pdf", "BC", 11),
    ("John Stow House.pdf", "Breezblok", 1),
]


def check_metspace_floor_plans(failures):
    """Targeted regression test for two real bugs that already shipped:
    MetSpace's rule originally never extracted Floor Plan/High Res Images
    at all, despite the source email genuinely containing a per-listing
    floor plan image (confirmed by actually viewing several of them - real
    floor-plan diagrams, not building photos, which is why this checks
    Floor Plan specifically and not High Res Images). The fix for that was
    then found to have the image-to-listing direction backwards (assumed
    "image precedes link", inherited from Knotel's pattern, when this
    source's own raw HTML is actually "image follows link" - the same
    class of off-by-one as GPE's High Res Images bug) - confirmed by
    walking the raw html_items sequence directly, which is why every one
    of the 14 listings has a real, distinct floor plan image, not 13/14.
    Pins the exact, known-correct count for this specific example file
    rather than a vague ">0" check, so a future regression (e.g. someone
    "fixing" the html_items image filter, or re-flipping the direction,
    and breaking this again) fails loudly here instead of only being
    caught by manually spot-checking a spreadsheet later.

    Also pins Brochure PDF (2026-07 audit): each building name in this
    source is itself the hyperlink to a real, listing-specific brochure
    (confirmed by actually following one — a Mailchimp click-tracking
    redirect that 302s to a Google Drive file literally titled "9-10
    Market Place - 2nd Floor") — reuses the exact same matched-link
    position that Floor Plan's own image lookup already relies on, so
    this also guards against that position tracking regressing (all 14
    rows, including the 5 same-building "141 Fenchurch Street (Monument)"
    floors, must each get their own distinct link, not a neighbour's)."""
    filename = "Fw_ MetSpace Availability Update.eml"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "MetSpace" or not records:
        failures.append(f"{filename}: expected rule 'MetSpace' with records, got '{rule_name}'")
        return

    floor_plan_count = sum(1 for r in records if (r.get("Floor Plan") or "").strip())
    high_res_count = sum(1 for r in records if (r.get("High Res Images") or "").strip())
    distinct_floor_plans = len({r["Floor Plan"] for r in records if (r.get("Floor Plan") or "").strip()})

    # Known-correct for this exact example file: all 14 listings have a
    # real, distinct floor plan image — confirmed directly against the raw
    # HTML (each listing's link is immediately followed by its own image).
    if floor_plan_count != 14:
        failures.append(
            f"{filename}: expected all 14 records to have a real Floor Plan URL, got {floor_plan_count}/{len(records)} "
            "— MetSpace's floor-plan-image extraction may be broken again"
        )
    if distinct_floor_plans != floor_plan_count:
        failures.append(
            f"{filename}: expected every populated Floor Plan to be distinct, got only {distinct_floor_plans} distinct "
            f"URLs across {floor_plan_count} populated rows — a listing may be getting a neighbor's image "
            "(the image-follows-link direction may have regressed to image-precedes-link)"
        )
    # High Res Images should stay blank for MetSpace: the only embedded
    # image per listing in this source is a floor plan, not a photo -
    # populating this too would be fabricating a distinction the source
    # doesn't actually have.
    if high_res_count != 0:
        failures.append(
            f"{filename}: expected High Res Images blank for all rows (MetSpace's only per-listing image is a "
            f"floor plan, not a photo), got {high_res_count}/{len(records)} populated"
        )

    brochure_count = sum(1 for r in records if (r.get("Brochure PDF") or "").strip())
    distinct_brochures = len({r["Brochure PDF"] for r in records if (r.get("Brochure PDF") or "").strip()})
    if brochure_count != 14:
        failures.append(
            f"{filename}: expected all 14 records to have a real Brochure PDF URL, got {brochure_count}/{len(records)} "
            "— MetSpace's brochure-link extraction may be broken"
        )
    if distinct_brochures != brochure_count:
        failures.append(
            f"{filename}: expected every populated Brochure PDF to be distinct, got only {distinct_brochures} distinct "
            f"URLs across {brochure_count} populated rows — a listing may be getting a neighbour's brochure link"
        )
    by_building_floor = {(r.get("Building"), r.get("Floor/Unit")): r.get("Brochure PDF") for r in records}
    expected_brochure = {
        ("9-10 Market Place", ""): "https://us.list-manage.com/Q6gnhZazI8v?e=095cb98613&c2id=a206dcecbc185bd9a5d5a46b47a3996f",
        ("141 Fenchurch Street (Monument)", "G Floor"): "https://us.list-manage.com/WrtQM3D1UAI?e=095cb98613&c2id=a206dcecbc185bd9a5d5a46b47a3996f",
        ("141 Fenchurch Street (Monument)", "7th Floor"): "https://us.list-manage.com/1Cu5cqfBviI?e=095cb98613&c2id=a206dcecbc185bd9a5d5a46b47a3996f",
    }
    for key, expected_url in expected_brochure.items():
        actual = by_building_floor.get(key)
        if actual != expected_url:
            failures.append(f"{filename}: {key} expected Brochure PDF {expected_url!r}, got {actual!r}")

    if floor_plan_count == 14 and distinct_floor_plans == 14 and high_res_count == 0 and brochure_count == 14 and distinct_brochures == 14:
        print(
            f"OK  {filename}: Floor Plan and Brochure PDF each populated with {distinct_floor_plans}/{distinct_brochures} "
            f"distinct URLs for all {len(records)} rows, High Res Images correctly blank"
        )


def check_gpe_high_res_images(failures):
    """Targeted regression test for a real bug that already shipped
    *twice*, plus a real assumption that turned out wrong a third time:
    first, GPE's rule never extracted High Res Images at all, despite the
    source email genuinely containing real per-building marketing photos
    (confirmed by actually viewing several - real building photos, unlike
    MetSpace's, which is why this checks High Res Images specifically and
    not Floor Plan). Then the fix itself had an off-by-one: it attributed
    each building's real photo to the *next* building instead, which a
    naive ">= N populated" count entirely failed to catch, since 11 of 15
    rows were still "populated" — just with the wrong photo. Then a third
    bug: the fix assumed every building has exactly one photo shared
    across all its floors, but visual confirmation against the actual
    email showed some buildings (those also featured in the promotional
    blurbs earlier in the email, not just their own "CURRENT AVAILABILITY"
    listing card) genuinely have TWO distinct real photos, not one.

    This rule now stashes candidate photo URLs on "_high_res_candidates"
    (app.py turns 1 candidate into a direct High Res Images link, 2+ into
    a small gallery page — see _finalize_high_res_images) rather than
    setting High Res Images itself, so this checks the candidates
    directly rather than routing through app.py's finalizer. It checks
    actual per-building correctness, not just a count: every distinct
    building must map to its own distinct candidate set (no collisions
    between buildings), and the known-correct counts for this exact
    example file — re-verified by actually viewing every photo in the
    source email — must hold exactly."""
    filename = "Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "GPE" or not records:
        failures.append(f"{filename}: expected rule 'GPE' with records, got '{rule_name}'")
        return

    floor_plan_count = sum(1 for r in records if (r.get("Floor Plan") or "").strip())
    # Floor Plan should stay blank for GPE: no separate floor-plan-labeled
    # image or link exists anywhere in this source - populating it would
    # be fabricating a distinction the source doesn't actually have.
    if floor_plan_count != 0:
        failures.append(
            f"{filename}: expected Floor Plan blank for all rows (no separate floor-plan resource exists in "
            f"GPE's source), got {floor_plan_count}/{len(records)} populated"
        )

    # Known-correct for this exact example file, re-verified by actually
    # viewing every photo in the source email: 5 buildings have exactly 1
    # real photo (their own "CURRENT AVAILABILITY" listing-card photo),
    # 4 buildings (also featured in the promotional blurbs earlier in the
    # email) have exactly 2 distinct real photos - 13 distinct images
    # total, none missing, none duplicated across buildings.
    EXPECTED_CANDIDATE_COUNT = {
        "16 Dufour's Place": 1,
        "City Tower": 1,
        "166 Piccadilly": 1,
        "Kent House": 1,
        "Elm Yard": 1,
        "170 Piccadilly": 2,
        "Thirty One Alfred Place": 2,
        "Nineteen Wells Street": 2,
        "Elsley": 2,
    }

    candidates_by_building = {}
    mismatched = []
    missing = []
    for r in records:
        building = r.get("Building")
        candidates = r.get("_high_res_candidates") or []
        if not candidates:
            missing.append(building)
            continue
        if building in candidates_by_building and candidates_by_building[building] != tuple(candidates):
            mismatched.append(building)
        candidates_by_building[building] = tuple(candidates)

    if missing:
        failures.append(
            f"{filename}: expected every row to have at least one real High Res Images candidate, but these had "
            f"none: {missing} — GPE's building-photo extraction may be broken again"
        )
    if mismatched:
        failures.append(
            f"{filename}: these buildings had inconsistent High Res Images candidates across their own floor rows: "
            f"{mismatched} — a listing's own multiple floors should all share the same building photo(s)"
        )

    # The check that would have caught the off-by-one bug: every distinct
    # building must map to a *distinct* candidate set (no two different
    # buildings sharing a photo) - a naive count-based check can't detect
    # a photo silently attributed to the wrong (adjacent) building.
    all_photos = [url for urls in candidates_by_building.values() for url in urls]
    distinct_photos = set(all_photos)
    if len(all_photos) != len(distinct_photos):
        failures.append(
            f"{filename}: {len(all_photos)} total candidate photo(s) across all buildings but only "
            f"{len(distinct_photos)} distinct URLs — at least one photo is being shared between two buildings "
            "that should each have their own"
        )

    # The check that would have caught the one-photo-per-building
    # assumption: some buildings genuinely have 2 distinct photos, not 1.
    count_mismatches = []
    for building, expected in EXPECTED_CANDIDATE_COUNT.items():
        actual = len(candidates_by_building.get(building, ()))
        if actual != expected:
            count_mismatches.append(f"{building}: expected {expected}, got {actual}")
    if count_mismatches:
        failures.append(
            f"{filename}: unexpected High Res Images candidate count(s) for these buildings: {count_mismatches}"
        )

    if (
        not missing
        and not mismatched
        and len(all_photos) == len(distinct_photos)
        and not count_mismatches
        and floor_plan_count == 0
    ):
        print(
            f"OK  {filename}: High Res Images candidates correctly populated and distinct for all "
            f"{len(candidates_by_building)} buildings ({len(records)} rows, {len(distinct_photos)} distinct photos), "
            "Floor Plan correctly blank"
        )


def check_knotel_records(failures):
    """Targeted regression test for extraction.rules.knotel, pinning known-
    correct Building text — including the real postcode — directly from
    the raw source text of both example emails. Not just "is a postcode
    present" but "is it the RIGHT one": a real, shipped regression had
    every Knotel row's Building silently reduced to just the bare
    marketing name (e.g. "Hallmark", "Gilray House") with the source's own
    real full-street-address-plus-postcode line discarded entirely, even
    though the earlier "min record count" check in EXPECTATIONS above kept
    passing the whole time (right count, wrong content) — confirmed
    against a real live email where the source genuinely has a full
    address for every single listing, so this rule should never need to
    fall back to bare-name geocoding at all.

    Also guards against a second, sharper bug from the same root cause:
    the old "is this a fresh building" gate required the address line to
    contain a full two-part postcode, which silently failed for a
    building whose address only carries a partial/outward-only postcode
    ("Market Exchange" at "8 Macklin Street, Covent Garden WC2", no inward
    code) — current_building then never updated away from the previous
    building ("33 Soho"), so that floor was attributed to entirely the
    wrong building. A naive count or "Building is non-empty" check can't
    catch this (both buildings' rows were still "populated", just with
    one of them wrong) — checking by Floor/Unit (a stable per-row
    identifier the source itself provides) against the expected Building
    catches it directly, the same way check_gpe_high_res_images above
    catches a photo misattributed to the *next* building instead of a
    missing one.

    Also pins three more real fields, each with its own real-world catch:
    Contacts (Knotel gives no individual broker name — only a shared team
    contact in its intro paragraph — must resolve to the real email/phone
    there, not the recipient's own forwarding signature or the forwarded
    email's From:/To: header addresses, both of which sit right next to
    it in the raw text); Special Features (a real per-floor price-drop
    note in the 13/07 email must land on exactly the two 15 Hatfields
    rows it names and nowhere else — checked against both fixtures so a
    regression that leaked it onto an unrelated row, or dropped it
    entirely, both fail); and Brochure PDF, which now prefers a real,
    working knotel.com "View property"/"View Listing" link over "View
    Brochure" itself (confirmed real, 2026-07: "View Brochure" always
    points at pitch.com, a JS-rendered viewer, not a real fetchable
    document) — pins this for Rufus House specifically, which has no
    "View Brochure" button in the source at all but DOES have a real
    "View property" link, so the correct value here is that knotel.com
    link, not blank; also still exercises (indirectly — see
    extraction.rules.knotel._group_items) two real, confirmed
    source-HTML quirks that would otherwise silently drop a link
    entirely — "View brochure" with inconsistent casing for 33 Soho, and
    text/href entirely reversed for 23 Great Titchfield Street."""
    # Pinned by list index — parse() order is deterministic for a fixed,
    # checked-in source file, and several of these buildings share the
    # same Area and Floor/Unit label as each other (e.g. three different
    # City Fringe buildings each have a plain "2nd Floor" listing), so
    # index position is a more reliable identifier here than either field
    # alone. Only the indices relevant to a reported regression are
    # pinned, not all 16/15 rows.
    KNOTEL_CONTACTS = "Knotel Brokers, londonbrokers@knotel.com, 0204 571 4271"
    checks = [
        (
            "Fw_ Knotel Availability _ 30_06_2026.eml",
            16,
            {
                2: dict(
                    building="The Hallmark Building, 106 Fenchurch St, London EC3M 5JE",
                    floor="Hallmark 6th Floor",
                    postcode="EC3M 5JE",
                    brochure="https://knotel.com/offices/london/hallmark/u/hallmark-6th-floor",
                ),
                4: dict(
                    building="Classic House, 174-180 Martha's Buildings, Old St, London EC1V 9BP",
                    floor="2nd Floor",
                    postcode="EC1V 9BP",
                    brochure="https://knotel.com/offices/london/classic-house/u/2nd-floor",
                ),
                5: dict(
                    building="Gilray House, 146-150 City Rd, London EC1V 2RL",
                    floor="3rd Floor",
                    postcode="EC1V 2RL",
                    brochure="https://knotel.com/offices/london/gilray-house/u/3rd-floor",
                ),
                6: dict(
                    building="Gilray House, 146-150 City Rd, London EC1V 2RL",
                    floor="4th Floor",
                    postcode="EC1V 2RL",
                    brochure="https://knotel.com/offices/london/gilray-house/u/4th-floor",
                ),
                7: dict(
                    building="Rufus House, 2-4 Rufus St, London N1 6PE",
                    floor="2nd Floor",
                    postcode="N1 6PE",
                    # No "View Brochure" button for this listing at all in
                    # the real source, but it DOES have a real "View
                    # property" knotel.com link — _best_brochure_link
                    # correctly falls through to that instead of leaving
                    # this blank now that a real alternative exists.
                    brochure="https://knotel.com/offices/london/rufus-house/u/2nd-floor",
                ),
                9: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 1st Floor",
                    postcode="SE1 8DJ",
                    brochure="https://knotel.com/offices/london/15-hatfields/u/1stfloor",
                    # No price-drop promo in this older email at all.
                    special_features="",
                ),
                10: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 3rd Floor",
                    postcode="SE1 8DJ",
                    brochure="https://knotel.com/offices/london/15-hatfields/u/3rd-floor",
                    special_features="",
                ),
                11: dict(
                    building="7 Howick Place, 7 Howick Pl, London SW1P 1BB",
                    floor="3rd Floor",
                    postcode="SW1P 1BB",
                    brochure="https://knotel.com/offices/london/7-howick-place/u/3rd-floor",
                ),
                12: dict(
                    building="23 Great Titchfield Street, 23 Great Titchfield St London W1W 7JA",
                    floor="3B",
                    postcode="W1W 7JA",
                    # Real source HTML has this listing's own "View Brochure"
                    # anchor text/href entirely reversed (href literally =
                    # "View Brochure", visible text = the real pitch.com
                    # URL) — _group_items still recovers that correctly, but
                    # _best_brochure_link now prefers this listing's real,
                    # working knotel.com "View property" link over it either
                    # way (confirmed real, 2026-07: "View Brochure" always
                    # points at pitch.com, a JS-rendered viewer, not a real
                    # fetchable document).
                    brochure="https://knotel.com/offices/london/great-titchfield-street/u/3b",
                ),
                14: dict(
                    building="Market Exchange, 8 Macklin Street, Covent Garden WC2",
                    floor="2nd - East Wing",
                    postcode="",
                    brochure="https://knotel.com/offices/london/market-exchange/u/part-2nd",
                ),
            },
        ),
        (
            "Fw_ Knotel Availability _ 13_07_2026.eml",
            15,
            {
                9: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 1st Floor",
                    postcode="SE1 8DJ",
                    brochure="https://knotel.com/offices/london/15-hatfields/u/1stfloor",
                    # The real promo note for this exact row/price — must
                    # match the "1st Floor" price, not the "3rd Floor" one.
                    special_features="Price drop: now £120 psf",
                ),
                10: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 3rd Floor",
                    postcode="SE1 8DJ",
                    brochure="https://knotel.com/offices/london/15-hatfields/u/3rd-floor",
                    special_features="Price drop: now £115 psf",
                ),
                # The exact regression case: two adjacent West End listings
                # with genuinely different buildings — "33 Soho" must not
                # leak onto the "Market Exchange" row that follows it. Also
                # covers the "View brochure" (lowercase b) casing quirk
                # (_group_items still recovers it correctly internally, even
                # though _best_brochure_link now prefers this listing's real
                # knotel.com "View property" link over it either way).
                13: dict(
                    building="33 soho square, W1D 3QU",
                    floor="2nd Floor",
                    postcode="W1D 3QU",
                    brochure="https://knotel.com/offices/london/33-soho-square/u/2nd-floor",
                ),
                14: dict(
                    building="Market Exchange, 8 Macklin Street, Covent Garden WC2",
                    floor="2nd - East Wing",
                    # This building's own address only ever gives a partial,
                    # outward-only postcode ("WC2", no inward part) — "" is
                    # the honest, correct extraction here, not a bug.
                    postcode="",
                    brochure="https://knotel.com/offices/london/market-exchange/u/part-2nd",
                ),
            },
        ),
    ]

    for filename, expected_count, expected_by_index in checks:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue

        content = read_file(path)
        rule_name, records = try_rules(content)
        if rule_name != "Knotel" or not records:
            failures.append(f"{filename}: expected rule 'Knotel' with records, got '{rule_name}'")
            continue
        if len(records) != expected_count:
            failures.append(f"{filename}: expected {expected_count} records, got {len(records)}")

        normalized = [normalize_record(r) for r in records]
        local_failures = []
        for idx, expected in expected_by_index.items():
            if idx >= len(normalized):
                local_failures.append(f"{filename}: expected a record at index {idx}, only {len(normalized)} present")
                continue
            row = normalized[idx]
            expected_floor = expected["floor"]
            if row["Floor/Unit"] != expected_floor:
                local_failures.append(
                    f"{filename}: record {idx} expected Floor/Unit {expected_floor!r}, got {row['Floor/Unit']!r} "
                    "(fixture may have changed, or record ordering shifted)"
                )
            if row["Building"] != expected["building"]:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Building {expected['building']!r}, got {row['Building']!r}"
                )
            if row["Property Postcode"] != expected["postcode"]:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Property Postcode {expected['postcode']!r}, "
                    f"got {row['Property Postcode']!r}"
                )
            if row["Brochure PDF"] != expected["brochure"]:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Brochure PDF {expected['brochure']!r}, "
                    f"got {row['Brochure PDF']!r}"
                )
            expected_special_features = expected.get("special_features")
            if expected_special_features is not None and row["Special Features"] != expected_special_features:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Special Features {expected_special_features!r}, "
                    f"got {row['Special Features']!r}"
                )

        # Every row (regardless of index) shares the same whole-email
        # contact — Knotel gives no individual broker name, just a shared
        # team email/phone in the intro paragraph.
        contacts_values = {r["Contacts"] for r in normalized}
        if contacts_values != {KNOTEL_CONTACTS}:
            local_failures.append(f"{filename}: expected Contacts == {KNOTEL_CONTACTS!r} for every row, got {contacts_values}")

        # Assigned Agents is a NAME-ONLY subset of Contacts, not a
        # duplicate of it — must drop the email/phone that Contacts
        # itself correctly keeps.
        assigned_agents_values = {r["Assigned Agents"] for r in normalized}
        if assigned_agents_values != {"Knotel Brokers"}:
            local_failures.append(
                f"{filename}: expected Assigned Agents == 'Knotel Brokers' (name only, no email/phone) for every row, "
                f"got {assigned_agents_values}"
            )

        # Every OTHER row (not one of the two 15 Hatfields price-drop rows
        # above) must have blank Special Features — guards against the
        # promo note leaking onto an unrelated building/floor.
        unexpected_special_features = {
            (i, r["Building"], r["Floor/Unit"]): r["Special Features"]
            for i, r in enumerate(normalized)
            if r["Special Features"] and expected_by_index.get(i, {}).get("special_features") != r["Special Features"]
        }
        if unexpected_special_features:
            local_failures.append(f"{filename}: unexpected Special Features on rows not covered by the price-drop promo: {unexpected_special_features}")

        # Knotel is a lettings-only source — it never sets a Sale Price
        # signal (see extraction.schema.normalize_record), so For Sale
        # must stay "No" for every row even when the email's own
        # promotional copy mentions a price drop (confirmed against the
        # real "** PRICE DROP AT 15 HATFIELDS **" banner in both example
        # emails) — a promotional/discount mention is not a sale signal.
        for_sale_values = {r["For Sale"] for r in normalized}
        if for_sale_values != {"No"}:
            local_failures.append(
                f"{filename}: expected For Sale == 'No' for every row (lettings-only source), got {for_sale_values}"
            )

        if not local_failures:
            print(f"OK  {filename}: {len(records)} Knotel records spot-checked against known-correct source values")
        failures.extend(local_failures)


def check_street_address_only(failures):
    """Targeted regression test for extraction.schema.street_address_only —
    pins real Building values pulled directly from every example source
    (not hand-invented strings) against the exact expected "Building
    Name, Street Number Street Name" result (name kept when one genuinely
    exists, postcode never kept), plus the illustrative Crown Estate-style
    "Princes House, 38 Jermyn Street, SW1Y" case from extraction.pipeline's
    own docstring. Deliberately a pure function-level check (no file I/O
    needed for most cases) since street_address_only never touches
    geocoding — see its own docstring for why it's called from
    extraction.pipeline.process_files only AFTER geocoding has already
    run, not from schema.normalize_record."""
    cases = [
        # Knotel — the "Name, Street, London POSTCODE" shape (both name
        # and street real, no overlap between them) — name is kept.
        ("Gilray House, 146-150 City Rd, London EC1V 2RL", "Gilray House, 146-150 City Rd"),
        ("The Hallmark Building, 106 Fenchurch St, London EC3M 5JE", "The Hallmark Building, 106 Fenchurch St"),
        ("Rufus House, 2-4 Rufus St, London N1 6PE", "Rufus House, 2-4 Rufus St"),
        # Knotel — "Street, POSTCODE" shape (no separate name at all) —
        # nothing to combine, just the street.
        ("2 Leonard Circus, EC2A 4LW", "2 Leonard Circus"),
        # Knotel — 3-comma-segment shape where the last segment (the real
        # street) has no digit of its own ("Old St") but an earlier one
        # does ("174-180 Martha's Buildings") — the digit-bearing segment
        # is kept, "Old St" is dropped, and the name before it ("Classic
        # House") IS kept since it precedes the digit-bearing segment.
        (
            "Classic House, 174-180 Martha's Buildings, Old St, London EC1V 9BP",
            "Classic House, 174-180 Martha's Buildings",
        ),
        # Knotel — no comma at all before "London POSTCODE".
        (
            "23 Great Titchfield Street, 23 Great Titchfield St London W1W 7JA",
            "23 Great Titchfield Street, 23 Great Titchfield St",
        ),
        # Knotel — the real regression case that motivated the digit-
        # preference rule in the first place: the marketing name ("6
        # Maiden Lane") IS the real street+number, while the address line
        # itself only ever gives a neighbourhood name with no number
        # ("Covent Garden") — the digit-bearing segment IS the first one
        # here, so there's nothing before it to keep as a separate name;
        # must not end up with "Covent Garden" (a neighbourhood, not a
        # street) attached at all.
        ("6 Maiden Lane, Covent Garden, WC2E 7ND", "6 Maiden Lane"),
        # Knotel — a neighbourhood name combined with only a PARTIAL
        # (outward-only) postcode in the very same segment, no comma
        # between them at all ("Covent Garden WC2") — name kept, the
        # neighbourhood+partial-postcode segment dropped entirely.
        ("Market Exchange, 8 Macklin Street, Covent Garden WC2", "Market Exchange, 8 Macklin Street"),
        # Breezblok — same "Name, Street, London POSTCODE" shape as Knotel.
        ("John Stow House, 18 Bevis Marks, London EC3A 7JB", "John Stow House, 18 Bevis Marks"),
        # Knotel — the digit-bearing segment is the FIRST one (a
        # marketing name that's itself a real street+number), with a
        # later non-numbered alternate name ("Chadwick Court") that's
        # correctly dropped rather than force-combined.
        ("15 Hatfields, Chadwick Court, London SE1 8DJ", "15 Hatfields"),
        # GPE/MetSpace/BC — Building is already just a street name/number
        # with no postcode or separate marketing name at all (nothing to
        # strip) — must pass through completely unchanged.
        ("9-10 Market Place", "9-10 Market Place"),
        ("170 Piccadilly", "170 Piccadilly"),
        ("10-12 Alie Street", "10-12 Alie Street"),
        # GPE/BC — a bare marketing name with no separate address info in
        # the source at all (no digit, no postcode) — the "should be
        # rare" case where nothing better is available; must not be
        # blanked out, just passed through as the best available text.
        ("Elsley", "Elsley"),
        ("Porters Place", "Porters Place"),
        # Kitt's — "Name, Street" shape with no postcode at all — name kept.
        ("The Hide, 3 Kingly Court", "The Hide, 3 Kingly Court"),
        ("Bridge House, 22 Newman Street", "Bridge House, 22 Newman Street"),
        # Illustrative Crown Estate/LLM-fallback-style example from
        # extraction.pipeline's own module docstring.
        ("Princes House, 38 Jermyn Street, SW1Y", "Princes House, 38 Jermyn Street"),
    ]
    local_failures = [
        f"street_address_only({building!r}) expected {expected!r}, got {street_address_only(building)!r}"
        for building, expected in cases
        if street_address_only(building) != expected
    ]
    if not local_failures:
        print(f"OK  street_address_only: {len(cases)} real Building values spot-checked against known-correct results")
    failures.extend(local_failures)


def check_names_only(failures):
    """Targeted regression test for extraction.schema.names_only, which
    extraction.schema.normalize_record uses to derive Assigned Agents
    from Contacts — must strip an email/phone number out of a combined
    contact string (Knotel's real "Knotel Brokers, londonbrokers@
    knotel.com, 0204 571 4271" -> "Knotel Brokers") while leaving a
    Contacts value that's already just name(s) completely unchanged
    (Kitt's real "Leah Noray, Ben Danaher", Breezblok's real "Sales")."""
    cases = [
        ("Knotel Brokers, londonbrokers@knotel.com, 0204 571 4271", "Knotel Brokers"),
        ("Leah Noray, Ben Danaher", "Leah Noray, Ben Danaher"),
        ("Kieran Christie, Sophie Haugh, Nicki Mayle", "Kieran Christie, Sophie Haugh, Nicki Mayle"),
        ("Sales", "Sales"),
        ("", ""),
    ]
    local_failures = [
        f"names_only({contacts!r}) expected {expected!r}, got {names_only(contacts)!r}"
        for contacts, expected in cases
        if names_only(contacts) != expected
    ]
    if not local_failures:
        print(f"OK  names_only: {len(cases)} real Contacts values spot-checked against known-correct results")
    failures.extend(local_failures)


def check_geocode_same_building_ambiguity(failures):
    """Targeted regression test for extraction.geocode._check_ambiguity's
    SAME_BUILDING_DISTANCE_KM check — a 2026-07 MetSpace audit found real,
    independently cross-checked wrong postcodes for two buildings ("9-10
    Market Place" showed W1W 8AE, really W1W 8AQ per Savills/CBRE/
    Workthere/Hubble/Rightmove; "1 Curtain Road" showed EC2A 3NY, really
    EC2A 3JX per a direct commercial listing) caused by a single Nominatim
    query returning two genuinely different OSM nodes — one per unit/
    entrance of the SAME building (an office node and a restaurant/bar
    node) — just a few metres apart with different postcodes, which the
    existing far-apart AMBIGUITY_DISTANCE_KM check (built for a
    completely different failure mode — Nominatim returning a confident
    match many km away in an unrelated borough) can't catch at all.
    Pure-function unit test (no live network needed) against the real
    coordinates/postcodes Nominatim actually returned for both cases,
    plus a close-same-postcode case (must NOT trip) and a far-apart case
    (must still trip the original, unrelated check)."""
    cases = [
        # Real case: "9-10 Market Place" — office node vs. restaurant node
        # ~1m apart, different postcodes. Must be flagged ambiguous.
        (51.5164430, -0.1402766, "W1W 8AE", [(51.5164451, -0.1402630, "W1W 8AQ")], True),
        # Real case: "1 Curtain Road" — office node vs. bar node ~5m apart,
        # different postcodes. Must be flagged ambiguous.
        (51.5221940, -0.0811526, "EC2A 3NY", [(51.5221450, -0.0811478, "EC2A 3JX")], True),
        # Close candidates that agree on postcode — same building, no real
        # conflict — must NOT be flagged.
        (51.5164430, -0.1402766, "W1W 8AE", [(51.5164451, -0.1402630, "W1W 8AE")], False),
        # Far apart (the original, unrelated failure mode) — must still trip.
        (51.5127184, -0.1412715, "W1S 2YZ", [(51.4, -0.3, "N9 0AA")], True),
        # Close candidates where the second has no postcode at all —
        # nothing to disagree with, must NOT be flagged.
        (51.5164430, -0.1402766, "W1W 8AE", [(51.5164451, -0.1402630, "")], False),
    ]
    local_failures = []
    for lat, lng, postcode, other_candidates, expect_ambiguous in cases:
        error = geocode_module._check_ambiguity(lat, lng, postcode, other_candidates)
        is_ambiguous = error is not None
        if is_ambiguous != expect_ambiguous:
            local_failures.append(
                f"_check_ambiguity({lat}, {lng}, {postcode!r}, {other_candidates}) expected "
                f"ambiguous={expect_ambiguous}, got ambiguous={is_ambiguous} (error={error!r})"
            )
    if not local_failures:
        print(f"OK  geocode same-building ambiguity: {len(cases)} cases (2 real, cross-checked) spot-checked")
    failures.extend(local_failures)


def check_xlsx_links_for_llm_fallback(failures):
    """Targeted regression test for extraction.xlsx_links — the generic
    Brochure PDF/Floor Plan enrichment for a raw-spreadsheet (.xlsx/.xls)
    source with no dedicated rule, going through the LLM fallback
    instead (the .xlsx counterpart to extraction.html_images for .eml/
    .html and app._attach_pdf_images for PDF).

    Confirmed real bug (2026-07, a UNION .xlsx with no dedicated rule):
    its own "Brochure" column links every row to a real box.com URL
    through a hyperlink on a generic display cell — pandas' own
    cell-value read (used to build the LLM's own plain-text prompt
    input) discards hyperlinks entirely, so Brochure PDF/Floor Plan came
    back blank for every row despite real links existing in the source.
    Fixed by extraction.file_readers._extract_xlsx_row_links capturing
    per-row hyperlinks directly via openpyxl, and this module matching
    each extracted record back to its own source row by Building-name
    search.

    Also confirmed real (same file): a second bug in the FIX itself —
    the actual source has "Nexus Place -  25 Farringdon Place" (two
    spaces after the dash), but the LLM's own extracted Building field
    normalizes that to a single space, so a naive substring check missed
    this row's real link entirely, even though it existed. Row 4 below
    pins that exact real case.

    Also confirmed real (2026-07, The Workplace Company): a source can
    give TWO separate link candidates per row under different labels —
    its own "Brochure" column linked to Canva (a JS-rendered viewer,
    confirmed unusable — fetched directly, it returns an HTML page, not
    real PDF bytes), while a separate "Website" column linked to the
    company's own domain, which works. The row order (Brochure column
    before Website column) previously meant the first-seen, unusable
    Canva link always won. Row 6 below pins that exact real case —
    Canva must be skipped in favor of the real domain.

    Pure function test against hand-built row_links shaped exactly like
    extraction.file_readers._extract_xlsx_row_links' real output — no
    live LLM/network call needed."""
    row_links = [
        {"row_text": "107 Cannon Street | 4th | Fitted | 2248", "links": [("CLICK HERE", "https://example.com/107-4th")]},
        {"row_text": "107 Cannon Street | 1st | Fitted | 1900", "links": [("CLICK HERE", "https://example.com/107-1st")]},
        {"row_text": "155 Fenchurch Street | 7th | CAT A | 3000", "links": [("FLOOR PLAN", "https://example.com/155-plan")]},
        # Real case: a double space in the source ("-  25") that the LLM's
        # own Building field normalizes to a single space ("- 25").
        {"row_text": "Nexus Place -  25 Farringdon Place | 5th", "links": [("Landlord Brochure", "https://example.com/nexus")]},
        # No hyperlink at all in this row — must be left alone, not error.
        {"row_text": "100 Lower Thames Street | 5th | CAT A | 5178", "links": []},
        # Real case: Brochure (Canva, low-trust) listed BEFORE Website
        # (the company's own domain) — must still prefer the website.
        {
            "row_text": "1 Valentine Place, London, SE1 8QH | 1,528 - 5,028",
            "links": [
                ("Brochure", "https://canva.link/jdox0d7g37f1aob"),
                ("Website", "https://www.theworkplacecompany.co.uk/offices-to-rent/london/1-valentine-place"),
            ],
        },
    ]
    records = [
        {"Building": "107 Cannon Street", "Floor/Unit": "4th", "Floor Plan": "", "Brochure PDF": ""},
        {"Building": "107 Cannon Street", "Floor/Unit": "1st", "Floor Plan": "", "Brochure PDF": ""},
        {"Building": "155 Fenchurch Street", "Floor/Unit": "7th", "Floor Plan": "", "Brochure PDF": ""},
        {"Building": "Nexus Place - 25 Farringdon Place", "Floor/Unit": "5th", "Floor Plan": "", "Brochure PDF": ""},
        {"Building": "100 Lower Thames Street", "Floor/Unit": "5th", "Floor Plan": "", "Brochure PDF": ""},
        {"Building": "1 Valentine Place, London, SE1 8QH", "Floor/Unit": "", "Floor Plan": "", "Brochure PDF": ""},
    ]
    xlsx_links.enrich_records(records, row_links)

    local_failures = []
    expected = [
        ("Brochure PDF", "https://example.com/107-4th", None),
        ("Brochure PDF", "https://example.com/107-1st", None),
        # FLOOR PLAN-labeled landlord links fill Floor Plan AND Brochure PDF
        # (shared xlsx_links.associate_row_links dual-fill).
        ("Floor Plan", "https://example.com/155-plan", "https://example.com/155-plan"),
        ("Brochure PDF", "https://example.com/nexus", None),
        (None, None, None),
        ("Brochure PDF", "https://www.theworkplacecompany.co.uk/offices-to-rent/london/1-valentine-place", None),
    ]
    for record, (field, url, also_brochure) in zip(records, expected):
        if field is None:
            if record.get("Floor Plan") or record.get("Brochure PDF"):
                local_failures.append(
                    f"{record['Building']!r}: expected no Floor Plan/Brochure PDF (source row has no link), "
                    f"got Floor Plan={record.get('Floor Plan')!r} Brochure PDF={record.get('Brochure PDF')!r}"
                )
            continue
        if record.get(field) != url:
            local_failures.append(f"{record['Building']!r} ({record['Floor/Unit']}): expected {field} {url!r}, got {record.get(field)!r}")
        if also_brochure is not None:
            if record.get("Brochure PDF") != also_brochure:
                local_failures.append(
                    f"{record['Building']!r} ({record['Floor/Unit']}): expected Brochure PDF {also_brochure!r} "
                    f"(floor-plan dual-fill), got {record.get('Brochure PDF')!r}"
                )
        else:
            other_field = "Brochure PDF" if field == "Floor Plan" else "Floor Plan"
            if record.get(other_field):
                local_failures.append(
                    f"{record['Building']!r} ({record['Floor/Unit']}): expected {other_field} to stay blank, got {record.get(other_field)!r}"
                )

    if not local_failures:
        print(
            "OK  xlsx_links: 2 same-building rows each get their OWN link, a floor-plan link dual-fills "
            "Brochure PDF, a real double-space mismatch resolved, a linkless row left blank, a Canva "
            "candidate skipped in favor of the real company domain"
        )
    failures.extend(local_failures)


def check_low_trust_link_domain(failures):
    """Targeted regression test for extraction.html_images.
    is_low_trust_link_domain — the domain classifier behind xlsx_links'
    Brochure PDF fix above (and reusable anywhere else a caller has
    multiple candidate links for the same listing). Confirmed real
    (2026-07): Box.com, Pitch.com, and Canva are each a JS-rendered
    viewer page when fetched directly (real HTTP response comes back as
    text/html, never actual PDF bytes) — checked directly against the
    real UNION, Knotel, and Workplace Company sources respectively."""
    cases = [
        ("https://canva.link/jdox0d7g37f1aob", True),
        ("https://www.canva.com/design/abc123/view", True),
        ("https://app.pitch.com/app/presentation/abc", True),
        ("https://pitch.com/v/1-finsbury-brochure-4jnj9d", True),
        ("https://app.box.com/s/5ln9uri46xhq586qdoskbc37rhrrftr7", True),
        ("https://www.theworkplacecompany.co.uk/offices-to-rent/london/abc", False),
        ("https://knotel.com/offices/london/1-finsbury-market", False),
        ("", False),
    ]
    local_failures = []
    for url, expected in cases:
        result = html_images.is_low_trust_link_domain(url)
        if result != expected:
            local_failures.append(f"is_low_trust_link_domain({url!r}) expected {expected!r}, got {result!r}")
    if not local_failures:
        print(f"OK  low-trust link domain: {len(cases)} cases (Canva/Pitch/Box vs 2 real company domains) spot-checked")
    failures.extend(local_failures)


def check_rule_sanity_check_fallback(failures):
    """Targeted regression test for extraction.rule_sanity — the general
    safety net added on request after a real incident (2026-07): MetSpace
    sent a second, structurally different email template ("Office Of The
    Week", a single-listing spotlight, real fixture below) that
    extraction.rules.metspace was never built for. detect() correctly
    recognized the sender, but parse()'s own area-header anchor logic
    found no area line to trim the buffered text on, so the ENTIRE email
    signature/header/legal-disclaimer text ahead of the one real
    "Sqft:" line ended up verbatim in Building — thousands of characters
    of boilerplate in a field meant to hold a building name, silently
    accepted because parse() ran without raising.

    Covers both layers: (1) extraction.rule_sanity.records_look_plausible
    directly, pure function, against a hand-built oversized/boilerplate
    record and a genuinely normal one (must NOT false-positive on real
    data); (2) extraction.rules.try_rules against the REAL "Office Of
    The Week" fixture end to end — confirms the whole rule (not just
    parse(), which previously "succeeded" with garbage) now returns the
    dedicated, validated single-listing template result rather than
    accepting boilerplate or requiring the LLM. The normal-file
    non-false-positive counterpart to this (MetSpace's OWN usual
    template still matching and returning real records) is already
    covered by main()'s own EXPECTATIONS loop, run every time this
    suite does — not duplicated here."""
    local_failures = []

    plausible_cases = [
        ([{"Building": "9-10 Market Place", "Area": "West End", "Floor/Unit": "3rd"}], True),
        (
            [{"Building": "X" * 500, "Area": "West End", "Floor/Unit": "3rd"}],
            False,
        ),
        (
            [{"Building": "IMPORTANT: This e-mail is intended for the named recipient only.", "Area": "", "Floor/Unit": ""}],
            False,
        ),
        (
            [{"Building": "44 Pentonville Road", "Contacts": "Sent: 14 July 2026 11:24 To: someone@example.com"}],
            False,
        ),
    ]
    for records, expected in plausible_cases:
        result = rule_sanity.records_look_plausible(records)
        if result != expected:
            local_failures.append(f"records_look_plausible({records!r}) expected {expected!r}, got {result!r}")

    filename = "Fw_ MetSpace - Office Of The Week!.eml"
    path = ROOT / filename
    if not path.exists():
        local_failures.append(f"{filename}: example file not found (expected at {path})")
    else:
        content = read_file(path)
        rule_name, records = try_rules(content)
        if rule_name != "MetSpace" or not records or len(records) != 1 or records[0].get("Building") != "44 Pentonville Road":
            local_failures.append(
                f"{filename}: expected the corrected MetSpace template parser to return one clean 44 Pentonville Road record, "
                f"got rule_name={rule_name!r} with "
                f"{len(records) if records else 0} record(s)"
            )

    if not local_failures:
        print(
            "OK  rule sanity check: 4 plausibility cases (1 normal, 3 implausible) spot-checked, real "
            "'Office Of The Week' fixture now returns one clean deterministic record"
        )
    failures.extend(local_failures)


def check_area_disambiguated_output_names(failures):
    """Targeted regression test for extraction.naming's area/subset
    disambiguation, added on request: UNION exports the same provider
    name and date as several separate area-based files in one sitting
    (City, City 2, Aldgate & Whitechapel, Shoreditch, ...), so provider
    + date alone produced indistinguishable output filenames for
    genuinely different files.

    Covers all three priority tiers together: (1) extract_area_hint from
    the original filename's own trailing " - <area>" segment — including
    the real "City 2" case, which must NOT be rejected just for ending
    in a digit (only an actual date-shaped fragment like "June 26"
    should be); (2) area_from_records, when the filename gives no hint
    but every extracted row shares one Area value; (3) make_unique_names'
    numeric "(2)"/"(3)" fallback, now only reached when provider+area+
    date are ALL still identical.

    Verified live against 3 real UNION files with different area names
    (City 2, Aldgate & Whitechapel, Shoreditch) through the real running
    pipeline — this is the offline pure-function counterpart, covering
    the tiers that real run didn't happen to exercise (Area-field
    consensus, numeric fallback)."""
    local_failures = []

    filename_cases = [
        ("UNION - Availability - June 26 - City.xlsx", "UNION", "City"),
        ("UNION - Availability - June 26 - City 2.xlsx", "UNION", "City 2"),
        ("UNION  - Availability - June 26 - Aldgate & Whitechapel.xlsx", "UNION", "Aldgate & Whitechapel"),
        ("UNION - Availability - June 26 - Shoreditch.xlsx", "UNION", "Shoreditch"),
        # No " - "-separated area segment at all — must return None, not
        # mistake the whole cleaned-up name for an "area".
        ("Fw_ MetSpace Availability Update.eml", "MetSpace", None),
        # Only generic/date/provider segments present — must return None.
        ("UNION - Availability - June 26.xlsx", "UNION", None),
    ]
    for filename, provider_name, expected in filename_cases:
        result = naming_module.extract_area_hint(filename, provider_name)
        if result != expected:
            local_failures.append(f"extract_area_hint({filename!r}, {provider_name!r}) expected {expected!r}, got {result!r}")

    area_records_cases = [
        ([{"Area": "Shoreditch"}, {"Area": "Shoreditch"}, {"Area": "Shoreditch"}], "Shoreditch"),
        ([{"Area": "City"}, {"Area": "Shoreditch"}], None),
        ([{"Area": ""}, {"Area": ""}], None),
        # A record with no Area value at all doesn't break consensus — it
        # carries no information either way, unlike a genuinely different
        # non-empty value (the mixed-areas case above).
        ([{"Area": "City"}, {}], "City"),
    ]
    for records, expected in area_records_cases:
        result = naming_module.area_from_records(records)
        if result != expected:
            local_failures.append(f"area_from_records({records!r}) expected {expected!r}, got {result!r}")

    unique_names_cases = [
        (["UNION - City_2026-07-14", "UNION - Shoreditch_2026-07-14"], ["UNION - City_2026-07-14", "UNION - Shoreditch_2026-07-14"]),
        (["MetSpace_2026-07-14", "MetSpace_2026-07-14"], ["MetSpace_2026-07-14", "MetSpace_2026-07-14 (2)"]),
        (
            ["MetSpace_2026-07-14", "MetSpace_2026-07-14", "MetSpace_2026-07-14"],
            ["MetSpace_2026-07-14", "MetSpace_2026-07-14 (2)", "MetSpace_2026-07-14 (3)"],
        ),
    ]
    for names, expected in unique_names_cases:
        result = naming_module.make_unique_names(names)
        if result != expected:
            local_failures.append(f"make_unique_names({names!r}) expected {expected!r}, got {result!r}")

    if not local_failures:
        print(
            f"OK  area-disambiguated naming: {len(filename_cases)} filename cases (incl. the real 'City 2' "
            f"digit-suffix case), {len(area_records_cases)} Area-consensus cases, {len(unique_names_cases)} "
            "collision-fallback cases"
        )
    failures.extend(local_failures)


def check_source_filename_disambiguation(failures):
    """Targeted regression test for app._disambiguate_source_filename.

    Confirmed real bug (2026-07, a real UNION file — sent as a raw .xlsx
    with no dedicated rule, going through the LLM fallback): app.py's
    /api/process route reuses the same collision-free `name` for both
    the copied original source artifact and the generated spreadsheet
    (always "{name}.xlsx"). When the ORIGINAL upload is itself .xlsx,
    both resolved to the exact same path in batch_dir — shutil.copy2
    wrote the real source there first, but write_xlsx later in the same
    loop then silently overwrote that exact file with the GENERATED
    spreadsheet. Link to File ended up pointing at a second copy of the
    output spreadsheet instead of the real original document, for every
    row — confirmed live via the real running app: before this fix, the
    hyperlink's own target pointed at "batch_id/UNION.xlsx" (the output)
    for a file named "UNION.xlsx"; after this fix, it correctly points
    at "batch_id/UNION (original).xlsx" (21,238 bytes, byte-for-byte the
    real upload) instead.

    Pure function test — no Flask request or real file needed, since the
    fix is a pure string transform independent of everything else in the
    route."""
    cases = [
        # The real collision case: an .xlsx source resolves to the exact
        # same name as the generated spreadsheet — must be disambiguated.
        ("UNION.xlsx", "UNION.xlsx", "UNION (original).xlsx"),
        # No collision at all (.eml's own extracted-HTML path, or any
        # other source extension that isn't literally .xlsx) — must be
        # left untouched.
        ("MetSpace.html", "MetSpace.xlsx", "MetSpace.html"),
        ("BC Current Availability.pdf", "BC Current Availability.xlsx", "BC Current Availability.pdf"),
        # A name with no extension at all (defensive — not expected in
        # practice, every real source/output name has one) must still
        # get a distinguishing suffix, not silently pass through unchanged.
        ("noext", "noext", "noext (original)"),
    ]
    local_failures = []
    for source_filename, output_filename, expected in cases:
        result = app_module._disambiguate_source_filename(source_filename, output_filename)
        if result != expected:
            local_failures.append(
                f"_disambiguate_source_filename({source_filename!r}, {output_filename!r}) "
                f"expected {expected!r}, got {result!r}"
            )
    if not local_failures:
        print(f"OK  source filename disambiguation: {len(cases)} cases (1 real collision, 3 non-colliding) spot-checked")
    failures.extend(local_failures)


def check_html_images_for_llm_fallback(failures):
    """Targeted regression test for extraction.html_images — the generic
    Floor Plan/High Res Images/Brochure PDF enrichment for a brand-new
    provider's .eml/.html source with no dedicated rule of its own (the
    non-PDF counterpart to app.py's own _attach_pdf_images, which already
    applied generically to any LLM-fallback PDF before this existed).

    Confirmed real bug (2026-07): a brand-new provider (The Workplace
    Company) went through the LLM fallback and came back with ALL of
    Link to File, High Res Images, Floor Plan, and Brochure PDF empty —
    Link to File turned out to already be fine (app.py always overwrites
    it with the persisted source-file link regardless of source), but
    the other three had genuinely NO enrichment step at all for a
    non-PDF LLM-fallback source, despite the source having 4 real listing
    photos and an explicit "Brochure" link.

    This is a pure function-level check against html_items read directly
    from the real source file (not a hand-written fixture) — no live
    Gemini call needed, since extraction.html_images only ever consumes
    html_items and a Building string, never re-derives them."""
    filename = "Fw_ Property of The Week _ City Fringe.eml"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, rule_records = try_rules(content)
    if rule_name is not None:
        failures.append(
            f"{filename}: expected no rule-based parser to match this brand-new provider (it must exercise "
            f"the LLM fallback path this test targets), got rule '{rule_name}'"
        )

    items = content.get("html_items") or []
    if not items:
        failures.append(f"{filename}: expected non-empty html_items from this real .eml source, got none")
        return

    local_failures = []

    # Real, known-correct classification for a representative sample of
    # this source's own images/links — pins the specific alt/domain/
    # extension signals confirmed against this real email, not just "some
    # image got through".
    real_photo_src = "https://gallery.eomail5.com/b2839f96-60af-11f0-9b87-8fe3b9c1184b%2F019f13c4-f173-70f1-b620-007e4f1fb3e0.jpg"
    if not html_images.is_real_content_image("", real_photo_src):
        local_failures.append(f"{filename}: expected a real listing photo (no alt, real gallery domain, .jpg) to be classified as real content")
    for alt, src, reason in [
        ("Company logo", "https://d36urhup7zbd7q.cloudfront.net/a/4bc2a91e.png", "alt contains 'logo'"),
        ("Employee photo", "https://gifo.srv.wisestamp.com/img/abc.png", "wisestamp.com signature-service domain"),
        ("linkedin social link", "https://gallery.eomail5.com/tentacles/icons/v1/social-block/rounded/color/linkedin.png", "alt contains 'social'"),
        ("", "https://eot.theworkplacecompany.com/q/zm1J6qChR3mvXnogwjYaMw~~/AAAHURA", "no recognizable image extension (a tracking pixel)"),
    ]:
        if html_images.is_real_content_image(alt, src):
            local_failures.append(f"{filename}: expected alt={alt!r} src={src!r} to be excluded ({reason}), but was classified as real content")

    if not html_images.is_brochure_link("Brochure"):
        local_failures.append(f"{filename}: expected a link literally named 'Brochure' to be classified as a brochure link")
    if html_images.is_floorplan_link("Brochure"):
        local_failures.append(f"{filename}: did not expect 'Brochure' to also be classified as a floor-plan link")

    # End-to-end against the real html_items: simulate the two real
    # records the LLM actually extracted from this file (both floors of
    # the one real building, "5-7 Ireland Yard") and confirm the single-
    # building tier attaches the real photos/brochure link to both.
    # is_floorplan_image_url does a real network fetch per candidate image
    # (extraction.html_images' own pixel-content check, added 2026-07 for
    # MetSpace's "Office Of The Week" case) — stubbed here to keep this
    # check fast/offline/deterministic like every other one in this suite;
    # already confirmed none of this file's own 4 real photos are actually
    # floor plans (Floor Plan expected blank below), so False is the
    # correct real answer here too, not just a convenient stub.
    original_is_floorplan_image_url = html_images.is_floorplan_image_url
    html_images.is_floorplan_image_url = lambda url: False
    try:
        records = [{"Building": "5-7 Ireland Yard"}, {"Building": "5-7 Ireland Yard"}]
        html_images.enrich_records(records, items)
    finally:
        html_images.is_floorplan_image_url = original_is_floorplan_image_url
    expected_brochure = (
        "https://eot.theworkplacecompany.com/f/a/SjaePlQ3sgIFTBokxwTJWg~~/AAAHURA~/"
        "6VqBvhnc5QlGQszomwEcqpzwgstqcSru4Kr4FwvfNNuDYDjmbdFz7r26trpdM3mbkpiSchIk3Qs5LCw-O3nIE8H8i2Mr0prK1Ni4"
        "nbOMKPjTg7fUeLeEcd5srAl0t7HFMqfpPm31u39SpzUs7nPQ-GuvKaE2HsO_xIamqPbRe4uQ6lgeKkcvxZm3aBGd7Gvj"
    )
    for i, r in enumerate(records):
        candidates = r.get("_high_res_candidates") or []
        if len(candidates) != 4:
            local_failures.append(f"{filename}: record {i} expected 4 real content image candidates, got {len(candidates)}: {candidates}")
        if r.get("Brochure PDF") != expected_brochure:
            local_failures.append(f"{filename}: record {i} expected Brochure PDF {expected_brochure!r}, got {r.get('Brochure PDF')!r}")
        if r.get("Floor Plan"):
            local_failures.append(f"{filename}: record {i} expected Floor Plan blank (no floor-plan-labeled link in this source), got {r.get('Floor Plan')!r}")

    if not local_failures:
        print(f"OK  {filename}: html_images classification and enrichment spot-checked against the real source file")
    failures.extend(local_failures)


def check_llm_prompt_handles_ranges_and_price_tiers(failures):
    """Targeted regression test for a real bug (2026-07, The Workplace
    Company — a brand-new provider's real email): the LLM fallback turned
    ONE flexible listing described with a size range ("ranges from 1,593
    sq ft to 2,729 sq ft") and two pricing tiers ("traditional lease from
    £13,907 pcm" vs "fully managed from £17,400 pcm") into a fabricated
    2-row (then, on a later real call, a full 2x2 = 4-row) cross-product
    of every size/price combination, while also silently dropping the
    £17,400 tier from every row.

    A live Gemini call is non-deterministic and would make this specific
    check flaky/costly in an automated suite (unlike every other check
    here, which is fast and offline) — this instead pins that the fixed
    prompt's own text actually contains the size-range/multi-tier-price
    instructions, so a future edit can't silently remove them. The real
    behavioral fix was verified live against the actual source email
    (twice, for consistency) and through the real running Flask endpoint
    end-to-end: both confirmed exactly 1 row, Size using the range's
    upper bound (2729), PCM using the first-quoted tier (13907), and the
    second tier's price preserved in Special Features rather than
    dropped."""
    prompt = _build_prompt("(sample document text)", "test")
    local_failures = []
    if "size range" not in prompt.lower() and "size_range" not in prompt.lower():
        local_failures.append("expected the LLM prompt to explicitly mention size ranges (e.g. 'SIZE RANGE') as a single-listing case")
    if "upper" not in prompt.lower() and "larger" not in prompt.lower():
        local_failures.append("expected the LLM prompt to specify using the upper/larger bound of a size range")
    if "pricing tier" not in prompt.lower() and "pricing option" not in prompt.lower():
        local_failures.append("expected the LLM prompt to explicitly mention multiple pricing tiers/options as a single-listing case")
    if "special features" not in prompt.lower():
        local_failures.append("expected the LLM prompt to instruct that an extra pricing tier goes into Special Features, not dropped")
    if not local_failures:
        print("OK  llm_fallback prompt: size-range/multi-tier-price instructions present")
    failures.extend(local_failures)


def check_derived_postcode_always_flagged(failures):
    """Targeted regression test for a real labeling bug (2026-07, found by
    directly inspecting a real MetSpace.xlsx): rows with a numbered street
    address (e.g. "9-10 Market Place") showed a Property Postcode with NO
    "(Not in source text)" flag at all, even though MetSpace's real
    source email never states a postcode for ANY building — numbered
    street address or bare name alike. The bug conflated two different
    things: geocoding CONFIDENCE (derived_note — a numbered address lets
    Nominatim match more confidently than a bare name, so Lat/Lng need no
    flag) and postcode PROVENANCE (postcode_from_geocode — whether the
    postcode was actually present in the source text at all, independent
    of how the address itself was matched). The old code only flagged
    Property Postcode when derived_note was ALSO true (i.e. only for a
    bare-name match), so a confident numbered-address geocode's
    Nominatim-derived postcode went completely unflagged.

    Pure function test against extraction.pipeline._geocode_records
    directly, with geocode replaced by a fake simulating a CONFIDENT
    numbered-address match (the exact shape that exposed this bug) —
    covers both a source with no postcode at all (must now be flagged)
    and, for contrast, a source that already had its own real postcode
    (must be left untouched, never flagged)."""
    records = [
        {"Property Address 1": "9-10 Market Place", "Property Postcode": ""},
        {"Property Address 1": "43-45 Charlotte Street", "Property Postcode": "W1T 4LU"},
    ]

    def fake_geocode(query, confident=True):
        if "9-10 Market Place" in query:
            return 51.5164430, -0.1402766, "W1W 8AQ", None
        return 51.5199, -0.1379, "W1T 4LU", None

    def fake_find_address(building, provider_name):
        raise AssertionError(f"find_address_via_web_search should never be called for a numbered address, got {building!r}")

    original_geocode = pipeline_module.geocode
    original_find = pipeline_module.find_address_via_web_search
    pipeline_module.geocode = fake_geocode
    pipeline_module.find_address_via_web_search = fake_find_address
    try:
        future_deadline = time.monotonic() + 100
        pipeline_module._geocode_records(records, "MetSpace test.eml", "MetSpace", future_deadline)
    finally:
        pipeline_module.geocode = original_geocode
        pipeline_module.find_address_via_web_search = original_find

    local_failures = []
    no_postcode_record = records[0]
    if no_postcode_record.get("Lat") != 51.5164430 or no_postcode_record.get("Lng") != -0.1402766:
        local_failures.append(
            f"expected a confident numbered-address match to leave Lat/Lng UNflagged (51.516443, -0.1402766), "
            f"got Lat={no_postcode_record.get('Lat')!r} Lng={no_postcode_record.get('Lng')!r}"
        )
    if no_postcode_record.get("Property Postcode") != "W1W 8AQ (Not in source text)":
        local_failures.append(
            f"expected a postcode derived from geocoding (source had none) to be flagged "
            f"'W1W 8AQ (Not in source text)', got {no_postcode_record.get('Property Postcode')!r}"
        )

    had_postcode_record = records[1]
    if had_postcode_record.get("Property Postcode") != "W1T 4LU":
        local_failures.append(
            f"expected a postcode already present in the source text to be left untouched ('W1T 4LU'), "
            f"got {had_postcode_record.get('Property Postcode')!r}"
        )

    if not local_failures:
        print("OK  derived postcode flagging: a confident numbered-address geocode's postcode is flagged, an already-known one is left alone")
    failures.extend(local_failures)


def check_batch_deadline_stops_remaining_lookups(failures):
    """Targeted regression test for a real Render SIGKILL (2026-07, "UNION
    - Availability - June 26 - City 2.xlsx"): a file with many bare-name/
    ambiguous buildings (Cannon Green, 4 Moorgate, 100 Cannon Street,
    Broadgate Tower, Chiswell Street, Birchin Lane, HYLO, Royal Exchange,
    Bow Bells House, Thames Court, and more) racked up enough cumulative
    extraction.address_lookup._throttle_rpm waiting and per-building
    retries to blow past gunicorn's own --timeout 120 (render.yaml), and
    the worker was killed mid-sleep with no response returned at all.

    extraction.pipeline._geocode_records now checks a shared
    time.monotonic() deadline before EVERY per-building lookup and, once
    it's passed, stops attempting any further lookups for the rest of
    the batch — pure function test with find_address_via_web_search/
    geocode both replaced by call-counting fakes (no live network/LLM
    needed) and a deadline already in the past, so every record must be
    skipped with zero lookup calls made at all, not just happen to
    resolve to the same values a real lookup might."""
    records = [
        {"Property Address 1": "Cannon Green", "Property Postcode": ""},
        {"Property Address 1": "100 Cannon Street", "Property Postcode": ""},
        {"Property Address 1": "Broadgate Tower", "Property Postcode": "EC2A 2BB"},
    ]
    calls = []

    def fake_find_address(building, provider_name):
        calls.append(("find_address", building))
        return None, [], False

    def fake_geocode(query, confident=True):
        calls.append(("geocode", query))
        return 51.5, -0.1, "EC2A 1AA", None

    original_find = pipeline_module.find_address_via_web_search
    original_geocode = pipeline_module.geocode
    pipeline_module.find_address_via_web_search = fake_find_address
    pipeline_module.geocode = fake_geocode
    try:
        past_deadline = time.monotonic() - 1
        quota_exhausted, deadline_hit = pipeline_module._geocode_records(
            records, "UNION - Availability - June 26 - City 2.xlsx", "UNION", past_deadline
        )
    finally:
        pipeline_module.find_address_via_web_search = original_find
        pipeline_module.geocode = original_geocode

    local_failures = []
    if not deadline_hit:
        local_failures.append("expected deadline_hit=True when the shared deadline had already passed, got False")
    if quota_exhausted:
        local_failures.append("expected quota_exhausted=False (no lookups were ever attempted), got True")
    if calls:
        local_failures.append(f"expected zero find_address_via_web_search/geocode calls once the deadline passed, got {calls}")
    for r in records:
        if r.get("Lat") != "Needs manual lookup" or r.get("Lng") != "Needs manual lookup":
            local_failures.append(
                f"{r['Property Address 1']!r}: expected Lat/Lng 'Needs manual lookup', got Lat={r.get('Lat')!r} Lng={r.get('Lng')!r}"
            )
    # A postcode already parsed from the source text (Broadgate Tower's
    # "EC2A 2BB" above) must survive untouched — only a genuinely missing
    # Property Postcode should be backfilled with the same manual-lookup flag.
    if records[0].get("Property Postcode") != "Needs manual lookup":
        local_failures.append(f"expected a missing Property Postcode to be backfilled to 'Needs manual lookup', got {records[0].get('Property Postcode')!r}")
    if records[2].get("Property Postcode") != "EC2A 2BB":
        local_failures.append(f"expected an already-known Property Postcode to be left untouched, got {records[2].get('Property Postcode')!r}")
    if not local_failures:
        print(f"OK  batch deadline: {len(records)} records correctly marked Needs manual lookup with zero lookup calls once the deadline passed")
    failures.extend(local_failures)


def check_daily_quota_short_circuits_remaining_bare_name_lookups(failures):
    """Targeted regression test for the other half of the same real
    Render incident (2026-07, "UNION - Availability - June 26 - City
    2.xlsx"): Broadgate Tower and HYLO both hit Gemini's daily 20-request
    quota (429 RESOURCE_EXHAUSTED) partway through the file's bare-name
    web-search lookups — every later bare-name building in the same batch
    was still retrying against the identical exhausted daily quota,
    wasting real time (extraction.address_lookup's own rate-limit pacing
    still waits before each attempt) that contributed to the same timeout
    risk BATCH_DEADLINE_SECONDS exists to guard against.

    Pure function test: find_address_via_web_search replaced with a fake
    that reports hit_quota=True on every call (so a regression that keeps
    calling it for every bare-name building is caught), geocode replaced
    with a call-counting fake standing in for the bare-name Nominatim
    fallback. Confirms only the FIRST bare-name building's lookup ever
    reaches find_address_via_web_search — every later one must skip
    straight to the Nominatim fallback instead."""
    records = [
        {"Property Address 1": "Broadgate Tower", "Property Postcode": ""},
        {"Property Address 1": "HYLO", "Property Postcode": ""},
        {"Property Address 1": "Royal Exchange", "Property Postcode": ""},
    ]
    find_calls = []
    geocode_calls = []

    def fake_find_address(building, provider_name):
        find_calls.append(building)
        return None, [], True

    def fake_geocode(query, confident=True):
        geocode_calls.append(query)
        return 51.5, -0.1, "EC2A 1AA", None

    original_find = pipeline_module.find_address_via_web_search
    original_geocode = pipeline_module.geocode
    pipeline_module.find_address_via_web_search = fake_find_address
    pipeline_module.geocode = fake_geocode
    try:
        future_deadline = time.monotonic() + 100
        quota_exhausted, deadline_hit = pipeline_module._geocode_records(
            records, "UNION - Availability - June 26 - City 2.xlsx", "UNION", future_deadline
        )
    finally:
        pipeline_module.find_address_via_web_search = original_find
        pipeline_module.geocode = original_geocode

    local_failures = []
    if not quota_exhausted:
        local_failures.append("expected quota_exhausted=True after the first bare-name lookup reported hit_quota, got False")
    if deadline_hit:
        local_failures.append("expected deadline_hit=False (the deadline is far in the future), got True")
    if find_calls != ["Broadgate Tower"]:
        local_failures.append(
            f"expected find_address_via_web_search called exactly once, for the first bare-name building only "
            f"(later ones must short-circuit past a known-exhausted daily quota), got {find_calls}"
        )
    if len(geocode_calls) != len(records):
        local_failures.append(
            f"expected the bare-name Nominatim fallback (geocode) called once per record ({len(records)}), got {len(geocode_calls)}"
        )
    if not local_failures:
        print(
            f"OK  daily quota short-circuit: {len(records)} bare-name records, only 1 web-search call made after the first hit the daily quota"
        )
    failures.extend(local_failures)


def check_gemini_overload_retry(failures):
    """Targeted regression test for extraction.quota.call_with_overload_retry
    — added on request to automatically retry a Gemini 503 UNAVAILABLE
    ("the model is overloaded, please try again later") error a couple
    of times, with a short wait in between, before surfacing it as a
    real failure. Deliberately distinct from a 429 daily-quota error
    (extraction.quota.is_quota_exceeded), which fails identically on
    every immediate retry and must NOT be retried at all here.

    Pure function test against fake exception objects shaped like the
    real google-genai ServerError/ClientError (a `code` attribute plus a
    message string) — no live Gemini call needed. quota.time is swapped
    for a call-counting fake so the test doesn't actually wait through
    the real 5s/15s retry delays. Covers three cases: (1) a transient
    503 that succeeds on the first retry, (2) a persistent 503 that
    still fails after every retry and must re-raise, not swallow, the
    final error, (3) a 429 that must propagate on the very first
    attempt with zero retries/waits at all."""
    from extraction import quota as quota_module

    class FakeOverloadedError(Exception):
        code = 503

        def __str__(self):
            return "503 UNAVAILABLE. The model is overloaded. Please try again later."

    class FakeQuotaError(Exception):
        code = 429

        def __str__(self):
            return "429 RESOURCE_EXHAUSTED."

    class FakeTime:
        def __init__(self):
            self.sleep_calls = []

        def sleep(self, seconds):
            self.sleep_calls.append(seconds)

    local_failures = []
    original_time = quota_module.time

    # Case 1: one transient 503, then success — must retry and return the
    # real result, waiting once (the first configured wait) beforehand.
    calls = {"n": 0}

    def flaky_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeOverloadedError()
        return "ok"

    fake_time = FakeTime()
    quota_module.time = fake_time
    log_calls = []
    try:
        result = quota_module.call_with_overload_retry(flaky_once, log=log_calls.append, label="Test Building")
    finally:
        quota_module.time = original_time

    if result != "ok":
        local_failures.append(f"expected call_with_overload_retry to return 'ok' after one 503 retry, got {result!r}")
    if calls["n"] != 2:
        local_failures.append(f"expected fn() called exactly twice (1 failure + 1 success), got {calls['n']}")
    if fake_time.sleep_calls != [quota_module.OVERLOAD_RETRY_WAITS_SECONDS[0]]:
        local_failures.append(f"expected exactly one wait of {quota_module.OVERLOAD_RETRY_WAITS_SECONDS[0]}s, got {fake_time.sleep_calls}")
    if not log_calls or "Test Building" not in log_calls[0] or "503" not in log_calls[0]:
        local_failures.append(f"expected a retry log message naming the label and mentioning 503, got {log_calls}")

    # Case 2: persistent 503 across every attempt — must re-raise the real
    # error after exhausting all retries, not swallow it or return None.
    calls2 = {"n": 0}

    def always_overloaded():
        calls2["n"] += 1
        raise FakeOverloadedError()

    fake_time2 = FakeTime()
    quota_module.time = fake_time2
    raised = False
    try:
        try:
            quota_module.call_with_overload_retry(always_overloaded, log=lambda m: None)
        except FakeOverloadedError:
            raised = True
    finally:
        quota_module.time = original_time

    expected_attempts = len(quota_module.OVERLOAD_RETRY_WAITS_SECONDS) + 1
    if not raised:
        local_failures.append("expected a persistent 503 to re-raise after exhausting all retries, got no exception")
    if calls2["n"] != expected_attempts:
        local_failures.append(f"expected exactly {expected_attempts} total attempts before giving up, got {calls2['n']}")
    if fake_time2.sleep_calls != list(quota_module.OVERLOAD_RETRY_WAITS_SECONDS):
        local_failures.append(f"expected waits of {list(quota_module.OVERLOAD_RETRY_WAITS_SECONDS)}, got {fake_time2.sleep_calls}")

    # Case 3: a 429 daily-quota error must propagate immediately — zero
    # retries, zero waits, since retrying it would just fail identically.
    calls3 = {"n": 0}

    def quota_exhausted_call():
        calls3["n"] += 1
        raise FakeQuotaError()

    fake_time3 = FakeTime()
    quota_module.time = fake_time3
    raised3 = False
    try:
        try:
            quota_module.call_with_overload_retry(quota_exhausted_call, log=lambda m: None)
        except FakeQuotaError:
            raised3 = True
    finally:
        quota_module.time = original_time

    if not raised3:
        local_failures.append("expected a 429 quota error to propagate on the first attempt (not be treated as retryable), got no exception")
    if calls3["n"] != 1:
        local_failures.append(f"expected exactly 1 attempt for a 429 (no retries at all), got {calls3['n']}")
    if fake_time3.sleep_calls:
        local_failures.append(f"expected zero waits for a 429 (never retried), got {fake_time3.sleep_calls}")

    if not local_failures:
        print(
            "OK  Gemini 503 overload retry: succeeds after a transient 503, re-raises after a persistent one, "
            "never retries a 429"
        )
    failures.extend(local_failures)


def check_contacts_include_phone_email(failures):
    """Targeted regression test for a class of gap a 2026-07 audit found
    across MetSpace, GPE, and Breezblok, prompted by Knotel's own earlier
    "Contacts was entirely blank" bug: each of these sources' contact
    block DOES have a real phone and/or email for every named contact,
    genuinely present in the source, that was being silently dropped —
    only the name(s) ever made it into Contacts. Pins the exact,
    real Contacts value (name + phone/email) for each, and confirms
    Assigned Agents (extraction.schema.names_only) still correctly
    reduces it back to just the name(s) — including GPE's own
    international "+44 (0) ..." phone format, which an earlier version of
    names_only's own phone-detection regex (UK-domestic-only) failed to
    recognize, silently leaking the phone number into Assigned Agents
    instead of stripping it."""
    cases = [
        (
            "Fw_ MetSpace Availability Update.eml",
            "MetSpace",
            "Kieran Christie, sales@metspace.co.uk, 07837 270 455, Sophie Haugh, sales@metspace.co.uk, "
            "07950 565 491, Nicki Mayle, sales@metspace.co.uk, 07946 136 004",
            "Kieran Christie, Sophie Haugh, Nicki Mayle",
        ),
        (
            "Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml",
            "GPE",
            "David Korman, +44 (0) 7435 939 956, Molly Maguire, +44 (0) 7887 841 816, Anna Tweed, "
            "+44 (0) 7990 633 486, Richard Carson, +44 (0) 7436 030 120",
            "David Korman, Molly Maguire, Anna Tweed, Richard Carson",
        ),
        (
            "John Stow House.pdf",
            "Breezblok",
            "Sales, Sales@breezblok.london, +44 7500665267",
            "Sales",
        ),
    ]
    for filename, expected_rule, expected_contacts, expected_agents in cases:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue
        content = read_file(path)
        rule_name, records = try_rules(content)
        if rule_name != expected_rule or not records:
            failures.append(f"{filename}: expected rule '{expected_rule}' with records, got '{rule_name}'")
            continue
        norm = normalize_record(records[0])
        local_failures = []
        if norm["Contacts"] != expected_contacts:
            local_failures.append(f"{filename}: expected Contacts {expected_contacts!r}, got {norm['Contacts']!r}")
        if norm["Assigned Agents"] != expected_agents:
            local_failures.append(f"{filename}: expected Assigned Agents {expected_agents!r}, got {norm['Assigned Agents']!r}")
        if not local_failures:
            print(f"OK  {filename}: Contacts/Assigned Agents spot-checked against known-correct source values")
        failures.extend(local_failures)


def check_bc_records(failures):
    """Targeted regression test for extraction.rules.bc, pinning known-correct
    field values from the real "BC Current Availability.pdf" table.

    Also guards against a real bug this rule already had: an earlier,
    looser version of its detect() (generic single-word keywords, the same
    style as extraction.rules.grid) matched Kitt's own table too, since
    Kitt's header already uses near-schema wording ("Building", "Floor/
    Unit", "Size (sq ft)", "Desks (max)", "Marketing Price...PCM") that a
    loose keyword count can't tell apart from BC's own generic English
    column names. Fixed by requiring BC's own distinctive "Num of Desks" +
    "Sale Price" combination, which Kitt's table doesn't have — this test
    would catch a future regression back to that looser detection by
    failing the "got 'BC'" assertion in the main EXPECTATIONS loop above
    for Kitt's own file, but pins the BC-specific values here too so a
    change to bc.py's column mapping doesn't silently misparse a value."""
    filename = "BC Current Availability.pdf"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "BC" or not records:
        failures.append(f"{filename}: expected rule 'BC' with records, got '{rule_name}'")
        return

    normalized = [normalize_record(r) for r in records]
    by_key = {(r["Building"], r["Floor/Unit"]): r for r in normalized}

    # A handful of known rows, re-verified directly against the source
    # table — covers a row with a real Sale Price (For Sale should be
    # "Yes"), a row with "N/A" (For Sale should be "No"), and the PCM/PSF
    # derivation (this source only gives PCM; PSF must be computed).
    checks = [
        (
            ("10-12 Alie Street", "G & LG Duplex"),
            {
                "Size (sq ft)": 4800,
                "Desks (max)": 70,
                "Marketing Price (Based on Min Term) PCM": 48000,
                "Marketing Price (Based on Min Term) PSF": 120.0,
                "Special Features": "Communal Lounge, Break-out & Terrace",
                "For Sale": "Yes",
                # BC's own table has no contact/agent column at all (see
                # this module's docstring) — Assigned Agents must fall
                # back to "Unknown", not be left blank or invent one.
                "Assigned Agents": "Unknown",
            },
        ),
        (
            ("17 Bevis Marks", "3rd Floor"),
            {
                "Size (sq ft)": 3200,
                "Desks (max)": 50,
                "Marketing Price (Based on Min Term) PCM": 35000,
                "For Sale": "No",
                "Assigned Agents": "Unknown",
            },
        ),
        (
            ("Porters Place", "4th Floor"),
            {
                "Size (sq ft)": 3476,
                "Desks (max)": 50,
                "Marketing Price (Based on Min Term) PCM": 55036,
                "State of Space": "Immediate",
                "For Sale": "No",
                "Assigned Agents": "Unknown",
            },
        ),
    ]
    local_failures = []
    for key, expected in checks:
        row = by_key.get(key)
        if not row:
            local_failures.append(f"{filename}: expected a row for {key}, not found")
            continue
        for field, expected_value in expected.items():
            if row.get(field) != expected_value:
                local_failures.append(
                    f"{filename}: {key} field {field!r} expected {expected_value!r}, got {row.get(field)!r}"
                )

    if len(records) != 11:
        local_failures.append(f"{filename}: expected 11 records, got {len(records)}")

    if not local_failures:
        print(f"OK  {filename}: {len(records)} BC records spot-checked against known-correct source values")
    failures.extend(local_failures)


def check_breezblok_records(failures):
    """Targeted regression test for extraction.rules.breezblok, pinning
    known-correct field values from the real "John Stow House.pdf"
    brochure — a single-listing, multi-page format where the building's
    own address/postcode and the listing's own size/desks/price sit on
    different pages, not one table."""
    filename = "John Stow House.pdf"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "Breezblok" or not records:
        failures.append(f"{filename}: expected rule 'Breezblok' with records, got '{rule_name}'")
        return
    if len(records) != 1:
        failures.append(f"{filename}: expected exactly 1 record (one 'Proposed space' section), got {len(records)}")
        return

    norm = normalize_record(records[0])
    expected = {
        "Building": "John Stow House, 18 Bevis Marks, London EC3A 7JB",
        "Floor/Unit": "Office 302",
        "Size (sq ft)": 1750,
        "Desks (max)": 32,
        "Marketing Price (Based on Min Term) PCM": 18000,
        # The real email/phone from the two lines following "Contact:
        # Sales" in the source — found missing in a 2026-07 audit (the
        # same class of gap as Knotel's own originally-missed contact
        # info) and fixed to be captured alongside the name.
        "Contacts": "Sales, Sales@breezblok.london, +44 7500665267",
        # Not an individual's name, but Breezblok's own source names no
        # one else either — see extraction.rules.breezblok._contact's own
        # docstring for why this is the correct, non-blank value.
        # names_only() must strip the email/phone above but leave "Sales"
        # itself unchanged.
        "Assigned Agents": "Sales",
        "Property Postcode": "EC3A 7JB",
    }
    local_failures = [
        f"{filename}: field {field!r} expected {expected_value!r}, got {norm.get(field)!r}"
        for field, expected_value in expected.items()
        if norm.get(field) != expected_value
    ]

    # Property Address 1 itself isn't set to the clean value by
    # normalize_record (see street_address_only's own docstring for why
    # that's deliberately deferred to extraction.pipeline.process_files,
    # after geocoding) — check the derivation function directly instead.
    # Keeps the building name ("John Stow House") alongside the street,
    # not just the street alone — Building itself is untouched either way.
    derived_street = street_address_only(norm["Building"])
    if derived_street != "John Stow House, 18 Bevis Marks":
        local_failures.append(
            f"{filename}: street_address_only(Building) expected 'John Stow House, 18 Bevis Marks', got {derived_street!r}"
        )

    if not local_failures:
        print(f"OK  {filename}: Breezblok record spot-checked against known-correct source values")
    failures.extend(local_failures)


def check_pdf_floorplan_vs_photos(failures, filename, building, name, expect_gallery):
    """Targeted regression test for two real bugs found in the same
    classification step, across two different PDF sources:

    1. BC's "2-7 Clerkenwell Green" brochure: Floor Plan showed the text
    "Example Floorplan" with no hyperlink attached at all — the LLM
    fallback was copying that literal heading into the Floor Plan field
    itself (a plain string, never a real link), and nothing overwrote it
    since app.py's own image-based Floor Plan logic didn't exist yet at
    the time. Fixed in two places: extraction.llm_fallback no longer asks
    the LLM for (or trusts it for) Floor Plan/High Res Images at all, and
    app.py's _attach_pdf_images now sets a real Floor Plan link itself
    when it finds one.

    2. Breezblok's "John Stow House" brochure: Floor Plan was blank
    despite page 6 genuinely containing a floor-plan diagram, while High
    Res Images correctly showed a gallery — the diagram had been silently
    swept into the photo gallery instead of recognized as a floor plan.
    Root cause: classification worked at PAGE granularity (a whole page
    was "the floor plan page" or not, based on that page's own text
    mentioning "floor plan") and this page's text never says so — yet the
    same page also has a genuine desk photo alongside the diagram. Fixed
    by classifying each image individually (extraction.pdf_images.
    is_floorplan_image, a pixel-content signal: a floor-plan diagram is
    rendered on a plain white background, confirmed empirically far
    whiter than any real photo or decorative logo graphic tested) instead
    of by page.

    This calls the real app.py code path (_attach_pdf_images) directly
    rather than reimplementing its logic, and checks actual byte-identity
    against the source PDF's own images — not just "is something
    populated" — so a future regression that mixes up which image is
    which would be caught, not just a blank-vs-populated regression."""
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    pages_text = content.get("pages_text", [])
    page_images = pdf_images.extract_page_images(path)
    local_failures = []

    # Independently determine ground truth: which real, extracted image(s)
    # this pixel/text classification calls a floor plan vs a photo, so the
    # assertions below check against the same source data app.py itself
    # would see, not a hand-picked expectation that could drift.
    floorplan_hashes = set()
    photo_hashes = set()
    for page_num, imgs in page_images.items():
        page_is_labeled_floorplan = pdf_images.is_floorplan_page(pages_text[page_num] if page_num < len(pages_text) else "")
        for image_bytes, _ext, _link_floorplan_url in imgs:
            h = hashlib.sha256(image_bytes).hexdigest()
            if page_is_labeled_floorplan or pdf_images.is_floorplan_image(image_bytes):
                floorplan_hashes.add(h)
            else:
                photo_hashes.add(h)

    if not floorplan_hashes:
        failures.append(
            f"{filename}: expected at least one image to be classified as a floor plan (this source is known to "
            "contain a real floor-plan diagram) — floor-plan classification may be broken"
        )
        return
    if not photo_hashes:
        failures.append(f"{filename}: expected at least one real (non-floor-plan) photo — got none")
        return

    records = [{"Building": building}]
    with tempfile.TemporaryDirectory() as tmp:
        batch_dir = Path(tmp)
        with app_module.app.test_request_context():
            jobs = app_module._attach_pdf_images(records, path, pages_text, batch_dir, "testbatch", name)

        saved_paths = {key: local_path for key, local_path in jobs}
        record = records[0]
        floor_plan_url = record.get("Floor Plan", "")
        high_res_url = record.get("High Res Images", "")

        if not floor_plan_url:
            local_failures.append(f"{filename}: expected Floor Plan populated with a real link, got blank")
        if not high_res_url:
            local_failures.append(f"{filename}: expected High Res Images populated with a real link, got blank")
        if floor_plan_url and high_res_url:
            if floor_plan_url == high_res_url:
                local_failures.append(f"{filename}: Floor Plan and High Res Images point to the same URL")
            else:
                # Resolve whichever local file(s) actually ended up
                # referenced by each column, and hash them back against
                # the ground truth above.
                def _referenced_hashes(url):
                    if url.rstrip("/").split("?", 1)[0].endswith(".html"):
                        gallery_path = next((p for k, p in saved_paths.items() if k.split("/")[-1] in url), None)
                        if gallery_path is None:
                            return set()
                        html = Path(gallery_path).read_text(encoding="utf-8")
                        return {
                            hashlib.sha256(Path(p).read_bytes()).hexdigest()
                            for k, p in saved_paths.items()
                            if k.split("/")[-1] in html
                        }
                    local_path = next((p for k, p in saved_paths.items() if k.split("/")[-1] in url), None)
                    return {hashlib.sha256(Path(local_path).read_bytes()).hexdigest()} if local_path else set()

                floor_plan_hashes_used = _referenced_hashes(floor_plan_url)
                high_res_hashes_used = _referenced_hashes(high_res_url)

                if not floor_plan_hashes_used & floorplan_hashes:
                    local_failures.append(f"{filename}: Floor Plan link doesn't point to a real floor-plan-classified image")
                if floor_plan_hashes_used & photo_hashes:
                    local_failures.append(
                        f"{filename}: Floor Plan link points to what was classified as a real photo, not the floor plan"
                    )
                if high_res_hashes_used & floorplan_hashes:
                    local_failures.append(
                        f"{filename}: the floor-plan image was found inside High Res Images (gallery or direct link) — "
                        "it should be excluded from the photo gallery and only appear in Floor Plan"
                    )
                if not (high_res_hashes_used & photo_hashes):
                    local_failures.append(f"{filename}: High Res Images doesn't reference any real photo")
                if expect_gallery and "gallery" not in high_res_url:
                    local_failures.append(
                        f"{filename}: expected High Res Images to be a gallery page (2+ real photos), got a direct link"
                    )

                if not local_failures:
                    print(
                        f"OK  {filename}: Floor Plan correctly linked to the real floor-plan image, kept separate "
                        f"from High Res Images ({len(high_res_hashes_used)} real photo(s) referenced, floor plan "
                        "excluded)"
                    )

    failures.extend(local_failures)


def main():
    failures = []
    for filename, expected_rule, expected_count in EXPECTATIONS:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue

        content = read_file(path)
        rule_name, records = try_rules(content)

        if rule_name != expected_rule:
            failures.append(f"{filename}: expected rule '{expected_rule}', got '{rule_name}'")
            continue

        # Exact, not just a minimum — a "some rows silently dropped"
        # regression (the exact Kitt's multi-table bug this pins) can
        # still clear a loose minimum. A source that's supposed to grow
        # over time should get its own pinned number bumped here rather
        # than switched back to a loose bound.
        if not records or len(records) != expected_count:
            failures.append(f"{filename}: expected exactly {expected_count} records, got {len(records) if records else 0}")
            continue

        for r in records:
            norm = normalize_record(r)
            if not (norm.get("Building") or norm.get("Area")):
                failures.append(f"{filename}: found a record with no Building or Area")
                break

        print(f"OK  {filename}: {len(records)} records via {rule_name}")

    check_metspace_floor_plans(failures)
    check_gpe_high_res_images(failures)
    check_knotel_records(failures)
    check_street_address_only(failures)
    check_names_only(failures)
    check_geocode_same_building_ambiguity(failures)
    check_source_filename_disambiguation(failures)
    check_area_disambiguated_output_names(failures)
    check_html_images_for_llm_fallback(failures)
    check_xlsx_links_for_llm_fallback(failures)
    check_low_trust_link_domain(failures)
    check_rule_sanity_check_fallback(failures)
    check_llm_prompt_handles_ranges_and_price_tiers(failures)
    check_derived_postcode_always_flagged(failures)
    check_batch_deadline_stops_remaining_lookups(failures)
    check_daily_quota_short_circuits_remaining_bare_name_lookups(failures)
    check_gemini_overload_retry(failures)
    check_contacts_include_phone_email(failures)
    check_bc_records(failures)
    check_breezblok_records(failures)
    check_pdf_floorplan_vs_photos(
        failures,
        filename="2nd Floor - 2-7 Clerkenwell Green Brochure.pdf",
        building="2-7 Clerkenwell Green",
        name="BC",
        expect_gallery=True,
    )
    check_pdf_floorplan_vs_photos(
        failures,
        filename="John Stow House.pdf",
        building="John Stow House",
        name="Breezblok",
        expect_gallery=True,
    )

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nAll example files extracted successfully.")


if __name__ == "__main__":
    main()
