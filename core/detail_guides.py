"""Exact enlarged code guides for details that must not be merged away.

Guides enlarge the saved region map with nearest-neighbour sampling. They do
not redraw a face, change a colour, or add details absent from the main page.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from scipy import ndimage as ndi


def coverage_report(mapping, main_layouts, guide_records):
    main = set(map(int, main_layouts)) & set(mapping)
    covered = {int(tile["region_id"]) for record in guide_records for tile in record["tiles"]} & set(mapping)
    missing = sorted(set(mapping) - main - covered)
    count = max(1, len(mapping))
    return {
        "main_coded_regions": len(main),
        "guide_only_coded_regions": len(covered - main),
        "uncoded_regions": missing,
        "all_regions_coded_main_or_guide": not missing,
        "main_page_label_coverage": len(main) / count,
        "combined_label_coverage": len(main | covered) / count,
        "guide_pages_required": bool(covered - main),
    }


def _verify_glyph(labels, region_id, layout, glyph_factory):
    glyph = glyph_factory(layout["text"], layout["scale"], layout["thickness"], layout.get("rotation", 0))
    yy, xx = np.nonzero(glyph)
    yy, xx = yy + layout["box"][1], xx + layout["box"][0]
    if (not len(yy) or yy.min() < 0 or xx.min() < 0
            or yy.max() >= labels.shape[0] or xx.max() >= labels.shape[1]
            or not np.all(labels[yy, xx] == region_id)):
        raise ValueError("Un code du guide ne correspond pas à sa zone agrandie.")


def generate_detail_guides(regions, mapping, palette, main_layouts, output_dir,
                           config, *, max_pages=12):
    """Return real, bounded PNG/PDF guides and their independently checked coverage.

    At most sixteen missing codes are placed on each page. Uncovered regions
    remain explicitly reported, so a caller can block validation instead of
    silently claiming complete coverage.
    """
    from . import engine
    from .palette import palette_codes

    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 24:
        raise ValueError("Le nombre de pages de guide doit être compris entre 1 et 24.")
    if regions.ndim != 2 or not np.issubdtype(regions.dtype, np.integer):
        raise ValueError("La carte des zones du guide est invalide.")
    if not len(palette) or any(not 0 <= int(cid) < len(palette) for cid in mapping.values()):
        raise ValueError("Le nuancier du guide ne correspond pas aux zones.")
    pending = sorted(set(mapping) - set(main_layouts))
    if not pending:
        return {"files": {}, "records": [], "coverage": coverage_report(mapping, main_layouts, []), "missing": []}

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    page_width, page_height = config.target_width, config.target_height
    sx, sy = page_width / 2480, page_height / 3508
    left, top = round(70 * sx), round(210 * sy)
    gap_x, gap_y = max(2, round(25 * sx)), max(2, round(24 * sy))
    cell_width = (page_width - left * 2 - gap_x * 3) // 4
    cell_height = (page_height - top - round(70 * sy) - gap_y * 3) // 4
    if min(cell_width, cell_height) < 20:
        raise ValueError("La résolution du guide est trop petite.")
    # The full-page reference width keeps the printed code size consistent when
    # the fitter works on a cropped tile instead of a padded A4-sized canvas.
    guide_config = replace(config, mystery_number_min_scale=.50, mystery_number_max_scale=.75)
    codes = palette_codes(len(palette))
    slices = ndi.find_objects(regions)
    records, files = [], {}
    remaining = list(pending)
    for page_number in range(1, max_pages + 1):
        if not remaining:
            break
        page = np.full((page_height, page_width, 3), 255, np.uint8)
        tiles, next_remaining = [], []
        for rid in remaining:
            if len(tiles) >= 16:
                next_remaining.append(rid)
                continue
            if rid <= 0 or rid > len(slices) or slices[rid - 1] is None:
                next_remaining.append(rid)
                continue
            sl = slices[rid - 1]
            mask = regions[sl] == rid
            distance = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
            cy, cx = np.unravel_index(int(distance.argmax()), distance.shape)
            radius = float(distance[cy, cx])
            cy, cx = int(cy + sl[0].start), int(cx + sl[1].start)
            half = max(max(2, round(8 * sx)),
                       min(max(2, round(160 * sx)), int(np.ceil(radius * 4))))
            x0, y0 = max(0, cx - half), max(0, cy - half)
            x1, y1 = min(regions.shape[1], cx + half + 1), min(regions.shape[0], cy + half + 1)
            crop = regions[y0:y1, x0:x1]
            zoom = min(cell_width / crop.shape[1], cell_height / crop.shape[0])
            width, height = max(1, int(crop.shape[1] * zoom)), max(1, int(crop.shape[0] * zoom))
            enlarged = cv2.resize(crop, (width, height), interpolation=cv2.INTER_NEAREST)
            layouts = engine.number_layouts(enlarged, {rid: int(mapping[rid])}, guide_config,
                                            reference_width=page_width)
            layout = layouts.get(rid)
            if layout is None:
                next_remaining.append(rid)
                continue
            if layout["text"] != codes[int(mapping[rid])]:
                raise ValueError("Le code du guide a changé de couleur.")
            _verify_glyph(enlarged, rid, layout, engine._number_glyph)
            tile = engine.render_mystery_page(enlarged, {rid: mapping[rid]}, palette, guide_config, layouts)
            index = len(tiles)
            tx = left + (index % 4) * (cell_width + gap_x) + (cell_width - width) // 2
            ty = top + (index // 4) * (cell_height + gap_y) + (cell_height - height) // 2
            page[ty:ty + height, tx:tx + width] = tile
            tiles.append({"region_id": int(rid), "source_box": [x0, y0, x1, y1],
                          "destination_box": [tx, ty, tx + width, ty + height],
                          "code": codes[int(mapping[rid])], "layout": layout,
                          "source_center_percent": [round(cx / regions.shape[1] * 100, 1),
                                                    round(cy / regions.shape[0] * 100, 1)]})
        if not tiles:
            break
        filename = f"detail_guide_{page_number:02d}.png"
        Image.fromarray(page).save(output / filename, dpi=(300, 300))
        files[f"detail_guide_{page_number:02d}"] = filename
        records.append({"file": filename, "tiles": tiles})
        remaining = next_remaining

    if records:
        pdf_name = "detail_guides.pdf"
        page_mm = (210.0, 297.0)
        pdf_width, pdf_height = (value / 25.4 * 72 for value in page_mm)
        pdf = canvas.Canvas(str(output / pdf_name), pagesize=(pdf_width, pdf_height))
        pdf.setTitle("ColorArt — Guides des détails")
        for index, record in enumerate(records, 1):
            pdf.drawImage(ImageReader(str(output / record["file"])), 0, 0, pdf_width, pdf_height)
            pdf.setFillColorRGB(.2, .2, .2)
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawString(18, pdf_height - 22, f"Les petits details - planche {index}")
            pdf.setFont("Helvetica", 8)
            pdf.drawString(18, pdf_height - 36, "Retrouvez les petits codes dans ces agrandissements de votre page.")
            for tile_number, tile in enumerate(record["tiles"], 1):
                tx, ty = tile["destination_box"][:2]
                x_percent, y_percent = tile["source_center_percent"]
                pdf.drawString(tx / page_width * pdf_width,
                               pdf_height - ty / page_height * pdf_height + 3,
                               f"{tile_number}. Repere : {x_percent:g}% / {y_percent:g}%")
            pdf.showPage()
        pdf.save()
        files["detail_guides"] = pdf_name
    coverage = coverage_report(mapping, main_layouts, records)
    return {"files": files, "records": records, "coverage": coverage,
            "missing": coverage["uncoded_regions"]}
