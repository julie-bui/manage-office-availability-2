"""Rule-based parser for BC's own "Current Availability" summary table — a
single-page table listing every currently available floor/desk-cluster
across all of BC's buildings (Building Name/Floor/Size (Sq ft)/Num of
Desks/Market Price/Sale Price/Available/Includes).

Distinct from BC's OTHER PDF format — a single-listing marketing brochure
per building (e.g. "2nd Floor - 2-7 Clerkenwell Green Brochure.pdf") — that
one has no table at all and still goes through the LLM fallback (with its
Floor Plan/High Res Images filled in separately by app.py's
extraction.pdf_images enrichment); this rule only ever matches the tabular
one.

Confirmed empirically that this table's own header row is NOT row 0 of the
extracted table — pdfplumber returns a leading blank spacer row first (an
artifact of this PDF's own layout) — so, unlike extraction.rules.grid
(which assumes the header is always row 0), this scans every row for the
first one that looks like a real header.

Detection deliberately does NOT use the same loose "N of these generic
keywords" approach as extraction.rules.grid: confirmed empirically that
doing so (an earlier version of this file) matched Kitt's own table too —
Kitt's own header already uses near-schema wording ("Building", "Floor/
Unit", "Size (sq ft)", "Desks (max)", "Marketing Price...PCM"), so generic
single-word keywords like "floor"/"size"/"desk" match both formats, and
"market" as a bare keyword even matches inside Kitt's "Marketing Price".
detect() instead requires BC's own genuinely distinctive column combination
— "Num of Desks" and "Sale Price" together, neither of which appears in
Kitt's or any other known source's header — so it can't fire on a
different, differently-labeled tabular source.

Carries over two behaviors that were previously specific to BC's own
LLM-fallback extraction (before this table got its own rule), so the
switch to direct parsing doesn't regress either one:

- For Sale / Sale Price: the LLM prompt used to tell the model to read
  this table's own real "Sale Price!" column value (blank/"N/A" vs a
  genuine price) rather than ever inferring a sale price from the rental
  "Market Price" column. Reading it directly here is strictly more
  reliable than that ever was — this is BC's real, printed column value,
  not a value an LLM had to correctly notice and transcribe. Untouched:
  schema.normalize_record's own "For Sale" derivation (real value -> Yes,
  ""/"N/A"/etc -> No) already does the right thing with whatever raw
  string ends up in the "Sale Price" key below, same as it always has.

- No Contacts, Floor Plan, or High Res Images keyword mapped: confirmed
  by direct inspection that this table genuinely has no contact/agent
  column and no images at all (a single flat text table, 0 embedded
  images) — there is no company/agency name to fall back to here the way
  the LLM prompt's Contacts rule does for other sources (e.g. Crown
  Estate's "sole agents" line) when no individual is named, because BC's
  own table has no contact signal of any kind to fall back to. If a
  future version of this table DOES add a contacts column, map it here
  directly (using the company/agency name when no individual person is
  given, not left blank) rather than leaving this rule blind to it.
  Floor Plan/High Res Images are deliberately never sourced from this
  table's own text either way — real links only ever come from app.py's
  extraction.pdf_images enrichment (see PDF_IMAGE_ENRICHED_METHODS),
  which is exactly the fix already in place for the LLM fallback after a
  real BC brochure once had the model copy a plain "Example Floorplan"
  heading into Floor Plan as an unclickable placeholder."""

KEYWORDS = {
    "Building": [["building"]],
    "Floor/Unit": [["floor"]],
    "Size (sq ft)": [["size"]],
    "Desks (max)": [["desk"]],
    # An exact phrase, not ["market", "price"] as two independent words —
    # the latter also matches "Marketing Price...", Kitt's own column name.
    "Marketing Price (Based on Min Term) PCM": [["market price"]],
    # Feeds schema.normalize_record's "For Sale" derivation directly from
    # this table's own real value — "Yes" only when it's a genuine price,
    # "No" for "N/A"/blank — never inferred from the rental price. See
    # the module docstring for why this replaces the old LLM-prompt rule.
    "Sale Price": [["sale price"]],
    "State of Space": [["available"]],
    "Special Features": [["includes"]],
}

# The specific combination that identifies BC's table and nothing else
# seen so far — "desk" and "price" alone are too generic on their own
# (see module docstring), but no other known source pairs "num of desks"
# with a distinct "sale price" column.
_REQUIRED_SIGNALS = ["num of desks", "sale price"]


def detect(content):
    return bool(_find_tables(content))


def parse(content):
    found_tables = _find_tables(content)
    if not found_tables:
        return []

    records = []
    for header_idx, col_map, table in found_tables:
        for row in table[header_idx + 1 :]:
            if not any((c or "").strip() for c in row):
                continue
            record = {}
            for idx, col_name in col_map.items():
                if idx < len(row):
                    record[col_name] = (row[idx] or "").strip()
            if record.get("Building"):
                records.append(record)
    return records


def _find_tables(content):
    """Every table in the source that has BC's own distinctive header row
    somewhere in it — not just the first. Same class of gap as
    extraction.rules.grid's own multi-table fix (2026-07): a long table
    can get split across multiple page-tables by pdfplumber, each with
    its own copy of the header row, and returning only the first would
    silently drop every row on any table after it. Only one table exists
    in the real BC fixture this rule was built for (confirmed directly),
    so this hasn't been observed to actually drop a real BC row yet —
    fixed defensively anyway, since the "never more than one matching
    table" assumption was never actually verified and grid.py's own
    identical assumption turned out to be wrong for a real source (Kitt's
    own PDF: 38 of 57 real rows were silently dropped before that fix)."""
    found = []
    for table in content.get("tables", []):
        for i, row in enumerate(table):
            lowered_cells = [(c or "").lower() for c in row]
            if all(any(signal in cell for cell in lowered_cells) for signal in _REQUIRED_SIGNALS):
                found.append((i, _map_columns(row), table))
                break  # this table's header is found — move to the next table
    return found


def _map_columns(header):
    mapping = {}
    used_targets = set()
    for idx, cell in enumerate(header):
        cell_l = (cell or "").lower()
        for target, keyword_groups in KEYWORDS.items():
            if target in used_targets:
                continue
            if any(all(kw in cell_l for kw in group) for group in keyword_groups):
                mapping[idx] = target
                used_targets.add(target)
                break
    return mapping
