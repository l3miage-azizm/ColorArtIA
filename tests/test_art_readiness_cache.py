"""Automatic Gateway availability uses a bounded read-only status cache.

No image is generated and no external service is called by these tests.
"""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import core.artist as artist
import core.studio_catalog as catalogue


@pytest.fixture
def auto_service(monkeypatch):
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "auto")
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "vercel_gateway")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-auto-gateway-one")
    monkeypatch.delenv("COLORART_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("COLORART_IMAGE_BASE_URL", raising=False)
    clock = [1000.0]
    monkeypatch.setattr(catalogue, "monotonic", lambda: clock[0])
    state = {"available": True, "provider": "vercel_gateway", "reason": "ready"}
    calls = []

    def status():
        calls.append(True)
        return dict(state)

    monkeypatch.setattr(artist, "artist_service_status", status)
    with catalogue._AUTO_READINESS_LOCK:
        catalogue._AUTO_READINESS.clear()
    yield clock, state, calls
    with catalogue._AUTO_READINESS_LOCK:
        catalogue._AUTO_READINESS.clear()


def test_positive_status_is_reused_for_30_seconds(auto_service):
    clock, state, calls = auto_service
    assert catalogue.art_service_enabled() is True
    state.update(available=False, reason="insufficient_funds")
    clock[0] += 29.9
    assert catalogue.art_service_enabled() is True
    assert len(calls) == 1
    clock[0] += .2
    assert catalogue.art_service_enabled() is False
    assert len(calls) == 2


def test_funding_unlocks_after_negative_cache_expires_without_flag_change(auto_service):
    clock, state, calls = auto_service
    state.update(available=False, reason="insufficient_funds")
    assert catalogue.art_service_enabled() is False
    state.update(available=True, reason="ready")
    clock[0] += 11.9
    assert catalogue.art_service_enabled() is False
    clock[0] += .2
    assert catalogue.art_service_enabled() is True
    assert len(calls) == 2


def test_credential_rotation_cannot_reuse_another_credentials_readiness(auto_service, monkeypatch):
    _, state, calls = auto_service
    assert catalogue.art_service_enabled() is True
    state.update(available=False, reason="authentication_failed")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-auto-gateway-two")
    assert catalogue.art_service_enabled() is False
    assert len(calls) == 2


def test_rotating_request_oidc_context_isolated_and_not_retained(auto_service, monkeypatch):
    _, state, calls = auto_service
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with artist.artist_request_context({"x-vercel-oidc-token": "request-token-one"}):
        assert catalogue.art_service_enabled() is True
    # A cached positive result must never substitute for a missing token.
    assert catalogue.art_service_enabled() is False
    state.update(available=False, reason="authentication_failed")
    with artist.artist_request_context({"x-vercel-oidc-token": "request-token-two"}):
        assert catalogue.art_service_enabled() is False
    assert len(calls) == 2
    assert "request-token" not in repr(catalogue._AUTO_READINESS)


@pytest.mark.parametrize("response", [None, [], True, {"available": 1, "provider": "vercel_gateway", "reason": "ready"},
    {"available": True, "provider": "openai", "reason": "ready"},
    {"available": True, "provider": "vercel_gateway", "reason": "unexpected"},
    {"available": True}, {"balance": "100"}])
def test_malformed_or_wrong_provider_status_never_enables_art(auto_service, monkeypatch, response):
    monkeypatch.setattr(artist, "artist_service_status", lambda: response)
    assert catalogue.art_service_enabled() is False


def test_adapter_failure_is_cached_as_unavailable_and_does_not_break_catalogue(auto_service, monkeypatch):
    calls = []

    def broken_status():
        calls.append(True)
        raise RuntimeError("untrusted provider body")

    monkeypatch.setattr(artist, "artist_service_status", broken_status)
    entries = catalogue.studio_universes()
    assert all(not entry["available"] for entry in entries if entry["kind"] == "artistic")
    assert catalogue.art_service_enabled() is False
    assert len(calls) == 1


def test_invalid_provider_configuration_never_checks_service(auto_service, monkeypatch):
    _, _, calls = auto_service
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "unrecognised")
    assert catalogue.art_service_enabled() is False
    assert calls == []


def test_disabled_or_explicit_flag_never_performs_readiness_request(auto_service, monkeypatch):
    _, _, calls = auto_service
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "0")
    assert catalogue.art_service_enabled() is False
    monkeypatch.setenv("COLORART_ARTISTIC_READY", "1")
    assert catalogue.art_service_enabled() is True
    monkeypatch.delenv("COLORART_ARTISTIC_READY")
    assert catalogue.art_service_enabled() is False
    assert calls == []


def test_cache_is_bounded_and_retains_only_hashed_keys_and_boolean_results(auto_service, monkeypatch):
    _, state, _ = auto_service
    state.update(balance="123.456", credential="sensitive-provider-response")
    for index in range(catalogue._AUTO_CACHE_LIMIT + 4):
        monkeypatch.setenv("AI_GATEWAY_API_KEY", f"rotating-test-credential-{index}")
        assert catalogue.art_service_enabled() is True
    cache = catalogue._AUTO_READINESS
    assert len(cache) == catalogue._AUTO_CACHE_LIMIT
    assert all(len(key) == 64 and all(char in "0123456789abcdef" for char in key) for key in cache)
    assert all(isinstance(expires, float) and enabled is True for expires, enabled in cache.values())
    assert "rotating-test-credential" not in repr(cache)
    assert "123.456" not in repr(cache)
    assert "sensitive-provider-response" not in repr(cache)


def test_concurrent_catalogue_requests_share_one_read_only_probe(auto_service, monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def status():
        calls.append(True)
        entered.set()
        assert release.wait(timeout=2)
        return {"available": True, "provider": "vercel_gateway", "reason": "ready"}

    monkeypatch.setattr(artist, "artist_service_status", status)
    with ThreadPoolExecutor(max_workers=5) as workers:
        futures = [workers.submit(catalogue.art_service_enabled) for _ in range(5)]
        assert entered.wait(timeout=2)
        release.set()
        assert all(future.result(timeout=2) is True for future in futures)
    assert len(calls) == 1
