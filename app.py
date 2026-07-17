import gc
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

load_dotenv()

import storage
from extraction import address_lookup, geocode as geocode_module, html_images, memlog, pdf_images, xlsx_links
from extraction.naming import make_unique_names
from extraction.assets import (
    MIN_PROPERTY_IMAGE_HEIGHT,
    MIN_PROPERTY_IMAGE_WIDTH,
    evaluate_image_bytes,
    image_content_hash,
    is_blank_or_empty_image,
    merge_candidate_urls,
    normalize_url,
    validate_image_url,
)
from extraction.models import AssetType, LinkDiagnostic
from extraction.pipeline import BATCH_DEADLINE_SECONDS, process_files
from spreadsheet import write_xlsx

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".eml", ".html", ".htm"}
BATCH_MAX_AGE_SECONDS = 60 * 60  # clean up old batch output dirs after an hour

# process()'s `method` values whose own extraction (rule or LLM) supplies no
# image data at all, so a PDF source needs extraction.pdf_images' own
# position-based real-image enrichment for Floor Plan/High Res Images.
# Deliberately explicit (not "any rule other than grid/knotel") so adding a
# future rule-based PDF parser that DOES supply its own images from its own
# table/link structure doesn't silently get double-processed here.
PDF_IMAGE_ENRICHED_METHODS = {"llm", "llm:chunked", "rule:BC", "rule:Breezblok"}
# Target band for High Res Images when the source/brochures actually contain
# property photos. Cap galleries at MAX so one brochure dump cannot flood a
# listing; warn when a non-exempt file finishes below MIN or with zero images.
MIN_HIGH_RES_IMAGES = 5
MAX_HIGH_RES_IMAGES = 8
# URL/filename floor-plan tokens — cheap pre-filter before pixel validation
# (MetSpace / brochure CDN paths that literally say "floorplan").
_FLOORPLAN_URL_RE = re.compile(r"floor[\s_-]*plan|floorplan|\blayout\b", re.I)
# Sources confirmed to ship availability with literally no property photos
# (tabular PDF only). Blank High Res is expected, not a coverage failure.
IMAGE_EXEMPT_METHODS = {"rule:BC"}

# Explicit Content-Type per extension for /api/download, rather than
# relying on send_file's default (Python's mimetypes module, which is
# backed by the OS's own registry/mime.types and is NOT consistent across
# platforms — e.g. .eml resolves to message/rfc822 via the Windows registry
# on a dev machine, but a bare Linux container like Render's often has no
# entry for it at all and falls back to application/octet-stream). A
# browser treating a download as unrecognized/unconfirmed rather than a
# normal, openable file is exactly the kind of symptom that mismatch causes.
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".eml": "message/rfc822",
    ".html": "text/html",
    ".htm": "text/html",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
# Extensions a browser can render natively — served as `inline` so
# clicking a downloaded source artifact opens it directly in-browser instead of
# downloading. PDFs use the browser's built-in PDF viewer. .html/.htm
# covers the HTML brochure saved for .eml sources below (the email's own
# HTML body, not a raw .eml) — it opens like the original email,
# including images, since that markup already points at the sender's
# hosted image URLs. Images (Floor Plan/High Res Images, extracted from a
# source PDF by extraction.pdf_images) should open directly too, same as
# a PDF, rather than force a download. DOCX/XLSX/CSV have no reliable
# native in-browser renderer, so they're deliberately left out — normal
# downloads for those.
INLINE_EXTENSIONS = {".pdf", ".html", ".htm", ".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Set in the hosting platform's environment variables (never committed). If
# unset, the app runs "open" with no path/token gating — fine for local dev,
# but you MUST set this before deploying anywhere reachable by other people.
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB per request


def _token_ok(supplied):
    if not ACCESS_TOKEN:
        return True
    return bool(supplied) and secrets.compare_digest(str(supplied), ACCESS_TOKEN)


@app.before_request
def _guard():
    # The landing page lives at /<token>, not /, so root and any wrong
    # guess 404 identically — nothing here confirms whether a token is
    # "close" to correct. Static assets (JS/CSS, no user data) stay open,
    # since the page can't even load them before it has the token otherwise.
    if request.path.startswith("/static/"):
        return
    if request.path.startswith("/api/"):
        supplied = request.headers.get("X-Access-Token") or request.args.get("token")
        if not _token_ok(supplied):
            abort(404)
        return
    if request.path == "/":
        abort(404)
    # else: the /<token> route itself checks the token and 404s there


@app.route("/<token>")
def index(token):
    if not _token_ok(token):
        abort(404)
    return render_template("index.html", access_token=ACCESS_TOKEN)


@app.route("/api/version")
def version():
    """So "is the fix actually deployed" can be answered directly instead
    of inferred from push timing — Render sets RENDER_GIT_COMMIT
    automatically on deployed services; falls back to asking git directly
    for local dev, where that env var isn't set."""
    commit = os.environ.get("RENDER_GIT_COMMIT")
    if not commit:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BASE_DIR, text=True, timeout=5).strip()
        except Exception:
            commit = "unknown"
    return jsonify({"commit": commit, "commit_short": commit[:7]})


@app.route("/api/cache/invalidate", methods=["GET", "POST"])
def cache_invalidate():
    """Removes cached geocode/address-lookup entries by building-name
    substring — the fix for the exact pain this app has hit repeatedly: a
    fix to geocoding/address-lookup logic can be completely correct and
    still keep producing the old wrong output, because the stale answer
    is served from cache before the new code ever runs. Before this
    existed, clearing a poisoned entry meant hand-editing the on-disk
    cache file directly, then separately editing the B2/S3-mirrored copy
    (via its own dashboard) so a Render redeploy didn't just pull the
    stale copy right back down, and finally restarting the Render service
    so the currently-running worker's own in-memory copy (loaded once,
    on first use, and never re-read from disk/S3 afterward) picked up
    the change at all. Calling this endpoint on the live app does all
    three at once, including the in-memory piece specifically *because*
    it runs inside that same worker process — no redeploy/restart
    needed. (That last part relies on this app running as a single
    gunicorn worker, per Procfile/render.yaml; with multiple workers a
    request here would only clear the one worker that happened to handle
    it.)

    GET or POST, query string or form field: ?building=<substring>
    (case-insensitive, matched against both caches' keys — geocode's
    "<address>, london, uk" and address_lookup's "<building>|<provider>").
    Add &dry_run=1 to preview what would be removed without changing
    anything, e.g. to sanity-check a substring isn't broader than
    intended before actually deleting."""
    building = (request.values.get("building") or "").strip()
    if not building:
        return jsonify({"error": "missing required 'building' parameter"}), 400
    dry_run = request.values.get("dry_run", "").lower() in ("1", "true", "yes")

    from extraction import address_lookup, geocode as geocode_module

    if dry_run:
        needle = building.lower()
        geo_cache = geocode_module._load_cache()
        addr_cache = address_lookup._load_cache()
        geo_matches = [k for k in geo_cache if needle in k]
        addr_matches = [k for k in addr_cache if needle in k]
    else:
        geo_matches = geocode_module.invalidate(building)
        addr_matches = address_lookup.invalidate(building)

    return jsonify(
        {
            "building": building,
            "dry_run": dry_run,
            "geocode_cache": geo_matches,
            "address_lookup_cache": addr_matches,
            "total_matched": len(geo_matches) + len(addr_matches),
        }
    )


@app.route("/api/process", methods=["POST"])
def process():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    # Computed at the true start of request handling (before file saving,
    # rule matching, LLM calls — everything that counts against gunicorn's
    # own --timeout for this WHOLE request, not just geocoding) and passed
    # straight through to process_files, which threads it into every
    # file's own _geocode_records call unchanged — one shared budget for
    # the whole batch, not reset per file. See extraction.pipeline's own
    # BATCH_DEADLINE_SECONDS for why this exists (confirmed via a real
    # Render SIGKILL, 2026-07).
    batch_deadline = time.monotonic() + BATCH_DEADLINE_SECONDS

    memlog.log("request start")
    _cleanup_old_batches()
    batch_id = uuid.uuid4().hex
    batch_dir = OUTPUT_DIR / batch_id

    tmpdir = Path(tempfile.mkdtemp(prefix="office-avail-"))
    try:
        saved_paths = []
        unsupported_results = []
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                unsupported_results.append(
                    {"filename": f.filename, "status": "error", "method": None, "record_count": 0, "error": f"Unsupported file type '{ext}'"}
                )
                continue
            dest = tmpdir / f.filename
            f.save(dest)
            saved_paths.append(dest)

        # Finish EACH file (materialize galleries + write xlsx + free
        # embedded brochure bitmaps) before starting the next file's
        # brochure enrichment. Confirmed real on Render free tier (~512MB):
        # holding MetSpace+Knotel+WP+Union embeds until the end peaked
        # ~1.2GB locally and left production with the same blank/single
        # photo symptoms even after deadline/gallery logic fixes.
        upload_jobs = []
        processed_results = []
        claimed_names = []
        batch_total = len(saved_paths)
        for file_index, path in enumerate(saved_paths):
            one = process_files(
                [path],
                deadline=batch_deadline,
                brochure_enrichment=True,
                batch_total_files=batch_total,
                batch_file_index=file_index,
            )[0]
            one["_source_path"] = path
            processed_results.append(one)
            if one["status"] != "ok":
                continue
            if not batch_dir.exists():
                batch_dir.mkdir(parents=True)
            name = make_unique_names(claimed_names + [one["display_name"]])[-1]
            claimed_names.append(name)
            one["output_file"] = f"{name}.xlsx"
            upload_jobs.extend(
                _finish_ok_result(one, batch_dir=batch_dir, batch_id=batch_id, name=name, deadline=batch_deadline)
            )
            # Drop heavy in-memory brochure payloads before the next file
            # (esp. UNION Box PDFs / MetSpace Drive embeds) starts.
            one["properties"] = []
            one["email_html"] = None
            one["pages_text"] = None
            one["html_items"] = None
            for rec in one.get("records") or []:
                rec.pop("_brochure_embedded_assets", None)
            gc.collect()

        results = processed_results + unsupported_results

        # Mirroring the geocode/address-lookup on-disk caches to B2/S3 used
        # to happen synchronously inside process_files itself — confirmed
        # via Render's own logs that a worker was once killed while stuck
        # inside exactly that call (a real network round-trip that can run
        # long), which a generic SIGKILL then gets misreported as "Perhaps
        # out of memory?" regardless of the real cause. Backgrounded here,
        # unconditionally (each flush_to_storage is already a cheap no-op
        # if nothing was cached this run, or if storage isn't configured
        # at all), same as every other storage.upload call below.
        threading.Thread(target=_flush_caches, daemon=True).start()

        if upload_jobs:
            # Hosted High Res galleries (and sibling brochure images) must
            # be durable before the client can click a cell — async upload left
            # MetSpace Drive embeds returning {"error":"File not found"} when
            # the worker's local disk was already gone (max-requests recycle).
            # Sync here; cache flush stays backgrounded above.
            _upload_all(upload_jobs)

        response_files = [
            {
                "filename": r["filename"],
                "status": r["status"],
                "method": r["method"],
                "record_count": r["record_count"],
                "error": r["error"],
                # Set alongside a normal "ok" status/None error — a file
                # that extracted fine but hit Gemini's daily quota partway
                # through its own address-lookup fallback (extraction.
                # pipeline._geocode_records), not something that failed
                # the file itself. The frontend's Notes column shows
                # whichever of error/warning is set.
                "warning": r.get("warning"),
                "output_file": r.get("output_file"),
                "source_file": r.get("source_file"),
            }
            for r in results
        ]
        memlog.log("request end, about to return response")
        return jsonify({"batch_id": batch_id, "files": response_files})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# Uploaded PDFs that *are* the brochure document (or the provider's PDF
# schedule that replaces a per-listing brochure link). Seed blank Brochure
# PDF cells with the hosted source download URL — this replaces the old
# Link to File surface for these formats.
_SOURCE_PDF_IS_BROCHURE_METHODS = {"rule:BC", "rule:Breezblok"}


def _should_seed_brochure_from_source_pdf(method, filename, records):
    """True when the uploaded PDF itself should fill blank Brochure PDF.

    Emails/xlsx never reach this helper (caller checks .pdf). BC Current
    Availability and Breezblok sheets are brochure-source PDFs; so are LLM
    brochure uploads, filenames containing 'brochure', and single-building
    PDFs. Other multi-building PDF schedules without an explicit brochure
    method stay unseeded.
    """
    method = method or ""
    if method.startswith("llm") or method in _SOURCE_PDF_IS_BROCHURE_METHODS:
        return True
    if "brochure" in (filename or "").lower():
        return True
    buildings = {
        (record.get("Building") or "").strip().lower()
        for record in records
        if (record.get("Building") or "").strip()
    }
    return len(buildings) == 1


def _seed_brochure_from_source_pdf(records, source_path, source_url, method, original_filename):
    """If Brochure PDF is blank and the upload is the property brochure PDF,
    point Brochure PDF at the hosted source download URL.

    Replaces the old Link to File surface for brochure-source PDFs (BC,
    Breezblok, LLM brochure uploads, etc.) without stuffing every PDF
    upload into Brochure PDF.
    """
    if not source_url or source_path.suffix.lower() != ".pdf":
        return
    if not _should_seed_brochure_from_source_pdf(method, original_filename, records):
        return
    for record in records:
        if not (record.get("Brochure PDF") or "").strip():
            record["Brochure PDF"] = source_url


def _finish_ok_result(r, batch_dir, batch_id, name, deadline):
    """Persist source + galleries + spreadsheet for one successful file.

    Returns upload_jobs for background mirroring. Called before the next
    uploaded file starts brochure enrichment so Render free-tier memory
    stays bounded and finalize still has batch clock left.
    """
    upload_jobs = []
    # Persist the source artifact alongside the generated spreadsheet for
    # download/API provenance (_source_file_url). Reuses the same
    # collision-free `name` the spreadsheet got, so it can't collide with
    # another source file in this batch.
    #
    # An .eml with an HTML body stores that HTML directly
    # (extraction.pipeline already parsed it out, unmodified) —
    # opens in-browser like the original email, images included,
    # since the markup already points at the sender's hosted image
    # URLs. There's nothing to render or convert. Everything else
    # (PDF, DOCX, XLSX, CSV, a plain-text-only .eml) stores the
    # original uploaded file as-is.
    source_path = r["_source_path"]
    email_html = r.get("email_html")
    source_filename = f"{name}.html" if email_html else f"{name}{source_path.suffix.lower()}"
    source_filename = _disambiguate_source_filename(source_filename, r["output_file"])

    if email_html:
        (batch_dir / source_filename).write_text(email_html, encoding="utf-8")
    else:
        shutil.copy2(source_path, batch_dir / source_filename)
    r["source_file"] = source_filename
    source_url = _download_url(batch_id, source_filename)
    # The hosted URL only exists after this source artifact has a
    # collision-safe batch filename. Update the canonical typed
    # properties, then serialize them (source URL stays on
    # _source_file_url — not a public spreadsheet column).
    for prop in r.get("properties") or []:
        # Display the user's original uploaded filename for audit
        # traceability.  The URL may target a collision-safe stored
        # copy (or an HTML rendering of an email), but that storage
        # implementation detail must not replace source identity.
        prop.set_source_reference(r["filename"], source_url)
    if r.get("properties"):
        r["records"] = [prop.to_record() for prop in r["properties"]]

    # Floor Plan/High Res Images for a PDF source whose own rule (or
    # the LLM fallback) doesn't already supply them from its own
    # text/table structure — Kitt's already gets these from its own
    # table columns (extraction.rules.grid) and Knotel already gets
    # Floor Plan from its email's own "Download Floorplan" link
    # (extraction.rules.knotel); neither goes through this. BC and
    # Breezblok are rule-based (extraction.rules.bc/breezblok) but,
    # like the LLM fallback, their own text has no image data at
    # all — real embedded images only — genuinely blank when a
    # source PDF has none (BC's own table has none at all) or a
    # listing's building can't be matched to a page.
    if r["method"] in PDF_IMAGE_ENRICHED_METHODS and source_path.suffix.lower() == ".pdf" and r.get("pages_text"):
        memlog.log("before image extraction", r["filename"])
        upload_jobs.extend(_attach_pdf_images(r["records"], source_path, r["pages_text"], batch_dir, batch_id, name))
        memlog.log("after image extraction", r["filename"])
    elif (r["method"] or "").startswith("llm") and r.get("html_items"):
        # The non-PDF counterpart to the branch above: a brand-new
        # provider's .eml/.html file with no dedicated rule yet
        # (confirmed 2026-07 — The Workplace Company, the first
        # real source seen through this path — previously got
        # NONE of Floor Plan/High Res Images/Brochure PDF at all,
        # despite the source genuinely having real listing photos
        # and a "Brochure" link). Sets Floor Plan/Brochure PDF
        # directly and stashes High Res Images candidates on
        # "_high_res_candidates" for _finalize_high_res_images
        # below, same convention as extraction.rules.gpe.
        # llm:chunked must take this path too (method is not exactly
        # "llm") — otherwise dense spreadsheet/email fallbacks skip
        # secondary media attachment after a successful chunk parse.
        html_images.enrich_records(r["records"], r["html_items"])
    elif (
        (r["method"] or "").startswith("llm")
        and source_path.suffix.lower() in (".xlsx", ".xls")
        and r.get("row_links")
    ):
        # The .xlsx/.xls counterpart to the two branches above: a
        # raw-spreadsheet source with no dedicated rule of its own
        # (confirmed 2026-07 — a UNION file, the first one seen
        # through this path — came back with Brochure PDF/Floor
        # Plan blank for every row despite its own "Brochure"
        # column linking every row to a real box.com URL; pandas'
        # own cell-value read, used to build the LLM's own
        # plain-text prompt input, discards hyperlinks entirely,
        # so nothing in that text could ever have recovered it).
        # Same llm:chunked gotcha as above — Workplace Plus London
        # hits chunked extraction and must still recover hyperlinks.
        xlsx_links.enrich_records(r["records"], r["row_links"])

    # Brochure-source PDFs (BC Current Availability, Breezblok, LLM
    # brochure uploads): when Brochure PDF is still blank, surface the
    # hosted source PDF there. Never applies to email/xlsx uploads.
    _seed_brochure_from_source_pdf(
        r["records"], source_path, source_url, r.get("method") or "", r.get("filename") or ""
    )

    # Generic, source-agnostic finishing step: any rule (not just
    # PDF ones) can stash a list of real candidate photo URLs on a
    # record as "_high_res_candidates" instead of setting High Res
    # Images directly, when it can't tell in advance whether a
    # listing has one photo or several (extraction.rules.gpe does
    # this — a building can genuinely have two distinct real
    # photos, one from a promotional blurb and one from its own
    # listing card). Turns 2+ into a small gallery page, 1 into a
    # direct link, same as the PDF path above. Sibling floors then
    # share finalized High Res / Floor Plan when one floor succeeded.
    upload_jobs.extend(_materialize_brochure_assets(r["records"], batch_dir, batch_id, name))
    upload_jobs.extend(_finalize_high_res_images(r["records"], batch_dir, batch_id, name, deadline=deadline))
    _share_finalized_media_across_buildings(r["records"])
    image_warning = _image_coverage_warning(r["records"], r.get("method") or "")
    if image_warning:
        existing = (r.get("warning") or "").strip()
        r["warning"] = f"{existing} {image_warning}".strip() if existing else image_warning

    memlog.log("before spreadsheet write", r["filename"])
    write_xlsx(batch_dir / r["output_file"], r["records"], sheet_title=name, include_qa_sheet=False)
    memlog.log("after spreadsheet write", r["filename"])

    # Queued for the background thread below (storage.upload is a
    # no-op returning False if S3_BUCKET etc. aren't configured) so
    # these download links keep working past Render's ephemeral
    # disk being wiped on redeploy/restart, and past our own
    # hourly local cleanup below — local disk stays the fast path,
    # this is just the durable fallback /api/download reaches for
    # when the local copy is already gone.
    upload_jobs.append((f"{batch_id}/{source_filename}", batch_dir / source_filename))
    upload_jobs.append((f"{batch_id}/{r['output_file']}", batch_dir / r["output_file"]))
    return upload_jobs


def _disambiguate_source_filename(source_filename, output_filename):
    """Returns source_filename unchanged unless it exactly matches
    output_filename (the generated spreadsheet's own name, always
    "{name}.xlsx") — in which case a distinguishing " (original)" suffix
    is inserted before the extension.

    Confirmed via a real report (2026-07, a UNION file — sent as a raw
    .xlsx with no dedicated rule, going through the LLM fallback): reusing
    the exact same collision-free `name` for both the copied source
    artifact and the generated spreadsheet collides whenever the
    ORIGINAL upload is itself .xlsx — both would resolve to the identical
    path in batch_dir. shutil.copy2 (in the caller, below) would write the
    real source there first, but write_xlsx further down that same loop
    then silently overwrites that exact file with the GENERATED
    spreadsheet, so the persisted source artifact becomes a second copy of the
    output file instead of the real original. Only .xlsx
    can actually collide today (an .eml's own extracted-HTML path always
    gets .html; every other format keeps its own distinct extension), but
    this is written generically rather than hardcoded to ".xlsx", so it
    stays correct if the generated spreadsheet's own extension ever
    changes."""
    if source_filename != output_filename:
        return source_filename
    stem, dot, ext = source_filename.rpartition(".")
    return f"{stem} (original).{ext}" if dot else f"{source_filename} (original)"


def _download_url(batch_id, filename):
    """Absolute URL for /api/download/<batch_id>/<filename>, usable outside
    the app's own JS fetch (e.g. an Excel hyperlink click) — so it carries
    the access token as a query param, since a browser navigating there
    directly can't send the X-Access-Token header the page's own JS uses.
    Uses X-Forwarded-Proto over request.scheme so this comes out as https
    on Render, which terminates TLS at its edge and forwards plain http."""
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    token_part = f"?token={quote(ACCESS_TOKEN)}" if ACCESS_TOKEN else ""
    return f"{scheme}://{request.host}/api/download/{quote(batch_id)}/{quote(filename)}{token_part}"


def _attach_pdf_images(records, source_path, pages_text, batch_dir, batch_id, name):
    """Fills High Res Images (and, where a real one is found, Floor Plan)
    for records whose Building can be matched to page(s) of the source PDF
    with genuine embedded images (extraction.pdf_images) — never
    fabricated, left blank when no match/no image exists. A listing can
    span several pages with several real photos each (confirmed
    empirically: BC's own single-listing brochures run up to 10 pages with
    as many as 6 images on one page) — since a spreadsheet cell can only
    hold one hyperlink, 2+ photos get a small, self-contained HTML gallery
    page instead of just the first one found; exactly 1 links directly, no
    gallery indirection needed.

    Every matched image is classified individually as a floor-plan diagram
    or a real photo — deliberately per-image, not per-page: confirmed on
    Breezblok's John Stow House brochure that a floor-plan diagram and a
    real desk photo can share the same PDF page, so an earlier per-page-
    only classification (excluding a whole "floor plan" page from the
    photo gallery, based only on that page's own text) missed this case
    entirely — the page's text never mentioned "floor plan" at all, so
    neither the page nor the diagram on it was ever excluded, and it was
    silently swept into the photo gallery instead of populating Floor
    Plan. See extraction.pdf_images.is_floorplan_page (source-labeled
    text, e.g. BC's own "Example Floorplan" heading) and
    is_floorplan_image (a pixel-content fallback for sources with no such
    label) — either signal marks an image as the floor plan rather than a
    photo. A listing with more than one floor-plan-classified image (not
    seen in any source tested) just uses the first found; there's no
    established gallery convention for Floor Plan the way there is for
    High Res Images.

    A single-record document (e.g. BC's own "2-7 Clerkenwell Green"
    brochure) attaches every real image in the whole PDF to that one
    record, position irrelevant — there's no other record to misattribute
    to. A multi-record document instead uses extraction.pdf_images.
    match_listings_to_images to pair each image to the SPECIFIC listing
    it's positioned next to on the page, not every record whose building
    name happens to appear anywhere on that page — confirmed necessary
    empirically (Crown Estate, 2026-07): its pages routinely hold 2-6
    distinct listings sharing one page (a 2- or 3-column grid), each with
    its own real photos, and the previous whole-page attribution silently
    merged unrelated buildings' photos into one shared gallery whenever a
    page held more than one listing.

    Returns the (storage_key, local_path) pairs for the caller to upload —
    doesn't upload them itself, so a source with many distinct images
    (e.g. Crown Estate's ~15) doesn't add that many synchronous network
    round-trips to this request; see the background-thread upload in
    process() above.

    Deliberately uses pdf_images.scan_pages (a cheap, hash-only pass) plus
    load_page_images/match_listings_to_images (decode one page's images
    at a time, on demand) rather than extract_page_images (which
    materializes every real image for the whole document at once) —
    confirmed via Render's own logs that processing a large PDF (Crown
    Estate, 4.3MB) got the worker SIGKILLed for exceeding the free tier's
    512MB RAM limit. Bounding this to one page's images at a time caps how
    much of a large, photo-heavy document this function can ever hold in
    memory at once, regardless of how many pages/records it has."""
    page_hashes = pdf_images.scan_pages(source_path)
    if not page_hashes:
        return []

    jobs = []
    saved_image_urls = {}  # image content hash -> already-saved download
    # URL, so the same real image isn't re-saved/re-uploaded twice across
    # different listings/galleries/floor-plan-links that happen to include it.
    gallery_url_by_photos = {}  # tuple(photo URLs) -> gallery (or single-
    # image) URL, so 2+ listings sharing the same real photos (e.g. two
    # floors of one building) share one file instead of a duplicate.
    gallery_state = {"count": 0}

    def _save(page_num, image_bytes, ext):
        h = hashlib.sha256(image_bytes).hexdigest()
        if h not in saved_image_urls:
            image_filename = f"{name}_p{page_num + 1}_{h[:8]}.{ext}"
            (batch_dir / image_filename).write_bytes(image_bytes)
            jobs.append((f"{batch_id}/{image_filename}", batch_dir / image_filename))
            saved_image_urls[h] = _download_url(batch_id, image_filename)
        return saved_image_urls[h]

    def _finish_record(record, page_images):
        """page_images: [(page_num, image_bytes, ext, link_floorplan_url), ...]
        already matched to this one record — classifies each as floor plan
        vs photo, saves/uploads, and sets Floor Plan/High Res Images.

        link_floorplan_url (extraction.pdf_images._link_uri_for_rect)
        takes priority over the pixel/text-based classification below:
        confirmed empirically (Crown Estate, 2026-07) that a source can
        put a link annotation directly on top of a listing's own photo,
        pointing to an external 3D-tour/floor-plan viewer — a real,
        source-labeled Floor Plan signal that isn't visible in the
        image's own pixel content or embedded bytes at all, so it can't
        be found by is_floorplan_image no matter how it's tuned. The
        image itself still gets classified/saved normally regardless —
        a listing can have both a real photo (High Res Images) and a
        separate floor-plan/tour link (Floor Plan) at once."""
        building = record.get("Building")
        photo_urls = []
        floorplan_url = None
        for page_num, image_bytes, ext, link_floorplan_url in page_images:
            if floorplan_url is None and link_floorplan_url:
                floorplan_url = link_floorplan_url
            page_is_labeled_floorplan = pdf_images.is_floorplan_page(pages_text[page_num] if page_num < len(pages_text) else "")
            is_floorplan = page_is_labeled_floorplan or pdf_images.is_floorplan_image(image_bytes)
            url = _save(page_num, image_bytes, ext)
            if is_floorplan:
                if floorplan_url is None:
                    floorplan_url = url
            elif url not in photo_urls:
                photo_urls.append(url)

        if floorplan_url:
            record["Floor Plan"] = floorplan_url
        if not photo_urls:
            return

        photos_key = tuple(photo_urls)
        if photos_key not in gallery_url_by_photos:
            if len(photo_urls) == 1:
                gallery_url_by_photos[photos_key] = photo_urls[0]
            else:
                gallery_state["count"] += 1
                gallery_filename = f"{name}_gallery{gallery_state['count']}.html"
                gallery_html = pdf_images.build_gallery_html(building or name, photo_urls)
                (batch_dir / gallery_filename).write_text(gallery_html, encoding="utf-8")
                jobs.append((f"{batch_id}/{gallery_filename}", batch_dir / gallery_filename))
                gallery_url_by_photos[photos_key] = _download_url(batch_id, gallery_filename)
        record["High Res Images"] = gallery_url_by_photos[photos_key]

    if len(records) == 1:
        # A single-listing brochure spanning the whole document — see the
        # docstring above for why this skips position-based matching
        # entirely: with only one record, every real image belongs to it
        # regardless of where on the page it sits.
        page_images = [
            (p, image_bytes, ext, link_floorplan_url)
            for p in sorted(page_hashes.keys())
            for image_bytes, ext, link_floorplan_url in pdf_images.load_page_images(source_path, p, page_hashes[p])
        ]
        _finish_record(records[0], page_images)
        return jobs

    # Grouped by exact Building text (not just find_matching_pages'
    # overlapping candidates) so several floors sharing byte-identical
    # text — e.g. Crown Estate's "Princes House, 38 Jermyn Street" across
    # 4 pages, 2 floors per page — get distributed across their REAL
    # distinct page occurrences via pdf_images.count_heading_occurrences,
    # rather than every one of them being registered on every matching
    # page (which would let several pages' images all pile onto whichever
    # records happen to come first, while later floors get none at all —
    # confirmed exactly this empirically, 2026-07).
    same_building_records = defaultdict(list)
    for i, record in enumerate(records):
        same_building_records[record.get("Building") or ""].append(i)

    records_by_page = defaultdict(list)
    for building, indices in same_building_records.items():
        if len(indices) == 1:
            matching_pages = [p for p in pdf_images.find_matching_pages(building, pages_text) if p in page_hashes]
            for p in matching_pages:
                records_by_page[p].append((indices[0], building, records[indices[0]].get("Floor/Unit") or ""))
            continue
        # Several records share this exact text — find_all_matching_pages
        # (the union across every candidate tier), not find_matching_pages
        # (stops at the first tier that matches anything): confirmed
        # necessary when those records' real occurrences sit on pages
        # with different levels of text detail (e.g. one page's raw text
        # repeats an area code, another floor of the same building sits
        # on a page that doesn't) — the narrower single-tier lookup would
        # silently miss whichever page only the broader tier matches.
        matching_pages = [p for p in pdf_images.find_all_matching_pages(building, pages_text) if p in page_hashes]
        if not matching_pages:
            continue
        occurrence_counts = pdf_images.count_heading_occurrences(source_path, matching_pages, building)
        remaining = list(indices)
        for p in matching_pages:
            for _ in range(occurrence_counts.get(p, 0)):
                if not remaining:
                    break
                idx = remaining.pop(0)
                records_by_page[p].append((idx, building, records[idx].get("Floor/Unit") or ""))

    images_by_record = pdf_images.match_listings_to_images(source_path, page_hashes, records_by_page)
    for i, record in enumerate(records):
        page_images = images_by_record.get(i)
        if page_images:
            _finish_record(record, page_images)

    return jobs


def _is_local_download_url(url, batch_id):
    """True for assets just materialized in this processing batch.

    Fetching these files back through the app's own HTTP endpoint can
    deadlock a Gunicorn worker and needlessly decode large images in memory.
    """
    parsed = urlparse(str(url or ""))
    batch_prefix = f"/api/download/{quote(str(batch_id), safe='')}/"
    return parsed.path.startswith(batch_prefix) or (
        parsed.path == "/api/download" and bool(parsed.query)
    )


def _accept_image_url_under_deadline(url):
    """Keep already-discovered property photos when HTTP validation is cut.

    Confirmed real (2026-07 MetSpace+Knotel+WP+Union): Knotel enrichment
    produced 5 Directus photo URLs per row, then Union Box PDFs overran
    batch_deadline; finalize marked every non-source URL
    OPTIONAL_IMAGE_VALIDATION_SKIPPED and left email-only singles. Trust
    image-like URLs already found during enrichment rather than erasing them.

    Prefer path/extension evidence over a fixed CDN host allowlist so
    unknown asset hosts survive the same way Directus/Cloudinary do.
    """
    from extraction.html_images import is_image_like_url

    text = str(url or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return False
    if is_image_like_url(text):
        return True
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    # Common image CDNs without a revealing path still host photos.
    if any(token in host for token in ("cloudinary", "imgix", "cloudfront", "imagekit", "cdn")):
        return True
    return False


def _local_download_path(url, batch_dir, batch_id):
    """Resolve a batch download URL back to the local file written for it."""
    from urllib.parse import parse_qs, unquote

    parsed = urlparse(str(url or ""))
    batch_prefix = f"/api/download/{quote(str(batch_id), safe='')}/"
    filename = ""
    if parsed.path.startswith(batch_prefix):
        filename = Path(unquote(parsed.path[len(batch_prefix) :])).name
    elif parsed.path == "/api/download":
        filename = Path(unquote((parse_qs(parsed.query).get("file") or [""])[0])).name
    if not filename:
        return None
    path = batch_dir / filename
    return path if path.is_file() else None


def _is_replaceable_viewer_url(url):
    """True for JS/document viewers that should yield to a hosted floor-plan image.

    UNION pre-fills Floor Plan with app.box.com/s/… and Workplace Plus /
    MetSpace use Drive viewers — those block materialising a real plan
    bitmap unless overwritten here.
    """
    text = str(url or "").strip()
    if not text:
        return True
    if "/api/download/" in text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if "box.com" in host and "/shared/static/" not in path:
        return True
    if host in {"drive.google.com", "docs.google.com"}:
        return True
    if any(token in host for token in ("canva.com", "canva.link", "pitch.com")):
        return True
    return False


def _finalize_high_res_images(records, batch_dir, batch_id, name, image_validator=validate_image_url, deadline=None):
    """Validate, deduplicate and publish property-photo candidates.

    Validation is bounded and cached once per canonical URL. Invalid,
    inaccessible, blank/near-solid, decorative, and non-image resources can
    never produce a successful gallery. Distinct CDN URLs that resolve to the
    exact same image bytes (confirmed real: Knotel Directus asset IDs) count
    once. Unhashed soft-accepts never join a gallery that already has photos,
    so deadline pressure cannot reintroduce those duplicates. Local batch
    assets are validated from disk; gallery HTML uses absolute download URLs
    (not base64 data URIs) so free-tier RSS stays bounded.
    """
    jobs = []
    gallery_url_by_candidates = {}
    gallery_count = 0
    validation_cache = {}

    def _finalize_photo_need(record):
        cands = list(record.get("_high_res_candidates") or [])
        cands.extend(record.get("_source_high_res_candidates") or [])
        existing = str(record.get("High Res Images") or "").strip()
        if existing and ".html" not in existing.lower():
            cands.insert(0, existing)
        count = len({normalize_url(url) for url in cands if url})
        if count <= 0:
            return 0
        if count < MIN_HIGH_RES_IMAGES:
            return 1
        return 2

    # Under-filled listings first so deadline pressure cannot starve them
    # after already-complete Knotel/GPE rows burn validation time.
    for record in sorted(records, key=_finalize_photo_need):
        raw_candidates = record.pop("_high_res_candidates", None)
        source_candidates = list(record.pop("_source_high_res_candidates", None) or [])
        if source_candidates:
            raw_candidates = source_candidates + list(raw_candidates or [])
        existing_image = str(record.get("High Res Images") or "")
        if not raw_candidates and existing_image and ".html" not in existing_image.lower():
            raw_candidates = [existing_image]
        if not raw_candidates:
            if not existing_image:
                record["_high_res_image_count"] = 0
                record.setdefault("_link_diagnostics", []).append(
                    LinkDiagnostic("NO_IMAGES_DISCOVERED", detail="No property-photo candidates reached media finalisation.")
                )
            continue
        candidates = merge_candidate_urls(raw_candidates)
        diagnostics = record.setdefault("_link_diagnostics", [])
        diagnostics.append(LinkDiagnostic("IMAGES_DISCOVERED", detail=f"{len(candidates)} candidate(s)"))
        # Never put the Floor Plan cell URL into High Res (MetSpace email
        # floorplans / hosted plan JPEGs sometimes reappear as candidates).
        floor_plan_url = normalize_url(str(record.get("Floor Plan") or ""))
        if floor_plan_url:
            before = len(candidates)
            candidates = [url for url in candidates if normalize_url(url) != floor_plan_url]
            if len(candidates) < before:
                diagnostics.append(LinkDiagnostic(
                    "IMAGE_IS_FLOORPLAN",
                    original_url=floor_plan_url,
                    detail="Excluded Floor Plan URL from High Res Images candidates.",
                ))
        named_plans = [url for url in candidates if _FLOORPLAN_URL_RE.search(url or "")]
        if named_plans:
            plan_names = {normalize_url(url) for url in named_plans}
            candidates = [url for url in candidates if normalize_url(url) not in plan_names]
            for plan_url in named_plans:
                diagnostics.append(LinkDiagnostic(
                    "IMAGE_IS_FLOORPLAN",
                    original_url=plan_url,
                    detail="Excluded floor-plan-named URL from High Res Images candidates.",
                ))
                if _is_replaceable_viewer_url(str(record.get("Floor Plan") or "")):
                    record["Floor Plan"] = plan_url
        trusted = set(merge_candidate_urls(source_candidates))
        valid = []
        seen_hashes = set()
        rejected = []
        for candidate in candidates:
            if len(valid) >= MAX_HIGH_RES_IMAGES:
                # Keep scanning only long enough to know we capped; further
                # candidates are ignored so duplicate URLs cannot displace
                # later distinct photos after the 5–8 target is already met.
                break
            cached = validation_cache.get(normalize_url(candidate))
            if _is_local_download_url(candidate, batch_id):
                local_path = _local_download_path(candidate, batch_dir, batch_id)
                if local_path is not None:
                    payload = local_path.read_bytes()
                    result = evaluate_image_bytes(payload, url=candidate)
                    if result.get("ok"):
                        result["status"] = "VALID_SOURCE_IMAGE"
                    elif result.get("status") == "IMAGE_IS_FLOORPLAN":
                        if _is_replaceable_viewer_url(str(record.get("Floor Plan") or "")):
                            record["Floor Plan"] = candidate
                    elif result.get("status") == "NOT_AN_IMAGE":
                        # Fixture/edge bytes that are not decodable as a
                        # bitmap still keep their batch URL; blank/too-small
                        # rejects above still apply for real placeholder slides.
                        result = {
                            "ok": True,
                            "url": candidate,
                            "status": "VALID_SOURCE_IMAGE",
                            "content_hash": image_content_hash(payload),
                        }
                else:
                    # Batch URL with no local file yet (tests / upload-only) —
                    # never loopback-fetch; trust the URL the batch just minted.
                    result = {"ok": True, "url": candidate, "status": "VALID_SOURCE_IMAGE"}
            elif candidate in trusted:
                # Source photos: validate when budget allows so Knotel-style
                # same-bytes/different-URL duplicates can be content-hashed out.
                # Soft-accept a fetch failure only when this listing still has
                # zero photos — never pad galleries with dead/blank/floorplan
                # URLs (MetSpace blank slots; floor-plan diagrams mis-kept as
                # "source" photos).
                if deadline is not None and time.monotonic() >= deadline - 5:
                    # Under deadline still reject obvious floor-plan URLs —
                    # soft-accept must never reintroduce plans into High Res.
                    if _FLOORPLAN_URL_RE.search(candidate or ""):
                        result = {"ok": False, "url": candidate, "status": "IMAGE_IS_FLOORPLAN"}
                    else:
                        result = {"ok": True, "url": candidate, "status": "VALID_SOURCE_IMAGE"}
                else:
                    validator_kwargs = {"cache": validation_cache}
                    if deadline is not None:
                        validator_kwargs["deadline"] = deadline
                    result = image_validator(candidate, **validator_kwargs)
                    hard_reject = {
                        "IMAGE_BLANK_OR_EMPTY",
                        "IMAGE_TOO_SMALL",
                        "IMAGE_IS_FLOORPLAN",
                        "NOT_AN_IMAGE",
                    }
                    if (
                        not result.get("ok")
                        and result.get("status") not in hard_reject
                        and not valid
                    ):
                        result = {"ok": True, "url": candidate, "status": "VALID_SOURCE_IMAGE"}
            elif cached is not None:
                # Confirmed real (2026-07, GPE): a building repeated across
                # several rows (one per floor) shares the exact same photo
                # URL(s) — already validated and cached on that building's
                # FIRST row. Without this check, once the shared batch
                # deadline (below) got close, every LATER row for the same
                # building hit the deadline branch and was marked skipped
                # even though its answer was already known for free, right
                # here in the cache — silently blanking High Res Images for
                # every row of a building after its first.
                result = cached
            elif deadline is not None and time.monotonic() >= deadline - 5:
                if _FLOORPLAN_URL_RE.search(candidate or ""):
                    result = {"ok": False, "url": candidate, "status": "IMAGE_IS_FLOORPLAN"}
                elif _accept_image_url_under_deadline(candidate):
                    result = {
                        "ok": True,
                        "url": candidate,
                        "status": "VALID_IMAGE_ACCEPTED_UNDER_DEADLINE",
                    }
                else:
                    result = {
                        "ok": False, "url": candidate,
                        "status": "OPTIONAL_IMAGE_VALIDATION_SKIPPED",
                    }
            else:
                validator_kwargs = {"cache": validation_cache}
                if deadline is not None:
                    validator_kwargs["deadline"] = deadline
                result = image_validator(candidate, **validator_kwargs)
            if result.get("ok"):
                resolved = result.get("url") or candidate
                content_hash = result.get("content_hash") or ""
                soft_unhashed = (not content_hash) and result.get("status") in {
                    "VALID_SOURCE_IMAGE",
                    "VALID_IMAGE_ACCEPTED_UNDER_DEADLINE",
                }
                if content_hash and content_hash in seen_hashes:
                    rejected.append((candidate, "IMAGE_DUPLICATE_CONTENT"))
                    continue
                if resolved in valid:
                    rejected.append((candidate, "IMAGE_DUPLICATE_URL"))
                    continue
                # Soft-accepts without a content hash: allow up to the High
                # Res minimum under deadline pressure so Knotel/GPE rows keep
                # a real multi-image gallery. Beyond that, skip — otherwise
                # same-bytes/different-UUID Directus URLs flood the cell.
                if soft_unhashed and len(valid) >= MIN_HIGH_RES_IMAGES:
                    rejected.append((candidate, "IMAGE_UNHASHED_SKIPPED"))
                    continue
                if content_hash:
                    seen_hashes.add(content_hash)
                valid.append(resolved)
            else:
                rejected.append((candidate, result.get("status") or "IMAGE_REJECTED"))
        for candidate, status in rejected:
            detail = {
                "IMAGE_DUPLICATE_CONTENT": "Exact duplicate of an already-selected photo; kept only one copy.",
                "IMAGE_DUPLICATE_URL": "Duplicate URL after normalisation; kept only one copy.",
                "IMAGE_BLANK_OR_EMPTY": "Near-solid/blank placeholder excluded from High Res Images.",
                "IMAGE_IS_FLOORPLAN": "Floor-plan diagram excluded from High Res Images (belongs in Floor Plan).",
                "IMAGE_UNHASHED_SKIPPED": "Skipped unhashed soft-accept after distinct photos already selected (prevents CDN duplicate galleries).",
            }.get(status, "Candidate was excluded from High Res Images.")
            diagnostics.append(LinkDiagnostic(status, original_url=candidate, detail=detail))
            if status == "IMAGE_IS_FLOORPLAN" and _is_replaceable_viewer_url(str(record.get("Floor Plan") or "")):
                record["Floor Plan"] = candidate
        if not valid:
            record["High Res Images"] = ""
            record["_high_res_image_count"] = 0
            diagnostics.append(LinkDiagnostic("IMAGES_DISCOVERED_BUT_REJECTED", detail=f"0 of {len(candidates)} candidate(s) passed validation"))
            continue

        if len(candidates) > len(valid) and len(valid) >= MAX_HIGH_RES_IMAGES:
            diagnostics.append(LinkDiagnostic(
                "IMAGE_CANDIDATES_CAPPED",
                detail=f"Using first {MAX_HIGH_RES_IMAGES} distinct validated image(s).",
            ))

        key = tuple(valid)
        if key not in gallery_url_by_candidates:
            if len(valid) == 1:
                gallery_url_by_candidates[key] = valid[0]
            else:
                gallery_count += 1
                gallery_filename = f"{name}_photos{gallery_count}.html"
                gallery_path = batch_dir / gallery_filename
                try:
                    # Absolute /api/download URLs (token included) — not base64
                    # data URIs. Inlining every MetSpace/UNION embed ballooned
                    # gallery HTML and kept a second copy of each JPEG in RSS
                    # on the free tier; sync upload makes these durable.
                    gallery_html = pdf_images.build_gallery_html(record.get("Building") or name, valid)
                    gallery_path.write_text(gallery_html, encoding="utf-8")
                    if gallery_path.exists() and gallery_path.stat().st_size and gallery_html.count("<img") >= len(valid):
                        jobs.append((f"{batch_id}/{gallery_filename}", gallery_path))
                        gallery_url_by_candidates[key] = _download_url(batch_id, gallery_filename)
                    else:
                        raise ValueError("generated gallery did not contain every validated image")
                except Exception as exc:
                    # Confirmed real (2026-07, GPE multi-photo buildings): a
                    # failed gallery write used to leave High Res blank even
                    # though validated candidates already existed. Fall back
                    # to the first validated URL so a gallery failure never
                    # erases source/linked photos.
                    diagnostics.append(LinkDiagnostic("GALLERY_CREATION_FAILED", detail=str(exc)))
                    gallery_url_by_candidates[key] = valid[0]

        record["High Res Images"] = gallery_url_by_candidates[key]
        record["_high_res_image_count"] = len(valid)
        if len(valid) == 1:
            status, detail = "DIRECT_IMAGE_ASSIGNED", "1 validated image(s)"
        elif record["High Res Images"] == valid[0]:
            # Gallery write failed; public cell keeps the first validated URL.
            status, detail = (
                "DIRECT_IMAGE_ASSIGNED",
                f"Gallery creation failed; fell back to first of {len(valid)} validated image(s).",
            )
        else:
            status, detail = "GALLERY_CREATED", f"{len(valid)} validated image(s)"
        diagnostics.append(LinkDiagnostic(status, final_url=record["High Res Images"], detail=detail))
        if 0 < len(valid) < MIN_HIGH_RES_IMAGES:
            diagnostics.append(LinkDiagnostic(
                "IMAGE_COUNT_BELOW_TARGET",
                detail=f"{len(valid)} image(s); target is {MIN_HIGH_RES_IMAGES}-{MAX_HIGH_RES_IMAGES} when source/brochures provide photos.",
            ))

    return jobs


def _image_coverage_warning(records, method):
    """Surface a Notes warning when a non-exempt file finishes without the
    expected High Res coverage. BC-style tabular PDFs (no photos in source)
    are exempt; every other provider should have building-matched photos from
    the email/sheet and/or linked brochure HTTPS/PDF embeds."""
    if not records:
        return ""
    if method in IMAGE_EXEMPT_METHODS:
        return ""
    with_images = sum(1 for record in records if str(record.get("High Res Images") or "").strip())
    if with_images == 0:
        return (
            "No High Res Images were produced for any listing. Re-check brochure/property "
            "links in the source, or process this file alone if enrichment hit the time budget."
        )
    below_target = sum(
        1
        for record in records
        if str(record.get("High Res Images") or "").strip()
        and int(record.get("_high_res_image_count") or 1) < MIN_HIGH_RES_IMAGES
    )
    blank = len(records) - with_images
    parts = []
    if blank:
        parts.append(f"{blank} listing(s) have no High Res Images")
    if below_target:
        parts.append(
            f"{below_target} listing(s) have fewer than {MIN_HIGH_RES_IMAGES} photos "
            f"(target {MIN_HIGH_RES_IMAGES}-{MAX_HIGH_RES_IMAGES} when available)"
        )
    if not parts:
        return ""
    return "Image coverage check: " + "; ".join(parts) + "."


def _materialize_brochure_assets(records, batch_dir, batch_id, name):
    """Persist classified embedded brochure visuals after batch URLs exist.

    Provider-neutral: every listing with brochure-embedded bytes (MetSpace
    Drive PDFs, Workplace Plus spreadsheet Drive packs, Union-linked
    documents, Knotel/GPE property pages that yielded embeds) gets Floor
    Plan first, then distinct non-blank property photos. Blank/near-solid
    slides and exact content duplicates are dropped here so they never
    reach gallery finalisation.

    Shared `_brochure_embedded_assets` lists (same brochure URL across
    floors) clear `content` after the first write — later floors MUST still
    assign High Res / Floor Plan from the digest→URL map, or siblings stay
    blank (confirmed Union / WP Manchester / MetSpace multi-floor).
    """
    jobs = []
    saved = {}
    saved_kind = {}  # digest -> "floorplan" | "photo"
    for record_index, record in enumerate(records, start=1):
        candidates = record.pop("_brochure_embedded_assets", None) or []
        if not candidates:
            continue
        diagnostics = record.setdefault("_link_diagnostics", [])
        # Floor plans first so a photo-heavy brochure can never leave the
        # Floor Plan cell empty merely because photo materialisation hit a
        # cap or error earlier in the same loop (confirmed gap: first
        # Workplace Plus / Drive-linked listing missing its floor plan).
        ordered = sorted(
            candidates,
            key=lambda item: 0 if item.classification == AssetType.FLOORPLAN else 1,
        )
        photo_urls = []
        seen_photo_hashes = set()
        for candidate in ordered:
            if candidate.classification not in {AssetType.PROPERTY_IMAGE, AssetType.FLOORPLAN}:
                candidate.content = None
                continue
            digest = candidate.content_hash or (
                image_content_hash(candidate.content) if candidate.content else ""
            )
            if not digest:
                candidate.content = None
                continue
            # Replay already-hosted digests for sibling floors (content wiped).
            if digest in saved and not candidate.content:
                url = saved[digest]
                kind = saved_kind.get(digest) or (
                    "floorplan" if candidate.classification == AssetType.FLOORPLAN else "photo"
                )
                if kind == "floorplan":
                    if _is_replaceable_viewer_url(str(record.get("Floor Plan") or "")):
                        record["Floor Plan"] = url
                elif digest not in seen_photo_hashes:
                    seen_photo_hashes.add(digest)
                    if url not in photo_urls:
                        photo_urls.append(url)
                continue
            if not candidate.content:
                continue
            # Floor-plan diagrams are naturally near-white — never apply the
            # blank-slide rejector to them. Property photos from the same PDF
            # (MetSpace Drive packs) do get entropy-checked so near-black
            # placeholder pages cannot become High Res Images.
            if candidate.classification == AssetType.PROPERTY_IMAGE:
                width = candidate.width or 0
                height = candidate.height or 0
                if width and height and (width < MIN_PROPERTY_IMAGE_WIDTH or height < MIN_PROPERTY_IMAGE_HEIGHT):
                    diagnostics.append(
                        LinkDiagnostic("IMAGE_TOO_SMALL", detail="Embedded brochure photo excluded before gallery materialisation.")
                    )
                    candidate.content = None
                    continue
                # Floor plans before blank reject — near-white diagrams would
                # otherwise be dropped as blank and never fill Floor Plan.
                if pdf_images.is_floorplan_image(candidate.content):
                    candidate.classification = AssetType.FLOORPLAN
                    diagnostics.append(
                        LinkDiagnostic(
                            "IMAGE_IS_FLOORPLAN",
                            detail="Floor-plan diagram moved to Floor Plan; excluded from High Res Images.",
                        )
                    )
                elif is_blank_or_empty_image(candidate.content):
                    diagnostics.append(
                        LinkDiagnostic("IMAGE_BLANK_OR_EMPTY", detail="Near-solid/blank placeholder excluded from High Res Images.")
                    )
                    candidate.content = None
                    continue
            if digest not in saved:
                extension = (candidate.extension or "png").lower().lstrip(".")
                filename = f"{name}_brochure_r{record_index}_{digest[:10]}.{extension}"
                path = batch_dir / filename
                path.write_bytes(candidate.content)
                jobs.append((f"{batch_id}/{filename}", path))
                saved[digest] = _download_url(batch_id, filename)
                saved_kind[digest] = (
                    "floorplan" if candidate.classification == AssetType.FLOORPLAN else "photo"
                )
            # Bytes are on disk now — free RSS before the next listing.
            candidate.content = None
            url = saved[digest]
            if candidate.classification == AssetType.FLOORPLAN:
                saved_kind[digest] = "floorplan"
                existing_plan = str(record.get("Floor Plan") or "")
                if _is_replaceable_viewer_url(existing_plan):
                    record["Floor Plan"] = url
                continue
            saved_kind[digest] = "photo"
            if digest in seen_photo_hashes:
                diagnostics.append(
                    LinkDiagnostic("IMAGE_DUPLICATE_CONTENT", detail="Exact duplicate embedded photo kept only once.")
                )
                continue
            seen_photo_hashes.add(digest)
            if url not in photo_urls:
                photo_urls.append(url)

        if photo_urls:
            existing_candidates = list(record.get("_high_res_candidates") or [])
            existing = str(record.get("High Res Images") or "")
            # CDN and redirect image URLs frequently have no useful extension.
            # A non-gallery value already assigned to this field is an image
            # candidate and must survive the brochure merge.
            if existing and Path(existing.split("?", 1)[0]).suffix.lower() != ".html":
                existing_candidates.insert(0, existing)
            combined = list(dict.fromkeys(existing_candidates + photo_urls))[:MAX_HIGH_RES_IMAGES]
            # Always publish merged candidates. A blank High Res with only
            # embedded brochure bytes (MetSpace Drive / UNION Box / Workplace
            # Plus spreadsheets) previously relied on `not existing` being
            # true; a single email featured photo with no _high_res_candidates
            # list also needs the embedded URLs merged in so finalize can
            # build a 5-8 image gallery.
            record["_high_res_candidates"] = combined

    _share_materialized_media_across_buildings(records)
    return jobs


def _share_materialized_media_across_buildings(records):
    """Copy High Res candidates + Floor Plan across floors of the same building.

    Generalized sibling fan-out after materialise so Union / Workplace Plus /
    Knotel / GPE multi-floor rows do not leave later units blank when an
    earlier floor already hosted brochure embeds.
    """
    by_building = defaultdict(list)
    for record in records:
        building = str(record.get("Building") or "").strip().lower()
        if building:
            by_building[building].append(record)

    def _candidate_count(record):
        cands = list(record.get("_high_res_candidates") or [])
        existing = str(record.get("High Res Images") or "").strip()
        if existing and ".html" not in existing.lower():
            cands.insert(0, existing)
        return len({normalize_url(url) for url in cands if url})

    def _usable_floor_plan(record):
        plan = str(record.get("Floor Plan") or "").strip()
        if not plan or _is_replaceable_viewer_url(plan):
            return ""
        return plan

    for group in by_building.values():
        if len(group) < 2:
            continue
        donor = max(
            group,
            key=lambda item: (_candidate_count(item), 1 if _usable_floor_plan(item) else 0),
        )
        donor_candidates = list(donor.get("_high_res_candidates") or [])
        donor_image = str(donor.get("High Res Images") or "").strip()
        if donor_image and ".html" not in donor_image.lower():
            donor_candidates.insert(0, donor_image)
        donor_plan = _usable_floor_plan(donor)
        if _candidate_count(donor) < 1 and not donor_plan:
            continue
        for record in group:
            if record is donor:
                continue
            if _candidate_count(record) < MIN_HIGH_RES_IMAGES and donor_candidates:
                existing = list(record.get("_high_res_candidates") or [])
                existing_image = str(record.get("High Res Images") or "").strip()
                if existing_image and ".html" not in existing_image.lower():
                    existing.insert(0, existing_image)
                record["_high_res_candidates"] = list(
                    dict.fromkeys(existing + donor_candidates)
                )[:MAX_HIGH_RES_IMAGES]
            sibling_plan = str(record.get("Floor Plan") or "").strip()
            if donor_plan and (not sibling_plan or _is_replaceable_viewer_url(sibling_plan)):
                record["Floor Plan"] = donor_plan


def _share_finalized_media_across_buildings(records):
    """After finalize, copy High Res galleries + Floor Plans to sibling floors.

    Materialise sharing only moves candidates. Finalize may still leave a
    later floor blank (deadline / validation order) even when an earlier
    floor of the same building already has a multi-image gallery. Provider-
    neutral fan-out closes that gap for Knotel/GPE/Union/MetSpace/WP+.
    """
    by_building = defaultdict(list)
    for record in records:
        building = str(record.get("Building") or "").strip().lower()
        if building:
            by_building[building].append(record)

    def _hr_score(record):
        hr = str(record.get("High Res Images") or "").strip()
        count = int(record.get("_high_res_image_count") or (1 if hr else 0))
        plan = str(record.get("Floor Plan") or "").strip()
        usable_plan = 1 if plan and not _is_replaceable_viewer_url(plan) else 0
        return (count, usable_plan, 1 if hr else 0)

    for group in by_building.values():
        if len(group) < 2:
            continue
        donor = max(group, key=_hr_score)
        donor_hr = str(donor.get("High Res Images") or "").strip()
        donor_count = int(donor.get("_high_res_image_count") or (1 if donor_hr else 0))
        donor_plan = str(donor.get("Floor Plan") or "").strip()
        if donor_plan and _is_replaceable_viewer_url(donor_plan):
            donor_plan = ""
        if not donor_hr and not donor_plan:
            continue
        for record in group:
            if record is donor:
                continue
            sibling_hr = str(record.get("High Res Images") or "").strip()
            sibling_count = int(record.get("_high_res_image_count") or (1 if sibling_hr else 0))
            if donor_hr and (not sibling_hr or sibling_count < donor_count):
                record["High Res Images"] = donor_hr
                record["_high_res_image_count"] = donor_count
                record.setdefault("_link_diagnostics", []).append(
                    LinkDiagnostic(
                        "IMAGES_SHARED_FROM_SIBLING_FLOOR",
                        detail="Copied finalized High Res Images from another floor of the same building.",
                    )
                )
            sibling_plan = str(record.get("Floor Plan") or "").strip()
            if donor_plan and (not sibling_plan or _is_replaceable_viewer_url(sibling_plan)):
                record["Floor Plan"] = donor_plan
                record.setdefault("_link_diagnostics", []).append(
                    LinkDiagnostic(
                        "FLOORPLAN_SHARED_FROM_SIBLING_FLOOR",
                        detail="Copied Floor Plan from another floor of the same building.",
                    )
                )


def _upload_all(jobs):
    """Mirror batch files to durable object storage.

    Called synchronously before /api/process returns so High Res gallery
    links do not 404 when ephemeral local disk is gone. Best-effort per
    job — one failing upload (logged inside storage.upload) doesn't stop
    the rest.
    """
    for key, path in jobs:
        storage.upload(key, path)


def _flush_caches():
    """Runs in a background thread (see process() above), mirroring the
    geocode/address-lookup on-disk caches to B2/S3 once per batch — used
    to run synchronously inside extraction.pipeline.process_files itself.
    Confirmed via Render's own logs that a worker was once killed while
    stuck inside exactly that call; a generic SIGKILL gets reported as
    "Perhaps out of memory?" regardless of whether the real cause was
    memory or a slow/hanging network call, so this had been silently
    contributing to that same symptom. Each flush_to_storage is already a
    no-op if nothing was cached this run or storage isn't configured."""
    address_lookup.flush_to_storage()
    geocode_module.flush_to_storage()


@app.route("/api/download/<batch_id>/<path:filename>")
def download(batch_id, filename):
    safe_batch = Path(batch_id).name
    safe_name = Path(filename).name  # strip any path components
    file_path = (OUTPUT_DIR / safe_batch / safe_name).resolve()
    ext = Path(safe_name).suffix.lower()
    mimetype = CONTENT_TYPES.get(ext, "application/octet-stream")
    disposition = "inline" if ext in INLINE_EXTENSIONS else "attachment"

    if OUTPUT_DIR.resolve() in file_path.parents and file_path.exists():
        # Fast path: still on local disk (recent batch, same instance
        # that generated it).
        response = send_file(file_path, mimetype=mimetype, as_attachment=(disposition == "attachment"), download_name=safe_name)
    else:
        # Local copy is gone — either Render redeployed/restarted since
        # (wiping its ephemeral disk) or our own hourly cleanup ran.
        # Fall back to object storage, which isn't tied to this instance's
        # disk at all (storage.fetch returns None if unconfigured or the
        # object genuinely doesn't exist there either).
        data = storage.fetch(f"{safe_batch}/{safe_name}")
        if data is None:
            return jsonify({"error": "File not found"}), 404
        response = Response(data, mimetype=mimetype)

    # Set this explicitly (quoted) rather than trusting send_file's default
    # formatting alone, so the header is deterministic regardless of
    # Werkzeug version quirks — this is the header a browser actually reads
    # to recognize a completed download's real filename/extension, and
    # inline vs. attachment decides whether it opens in-browser or downloads.
    response.headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    return response


def _cleanup_old_batches():
    """Each batch gets its own output subfolder so concurrent users never
    clobber each other's files (the previous version wiped one shared
    output/ dir on every run). This just prevents unbounded buildup on
    long-running instances — Render's disk is ephemeral anyway and resets
    on every deploy/restart."""
    if not OUTPUT_DIR.exists():
        return
    cutoff = time.time() - BATCH_MAX_AGE_SECONDS
    for child in OUTPUT_DIR.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
