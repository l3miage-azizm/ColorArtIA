"""Owner-scoped SQLite snapshots committed through one conditional Blob head.

Only the head is mutable. Database backups and artwork are immutable objects;
publishing the head last makes a request's two databases and assets visible as
one revision. The injected store must provide uncached reads and atomic
``if_match`` / ``create_only`` writes, including across independent instances.
"""
from __future__ import annotations

from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
from typing import Protocol
import uuid

from core.library import Library, NotFound, ValidationError


MAX_HEAD_BYTES = 2 * 1024 * 1024
MAX_DATABASE_BYTES = 16 * 1024 * 1024
MAX_ASSET_BYTES = 500 * 1024 * 1024
MAX_FILES = 20_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_DATABASES = ("library.sqlite3", "jobs.sqlite3")


class CloudConflict(RuntimeError):
    status_code = 409

    def __init__(self, message="Ton atelier a changé dans une autre fenêtre. Recharge-le avant de réessayer."):
        super().__init__(message)


class CloudUnavailable(RuntimeError):
    status_code = 503

    def __init__(self, message="Le stockage de ton atelier est temporairement indisponible. Réessaie dans un instant."):
        super().__init__(message)


@dataclass(frozen=True)
class BlobValue:
    body: bytes
    etag: str


class BlobStore(Protocol):
    def get(self, path: str) -> BlobValue | None: ...

    def put(self, path: str, body: bytes | Path, *, if_match: str | None = None,
            create_only: bool = False,
            content_type: str = "application/octet-stream") -> BlobValue: ...


def _digest_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


class Snapshot:
    """One request's isolated, optimistic transaction for one cookie owner."""

    def __init__(self, owner: str, root: Path, store: BlobStore):
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ValueError("A non-empty, bounded owner identifier is required")
        supplied = Path(root)
        if supplied.is_symlink():
            raise CloudUnavailable("Le dossier temporaire de l’atelier est invalide.")
        self.owner, self.root, self.store = owner, supplied.resolve(), store
        self.namespace = "owners/" + hashlib.sha256(owner.encode("utf-8")).hexdigest()
        self.head_path = self.namespace + "/head.json"
        self.head_etag: str | None = None
        self.revision: str | None = None
        self.operation_id = uuid.uuid4().hex
        self.files: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self._materialized: set[str] = set()
        self._library: CloudLibrary | None = None
        self._loaded = False
        self._committed = False

    def _get(self, path: str):
        try:
            return self.store.get(path)
        except (CloudConflict, CloudUnavailable):
            raise
        except Exception as exc:
            raise CloudUnavailable() from exc

    def _put(self, path: str, body: bytes | Path, **options):
        try:
            return self.store.put(path, body, **options)
        except (CloudConflict, CloudUnavailable):
            raise
        except Exception as exc:
            raise CloudUnavailable() from exc

    def _relative(self, path: str | Path) -> tuple[str, Path]:
        supplied = str(path)
        if "\\" in supplied or "\x00" in supplied:
            raise ValidationError("Chemin de fichier non autorisé.")
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(self.root)
            except ValueError as exc:
                raise ValidationError("Le fichier doit appartenir à l’atelier.") from exc
        relative = candidate.as_posix()
        # Inspect the original spelling as well: Path normalizes away './'.
        spelling = supplied[len(str(self.root)) + 1:] if Path(path).is_absolute() else supplied
        if (not spelling or any(part in {"", ".", ".."} for part in spelling.split("/"))
                or len(relative) > 512 or not candidate.parts
                or candidate.parts[0] != "books" or len(candidate.parts) < 3
                or not _ID.fullmatch(candidate.parts[1])
                or any(part.startswith(".") for part in candidate.parts)):
            raise ValidationError("Chemin de fichier non autorisé.")
        local = self.root / candidate
        current = self.root
        if current.is_symlink():
            raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        for part in candidate.parts:
            current /= part
            if current.is_symlink():
                raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        if not local.resolve().is_relative_to(self.root):
            raise ValidationError("Le fichier doit appartenir à l’atelier.")
        return relative, local

    def _entry(self, value, *, maximum: int, database: str | None = None) -> dict:
        if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
            raise CloudUnavailable("Le manifeste de l’atelier est invalide.")
        digest, size, key = value["sha256"], value["size"], value["path"]
        if (not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                or type(size) is not int or not 0 <= size <= maximum):
            raise CloudUnavailable("Le manifeste de l’atelier est invalide.")
        expected = (f"{self.namespace}/databases/{database}/{digest}.sqlite3" if database
                    else f"{self.namespace}/objects/{digest}")
        if key != expected:
            raise CloudUnavailable("Un fichier du manifeste ne correspond pas à cet atelier.")
        return dict(value)

    def _decode_head(self, value) -> dict:
        if (not isinstance(value.body, bytes) or len(value.body) > MAX_HEAD_BYTES
                or not isinstance(value.etag, str) or not value.etag):
            raise CloudUnavailable("Le manifeste de l’atelier est invalide.")
        try:
            head = json.loads(value.body)
        except (ValueError, UnicodeError) as exc:
            raise CloudUnavailable("Le manifeste de l’atelier est illisible.") from exc
        if (not isinstance(head, dict) or head.get("schema_version") != 1
                or not isinstance(head.get("revision"), str)
                or not _ID.fullmatch(head["revision"])
                or head.get("operation_id") != head["revision"]
                or not isinstance(head.get("databases"), dict)
                or set(head["databases"]) != set(_DATABASES)
                or not isinstance(head.get("files"), dict) or len(head["files"]) > MAX_FILES):
            raise CloudUnavailable("Le manifeste de l’atelier est invalide.")
        for name, entry in head["databases"].items():
            head["databases"][name] = self._entry(entry, maximum=MAX_DATABASE_BYTES, database=name)
        for relative, entry in head["files"].items():
            if not isinstance(relative, str) or PurePosixPath(relative).as_posix() != relative:
                raise CloudUnavailable("Un chemin du manifeste est invalide.")
            try:
                self._relative(relative)
            except ValidationError as exc:
                raise CloudUnavailable("Un chemin du manifeste est invalide.") from exc
            head["files"][relative] = self._entry(entry, maximum=MAX_ASSET_BYTES)
        return head

    def _read_verified(self, entry: dict) -> bytes:
        value = self._get(entry["path"])
        if (value is None or not isinstance(value.body, bytes)
                or len(value.body) != entry["size"]
                or hashlib.sha256(value.body).hexdigest() != entry["sha256"]):
            raise CloudUnavailable("Un fichier de ton atelier est absent ou endommagé.")
        return value.body

    @staticmethod
    def _check_database(path: Path):
        try:
            with path.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    raise CloudUnavailable("La base de l’atelier est endommagée.")
            db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise CloudUnavailable("La base de l’atelier est endommagée.")
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise CloudUnavailable("La base de l’atelier est illisible.") from exc

    def load(self) -> Snapshot:
        if self._loaded:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        if any(self.root.iterdir()):
            raise CloudUnavailable("Chaque requête doit utiliser un dossier temporaire isolé.")
        value = self._get(self.head_path)
        if value is not None:
            head = self._decode_head(value)
            for name, entry in head["databases"].items():
                destination = self.root / name
                destination.write_bytes(self._read_verified(entry))
                self._check_database(destination)
            self.files = head["files"]
            self.databases = head["databases"]
            for relative in self.files:
                _, candidate = self._relative(relative)
                candidate.parent.mkdir(parents=True, exist_ok=True)
            self.head_etag, self.revision = value.etag, head["revision"]
        self._loaded = True
        return self

    def library(self) -> CloudLibrary:
        if not self._loaded:
            raise RuntimeError("Call Snapshot.load() before opening the library")
        if self._library is None:
            self._library = CloudLibrary(self)
        return self._library

    def reference(self, path: str | Path) -> Path:
        """Validate an existing local or committed file without downloading it."""
        relative, candidate = self._relative(path)
        if candidate.is_file():
            return candidate
        if relative not in self.files or not candidate.parent.is_dir():
            raise NotFound("Ce fichier n'existe plus.")
        return candidate

    def hydrate(self, path: str | Path) -> Path:
        relative, candidate = self._relative(path)
        self.reference(candidate)
        if candidate.is_file():
            return candidate
        content = self._read_verified(self.files[relative])
        # A request owns this directory; no shared filesystem or mutable cache.
        with tempfile.NamedTemporaryFile(dir=candidate.parent, prefix=".hydrate-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(content)
                handle.flush()
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(candidate)
        finally:
            temporary.unlink(missing_ok=True)
        self._materialized.add(relative)
        return candidate

    def _publish(self, source: Path, *, database: str | None = None,
                 fingerprint: tuple[str, int] | None = None) -> dict:
        digest, size = fingerprint if fingerprint is not None else _digest_file(source)
        maximum = MAX_DATABASE_BYTES if database else MAX_ASSET_BYTES
        if size > maximum:
            raise CloudUnavailable("L’atelier dépasse la taille de stockage autorisée pour ce fichier.")
        key = (f"{self.namespace}/databases/{database}/{digest}.sqlite3" if database
               else f"{self.namespace}/objects/{digest}")
        entry = {"path": key, "sha256": digest, "size": size}
        try:
            self._put(key, source, create_only=True,
                      content_type=("application/vnd.sqlite3" if database else
                                    mimetypes.guess_type(source.name)[0] or "application/octet-stream"))
        except CloudConflict:
            # Content-addressed deduplication never overwrites the old object.
            self._read_verified(entry)
        return entry

    def _backup(self, name: str, destination: Path):
        source = sqlite3.connect(self.root / name, timeout=30)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.commit()
        except sqlite3.Error as exc:
            raise CloudUnavailable("La sauvegarde de l’atelier n’a pas pu être préparée.") from exc
        finally:
            target.close()
            source.close()
        self._check_database(destination)

    def _accept(self, value, head: dict) -> str:
        if not isinstance(value.etag, str) or not value.etag:
            raise CloudUnavailable("La confirmation du stockage est invalide.")
        self.head_etag, self.revision = value.etag, head["revision"]
        self.files = head["files"]
        self.databases = head["databases"]
        self._committed = True
        return self.revision

    def commit(self) -> str:
        if not self._loaded:
            raise RuntimeError("Call Snapshot.load() before committing")
        if self._committed:
            return self.revision
        from core.jobs import Registry
        self.library()
        Registry(self.root)
        # These entries were authenticated together in the loaded head. Their
        # objects are immutable, so equal bytes can reuse a reference without
        # another failed create-only PUT, metadata lookup and full download.
        known = {(None, entry["sha256"], entry["size"]): entry
                 for entry in self.files.values()}
        known.update({(name, entry["sha256"], entry["size"]): entry
                      for name, entry in self.databases.items()})
        publications = {}
        files = {}
        for relative, entry in self.files.items():
            _, candidate = self._relative(relative)
            # Missing placeholders mean the photo/book was deleted. A formerly
            # hydrated file explicitly removed locally must also stay deleted.
            if candidate.parent.is_dir() and (candidate.exists() or relative not in self._materialized):
                files[relative] = entry
        books = self.root / "books"
        for candidate in sorted(books.rglob("*")):
            if candidate.is_symlink():
                raise ValidationError("Les liens symboliques ne sont pas autorisés.")
            if not candidate.is_file():
                continue
            relative, _ = self._relative(candidate)
            digest, size = _digest_file(candidate)
            previous = self.files.get(relative)
            if previous and previous["sha256"] == digest and previous["size"] == size:
                files[relative] = previous
            else:
                key = (None, digest, size)
                if key in known:
                    files[relative] = known[key]
                else:
                    publication = publications.setdefault(key, {"source": candidate, "relatives": []})
                    publication["relatives"].append(relative)
        if len(files) + sum(len(p["relatives"]) for p in publications.values()) > MAX_FILES:
            raise CloudUnavailable("L’atelier contient trop de fichiers pour cette sauvegarde.")
        if publications:
            # This pool belongs to this commit only. The context manager joins
            # every started upload, including when one fails, before scratch is
            # removed. No background work can outlive the request or its CAS.
            with ThreadPoolExecutor(max_workers=min(4, len(publications)),
                                    thread_name_prefix="colorart-publish") as pool:
                pending = [(key, publication, pool.submit(
                    self._publish, publication["source"], fingerprint=(key[1], key[2])))
                    for key, publication in publications.items()]
                for key, publication, future in pending:
                    entry = future.result()
                    known[key] = entry
                    for relative in publication["relatives"]:
                        files[relative] = entry
        databases = {}
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=self.root) as directory:
            for name in _DATABASES:
                backup = Path(directory) / name
                self._backup(name, backup)
                digest, size = _digest_file(backup)
                key = (name, digest, size)
                databases[name] = (known[key] if key in known else
                                   self._publish(backup, database=name, fingerprint=(digest, size)))
        head = {"schema_version": 1, "revision": self.operation_id,
                "operation_id": self.operation_id, "databases": databases, "files": files}
        body = json.dumps(head, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > MAX_HEAD_BYTES:
            raise CloudUnavailable("Le manifeste de l’atelier dépasse la taille autorisée.")
        try:
            value = self._put(self.head_path, body, if_match=self.head_etag,
                              create_only=self.head_etag is None, content_type="application/json")
        except (CloudConflict, CloudUnavailable) as failure:
            # A server may have committed before its response was lost. Read
            # the operation marker; never replay the underlying UI mutation.
            try:
                current = self._get(self.head_path)
                current_head = self._decode_head(current) if current is not None else None
                if current_head is not None and current_head["operation_id"] == self.operation_id:
                    return self._accept(current, current_head)
            except CloudUnavailable:
                pass
            raise failure
        return self._accept(value, head)


class CloudLibrary(Library):
    """The existing library API with lazy, verified private-Blob artwork."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self._references_only = ContextVar("colorart_references_only", default=False)
        super().__init__(snapshot.root)

    def _safe_file(self, base: Path, relative: str | Path) -> Path:
        # First apply the original lexical/symlink checks without assuming the
        # file has already been materialized on this particular instance.
        supplied = Path(relative)
        if (supplied.is_absolute() or not supplied.parts
                or any(part in {"..", "."} for part in supplied.parts)):
            raise ValidationError("Chemin de fichier non autorisé.")
        candidate = base / supplied
        if not candidate.resolve().is_relative_to(base.resolve()):
            raise ValidationError("Le fichier doit appartenir au projet.")
        current = base
        if current.is_symlink():
            raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        for part in supplied.parts:
            current /= part
            if current.is_symlink():
                raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        if candidate.is_relative_to(self.books_dir):
            if self._references_only.get():
                return self.snapshot.reference(candidate)
            self.snapshot.hydrate(candidate)
        return Library._safe_file(base, relative)

    def asset_reference(self, book_id: str, relativepath: str | Path) -> Path:
        with self._db() as db:
            self._book(db, book_id)
        token = self._references_only.set(True)
        try:
            return self._safe_file(self.book_dir(book_id), relativepath)
        finally:
            self._references_only.reset(token)

    def validate_book(self, book_id: str) -> dict:
        token = self._references_only.set(True)
        try:
            return super().validate_book(book_id)
        finally:
            self._references_only.reset(token)
