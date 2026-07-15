from extraction.rules import spreadsheet_blocks
from extraction.spreadsheet_chunks import (
    MAX_LLM_CHUNK_CHARS,
    MAX_LLM_CHUNK_ROWS,
    build_row_chunks,
    extract_in_chunks,
    is_large_spreadsheet,
)


def _block(address, floors, link=None, start_row=1):
    table = [
        [address, "Mayfair", "", "", ""],
        ["Unit/Floor", "Sq Ft", "Desks", "Term", "Per Month"],
    ]
    table.extend(floors)
    row_links = []
    if link:
        row_links.append({
            "sheet_name": "Sheet 1", "row_number": start_row + len(table) - 1,
            "row_text": "Brochure", "links": [("Brochure", link)],
        })
    return table, row_links


def test_repeated_blocks_parse_multiple_floors_and_keep_links_local():
    first, links1 = _block("1 Alpha Street, London W1A 1AA", [["1st", 1000, 10, "3 years", 5000], ["2nd", 1100, 12, "3 years", 5500]], "https://example.com/a")
    second, links2 = _block("2 Beta Street, London W1B 2BB", [["Ground", 900, 8, "2 years", 4000]], "https://example.com/b", len(first) + 1)
    content = {"tables": [first + second], "row_links": links1 + links2}
    blocks = spreadsheet_blocks.find_property_blocks(content)
    records = spreadsheet_blocks.parse(content)
    assert len(blocks) == 2
    assert len(records) == 3
    assert [r["Floor/Unit"] for r in records] == ["1st", "2nd", "Ground"]
    assert records[0]["Brochure PDF"] == "https://example.com/a"
    assert records[1]["Brochure PDF"] == "https://example.com/a"
    assert records[2]["Brochure PDF"] == "https://example.com/b"


def test_unknown_large_sheet_is_split_into_bounded_calls():
    content = {"tables": [[[f"row {i}", "x" * 100] for i in range(100)]]}
    chunks = build_row_chunks(content)
    assert len(chunks) > 1
    assert all(c["end_row"] - c["start_row"] + 1 <= MAX_LLM_CHUNK_ROWS for c in chunks)
    assert all(len(c["text"]) <= MAX_LLM_CHUNK_CHARS for c in chunks)


def test_failed_chunk_preserves_successful_chunks_and_deduplicates_overlap():
    content = {"tables": [[[f"row {i}"] for i in range(70)]]}
    calls = []

    def fake(text, **kwargs):
        calls.append((text, kwargs))
        if len(calls) == 2:
            raise ValueError("malformed JSON")
        record = {"Building": f"Building {len(calls)}", "Floor/Unit": "1st", "Size (sq ft)": "100"}
        return [record], "Test", {"prompt_chars": len(text) + 100, "response_chars": 200}

    records, source, diagnostics = extract_in_chunks(content, "large.xlsx", extractor=fake)
    assert records
    assert source == "Test"
    assert diagnostics["failed_chunks"]
    assert diagnostics["successful_chunks"] == len(calls) - 1
    assert all(kwargs["retry_malformed"] is True for _, kwargs in calls)
    assert all(kwargs["max_output_tokens"] <= 8000 for _, kwargs in calls)


def test_deterministic_large_sheet_needs_no_llm():
    table = []
    links = []
    row = 1
    for i in range(30):
        block, block_links = _block(f"{i + 1} Example Street, London W1A 1AA", [["1st", 1000 + i, 10, "3 years", 5000]], f"https://example.com/{i}", row)
        table.extend(block)
        links.extend(block_links)
        row += len(block)
    content = {"tables": [table], "row_links": links}
    assert spreadsheet_blocks.detect(content)
    assert len(spreadsheet_blocks.parse(content)) == 30

def test_small_spreadsheet_stays_below_large_file_threshold():
    content = {"tables": [[["Building", "Floor"], ["1 Small Street", "1st"]]]}
    assert not is_large_spreadsheet(content)
    assert len(build_row_chunks(content)) == 1


def test_source_row_context_is_retained_per_record():
    table, links = _block("10 Context Street, London W1A 1AA", [["Ground", 750, 6, "2 years", 3000]], "https://example.com/context")
    record = spreadsheet_blocks.parse({"tables": [table], "row_links": links})[0]
    context = record["_spreadsheet_block"]
    assert context == {"sheet": "Sheet 1", "address_row": 1, "header_row": 2, "source_row": 3, "association": "same_property_block"}


def test_overlap_records_are_deduplicated():
    content = {"tables": [[[f"row {i}"] for i in range(70)]]}

    def fake(text, **kwargs):
        return [{"Building": "Same", "Floor/Unit": "1st", "Size (sq ft)": "100"}], "Test", {"prompt_chars": len(text), "response_chars": 100}

    records, _, diagnostics = extract_in_chunks(content, extractor=fake)
    assert len(records) == 1
    assert diagnostics["successful_chunks"] > 1


def test_all_malformed_chunks_raise_clear_file_error():
    content = {"tables": [[[f"row {i}"] for i in range(90)]]}

    def malformed(text, **kwargs):
        raise ValueError("unterminated JSON")

    try:
        extract_in_chunks(content, extractor=malformed)
    except Exception as exc:
        assert "All bounded spreadsheet chunks failed" in str(exc)
        assert "unterminated JSON" in str(exc)
    else:
        raise AssertionError("all failed chunks must fail the file")


def test_excessive_chunk_record_count_is_isolated_as_failure():
    content = {"tables": [[[f"row {i}"] for i in range(70)]]}
    calls = 0

    def fake(text, **kwargs):
        nonlocal calls
        calls += 1
        count = 26 if calls == 1 else 1
        records = [{"Building": f"B{calls}-{i}", "Floor/Unit": "1st", "Size (sq ft)": "100"} for i in range(count)]
        return records, "Test", {"prompt_chars": len(text), "response_chars": 500}

    records, _, diagnostics = extract_in_chunks(content, extractor=fake)
    assert records
    assert diagnostics["failed_chunks"][0]["chunk"] == 1
    assert "safe 25-record limit" in diagnostics["failed_chunks"][0]["error"]


def test_chunk_diagnostics_track_largest_prompt_and_response():
    content = {"tables": [[[f"row {i}"] for i in range(70)]]}
    sizes = []

    def fake(text, **kwargs):
        response_size = len(sizes) * 100 + 50
        sizes.append((len(text) + 20, response_size))
        return [{"Building": f"B{len(sizes)}", "Floor/Unit": "1st", "Size (sq ft)": "100"}], "Test", {"prompt_chars": sizes[-1][0], "response_chars": sizes[-1][1]}

    _, _, diagnostics = extract_in_chunks(content, extractor=fake)
    assert diagnostics["largest_prompt_chars"] == max(x[0] for x in sizes)
    assert diagnostics["largest_response_chars"] == max(x[1] for x in sizes)