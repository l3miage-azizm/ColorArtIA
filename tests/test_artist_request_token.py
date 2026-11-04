"""Rotating Vercel identity is scoped to one request, including worker threads."""
import asyncio
import anyio
import pytest
from core import artist


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    monkeypatch.setenv("COLORART_IMAGE_PROVIDER", "vercel_gateway")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)


def test_rotating_header_is_used_then_cleared():
    for value in ("first-request-token", "rotated-request-token"):
        with artist.artist_request_context({"x-vercel-oidc-token": value}):
            assert artist._configuration().api_key == value
        assert not artist.premium_available()


def test_untrusted_local_header_does_not_configure_service(monkeypatch):
    monkeypatch.delenv("VERCEL")
    with artist.artist_request_context({"x-vercel-oidc-token": "untrusted"}):
        assert not artist.premium_available()


def test_key_priority_and_nested_context_restoration(monkeypatch):
    with artist.artist_request_context({"x-vercel-oidc-token": "outer"}):
        with artist.artist_request_context({"x-vercel-oidc-token": "inner"}):
            assert artist._configuration().api_key == "inner"
        assert artist._configuration().api_key == "outer"
        monkeypatch.setenv("AI_GATEWAY_API_KEY", "configured-key")
        assert artist._configuration().api_key == "configured-key"


def test_concurrent_contexts_reach_worker_without_leaking():
    async def worker(value):
        with artist.artist_request_context({"x-vercel-oidc-token": value}):
            await asyncio.sleep(0)
            return await anyio.to_thread.run_sync(lambda: artist._configuration().api_key)
    async def run():
        return await asyncio.gather(worker("request-one"), worker("request-two"))
    assert asyncio.run(run()) == ["request-one", "request-two"]
    assert not artist.premium_available()
