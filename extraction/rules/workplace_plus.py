"""Parser for Workplace Plus availability email campaigns."""
import re
from urllib.parse import unquote


# City is optional and not London-only — confirmed real (2026-07): Manchester
# (and other UK city) Workplace Plus campaigns use the same card layout with
# "…, Manchester, M1 2AB" style lines. Restricting to London silently dropped
# every address and left the rule with zero rows / no photos.
ADDRESS_RE = re.compile(
    r"^\d[\w\-/' ]*,?\s+.+,\s*(?:[A-Za-z][A-Za-z .'\-]+,\s*)?([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})$",
    re.I,
)
FLOOR_RE = re.compile(r"^(?:Ground|Lower Ground|Basement|\d+(?:st|nd|rd|th))(?:\s*&\s*(?:First|\d+(?:st|nd|rd|th)))?\s+Floor$", re.I)
DESKS_RE = re.compile(r"^(\d+)\s+Desks?\b", re.I)
PRICE_RE = re.compile(r"£\s*([\d,]+)\s*Per Month", re.I)


def detect(content):
    blob = " ".join([content.get("sender") or "", content.get("subject") or "", content.get("text") or ""]).lower()
    return "workplace plus" in blob or "@workplaceplus.co.uk" in blob


def parse(content):
    lines = [line.strip() for line in (content.get("text") or "").splitlines() if line.strip()]
    address_indexes = [i for i, line in enumerate(lines) if ADDRESS_RE.match(line)]
    if not address_indexes:
        return []

    assets = _listing_assets(content.get("html_items") or [])
    records = []
    for building_index, start in enumerate(address_indexes):
        end = address_indexes[building_index + 1] if building_index + 1 < len(address_indexes) else len(lines)
        block = lines[start + 1 : end]
        brochure, photo = assets[building_index] if building_index < len(assets) else ("", "")
        floor_indexes = [i for i, line in enumerate(block) if FLOOR_RE.match(line)]
        for position, floor_idx in enumerate(floor_indexes):
            floor_end = floor_indexes[position + 1] if position + 1 < len(floor_indexes) else len(block)
            floor_block = block[floor_idx + 1 : floor_end]
            desks_line = next((line for line in floor_block if DESKS_RE.match(line)), "")
            price_line = next((line for line in floor_block if PRICE_RE.search(line)), "")
            desks_match = DESKS_RE.match(desks_line)
            price_match = PRICE_RE.search(price_line)
            records.append(
                {
                    "Building": lines[start],
                    "Floor/Unit": block[floor_idx],
                    "Desks (max)": desks_match.group(1) if desks_match else "",
                    "Marketing Price (Based on Min Term) PCM": price_match.group(1).replace(",", "") if price_match else "",
                    "Special Features": desks_line,
                    "Brochure PDF": brochure,
                    "High Res Images": photo,
                    "Contacts": "Workplace Plus, hello@workplaceplus.co.uk",
                }
            )
    return records


def _listing_assets(html_items):
    """Associate each photo with the brochure in its own HTML card.

    Workplace Plus repeats the same tracked href on the card's wrapping
    image link and its visible Brochure link.  That shared href is structural
    identity; independently collecting two global lists and zipping them can
    shift every later property when one optional asset is absent.
    """
    pairs = []
    for index, (kind, alt, url) in enumerate(html_items):
        decoded = unquote(url)
        if not (
            kind == "image"
            and "gallery.eocampaign1.com" in decoded
            and "logo" not in (alt or "").lower()
            and "tentacles/icons" not in decoded
            and "availability" not in decoded.lower()
        ):
            continue
        nearby = html_items[max(0, index - 3) : min(len(html_items), index + 4)]
        wrapping_urls = {
            candidate_url
            for candidate_kind, text, candidate_url in nearby
            if candidate_kind == "link" and not text.strip()
        }
        brochure = next(
            (
                candidate_url
                for candidate_kind, text, candidate_url in nearby
                if candidate_kind == "link"
                and text.strip().lower() == "brochure"
                and (not wrapping_urls or candidate_url in wrapping_urls)
            ),
            "",
        )
        if not brochure:
            # Manchester/other campaigns sometimes wrap the photo with a
            # tracked href but omit a nearby "Brochure" label. Use that
            # wrapping href only when no conflicting Brochure label is
            # present — never steal another card's brochure URL.
            has_brochure_label = any(
                candidate_kind == "link" and (text or "").strip().lower() == "brochure"
                for candidate_kind, text, _candidate_url in nearby
            )
            if not has_brochure_label:
                brochure = next(iter(wrapping_urls), "")
        if brochure:
            pairs.append((brochure, url))
    return pairs
