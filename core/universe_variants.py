"""Instant universe previews and palette-only variants of a canonical drawing.

The expensive geometry is computed once in the ``original`` universe. Every
subsequent colour treatment uses precisely the same regions, codes, worksheet
and preprinted ink. No photographic texture or new detail enters this path.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

import cv2
import numpy as np
from PIL import Image

from .engine import progressive_reveal, save_rgb
from .illustration import apply_ink
from .palette import PaletteSpec, palette_preview
from .universes import UNIVERSES, apply_universe


PREVIEW_INDEX = "studio_index.png"
PREVIEW_METADATA = "studio_preview.json"
MAX_PIXELS = 12_000_000
MAX_METADATA_BYTES = 1_000_000
MAX_ARRAY_BYTES = 160 * 1024 * 1024
_REQUIRED = {"labels", "region_to_color", "palette", "ink_mask", "ink_color"}
_LOCKED_ARRAYS = {"labels", "region_to_color", "ink_mask", "ink_color", "ink_composition"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_name(root: Path, name: str) -> str:
    if not isinstance(name, str) or "\\" in name or "\x00" in name:
        raise ValueError("Un fichier du dessin est invalide.")
    supplied = Path(name)
    candidate = supplied if supplied.is_absolute() else root / supplied
    if ".." in supplied.parts or not candidate.resolve().is_relative_to(root):
        raise ValueError("Un fichier du dessin est hors de son dossier.")
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Les liens symboliques ne sont pas autorisés.")
    return candidate.relative_to(root).as_posix()


def _asset(root: Path, name: str) -> Path:
    candidate = root / _relative_name(root, name)
    if not candidate.is_file():
        raise ValueError("Un fichier du dessin est absent.")
    return candidate


def _load(run_dir: Path):
    supplied = Path(run_dir)
    if supplied.is_symlink() or not supplied.is_dir():
        raise ValueError("Le dessin de référence est introuvable.")
    root = supplied.resolve()
    meta_path = _asset(root, "metadata.json")
    if meta_path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("Les métadonnées du dessin sont trop volumineuses.")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        files = metadata["files"]
        if not isinstance(files, dict) or not {"regions", "mystery", "metadata"} <= set(files):
            raise ValueError
        if _asset(root, files["metadata"]) != meta_path:
            raise ValueError
        arrays_path = _asset(root, files["regions"])
        with zipfile.ZipFile(arrays_path) as archive:
            if sum(member.file_size for member in archive.infolist()) > MAX_ARRAY_BYTES:
                raise ValueError
        with np.load(arrays_path, allow_pickle=False) as archive:
            arrays = {key: archive[key].copy() for key in archive.files}
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("Les données du dessin sont illisibles ou incomplètes.") from exc
    if not _REQUIRED <= set(arrays):
        raise ValueError("La carte des zones du dessin est incomplète.")
    labels, lookup, palette = arrays["labels"], arrays["region_to_color"], arrays["palette"]
    ink, ink_color = arrays["ink_mask"], arrays["ink_color"]
    if (labels.ndim != 2 or not 0 < labels.size <= MAX_PIXELS
            or not np.issubdtype(labels.dtype, np.integer) or np.any(labels < 0)
            or lookup.ndim != 1 or not lookup.size or labels.max() >= lookup.size
            or not np.issubdtype(lookup.dtype, np.integer)
            or palette.ndim != 2 or palette.shape[1:] != (3,) or not 1 <= len(palette) <= 64
            or not np.issubdtype(palette.dtype, np.integer)
            or np.any(palette < 0) or np.any(palette > 255)
            or np.any(lookup < 0) or np.any(lookup >= len(palette))
            or ink.shape != labels.shape or not np.all((ink == 0) | (ink == 1))
            or ink_color.shape != (3,) or not np.issubdtype(ink_color.dtype, np.integer)
            or np.any(ink_color < 0) or np.any(ink_color > 255)):
        raise ValueError("Les régions, les couleurs et l’encre du dessin sont incohérentes.")
    try:
        composition = str(arrays.get("ink_composition", np.array("multiply")).item())
    except (ValueError, TypeError) as exc:
        raise ValueError("La composition de l’encre est invalide.") from exc
    if composition not in {"multiply", "darken", "replace"}:
        raise ValueError("La composition de l’encre est invalide.")
    declared_palette = np.asarray(metadata.get("palette"))
    if not np.array_equal(declared_palette, palette):
        raise ValueError("Le nuancier et la carte des zones ne correspondent pas.")
    return root, metadata, arrays, composition


def _canonical_palette(metadata: dict, arrays: dict) -> np.ndarray:
    if metadata.get("universe") == "original":
        return arrays["palette"].astype(np.uint8)
    raw = metadata.get("studio_base_palette")
    if metadata.get("studio_base_universe") != "original" or raw is None:
        raise ValueError("Un dessin fidèle à la photo doit être créé avant les univers rapides.")
    palette = np.asarray(raw)
    if (palette.shape != arrays["palette"].shape or not np.issubdtype(palette.dtype, np.integer)
            or np.any(palette < 0) or np.any(palette > 255)):
        raise ValueError("Le nuancier de référence est invalide.")
    return palette.astype(np.uint8)


def prepare_preview_bundle(run_dir: Path, *, max_width: int = 800) -> None:
    """Add a compact indexed preview and all exact theme palettes to a run.

    ``studio_index.png`` is opaque RGB data, not an artwork: red is the palette
    index, green is 255 for preprinted ink and zero otherwise, blue is zero.
    JavaScript reconstructs palette[red], then applies the declared ink formula.
    Palette values are computed here by the same colour function as final files.
    """
    if type(max_width) is not int or not 32 <= max_width <= 1600:
        raise ValueError("La largeur de l’aperçu doit être comprise entre 32 et 1600 pixels.")
    root, metadata, arrays, composition = _load(run_dir)
    for key, name in metadata["files"].items():
        relative = _relative_name(root, name)
        if key not in {"studio_index", "studio_preview"} and relative in {PREVIEW_INDEX, PREVIEW_METADATA}:
            raise ValueError("Un aperçu recouvre un fichier du dessin.")
    canonical = _canonical_palette(metadata, arrays)
    labels, lookup = arrays["labels"], arrays["region_to_color"]
    height, width = labels.shape
    preview_width = min(width, max_width)
    preview_height = max(1, round(height * preview_width / width))
    # Shared nearest-neighbour coordinates keep both channels on exactly the
    # same original pixel. No antialiasing of numeric palette indices is legal.
    xs = np.minimum(width - 1, np.arange(preview_width) * width // preview_width)
    ys = np.minimum(height - 1, np.arange(preview_height) * height // preview_height)
    sample_labels = labels[np.ix_(ys, xs)]
    encoded = np.zeros((preview_height, preview_width, 3), np.uint8)
    encoded[:, :, 0] = lookup[sample_labels]
    encoded[:, :, 1] = arrays["ink_mask"][np.ix_(ys, xs)].astype(np.uint8) * 255
    palettes = {key: apply_universe(canonical.reshape(-1, 1, 3), key).reshape(-1, 3).tolist()
                for key in UNIVERSES}
    preview = {"schema_version": 1, "width": preview_width, "height": preview_height,
               "source_width": width, "source_height": height,
               "palettes": palettes, "ink_color": arrays["ink_color"].astype(int).tolist(),
               "ink_composition": composition, "base_universe": "original",
               "sampling": "nearest", "index_channel": 0, "ink_channel": 1}
    Image.fromarray(encoded).save(root / PREVIEW_INDEX, format="PNG", optimize=True)
    (root / PREVIEW_METADATA).write_text(json.dumps(preview, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    metadata["files"]["studio_index"] = PREVIEW_INDEX
    metadata["files"]["studio_preview"] = PREVIEW_METADATA
    metadata["studio_base_palette"] = canonical.tolist()
    metadata["studio_base_universe"] = "original"
    (root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def recolor_project(source_run: Path, work_dir: Path, universe: str) -> dict[str, str]:
    """Create a fully printable variant without segmenting or placing codes.

    The canonical palette remains available in metadata, so repeated calls from
    an already themed run never compound treatments. The copied worksheet is
    byte-identical; only colour-bearing outputs and their metadata are replaced.
    """
    if universe not in UNIVERSES:
        raise ValueError("L’univers sélectionné n’existe pas.")
    source, metadata, arrays, composition = _load(source_run)
    canonical = _canonical_palette(metadata, arrays)
    original_files = metadata["files"]
    required = {"mystery", "solution_flat", "solution_final", "palette", "regions", "quality", "metadata"}
    if not required <= set(original_files):
        raise ValueError("Le dessin de référence est incomplet.")
    declared = {key: _relative_name(source, name) for key, name in original_files.items()}
    if len(set(declared.values())) != len(declared):
        raise ValueError("Deux fichiers du dessin utilisent le même chemin.")
    # Cloud callers need only hydrate these three files. Every other coloured
    # output is reconstructed below, avoiding downloads of obsolete previews.
    assets = {key: source / name for key, name in declared.items() if (source / name).is_file()}
    for key in ("mystery", "regions", "metadata"):
        if key not in assets:
            raise ValueError("Le dessin de référence est incomplet.")
    regenerated = required | {key for key in declared if key.startswith("reveal_")}
    relative_files = {key: name for key, name in declared.items() if key in regenerated or key in assets}
    target_root = Path(work_dir).resolve()
    if target_root == source or target_root.is_relative_to(source):
        raise ValueError("Le nouveau rendu doit être enregistré hors du dessin de référence.")
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".tmp_universe_", dir=target_root))
    destination = target_root / ("run_" + uuid.uuid4().hex)
    locked_mystery = _sha(assets["mystery"])
    try:
        for name in relative_files.values():
            (staging / name).parent.mkdir(parents=True, exist_ok=True)
        for key, path in assets.items():
            if key in regenerated and key not in {"mystery", "metadata", "regions"}:
                continue
            target = staging / relative_files[key]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(path, target)
        palette = apply_universe(canonical.reshape(-1, 1, 3), universe).reshape(-1, 3)
        arrays["palette"] = palette
        labels, lookup = arrays["labels"], arrays["region_to_color"]
        flat = palette[lookup[labels]]
        final = apply_ink(flat, arrays["ink_mask"].astype(bool), arrays["ink_color"], composition=composition)
        with Image.open(assets["mystery"]) as image:
            mystery = np.array(image.convert("RGB"))
        if mystery.shape != flat.shape:
            raise ValueError("Le masque ne correspond pas aux régions du dessin.")
        save_rgb(staging / relative_files["solution_flat"], flat)
        save_rgb(staging / relative_files["solution_final"], final)
        save_rgb(staging / relative_files["palette"], palette_preview(PaletteSpec("adaptive", palette.tolist())))
        steps = tuple(metadata.get("config", {}).get("reveal_steps", (.25, .5, .75, 1.0)))
        if (not 1 <= len(steps) <= 10 or any(type(s) not in {float, int} or not 0 <= s <= 1 for s in steps)
                or len({round(s * 100) for s in steps}) != len(steps)):
            raise ValueError("Les étapes de révélation du dessin sont invalides.")
        revealed = progressive_reveal(mystery, final, labels, steps)
        for key, image in revealed.items():
            logical = f"reveal_{key}"
            relative_files.setdefault(logical, f"reveal_{key}.png")
            save_rgb(staging / relative_files[logical], image)
        with (staging / relative_files["regions"]).open("wb") as handle:
            np.savez_compressed(handle, **arrays)
        reuse = {"method": "canonical_palette_only_v1", "base_universe": "original",
                 "geometry_reused": True, "codes_reused": True,
                 "source_metadata_sha256": _sha(source / "metadata.json"),
                 "worksheet_sha256": locked_mystery,
                 "locked_arrays_sha256": {key: hashlib.sha256(arrays[key].tobytes()).hexdigest()
                                          for key in sorted(_LOCKED_ARRAYS & set(arrays))}}
        metadata = deepcopy(metadata)
        metadata["universe"] = universe
        metadata["palette"] = palette.tolist()
        metadata["n_colors"] = len(palette)
        metadata["files"] = relative_files
        metadata["studio_base_palette"] = canonical.tolist()
        metadata["studio_base_universe"] = "original"
        metadata["universe_reuse"] = reuse
        quality = metadata.get("quality", {})
        if not isinstance(quality, dict):
            raise ValueError("Le contrôle du dessin est invalide.")
        base_error = quality.pop("mean_rgb_error", None)
        quality["universe_reuse"] = reuse
        quality["universe_color_measurement"] = {"method": "palette_only",
                                                "base_mean_rgb_error": base_error,
                                                "geometry_error": 0}
        metadata["rendering_note"] = (
            "Les univers modifient uniquement les couleurs du dessin de référence. "
            "Masquage, zones, codes et encre préimprimée restent strictement identiques. "
            "Le résultat colorié se reconstruit depuis cette même carte et ce nuancier.")
        (staging / relative_files["quality"]).write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        # Keep the preview independent of which universe was most recently saved.
        prepare_preview_bundle(staging)
        if _sha(staging / relative_files["mystery"]) != locked_mystery:
            raise ValueError("Le masque de référence a été modifié.")
        os.replace(staging, destination)
        final_files = json.loads((destination / "metadata.json").read_text(encoding="utf-8"))["files"]
        return {**{key: str(destination / name) for key, name in final_files.items()}, "run_dir": str(destination)}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
