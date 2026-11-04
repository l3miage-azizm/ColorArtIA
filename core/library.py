"""Persistent local book projects, isolated assets and explicit page validation.

Only identifiers cross the UI boundary. Generated runs must originate in
``Library.work_dir``; a browser-supplied filesystem path is never trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import hashlib
import json
import math
import re
import shutil
import sqlite3
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


TEMPLATES = {"heritage", "nocturne", "botanique"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MAX_RUN_BYTES = 500 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")


class LibraryError(ValueError):
    """Expected, presentable project error."""


class ValidationError(LibraryError):
    pass


class NotFound(LibraryError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise NotFound("Identifiant de projet ou de page invalide.")
    return value


def _text(value, name: str, maximum: int, required=False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"Le champ {name} doit être un texte.")
    value = value.strip()
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValidationError(f"Le champ {name} contient un caractère non autorisé.")
    if len(value) > maximum or (required and not value):
        raise ValidationError(f"Le champ {name} doit contenir entre {int(required)} et {maximum} caractères.")
    return value


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Les paramètres du projet ne sont pas valides.") from exc


def _warning_list(quality: dict) -> list[str]:
    raw = quality.get("warnings", [])
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            item = item.get("message", item.get("code", str(item)))
        if str(item) not in out:
            out.append(str(item))
    return out


def _quality_blocked(quality: dict) -> bool:
    """A human confirmation cannot override missing codes or a failed check."""
    if (quality.get("errors") or quality.get("critical_errors")
            or quality.get("status") in {"failed", "error", "blocked"}):
        return True
    coverage = quality.get("label_coverage", 1)
    if (isinstance(coverage, bool) or not isinstance(coverage, (int, float))
            or not math.isfinite(coverage) or not 1 <= coverage <= 1):
        return True
    missing = quality.get("unlabeled_regions", 0)
    return not isinstance(missing, (int, float)) or missing != 0


def _detail_guide_assets(metadata: dict, files: dict, resolve, *, inspect_content: bool = True) -> dict:
    """Require real, complete guide assets for codes absent from the main page.

    Counts alone cannot make a printable book complete. Every extra code must
    have one recorded enlarged tile and an existing image/PDF in the same run.
    ``resolve`` is supplied by the local or private-cloud library, so this check
    never opens a path chosen outside that library's safe asset resolver.
    """
    if not isinstance(metadata, dict) or not isinstance(files, dict):
        raise ValidationError("Les métadonnées des petits détails sont invalides.")
    quality = metadata.get("quality", metadata.get("quality_report", {}))
    records = metadata.get("detail_guides", [])
    if not isinstance(quality, dict):
        raise ValidationError("Le contrôle des petits détails est invalide.")
    required = bool(quality.get("guide_pages_required") or quality.get("guide_only_coded_regions") or records)
    if not required:
        return {}
    if (not isinstance(records, list) or not 1 <= len(records) <= 24
            or quality.get("all_regions_coded_main_or_guide") is not True
            or quality.get("combined_label_coverage") != 1
            or quality.get("uncoded_regions")):
        raise ValidationError("Les guides des petits détails sont incomplets.")
    layouts = metadata.get("number_layouts", {})
    if not isinstance(layouts, dict):
        raise ValidationError("Les codes du dessin sont invalides.")
    try:
        main_ids = {int(key) for key in layouts}
    except (ValueError, TypeError):
        raise ValidationError("Les codes du dessin sont invalides.") from None
    seen_ids, seen_files, assets = set(), set(), {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("file"), str):
            raise ValidationError("Un guide des petits détails est invalide.")
        if Path(record["file"]).name != record["file"]:
            raise ValidationError("Le chemin d’une planche de détails est invalide.")
        matches = [key for key, value in files.items()
                   if re.fullmatch(r"detail_guide_\d{2}", key)
                   and Path(value).name == Path(record["file"]).name]
        if len(matches) != 1 or record["file"] in seen_files:
            raise ValidationError("Un guide des petits détails est absent ou dupliqué.")
        seen_files.add(record["file"])
        tiles = record.get("tiles")
        if not isinstance(tiles, list) or not 1 <= len(tiles) <= 16:
            raise ValidationError("Une planche de détails ne contient pas ses codes.")
        for tile in tiles:
            rid = tile.get("region_id") if isinstance(tile, dict) else None
            if type(rid) is not int or rid <= 0 or rid in seen_ids or rid in main_ids:
                raise ValidationError("Un code du guide est absent ou dupliqué.")
            code = tile.get("code")
            if (not isinstance(code, str) or code not in metadata.get("palette_codes", [])
                    or not isinstance(tile.get("layout"), dict)
                    or tile["layout"].get("text") != code):
                raise ValidationError("Un code du guide ne correspond pas au nuancier.")
            for field in ("source_box", "destination_box"):
                box = tile.get(field)
                if (not isinstance(box, list) or len(box) != 4
                        or any(type(value) is not int or value < 0 for value in box)
                        or box[2] <= box[0] or box[3] <= box[1]):
                    raise ValidationError("Le repère d’une zone agrandie est invalide.")
            center = tile.get("source_center_percent")
            if (not isinstance(center, list) or len(center) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) or not 0 <= value <= 100 for value in center)):
                raise ValidationError("Le repère d’une zone agrandie est invalide.")
            seen_ids.add(rid)
        key = matches[0]
        assets[key] = resolve(files[key])
        if inspect_content:
            try:
                with Image.open(assets[key]) as image:
                    if image.format != "PNG":
                        raise ValidationError("Une planche de détails n’est pas une image PNG.")
                    image.verify()
            except (OSError, UnidentifiedImageError) as exc:
                raise ValidationError("Une planche de détails est illisible.") from exc
    if (quality.get("guide_only_coded_regions") != len(seen_ids)
            or quality.get("main_coded_regions") != len(main_ids)
            or metadata.get("n_regions") != len(main_ids | seen_ids)):
        raise ValidationError("Les guides ne couvrent pas toutes les zones du dessin.")
    if not isinstance(files.get("detail_guides"), str):
        raise ValidationError("Le PDF des petits détails est absent.")
    assets["detail_guides"] = resolve(files["detail_guides"])
    if inspect_content:
        with assets["detail_guides"].open("rb") as guide_pdf:
            if guide_pdf.read(5) != b"%PDF-":
                raise ValidationError("Le PDF des petits détails est illisible.")
    return assets


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.books_dir = self.root / "books"
        self.work_dir = self.root / "work"
        if self.books_dir.is_symlink() or self.work_dir.is_symlink():
            raise ValidationError("Les dossiers de stockage ne peuvent pas être des liens symboliques.")
        self.books_dir.mkdir(exist_ok=True)
        self.work_dir.mkdir(exist_ok=True)
        self.database = self.root / "library.sqlite3"
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    recipient TEXT NOT NULL, template TEXT NOT NULL,
                    target_photos INTEGER NOT NULL, dedication TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, filename TEXT NOT NULL,
                    source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    warnings TEXT NOT NULL DEFAULT '[]',
                    selected_variant_id TEXT,
                    accepted_warnings INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(book_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS variants (
                    id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    universe TEXT NOT NULL, config TEXT NOT NULL,
                    run_dir TEXT NOT NULL, files TEXT NOT NULL,
                    quality TEXT NOT NULL, warnings TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pages_book ON pages(book_id, position);
                CREATE INDEX IF NOT EXISTS variants_page ON variants(page_id, created_at);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(pages)")}
            if "thumbnail_path" not in columns:
                db.execute("ALTER TABLE pages ADD COLUMN thumbnail_path TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _book(db, book_id):
        _identifier(book_id)
        row = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise NotFound("Ce livre n'existe plus.")
        return row

    @staticmethod
    def _page(db, book_id, page_id):
        _identifier(page_id)
        row = db.execute("SELECT * FROM pages WHERE book_id=? AND id=?", (book_id, page_id)).fetchone()
        if row is None:
            raise NotFound("Cette photo n'appartient pas à ce livre.")
        return row

    @staticmethod
    def _variant(db, book_id, page_id, variant_id):
        _identifier(variant_id)
        row = db.execute("SELECT * FROM variants WHERE book_id=? AND page_id=? AND id=?",
                         (book_id, page_id, variant_id)).fetchone()
        if row is None:
            raise NotFound("Ce dessin n'appartient pas à cette photo.")
        return row

    @staticmethod
    def _touch(db, book_id):
        db.execute("UPDATE books SET revision=revision+1, updated_at=? WHERE id=?", (_now(), book_id))

    def book_dir(self, book_id: str) -> Path:
        _identifier(book_id)
        return self._safe_directory(self.books_dir, book_id)

    @staticmethod
    def _safe_directory(base: Path, relative: str | Path) -> Path:
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValidationError("Chemin de dossier non autorisé.")
        candidate = base / relative
        if not candidate.resolve().is_relative_to(base.resolve()):
            raise ValidationError("Le dossier doit appartenir au projet.")
        current = base
        if base.is_symlink():
            raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        return candidate

    @staticmethod
    def _safe_file(base: Path, relative: str | Path) -> Path:
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts or any(p in ("..", ".") for p in relative.parts):
            raise ValidationError("Chemin de fichier non autorisé.")
        candidate = base / relative
        if not candidate.resolve().is_relative_to(base.resolve()):
            raise ValidationError("Le fichier doit appartenir au projet.")
        current = base
        if base.is_symlink():
            raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValidationError("Les liens symboliques ne sont pas autorisés.")
        if not candidate.is_file():
            raise NotFound("Ce fichier n'existe plus.")
        return candidate

    def safe_asset(self, book_id: str, relativepath: str | Path) -> Path:
        with self._db() as db:
            self._book(db, book_id)
        return self._safe_file(self.book_dir(book_id), relativepath)

    def create_book(self, title: str, recipient: str = "", template: str = "heritage",
                    target_photos: int = 8, dedication: str = "") -> dict:
        title = _text(title, "titre", 100, required=True)
        recipient = _text(recipient, "destinataire", 100)
        dedication = _text(dedication, "dédicace", 1800)
        self._validate_settings(template, target_photos)
        book_id, now = uuid.uuid4().hex, _now()
        directory = self.book_dir(book_id)
        directory.mkdir()
        try:
            with self._db(write=True) as db:
                db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,1,?,?)",
                           (book_id, title, recipient, template, target_photos, dedication, now, now))
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return self.get_book(book_id)

    @staticmethod
    def _validate_settings(template, target_photos):
        if template not in TEMPLATES:
            raise ValidationError("Choisis un modèle de livre disponible.")
        if type(target_photos) is not int or not 1 <= target_photos <= 40:
            raise ValidationError("Le livre doit contenir entre 1 et 40 photos.")

    def list_books(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("""SELECT b.*,
                (SELECT COUNT(*) FROM pages p WHERE p.book_id=b.id) AS photo_count,
                (SELECT COUNT(*) FROM pages p WHERE p.book_id=b.id AND p.selected_variant_id IS NOT NULL) AS selected_count
                FROM books b ORDER BY updated_at DESC, id""").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _variant_dict(row) -> dict:
        value = dict(row)
        for name in ("config", "files", "quality", "warnings"):
            value[name] = json.loads(value[name])
        value["favorite"] = bool(value["favorite"])
        return value

    def get_book(self, book_id: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN")
            value = dict(self._book(db, book_id))
            pages = []
            for row in db.execute("SELECT * FROM pages WHERE book_id=? ORDER BY position,id", (book_id,)):
                page = dict(row)
                page["warnings"] = json.loads(page["warnings"])
                page["accepted_warnings"] = bool(page["accepted_warnings"])
                page["variants"] = [self._variant_dict(v) for v in db.execute(
                    "SELECT * FROM variants WHERE page_id=? ORDER BY created_at,id", (page["id"],))]
                pages.append(page)
        value["pages"] = pages
        value["photo_count"] = len(pages)
        value["selected_count"] = sum(p["selected_variant_id"] is not None for p in pages)
        return value

    def update_book(self, book_id: str, **fields) -> dict:
        if set(fields) - {"title", "recipient", "template", "target_photos", "dedication"}:
            raise ValidationError("Un champ de personnalisation n'est pas reconnu.")
        with self._db(write=True) as db:
            current = dict(self._book(db, book_id))
            values = {**current, **fields}
            values["title"] = _text(values["title"], "titre", 100, required=True)
            values["recipient"] = _text(values["recipient"], "destinataire", 100)
            values["dedication"] = _text(values["dedication"], "dédicace", 1800)
            self._validate_settings(values["template"], values["target_photos"])
            count = db.execute("SELECT COUNT(*) FROM pages WHERE book_id=?", (book_id,)).fetchone()[0]
            if values["target_photos"] < count:
                raise ValidationError("Retire d'abord les photos en trop avant de réduire le nombre de pages.")
            if any(values[k] != current[k] for k in fields):
                # Field names are selected above; all values remain SQL parameters.
                names = sorted(fields)
                db.execute("UPDATE books SET " + ",".join(k + "=?" for k in names) + " WHERE id=?",
                           [values[k] for k in names] + [book_id])
                self._touch(db, book_id)
        return self.get_book(book_id)

    def add_photo(self, book_id: str, source: str | Path | bytes,
                  filename: str = "photo.jpg") -> dict:
        if isinstance(source, bytes):
            content = source
        else:
            source = Path(source)
            if not source.is_file() or source.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValidationError("La photo doit être un fichier de moins de 20 Mio.")
            content = source.read_bytes()
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ValidationError("La photo doit être un fichier de moins de 20 Mio.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as uploaded:
                    if uploaded.format not in {"JPEG", "PNG", "WEBP"}:
                        raise ValidationError("Utilise une photo JPEG, PNG ou WebP.")
                    if uploaded.width * uploaded.height > MAX_IMAGE_PIXELS:
                        raise ValidationError("La photo dépasse la limite de 30 millions de pixels.")
                    source_format = uploaded.format
                    source_orientation = uploaded.getexif().get(274, 1)
                    image = ImageOps.exif_transpose(uploaded)
                    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        image = image.convert("RGB")
                    image.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning,
                Image.DecompressionBombError) as exc:
            raise ValidationError("Cette photo est illisible ou trop volumineuse.") from exc
        # Normalized pixels also catch duplicate uploads with different metadata.
        digest = hashlib.sha256(image.tobytes() + str(image.size).encode()).hexdigest()
        # Pillow may otherwise copy EXIF (including GPS) implicitly into PNGs.
        image.info.clear()
        jpeg_content = None
        if source_format == "JPEG":
            from .photo_storage import sanitized_jpeg
            jpeg_content = sanitized_jpeg(content, source_orientation)
        filename = Path(str(filename).replace("\\", "/")).name[:180] or "photo.jpg"
        photo_warnings = []
        if min(image.size) < 600:
            photo_warnings.append("La résolution de cette photo est faible pour une impression détaillée.")
        page_id, directory = uuid.uuid4().hex, None
        try:
            with self._db(write=True) as db:
                book = self._book(db, book_id)
                count = db.execute("SELECT COUNT(*) FROM pages WHERE book_id=?", (book_id,)).fetchone()[0]
                if count >= book["target_photos"]:
                    raise ValidationError("Toutes les photos prévues sont déjà présentes dans ce livre.")
                if db.execute("SELECT 1 FROM pages WHERE book_id=? AND sha256=?", (book_id, digest)).fetchone():
                    raise ValidationError("Cette photo est déjà présente dans le livre.")
                directory = self._safe_directory(self.book_dir(book_id), Path("photos") / page_id)
                directory.mkdir(parents=True)
                source_name = "source.jpg" if jpeg_content is not None else "source.png"
                source_path = f"photos/{page_id}/{source_name}"
                if jpeg_content is not None:
                    (directory / source_name).write_bytes(jpeg_content)
                else:
                    image.save(directory / source_name, format="PNG")
                thumbnail = image.copy()
                thumbnail.thumbnail((480, 480), Image.Resampling.LANCZOS)
                thumbnail.save(directory / "thumbnail.jpg", format="JPEG", quality=88, optimize=True)
                thumbnail_path = f"photos/{page_id}/thumbnail.jpg"
                db.execute("""INSERT INTO pages (id,book_id,position,filename,source_path,sha256,
                    width,height,warnings,created_at,thumbnail_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (page_id, book_id, count, filename, source_path, digest, image.width, image.height,
                     _json(photo_warnings), _now(), thumbnail_path))
                self._touch(db, book_id)
        except Exception:
            if directory:
                shutil.rmtree(directory, ignore_errors=True)
            raise
        return next(p for p in self.get_book(book_id)["pages"] if p["id"] == page_id)

    def remove_photo(self, book_id: str, page_id: str) -> dict:
        directory = self._safe_directory(self.book_dir(book_id), Path("photos") / _identifier(page_id))
        tombstone = directory.with_name(".deleted_" + uuid.uuid4().hex)
        moved = False
        try:
            with self._db(write=True) as db:
                self._book(db, book_id)
                self._page(db, book_id, page_id)
                if directory.is_symlink():
                    raise ValidationError("Le dossier de la photo n'est pas valide.")
                if directory.exists():
                    directory.rename(tombstone)
                    moved = True
                db.execute("DELETE FROM pages WHERE book_id=? AND id=?", (book_id, page_id))
                ids = [r[0] for r in db.execute("SELECT id FROM pages WHERE book_id=? ORDER BY position,id", (book_id,))]
                db.executemany("UPDATE pages SET position=? WHERE id=?", list(enumerate(ids)))
                self._touch(db, book_id)
        except Exception:
            if moved and tombstone.exists():
                tombstone.rename(directory)
            raise
        if moved:
            shutil.rmtree(tombstone)
        return self.get_book(book_id)

    def reorder_pages(self, book_id: str, page_ids: list[str]) -> dict:
        if not isinstance(page_ids, list) or any(not isinstance(x, str) for x in page_ids):
            raise ValidationError("L'ordre des pages est invalide.")
        with self._db(write=True) as db:
            self._book(db, book_id)
            existing = [r[0] for r in db.execute("SELECT id FROM pages WHERE book_id=? ORDER BY position,id", (book_id,))]
            if len(page_ids) != len(set(page_ids)) or set(page_ids) != set(existing):
                raise ValidationError("L'ordre doit contenir chaque photo du livre exactement une fois.")
            if page_ids != existing:
                db.executemany("UPDATE pages SET position=? WHERE id=? AND book_id=?",
                               [(i, page_id, book_id) for i, page_id in enumerate(page_ids)])
                self._touch(db, book_id)
        return self.get_book(book_id)

    def add_variant(self, book_id: str, page_id: str, run_dir: str | Path,
                    universe: str, config: dict | None = None) -> dict:
        run = Path(run_dir)
        resolved = run.resolve()
        if (not run.is_dir() or run.is_symlink() or resolved == self.work_dir
                or not resolved.is_relative_to(self.work_dir.resolve())):
            raise ValidationError("Ce rendu ne provient pas de l'atelier de génération.")
        try:
            relative_run = run.absolute().relative_to(self.work_dir.absolute())
        except ValueError as exc:
            raise ValidationError("Le rendu doit appartenir à l'atelier.") from exc
        run = self._safe_directory(self.work_dir, relative_run)
        all_files = list(run.rglob("*"))
        if any(p.is_symlink() for p in all_files):
            raise ValidationError("Un rendu ne peut pas contenir de lien symbolique.")
        if sum(p.stat().st_size for p in all_files if p.is_file()) > MAX_RUN_BYTES:
            raise ValidationError("Le rendu dépasse la taille autorisée pour une page.")
        try:
            meta_file = self._safe_file(run, "metadata.json")
            if meta_file.stat().st_size > 1_000_000:
                raise ValidationError("Les métadonnées du rendu sont trop volumineuses.")
            metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("Les métadonnées du rendu sont illisibles.") from exc
        if not isinstance(metadata, dict):
            raise ValidationError("Les métadonnées du rendu sont invalides.")
        raw_files = metadata.get("files", {})
        if not isinstance(raw_files, dict):
            raise ValidationError("Les fichiers du rendu sont invalides.")
        defaults = {"mystery": "02_mystere_total.png", "solution_final": "04_solution_finale_dessin.png",
                    "palette": "05_palette.png", "metadata": "metadata.json"}
        normalized = {}
        for key, value in {**defaults, **raw_files, "metadata": "metadata.json"}.items():
            if key == "run_dir":
                continue
            if not isinstance(value, str):
                raise ValidationError("Un chemin du rendu est invalide.")
            path = Path(value)
            if path.is_absolute():
                if not path.resolve().is_relative_to(resolved):
                    raise ValidationError("Tous les fichiers doivent appartenir au rendu.")
                path = path.relative_to(run) if path.is_relative_to(run) else path.resolve().relative_to(resolved)
            normalized[str(key)] = str(self._safe_file(run, path).relative_to(run))
        quality = metadata.get("quality", metadata.get("quality_report", {}))
        if not isinstance(quality, dict):
            quality = {}
        _detail_guide_assets(metadata, normalized, lambda path: self._safe_file(run, path))
        variant_warnings = _warning_list(quality)
        if metadata.get("warnings"):
            variant_warnings += _warning_list({"warnings": metadata["warnings"]})
        config = metadata.get("config", {}) if config is None else config
        _json(config)
        universe = _text(universe, "univers", 60, required=True)
        variant_id = uuid.uuid4().hex
        prefix = Path("photos") / _identifier(page_id) / "variants" / variant_id
        destination, stage = self._safe_directory(self.book_dir(book_id), prefix), None
        try:
            with self._db(write=True) as db:
                self._book(db, book_id)
                self._page(db, book_id, page_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                stage = destination.with_name(".incoming_" + uuid.uuid4().hex)
                shutil.copytree(run, stage)
                # Keep each copied run independently portable, never pointing into work/.
                metadata["files"] = normalized
                (stage / "metadata.json").write_text(_json(metadata), encoding="utf-8")
                stage.rename(destination)
                files = {k: (prefix / p).as_posix() for k, p in normalized.items()}
                db.execute("""INSERT INTO variants
                    (id,book_id,page_id,universe,config,run_dir,files,quality,warnings,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (variant_id, book_id, page_id, universe, _json(config), prefix.as_posix(),
                     _json(files), _json(quality), _json(list(dict.fromkeys(variant_warnings))), _now()))
                self._touch(db, book_id)
        except Exception:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(destination, ignore_errors=True)
            raise
        with self._db() as db:
            return self._variant_dict(self._variant(db, book_id, page_id, variant_id))

    def toggle_favorite(self, book_id: str, page_id: str, variant_id: str) -> dict:
        with self._db(write=True) as db:
            self._book(db, book_id)
            self._page(db, book_id, page_id)
            variant = self._variant(db, book_id, page_id, variant_id)
            db.execute("UPDATE variants SET favorite=? WHERE id=?", (int(not variant["favorite"]), variant_id))
            return self._variant_dict(self._variant(db, book_id, page_id, variant_id))

    def select_variant(self, book_id: str, page_id: str, variant_id: str,
                       accept_warnings: bool = False) -> dict:
        if type(accept_warnings) is not bool:
            raise ValidationError("La confirmation du contrôle doit être explicite.")
        with self._db(write=True) as db:
            self._book(db, book_id)
            page = self._page(db, book_id, page_id)
            variant = self._variant(db, book_id, page_id, variant_id)
            quality = json.loads(variant["quality"])
            if _quality_blocked(quality):
                raise ValidationError("Ce rendu présente une erreur de contrôle et doit être régénéré.")
            for name in ("mystery", "solution_final", "palette", "metadata"):
                files = json.loads(variant["files"])
                if name not in files:
                    raise ValidationError("Ce rendu est incomplet.")
                self._safe_file(self.book_dir(book_id), files[name])
            metadata = json.loads(self._safe_file(self.book_dir(book_id), files["metadata"]).read_text(encoding="utf-8"))
            _detail_guide_assets(metadata, files,
                lambda path: getattr(self, "asset_reference", self.safe_asset)(book_id, path), inspect_content=False)
            review = json.loads(page["warnings"]) + json.loads(variant["warnings"])
            if review and not accept_warnings:
                raise ValidationError("Vérifie les points d'attention puis confirme explicitement ce dessin.")
            if (page["selected_variant_id"] != variant_id
                    or bool(page["accepted_warnings"]) != accept_warnings):
                db.execute("UPDATE pages SET selected_variant_id=?,accepted_warnings=? WHERE id=?",
                           (variant_id, int(accept_warnings), page_id))
                self._touch(db, book_id)
        return next(p for p in self.get_book(book_id)["pages"] if p["id"] == page_id)

    def clear_selection(self, book_id: str, page_id: str) -> dict:
        """Revoke validation while preserving all variants and favorites."""
        with self._db(write=True) as db:
            self._book(db, book_id)
            page = self._page(db, book_id, page_id)
            if page["selected_variant_id"] is not None:
                db.execute("UPDATE pages SET selected_variant_id=NULL,accepted_warnings=0 WHERE id=?",
                           (page_id,))
                self._touch(db, book_id)
        return next(p for p in self.get_book(book_id)["pages"] if p["id"] == page_id)

    unselect_variant = clear_selection

    def validate_book(self, book_id: str) -> dict:
        book = self.get_book(book_id)
        problems, notes = [], []
        if book["photo_count"] != book["target_photos"]:
            problems.append(f"Le livre contient {book['photo_count']} photo(s) sur {book['target_photos']} prévue(s).")
        for page in book["pages"]:
            label = f"Photo {page['position'] + 1}"
            selected = next((v for v in page["variants"] if v["id"] == page["selected_variant_id"]), None)
            if not selected:
                problems.append(f"{label} : choisis et valide un dessin.")
                continue
            quality = selected["quality"]
            if _quality_blocked(quality):
                problems.append(f"{label} : ce dessin doit être régénéré après une erreur de contrôle.")
            review = page["warnings"] + selected["warnings"]
            if review:
                notes.extend(f"{label} : {message}" for message in review)
                if not page["accepted_warnings"]:
                    problems.append(f"{label} : les points d'attention doivent être confirmés.")
            for name in ("mystery", "solution_final", "palette", "metadata"):
                try:
                    self.safe_asset(book_id, selected["files"][name])
                except (LibraryError, KeyError):
                    problems.append(f"{label} : fichier {name} absent ou invalide.")
            if quality.get("guide_pages_required") or quality.get("guide_only_coded_regions"):
                # Cloud readiness deliberately validates committed references
                # without downloading rasters. Full tile/content validation
                # already ran when the variant was added and runs at export.
                guide_keys = [key for key in selected["files"] if re.fullmatch(r"detail_guide_\d{2}", key)]
                try:
                    if not guide_keys or "detail_guides" not in selected["files"]:
                        raise ValidationError("Les guides des petits détails sont absents.")
                    for key in ["detail_guides", *guide_keys]:
                        self.safe_asset(book_id, selected["files"][key])
                except (LibraryError, KeyError):
                    problems.append(f"{label} : les guides des petits détails sont absents ou invalides.")
        return {"ready": not problems, "problems": problems, "warnings": notes,
                "revision": book["revision"], "photo_count": book["photo_count"],
                "selected_count": book["selected_count"], "target_photos": book["target_photos"]}

    def delete_book(self, book_id: str) -> None:
        directory = self.book_dir(book_id)
        tombstone = self.books_dir / (".deleted_" + uuid.uuid4().hex)
        moved = False
        try:
            with self._db(write=True) as db:
                self._book(db, book_id)
                if directory.exists():
                    directory.rename(tombstone)
                    moved = True
                db.execute("DELETE FROM books WHERE id=?", (book_id,))
        except Exception:
            if moved:
                tombstone.rename(directory)
            raise
        if moved:
            shutil.rmtree(tombstone)
