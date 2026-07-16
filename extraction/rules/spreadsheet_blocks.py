"""Generic parser for spreadsheets made of repeated property blocks.

Recognises structure rather than a provider: an address/postcode row, a nearby
semantic header row, one or more availability rows, and optional hyperlinks
inside that same bounded block. Links never cross a block boundary.
"""
import re
from datetime import datetime

_HEADER_ALIASES = {
    "Floor/Unit": (("unit", "floor"), ("floor",), ("unit",)),
    "Size (sq ft)": (("sq", "ft"), ("sqft",), ("size",)),
    "Desks (max)": (("desk",), ("desks",)),
    "Min. Term": (("term",),),
    "Marketing Price (Based on Min Term) PCM": (("per", "month"), ("pcm",), ("monthly",)),
}
_MIN_HEADER_MATCHES = 4
_MAX_ADDRESS_DISTANCE = 5
_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)


def detect(content):
    return bool(find_property_blocks(content))


def parse(content):
    blocks = find_property_blocks(content)
    contacts = _default_contacts(content)
    records = []
    for block in blocks:
        links = _links_for_block(content.get("row_links") or [], block)
        for source_row, row in block["availability_rows"]:
            desks_raw = _cell(row, block["columns"].get("Desks (max)"))
            record = {
                "Area": block["area"] or _area_from_sheet(block.get("sheet_name") or ""),
                "Building": block["building"],
                "Floor/Unit": _cell(row, block["columns"].get("Floor/Unit")),
                "Size (sq ft)": _number(_cell(row, block["columns"].get("Size (sq ft)"))),
                "Desks (max)": _desks(desks_raw),
                # Workplace Plus Manchester (etc.): desk cell holds
                # "30 + 3 MR + Collab" — extras belong in Special Features.
                "Special Features": _desks_features(desks_raw),
                "Min. Term": _cell(row, block["columns"].get("Min. Term")),
                "Marketing Price (Based on Min Term) PCM": _number(
                    _cell(row, block["columns"].get("Marketing Price (Based on Min Term) PCM"))
                ),
                "Contacts": contacts,
                "_spreadsheet_block": {
                    "sheet": block["sheet_name"],
                    "address_row": block["address_row"],
                    "header_row": block["header_row"],
                    "source_row": source_row,
                    "association": "same_property_block",
                },
            }
            for text, url in links:
                label = str(text or "").lower()
                if "floor" in label and ("plan" in label or "layout" in label):
                    record.setdefault("Floor Plan", url)
                else:
                    record.setdefault("Brochure PDF", url)
            records.append(record)
    return records


def _default_contacts(content):
    """File/sheet-level contact when the grid has no per-row agent column."""
    blob = " ".join(
        [
            content.get("filename") or "",
            content.get("source_file_name") or "",
            " ".join(content.get("sheet_names") or []),
            (content.get("text") or "")[:500],
        ]
    ).lower()
    if "workplace plus" in blob or "workplaceplus" in blob:
        return "Workplace Plus, hello@workplaceplus.co.uk"
    return ""


def _area_from_sheet(sheet_name):
    text = str(sheet_name or "").strip()
    if text and text.lower() not in {"sheet1", "sheet 1"}:
        return text.title() if text.isupper() else text
    return ""


def _desks_features(value):
    """Keep the descriptive desk package text (MR / Collab / BR) as features."""
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text or re.fullmatch(r"\d+", text):
        return ""
    # Pure numeric ranges are not features.
    if re.fullmatch(r"\d+\s*[-–]\s*\d+", text):
        return ""
    return text


def find_property_blocks(content):
    blocks = []
    row_links = content.get("row_links") or []
    sheet_names = list(content.get("sheet_names") or [])
    if not sheet_names:
        for item in row_links:
            name = item.get("sheet_name")
            if name and name not in sheet_names:
                sheet_names.append(name)
    for table_index, table in enumerate(content.get("tables") or []):
        if len(table) < 3:
            continue
        sheet_name = sheet_names[table_index] if table_index < len(sheet_names) else f"Sheet {table_index + 1}"
        candidates = []
        for index, row in enumerate(table):
            columns = _header_columns(row)
            if len(columns) < _MIN_HEADER_MATCHES or "Floor/Unit" not in columns or "Size (sq ft)" not in columns:
                continue
            address_index = _find_address_row(table, index)
            if address_index is None:
                continue
            building, area = _address_and_area(table[address_index])
            if not building:
                continue
            candidates.append((address_index, index, columns, building, area))
        for position, (address_index, header_index, columns, building, area) in enumerate(candidates):
            next_address = candidates[position + 1][0] if position + 1 < len(candidates) else len(table)
            availability = []
            for row_index in range(header_index + 1, next_address):
                row = table[row_index]
                floor = _cell(row, columns.get("Floor/Unit"))
                size = _number(_cell(row, columns.get("Size (sq ft)")))
                if floor and size not in (None, "") and len(_header_columns(row)) < _MIN_HEADER_MATCHES:
                    availability.append((row_index + 1, row))
            if availability:
                blocks.append(
                    {
                        "sheet_name": sheet_name,
                        "table_index": table_index,
                        "address_index": address_index,
                        "header_index": header_index,
                        "address_row": address_index + 1,
                        "header_row": header_index + 1,
                        "end_row": next_address,
                        "columns": columns,
                        "building": building,
                        "area": area,
                        "availability_rows": availability,
                    }
                )
    return blocks


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


def _find_address_row(table, header_index):
    for distance in range(1, _MAX_ADDRESS_DISTANCE + 1):
        index = header_index - distance
        if index < 0:
            break
        if any(_POSTCODE_RE.search(str(value or "")) for value in table[index]):
            return index
    return None


def _address_and_area(row):
    building_index = next(
        (index for index, value in enumerate(row) if _POSTCODE_RE.search(str(value or ""))),
        None,
    )
    if building_index is None:
        return "", ""
    building = str(row[building_index]).strip()
    area = next(
        (
            str(value).strip()
            for value in row[building_index + 1 :]
            if value not in (None, "") and not _POSTCODE_RE.search(str(value))
        ),
        "",
    )
    return building, area


def _links_for_block(row_links, block):
    found = []
    for item in row_links:
        sheet = item.get("sheet_name")
        row_number = item.get("row_number")
        same_sheet = not sheet or sheet == block["sheet_name"]
        in_rows = isinstance(row_number, int) and block["address_row"] <= row_number <= block["end_row"]
        legacy_match = row_number is None and block["building"].lower() in str(item.get("row_text") or "").lower()
        if not same_sheet or not (in_rows or legacy_match):
            continue
        for text, url in item.get("links") or []:
            if url and (text, url) not in found:
                found.append((text, url))
    return found


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else value


def _number(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"-?[\d,]+(?:\.\d+)?", str(value))
    if not match:
        return ""
    number = match.group(0).replace(",", "")
    return float(number) if "." in number else int(number)


def _desks(value):
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return max(value.day, value.month)
    text = str(value).strip()
    date_match = re.match(r"\d{4}-(\d{2})-(\d{2})(?:\s|$)", text)
    if date_match:
        return max(int(date_match.group(1)), int(date_match.group(2)))
    range_match = re.match(r"\s*(\d+)\s*[-?]\s*(\d+)", text)
    if range_match:
        return int(range_match.group(2))
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else ""
