"""Public studio choices: illustrated scenes and colour treatments stay distinct."""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import os
import threading
from time import monotonic

from .art_universes import ART_UNIVERSES, art_universe_catalog
from .universes import UNIVERSES


_AUTO_READY_TTL = 30.0
_AUTO_UNAVAILABLE_TTL = 12.0
_AUTO_CACHE_LIMIT = 32
_AUTO_READINESS: OrderedDict[str, tuple[float, bool]] = OrderedDict()
_AUTO_READINESS_LOCK = threading.Lock()


@dataclass(frozen=True)
class StudioSelection:
    key: str
    art_universe: str
    palette_universe: str


def resolve_selection(key: str) -> StudioSelection:
    """Accept only catalogue scene/palette pairs; never accept arbitrary prompts."""
    if key in UNIVERSES:
        return StudioSelection(key, "original", key)
    if key in ART_UNIVERSES:
        return StudioSelection(key, key, "original")
    if isinstance(key, str) and key.count("__") == 1:
        art, palette = key.split("__")
        if art in ART_UNIVERSES and palette in UNIVERSES:
            return StudioSelection(art if palette == "original" else key, art, palette)
    raise ValueError("Choisis un univers disponible.")


def _automatic_readiness() -> bool:
    """Cache only a safe boolean, scoped to the current validated credential.

    Tokens can rotate per Vercel request. Hashing the validated configuration
    prevents one token's readiness being applied to another token or provider;
    no raw token, credit amount or provider response is retained in this cache.
    The lock also coalesces concurrent catalogue requests into one read-only
    service check. The underlying check has its own bounded network timeout.
    """
    from .artist import _configuration, artist_service_status
    try:
        config = _configuration()
    except ValueError:
        return False
    digest = hashlib.sha256()
    for value in (config.provider, config.base_url, config.model, config.api_key):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    key, provider = digest.hexdigest(), config.provider
    del config, value
    with _AUTO_READINESS_LOCK:
        now = monotonic()
        for stale in [cached for cached, (expires, _) in _AUTO_READINESS.items() if expires <= now]:
            del _AUTO_READINESS[stale]
        cached = _AUTO_READINESS.get(key)
        if cached is not None:
            _AUTO_READINESS.move_to_end(key)
            return cached[1]
        try:
            status = artist_service_status()
            # Fail closed on malformed or cross-provider readiness responses.
            enabled = (isinstance(status, dict) and status.get("available") is True
                       and status.get("provider") == provider
                       and status.get("reason") == ("ready" if provider == "vercel_gateway" else "configured"))
        except Exception:
            # A catalogue remains usable when a readiness adapter is temporarily
            # unavailable. Never cache an exception body or log its credentials.
            enabled = False
        ttl = _AUTO_READY_TTL if enabled else _AUTO_UNAVAILABLE_TTL
        _AUTO_READINESS[key] = (monotonic() + ttl, enabled)
        while len(_AUTO_READINESS) > _AUTO_CACHE_LIMIT:
            _AUTO_READINESS.popitem(last=False)
        return enabled


def art_service_enabled() -> bool:
    """Explicit activation, deactivation or short-lived automatic readiness.

    ``auto`` performs only a cached, read-only service check: funding the
    Gateway account unlocks the studio after a reload without a redeployment.
    Unset Gateway configuration remains disabled; direct providers retain their
    historical explicit-key activation.
    """
    from .artist import premium_available
    flag = os.environ.get("COLORART_ARTISTIC_READY", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag == "auto":
        return _automatic_readiness()
    if os.environ.get("COLORART_IMAGE_PROVIDER", "").strip().lower() == "vercel_gateway" and flag not in {"1", "true", "yes", "on"}:
        return False
    return premium_available()


def studio_universes() -> list[dict]:
    enabled = art_service_enabled()
    colours = [{"key": key, "label": value.label, "description": value.description,
                "kind": "original" if key == "original" else "palette",
                "available": True}
               for key, value in UNIVERSES.items()]
    return colours + [{**item, "available": enabled} for item in art_universe_catalog()]
