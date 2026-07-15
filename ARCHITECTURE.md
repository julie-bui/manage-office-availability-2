# Reliable extraction architecture

## 1. Purpose and design principle

This application converts heterogeneous commercial-property availability files
into structured Excel workbooks. Inputs may be PDF, DOCX, XLSX/XLS, CSV, EML,
or HTML. Known providers use deterministic parsers where their layouts are
reliably understood; other compatible inputs can use a generic LLM fallback.

Reliability is difficult because provider layouts differ, source values may be
missing or inconsistent, addresses may require external evidence, hyperlinks
can be hidden in document structure, and visually similar assets can have very
different meanings. A plausible upstream error must not silently become an
authoritative spreadsheet value.

The intended shared lifecycle is:

`extract → normalise → validate → enrich → classify → validate → export`

The current implementation performs one explicit property-validation pass after
normalisation, typed conversion, address enrichment, and applicable asset
discovery. It does not yet expose a separate initial property-validation stage.
Throughout the implemented path, explicit uncertainty and QA visibility are
preferred to silent guessing.

## 2. Repository, branch, and runtime context

The refactor is developed in `julie-bui/manage-office-availability-2` on
`refactor/reliable-extraction-pipeline`. This document does not imply any change
to `lauriethomasson/manage-office-availability`.

- Entry point: `app.py`, a Flask web application.
- Runtime: Python, installed from `requirements.txt`.
- Deployment definition: `render.yaml` configures a Render web service using
  `pip install -r requirements.txt` and Gunicorn.
- Gunicorn safeguards: one worker, a 120-second timeout, and worker recycling
  after 15 requests plus jitter to limit cumulative native-library memory
  growth.
- Required deployed secrets: `GEMINI_API_KEY` for LLM/search functionality and
  `ACCESS_TOKEN` for application access. Values are environment variables and
  are never stored in this document.
- Optional persistence: S3-compatible storage configured through the variables
  listed in `.env.example`.
- External services: Gemini for fallback extraction and grounded address
  lookup, OpenStreetMap Nominatim for geocoding, and optional S3-compatible
  object storage.
- Request budget: `extraction.pipeline.BATCH_DEADLINE_SECONDS` is 100 seconds,
  leaving response/export margin below Gunicorn's 120-second ceiling.

PDF/image processing is deliberately page-bounded and workers are recycled
because PyMuPDF, Pillow, Google client libraries, and related native components
can retain memory between requests.

## 3. Actual end-to-end pipeline

`extraction.pipeline.process_files` isolates each input file and returns one
result object per file. A failure in one file does not discard successful files
from the same batch.

| Stage | Code | Input → output | Failure behaviour |
|---|---|---|---|
| File ingestion | `app.py`, `extraction.file_readers` | Uploaded path → parsed content dictionary | Unsupported, unreadable, empty, oversized beyond a hard limit, or unparseable files produce a per-file error. Other files continue. |
| Raw document boundary | `extraction.models.RawDocument` | Filename, parsed content, optional hosted URL → typed document context | Created immediately after a successful read; read failure is fatal only for that file. |
| Provider detection | `extraction.rules.try_rules` | Parsed content → matching rule or no match | Expected layout/data exceptions reject that rule and continue. Implausible rule output is rejected by `rule_sanity`. |
| Deterministic extraction | `extraction/rules/*` | Provider/layout content → raw record dictionaries | Preferred for supported layouts. Minor row problems may be skipped by the rule. No usable output proceeds to fallback. |
| Generic LLM fallback | `extraction.llm_fallback` | Plain document text → raw record dictionaries | Used only when no trusted rule output exists. Missing credentials, timeout, malformed output, or API failure becomes a per-file extraction error. |
| Normalisation | `extraction.schema.normalize_record` | Raw dictionaries → named `COLUMNS` records | Missing fields become empty values; numeric fields are coerced and established derived fields are calculated. No usable building/area rows is fatal for that file. |
| Generic link discovery | `extraction.html_images`, `extraction.xlsx_links` | HTML items or spreadsheet hyperlinks → semantic link/image candidates on records | Best effort and currently applied to applicable LLM-fallback inputs. Failure does not replace primary extraction. |
| Canonical property boundary | `extraction.models.Property` | Normalised records → typed properties with provenance and source-file identity | All successfully extracted records enter this boundary before address enrichment and final validation. |
| Brochure enrichment | `extraction.brochure` | Valid brochure URL → resolved PDF/HTML content, typed field evidence, and classified media | Provider-neutral and triggered by the asset, not provider name. Redirects are followed; HTML pages may lead to downloadable PDFs. Retrieval/parsing failure adds an informational diagnostic and preserves primary extraction. |
| Address enrichment | `extraction.pipeline._geocode_records`, `address_resolution`, `address_lookup`, `geocode` | Existing address/postcode → coordinates, optional postcode, diagnostics | Existing valid postcodes are preserved. Rejected or unavailable candidates lead to explicit uncertainty/manual lookup; the property remains. Deadline/quota degradation becomes a file warning. |
| Application media finalisation | `app.py`, `pdf_images`, `html_images` | Embedded or hosted candidates → floorplan/image links or gallery pages | Best effort. Missing or ambiguous media remains blank rather than being guessed. |
| Final validation | `extraction.validation` | Typed properties → properties plus `ValidationIssue` objects | Validation retains source values, marks review requirements, and does not abort the batch. |
| Spreadsheet export | `spreadsheet.write_xlsx` | Named records → XLSX | Uses the central schema order and semantic field lookup. Export/storage failures are request-level operational failures rather than silently malformed rows. |
| QA and reporting | `spreadsheet._write_qa_sheet`, `ProcessingReport` | Validation/stage diagnostics → `QA Review` and per-file report | Warnings/errors remain associated with the relevant file/property/field. |

Some application-level image attachment occurs after pipeline serialisation
because hosted download/gallery URLs do not exist until a batch directory and
download route have been created. These operations still assign by semantic
field name rather than positional column index.

## 4. Canonical typed models

`extraction.models` supplies the shared contracts:

- `RawDocument`: original source filename, parsed content, optional source URL,
  and optional provider context. It establishes source identity immediately
  after ingestion.
- `Property`: provider, named values, source filename/URL, whether a hosted URL
  was expected, per-field provenance, asset candidates, validation issues, and
  `review_required`. `to_record()` is the typed-to-export boundary.
- `FieldProvenance`: source, method, confidence, original value, and optional
  source document. It describes why a value exists without changing the public
  spreadsheet schema.
- `AssetCandidate`: original/final URL, source file/provider/section/block,
  nearest-property and row/cell/container context, surrounding text, discovery
  method, association confidence, dimensions/repetition, validation status,
  classification/confidence, and optional embedded content/hash/extension.
- `LinkDiagnostic`: resolution, resource typing, identity decision, source
  context, validation/gallery outcome, original/final URL, and human-readable
  detail for one linked-resource decision.
- `BrochureResource`: retrieved bytes, response content type, and final URL
  after redirects.
- `ExtractedValue` and `BrochureExtraction`: typed secondary field evidence
  (value, source document, method, confidence) and the combined field/media
  result for one resolved brochure destination.
- `ValidationIssue`: field, message, severity, value, suggested action, and
  producing stage.
- `StageResult`: stage name, status, message, and item count.
- `ProcessingReport`: per-file stage results and issues, with a derived review
  requirement.
- `AddressComponents`, `AddressCandidate`, `CandidateAssessment`, and
  `AddressResolution` in `extraction.address_resolution`: comparable address
  identity, scored/rejected candidate evidence, attempted queries, selected
  result, confidence, sources, and final status.

Provider parsers still return dictionaries internally. This is an intentional
incremental migration boundary: dictionaries are normalised and wrapped in
`Property` before shared enrichment, validation, and export.

## 5. Provenance

`Property.from_record` creates `FieldProvenance` for non-empty source fields,
recording the source filename and extraction method (`rule:<provider>` or
`llm`). Later trusted transformations may replace the provenance entry with the
actual enrichment source and confidence. Address-resolution diagnostics are
stored internally under `_address_resolution`; grounded address sources may
also be retained under `_geocode_sources` and surfaced as a spreadsheet cell
comment.

Provenance supports debugging, validation, conflict analysis, QA, and the rule
that weaker enrichment must not silently replace stronger source data. The
confidence values are implemented metadata used by typed enrichment/conflict
logic; they are not displayed as a normal Listings column.

The system does not yet provide complete field-level provenance for every
legacy provider parser's internal transformation. That remains an incremental
migration area.

## 6. Provider extraction strategy

Known deterministic rules are registered in this order:

1. Knotel
2. MetSpace
3. Workplace Plus
4. GPE
5. BC
6. Breezblok
7. Grid/Tabular

Each module exposes `detect(content)` and `parse(content)`. The first plausible,
non-empty result is used. `rule_sanity.records_look_plausible` prevents a layout
variant from being accepted merely because provider detection matched.

`try_rules` catches only expected input/layout failures (`KeyError`,
`TypeError`, `ValueError`, `IndexError`, and `AttributeError`). Unexpected
programming errors are not broadly suppressed. Unknown or incompatible layouts
continue to the generic LLM fallback where their file type/content is supported.

The latest local real-example run completed successfully with these pinned
counts: Knotel 16 and 15 rows across two fixtures; MetSpace 14; GPE 15;
Grid/Tabular (Kitt's) 57; BC 11; and Breezblok 1. The exact July regression
fixtures additionally produce one MetSpace row and four Workplace Plus rows.
These figures describe deterministic/local regression execution, not a live
credentialed API verification.

## 7. Validation architecture

`extraction.validation.validate_property` accumulates issues without erasing
the value being reviewed. Warning and error severities set
`Property.review_required`; informational issues do not.

### Address and postcode

- Building and address presence are required structurally.
- UK postcodes are extracted and normalised by `address.extract_postcode` and
  checked against the implemented UK format.
- Missing sizes, invalid postcodes, derived postcodes, and manual-lookup values
  create review issues with suggested actions.
- A valid source postcode is not overwritten by geocoding.
- Nominatim candidates are not accepted simply because they are first or near.
  They enter the shared component matcher.
- Building number and street are the strongest identity signals. An explicit
  different building number or street is a hard rejection.
- Consequently, another numbered property on the same street cannot supply the
  requested property's postcode. `49 Southwark Bridge Road` versus `138
  Southwark Bridge Road` is preserved as a provider-neutral regression test.
- Geocoding is supporting/fallback evidence. Unresolved identity remains
  explicit instead of becoming a guessed postcode.

### URLs and semantic assets

`assets.normalize_url` accepts only HTTP(S), lowercases scheme/host, removes
known tracking parameters/fragments, and rejects malformed or unsafe schemes.
Candidates are deduplicated on the normalised URL. Validation checks URL syntax
for source links, brochure links, floorplans, and property images.

Brochure links are resolved through redirects and may represent direct PDFs,
HTML property/brochure pages, or pages linking to downloadable PDFs. Extracted
text can supply reliable missing Special Features, floor/unit availability,
size, minimum-term information, postcode, and descriptive specification,
amenity, pricing/service-charge/rates, lease, or sustainability notes where the
implemented deterministic signals find them. These values are typed secondary
evidence: blanks may be filled, weak lower-confidence values may be improved,
and strong conflicts retain the primary value and create a QA issue.

HTML images/links and PDF annotations/embedded images become `AssetCandidate`
objects before assignment. Embedded PDF visuals are conservatively classified
using page labels, pixel characteristics, dimensions, and repeated-content
signals. Repeated/small decorative graphics, logos, maps, and unknown assets are
not assigned as property photographs. The application persists accepted
embedded images only after a batch download namespace exists.

Floorplans and ordinary photographs remain separate scalar spreadsheet fields.
Validation flags a brochure URL duplicated into either media field and flags a
floorplan duplicated as a property image. Multi-photo sets created by the web
application are represented by one hosted gallery URL; arbitrary list values
are not a supported public spreadsheet representation.

## 8. Asset discovery and classification

The deterministic asset process is:

`candidate discovery → URL normalisation → deduplication → classification → semantic assignment → conflict validation`

`extraction.assets.AssetType` currently includes `BROCHURE`, `FLOORPLAN`,
`PROPERTY_IMAGE`, `LOGO`, `MAP`, `DECORATIVE`, the legacy
`DOCUMENT_PREVIEW`, `TRACKING_OR_DECORATIVE`, and `UNKNOWN`.

Classification uses filename, URL path, MIME type, link text, alt text,
surrounding/container text, dimensions, repetition, and structural association
confidence. Strong floorplan, brochure, logo, map, document-preview, and
decorative/tracking signals take priority. An image extension or MIME type alone
does not make a property photograph: positive property context or a strong
record association is required; otherwise the candidate remains `UNKNOWN`.

Semantic conflicts become validation issues rather than silent category reuse.
Classification is deterministic but heuristic; ambiguous assets can still
require review.

### Generic linked-property-source enrichment flow

The implemented provider-neutral flow is:

`candidate link → HTTP(S)-only validation → bounded redirect resolution → final payload inspection → PDF, HTML, or image extraction → identity validation → typed text/media evidence → atomic confidence-aware merge → final validation`

`Brochure PDF` is the legacy spreadsheet field that triggers this shared stage;
it does not imply that the linked resource is a PDF. The resolver preserves the
original and final URLs and determines resource type from response headers plus
payload signatures. A misleading `.pdf` suffix cannot force PDF parsing, and an
extensionless URL remains usable when its actual content is supported. HTML
pages may contribute lightweight visible text/media and may lead to at most two
downloadable documents. Direct images are decoded and classified before they
can enter either media field.

Only public `http://` and `https://` references are followed. `mailto:`, `tel:`,
`javascript:`, `data:`, local files, custom schemes, private literal IPs, and
malformed references are never executed or fetched. Adding another scheme in
future requires an explicit safe resolver. Redirect loops/excess chains,
oversized responses, unsupported/corrupt content, login or JavaScript-only
pages, HTTP access/not-found/rate-limit/server errors, timeouts, SSL/DNS errors,
and anti-bot responses produce a structured skip/failure diagnostic while the
primary property remains usable.

Before any linked fields or media merge, `extraction.identity` emits exactly
`MATCH`, `PROBABLE_MATCH`, `AMBIGUOUS`, or `HARD_CONFLICT` using postcode,
numbered-street, building-name, and structural-association evidence. Both
`AMBIGUOUS` and `HARD_CONFLICT` are rejected; structural confidence can support
a generic landing page but can never override an explicit address conflict.
Successful enrichment is merged into a copy of the property and published atomically, so
an unexpected failure cannot expose a partially changed record. Strong primary
values still take precedence and credible conflicts still enter QA Review.

Internal observability records `LINK_RESOLVED`, `LINK_REDIRECT_RESOLVED`,
`LINK_RESOURCE_PDF`, `LINK_RESOURCE_HTML`, `LINK_RESOURCE_IMAGE`,
`LINK_UNSUPPORTED`, `LINK_ACCESS_DENIED`, `LINK_NOT_FOUND`, `LINK_TIMEOUT`,
`LINK_RATE_LIMITED`, all four `LINK_IDENTITY_*` decisions,
`LINK_ENRICHMENT_SKIPPED`, `LINK_ENRICHMENT_SUCCESS`, and
`LINK_ENRICHMENT_FAILED`. Media finalisation also records discovery, rejection,
inaccessibility, direct assignment, and gallery success/failure. These records
are surfaced in `QA Review` without changing the Listings schema.

Brochure media is collection-oriented: every relevant HTML image (including
lazy-load, responsive `srcset`, and social-preview attributes) and every
meaningful embedded image on all inspected PDF pages becomes a separate typed
candidate. Candidates are deduplicated only by their complete canonical URL or
exact content hash, never by host, filename pattern, brochure, or page. Existing
source photographs and classified brochure photographs are combined before the
application performs one final gallery pass. Classified floorplans remain on a
separate path to `Floor Plan`; they are never passed to the photo gallery.

The application materialises embedded brochure assets before gallery
generation. It validates each canonical external image URL once per finalisation
pass with bounded requests, content-type and image decoding, size checks, and
redirect preservation. Rejected links never enter a successful gallery. A
gallery is published only after its file exists and contains every validated
image; otherwise the output stays blank and QA records the reason.

Public document-viewer pages are resolved at the hosting-platform layer. For
example, a public Google Drive PDF viewer is converted to its public document
download and the PDF itself is inspected; the viewer's single preview bitmap
is not mistaken for the complete brochure. This resolver is provider-neutral
and applies equally to dedicated parsers, generic extraction, and future
unknown providers.

It runs for deterministic provider records and LLM/generic records alike because
the trigger is a valid linked resource in `Brochure PDF`, not a provider name.
Retrieved destinations are cached
within the batch so several rows sharing one brochure do not refetch/reparse it.
At most two linked document assets are followed from one HTML page, and each
retrieval is subject to a six-second request timeout and 20MB brochure limit.
The pipeline also caps the brochure stage to approximately 20 seconds per batch
and preserves an informational diagnostic for rows skipped after that budget.

Linked-source enrichment is optional. External property-image URLs and embedded photo bytes are added to the
property's media candidates. Existing direct property photos are retained and
combined into the application's gallery candidate list; an existing opaque
gallery URL is not blindly replaced. Floorplans are assigned only to `Floor
Plan`, while logos, maps, decorative assets, and unknowns remain excluded from
`High Res Images`.

Unsupported, difficult, expensive, inaccessible, or unsafe resources are
skipped without browser automation or bypass attempts. The primary uploaded
source and its extraction always remain authoritative.

## 9. Source-file provenance and `Link to File`

The four link concepts are intentionally different:

- `Link to File`: the original input/reference used to create the property.
- `Brochure PDF`: a brochure or marketing-document asset.
- `Floor Plan`: a floorplan/layout asset.
- `High Res Images`: property photographs or a generated photo gallery.

At ingestion, `RawDocument` captures `source_file_name` and optional
`source_file_url`. Every normalised row becomes a `Property` carrying those
fields. One input producing multiple properties therefore repeats the same
source identity on every row. `Property.to_record()` serialises
`_source_file_name`, `_source_file_url`, and `Link to File` when a real URL is
available; it never reconstructs the source from brochure or media fields.

In the web application, the uploaded artifact is copied to a collision-safe
batch filename (or an EML's HTML body is persisted for browser viewing), a real
download URL is created, and `Property.set_source_reference` updates the typed
properties before final serialisation. The spreadsheet displays the original
uploaded filename as hyperlink text and uses the stored URL as its target.

For local/test processing with no hosted URL, the source filename remains in
internal provenance and QA, but `Link to File` stays blank. No public URL is
fabricated. Without optional object storage, application download links are
ephemeral and may disappear after cleanup, restart, or redeployment.

## 10. Spreadsheet export safety

`extraction.schema.COLUMNS` is the single Listings column order.
`spreadsheet.write_xlsx` constructs each row with
`record.get(column_name, "")`; extraction and enrichment therefore cannot shift
cells by returning a shorter or reordered positional array.

Numeric and currency columns receive type-appropriate formats. Missing values
remain empty. HTTP(S) link fields become real Excel hyperlinks with concise
display text. `Link to File` displays the source filename; brochure, floorplan,
and image fields use their own labels. The exporter expects scalar schema
values. Multiple property photographs are exported through a single gallery
URL where application media finalisation creates one, not as an arbitrary list.

The `QA Review` sheet is added without changing the established `Listings`
column order.

## 11. QA Review

`QA Review` makes structural uncertainty visible before downstream upload. Each
row records source file, property/building, field, issue, severity, extracted
value, and suggested action. It receives the `ValidationIssue` objects and structured linked-media
 diagnostics serialised with each record.

Warnings and errors indicate that human review is required; informational rows
describe recoverable optional-enrichment failures without setting review status.
When no issues exist, the sheet contains an explicit "No validation issues
detected" informational row. QA does not silently correct questionable source
data; it explains what must be checked.

## 12. Processing diagnostics and observability

Each file has a `ProcessingReport`. `StageResult` records a stage name, one of
the current string statuses `PASS`, `WARNING`, or `FAIL`, a message, and an item
count. Implemented report stages include `READ`, `EXTRACTION`, `NORMALISATION`,
`ENRICHMENT`, `FINAL_VALIDATION`, and `EXPORT_READY`; optional stages may be
recorded when enabled by application features.

Expected file failures become a result with `status="error"`, its message, and
the partial processing report. Successful files include records, method,
provider/display name, warnings, typed properties, and processing report.

Address diagnostics can retain original address, attempted query variants,
considered candidates, scores, rejection flags/reasons, selected candidate,
confidence, status, and final address/postcode source. QA receives concise
unresolved/conflict summaries; detailed diagnostics remain internal metadata.
Secrets are never included.

## 13. Address-resolution strategy

`extraction.address_resolution` provides the provider-neutral identity model and
ranking rules:

1. Preserve a valid postcode already present in source data.
2. Accept trusted property-specific candidates supplied to the shared resolver.
3. Generate deterministic variants from known number, street, building name,
   locality, and postcode.
4. Collect and deduplicate candidates.
5. Parse requested/candidate values into building number, building name, street,
   locality, and postcode.
6. Hard-reject a missing/conflicting requested number or a conflicting street.
7. Score valid candidates: exact number and street dominate; name, locality, and
   postcode provide supporting agreement.
8. Prefer the strongest valid identity and increase confidence when independent
   sources agree on its postcode.
9. Preserve credible near-tied postcode disagreement as a conflict.
10. Require manual review when safe candidate paths produce no valid identity.

Implemented statuses are `RESOLVED_FROM_SOURCE`, `RESOLVED_FROM_BROCHURE`,
`RESOLVED_FROM_PROPERTY_PAGE`, `RESOLVED_FROM_VALIDATED_LOOKUP`,
`CONFLICTING_CANDIDATES`, `NO_VALID_CANDIDATE`, and
`MANUAL_REVIEW_REQUIRED`.

The pipeline also generates practical retry queries for numbered addresses and
uses grounded web search before low-confidence bare-name geocoding. The shared
resolver supports multiple trusted evidence candidates, but automatic discovery
and structured ingestion of every possible landlord/agent/property page is not
complete. Multi-source confidence therefore applies when those candidates or
grounded source agreement are actually available; it is not guaranteed for
every property.

Geocoding has a versioned cache, filters and ranks multiple Nominatim responses,
rejects wrong house numbers/streets, checks geographic/postcode ambiguity among
remaining matches, and records compact diagnostics. Proximity alone is never a
substitute for address identity.

## 14. Error-handling philosophy

Recoverable data problems—missing optional links, unresolved address, no safe
floorplan/image, unavailable optional lookup, quota exhaustion, and ambiguous
assets—preserve the primary property where safe. They become warnings,
validation issues, manual-lookup markers, or empty optional fields. Other files
in the batch continue.

Programming errors are different. Provider dispatch catches only the expected
layout/data exception types. Broadly swallowing every parser exception would
turn regressions into apparently successful but incomplete spreadsheets, so
unexpected rule defects are allowed to surface. Broad catches remain at true
external/recoverable boundaries such as network calls, file-format adapters,
and optional media extraction, where the code converts failures into explicit
operational results or safe degradation.

## 15. Timeouts, deadlines, and production safeguards

- One shared 100-second batch deadline stops additional per-building lookups
  before Gunicorn's 120-second timeout.
- Remaining unresolved rows are explicitly marked rather than allowing a
  worker kill to discard the whole response.
- Gemini calls use hard timeouts; grounded lookup has bounded retries and a
  rolling request-per-minute throttle.
- Nominatim uses a 10-second HTTP timeout, a descriptive user agent, and a
  minimum one-second interval.
- PDF parsing has a hard page limit and softer tested-size warnings.
- PDF image handling is page-bounded and deduplicates saved image content.
- Optional storage uploads and cache flushes run after response construction in
  background threads to avoid extending request latency.
- Gunicorn worker recycling bounds cumulative memory growth.

Remote validation is conservative, bounded, and cached per canonical URL.
Linked documents are fetched only during optional enrichment; external photo
URLs are fetched during media finalisation because invalid photos must not be
published as successful galleries.

## 16. Caching

- `extraction.geocode` caches address → latitude/longitude/postcode/error plus
  resolution diagnostics in `.geocode_cache.json`. A logic-version field
  invalidates results produced by older acceptance rules. Low-confidence
  bare-name results are not trusted as permanent confident answers.
- `extraction.address_lookup` caches building/provider grounded-search results,
  source domains, final/flaky state, and bounded empty-metadata misses in
  `.address_lookup_cache.json`. Transient quota/network failures are not cached
  as permanent negative answers.
- Repeated buildings within one file are consolidated around the richest known
  address and underlying caches prevent repeated calls across rows/runs.
- Both caches are local in process/on disk and can be mirrored once per batch to
  optional object storage. Render's ephemeral disk alone does not survive every
  restart/redeploy.

Caching reduces latency, Nominatim traffic, Gemini quota consumption, and
nondeterministic drift. Versioning and explicit invalidation prevent obsolete
logic from silently preserving known-bad answers.

## 17. Conflict resolution and source priority

The implemented invariant is that a weaker enrichment value cannot silently
overwrite a populated stronger source value. Specifically, an extracted valid
postcode is retained when geocoding disagrees, and incompatible asset reuse
becomes validation output.

The address resolver gives source postcode evidence immediate precedence,
hard-rejects identity conflicts, ranks remaining candidates, and records
credible postcode disagreement. Exact confidence/source precedence is applied
only where typed provenance exists; legacy rule-internal transformations do not
yet all expose granular confidence.

## 18. Testing and regression strategy

The pytest suite covers postcode extraction/normalisation, address parsing and
query generation, wrong-building-number/street rejection, the `49 Southwark
Bridge Road` candidate regression, preservation of source evidence, address
resolution statuses/confidence, asset classification, URL normalisation and
deduplication, semantic media separation, source-file provenance, `Link to
File`, named spreadsheet mapping, hyperlink output, validation issues, QA,
redirected HTML/downloadable-PDF brochure handling, brochure text provenance,
embedded/external brochure media classification, unknown-provider brochure
enrichment, actual-content resource typing, direct images, redirect provenance,
unsupported URI schemes, HTTP/network failure isolation, identity rejection,
atomic merging, structured link statuses, and recoverable enrichment failures.

The real-example script pins deterministic provider behaviour and record counts,
address/postcode flags, asset separation, link matching, provider sanity
fallback, deadline/quota short-circuiting, and memory-sensitive PDF image
handling. Exact fixtures present in the repository include:

- `Fw_ MetSpace - Office Of The Week!.eml` — one row.
- `Fw_ Workplace Plus - Availability 14th July (1).eml` — four rows.

Tests verify their values, addresses/postcodes, brochure links, floorplan/photo
separation, column alignment, and QA output. Live LLM/search behaviour was not
verified by the credential-free local suite; API calls in unit tests are mocked
or avoided.

## 19. Known limitations and remaining risks

- Provider parsers still use dictionaries internally and do not all emit
  granular field-level confidence/provenance.
- There is one explicit final property-validation pass, not a separate initial
  and final property-validation pair.
- Unknown future layouts can reach the LLM fallback but cannot be guaranteed to
  extract perfectly.
- LLM and grounded-search behaviour depends on credentials, quota, model
  availability, and external service responses.
- Generic automatic structured ingestion of every landlord, agent, or property
  page is incomplete; dynamic or access-controlled pages may be unavailable.
- External image validation is bounded and cached, but remote servers can still
  change or expire after workbook generation.
- Scanned/image-only PDFs without extractable text are rejected; no general OCR
  stage is implemented.
- Asset classification is heuristic and ambiguous assets may need review.
- HTML pages rendered entirely by client-side JavaScript may expose little
  usable server-returned text/media; the brochure stage does not run a browser.
- Embedded PDF visual classification is conservative. Small or repeated genuine
  photographs may be withheld as decorative rather than risk exporting logos or
  boilerplate as property images.
- The public Listings schema expects scalar values; arbitrary list
  serialisation is not implemented. Application-created image galleries provide
  one URL for multi-photo sets.
- Local-only source processing preserves the filename internally but cannot
  populate a public `Link to File` target without fabricating one.
- Optional object storage is required for source/gallery links to survive
  ephemeral storage cleanup and redeployment.

## 20. Extension guide

- Add a provider parser when a recurring, identifiable layout has stable
  structure that deterministic code can parse more accurately than the generic
  fallback. Implement `detect`/`parse`, register it in `RULES`, and add exact
  fixture expectations.
- Improve shared logic when the bug class applies across providers—for example,
  address identity, URL normalisation, asset classification, or spreadsheet
  mapping. Do not add a provider-specific patch for a generic failure mode.
- Add validators in `extraction.validation`; emit `ValidationIssue` with field,
  severity, source value, action, and stage. Never silently delete the evidence.
- Add an asset rule in `extraction.assets.classify_candidate`, using source
  context and a confidence appropriate to the signal. Preserve semantic
  separation and add deduplication/conflict tests.
- Preserve provenance by updating the canonical `Property.provenance` entry when
  a value is introduced or deliberately replaced. Retain source document,
  method, confidence, and original value.
- Add small legal regression fixtures to the repository, test both the parser
  and full `process_files` path, verify named columns and QA, then run pytest and
  `tests/test_examples.py`.
- Ensure every new provider still reaches normalisation, typed `Property`
  conversion, shared address enrichment, validation, semantic export, QA, and
  processing reports. A provider must not bypass downstream safeguards.

## 21. Architecture invariants

1. A linked resource must not enrich a property until property identity is validated.
2. Content belonging to another property must never be merged.
3. Logos, branding, tracking graphics, document previews, and decorative assets must not become property photographs.
4. Multiple distinct valid property photographs must not be collapsed into one.
5. `Brochure PDF`, `Floor Plan`, and `High Res Images` are separate semantic outputs.
6. A failed or inaccessible optional linked source must not damage the primary extraction.
7. A blank media cell must be explainable by diagnostics.
8. Provider-specific and generic extraction results use the same downstream identity, media, and validation pipeline.
9. No successful gallery link may point to an empty or known-broken gallery.
10. New providers inherit these protections through the canonical `Property` pipeline.
11. Every exported property retains its original source provenance.
12. Strong source data must not be silently replaced by weaker enrichment.
13. A conflicting building number must never provide a postcode for another property.
14. Spreadsheet output is mapped semantically and preserves the expected `Listings` schema.
15. Reproduced bug classes are preserved with regression tests.
16. Documentation must not claim features or verification unsupported by code and the latest test run.

## 22. Large spreadsheet reliability

Large workbooks are never flattened into one unbounded AI extraction request. The rule registry first tries deterministic parsers, including the generic repeated-block parser in `extraction.rules.spreadsheet_blocks`. It recognizes postcode/address rows followed by semantic headers (`Unit/Floor`, `Sq Ft`, `Desks`, `Term`, `Per Month`), permits a variable number of availability rows, and retains source sheet/row context.

Hyperlinks are associated inside the detected property boundary using their source sheet and row. They are not globally collected or assigned by document order. A block-level brochure may therefore serve sibling floor rows from the same property, but cannot leak into the next property block.

If an unknown large spreadsheet has no usable deterministic structure, `extraction.spreadsheet_chunks` divides it into bounded row groups with a small overlap. Limits apply independently to rows, input characters, expected records, and output tokens. Each chunk uses structured JSON output, receives at most one malformed-output retry, and is validated before aggregation. Duplicate overlap records are removed by stable property/floor/size/price identity. A failed chunk is recorded while successful chunks are preserved and the file is clearly marked as a partial extraction; all chunks failing remains a file-level error.

Processing reports expose `SPREADSHEET_STRUCTURE_DETECTED`, `PROPERTY_BLOCKS_FOUND`, `DETERMINISTIC_BLOCKS_PARSED`, `LLM_BLOCKS_REQUIRED`, `LARGE_FILE_CHUNKED`, `LLM_CHUNK_SUCCESS`, `LLM_CHUNK_FAILED`, and `PARTIAL_EXTRACTION` as applicable. Diagnostics also record chunk counts, failures, and largest prompt/response sizes.

**Invariant:** No large file should depend on a single unbounded LLM response for all extracted records.
