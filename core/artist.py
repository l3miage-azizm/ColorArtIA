"""Optional upstream illustration service, separate from colouring segmentation.

Configuration belongs to the server. A photo is sent only when the controller
explicitly requests the premium workflow; this module never replaces a failed
remote request with a local approximation and never retries a charged request.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, urlsplit
import uuid
import warnings

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .universes import UNIVERSES
from .art_universes import ART_UNIVERSES, build_art_prompt

PROMPT_VERSION = "colorart-line-drawing-2"
ART_PROMPT_VERSION = "colorart-art-worlds-1"
GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
GATEWAY_DEFAULT_MODEL = "openai/gpt-image-2"
MAX_STATUS_BYTES = 64 * 1024
TOTAL_TIMEOUT_SECONDS = 165
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
_REQUEST_OIDC_TOKEN: ContextVar[str | None] = ContextVar("colorart_request_oidc", default=None)


@contextmanager
def artist_request_context(headers):
    """Use the current platform token, isolated across concurrent requests.

    Vercel Functions receive rotating OIDC tokens in a request header rather
    than a persistent environment variable. Never retain one globally or accept
    this header outside the Vercel runtime. AnyIO copies this context into the
    rendering worker thread.
    """
    value = headers.get("x-vercel-oidc-token") if os.environ.get("VERCEL") == "1" else None
    token = _REQUEST_OIDC_TOKEN.set(value)
    try:
        yield
    finally:
        _REQUEST_OIDC_TOKEN.reset(token)

ILLUSTRATION_PROMPT = """Create ONE refined, premium COLORED DRAWING for an adult
colouring gift book from the supplied photograph. Preserve the specific people's
recognizable facial proportions, natural asymmetry, age, expression, gaze, hair,
pose, clothing, objects and original composition. Do not enlarge the eyes, beautify
the face, change the body, reframe, change pose, or invent a different identity.
Draw the scene intentionally with elegant, clean and controlled ink contours,
smooth closed shapes, thoughtfully grouped details and approximately 24–32
harmonious flat colour families. Use precise drawn eyelids, eyebrows, nose and lip
structure to preserve likeness. Simplify hair, fur, fabric and background into
medium-detail, physically colourable shapes rather than thousands of tiny hairs.
The result must clearly read as a professional illustrator's finished DRAWING:
no photographic skin texture, no pore noise, no automatic posterization, no pixel
mosaic, no low-poly facets, no watercolour or oil painting, no paint brush texture,
and no photorealistic gradients. Use restrained cel-shaded shadow shapes, retaining
the photo's lighting and emotional character. Keep clean untextured regions and
legible contours suitable for later colour-by-code segmentation. No text, numbers,
labels, watermark, comparison panel, page mockup, added border or new decoration.
Return the single portrait-oriented illustration only."""
ILLUSTRATION_PROMPT += """
The main visual feature MUST be clean BLACK PEN LINEWORK, like an elegantly
inked adult colouring-book drawing that has been carefully coloured. Use smooth
continuous outlines for the nose, eyelids, lips, ears, face, grouped curls and
fabric folds. On skin use one warm base colour and at most two restrained clean
shadow shapes. No mottled patches, pools of colour, irregular painted shading
or photographic textures. The ink, rather than shading, must describe the face.
Do not add flowers, clouds, trees, jewellery, props or decorations that are not
visible in the supplied photo. If the original background is out of focus,
simplify its existing masses rather than inventing detailed new objects.
"""


class ArtistError(ValueError):
    """A presentable failure, deliberately excluding provider bodies and secrets."""

    def __init__(self, message: str, code: str = "illustration_failed", *, action_url: str | None = None):
        super().__init__(message)
        self.code = code
        self.action_url = action_url


@dataclass(frozen=True)
class _Configuration:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    provider: str = "openai"


def _configuration(*, local_mock: bool = False) -> _Configuration:
    provider = os.environ.get("COLORART_IMAGE_PROVIDER", "openai").strip().lower()
    if provider == "vercel_gateway":
        key = (os.environ.get("AI_GATEWAY_API_KEY") or _REQUEST_OIDC_TOKEN.get()
               or os.environ.get("VERCEL_OIDC_TOKEN") or "").strip()
        base = GATEWAY_BASE_URL
        # A server override for the direct provider must never redirect an OIDC
        # credential to a different host, even if both services are configured.
        configured_base = os.environ.get("COLORART_IMAGE_BASE_URL", base).strip().rstrip("/")
        if configured_base != base:
            raise ArtistError("L’adresse du service AI Gateway n’est pas valide.", "invalid_configuration")
        model = os.environ.get("COLORART_IMAGE_MODEL", GATEWAY_DEFAULT_MODEL).strip()
        model_pattern = r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    elif provider == "openai":
        key = (os.environ.get("COLORART_IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
        base = os.environ.get("COLORART_IMAGE_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        model = os.environ.get("COLORART_IMAGE_MODEL", "gpt-image-1.5").strip()
        model_pattern = r"[A-Za-z0-9._-]{1,100}"
    else:
        raise ArtistError("Le service de dessin n’est pas correctement configuré.", "invalid_configuration")
    if not key:
        raise ArtistError("La création du dessin premium n’est pas activée dans cet atelier.", "not_configured")
    if any(not 33 <= ord(char) <= 126 for char in key) or len(key) > 4096:
        raise ArtistError("Le service de dessin n’est pas correctement configuré.", "invalid_configuration")
    try:
        parsed = urlsplit(base)
        parsed.port  # Reject malformed ports before any request is attempted.
    except ValueError:
        raise ArtistError("Le service de dessin doit utiliser une connexion HTTPS valide.", "invalid_configuration") from None
    mock_http = local_mock and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.hostname or (parsed.scheme != "https" and not mock_http)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ArtistError("Le service de dessin doit utiliser une connexion HTTPS valide.", "invalid_configuration")
    if not re.fullmatch(model_pattern, model) or len(model) > 160:
        raise ArtistError("Le modèle de dessin n’est pas correctement configuré.", "invalid_configuration")
    return _Configuration(key, base, model, provider)


def premium_available() -> bool:
    """Only configuration readiness; this never makes a remote readiness claim."""
    try:
        _configuration()
    except (ArtistError, ValueError):
        return False
    return True


def _safe_action_url(value: object, secret: str) -> str | None:
    """Only return an operator link on Vercel, never credentials or other hosts."""
    if (not isinstance(value, str) or len(value) > 2048
            or any(ord(char) < 33 for char in value)
            or secret in value or quote(secret, safe="") in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "vercel.com"
                or parsed.port not in {None, 443} or parsed.username or parsed.password
                or parsed.fragment):
            return None
    except ValueError:
        return None
    return value


async def _response_body(response: httpx.Response, limit: int) -> bytes:
    length = response.headers.get("content-length")
    if length and (not length.isdecimal() or int(length) > limit):
        raise ArtistError("Le service de dessin a renvoyé une réponse trop volumineuse.", "response_too_large")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise ArtistError("Le service de dessin a renvoyé une réponse trop volumineuse.", "response_too_large")
        body.extend(chunk)
    return bytes(body)


def _provider_failure(status: int, body: bytes, config: _Configuration) -> ArtistError:
    """Classify actionable failures without copying provider messages into logs/UI."""
    error = {}
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            error = payload.get("error", payload)
            if not isinstance(error, dict):
                error = {}
    except (ValueError, TypeError):
        pass
    codes = {error.get(key) for key in ("code", "type") if isinstance(error.get(key), str)}
    action_url = (_safe_action_url(error.get("action_url"), config.api_key)
                  if config.provider == "vercel_gateway" else None)
    if "customer_verification_required" in codes:
        return ArtistError("Le service de dessin doit être activé dans le compte Vercel de l’atelier.",
                           "customer_verification_required", action_url=action_url)
    if "insufficient_funds" in codes:
        return ArtistError("Le service de dessin est temporairement indisponible : les crédits de l’atelier doivent être renouvelés.",
                           "insufficient_funds", action_url=action_url)
    if "quota_for_entity_exceeded" in codes:
        return ArtistError("La limite de création de l’atelier est atteinte. Ta photo et tes essais sont conservés.",
                           "quota_for_entity_exceeded", action_url=action_url)
    if status == 401:
        return ArtistError("La connexion au service de dessin doit être rétablie par l’atelier.", "authentication_failed")
    if status == 429:
        return ArtistError("Le service de dessin reçoit beaucoup de demandes. Réessaie dans quelques instants ; ta photo est conservée.",
                           "rate_limited")
    return ArtistError("L’atelier n’a pas pu confirmer la création du dessin. Ta photo est conservée.", "provider_rejected")


async def gateway_readiness(*, transport=None) -> dict:
    """Check Gateway credits without creating an image or disclosing the balance.

    A positive result confirms Gateway authentication and positive credit balance,
    not visual quality, per-model access, or the outcome of a future charged call.
    Direct-provider configuration retains its historical local readiness check.
    """
    provider = ("vercel_gateway" if os.environ.get("COLORART_IMAGE_PROVIDER", "").strip().lower()
                == "vercel_gateway" else "openai")

    def status(available: bool, reason: str, action_url: str | None = None) -> dict:
        return {"available": available, "provider": provider, "reason": reason, "action_url": action_url}

    try:
        config = _configuration(local_mock=isinstance(transport, httpx.MockTransport))
    except (ArtistError, ValueError) as exc:
        return status(False, getattr(exc, "code", "invalid_configuration"))
    if config.provider != "vercel_gateway":
        return status(True, "configured")
    try:
        async with asyncio.timeout(12):
            async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                    timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                async with client.stream("GET", GATEWAY_BASE_URL + "/credits",
                        headers={"Authorization": "Bearer " + config.api_key,
                                 "Accept": "application/json"}) as response:
                    body = await _response_body(response, MAX_STATUS_BYTES)
                    if not 200 <= response.status_code < 300:
                        failure = _provider_failure(response.status_code, body, config)
                        return status(False, failure.code, failure.action_url)
                    payload = json.loads(body)
                    balance = payload.get("balance") if isinstance(payload, dict) else None
                    if isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
                        return status(False, "invalid_response")
                    amount = Decimal(str(balance))
                    if not amount.is_finite():
                        return status(False, "invalid_response")
                    return status(amount > 0, "ready" if amount > 0 else "insufficient_funds")
    except (TimeoutError, httpx.TimeoutException):
        return status(False, "timeout")
    except httpx.HTTPError:
        return status(False, "connection_failed")
    except (ValueError, InvalidOperation, ArtistError):
        return status(False, "invalid_response")


def artist_service_status(*, transport=None) -> dict:
    """Synchronous adapter for the Flask controller; never generates an image."""
    return asyncio.run(gateway_readiness(transport=transport))


def _normalized_photo(path: Path) -> tuple[bytes, str]:
    if not path.is_file() or path.is_symlink():
        raise ArtistError("La photo originale n’est plus disponible.", "invalid_source")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ArtistError("La photo doit peser moins de 20 Mio.", "invalid_source")
    original = path.read_bytes()
    if not original or len(original) > MAX_INPUT_BYTES:
        raise ArtistError("La photo doit peser moins de 20 Mio.", "invalid_source")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(original)) as photo:
                if photo.format not in {"PNG", "JPEG", "WEBP"} or photo.width * photo.height > MAX_IMAGE_PIXELS:
                    raise ArtistError("Utilise une photo JPEG, PNG ou WebP de moins de 30 millions de pixels.", "invalid_source")
                image = ImageOps.exif_transpose(photo).convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(background, image).convert("RGB")
                image.info.clear()
                output = BytesIO()
                image.save(output, format="PNG")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ArtistError("Cette photo est illisible ou trop volumineuse.", "invalid_source") from None
    normalized = output.getvalue()
    if len(normalized) > MAX_INPUT_BYTES:
        raise ArtistError("La photo normalisée est trop volumineuse. Réduis sa résolution avant de réessayer.", "invalid_source")
    return normalized, hashlib.sha256(original).hexdigest()


async def _request(config: _Configuration, content: bytes, transport, *, prompt: str = ILLUSTRATION_PROMPT) -> bytes:
    data = {"model": config.model, "prompt": prompt, "n": "1",
            "size": "1024x1536", "quality": "high",
            "output_format": "png", "background": "opaque"}
    if config.provider == "openai" or config.model == "openai/gpt-image-1.5":
        # Preserve the direct 1.5 contract. Image 2 does not document this field.
        data["input_fidelity"] = "high"
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                    timeout=httpx.Timeout(150.0, connect=10.0, write=20.0, pool=5.0)) as client:
                async with client.stream("POST", config.base_url + "/images/edits",
                        headers={"Authorization": "Bearer " + config.api_key},
                        data=data,
                        files={"image": ("source.png", content, "image/png")}) as response:
                    if not 200 <= response.status_code < 300:
                        body = await _response_body(response, MAX_STATUS_BYTES)
                        raise _provider_failure(response.status_code, body, config)
                    if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                        raise ArtistError("Le service de dessin a renvoyé un résultat incomplet.", "invalid_response")
                    return await _response_body(response, MAX_RESPONSE_BYTES)
    except (TimeoutError, httpx.TimeoutException):
        raise ArtistError("La création du dessin n’a pas pu être confirmée dans le délai prévu. Ta photo est conservée ; aucune relance automatique n’a été effectuée.", "timeout") from None
    except httpx.HTTPError:
        raise ArtistError("Le service de dessin ne répond pas. Ta photo et tes essais sont conservés.", "connection_failed") from None


def _validated_artwork(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
        values = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError()
        encoded = values[0].get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            # URL responses are intentionally not downloaded (no second network
            # target and no client/provider-controlled URL fetch).
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format != "PNG" or min(image.size) < 128 or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError()
                image.load()
                # Store a clean, opaque PNG, never provider-supplied EXIF/text.
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                clean = Image.alpha_composite(background, rgba).convert("RGB")
                clean.info.clear()
                output = BytesIO()
                clean.save(output, format="PNG")
        result = output.getvalue()
        if len(result) > MAX_RESPONSE_BYTES:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError, binascii.Error, OSError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ArtistError("Le dessin reçu est invalide ou incomplet. Aucun résultat n’a remplacé ta photo.", "invalid_artwork") from None


def create_illustration(source_path: str | Path, output_dir: str | Path,
                        universe_key: str = "original", *, transport=None) -> dict:
    """Create one artwork; call synchronously from the rendering worker.

    ``transport`` is only an injected HTTPX transport for deterministic contract
    tests. There is no browser-supplied endpoint, model, key or prompt parameter.
    Artistic universes use a complete scene-drawing prompt. Legacy palette keys
    retain the photo-faithful drawing contract; colour treatment happens later.
    """
    if not isinstance(universe_key, str) or (universe_key not in UNIVERSES and universe_key not in ART_UNIVERSES):
        raise ArtistError("Choisis un univers disponible.", "invalid_universe")
    artistic = universe_key in ART_UNIVERSES
    prompt = build_art_prompt(universe_key) if artistic else ILLUSTRATION_PROMPT
    config = _configuration(local_mock=isinstance(transport, httpx.MockTransport))
    content, original_hash = _normalized_photo(Path(source_path))
    response = asyncio.run(_request(config, content, transport, prompt=prompt))
    artwork = _validated_artwork(response)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / ("illustration_" + uuid.uuid4().hex + ".png")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".artwork_", suffix=".tmp", dir=destination, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(artwork)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise
    return {"path": final, "provenance": {
            "provider": ("vercel-ai-gateway-images-edit" if config.provider == "vercel_gateway"
                         else "openai-compatible-images-edit"),
            "model": config.model, "universe_key": universe_key,
            "prompt_version": ART_PROMPT_VERSION if artistic else PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "original_sha256": original_hash, "artwork_sha256": hashlib.sha256(artwork).hexdigest(),
            "human_review_required": True}}
