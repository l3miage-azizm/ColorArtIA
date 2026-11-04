"""Photo-to-colouring engine: reproducible regions, verifiable labels and atomic runs.

The coloured result is an exact lookup of the same region map used by the numbered
page. No photographic texture is silently added to the promised colouring result.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Tuple
import hashlib
import heapq
import json
import math
import os
import shutil
import tempfile
import uuid
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from scipy import ndimage as ndi
from skimage.measure import label as connected_label
from skimage.segmentation import find_boundaries

from .palette import PaletteSpec, adaptive_palette, palette_preview, palette_codes
from .universes import UNIVERSES, apply_universe

ENGINE_VERSION = "2.4.0-details.1"


@dataclass
class GenerationConfig:
    n_colors: int = 24
    target_width: int = 2480
    target_height: int = 3508
    n_segments: int = 900
    compactness: float = 12.0  # retained for CLI/config compatibility
    min_region_area: int = 180  # units at the working resolution, not print pixels
    mystery_line_gray: int = 190
    mystery_number_min_scale: float = 0.40
    mystery_number_max_scale: float = 0.68
    outline_dark: int = 72
    reveal_steps: Tuple[float, ...] = (0.25, 0.50, 0.75, 1.0)
    fit_mode: str = "contain"
    difficulty: str = "balanced"
    seed: int = 42
    working_width: int = 1240
    max_source_pixels: int = 40_000_000
    density: str = "rich"
    target_regions: int = 3200
    preserve_ink: bool = False
    preserve_details: bool = False
    masking: str = "mystery"
    allow_rotated_codes: bool = True

    def __post_init__(self):
        integer_bounds = {"n_colors": (2, 48), "target_width": (32, 4096),
            "target_height": (32, 5793), "n_segments": (2, 4000),
            "min_region_area": (1, 20000), "working_width": (32, 1600),
            "seed": (0, 2**31-1), "max_source_pixels": (1024, 80_000_000),
            "mystery_line_gray": (80, 245), "outline_dark": (0, 180),
            "target_regions": (100, 6000)}
        for key, (low, high) in integer_bounds.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not low <= value <= high:
                raise ValueError(f"Paramètre {key} : entier attendu entre {low} et {high}.")
        if self.target_width * self.target_height > 12_000_000:
            raise ValueError("La résolution de sortie dépasse 12 millions de pixels.")
        if self.fit_mode not in {"contain", "cover"}:
            raise ValueError("Le cadrage doit être 'contain' ou 'cover'.")
        if self.difficulty not in {"relaxed", "balanced", "expert"}:
            raise ValueError("Le niveau doit être relaxed, balanced ou expert.")
        if self.density not in {"natural", "rich"}:
            raise ValueError("La densité doit être natural ou rich.")
        if self.masking not in {"guided", "discreet", "mystery"}:
            raise ValueError("Le masquage doit être guided, discreet ou mystery.")
        if type(self.preserve_ink) is not bool:
            raise ValueError("La conservation du trait doit être un booléen.")
        if type(self.preserve_details) is not bool:
            raise ValueError("La conservation des détails doit être un booléen.")
        if type(self.allow_rotated_codes) is not bool:
            raise ValueError("L'orientation des codes doit être un booléen.")
        if not 0.1 <= self.mystery_number_min_scale <= self.mystery_number_max_scale <= 2:
            raise ValueError("Les tailles de chiffres doivent être ordonnées entre 0,1 et 2.")
        if not math.isfinite(self.compactness) or self.compactness <= 0:
            raise ValueError("La compacité doit être strictement positive.")
        if not 1 <= len(self.reveal_steps) <= 10 or any(not math.isfinite(s) or not 0 <= s <= 1 for s in self.reveal_steps):
            raise ValueError("Les étapes de révélation doivent être comprises entre 0 et 1.")
        if len({round(s * 100) for s in self.reveal_steps}) != len(self.reveal_steps):
            raise ValueError("Les étapes de révélation doivent avoir des pourcentages distincts.")


def fit_a4(image_rgb: np.ndarray, width: int, height: int, mode: str = "contain") -> np.ndarray:
    """Preserve the whole source by default; crop only after explicit selection."""
    h, w = image_rgb.shape[:2]
    if w < 1 or h < 1 or width < 1 or height < 1 or mode not in {"contain", "cover"}:
        raise ValueError("Dimensions ou mode de cadrage invalides.")
    scale = (min if mode == "contain" else max)(width/w, height/h)
    rounder = math.floor if mode == "contain" else math.ceil
    nw, nh = max(1, rounder(w*scale)), max(1, rounder(h*scale))
    resized = cv2.resize(image_rgb, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    if mode == "cover":
        x, y = (nw-width)//2, (nh-height)//2
        return resized[y:y+height, x:x+width].copy()
    page = np.full((height, width, 3), 255, np.uint8)
    x, y = (width-nw)//2, (height-nh)//2
    page[y:y+nh, x:x+nw] = resized
    return page


def _lab(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.ascontiguousarray(rgb).astype(np.float32)/255, cv2.COLOR_RGB2LAB)


def quantize_with_palette(image_rgb: np.ndarray, n_colors: int, seed: int = 42):
    spec = adaptive_palette(image_rgb, n_colors, seed=seed)
    colors = np.array(spec.colors, dtype=np.uint8)
    centers = _lab(colors.reshape(-1, 1, 3)).reshape(-1, 3)
    pixels = _lab(image_rgb).reshape(-1, 3)
    labels = np.empty(len(pixels), np.int32)
    # Float32 avoids the old int16 square overflow; blocks bound intermediate RAM.
    for start in range(0, len(pixels), 16384):
        block = pixels[start:start+16384]
        distances = np.sum((block[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        labels[start:start+len(block)] = distances.argmin(axis=1)
    labels = labels.reshape(image_rgb.shape[:2])
    return colors[labels], labels, spec.colors


def remap_labels_sequential(labels: np.ndarray):
    uniq = np.unique(labels)
    positive = uniq[uniq != 0]
    lookup = np.zeros(int(labels.max())+1, np.int32)
    lookup[positive] = np.arange(1, len(positive)+1)
    return lookup[labels], positive, {int(old): i for i, old in enumerate(positive, 1)}


def region_color_mapping(region_labels: np.ndarray, color_labels: np.ndarray) -> Dict[int, int]:
    count = int(color_labels.max()) + 1
    codes = region_labels.ravel().astype(np.int64)*count + color_labels.ravel()
    histogram = np.bincount(codes, minlength=(int(region_labels.max())+1)*count).reshape(-1, count)
    return {rid: int(histogram[rid].argmax()) for rid in range(1, len(histogram)) if histogram[rid].sum()}


def _adjacency(regions: np.ndarray):
    """Four-connected shared-edge graph; no corner-only/disconnected merges."""
    count = int(regions.max()) + 1
    graph = [dict() for _ in range(count)]
    for a, b in [(regions[:, :-1], regions[:, 1:]), (regions[:-1, :], regions[1:, :])]:
        different = a != b
        aa, bb = a[different].astype(np.int64), b[different].astype(np.int64)
        codes, amounts = np.unique(np.minimum(aa, bb)*count + np.maximum(aa, bb), return_counts=True)
        for code, amount in zip(codes.tolist(), amounts.tolist()):
            left, right = divmod(code, count)
            graph[left][right] = graph[left].get(right, 0) + amount
            graph[right][left] = graph[right].get(left, 0) + amount
    return graph


def _merge_regions(regions: np.ndarray, color_labels: np.ndarray, config: GenerationConfig,
                   forced: set[int] | None = None, palette=None) -> np.ndarray:
    """Merge small adjacent colour regions, updating areas after every merge."""
    regions, _, _ = remap_labels_sequential(regions)
    count = int(regions.max())+1
    graph = _adjacency(regions)
    areas = np.bincount(regions.ravel(), minlength=count).astype(np.int64)
    n_colors = int(color_labels.max())+1
    lab_colors = _lab(np.array(palette, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3) if palette is not None else None
    codes = regions.ravel().astype(np.int64)*n_colors + color_labels.ravel()
    hist = np.bincount(codes, minlength=count*n_colors).reshape(count, n_colors)
    # Mean source RGB is not needed: palette-majority differences preserve value order.
    parents = np.arange(count, dtype=np.int32)
    forced = set(forced or ())
    active = count-1
    threshold = config.min_region_area * {"relaxed": 1.7, "balanced": 1, "expert": .65}[config.difficulty]
    budget = max(2, int(config.n_segments * {"relaxed": .75, "balanced": 1, "expert": 1.25}[config.difficulty]))
    heap = [(int(areas[r]), r) for r in range(1, count)]
    heapq.heapify(heap)
    # Process forced thin regions before ordinary area-based cleanup.
    for rid in sorted(forced):
        heapq.heappush(heap, (-1, rid))
    while heap:
        old_area, rid = heapq.heappop(heap)
        if parents[rid] != rid or (old_area != -1 and old_area != areas[rid]):
            continue
        if areas[rid] >= threshold and active <= budget and rid not in forced:
            continue
        neighbours = graph[rid]
        if not neighbours:
            continue
        cid = int(hist[rid].argmax())
        def cost(other):
            other_cid = int(hist[other].argmax())
            # Lab distance respects hue as well as luminance; shared edge breaks ties.
            delta = float(np.sum((lab_colors[cid]-lab_colors[other_cid])**2)) if lab_colors is not None else abs(cid-other_cid)
            return (delta, -neighbours[other], -int(areas[other]), other)
        target = min(neighbours, key=cost)
        parents[rid] = target
        areas[target] += areas[rid]
        areas[rid] = 0
        hist[target] += hist[rid]
        for neighbour, shared in list(neighbours.items()):
            graph[neighbour].pop(rid, None)
            if neighbour != target:
                graph[target][neighbour] = graph[target].get(neighbour, 0)+shared
                graph[neighbour][target] = graph[neighbour].get(target, 0)+shared
        graph[target].pop(rid, None)
        graph[rid] = {}
        forced.discard(rid)
        active -= 1
        heapq.heappush(heap, (int(areas[target]), target))
    for rid in range(1, count):
        ancestor = rid
        while parents[ancestor] != ancestor:
            ancestor = int(parents[ancestor])
        parents[rid] = ancestor
    return remap_labels_sequential(parents[regions])[0]


def refine_labels(quantized_rgb: np.ndarray, labels: np.ndarray, config: GenerationConfig) -> np.ndarray:
    """Connected palette shapes follow image contours rather than a superpixel grid."""
    clean = cv2.medianBlur(labels.astype(np.uint8), 3).astype(np.int32) if min(labels.shape) >= 3 else labels
    regions = connected_label(clean, background=-1, connectivity=1).astype(np.int32)
    if regions.max() > 60000:
        clean = cv2.medianBlur(clean.astype(np.uint8), 7).astype(np.int32)
        regions = connected_label(clean, background=-1, connectivity=1).astype(np.int32)
    if regions.max() > 100000:
        raise ValueError("La photo contient trop de bruit. Choisissez une photo plus nette ou un niveau plus simple.")
    palette = np.zeros((int(labels.max())+1, 3), np.uint8)
    ids, first = np.unique(labels.ravel(), return_index=True)
    palette[ids] = quantized_rgb.reshape(-1, 3)[first]
    return _merge_regions(regions, labels, config, palette=palette)


@lru_cache(maxsize=2048)
def _number_glyph(text: str, scale: float, thickness: int, rotation: int = 0):
    """The exact antialiased ink later printed on the page (no white box)."""
    (width, height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    canvas = np.zeros((height+baseline+8, width+8), np.uint8)
    cv2.putText(canvas, text, (4, height+3), cv2.FONT_HERSHEY_SIMPLEX, scale,
                255, thickness, cv2.LINE_AA)
    ys, xs = np.nonzero(canvas)
    glyph = canvas[ys.min():ys.max()+1, xs.min():xs.max()+1].copy()
    if rotation == 90:
        glyph = cv2.rotate(glyph, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return glyph


def _glyph_kernel(glyph: np.ndarray, margin: int):
    # Extra whitespace is only a fitting margin, not a printed rectangle.
    padded = np.pad((glyph > 0).astype(np.uint8), margin)
    return cv2.dilate(padded, np.ones((margin*2+1, margin*2+1), np.uint8))


def _center_union(left: np.ndarray, right: np.ndarray):
    height, width = max(left.shape[0], right.shape[0]), max(left.shape[1], right.shape[1])
    # Odd dimensions fix an unambiguous centre for erosion and placement.
    height += not height % 2
    width += not width % 2
    result = np.zeros((height, width), np.uint8)
    for kernel in (left, right):
        y, x = height//2-kernel.shape[0]//2, width//2-kernel.shape[1]//2
        result[y:y+kernel.shape[0], x:x+kernel.shape[1]] |= kernel
    return result


@lru_cache(maxsize=4096)
def _layout_kernel(text: str, base_scale: float, factor: float,
                   final_factor: float, projection: float, rotation: int):
    """Reuse the exact fitting stencil across same-size printed codes.

    A dense page tests the same few dozen glyphs thousands of times. Building
    their projected safety margins once avoids repeated padding and resizing;
    the returned stencil is read-only so a later region cannot change it.
    """
    glyph = _number_glyph(text, float(base_scale * factor), max(1, round(factor)), rotation)
    kernel = _glyph_kernel(glyph, max(1, round(factor * .65)))
    if projection < 1:
        final_glyph = _number_glyph(text, float(base_scale * final_factor),
                                   max(1, round(final_factor)), rotation)
        final_kernel = _glyph_kernel(final_glyph, max(1, round(final_factor * .65)))
        shape = (max(1, math.ceil(final_kernel.shape[1] * projection)),
                 max(1, math.ceil(final_kernel.shape[0] * projection)))
        projected = (cv2.resize(final_kernel.astype(np.float32), shape,
                                interpolation=cv2.INTER_AREA) > 0).astype(np.uint8)
        kernel = _center_union(kernel, projected)
    kernel.setflags(write=False)
    return kernel


def number_layouts(region_labels: np.ndarray, mapping: Dict[int, int], config: GenerationConfig,
                   avoid_mask: np.ndarray | None = None, *, reference_width: int | None = None):
    """Fit actual code ink plus a safety margin inside a connected region.

    A rectangular text bounding box has many empty pixels around an A or a 1.
    Requiring those empty corners to fit destroys narrow facial details. Here
    every printed pixel is checked against its own region, including rotation.
    """
    if avoid_mask is not None:
        if not isinstance(avoid_mask, np.ndarray) or avoid_mask.shape != region_labels.shape:
            raise ValueError("Le masque des traits ne correspond pas aux régions.")
        avoid_mask = np.asarray(avoid_mask, dtype=bool)
    if reference_width is not None and (isinstance(reference_width, bool)
            or not isinstance(reference_width, (int, np.integer))
            or reference_width < region_labels.shape[1]):
        raise ValueError("La largeur de référence doit contenir la carte locale.")
    page_width = int(reference_width or region_labels.shape[1])
    layouts = {}
    factor = max(.45, page_width/1240.0)
    final_factor = max(.45, config.target_width/1240.0)
    projection = page_width/config.target_width
    codes = palette_codes(max(mapping.values())+1) if mapping else []
    slices = ndi.find_objects(region_labels)
    for rid, sl in enumerate(slices, 1):
        if sl is None or rid not in mapping:
            continue
        mask = np.ascontiguousarray((region_labels[sl] == rid).astype(np.uint8))
        if avoid_mask is not None:
            mask &= ~avoid_mask[sl]
        distance = None
        text = codes[mapping[rid]]
        thickness = max(1, round(factor))
        rotations = (0, 90) if config.allow_rotated_codes else (0,)
        found = False
        # Horizontal codes are preferred. Rotation is only a fallback when no
        # normal-size horizontal code can fit, not a random design choice.
        for rotation in rotations:
            for base_scale in np.linspace(config.mystery_number_max_scale, config.mystery_number_min_scale, 6):
                scale = float(base_scale*factor)
                glyph = _number_glyph(text, scale, thickness, rotation)
                kernel = _layout_kernel(text, float(base_scale), factor, final_factor, projection, rotation)
                if kernel.shape[1] > mask.shape[1] or kernel.shape[0] > mask.shape[0]:
                    continue
                valid = cv2.erode(mask, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)
                if not valid.any():
                    continue
                if distance is None:
                    distance = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
                scores = np.where(valid, distance, -1)
                cy, cx = np.unravel_index(int(scores.argmax()), scores.shape)
                x0 = int(cx)-glyph.shape[1]//2+sl[1].start
                y0 = int(cy)-glyph.shape[0]//2+sl[0].start
                layouts[rid] = {"text": text, "center": [int(cx)+sl[1].start, int(cy)+sl[0].start],
                    "box": [x0, y0, x0+glyph.shape[1], y0+glyph.shape[0]],
                    "scale": scale, "thickness": thickness, "rotation": rotation,
                    "ink_height_px": int(glyph.shape[1] if rotation else glyph.shape[0])}
                found = True
                break
            if found:
                break
    return layouts


def _draw_numbers(page: np.ndarray, layouts, ink=58):
    for layout in layouts.values():
        glyph = _number_glyph(layout["text"], layout["scale"], layout["thickness"], layout.get("rotation", 0))
        x0, y0, x1, y1 = layout["box"]
        # Layout validation ensures every nonzero pixel is inside the page.
        sx, sy = max(0, -x0), max(0, -y0)
        ex, ey = glyph.shape[1]-max(0, x1-page.shape[1]), glyph.shape[0]-max(0, y1-page.shape[0])
        alpha = glyph[sy:ey, sx:ex].astype(np.float32)[..., None]/255
        view = page[max(0, y0):min(y1, page.shape[0]), max(0, x0):min(x1, page.shape[1])]
        view[:] = np.rint(view*(1-alpha)+ink*alpha).astype(np.uint8)


def _fit_palette_stable_regions(regions: np.ndarray, mapping: Dict[int, int],
                                palette, config: GenerationConfig):
    """Recheck codes after palette pruning without renumbering colours again.

    Removing an unused colour changes compact codes (for example K becomes J).
    The glyph that fitted before pruning is not necessarily the glyph that will
    be printed. Repair those shapes against the final codebook, and keep that
    codebook fixed during all subsequent merges and dense subdivision.
    """
    lookup = np.zeros(int(regions.max())+1, np.int32)
    for rid, cid in mapping.items():
        lookup[rid] = cid
    color_labels = lookup[regions]
    initial_regions = len(mapping)
    history = []
    for iteration in range(min(24, max(1, initial_regions))):
        layouts = number_layouts(regions, mapping, config)
        missing = set(mapping)-set(layouts)
        history.append({"pass": iteration, "regions": len(mapping), "unlabeled": len(missing)})
        if not missing or len(mapping) <= 1:
            break
        updated = _merge_regions(regions, color_labels, config, missing, palette)
        # A bounded no-progress guard retains a blocking QA error rather than
        # pretending an impossible shape was made printable.
        if np.array_equal(updated, regions):
            break
        regions = updated
        mapping = region_color_mapping(regions, color_labels)
    final_missing = len(mapping)-len(number_layouts(regions, mapping, config))
    return regions, mapping, {"palette_stable": True, "regions_before": initial_regions,
        "regions_after": len(mapping), "unlabeled_after": final_missing,
        "passes": history, "converged": final_missing == 0}


def draw_readable_number(canvas_bgr, text, center, area, bg_gray):
    """Compatibility helper; pipeline rendering uses mask-checked number_layouts."""
    x, y = center
    scale = max(.35, min(.68, math.sqrt(area)/70))
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    cv2.putText(canvas_bgr, text, (max(0, x-tw//2), max(th, y+th//2)),
                cv2.FONT_HERSHEY_SIMPLEX, scale, (58, 58, 58), 1, cv2.LINE_AA)


def render_mystery_page(region_labels, region_to_color, palette, config, layouts=None):
    page = np.full((*region_labels.shape, 3), 255, np.uint8)
    page[find_boundaries(region_labels, mode="inner")] = config.mystery_line_gray
    if layouts is None:
        layouts = number_layouts(region_labels, region_to_color, config)
    _draw_numbers(page, layouts, config.outline_dark)
    return page


def render_coloring_solution(region_labels, region_to_color, palette):
    lookup = np.full((int(region_labels.max())+1, 3), 255, np.uint8)
    for rid, cid in region_to_color.items():
        lookup[rid] = palette[cid]
    return lookup[region_labels]


def render_final_drawing(solution_rgb):
    """The promised colouring is exactly the palette's flat colours."""
    return solution_rgb.copy()


def _reveal_selection(areas, steps):
    """Nested whole-region selections never exceed each requested surface budget."""
    remaining = sorted(range(1, len(areas)), key=lambda r: (-int(areas[r]), r))
    total = int(areas[1:].sum())
    active, active_area, selections = set(), 0, {}
    for step in sorted(steps):
        target = total*step
        for rid in remaining:
            if active_area+areas[rid] <= target or step >= 1:
                active.add(rid)
                active_area += int(areas[rid])
        remaining = [rid for rid in remaining if rid not in active]
        selections[f"{round(step*100):02d}"] = set(active)
    return selections


def progressive_reveal(mystery_rgb, colored_solution_rgb, region_labels, steps):
    areas = np.bincount(region_labels.ravel())
    selections = _reveal_selection(areas, steps)
    out = {}
    for step in steps:
        key = f"{round(step*100):02d}"
        if step >= 1:
            canvas = colored_solution_rgb.copy()
        elif step <= 0:
            canvas = mystery_rgb.copy()
        else:
            lookup = np.zeros(len(areas), bool)
            lookup[list(selections[key])] = True
            mask = lookup[region_labels]
            canvas = mystery_rgb.copy()
            canvas[mask] = colored_solution_rgb[mask]
        out[key] = canvas
    return out


def save_rgb(path: Path, arr: np.ndarray):
    options = {"quality": 95, "subsampling": 0} if path.suffix.lower() in {".jpg", ".jpeg"} else {}
    Image.fromarray(np.ascontiguousarray(arr)).save(path, dpi=(300, 300), **options)


def _read_source(path: Path, config: GenerationConfig):
    if not path.is_file():
        raise ValueError("La photo source est introuvable.")
    if path.stat().st_size > 35*1024*1024:
        raise ValueError("La photo dépasse la limite de 35 Mo.")
    try:
        with Image.open(path) as image:
            if image.width*image.height > config.max_source_pixels:
                raise ValueError("La photo dépasse la limite de pixels autorisée.")
            image = ImageOps.exif_transpose(image)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                image = Image.alpha_composite(background, rgba).convert("RGB")
            return np.array(image.convert("RGB"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Cette photo est illisible ou son format n'est pas pris en charge.") from exc


def generate_project(image_path: str | Path, output_root: str | Path,
                     config: GenerationConfig, universe_key: str = "original") -> Dict[str, str]:
    config.__post_init__()  # also validate configs changed after construction
    if universe_key not in UNIVERSES:
        raise ValueError("L'univers sélectionné n'existe pas.")
    source_path = Path(image_path)
    original = _read_source(source_path, config)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / f"run_{uuid.uuid4().hex}"
    temp = Path(tempfile.mkdtemp(prefix=".tmp_run_", dir=output_root))
    try:
        ww = min(config.working_width, config.target_width)
        wh = max(1, round(config.target_height*ww/config.target_width))
        working = fit_a4(original, ww, wh, config.fit_mode)
        styled = apply_universe(working, universe_key)
        smoothing = {"relaxed": 32, "balanced": 22, "expert": 14}[config.difficulty]
        smoothed = cv2.bilateralFilter(styled, 5, smoothing, 4)
        quantized, colors, palette = quantize_with_palette(smoothed, config.n_colors, config.seed)
        regions = refine_labels(quantized, colors, config)
        natural = regions.copy() if config.preserve_details else None
        natural_mapping = region_color_mapping(regions, colors) if config.preserve_details else None
        # Label thin regions before final rendering; merge rather than silently omit codes.
        for _ in range(4):
            mapping = region_color_mapping(regions, colors)
            layouts = number_layouts(regions, mapping, config)
            missing = set(mapping)-set(layouts)
            if not missing or len(mapping) == 1:
                break
            regions = _merge_regions(regions, colors, config, missing, palette)
        mapping = region_color_mapping(regions, colors)
        detail_preservation = None
        if config.preserve_details:
            from .detail_preservation import restore_critical_shapes
            regions, mapping, detail_preservation = restore_critical_shapes(
                working, natural, natural_mapping, regions, mapping)
            # Keep palette codes stable. Removing a currently unused colour can
            # change the actual glyph required by a protected narrow eye shape.
            post_palette_fit = {"palette_stable": True, "method": "detail_guides",
                "regions_before": len(mapping), "regions_after": len(mapping),
                "merges_for_code_fit": 0}
        else:
            used = sorted(set(mapping.values()))
            palette_remap = {old: i for i, old in enumerate(used)}
            palette = [palette[i] for i in used]
            mapping = {rid: palette_remap[cid] for rid, cid in mapping.items()}
            regions, mapping, post_palette_fit = _fit_palette_stable_regions(regions, mapping, palette, config)
        density_details = {"method": "natural", "requested_regions": config.target_regions,
                           "actual_regions": len(mapping), "warnings": []}
        if config.density == "rich" and config.preserve_details:
            from .density import densify_with_microdetails
            regions, mapping, density_details = densify_with_microdetails(
                regions, mapping, config, number_layouts,
                target_regions=config.target_regions, seed=config.seed)
        elif config.density == "rich" and len(number_layouts(regions, mapping, config)) == len(mapping):
            from .density import densify_regions
            regions, mapping, density_details = densify_regions(
                regions, mapping, config, number_layouts,
                target_regions=config.target_regions, seed=config.seed)
        if regions.shape != (config.target_height, config.target_width):
            regions = cv2.resize(regions, (config.target_width, config.target_height), interpolation=cv2.INTER_NEAREST)
        regions = regions.astype(np.int32)
        layouts = number_layouts(regions, mapping, config)
        mystery = render_mystery_page(regions, mapping, palette, config, layouts)
        final = render_coloring_solution(regions, mapping, palette)
        flat_solution = final
        fixed_ink = np.zeros(regions.shape, dtype=bool)
        from .illustration import prepare_print_ink, apply_ink
        fixed_ink, ink_color = prepare_print_ink(fixed_ink, config.masking)
        extracted_ink_pixels = 0
        if config.preserve_ink:
            from .illustration import drawing_ink, clear_code_footprints
            illustrated = fit_a4(original, config.target_width, config.target_height, config.fit_mode)
            raw_ink = drawing_ink(illustrated)
            if config.preserve_details:
                # Prefer positions away from glasses, pupils and mouth lines;
                # do not erase those strokes merely to centre a printed code.
                layouts.update(number_layouts(regions, mapping, config, avoid_mask=raw_ink))
                mystery = render_mystery_page(regions, mapping, palette, config, layouts)
            fixed_ink = clear_code_footprints(raw_ink, layouts, _number_glyph)
            extracted_ink_pixels = int(fixed_ink.sum())
            fixed_ink, ink_color = prepare_print_ink(fixed_ink, config.masking)
            # The very same grey strokes are preprinted on the worksheet.
            # Colour laid over them may darken them; light ink cannot turn
            # dark hair grey. No black-only details appear in the solution.
            mystery = apply_ink(mystery, fixed_ink, ink_color, composition="darken")
            final = apply_ink(final, fixed_ink, ink_color, composition="multiply")
        warnings, errors = list(density_details.get("warnings", [])), []
        missing = len(mapping)-len(layouts)
        guides = None
        if config.preserve_details and missing:
            from .detail_guides import generate_detail_guides
            guides = generate_detail_guides(regions, mapping, palette, layouts, temp, config)
            if guides["missing"]:
                errors.append(f"{len(guides['missing'])} zone(s) sans code lisible, même dans les guides de détails.")
            if guides["coverage"]["guide_pages_required"]:
                warnings.append("Les guides de détails joints sont nécessaires pour lire les codes de certaines petites zones.")
        elif missing:
            errors.append(f"{missing} zone(s) trop étroite(s) pour un numéro lisible. Réduisez le niveau de détail.")
        if min(original.shape[1]/config.target_width, original.shape[0]/config.target_height) < .45:
            warnings.append("La photo source est petite pour une impression A4 détaillée : vérifiez les détails importants dans l'aperçu.")
        if config.fit_mode == "cover":
            warnings.append("Le cadrage remplit la page et recadre les bords : vérifiez que tous les sujets sont conservés.")
        work_final = cv2.resize(final, (ww, wh), interpolation=cv2.INTER_NEAREST)
        error_rgb = float(np.abs(work_final.astype(np.float32)-styled.astype(np.float32)).mean())
        if error_rgb > 35:
            warnings.append("La simplification modifie fortement certaines couleurs : comparez attentivement avec la photo.")
        quality = {"status": "needs_attention" if errors else "ready_for_review", "errors": errors,
            "warnings": warnings, "label_coverage": round(len(layouts)/max(1, len(mapping)), 6),
            "unlabeled_regions": missing, "regions_too_small_for_number": missing,
            "resolution": [config.target_width, config.target_height], "working_resolution": [ww, wh],
            "effective_dpi": round(min(config.target_width/(210/25.4), config.target_height/(297/25.4)), 2),
            "source_resolution": [original.shape[1], original.shape[0]],
            "mean_rgb_error": round(error_rgb, 3), "human_review_required": True}
        quality["density"] = density_details
        quality["post_palette_fit"] = post_palette_fit
        if detail_preservation is not None:
            quality["detail_preservation"] = detail_preservation
        if guides is not None:
            quality.update(guides["coverage"])
            quality["main_page_unlabeled_regions"] = missing
            quality["main_page_label_coverage"] = round(len(layouts)/max(1, len(mapping)), 6)
            quality["label_coverage"] = round(guides["coverage"]["combined_label_coverage"], 6)
            quality["unlabeled_regions"] = len(guides["missing"])
            quality["regions_too_small_for_number"] = len(guides["missing"])
        quality["preprinted_ink_pixels"] = int(fixed_ink.sum())
        quality["masking"] = {"mode": config.masking, "applied": config.preserve_ink,
            "ink_gray": int(ink_color[0]), "extracted_ink_pixels": extracted_ink_pixels,
            "printed_ink_pixels": int(fixed_ink.sum()), "composition": "darken",
            "subject_invisibility_guaranteed": False}
        quality["colored_render"] = {"page_ink_composition": "darken",
            "solution_ink_composition": "multiply", "masking_pipeline_changed": False}
        areas = np.bincount(regions.ravel())
        quality["reveal_surface_percentages"] = {key: round(sum(int(areas[r]) for r in selected)/regions.size*100, 2)
            for key, selected in _reveal_selection(areas, config.reveal_steps).items()}
        files = {"source": "00_source.png", "original": "01_original.jpg",
            "mystery": "02_mystere_total.png", "solution_flat": "03_solution_aplats.png",
            "solution_final": "04_solution_finale_dessin.png", "palette": "05_palette.png",
            "regions": "regions.npz", "quality": "quality.json", "metadata": "metadata.json"}
        if guides is not None:
            files.update(guides["files"])
        save_rgb(temp/files["source"], original)
        fitted = fit_a4(original, config.target_width, config.target_height, config.fit_mode)
        save_rgb(temp/files["original"], fitted)
        save_rgb(temp/files["mystery"], mystery)
        save_rgb(temp/files["solution_flat"], flat_solution)
        save_rgb(temp/files["solution_final"], final)
        save_rgb(temp/files["palette"], palette_preview(PaletteSpec("adaptive", palette)))
        for key, image in progressive_reveal(mystery, final, regions, config.reveal_steps).items():
            files[f"reveal_{key}"] = f"reveal_{key}.png"
            save_rgb(temp/files[f"reveal_{key}"], image)
        lookup = np.zeros(int(regions.max())+1, np.int16)
        for rid, cid in mapping.items():
            lookup[rid] = cid
        np.savez_compressed(temp/files["regions"], labels=regions.astype(np.uint16 if len(mapping)<65535 else np.uint32),
                            region_to_color=lookup, palette=np.array(palette, np.uint8),
                            ink_mask=fixed_ink, ink_color=ink_color,
                            ink_composition=np.array("multiply"))
        (temp/files["quality"]).write_text(json.dumps(quality, indent=2, ensure_ascii=False), encoding="utf-8")
        metadata = {"engine_version": ENGINE_VERSION, "config": asdict(config), "universe": universe_key,
            "n_regions": len(mapping), "n_colors": len(palette), "palette": palette, "files": files,
            "palette_codes": palette_codes(len(palette)),
            "quality": quality, "warnings": warnings, "number_layouts": layouts,
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "palette_scope": "page", "segmentation": "connected_palette_regions",
            "density": density_details,
            "rendering_note": "Masquage, régions, codes, nuancier et encre préimprimée conservés. La page garde sa composition darken ; seul le rendu colorié simule la superposition par multiply pour conserver le contraste des traits. La reconstruction ne recrée aucun contour absent du masque. Essai papier et contrôle visuel requis."}
        if guides is not None:
            metadata["detail_guides"] = guides["records"]
        (temp/files["metadata"]).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, run_dir)
        return {**{key: str(run_dir/name) for key, name in files.items()}, "run_dir": str(run_dir)}
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
