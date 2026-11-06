"""Exercise real Home Assistant forms, entities and lifecycle without cloud commands."""

import asyncio
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from homeassistant import config_entries, loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry, device_registry, entity_registry, frame
from homeassistant.setup import async_setup_component

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "vezdehod"
PHONE = "79990001122"
KEY = "dummy-key-for-offline-tests"


@pytest_asyncio.fixture
async def hass(tmp_path, monkeypatch):
    # Each test owns a separate HA. Its global singleton diagnostic would
    # otherwise return the first test's closed native object in later tests.
    monkeypatch.setattr(zeroconf, "install_multiple_zeroconf_catcher", lambda _: None)
    (tmp_path / "custom_components").symlink_to(
        ROOT / "custom_components", target_is_directory=True
    )
    instance = HomeAssistant(str(tmp_path))
    instance.config.media_dirs = {"local": str(tmp_path / "media")}
    frame.async_setup(instance)
    loader.async_setup(instance)
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    device_registry.async_setup(instance)
    await area_registry.async_load(instance)
    await device_registry.async_load(instance)
    await entity_registry.async_load(instance)
    instance.auth = await auth_manager_from_config(instance, [], [])
    assert await async_setup_component(instance, "network", {})
    discovery = await zeroconf.async_get_instance(instance)
    assert not discovery.done, "A previous HA test left a closed Zeroconf singleton installed"
    yield instance
    await instance.async_stop(force=True)


def make_entry(*, save_snapshots=None):
    return config_entries.ConfigEntry(
        domain=DOMAIN,
        title="Вездеход",
        version=1,
        minor_version=1,
        data={"auth_phone": PHONE, "mobile_key": KEY},
        options={"barrier_ids": ["101"]}
        | ({"save_snapshots": save_snapshots} if save_snapshots is not None else {}),
        source=config_entries.SOURCE_USER,
        unique_id=sha256(PHONE.encode()).hexdigest(),
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )


def barriers():
    from custom_components.vezdehod.api import Barrier

    return {
        "101": Barrier("101", "Тестовый въезд", True),
        "102": Barrier("102", "Второй въезд", True),
    }


@pytest.fixture
def mock_api(monkeypatch):
    from custom_components.vezdehod.api import VezdehodApi

    get = AsyncMock(return_value=barriers())
    open_command = AsyncMock()
    request_sms = AsyncMock()
    confirm_sms = AsyncMock()
    camera_image = AsyncMock(return_value=b"\xff\xd8\xff\xe0test-frame\xff\xd9")
    monkeypatch.setattr(VezdehodApi, "async_get_barriers", get)
    monkeypatch.setattr(VezdehodApi, "async_open_barrier", open_command)
    monkeypatch.setattr(VezdehodApi, "async_request_sms", request_sms, raising=False)
    monkeypatch.setattr(VezdehodApi, "async_confirm_sms", confirm_sms, raising=False)
    monkeypatch.setattr(VezdehodApi, "async_get_camera_image", camera_image, raising=False)
    return get, open_command, request_sms, confirm_sms, camera_image


async def begin(hass):
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert menu["type"] == FlowResultType.MENU
    assert set(menu["menu_options"]) == {"sms_phone", "import_key"}
    return await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": "import_key"}
    )


async def test_user_form_asks_for_imported_phone_and_key(hass):
    assert (ROOT / "custom_components/vezdehod/config_flow.py").exists(), (
        "Imported-credentials config flow is not implemented"
    )
    result = await begin(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "import_key"
    fields = {str(key) for key in result["data_schema"].schema}
    assert fields == {"auth_phone", "mobile_key"}


async def test_valid_credentials_discover_barriers_without_opening(hass, mock_api):
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE, "mobile_key": KEY}
    )
    assert result["step_id"] == "barriers"
    assert not result["errors"]
    mock_api[0].assert_awaited_once()
    mock_api[1].assert_not_awaited()


@pytest.mark.parametrize(
    ("phone", "key", "field", "error"),
    [
        ("not a phone", KEY, "auth_phone", "invalid_phone"),
        (PHONE, "   ", "mobile_key", "invalid_key"),
    ],
)
async def test_invalid_input_stays_in_form(hass, phone, key, field, error):
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": phone, "mobile_key": key}
    )
    assert result["step_id"] == "import_key"
    assert result["errors"][field] == error


@pytest.mark.parametrize(
    ("exception_name", "error"),
    [
        ("InvalidAuth", "invalid_auth"),
        ("CannotConnect", "cannot_connect"),
        ("ApiError", "invalid_response"),
    ],
)
async def test_cloud_errors_are_displayed(hass, mock_api, exception_name, error):
    from custom_components.vezdehod import api

    mock_api[0].side_effect = getattr(api, exception_name)("safe message")
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE, "mobile_key": KEY}
    )
    assert result["step_id"] == "import_key"
    assert result["errors"]["base"] == error


async def test_empty_account_is_reported(hass, mock_api):
    mock_api[0].return_value = {}
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE, "mobile_key": KEY}
    )
    assert result["errors"]["base"] == "no_barriers"


async def test_creates_entry_with_selected_barrier(hass, mock_api, monkeypatch):
    monkeypatch.setattr(hass.config_entries, "async_setup", AsyncMock(return_value=True))
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE, "mobile_key": KEY}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"barrier_ids": ["101"]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {"auth_phone": PHONE, "mobile_key": KEY}
    assert result["options"] == {"barrier_ids": ["101"]}
    assert result["result"].unique_id == sha256(PHONE.encode()).hexdigest()
    mock_api[1].assert_not_awaited()


@pytest.mark.parametrize(
    ("selected", "error"), [([], "no_selection"), (["999"], "unknown_barrier")]
)
async def test_invalid_selection_is_rejected(hass, mock_api, selected, error):
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE, "mobile_key": KEY}
    )
    # Calling the step directly also validates IDs beyond the UI selector.
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    result = await flow.async_step_barriers({"barrier_ids": selected})
    assert result["errors"]["base"] == error


async def test_duplicate_account_is_not_created(hass, mock_api):
    await hass.config_entries.async_add(make_entry())
    result = await begin(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE, "mobile_key": "another-imported-key"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_entry_setup_creates_selected_button_and_unloads(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    assert entry.state == config_entries.ConfigEntryState.LOADED
    await hass.async_block_till_done()
    entities = hass.states.async_all("button")
    assert len(entities) == 1
    assert entities[0].state != "unavailable"
    mock_api[1].assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def make_button(hass, mock_api):
    from custom_components.vezdehod.api import VezdehodApi
    from custom_components.vezdehod.button import VezdehodButton
    from custom_components.vezdehod.coordinator import VezdehodCoordinator

    entry = make_entry()
    coordinator = VezdehodCoordinator(hass, entry, VezdehodApi(None, PHONE, KEY))
    await coordinator.async_refresh()
    button = VezdehodButton(coordinator, entry, "101")
    button.hass = hass
    return button, coordinator


async def test_button_sends_one_open_command(hass, mock_api):
    button, _ = await make_button(hass, mock_api)
    await button.async_press()
    mock_api[1].assert_awaited_once_with("101")


async def test_double_press_does_not_send_second_command(hass, mock_api):
    button, _ = await make_button(hass, mock_api)
    await button.async_press()
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    mock_api[1].assert_awaited_once_with("101")


async def test_parallel_press_does_not_queue_second_opening(hass, mock_api):
    button, _ = await make_button(hass, mock_api)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(_):
        entered.set()
        await release.wait()

    mock_api[1].side_effect = delayed
    first = asyncio.create_task(button.async_press())
    await entered.wait()
    try:
        with pytest.raises(HomeAssistantError):
            await button.async_press()
    finally:
        release.set()
        await first
    mock_api[1].assert_awaited_once()


async def test_failed_opening_can_be_retried(hass, mock_api):
    from custom_components.vezdehod.api import CannotConnect

    button, _ = await make_button(hass, mock_api)
    mock_api[1].side_effect = [CannotConnect("offline"), None]
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    await button.async_press()
    assert mock_api[1].await_count == 2


async def test_missing_barrier_does_not_block_an_explicit_command(hass, mock_api):
    button, coordinator = await make_button(hass, mock_api)
    coordinator.data = {}
    assert button.available
    await button.async_press()
    mock_api[1].assert_awaited_once_with("101")


async def test_reported_permission_does_not_block_an_explicit_command(hass, mock_api):
    from custom_components.vezdehod.api import Barrier

    button, coordinator = await make_button(hass, mock_api)
    coordinator.data = {"101": Barrier("101", "Тестовый въезд", False)}
    assert button.available
    await button.async_press()
    mock_api[1].assert_awaited_once_with("101")


async def test_real_button_service_routes_one_open_command(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    entity = hass.states.async_all("button")[0]
    await hass.services.async_call(
        "button", "press", {"entity_id": entity.entity_id}, blocking=True
    )
    mock_api[1].assert_awaited_once_with("101")


async def test_options_change_the_selected_barrier(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"barrier_ids": ["102"]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["barrier_ids"] == ["102"]
    entities = hass.states.async_all("button")
    assert len(entities) == 1
    await hass.services.async_call(
        "button", "press", {"entity_id": entities[0].entity_id}, blocking=True
    )
    mock_api[1].assert_awaited_once_with("102")


async def test_reauth_imports_key_without_changing_phone(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=dict(entry.data),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "import_key"}
    )
    assert result["step_id"] == "import_key"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mobile_key": "replacement-imported-key"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {"auth_phone": PHONE, "mobile_key": "replacement-imported-key"}
    mock_api[1].assert_not_awaited()


async def test_reconfigure_rejects_different_account(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "import_key"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": "79990002233", "mobile_key": KEY}
    )
    assert result["step_id"] == "import_key"
    assert result["errors"]["base"] == "wrong_account"
    assert entry.data["auth_phone"] == PHONE


@pytest.mark.parametrize("has_disabled_barrier", [False, True])
async def test_reauth_accepts_valid_key_when_opening_is_temporarily_unavailable(
    hass, mock_api, has_disabled_barrier
):
    from custom_components.vezdehod.api import Barrier

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    mock_api[0].return_value = (
        {"101": Barrier("101", "Тестовый въезд", False)} if has_disabled_barrier else {}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=dict(entry.data),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "import_key"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mobile_key": "valid-replacement-key"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert entry.data["mobile_key"] == "valid-replacement-key"
    mock_api[1].assert_not_awaited()


@pytest.mark.parametrize(
    ("exception_name", "status", "error"),
    [
        ("CannotConnect", 0, "cannot_connect"),
        ("ApiError", 0, "invalid_response"),
        ("InvalidAuth", -1, "invalid_auth"),
    ],
)
async def test_failed_check_reports_numeric_status_and_keeps_button_working(
    hass, mock_api, exception_name, status, error
):
    from custom_components.vezdehod import api

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    sensor = hass.states.async_all("sensor")[0]
    assert sensor.state == "1"
    previous_success = sensor.attributes["last_successful_check"]
    previous_check = sensor.attributes["last_check"]
    coordinator = entry.runtime_data
    previous_data = coordinator.data

    mock_api[0].side_effect = getattr(api, exception_name)("not for display")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    sensor = hass.states.get(sensor.entity_id)
    assert sensor.state == str(status)
    assert sensor.attributes["last_error"] == error
    assert sensor.attributes["last_successful_check"] == previous_success
    assert sensor.attributes["last_check"] != previous_check
    assert coordinator.data == previous_data
    assert coordinator._unsub_refresh is not None
    button = hass.states.async_all("button")[0]
    assert button.state != "unavailable"
    mock_api[1].assert_not_awaited()

    # A real HA service must still dispatch exactly one explicit command.
    await hass.services.async_call(
        "button", "press", {"entity_id": button.entity_id}, blocking=True
    )
    mock_api[1].assert_awaited_once_with("101")

    mock_api[0].side_effect = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    sensor = hass.states.get(sensor.entity_id)
    assert sensor.state == "1"
    assert sensor.attributes["last_error"] is None
    assert sensor.attributes["last_successful_check"] != previous_success
    assert not list(entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))


@pytest.mark.parametrize(
    ("exception_name", "status"),
    [("CannotConnect", 0), ("ApiError", 0), ("InvalidAuth", -1)],
)
async def test_startup_with_failed_check_still_loads_button_and_status(
    hass, mock_api, exception_name, status
):
    from custom_components.vezdehod import api

    mock_api[0].side_effect = getattr(api, exception_name)("not for display")
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert entry.state == config_entries.ConfigEntryState.LOADED
    sensor = hass.states.async_all("sensor")[0]
    assert sensor.state == str(status)
    assert sensor.attributes["last_successful_check"] is None
    assert entry.runtime_data._unsub_refresh is not None
    button = hass.states.async_all("button")[0]
    assert button.state != "unavailable"
    await hass.services.async_call(
        "button", "press", {"entity_id": button.entity_id}, blocking=True
    )
    mock_api[1].assert_awaited_once_with("101")


async def test_unchanged_successful_list_updates_check_timestamp(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    sensor = hass.states.async_all("sensor")[0]
    previous_check = sensor.attributes["last_check"]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    updated = hass.states.get(sensor.entity_id)
    assert updated.state == "1"
    assert updated.attributes["last_check"] != previous_check
    assert updated.attributes["last_successful_check"] == updated.attributes["last_check"]
    mock_api[1].assert_not_awaited()


async def test_failed_check_during_reload_preserves_button_name(hass, mock_api):
    from custom_components.vezdehod.api import CannotConnect

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    button = hass.states.async_all("button")[0]
    original_name = button.attributes["friendly_name"]
    mock_api[0].side_effect = CannotConnect("not for display")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = hass.states.get(button.entity_id)
    assert restored.state != "unavailable"
    assert restored.attributes["friendly_name"] == original_name
    mock_api[1].assert_not_awaited()


async def test_rejected_opening_can_be_retried_with_same_key(hass, mock_api):
    from custom_components.vezdehod.api import InvalidAuth

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    entity_id = hass.states.async_all("button")[0].entity_id
    mock_api[1].side_effect = [InvalidAuth("not for display"), None]
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    assert raised.value.translation_key == "invalid_auth"
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unavailable"
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    assert mock_api[1].await_count == 2


async def begin_sms(hass, entry=None, source=config_entries.SOURCE_USER):
    context = {"source": source}
    if entry:
        context.update(entry_id=entry.entry_id, unique_id=entry.unique_id)
    menu = await hass.config_entries.flow.async_init(DOMAIN, context=context)
    return await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": "sms_phone"}
    )


async def test_sms_login_requests_only_after_phone_submission_and_saves_verified_key(
    hass, mock_api
):
    form = await begin_sms(hass)
    assert form["step_id"] == "sms_phone"
    mock_api[2].assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    assert result["step_id"] == "sms_code"
    mock_api[2].assert_awaited_once()
    mock_api[0].assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sms_code": "001234"}
    )
    assert result["step_id"] == "barriers"
    mock_api[3].assert_awaited_once_with("001234")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"barrier_ids": ["101"]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["auth_phone"] == PHONE
    key = result["data"]["mobile_key"]
    assert len(key) == 16 and all(char in "0123456789abcdef" for char in key)
    assert "sms_code" not in result["data"]
    mock_api[1].assert_not_awaited()


@pytest.mark.parametrize("phone", [PHONE + "abc", "7.9990001122", "7e10", "+" + PHONE])
async def test_phone_form_is_numeric_and_rejects_non_digits_without_sms(hass, mock_api, phone):
    form = await begin_sms(hass)
    phone_selector = next(iter(form["data_schema"].schema.values()))
    assert phone_selector.config["type"] == "number"
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": phone})
    assert result["errors"]["auth_phone"] == "invalid_phone"
    mock_api[2].assert_not_awaited()


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
async def test_sms_replaces_existing_key_only_after_confirmation(hass, mock_api, source):
    from custom_components.vezdehod.api import InvalidAuth

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    form = await begin_sms(hass, entry, source)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    assert result["step_id"] == "sms_code"
    assert entry.data["mobile_key"] == KEY
    mock_api[3].side_effect = InvalidAuth("not for display")
    mock_api[0].side_effect = InvalidAuth("pending key was not confirmed")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sms_code": "1234"})
    assert result["errors"]["base"] == "invalid_sms_code"
    assert entry.data["mobile_key"] == KEY
    mock_api[3].side_effect = None
    mock_api[0].side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sms_code": "5678"})
    assert result["type"] == FlowResultType.ABORT
    assert entry.data["mobile_key"] != KEY
    assert entry.options["barrier_ids"] == ["101"]
    mock_api[2].assert_awaited_once()
    mock_api[1].assert_not_awaited()


async def test_sms_resend_requires_explicit_request_and_sixty_second_pause(
    hass, mock_api, monkeypatch
):
    from custom_components.vezdehod import config_flow

    now = [100.0]
    monkeypatch.setattr(config_flow, "monotonic", lambda: now[0])
    form = await begin_sms(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    mock_api[2].assert_awaited_once()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"resend_sms": True})
    assert result["errors"]["base"] == "sms_cooldown"
    mock_api[2].assert_awaited_once()
    now[0] += 61
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"resend_sms": True})
    assert result["step_id"] == "sms_code" and not result["errors"]
    assert mock_api[2].await_count == 2
    mock_api[3].assert_not_awaited()


async def test_sms_reconfigure_rejects_different_account_before_sending(hass, mock_api):
    entry = make_entry()
    await hass.config_entries.async_add(entry)
    form = await begin_sms(hass, entry, config_entries.SOURCE_RECONFIGURE)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"auth_phone": "79990002233"}
    )
    assert result["errors"]["base"] == "wrong_account"
    mock_api[2].assert_not_awaited()


async def test_duplicate_account_does_not_request_another_sms(hass, mock_api):
    await hass.config_entries.async_add(make_entry())
    form = await begin_sms(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_api[2].assert_not_awaited()


async def test_unconfirmed_sms_delivery_allows_code_without_resending(hass, mock_api):
    from custom_components.vezdehod.api import CannotConnect

    form = await begin_sms(hass)
    mock_api[2].side_effect = CannotConnect("not for display")
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    assert result["step_id"] == "sms_code"
    assert result["errors"]["base"] == "sms_delivery_unknown"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sms_code": "001234"}
    )
    assert result["step_id"] == "barriers"
    mock_api[2].assert_awaited_once()
    mock_api[3].assert_awaited_once_with("001234")


async def test_confirmed_sms_retries_only_list_if_followup_request_fails(hass, mock_api):
    from custom_components.vezdehod.api import CannotConnect

    form = await begin_sms(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    mock_api[0].side_effect = CannotConnect("not for display")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sms_code": "1234"})
    assert result["errors"]["base"] == "cannot_connect"
    mock_api[0].side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "barriers"
    mock_api[3].assert_awaited_once_with("1234")
    mock_api[2].assert_awaited_once()


async def test_confirmed_sms_survives_transient_key_rejection(hass, mock_api):
    from custom_components.vezdehod.api import InvalidAuth

    form = await begin_sms(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    mock_api[0].side_effect = InvalidAuth("temporary rejection after confirmation")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sms_code": "1234"})
    assert result["errors"]["base"] == "invalid_auth"
    mock_api[0].side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "barriers"
    mock_api[3].assert_awaited_once_with("1234")
    assert mock_api[0].await_count == 2


@pytest.mark.parametrize("error_name", ["CannotConnect", "InvalidAuth"])
async def test_sms_confirmation_error_checks_whether_key_is_already_accepted(
    hass, mock_api, error_name
):
    from custom_components.vezdehod import api

    form = await begin_sms(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"auth_phone": PHONE})
    # The server may have consumed the code before the confirmation reply was lost.
    mock_api[3].side_effect = getattr(api, error_name)("not for display")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sms_code": "1234"})
    assert result["step_id"] == "barriers"
    mock_api[3].assert_awaited_once_with("1234")
    mock_api[0].assert_awaited_once()
    mock_api[1].assert_not_awaited()


async def test_recovered_key_does_not_cancel_user_entering_sms(hass, mock_api):
    from custom_components.vezdehod.api import InvalidAuth

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    mock_api[0].side_effect = InvalidAuth("not for display")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flow = list(entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))[0]
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "sms_phone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_phone": PHONE}
    )
    assert result["step_id"] == "sms_code"
    mock_api[0].side_effect = None
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    active = list(entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))
    assert len(active) == 1 and active[0]["step_id"] == "sms_code"
    mock_api[3].assert_not_awaited()


async def test_late_reauth_creation_is_cleared_by_next_successful_check(
    hass, mock_api, monkeypatch
):
    from custom_components.vezdehod.api import InvalidAuth

    entry = make_entry()
    await hass.config_entries.async_add(entry)
    original_init = hass.config_entries.flow.async_init
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_init(*args, **kwargs):
        if kwargs.get("context", {}).get("source") == config_entries.SOURCE_REAUTH:
            entered.set()
            await release.wait()
        return await original_init(*args, **kwargs)

    monkeypatch.setattr(hass.config_entries.flow, "async_init", delayed_init)
    mock_api[0].side_effect = InvalidAuth("not for display")
    await entry.runtime_data.async_refresh()
    await entered.wait()
    try:
        mock_api[0].side_effect = None
        await entry.runtime_data.async_refresh()
    finally:
        release.set()
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert not list(entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))
    mock_api[1].assert_not_awaited()
