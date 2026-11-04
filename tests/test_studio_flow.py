"""Batch import and one-click universes through real cloud HTTP requests.

Only remote byte storage is simulated. Drawing creation, recolouring, indexed
previews, source decoding and the persistent library are their real code paths.
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest

import atelier
from cloud.store import PrivateBlobStore
from cloud.web import app
from core.illustration import apply_ink
from core.universes import UNIVERSES, apply_universe
import core.universe_variants as universe_variants
from test_cloud_http import BASE_URL, HttpBlobStore, new_book, photo_bytes


class BatchHttpBlobStore(HttpBlobStore):
    namespace = staticmethod(PrivateBlobStore.namespace)
    read_upload = PrivateBlobStore.read_upload

    def __init__(self):
        super().__init__()
        self.reservations = {}
        self.prepared_batches = []

    def prepare_uploads(self, owner, book_id, files):
        namespace = self.namespace(owner)
        self.prepared_batches.append((owner, book_id, len(files)))
        uploads = []
        for item in files:
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[item["content_type"]]
            pathname = f"{namespace}/uploads/{book_id}/{uuid.uuid4().hex}{suffix}"
            self.reservations[pathname] = dict(item)
            uploads.append({"pathname": pathname, "filename": item["filename"],
                "upload_url": "https://upload.test/" + uuid.uuid4().hex,
                "headers": {"Content-Type": item["content_type"]}})
        return {"uploads": uploads}

    def upload_reserved(self, pathname, body):
        reservation = self.reservations.get(pathname)
        if reservation is None or len(body) > reservation["size"]:
            raise HTTPException(403, "Transfer outside its reservation")
        self.put(pathname, body, create_only=True, content_type=reservation["content_type"])


@pytest.fixture
def studio(monkeypatch):
    store = BatchHttpBlobStore()
    monkeypatch.setattr(app.state, "store_factory", lambda: store, raising=False)
    with TestClient(app, base_url=BASE_URL) as client:
        yield client, store


def prepare(client, book_id, bodies):
    response = client.post(f"/api/books/{book_id}/uploads/prepare-batch", json={
        "files": [{"filename": f"souvenir-{index}.png", "content_type": "image/png", "size": len(body)}
                  for index, body in enumerate(bodies)]})
    assert response.status_code == 200, response.text
    return response.json()["uploads"]


def import_batch(client, book_id, prepared):
    return client.post(f"/api/books/{book_id}/photos/import-batch", json={
        "files": [{"pathname": item["pathname"], "filename": item["filename"]} for item in prepared]})


def asset(client, variant, key):
    response = client.get(variant["files"][key])
    assert response.status_code == 200, response.text
    return response.content


def arrays(client, variant):
    with np.load(BytesIO(asset(client, variant, "regions")), allow_pickle=False) as archive:
        return {key: archive[key].copy() for key in archive.files}


def image(body):
    with Image.open(BytesIO(body)) as value:
        return np.array(value.convert("RGB"))


def test_three_photo_batch_then_three_universes_reuse_real_geometry(studio, monkeypatch):
    client, store = studio
    book = new_book(client, count=3)
    book_id = book["id"]
    bodies = [photo_bytes(index) for index in range(3)]
    store.put_calls.clear()
    prepared = prepare(client, book_id, bodies)
    assert len(prepared) == 3
    assert len({item["pathname"] for item in prepared}) == 3
    assert not any(path.endswith("/head.json") for path in store.put_calls), "Signing a batch is not an edit"
    for item, body in zip(prepared, bodies):
        store.upload_reserved(item["pathname"], body)
    store.put_calls.clear()
    imported = import_batch(client, book_id, prepared)
    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert result["errors"] == [] and len(result["added"]) == 3
    assert result["book"]["photo_count"] == 3
    assert [page["filename"] for page in result["book"]["pages"]] == [item["filename"] for item in prepared]
    assert len([path for path in store.put_calls if path.endswith("/head.json")]) == 1
    book = client.get(f"/api/books/{book_id}").json()
    assert book["photo_count"] == 3

    with TestClient(app, base_url=BASE_URL) as outsider:
        assert outsider.get(f"/api/books/{book_id}").status_code == 404
        assert outsider.post(f"/api/books/{book_id}/uploads/prepare-batch", json={"files": [
            {"filename": "other.png", "content_type": "image/png", "size": len(bodies[0])}]}).status_code == 404
        assert import_batch(outsider, book_id, prepared[:1]).status_code == 404
        other = new_book(outsider)
        assert import_batch(outsider, other["id"], prepared[:1]).status_code == 404
        assert outsider.get(f"/api/books/{other['id']}").json()["photo_count"] == 0

    generation_calls, recolour_calls = [], []
    real_generate, real_recolour = atelier.generate_project, universe_variants.recolor_project

    def counted_generate(*args, **kwargs):
        generation_calls.append((args, kwargs))
        return real_generate(*args, **kwargs)

    def counted_recolour(*args, **kwargs):
        recolour_calls.append((args, kwargs))
        return real_recolour(*args, **kwargs)

    monkeypatch.setattr(atelier, "generate_project", counted_generate)
    monkeypatch.setattr(universe_variants, "recolor_project", counted_recolour)
    page_id = book["pages"][0]["id"]
    settings = {"n_colors": 16, "difficulty": "balanced", "fit_mode": "contain",
                "target_regions": 600, "masking": "mystery", "artwork_mode": "local"}
    variants = {}
    # Start with a non-original universe, as a first-click user would. The
    # server must create its canonical base once and return the requested theme.
    for universe in ("ocean", "original", "golden"):
        response = client.post(f"/api/books/{book_id}/photos/{page_id}/generate",
                               json={**settings, "universe": universe})
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["status"] == "succeeded", job
        restored = client.get(f"/api/books/{book_id}").json()
        page = next(page for page in restored["pages"] if page["id"] == page_id)
        variants[universe] = next(v for v in page["variants"] if v["id"] == job["result"]["variant_id"])
        assert variants[universe]["universe"] == universe
    assert len(generation_calls) == 1
    assert len(recolour_calls) == 2

    baseline = arrays(client, variants["original"])
    worksheet_sha = hashlib.sha256(asset(client, variants["original"], "mystery")).hexdigest()
    base_palette = baseline["palette"]
    for universe, variant in variants.items():
        actual = arrays(client, variant)
        for key in ("labels", "region_to_color", "ink_mask", "ink_color", "ink_composition"):
            assert np.array_equal(actual[key], baseline[key]), (universe, key)
        assert hashlib.sha256(asset(client, variant, "mystery")).hexdigest() == worksheet_sha
        expected_palette = apply_universe(base_palette.reshape(-1, 1, 3), universe).reshape(-1, 3)
        assert np.array_equal(actual["palette"], expected_palette)
        flat = actual["palette"][actual["region_to_color"][actual["labels"]]]
        expected_final = apply_ink(flat, actual["ink_mask"].astype(bool), actual["ink_color"],
                                   composition=str(actual["ink_composition"].item()))
        final = image(asset(client, variant, "solution_final"))
        assert np.array_equal(final, expected_final)

        preview = json.loads(asset(client, variant, "studio_preview"))
        assert set(preview["palettes"]) == set(UNIVERSES)
        for key in UNIVERSES:
            expected = apply_universe(base_palette.reshape(-1, 1, 3), key).reshape(-1, 3)
            assert np.array_equal(preview["palettes"][key], expected)
        indexed = image(asset(client, variant, "studio_index"))
        width, height = preview["width"], preview["height"]
        assert indexed.shape == (height, width, 3)
        xs = np.arange(width) * preview["source_width"] // width
        ys = np.arange(height) * preview["source_height"] // height
        sampled_labels = actual["labels"][np.ix_(ys, xs)]
        assert np.array_equal(indexed[:, :, 0], actual["region_to_color"][sampled_labels])
        assert np.array_equal(indexed[:, :, 1], actual["ink_mask"][np.ix_(ys, xs)].astype(np.uint8) * 255)
        assert not indexed[:, :, 2].any()
        reconstructed = np.array(preview["palettes"][universe], dtype=np.uint8)[indexed[:, :, 0]]
        reconstructed = apply_ink(reconstructed, indexed[:, :, 1] > 0, preview["ink_color"],
                                  composition=preview["ink_composition"])
        assert np.array_equal(reconstructed, final[np.ix_(ys, xs)])

    existing_ids = {v["id"] for v in page["variants"]}
    repeated = client.post(f"/api/books/{book_id}/photos/{page_id}/generate",
                           json={**settings, "universe": "ocean"})
    assert repeated.status_code == 202, repeated.text
    cached = repeated.json()
    assert cached["status"] == "succeeded" and cached["result"]["cache_hit"] is True
    assert cached["result"]["variant_id"] == variants["ocean"]["id"]
    current = client.get(f"/api/books/{book_id}").json()
    current_page = next(p for p in current["pages"] if p["id"] == page_id)
    assert {v["id"] for v in current_page["variants"]} == existing_ids
    assert len(generation_calls) == 1 and len(recolour_calls) == 2


def test_batch_keeps_valid_photos_and_reports_duplicate_photos_once(studio):
    client, store = studio
    book = new_book(client, count=3)
    bodies = [photo_bytes(0), photo_bytes(1), photo_bytes(0)]
    prepared = prepare(client, book["id"], bodies)
    for item, body in zip(prepared, bodies):
        store.upload_reserved(item["pathname"], body)
    store.put_calls.clear()
    response = import_batch(client, book["id"], prepared)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["book"]["photo_count"] == 2 and len(value["added"]) == 2
    assert len(value["errors"]) == 1
    assert value["errors"][0]["filename"] == prepared[2]["filename"]
    assert "déjà" in value["errors"][0]["message"]
    assert len([path for path in store.put_calls if path.endswith("/head.json")]) == 1
    restored = client.get(f"/api/books/{book['id']}").json()
    assert restored["photo_count"] == 2
    oversize = client.post(f"/api/books/{book['id']}/uploads/prepare-batch", json={"files": [
        {"filename": "extra.png", "content_type": "image/png", "size": len(bodies[0])} for _ in range(9)]})
    assert oversize.status_code == 422
