"""LLM-based extraction fallback, used when no rule-based parser recognizes
a file's layout. Calls the Gemini API with the raw extracted text and
asks for JSON matching the target schema; validates the response before it
ever reaches the spreadsheet.
"""
import json
import os
import re

from . import quota
from .hard_timeout import call_with_timeout
from .schema import LLM_FIELDS

# Beyond the visible spreadsheet columns in LLM_FIELDS, also ask for a raw
# sale-price signal — some sources list a genuine per-listing sale price
# alongside their rental price (originally found on BC's own "Current
# Availability" table, which now has its own dedicated rule —
# extraction.rules.bc — and reads this same signal directly from its own
# Sale Price column instead of through this prompt; kept here too for any
# other LLM-fallback source with the same pattern). schema.normalize_record
# uses this to set "For Sale" instead of hardcoding it. Not a spreadsheet
# column itself, so it's kept separate from LLM_FIELDS/schema.COLUMNS.
EXTRA_FIELDS = ["Sale Price"]
ALL_FIELDS = LLM_FIELDS + EXTRA_FIELDS

# Floor Plan and High Res Images are never trusted from the LLM's own text
# extraction, unlike every other field — real links for these two are
# always resolved afterwards from the source PDF's actual embedded images
# (see app.py's _attach_pdf_images). Source-file provenance is set
# separately by app.py on `_source_file_*` fields. Confirmed empirically on
# a Business Cube brochure: without this, the LLM copied the brochure's
# own "Example Floorplan" heading text into the Floor Plan field verbatim,
# producing a plausible-looking but entirely non-clickable placeholder
# (plain text, no hyperlink) whenever no real image match overwrote it
# afterwards. Excluded from the prompt entirely rather than just
# discarded after the fact, so the model doesn't waste output tokens
# guessing at fields it was never going to be trusted for anyway.
#
# Brochure PDF is excluded for the same reason: it's only ever a real
# href pulled directly from a source's own HTML (currently just Knotel's
# "View Brochure" link — see extraction.rules.knotel), never text the
# LLM fallback could plausibly reconstruct a working URL for from raw
# extracted text alone.
PROMPT_FIELDS = [f for f in ALL_FIELDS if f not in ("Floor Plan", "High Res Images", "Brochure PDF")]

# gemini-3.5-flash's free tier is capped at just 20 requests/day (Google's
# newest flash-tier model). gemini-3.1-flash-lite is explicitly positioned
# for high-volume, cost-sensitive traffic and gets a much more generous
# free-tier daily quota, while still supporting JSON mode and thinking_config.
MODEL = "gemini-3.1-flash-lite"
MAX_TEXT_CHARS = 40000  # keep prompts bounded; most sources are far shorter,
# but a multi-page brochure with 40+ listings can get close to this.
MAX_OUTPUT_TOKENS = 24000  # a 17-field JSON schema repeated per listing adds
# up fast — a ~40-listing document needs well over the old 4000-token budget.
# The model's ceiling is 65536; this leaves comfortable headroom for larger ones.

# Hard wall-clock deadline (extraction.hard_timeout), comfortably under
# gunicorn's own 120s worker timeout — see call_with_timeout below for why
# google-genai's own http_options.timeout isn't enough on its own for a
# large-output call like this one.
CALL_TIMEOUT_SECONDS = 60


class LLMExtractionError(Exception):
    pass


def extract_with_llm(
    text,
    source_hint="",
    max_output_tokens=MAX_OUTPUT_TOKENS,
    retry_malformed=False,
    include_metrics=False,
):
    """Returns (records, source_name). source_name is the LLM's best guess at
    the sender/broker/company this document is from (used to name the output
    spreadsheet), or "" if it couldn't confidently tell."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMExtractionError(
            "No GEMINI_API_KEY set — cannot use the LLM fallback for this file. "
            "Add it to your .env file (see README)."
        )

    max_output_tokens = max(512, min(int(max_output_tokens), MAX_OUTPUT_TOKENS))

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise LLMExtractionError("The 'google-genai' package is not installed — run: pip install -r requirements.txt")

    truncated = text[:MAX_TEXT_CHARS]
    prompt = _build_prompt(truncated, source_hint)

    try:
        # vertexai=False pins the client to the Gemini Developer API backend
        # explicitly. Without it, an ambient environment variable the SDK
        # checks implicitly (GOOGLE_GENAI_USE_VERTEXAI) can silently reroute
        # it to the Vertex AI backend instead, which authenticates via
        # OAuth2/Application Default Credentials rather than an API key —
        # producing a 401 ACCESS_TOKEN_TYPE_UNSUPPORTED error that has
        # nothing to do with whether GEMINI_API_KEY itself is valid.
        client = genai.Client(api_key=api_key, vertexai=False)

        def _call():
            return client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                    # Keep the token budget for the actual JSON response,
                    # not hidden reasoning — max_output_tokens caps both
                    # combined.
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                    # Also set, but confirmed (2026-07, via a real Render
                    # crash) NOT sufficient on its own for a large-output
                    # call like this one — see call_with_timeout below for
                    # the actual enforcement. Kept as a reasonable inner
                    # hint/backup; doesn't hurt.
                    http_options=types.HttpOptions(timeout=CALL_TIMEOUT_SECONDS * 1000),
                ),
            )

        # Confirmed via a real Render crash log (2026-07) that http_options.
        # timeout above, on its own, did NOT stop this from hanging past
        # gunicorn's own 120s worker timeout — verified empirically
        # (a 1ms http_options.timeout against the real API failed almost
        # instantly, so the mechanism itself works) that httpx's timeout
        # bounds the gap *between* chunks of data arriving, not the total
        # call duration: a large response (this call asks for up to
        # MAX_OUTPUT_TOKENS) can keep trickling data slowly enough that no
        # single inter-chunk gap ever exceeds the configured timeout, while
        # the call's *total* duration still runs far longer than it. A
        # worker blocked that way in a low-level SSL socket read (confirmed
        # from the actual crash traceback: stuck in ssl.py's recv, deep
        # inside httpx/httpcore) can't be interrupted cleanly by gunicorn's
        # own signal-based --timeout either, so gunicorn eventually has to
        # escalate to a raw SIGKILL — logged as "Perhaps out of memory?"
        # regardless of the real cause. call_with_timeout enforces a true,
        # independent wall-clock deadline on the whole call instead.
        # Wrapped in call_with_overload_retry so a transient 503 "high
        # demand" error gets a couple of automatic short-wait retries
        # before this file counts it as a real failure — see that
        # function's own docstring for why this is deliberately NOT the
        # same handling as the 429 quota error below.
        response = quota.call_with_overload_retry(
            lambda: call_with_timeout(_call, CALL_TIMEOUT_SECONDS), label=source_hint
        )
    except Exception as e:
        # Check for an auth/permission failure defensively (by status code,
        # not by exception class) — a previous version only caught
        # google.genai.errors.APIError specifically, which turned out not
        # to match whatever's actually being raised for this failure mode,
        # so it silently fell through to the generic message below every
        # time. Always include the real exception's type in that generic
        # message now, so if this still doesn't match, the next occurrence
        # tells us exactly what to catch instead of us guessing again.
        code = getattr(e, "code", None) or getattr(e, "status_code", None)
        err_text = str(e)
        if "timeout" in type(e).__name__.lower() or "timeout" in err_text.lower():
            # The http_options timeout above tripped — a slow/hanging
            # network path, not necessarily anything wrong with the
            # request itself. Distinct message so this reads as "safe to
            # just retry", not a real extraction failure.
            raise LLMExtractionError(
                f"Gemini API call timed out without a response ({type(e).__module__}.{type(e).__name__}) "
                "— likely a slow network path rather than a problem with this file. Safe to retry."
            )
        if quota.is_quota_exceeded(e):
            # Distinct from every other failure below: this file's own
            # extraction didn't go wrong, Gemini's free-tier daily quota
            # for this model is simply used up for today. Framed
            # per-file/scoped (never "the app is down") since a
            # rule-based source (Knotel/MetSpace/GPE/Kitts/BC/Breezblok)
            # never reaches this code path at all and keeps processing
            # normally in the same batch.
            raise LLMExtractionError(
                quota.reset_message("Gemini's daily AI-extraction limit")
                + " This file needs the AI fallback because no known-provider parser "
                "(Knotel, MetSpace, GPE, Kitts, BC, Breezblok) recognized its layout — "
                "files that DO match one of those aren't affected and will still "
                "process normally."
            )
        is_auth_error = (
            code in (401, "401", 403, "403")
            or "UNAUTHENTICATED" in err_text
            or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in err_text
            or "PERMISSION_DENIED" in err_text
        )
        if is_auth_error:
            raise LLMExtractionError(
                f"Gemini API rejected the request as unauthenticated "
                f"({code if code else 'no code attribute'} — {getattr(e, 'message', None) or e}). "
                "This is not necessarily GEMINI_API_KEY being wrong — it can also mean "
                "something is causing the client to attempt OAuth/ADC auth instead of "
                "using the key. Check aistudio.google.com/apikey for the key's validity, "
                "and rule out any Google Cloud-related env vars."
            )
        raise LLMExtractionError(f"Gemini API call failed [{type(e).__module__}.{type(e).__name__}]: {e}")

    raw = response.text or ""
    try:
        records, source_name = _parse_and_validate(raw)
    except LLMExtractionError:
        if not retry_malformed:
            raise
        # Retry one bounded malformed chunk; never repair or repeat a giant request.
        response = quota.call_with_overload_retry(
            lambda: call_with_timeout(_call, CALL_TIMEOUT_SECONDS), label=f"{source_hint} retry"
        )
        raw = response.text or ""
        records, source_name = _parse_and_validate(raw)
    if not records:
        raise LLMExtractionError("LLM returned no usable records for this file")
    if include_metrics:
        return records, source_name, {"prompt_chars": len(prompt), "response_chars": len(raw)}
    return records, source_name


def _build_prompt(text, source_hint):
    fields = ", ".join(f'"{f}"' for f in PROMPT_FIELDS)
    return (
        "You extract commercial office-space listings from arbitrary documents "
        "(broker emails, PDFs, spreadsheets) into a fixed JSON schema.\n\n"
        "Return ONLY a JSON object (no markdown, no commentary) shaped like:\n"
        '{"source_name": "...", "listings": [...]}\n\n'
        '"source_name" is the sender/broker/company this document is from, if evident '
        "(e.g. from a letterhead, email signature, or branding) — a short name suitable "
        'for use as a filename (e.g. "MetSpace", "Acme Realty"). If you cannot confidently '
        'identify it, use "".\n\n'
        f'"listings" is a JSON array. Each element is one listing (one floor/unit = one '
        f"element) with exactly these string fields: [{fields}].\n\n"
        "Rules:\n"
        '- If a field isn\'t present in the source, use "" (empty string) — never omit a field or invent data.\n'
        "- \"Size (sq ft)\", \"Desks (max)\", the two \"Marketing Price...\" fields should be plain numbers "
        "as strings (no currency symbols, no commas, no units) — e.g. \"4284\" not \"4,284 sqft\".\n"
        "- If only a monthly price OR only a price-per-sqft is given, still only fill in the one you can "
        "read directly from the source — do not calculate the other yourself.\n"
        "- A single flexible space described with a SIZE RANGE (e.g. \"ranges from 1,593 sq ft to 2,729 "
        "sq ft\", \"up to 2,729 sq ft\", \"1,593-2,729 sq ft\") is ONE listing, not one listing per size "
        "in the range — never create a separate row for the lower bound and another for the upper bound. "
        "Use the UPPER (larger) bound as \"Size (sq ft)\" (the same convention as a desk-count range like "
        "\"20-27 desks\", which also always uses the upper bound).\n"
        "- A single listing described with MULTIPLE PRICING TIERS/OPTIONS (e.g. \"traditional lease from "
        "£13,907 pcm\" vs \"fully managed from £17,400 pcm\") is ALSO one listing, not one row per tier — "
        "never create a separate row per pricing option. Use the FIRST/lower-priced tier (usually the one "
        "quoted earliest, e.g. in the listing's own opening sentence) for \"Marketing Price (Based on Min "
        "Term) PCM\"/\"...PSF\", and name every OTHER tier and its own price explicitly in \"Special "
        "Features\" (e.g. \"Fully managed option available from £17,400 pcm\") so it isn't silently lost — "
        "never merge, average, or drop a second tier's price.\n"
        "- \"Area\" is the neighbourhood/district name if given (e.g. \"Mayfair\", \"Fitzrovia\").\n"
        "- \"Building\" must include any postal district/area code printed directly next to the "
        "building or street name in the source (e.g. \"25 Bury Street, SW1\" not just \"25 Bury "
        "Street\"; \"1 Vine Street, W1\" not just \"1 Vine Street\") — never drop it even if the "
        "same building's other listings elsewhere in the document don't repeat it.\n"
        "- \"Contacts\" should list every named contact/broker/agent for that listing, comma-separated "
        "(e.g. \"Kieran Christie, Sophie Haugh, Nicki Mayle\") — however many are actually given, not "
        "just one or two. If the source names no individual person but does give a company/agency "
        "acting as sole agent (e.g. \"Strictly by appointment through the sole agents, Acme Realty\"), "
        "use that company name instead — only use \"\" if the source gives no contact of any kind, "
        "named or otherwise.\n"
        "- \"Special Features\" can combine any descriptive text that doesn't fit another field.\n"
        '- "Sale Price" is a distinct per-listing sale price, only if the source explicitly lists one '
        "separate from its rental price (e.g. a table with both a \"Market Price\" rental column and a "
        "\"Sale Price\" column) — use \"\" if the source shows no such value, or shows \"N/A\"/\"-\" for "
        "that listing's sale price specifically. Never infer a sale price from the rental price.\n"
        '- If the document has no office-listing data at all, return {"source_name": "", "listings": []}.\n\n'
        f"Source file hint: {source_hint or 'unknown'}\n\n"
        "Document text:\n"
        "-----\n"
        f"{text}\n"
        "-----"
    )


def _parse_and_validate(raw):
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        # raw_decode (not json.loads) deliberately: confirmed empirically
        # on a real Crown Estate response that the model can otherwise
        # return a fully complete, valid JSON object with one stray
        # trailing character after it (e.g. an extra '"' with nothing
        # else following) — json.loads rejects the entire response for
        # that ("Extra data"), even though the actual listings data is
        # intact and parses correctly. raw_decode parses just the first
        # complete JSON value and ignores anything after it, so a
        # genuinely truncated/malformed prefix (the failure mode this
        # should still catch) still raises, but a complete answer with
        # harmless trailing noise doesn't get thrown away.
        data, _ = json.JSONDecoder().raw_decode(cleaned)
    except json.JSONDecodeError as e:
        raise LLMExtractionError(f"LLM response was not valid JSON: {e}")

    if not isinstance(data, dict) or "listings" not in data:
        raise LLMExtractionError('LLM response was valid JSON but not shaped like {"source_name", "listings"}')

    listings = data.get("listings")
    if not isinstance(listings, list):
        raise LLMExtractionError("LLM response's \"listings\" field was not a list")

    source_name = data.get("source_name")
    source_name = source_name.strip() if isinstance(source_name, str) else ""

    records = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        record = {}
        for field in ALL_FIELDS:
            v = item.get(field, "")
            record[field] = "" if v is None else str(v)
        # Belt-and-braces: force blank even if the model returns these keys
        # anyway (not asked for — see PROMPT_FIELDS above) or copies a
        # source heading into them unprompted.
        record["Floor Plan"] = ""
        record["High Res Images"] = ""
        record["Brochure PDF"] = ""
        records.append(record)
    return records, source_name
