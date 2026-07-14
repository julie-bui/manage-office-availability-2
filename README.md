# Office Availability Consolidator

A local tool for extracting office-space listings from broker emails,
PDFs, spreadsheets, and Word docs — one clean, formatted `.xlsx` per
source file.

Drag files onto the page, click **Process Files**, and download each
generated spreadsheet, named after the source it came from (e.g.
`MetSpace.xlsx`, `Knotel.xlsx`).

## Setup

```
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env         # then edit .env and add your GEMINI_API_KEY
```

The API key is only used as a fallback, when a file's layout doesn't match
one of the known rule-based parsers. Get a key from
[Google AI Studio](https://aistudio.google.com/apikey).

## Run

```
python app.py
```

Then open **http://127.0.0.1:5000** in your browser — local dev runs
"open" (no access token needed) unless you've set `ACCESS_TOKEN` in your
`.env`, in which case the app lives at `http://127.0.0.1:5000/<token>`
instead (see [Deployment](#deployment-render) for why).

## How it works

1. **Read** — each uploaded file's raw text/tables are extracted based on
   its type (PDF, DOCX, XLSX/CSV, EML, HTML).
2. **Rule-based extraction first** — a handful of parsers recognize known
   layouts (currently: Knotel, MetSpace, and GPE broker emails; BC's own
   "Current Availability" summary table; Breezblok's single-listing
   brochure PDF; plus a generic "grid" parser for any spreadsheet/PDF whose
   columns already resemble the target schema). This is fast and free.
3. **LLM fallback** — if nothing recognizes the file, its raw text is sent
   to Gemini, which is asked to return structured JSON matching the target
   schema, plus its best guess at the sender/broker name. The response is
   validated before anything is written to a spreadsheet; a malformed or
   empty response is reported as a per-file error, not a crash.

   If Gemini's free-tier daily quota is exhausted (429 RESOURCE_EXHAUSTED),
   this is caught distinctly from every other failure and reported as a
   clear, scoped message in that file's own Notes/error cell — e.g.
   *"Gemini's daily AI-extraction limit has been reached for today. This
   resets at midnight Pacific Time — 08:00 UK time, in about 21 hours.
   This file needs the AI fallback because no known-provider parser
   (Knotel, MetSpace, GPE, Kitts, BC, Breezblok) recognized its layout —
   files that DO match one of those aren't affected and will still process
   normally."*
   — never a raw exception dump, and never implying the whole app/batch
   is down (`extraction/quota.py`, shared with the address-search quota
   note below).

   The daily reset time is computed from the real current time (Pacific
   and UK clocks each observe their own DST on different dates, so this
   is never hardcoded as "8 hours"), converted to UK time since this
   app's own audience is UK-based.

   Note: this is a genuinely *separate* Gemini quota from the address-search
   one below (different model, different daily limit) — a rule-based file
   (Knotel/MetSpace/GPE/Kitts/BC/Breezblok) never reaches this specific
   quota at all, but can still carry a warning of its own if the
   address-search quota (below) is what's exhausted, since that's a
   per-*building* concern, not tied to which parser matched the file.
4. **One spreadsheet per file** — each source file gets its own `.xlsx`,
   named after the identified provider:
   - Knotel/MetSpace/GPE/BC/Breezblok → `Knotel.xlsx`, `MetSpace.xlsx`,
     `GPE.xlsx`, `BC.xlsx`, `Breezblok.xlsx`
   - Anything else → the LLM's identified sender name if it's confident,
     otherwise a cleaned-up version of the original filename (e.g.
     `Kitt's Availability (External)....pdf` → `Kitts.xlsx`)
   - If two files in the same batch resolve to the same name (e.g. two
     MetSpace emails from different weeks), the second one gets a
     distinguishing suffix — the email's date if available (`MetSpace
     (2026-06-30).xlsx`), otherwise a counter (`MetSpace (2).xlsx`).

   Each processing run's outputs are independent — there's no persistent
   master file merging data across runs. Re-processing a batch overwrites
   the previous batch's generated spreadsheets.

The results summary after processing shows, per file: how many records were
extracted, which method was used (rule-based vs LLM), a download link for
its generated spreadsheet, and a clear error message for anything that
failed — so you can tell at a glance when a new source needs a proper
parser added to `extraction/rules/`.

5. **Link to File** — every extracted row's `Link to File` column
   is set to an absolute URL (`app.py`, `_download_url`) back to a
   source artifact saved alongside the generated spreadsheet. All rows
   from the same source file share the exact same URL, since it
   identifies the source document, not a listing. The link carries the
   access token as a query param (`?token=...`) rather than relying on
   the page's own JS header, since clicking a hyperlink in Excel opens a
   plain browser navigation with no custom headers.

   Opens directly in-browser rather than downloading, for PDF and
   `.eml` sources:
   - A PDF source is used as-is, served with `Content-Disposition: inline`
     — the browser's own PDF viewer renders it.
   - An `.eml` with an HTML body links to that HTML directly (the email's
     own HTML MIME part, already parsed by `file_readers.py`, unmodified
     — not a re-rendered conversion), served as `text/html` + `inline`.
     Opens like the original email, images included, since the markup
     already points at the sender's hosted image URLs. A plain-text-only
     `.eml` (no HTML part) falls back to linking the raw `.eml` as a
     normal attachment download.
   - `.docx`/`.xlsx`/`.xls`/`.csv` have no reliable native in-browser
     renderer, so they're left as normal attachment downloads of the
     original file.

   **Floor Plan / High Res Images** — Kitt's-style sources get these from
   their own table columns (`extraction/rules/grid.py`) and Knotel gets
   Floor Plan from its email's own "Download Floorplan" link
   (`extraction/rules/knotel.py`). For any other PDF source — BC's brochure
   format, Breezblok, Crown Estate, or anything still on the LLM fallback —
   `extraction/pdf_images.py` extracts the source PDF's own embedded images
   (excluding logos/banners repeated across many pages) regardless of
   whether a rule or the LLM fallback extracted that PDF's listing data
   (`app.py`'s `PDF_IMAGE_ENRICHED_METHODS`). BC's own "Current
   Availability" table (a different PDF format from its brochures) has no
   embedded images at all, so this step is a harmless no-op for it.

   Floor Plan can come from two different signals, checked in this order:
   1. A link annotation the source itself placed directly on top of a
      listing's own image, pointing to an external resource
      (`_link_uri_for_rect`, IoU-matched against the image's own
      placement rect — deliberately intersection-over-union, not "does
      the link's rect sit inside the image's", since a small unrelated
      icon fully contained in a much larger photo would otherwise score
      as a full match; confirmed empirically on BC's own brochure, where
      a "watch our video" YouTube icon sits inside a much bigger unrelated
      photo). Confirmed on Crown Estate (2026-07) — by directly clicking
      images in the real PDF — that each listing's own photo is a
      clickable link to an external 3D-tour/floor-plan viewer
      (my.matterport.com, a white-label viewer domain hosting the same
      kind of tour, or in one case the broker's own property page/
      homepage) — a real, source-labeled signal sitting on top of the
      image, not visible in the image's own pixel content or embedded
      bytes at all, so no pixel-based classifier could ever find it.
   2. Failing that, `is_floorplan_page`'s source-labeled text check (e.g.
      BC's own "Example Floorplan" heading) plus `is_floorplan_image`'s
      pixel-content fallback — a floor plan's mostly-white background
      versus a real photo's, confirmed empirically across several
      sources. On Crown Estate specifically, every one of its 83 embedded
      images turned out to be a real interior/exterior photograph when
      actually viewed — this tier alone would have left Floor Plan blank
      for that source entirely; tier 1 above is what actually populates
      it there.

   A listing can have both a real photo (High Res Images) and a separate
   floor-plan/tour link (Floor Plan) from the same source image at once —
   the image itself is still saved/classified normally regardless of
   whichever link, if any, was found for it.

   A page with more than one listing (routine on Crown Estate — a 2- or
   3-column grid, several listings sharing one page) matches each real
   image to the SPECIFIC listing it's positioned next to
   (`match_listings_to_images`: each listing's own heading text block on
   the page, then the nearest image above it in the same horizontal
   column), not to every listing whose name happens to appear anywhere on
   that page — confirmed this matters empirically: the earlier whole-page
   attribution silently merged unrelated buildings' photos into one shared
   gallery whenever a page held more than one listing.

   Several records can also share byte-identical Building text when the
   same building spans multiple floors, each its own page or page-pair
   (e.g. Crown Estate's "Princes House, 38 Jermyn Street" across 4 pages,
   2 floors per page) — `find_all_matching_pages` (the union across every
   name-matching tier, not just the first one that matches anything) plus
   `count_heading_occurrences` distribute those records across their real
   distinct page occurrences in order (2 records to a page with 2
   headings, 1 to a page with only 1, etc.), confirmed necessary
   empirically: naively registering every same-name record on every page
   its name appears on let several pages' images all pile onto whichever
   records happened to come first, while later floors got no images at
   all — and a narrower single-tier page lookup (fine for one record on
   its own) can miss a floor's own page entirely when that floor's
   occurrence only matches a broader tier (e.g. one page's raw text
   includes a postal area code a sibling floor's page doesn't repeat).

   A single-record document (e.g. BC's own single-listing brochure) skips
   all of the above — every real image in it belongs to that one record
   regardless of position, no attribution risk with nothing else to
   confuse it with.

   Either way, Floor Plan/High Res Images are left blank whenever a source
   has no embedded images/links at all, or a listing's page/position can't
   be matched — never fabricated.

   `/api/download` always tries local disk first (fast path — the batch
   that just ran). If the local copy is gone — Render's free-tier disk is
   wiped on every redeploy/restart, and our own hourly cleanup deletes
   local batch folders regardless (see [Multi-user
   behavior](#multi-user-behavior)) — it falls back to object storage
   (`storage.py`) if configured (see [Persistent storage
   (optional)](#persistent-storage-optional)), which isn't tied to the
   instance's disk at all. Without object storage configured, the link
   really does only last for that hour/until the next redeploy —
   "where did this come from" during/soon after a processing run, not
   long-term archival. `Floor Plan`/`High Res Images` are left as plain,
   non-clickable text for now (whatever URL, if any, a source document
   itself provided) — not reliable enough yet to treat as real links.

## Target schema

Each spreadsheet's columns were derived from the example output (`Kitt's
Availability... .pdf`) — see `extraction/schema.py`. The "Contacts"
column holds every identified contact for that listing/source,
comma-separated, however many there are (not capped at a fixed number) —
the source PDF had a single merged header, "Please reach out to the team
assigned to this space", over what happened to be two name columns with
no sub-header text of their own, but other sources (e.g. MetSpace) list
three.

Every spreadsheet also carries eight extra columns for Kato bulk-upload
compatibility (matching the "Loader" sheet of
`kato-disposals-loader-example-LATEST VERSION (3).xlsx`): `External Ref`,
`Assigned Agents`, `Property Address 1`, `Property Postcode`, `Lat`,
`Lng`, `For Sale`, `To Let`. These are derived, not extracted — see
`normalize_record` in `extraction/schema.py` and `process_files` in
`extraction/pipeline.py`:
- `External Ref` is `<ProviderName>_<YYYY-MM-DD>` — the same value for
  every row in a spreadsheet, since it identifies the source batch, not
  an individual listing. The date prefers the source document's own date
  (an email's `Date` header, then PDF/DOCX metadata) over processing
  time, falling back to the processing date only when neither is
  available (`extraction/naming.resolve_source_date`).
- `Assigned Agents` mirrors `Contacts`.
- `Property Address 1` mirrors `Building`, and `Property Postcode` is
  parsed out of it with a UK postcode regex (`extraction/address.py`) —
  left blank rather than guessed when no postcode is confidently present
  (true for all current sources, whose `Building` text is usually just a
  building/street name with no postcode).
- `For Sale`/`To Let` are hardcoded `"No"`/`"Yes"` — every current source
  is lettings, not sales.
- `Lat`/`Lng` come from geocoding `Property Address 1` (+ postcode, +
  ", London, UK" if not already implied) via OpenStreetMap Nominatim
  (`extraction/geocode.py`), called once per unique address per
  `process_files` batch. Results are cached on disk in
  `.geocode_cache.json` (gitignored) so the same building is never
  re-geocoded across runs, and requests are throttled to Nominatim's
  1/second usage policy with a descriptive `User-Agent`.

  `Property Postcode`/`Lat`/`Lng` are required fields, so a failed direct
  lookup doesn't just give up — `extraction/pipeline._geocode_records`
  works through several fallback tiers, in order, each logged distinctly
  (`[geocode] ...`) so which tier produced a result is always traceable:
  1. A spelled-out leading building number converted to digits (e.g.
     "Thirty One Alfred Place" → "31 Alfred Place") — counts as a
     confident match (a full street address, just spelled out in words),
     not the bare-name case below.

     An LLM-extracted `Building` combining a marketing name with its real
     street address (e.g. "Princes House, 38 Jermyn Street", sometimes
     with a trailing postal district too — "Princes House, 38 Jermyn
     Street, SW1Y") isn't a bare name (it has a house number), but
     confirmed empirically (Crown Estate, 2026-07) that Nominatim returns
     NO MATCH AT ALL for the combined name+address string — even though
     the address portion alone resolves correctly every time. When the
     record's own text as given fails,
     `extraction/pipeline._address_retry_candidates` retries, in order: a
     bare trailing postal district (e.g. the "W1" in "5 Swallow Place,
     W1") stripped entirely — never a query built from the code alone,
     confirmed to resolve to a generic district-wide centroid shared by
     every other building that also fell back to it; then the address
     portion of a "Name, Address[, District]" string, with and without
     its own trailing district code. The address is still fully present
     in the source text either way, so a match found this way isn't
     flagged as derived/"Not in source text" — just a query that needed
     reshaping to be understood.

     Multiple occurrences of the exact same building within one file
     (e.g. several floors, each its own row) are also consolidated: if
     one occurrence's own text happens to include a qualifier another
     lacks (confirmed on Crown Estate: the same "1 Vine Street" listed
     with ", W1" on one page and without it on another, since the source
     itself doesn't repeat the qualifier in every section), every
     occurrence is geocoded using whichever text is richest — a bare
     version alone was confirmed to resolve confidently to a
     coincidentally-real but wrong address (Walthamstow, ~12km from the
     real Mayfair one) with no second Nominatim candidate for the
     ambiguity check below to catch.

     Finally, `extraction/geocode.py`'s own `_fetch` requests a few
     candidate matches (not just the top one) and checks how far apart
     they are: a common street name with no distinguishing context can
     return a *confident-looking* top result that's actually many km away
     in an unrelated borough (confirmed: "25 Bury Street" → Edmonton, N9,
     ~15km from the real St James's one) — Nominatim itself doesn't flag
     this. Candidates spanning more than a few km are treated as
     unresolved rather than silently trusted (falls through the same as
     "no match" — to a further tier, or to `"Needs manual lookup"` if none
     is left) — verified this doesn't risk flagging a genuinely good match
     just for returning more than one plausible *nearby* result: every
     known-good address already in this app's own sources clusters within
     a few hundred metres across its own top candidates.
  2. For a genuinely bare building name (no house number/street at all,
     spelled out or otherwise) — an actual web search for the building's
     real address, Gemini with Google Search grounding
     (`extraction/address_lookup.py`), with the source/provider name
     (e.g. "GPE", "MetSpace") included as disambiguating context (e.g.
     "Elsley GPE Fully Managed" instead of just "Elsley"). Not another
     Nominatim query — Nominatim can only match an address it's given,
     and this is tried *before* a bare Nominatim search specifically
     because a bare name alone isn't trustworthy (see below).
  3. Only if that finds nothing: Nominatim on just the bare building name
     + "London, UK", as a last resort — same risk as tier 2's justification.

  When tier 2 fails specifically because Gemini's daily grounding quota is
  exhausted (429 RESOURCE_EXHAUSTED), the file this building belongs to
  still processes normally (status stays "ok", every record still
  extracted) — it just carries a per-file warning noting that some rows
  fell back further than usual, with the same reset-time note as the
  LLM-extraction quota above. Deliberately a *warning* on an otherwise-ok
  file, not an error — the records are real; only some addresses are
  less confidently placed than they'd otherwise be. Note there's no
  running "X of N Gemini calls used today" counter anywhere in this app —
  confirmed (2026-07) while adding this: it was never built, despite the
  "20/day" figure appearing in several comments as *context* for why
  quota exhaustion happens easily, not as anything actually tracked.

  Tier 2 itself retries immediately (up to `MAX_EMPTY_METADATA_RETRIES`,
  currently 3, real API calls within the same lookup) when Gemini's
  grounding call comes back with a confident, correct-looking address but
  completely empty `grounding_chunks` metadata — confirmed via real
  Render logs (2026-07) that the search itself was consistently correct
  (City Tower → 40 Basinghall Street, Elsley → 20/30 Great Titchfield
  Street W1W 8BF, Kent House → 17 Market Place W1W 8AJ) but the metadata
  needed to pass the `MIN_INDEPENDENT_SOURCES` check was sometimes just
  missing from that specific call, not evidence the answer was wrong.
  Before this, one flaky call would fall all the way through to tier 3's
  own coincidental-match risk and silently re-poison the result, even
  though the correct answer was one retry away. Only falls through to
  `address_lookup.py`'s own cross-run "flaky" miss-counter (see
  `MAX_EMPTY_METADATA_MISSES`) if every retry within this call is also
  empty.

  Tier 3's own result is never trusted from cache (`extraction/geocode.py`'s
  `geocode(..., confident=False)`) — confirmed (2026-07) that testing GPE
  while Gemini's grounding quota was exhausted made tier 2 fail every
  time, so every lookup fell through to tier 3, and its cached (also
  wrong) answer then got trusted permanently — silently re-poisoning the
  cache the exact same way once already fixed, just via a different cache
  file than the one that was cleared. A `confident=False` call's cache
  entry is marked `low_confidence` and is never treated as authoritative
  on a future lookup — the fetch is always redone — the same "final vs
  retriable" distinction `address_lookup.py`'s own cache already made for
  tier 2, extended to tier 3. Tiers 1/2's own results are unaffected and
  still cached/trusted normally forever.

  A bare building name (no house number/street) is inherently a weaker
  signal than a full address — not guaranteed unique, and confirmed
  empirically *twice*: BC's "Porters Place" alone (no city qualifier) once
  matched a street in Barbados, and GPE's "Elsley" alone matched a
  building in Battersea (SW11 5LL) when the real GPE-managed "Elsley" is
  in Fitzrovia (W1W 8BF) — adding "GPE Fully Managed" as search context
  fixed it. So a value from tier 2 or 3 gets `" (Not in source text)"`
  appended to `Lat`/`Lng` (and `Property Postcode`, if that was also
  backfilled from the same lookup) — visible in the spreadsheet itself,
  not just the console log. The label describes *why* it's flagged (the
  source document never stated this address at all, so it had to be
  derived some other way), not a claim that the value is wrong — most
  derived values are correct, they just couldn't be read directly from
  the source. If every tier still finds nothing, the cell shows literal
  text `"Needs manual lookup"` instead of being left blank. None of this
  ever blocks the rest of the batch.

  Note: as of testing this (2026-07), Google Search grounding returned
  429 RESOURCE_EXHAUSTED on this project's free tier for
  `gemini-3.1-flash-lite` and `gemini-2.0-flash` — only `gemini-2.5-flash`
  had free grounding quota available, so `address_lookup.py` uses that
  model specifically (the rest of this app's Gemini calls, which don't
  use grounding, use `gemini-3.1-flash-lite` and are unaffected). Worth
  re-checking if this starts failing later — Google's model/quota lineup
  for grounding shifts over time.

### Clearing a stale/wrong cached lookup

A fix to geocoding or address-lookup logic can be completely correct and
still keep producing the old wrong output, because the previous (wrong)
answer is already cached — locally, in the S3/B2 mirror, and in the
memory of whichever worker process is currently running. `GET`/`POST
/api/cache/invalidate` (token-protected, same as every other `/api/`
route) fixes all three at once, since it runs inside the live app itself:

```
curl "https://<your-app>/api/cache/invalidate?building=elsley&token=<ACCESS_TOKEN>"
```

- `building` (required): a case-insensitive substring matched against
  both caches' keys — `extraction.geocode`'s `"<address>, london, uk"`
  and `extraction.address_lookup`'s `"<building>|<provider>"`. Removes
  every match from the in-memory cache, local disk, and the S3 mirror.
- `dry_run=1`: preview which keys would be removed without changing
  anything — worth a quick check first that a substring isn't broader
  than intended (e.g. `building=house` would match every building whose
  name contains "house").

Response reports exactly which keys were matched in each cache. Because
this mutates the already-loaded cache inside the running process, it
also fixes the "worker already has the stale answer in memory" problem
that a redeploy alone doesn't (a redeploy just pulls the same stale S3
copy back down) — no restart needed. That relies on this app running as
a single gunicorn worker (see `Procfile`/`render.yaml`); with multiple
workers, one call here would only clear whichever worker happened to
handle that request.

## Adding a new source

If a new sender's emails consistently fail to a rule and always land on
the LLM fallback, it's worth adding a dedicated parser: drop a new module
in `extraction/rules/` following the pattern in `knotel.py`/`metspace.py`/
`gpe.py` (a `detect(content)` and `parse(content)` function), then register
it in `extraction/rules/__init__.py`. Doing so also means its output
spreadsheet gets a proper provider-name (e.g. `Foo.xlsx`) instead of
falling back to a filename guess.

### Checklist: 5 failure patterns to check for every new/modified parser

A 2026-07 audit of Knotel's own real bugs found the same 5 failure
patterns recurring across MetSpace, GPE, Kitt's, BC, and Breezblok too
(see each rule's own docstrings/comments for the specific real examples
found and fixed). Check every one of these — against the ACTUAL source
text and ACTUAL spreadsheet output for a few real rows, not just by
reading the code — whenever a parser is added or changed:

1. **Silently discarding real address text.** Does the rule read a
   building/listing name but drop the very next line/field where the
   real street address actually lives, falling back to unreliable
   bare-name geocoding when a real address was available all along?
   (The original Knotel bug — its own address line was right there,
   discarded, for every single listing.)
2. **Missing company-level contacts.** Does the source have a company/
   team-level contact block (name, email, phone) that isn't being
   extracted into `Contacts`? A rule can capture the name and still
   silently drop a real phone/email sitting right next to it (found in
   MetSpace, GPE, and Breezblok — not just "blank", a subtler partial
   miss). `Contacts` should hold the fuller info; `extraction.schema.
   names_only` derives the name-only `Assigned Agents` from it
   automatically — don't hand-roll a second, separate name-only
   extraction in the rule itself.
3. **Silently overwritten/discarded link fields.** Is a real per-listing
   link (brochure, floor plan, photos) correctly extracted by the rule
   but then clobbered before reaching the spreadsheet? `app.py`
   unconditionally overwrites `Link to File` for every record with the
   persisted source-file link, regardless of what any rule sets there —
   a rule's own distinct per-listing link needs its own column (see
   Knotel's `Brochure PDF`), never `Link to File`.
4. **Case-sensitive or reversed text/link matching.** Does the rule's
   link-matching assume consistent capitalization (a real source can use
   inconsistent casing for the same button/label on different listings)
   or a consistent href/visible-text order (a real source can have them
   swapped for one specific listing)? Compare case-insensitively; don't
   assume href and visible text are never swapped.
5. **Record-boundary detection relying on incomplete signals.** Does
   "has a new listing/building started" rely on an assumption that can
   fail for a differently-formatted record within the same source (e.g.
   gating on a full postcode being present, when a real building's
   address sometimes only has a partial one)? Prefer a structural
   invariant the source's own layout guarantees over a heuristic that
   happens to hold for most rows.

Also worth checking for a table-based rule specifically: does
`_find_table`-style detection return only the FIRST table whose header
matches, when a long table can be split across multiple page-tables
(pdfplumber does this for a table spanning a page break, each split
repeating the same header row)? `extraction.rules.grid` silently dropped
38 of 57 real Kitt's listings this way before being fixed to collect
every matching table, not just the first.

## Tests

```
python tests\test_examples.py
```

Runs every example source file (broker emails and PDFs, one per known rule
— Knotel, MetSpace, GPE, Kitts/Grid, BC, Breezblok) through the rule-based
parsers, checks record counts, and pins known-correct field values/images
for the sources that have had real extraction bugs before — a quick
regression check that nothing has silently broken.

## Deployment (Render)

The app can also run as a small hosted service for a handful of known
people, instead of only on localhost. It's set up for
[Render](https://render.com)'s free tier — a good fit here because it
deploys straight from a GitHub repo with no Docker/config authoring
required, environment variables are first-class (no secrets in the repo),
and it doesn't restrict outbound network calls (some free hosts do, which
would silently break the Gemini API fallback).

### Deploy steps

1. Push this repo to GitHub if it isn't already.
2. Create a free account at [render.com](https://render.com) (GitHub
   login is easiest).
3. **New → Web Service**, connect your GitHub account, and select this
   repo. Render will detect `render.yaml` automatically; if it doesn't,
   set these manually:
   - **Environment**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT`
   - **Plan**: Free
4. Under **Environment**, add two environment variables:
   - `GEMINI_API_KEY` — your key from
     [Google AI Studio](https://aistudio.google.com/apikey)
   - `ACCESS_TOKEN` — a long random string (see below); generate one with:
     ```
     python -c "import secrets; print(secrets.token_urlsafe(16))"
     ```
5. Click **Create Web Service**. The first build takes a few minutes;
   Render gives you a URL like `https://office-availability-consolidator.onrender.com`.

### The access token

`ACCESS_TOKEN` gates the app behind an unguessable URL: the real page
lives at `https://<your-app>.onrender.com/<ACCESS_TOKEN>`, not at the
root. The root path and any wrong/missing token both return a plain 404,
so there's nothing to distinguish "wrong guess" from "page doesn't
exist." The API endpoints (`/api/process`, `/api/download/...`) require
the same token too (sent automatically by the page's own JS), so knowing
those paths without the token doesn't help.

**What this protects against:** casual discovery — search engine
crawling, opportunistic URL scanning, someone finding the Render app
name and guessing at paths.

**What this does NOT protect against:** anyone who already has the full
URL can use the app fully, indistinguishable from an authorized user —
there's no per-person login, so you can't tell people apart or revoke
just one person's access without rotating the token for everyone. The
token also isn't encrypted-at-rest anywhere special — it's a shared
secret, similar in spirit to an unlisted Google Doc link. If it leaks
(e.g. forwarded somewhere public), rotate it: change `ACCESS_TOKEN` in
Render's dashboard and share the new URL — no code change or redeploy
needed for the value itself, though Render does restart the service to
pick up the new env var.

If you outgrow this later, the natural upgrade is Flask-Login or
HTTP Basic Auth with per-person credentials, or Render's own paid
"Preview/Access Control" features.

### Multi-user behavior

Each **Process Files** click gets its own isolated batch folder
(`output/<random-batch-id>/`) and its own download links — two people
using the app at the same time (or one person running two batches back
to back) never overwrite each other's generated spreadsheets. Batch
folders older than an hour are cleaned up automatically on the next
request; Render's disk is also ephemeral and resets on every deploy/restart
regardless, so nothing here is meant to persist long-term.

### Free tier limits & costs

- **Spins down after 15 minutes of inactivity.** The next request after
  that wakes it back up, which takes roughly 30–60 seconds (the request
  will just hang/load slowly during that time — no action needed, it
  resolves itself). Fine for occasional use by a small group; annoying if
  someone's waiting on a fast response for a demo.
- **750 free instance-hours/month** across all your free services —
  effectively unlimited for a low-traffic internal tool used by a few
  people, since a spun-down service doesn't consume hours.
- **No persistent disk on the free tier**, and the disk resets on every
  deploy/restart even on paid tiers — this is why `Link to File`
  needs [object storage](#persistent-storage-optional) to keep working
  past that; see there if you want those links to actually last.
- **Outbound bandwidth and build minutes** are generously capped on the
  free tier; a handful of users uploading office-listing documents won't
  come close.
- **When to upgrade**: if the 30–60s wake-up delay becomes annoying (a
  paid instance stays running), or if you need more than 750 hours/month
  across free services (unlikely for this use case). Render's cheapest
  paid tier is a few dollars/month.
- **512MB RAM.** Confirmed (2026-07) that processing a large, image-heavy
  PDF (Crown Estate, 4.3MB/20 pages) got the worker SIGKILLed for
  exceeding this. `extraction/pdf_images.py`'s embedded-image extraction
  now decodes at most one page's images at a time (`scan_pages` +
  `load_page_images`, also explicitly shrinking PyMuPDF's own
  process-global object store after each call — not something Python's
  own garbage collector touches) instead of holding every real image in
  the whole document in memory at once, and `extraction/file_readers.py`
  rejects a PDF over `MAX_PDF_PAGES` (300) up front with a clear per-file
  error instead of attempting it. The Procfile/`render.yaml` startCommand
  also adds `--max-requests 15 --max-requests-jitter 5`: this app runs a
  single long-lived gunicorn worker, and native/heavy dependencies
  (PyMuPDF, Pillow, `google-genai` — confirmed importing the latter alone
  costs ~53MB, permanently, the first time either Gemini call site is
  used in a worker's lifetime) don't reliably return freed memory to the
  OS, so RSS can ratchet upward across a session's worth of requests even
  when each individual file is well-behaved on its own — periodically
  recycling the worker resets that accumulation.

  Also confirmed (2026-07) that at least one "Perhaps out of memory?"
  SIGKILL actually caught a worker stuck inside `storage.upload()` —
  mirroring the geocode/address-lookup caches to B2/S3 used to happen
  *synchronously* inside `extraction.pipeline.process_files`, so a slow
  or hanging B2 call could itself run long enough to look identical to an
  OOM kill (gunicorn/Render report a generic SIGKILL message regardless
  of the real cause). Moved to the same background-thread pattern
  `app.py` already used for every other `storage.upload` call (see
  `app.py`'s `_flush_caches`) so it can never block the HTTP response or
  contribute to a worker timeout.

  **None of the above turned out to be the actual root cause of Crown
  Estate's own repeated SIGKILL.** Every local memory measurement of that
  exact file — before and after each fix above — stayed around ~200MB,
  nowhere near 512MB, and the crash kept recurring on Render regardless.
  Added `extraction/memlog.py` (real RSS via `psutil`, logged at each
  processing stage with an immediate `stdout` flush so a checkpoint isn't
  lost to buffering right before a kill) to get real numbers from the
  real server instead of continuing to guess from a local environment
  that clearly wasn't reproducing it. The actual Render crash log then
  showed a real Python traceback, not just the generic SIGKILL message:
  the worker was blocked inside `ssl.py`'s `recv()`, deep in `httpx`/
  `httpcore`, waiting on a network read **from Gemini's own API** — the
  `[memlog]` trail confirmed it, stalling forever right after "before LLM
  call" with no "after" ever logged. **`google-genai`'s HTTP client had no
  request timeout configured**, so a slow/hanging network path to
  Google's API blocked the call — and the whole worker — indefinitely.
  gunicorn's own `--timeout` tries to abort a stuck worker via a signal,
  but a worker blocked in a low-level, uninterruptible SSL socket read
  can't be stopped that way, so gunicorn eventually has to escalate to a
  raw SIGKILL, which is exactly what got misreported as "Perhaps out of
  memory?" every time.

  First attempt: an explicit `http_options=types.HttpOptions(timeout=...)`
  on both Gemini calls. Confirmed via a second real Render crash that this
  *alone* wasn't enough — gunicorn's own `[CRITICAL] WORKER TIMEOUT`
  (120s) fired anyway, meaning the call was still blocked well past the
  configured `http_options` timeout. Verified empirically (a 1ms
  `http_options.timeout` against the real API failed almost instantly, so
  the mechanism itself isn't broken) that httpx's `timeout` bounds the gap
  *between* chunks of data arriving, not the call's total duration — a
  large response (this app's extraction call asks for up to
  `MAX_OUTPUT_TOKENS`, 24000) can keep trickling data slowly enough that
  no single inter-chunk gap ever exceeds the configured timeout, while the
  *total* call still runs far longer than it.

  Real fix: `extraction/hard_timeout.py`'s `call_with_timeout` runs the
  Gemini call in a background thread and enforces a true, independent
  wall-clock deadline via `concurrent.futures.Future.result(timeout=...)`
  — 60s for `extraction/llm_fallback.py`'s single extraction call, 25s per
  attempt for `extraction/address_lookup.py`'s grounding call (smaller
  since that one can retry up to `MAX_EMPTY_METADATA_RETRIES` times per
  lookup) — both comfortably under gunicorn's 120s. This doesn't depend on
  the callee's own timeout semantics at all, so it can't be defeated the
  same way: verified with a mocked call that sleeps for 10 minutes and
  completely ignores `http_options` — the outer deadline still fires
  exactly on schedule. (There's no safe way to force-kill a thread blocked
  in a C-level socket read, so a timed-out call may keep running in the
  background afterward; `call_with_timeout` explicitly uses
  `executor.shutdown(wait=False)`, not a `with` block, so this function
  itself returns immediately either way rather than blocking on exactly
  the hang it exists to avoid.) A hang now fails fast with a normal,
  catchable exception — a clear per-file "timed out" message, never a
  crash — instead of taking the worker down with it.

  The memory work earlier in this list wasn't wasted — `pdf_images.py`'s
  page-by-page bound and the backgrounded cache flush are both real,
  legitimate fixes for real (if not, on their own, *this* crash's) risks —
  but the timeout was the actual answer for this specific,
  repeatedly-reported symptom.

### Persistent storage (optional)

Without this, `Link to File` (and the generated spreadsheet's own
download link) only work for about an hour, or until the next
redeploy/restart, whichever comes first — Render's disk (free tier or
paid) doesn't survive either. `storage.py` adds an optional mirror to any
S3-compatible object store, which isn't tied to the instance's disk at
all — `/api/download` falls back to it automatically whenever the local
copy is gone.

It's entirely inert until configured: no env vars set means every
`storage` call is a no-op, and the app behaves exactly as if this feature
didn't exist (local disk only, same lifetime as before).

This app's Render deployment currently uses **Backblaze B2** (private
bucket) — free tier covers 10GB storage with no egress fees, similar in
spirit to Cloudflare R2 below. Any S3-compatible provider works the same
way through the same five env vars; pick whichever's most convenient.

**Backblaze B2** (currently in use):
1. Create a private B2 bucket, then an "Application Key" scoped to it
   (read+write) in the B2 dashboard.
2. Set these environment variables (Render dashboard → Environment, or
   your local `.env`):
   ```
   S3_BUCKET=<your-bucket-name>
   S3_ENDPOINT_URL=https://s3.<region>.backblazeb2.com
   S3_ACCESS_KEY_ID=<the Application Key's keyID>
   S3_SECRET_ACCESS_KEY=<the Application Key's own secret>
   S3_REGION=<region, e.g. us-west-002>
   ```
   B2's S3-compatible API doesn't support recent botocore versions'
   default request-checksum headers — `storage.py`'s client `Config`
   already disables those (`request_checksum_calculation` /
   `response_checksum_validation` set to `"when_required"`), which is
   required for B2 specifically (AWS S3 and R2 don't need it, but it's
   harmless for them too).

**Cloudflare R2** (alternative — also free, no egress fees):
   ```
   S3_BUCKET=<your-bucket-name>
   S3_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
   S3_ACCESS_KEY_ID=<from an R2 API token scoped to that bucket>
   S3_SECRET_ACCESS_KEY=<from the same token>
   S3_REGION=auto
   ```

**AWS S3** (alternative — small per-GB cost) — same env vars, but omit
`S3_ENDPOINT_URL` (boto3 uses AWS's own regional endpoint) and set
`S3_REGION` to a real AWS region (e.g. `eu-west-2`).

Whichever you pick, nothing else in the app changes — same `Link to
Brochure` URLs, same access-token gating (the bucket itself stays
private; only this app's own credentials can read/write it).

Verified end-to-end against the real B2 bucket in production: uploaded a
file, restarted the service (wiping its local disk), and the exact same
download link still returned the original file afterward — byte-for-byte
identical, correct `Content-Type`/`Content-Disposition` — served from B2.

## Notes

- The staged extraction architecture, typed contracts, validation/reporting
  behavior, QA worksheet, and provider extension guide are documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).

- Outbound network calls: the Gemini API (only for files that need the
  LLM fallback), and OpenStreetMap Nominatim (for geocoding every
  extracted listing's address into `Lat`/`Lng` — see [Target
  schema](#target-schema)).
- Generated spreadsheets live in `output/`, which — like `.env` — is
  gitignored: it's local, working-copy data, not something to version.
- `.geocode_cache.json` (repo root, gitignored) persists geocoding
  results across runs/restarts on a local machine. On Render's free tier
  the disk is ephemeral (see [Deployment](#deployment-render)), so this
  cache resets on every deploy/restart there — it still avoids
  re-geocoding within a single running instance's lifetime.
