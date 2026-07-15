"""Rule-based parser for UNION availability spreadsheets.

UNION sheets are a plain availability grid (header mid-sheet, one row per
floor) with Brochure cells labeled "CLICK HERE" whose real destinations are
Box shared links — invisible to pandas' value-only read and previously only
recoverable after an LLM parse + xlsx_links enrichment. Without GEMINI_API_KEY
that path fails entirely ("doesn't process") and High Res / Brochure stay
blank even though the hyperlinks were always present.

Layout (per sheet — City, Shoreditch, …):
    intro / instruction rows
    | City | Floor | Current Spec | Size sq.ft | Minimum Term | Monthly Rate | Price p/sq.ft | Brochure |
    | <building name> | 3rd | Fitted | 1466 | 2 Years | 20157.5 | 165 | CLICK HERE |

Column "City" holds the building name (UNION's own header wording); the sheet
name / Area column value is the sub-market, not a UK city name.
"""
import re

from extraction.html_images import is_floorplan_link
from extraction.xlsx_links import _best_brochure_candidate, _normalize_for_matching

_HEADER_ALIASES = {
    "Building": (("city",), ("building",), ("address",), ("property",)),
    "Floor/Unit": (("floor",), ("unit",)),
    "Special Features": (("current", "spec"), ("spec",), ("fit", "out")),
    "Size (sq ft)": (("size",), ("sq", "ft"), ("sqft",)),
    "Min. Term": (("minimum", "term"), ("min", "term"), ("term",)),
    "Marketing Price (Based on Min Term) PCM": (("monthly", "rate"), ("pcm",), ("per", "month")),
    "Marketing Price (Based on Min Term) PSF": (("price", "sq"), ("p", "sq"), ("psf",)),
    "Brochure PDF": (("brochure",),),
}
_MIN_HEADER_MATCHES = 4
# Confirmed real UNION intro blurbs misspell "availability" as "avaiability".
_UNION_HINT_RE = re.compile(
    r"\bunion\b|sub-market\s+avai?ability|short form all inclusive lease",
    re.I,
)


def detect(content):
    blob = " ".join(
        [
            content.get("text") or "",
            " ".join(content.get("sheet_names") or []),
            content.get("filename") or "",
            content.get("source_file_name") or "",
        ]
    )
    if not _UNION_HINT_RE.search(blob):
        return False
    return bool(_find_header_tables(content.get("tables") or []))


def parse(content):
    records = []
    sheet_names = list(content.get("sheet_names") or [])
    for table_index, header_index, columns, table in _find_header_tables(content.get("tables") or []):
        sheet_name = sheet_names[table_index] if table_index < len(sheet_names) else ""
        area = sheet_name
        for row in table[header_index + 1 :]:
            building = _cell(row, columns.get("Building"))
            floor = _cell(row, columns.get("Floor/Unit"))
            size = _cell(row, columns.get("Size (sq ft)"))
            if not building or not (floor or size):
                continue
            if _looks_like_header(row):
                continue
            record = {
                "Area": area,
                "Building": building,
                "Floor/Unit": floor,
                "Size (sq ft)": size,
                "Desks (max)": "",
                "Min. Term": _cell(row, columns.get("Min. Term")),
                "Marketing Price (Based on Min Term) PCM": _cell(
                    row, columns.get("Marketing Price (Based on Min Term) PCM")
                ),
                "Marketing Price (Based on Min Term) PSF": _cell(
                    row, columns.get("Marketing Price (Based on Min Term) PSF")
                ),
                "Special Features": _cell(row, columns.get("Special Features")),
                "Contacts": "UNION",
            }
            records.append(record)
    _attach_row_links(records, content.get("row_links") or [])
    return records


def _find_header_tables(tables):
    """Yield (table_index, header_index, column_map, table) for UNION-like sheets."""
    found = []
    for table_index, table in enumerate(tables):
        for index, row in enumerate(table[:20]):
            columns = _header_columns(row)
            if len(columns) < _MIN_HEADER_MATCHES:
                continue
            if "Floor/Unit" not in columns or "Size (sq ft)" not in columns:
                continue
            if "Building" not in columns and "Brochure PDF" not in columns:
                continue
            found.append((table_index, index, columns, table))
            break
    return found


def _header_columns(row):
    result = {}
    for index, raw in enumerate(row):
        text = re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).strip()
        if not text:
            continue
        for target, alternatives in _HEADER_ALIASES.items():
            if target in result:
                continue
            if any(all(word in text.split() for word in words) for words in alternatives):
                result[target] = index
                break
    return result


def _looks_like_header(row):
    joined = " ".join(str(cell or "").lower() for cell in row)
    return "floor" in joined and ("size" in joined or "brochure" in joined) and "sq" in joined


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    value = row[index]
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "click here"} else text


def _attach_row_links(records, row_links):
    """Recover Box (etc.) hyperlinks hidden behind CLICK HERE display text."""
    if not records or not row_links:
        return
    available = [
        {"row_text": _normalize_for_matching(row["row_text"]), "links": row["links"]}
        for row in row_links
    ]
    for record in records:
        building = (record.get("Building") or "").strip()
        if not building:
            continue
        building_l = _normalize_for_matching(building)
        floor_l = _normalize_for_matching(record.get("Floor/Unit") or "")
        match_idx = None
        for i, row in enumerate(available):
            if building_l not in row["row_text"]:
                continue
            if floor_l and floor_l not in row["row_text"]:
                continue
            match_idx = i
            break
        if match_idx is None:
            match_idx = next(
                (i for i, row in enumerate(available) if building_l in row["row_text"]),
                None,
            )
        if match_idx is None:
            continue
        row = available.pop(match_idx)
        floorplan_url = None
        brochure_candidates = []
        for display_text, url in row["links"]:
            if is_floorplan_link(display_text):
                if floorplan_url is None:
                    floorplan_url = url
            else:
                brochure_candidates.append(url)
        brochure_url = _best_brochure_candidate(brochure_candidates)
        if floorplan_url and not record.get("Floor Plan"):
            record["Floor Plan"] = floorplan_url
        if brochure_url and not record.get("Brochure PDF"):
            record["Brochure PDF"] = brochure_url
