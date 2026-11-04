"""Live Vercel acceptance test using curl and the real rendering engine.

The fixture is procedural artwork, not a quality benchmark or a user photograph.
All cookies and signed storage URLs stay in an automatically removed private
temporary directory. Reports contain only public URLs, IDs, timings and hashes.
This script never creates a payment, order, or print request.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import numpy as np
from PIL import Image, ImageDraw
import pypdfium2 as pdfium


class SmokeFailure(RuntimeError):
    """A deliberately sanitized failure message, safe for a test report."""


def check(value, message):
    if not value:
        raise SmokeFailure(message)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def write_private(path, body):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)


def write_report(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def private_blob_url(value):
    parsed = urlsplit(value)
    check(parsed.scheme == "https" and parsed.hostname
          and (parsed.hostname == "blob.vercel-storage.com" or parsed.hostname.endswith(".blob.vercel-storage.com")
               or (parsed.hostname == "vercel.com" and (parsed.path == "/api/blob" or parsed.path.startswith("/api/blob/"))))
          and not parsed.username and not parsed.password
          and parsed.port in {None, 443}, "The server returned an invalid storage destination.")
    return value


def config_string(value):
    # curl config quoting is not shell quoting; subprocess never invokes a shell.
    check(isinstance(value, str) and not any(char in value for char in "\r\n\x00"),
          "An invalid request setting was generated.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_headers(raw):
    headers = {}
    for block in raw.replace(b"\r\n", b"\n").split(b"\n\n"):
        lines = block.split(b"\n")
        if not lines or not lines[0].startswith(b"HTTP/"):
            continue
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                key = name.decode("ascii", errors="ignore").lower()
                decoded = value.strip().decode("latin-1")
                headers[key] = headers.get(key, "") + ("\n" if key in headers else "") + decoded
    return headers


@dataclass
class Result:
    status: int
    body: bytes
    headers: dict
    duration: float

    def json(self):
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeError) as exc:
            raise SmokeFailure("The application returned a non-JSON response.") from exc
        check(isinstance(value, dict), "The application returned an unexpected JSON response.")
        return value


class CurlTransport:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.counter = 0

    def request(self, method, url, *, timeout=60, headers=None, payload=None,
                upload=None, cookie_jar=None):
        check(payload is None or upload is None, "A request cannot contain two bodies.")
        self.counter += 1
        prefix = self.directory / f"request-{self.counter:03d}"
        output = prefix.with_suffix(".body")
        response_headers = prefix.with_suffix(".headers")
        write_private(output, b"")
        write_private(response_headers, b"")
        options = [
            "silent", "show-error", "proto = \"=https\"",
            "connect-timeout = 15", f"max-time = {int(timeout)}",
            "request = " + config_string(method),
            "url = " + config_string(url),
            "output = " + config_string(str(output)),
            "dump-header = " + config_string(str(response_headers)),
            'write-out = "%{http_code}\\t%{time_total}\\t%{size_download}"',
        ]
        for key, value in (headers or {}).items():
            options.append("header = " + config_string(f"{key}: {value}"))
        if cookie_jar is not None:
            options.extend([
                "cookie = " + config_string(str(cookie_jar)),
                "cookie-jar = " + config_string(str(cookie_jar)),
            ])
        if payload is not None:
            data_path = prefix.with_suffix(".json")
            write_private(data_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            options.extend([
                'header = "Content-Type: application/json"',
                "data-binary = " + config_string("@" + str(data_path)),
            ])
        if upload is not None:
            options.append("data-binary = " + config_string("@" + str(upload)))
        try:
            completed = subprocess.run(["curl", "--config", "-"],
                input="\n".join(options) + "\n", capture_output=True, text=True,
                timeout=timeout + 20, check=False)
        except subprocess.TimeoutExpired as exc:
            raise SmokeFailure("A request exceeded its time limit.") from exc
        except OSError as exc:
            raise SmokeFailure("The curl executable could not be started.") from exc
        check(completed.returncode == 0,
              f"curl failed with exit code {completed.returncode}; credentials and URL omitted.")
        try:
            fields = completed.stdout.strip().split("\t")
            status, duration = int(fields[0]), float(fields[1])
        except (IndexError, ValueError) as exc:
            raise SmokeFailure("curl returned invalid request statistics.") from exc
        return Result(status, output.read_bytes(), parse_headers(response_headers.read_bytes()), duration)


class Session:
    def __init__(self, base_url, directory, transport, name):
        self.url, self.transport = base_url.rstrip("/"), transport
        self.cookie_jar = Path(directory) / (name + ".cookies")
        write_private(self.cookie_jar, b"")
        self.last_cookie_flags = None

    def request(self, method, path, *, expected=200, headers=None, payload=None, timeout=60):
        check(path.startswith("/") and not path.startswith("//")
              and not urlsplit(path).netloc, "An API path must stay on the application origin.")
        merged = {"Origin": self.url, "Cache-Control": "no-cache", **(headers or {})}
        result = self.transport.request(method, self.url + path, timeout=timeout,
            headers=merged, payload=payload, cookie_jar=self.cookie_jar)
        if "set-cookie" in result.headers:
            # Only flags enter reports; never retain the bearer cookie value.
            raw = result.headers["set-cookie"].casefold()
            self.last_cookie_flags = {
                "secure": "; secure" in raw, "httponly": "; httponly" in raw,
                "samesite_strict": "samesite=strict" in raw,
            }
        allowed = {expected} if isinstance(expected, int) else set(expected)
        if result.status not in allowed:
            detail = ""
            try:
                detail = str(result.json().get("detail", ""))[:240]
            except SmokeFailure:
                pass
            raise SmokeFailure(f"{method} {path.split('/assets/')[0]} returned HTTP {result.status}; expected {sorted(allowed)}. {detail}")
        return result

    def asset(self, path):
        response = self.request("GET", path, expected=(200, 302, 303, 307))
        if response.status == 200:
            return response
        location = response.headers.get("location", "")
        private_blob_url(location)
        downloaded = self.transport.request("GET", location, timeout=120,
                                            headers={"Origin": self.url})
        check(downloaded.status == 200, f"A private asset download returned HTTP {downloaded.status}.")
        return downloaded


def procedural_photo():
    """A structured portrait-like fixture with gradients, texture and small edges."""
    rng = np.random.default_rng(20261101)
    width, height = 840, 1120
    y, x = np.mgrid[:height, :width]
    background = np.empty((height, width, 3), dtype=np.float32)
    background[..., 0] = 150 + 55 * x / width + 20 * y / height
    background[..., 1] = 176 - 30 * x / width + 18 * np.sin(y / 150)
    background[..., 2] = 159 + 32 * np.cos(x / 220)
    # Small correlated variations add real segmentation work without making
    # the source an unstructured random-noise stress image.
    small = rng.integers(85, 170, (140, 105, 3), dtype=np.uint8)
    texture = np.asarray(Image.fromarray(small).resize((width, height), Image.Resampling.BICUBIC))
    pixels = np.clip(background + (texture.astype(np.float32) - 127) * 0.4, 0, 255).astype(np.uint8)
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 760, width, height), fill=(70, 108, 81))
    for index in range(34):
        left = int(rng.integers(0, width))
        top = int(rng.integers(460, height))
        radius = int(rng.integers(10, 37))
        draw.ellipse((left-radius, top-radius, left+radius, top+radius),
                     fill=(145 + index % 50, 158 + index % 45, 112 + index % 55))
    draw.rounded_rectangle((186, 668, 672, 1120), radius=75, fill=(69, 104, 150))
    draw.ellipse((155, 118, 686, 743), fill=(75, 55, 46))
    draw.ellipse((202, 187, 641, 754), fill=(218, 171, 137))
    draw.pieslice((152, 115, 686, 495), 178, 356, fill=(66, 49, 43))
    for left in (285, 495):
        draw.arc((left-20, 365, left+70, 411), 190, 340, fill=(72, 51, 43), width=10)
        draw.ellipse((left, 409, left+42, 434), fill=(240, 233, 220))
        draw.ellipse((left+15, 408, left+29, 434), fill=(67, 66, 51))
        draw.ellipse((left+19, 412, left+25, 430), fill=(29, 30, 28))
    draw.line((418, 432, 401, 515, 433, 523), fill=(166, 119, 94), width=5)
    draw.arc((328, 505, 507, 604), 12, 166, fill=(135, 74, 70), width=9)
    draw.line((339, 553, 494, 553), fill=(229, 205, 182), width=4)
    for position in range(220, 660, 21):
        draw.line((position, 735, position-40, 1088), fill=(87, 125, 169), width=3)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def run(args, report):
    parsed = urlsplit(args.url)
    check(parsed.scheme == "https" and parsed.hostname and not parsed.username
          and not parsed.password and not parsed.query and not parsed.fragment
          and parsed.path in {"", "/"}, "--url must be a public HTTPS application origin.")
    base_url = args.url.rstrip("/")
    report["url"] = base_url
    with tempfile.TemporaryDirectory(prefix="colorart-live-smoke-") as directory:
        os.chmod(directory, 0o700)
        transport = CurlTransport(directory)
        owner = Session(base_url, directory, transport, "owner")
        outsider = Session(base_url, directory, transport, "outsider")

        def phase(name, action):
            started = time.monotonic()
            report["current_phase"] = name
            write_report(args.report, report)
            try:
                value = action()
            except Exception:
                report["checks"].append({"phase": name, "status": "failed",
                                         "seconds": round(time.monotonic() - started, 3)})
                raise
            elapsed = round(time.monotonic() - started, 3)
            report["checks"].append({"phase": name, "status": "passed", "seconds": elapsed})
            write_report(args.report, report)
            print(json.dumps({"phase": name, "status": "passed", "seconds": elapsed}), flush=True)
            return value

        health = phase("health", lambda: owner.request("GET", "/api/health").json())
        check(health.get("hosting") == "vercel" and health.get("status") == "ok", "Unexpected deployment health.")
        check(health.get("printer") == "not_configured", "This test expects printing to remain disconnected.")
        check(health.get("storage_configured") is True, "Private storage is not configured.")
        report["version"] = health.get("version")
        report["cookie_flags"] = owner.last_cookie_flags
        check(owner.last_cookie_flags and all(owner.last_cookie_flags.values()), "The owner cookie lacks required privacy flags.")
        catalogue = phase("catalogue", lambda: owner.request("GET", "/api/catalog").json())
        check(sum(item.get("kind") in {"original", "palette"} for item in catalogue.get("universes", [])) == 15 and len(catalogue.get("templates", [])) == 3,
              "The public catalogue is incomplete.")
        check(catalogue.get("direct_uploads") is True and catalogue.get("generation_mode") == "awaited",
              "Unexpected cloud upload/render contract.")
        report["premium_enabled"] = catalogue.get("premium_enabled")
        report["printing_connected"] = catalogue.get("print_configured")
        check(catalogue.get("print_configured") is False, "Printing must remain disconnected for this smoke.")

        book = phase("create_book", lambda: owner.request("POST", "/api/books", expected=201, payload={
            "title": "Un souvenir pour Élodie", "recipient": "Élodie", "template": "heritage",
            "target_photos": 1, "dedication": "Un cadeau unique, couleur après couleur.",
        }).json())
        book_id = book["id"]
        report["book_id"] = book_id
        original_revision = book["revision"]
        image = procedural_photo()
        image_path = Path(directory) / "procedural-fixture.png"
        write_private(image_path, image)
        report["fixture"] = {"type": "procedural_portrait", "bytes": len(image), "sha256": sha(image)}
        signed = phase("prepare_direct_upload", lambda: owner.request("POST", f"/api/books/{book_id}/uploads/prepare", payload={
            "filename": "souvenir-test.png", "content_type": "image/png", "size": len(image),
        }).json())
        destination = private_blob_url(signed.get("upload_url", ""))
        upload_headers = signed.get("headers", {})
        check(isinstance(upload_headers, dict) and upload_headers.get("Content-Type") == "image/png",
              "The upload authorization returned invalid headers.")
        def preflight_upload():
            response = transport.request("OPTIONS", destination, headers={
                "Origin": base_url, "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            })
            check(response.status in {200, 204}, "The upload CORS preflight was rejected.")
            check(response.headers.get("access-control-allow-origin") in {"*", base_url},
                  "The upload CORS origin is not allowed.")
            check("put" in response.headers.get("access-control-allow-methods", "").lower(),
                  "The upload CORS policy does not allow PUT.")
        phase("direct_upload_cors", preflight_upload)
        def upload_image():
            response = transport.request("PUT", destination, timeout=120,
                headers={**upload_headers, "Origin": base_url}, upload=image_path)
            check(response.status in {200, 201, 204}, f"The direct upload returned HTTP {response.status}.")
            return response
        phase("direct_upload", upload_image)
        imported = phase("import_uploaded_photo", lambda: owner.request("POST", f"/api/books/{book_id}/photos/import", payload={
            "pathname": signed["pathname"], "filename": "souvenir-test.png",
        }).json())
        check(imported.get("errors") == [] and len(imported.get("added", [])) == 1, "The uploaded photo was not imported.")
        book = imported["book"]
        page_id = book["pages"][0]["id"]

        def isolation_checks():
            outsider.request("GET", "/api/health")
            check(outsider.request("GET", "/api/books").json()["books"] == [], "Another browser can list the owner's books.")
            outsider.request("GET", f"/api/books/{book_id}", expected=404)
            outsider.request("GET", book["pages"][0]["source_url"], expected=404)
            owner.request("PATCH", f"/api/books/{book_id}", expected=403,
                headers={"Origin": "https://hostile.example"}, payload={"title": "Must be rejected"})
            check(owner.request("GET", f"/api/books/{book_id}").json()["title"] == book["title"],
                  "The rejected cross-origin edit changed the book.")
        phase("owner_and_origin_isolation", isolation_checks)

        generated = phase("real_render_3200_regions", lambda: owner.request("POST",
            f"/api/books/{book_id}/photos/{page_id}/generate", expected=202, timeout=240, payload={
                "universe": "original", "n_colors": 24, "difficulty": "balanced", "fit_mode": "contain",
                "target_regions": 3200, "masking": "mystery", "artwork_mode": "local",
            }).json())
        check(generated.get("status") == "succeeded" and generated.get("progress") == 100,
              "The real renderer did not finish successfully.")
        report["job_id"] = generated["id"]
        persisted_job = phase("persisted_completed_job", lambda: owner.request("GET", f"/api/jobs/{generated['id']}").json())
        check(persisted_job.get("status") == "succeeded" and persisted_job.get("result") == generated.get("result"),
              "A fresh request lost the completed render job.")
        book = owner.request("GET", f"/api/books/{book_id}").json()
        variant = next(item for item in book["pages"][0]["variants"]
                       if item["id"] == generated["result"]["variant_id"])
        metadata = phase("private_render_metadata", lambda: owner.asset(variant["files"]["metadata"]).json())
        config, quality = metadata.get("config", {}), metadata.get("quality", {})
        check(config.get("target_width") == 2480 and config.get("target_height") == 3508
              and config.get("target_regions") == 3200, "The renderer did not produce the requested A4 configuration.")
        check(quality.get("label_coverage") == 1 and not quality.get("errors")
              and quality.get("unlabeled_regions", 0) == 0, "The generated drawing has uncoded or blocked regions.")
        report["render"] = {"target_regions": 3200, "actual_regions": metadata.get("n_regions"),
                            "label_coverage": quality.get("label_coverage"),
                            "unlabeled_regions": quality.get("unlabeled_regions", 0)}

        phase("favorite", lambda: owner.request("POST", f"/api/books/{book_id}/photos/{page_id}/variants/{variant['id']}/favorite"))
        phase("validate_drawing", lambda: owner.request("POST",
            f"/api/books/{book_id}/photos/{page_id}/variants/{variant['id']}/select", payload={"accept_warnings": True}))
        restored = phase("fresh_request_book", lambda: owner.request("GET", f"/api/books/{book_id}").json())
        restored_variant = next(v for v in restored["pages"][0]["variants"] if v["id"] == variant["id"])
        check(restored_variant.get("favorite") is True and restored["pages"][0].get("selected_variant_id") == variant["id"],
              "A fresh request lost the favorite or approved drawing.")
        ready = owner.request("GET", f"/api/books/{book_id}/readiness").json()
        check(ready.get("ready") is True, "The approved book is not ready for export.")
        owner.request("POST", f"/api/books/{book_id}/exports", expected=409,
                      payload={"approved_revision": original_revision})
        export = phase("export_gift_book", lambda: owner.request("POST", f"/api/books/{book_id}/exports", expected=201,
            payload={"approved_revision": restored["revision"]}, timeout=180).json())
        check(export.get("print_configured") is False, "The test unexpectedly connected to a printer.")
        latest = owner.request("GET", f"/api/books/{book_id}/exports").json()
        check(latest.get("id") == export.get("id"), "A fresh request lost the completed PDF export.")
        report["pdfs"] = {}
        for key, expected_pages in (("interior_pdf", 4), ("solutions_pdf", 1), ("cover_pdf", 1)):
            downloaded = phase("download_" + key, lambda key=key: owner.asset(export["files"][key]))
            check(downloaded.body.startswith(b"%PDF-"), f"The {key} download is not a PDF document.")
            document = pdfium.PdfDocument(downloaded.body)
            try:
                pages = len(document)
            finally:
                document.close()
            check(pages == expected_pages, f"The {key} contains an unexpected number of pages.")
            report["pdfs"][key] = {"pages": pages, "bytes": len(downloaded.body), "sha256": sha(downloaded.body)}
        report["created_payments"] = 0
        report["created_orders"] = 0
        report["created_print_requests"] = 0
        report.pop("current_phase", None)
        report["status"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Public HTTPS application origin")
    parser.add_argument("--report", type=Path, required=True, help="Sanitized JSON report destination")
    args = parser.parse_args()
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    started = time.monotonic()
    try:
        run(args, report)
    except SmokeFailure as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    except Exception as exc:
        # Do not accidentally expose signed URLs, response bodies or cookies
        # from a dependency exception's message or traceback.
        report["status"] = "failed"
        report["error"] = "Unexpected local error: " + type(exc).__name__
    report["seconds"] = round(time.monotonic() - started, 3)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(args.report, report)
    print(json.dumps({"status": report["status"], "seconds": report["seconds"],
                      **({"error": report["error"]} if "error" in report else {})}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
