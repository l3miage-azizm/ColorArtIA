"""Printable pen strokes from an approved illustration, with adjustable mystery.

The same ink is used in the worksheet and solution. Light preprinted strokes
must never lighten the palette colours laid over them.
"""
from __future__ import annotations

import cv2
import numpy as np
from skimage.morphology import skeletonize


MASKING_PROFILES = {
    "guided": {"gray": 32, "thin": False},
    "discreet": {"gray": 150, "thin": False},
    "mystery": {"gray": 190, "thin": True},
}


def prepare_print_ink(mask: np.ndarray, masking: str) -> tuple[np.ndarray, np.ndarray]:
    """Soften subject cues while retaining the original colouring geometry.

    Mystery uses a two-pixel pen at A4/300 dpi (about 0.17 mm), rather than
    thinning to a fragile single print pixel. Clipping preserves code clearance
    and prevents dilation from inventing strokes outside the approved artwork.
    """
    if masking not in MASKING_PROFILES:
        raise ValueError("Le masquage doit être guided, discreet ou mystery.")
    profile = MASKING_PROFILES[masking]
    printed = np.asarray(mask, dtype=bool).copy()
    if profile["thin"] and printed.any():
        width = max(1, round(mask.shape[1] / 2480 * 2))
        pen = cv2.dilate(skeletonize(printed).astype(np.uint8),
                         np.ones((width, width), np.uint8)).astype(bool)
        printed &= pen
    return printed, np.full(3, profile["gray"], np.uint8)


def drawing_ink(image: np.ndarray) -> np.ndarray:
    """Extract thin dark pen marks from an already illustrated input.

    This is deliberately opt-in: extracting texture from a photograph would
    defeat the distinction between the upstream drawing and the source photo.
    """
    gray = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2GRAY)
    size = max(5, round(image.shape[1] / 1240 * 5)) | 1
    closing = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    return (closing.astype(np.int16) - gray.astype(np.int16) >= 18) & (gray < 110)


def clear_code_footprints(mask: np.ndarray, layouts: dict, glyph_factory) -> np.ndarray:
    """Keep pen details away from every actual code's ink footprint."""
    mask = mask.copy()
    h, w = mask.shape
    for layout in layouts.values():
        glyph = glyph_factory(layout["text"], layout["scale"], layout["thickness"], layout.get("rotation", 0))
        footprint = cv2.dilate((glyph > 0).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        x0, y0, x1, y1 = layout["box"]
        sx, sy = max(0, -x0), max(0, -y0)
        ex, ey = glyph.shape[1] - max(0, x1-w), glyph.shape[0] - max(0, y1-h)
        mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] &= ~footprint[sy:ey, sx:ex]
    return mask


def structural_ink(regions: np.ndarray, mapping: dict[int, int], palette,
                   threshold: float = 23.0) -> np.ndarray:
    """Ink strong boundaries; keep low-contrast shading boundaries discreet."""
    if not mapping:
        return np.zeros(regions.shape, dtype=bool)
    colors = np.array(palette, dtype=np.uint8)
    lab = cv2.cvtColor(colors.reshape(-1, 1, 3).astype(np.float32) / 255,
                       cv2.COLOR_RGB2LAB).reshape(-1, 3)
    contrast = np.sqrt(((lab[:, None, :] - lab[None, :, :]) ** 2).sum(axis=2))
    lookup = np.zeros(int(regions.max()) + 1, dtype=np.int16)
    for rid, cid in mapping.items():
        lookup[int(rid)] = int(cid)
    ids = lookup[regions]
    mask = np.zeros(regions.shape, dtype=bool)
    mask[:, :-1] |= contrast[ids[:, :-1], ids[:, 1:]] >= threshold
    mask[:-1, :] |= contrast[ids[:-1, :], ids[1:, :]] >= threshold
    return mask


def apply_ink(image: np.ndarray, mask: np.ndarray, ink=(35, 35, 35),
              composition: str = "replace") -> np.ndarray:
    """Composite legacy opaque ink or preprint that cannot lighten a colour."""
    if composition not in {"replace", "darken", "multiply"}:
        raise ValueError("Composition d'encre inconnue.")
    rendered = image.copy()
    if composition == "multiply":
        # Simulation of colouring over the existing grey preprint. Unlike min,
        # this preserves its contrast on colours already darker than the ink.
        # uint16 accommodates 255*255+127 without wrapping.
        rendered[mask] = ((rendered[mask].astype(np.uint16) *
                          np.asarray(ink, dtype=np.uint16) + 127) // 255).astype(np.uint8)
    else:
        rendered[mask] = np.minimum(rendered[mask], ink) if composition == "darken" else ink
    return rendered
