"""Correct only the coloured rendering of an existing, approved run.

No photo, illustration, segmentation, palette, number placement or worksheet is
regenerated. Publishing into a separate run keeps the approved input immutable.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np
from PIL import Image

from .engine import progressive_reveal, save_rgb
from .illustration import apply_ink


_REQUIRED = {"source", "original", "mystery", "solution_flat", "solution_final",
             "palette", "regions", "quality", "metadata"}
_ARRAYS = {"labels", "region_to_color", "palette", "ink_mask", "ink_color"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _array_sha(value: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(value.dtype.str.encode())
    digest.update(json.dumps(value.shape).encode())
    digest.update(value.tobytes())
    return digest.hexdigest()


def _asset(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "\\" in name
            or Path(name).is_absolute() or ".." in Path(name).parts):
        raise ValueError("Un fichier du rendu utilise un chemin non autorisé.")
    asset = root / name
    if not asset.is_file() or asset.is_symlink() or not asset.resolve().is_relative_to(root):
        raise ValueError("Un fichier du rendu est absent ou sort de son dossier.")
    return asset


def correct_render_run(source_run: str | Path, output_root: str | Path) -> dict[str, str]:
    """Copy an approved run and replace only its final solution and reveals.

    The page retains its original ``darken`` composition. The solution models
    pigment over that same printed ink with ``multiply``; applying multiply to
    the worksheet itself would change already approved line intersections.
    """
    supplied = Path(source_run)
    if not supplied.is_dir() or supplied.is_symlink():
        raise ValueError("Le rendu approuvé est introuvable.")
    source = supplied.resolve()
    destination_root = Path(output_root).resolve()
    if destination_root == source or destination_root.is_relative_to(source):
        raise ValueError("Le nouveau rendu doit être enregistré hors du rendu approuvé.")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Le rendu approuvé ne peut pas contenir de lien symbolique.")
    meta_path = _asset(source, "metadata.json")
    if meta_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Les métadonnées du rendu sont trop volumineuses.")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Les métadonnées du rendu sont illisibles.") from exc
    files = metadata.get("files") if isinstance(metadata, dict) else None
    if not isinstance(files, dict) or not _REQUIRED.issubset(files):
        raise ValueError("Les fichiers du rendu approuvé sont incomplets.")
    assets = {key: _asset(source, name) for key, name in files.items()}
    if assets["metadata"] != meta_path:
        raise ValueError("Le fichier des métadonnées du rendu est incohérent.")
    config = metadata.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("La configuration du rendu est invalide.")
    steps = tuple(config.get("reveal_steps", (.25, .5, .75, 1.0)))
    if (not 1 <= len(steps) <= 10 or any(isinstance(step, bool) or not isinstance(step, (int, float))
            or not math.isfinite(step) or not 0 <= step <= 1 for step in steps)
            or len({round(step * 100) for step in steps}) != len(steps)):
        raise ValueError("Les étapes de révélation du rendu sont invalides.")
    reveal_keys = {f"reveal_{round(step * 100):02d}" for step in steps}
    existing_reveals = {key for key in files if key.startswith("reveal_")}
    if reveal_keys != existing_reveals:
        raise ValueError("Les fichiers de révélation ne correspondent pas à la configuration.")
    mutable = {"solution_final", "regions", "quality", "metadata"} | reveal_keys
    mutable_paths = [assets[key] for key in mutable]
    if (len(set(mutable_paths)) != len(mutable_paths)
            or set(mutable_paths) & {assets[key] for key in files if key not in mutable}):
        raise ValueError("Un fichier à corriger recouvre un fichier approuvé à conserver.")
    try:
        with np.load(assets["regions"], allow_pickle=False) as archive:
            arrays = {key: archive[key].copy() for key in archive.files}
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError("La carte du rendu approuvé est illisible.") from exc
    if not _ARRAYS.issubset(arrays):
        raise ValueError("La carte du rendu approuvé est incomplète.")
    labels, lookup, palette = (arrays[key] for key in ("labels", "region_to_color", "palette"))
    ink, ink_color = arrays["ink_mask"], arrays["ink_color"]
    if (labels.ndim != 2 or not labels.size or labels.size > 12_000_000
            or not np.issubdtype(labels.dtype, np.integer) or np.any(labels < 0)
            or lookup.ndim != 1 or not np.issubdtype(lookup.dtype, np.integer)
            or labels.max() >= len(lookup) or not lookup.size
            or palette.ndim != 2 or palette.shape[1:] != (3,) or not len(palette)
            or not np.issubdtype(palette.dtype, np.integer)
            or np.any(palette < 0) or np.any(palette > 255)
            or np.any(lookup < 0) or np.any(lookup >= len(palette))
            or ink.shape != labels.shape or ink_color.shape != (3,)
            or not np.issubdtype(ink_color.dtype, np.integer)
            or np.any(ink_color < 0) or np.any(ink_color > 255)):
        raise ValueError("Les données du rendu approuvé sont incohérentes.")
    flat = palette[lookup[labels]].astype(np.uint8)
    with Image.open(assets["mystery"]) as image:
        mystery = np.array(image.convert("RGB"))
    with Image.open(assets["solution_flat"]) as image:
        old_flat = np.array(image.convert("RGB"))
    if mystery.shape != flat.shape or not np.array_equal(old_flat, flat):
        raise ValueError("Les images ne correspondent pas à la carte du rendu approuvé.")
    mask = ink.astype(bool)
    final = apply_ink(flat, mask, ink_color, composition="multiply")
    corrected_reveals = progressive_reveal(mystery, final, labels, steps)
    locked_keys = set(files) - mutable
    locked_hashes = {key: _sha(assets[key]) for key in sorted(locked_keys)}
    correction = {
        "scope": "final_render_only", "page_composition": "darken",
        "solution_composition": "multiply", "source_metadata_sha256": _sha(meta_path),
        "locked_files_sha256": locked_hashes,
        "locked_arrays_sha256": {key: _array_sha(arrays[key]) for key in sorted(_ARRAYS)},
        "outside_ink_mask_unchanged": True,
    }
    arrays["ink_composition"] = np.array("multiply")
    quality = metadata.get("quality", {})
    if not isinstance(quality, dict):
        raise ValueError("Le contrôle qualité du rendu est invalide.")
    quality["render_correction"] = correction
    metadata["quality"] = quality
    metadata["render_correction"] = correction
    metadata["rendering_note"] = (
        "Correction du rendu colorié uniquement. Photos, dessin, masquage, zones, "
        "palette et positions des codes conservés. La page garde sa composition "
        "darken ; la solution utilise multiply sur la même encre préimprimée. "
        "Aucun nouveau détail ni trait n'est ajouté.")
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".tmp_render_", dir=destination_root))
    destination = destination_root / f"run_{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        save_rgb(temporary / files["solution_final"], final)
        for key, image in corrected_reveals.items():
            save_rgb(temporary / files[f"reveal_{key}"], image)
        with (temporary / files["regions"]).open("wb") as handle:
            np.savez_compressed(handle, **arrays)
        (temporary / files["quality"]).write_text(
            json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        (temporary / files["metadata"]).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        if any(_sha(temporary / files[key]) != digest for key, digest in locked_hashes.items()):
            raise ValueError("Un fichier approuvé a été modifié pendant la correction.")
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {**{key: str(destination / name) for key, name in files.items()}, "run_dir": str(destination)}
