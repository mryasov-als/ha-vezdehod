"""Contract tests with intercepted transport; no connection to the real API."""

import asyncio
import importlib.util
import json
import sys
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web

API_PATH = Path(__file__).parents[1] / "custom_components" / "vezdehod" / "api.py"
PHONE = "79991234567"
KEY = "test-only-mobile-secret"
BASE_URL = "https://lk.comendant24.ru/vezdehod/v1/"


def test_api_module_exists():
    assert API_PATH.is_file(), "API client has not been implemented yet"


@pytest.fixture(scope="module")
def api():
    assert API_PATH.is_file(), "API-клиент ещё не реализован"
    spec = importlib.util.spec_from_file_location("vezdehod_api_contract", API_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.payload = payload
        self.content_type = "application/json"
        self.body = ""

    async def json(self, **kwargs):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def text(self, **kwargs):
        return self.body


class Transport:
    """ClientSession.request transport double, including context-manager errors."""

    def __init__(self, payload=None, status=200, error=None):
        self.response = Response(payload, status)
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.response

    async def __aexit__(self, *exc):
        return False


def run(awaitable):
    return asyncio.run(awaitable)


CAMERA_URL = "https://camera.invalid/image.jpg?camera_token=test-camera-only-secret"
JPEG = b"\xff\xd8\xff\xe0test-jpeg\xff\xd9"


class ImageContent:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


def image_transport(body=JPEG, status=200, content_type="image/jpeg", declared_size=True):
    transport = Transport(status=status)
    transport.response.content_type = content_type
    transport.response.content_length = len(body) if declared_size else None
    transport.response.content = ImageContent(body)
    return transport


def test_barrier_camera_url_is_parsed_but_hidden_in_repr(api):
    transport = Transport({"list": [{"uid": 42, "can_open": True, "camera": CAMERA_URL}]})
    barrier = run(api.VezdehodApi(transport, PHONE, KEY).async_get_barriers())["42"]
    assert barrier.camera_url == CAMERA_URL
    assert "camera_token" not in repr(barrier)


@pytest.mark.parametrize("url", [None, "", "not a URL", "file:///etc/passwd", "https://"])
def test_invalid_camera_url_does_not_invalidate_account_list(api, url):
    transport = Transport({"list": [{"uid": 42, "can_open": True, "camera": url}]})
    barrier = run(api.VezdehodApi(transport, PHONE, KEY).async_get_barriers())["42"]
    assert barrier.can_open and barrier.camera_url is None


def test_camera_get_returns_jpeg_without_forwarding_mobile_credentials(api):
    transport = image_transport()
    result = run(api.VezdehodApi(transport, PHONE, KEY).async_get_camera_image(CAMERA_URL))
    assert result == JPEG
    assert len(transport.calls) == 1
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("GET", CAMERA_URL)
    assert not kwargs.get("params")
    assert kwargs["headers"] == {"User-Agent": "okhttp/4.9.1"}
    assert kwargs["timeout"].total == 15
    assert kwargs["allow_redirects"] is False
    assert kwargs["raise_for_status"] is False


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_camera_http_error_is_separate_from_mobile_key_auth(api, status):
    transport = image_transport(status=status)
    with pytest.raises(api.VezdehodError) as raised:
        run(api.VezdehodApi(transport, PHONE, KEY).async_get_camera_image(CAMERA_URL))
    assert not isinstance(raised.value, api.InvalidAuth)
    assert CAMERA_URL not in str(raised.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("body", "content_type", "declared_size"),
    [
        (b"", "image/jpeg", True),
        (b"<html>private error</html>", "image/jpeg", True),
        (JPEG, "text/html", True),
        (JPEG + b"x" * (5 * 1024 * 1024), "image/jpeg", True),
        (JPEG + b"x" * (5 * 1024 * 1024), "image/jpeg", False),
    ],
    ids=["empty", "html", "wrong_mime", "large_header", "large_stream"],
)
def test_camera_rejects_non_images_and_oversized_frames(api, body, content_type, declared_size):
    transport = image_transport(body=body, content_type=content_type, declared_size=declared_size)
    with pytest.raises(api.ApiError):
        run(api.VezdehodApi(transport, PHONE, KEY).async_get_camera_image(CAMERA_URL))


def test_camera_connection_exception_does_not_expose_url(api):
    transport = Transport(error=aiohttp.ClientConnectionError(CAMERA_URL))
    with pytest.raises(api.CannotConnect) as raised:
        run(api.VezdehodApi(transport, PHONE, KEY).async_get_camera_image(CAMERA_URL))
    assert CAMERA_URL not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("payload", [{"message": "ok"}, ValueError("empty body")])
def test_sms_request_uses_one_explicit_post_without_code(api, payload):
    transport = Transport(payload)
    run(api.VezdehodApi(transport, PHONE, KEY).async_request_sms())
    assert len(transport.calls) == 1
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("POST", BASE_URL + "auth")
    assert kwargs["params"] == {"auth_phone": PHONE, "mobile_key": KEY}
    assert kwargs["allow_redirects"] is False


def test_sms_confirmation_keeps_leading_zeroes_and_uses_same_key(api):
    transport = Transport({"message": "ok"})
    run(api.VezdehodApi(transport, PHONE, KEY).async_confirm_sms("001234"))
    assert len(transport.calls) == 1
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("POST", BASE_URL + "auth")
    assert kwargs["params"] == {"auth_phone": PHONE, "mobile_key": KEY, "sms_code": "001234"}


@pytest.mark.parametrize("code", ["", "1a23", "1e3", "12 34", "１２３４", None])
def test_sms_confirmation_rejects_non_digits_without_network(api, code):
    transport = Transport({"message": "ok"})
    with pytest.raises(ValueError):
        run(api.VezdehodApi(transport, PHONE, KEY).async_confirm_sms(code))
    assert not transport.calls


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (401, "InvalidAuth"),
        (403, "InvalidAuth"),
        (429, "CannotConnect"),
        (503, "CannotConnect"),
        (400, "ApiError"),
    ],
)
def test_sms_errors_are_safe_and_never_retried(api, status, exception):
    transport = Transport({"message": "private server error"}, status=status)
    with pytest.raises(getattr(api, exception)) as raised:
        run(api.VezdehodApi(transport, PHONE, KEY).async_confirm_sms("1234"))
    assert len(transport.calls) == 1
    assert KEY not in str(raised.value)
    assert "private server error" not in str(raised.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+7 (999) 123-45-67", PHONE),
        ("8 (999) 123-45-67", PHONE),
        (PHONE, PHONE),
        ("+44 20 1234 5678", "442012345678"),
        ("1234567890", "1234567890"),
        ("123456789012345", "123456789012345"),
    ],
)
def test_phone_normalization(api, value, expected):
    assert api.normalize_phone(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "123456789",
        "1234567890123456",
        "+7abc9991234567",
        "7/9991234567",
        "7+9991234567",
        "++79991234567",
        "７９９９１２３４５６７",
        None,
        79991234567,
    ],
)
def test_phone_rejects_invalid_input_without_exposing_it(api, value):
    with pytest.raises(ValueError) as caught:
        api.normalize_phone(value)
    if value:
        assert str(value) not in str(caught.value)


def test_barrier_is_frozen_and_slotted(api):
    barrier = api.Barrier("42", "Двор", True)
    assert (barrier.uid, barrier.name, barrier.can_open) == ("42", "Двор", True)
    assert not hasattr(barrier, "__dict__")
    with pytest.raises(FrozenInstanceError):
        barrier.name = "Изменено"
    for error in (api.CannotConnect, api.InvalidAuth, api.ApiError):
        assert issubclass(error, api.VezdehodError)


def test_get_contract_normalizes_phone_and_parses_list(api):
    transport = Transport(
        {
            "list": [
                {"uid": 42, "description": "Двор", "can_open": "1"},
                {
                    "uid": "43",
                    "description": "",
                    "address": "Адрес",
                    "can_open": "false",
                },
                {"uid": 44, "name": "Имя", "can_open": 1},
                {"uid": 45, "openUrl": "https://untrusted.invalid/open"},
            ]
        }
    )
    result = run(api.VezdehodApi(transport, "8 (999) 123-45-67", KEY).async_get_barriers())
    assert result == {
        "42": api.Barrier("42", "Двор", True),
        "43": api.Barrier("43", "Адрес", False),
        "44": api.Barrier("44", "Имя", True),
        "45": api.Barrier("45", "Шлагбаум 45", False),
    }
    method, url, kwargs = transport.calls[0]
    assert len(transport.calls) == 1
    assert method == "GET"
    assert url == BASE_URL + "get_facility"
    assert kwargs["params"] == {"auth_phone": PHONE, "mobile_key": KEY}
    assert kwargs["headers"]["User-Agent"] == "okhttp/4.9.1"
    assert kwargs["timeout"].total == 15
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (1, True),
        ("true", True),
        ("1", True),
        (False, False),
        (0, False),
        ("false", False),
        ("0", False),
        (None, False),
        ("yes", False),
        (2, False),
        ("false ", False),
    ],
)
def test_can_open_is_explicit_not_truthiness(api, value, expected):
    transport = Transport({"list": [{"uid": 1, "can_open": value}]})
    result = run(api.VezdehodApi(transport, PHONE, KEY).async_get_barriers())
    assert result["1"].can_open is expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"list": {}},
        {"list": None},
        {"list": [None]},
        {"list": [{}]},
        {"list": [{"uid": 0}]},
        {"list": [{"uid": -1}]},
        {"list": [{"uid": True}]},
        {"list": [{"uid": 1.5}]},
        {"list": [{"uid": "not-an-id"}]},
        {"list": [{"uid": 1}, {"uid": "1"}]},
    ],
)
def test_get_rejects_malformed_or_ambiguous_list(api, payload):
    with pytest.raises(api.ApiError):
        run(api.VezdehodApi(Transport(payload), PHONE, KEY).async_get_barriers())


def test_empty_list_is_valid(api):
    assert run(api.VezdehodApi(Transport({"list": []}), PHONE, KEY).async_get_barriers()) == {}


@pytest.mark.parametrize("message", ["success", "ok", "Successfully opened", "barrier opened"])
def test_open_uses_one_post_with_empty_body_and_explicit_success(api, message):
    transport = Transport({"message": message})
    assert run(api.VezdehodApi(transport, PHONE, KEY).async_open_barrier("42")) is None
    assert len(transport.calls) == 1
    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url == BASE_URL + "open"
    assert kwargs["params"] == {"auth_phone": PHONE, "mobile_key": KEY, "uid": "42"}
    assert kwargs.get("data") in (None, b"", "")
    assert "json" not in kwargs
    assert kwargs["headers"]["User-Agent"] == "okhttp/4.9.1"
    assert kwargs["timeout"].total == 15
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        [],
        {"success": True},
        {"message": "not ok"},
        {"message": "unsuccessful"},
        {"message": "error"},
        {"message": ""},
        {"message": None},
        {"message": True},
        {"message": "success but not opened"},
        {"message": "unknown"},
        {"message": "not successfully opened"},
        {"message": "Successfully opened with error"},
        {"message": "barrier not opened"},
        {"message": "barrier opened with error"},
    ],
)
def test_open_requires_unambiguous_confirmation(api, payload):
    transport = Transport(payload)
    with pytest.raises(api.ApiError):
        run(api.VezdehodApi(transport, PHONE, KEY).async_open_barrier("42"))
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("body", "status", "error_name"),
    [
        ("barrier opened", 200, None),
        ('{"message":"barrier opened"}', 200, None),
        ('"barrier opened"', 200, None),
        ("  BARRIER OPENED\n", 200, None),
        ("barrier not opened", 200, "ApiError"),
        ("barrier opened with error", 200, "ApiError"),
        ("success but not opened", 200, "ApiError"),
        ('{"message":"Key Invalid"}', 200, "InvalidAuth"),
        ("Key Invalid", 200, "InvalidAuth"),
        ("barrier opened", 400, "ApiError"),
        ("barrier opened", 401, "InvalidAuth"),
        ("barrier opened", 503, "CannotConnect"),
    ],
)
def test_open_text_plain_response_is_classified_without_retries(
    api, monkeypatch, body, status, error_name
):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append((request.method, dict(request.query), await request.read()))
            return web.Response(text=body, status=status, content_type="text/plain")

        app = web.Application()
        app.router.add_post("/open", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            monkeypatch.setattr(api, "_BASE_URL", f"http://127.0.0.1:{port}/")
            async with aiohttp.ClientSession() as session:
                client = api.VezdehodApi(session, PHONE, KEY)
                if error_name:
                    with pytest.raises(getattr(api, error_name)):
                        await client.async_open_barrier("42")
                else:
                    assert await client.async_open_barrier("42") is None
        finally:
            await runner.cleanup()
        assert calls == [("POST", {"auth_phone": PHONE, "mobile_key": KEY, "uid": "42"}, b"")]

    run(scenario())


def test_open_debug_log_explains_unrecognized_message_without_secrets(api, caplog):
    message = f"unrecognized {PHONE} {KEY} https://camera.invalid/path?token=hidden 12345678"
    transport = Transport({"message": message, "private_field": "must-not-be-logged"})
    with caplog.at_level("DEBUG", logger=api.__name__), pytest.raises(api.ApiError):
        run(api.VezdehodApi(transport, PHONE, KEY).async_open_barrier("42"))
    assert "Opening response" in caplog.text
    assert "HTTP 200" in caplog.text
    assert "application/json" in caplog.text
    assert "unrecognized" in caplog.text
    for private in (PHONE, KEY, "camera.invalid", "hidden", "12345678", "must-not-be-logged"):
        assert private not in caplog.text
    assert len(transport.calls) == 1


def test_open_debug_log_reads_message_despite_wrong_content_type(api, caplog):
    error = aiohttp.ContentTypeError(None, (), message="unexpected content type")
    transport = Transport(error)
    transport.response.content_type = "text/html"
    transport.response.body = json.dumps({"message": "success!", "private": KEY})
    with caplog.at_level("DEBUG", logger=api.__name__), pytest.raises(api.ApiError):
        run(api.VezdehodApi(transport, PHONE, KEY).async_open_barrier("42"))
    assert "success!" in caplog.text
    assert "text/html" in caplog.text
    assert KEY not in caplog.text
    assert len(transport.calls) == 1


@pytest.mark.parametrize("charset", ["utf-8", "invalid-charset"])
def test_open_debug_invalid_encoding_keeps_safe_api_error(api, caplog, monkeypatch, charset):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.Response(
                body=b"\xff", headers={"Content-Type": "text/html; charset=" + charset}
            )

        app = web.Application()
        app.router.add_post("/open", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            monkeypatch.setattr(api, "_BASE_URL", f"http://127.0.0.1:{port}/")
            async with aiohttp.ClientSession() as session:
                client = api.VezdehodApi(session, PHONE, KEY)
                with (
                    caplog.at_level("DEBUG", logger=api.__name__),
                    pytest.raises(api.ApiError) as caught,
                ):
                    await client.async_open_barrier("42")
                rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
                assert PHONE not in rendered
                assert KEY not in rendered
                assert "Opening response:" in caplog.text
        finally:
            await runner.cleanup()
        assert calls == ["POST"]

    run(scenario())


@pytest.mark.parametrize("operation", ["async_get_barriers", "async_open_barrier"])
@pytest.mark.parametrize(
    ("status", "error_name"),
    [
        (401, "InvalidAuth"),
        (403, "InvalidAuth"),
        (429, "CannotConnect"),
        (500, "CannotConnect"),
        (502, "CannotConnect"),
        (503, "CannotConnect"),
        (400, "ApiError"),
        (404, "ApiError"),
        (302, "ApiError"),
    ],
)
def test_http_errors_are_classified_without_post_retries(api, operation, status, error_name):
    transport = Transport(json.JSONDecodeError("secret body", "", 0), status)
    method = getattr(api.VezdehodApi(transport, PHONE, KEY), operation)
    with pytest.raises(getattr(api, error_name)):
        run(method(*(("42",) if operation == "async_open_barrier" else ())))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("operation", ["async_get_barriers", "async_open_barrier"])
@pytest.mark.parametrize("message", ["Key Invalid", "Unauthorized"])
def test_authentication_error_in_http_200(api, operation, message):
    method = getattr(api.VezdehodApi(Transport({"message": message}), PHONE, KEY), operation)
    with pytest.raises(api.InvalidAuth):
        run(method(*(("42",) if operation == "async_open_barrier" else ())))


@pytest.mark.parametrize("operation", ["async_get_barriers", "async_open_barrier"])
@pytest.mark.parametrize("error_type", [aiohttp.ClientError, asyncio.TimeoutError])
def test_network_errors_hide_sensitive_details_in_traceback(api, operation, error_type):
    transport = Transport(
        error=error_type(BASE_URL + "?auth_phone=" + PHONE + "&mobile_key=" + KEY)
    )
    method = getattr(api.VezdehodApi(transport, PHONE, KEY), operation)
    with pytest.raises(api.CannotConnect) as caught:
        run(method(*(("42",) if operation == "async_open_barrier" else ())))
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    for secret in (PHONE, KEY, BASE_URL):
        assert secret not in str(caught.value)
        assert secret not in rendered
    assert len(transport.calls) == 1


@pytest.mark.parametrize("operation", ["async_get_barriers", "async_open_barrier"])
def test_invalid_json_is_sanitized(api, operation):
    transport = Transport(json.JSONDecodeError(KEY + PHONE, KEY, 0))
    method = getattr(api.VezdehodApi(transport, PHONE, KEY), operation)
    with pytest.raises(api.ApiError) as caught:
        run(method(*(("42",) if operation == "async_open_barrier" else ())))
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert KEY not in rendered
    assert PHONE not in rendered


@pytest.mark.parametrize("uid", ["", "0", "-1", "1/2", "not-an-id", None, True])
def test_invalid_uid_does_not_send_request(api, uid):
    transport = Transport({"message": "success"})
    with pytest.raises(ValueError):
        run(api.VezdehodApi(transport, PHONE, KEY).async_open_barrier(uid))
    assert transport.calls == []


@pytest.mark.parametrize("key", [None, "", "  ", 123])
def test_constructor_rejects_missing_key_without_a_request(api, key):
    transport = Transport()
    with pytest.raises(ValueError):
        api.VezdehodApi(transport, PHONE, key)
    assert transport.calls == []


@pytest.mark.parametrize(
    ("error", "error_name"),
    [
        (
            aiohttp.ContentTypeError(request_info=None, history=(), message=KEY),
            "ApiError",
        ),
        (aiohttp.ClientPayloadError(KEY), "CannotConnect"),
        (TimeoutError(KEY), "CannotConnect"),
    ],
)
def test_errors_while_reading_body_are_classified_and_sanitized(api, error, error_name):
    transport = Transport(error)
    with pytest.raises(getattr(api, error_name)) as caught:
        run(api.VezdehodApi(transport, PHONE, KEY).async_open_barrier("42"))
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert KEY not in rendered
    assert len(transport.calls) == 1


def test_real_aiohttp_serializes_queries_headers_and_empty_post_to_localhost(api):
    async def scenario():
        seen = []

        async def handler(request):
            seen.append(
                (
                    request.method,
                    request.path,
                    dict(request.query),
                    request.headers.get("User-Agent"),
                    await request.read(),
                )
            )
            if request.method == "GET":
                return web.json_response({"list": [{"uid": 42, "can_open": True}]})
            return web.json_response({"message": "ok"})

        app = web.Application()
        app.router.add_get("/vezdehod/v1/get_facility", handler)
        app.router.add_post("/vezdehod/v1/open", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            async with aiohttp.ClientSession() as session:
                original_request = session.request

                def local_request(method, url, **kwargs):
                    assert url.startswith(BASE_URL)
                    return original_request(
                        method,
                        f"http://127.0.0.1:{port}" + urlsplit(url).path,
                        **kwargs,
                    )

                session.request = local_request
                client = api.VezdehodApi(session, PHONE, KEY)
                assert (await client.async_get_barriers())["42"].can_open is True
                await client.async_open_barrier("42")
                assert not session.closed
        finally:
            await runner.cleanup()
        assert seen == [
            (
                "GET",
                "/vezdehod/v1/get_facility",
                {"auth_phone": PHONE, "mobile_key": KEY},
                "okhttp/4.9.1",
                b"",
            ),
            (
                "POST",
                "/vezdehod/v1/open",
                {"auth_phone": PHONE, "mobile_key": KEY, "uid": "42"},
                "okhttp/4.9.1",
                b"",
            ),
        ]

    run(scenario())


@pytest.mark.parametrize("operation", ["async_get_barriers", "async_open_barrier"])
@pytest.mark.parametrize(
    ("status", "error_name"),
    [
        (401, "InvalidAuth"),
        (403, "InvalidAuth"),
        (400, "ApiError"),
        (404, "ApiError"),
        (429, "CannotConnect"),
        (503, "CannotConnect"),
    ],
)
def test_caller_raise_for_status_does_not_override_error_classification(
    api, operation, status, error_name
):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.json_response({"message": "request rejected"}, status=status)

        app = web.Application()
        app.router.add_route("*", "/vezdehod/v1/{endpoint}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            async with aiohttp.ClientSession(raise_for_status=True) as session:
                original_request = session.request

                def local_request(method, url, **kwargs):
                    assert url.startswith(BASE_URL)
                    return original_request(
                        method,
                        f"http://127.0.0.1:{port}" + urlsplit(url).path,
                        **kwargs,
                    )

                session.request = local_request
                method = getattr(api.VezdehodApi(session, PHONE, KEY), operation)
                with pytest.raises(getattr(api, error_name)):
                    await method(*(("42",) if operation == "async_open_barrier" else ()))
        finally:
            await runner.cleanup()
        assert calls == ["POST" if operation == "async_open_barrier" else "GET"]

    run(scenario())


@pytest.mark.parametrize("operation", ["async_get_barriers", "async_open_barrier"])
@pytest.mark.parametrize("status", [400, 404])
@pytest.mark.parametrize("message", ["Key Invalid", "Unauthorized"])
def test_authentication_message_in_other_4xx_is_invalid_auth(api, operation, status, message):
    transport = Transport({"message": message}, status=status)
    method = getattr(api.VezdehodApi(transport, PHONE, KEY), operation)
    with pytest.raises(api.InvalidAuth):
        run(method(*(("42",) if operation == "async_open_barrier" else ())))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_temporary_failure_status_has_priority_over_authentication_message(api, status):
    with pytest.raises(api.CannotConnect):
        run(
            api.VezdehodApi(
                Transport({"message": "Key Invalid"}, status=status), PHONE, KEY
            ).async_get_barriers()
        )
