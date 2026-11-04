"""Persistence guarantees across independent Vercel invocations.

The in-memory Blob fake enforces conditional writes, including a successfully
committed write whose response is lost. Image rendering is intentionally outside
these tests: the subject is snapshot publication and cross-request isolation.
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from PIL import Image
import pytest

from cloud.state import BlobValue, CloudConflict, CloudUnavailable, Snapshot
from core.jobs import Registry
from core.library import NotFound


OWNER_A = "a" * 64
OWNER_B = "b" * 64


class AtomicBlobStore:
    """A shared remote service, not an invocation-local cache."""

    def __init__(self):
        self.objects = {}
        self.get_calls = []
        self.put_calls = []
        self._counter = 0
        self._lock = threading.Lock()
        self.failure = None

    def _fail(self, phase, path):
        if self.failure and self.failure(phase, path):
            self.failure = None
            raise CloudUnavailable("Simulated transport interruption")

    def get(self, path):
        self.get_calls.append(path)
        self._fail("before_get", path)
        with self._lock:
            value = self.objects.get(path)
            return None if value is None else BlobValue(value.body, value.etag)

    def put(self, path, body, *, if_match=None, create_only=False,
            content_type="application/octet-stream"):
        data = body.read_bytes() if isinstance(body, Path) else bytes(body)
        self.put_calls.append(path)
        self._fail("before_put", path)
        with self._lock:
            previous = self.objects.get(path)
            if create_only and previous is not None:
                raise CloudConflict("Object already exists")
            if if_match is not None and (previous is None or previous.etag != if_match):
                raise CloudConflict("ETag no longer matches")
            self._counter += 1
            result = BlobValue(data, f'"etag-{self._counter}"')
            self.objects[path] = result
        self._fail("after_put", path)
        return result


def owner_head(owner=OWNER_A):
    digest = hashlib.sha256(owner.encode()).hexdigest()
    return f"owners/{digest}/head.json"


def open_invocation(tmp_path, name, store, owner=OWNER_A):
    snapshot = Snapshot(owner, tmp_path / name, store)
    snapshot.load()
    library = snapshot.library()
    registry = Registry(library.root)
    return snapshot, library, registry


def photo(color="red"):
    output = BytesIO()
    Image.new("RGB", (1200, 1600), color).save(output, format="PNG")
    return output.getvalue()


def create_book(library, registry, count=1, owner=OWNER_A):
    book = library.create_book("Les souvenirs d’Élodie", recipient="Élodie", target_photos=count)
    registry.assign(book["id"], owner)
    return book


def make_variant(library, book_id, page_id):
    run = library.work_dir / "transaction-test-run"
    run.mkdir()
    files = {
        "mystery": "02_mystere_total.png",
        "solution_final": "04_solution_finale_dessin.png",
        "palette": "05_palette.png",
        "metadata": "metadata.json",
    }
    for key, filename in files.items():
        if key != "metadata":
            Image.new("RGB", (64, 90), "white").save(run / filename)
    (run / "metadata.json").write_text(json.dumps({
        "files": files,
        "config": {"n_colors": 24},
        "quality": {"status": "passed", "errors": [], "warnings": [],
                    "label_coverage": 1.0, "unlabeled_regions": 0},
    }), encoding="utf-8")
    return library.add_variant(book_id, page_id, run, "original")


def test_both_sqlite_databases_and_assets_survive_a_fresh_invocation(tmp_path):
    store = AtomicBlobStore()
    first, library, registry = open_invocation(tmp_path, "first", store)
    book = create_book(library, registry)
    page = library.add_photo(book["id"], photo(), "cadeau.png")
    variant = make_variant(library, book["id"], page["id"])
    library.toggle_favorite(book["id"], page["id"], variant["id"])
    library.select_variant(book["id"], page["id"], variant["id"], accept_warnings=True)
    job = registry.create_job(OWNER_A, book["id"], page["id"])
    registry.update_job(job["id"], status="succeeded", progress=100,
                        result={"variant_id": variant["id"]})
    expected_source = library.safe_asset(book["id"], page["source_path"]).read_bytes()
    first.commit()

    second, restored, restored_registry = open_invocation(tmp_path, "second", store)
    actual = restored.get_book(book["id"])
    assert actual["title"] == "Les souvenirs d’Élodie"
    assert actual["pages"][0]["selected_variant_id"] == variant["id"]
    assert actual["pages"][0]["variants"][0]["favorite"] is True
    assert restored_registry.own(book["id"], OWNER_A)
    assert restored_registry.get_job(job["id"], OWNER_A)["result"] == {"variant_id": variant["id"]}
    assert restored.safe_asset(book["id"], page["source_path"]).read_bytes() == expected_source


def test_wal_transactions_are_included_in_published_sqlite_backup(tmp_path):
    store = AtomicBlobStore()
    snapshot, library, registry = open_invocation(tmp_path, "writer", store)
    # Keep a connection open so closing Library's writer does not checkpoint
    # and remove the WAL before snapshot.commit() runs.
    hold = sqlite3.connect(library.database)
    hold.execute("PRAGMA journal_mode=WAL")
    hold.execute("SELECT COUNT(*) FROM books").fetchone()
    try:
        book = create_book(library, registry)
        library.update_book(book["id"], dedication="Toutes les transactions ♥")
        wal = Path(str(library.database) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        snapshot.commit()
    finally:
        hold.close()
    _, restored, _ = open_invocation(tmp_path, "reader", store)
    assert restored.get_book(book["id"])["dedication"] == "Toutes les transactions ♥"


def test_owner_namespaces_do_not_share_books_or_asset_access(tmp_path):
    store = AtomicBlobStore()
    first, library, registry = open_invocation(tmp_path, "alice", store)
    book = create_book(library, registry)
    page = library.add_photo(book["id"], photo())
    first.commit()
    second, other, other_registry = open_invocation(tmp_path, "bob", store, OWNER_B)
    assert other.list_books() == []
    assert other_registry.own(book["id"], OWNER_B) is False
    with pytest.raises(NotFound):
        other.safe_asset(book["id"], page["source_path"])
    own_book = create_book(other, other_registry, owner=OWNER_B)
    second.commit()
    assert owner_head(OWNER_A) in store.objects
    assert owner_head(OWNER_B) in store.objects
    _, alice_again, _ = open_invocation(tmp_path, "alice-again", store)
    assert [row["id"] for row in alice_again.list_books()] == [book["id"]]
    with pytest.raises(NotFound):
        alice_again.get_book(own_book["id"])


def test_competing_updates_cannot_silently_overwrite_a_committed_edit(tmp_path):
    store = AtomicBlobStore()
    initial, library, registry = open_invocation(tmp_path, "initial", store)
    book = create_book(library, registry)
    initial.commit()
    left, left_lib, _ = open_invocation(tmp_path, "left", store)
    right, right_lib, _ = open_invocation(tmp_path, "right", store)
    left_lib.update_book(book["id"], title="Version gagnante")
    right_lib.update_book(book["id"], dedication="Modification concurrente")
    left.commit()
    with pytest.raises(CloudConflict):
        right.commit()
    _, current, _ = open_invocation(tmp_path, "current", store)
    actual = current.get_book(book["id"])
    assert actual["title"] == "Version gagnante"
    assert actual["dedication"] == ""


def test_concurrent_first_writes_use_create_only_head(tmp_path):
    store = AtomicBlobStore()
    left, left_lib, left_registry = open_invocation(tmp_path, "first-left", store)
    right, right_lib, right_registry = open_invocation(tmp_path, "first-right", store)
    book = create_book(left_lib, left_registry)
    create_book(right_lib, right_registry)
    left.commit()
    with pytest.raises(CloudConflict):
        right.commit()
    _, restored, _ = open_invocation(tmp_path, "first-reader", store)
    assert [row["id"] for row in restored.list_books()] == [book["id"]]


def test_upload_failure_before_head_publication_keeps_the_previous_snapshot(tmp_path):
    store = AtomicBlobStore()
    initial, library, registry = open_invocation(tmp_path, "before-failure", store)
    book = create_book(library, registry)
    initial.commit()
    old_head = store.objects[owner_head()]
    update, changed, _ = open_invocation(tmp_path, "failing-writer", store)
    changed.add_photo(book["id"], photo("blue"))
    store.failure = lambda phase, path: phase == "before_put" and not path.endswith("/head.json")
    with pytest.raises(CloudUnavailable):
        update.commit()
    assert store.objects[owner_head()] == old_head
    _, restored, _ = open_invocation(tmp_path, "after-failure", store)
    assert restored.get_book(book["id"])["photo_count"] == 0


def test_successful_head_write_with_lost_response_is_recognized_as_committed(tmp_path):
    store = AtomicBlobStore()
    snapshot, library, registry = open_invocation(tmp_path, "lost-response", store)
    book = create_book(library, registry)
    store.failure = lambda phase, path: phase == "after_put" and path.endswith("/head.json")
    token = snapshot.commit()
    assert token
    _, restored, _ = open_invocation(tmp_path, "lost-response-reader", store)
    assert [row["id"] for row in restored.list_books()] == [book["id"]]
    head_writes = [path for path in store.put_calls if path.endswith("/head.json")]
    assert len(head_writes) == 1


def test_readiness_checks_do_not_download_every_selected_image(tmp_path):
    store = AtomicBlobStore()
    first, library, registry = open_invocation(tmp_path, "ready-writer", store)
    book = create_book(library, registry)
    page = library.add_photo(book["id"], photo())
    variant = make_variant(library, book["id"], page["id"])
    library.select_variant(book["id"], page["id"], variant["id"], accept_warnings=True)
    first.commit()
    _, restored, _ = open_invocation(tmp_path, "ready-reader", store)
    store.get_calls.clear()
    assert restored.validate_book(book["id"])["ready"] is True
    assert store.get_calls == []
    assert list((restored.root / "books").rglob("*.png")) == []


def test_removing_unhydrated_photo_preserves_other_remote_assets(tmp_path):
    store = AtomicBlobStore()
    initial, library, registry = open_invocation(tmp_path, "delete-writer", store)
    book = create_book(library, registry, count=2)
    removed = library.add_photo(book["id"], photo("red"))
    retained = library.add_photo(book["id"], photo("blue"))
    expected = library.safe_asset(book["id"], retained["source_path"]).read_bytes()
    initial.commit()
    update, changed, _ = open_invocation(tmp_path, "delete-cold", store)
    assert list((changed.root / "books").rglob("*.png")) == []
    changed.remove_photo(book["id"], removed["id"])
    update.commit()
    _, restored, _ = open_invocation(tmp_path, "delete-reader", store)
    assert [p["id"] for p in restored.get_book(book["id"])["pages"]] == [retained["id"]]
    assert restored.safe_asset(book["id"], retained["source_path"]).read_bytes() == expected
    with pytest.raises(NotFound):
        restored.safe_asset(book["id"], removed["source_path"])


def test_corrupted_immutable_asset_is_not_served(tmp_path):
    store = AtomicBlobStore()
    initial, library, registry = open_invocation(tmp_path, "integrity-writer", store)
    book = create_book(library, registry)
    page = library.add_photo(book["id"], photo())
    original = library.safe_asset(book["id"], page["source_path"]).read_bytes()
    initial.commit()
    targets = [key for key, value in store.objects.items() if value.body == original]
    assert targets
    for key in targets:
        value = store.objects[key]
        store.objects[key] = BlobValue(b"tampered", value.etag)
    _, restored, _ = open_invocation(tmp_path, "integrity-reader", store)
    with pytest.raises(CloudUnavailable):
        restored.safe_asset(book["id"], page["source_path"])
    assert not (restored.book_dir(book["id"]) / page["source_path"]).exists()


def test_an_unreadable_head_does_not_silently_create_an_empty_workshop(tmp_path):
    store = AtomicBlobStore()
    initial, library, registry = open_invocation(tmp_path, "head-writer", store)
    create_book(library, registry)
    initial.commit()
    value = store.objects[owner_head()]
    store.objects[owner_head()] = BlobValue(b"{truncated-json", value.etag)
    original_head = store.objects[owner_head()]
    with pytest.raises(CloudUnavailable):
        open_invocation(tmp_path, "head-reader", store)
    assert store.objects[owner_head()] == original_head


def test_export_paths_and_files_survive_moving_to_a_new_invocation_root(tmp_path):
    store = AtomicBlobStore()
    initial, library, registry = open_invocation(tmp_path, "export-writer", store)
    book = create_book(library, registry)
    export_directory = library.book_dir(book["id"]) / "exports" / "example"
    export_directory.mkdir(parents=True)
    pdf = export_directory / "interior.pdf"
    pdf.write_bytes(b"%PDF-1.4\ntransaction-persistence-fixture\n%%EOF")
    export_id = registry.save_export(OWNER_A, book["id"], book["revision"], {
        "interior_pdf": str(pdf),
    })
    initial.commit()
    _, restored, restored_registry = open_invocation(tmp_path, "export-reader", store)
    persisted = restored_registry.get_export(OWNER_A, book["id"], export_id)
    relative = persisted["payload"]["interior_pdf"]
    assert not Path(relative).is_absolute()
    assert restored.safe_asset(book["id"], relative).read_bytes() == pdf.read_bytes()
