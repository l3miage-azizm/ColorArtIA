"""Scene boundaries, illustration reuse and provider gating through real APIs.

Only the paid illustration service is simulated; segmentation and saved artwork
remain real. These checks do not establish image-provider visual quality.
"""
import hashlib
import json
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw
import pytest

import atelier
import cloud.web as cloud_web
import core.artist as artist
from cloud.state import Snapshot
from core.studio_catalog import art_service_enabled, resolve_selection
from test_cloud_http import cloud_http, new_book, upload


@pytest.mark.parametrize("key", ["art_fairy__golden", "art_royal", "original", "ocean"])
def test_only_declared_selections_are_accepted(key):
    assert resolve_selection(key).key == key


@pytest.mark.parametrize("key", ["unknown", "original__golden", "art_fairy__bad", "art_fairy__ocean__golden", "../art_fairy"])
def test_arbitrary_scene_requests_are_rejected(key):
    with pytest.raises(ValueError):
        resolve_selection(key)


def test_original_palette_suffix_is_canonicalized():
    assert resolve_selection("art_fairy__original").key == "art_fairy"


def test_gateway_requires_operator_readiness_flag(monkeypatch):
    monkeypatch.setattr(artist, "premium_available", lambda: True)
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "vercel_gateway")
    monkeypatch.delenv("COLORART_ARTISTIC_READY", raising=False)
    assert not art_service_enabled()
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "1")
    assert art_service_enabled()
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "0")
    assert not art_service_enabled()


def test_missing_provider_never_substitutes_colour_filter_for_art(cloud_http, monkeypatch):
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "0")
    monkeypatch.setattr(atelier, "generate_project", lambda *a, **k: pytest.fail("Unavailable art must not run local posterization"))
    client, _, _ = cloud_http
    book = upload(client, new_book(client)["id"])
    response = client.post(f"/api/books/{book['id']}/photos/{book['pages'][0]['id']}/generate",
                           json={"universe": "art_fairy", "artwork_mode": "local"})
    assert response.status_code == 503
    assert client.get(f"/api/books/{book['id']}").json()["pages"][0]["variants"] == []


def test_worlds_have_separate_geometry_and_saved_scene_reuses_illustration_offline(cloud_http, monkeypatch):
    client, store, _ = cloud_http
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "1")
    monkeypatch.setattr(artist, "premium_available", lambda: True)
    art_calls = []

    def illustrated(source, output, universe_key="original"):
        art_calls.append(universe_key)
        output = Path(output); output.mkdir(parents=True)
        image = Image.open(source).convert("RGB")
        draw = ImageDraw.Draw(image)
        if universe_key == "art_fairy":
            draw.rectangle((0, 0, 100, image.height), fill=(30, 100, 55), outline="black", width=5)
        else:
            draw.ellipse((20, 20, 500, 310), fill=(115, 43, 175), outline="black", width=5)
        path = output / "illustration.png"; image.save(path)
        return {"path": str(path), "provenance": {"provider": "test", "universe_key": universe_key}}

    monkeypatch.setattr(artist, "create_illustration", illustrated)
    book = upload(client, new_book(client)["id"])
    book_id, page_id = book["id"], book["pages"][0]["id"]
    settings = {"n_colors": 16, "target_regions": 600, "masking": "mystery", "artwork_mode": "local"}

    def generate(key, **changes):
        response = client.post(f"/api/books/{book_id}/photos/{page_id}/generate", json={**settings, **changes, "universe": key})
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["status"] == "succeeded", job
        current = client.get(f"/api/books/{book_id}").json()["pages"][0]
        variant = next(v for v in current["variants"] if v["id"] == job["result"]["variant_id"])
        return job, variant

    _, fairy = generate("art_fairy")
    _, palace = generate("art_royal")
    assert art_calls == ["art_fairy", "art_royal"]
    assert fairy["config"]["artwork_mode"] == "premium"
    assert fairy["config"]["art_universe"] == "art_fairy"
    assert fairy["config"]["difficulty"] == "expert"
    assert fairy["config"]["preserve_details"] is True
    assert fairy["config"]["n_segments"] == 2000
    assert fairy["config"]["min_region_area"] == 85
    fairy_mask = client.get(fairy["files"]["mystery"]).content
    palace_mask = client.get(palace["files"]["mystery"]).content
    assert hashlib.sha256(fairy_mask).digest() != hashlib.sha256(palace_mask).digest()
    metadata = client.get(fairy["files"]["metadata"]).json()
    assert metadata["art_universe"] == "art_fairy" and metadata["input_role"] == "illustration"
    assert metadata["quality"]["masking"]["mode"] == "mystery"

    # No provider is needed to recolour or reopen an existing scene.
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "0")
    with monkeypatch.context() as offline:
        offline.setattr(atelier, "art_service_enabled", lambda: pytest.fail("Cached recolour must not wait for provider readiness"))
        _, warm_fairy = generate("art_fairy__golden")
    assert warm_fairy["config"]["palette_universe"] == "golden"
    assert client.get(warm_fairy["files"]["mystery"]).content == fairy_mask
    for key in fairy["files"]:
        if key.startswith("detail_guide"):
            assert key in warm_fairy["files"]
            assert client.get(warm_fairy["files"][key]).content == client.get(fairy["files"][key]).content
    offline_new = client.post(f"/api/books/{book_id}/photos/{page_id}/generate", json={**settings, "universe": "art_space_explorer"})
    assert offline_new.status_code == 503
    with monkeypatch.context() as offline:
        offline.setattr(atelier, "art_service_enabled", lambda: pytest.fail("Cached scene must not wait for provider readiness"))
        repeated, repeated_fairy = generate("art_fairy")
    assert repeated["result"]["cache_hit"] is True and repeated_fairy["id"] == fairy["id"]
    # A legacy failed segmentation must still be recoverable from its good
    # saved input illustration. Store that legacy state in the real database.
    with tempfile.TemporaryDirectory() as folder:
        snapshot = Snapshot(client.cookies.get(atelier.COOKIE), Path(folder) / "atelier", store).load()
        library = snapshot.library()
        with library._db(write=True) as database:
            for variant in (fairy, warm_fairy):
                old_config = {**variant["config"], "preserve_details": False}
                old_quality = {**variant["quality"], "errors": ["Ancien découpage incomplet"]}
                database.execute("UPDATE variants SET config=?, quality=? WHERE id=?",
                                 (json.dumps(old_config), json.dumps(old_quality), variant["id"]))
        snapshot.commit()
    # Different settings segment the SAME saved illustration, preserving faces.
    _, denser_fairy = generate("art_fairy", target_regions=650)
    assert len(art_calls) == 2
    assert client.get(denser_fairy["files"]["source"]).content == client.get(fairy["files"]["source"]).content


def test_operator_probe_is_private_and_filters_status_fields(cloud_http, monkeypatch):
    client, store, _ = cloud_http
    monkeypatch.setenv("COLORART_STORAGE_BRIDGE_SECRET", "operator-test-secret")
    monkeypatch.setattr(cloud_web, "artist_service_status", lambda: {
        "available": False, "provider": "vercel_gateway", "reason": "insufficient_funds",
        "action_url": None, "balance": "123", "credential": "must never appear"})
    assert client.get("/api/art-service").status_code == 404
    assert client.get("/api/art-service", headers={"Authorization": "Bearer incorrect"}).status_code == 404
    response = client.get("/api/art-service", headers={"Authorization": "Bearer operator-test-secret"})
    assert response.status_code == 200
    assert response.json() == {"available": False, "provider": "vercel_gateway", "reason": "insufficient_funds", "action_url": None}
    assert store.get_calls == [] and store.put_calls == []
