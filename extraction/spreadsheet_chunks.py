"""Bounded LLM fallback for spreadsheets without a deterministic rule."""
from .llm_fallback import LLMExtractionError, extract_with_llm

LARGE_SPREADSHEET_ROWS = 80
MAX_LLM_CHUNK_ROWS = 35
MAX_LLM_CHUNK_CHARS = 12000
MAX_EXPECTED_RECORDS = 25
CHUNK_OVERLAP_ROWS = 2
CHUNK_OUTPUT_TOKENS = 8000


def is_spreadsheet(content):
    return bool(content.get("tables"))


def is_large_spreadsheet(content):
    return sum(len(table) for table in content.get("tables") or []) > LARGE_SPREADSHEET_ROWS


def _row_text(row):
    return " | ".join(str(value) for value in row if value not in (None, ""))


def build_row_chunks(content):
    """Build bounded chunks with two-row overlap; never exceed row/char caps."""
    chunks = []
    for table_index, table in enumerate(content.get("tables") or []):
        start = 0
        while start < len(table):
            lines = []
            end = start
            while end < len(table) and end - start < MAX_LLM_CHUNK_ROWS:
                line = f"Row {end + 1}: {_row_text(table[end])}"
                if lines and len("\n".join(lines + [line])) > MAX_LLM_CHUNK_CHARS:
                    break
                lines.append(line)
                end += 1
            if not lines:
                lines = [f"Row {start + 1}: {_row_text(table[start])}"[:MAX_LLM_CHUNK_CHARS]]
                end = start + 1
            chunks.append({"table": table_index, "start_row": start + 1, "end_row": end, "text": "\n".join(lines)})
            if end >= len(table):
                break
            start = max(start + 1, end - CHUNK_OVERLAP_ROWS)
    return chunks


def _dedupe(records):
    seen = set()
    output = []
    for record in records:
        key = tuple(str(record.get(field, "")).strip().lower() for field in (
            "Building", "Floor/Unit", "Size (sq ft)", "Marketing Price (Based on Min Term) PCM"
        ))
        if key not in seen:
            seen.add(key)
            output.append(record)
    return output


def extract_in_chunks(content, source_hint="", extractor=extract_with_llm):
    chunks = build_row_chunks(content)
    diagnostics = {
        "large_file_chunked": is_large_spreadsheet(content),
        "chunks": len(chunks), "successful_chunks": 0, "failed_chunks": [],
        "largest_prompt_chars": 0, "largest_response_chars": 0,
    }
    records = []
    source_name = ""
    for index, chunk in enumerate(chunks, start=1):
        try:
            chunk_records, chunk_source, metrics = extractor(
                chunk["text"], source_hint=f"{source_hint} [chunk {index}/{len(chunks)}]",
                max_output_tokens=CHUNK_OUTPUT_TOKENS, retry_malformed=True, include_metrics=True,
            )
            if len(chunk_records) > MAX_EXPECTED_RECORDS:
                raise LLMExtractionError(
                    f"Chunk {index} returned {len(chunk_records)} records, above the safe {MAX_EXPECTED_RECORDS}-record limit"
                )
            records.extend(chunk_records)
            source_name = source_name or chunk_source
            diagnostics["successful_chunks"] += 1
            diagnostics["largest_prompt_chars"] = max(diagnostics["largest_prompt_chars"], metrics.get("prompt_chars", 0))
            diagnostics["largest_response_chars"] = max(diagnostics["largest_response_chars"], metrics.get("response_chars", 0))
        except Exception as exc:
            diagnostics["failed_chunks"].append({"chunk": index, "rows": [chunk["start_row"], chunk["end_row"]], "error": str(exc)})
    if not records:
        details = diagnostics["failed_chunks"][0]["error"] if diagnostics["failed_chunks"] else "no usable records"
        raise LLMExtractionError(f"All bounded spreadsheet chunks failed: {details}")
    return _dedupe(records), source_name, diagnostics