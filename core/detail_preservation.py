"""Keep existing eye/mouth colour shapes through printable-code cleanup.

YuNet supplies face geometry only; it neither identifies people nor redraws
features. Protected shapes come from the existing quantized illustration and
retain their original palette index. Tiny pen strokes remain preprinted ink.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage.measure import label as connected_label


MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def _critical_boxes(rgb: np.ndarray):
    height, width = rgb.shape[:2]
    model = Path(__file__).resolve().parents[1] / "assets" / "models" / MODEL_NAME
    if not model.is_file() or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("Le modèle de protection des détails est absent ou invalide.")
    # The per-call detector avoids mutable input-size sharing across requests.
    detector = cv2.FaceDetectorYN.create(str(model), "", (640, 640), .60, .3, 5000)
    scale = min(1.0, 640 / max(height, width))
    size = (max(32, round(width * scale)), max(32, round(height * scale)))
    detector.setInputSize(size)
    _, detected = detector.detect(cv2.cvtColor(cv2.resize(rgb, size), cv2.COLOR_RGB2BGR))
    boxes, faces = [], []
    for raw in [] if detected is None else detected:
        face = raw.copy()
        face[:14] /= scale
        x, y, fw, fh = map(float, face[:4])
        eyes, mouth = face[4:8].reshape(2, 2), face[10:14].reshape(2, 2)
        boxes.extend([
            [float(eyes[:, 0].min() - .14 * fw), float(eyes[:, 1].min() - .08 * fh),
             float(eyes[:, 0].max() + .14 * fw), float(eyes[:, 1].max() + .08 * fh)],
            [float(mouth[:, 0].min() - .08 * fw), float(mouth[:, 1].min() - .06 * fh),
             float(mouth[:, 0].max() + .08 * fw), float(mouth[:, 1].max() + .06 * fh)],
        ])
        faces.append({"box": [round(x), round(y), round(x + fw), round(y + fh)],
                      "confidence": round(float(raw[14]), 4)})
    if not boxes:
        # A conservative central-detail safeguard also helps centered pets and
        # objects. It is explicitly not an animal detector or an identity score.
        boxes = [[.23 * width, .24 * height, .43 * width, .42 * height],
                 [.57 * width, .24 * height, .77 * width, .42 * height],
                 [.40 * width, .40 * height, .60 * width, .58 * height]]
    clipped = [[max(0, int(x0)), max(0, int(y0)), min(width, int(x1) + 1), min(height, int(y1) + 1)]
               for x0, y0, x1, y1 in boxes]
    return clipped, faces


def restore_critical_shapes(working, natural, natural_mapping, coarse, coarse_mapping):
    """Restore complete original shapes, with no rectangular ROI seams."""
    boxes, faces = _critical_boxes(working)
    protected = set()
    for x0, y0, x1, y1 in boxes:
        protected.update(map(int, np.unique(natural[y0:y1, x0:x1])))
    kept = []
    for rid, sl in enumerate(ndi.find_objects(natural), 1):
        if sl is not None and rid in protected:
            mask = natural[sl] == rid
            radius = float(ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1].max())
            if radius >= 3:
                kept.append(rid)
    natural_lut = np.zeros(int(natural.max()) + 1, np.int32)
    coarse_lut = np.zeros(int(coarse.max()) + 1, np.int32)
    for rid, cid in natural_mapping.items():
        natural_lut[rid] = cid
    for rid, cid in coarse_mapping.items():
        coarse_lut[rid] = cid
    keep_lut = np.zeros(int(natural.max()) + 1, bool)
    keep_lut[kept] = True
    keep = keep_lut[natural]
    color_ids = coarse_lut[coarse]
    color_ids[keep] = natural_lut[natural][keep]
    regions = connected_label(color_ids, background=-1, connectivity=1).astype(np.int32)
    ids, first = np.unique(regions, return_index=True)
    mapping = {int(rid): int(cid) for rid, cid in zip(ids, color_ids.ravel()[first])}
    report = {"method": "natural_shapes_landmark_protection_v1", "face_detector": "YuNet 2023mar",
              "face_detector_sha256": MODEL_SHA256, "detected_faces": faces,
              "critical_boxes": boxes, "protected_natural_shapes": len(kept),
              "minimum_shape_radius_at_1240": 3, "redraws_features": False,
              "fallback_central_details": not bool(faces), "human_review_required": True}
    return regions, mapping, report
