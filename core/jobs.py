"""Small durable job/export registry; no external queue or printer is implied."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = root / "jobs.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS owners (book_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                    page_id TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL,
                    stage TEXT NOT NULL, error TEXT, result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS print_requests (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                    export_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(book_id,revision));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='failed',error=?,stage=?,updated_at=? WHERE status IN ('queued','running')",
                       ("La génération a été interrompue. Tes photos sont conservées ; tu peux réessayer.",
                        "Génération interrompue", now()))

    def own(self, book_id, owner_id):
        with self.db() as db:
            return db.execute("SELECT 1 FROM owners WHERE book_id=? AND owner_id=?", (book_id, owner_id)).fetchone() is not None

    def assign(self, book_id, owner_id):
        with self.db() as db:
            db.execute("INSERT INTO owners VALUES (?,?)", (book_id, owner_id))

    def forget(self, book_id):
        with self.db() as db:
            for table in ("owners", "jobs", "exports", "print_requests"):
                db.execute(f"DELETE FROM {table} WHERE book_id=?", (book_id,))

    def active(self, book_id=None, page_id=None, owner_id=None):
        terms, values = ["status IN ('queued','running')"], []
        for key, value in (("book_id", book_id), ("page_id", page_id), ("owner_id", owner_id)):
            if value is not None:
                terms.append(f"{key}=?")
                values.append(value)
        with self.db() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE " + " AND ".join(terms), values).fetchone()[0]

    def create_job(self, owner_id, book_id, page_id):
        ident, timestamp = uuid.uuid4().hex, now()
        with self.db() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,'queued',0,?,NULL,NULL,?,?)",
                       (ident, owner_id, book_id, page_id, "En attente dans l’atelier", timestamp, timestamp))
        return self.get_job(ident, owner_id)

    def get_job(self, ident, owner_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?", (ident, owner_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.pop("owner_id")
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def update_job(self, ident, **fields):
        allowed = {"status", "progress", "stage", "error", "result"}
        if not set(fields) <= allowed:
            raise ValueError("Invalid job fields")
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        fields["updated_at"] = now()
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                       [*fields.values(), ident])

    def save_export(self, owner_id, book_id, revision, payload):
        ident = payload.get("export_id", uuid.uuid4().hex)
        # Persist paths relative to the book, so a complete data directory can
        # be backed up and restored to a different machine or directory.
        payload = dict(payload)
        book_dir = (self.root / "books" / book_id).resolve()
        for key in ("run_dir", "interior_pdf", "solutions_pdf", "cover_pdf",
                    "cover_preview", "manifest", "archive"):
            if key not in payload:
                continue
            path = Path(payload[key])
            path = path.resolve() if path.is_absolute() else (book_dir / path).resolve()
            if not path.is_relative_to(book_dir):
                raise ValueError("Le dossier d’export doit appartenir au livre.")
            payload[key] = path.relative_to(book_dir).as_posix()
        with self.db() as db:
            db.execute("INSERT INTO exports VALUES (?,?,?,?,?,?)",
                       (ident, owner_id, book_id, revision, json.dumps(payload, ensure_ascii=False), now()))
        return ident

    def get_export(self, owner_id, book_id, ident=None):
        with self.db() as db:
            if ident:
                row = db.execute("SELECT * FROM exports WHERE owner_id=? AND book_id=? AND id=?",
                                 (owner_id, book_id, ident)).fetchone()
            else:
                row = db.execute("SELECT * FROM exports WHERE owner_id=? AND book_id=? ORDER BY created_at DESC LIMIT 1",
                                 (owner_id, book_id)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def print_draft(self, owner_id, book_id, export_id, revision):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO print_requests VALUES (?,?,?,?,?,'draft',?)",
                       (uuid.uuid4().hex, owner_id, book_id, export_id, revision, now()))
            row = db.execute("SELECT * FROM print_requests WHERE book_id=? AND revision=? AND owner_id=?",
                             (book_id, revision, owner_id)).fetchone()
        value = dict(row)
        value.pop("owner_id")
        value["message"] = "Demande préparée localement. Aucun paiement ni envoi à un imprimeur n’a été effectué."
        return value
