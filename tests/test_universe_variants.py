"""Universes reuse one approved worksheet and reconstruct their exact colours."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from core.universe_variants import prepare_preview_bundle, recolor_project
from core.universes import UNIVERSES, apply_universe


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def canonical_run(tmp_path):
    root = tmp_path / "base"
    root.mkdir()
    height, width = 220, 160
    y, x = np.mgrid[:height, :width]
    labels = (1 + (x >= width//2) + 2*(y >= height//2)).astype(np.uint16)
    lookup = np.array([0, 0, 1, 2, 3], np.int16)
    palette = np.array([[20, 40, 80], [220, 160, 90], [150, 80, 190], [255, 255, 255]], np.uint8)
    ink = ((x == width//3) | (y == height//3)).astype(bool)
    ink_color = np.array([190, 130, 210], np.uint8)
    flat = palette[lookup[labels]]
    final = flat.copy()
    final[ink] = ((flat[ink].astype(np.uint16)*ink_color.astype(np.uint16)+127)//255).astype(np.uint8)
    mystery = np.full_like(flat, 255)
    mystery[ink] = ink_color
    files = {"source": "00_source.png", "original": "01_original.jpg",
             "mystery": "02_mystere_total.png", "solution_flat": "03_solution_aplats.png",
             "solution_final": "04_solution_finale_dessin.png", "palette": "05_palette.png",
             "regions": "regions.npz", "quality": "quality.json", "metadata": "metadata.json"}
    for name, content in (("source", flat), ("original", flat), ("mystery", mystery),
                          ("solution_flat", flat), ("solution_final", final), ("palette", flat[:12])):
        Image.fromarray(content).save(root / files[name])
    for percentage in (25, 50, 75, 100):
        files[f"reveal_{percentage}"] = f"reveal_{percentage}.png"
        Image.fromarray(final).save(root / files[f"reveal_{percentage}"])
    np.savez_compressed(root / "regions.npz", labels=labels, region_to_color=lookup,
                        palette=palette, ink_mask=ink, ink_color=ink_color,
                        ink_composition=np.array("multiply"))
    quality = {"errors": [], "warnings": [], "label_coverage": 1,
               "unlabeled_regions": 0, "status": "ready_for_review", "mean_rgb_error": 12.5}
    (root / "quality.json").write_text(json.dumps(quality))
    metadata = {"universe": "original", "palette": palette.tolist(), "palette_codes": ["1", "2", "3", "4"],
                "n_regions": 4, "n_colors": 4, "files": files, "quality": quality, "warnings": [],
                "config": {"n_colors": 4, "target_width": width, "target_height": height,
                           "reveal_steps": [.25, .5, .75, 1.0]},
                "number_layouts": {str(i): {"text": str(i), "box": [10*i, 10, 10*i+5, 18]} for i in range(1, 5)}}
    (root / "metadata.json").write_text(json.dumps(metadata))
    return root


def load_arrays(root):
    with np.load(root / "regions.npz", allow_pickle=False) as archive:
        return {key: archive[key].copy() for key in archive.files}


@pytest.mark.parametrize("universe", list(UNIVERSES))
def test_every_universe_keeps_mask_geometry_codes_and_exact_reconstruction(canonical_run, tmp_path, universe):
    source = canonical_run
    prepare_preview_bundle(source, max_width=100)
    base_arrays = load_arrays(source)
    base_meta = json.loads((source / "metadata.json").read_text())
    source_hashes = {p.name: sha(p) for p in source.iterdir()}
    result = recolor_project(source, tmp_path / "work", universe)
    output = Path(result["run_dir"])
    arrays = load_arrays(output)
    metadata = json.loads((output / "metadata.json").read_text())
    expected_palette = apply_universe(base_arrays["palette"].reshape(-1, 1, 3), universe).reshape(-1, 3)
    assert np.array_equal(arrays["palette"], expected_palette)
    for name in ("labels", "region_to_color", "ink_mask", "ink_color", "ink_composition"):
        assert arrays[name].dtype == base_arrays[name].dtype
        assert arrays[name].shape == base_arrays[name].shape
        assert arrays[name].tobytes() == base_arrays[name].tobytes()
    assert sha(output / base_meta["files"]["mystery"]) == source_hashes[base_meta["files"]["mystery"]]
    assert metadata["number_layouts"] == base_meta["number_layouts"]
    assert metadata["palette_codes"] == base_meta["palette_codes"]
    expected = expected_palette[arrays["region_to_color"][arrays["labels"]]]
    expected[arrays["ink_mask"]] = ((expected[arrays["ink_mask"]].astype(np.uint16) *
                                    arrays["ink_color"].astype(np.uint16)+127)//255).astype(np.uint8)
    actual = np.array(Image.open(result["solution_final"]).convert("RGB"))
    assert np.array_equal(actual, expected)
    assert metadata["quality"]["label_coverage"] == 1
    assert metadata["quality"]["errors"] == []
    assert {p.name: sha(p) for p in source.iterdir()} == source_hashes


def test_preview_numeric_channels_reconstruct_the_same_sampled_pixels(canonical_run):
    prepare_preview_bundle(canonical_run, max_width=80)
    arrays = load_arrays(canonical_run)
    preview = json.loads((canonical_run / "studio_preview.json").read_text())
    encoded = np.array(Image.open(canonical_run / "studio_index.png").convert("RGB"))
    assert encoded.shape == (110, 80, 3)
    assert set(np.unique(encoded[:, :, 1])) <= {0, 255}
    assert not encoded[:, :, 2].any()
    ink_color = np.asarray(preview["ink_color"], np.uint16)
    for universe in UNIVERSES:
        palette = np.asarray(preview["palettes"][universe], np.uint8)
        canvas = palette[encoded[:, :, 0]]
        mask = encoded[:, :, 1] != 0
        canvas[mask] = ((canvas[mask].astype(np.uint16)*ink_color+127)//255).astype(np.uint8)
        full = palette[arrays["region_to_color"][arrays["labels"]]]
        full[arrays["ink_mask"]] = ((full[arrays["ink_mask"]].astype(np.uint16)*ink_color+127)//255).astype(np.uint8)
        assert np.array_equal(canvas, full[::2, ::2])


def test_repeated_themes_always_start_from_the_original_palette(canonical_run, tmp_path):
    original = load_arrays(canonical_run)["palette"]
    first = Path(recolor_project(canonical_run, tmp_path / "work", "pastel")["run_dir"])
    second = Path(recolor_project(first, tmp_path / "work", "sepia")["run_dir"])
    expected = apply_universe(original.reshape(-1, 1, 3), "sepia").reshape(-1, 3)
    assert np.array_equal(load_arrays(second)["palette"], expected)
    preview = json.loads((second / "studio_preview.json").read_text())
    assert preview["palettes"]["original"] == original.tolist()
    assert preview["palettes"]["sepia"] == expected.tolist()


def test_invalid_palette_is_rejected_before_an_output_is_published(canonical_run, tmp_path):
    meta_path = canonical_run / "metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["palette"][0][0] = 256
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="nuancier"):
        recolor_project(canonical_run, tmp_path / "work", "pastel")
    assert not (tmp_path / "work").exists()


def test_old_theme_without_canonical_palette_cannot_pretend_to_be_original(canonical_run):
    path = canonical_run / "metadata.json"
    meta = json.loads(path.read_text())
    meta["universe"] = "pastel"
    path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="fidèle"):
        prepare_preview_bundle(canonical_run)


def test_regions_symlink_is_rejected(canonical_run, tmp_path):
    path = canonical_run / "regions.npz"
    external = tmp_path / "other.npz"
    path.rename(external)
    path.symlink_to(external)
    with pytest.raises(ValueError):
        prepare_preview_bundle(canonical_run)


def test_cloud_fastpath_only_requires_metadata_regions_and_worksheet(canonical_run, tmp_path):
    prepare_preview_bundle(canonical_run)
    original_mystery = sha(canonical_run / "02_mystere_total.png")
    for path in canonical_run.iterdir():
        if path.name not in {"metadata.json", "regions.npz", "02_mystere_total.png"}:
            path.unlink()
    result = recolor_project(canonical_run, tmp_path / "work", "ocean")
    metadata = json.loads(Path(result["metadata"]).read_text())
    assert {"mystery", "regions", "solution_final", "palette", "studio_index", "studio_preview"} <= set(metadata["files"])
    assert "source" not in metadata["files"]
    assert "original" not in metadata["files"]
    assert sha(result["mystery"]) == original_mystery
    for path in result.values():
        assert Path(path).exists()


def test_output_palette_alias_cannot_replace_an_approved_input(canonical_run, tmp_path):
    path = canonical_run / "metadata.json"
    meta = json.loads(path.read_text())
    meta["files"]["palette"] = meta["files"]["source"]
    path.write_text(json.dumps(meta))
    source_hash = sha(canonical_run / "00_source.png")
    with pytest.raises(ValueError, match="même chemin"):
        recolor_project(canonical_run, tmp_path / "work", "pastel")
    assert sha(canonical_run / "00_source.png") == source_hash
