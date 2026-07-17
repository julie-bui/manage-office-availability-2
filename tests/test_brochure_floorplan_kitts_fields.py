"""Provider-neutral brochure Floor Plan depth/pixel gates + Kitts SoS tags."""
from io import BytesIO

from PIL import Image

from extraction.brochure import (
    _PLAN_ONLY_MAX_PAGES,
    _LIGHT_MAX_PAGES,
    extract_brochure,
)
from extraction.models import AssetType
from extraction.pdf_images import is_floorplan_image
from extraction.text_utils import clean_state_of_space, extract_state_of_space_status


def _photo_jpeg(seed=0, size=(640, 400)):
    image = Image.new("RGB", size, (30 + seed, 40, 50))
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = ((x + seed) % 256, (y * 2) % 256, (x + y) % 256)
    out = BytesIO()
    image.save(out, "JPEG")
    return out.getvalue()


def _floorplan_png(size=(800, 600)):
    """Near-white CAD-like diagram that passes is_floorplan_image."""
    image = Image.new("RGB", size, (250, 250, 250))
    pixels = image.load()
    for x in range(0, size[0], 40):
        for y in range(size[1]):
            pixels[x, y] = (20, 20, 20)
    for y in range(0, size[1], 40):
        for x in range(size[0]):
            pixels[x, y] = (20, 20, 20)
    out = BytesIO()
    image.save(out, "PNG")
    data = out.getvalue()
    assert is_floorplan_image(data)
    return data


def _pdf_with_pages(pages):
    """Build a minimal PDF: each page is (text, optional image_bytes)."""
    import fitz

    doc = fitz.open()
    for text, image_bytes in pages:
        page = doc.new_page(width=600, height=800)
        if text:
            page.insert_text((50, 50), text, fontsize=12)
        if image_bytes:
            page.insert_image(fitz.Rect(40, 80, 560, 700), stream=image_bytes)
    data = doc.tobytes()
    doc.close()
    return data


def test_light_extract_reads_state_of_space_beyond_visual_page_window():
    # Visual scan limited to 2 pages; status text lives on page 4.
    pages = [
        ("Cover marketing photo page", _photo_jpeg(1)),
        ("Amenities overview page", _photo_jpeg(2)),
        ("More marketing", _photo_jpeg(3)),
        ("Availability: Fully fitted floor ready now", None),
    ]
    payload = _pdf_with_pages(pages)
    result = extract_brochure(
        payload,
        "application/pdf",
        "https://example.test/brochure.pdf",
        max_photos=0,
        stop_after_floorplans=1,
        prefer_photos=False,
        max_pages=2,
    )
    assert extract_state_of_space_status(result.identity_text) == "Fully Fitted"
    assert result.fields.get("State of Space")
    assert result.fields["State of Space"].value == "Fully Fitted"


def test_capped_floorplan_extract_skips_text_only_banner_keeps_pixel_plan():
    # Page 1 says "Floor plan" but embeds a colourful photo; real CAD is page 2.
    pages = [
        ("Floor plan coming soon — see brochure", _photo_jpeg(5, size=(900, 500))),
        ("Level 2 layout", _floorplan_png()),
    ]
    payload = _pdf_with_pages(pages)
    result = extract_brochure(
        payload,
        "application/pdf",
        "https://cdn.example.test/landlord-brochure.pdf",
        max_photos=0,
        stop_after_floorplans=1,
        prefer_photos=False,
        max_pages=5,
    )
    plans = [a for a in result.assets if a.classification == AssetType.FLOORPLAN and a.content]
    assert len(plans) == 1
    assert is_floorplan_image(plans[0].content)


def test_plan_only_extract_scans_deeper_than_photo_light_window():
    """Nested/missing-plan light extracts must reach CAD pages past _LIGHT_MAX_PAGES."""
    assert _PLAN_ONLY_MAX_PAGES > _LIGHT_MAX_PAGES
    pages = []
    for i in range(_LIGHT_MAX_PAGES + 2):
        pages.append((f"Marketing page {i + 1}", _photo_jpeg(i + 1)))
    # Real CAD after the shallow photo window (page index == _LIGHT_MAX_PAGES + 2).
    pages.append(("Floor plan Level 3", _floorplan_png()))
    payload = _pdf_with_pages(pages)
    shallow = extract_brochure(
        payload,
        "application/pdf",
        "https://drive.example.test/shared/brochure.pdf",
        max_photos=0,
        stop_after_floorplans=1,
        prefer_photos=False,
        max_pages=_LIGHT_MAX_PAGES,
    )
    assert not [
        a for a in shallow.assets if a.classification == AssetType.FLOORPLAN and a.content
    ]
    deep = extract_brochure(
        payload,
        "application/pdf",
        "https://drive.example.test/shared/brochure.pdf",
        max_photos=0,
        stop_after_floorplans=1,
        prefer_photos=False,
        max_pages=max(_LIGHT_MAX_PAGES, _PLAN_ONLY_MAX_PAGES),
    )
    plans = [a for a in deep.assets if a.classification == AssetType.FLOORPLAN and a.content]
    assert len(plans) == 1
    assert is_floorplan_image(plans[0].content)


def test_clean_state_of_space_available_now_is_status_tag():
    assert clean_state_of_space("Available now — move in ready") == "Available now"
    assert clean_state_of_space("Bright dual aspect floor with new kitchen") == ""
