"""Image-edit transport contracts, not a substitute for visual AI evaluation.

All images returned here are fixtures. These tests spend no provider credits and
make no claim about facial likeness or the quality of a generated illustration.
"""
import asyncio
import base64
from email import policy
from email.parser import BytesParser
import hashlib
from io import BytesIO
import json

import httpx
from PIL import Image, PngImagePlugin
import pytest

from core import artist
from core.art_universes import ART_UNIVERSES, build_art_prompt
from core.universes import UNIVERSES


@pytest.fixture(autouse=True)
def isolated_service_configuration(monkeypatch):
    # Tests must neither use nor disclose the operator's real credentials.
    for key in ("COLORART_IMAGE_PROVIDER", "COLORART_IMAGE_API_KEY", "OPENAI_API_KEY",
                "COLORART_IMAGE_BASE_URL", "COLORART_IMAGE_MODEL", "AI_GATEWAY_API_KEY",
                "VERCEL_OIDC_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-direct-key")


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "original.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("GPSLatitude", "private-location")
    info.add_text("Author", "private-author")
    Image.new("RGBA", (128, 192), (50, 100, 150, 0)).save(path, pnginfo=info)
    return path


@pytest.fixture
def artwork_bytes():
    output = BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Provider", "private-provider-metadata")
    Image.new("RGBA", (128, 192), (80, 120, 160, 0)).save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def multipart(request):
    envelope = ("Content-Type: " + request.headers["content-type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
    message = BytesParser(policy=policy.default).parsebytes(envelope + request.content)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in message.iter_parts()}


def recording_transport(artwork_bytes, calls):
    def handler(request):
        calls.append((request, multipart(request)))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(artwork_bytes).decode()}]})
    return httpx.MockTransport(handler)


def use_gateway(monkeypatch):
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "vercel_gateway")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "test.oidc.signature")


@pytest.mark.parametrize("universe_key", list(UNIVERSES))
def test_original_and_legacy_palettes_preserve_original_drawing_contract(photo, artwork_bytes, tmp_path, universe_key):
    calls = []
    result = artist.create_illustration(photo, tmp_path / "output", universe_key,
                                        transport=recording_transport(artwork_bytes, calls))
    assert len(calls) == 1
    request, data = calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/images/edits"
    assert request.headers["authorization"] == "Bearer test-direct-key"
    assert data["prompt"].decode() == artist.ILLUSTRATION_PROMPT
    assert data["model"] == b"gpt-image-1.5"
    assert data["input_fidelity"] == b"high"
    assert data["size"] == b"1024x1536"
    assert data["quality"] == b"high"
    assert data["n"] == b"1"
    assert data["output_format"] == b"png"
    assert data["background"] == b"opaque"
    provenance = result["provenance"]
    assert provenance["universe_key"] == universe_key
    assert provenance["prompt_version"] == artist.PROMPT_VERSION
    assert provenance["prompt_sha256"] == hashlib.sha256(data["prompt"]).hexdigest()
    assert provenance["original_sha256"] == hashlib.sha256(photo.read_bytes()).hexdigest()
    assert provenance["human_review_required"] is True


@pytest.mark.parametrize("universe_key", list(ART_UNIVERSES))
def test_art_universe_sends_its_complete_prompt_without_photo_only_contradictions(photo, artwork_bytes, tmp_path, universe_key):
    calls = []
    result = artist.create_illustration(photo, tmp_path / "output", universe_key,
                                        transport=recording_transport(artwork_bytes, calls))
    prompt = calls[0][1]["prompt"].decode()
    assert prompt == build_art_prompt(universe_key)
    assert ART_UNIVERSES[universe_key].prompt.strip() in prompt
    assert "Do not add flowers, clouds, trees" not in prompt
    assert "original composition" not in prompt
    assert result["provenance"]["universe_key"] == universe_key
    assert result["provenance"]["prompt_version"] == artist.ART_PROMPT_VERSION
    assert result["provenance"]["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()


def test_original_prompt_retains_likeness_ink_and_anti_painting_instructions():
    prompt = artist.ILLUSTRATION_PROMPT
    assert "recognizable facial proportions, natural asymmetry, age, expression, gaze" in prompt
    assert "Do not enlarge the eyes" in prompt
    assert "no automatic posterization, no pixel\nmosaic, no low-poly facets" in prompt
    assert "no watercolour or oil painting" in prompt
    assert "The main visual feature MUST be clean BLACK PEN LINEWORK" in prompt
    assert "The ink, rather than shading, must describe the face." in prompt
    assert "Do not add flowers, clouds, trees" in prompt


def test_uploaded_and_saved_images_are_clean_opaque_pngs(photo, artwork_bytes, tmp_path):
    original = photo.read_bytes()
    calls = []
    result = artist.create_illustration(photo, tmp_path / "output", "art_floral",
                                        transport=recording_transport(artwork_bytes, calls))
    with Image.open(BytesIO(calls[0][1]["image"])) as upload:
        assert upload.format == "PNG"
        assert upload.mode == "RGB"
        assert upload.getpixel((0, 0)) == (255, 255, 255)
        assert upload.info == {}
        assert not upload.getexif()
    with Image.open(result["path"]) as output:
        assert output.format == "PNG"
        assert output.mode == "RGB"
        assert output.info == {}
        assert not output.getexif()
    assert photo.read_bytes() == original
    assert result["provenance"]["artwork_sha256"] == hashlib.sha256(result["path"].read_bytes()).hexdigest()
    assert not list((tmp_path / "output").glob(".artwork_*"))


def test_gateway_uses_oidc_fixed_endpoint_and_documented_image_two_contract(monkeypatch, photo, artwork_bytes, tmp_path):
    use_gateway(monkeypatch)
    calls = []
    result = artist.create_illustration(photo, tmp_path / "output", "art_space_explorer",
                                        transport=recording_transport(artwork_bytes, calls))
    request, data = calls[0]
    assert str(request.url) == "https://ai-gateway.vercel.sh/v1/images/edits"
    assert request.headers["authorization"] == "Bearer test.oidc.signature"
    assert data["model"] == b"openai/gpt-image-2"
    assert "input_fidelity" not in data
    assert result["provenance"]["provider"] == "vercel-ai-gateway-images-edit"
    assert "test.oidc.signature" not in json.dumps(result["provenance"])
    assert "test.oidc.signature" not in repr(artist._configuration())


def test_gateway_key_precedes_oidc_and_requests_do_not_leak_previous_prompt(monkeypatch, photo, artwork_bytes, tmp_path):
    use_gateway(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    calls = []
    for universe_key in ("art_floral", "original", "art_royal"):
        artist.create_illustration(photo, tmp_path / "output", universe_key,
                                   transport=recording_transport(artwork_bytes, calls))
    assert [data["prompt"].decode() for _, data in calls] == [build_art_prompt("art_floral"),
            artist.ILLUSTRATION_PROMPT, build_art_prompt("art_royal")]
    assert all(request.headers["authorization"] == "Bearer test-gateway-key" for request, _ in calls)


def test_gateway_image_one_point_five_retains_high_fidelity_when_explicitly_configured(monkeypatch, photo, artwork_bytes, tmp_path):
    use_gateway(monkeypatch)
    monkeypatch.setenv("COLORART_IMAGE_MODEL", "openai/gpt-image-1.5")
    calls = []
    artist.create_illustration(photo, tmp_path / "output", transport=recording_transport(artwork_bytes, calls))
    assert calls[0][1]["model"] == b"openai/gpt-image-1.5"
    assert calls[0][1]["input_fidelity"] == b"high"


@pytest.mark.parametrize("model", ["https://example.com/model", "openai/../../model", "gpt-image-2",
                                    "openai/gpt-image-2?key=value", "openai/model\nextra", "openai/" + "x" * 154])
def test_gateway_rejects_invalid_model_before_any_request(monkeypatch, model):
    use_gateway(monkeypatch)
    monkeypatch.setenv("COLORART_IMAGE_MODEL", model)
    assert not artist.premium_available()
    with pytest.raises(artist.ArtistError) as failure:
        artist._configuration()
    assert failure.value.code == "invalid_configuration"


def test_gateway_refuses_endpoint_override_and_never_falls_back_to_provider_key(monkeypatch):
    use_gateway(monkeypatch)
    monkeypatch.setenv("COLORART_IMAGE_BASE_URL", "https://untrusted.example/v1")
    assert not artist.premium_available()
    monkeypatch.delenv("COLORART_IMAGE_BASE_URL")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN")
    assert not artist.premium_available()  # OPENAI_API_KEY exists but has the wrong scope.
    assert artist.artist_service_status()["reason"] == "not_configured"


@pytest.mark.parametrize("universe_key", ["not-an-universe", "art_unknown", "", None, [], {"prompt": "override"}])
def test_unknown_universe_is_rejected_before_reading_source_or_calling_provider(tmp_path, universe_key):
    with pytest.raises(artist.ArtistError) as failure:
        artist.create_illustration(tmp_path / "missing.png", tmp_path / "output", universe_key)
    assert failure.value.code == "invalid_universe"
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("status,code,expected", [(402, "insufficient_funds", "insufficient_funds"),
    (402, "quota_for_entity_exceeded", "quota_for_entity_exceeded"),
    (403, "customer_verification_required", "customer_verification_required"),
    (401, "invalid_api_key", "authentication_failed"), (429, "rate_limit", "rate_limited"),
    (503, "provider_error", "provider_rejected")])
def test_paid_errors_are_safe_not_retried_and_do_not_save_artwork(monkeypatch, photo, tmp_path, status, code, expected):
    use_gateway(monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": code,
            "message": "test.oidc.signature private provider response", "action_url": "https://evil.example/steal"}})
    with pytest.raises(artist.ArtistError) as failure:
        artist.create_illustration(photo, tmp_path / "output", "art_royal", transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert failure.value.code == expected
    assert failure.value.action_url is None
    assert "test.oidc.signature" not in str(failure.value)
    assert "private provider response" not in str(failure.value)
    assert not (tmp_path / "output").exists()


def test_paid_timeout_is_not_retried(monkeypatch, photo, tmp_path):
    use_gateway(monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private provider error", request=request)
    with pytest.raises(artist.ArtistError) as failure:
        artist.create_illustration(photo, tmp_path / "output", transport=httpx.MockTransport(handler))
    assert failure.value.code == "timeout"
    assert "aucune relance automatique" in str(failure.value)
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [{"data": [{"url": "https://evil.example/track"}]},
                                    {"data": []}, {"data": [{"b64_json": "invalid base64"}]}])
def test_bad_artwork_never_fetches_provider_urls_or_replaces_source(photo, tmp_path, payload):
    original = photo.read_bytes()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)
    with pytest.raises(artist.ArtistError) as failure:
        artist.create_illustration(photo, tmp_path / "output", transport=httpx.MockTransport(handler))
    assert failure.value.code == "invalid_artwork"
    assert len(calls) == 1
    assert photo.read_bytes() == original
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("balance,available,reason", [("95.50", True, "ready"), ("0", False, "insufficient_funds"),
    ("-0.01", False, "insufficient_funds"), ("NaN", False, "invalid_response"),
    ("Infinity", False, "invalid_response"), (True, False, "invalid_response"), (None, False, "invalid_response")])
def test_gateway_readiness_is_get_only_and_never_publishes_money_or_credentials(monkeypatch, balance, available, reason):
    use_gateway(monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://ai-gateway.vercel.sh/v1/credits"
        assert request.headers["authorization"] == "Bearer test.oidc.signature"
        return httpx.Response(200, json={"balance": balance, "total_used": "123.45", "secret": "test.oidc.signature"})
    result = artist.artist_service_status(transport=httpx.MockTransport(handler))
    assert result == {"available": available, "provider": "vercel_gateway", "reason": reason, "action_url": None}
    assert len(calls) == 1
    assert "123.45" not in json.dumps(result)
    assert "test.oidc.signature" not in json.dumps(result)


@pytest.mark.parametrize("action_url,expected", [
    ("https://vercel.com/d?to=%2Fmy%20team%2F%7E%2Fai-gateway", "https://vercel.com/d?to=%2Fmy%20team%2F%7E%2Fai-gateway"),
    ("https://vercel.com.evil.example/activate", None), ("http://vercel.com/activate", None),
    ("https://vercel.com@evil.example/activate", None), ("https://user@vercel.com/activate", None),
    ("https://vercel.com:444/activate", None), ("https://evil.example/activate", None),
    ("https://vercel.com/activate?token=test.oidc.signature", None), ("javascript:alert(1)", None),
    ("https://vercel.com/activate\nprivate", None)])
def test_verification_status_only_returns_safe_vercel_action_links(monkeypatch, action_url, expected):
    use_gateway(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(403, json={"error": {
        "type": "customer_verification_required", "message": "never copy private details", "action_url": action_url}}))
    result = asyncio.run(artist.gateway_readiness(transport=transport))
    assert result == {"available": False, "provider": "vercel_gateway",
                      "reason": "customer_verification_required", "action_url": expected}


def test_gateway_readiness_does_not_follow_redirects(monkeypatch):
    use_gateway(monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})
    assert artist.artist_service_status(transport=httpx.MockTransport(handler))["available"] is False
    assert len(calls) == 1


def test_gateway_readiness_handles_unreachable_malformed_and_oversized_results(monkeypatch):
    use_gateway(monkeypatch)
    responses = [httpx.Response(200, content=b"not json"),
                 httpx.Response(200, content=b"{}", headers={"content-length": str(artist.MAX_STATUS_BYTES + 1)})]
    for response in responses:
        assert artist.artist_service_status(transport=httpx.MockTransport(lambda request: response))["reason"] == "invalid_response"
    def timeout(request):
        raise httpx.ConnectTimeout("test.oidc.signature", request=request)
    assert artist.artist_service_status(transport=httpx.MockTransport(timeout))["reason"] == "timeout"


def test_configuration_readiness_never_creates_an_http_client(monkeypatch):
    use_gateway(monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("configuration readiness must not make a network call")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert artist.premium_available()
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "openai")
    assert artist.artist_service_status() == {"available": True, "provider": "openai",
                                            "reason": "configured", "action_url": None}
