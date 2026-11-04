"""ColorArt Atelier: local, persistent book studio and bounded rendering API.

Run with ``python atelier.py``. Printing requests are local drafts, never payments.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from core.engine import ENGINE_VERSION, GenerationConfig, generate_project
from core.jobs import Registry
from core.library import Library, LibraryError, NotFound, MAX_UPLOAD_BYTES
from core.universes import UNIVERSES
from core.studio_catalog import art_service_enabled, resolve_selection, studio_universes

APP_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("colorart")
COOKIE = "colorart_session"
TOKEN = re.compile(r"^[a-f0-9]{64}$")

TEMPLATES = [
    {"key": "heritage", "label": "Héritage", "description": "Ivoire, or et souvenirs précieux."},
    {"key": "nocturne", "label": "Nocturne", "description": "Noir profond et élégance dorée."},
    {"key": "botanique", "label": "Botanique", "description": "Sauge et détails inspirés de la nature."},
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewBook(Model):
    title: str = Field(default="Nos plus beaux souvenirs", min_length=1, max_length=100)
    recipient: str = Field(default="", max_length=100)
    template: Literal["heritage", "nocturne", "botanique"] = "heritage"
    target_photos: int = Field(default=8, ge=1, le=40, strict=True)
    dedication: str = Field(default="", max_length=1800)


class BookPatch(Model):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    recipient: str | None = Field(default=None, max_length=100)
    template: Literal["heritage", "nocturne", "botanique"] | None = None
    target_photos: int | None = Field(default=None, ge=1, le=40, strict=True)
    dedication: str | None = Field(default=None, max_length=1800)


class RenderRequest(Model):
    universe: str = "original"
    n_colors: Literal[16, 24, 32] = 24
    difficulty: Literal["relaxed", "balanced", "expert"] = "balanced"
    fit_mode: Literal["contain", "cover"] = "contain"
    artwork_mode: Literal["local", "premium"] = "local"
    target_regions: int = Field(default=3200, ge=600, le=6000, strict=True)
    masking: Literal["guided", "discreet", "mystery"] = "mystery"


class Selection(Model):
    accept_warnings: bool = Field(default=False, strict=True)


class Reorder(Model):
    page_ids: list[str] = Field(max_length=40)


class ExportRequest(Model):
    approved_revision: int = Field(ge=1, strict=True)


class PrintRequest(ExportRequest):
    export_id: str = Field(min_length=32, max_length=64)


class DirectUpload(Model):
    filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    size: int = Field(gt=0, le=MAX_UPLOAD_BYTES, strict=True)


class ImportUpload(Model):
    pathname: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)


class PrepareUploads(Model):
    files: list[DirectUpload] = Field(min_length=1, max_length=8)


class ImportUploads(Model):
    files: list[ImportUpload] = Field(min_length=1, max_length=8)


def create_app(data_root: str | Path | None = None, testing: bool = False, *, library_instance=None, inline_render=False, cloud_assets=None) -> FastAPI:
    root = Path(data_root or os.environ.get("COLORART_DATA_DIR", APP_ROOT / "data")).resolve()
    library = library_instance if library_instance is not None else Library(root)
    registry = Registry(root)
    executor = None if inline_render else ThreadPoolExecutor(max_workers=1, thread_name_prefix="colorart-render")
    locks = defaultdict(threading.RLock)
    submission_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(application):
        if not inline_render:
            registry.recover()
        try:
            yield
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            # Futures cancelled before starting must also reach a terminal
            # state; the next session must never display a phantom queue.
            registry.recover()

    application = FastAPI(title="ColorArt Atelier", version=ENGINE_VERSION, lifespan=lifespan,
                          docs_url="/api/docs" if testing else None, redoc_url=None)
    application.state.library = library
    application.state.registry = registry
    application.state.executor = executor

    @application.middleware("http")
    async def private_session(request: Request, call_next):
        session = request.cookies.get(COOKIE, "")
        new_session = not TOKEN.fullmatch(session)
        request.state.owner = secrets.token_hex(32) if new_session else session
        # SameSite cookies plus an explicit origin check for browser mutations.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            supplied_origin = urlsplit(origin)
            if (supplied_origin.scheme != request.url.scheme
                    or supplied_origin.netloc != request.url.netloc):
                return JSONResponse({"detail": "Cette action doit être effectuée depuis ton atelier."}, status_code=403)
        response = await call_next(request)
        if new_session:
            response.set_cookie(COOKIE, request.state.owner, httponly=True, samesite="strict",
                                max_age=365 * 86400, secure=request.url.scheme == "https")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
            "frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @application.exception_handler(LibraryError)
    async def project_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404 if isinstance(exc, NotFound) else 422)

    def owned(request: Request, book_id: str):
        if not registry.own(book_id, request.state.owner):
            raise HTTPException(404, "Ce livre n’est pas disponible dans cet atelier.")
        return library.get_book(book_id)

    def page_of(book: dict, page_id: str):
        for page in book["pages"]:
            if page["id"] == page_id:
                return page
        raise HTTPException(404, "Cette photo n’appartient pas à ce livre.")

    def asset_url(book_id: str, relative: str):
        return f"/api/books/{book_id}/assets/{quote(relative, safe='/')}"

    def public_book(book: dict):
        book_id = book["id"]
        result = dict(book)
        result["validated_count"] = book["selected_count"]
        result["readiness"] = library.validate_book(book_id)
        result["pages"] = []
        for original in book["pages"]:
            page = {k: v for k, v in original.items() if k not in {"source_path", "thumbnail_path", "sha256"}}
            page["source_url"] = asset_url(book_id, original["source_path"])
            page["thumbnail_url"] = asset_url(book_id, original.get("thumbnail_path") or original["source_path"])
            page["variants"] = []
            for variant in original["variants"]:
                item = {k: v for k, v in variant.items() if k != "run_dir"}
                item["files"] = {k: asset_url(book_id, v) for k, v in variant["files"].items()}
                page["variants"].append(item)
            result["pages"].append(page)
        return result

    def public_export(row: dict):
        payload = row["payload"]
        directory = library.book_dir(row["book_id"])
        files = {}
        for key in ("interior_pdf", "solutions_pdf", "cover_pdf", "cover_preview", "guides_pdf", "manifest", "archive"):
            if key in payload:
                path = Path(payload[key])
                path = path.resolve() if path.is_absolute() else (directory / path).resolve()
                if not path.is_relative_to(directory.resolve()):
                    raise HTTPException(500, "Le dossier d’export est invalide.")
                relative = path.relative_to(directory).as_posix()
                getattr(library, "asset_reference", library.safe_asset)(row["book_id"], relative)
                files[key] = asset_url(row["book_id"], relative)
        return {"id": row["id"], "export_id": row["id"], "revision": row["revision"],
                "book_id": row["book_id"], "files": files,
                "warnings": payload.get("warnings", []), "print_configured": False}

    @application.get("/api/catalog")
    def catalog():
        return {"templates": TEMPLATES,
                "universes": studio_universes(),
                "difficulties": [{"key": "relaxed", "label": "Détente"},
                                 {"key": "balanced", "label": "Équilibré"},
                                 {"key": "expert", "label": "Détaillé"}],
                "print_configured": False, "premium_enabled": art_service_enabled(),
                "artistic_enabled": art_service_enabled(),
                "direct_uploads": cloud_assets is not None,
                "batch_uploads": cloud_assets is not None and hasattr(cloud_assets, "prepare_uploads"),
                "instant_palettes": True,
                "instant_universes": False,
                "generation_mode": "awaited" if inline_render else "queued",
                "max_photos": 40, "max_upload_mb": 20,
                "generation_resolution": "2480 × 3508 · page A4 à 300 ppp"}

    @application.get("/api/books")
    def books(request: Request):
        return {"books": [dict(book, validated_count=book["selected_count"])
                          for book in library.list_books() if registry.own(book["id"], request.state.owner)]}

    @application.post("/api/books", status_code=201)
    def new_book(body: NewBook, request: Request):
        book = library.create_book(**body.model_dump())
        try:
            registry.assign(book["id"], request.state.owner)
        except Exception:
            library.delete_book(book["id"])
            raise
        return public_book(book)

    @application.get("/api/books/{book_id}")
    def get_book(book_id: str, request: Request):
        return public_book(owned(request, book_id))

    @application.patch("/api/books/{book_id}")
    def update_book(book_id: str, body: BookPatch, request: Request):
        owned(request, book_id)
        fields = body.model_dump(exclude_unset=True)
        if any(v is None for v in fields.values()):
            raise HTTPException(422, "Les champs de personnalisation ne peuvent pas être nuls.")
        with locks[book_id]:
            return public_book(library.update_book(book_id, **fields))

    @application.delete("/api/books/{book_id}", status_code=204)
    def delete_book(book_id: str, request: Request):
        owned(request, book_id)
        with submission_lock, locks[book_id]:
            if registry.active(book_id=book_id):
                raise HTTPException(409, "Attends la fin de la génération avant de supprimer ce livre.")
            library.delete_book(book_id)
            registry.forget(book_id)
        return Response(status_code=204)

    @application.post("/api/books/{book_id}/photos")
    async def upload_photos(book_id: str, request: Request, files: list[UploadFile] = File(...)):
        owned(request, book_id)
        if len(files) > 40:
            raise HTTPException(422, "Dépose au maximum 40 photos à la fois.")
        added, errors = [], []
        for photo in files:
            try:
                content = await photo.read(MAX_UPLOAD_BYTES + 1)
                with locks[book_id]:
                    page = library.add_photo(book_id, content, filename=photo.filename or "photo.jpg")
                    added.append(page["id"])
            except LibraryError as exc:
                errors.append({"filename": photo.filename or "photo", "message": str(exc)})
            finally:
                await photo.close()
        book = public_book(library.get_book(book_id))
        return {"book": book, "added": [p for p in book["pages"] if p["id"] in added], "errors": errors}

    @application.delete("/api/books/{book_id}/photos/{page_id}")
    def remove_photo(book_id: str, page_id: str, request: Request):
        owned(request, book_id)
        with submission_lock, locks[book_id]:
            if registry.active(book_id=book_id, page_id=page_id):
                raise HTTPException(409, "Attends la fin du dessin avant de retirer cette photo.")
            return public_book(library.remove_photo(book_id, page_id))

    @application.post("/api/books/{book_id}/uploads/prepare")
    def prepare_upload(book_id: str, body: DirectUpload, request: Request):
        book = owned(request, book_id)
        if cloud_assets is None:
            raise HTTPException(404, "Le transfert direct n’est pas activé.")
        if book["photo_count"] >= book["target_photos"]:
            raise HTTPException(422, "Toutes les photos prévues sont déjà présentes dans ce livre.")
        return cloud_assets.prepare_upload(request.state.owner, book_id, body.filename,
                                            body.content_type, body.size)

    @application.post("/api/books/{book_id}/photos/import")
    def import_upload(book_id: str, body: ImportUpload, request: Request):
        owned(request, book_id)
        if cloud_assets is None:
            raise HTTPException(404, "Le transfert direct n’est pas activé.")
        content = cloud_assets.read_upload(request.state.owner, book_id, body.pathname)
        page = library.add_photo(book_id, content, filename=body.filename)
        book = public_book(library.get_book(book_id))
        return {"book": book, "added": [p for p in book["pages"] if p["id"] == page["id"]], "errors": []}

    @application.post("/api/books/{book_id}/uploads/prepare-batch")
    def prepare_uploads(book_id: str, body: PrepareUploads, request: Request):
        book = owned(request, book_id)
        if cloud_assets is None or not hasattr(cloud_assets, "prepare_uploads"):
            raise HTTPException(404, "Le transfert groupé n’est pas activé.")
        if len(body.files) > book["target_photos"] - book["photo_count"]:
            raise HTTPException(422, "Ce lot dépasse le nombre de photos prévu pour ton livre.")
        return cloud_assets.prepare_uploads(request.state.owner, book_id,
                                             [item.model_dump() for item in body.files])

    @application.post("/api/books/{book_id}/photos/import-batch")
    def import_uploads(book_id: str, body: ImportUploads, request: Request):
        book = owned(request, book_id)
        if cloud_assets is None:
            raise HTTPException(404, "Le transfert direct n’est pas activé.")
        if len(body.files) > book["target_photos"] - book["photo_count"]:
            raise HTTPException(422, "Ce lot dépasse le nombre de photos prévu pour ton livre.")
        if len({item.pathname for item in body.files}) != len(body.files):
            raise HTTPException(422, "Une photo est présente deux fois dans ce lot.")
        def download(item):
            try:
                return item, cloud_assets.read_upload(request.state.owner, book_id, item.pathname), None
            except LibraryError as exc:
                return item, None, str(exc)
        with ThreadPoolExecutor(max_workers=3) as transfers:
            downloaded = list(transfers.map(download, body.files))
        added, errors = [], []
        for item, content, error in downloaded:
            try:
                if error:
                    raise LibraryError(error)
                added.append(library.add_photo(book_id, content, filename=item.filename)["id"])
            except LibraryError as exc:
                errors.append({"filename": item.filename, "message": str(exc)})
        book = public_book(library.get_book(book_id))
        return {"book": book, "added": [page for page in book["pages"] if page["id"] in added], "errors": errors}

    @application.post("/api/books/{book_id}/pages/reorder")
    def reorder(book_id: str, body: Reorder, request: Request):
        owned(request, book_id)
        with locks[book_id]:
            return public_book(library.reorder_pages(book_id, body.page_ids))

    def render_settings(render_config: dict):
        selection = resolve_selection(render_config["universe"])
        mode = "premium" if selection.art_universe != "original" else render_config.get("artwork_mode", "local")
        config = GenerationConfig(n_colors=render_config["n_colors"],
            difficulty=render_config["difficulty"], fit_mode=render_config["fit_mode"],
            target_regions=render_config.get("target_regions", 3200),
            masking=render_config.get("masking", "mystery"), preserve_ink=mode == "premium",
            preserve_details=mode == "premium", n_segments=2000 if mode == "premium" else 900,
            min_region_area=85 if mode == "premium" else 180)
        settings = {**asdict(config), "artwork_mode": mode,
                    "art_universe": selection.art_universe,
                    "palette_universe": selection.palette_universe}
        return selection, config, settings

    def usable_scene(variant: dict, settings: dict, *, exact_settings: bool = True):
        quality, previous = variant.get("quality", {}), variant.get("config", {})
        if quality.get("errors") or quality.get("critical_errors"):
            return False
        if previous.get("art_universe", "original") != settings["art_universe"]:
            return False
        if previous.get("artwork_mode", "local") != settings["artwork_mode"]:
            return False
        keys = ("n_colors", "difficulty", "fit_mode", "target_regions", "masking", "preserve_ink")
        return not exact_settings or (all(previous.get(key) == settings[key] for key in keys)
                                     and previous.get("preserve_details", False) == settings["preserve_details"]
                                     and (not settings["preserve_details"]
                                          or all(previous.get(key) == settings[key] for key in ("n_segments", "min_region_area"))))

    def reusable_illustration(variant: dict, settings: dict):
        # A failed segmentation does not invalidate its saved input drawing.
        # Restoration must be able to recover that drawing without buying or
        # inventing a different face from the image provider.
        previous = variant.get("config", {})
        return (previous.get("artwork_mode") == "premium"
                and previous.get("art_universe", "original") == settings["art_universe"]
                and "source" in variant.get("files", {}))

    def save_render_variant(book_id, page_id, run_dir, key, settings):
        path = Path(run_dir) / "metadata.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["art_universe"] = settings["art_universe"]
        metadata["palette_universe"] = settings["palette_universe"]
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return library.add_variant(book_id, page_id, run_dir, key, settings)

    def render(job: dict, render_config: dict):
        from core.universe_variants import prepare_preview_bundle, recolor_project
        ident, book_id, page_id = job["id"], job["book_id"], job["page_id"]
        temporary_runs, art_dir = [], None
        try:
            registry.update_job(ident, status="running", progress=10, stage="Préparation de ton dessin")
            with locks[book_id]:
                page = page_of(library.get_book(book_id), page_id)
            selection, config, settings = render_settings(render_config)
            candidates = [variant for variant in reversed(page["variants"]) if usable_scene(variant, settings)]
            requested = next((v for v in candidates if v["universe"] == selection.key
                              and "studio_preview" in v["files"]), None)
            if requested:
                registry.update_job(ident, status="succeeded", progress=100, stage="Ton dessin est prêt",
                    result={"book_id": book_id, "page_id": page_id, "variant_id": requested["id"], "cache_hit": True})
                return
            base_key = selection.art_universe
            base = next((v for v in candidates if v["universe"] == base_key), None)
            # All variants of one scene retain the canonical palette. A saved
            # colour variant also works when its original base is unavailable.
            base = base or next((v for v in candidates if "studio_preview" in v["files"]), None)
            if base:
                registry.update_job(ident, progress=30, stage="Application des couleurs à ton dessin")
                required = [relative for key, relative in base["files"].items()
                            if key in {"metadata", "regions", "mystery"} or key.startswith("detail_guide")]
                with ThreadPoolExecutor(max_workers=3) as reads:
                    list(reads.map(lambda relative: library.safe_asset(book_id, relative), required))
                source_run = library.book_dir(book_id) / base["run_dir"]
                result = recolor_project(source_run, library.work_dir, selection.palette_universe)
                run_dir = Path(result["run_dir"]); temporary_runs.append(run_dir)
                variant = save_render_variant(book_id, page_id, run_dir, selection.key, settings)
            else:
                source = library.safe_asset(book_id, page["source_path"])
                provenance = None
                if settings["artwork_mode"] == "premium":
                    from core.artist import create_illustration
                    # Reuse the actual saved illustration when only density,
                    # masking or colour-count settings change. This preserves
                    # faces and avoids a second image-provider call.
                    saved = next((v for v in reversed(page["variants"])
                                  if reusable_illustration(v, settings)), None)
                    if saved:
                        saved_meta = json.loads(library.safe_asset(book_id, saved["files"]["metadata"]).read_text(encoding="utf-8"))
                        if saved_meta.get("input_role") == "illustration":
                            source = library.safe_asset(book_id, saved["files"]["source"])
                            provenance = saved_meta.get("artwork_provenance")
                    if provenance is None:
                        if not art_service_enabled():
                            raise ValueError("La création de dessins premium n’est pas encore activée dans cet atelier.")
                        art_dir = library.work_dir / ("art_" + ident)
                        registry.update_job(ident, progress=15, stage="Création de ton illustration")
                        illustration = create_illustration(source, art_dir, universe_key=selection.art_universe)
                        source, provenance = Path(illustration["path"]), illustration["provenance"]
                registry.update_job(ident, progress=25, stage="Création de ton dessin de référence")
                result = generate_project(source, library.work_dir, config, "original")
                canonical = Path(result["run_dir"]); temporary_runs.append(canonical)
                meta_file = canonical / "metadata.json"
                metadata = json.loads(meta_file.read_text(encoding="utf-8"))
                metadata["artwork_mode"] = settings["artwork_mode"]
                metadata["art_universe"] = selection.art_universe
                metadata["palette_universe"] = "original"
                metadata["artwork_provenance"] = provenance
                metadata["input_role"] = "illustration" if provenance else "photograph"
                meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                prepare_preview_bundle(canonical)
                variant = save_render_variant(book_id, page_id, canonical, base_key, {**settings, "palette_universe": "original"})
                if selection.palette_universe != "original":
                    result = recolor_project(canonical, library.work_dir, selection.palette_universe)
                    run_dir = Path(result["run_dir"]); temporary_runs.append(run_dir)
                    variant = save_render_variant(book_id, page_id, run_dir, selection.key, settings)
            registry.update_job(ident, status="succeeded", progress=100, stage="Ton dessin est prêt",
                result={"book_id": book_id, "page_id": page_id, "variant_id": variant["id"]})
        except (LibraryError, ValueError) as exc:
            registry.update_job(ident, status="failed", stage="Le dessin n’a pas pu être terminé", error=str(exc))
        except Exception:
            LOGGER.exception("Rendering job %s failed", ident)
            registry.update_job(ident, status="failed", stage="Génération interrompue",
                error="Le dessin n’a pas pu être terminé. Ta photo est conservée ; réessaie.")
        finally:
            if art_dir and art_dir.resolve().is_relative_to(library.work_dir.resolve()):
                shutil.rmtree(art_dir, ignore_errors=True)
            for run_dir in temporary_runs:
                if run_dir.resolve() != library.work_dir.resolve() and run_dir.resolve().is_relative_to(library.work_dir.resolve()):
                    shutil.rmtree(run_dir, ignore_errors=True)

    @application.post("/api/books/{book_id}/photos/{page_id}/generate", status_code=202)
    def generate(book_id: str, page_id: str, body: RenderRequest, request: Request):
        owned(request, book_id)
        try:
            values = body.model_dump()
            selected = resolve_selection(body.universe)
            if body.artwork_mode == "premium" or selected.art_universe != "original":
                # The approved detailed portrait preset is the default. An
                # explicit advanced choice remains the user's choice.
                for key, value in (("n_colors", 32), ("difficulty", "expert")):
                    if key not in body.model_fields_set:
                        values[key] = value
            selection, _, settings = render_settings(values)
        except ValueError:
            raise HTTPException(422, "Choisis un univers disponible.")
        with submission_lock, locks[book_id]:
            # Deletion and submission share these locks. Re-read after
            # acquiring them instead of queuing a now-deleted photo.
            page = page_of(library.get_book(book_id), page_id)
            if settings["artwork_mode"] == "premium":
                cached = any(usable_scene(v, settings) for v in page["variants"])
                saved_source = any(reusable_illustration(v, settings) for v in page["variants"])
                if not cached and not saved_source and not art_service_enabled():
                    raise HTTPException(503, "La création de dessins premium n’est pas encore activée dans cet atelier.")
            if registry.active(book_id=book_id, page_id=page_id):
                raise HTTPException(409, "Cette photo est déjà en cours de génération.")
            if registry.active() >= 8 or registry.active(owner_id=request.state.owner) >= 3:
                raise HTTPException(429, "L’atelier termine tes dessins en cours. Réessaie dans un instant.")
            job = registry.create_job(request.state.owner, book_id, page_id)
            try:
                if inline_render:
                    render(job, {**values, "universe": selection.key})
                else:
                    executor.submit(render, job, {**values, "universe": selection.key})
            except RuntimeError:
                registry.update_job(job["id"], status="failed", error="L’atelier redémarre. Réessaie dans un instant.")
                raise HTTPException(503, "L’atelier redémarre.")
        return registry.get_job(job["id"], request.state.owner) if inline_render else job

    @application.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        value = registry.get_job(job_id, request.state.owner)
        if value is None:
            raise HTTPException(404, "Cette génération n’est pas disponible.")
        return value

    @application.post("/api/books/{book_id}/photos/{page_id}/variants/{variant_id}/favorite")
    def favorite(book_id: str, page_id: str, variant_id: str, request: Request):
        owned(request, book_id)
        with locks[book_id]:
            library.toggle_favorite(book_id, page_id, variant_id)
            return public_book(library.get_book(book_id))

    @application.post("/api/books/{book_id}/photos/{page_id}/variants/{variant_id}/select")
    def select(book_id: str, page_id: str, variant_id: str, body: Selection, request: Request):
        owned(request, book_id)
        with locks[book_id]:
            library.select_variant(book_id, page_id, variant_id, accept_warnings=body.accept_warnings)
            return public_book(library.get_book(book_id))

    @application.delete("/api/books/{book_id}/photos/{page_id}/selection")
    def clear_selection(book_id: str, page_id: str, request: Request):
        owned(request, book_id)
        with locks[book_id]:
            library.clear_selection(book_id, page_id)
            return public_book(library.get_book(book_id))

    @application.get("/api/books/{book_id}/readiness")
    def readiness(book_id: str, request: Request):
        owned(request, book_id)
        return library.validate_book(book_id)

    @application.post("/api/books/{book_id}/exports", status_code=201)
    def export(book_id: str, body: ExportRequest, request: Request):
        owned(request, book_id)
        from core.book_export import export_book
        with locks[book_id]:
            book = library.get_book(book_id)
            if book["revision"] != body.approved_revision:
                raise HTTPException(409, "Ton livre a changé. Vérifie son aperçu avant de préparer un nouveau dossier.")
            ready = library.validate_book(book_id)
            if not ready["ready"]:
                raise HTTPException(422, {"message": "Valide chaque page avant de préparer ton livre.", "problems": ready["problems"]})
            try:
                payload = export_book(library, book_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            export_id = registry.save_export(request.state.owner, book_id, book["revision"], payload)
            return public_export(registry.get_export(request.state.owner, book_id, export_id))

    @application.get("/api/books/{book_id}/exports")
    def latest_export(book_id: str, request: Request):
        book = owned(request, book_id)
        row = registry.get_export(request.state.owner, book_id)
        if not row:
            raise HTTPException(404, "Le dossier de ce livre n’a pas encore été préparé.")
        if row["revision"] != book["revision"]:
            raise HTTPException(409, "Cet aperçu est antérieur à tes dernières modifications. Prépare un nouveau dossier.")
        return public_export(row)

    @application.post("/api/books/{book_id}/print-requests", status_code=201)
    def prepare_print(book_id: str, body: PrintRequest, request: Request):
        owned(request, book_id)
        from core.book_export import assert_export_current
        with locks[book_id]:
            book = library.get_book(book_id)
            row = registry.get_export(request.state.owner, book_id, body.export_id)
            if not row or body.approved_revision != book["revision"] or row["revision"] != book["revision"]:
                raise HTTPException(409, "Vérifie et prépare la version actuelle du livre avant de continuer.")
            try:
                manifest = Path(row["payload"]["manifest"])
                manifest = manifest if manifest.is_absolute() else library.safe_asset(book_id, manifest)
                assert_export_current(library, book_id, manifest)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return registry.print_draft(request.state.owner, book_id, row["id"], book["revision"])

    @application.get("/api/books/{book_id}/assets/{relative:path}")
    def asset(book_id: str, relative: str, request: Request):
        owned(request, book_id)
        if cloud_assets is not None:
            return cloud_assets.asset_response(library, book_id, relative)
        path = library.safe_asset(book_id, relative)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf", ".zip", ".json", ".npz"}:
            raise HTTPException(404, "Ce fichier n’est pas disponible.")
        if path.suffix.lower() == ".zip":
            return FileResponse(path, filename="ColorArt-livre-cadeau.zip", media_type="application/zip")
        return FileResponse(path)

    @application.get("/api/health")
    def health():
        return {"status": "ok", "version": ENGINE_VERSION, "printer": "not_configured"}

    @application.get("/")
    def index():
        return FileResponse(APP_ROOT / "web" / "index.html")

    application.mount("/static", StaticFiles(directory=APP_ROOT / "web", check_dir=False), name="static")
    return application


# The deployed ASGI entrypoint is app.py; this module supplies the unchanged atelier routes.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "7860")), log_level="info")
