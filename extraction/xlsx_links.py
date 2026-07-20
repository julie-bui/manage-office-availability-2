"""Generic Brochure PDF / Floor Plan enrichment for raw-spreadsheet
(.xlsx/.xls) sources with NO dedicated rule of their own, going through
the LLM fallback instead — the same generic-enrichment role as
extraction.html_images (.eml/.html) and app.py's own _attach_pdf_images
(PDF), but for a source whose real per-row hyperlinks
(extraction.file_readers.row_links) pandas' own cell-value read (used to
build the LLM's own plain-text prompt input) discards entirely.

Confirmed real (2026-07, a UNION .xlsx with no dedicated rule of its
own): its own "Brochure" column links every row to a real box.com
brochure/floor-plan URL through a hyperlink on a generic display cell
("CLICK HERE", "Landlord Brochure", "FLOOR PLAN") — invisible to the
LLM's own text input, so Brochure PDF/Floor Plan came back blank for
every row despite real links existing in the source.

The same FLOOR-PLAN-labeled share is also filed as Brochure PDF when no
separate brochure cell exists — otherwise High Res enrichment never
fetches the landlord PDF. That dual-fill lives here so dedicated rules
(UNION) and the LLM fallback path share one association helper.
"""
import re

from .html_images import is_floorplan_link, is_image_like_url, is_low_trust_link_domain

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_for_matching(text):
    """Collapses whitespace runs to a single space, so a raw source cell
    with a stray double space (confirmed real, 2026-07 — the actual
    UNION source has "Nexus Place -  25 Farringdon Place", two spaces
    after the dash) still matches the LLM's own Building field, which
    normalizes whitespace when extracting text. Without this, a plain
    substring check silently missed this exact row's real Brochure link
    — not because it didn't exist, but because "-  25" (source) never
    equals "- 25" (extracted) as raw substrings."""
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def associate_row_links(links):
    """Map one spreadsheet row's (display_text, url) pairs to fields.

    Returns (floorplan_url_or_None, brochure_url_or_None).

    Floor-plan-labeled cells populate Floor Plan only when the URL looks like
    a hosted image; their URL is ALWAYS a Brochure PDF candidate — hosts often
    put the only landlord PDF behind "FLOOR PLAN" with no separate brochure
    label. Other hyperlinks (CLICK HERE, Landlord Brochure, Website, …) are
    brochure candidates regardless of display text: a per-row spreadsheet link
    column has little of the unrelated-link noise that email bodies have.

    When several brochure candidates exist, prefer domains that are not
    known JS viewers (is_low_trust_link_domain); fall back to the first
    candidate only when every option is low-trust.
    """
    floorplan_url = None
    brochure_candidates = []
    for display_text, url in links or []:
        if not url:
            continue
        if is_floorplan_link(display_text):
            if floorplan_url is None and is_image_like_url(url):
                floorplan_url = url
            brochure_candidates.append(url)
        else:
            brochure_candidates.append(url)
    return floorplan_url, _best_brochure_candidate(brochure_candidates)


def enrich_records(records, row_links):
    """Fills Floor Plan / Brochure PDF directly for records from an
    LLM-fallback .xlsx/.xls source with no dedicated rule. Mutates
    records in place; never raises — always a best-effort enrichment,
    never something that should fail the batch.

    Matches each record to its own source row by Building-name substring
    search against that row's own dumped text (row_text) — the LLM reads
    the sheet top to bottom, so its own extracted Building field should
    appear (up to whitespace differences — see _normalize_for_matching)
    within the row it came from. Each row is consumed at most once, in
    record order, so two records sharing the same building name (e.g.
    two floors of "107 Cannon Street", each with its own Brochure link)
    each get their OWN row's link, not both getting whichever matched
    first.

    Unlike extraction.html_images' own is_brochure_link (which requires
    the link text to actually mention "brochure"/"particulars"/etc —
    appropriate for scanning a whole free-text email body full of
    unrelated links), any non-floorplan link found in a source row here
    is a Brochure PDF candidate regardless of its own display text
    ("CLICK HERE" says nothing on its own) — a per-row hyperlink in a
    spreadsheet's own dedicated link column is reliably one or the
    other; there's no "unrelated link" noise here to filter the way a
    marketing email body has. When a row has more than one such
    candidate (confirmed real, 2026-07 — The Workplace Company gives a
    separate "Brochure" AND "Website" column per listing), the first
    one whose domain ISN'T a known JS-viewer/presentation tool
    (is_low_trust_link_domain) wins, rather than just taking whichever
    column happens to come first — a link literally labeled "Brochure"
    pointed at Canva there, while "Website" pointed at the company's own
    domain and actually works. Only falls back to a low-trust candidate
    when every candidate in the row is one — still better than nothing."""
    if not records or not row_links:
        return

    available = [{"row_text": _normalize_for_matching(row["row_text"]), "links": row["links"]} for row in row_links]
    for record in records:
        building = (record.get("Building") or "").strip()
        if not building:
            continue
        building_l = _normalize_for_matching(building)
        match_idx = next(
            (i for i, row in enumerate(available) if building_l in row["row_text"]),
            None,
        )
        if match_idx is None:
            continue
        row = available.pop(match_idx)
        floorplan_url, brochure_url = associate_row_links(row["links"])

        if floorplan_url and not record.get("Floor Plan"):
            record["Floor Plan"] = floorplan_url
        if brochure_url and not record.get("Brochure PDF"):
            record["Brochure PDF"] = brochure_url
        # Floor Plan is hosted plan images only — never seed brochure/Box
        # document URLs into Floor Plan (Brochure PDF holds those).


def _best_brochure_candidate(urls):
    """The first URL that ISN'T a known low-trust JS-viewer domain, or
    the first URL at all if every candidate is one — see enrich_records'
    own docstring for why domain, not column order, decides this."""
    if not urls:
        return None
    for url in urls:
        if not is_low_trust_link_domain(url):
            return url
    return urls[0]
