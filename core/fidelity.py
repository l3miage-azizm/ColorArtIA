"""Diagnostics for details lost between an aligned drawing and its colouring.

Exact reconstruction of a region map proves that the exported colours agree with
the codes. It does not prove that the map still represents the reference drawing.
This module performs that second, separate comparison. It never assesses identity
or declares a picture suitable for sale. Named eye/mouth/glasses crops should be
supplied by the caller; averaging a complete page can conceal a missing pupil.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping, Sequence

import cv2
import numpy as np


@dataclass(frozen=True)
class DetailPolicy:
    """Explicit diagnostic thresholds, not a calibrated perceptual quality score.

    Distances and contrast windows scale with the full image width. The defaults
    tolerate roughly 1.5 pixels at 1240 px width. Thresholds must be calibrated on
    accepted printed examples before being used as a production acceptance gate.
    """

    edge_low: int = 40
    edge_high: int = 80
    match_tolerance_at_1240: float = 1.5
    contrast_window_at_1240: int = 7
    min_edge_recall: float = .80
    min_edge_precision: float = .55
    min_local_contrast_ratio: float = .45
    min_contrast_coverage: float = .80
    min_reference_edge_pixels: int = 12

    def __post_init__(self):
        if not 0 < self.edge_low < self.edge_high <= 255:
            raise ValueError("Edge thresholds must satisfy 0 < low < high <= 255.")
        if not math.isfinite(self.match_tolerance_at_1240) or self.match_tolerance_at_1240 < 0:
            raise ValueError("The edge match tolerance must be finite and non-negative.")
        if (isinstance(self.contrast_window_at_1240, bool)
                or not isinstance(self.contrast_window_at_1240, int)
                or self.contrast_window_at_1240 < 3):
            raise ValueError("The contrast window must be an integer of at least 3.")
        for name in ("min_edge_recall", "min_edge_precision", "min_local_contrast_ratio", "min_contrast_coverage"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1].")
        if (isinstance(self.min_reference_edge_pixels, bool)
                or not isinstance(self.min_reference_edge_pixels, int)
                or self.min_reference_edge_pixels < 1):
            raise ValueError("The minimum reference edge count must be positive.")


def _validate_rgb(image: np.ndarray, name: str) -> None:
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3
            or min(image.shape[:2]) < 8):
        raise ValueError(f"{name} must be an RGB uint8 image of at least 8 × 8 pixels.")


def _edges_and_contrast(rgb: np.ndarray, low: int, high: int, window: int):
    gray = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY)
    # Mild antialiasing tolerance, applied identically to reference and candidate.
    blurred = cv2.GaussianBlur(gray, (3, 3), .6)
    edges = cv2.Canny(blurred, low, high, L2gradient=True) > 0
    kernel = np.ones((window, window), np.uint8)
    contrast = cv2.subtract(cv2.dilate(gray, kernel), cv2.erode(gray, kernel))
    return edges, contrast


def _match_fraction(source_edges: np.ndarray, target_edges: np.ndarray, tolerance: float):
    count = int(source_edges.sum())
    if not count:
        return None
    if not target_edges.any():
        return 0.0
    distance = cv2.distanceTransform((~target_edges).astype(np.uint8), cv2.DIST_L2,
                                     cv2.DIST_MASK_PRECISE)
    return float(np.count_nonzero(distance[source_edges] <= tolerance) / count)


def _color_error(reference: np.ndarray, rendered: np.ndarray) -> dict:
    # Deterministic sampling bounds memory on A4/300 dpi pages. This colour metric
    # is diagnostic only: it is not used to approve likeness or a new universe.
    pixel_count = reference.shape[0] * reference.shape[1]
    step = max(1, math.ceil(pixel_count / 250_000))
    a = np.ascontiguousarray(reference.reshape(-1, 3)[::step].reshape(-1, 1, 3))
    b = np.ascontiguousarray(rendered.reshape(-1, 3)[::step].reshape(-1, 1, 3))
    lab_a = cv2.cvtColor(a.astype(np.float32) / 255, cv2.COLOR_RGB2LAB).reshape(-1, 3)
    lab_b = cv2.cvtColor(b.astype(np.float32) / 255, cv2.COLOR_RGB2LAB).reshape(-1, 3)
    delta = np.linalg.norm(lab_a - lab_b, axis=1)
    return {"mean_delta_e76": float(delta.mean()),
            "p95_delta_e76": float(np.percentile(delta, 95)),
            "sampled_pixels": int(len(delta)), "sampling_stride": step}


def _measure(reference: np.ndarray, rendered: np.ndarray, full_width: int,
             policy: DetailPolicy) -> dict:
    scale = full_width / 1240
    window = max(3, round(policy.contrast_window_at_1240 * scale)) | 1
    tolerance = (max(1.0, policy.match_tolerance_at_1240 * scale)
                 if policy.match_tolerance_at_1240 else 0.0)
    ref_edges, ref_contrast = _edges_and_contrast(reference, policy.edge_low, policy.edge_high, window)
    out_edges, out_contrast = _edges_and_contrast(rendered, policy.edge_low, policy.edge_high, window)
    edge_count = int(ref_edges.sum())
    recall = _match_fraction(ref_edges, out_edges, tolerance)
    precision = _match_fraction(out_edges, ref_edges, tolerance)
    result = {"reference_edge_pixels": edge_count,
              "rendered_edge_pixels": int(out_edges.sum()),
              "edge_recall": recall, "edge_precision": precision,
              "lost_reference_edge_fraction": None if recall is None else 1 - recall,
              "match_tolerance_pixels": tolerance, "contrast_window_pixels": window,
              "color_error": _color_error(reference, rendered),
              "failures": []}
    if edge_count < policy.min_reference_edge_pixels:
        result.update(status="insufficient_reference_detail", contrast_coverage=None,
                      local_contrast_ratio_median=None, local_contrast_ratio_p10=None)
        return result
    # Spatial tolerance accommodates antialiased/one-pixel shifted boundaries,
    # without accepting an absent feature merely because another edge exists.
    radius = int(math.ceil(tolerance))
    nearby = cv2.dilate(out_contrast, np.ones((2 * radius + 1, 2 * radius + 1), np.uint8))
    ratios = nearby[ref_edges].astype(np.float32) / np.maximum(ref_contrast[ref_edges], 1)
    coverage = float(np.mean(ratios >= policy.min_local_contrast_ratio))
    result.update(contrast_coverage=coverage,
                  local_contrast_ratio_median=float(np.median(ratios)),
                  local_contrast_ratio_p10=float(np.percentile(ratios, 10)))
    if recall < policy.min_edge_recall:
        result["failures"].append("reference_edges_missing")
    if precision is not None and precision < policy.min_edge_precision:
        result["failures"].append("unrelated_edges_added")
    if coverage < policy.min_contrast_coverage:
        result["failures"].append("reference_detail_contrast_lost")
    result["status"] = "needs_review" if result["failures"] else "no_detected_detail_loss"
    return result


def assess_drawing_fidelity(reference_rgb: np.ndarray, rendered_rgb: np.ndarray,
                            rois: Mapping[str, Sequence[int]] | None = None, *,
                            policy: DetailPolicy | None = None) -> dict:
    """Compare the colouring with an aligned reference *drawing*.

    ``rois`` contains named half-open pixel boxes ``[x0, y0, x1, y1]`` in the
    supplied images. It should cover each face and its eyes/mouth separately.
    Images must already have the same framing and universe; no registration or
    resizing silently hides geometric errors. Inputs are never mutated.

    ``no_detected_detail_loss`` only means that these explicit edge/contrast
    checks passed. It is not an identity assessment or commercial approval.
    Missing/blank reference detail remains inconclusive, never a positive pass.
    """
    _validate_rgb(reference_rgb, "reference_rgb")
    _validate_rgb(rendered_rgb, "rendered_rgb")
    if reference_rgb.shape != rendered_rgb.shape:
        raise ValueError("Reference and colouring must have identical dimensions and framing.")
    policy = policy or DetailPolicy()
    height, width = reference_rgb.shape[:2]
    boxes = {"whole_image": [0, 0, width, height]}
    if rois is not None:
        if not isinstance(rois, Mapping):
            raise ValueError("ROIs must be a mapping of names to pixel boxes.")
        for name, box in rois.items():
            if not isinstance(name, str) or not name or name == "whole_image":
                raise ValueError("Each ROI needs a non-empty, unique name other than whole_image.")
            if (not isinstance(box, (tuple, list, np.ndarray)) or len(box) != 4
                    or any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) for v in box)):
                raise ValueError(f"ROI {name} must contain four integer pixel coordinates.")
            x0, y0, x1, y1 = map(int, box)
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height
                    and x1 - x0 >= 8 and y1 - y0 >= 8):
                raise ValueError(f"ROI {name} must lie inside the image and cover at least 8 × 8 pixels.")
            boxes[name] = [x0, y0, x1, y1]
    regions = {}
    for name, (x0, y0, x1, y1) in boxes.items():
        metrics = _measure(reference_rgb[y0:y1, x0:x1], rendered_rgb[y0:y1, x0:x1], width, policy)
        regions[name] = {"box": boxes[name], **metrics}
    failing = [name for name, value in regions.items() if value["status"] == "needs_review"]
    inconclusive = [name for name, value in regions.items()
                    if value["status"] == "insufficient_reference_detail"]
    if failing:
        status = "needs_review"
    elif inconclusive:
        status = "insufficient_reference_detail"
    else:
        status = "no_detected_detail_loss"
    return {"schema_version": 1, "scope": "aligned_drawing_reconstruction_only",
            "status": status, "is_identity_assessment": False,
            "commercial_approval": False,
            "policy_calibration": "diagnostic_defaults_not_print_calibrated",
            "limitations": [
                "No identity, age, expression or anatomical assessment.",
                "Near-neighbour edges can match a different feature inside the stated tolerance.",
                "Local contrast can be supplied by another nearby boundary; inspect eye and pupil crops.",
                "Colour error is diagnostic and does not approve palette or universe choice.",
                "Luminance edges do not measure all equal-luminance chromatic boundaries.",
                "A passing diagnostic is not commercial or print approval.",
            ],
            "policy": asdict(policy), "regions": regions,
            "failing_regions": failing, "inconclusive_regions": inconclusive}
