"""Acceptance test for the deployed one-click studio, using its real APIs.

Creates one private test book, uploads three procedural photos concurrently,
renders A4 artwork and two palette-only universes, then downloads the book PDFs.
This writes synthetic test data but never creates a payment or print request.
Cookies and scoped URLs remain in a private, automatically deleted directory.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, ImageDraw
import pypdfium2 as pdfium

from live_smoke import (CurlTransport, Session, SmokeFailure, check,
                        private_blob_url, procedural_photo, sha, write_private,
                        write_report)


class StudioSession(Session):
    def asset(self, value, *, cors=False):
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            check(parsed.scheme == "https" and parsed.hostname
                  and parsed.hostname.endswith(".blob.vercel-storage.com")
                  and not parsed.username and not parsed.password
                  and parsed.port in {None, 443}, "Invalid private asset destination.")
            result = self.transport.request("GET", value, timeout=120,
                                            headers={"Origin": self.url})
            check(result.status == 200, f"Private asset returned HTTP {result.status}.")
        else:
            result = super().asset(value)
        if cors:
            check(result.headers.get("access-control-allow-origin") in {"*", self.url},
                  "Private preview download does not permit browser canvas access.")
        return result


def fixture_photos(directory):
    with Image.open(BytesIO(procedural_photo())) as image:
        base = image.convert("RGB").resize((420, 560), Image.Resampling.LANCZOS)
    files = []
    for index in range(3):
        image = base.copy()
        ImageDraw.Draw(image).rectangle((12, base.height - 36, 98, base.height - 11),
                                       fill=[(86, 120, 93), (139, 99, 113), (102, 114, 149)][index])
        output = BytesIO()
        if index == 0:
            image.save(output, format="JPEG", quality=96, subsampling=0)
            extension, media_type = "jpg", "image/jpeg"
        else:
            image.save(output, format="PNG", optimize=True)
            extension, media_type = "png", "image/png"
        body = output.getvalue()
        path = Path(directory) / f"souvenir-{index + 1}.{extension}"
        write_private(path, body)
        files.append({"path": path, "filename": path.name, "bytes": len(body), "sha256": sha(body), "content_type": media_type})
    return files


def rgb(body):
    with Image.open(BytesIO(body)) as image:
        return np.asarray(image.convert("RGB"))


def compose(flat, mask, color, composition):
    result = flat.copy()
    color = np.asarray(color, dtype=np.uint16)
    if composition == "multiply":
        result[mask] = ((flat[mask].astype(np.uint16) * color + 127) // 255).astype(np.uint8)
    elif composition == "darken":
        result[mask] = np.minimum(flat[mask], color).astype(np.uint8)
    elif composition == "replace":
        result[mask] = color.astype(np.uint8)
    else:
        raise SmokeFailure("Unsupported preview ink composition.")
    return result


def run(args, report):
    origin = urlsplit(args.url)
    check(origin.scheme == "https" and origin.hostname and not origin.username
          and not origin.password and not origin.query and not origin.fragment
          and origin.path in {"", "/"}, "--url must be a public HTTPS application origin.")
    base_url = args.url.rstrip("/")
    report["url"] = base_url
    with tempfile.TemporaryDirectory(prefix="colorart-live-studio-") as directory:
        os.chmod(directory, 0o700)
        transport = CurlTransport(directory)
        owner = StudioSession(base_url, directory, transport, "owner")
        outsider = StudioSession(base_url, directory, transport, "outsider")

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
            duration = round(time.monotonic() - started, 3)
            report["checks"].append({"phase": name, "status": "passed", "seconds": duration})
            write_report(args.report, report)
            print(json.dumps({"phase": name, "status": "passed", "seconds": duration}), flush=True)
            return value

        health = phase("health", lambda: owner.request("GET", "/api/health").json())
        check(health.get("hosting") == "vercel" and health.get("storage_configured") is True,
              "The deployed private studio is not configured.")
        check(owner.last_cookie_flags and all(owner.last_cookie_flags.values()),
              "The owner cookie lacks privacy flags.")
        report["cookie_flags"] = owner.last_cookie_flags
        report["version"] = health.get("version")
        catalog = phase("catalogue", lambda: owner.request("GET", "/api/catalog").json())
        check(catalog.get("batch_uploads") is True and catalog.get("direct_uploads") is True,
              "Batch direct uploads are not enabled.")
        choices = catalog.get("universes", [])
        check(sum(item.get("kind") in {"original", "palette"} for item in choices) == 15,
              "The colour palette catalogue is incomplete.")
        check(sum(item.get("kind") == "artistic" for item in choices) == 18
              and sum(bool(item.get("is_new")) for item in choices) == 15,
              "The artistic universe catalogue is incomplete.")
        check(catalog.get("instant_universes") is False, "New scenes must not be advertised as instant colour filters.")
        report["premium_enabled"] = catalog.get("premium_enabled")
        report["printing_connected"] = catalog.get("print_configured")
        check(catalog.get("print_configured") is False, "The test expects printing to remain disconnected.")

        book = phase("create_three_photo_book", lambda: owner.request("POST", "/api/books", expected=201,
            payload={"title": "Le studio de nos souvenirs", "recipient": "Élodie",
                     "target_photos": 3, "template": "heritage"}).json())
        book_id = book["id"]
        report["book_id"] = book_id
        fixtures = fixture_photos(directory)
        report["fixtures"] = [{k: value for k, value in item.items() if k != "path"} for item in fixtures]
        prepared = phase("prepare_three_uploads", lambda: owner.request("POST",
            f"/api/books/{book_id}/uploads/prepare-batch", payload={"files": [
                {"filename": item["filename"], "content_type": item["content_type"], "size": item["bytes"]}
                for item in fixtures]}).json())
        uploads = prepared.get("uploads")
        check(isinstance(uploads, list) and len(uploads) == 3, "A batch upload authorization is missing.")
        check([item.get("filename") for item in uploads] == [item["filename"] for item in fixtures],
              "Batch upload order changed.")

        def preflight():
            destination = private_blob_url(uploads[0].get("upload_url", ""))
            response = transport.request("OPTIONS", destination, headers={"Origin": base_url,
                "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "content-type"})
            check(response.status in {200, 204}, "The upload browser preflight was rejected.")
            check(response.headers.get("access-control-allow-origin") in {"*", base_url},
                  "The upload origin is not allowed.")
            methods = response.headers.get("access-control-allow-methods", "").lower()
            check("put" in methods or methods == "*", "The upload method is not allowed.")
        phase("direct_upload_cors", preflight)

        def upload_all():
            # CurlTransport's filenames/counter are request-local. Give every
            # concurrent transfer its own private directory instead of sharing it.
            def transfer(pair):
                index, (authorization, fixture) = pair
                slot = Path(directory) / f"transfer-{index}"
                slot.mkdir(mode=0o700)
                client = CurlTransport(slot)
                destination = private_blob_url(authorization.get("upload_url", ""))
                headers = authorization.get("headers") or {}
                check(headers.get("Content-Type") == fixture["content_type"], "Unexpected upload media type.")
                result = client.request("PUT", destination, timeout=120,
                    headers={**headers, "Origin": base_url}, upload=fixture["path"])
                check(result.status in {200, 201, 204}, f"A direct upload returned HTTP {result.status}.")
                return result.duration
            with ThreadPoolExecutor(max_workers=3) as executor:
                durations = list(executor.map(transfer, enumerate(zip(uploads, fixtures))))
            report["parallel_transfer_seconds"] = durations
        phase("three_parallel_transfers", upload_all)
        imported = phase("import_three_photos_one_batch", lambda: owner.request("POST",
            f"/api/books/{book_id}/photos/import-batch", timeout=120, payload={"files": [
                {"pathname": item["pathname"], "filename": item["filename"]} for item in uploads]}).json())
        check(imported.get("errors") == [] and len(imported.get("added", [])) == 3,
              "The three-photo batch was not fully imported.")
        book = imported["book"]
        check(book.get("photo_count") == 3 and len(book.get("pages", [])) == 3,
              "The imported book has an unexpected page count.")
        check([page["filename"] for page in book["pages"]] == [item["filename"] for item in fixtures],
              "Imported photo order changed.")
        page_id = book["pages"][0]["id"]
        def verify_jpeg_source():
            downloaded = owner.asset(book["pages"][0]["source_url"])
            check(np.array_equal(rgb(downloaded.body), rgb(fixtures[0]["path"].read_bytes())),
                  "JPEG storage changed normalized source pixels.")
            check(downloaded.body.startswith(b"\xff\xd8"), "JPEG source was expanded into PNG.")
            report["jpeg_stored_bytes"] = len(downloaded.body)
        phase("jpeg_source_preserved_without_png_expansion", verify_jpeg_source)

        def isolation():
            outsider.request("GET", "/api/health")
            check(outsider.request("GET", "/api/books").json()["books"] == [],
                  "Another browser can list the owner's books.")
            outsider.request("GET", f"/api/books/{book_id}", expected=404)
            # Use the authenticated API path, rather than a deliberately shared
            # short-lived signed URL, for the owner-bound asset check.
            outsider.request("GET", f"/api/books/{book_id}/assets/photos/{page_id}/source.png", expected=404)
            owner.request("PATCH", f"/api/books/{book_id}", expected=403,
                headers={"Origin": "https://hostile.example"}, payload={"title": "Rejected edit"})
        phase("owner_and_origin_isolation", isolation)

        settings = {"n_colors": 24, "difficulty": "balanced", "fit_mode": "contain",
                    "target_regions": 3200, "masking": "mystery", "artwork_mode": "local"}
        variants, maps, masks = {}, {}, {}
        report["renders"] = {}
        for universe in ("original", "ocean", "golden"):
            job = phase("render_" + universe, lambda universe=universe: owner.request("POST",
                f"/api/books/{book_id}/photos/{page_id}/generate", expected=202, timeout=240,
                payload={**settings, "universe": universe}).json())
            check(job.get("status") == "succeeded" and job.get("progress") == 100,
                  "A drawing did not finish successfully.")
            persisted = owner.request("GET", f"/api/jobs/{job['id']}").json()
            check(persisted.get("status") == "succeeded" and persisted.get("result") == job.get("result"),
                  "The completed drawing was not persisted.")
            book = owner.request("GET", f"/api/books/{book_id}").json()
            page = next(page for page in book["pages"] if page["id"] == page_id)
            variant = next(value for value in page["variants"] if value["id"] == job["result"]["variant_id"])
            check(variant.get("universe") == universe, "The returned variant does not match the selected universe.")
            variants[universe] = variant

            def verify_variant():
                metadata = owner.asset(variant["files"]["metadata"]).json()
                config, quality = metadata.get("config", {}), metadata.get("quality", {})
                check(config.get("target_width") == 2480 and config.get("target_height") == 3508,
                      "The drawing is not an A4 300-ppi rendering.")
                check(quality.get("label_coverage") == 1 and not quality.get("errors")
                      and quality.get("unlabeled_regions", 0) == 0, "The drawing contains uncoded or blocked regions.")
                with np.load(BytesIO(owner.asset(variant["files"]["regions"]).body), allow_pickle=False) as arrays:
                    data = {key: arrays[key].copy() for key in arrays.files}
                maps[universe] = data
                worksheet = owner.asset(variant["files"]["mystery"]).body
                masks[universe] = sha(worksheet)
                final = rgb(owner.asset(variant["files"]["solution_final"]).body)
                flat = data["palette"][data["region_to_color"][data["labels"]]]
                composition = str(data.get("ink_composition", np.array("multiply")).item())
                rebuilt = compose(flat, data["ink_mask"].astype(bool), data["ink_color"], composition)
                check(np.array_equal(final, rebuilt), "The saved coloured drawing does not reconstruct from its region data.")
                preview = owner.asset(variant["files"]["studio_preview"], cors=True).json()
                indexed = rgb(owner.asset(variant["files"]["studio_index"], cors=True).body)
                check(len(preview.get("palettes", {})) == 15, "A universe preview palette is missing.")
                check(np.array_equal(preview["palettes"][universe], data["palette"]),
                      "The immediate preview palette differs from the saved drawing.")
                width, height = preview["width"], preview["height"]
                check(indexed.shape == (height, width, 3), "The indexed preview dimensions are invalid.")
                xs = np.arange(width) * preview["source_width"] // width
                ys = np.arange(height) * preview["source_height"] // height
                sampled = data["labels"][np.ix_(ys, xs)]
                check(np.array_equal(indexed[:, :, 0], data["region_to_color"][sampled]),
                      "The preview indexes differ from the saved geometry.")
                preview_rgb = np.asarray(preview["palettes"][universe], dtype=np.uint8)[indexed[:, :, 0]]
                preview_rgb = compose(preview_rgb, indexed[:, :, 1] > 0,
                                      preview["ink_color"], preview["ink_composition"])
                check(np.array_equal(preview_rgb, final[np.ix_(ys, xs)]),
                      "The browser preview differs from the corresponding saved pixels.")
                if universe != "original":
                    for key in ("labels", "region_to_color", "ink_mask", "ink_color", "ink_composition"):
                        check(np.array_equal(data[key], maps["original"][key]), "A universe changed the drawing geometry or ink.")
                    check(masks[universe] == masks["original"], "A universe changed the validated masking.")
                    check(metadata.get("universe_reuse", {}).get("geometry_reused") is True,
                          "A universe did not record geometry reuse.")
                report["renders"][universe] = {"variant_id": variant["id"],
                    "actual_regions": metadata.get("n_regions"), "label_coverage": quality.get("label_coverage"),
                    "worksheet_sha256": masks[universe], "exact_reconstruction": True,
                    "exact_preview": True, "preview_get_cors": True}
            phase("verify_" + universe + "_geometry_preview_and_cors", verify_variant)

        def verify_cache():
            before = {value["id"] for value in page["variants"]}
            job = owner.request("POST", f"/api/books/{book_id}/photos/{page_id}/generate", expected=202,
                timeout=120, payload={**settings, "universe": "ocean"}).json()
            check(job.get("status") == "succeeded" and job.get("result", {}).get("cache_hit") is True,
                  "The saved universe was needlessly regenerated.")
            check(job["result"].get("variant_id") == variants["ocean"]["id"], "The cached result returned the wrong variant.")
            current = owner.request("GET", f"/api/books/{book_id}").json()
            values = next(value for value in current["pages"] if value["id"] == page_id)["variants"]
            check({value["id"] for value in values} == before, "A repeated universe click created a duplicate variant.")
        phase("repeated_universe_uses_saved_variant", verify_cache)

        selected_id = variants["golden"]["id"]
        phase("favorite", lambda: owner.request("POST",
            f"/api/books/{book_id}/photos/{page_id}/variants/{selected_id}/favorite"))
        phase("validate_drawing", lambda: owner.request("POST",
            f"/api/books/{book_id}/photos/{page_id}/variants/{selected_id}/select",
            payload={"accept_warnings": True}))
        restored = owner.request("GET", f"/api/books/{book_id}").json()
        page = next(value for value in restored["pages"] if value["id"] == page_id)
        check(page.get("selected_variant_id") == selected_id and
              next(value for value in page["variants"] if value["id"] == selected_id).get("favorite") is True,
              "The favorite or approved drawing was lost across fresh requests.")

        def trim_book():
            # The extra two photos exercised batch imports. Remove those unused
            # pages before exporting a one-drawing gift book, without spending
            # two additional full renders purely to test the PDF route.
            for value in restored["pages"]:
                if value["id"] != page_id:
                    owner.request("DELETE", f"/api/books/{book_id}/photos/{value['id']}")
            return owner.request("PATCH", f"/api/books/{book_id}", payload={"target_photos": 1}).json()
        ready_book = phase("prepare_single_drawing_export", trim_book)
        readiness = owner.request("GET", f"/api/books/{book_id}/readiness").json()
        check(readiness.get("ready") is True, "The approved book is not exportable.")
        exported = phase("export_gift_book", lambda: owner.request("POST", f"/api/books/{book_id}/exports",
            expected=201, timeout=180, payload={"approved_revision": ready_book["revision"]}).json())
        check(exported.get("print_configured") is False, "The test unexpectedly connected an order provider.")
        report["pdfs"] = {}
        for key, expected_pages in (("interior_pdf", 4), ("solutions_pdf", 1), ("cover_pdf", 1)):
            downloaded = phase("download_" + key, lambda key=key: owner.asset(exported["files"][key]))
            check(downloaded.body.startswith(b"%PDF-"), "An exported document is not a PDF.")
            document = pdfium.PdfDocument(downloaded.body)
            try:
                count = len(document)
            finally:
                document.close()
            check(count == expected_pages, "An exported PDF has an unexpected page count.")
            report["pdfs"][key] = {"pages": count, "bytes": len(downloaded.body), "sha256": sha(downloaded.body)}
        report["created_payments"] = report["created_orders"] = report["created_print_requests"] = 0
        report.pop("current_phase", None)
        report["status"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    try:
        run(args, report)
    except SmokeFailure as exc:
        report.update(status="failed", error=str(exc))
    except Exception as exc:
        report.update(status="failed", error="Unexpected local error: " + type(exc).__name__)
    report["seconds"] = round(time.monotonic() - started, 3)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(args.report, report)
    print(json.dumps({"status": report["status"], "seconds": report["seconds"],
                      **({"error": report["error"]} if "error" in report else {})}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
