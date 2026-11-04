"""Gift-book exports with separate colouring and spoiler documents.

The cover is an A4 *front cover*, not a press-specific wraparound cover.
An export is an immutable snapshot of one book revision. Nothing is sent to an
external printer and no payment or order is created by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import threading
from typing import Any
import uuid
import zipfile

from PIL import Image
import reportlab
from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from .library import ValidationError, _detail_guide_assets, _quality_blocked


SCHEMA_VERSION = 1
PAGE_W, PAGE_H = A4
_FONT_LOCK = threading.Lock()


class ExportError(ValueError):
    """The project cannot produce a complete, current book export."""


class StaleExportError(ExportError):
    """The book changed after the export was prepared."""


@dataclass(frozen=True)
class Theme:
    paper: str
    ink: str
    accent: str
    subtitle: str


THEMES = {
    "heritage": Theme("#FAF6ED", "#29251F", "#A77F36", "ÉDITION HÉRITAGE"),
    "nocturne": Theme("#202623", "#FAF6ED", "#C8A766", "ÉDITION NOCTURNE"),
    "botanique": Theme("#F1F4EC", "#293F36", "#8B9B72", "ÉDITION BOTANIQUE"),
}


def _fonts() -> tuple[str, str]:
    """Use embedded Unicode fonts; ReportLab's bundled fonts are portable."""
    with _FONT_LOCK:
        if "ColorArtBody" not in pdfmetrics.getRegisteredFontNames():
            bundled = Path(reportlab.__file__).parent / "fonts"
            studio_fonts = Path(__file__).resolve().parents[1] / "assets" / "fonts"
            body = studio_fonts / "DejaVuSans.ttf"
            display = studio_fonts / "DejaVuSerif.ttf"
            pdfmetrics.registerFont(TTFont("ColorArtBody", str(body if body.is_file() else bundled / "Vera.ttf")))
            pdfmetrics.registerFont(TTFont("ColorArtDisplay", str(display if display.is_file() else bundled / "Vera.ttf")))
    return "ColorArtBody", "ColorArtDisplay"


def _text(value: Any, maximum: int = 4000) -> str:
    # Draw plain text with Canvas, never interpolate user text into PDF markup.
    value = str(value or "")[:maximum]
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32).replace("\t", " ")


def _validate_printable_text(book: dict) -> None:
    """Refuse silent missing-glyph boxes on a personalized gift.

    The bundled portable fonts support French accents and many alphabets, but
    not every emoji or writing system. A precise error is preferable to an
    exported dedication whose characters have disappeared.
    """
    body, display = _fonts()
    fields = (("title", "titre", (body, display)),
              ("recipient", "nom du destinataire", (body, display)),
              ("dedication", "dédicace", (body,)))
    for key, label, fonts in fields:
        for name in fonts:
            glyphs = pdfmetrics.getFont(name).face.charToGlyph
            missing = sorted({ord(char) for char in _text(book.get(key))
                              if not char.isspace() and ord(char) not in glyphs})
            if missing:
                codes = ", ".join(f"U+{code:04X}" for code in missing[:5])
                raise ExportError(f"Le champ {label} contient un caractère non pris en charge "
                                  f"par les polices du livre ({codes}). Remplacez-le avant l'export.")


def _lines(text: str, font: str, size: float, width: float) -> list[str]:
    result: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            result.append("")
            continue
        current = ""
        for word in paragraph.split():
            # Long unbroken titles and names must also respect the page width.
            fragments = []
            part = ""
            for letter in word:
                if part and pdfmetrics.stringWidth(part + letter, font, size) > width:
                    fragments.append(part)
                    part = ""
                part += letter
            fragments.append(part)
            for fragment in fragments:
                candidate = f"{current} {fragment}".strip()
                if current and pdfmetrics.stringWidth(candidate, font, size) > width:
                    result.append(current)
                    current = fragment
                else:
                    current = candidate
        result.append(current)
    return result


def _paragraph(c: canvas.Canvas, text: str, x: float, top: float, width: float,
               font: str, size: float, leading: float | None = None,
               center: bool = False) -> float:
    leading = leading or size * 1.5
    c.setFont(font, size)
    y = top
    for line in _lines(text, font, size, width):
        if center:
            c.drawCentredString(x + width / 2, y, line)
        else:
            c.drawString(x, y, line)
        y -= leading
    return y


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_selected(library: Any, book: dict) -> list[dict]:
    selected = []
    pages = sorted(book.get("pages", []), key=lambda p: p["position"])
    if not pages or len(pages) != book["target_photos"]:
        raise ExportError("Le nombre de photos doit correspondre au livre choisi.")
    for page in pages:
        variants = {v["id"]: v for v in page.get("variants", [])}
        variant = variants.get(page.get("selected_variant_id"))
        if not variant:
            raise ExportError("Chaque photo doit avoir un dessin validé.")
        quality = variant.get("quality", {})
        if _quality_blocked(quality):
            raise ExportError("Un dessin comporte une erreur de qualité bloquante.")
        files = variant.get("files", {})
        resolved = {}
        for key in ("mystery", "solution_final", "metadata"):
            if not files.get(key):
                raise ExportError(f"Fichier de dessin manquant : {key}.")
            resolved[key] = library.safe_asset(book["id"], files[key])
        meta_path = resolved["metadata"]
        if meta_path.stat().st_size > 4 * 1024 * 1024:
            raise ExportError("Métadonnées de dessin trop volumineuses.")
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExportError("Les métadonnées du dessin sont illisibles.") from exc
        palette = metadata.get("palette")
        if not isinstance(palette, list) or not 1 <= len(palette) <= 64:
            raise ExportError("Le nuancier du dessin est absent ou invalide.")
        if any(not isinstance(rgb, (list, tuple)) or len(rgb) != 3 or
               any(not isinstance(v, int) or not 0 <= v <= 255 for v in rgb) for rgb in palette):
            raise ExportError("Les couleurs du nuancier sont invalides.")
        codes = metadata.get("palette_codes", [str(index + 1) for index in range(len(palette))])
        if (not isinstance(codes, list) or len(codes) != len(palette)
                or any(not isinstance(code, str) or not 1 <= len(code) <= 3
                       or not code.isascii() or not code.isalnum() for code in codes)
                or len(set(codes)) != len(codes)):
            raise ExportError("Les codes du nuancier sont invalides ou ne correspondent pas aux couleurs.")
        try:
            resolved.update(_detail_guide_assets(metadata, files,
                lambda path: library.safe_asset(book["id"], path)))
        except (ValidationError, OSError) as exc:
            raise ExportError("Les guides des petits détails sont absents ou incomplets.") from exc
        with Image.open(resolved["mystery"]) as drawing:
            size = drawing.size
        with Image.open(resolved["solution_final"]) as solution:
            if solution.size != size:
                raise ExportError("Le dessin et sa solution n'ont pas les mêmes dimensions.")
        selected.append({"page": page, "variant": variant, "files": resolved,
                         "metadata": metadata, "palette": palette, "codes": codes, "size": size})
    return selected


def _content_hash(book: dict, selected: list[dict]) -> str:
    payload = {key: book.get(key) for key in
               ("id", "revision", "title", "recipient", "template", "target_photos", "dedication")}
    payload["pages"] = [{"id": s["page"]["id"], "position": s["page"]["position"],
                         "variant_id": s["variant"]["id"],
                         "assets": {key: _sha(path) for key, path in s["files"].items()}}
                        for s in selected]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _new_pdf(path: Path, title: str) -> canvas.Canvas:
    c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    c.setTitle(_text(title, 180))
    c.setAuthor("ColorArt Atelier")
    c.setCreator("ColorArt — export de projet personnalisé")
    return c


def _ornament(c: canvas.Canvas, theme: Theme) -> None:
    c.setStrokeColor(HexColor(theme.accent))
    c.setLineWidth(0.7)
    margin = 17 * mm
    c.rect(margin, margin, PAGE_W - margin * 2, PAGE_H - margin * 2)
    c.setLineWidth(0.25)
    c.rect(margin + 4, margin + 4, PAGE_W - margin * 2 - 8, PAGE_H - margin * 2 - 8)
    for x in (margin, PAGE_W - margin):
        for y in (margin, PAGE_H - margin):
            c.circle(x, y, 2, stroke=1, fill=0)


def _place_image(c: canvas.Canvas, path: Path, box: tuple[float, float, float, float]) -> dict:
    x, y, width, height = box
    with Image.open(path) as image:
        iw, ih = image.size
    scale = min(width / iw, height / ih)
    draw_w, draw_h = iw * scale, ih * scale
    c.drawImage(ImageReader(str(path)), x + (width - draw_w) / 2,
                y + (height - draw_h) / 2, draw_w, draw_h, mask="auto")
    return {"pixels": [iw, ih], "placed_mm": [round(draw_w / mm, 2), round(draw_h / mm, 2)],
            "effective_dpi": round(72 / scale, 1)}


def _number_measurements(metadata: dict, placement: dict) -> dict:
    """Measure the actual ink height after fitting the drawing inside the PDF.

    Raster DPI describes sampling, not whether a code is readable. The palette
    and binding margins reduce the drawing, so report physical glyph sizes too.
    """
    layouts = metadata.get("number_layouts")
    if not isinstance(layouts, dict) or not layouts:
        return {}
    from .engine import _number_glyph
    heights = []
    for layout in layouts.values():
        if not isinstance(layout, dict):
            raise ExportError("Les mesures des codes du dessin sont invalides.")
        try:
            text, scale = layout["text"], float(layout["scale"])
            thickness = int(layout["thickness"])
            rotation = layout.get("rotation", 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportError("Les mesures des codes du dessin sont invalides.") from exc
        if (not isinstance(text, str) or not 1 <= len(text) <= 3
                or not math.isfinite(scale) or not 0 < scale <= 10
                or not 1 <= thickness <= 20 or rotation not in (0, 90)):
            raise ExportError("Les mesures des codes du dessin sont invalides.")
        glyph = _number_glyph(text, scale, thickness, rotation)
        # The glyph is cropped to nonzero ink. getTextSize measures a nominal
        # box and overstates the physical height, especially for rotated codes.
        axis = 0 if rotation else 1
        ink_height = glyph.shape[1] if rotation else glyph.shape[0]
        heights.append(ink_height * placement["placed_mm"][axis] / placement["pixels"][axis])
    return {"physical_label_height_min_mm": round(min(heights), 3),
            "minimum_code_height_mm": round(min(heights), 3),
            "maximum_code_height_mm": round(max(heights), 3),
            "measured_code_count": len(heights),
            "code_height_measurement": "actual_nonzero_glyph_ink_after_pdf_placement"}


def _palette(c: canvas.Canvas, palette: list, x: float, y: float, width: float,
             codes: list[str] | None = None) -> None:
    body, _ = _fonts()
    columns = min(12, len(palette))
    rows = math.ceil(len(palette) / columns)
    cell_w = width / columns
    row_h = 17
    c.setFillColor(HexColor("#4F4C46"))
    c.setFont(body, 6.5)
    c.drawString(x, y + rows * row_h + 6, "NUANCIER DE CETTE PAGE · UN CODE, UNE COULEUR")
    for index, rgb in enumerate(palette):
        row, col = divmod(index, columns)
        cx = x + col * cell_w
        cy = y + (rows - row - 1) * row_h
        c.setFillColor(Color(*(v / 255 for v in rgb)))
        c.setStrokeColor(HexColor("#AAA69C"))
        c.setLineWidth(.3)
        c.rect(cx, cy, 10, 10, stroke=1, fill=1)
        c.setFillColor(HexColor("#34312D"))
        c.setFont(body, 6.5)
        c.drawString(cx + 13, cy + 2, codes[index] if codes else str(index + 1))


def _draw_cover(path: Path, book: dict, selected: list[dict]) -> None:
    body, display = _fonts()
    theme = THEMES.get(book.get("template"), THEMES["heritage"])
    c = _new_pdf(path, book["title"])
    c.setFillColor(HexColor(theme.paper))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    _ornament(c, theme)
    c.setFillColor(HexColor(theme.accent))
    c.setFont(display, 24)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 92, "CA")
    c.setFont(body, 8)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 116, "COLLECTION COLORART")
    c.setFillColor(HexColor(theme.ink))
    title = " ".join(_text(book["title"], 180).split())
    size = 25
    while len(_lines(title, display, size, PAGE_W - 130)) > 3 and size > 14:
        size -= 1
    _paragraph(c, title, 65, PAGE_H - 164, PAGE_W - 130, display, size, center=True)
    image_box = (83, 205, PAGE_W - 166, 366)
    c.setFillColor(white)
    c.rect(*image_box, fill=1, stroke=0)
    # The gift cover presents the first approved illustration, matching the
    # studio preview. Numbered worksheets and answers remain separate inside.
    _place_image(c, selected[0]["files"]["solution_final"], image_box)
    c.setStrokeColor(HexColor(theme.accent))
    c.setLineWidth(.6)
    c.rect(*image_box, fill=0, stroke=1)
    c.setFillColor(HexColor(theme.ink))
    recipient = " ".join(_text(book.get("recipient"), 120).split())
    if recipient:
        _paragraph(c, f"Pour {recipient}", 75, 162, PAGE_W - 150, display, 15, center=True)
    c.setFillColor(HexColor(theme.accent))
    c.setFont(body, 7.5)
    c.drawCentredString(PAGE_W / 2, 93, theme.subtitle)
    c.setFillColor(HexColor(theme.ink))
    c.setFont(body, 7)
    c.drawCentredString(PAGE_W / 2, 75, "Des souvenirs à révéler, couleur après couleur.")
    c.showPage()
    c.save()


def _draw_interior(path: Path, book: dict, selected: list[dict]) -> list[dict]:
    body, display = _fonts()
    c = _new_pdf(path, f"{book['title']} — pages à colorier")
    c.setFillColor(HexColor("#A77F36"))
    c.setFont(body, 9)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 110, "UN LIVRE CRÉÉ POUR VOUS")
    c.setFillColor(HexColor("#29251F"))
    y = _paragraph(c, " ".join(_text(book["title"], 180).split()), 65, PAGE_H - 164,
                   PAGE_W - 130, display, 25, center=True)
    if book.get("recipient"):
        recipient = " ".join(_text(book['recipient'], 120).split())
        y = _paragraph(c, f"Pour {recipient}", 75, y - 20,
                       PAGE_W - 150, body, 12, center=True)
    dedication = _text(book.get("dedication"), 2000)
    if dedication:
        # Fit the full dedication into its reserved space rather than silently clip it.
        size = 11
        while len(_lines(dedication, body, size, PAGE_W - 150)) * size * 1.5 > 240 and size > 7:
            size -= .5
        if len(_lines(dedication, body, size, PAGE_W - 150)) * size * 1.5 > 240:
            raise ExportError("La dédicace comporte trop de lignes pour la page d'ouverture. Réduisez les sauts de ligne.")
        _paragraph(c, dedication, 75, min(y - 55, 510), PAGE_W - 150, body, size, center=True)
    c.setFillColor(HexColor("#5C5953"))
    _paragraph(c, "Chaque page possède son propre nuancier. Coloriez chaque zone avec la couleur de son code. "
               "Les solutions sont fournies dans un document séparé, à ouvrir lorsque vous le souhaitez.",
               75, 145, PAGE_W - 150, body, 8.5, center=True)
    c.showPage()
    c.showPage()  # verso blank: each colouring page starts on a recto.
    placements = []
    for index, selected_page in enumerate(selected, 1):
        c.setFont(body, 7)
        c.setFillColor(HexColor("#6E685D"))
        c.drawString(25 * mm, PAGE_H - 15 * mm, f"COLORART · SOUVENIR {index:02d}")
        c.drawRightString(PAGE_W - 18 * mm, PAGE_H - 15 * mm, f"{index} / {len(selected)}")
        # Wider inner margin on all colouring rectos, space below for the page palette.
        palette_rows = math.ceil(len(selected_page["palette"]) / 12)
        palette_height = palette_rows * 17 + 13
        bottom = 17 * mm + palette_height + 12
        box = (25 * mm, bottom, PAGE_W - 43 * mm, PAGE_H - bottom - 23 * mm)
        placement = _place_image(c, selected_page["files"]["mystery"], box)
        placement.update(_number_measurements(selected_page["metadata"], placement))
        placement.update(page_id=selected_page["page"]["id"], page_number=2 * index + 1,
                         variant_id=selected_page["variant"]["id"])
        placements.append(placement)
        _palette(c, selected_page["palette"], 25 * mm, 17 * mm, PAGE_W - 43 * mm,
                 selected_page["codes"])
        c.showPage()
        c.showPage()
    c.save()
    return placements


def _draw_solutions(path: Path, book: dict, selected: list[dict]) -> None:
    body, display = _fonts()
    c = _new_pdf(path, f"{book['title']} — solutions")
    for index, selected_page in enumerate(selected, 1):
        c.setFillColor(HexColor("#29251F"))
        c.setFont(display, 16)
        c.drawCentredString(PAGE_W / 2, PAGE_H - 45, f"Votre souvenir révélé · {index:02d}")
        c.setFont(body, 8)
        c.drawCentredString(PAGE_W / 2, PAGE_H - 63, "Document séparé — contient les résultats coloriés")
        _place_image(c, selected_page["files"]["solution_final"],
                     (25 * mm, 25 * mm, PAGE_W - 50 * mm, PAGE_H - 55 * mm))
        c.showPage()
    c.save()


def _draw_detail_guides(path: Path, book: dict, selected: list[dict]) -> int:
    """Keep enlarged codes in a separate, grayscale companion document.

    Every enlargement uses the engine's exact saved raster. Coordinates locate
    each small shape on its colouring page; no completed illustration is added.
    """
    body, display = _fonts()
    c = _new_pdf(path, f"{book['title']} — guides des petits détails")
    count = 0
    for photo_number, selected_page in enumerate(selected, 1):
        records = selected_page["metadata"].get("detail_guides", [])
        for guide_number, record in enumerate(records, 1):
            key = next(key for key in selected_page["files"]
                       if key.startswith("detail_guide_")
                       and selected_page["files"][key].name == Path(record["file"]).name)
            image_path = selected_page["files"][key]
            with Image.open(image_path) as image:
                image_width, image_height = image.size
            c.drawImage(ImageReader(str(image_path)), 0, 0, PAGE_W, PAGE_H)
            c.setFillColor(HexColor("#292929"))
            c.setFont(display, 13)
            c.drawString(18 * mm, PAGE_H - 8 * mm,
                         f"Souvenir {photo_number:02d} · Petits détails {guide_number:02d}")
            c.setFont(body, 8)
            c.drawString(18 * mm, PAGE_H - 13 * mm,
                         "Codes agrandis · repères mesurés depuis le haut et la gauche du dessin")
            for tile_number, tile in enumerate(record["tiles"], 1):
                tx, ty = tile["destination_box"][:2]
                x_percent, y_percent = tile["source_center_percent"]
                c.drawString(tx / image_width * PAGE_W,
                             PAGE_H - ty / image_height * PAGE_H + 3,
                             f"{tile_number}. Repère : {x_percent:g} % / {y_percent:g} %")
            c.showPage()
            count += 1
    c.save()
    return count


def _cover_preview(pdf_path: Path, image_path: Path) -> None:
    # PDFium keeps the runtime independent of an AGPL/commercial PDF renderer.
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(pdf_path)) as document:
        page = document[0]
        bitmap = page.render(scale=1.5)
        try:
            bitmap.to_pil().save(image_path)
        finally:
            bitmap.close()
            page.close()


def export_book(library: Any, book_id: str, export_root: str | Path | None = None) -> dict:
    """Export one validated snapshot. Source drawings are never regenerated."""
    validation = library.validate_book(book_id)
    if not validation.get("ready"):
        raise ExportError("Le livre n'est pas prêt : " + "; ".join(validation.get("problems", [])))
    book = library.get_book(book_id)
    _validate_printable_text(book)
    selected = _load_selected(library, book)
    fingerprint = _content_hash(book, selected)
    book_dir = library.book_dir(book_id).resolve()
    root = Path(export_root).resolve() if export_root else book_dir / "exports"
    if not root.is_relative_to(book_dir):
        raise ExportError("Le dossier d'export doit appartenir au projet.")
    root.mkdir(parents=True, exist_ok=True)
    export_id = uuid.uuid4().hex
    staging = root / f".pending-{export_id}"
    destination = root / export_id
    staging.mkdir()
    names = {"interior_pdf": "01_interieur_coloriage.pdf", "solutions_pdf": "02_solutions_a_garder_separees.pdf",
             "cover_pdf": "03_couverture_face_A4.pdf", "cover_preview": "apercu_couverture.png",
             "manifest": "manifest.json", "archive": "ColorArt_livre.zip"}
    if any(page["metadata"].get("detail_guides") for page in selected):
        names["guides_pdf"] = "04_guides_des_petits_details.pdf"
    try:
        placements = _draw_interior(staging / names["interior_pdf"], book, selected)
        _draw_solutions(staging / names["solutions_pdf"], book, selected)
        _draw_cover(staging / names["cover_pdf"], book, selected)
        _cover_preview(staging / names["cover_pdf"], staging / names["cover_preview"])
        guide_page_count = (_draw_detail_guides(staging / names["guides_pdf"], book, selected)
                            if "guides_pdf" in names else 0)
        current = library.get_book(book_id)
        if current["revision"] != book["revision"] or _content_hash(current, _load_selected(library, current)) != fingerprint:
            raise StaleExportError("Le livre a changé pendant l'export. Relancez sa préparation.")
        warnings = list(validation.get("warnings", []))
        for index, placement in enumerate(placements, 1):
            if placement["effective_dpi"] < 220:
                warnings.append(f"Dessin {index} : résolution effective {placement['effective_dpi']:.0f} ppp ; "
                                "vérifiez la finesse des traits sur une épreuve imprimée.")
            if placement.get("minimum_code_height_mm", 2) < 1.2:
                warnings.append(f"Dessin {index} : certains codes mesurent seulement "
                                f"{placement['minimum_code_height_mm']:.2f} mm après mise en page ; "
                                "vérifiez leur lecture à la taille réelle ou choisissez un niveau plus simple.")
        warnings.extend([
            "Couverture fournie : face avant A4 uniquement, sans dos ni fond perdu.",
            "PDF en couleurs RGB : profil d'impression, papier, reliure et éventuelle conversion CMJN "
            "doivent être convenus avec l'imprimeur avant production.",
            "Une épreuve physique reste nécessaire pour valider couleurs, lisibilité et confort de coloriage.",
        ])
        manifest = {
            "schema_version": SCHEMA_VERSION, "export_id": export_id, "book_id": book_id,
            "book_revision": book["revision"], "content_hash": fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title": book["title"], "recipient": book.get("recipient", ""),
            "template": book.get("template", "heritage"), "photo_count": len(selected),
            "interior_page_count": 2 + 2 * len(selected), "solutions_page_count": len(selected),
            "guides_page_count": guide_page_count, "guides_separate": bool(guide_page_count),
            "cover_page_count": 1, "page_size_mm": [210, 297], "color_space": "RGB",
            "coloring_rectos": True, "blank_versos": True, "solutions_separate": True,
            "print_provider_connected": False, "warnings": warnings, "placements": placements,
            "files": {key: {"name": name, "sha256": _sha(staging / name)}
                      for key, name in names.items() if key not in ("manifest", "archive")},
        }
        (staging / names["manifest"]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with zipfile.ZipFile(staging / names["archive"], "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for key, name in names.items():
                if key != "archive":
                    archive.write(staging / name, name)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"export_id": export_id, "book_id": book_id, "revision": book["revision"],
            "book_revision": book["revision"], "content_hash": fingerprint,
            "run_dir": str(destination), "warnings": warnings,
            **{key: str(destination / name) for key, name in names.items()}}


def assert_export_current(library: Any, book_id: str, manifest_path: str | Path) -> dict:
    """Reject altered files and stale snapshots before an approval or hand-off."""
    base = library.book_dir(book_id).resolve()
    candidate = Path(manifest_path).resolve()
    if not candidate.is_relative_to(base):
        raise ExportError("Manifest hors du projet.")
    relative = str(candidate.relative_to(base))
    manifest_file = library.safe_asset(book_id, relative)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    book = library.get_book(book_id)
    if manifest.get("book_id") != book_id or manifest.get("book_revision") != book["revision"]:
        raise StaleExportError("Cet aperçu ne correspond plus à la version actuelle du livre.")
    if manifest.get("content_hash") != _content_hash(book, _load_selected(library, book)):
        raise StaleExportError("Les dessins ou les informations du livre ont changé.")
    for item in manifest.get("files", {}).values():
        path = library.safe_asset(book_id, str((candidate.parent / item["name"]).relative_to(base)))
        if _sha(path) != item["sha256"]:
            raise ExportError("Un fichier d'export a été modifié après sa préparation.")
    return manifest
