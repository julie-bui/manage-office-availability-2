# Reliable extraction pipeline

## Data flow

The application preserves the established provider parsers and output columns,
but each batch now follows explicit stage contracts:

1. **Read** — `file_readers.read_file` produces document content and structural metadata.
2. **Extract** — a provider parser is tried through the common rule interface; implausible or unsupported layouts use the LLM fallback.
3. **Normalize** — `schema.normalize_record` maps values by semantic column name and derives only established Kato fields.
4. **Discover assets and brochures** — generic HTML/spreadsheet link discovery supplements provider rules without replacing their stronger layout knowledge.
5. **Enrich** — address lookup runs with a batch deadline. When enabled by the application, `brochure.py` reads valid brochure PDFs as secondary evidence, classifies their media, and merges only reliable values using confidence-aware conflict rules. A brochure failure never aborts primary extraction.
6. **Resolve conflicts** — blank or weak values can be filled or improved; strong conflicting primary values are retained and produce a review issue.
7. **Validate** — records use the canonical `Property` model, preserving primary and brochure provenance and accumulating typed `ValidationIssue` objects.
8. **Export** — `spreadsheet.write_xlsx` uses the centralized `COLUMNS` mapping and writes hyperlinks by field name. A `QA Review` sheet lists validation problems without changing the established Listings layout.
9. **Report** — every file result includes `processing_report`, containing PASS/WARNING/FAIL stage entries and review issues.

## Typed contracts

`extraction.models` defines `RawDocument`, `Property`, `ExtractedValue`,
`BrochureExtraction`, `FieldProvenance`, `AssetCandidate`, `ValidationIssue`,
`ProcessingReport`, and stage results. Provider
rules remain compatible with their existing dictionary output, but dictionaries
no longer cross final validation without being converted into a canonical model.

## Reliability principles

- Discovery and classification are separate from asset assignment.
- Brochures, floorplans and property photographs have distinct classifications.
- Invalid or conflicting values are retained for traceability and explicitly flagged.
- A per-record failure does not abort the batch; programming errors outside expected parser input failures are not silently swallowed.
- Address enrichment never erases a plausible extracted address after a failed lookup.
- Export order comes from one named schema, never scattered positional indexes.
- External requests remain bounded by existing caches, quotas and the batch deadline.

## Address resolution

Address resolution is shared by every provider through
`extraction.address_resolution`; provider rules must not invent their own
geocoder acceptance rules. The staged priority is:

1. Preserve a valid address/postcode explicitly present in the uploaded source.
2. Use validated secondary evidence already associated with that property,
   including brochures, landlord/property pages, and agent listings.
3. Generate deterministic exact-address query variants only for components
   that remain missing.
4. Parse every returned candidate into building number, building name, street,
   locality, and postcode; compare multiple candidates using transparent
   weighted agreement.
5. Use geocoding as supporting/fallback evidence, never as automatic truth.
6. Require manual review only after all safe variants and evidence sources have
   been exhausted.

An explicit building-number mismatch or clearly different street is a hard
rejection, regardless of geographic proximity. Exact building number and street
agreement carry the highest score; building name, locality, and postcode are
supporting signals. Independent sources agreeing on the same property/postcode
increase confidence. Credible disagreement is retained as a conflict rather
than guessed away, and a weaker lookup never overwrites a valid source or
brochure postcode.

Resolution diagnostics retain the original address/postcode, attempted query
variants, considered and rejected candidates with reasons, selected candidate,
agreeing evidence sources, confidence/status, and final address/postcode source.
Normal spreadsheet rows receive only the selected values; unresolved or
conflicting outcomes are summarized in `QA Review`. Candidate lookup results
continue to use the versioned on-disk/durable cache, so a repeated address is
not queried repeatedly within a batch or across runs.

## Adding a provider

Implement `detect(content)` and `parse(content)` in `extraction/rules/`, return
the established source-field names, and register the module in `RULES`. Keep
only layout-specific interpretation in the provider module; reuse normalization,
asset classification, validation, enrichment and export stages.

## Verification

Run both suites:

```sh
.venv/bin/python tests/test_examples.py
.venv/bin/python -m pytest -q
```

The first suite pins real sample behavior and exact record counts. The pytest
suite covers typed validation, postcode handling, address retries, asset
classification/deduplication, optional values and spreadsheet mapping.
