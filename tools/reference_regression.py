"""Rebuild the five user-supplied reference examples through the real engine.

The supplied drawings are existing manual ChatGPT illustrations. This measures
drawing-to-colouring preservation; it does not test or certify an image provider.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.engine import GenerationConfig, generate_project, fit_a4, _number_glyph
from core.fidelity import assess_drawing_fidelity


def read_rgb(path):
    with Image.open(path) as image:
        return np.array(image.convert("RGB"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def font(size):
    return ImageFont.truetype(str(ROOT / "assets/fonts/DejaVuSans.ttf"), size)


def panel(path, width=300, height=424):
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    image.thumbnail((width, height))
    page = Image.new("RGB", (width, height), "white")
    page.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cv2.setNumThreads(1)
    records = []
    cases = sorted(p for p in args.fixtures.iterdir() if p.is_dir() and (p / "02_dessin_reference.png").is_file())
    if args.case:
        cases = [p for p in cases if p.name in args.case]
    if not cases:
        raise ValueError("Aucun dessin de référence trouvé.")
    if "preserve_details" not in {field.name for field in fields(GenerationConfig)}:
        raise ValueError("Le moteur de conservation des détails n’est pas encore intégré.")
    overview = Image.new("RGB", (1550, len(cases) * 478 + 65), "#f8f5ee")
    draw = ImageDraw.Draw(overview)
    for index, title in enumerate(("Photo de référence", "Dessin validé", "Coloriage validé", "Nouveau masquage", "Nouvelle reconstruction")):
        draw.text((10 + index * 310, 16), title, fill="#1d2826", font=font(15))
    for case_index, case in enumerate(cases):
        previous = json.loads((case / "metadata.json").read_text())
        source = case / "02_dessin_reference.png"
        source_hash = digest(source)
        config = GenerationConfig(n_colors=32, difficulty="expert", n_segments=2000,
                                  min_region_area=85, preserve_ink=True, preserve_details=True,
                                  density="rich", target_regions=3200, masking="mystery")
        start = time.perf_counter()
        result = generate_project(source, args.output / case.name, config)
        elapsed = time.perf_counter() - start
        metadata = json.loads(Path(result["metadata"]).read_text())
        with np.load(result["regions"], allow_pickle=False) as saved:
            labels = saved["labels"].copy()
            palette = saved["palette"].copy()
            lookup = saved["region_to_color"].copy()
            ink = saved["ink_mask"].copy()
            ink_color = saved["ink_color"].copy()
            expected = palette[lookup[labels]]
            expected[ink] = ((expected[ink].astype(np.uint16) * ink_color.astype(np.uint16) + 127) // 255).astype(np.uint8)
            exact = np.array_equal(expected, read_rgb(result["solution_final"]))
        if not exact:
            raise ValueError("La reconstruction du dessin ne correspond pas à la carte sauvegardée.")
        for rid, layout in metadata["number_layouts"].items():
            rid = int(rid)
            glyph = _number_glyph(layout["text"], layout["scale"], layout["thickness"], layout.get("rotation", 0))
            yy, xx = np.nonzero(glyph)
            yy, xx = yy + layout["box"][1], xx + layout["box"][0]
            if not np.all(labels[yy, xx] == rid):
                raise ValueError("Un numéro de la page déborde de sa vraie zone.")
        if digest(source) != source_hash:
            raise ValueError("Le dessin validé a été modifié.")
        fitted = fit_a4(read_rgb(source), config.target_width, config.target_height)
        final = read_rgb(result["solution_final"])
        rois = previous.get("detail_boxes", {})
        fidelity = assess_drawing_fidelity(fitted, final, rois)
        record = {"case": case.name, "seconds": round(elapsed, 3), "source_sha256": source_hash,
                  "regions": metadata["n_regions"], "colors": metadata["n_colors"],
                  "exact_npz_reconstruction": bool(exact), "all_main_glyphs_inside_region": True,
                  "quality": metadata["quality"], "fidelity": fidelity,
                  "previous_fidelity": previous.get("fidelity"),
                  "files": result,
                  "upstream": "Existing manual ChatGPT reference drawing; no live photo-to-illustration provider was called.",
                  "commercial_quality_approved": False}
        records.append(record)
        y = 55 + case_index * 478
        draw.text((10, y), case.name.replace("_", " "), fill="#1d2826", font=font(17))
        for index, path in enumerate((case / "01_photo_originale.png", source,
                                     case / "04_dessin_colorie_A4.png", result["mystery"], result["solution_final"])):
            overview.paste(panel(path), (10 + index * 310, y + 25))
        draw.text((10, y + 453), f'{metadata["n_regions"]} zones · {elapsed:.1f}s · {fidelity["status"]}',
                  fill="#655f52", font=font(13))
        print(json.dumps({"case": case.name, "seconds": round(elapsed, 3), "regions": metadata["n_regions"],
                          "fidelity": fidelity["status"], "quality_errors": metadata["quality"].get("errors", [])}), flush=True)
        (args.output / "report.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
        overview.save(args.output / "comparaison-reference.jpg", quality=94)
    print(str(args.output / "report.json"), flush=True)


if __name__ == "__main__":
    main()
