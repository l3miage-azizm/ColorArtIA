"""Dense adult-colouring cells without changing a single final colour.

Existing colour regions are subdivided by a deterministic watershed. Every
child keeps its parent's palette index, remains connected and must fit its real
printed code. A requested density is a target, never a fabricated region count.
"""
from __future__ import annotations

from typing import Callable
import heapq
import math

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage.measure import label as connected_label
from skimage.segmentation import watershed


def _normalize(regions: np.ndarray, mapping: dict[int, int]):
    if not isinstance(regions, np.ndarray) or regions.ndim != 2 or not regions.size:
        raise ValueError("La carte des régions doit être une image non vide à deux dimensions.")
    if not np.issubdtype(regions.dtype, np.integer) or np.any(regions <= 0):
        raise ValueError("Chaque pixel du dessin doit appartenir à une région positive.")
    if regions.size > 12_000_000:
        raise ValueError("La carte des régions dépasse la limite de 12 millions de pixels.")
    unique = np.unique(regions)
    if any(int(rid) not in mapping for rid in unique):
        raise ValueError("Une région ne possède pas de couleur dans le nuancier.")
    if any(isinstance(mapping[int(rid)], (bool, np.bool_)) or
           not isinstance(mapping[int(rid)], (int, np.integer)) or mapping[int(rid)] < 0 for rid in unique):
        raise ValueError("Les indices du nuancier doivent être des entiers positifs ou nuls.")
    # An input label may accidentally name disconnected islands. Split them
    # explicitly rather than reporting a single code for several closed cells.
    labels = connected_label(regions, background=0, connectivity=1).astype(np.int32)
    ids, first = np.unique(labels, return_index=True)
    original_ids = regions.ravel()[first]
    colors = {int(rid): int(mapping[int(old)]) for rid, old in zip(ids, original_ids)}
    return labels, colors


def _allocate(areas: np.ndarray, target: int, eligible: set[int] | None = None) -> dict[int, int]:
    """Allocate additional cells proportionally; the exact sum is the target."""
    count = len(areas) - 1
    extra = max(0, target - count)
    weights = areas[1:].astype(np.float64)
    if eligible is not None:
        weights = np.where(np.isin(np.arange(1, len(areas)), list(eligible)), weights, 0)
        if not weights.any():
            return {rid: 1 for rid in range(1, count + 1)}
    shares = weights / max(1, weights.sum()) * extra
    allocation = np.floor(shares).astype(np.int32) + 1
    left = target - int(allocation.sum())
    if left > 0:
        # Stable ties avoid depending on unstable floating point sort choices.
        order = np.lexsort((np.arange(count), -(shares - np.floor(shares))))
        allocation[order[:left]] += 1
    return {rid: int(allocation[rid - 1]) for rid in range(1, count + 1)}


def _seed_markers(mask: np.ndarray, distance: np.ndarray, wanted: int,
                  rng: np.random.Generator, page_width: int) -> np.ndarray:
    """Blue-noise-like seeds in the mask, without a visible square grid."""
    markers = np.zeros(mask.shape, np.int32)
    safety = max(2.0, page_width / 1240 * 2.8)
    candidates = np.argwhere(distance >= safety)
    if len(candidates) < 2:
        return markers
    pool_size = min(len(candidates), max(1024, min(18000, wanted * 40)))
    if len(candidates) > pool_size:
        candidates = candidates[rng.choice(len(candidates), pool_size, replace=False)]
    count = min(wanted, len(candidates))
    candidates = candidates.astype(np.float32)
    # Start at a random well-inside point. Farthest-point updates distribute
    # seeds across branches and holes while keeping a deterministic result.
    initial = int(rng.integers(len(candidates)))
    minimum = np.full(len(candidates), np.inf, np.float32)
    chosen = initial
    for marker in range(1, count + 1):
        y, x = candidates[chosen].astype(np.int32)
        markers[y, x] = marker
        squared = np.sum((candidates - candidates[chosen]) ** 2, axis=1)
        minimum = np.minimum(minimum, squared)
        minimum[chosen] = -1
        chosen = int(minimum.argmax())
        if minimum[chosen] < 1:
            break
    return markers


def _clean_children(local: np.ndarray, mask: np.ndarray, color: int,
                    fitted: Callable) -> tuple[np.ndarray, int, bool]:
    removed = 0
    cross = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    for _ in range(12):
        ids = np.unique(local)
        ids = ids[ids > 0]
        child_mapping = {int(rid): color for rid in ids}
        layouts = fitted(local, child_mapping)
        missing = [int(rid) for rid in ids if int(rid) not in layouts]
        if not missing:
            return local, removed, False
        changed = False
        for child in missing:
            child_mask = local == child
            if not child_mask.any():
                continue
            ring = cv2.dilate(child_mask.astype(np.uint8), cross).astype(bool) & ~child_mask & mask
            neighbours = local[ring]
            neighbours = neighbours[neighbours > 0]
            if not neighbours.size:
                continue
            choices, edges = np.unique(neighbours, return_counts=True)
            # Growing an already printable neighbour keeps its valid code.
            target = min(zip(choices.tolist(), edges.tolist()),
                         key=lambda pair: (pair[0] not in layouts, -pair[1], pair[0]))[0]
            local[child_mask] = target
            removed += 1
            changed = True
        if not changed:
            break
    ids = np.unique(local)
    ids = ids[ids > 0]
    if len(fitted(local, {int(rid): color for rid in ids})) == len(ids):
        return local, removed, False
    # The parent was checked before subdivision; keeping it is a safe fallback.
    return np.where(mask, 1, 0).astype(np.int32), removed + max(0, len(ids) - 1), True


def _cap_density(labels, mapping, source_parents, target):
    """Merge only surplus sibling cells; an existing printable code stays valid."""
    count = len(mapping)
    if count <= target:
        return labels, mapping, 0
    size = count + 1
    graph = [dict() for _ in range(size)]
    for a, b in ((labels[:, :-1], labels[:, 1:]), (labels[:-1, :], labels[1:, :])):
        different = a != b
        left, right = a[different].astype(np.int64), b[different].astype(np.int64)
        pairs, amounts = np.unique(np.minimum(left, right) * size + np.maximum(left, right), return_counts=True)
        for pair, amount in zip(pairs.tolist(), amounts.tolist()):
            x, y = divmod(pair, size)
            if source_parents[x] == source_parents[y]:
                graph[x][y] = graph[x].get(y, 0) + amount
                graph[y][x] = graph[y].get(x, 0) + amount
    areas = np.bincount(labels.ravel(), minlength=size)
    parents = np.arange(size, dtype=np.int32)
    heap = [(int(areas[rid]), rid) for rid in range(1, size)]
    heapq.heapify(heap)
    active = count
    while heap and active > target:
        old_area, rid = heapq.heappop(heap)
        if parents[rid] != rid or old_area != areas[rid] or not graph[rid]:
            continue
        neighbour = min(graph[rid], key=lambda other: (-graph[rid][other], -int(areas[other]), other))
        parents[rid] = neighbour
        areas[neighbour] += areas[rid]
        areas[rid] = 0
        for other, shared in list(graph[rid].items()):
            graph[other].pop(rid, None)
            if other != neighbour:
                graph[neighbour][other] = graph[neighbour].get(other, 0) + shared
                graph[other][neighbour] = graph[other].get(neighbour, 0) + shared
        graph[neighbour].pop(rid, None)
        graph[rid] = {}
        active -= 1
        heapq.heappush(heap, (int(areas[neighbour]), neighbour))
    for rid in range(1, size):
        ancestor = rid
        while parents[ancestor] != ancestor:
            ancestor = int(parents[ancestor])
        parents[rid] = ancestor
    joined = parents[labels]
    unique, inverse = np.unique(joined, return_inverse=True)
    final_mapping = {index: mapping[int(old)] for index, old in enumerate(unique, 1)}
    return inverse.reshape(labels.shape).astype(np.int32) + 1, final_mapping, count - len(unique)


def densify_regions(regions: np.ndarray, mapping: dict[int, int], config,
                    number_layouts: Callable, target_regions: int = 2800,
                    seed: int = 42, *, _preserve_microdetails: bool = False):
    """Return ``(labels, palette_mapping, density_report)`` for a dense page.

    Call after initial colour cleanup and final palette reindexing, before print
    upscaling. ``number_layouts`` must be the same real-glyph fitter used by the
    final page; padding crop checks to the full working width preserves its
    physical font size and final-resolution projection rules.
    """
    if isinstance(target_regions, bool) or not isinstance(target_regions, (int, np.integer)) or not 1 <= target_regions <= 12000:
        raise ValueError("La densité doit être une cible entière entre 1 et 12 000 régions.")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or not 0 <= seed < 2**31:
        raise ValueError("La graine de densité doit être un entier positif inférieur à 2³¹.")
    if not callable(number_layouts):
        raise ValueError("Le contrôle de placement des codes est requis.")
    labels, colors = _normalize(regions, mapping)
    original_layouts = number_layouts(labels, colors, config)
    if len(original_layouts) != len(colors) and not _preserve_microdetails:
        raise ValueError("Le dessin source contient des zones sans code lisible ; simplifiez-les avant la densification.")
    natural_count = len(colors)
    requested = int(target_regions)
    actual_target = max(natural_count, requested)
    # Narrow cells removed by real-glyph fitting are expected. Plan extra seeds
    # first, then remove only surplus sibling cells so the public target remains
    # meaningful without ever reducing the printed code size.
    planning_target = min(12000, natural_count + math.ceil((actual_target - natural_count) * 1.8))
    areas = np.bincount(labels.ravel())
    allocations = _allocate(areas, planning_target,
                            set(original_layouts) if _preserve_microdetails else None)
    output = np.zeros_like(labels)
    result_mapping: dict[int, int] = {}
    source_parents: dict[int, int] = {}
    next_id = 1
    split_parents = fallback_parents = removed = proposed = 0
    page_width = labels.shape[1]

    def fitted(local, local_mapping):
        # Keep the page's physical code size without allocating and scanning a
        # full-width zero border for every small subdivision candidate.
        return number_layouts(local, local_mapping, config, reference_width=page_width)

    for rid, sl in enumerate(ndi.find_objects(labels), 1):
        if sl is None:
            continue
        mask = labels[sl] == rid
        local = np.where(mask, 1, 0).astype(np.int32)
        wanted = allocations[rid]
        if wanted > 1 and (not _preserve_microdetails or rid in original_layouts):
            rng = np.random.default_rng(int(seed) + rid * 104729)
            distance = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
            markers = _seed_markers(mask, distance, wanted, rng, page_width)
            if markers.max() > 1:
                proposed += int(markers.max()) - 1
                elevation = ndi.gaussian_filter(rng.random(mask.shape).astype(np.float32), sigma=max(2, page_width / 1240 * 3.5))
                local = watershed(elevation, markers, mask=mask, connectivity=1,
                                  compactness=.0005).astype(np.int32)
                local, lost, fallback = _clean_children(local, mask, colors[rid], fitted)
                removed += lost
                fallback_parents += int(fallback)
        child_ids = np.unique(local)
        child_ids = child_ids[child_ids > 0]
        split_parents += int(len(child_ids) > 1)
        for child in child_ids:
            output[sl][local == child] = next_id
            result_mapping[next_id] = colors[rid]
            source_parents[next_id] = rid
            next_id += 1

    output, result_mapping, merged_for_target = _cap_density(output, result_mapping, source_parents, actual_target)

    # Final global fitting is the authoritative count; never make up requested
    # regions or silently emit an unnumbered cell to satisfy a density target.
    layouts = number_layouts(output, result_mapping, config)
    if (len(layouts) != len(result_mapping) and not _preserve_microdetails) or np.any(output <= 0):
        raise ValueError("Le découpage dense n'a pas satisfait le contrôle de tous les codes.")
    actual = len(result_mapping)
    warnings = []
    if actual < requested * .85:
        warnings.append("La densité demandée a été limitée pour conserver des zones fermées et des codes lisibles.")
    details = {"method": "same_color_watershed", "requested_regions": requested,
               "planned_regions": planning_target,
               "actual_regions": actual, "natural_regions": natural_count,
               "subdivided_parents": split_parents, "fallback_parents": fallback_parents,
               "proposed_additional_regions": proposed, "merged_for_readability": removed,
               "merged_for_density_target": merged_for_target,
               "label_coverage": len(layouts) / max(1, actual), "seed": int(seed),
               "preserves_final_colors": True, "warnings": warnings}
    if _preserve_microdetails:
        details.update(microparents_unchanged=True,
                       eligible_main_parents=len(original_layouts),
                       guide_only_parent_regions=len(colors)-len(original_layouts))
    return output, result_mapping, details


def densify_with_microdetails(regions: np.ndarray, mapping: dict[int, int], config,
                             number_layouts: Callable, target_regions: int = 3200,
                             seed: int = 42):
    """Subdivide printable parents, keeping delicate uncodeable shapes whole.

    Those original shapes are retained for enlarged code guides. They never
    receive a fabricated main-page code or get merged just to satisfy a count.
    """
    return densify_regions(regions, mapping, config, number_layouts,
                           target_regions, seed, _preserve_microdetails=True)
