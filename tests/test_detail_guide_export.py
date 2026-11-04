"""A guide-only facial detail remains usable, separately exported and immutable."""
from copy import deepcopy
import json
from pathlib import Path
import zipfile

from PIL import Image, ImageDraw
from pypdf import PdfReader
import pytest
from reportlab.pdfgen import canvas

from core.book_export import ExportError, StaleExportError, export_book, assert_export_current
from core.library import ValidationError, _detail_guide_assets


class GuidedBook:
    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        files = {"metadata": "metadata.json", "mystery": "mystery.png", "solution_final": "solution.png",
                 "detail_guide_01": "detail_guide_01.png", "detail_guides": "detail_guides.pdf"}
        Image.new("RGB", (320, 450), "white").save(root / files["mystery"])
        Image.new("RGB", (320, 450), "red").save(root / files["solution_final"])
        guide = Image.new("RGB", (320, 450), "white")
        draw = ImageDraw.Draw(guide)
        draw.rectangle((30, 100, 130, 200), outline=(100, 100, 100), width=2)
        draw.text((75, 145), "B", fill=(72, 72, 72))
        guide.save(root / files["detail_guide_01"])
        pdf = canvas.Canvas(str(root / files["detail_guides"]))
        pdf.drawString(30, 700, "Detail B")
        pdf.showPage()
        pdf.save()
        quality = {"label_coverage": 1.0, "unlabeled_regions": 0,
                   "main_page_label_coverage": .5, "combined_label_coverage": 1.0,
                   "main_coded_regions": 1, "guide_only_coded_regions": 1,
                   "uncoded_regions": [], "all_regions_coded_main_or_guide": True,
                   "guide_pages_required": True}
        self.metadata = {"n_regions": 2, "palette": [[255, 0, 0], [0, 0, 255]],
                         "palette_codes": ["A", "B"], "quality": quality,
                         "number_layouts": {"1": {"text": "A", "scale": .5, "thickness": 1}},
                         "detail_guides": [{"file": "detail_guide_01.png", "tiles": [
                             {"region_id": 2, "code": "B", "layout": {"text": "B"},
                              "source_box": [20, 20, 40, 40], "destination_box": [30, 100, 130, 200],
                              "source_center_percent": [10, 12]}]}]}
        self.save_metadata()
        self.book = {"id": "testbook", "title": "Les souvenirs de Léa", "recipient": "Léa",
                     "template": "heritage", "dedication": "Pour toi", "revision": 1, "target_photos": 1,
                     "pages": [{"id": "photo1", "position": 0, "selected_variant_id": "drawing1",
                                "variants": [{"id": "drawing1", "quality": quality, "files": files}]}]}

    def save_metadata(self):
        (self.root / "metadata.json").write_text(json.dumps(self.metadata), encoding="utf-8")

    def get_book(self, book_id):
        assert book_id == "testbook"
        return deepcopy(self.book)

    def validate_book(self, book_id):
        return {"ready": True, "warnings": []}

    def book_dir(self, book_id):
        return self.root

    def safe_asset(self, book_id, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise ValidationError("Missing asset")
        return path


@pytest.fixture
def guided(tmp_path):
    return GuidedBook(tmp_path / "book")


def test_guide_only_code_exports_as_separate_grayscale_companion(guided):
    result = export_book(guided, "testbook")
    guides = PdfReader(result["guides_pdf"])
    assert len(guides.pages) == 1
    text = guides.pages[0].extract_text()
    assert "Souvenir 01" in text and "10 % / 12 %" in text
    raster = list(guides.pages[0].images)[0].image.convert("RGB")
    assert all(r == g == b for r, g, b in raster.get_flattened_data())
    assert len(PdfReader(result["interior_pdf"]).pages) == 4
    with zipfile.ZipFile(result["archive"]) as archive:
        assert "04_guides_des_petits_details.pdf" in archive.namelist()
        assert archive.testzip() is None
    manifest = assert_export_current(guided, "testbook", result["manifest"])
    assert manifest["guides_separate"] is True and manifest["guides_page_count"] == 1
    assert "guides_pdf" in manifest["files"]


def test_missing_guides_cannot_turn_claimed_coverage_into_printable_book(guided):
    (guided.root / "detail_guide_01.png").unlink()
    with pytest.raises(ExportError, match="guides"):
        export_book(guided, "testbook")


def test_altered_guide_raster_invalidates_prepared_book(guided):
    result = export_book(guided, "testbook")
    Image.new("RGB", (320, 450), "white").save(guided.root / "detail_guide_01.png")
    with pytest.raises(StaleExportError, match="dessins"):
        assert_export_current(guided, "testbook", result["manifest"])


def test_duplicate_or_incomplete_guide_code_fails_validation(guided):
    records = guided.metadata["detail_guides"]
    records[0]["tiles"].append(deepcopy(records[0]["tiles"][0]))
    files = guided.book["pages"][0]["variants"][0]["files"]
    with pytest.raises(ValidationError, match="dupliqué"):
        _detail_guide_assets(guided.metadata, files, lambda p: guided.safe_asset("testbook", p))
    records[0]["tiles"].pop()
    guided.metadata["quality"]["guide_only_coded_regions"] = 2
    with pytest.raises(ValidationError, match="toutes les zones"):
        _detail_guide_assets(guided.metadata, files, lambda p: guided.safe_asset("testbook", p))
