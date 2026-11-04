"""Network work stays proportional to new bytes, without weakening publication."""
from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from cloud.state import CloudUnavailable
from test_cloud_state import AtomicBlobStore, create_book, open_invocation, photo


def seed(tmp_path, store):
    snapshot, library, registry = open_invocation(tmp_path, "seed", store)
    book = create_book(library, registry)
    page = library.add_photo(book["id"], photo(), "souvenir.png")
    source = library.safe_asset(book["id"], page["source_path"]).read_bytes()
    snapshot.commit()
    return book, page, source


def clear_calls(store):
    store.get_calls.clear()
    store.put_calls.clear()


def test_unchanged_sqlite_backups_do_not_contact_remote_objects(tmp_path):
    store = AtomicBlobStore()
    book, _, _ = seed(tmp_path, store)
    snapshot, library, _ = open_invocation(tmp_path, "reader", store)
    assert set(snapshot.databases) == {"library.sqlite3", "jobs.sqlite3"}
    assert library.get_book(book["id"])["title"] == book["title"]
    clear_calls(store)
    snapshot.commit()
    # A CAS head still establishes the request revision. Both unchanged DBs
    # reuse authenticated immutable references, with no PUT+GET dedupe cycle.
    assert store.put_calls == [snapshot.head_path]
    assert store.get_calls == []


def test_metadata_edit_uploads_only_the_changed_database_and_head(tmp_path):
    store = AtomicBlobStore()
    book, _, _ = seed(tmp_path, store)
    snapshot, library, _ = open_invocation(tmp_path, "editor", store)
    library.update_book(book["id"], dedication="Un cadeau plus simple à créer.")
    clear_calls(store)
    snapshot.commit()
    assert len(store.put_calls) == 2
    assert sum("/databases/library.sqlite3/" in path for path in store.put_calls) == 1
    assert not any("/databases/jobs.sqlite3/" in path for path in store.put_calls)
    assert store.put_calls[-1] == snapshot.head_path
    assert store.get_calls == []
    _, restored, _ = open_invocation(tmp_path, "restored", store)
    assert restored.get_book(book["id"])["dedication"] == "Un cadeau plus simple à créer."


def test_known_artwork_is_reused_under_new_paths_without_download_or_upload(tmp_path):
    store = AtomicBlobStore()
    book, page, source = seed(tmp_path, store)
    snapshot, library, _ = open_invocation(tmp_path, "reuse", store)
    original = library.book_dir(book["id"]) / page["source_path"]
    assert not original.exists(), "The prior asset remains lazily stored remotely"
    directory = library.book_dir(book["id"]) / "reused"
    directory.mkdir()
    (directory / "copy-one.png").write_bytes(source)
    (directory / "copy-two.png").write_bytes(source)
    clear_calls(store)
    snapshot.commit()
    assert store.put_calls == [snapshot.head_path]
    assert store.get_calls == []
    entries = json.loads(store.objects[snapshot.head_path].body)["files"]
    prefix = "books/" + book["id"] + "/"
    assert entries[prefix + "reused/copy-one.png"] == entries[prefix + page["source_path"]]
    assert entries[prefix + "reused/copy-two.png"] == entries[prefix + page["source_path"]]
    _, restored, _ = open_invocation(tmp_path, "after-reuse", store)
    assert restored.safe_asset(book["id"], "reused/copy-two.png").read_bytes() == source


class ParallelStore(AtomicBlobStore):
    def __init__(self):
        super().__init__()
        self.barrier = threading.Barrier(4, timeout=5)
        self.active = 0
        self.maximum_active = 0
        self.completed_assets = 0
        self.work_lock = threading.Lock()
        self.tracking = False

    def put(self, path, body, **kwargs):
        asset = self.tracking and "/objects/" in path
        if asset:
            with self.work_lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                self.barrier.wait()
                result = super().put(path, body, **kwargs)
                with self.work_lock:
                    self.completed_assets += 1
                return result
            finally:
                with self.work_lock:
                    self.active -= 1
        if self.tracking and "/databases/" in path:
            assert self.active == 0
            assert self.completed_assets == 8
        if self.tracking and path.endswith("/head.json"):
            assert self.active == 0
            assert self.completed_assets == 8
        return super().put(path, body, **kwargs)


def test_new_unique_objects_publish_in_bounded_parallel_before_head(tmp_path):
    store = ParallelStore()
    snapshot, library, registry = open_invocation(tmp_path, "parallel", store)
    book = create_book(library, registry)
    directory = library.book_dir(book["id"]) / "new-artwork"
    directory.mkdir()
    for index in range(8):
        content = f"independent-artwork-{index}".encode() * 100
        (directory / f"{index:02d}-original.png").write_bytes(content)
        (directory / f"{index:02d}-duplicate.png").write_bytes(content)
    store.tracking = True
    snapshot.commit()
    assert store.maximum_active == 4
    assert store.completed_assets == 8
    assert len([path for path in store.put_calls if "/objects/" in path]) == 8
    assert store.put_calls[-1] == snapshot.head_path
    head = json.loads(store.objects[snapshot.head_path].body)
    assert len(head["files"]) == 16
    assert len({entry["path"] for entry in head["files"].values()}) == 8


class FailingParallelStore(AtomicBlobStore):
    def __init__(self):
        super().__init__()
        self.both_started = threading.Barrier(2, timeout=5)
        self.failure_raised = threading.Event()
        self.release_sibling = threading.Event()
        self.sibling_finished = threading.Event()

    def put(self, path, body, **kwargs):
        if "/objects/" not in path:
            return super().put(path, body, **kwargs)
        content = body.read_bytes() if isinstance(body, Path) else body
        self.both_started.wait()
        if content == b"failing-upload":
            self.failure_raised.set()
            raise CloudUnavailable("Failed upload")
        try:
            if not self.release_sibling.wait(5):
                raise CloudUnavailable("Sibling release was not received")
            return super().put(path, body, **kwargs)
        finally:
            self.sibling_finished.set()


def test_failure_waits_for_started_uploads_before_caller_can_remove_scratch(tmp_path):
    store = FailingParallelStore()
    snapshot, library, registry = open_invocation(tmp_path, "failure", store)
    book = create_book(library, registry)
    directory = library.book_dir(book["id"])
    (directory / "first.png").write_bytes(b"failing-upload")
    (directory / "second.png").write_bytes(b"sibling-upload")
    finished = threading.Event()
    errors = []

    def commit():
        try:
            snapshot.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=commit)
    worker.start()
    try:
        assert store.failure_raised.wait(5)
        assert not finished.is_set(), "Commit cannot release scratch while an upload reads it"
        assert snapshot.head_path not in store.objects
        store.release_sibling.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert finished.is_set() and store.sibling_finished.is_set()
        assert len(errors) == 1 and isinstance(errors[0], CloudUnavailable)
        assert snapshot.head_path not in store.objects
        assert not any("/databases/" in path for path in store.put_calls)
    finally:
        store.release_sibling.set()
        worker.join(timeout=5)
