"""Details survive code fitting; cropped fitting keeps printed physical size."""
import numpy as np

from core import engine
from core.detail_preservation import restore_critical_shapes
from core.density import densify_with_microdetails


def test_cropped_fitter_is_identical_to_full_width_padding():
    labels = np.zeros((110, 95), np.int32)
    labels[3:80, 4:40] = 1
    labels[30:100, 43:90] = 2
    cfg = engine.GenerationConfig(target_width=320, target_height=450, working_width=160)
    mapping = {1: 0, 2: 7}
    expected = engine.number_layouts(np.pad(labels, ((0, 0), (0, 65))), mapping, cfg)
    actual = engine.number_layouts(labels, mapping, cfg, reference_width=160)
    assert actual == expected


def test_avoided_pen_stroke_keeps_codes_inside_their_actual_region():
    labels = np.ones((140, 200), np.int32)
    ink = np.zeros_like(labels, bool)
    ink[:, 80:120] = True
    cfg = engine.GenerationConfig(target_width=200, target_height=280)
    layouts = engine.number_layouts(labels, {1: 0}, cfg, avoid_mask=ink)
    layout = layouts[1]
    glyph = engine._number_glyph(layout['text'], layout['scale'], layout['thickness'], layout['rotation'])
    yy, xx = np.nonzero(glyph)
    yy, xx = yy + layout['box'][1], xx + layout['box'][0]
    assert np.all(labels[yy, xx] == 1)
    assert not ink[yy, xx].any()


def test_complete_critical_shape_restored_without_rectangular_roi_seam(monkeypatch):
    from core import detail_preservation
    natural = np.ones((80, 100), np.int32)
    natural[22:48, 30:65] = 2
    natural[29:40, 38:55] = 3
    coarse = np.ones_like(natural)
    monkeypatch.setattr(detail_preservation, '_critical_boxes',
                        lambda rgb: ([[40, 30, 45, 36]], []))
    result, mapping, report = restore_critical_shapes(
        np.zeros((*natural.shape, 3), np.uint8), natural, {1: 0, 2: 1, 3: 2}, coarse, {1: 0})
    colors = np.array([mapping[rid] for rid in range(1, int(result.max()) + 1)])
    actual = colors[result - 1]
    assert np.all(actual[natural == 3] == 2)
    assert np.all(actual[natural != 3] == 0)
    assert report['redraws_features'] is False


def test_density_does_not_merge_uncodeable_microshape_or_change_flat_colors():
    labels = np.ones((160, 180), np.int32)
    labels[75:79, 85:89] = 2
    mapping = {1: 0, 2: 1}
    palette = [(220, 180, 160), (15, 20, 30)]
    cfg = engine.GenerationConfig(target_width=360, target_height=510, working_width=180)
    before = engine.render_coloring_solution(labels, mapping, palette)
    after, new_mapping, report = densify_with_microdetails(
        labels, mapping, cfg, engine.number_layouts, target_regions=100)
    assert np.array_equal(before, engine.render_coloring_solution(after, new_mapping, palette))
    assert len(np.unique(after[75:79, 85:89])) == 1
    assert report['microparents_unchanged'] is True
    assert report['guide_only_parent_regions'] == 1
