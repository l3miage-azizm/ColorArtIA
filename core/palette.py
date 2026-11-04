"""Deterministic, bounded-memory palette extraction and RGB legend rendering."""
from __future__ import annotations
from dataclasses import dataclass
from threading import Lock
from typing import List, Tuple
import cv2
import numpy as np

Color = Tuple[int, int, int]
_KMEANS_LOCK = Lock()


def palette_codes(count: int) -> list[str]:
    """Compact adult-colouring codes, avoiding ambiguous I/O versus 1/0.

    Up to 33 colours fit in one character. Larger CLI palettes use two-character
    codes without ever changing the meaning of an existing prefix.
    """
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 48:
        raise ValueError("Le nuancier doit contenir entre 1 et 48 couleurs.")
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    result = list(alphabet[:count])
    result.extend("A" + alphabet[i] for i in range(max(0, count-len(alphabet))))
    return result

@dataclass(frozen=True)
class PaletteSpec:
    name: str
    colors: List[Color]

def adaptive_palette(image_rgb: np.ndarray, n_colors: int, seed: int = 42) -> PaletteSpec:
    """Cluster a deterministic sample in Lab; serialize OpenCV's global RNG use."""
    if not 2 <= n_colors <= 48:
        raise ValueError("Le nombre de couleurs doit être compris entre 2 et 48.")
    pixels = np.ascontiguousarray(image_rgb.reshape(-1, 3))
    rng = np.random.default_rng(seed)
    if len(pixels) > 32768:
        pixels = pixels[rng.choice(len(pixels), 32768, replace=False)]
    distinct = np.unique(pixels, axis=0)
    count = min(n_colors, len(distinct))
    centers_rgb = distinct
    if len(distinct) > n_colors:
        lab = cv2.cvtColor(pixels.reshape(-1, 1, 3).astype(np.float32) / 255.0,
                           cv2.COLOR_RGB2LAB).reshape(-1, 3)
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 32, 0.15)
        with _KMEANS_LOCK:
            cv2.setRNGSeed(int(seed) & 0x7FFFFFFF)
            _, _, centers = cv2.kmeans(lab, count, None, criteria, 2, cv2.KMEANS_PP_CENTERS)
        centers_rgb = np.rint(np.clip(cv2.cvtColor(centers.reshape(-1, 1, 3),
                          cv2.COLOR_LAB2RGB).reshape(-1, 3), 0, 1) * 255).astype(np.uint8)
    colors = {tuple(map(int, color)) for color in centers_rgb}
    ordered = sorted(colors, key=lambda c: (0.2126*c[0] + 0.7152*c[1] + 0.0722*c[2], c))
    return PaletteSpec(f"adaptive_{len(ordered)}", ordered)

def palette_preview(spec: PaletteSpec, cell_size: int = 54, columns: int = 8) -> np.ndarray:
    if not spec.colors or cell_size < 20 or columns < 1:
        raise ValueError("Le nuancier doit contenir des couleurs et des dimensions valides.")
    columns = min(columns, len(spec.colors))
    rows = (len(spec.colors) + columns - 1) // columns
    margin, label_h = 20, 28
    width = columns * cell_size + (columns + 1) * margin
    height = rows * (cell_size + label_h) + (rows + 1) * margin
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    codes = palette_codes(len(spec.colors))
    for idx, rgb in enumerate(spec.colors):
        row, col = divmod(idx, columns)
        x = margin + col * (cell_size + margin)
        y = margin + row * (cell_size + label_h + margin)
        cv2.rectangle(canvas, (x, y), (x+cell_size-1, y+cell_size-1), tuple(rgb), -1)
        cv2.rectangle(canvas, (x, y), (x+cell_size-1, y+cell_size-1), (175, 175, 175), 1)
        text = codes[idx]
        (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .55, 1)
        cv2.putText(canvas, text, (x+(cell_size-tw)//2, y+cell_size+21),
                    cv2.FONT_HERSHEY_SIMPLEX, .55, (40, 40, 40), 1, cv2.LINE_AA)
    return canvas
