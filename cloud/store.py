"""Private Blob access through the official JavaScript SDK on this deployment.

The Python SDK currently lacks conditional PUT and presigned URLs. The small
authenticated bridge uses those supported SDK operations; large files travel
directly to Blob, outside Function request/response size limits.
"""
from __future__ import annotations

import base64
import hashlib
import os
from collections import OrderedDict
import threading
import time
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from .state import BlobValue, CloudConflict, CloudUnavailable


MAX_SMALL = 2 * 1024 * 1024
MAX_FILE = 500 * 1024 * 1024
MAX_PHOTO = 20 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
OWNER = re.compile(r"^[a-f0-9]{64}$")
IDENT = re.compile(r"^[a-f0-9]{32}$")
_VIEW_CACHE = OrderedDict()
_VIEW_CACHE_LOCK = threading.Lock()
_VIEW_CACHE_LIMIT = 4096


def private_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname
            or not (parsed.hostname == "blob.vercel-storage.com" or parsed.hostname.endswith(".blob.vercel-storage.com")
                    or (parsed.hostname == "vercel.com" and (parsed.path == "/api/blob" or parsed.path.startswith("/api/blob/"))))
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise CloudUnavailable("L’adresse de stockage reçue est invalide.")
    return url


class PrivateBlobStore:
    def __init__(self, *, bridge_url: str | None = None, secret: str | None = None):
        host = os.environ.get("VERCEL_URL", "")
        self.bridge_url = bridge_url or ("https://" + host + "/api/storage" if host else "")
        self.secret = secret or os.environ.get("COLORART_STORAGE_BRIDGE_SECRET", "")
        if not self.bridge_url or len(self.secret) < 48:
            raise CloudUnavailable("Le stockage privé de cet atelier n’est pas encore configuré.")

    def _request(self, payload: dict, *, missing=False) -> dict | None:
        try:
            response = httpx.post(self.bridge_url, json=payload,
                headers={"Authorization": "Bearer " + self.secret}, timeout=90)
            if missing and response.status_code == 404:
                return None
            if response.status_code in {409, 412}:
                raise CloudConflict()
            if response.status_code >= 400:
                raise CloudUnavailable()
            result = response.json()
            if not isinstance(result, dict):
                raise CloudUnavailable()
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudUnavailable() from exc

    def get(self, path: str) -> BlobValue | None:
        result = self._request({"op": "read", "path": path}, missing=True)
        if result is None:
            return None
        etag = result.get("etag")
        if not isinstance(etag, str) or not etag:
            raise CloudUnavailable()
        if "body" in result:
            try:
                body = base64.b64decode(result["body"], validate=True)
            except (ValueError, TypeError) as exc:
                raise CloudUnavailable() from exc
        else:
            try:
                url = private_url(result["url"])
                with httpx.stream("GET", url, timeout=120) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE:
                            raise CloudUnavailable("Le fichier dépasse la taille autorisée.")
                        chunks.append(chunk)
                    body = b"".join(chunks)
            except (httpx.HTTPError, KeyError) as exc:
                raise CloudUnavailable() from exc
        return BlobValue(body, etag)

    def put(self, path: str, body: bytes | Path, *, if_match: str | None = None,
            create_only=False, content_type="application/octet-stream") -> BlobValue:
        size = body.stat().st_size if isinstance(body, Path) else len(body)
        if size > MAX_FILE or (if_match is None and not create_only):
            raise CloudUnavailable("La sauvegarde doit être conditionnelle.")
        options = {"path": path, "ifMatch": if_match, "createOnly": create_only,
                   "contentType": content_type}
        if size <= MAX_SMALL:
            content = body.read_bytes() if isinstance(body, Path) else body
            result = self._request({"op": "put", **options,
                "body": base64.b64encode(content).decode("ascii")})
        else:
            if if_match is not None:
                # Heads stay below MAX_SMALL; never weaken their CAS semantics.
                raise CloudUnavailable("Le manifeste de l’atelier est trop volumineux.")
            signed = self._request({"op": "sign_put", **options, "size": size})
            url = private_url(signed["url"])
            headers = {"Content-Type": content_type, "Content-Length": str(size)}
            try:
                if isinstance(body, Path):
                    with body.open("rb") as handle:
                        response = httpx.put(url, content=iter(lambda: handle.read(256 * 1024), b""),
                                             headers=headers, timeout=180)
                else:
                    response = httpx.put(url, content=body, headers=headers, timeout=180)
                if response.status_code in {409, 412}:
                    raise CloudConflict()
                if response.status_code >= 400:
                    existing = self._request({"op": "sign_get", "path": path}, missing=True)
                    if existing is not None:
                        raise CloudConflict()
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError:
                    result = {}
                if not result.get("etag"):
                    # Only immutable objects use this path. Their identity
                    # cannot race with an overwrite while retrieving metadata.
                    result = self._request({"op": "sign_get", "path": path})
            except httpx.HTTPError as exc:
                raise CloudUnavailable() from exc
        etag = result.get("etag")
        if not isinstance(etag, str) or not etag:
            raise CloudUnavailable("La confirmation de la sauvegarde est incomplète.")
        return BlobValue(b"", etag)

    @staticmethod
    def namespace(owner: str) -> str:
        if not OWNER.fullmatch(owner):
            raise HTTPException(404, "Cet atelier n’est pas disponible.")
        return "owners/" + hashlib.sha256(owner.encode()).hexdigest()

    def prepare_upload(self, owner, book_id, filename, content_type, size):
        if not IDENT.fullmatch(book_id) or content_type not in IMAGE_TYPES:
            raise HTTPException(422, "Choisis une photo JPEG, PNG ou WebP.")
        if type(size) is not int or not 0 < size <= MAX_PHOTO:
            raise HTTPException(422, "Chaque photo doit peser au maximum 20 Mo.")
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
        pathname = f"{self.namespace(owner)}/uploads/{book_id}/{uuid.uuid4().hex}{suffix}"
        result = self._request({"op": "sign_put", "path": pathname, "size": size,
            "contentType": content_type, "createOnly": True})
        return {"pathname": pathname, "upload_url": private_url(result["url"]),
                "headers": {"Content-Type": content_type}}

    def prepare_uploads(self, owner, book_id, files):
        if not IDENT.fullmatch(book_id) or not isinstance(files, (list, tuple)) or not 1 <= len(files) <= 8:
            raise HTTPException(422, "Choisis entre une et huit photos à la fois.")
        namespace = self.namespace(owner)
        prepared, originals = [], []
        for item in files:
            item = item.model_dump() if hasattr(item, "model_dump") else item
            if not isinstance(item, dict):
                raise HTTPException(422, "Cette photo n’est pas valide.")
            content_type, size, filename = item.get("content_type"), item.get("size"), item.get("filename")
            if content_type not in IMAGE_TYPES or not isinstance(filename, str) or not 1 <= len(filename) <= 255:
                raise HTTPException(422, "Choisis une photo JPEG, PNG ou WebP.")
            if type(size) is not int or not 0 < size <= MAX_PHOTO:
                raise HTTPException(422, "Chaque photo doit peser au maximum 20 Mo.")
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
            path = f"{namespace}/uploads/{book_id}/{uuid.uuid4().hex}{suffix}"
            prepared.append({"path": path, "size": size, "contentType": content_type})
            originals.append(filename)
        result = self._request({"op": "sign_many_put", "files": prepared})
        returned = result.get("uploads")
        if not isinstance(returned, list) or len(returned) != len(prepared):
            raise CloudUnavailable()
        uploads = []
        for source, result, filename in zip(prepared, returned, originals):
            if result.get("path") != source["path"]:
                raise CloudUnavailable()
            uploads.append({"pathname": source["path"], "upload_url": private_url(result["url"]),
                "headers": {"Content-Type": source["contentType"]}, "filename": filename})
        return {"uploads": uploads}

    def view_urls(self, paths):
        # Only callers that already authorized these immutable paths may use
        # this method. No mutable head or database bytes reach the browser.
        paths = list(dict.fromkeys(paths))
        if not paths:
            return {}
        pattern = re.compile(r"^owners/[a-f0-9]{64}/objects/[a-f0-9]{64}$")
        if any(not isinstance(path, str) or not pattern.fullmatch(path) for path in paths):
            raise CloudUnavailable("Un aperçu de cet atelier est invalide.")
        owner = paths[0].split('/')[1]
        if any(path.split('/')[1] != owner for path in paths):
            raise CloudUnavailable("Les aperçus doivent appartenir au même atelier.")
        urls, missing = {}, []
        now = time.monotonic()
        with _VIEW_CACHE_LOCK:
            for path in paths:
                cache_key = (self.bridge_url, path)
                cached = _VIEW_CACHE.get(cache_key)
                if cached is not None and cached[0] > now:
                    urls[path] = cached[1]
                    _VIEW_CACHE.move_to_end(cache_key)
                else:
                    _VIEW_CACHE.pop(cache_key, None)
                    missing.append(path)
        for start in range(0, len(missing), 120):
            subset = missing[start:start + 120]
            result = self._request({"op": "sign_many_get", "paths": subset})
            batch = result.get("urls")
            if not isinstance(batch, dict) or set(batch) != set(subset):
                raise CloudUnavailable()
            expires = result.get("expiresAt")
            # Scoped URLs are an optional immutable cache, never an authority.
            # Retain a full minute of browser access and do not cache when a
            # signing server does not report its actual expiration.
            lifetime = min(240, max(0, expires / 1000 - time.time() - 30)) if type(expires) in {int, float} else 0
            fresh = {path: private_url(batch[path]) for path in subset}
            urls.update(fresh)
            if lifetime > 0:
                with _VIEW_CACHE_LOCK:
                    for path, url in fresh.items():
                        key = (self.bridge_url, path)
                        _VIEW_CACHE[key] = (time.monotonic() + lifetime, url)
                        _VIEW_CACHE.move_to_end(key)
                    while len(_VIEW_CACHE) > _VIEW_CACHE_LIMIT:
                        _VIEW_CACHE.popitem(last=False)
        return urls

    def read_upload(self, owner, book_id, pathname):
        prefix = f"{self.namespace(owner)}/uploads/{book_id}/"
        if (not isinstance(pathname, str) or not pathname.startswith(prefix)
                or not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|webp)", pathname[len(prefix):])):
            raise HTTPException(404, "Cette photo n’appartient pas à ton atelier.")
        value = self.get(pathname)
        if value is None:
            raise HTTPException(404, "Le transfert de cette photo n’est pas terminé.")
        if len(value.body) > MAX_PHOTO:
            raise HTTPException(422, "Cette photo dépasse la taille autorisée.")
        return value.body

    def asset_response(self, library, book_id, relative):
        candidate = library.asset_reference(book_id, relative)
        if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf", ".zip", ".json", ".npz"}:
            raise HTTPException(404, "Ce fichier n’est pas disponible.")
        key = candidate.relative_to(library.root).as_posix()
        entry = library.snapshot.files.get(key)
        if not entry:
            raise HTTPException(404, "Ce fichier n’est pas disponible.")
        url = self.view_urls([entry["path"]])[entry["path"]]
        return RedirectResponse(url, status_code=302,
                                headers={"Cache-Control": "private, no-store"})
