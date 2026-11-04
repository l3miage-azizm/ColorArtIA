"""Real HTTP-to-persistence checks for the Vercel adapter.

Blob storage is an atomic in-memory fake; the drawing engine and PDF exporter
are the actual application implementations, exercised at A4 resolution.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json

from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
import numpy as np
from PIL import Image, ImageDraw
import pypdfium2 as pdfium
import pytest

from cloud.state import Snapshot
from cloud.web import app
from test_cloud_state import AtomicBlobStore


BASE_URL = "https://atelier.test"


class HttpBlobStore(AtomicBlobStore):
    def asset_response(self, library, book_id, relative):
        # Production redirects to a short-lived private Blob URL. This local
        # response verifies that the committed bytes really hydrate correctly.
        return FileResponse(library.safe_asset(book_id, relative))


@pytest.fixture
def cloud_http(monkeypatch):
    store = HttpBlobStore()
    roots = []
    original_load = Snapshot.load

    def tracked_load(snapshot):
        roots.append(snapshot.root)
        return original_load(snapshot)

    monkeypatch.setattr(Snapshot, "load", tracked_load)
    monkeypatch.setattr(app.state, "store_factory", lambda: store, raising=False)
    with TestClient(app, base_url=BASE_URL) as client:
        yield client, store, roots
    assert len(roots) == len(set(roots)), "Requests reused a temporary state directory"
    assert all(not root.exists() for root in roots), "A response leaked its temporary files"


def photo_bytes(index=0):
    image = Image.new("RGBA", (840, 1120), (244, 236, 215, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 650, 840, 1120), fill=(72 + index * 24, 127, 87, 255))
    draw.ellipse((120, 120, 720, 730), fill=(225, 171 - index * 25, 119, 255))
    draw.rectangle((180, 650, 660, 1060), fill=(72, 119 + index * 25, 152, 255))
    draw.ellipse((260, 330, 325, 375), fill=(40, 43, 46, 255))
    draw.ellipse((515, 330, 580, 375), fill=(40, 43, 46, 255))
    draw.rounded_rectangle((310, 500, 540, 550), radius=22, fill=(155, 75, 71, 255))
    draw.rectangle((30, 40, 110, 610), fill=(100, 135, 86, 180))
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def new_book(client, count=1):
    response = client.post("/api/books", json={
        "title": "Les souvenirs d’Élodie", "recipient": "Élodie",
        "template": "heritage", "target_photos": count,
        "dedication": "Pour toi, avec affection.",
    })
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, book_id, count=1):
    response = client.post(f"/api/books/{book_id}/photos", files=[
        ("files", (f"souvenir-{index}.png", photo_bytes(index), "image/png"))
        for index in range(count)
    ])
    assert response.status_code == 200, response.text
    assert response.json()["errors"] == []
    return response.json()["book"]


def test_catalogue_security_headers_and_awaited_progress_contract(cloud_http):
    client, store, roots = cloud_http
    response = client.get("/api/catalog")
    assert response.status_code == 200
    data = response.json()
    assert len(data["universes"]) == 33
    assert sum(item["kind"] == "artistic" for item in data["universes"]) == 18
    assert sum(item.get("is_new", False) for item in data["universes"]) == 15
    assert len(data["templates"]) == 3
    assert data["direct_uploads"] is True
    assert data["generation_mode"] == "awaited"
    assert data["print_configured"] is False
    assert data["max_upload_mb"] == 20
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "https://*.blob.vercel-storage.com" in policy
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["hosting"] == "vercel"
    # Static/catalogue requests must not fetch or create an owner database.
    assert store.get_calls == [] and store.put_calls == [] and roots == []


def test_each_request_restores_metadata_and_order_from_committed_blobs(cloud_http):
    client, _, roots = cloud_http
    assert client.get("/api/books").json() == {"books": []}
    book = new_book(client, count=2)
    book = upload(client, book["id"], count=2)
    order = [page["id"] for page in book["pages"]]
    changed = client.patch(f"/api/books/{book['id']}", json={"dedication": "Un cadeau unique, pour toi."})
    assert changed.status_code == 200, changed.text
    response = client.post(f"/api/books/{book['id']}/pages/reorder", json={"page_ids": order[::-1]})
    assert response.status_code == 200, response.text
    restored = client.get(f"/api/books/{book['id']}").json()
    assert restored["dedication"] == "Un cadeau unique, pour toi."
    assert [page["id"] for page in restored["pages"]] == order[::-1]
    assert restored["photo_count"] == 2
    for page in restored["pages"]:
        response = client.get(page["source_url"])
        assert response.status_code == 200, response.text
        with Image.open(BytesIO(response.content)) as image:
            assert image.size == (840, 1120)
    assert len(roots) >= 8
    assert len(roots) == len(set(roots))
    assert all(not root.exists() for root in roots)


def test_owner_isolation_and_cross_origin_mutations_are_enforced(cloud_http):
    client, _, _ = cloud_http
    book = upload(client, new_book(client)["id"])
    source_url = book["pages"][0]["source_url"]
    with TestClient(app, base_url=BASE_URL) as outsider:
        assert outsider.get("/api/books").json()["books"] == []
        assert outsider.get(f"/api/books/{book['id']}").status_code == 404
        assert outsider.get(source_url).status_code == 404
        assert outsider.patch(f"/api/books/{book['id']}", json={"title": "Forbidden"}).status_code == 404
    refused = client.patch(f"/api/books/{book['id']}", json={"title": "Forbidden"},
                           headers={"Origin": "https://hostile.example"})
    assert refused.status_code == 403
    assert client.get(f"/api/books/{book['id']}").json()["title"] == book["title"]


def test_real_render_favorite_validation_and_pdf_export_survive_fresh_requests(cloud_http):
    client, _, roots = cloud_http
    book = upload(client, new_book(client)["id"])
    book_id, page_id = book["id"], book["pages"][0]["id"]
    response = client.post(f"/api/books/{book_id}/photos/{page_id}/generate", json={
        "universe": "original", "n_colors": 16, "difficulty": "balanced",
        "fit_mode": "contain", "target_regions": 600, "masking": "mystery",
        "artwork_mode": "local",
    })
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "succeeded", job
    assert job["progress"] == 100
    persisted_job = client.get(f"/api/jobs/{job['id']}").json()
    assert persisted_job["status"] == "succeeded"
    assert persisted_job["result"] == job["result"]
    book = client.get(f"/api/books/{book_id}").json()
    variant = book["pages"][0]["variants"][0]
    metadata_response = client.get(variant["files"]["metadata"])
    assert metadata_response.status_code == 200
    metadata = metadata_response.json()
    assert metadata["config"]["target_width"] == 2480
    assert metadata["config"]["target_height"] == 3508
    assert metadata["config"]["target_regions"] == 600
    assert metadata["quality"]["label_coverage"] == 1
    assert metadata["quality"]["errors"] == []
    final = client.get(variant["files"]["solution_final"])
    assert final.status_code == 200
    with Image.open(BytesIO(final.content)) as image:
        assert image.size == (2480, 3508)
    arrays = client.get(variant["files"]["regions"])
    assert arrays.status_code == 200
    with np.load(BytesIO(arrays.content), allow_pickle=False) as archive:
        assert archive["labels"].shape == (3508, 2480)
        assert len(np.unique(archive["labels"])) == metadata["n_regions"]
    favorite = client.post(f"/api/books/{book_id}/photos/{page_id}/variants/{variant['id']}/favorite")
    assert favorite.status_code == 200, favorite.text
    selected = client.post(f"/api/books/{book_id}/photos/{page_id}/variants/{variant['id']}/select",
                           json={"accept_warnings": True})
    assert selected.status_code == 200, selected.text
    restored = client.get(f"/api/books/{book_id}").json()
    assert restored["pages"][0]["variants"][0]["favorite"] is True
    assert restored["pages"][0]["selected_variant_id"] == variant["id"]
    ready = client.get(f"/api/books/{book_id}/readiness").json()
    assert ready["ready"] is True
    exported = client.post(f"/api/books/{book_id}/exports", json={"approved_revision": restored["revision"]})
    assert exported.status_code == 201, exported.text
    export = exported.json()
    assert export["print_configured"] is False
    latest = client.get(f"/api/books/{book_id}/exports")
    assert latest.status_code == 200, latest.text
    assert latest.json()["id"] == export["id"]
    for key, expected_pages in (("interior_pdf", 4), ("solutions_pdf", 1), ("cover_pdf", 1)):
        downloaded = client.get(export["files"][key])
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content.startswith(b"%PDF-")
        document = pdfium.PdfDocument(downloaded.content)
        try:
            assert len(document) == expected_pages
        finally:
            document.close()
    changed = client.patch(f"/api/books/{book_id}", json={"dedication": "Une version plus récente."})
    assert changed.status_code == 200
    assert client.get(f"/api/books/{book_id}/exports").status_code == 409
    listed = client.get("/api/books").json()["books"]
    assert listed[0]["id"] == book_id and listed[0]["selected_count"] == 1
    assert all(not root.exists() for root in roots)
