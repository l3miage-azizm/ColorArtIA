"""The artistic catalogue contains actual settings, separate from colour LUTs."""
from dataclasses import FrozenInstanceError
import re

import pytest

from core.art_universes import (
    ART_DIRECTION, ART_UNIVERSES, NEW_UNIVERSE_KEYS, art_universe_catalog,
    build_art_prompt, is_art_universe,
)


def test_eighteen_worlds_include_exactly_fifteen_supplemental_worlds():
    assert len(ART_UNIVERSES) == 18
    assert len(NEW_UNIVERSE_KEYS) == len(set(NEW_UNIVERSE_KEYS)) == 15
    assert set(ART_UNIVERSES) - set(NEW_UNIVERSE_KEYS) == {
        "art_royal", "art_floral", "art_noel",
    }
    assert len({world.label for world in ART_UNIVERSES.values()}) == 18
    assert all(re.fullmatch(r"art_[a-z_]+", key) for key in ART_UNIVERSES)


def test_public_catalogue_labels_worlds_and_never_returns_prompts():
    catalogue = art_universe_catalog()
    assert [entry["key"] for entry in catalogue] == list(ART_UNIVERSES)
    assert all(entry["kind"] == "artistic" for entry in catalogue)
    assert sum(entry["is_new"] for entry in catalogue) == 15
    assert all(re.fullmatch(r"#[0-9a-f]{6}", entry["accent"]) for entry in catalogue)
    assert all("prompt" not in entry for entry in catalogue)
    catalogue[0]["label"] = "Client mutation"
    assert art_universe_catalog()[0]["label"] == "Royal / Château"


def test_catalogue_entries_are_immutable():
    with pytest.raises(FrozenInstanceError):
        ART_UNIVERSES["art_royal"].label = "Changed"


@pytest.mark.parametrize("key", ART_UNIVERSES)
def test_every_world_requests_a_distinct_real_scene_and_preserves_identity(key):
    prompt = build_art_prompt(key)
    assert prompt.startswith(ART_DIRECTION)
    assert "Replacing and enriching the background is explicitly" in prompt
    assert "facial proportions" in prompt and "actual age" in prompt
    assert "Do not enlarge eyes" in prompt and "Keep faces unobstructed" in prompt
    assert "24–32" in prompt and "clean, continuous black pen contours" in prompt
    assert "no recognizable franchise characters" in prompt
    assert len(ART_UNIVERSES[key].prompt) > 250
    assert len(ART_UNIVERSES[key].description) > 45


def test_distinctive_scene_elements_are_not_just_colour_names():
    expected = {
        "art_royal": "castle", "art_floral": "pergola", "art_noel": "chalet",
        "art_fairy": "tree roots", "art_celestial": "orrery",
        "art_ocean_deep": "coral", "art_safari": "acacia",
        "art_adventure": "stone terraces", "art_steampunk": "clockwork",
        "art_artnouveau": "wrought-iron", "art_japanese": "wooden",
        "art_mediterranean": "terracotta", "art_fantasy": "citadel",
        "art_winterpalace": "ice", "art_masquerade": "canal",
        "art_circus": "carousel", "art_space_explorer": "rover",
        "art_bohogarden": "woven",
    }
    for key, element in expected.items():
        assert element in " ".join(ART_UNIVERSES[key].prompt.split())


@pytest.mark.parametrize("key", ["original", "royal", "pastel", "golden", "unknown", "", None, [], {}])
def test_palette_keys_and_unknown_values_are_not_artistic_worlds(key):
    assert is_art_universe(key) is False
    with pytest.raises(ValueError, match="univers artistique"):
        build_art_prompt(key)
