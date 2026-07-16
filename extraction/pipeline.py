"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.

Unlike an earlier version of this module, each file's records stay
separate (one output spreadsheet per source file, not one combined master).
"""
import gc
import re
import time
from datetime import date

from . import brochure, html_images, memlog, quota, xlsx_links
from .address import extract_postcode, spelled_number_to_digits
from .address_lookup import find_address as find_address_via_web_search
from .file_readers import read_file
from .geocode import geocode, get_resolution_diagnostics
from .llm_fallback import LLMExtractionError, extract_with_llm
from .models import FieldProvenance, ProcessingReport, Property, RawDocument
from .naming import area_from_records, extract_area_hint, resolve_provider_name, resolve_source_date
from .rules import try_rules
from .schema import normalize_record, street_address_only
from .spreadsheet_chunks import extract_in_chunks, is_large_spreadsheet, is_spreadsheet
from .validation import validate_properties

# Confirmed via a real Render SIGKILL (2026-07, "UNION - Availability -
# June 26 - City 2.xlsx"): gunicorn's own --timeout 120 (render.yaml) is a
# hard ceiling on the WHOLE request, not just geocoding — file reading,
# LLM calls, and image attachment for every file in the batch all count
# against it too. A file with many bare-name/ambiguous buildings can rack
# up enough cumulative extraction.address_lookup._throttle_rpm waiting
# and per-building retries to blow straight past that ceiling with no
# warning — the crash was caught mid-sleep inside _throttle_rpm itself.
# _geocode_records checks this deadline before EVERY per-building lookup
# (Nominatim or Gemini) and, once passed, stops attempting further
# lookups entirely for the rest of the batch — trading address
# completeness (those rows fall back to "Needs manual lookup", the same
# honest flag used for any other geocoding gap) for a guaranteed response
# within the timeout, rather than a silent SIGKILL that returns nothing
# at all. 100s (not the full 120s) deliberately leaves ~20s of margin for
# whatever still needs to happen after geocoding finishes — image
# attachment, spreadsheet writing — for this file and any others already
# queued in the same batch.
BATCH_DEADLINE_SECONDS = 100
# Do not start another optional address/geocode operation unless there is
# enough shared time for the longest bounded lookup (Gemini: 25s) plus a
# small scheduling margin. Core extraction and spreadsheet writing win.
OPTIONAL_LOOKUP_START_SECONDS = 30
# Reserve wall-clock for finalize/write after the last file's enrichment.
# Must cover gallery HTML writes for every earlier file: Union Box waves
# previously ran into (and past) batch_deadline and left Knotel finalize
# with OPTIONAL_IMAGE_VALIDATION_SKIPPED for every Directus URL.
# Galleries now use absolute download URLs (not inlined base64), so finalize
# needs less reserve — free those seconds for Knotel/GPE under-target rows.
ENRICHMENT_FINALIZE_RESERVE_SECONDS = 15
# Confirmed real (2026-07 batch MetSpace+Knotel+WP+Union): enrichment used
# ONE absolute deadline shared by every file. MetSpace (first) consumed the
# budget fetching Drive brochure photos; Knotel (second) kept only its
# single email featured image; Union got almost no Box PDF photos.
# Absolute equal windows FROM batch-start failed too: when MetSpace overrun
# its slice, Knotel's fixed end-time was already nearly gone (email-only
# singles again) while Workplace Plus — later, with a later absolute end —
# still got galleries. Each file therefore takes a fair share of WHATEVER
# enrichment time remains when that file starts (remaining / files left),
# and in-flight brochure waves hard-stop at that deadline.


# A trailing postal district/area code with no inward part (e.g. "W1",
# "SW1Y", "EC2V") — short enough to be a district code, not a full street
# address of its own. Used only to group the SAME building's multiple
# occurrences within one file for _geocode_records' consolidation below,
# never to build a query itself (extraction.geocode's own callers handle
# that; appending a bare district code to a query was confirmed
# empirically to be unreliable — sometimes helps, sometimes breaks an
# otherwise-working match entirely).
_TRAILING_DISTRICT_RE = re.compile(r",?\s*[A-Za-z]{1,2}\d[A-Za-z\d]?\s*$")

# A comma-separated segment that's ENTIRELY just a bare postal district
# (e.g. the "W1" in "5 Swallow Place, W1") — as opposed to a real street
# address (e.g. the "38 Jermyn Street" in "Princes House, 38 Jermyn
# Street"). Used to tell apart the two different reasons a Building
# string can contain a comma when retrying a failed geocode query below.
_BARE_DISTRICT_RE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?$")


def _normalize_building_for_grouping(building):
    stripped = _TRAILING_DISTRICT_RE.sub("", building or "").strip().rstrip(",").strip()
    return stripped.lower()


def _address_retry_candidates(building, digit_address):
    """Ordered fallback address strings to retry when the first geocode
    attempt (this record's own Building text as given) fails — the
    caller tries each in turn, stopping at whichever one succeeds:

    1. digit_address, if given (a spelled-out house number converted to
       digits, e.g. "Thirty One Alfred Place" -> "31 Alfred Place") —
       still a confident full street address, just spelled out.
    2. `building` with a trailing bare postal district (no street of its
       own, e.g. the "W1" in "5 Swallow Place, W1") stripped entirely —
       confirmed empirically that appending a bare district code to a
       query is unreliable (sometimes helps, sometimes breaks an
       otherwise-working match), so this only ever tries WITHOUT one,
       never a query built from the code alone (which was confirmed to
       resolve to some generic district-wide centroid — shared
       identically by every OTHER building that also fell back to it).
    3. The address portion of a "Name, Address[, DistrictCode]" string
       (e.g. "Princes House, 38 Jermyn Street" or, with a district code
       also present, "Princes House, 38 Jermyn Street, SW1Y") —
       confirmed empirically (Crown Estate, 2026-07) that Nominatim
       returns NO MATCH AT ALL for the combined name+address string,
       even though the address portion alone resolves correctly every
       time it was tested — tried both with and without a trailing bare
       district code of its own, since that combination was also
       confirmed to sometimes fail where the bare address alone
       succeeds."""
    seen = set()
    candidates = []

    def add(value):
        value = (value or "").strip().strip(",").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    if digit_address:
        add(digit_address)

    no_district = _TRAILING_DISTRICT_RE.sub("", building or "").strip()
    add(no_district)

    for base in (building or "", no_district):
        if "," not in base:
            continue
        before_comma, after_comma = (p.strip() for p in base.split(",", 1))
        if _BARE_DISTRICT_RE.match(after_comma):
            add(before_comma)
        else:
            add(after_comma)
            add(_TRAILING_DISTRICT_RE.sub("", after_comma).strip())

    return candidates


def process_files(
    paths,
    deadline=None,
    source_urls=None,
    source_url_expected=False,
    brochure_enrichment=False,
    brochure_fetcher=None,
    brochure_extractor=None,
    batch_total_files=None,
    batch_file_index=None,
):
    """Returns a list of per-file dicts:
    {filename, status: "ok"|"error", method: "rule:<Name>"|"llm"|None,
     records, record_count, error, warning, provider_name, date}
    provider_name/date/records are only meaningful when status == "ok".

    deadline: a time.monotonic() value beyond which _geocode_records stops
    attempting further per-building lookups for the REST of the batch —
    see BATCH_DEADLINE_SECONDS above for why. Computed fresh from here
    (BATCH_DEADLINE_SECONDS from now) if not given, so a caller with no
    opinion (e.g. tests/test_examples.py's own direct usage) still gets a
    real safety net; app.py's own /api/process passes one computed at the
    true start of request handling, before this function is ever called,
    so time already spent reading files/calling the LLM for an earlier
    file in the same batch correctly eats into the budget for a later
    one's geocoding too — this is one shared deadline for the whole
    batch, not reset per file.

    batch_total_files / batch_file_index: when the app finishes each file
    (materialize + write + free embeds) before starting the next — required
    on Render free-tier ~512MB — it calls this with one path at a time.
    Pass the overall batch size/index so fair enrichment shares still
    reserve time for later files (otherwise each solo call would take the
    entire remaining pool).

    warning is set alongside a normal "ok" status/None error — any of: a
    PDF bigger than what's actually been tested end-to-end
    (extraction.file_readers.TESTED_MAX_PDF_PAGES/TESTED_MAX_PDF_BYTES —
    distinct from that module's own hard MAX_PDF_PAGES ceiling, which is
    a real error instead), a file that extracted successfully but hit
    Gemini's daily quota partway through its own address-lookup fallback,
    and/or one that hit the batch deadline before every building could be
    looked up — in every case, some rows' Lat/Lng/Property Postcode fell
    back further than usual (to a less reliable bare-name Nominatim
    match, or to "Needs manual lookup" outright) — none of these is
    something that failed the file itself.
    """
    if deadline is None:
        deadline = time.monotonic() + BATCH_DEADLINE_SECONDS

    results = []
    path_list = list(paths)
    total_files = max(1, int(batch_total_files) if batch_total_files is not None else len(path_list))
    _enrich_pool_end = deadline - ENRICHMENT_FINALIZE_RESERVE_SECONDS

    for local_index, path in enumerate(path_list):
        file_index = int(batch_file_index) if batch_file_index is not None else local_index
        filename = path.name if hasattr(path, "name") else str(path)
        source_url = _source_url_for(path, filename, source_urls)
        report = ProcessingReport(filename)
        result = {
            "filename": filename,
            "status": "ok",
            "method": None,
            "records": [],
            "record_count": 0,
            "error": None,
            "warning": None,
            "provider_name": None,
            "display_name": None,
            "email_html": None,
            "pages_text": None,
            "html_items": None,
            "row_links": None,
            "processing_report": None,
            "properties": [],
            "spreadsheet_diagnostics": None,
        }
        memlog.log("before file parsing", filename)
        try:
            content = read_file(path)
            document = RawDocument(filename, content, source_url)
            report.record("READ", "PASS", item_count=1)
        except ValueError as e:
            report.record("READ", "FAIL", str(e))
            result["status"] = "error"
            result["error"] = str(e)
            result["processing_report"] = report.as_dict()
            results.append(result)
            continue
        except Exception as e:
            report.record("READ", "FAIL", f"Unexpected error: {e}")
            result["status"] = "error"
            result["error"] = f"Unexpected error reading file: {e}"
            result["processing_report"] = report.as_dict()
            results.append(result)
            continue
        memlog.log("after file parsing", filename)

        # Set as soon as it's known (rather than only at the very end)
        # so a later warning — e.g. Gemini quota exhaustion, below — can
        # append to it instead of clobbering it; see extraction.
        # file_readers.TESTED_MAX_PDF_PAGES/TESTED_MAX_PDF_BYTES for what
        # this is actually based on (a real, repeatedly-tested figure,
        # not a guess) and why it's a warning, not an error, unlike the
        # separate hard MAX_PDF_PAGES ceiling.
        if content.get("size_warning"):
            result["warning"] = content["size_warning"]

        # An .eml's own HTML body (already parsed by file_readers, not
        # re-rendered) — lets app.py link Link to File at that HTML
        # directly instead of the raw .eml, so it opens in-browser with its
        # original images (the markup already points at the sender's
        # hosted image URLs) rather than downloading a mail file. Falls
        # back to None for a plain-text-only email, or anything else.
        if path.suffix.lower() == ".eml" and content.get("html"):
            result["email_html"] = content["html"]

        # Per-page PDF text, so app.py's Floor Plan/High Res Images
        # enrichment (extraction.pdf_images) can tell which source page a
        # given LLM-extracted listing came from — None for anything that
        # isn't a PDF (an .eml/table-based source has no "pages" concept).
        if path.suffix.lower() == ".pdf" and content.get("pages_text"):
            result["pages_text"] = content["pages_text"]

        # The same (kind, text_or_alt, href_or_src) stream a rule like
        # extraction.rules.knotel/metspace/gpe already reads directly from
        # content — needed here too so app.py's generic Floor Plan/High
        # Res Images/Brochure PDF enrichment for an .eml/.html source with
        # NO dedicated rule (extraction.html_images) has the same raw
        # material to work from. None for anything with no HTML structure
        # at all (PDF/DOCX/XLSX/CSV).
        if content.get("html_items"):
            result["html_items"] = content["html_items"]

        # .xlsx/.xls only: real per-row hyperlink data (extraction.
        # file_readers._extract_xlsx_row_links) that pandas' own cell-value
        # read discards entirely — needed here so app.py's generic
        # Brochure PDF/Floor Plan enrichment for a raw-spreadsheet source
        # with no dedicated rule (extraction.xlsx_links) has real link
        # data to work from. Confirmed real (2026-07, UNION): a source
        # xlsx's own "Brochure" column links every row to a real
        # brochure/floor-plan URL through a hyperlink on a generic display
        # cell ("CLICK HERE"), never the URL as visible text — invisible
        # to the LLM's own plain-text prompt input, built from those same
        # pandas-read values.
        if content.get("row_links"):
            result["row_links"] = content["row_links"]

        # Rules (UNION especially) match on filename / sheet names — without
        # this, a single-tab export of "Clerkenwell & Farringdon" that omits
        # the word "union" in cell text falls through to the LLM and OOMs.
        content["filename"] = filename
        content["source_file_name"] = filename

        rule_name, raw_records = try_rules(content)
        llm_source_name = None
        if raw_records:
            result["method"] = f"rule:{rule_name}"
            report.record("EXTRACTION", "PASS", f"rule:{rule_name}", len(raw_records))
            if rule_name == "Spreadsheet Blocks":
                blocks = len({((r.get("_spreadsheet_block") or {}).get("sheet"), (r.get("_spreadsheet_block") or {}).get("address_row")) for r in raw_records})
                result["spreadsheet_diagnostics"] = {
                    "property_blocks_detected": blocks,
                    "deterministic_blocks_parsed": blocks,
                    "llm_blocks_required": 0,
                    "final_records": len(raw_records),
                    "largest_llm_prompt_chars": 0,
                    "largest_llm_response_chars": 0,
                    "failed_chunks": [],
                }
                report.record("SPREADSHEET_STRUCTURE_DETECTED", "PASS", "Repeated address/header blocks", blocks)
                report.record("PROPERTY_BLOCKS_FOUND", "PASS", item_count=blocks)
                report.record("DETERMINISTIC_BLOCKS_PARSED", "PASS", item_count=blocks)
                report.record("LLM_BLOCKS_REQUIRED", "PASS", item_count=0)
        else:
            memlog.log("before LLM call", filename)
            try:
                if is_spreadsheet(content) and is_large_spreadsheet(content):
                    raw_records, llm_source_name, diagnostics = extract_in_chunks(content, source_hint=filename)
                    result["method"] = "llm:chunked"
                    result["spreadsheet_diagnostics"] = diagnostics
                    report.record("LARGE_FILE_CHUNKED", "PASS", item_count=diagnostics["chunks"])
                    report.record("LLM_CHUNK_SUCCESS", "PASS", item_count=diagnostics["successful_chunks"])
                    if diagnostics["failed_chunks"]:
                        report.record("LLM_CHUNK_FAILED", "WARNING", item_count=len(diagnostics["failed_chunks"]))
                        report.record("PARTIAL_EXTRACTION", "WARNING", "Successful chunks retained", len(raw_records))
                        result["warning"] = "Some spreadsheet sections could not be extracted; successful sections are included. Please review this output for missing rows."
                else:
                    try:
                        raw_records, llm_source_name = extract_with_llm(content["text"], source_hint=filename)
                        result["method"] = "llm"
                    except LLMExtractionError as e:
                        err = str(e).lower()
                        if is_spreadsheet(content) and (
                            "not valid json" in err
                            or "truncated" in err
                            or "max_tokens" in err
                            or "max output" in err
                            or "output size" in err
                        ):
                            # The single-call response was truncated (hit
                            # MAX_OUTPUT_TOKENS) before the density/row
                            # thresholds selected chunking — confirmed real
                            # ("Workplace Plus - London.xlsx", 2026-07): dense
                            # enough per-row text that the JSON output still
                            # overran the token budget mid-string. Retrying
                            # the SAME full-text prompt would truncate at the
                            # same point again (this is exactly what
                            # extract_with_llm's own retry_malformed already
                            # tried and still failed), so fall back to bounded
                            # per-chunk extraction instead of failing the
                            # whole file.
                            raw_records, llm_source_name, diagnostics = extract_in_chunks(content, source_hint=filename)
                            result["method"] = "llm:chunked"
                            result["spreadsheet_diagnostics"] = diagnostics
                            report.record("LARGE_FILE_CHUNKED", "PASS", item_count=diagnostics["chunks"])
                            report.record("LLM_CHUNK_SUCCESS", "PASS", item_count=diagnostics["successful_chunks"])
                            result["warning"] = (
                                "This file's single-call extraction exceeded Gemini's output size limit "
                                "(more/denser listing text than its row count suggested), so it was "
                                "automatically re-processed in smaller chunks instead."
                            )
                            if diagnostics["failed_chunks"]:
                                report.record("LLM_CHUNK_FAILED", "WARNING", item_count=len(diagnostics["failed_chunks"]))
                                result["warning"] += " Some spreadsheet sections could not be extracted; please review this output for missing rows."
                        else:
                            raise
                report.record("EXTRACTION", "PASS", "llm fallback", len(raw_records))
            except LLMExtractionError as e:
                memlog.log("after LLM call (raised LLMExtractionError)", filename)
                result["status"] = "error"
                result["error"] = str(e)
                report.record("EXTRACTION", "FAIL", str(e))
                result["processing_report"] = report.as_dict()
                results.append(result)
                continue
            except Exception as e:
                memlog.log("after LLM call (raised unexpected exception)", filename)
                result["status"] = "error"
                result["error"] = f"Unexpected error during LLM extraction: {e}"
                report.record("EXTRACTION", "FAIL", result["error"])
                result["processing_report"] = report.as_dict()
                results.append(result)
                continue
            memlog.log("after LLM call", filename)
            # LLM JSON + source text can leave a large transient spike; free
            # what we can before brochure enrichment (UNION Box PDFs) runs.
            content["text"] = ""
            gc.collect()

        normalized = [normalize_record(r) for r in raw_records]
        normalized = [r for r in normalized if r.get("Building") or r.get("Area")]
        report.record("NORMALISATION", "PASS" if normalized else "FAIL", item_count=len(normalized))
        if not normalized:
            result["status"] = "error"
            result["error"] = "No usable records found in this file"
            result["processing_report"] = report.as_dict()
            results.append(result)
            continue

        # Generic asset/brochure discovery belongs in the pipeline, before
        # typed properties and validation.  These helpers only fill blanks,
        # so dedicated provider rules retain priority.
        if (result["method"] or "").startswith("llm") and content.get("html_items"):
            html_images.enrich_records(normalized, content["html_items"])
        if (result["method"] or "").startswith("llm") and content.get("row_links"):
            xlsx_links.enrich_records(normalized, content["row_links"])

        # Resolved before geocoding (rather than after, as before) so the
        # web-search fallback can pass it along as disambiguating context
        # (e.g. "Elsley GPE Fully Managed" instead of just "Elsley").
        # Snapshot every image supplied by the uploaded file before optional
        # linked-source enrichment adds anything. The web layer trusts these
        # source-derived candidates and must never blank them merely because
        # an external validator is slow or unavailable.
        for record in normalized:
            source_images = list(record.get("_high_res_candidates") or [])
            existing_image = str(record.get("High Res Images") or "").strip()
            if existing_image:
                source_images.insert(0, existing_image)
            if source_images:
                record["_source_high_res_candidates"] = list(dict.fromkeys(source_images))


        provider_name = resolve_provider_name(rule_name, filename, llm_source_name)

        # Create the canonical properties before secondary-source address
        # enrichment.  A brochure postcode can therefore prevent a weaker
        # geocoder result from ever overwriting it.
        properties = [
            Property.from_record(
                record,
                document.source_file_name,
                provider_name,
                result["method"] or "unknown",
                document.source_file_url,
                source_url_expected,
            )
            for record in normalized
        ]

        enrichment_deadline = None
        if brochure_enrichment:
            # Fair share of remaining enrichment time when THIS file starts —
            # not an absolute clock from batch start (that left Knotel with
            # ~5s after MetSpace overrun while WP still got a full window).
            files_remaining = max(1, total_files - file_index)
            remaining_enrich = max(0.0, _enrich_pool_end - time.monotonic())
            enrichment_deadline = time.monotonic() + remaining_enrich / files_remaining
            kwargs = {}
            if brochure_fetcher is not None:
                kwargs["fetcher"] = brochure_fetcher
            if brochure_extractor is not None:
                kwargs["extractor"] = brochure_extractor
            kwargs["deadline"] = enrichment_deadline
            if enrichment_deadline <= time.monotonic() + 1:
                report.record(
                    "BROCHURE_ENRICHMENT", "WARNING",
                    "Optional linked-source enrichment skipped to preserve the request deadline.",
                    len(properties),
                )
                kwargs["deadline"] = time.monotonic()
            properties = brochure.enrich_properties(properties, **kwargs)
            brochure_issues = [issue for prop in properties for issue in prop.issues if issue.stage.startswith(("brochure_", "linked_source_"))]
            report.record("BROCHURE_ENRICHMENT", "WARNING" if brochure_issues else "PASS", f"{len(brochure_issues)} linked-source issue(s)" if brochure_issues else "Optional linked-source enrichment complete", len(properties))
        normalized = [prop.values for prop in properties]

        # Geocode only with surplus left before this file's enrichment
        # deadline — never spend later files' brochure shares on Nominatim.
        files_after = max(0, total_files - file_index - 1)
        if files_after > 0 and enrichment_deadline is not None:
            geocode_deadline = min(deadline, enrichment_deadline)
            if geocode_deadline <= time.monotonic():
                geocode_deadline = time.monotonic()
        elif files_after > 0:
            rem = max(0.0, _enrich_pool_end - time.monotonic())
            geocode_deadline = time.monotonic() + rem / (files_after + 1)
        else:
            geocode_deadline = deadline
        quota_exhausted, deadline_hit = _geocode_records(normalized, filename, provider_name, geocode_deadline)
        report.record(
            "ENRICHMENT",
            "WARNING" if quota_exhausted or deadline_hit else "PASS",
            "Address lookup quota/deadline limited some records" if quota_exhausted or deadline_hit else "Address enrichment complete",
            len(normalized),
        )

        # Deliberately AFTER geocoding, not before: _geocode_records (and
        # everything it calls — _geocode_query, _address_retry_candidates,
        # the is_bare_name web-search branch) reads Property Address 1 as
        # the full "Name, Street, City Postcode" text straight from
        # Building, exactly as it always has — that's what its own retry
        # logic was built around (a combined name+address string can
        # confuse Nominatim; see this module's docstring). Only now, once
        # nothing further needs that fuller text, is Property Address 1
        # overwritten with a clean street-only value for the actual
        # spreadsheet output — Building itself is never touched.
        for record in normalized:
            record["Property Address 1"] = street_address_only(record.get("Building"))
        for prop, record in zip(properties, normalized):
            diagnostics = record.get("_address_resolution") or {}
            if diagnostics and "not in source text" in str(record.get("Property Postcode") or "").lower():
                source = diagnostics.get("final_postcode_source") or "validated_lookup"
                confidence = float(diagnostics.get("confidence") or 0)
                prop.provenance["Property Postcode"] = FieldProvenance(
                    source=source,
                    method="address_resolution",
                    confidence=confidence,
                    original_value=record.get("Property Postcode"),
                    source_document=diagnostics.get("selected_candidate"),
                )

        properties = validate_properties(properties)
        report.issues.extend(issue for prop in properties for issue in prop.issues)
        report.record(
            "FINAL_VALIDATION",
            "WARNING" if report.issues else "PASS",
            f"{len(report.issues)} issue(s) require review" if report.issues else "All records passed structural validation",
            len(properties),
        )
        normalized = [prop.to_record() for prop in properties]

        if quota_exhausted:
            # Scoped to this file's own note, not a batch-wide error — the
            # file's records extracted fine; this only affects rows whose
            # address had to fall back to the web-search tier and hit the
            # daily limit there specifically (a plain building name with
            # no street/postcode in the source at all — see
            # _geocode_records below). Appended, not overwritten — a
            # large/untested-size warning may already be set above, and a
            # file can genuinely have both going on at once.
            quota_note = (
                quota.reset_message("Gemini's daily address-search limit")
                + " Some rows' Property Address/Postcode/Lat/Lng fell back to a plain "
                "building-name lookup, which is less reliable — worth a manual check "
                'for any row marked "(Not in source text)".'
            )
            result["warning"] = f"{result['warning']} {quota_note}" if result["warning"] else quota_note

        if deadline_hit:
            # Same scoping principle as the quota note above — this file's
            # records still extracted fine, only some rows' address lookup
            # was skipped once the whole batch's shared time budget ran out
            # (see BATCH_DEADLINE_SECONDS) rather than risking a SIGKILL
            # that would have returned nothing for the whole batch instead.
            deadline_note = (
                "This file has enough ambiguous buildings that address lookup couldn't finish within the "
                'time available for this batch. Some rows are marked "Needs manual lookup" that would '
                "otherwise have been looked up automatically — try processing this file on its own, or in "
                "a smaller batch, to give it the full time budget."
            )
            result["warning"] = f"{result['warning']} {deadline_note}" if result["warning"] else deadline_note

        # Prefer the source document's own date (email Date header, or PDF/
        # DOCX metadata) over processing time, so External Ref reflects when
        # the listing was actually sent/dated, not when someone happened to
        # run this batch. Only falls back to today when neither is available.
        ref_date = resolve_source_date(content) or date.today().strftime("%Y-%m-%d")
        external_ref = f"{provider_name}_{ref_date}"
        for prop, record in zip(properties, normalized):
            record["External Ref"] = external_ref
            # Keep the typed model authoritative.  The web application may
            # attach the durable source URL after persistence and serialize
            # the properties again; late export fields must survive that.
            prop.values["External Ref"] = external_ref

        # The output spreadsheet's own display name — same provider_name,
        # same ref_date as External Ref above (one consistent date across
        # both, rather than maintaining two separate resolutions), plus an
        # area/subset disambiguator when one's available. Confirmed real
        # (2026-07): UNION exports the same provider+date combination as
        # several separate area-based files (City, Aldgate & Whitechapel,
        # Shoreditch, ...) in one sitting, so provider+date alone produced
        # indistinguishable filenames for genuinely different files.
        # extract_area_hint (the ORIGINAL uploaded filename's own trailing
        # " - <area>" segment, when present) is tried first — it's the
        # most specific, human-authored signal; area_from_records (every
        # extracted row sharing one Area value) is a weaker fallback for
        # when the filename itself gives no hint. Neither found is a
        # normal, expected case (most sources' filenames/Area values never
        # carry this kind of split), not a failure — the name is just
        # provider_name plus the date, exactly as before this existed.
        area_hint = extract_area_hint(filename, provider_name) or area_from_records(normalized)
        display_name = f"{provider_name} - {area_hint}" if area_hint else provider_name
        result["display_name"] = f"{display_name}_{ref_date}"

        result["records"] = normalized
        result["properties"] = properties
        result["record_count"] = len(normalized)
        result["provider_name"] = provider_name
        report.record("EXPORT_READY", "PASS", item_count=len(normalized))
        result["processing_report"] = report.as_dict()
        results.append(result)

    # Mirroring both on-disk lookup caches to durable storage (once per
    # batch, not once per record — see _save_cache in each module) used to
    # happen synchronously right here. Confirmed via Render's own logs that
    # a worker was once killed while stuck inside exactly this call —
    # a real network round-trip to B2/S3 that can run long — which a
    # generic SIGKILL then gets misreported as "Perhaps out of memory?"
    # regardless of the real cause. Moved to the same background-thread
    # pattern app.py already uses for every other storage.upload call
    # (see app.py's _flush_caches, started right after this function
    # returns) so it can never block the HTTP response or contribute to a
    # worker timeout.
    return results


def _source_url_for(path, filename, source_urls):
    if not source_urls:
        return None
    if callable(source_urls):
        return source_urls(path)
    return source_urls.get(path) or source_urls.get(str(path)) or source_urls.get(filename)


def _geocode_records(records, filename, provider_name, deadline):
    """Fills Lat/Lng in place for each record via extraction.geocode.
    geocode() caches on disk by address string, so repeat buildings (e.g.
    several floors in the same Knotel building) cost one lookup, not one
    per row. Failures are never fatal to the row — Lat/Lng are just left
    blank, with a clear note printed for whoever's running the batch.

    Also backfills Property Postcode from Nominatim's address breakdown
    when the source text didn't have one (e.g. MetSpace's "9-10 Market
    Place" has no postcode at all) — only as a fallback; a postcode already
    parsed from the source text is never overwritten.

    Lat/Lng/Property Postcode are required fields. When the source gives
    nothing but a bare building name (no street/house number at all — not
    even one spelled out in words) there's a real risk of geocoding it to
    the wrong place entirely: a bare-name Nominatim search for "Porters
    Place" alone matched a street in Barbados, and "Elsley" alone matched a
    building in Battersea (SW11) when the real GPE-managed "Elsley" is in
    Fitzrovia (W1W) — and critically, Nominatim returned a match either
    way, so this can't be caught by "retry only if the direct lookup found
    nothing." For a genuinely bare name, this never trusts a direct/bare
    Nominatim match as the primary result at all — it tries an actual web
    search FIRST, Gemini + Google Search grounding
    (extraction.address_lookup), with the source/provider name included as
    disambiguating context (e.g. "Elsley GPE Fully Managed" instead of
    just "Elsley"). Only if that finds nothing does this fall back to a
    plain bare-name Nominatim search, still exposed to the same risk.
    Either way, the result is marked "(Not in source text)" in the output,
    since it reflects a real gap in the source document, not a
    wrong-vs-right judgment on the value itself — it's never used to
    silently overwrite Property Address 1/Building.

    Returns (quota_exhausted, deadline_hit):
    - quota_exhausted is True if the web-search tier hit Gemini's daily
      quota limit for at least one record in this file. Once that
      happens, every LATER bare-name record in this SAME call skips the
      web-search attempt entirely (falling straight to the bare-name
      Nominatim tier) rather than making another Gemini call guaranteed
      to hit the same daily 429 — confirmed via real Render logs
      (2026-07) that retrying it anyway for every remaining bare-name
      building wastes real time (extraction.address_lookup's own rate-
      limit pacing still waits before each attempt even though it's
      certain to fail), contributing to the exact timeout risk deadline
      (below) exists to guard against.
    - deadline_hit is True if `deadline` (a time.monotonic() value) was
      reached before every record could be looked up — remaining
      records are marked "Needs manual lookup" immediately, without
      attempting a lookup at all, rather than risking gunicorn's own
      --timeout SIGKILLing the whole request (confirmed this actually
      happened, 2026-07 — see BATCH_DEADLINE_SECONDS above).
    Either way, process_files turns this into a per-file "warning" (not
    "error"; the records themselves still extracted fine) so a batch
    that had to fall back further than usual is explained rather than
    silently degraded."""
    # When the SAME building appears more than once in this file with
    # different amounts of qualifying detail (e.g. Crown Estate's "1 Vine
    # Street, W1" for one floor vs a plain "1 Vine Street" for a different
    # floor elsewhere in the same document — confirmed the source PDF
    # itself just doesn't repeat the area code in every section), geocode
    # every occurrence using whichever text is richest/most qualified.
    # Confirmed empirically that the bare version alone can resolve
    # confidently to a coincidentally-real but wrong address (Walthamstow,
    # ~12km from the real Mayfair one) with no second Nominatim candidate
    # for extraction.geocode's own ambiguity check to catch, while the
    # qualified version resolves correctly every time.
    richest_building = {}
    for record in records:
        building = (record.get("Property Address 1") or "").strip()
        if not building:
            continue
        base = _normalize_building_for_grouping(building)
        if base not in richest_building or len(building) > len(richest_building[base]):
            richest_building[base] = building

    quota_exhausted = False
    daily_quota_hit = False
    deadline_hit = False
    for record in records:
        if deadline - time.monotonic() < OPTIONAL_LOOKUP_START_SECONDS:
            deadline_hit = True
            record["Lat"] = "Needs manual lookup"
            record["Lng"] = "Needs manual lookup"
            if not record.get("Property Postcode"):
                record["Property Postcode"] = "Needs manual lookup"
            _safe_print(
                f"[geocode] (batch deadline reached) {filename}: skipping remaining lookups — "
                f"'{(record.get('Property Address 1') or '').strip()}' marked Needs manual lookup"
            )
            continue

        building = (record.get("Property Address 1") or "").strip()
        has_digit = any(ch.isdigit() for ch in building)
        # Nominatim can't match a building number spelled out in words
        # (e.g. "Thirty One Alfred Place" for "31 Alfred Place") — this
        # still counts as a confident full street address, just spelled
        # out, not the bare-name case below.
        digit_address = spelled_number_to_digits(building) if building and not has_digit else None
        is_bare_name = bool(building) and not has_digit and not digit_address

        # The record actually asked about below always keeps its own
        # Building/Property Address 1 text — this only substitutes a
        # richer sibling's text for the *query* sent to the geocoder.
        query_source = record
        if building and not is_bare_name:
            richer = richest_building.get(_normalize_building_for_grouping(building))
            if richer and richer != building:
                query_source = {**record, "Property Address 1": richer}

        query = _geocode_query(query_source)
        attempted_queries = [query] if query else []
        derived_note = False
        sources = []

        if is_bare_name:
            lat = lng = geo_postcode = None
            error = None
            if daily_quota_hit:
                # Already confirmed exhausted for today by an earlier
                # building in this same batch — every later attempt is
                # certain to hit the identical 429 (Gemini's daily quota
                # doesn't reset mid-batch), so skip straight to the
                # bare-name Nominatim fallback below instead of wasting
                # real time (extraction.address_lookup's own rate-limit
                # pacing still waits before making a call that's
                # guaranteed to fail) on a call we already know the
                # answer to.
                web_address, web_sources, hit_quota = None, [], False
            else:
                web_address, web_sources, hit_quota = find_address_via_web_search(building, provider_name)
                if hit_quota:
                    quota_exhausted = True
                    daily_quota_hit = True
            if web_address:
                web_query = _geocode_query({"Property Address 1": web_address})
                attempted_queries.append(web_query)
                lat, lng, geo_postcode, error = geocode(web_query)
                if lat is not None:
                    query = web_query
                    # Prefer the postcode actually present in the found
                    # address text over Nominatim's address-breakdown
                    # postcode — confirmed empirically that Nominatim can
                    # tag a wide building polygon with a different, coarser
                    # postcode than the specific address searched for (e.g.
                    # "11 St John Street" geocodes to a building spanning
                    # house numbers 11-33, whose OSM postcode, EC1M 4NX,
                    # doesn't match number 11's real postcode).
                    geo_postcode = extract_postcode(web_address) or geo_postcode
                    derived_note = True
                    sources = web_sources

            if lat is None:
                # Web search found nothing confident enough (unconfigured,
                # not enough independent sources, or genuinely not found)
                # — last resort: Nominatim on just the bare name. Same
                # risk as before (a coincidental match elsewhere), so
                # still flagged if it does find something. confident=False
                # so this specific result is never trusted from cache on a
                # future run (extraction.geocode.geocode) — otherwise a
                # run where the web-search tier fails for a transient
                # reason (e.g. quota exhaustion) permanently poisons the
                # cache with this tier's own coincidental-match risk,
                # exactly like the bug this same safeguard already fixed
                # once for extraction.address_lookup's own cache.
                lat, lng, geo_postcode, error = geocode(query, confident=False)
                if lat is not None:
                    derived_note = True
        else:
            lat, lng, geo_postcode, error = geocode(query)
            if lat is None:
                for candidate_address in _address_retry_candidates(building, digit_address):
                    retry_query = _geocode_query({**record, "Property Address 1": candidate_address})
                    if retry_query == query:
                        continue
                    attempted_queries.append(retry_query)
                    retry_lat, retry_lng, retry_postcode, retry_error = geocode(retry_query)
                    if retry_lat is not None:
                        query, lat, lng, geo_postcode, error = retry_query, retry_lat, retry_lng, retry_postcode, retry_error
                        break
            # A numbered address can still be ambiguous when the source
            # omits its postcode (44 vs 44A Pentonville Road is the real
            # regression). If deterministic geocoding rejects every
            # house-number-consistent candidate, use the existing grounded
            # lookup tier rather than accepting a neighbour or silently
            # leaving a blank. This remains bounded by the same quota and
            # batch deadline as bare-name enrichment.
            if lat is None and not record.get("Property Postcode") and not daily_quota_hit:
                web_address, web_sources, hit_quota = find_address_via_web_search(building, provider_name)
                if hit_quota:
                    quota_exhausted = True
                    daily_quota_hit = True
                if web_address:
                    web_query = _geocode_query({"Property Address 1": web_address})
                    attempted_queries.append(web_query)
                    web_lat, web_lng, web_postcode, web_error = geocode(web_query)
                    if web_lat is not None:
                        query, lat, lng, error = web_query, web_lat, web_lng, web_error
                        geo_postcode = extract_postcode(web_address) or web_postcode
                        derived_note = True
                        sources = web_sources

        postcode_from_geocode = False
        if not record.get("Property Postcode") and geo_postcode:
            record["Property Postcode"] = geo_postcode
            postcode_from_geocode = True

        diagnostics = get_resolution_diagnostics(query)
        if diagnostics:
            record["_address_resolution"] = {**diagnostics, "query_variants": list(dict.fromkeys(attempted_queries))}

        if lat is not None:
            if derived_note:
                # The source gave nothing but a bare building name — this
                # value was derived (web search or a bare-name geocode),
                # not read directly from the source document. Flagged in
                # the spreadsheet itself, not just the console log, so
                # it's distinguishable at a glance.
                record["Lat"] = f"{lat} (Not in source text)"
                record["Lng"] = f"{lng} (Not in source text)"
                # Surfaced in the spreadsheet too (spreadsheet.write_xlsx
                # attaches this as a cell comment on Lat) — so a wrong
                # answer is traceable to what it was actually based on,
                # not just an opaque coordinate.
                if sources:
                    record["_geocode_sources"] = sources
                sources_note = f" Sources: {'; '.join(sources)}." if sources else ""
                _safe_print(
                    f"[geocode] (Not in source text) {filename}: '{building}' -> '{query}': "
                    f"lat={lat}, lng={lng}.{sources_note} No street/postcode in the source — verify before relying on it."
                )
            else:
                record["Lat"] = lat
                record["Lng"] = lng
            # Property Postcode's own provenance is a DIFFERENT question
            # from Lat/Lng's (derived_note above): a numbered street
            # address can geocode confidently enough that Lat/Lng need no
            # flag at all, while the POSTCODE specifically still came from
            # Nominatim's own address breakdown, not the source document,
            # whenever the source never had one to begin with. Confirmed a
            # real bug (2026-07, MetSpace): its source email never states
            # a postcode for ANY building, numbered street address or
            # not, but this flag previously only applied when derived_note
            # was ALSO true (i.e. only for a bare-name match) — a
            # confident numbered-address geocode's postcode went
            # completely unflagged, indistinguishable from one actually
            # read out of the source text. Geocoding CONFIDENCE
            # (derived_note) and postcode PROVENANCE
            # (postcode_from_geocode) are different questions; flagged
            # independently here so conflating them can't happen again.
            if postcode_from_geocode:
                record["Property Postcode"] = f"{geo_postcode} (Not in source text)"
        else:
            # Required fields — flag directly in the cell, not just the
            # console log, so a genuine geocoding gap is visible to anyone
            # opening the spreadsheet, distinguishable from a field that's
            # blank for some other reason.
            record["Lat"] = "Needs manual lookup"
            record["Lng"] = "Needs manual lookup"
            if not record.get("Property Postcode"):
                record["Property Postcode"] = "Needs manual lookup"
            prefix = "[geocode] (bare building name, no match found) " if is_bare_name else "[geocode] "
            target = query or building or "(blank)"
            _safe_print(f"{prefix}{filename}: could not geocode '{target}': {error}")

    return quota_exhausted, deadline_hit


def _safe_print(message):
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


# Outward postcode areas that are not London — appending ", London, UK"
# (Workplace Plus Manchester M1/M2) makes Nominatim fail entirely.
_NON_LONDON_POSTCODE_AREA_RE = re.compile(
    r"^\s*(LS|NE|NG|BS|CF|EH|AB|BT|GY|JE|IM|M|B|G|L|S)\d",
    re.I,
)
_CITY_FROM_POSTCODE = {
    "M": "Manchester",
    "B": "Birmingham",
    "G": "Glasgow",
    "L": "Liverpool",
    "LS": "Leeds",
    "S": "Sheffield",
    "NE": "Newcastle",
    "NG": "Nottingham",
    "BS": "Bristol",
    "CF": "Cardiff",
    "EH": "Edinburgh",
    "AB": "Aberdeen",
}


def _geocode_query(record):
    """Shape a geocoder query from address + postcode.

    Bare London streets get ", London, UK". Non-London UK postcodes
    (Manchester M*, Birmingham B*, …) get their own city — never London.
    This only shapes the search query; spreadsheet address fields are
    untouched. Deliberately omits informal Area labels (West End, etc.).
    """
    address = (record.get("Property Address 1") or "").strip()
    if not address:
        return ""

    postcode = (record.get("Property Postcode") or "").strip()
    if postcode and postcode not in address:
        address = f"{address}, {postcode}"

    lower = address.lower()
    if "london" in lower or re.search(
        r"\b(manchester|birmingham|glasgow|liverpool|leeds|sheffield|newcastle|nottingham|bristol|cardiff|edinburgh)\b",
        lower,
    ):
        if not lower.endswith("uk"):
            address = f"{address}, UK" if ", uk" not in lower else address
        return address

    area_match = _NON_LONDON_POSTCODE_AREA_RE.match(postcode or "")
    if not area_match:
        # Postcode may already be inside the address string.
        embedded = re.search(
            r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*\d[A-Z]{2}\b",
            address,
            re.I,
        )
        if embedded:
            area_match = _NON_LONDON_POSTCODE_AREA_RE.match(embedded.group(1))
    if area_match:
        prefix = area_match.group(1).upper()
        city = _CITY_FROM_POSTCODE.get(prefix)
        if not city and len(prefix) >= 2:
            city = _CITY_FROM_POSTCODE.get(prefix[:2])
        if city and city.lower() not in lower:
            address = f"{address}, {city}, UK"
        elif not lower.endswith("uk"):
            address = f"{address}, UK"
        return address

    address = f"{address}, London, UK"
    return address
