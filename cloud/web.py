"""Isolated Vercel requests with atomic private-Blob persistence."""
from pathlib import Path
import os
import json
import re
import secrets
import tempfile
from urllib.parse import urlsplit, unquote
import anyio
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from atelier import COOKIE, TEMPLATES, TOKEN, create_app
from core.artist import artist_service_status
from core.engine import ENGINE_VERSION
from core.library import LibraryError
from core.studio_catalog import art_service_enabled, studio_universes
from .state import CloudConflict, CloudUnavailable, Snapshot
from .store import PrivateBlobStore

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="ColorArt Atelier", version=ENGINE_VERSION, docs_url=None, redoc_url=None, openapi_url=None)

@app.middleware("http")
async def session(request: Request, call_next):
    cookie = request.cookies.get(COOKIE, "")
    is_new = not TOKEN.fullmatch(cookie)
    request.state.owner = secrets.token_hex(32) if is_new else cookie
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        supplied = urlsplit(origin)
        if supplied.scheme != request.url.scheme or supplied.netloc != request.url.netloc:
            return JSONResponse({"detail": "Cette action doit être effectuée depuis ton atelier."}, status_code=403)
    from core.artist import artist_request_context
    with artist_request_context(request.headers):
        try:
            response = await call_next(request)
        except (CloudConflict, CloudUnavailable) as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
    if is_new:
        response.set_cookie(COOKIE, request.state.owner, httponly=True, samesite="strict", max_age=365*86400, secure=request.url.scheme == "https")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob: https://*.blob.vercel-storage.com; "
        "font-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self' https://vercel.com/api/blob/ https://blob.vercel-storage.com https://*.blob.vercel-storage.com; "
        "frame-src 'self' https://*.blob.vercel-storage.com; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'self'")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response

@app.exception_handler(CloudConflict)
@app.exception_handler(CloudUnavailable)
async def cloud_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

@app.get("/api/health")
def health():
    return {"status": "ok", "version": ENGINE_VERSION, "hosting": "vercel", "studio_version": 2, "storage_configured": bool(os.environ.get("COLORART_STORAGE_BRIDGE_SECRET")), "printer": "not_configured"}

@app.get("/api/catalog")
def catalog():
    return {"templates": TEMPLATES,
        "universes": studio_universes(),
        "difficulties": [{"key":"relaxed","label":"Détente"},{"key":"balanced","label":"Équilibré"},{"key":"expert","label":"Détaillé"}],
        "print_configured": False, "premium_enabled": art_service_enabled(),
        "artistic_enabled": art_service_enabled(), "instant_palettes": True, "instant_universes": False,
        "direct_uploads": True, "batch_uploads": True,
        "generation_mode": "awaited", "max_photos": 40, "max_upload_mb": 20,
        "generation_resolution": "2480 × 3508 · page A4 à 300 ppp"}


@app.get("/api/art-service")
async def art_service(request: Request):
    """Operator-only read-only probe; no images, balances or credentials returned."""
    tokens = [os.environ.get(name, "") for name in
              ("COLORART_STORAGE_BRIDGE_SECRET", "COLORART_DIAGNOSTIC_TOKEN")]
    supplied = request.headers.get("authorization", "")
    if not any(token and secrets.compare_digest(supplied.encode("utf-8"),
                ("Bearer " + token).encode("utf-8")) for token in tokens):
        raise HTTPException(404, "Ce service n’est pas disponible.")
    result = await anyio.to_thread.run_sync(artist_service_status)
    return {key: result.get(key) for key in ("available", "provider", "reason", "action_url")}

@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")

app.mount("/static", StaticFiles(directory=ROOT / "web", check_dir=False), name="static")

_ASSET_URL = re.compile(r"^/api/books/([a-f0-9]{32})/assets/(.+)$")
_VIEW_FIELDS = ("studio_preview", "studio_index", "solution_final", "mystery", "reveal_50", "palette")


def _decorate_book_views(payload, snapshot, store):
    """Authorize previews against the committed snapshot, then sign in batches.

    Export, metadata and NPZ routes remain authenticated API URLs. Old/extra
    previews beyond the bounded response budget retain the fast API fallback.
    """
    book = payload if isinstance(payload, dict) and isinstance(payload.get("pages"), list) else None
    if book is None and isinstance(payload, dict):
        candidate = payload.get("book")
        if isinstance(candidate, dict) and isinstance(candidate.get("pages"), list):
            book = candidate
    if book is None:
        return payload
    records, seen = [], set()

    def add(container, key):
        value = container.get(key)
        if not isinstance(value, str) or value in seen or len(records) >= 250:
            return
        match = _ASSET_URL.fullmatch(value)
        if match is None:
            return
        try:
            relative, _ = snapshot._relative("books/" + match[1] + "/" + unquote(match[2]))
        except LibraryError:
            return
        entry = snapshot.files.get(relative)
        if entry is not None:
            records.append((container, key, entry["path"]))
            seen.add(value)

    pages = book["pages"]
    for page in pages:
        add(page, "source_url")
        add(page, "thumbnail_url")
    for page in pages:
        variants = page.get("variants") or []
        original = next((v for v in reversed(variants) if v.get("universe") == "original"
                         and (v.get("files") or {}).get("studio_preview")), None)
        if original is not None:
            for field in ("studio_preview", "studio_index"):
                add(original.get("files") or {}, field)
        preferred = variants[-1:] + [v for v in variants if v.get("id") == page.get("selected_variant_id")]
        for variant in preferred:
            files = variant.get("files") or {}
            for field in _VIEW_FIELDS:
                add(files, field)
    for page in pages:
        for variant in reversed(page.get("variants") or []):
            add(variant.get("files") or {}, "solution_final")
    if not records:
        return payload
    try:
        urls = store.view_urls([path for _, _, path in records])
    except CloudUnavailable:
        # The signed optimization is optional; the authenticated API still
        # resolves the exact same assets if issuance is temporarily unavailable.
        return payload
    for container, key, path in records:
        if path in urls:
            container[key] = urls[path]
    return payload


@app.api_route("/api/books/{book_id}/assets/{relative:path}", methods=["GET", "HEAD"])
async def private_asset(book_id: str, relative: str, request: Request):
    store = getattr(app.state, "store_factory", PrivateBlobStore)()
    if not callable(getattr(store, "view_urls", None)):
        # Local/fake stores exercise the existing library-backed route.
        return await atelier_request(f"books/{book_id}/assets/{relative}", request)

    def resolve():
        with tempfile.TemporaryDirectory(prefix="colorart-asset-") as directory:
            snapshot = Snapshot(request.state.owner, Path(directory) / "atelier", store)
            value = store.get(snapshot.head_path)
            if value is None:
                raise HTTPException(404, "Ce fichier n’est pas disponible.")
            # The owner's private head is the authority for immutable assets.
            # No SQLite backup or image is downloaded just to redirect a preview.
            head = snapshot._decode_head(value)
            try:
                key, candidate = snapshot._relative(f"books/{book_id}/{relative}")
            except LibraryError as exc:
                raise HTTPException(404, "Ce fichier n’est pas disponible.") from exc
            if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf", ".zip", ".json", ".npz"}:
                raise HTTPException(404, "Ce fichier n’est pas disponible.")
            entry = head["files"].get(key)
            if entry is None:
                raise HTTPException(404, "Ce fichier n’est pas disponible.")
            return store.view_urls([entry["path"]])[entry["path"]]

    url = await anyio.to_thread.run_sync(resolve)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "private, no-store"})


@app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PATCH", "DELETE", "PUT"])
async def atelier_request(path: str, request: Request):
    maximum = 4 * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > maximum):
        return JSONResponse({"detail":"Utilise le transfert direct pour importer tes photos."}, status_code=413)
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            return JSONResponse({"detail":"Cette requête est trop volumineuse."}, status_code=413)
        chunks.append(chunk)
    content = b"".join(chunks)
    store = getattr(app.state, "store_factory", PrivateBlobStore)()
    with tempfile.TemporaryDirectory(prefix="colorart-request-") as directory:
        root = Path(directory) / "atelier"
        snapshot = Snapshot(request.state.owner, root, store)
        await anyio.to_thread.run_sync(snapshot.load)
        library = await anyio.to_thread.run_sync(snapshot.library)
        inner = create_app(root, library_instance=library, inline_render=True, cloud_assets=store)
        headers = {key:value for key,value in request.headers.items() if key.lower() not in {"host","cookie","content-length","connection"}}
        headers["cookie"] = COOKIE + "=" + request.state.owner
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=inner), timeout=None) as client:
            response = await client.request(request.method, str(request.url), headers=headers, content=content)
            body = response.content
        if (request.method not in {"GET","HEAD","OPTIONS"} and not path.endswith(("/uploads/prepare", "/uploads/prepare-batch")) and 200 <= response.status_code < 300):
            await anyio.to_thread.run_sync(snapshot.commit)
        if (200 <= response.status_code < 300
                and response.headers.get("content-type", "").startswith("application/json")
                and callable(getattr(store, "view_urls", None))):
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = None
            if isinstance(payload, dict):
                payload = await anyio.to_thread.run_sync(_decorate_book_views, payload, snapshot, store)
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        outgoing = {key:value for key,value in response.headers.items() if key.lower() not in {"content-length","content-encoding","transfer-encoding","set-cookie"}}
        return Response(content=body, status_code=response.status_code, headers=outgoing)
