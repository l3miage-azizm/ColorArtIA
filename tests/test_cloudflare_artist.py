"""Contract tests for the optional Cloudflare FLUX.2 Klein image-edit provider.

All network responses are simulated. These tests never generate a paid image.
"""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO

import httpx
from PIL import Image
import pytest

from core.artist import (
    ArtistError, CLOUDFLARE_MODEL, _cloudflare_reference,
    _configuration, _normalized_photo, _request, _validated_artwork,
    create_illustration, premium_available, gateway_readiness,
)


@pytest.fixture
def cloudflare(monkeypatch):
    for key in (
        "COLORART_IMAGE_BASE_URL", "COLORART_IMAGE_MODEL", "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN", "COLORART_IMAGE_API_KEY", "OPENAI_API_KEY",
        "AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "cloudflare")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-local-token")
    yield


def png(size=(1200, 1600)):
    stream = BytesIO()
    Image.new("RGB", size, (203, 158, 119)).save(stream, format="PNG")
    return stream.getvalue()


def cf_response_bytes(image_bytes=None):
    return httpx.Response(200, json={
        "success": True, "errors": [], "result": {
            "image": base64.b64encode(image_bytes or png((256, 384))).decode("ascii"),
        },
    })


def test_configuration_is_fixed_to_cloudflare_host_and_model(cloudflare):
    config = _configuration()
    assert config.provider == "cloudflare"
    assert config.model == CLOUDFLARE_MODEL
    assert config.base_url == (
        "https://api.cloudflare.com/client/v4/accounts/" + ("a" * 32) + "/ai/run"
    )
    assert premium_available() is True


@pytest.mark.parametrize("variable,value", [
    ("CLOUDFLARE_ACCOUNT_ID", "invalid"),
    ("COLORART_IMAGE_MODEL", "@cf/other/suspicious"),
    ("COLORART_IMAGE_BASE_URL", "https://attacker.example/steal"),
])
def test_invalid_endpoint_and_configuration_cannot_redirect_token(cloudflare, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ArtistError) as exc:
        _configuration()
    assert exc.value.code == "invalid_configuration"


def test_missing_token_fails_closed(cloudflare, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    with pytest.raises(ArtistError) as exc:
        _configuration()
    assert exc.value.code == "not_configured"
    assert premium_available() is False


def test_reference_resized_below_512_and_stripped_of_metadata(cloudflare, tmp_path):
    source = tmp_path / "photo.png"
    source.write_bytes(png())
    normal, digest = _normalized_photo(source)
    assert len(digest) == 64
    reference = _cloudflare_reference(normal)
    with Image.open(BytesIO(reference)) as image:
        assert image.format == "PNG"
        assert max(image.size) <= 496
        assert min(image.size) >= 128
        assert image.info == {}


def test_image_edit_request_and_atomic_png_result(cloudflare, tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(png())
    calls = []

    def reply(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.scheme == "https"
        assert request.url.host == "api.cloudflare.com"
        assert request.url.path.endswith("/ai/run/" + CLOUDFLARE_MODEL)
        assert request.headers["authorization"] == "Bearer test-local-token"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        body = request.read()
        assert b'name="input_image_0"' in body
        assert b'name="prompt"' in body
        assert b"ONLY visual reference" in body
        assert b'name="width"' in body and b"1024" in body
        assert b'name="height"' in body and b"1536" in body
        return cf_response_bytes()

    output = create_illustration(source, tmp_path / "result",
                                 transport=httpx.MockTransport(reply))
    assert len(calls) == 1
    assert output["path"].is_file()
    with Image.open(output["path"]) as image:
        assert image.format == "PNG"
        assert image.size == (256, 384)
    info = output["provenance"]
    assert info["provider"] == "cloudflare-workers-ai-flux-edit"
    assert info["model"] == CLOUDFLARE_MODEL
    assert info["human_review_required"] is True
    assert info["original_sha256"] != info["artwork_sha256"]


@pytest.mark.parametrize("bad_json", [
    {"success": False, "result": {"image": "abc"}},
    {"success": True, "result": {"image": "not-base64"}},
    {"success": True, "result": {"image": ""}},
    {"success": True, "result": {"url": "https://somewhere.example/image.png"}},
    {"success": True, "result": {"image": base64.b64encode(png((32, 32))).decode()}},
])
def test_invalid_cloudflare_images_never_get_accepted(bad_json):
    import json
    with pytest.raises(ArtistError) as exc:
        _validated_artwork(json.dumps(bad_json).encode(), provider="cloudflare")
    assert exc.value.code == "invalid_artwork"


def test_cloudflare_accepts_jpeg_but_normalizes_to_png():
    stream = BytesIO()
    Image.new("RGB", (256, 384), "blue").save(stream, format="JPEG")
    result = _validated_artwork(cf_response_bytes(stream.getvalue()).content,
                                provider="cloudflare")
    with Image.open(BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.size == (256, 384)


def test_http_429_no_retry_and_no_provider_secret_disclosure(cloudflare):
    calls = []

    def reply(request):
        calls.append(1)
        return httpx.Response(429, json={
            "error": {"message": "debug private test-local-token"}
        })

    config = _configuration()
    with pytest.raises(ArtistError) as exc:
        asyncio.run(_request(config, png((256, 384)), httpx.MockTransport(reply)))
    assert exc.value.code == "rate_limited"
    assert "test-local-token" not in str(exc.value)
    assert len(calls) == 1


def test_cloudflare_readiness_does_not_generate_or_consume_credits(cloudflare):
    def fail_if_called(_request):
        raise AssertionError("A readiness check must not send an inference request")

    status = asyncio.run(gateway_readiness(transport=httpx.MockTransport(fail_if_called)))
    assert status == {"available": True, "provider": "cloudflare",
                      "reason": "configured", "action_url": None}


def test_existing_openai_response_remains_compatible():
    import json
    encoded = base64.b64encode(png((256, 384))).decode("ascii")
    old_format = {"data": [{"b64_json": encoded}]}
    valid = _validated_artwork(json.dumps(old_format).encode())
    with Image.open(BytesIO(valid)) as image:
        assert image.format == "PNG"
