# Reliable extraction pipeline

## Data flow

The application preserves the established provider parsers and output columns,
but each batch now follows explicit stage contracts:

1. **Read** — `file_readers.read_file` produces document content and structural metadata.
2. **Extract** — a provider parser is tried through the common rule interface; implausible or unsupported layouts use the LLM fallback.
3. **Normalize** — `schema.normalize_record` maps values by semantic column name and derives only established Kato fields.
4. **Enrich** — address/geocode lookup runs with a batch deadline and retains source-derived values when lookup fails.
5. **Validate** — dictionary records are wrapped in the canonical `Property` model, preserving field provenance and accumulating typed `ValidationIssue` objects.
6. **Discover/classify assets** — `assets.py` provides the common candidate pipeline: URL normalization, deduplication, deterministic classification, then later assignment. Provider-specific parsers may supply stronger context where their layout is known.
7. **Export** — `spreadsheet.write_xlsx` uses the centralized `COLUMNS` mapping and writes hyperlinks by field name. A `QA Review` sheet lists validation problems without changing the established Listings layout.
8. **Report** — every file result includes `processing_report`, containing PASS/WARNING/FAIL stage entries and review issues.

## Typed contracts

`extraction.models` defines `RawDocument`, `Property`, `FieldProvenance`,
`AssetCandidate`, `ValidationIssue`, `ProcessingReport`, and stage results. Provider
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
