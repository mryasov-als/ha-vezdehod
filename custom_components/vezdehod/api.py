"""Small, asynchronous client for an existing Vezdehod mobile key."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp

_BASE_URL = "https://lk.comendant24.ru/vezdehod/v1/"
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_CAMERA_BYTES = 5 * 1024 * 1024
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Barrier:
    """A barrier returned by the account's facility list."""

    uid: str
    name: str
    can_open: bool
    camera_url: str | None = field(default=None, repr=False)


class VezdehodError(Exception):
    """Base class for safe-to-display API errors."""


class CannotConnect(VezdehodError):
    """The server cannot currently process the request."""


class InvalidAuth(VezdehodError):
    """The supplied phone/key pair was rejected."""


class ApiError(VezdehodError):
    """The server returned an unexpected or unsuccessful response."""


def normalize_phone(value: str) -> str:
    """Normalize a formatted phone without guessing missing country codes."""
    if not isinstance(value, str):
        raise ValueError("Invalid phone number")
    value = value.strip()
    if not re.fullmatch(r"\+?[0-9\s()\-]+", value):
        raise ValueError("Invalid phone number")
    digits = re.sub(r"[^0-9]", "", value)
    if not 10 <= len(digits) <= 15:
        raise ValueError("Invalid phone number")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _normalize_uid(value: Any) -> str:
    """Accept only positive integer identifiers, never paths or URLs."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Invalid barrier identifier")
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
        raise ValueError("Invalid barrier identifier")
    return str(int(value))


def _camera_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return value.strip()
    except ValueError:
        pass
    return None


class VezdehodApi:
    """Use the caller-owned session without automatically retrying requests."""

    def __init__(self, session: aiohttp.ClientSession, phone: str, mobile_key: str) -> None:
        self._session = session
        self._phone = normalize_phone(phone)
        if not isinstance(mobile_key, str) or not mobile_key.strip():
            raise ValueError("Invalid mobile key")
        self._mobile_key = mobile_key

    def _debug_open_response(self, status: int, content_type: str, payload: Any) -> None:
        """Explain a command response without recording credentials or URLs."""
        message = payload.get("message") if isinstance(payload, dict) else payload
        if isinstance(message, str):
            for secret in (self._mobile_key, self._phone):
                message = message.replace(secret, "[redacted]")
            message = re.sub(r"https?://[^\s<>\"']+", "[url]", message, flags=re.IGNORECASE)
            message = re.sub(r"\+?[0-9][0-9 ()-]{6,}[0-9]", "[number]", message)
            message = message[:512]
        else:
            message = f"<{type(message).__name__}>"
        _LOGGER.debug(
            "Opening response: HTTP %s; Content-Type %s; message %r",
            status,
            content_type,
            message,
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        uid: str | None = None,
        *,
        sms_code: str | None = None,
        expect_json: bool = True,
    ) -> Any:
        params = {"auth_phone": self._phone, "mobile_key": self._mobile_key}
        if uid is not None:
            params["uid"] = uid
        if sms_code is not None:
            params["sms_code"] = sms_code
        try:
            async with self._session.request(
                method,
                _BASE_URL + endpoint,
                params=params,
                headers={"User-Agent": "okhttp/4.9.1"},
                timeout=_TIMEOUT,
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                status = response.status
                if status in (401, 403):
                    raise InvalidAuth("Authentication rejected")
                if status == 429 or 500 <= status <= 599:
                    raise CannotConnect("Service temporarily unavailable")
                if not (200 <= status <= 299 or 400 <= status <= 499):
                    raise ApiError("Unexpected HTTP response")
                try:
                    if endpoint == "open":
                        # This endpoint returns its confirmation as text/plain,
                        # including JSON documents with an incorrect MIME type.
                        payload = await response.json(content_type=None, encoding="utf-8")
                    else:
                        payload = await response.json()
                except ValueError, aiohttp.ContentTypeError:
                    if endpoint == "open":
                        # Accept only exact known messages in async_open_barrier.
                        # Replacement characters keep malformed text unrecognized.
                        payload = await response.text(encoding="utf-8", errors="replace")
                    elif expect_json or not 200 <= status <= 299:
                        raise ApiError("Invalid JSON response") from None
                    else:
                        # Android accepts an HTTP success for auth without JSON.
                        # A separate facility read verifies the resulting key.
                        payload = None
                if endpoint == "open":
                    self._debug_open_response(status, response.content_type, payload)
        except TimeoutError, aiohttp.ClientError:
            # aiohttp exceptions may contain the URL with both credentials.
            raise CannotConnect("Unable to communicate with service") from None

        message = (
            payload.get("message")
            if isinstance(payload, dict)
            else payload
            if endpoint == "open"
            else None
        )
        if isinstance(message, str) and message.strip().casefold() in (
            "key invalid",
            "unauthorized",
        ):
            raise InvalidAuth("Authentication rejected")
        if not 200 <= status <= 299:
            raise ApiError("Unexpected HTTP response")
        return payload

    async def async_request_sms(self) -> None:
        """Request one SMS only after an explicit user action."""
        await self._request("POST", "auth", expect_json=False)

    async def async_confirm_sms(self, code: str) -> None:
        """Bind this same client key using the code entered by the user."""
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,10}", code):
            raise ValueError("Invalid SMS code")
        await self._request("POST", "auth", sms_code=code, expect_json=False)

    async def async_get_barriers(self) -> dict[str, Barrier]:
        """Read barriers, failing clearly on malformed or ambiguous records."""
        payload = await self._request("GET", "get_facility")
        if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
            raise ApiError("Invalid facility list")
        barriers = {}
        for item in payload["list"]:
            if not isinstance(item, dict):
                raise ApiError("Invalid barrier record")
            try:
                uid = _normalize_uid(item.get("uid"))
            except ValueError:
                raise ApiError("Invalid barrier identifier") from None
            if uid in barriers:
                raise ApiError("Duplicate barrier identifier")
            name = next(
                (
                    value.strip()
                    for field in ("description", "address", "name")
                    if isinstance(value := item.get(field), str) and value.strip()
                ),
                f"Шлагбаум {uid}",
            )
            value = item.get("can_open")
            can_open = (
                value is True
                or (type(value) is int and value == 1)
                or (isinstance(value, str) and value in ("true", "1"))
            )
            barriers[uid] = Barrier(uid, name, can_open, _camera_url(item.get("camera")))
        return barriers

    async def async_get_camera_image(self, url: str) -> bytes:
        """Read only the supplied camera URL, without mobile credentials."""
        if _camera_url(url) is None:
            raise ApiError("Invalid camera URL")
        try:
            async with self._session.request(
                "GET",
                url,
                headers={"User-Agent": "okhttp/4.9.1"},
                timeout=_TIMEOUT,
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                if response.status != 200:
                    raise ApiError("Camera image is unavailable")
                if response.content_type not in ("image/jpeg", "image/jpg"):
                    raise ApiError("Camera did not return a JPEG image")
                if response.content_length and response.content_length > _MAX_CAMERA_BYTES:
                    raise ApiError("Camera image is too large")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > _MAX_CAMERA_BYTES:
                        raise ApiError("Camera image is too large")
                    chunks.append(chunk)
        except TimeoutError, aiohttp.ClientError, ValueError:
            raise CannotConnect("Unable to get camera image") from None
        image = b"".join(chunks)
        if not image.startswith(b"\xff\xd8\xff"):
            raise ApiError("Camera returned an invalid JPEG image")
        return image

    async def async_open_barrier(self, uid: str) -> None:
        """Send exactly one command and require a known success confirmation."""
        uid = _normalize_uid(uid)
        payload = await self._request("POST", "open", uid)
        message = payload.get("message") if isinstance(payload, dict) else payload
        if not isinstance(message, str) or message.strip().casefold() not in (
            "success",
            "ok",
            "successfully opened",
            "barrier opened",
        ):
            raise ApiError("Opening was not confirmed")
