"""Import a mobile key or obtain a separate HA key by an explicit SMS login."""

import re
from hashlib import sha256
from secrets import token_hex
from time import monotonic
from typing import Any

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, Barrier, CannotConnect, InvalidAuth, VezdehodApi, normalize_phone
from .const import (
    CONF_AUTH_PHONE,
    CONF_BARRIER_IDS,
    CONF_MOBILE_KEY,
    CONF_SAVE_SNAPSHOTS,
    DOMAIN,
)


def _phone_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.NUMBER, autocomplete="tel")
    )


def _form_phone(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10,15}", value):
        raise ValueError("Phone must contain digits only")
    return normalize_phone(value)


def _credentials_schema(phone: str = "") -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_AUTH_PHONE, default=phone): _phone_selector(),
            probatio.Required(CONF_MOBILE_KEY): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _barriers_schema(barriers: dict[str, Barrier], selected: list[str]) -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_BARRIER_IDS, default=selected): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    multiple=True,
                    options=[
                        selector.SelectOptionDict(value=uid, label=barrier.name)
                        for uid, barrier in barriers.items()
                    ],
                )
            ),
        }
    )


async def _validate_credentials(
    hass: HomeAssistant, data: dict[str, Any], *, require_barriers: bool = True
) -> tuple[dict[str, str], dict[str, Barrier], dict[str, str]]:
    errors: dict[str, str] = {}
    try:
        phone = _form_phone(data.get(CONF_AUTH_PHONE, ""))
    except ValueError:
        phone = ""
        errors[CONF_AUTH_PHONE] = "invalid_phone"
    key = data.get(CONF_MOBILE_KEY, "")
    if not isinstance(key, str) or not key.strip():
        errors[CONF_MOBILE_KEY] = "invalid_key"
    if errors:
        return {}, {}, errors
    credentials = {CONF_AUTH_PHONE: phone, CONF_MOBILE_KEY: key.strip()}
    api = VezdehodApi(async_get_clientsession(hass), phone, key.strip())
    try:
        all_barriers = await api.async_get_barriers()
    except InvalidAuth:
        errors["base"] = "invalid_auth"
    except CannotConnect:
        errors["base"] = "cannot_connect"
    except ApiError:
        errors["base"] = "invalid_response"
    else:
        barriers = {uid: barrier for uid, barrier in all_barriers.items() if barrier.can_open}
        if barriers or not require_barriers:
            return credentials, barriers, errors
        errors["base"] = "no_barriers"
    return credentials, {}, errors


def _selection_error(data: dict[str, Any], barriers: dict[str, Barrier]) -> str | None:
    selected = data.get(CONF_BARRIER_IDS)
    if not isinstance(selected, list) or not selected:
        return "no_selection"
    if any(not isinstance(uid, str) or uid not in barriers for uid in selected):
        return "unknown_barrier"
    return None


class VezdehodConfigFlow(ConfigFlow, domain=DOMAIN):
    """Choose an imported session or an explicit, user-confirmed SMS login."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._barriers: dict[str, Barrier] = {}
        self._sms_credentials: dict[str, str] = {}
        self._sms_requested_at: float | None = None
        self._sms_confirmed = False
        self._sms_confirmation_uncertain = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VezdehodOptionsFlow()

    def _existing_entry(self) -> ConfigEntry | None:
        if self.context.get("source") == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.context.get("source") == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    def _check_phone(self, value: Any) -> tuple[str, dict[str, str]]:
        try:
            phone = _form_phone(value)
        except ValueError:
            return "", {CONF_AUTH_PHONE: "invalid_phone"}
        entry = self._existing_entry()
        if entry and phone != entry.data[CONF_AUTH_PHONE]:
            return phone, {"base": "wrong_account"}
        return phone, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["sms_phone", "import_key"])

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reauth", menu_options=["sms_phone", "import_key"])

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reconfigure", menu_options=["sms_phone", "import_key"])

    async def _async_accept_credentials(
        self, credentials: dict[str, str], barriers: dict[str, Barrier]
    ) -> ConfigFlowResult:
        entry = self._existing_entry()
        if entry:
            reason = (
                "reauth_successful"
                if self.context.get("source") == SOURCE_REAUTH
                else "reconfigure_successful"
            )
            entries = self.hass.config_entries
            # The listener can start unloading the entry during the update.
            has_update_listener = bool(entry.update_listeners)
            changed = entries.async_update_entry(entry, data=entry.data | credentials)
            if not changed or not has_update_listener:
                entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason=reason)
        self._credentials, self._barriers = credentials, barriers
        await self.async_set_unique_id(sha256(credentials[CONF_AUTH_PHONE].encode()).hexdigest())
        self._abort_if_unique_id_configured()
        return await self.async_step_barriers()

    async def async_step_import_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._existing_entry()
        phone = entry.data[CONF_AUTH_PHONE] if entry else ""
        errors: dict[str, str] = {}
        if user_input is not None:
            phone, errors = self._check_phone(user_input.get(CONF_AUTH_PHONE, phone))
            if not errors:
                credentials, barriers, errors = await _validate_credentials(
                    self.hass,
                    {**user_input, CONF_AUTH_PHONE: phone},
                    require_barriers=entry is None,
                )
                if not errors:
                    return await self._async_accept_credentials(credentials, barriers)
        return self.async_show_form(
            step_id="import_key", data_schema=_credentials_schema(phone), errors=errors
        )

    async def async_step_barriers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _selection_error(user_input, self._barriers)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="Вездеход",
                    data=self._credentials,
                    options={CONF_BARRIER_IDS: list(dict.fromkeys(user_input[CONF_BARRIER_IDS]))},
                )
        selected = list(self._barriers) if len(self._barriers) == 1 else []
        return self.async_show_form(
            step_id="barriers",
            data_schema=_barriers_schema(self._barriers, selected),
            errors=errors,
        )

    def _sms_api(self) -> VezdehodApi:
        return VezdehodApi(
            async_get_clientsession(self.hass),
            self._sms_credentials[CONF_AUTH_PHONE],
            self._sms_credentials[CONF_MOBILE_KEY],
        )

    async def _async_send_sms(self) -> str | None:
        now = monotonic()
        if self._sms_requested_at is not None and now - self._sms_requested_at < 60:
            return "sms_cooldown"
        # Keep the same key and enforce a pause even if the response is lost.
        self._sms_requested_at = now
        self._sms_confirmed = False
        self._sms_confirmation_uncertain = False
        try:
            await self._sms_api().async_request_sms()
        except CannotConnect:
            return "sms_delivery_unknown"
        except InvalidAuth, ApiError:
            return "cannot_send_sms"
        return None

    async def async_step_sms_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._existing_entry()
        phone = entry.data[CONF_AUTH_PHONE] if entry else ""
        errors: dict[str, str] = {}
        if user_input is not None:
            phone, errors = self._check_phone(user_input.get(CONF_AUTH_PHONE, ""))
            if not errors:
                if entry is None:
                    await self.async_set_unique_id(sha256(phone.encode()).hexdigest())
                    self._abort_if_unique_id_configured()
                if self._sms_credentials.get(CONF_AUTH_PHONE) != phone:
                    self._sms_credentials = {CONF_AUTH_PHONE: phone, CONF_MOBILE_KEY: token_hex(8)}
                    self._sms_requested_at = None
                    self._sms_confirmed = False
                error = await self._async_send_sms()
                if error in (None, "sms_delivery_unknown"):
                    return self._show_sms_code({"base": error} if error else {})
                errors["base"] = error
        return self.async_show_form(
            step_id="sms_phone",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_AUTH_PHONE, default=phone): _phone_selector()}
            ),
            errors=errors,
        )

    def _show_sms_code(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="sms_code",
            data_schema=probatio.Schema(
                {
                    probatio.Optional("sms_code"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEL, autocomplete="one-time-code"
                        )
                    ),
                    probatio.Optional("resend_sms", default=False): selector.BooleanSelector(),
                }
            ),
            description_placeholders={"phone": self._sms_credentials[CONF_AUTH_PHONE]},
            errors=errors,
        )

    async def async_step_sms_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._sms_credentials:
            return await self.async_step_sms_phone()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("resend_sms"):
                error = await self._async_send_sms()
                return self._show_sms_code({"base": error} if error else {})
            verify_key = self._sms_confirmed or self._sms_confirmation_uncertain
            confirmation_error = None
            if not self._sms_confirmed and not (
                self._sms_confirmation_uncertain and not user_input.get("sms_code")
            ):
                code = user_input.get("sms_code", "")
                if not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,10}", code):
                    errors["sms_code"] = "invalid_sms_code"
                else:
                    try:
                        await self._sms_api().async_confirm_sms(code)
                    except InvalidAuth:
                        # A previously consumed code can be rejected even when
                        # its client key is already accepted. Check that key.
                        confirmation_error = "invalid_sms_code"
                        verify_key = True
                    except CannotConnect:
                        confirmation_error = "cannot_connect"
                        self._sms_confirmation_uncertain = True
                        verify_key = True
                    except ApiError:
                        errors["base"] = "invalid_response"
                    else:
                        self._sms_confirmed = True
                        self._sms_confirmation_uncertain = False
                        verify_key = True
            if not errors and verify_key:
                credentials, barriers, errors = await _validate_credentials(
                    self.hass,
                    self._sms_credentials,
                    require_barriers=self._existing_entry() is None,
                )
                if not errors:
                    return await self._async_accept_credentials(credentials, barriers)
                if errors.get("base") == "no_barriers":
                    self._sms_confirmed = True
                    self._sms_confirmation_uncertain = False
                elif errors.get("base") == "invalid_auth" and confirmation_error:
                    errors = {"base": confirmation_error}
        return self._show_sms_code(errors)


class VezdehodOptionsFlow(OptionsFlow):
    """Change which account barriers have buttons."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        save_snapshots = self.config_entry.options.get(CONF_SAVE_SNAPSHOTS, False)
        _, barriers, errors = await _validate_credentials(
            self.hass, dict(self.config_entry.data), require_barriers=False
        )
        # Photo settings must remain editable while the service is offline.
        # Only previously selected IDs are offered as a fallback.
        for uid in self.config_entry.options.get(CONF_BARRIER_IDS, []):
            if uid not in barriers:
                coordinator = getattr(self.config_entry, "runtime_data", None)
                known = coordinator.data.get(uid) if coordinator else None
                barriers[uid] = known or Barrier(uid, f"Шлагбаум {uid}", False)
        errors = {}
        if user_input is not None and not errors:
            error = _selection_error(user_input, barriers)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_BARRIER_IDS: list(dict.fromkeys(user_input[CONF_BARRIER_IDS])),
                        CONF_SAVE_SNAPSHOTS: user_input.get(CONF_SAVE_SNAPSHOTS, save_snapshots),
                    },
                )
        selected = [
            uid for uid in self.config_entry.options.get(CONF_BARRIER_IDS, []) if uid in barriers
        ]
        schema = probatio.Schema(
            {
                **_barriers_schema(barriers, selected).schema,
                probatio.Required(
                    CONF_SAVE_SNAPSHOTS,
                    default=save_snapshots,
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
