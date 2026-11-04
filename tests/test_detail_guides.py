import hashlib

import numpy as np
from PIL import Image
import pypdfium2 as pdfium

from core.detail_guides import coverage_report, generate_detail_guides
from core.engine import GenerationConfig, _number_glyph


def fixture():
    regions = np.ones((877, 620), np.int32)
    regions[150, 130] = 2
    regions[400:402, 302:304] = 3
    return regions, {1: 0, 2: 15, 3: 31}, [(i * 7, i * 5, i * 3) for i in range(32)]


def test_enlarged_codes_are_real_and_do_not_modify_saved_map(tmp_path):
    regions, mapping, palette = fixture()
    before = hashlib.sha256(regions.tobytes()).hexdigest()
    config = GenerationConfig(target_width=620, target_height=877, preserve_ink=True)
    result = generate_detail_guides(regions, mapping, palette, {1: {}}, tmp_path, config)
    assert hashlib.sha256(regions.tobytes()).hexdigest() == before
    assert result["missing"] == []
    assert result["coverage"]["guide_only_coded_regions"] == 2
    assert result["coverage"]["main_page_label_coverage"] == 1 / 3
    assert result["coverage"]["combined_label_coverage"] == 1
    assert result["coverage"]["guide_pages_required"]
    assert result["files"]["detail_guides"] == "detail_guides.pdf"
    with pdfium.PdfDocument(str(tmp_path / result["files"]["detail_guides"])) as document:
        assert len(document) == len(result["records"]) == 1
    for record in result["records"]:
        with Image.open(tmp_path / record["file"]) as image:
            assert image.size == (620, 877)
            assert image.mode == "RGB"
            pixels = np.array(image)
            assert np.array_equal(pixels[:, :, 0], pixels[:, :, 1])
            assert np.array_equal(pixels[:, :, 1], pixels[:, :, 2])
        assert {tile["region_id"] for tile in record["tiles"]} == {2, 3}
        assert {tile["code"] for tile in record["tiles"]} == {"G", "Y"}
        for tile in record["tiles"]:
            layout = tile["layout"]
            glyph = _number_glyph(layout["text"], layout["scale"], layout["thickness"], layout.get("rotation", 0))
            assert np.any(glyph)
            x0, y0, x1, y1 = tile["source_box"]
            assert np.any(regions[y0:y1, x0:x1] == tile["region_id"])


def test_main_page_with_complete_codes_has_no_extra_files(tmp_path):
    regions, mapping, palette = fixture()
    config = GenerationConfig(target_width=620, target_height=877)
    result = generate_detail_guides(regions, mapping, palette, {1: {}, 2: {}, 3: {}}, tmp_path, config)
    assert result["files"] == {}
    assert result["records"] == []
    assert result["coverage"]["combined_label_coverage"] == 1
    assert not result["coverage"]["guide_pages_required"]
    assert not list(tmp_path.iterdir())


def test_bounded_guides_report_every_remaining_code(tmp_path):
    regions = np.ones((877, 620), np.int32)
    mapping = {1: 0}
    for index in range(20):
        rid = index + 2
        regions[100 + index * 15, 100] = rid
        mapping[rid] = index % 16
    config = GenerationConfig(target_width=620, target_height=877)
    result = generate_detail_guides(regions, mapping, [(10, 20, 30)] * 16, {1: {}}, tmp_path, config, max_pages=1)
    assert len(result["records"]) == 1
    assert len(result["records"][0]["tiles"]) == 16
    assert len(result["missing"]) == 4
    assert not result["coverage"]["all_regions_coded_main_or_guide"]
    assert result["coverage"]["combined_label_coverage"] == 17 / 21


def test_coverage_cannot_hide_missing_region_in_claimed_main_layout():
    result = coverage_report({1: 0, 2: 1}, {1: {}, 999: {}}, [{"tiles": [{"region_id": 999}]}])
    assert result["main_coded_regions"] == 1
    assert result["uncoded_regions"] == [2]
    assert result["combined_label_coverage"] == .5
