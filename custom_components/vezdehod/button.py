"""Expose single opening commands as Home Assistant buttons."""

import time

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VezdehodConfigEntry
from .api import ApiError, CannotConnect, InvalidAuth
from .const import CONF_BARRIER_IDS, DOMAIN, PRESS_COOLDOWN
from .coordinator import VezdehodCoordinator
from .entity import barrier_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VezdehodConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        VezdehodButton(coordinator, entry, uid) for uid in entry.options.get(CONF_BARRIER_IDS, [])
    )


class VezdehodButton(CoordinatorEntity[VezdehodCoordinator], ButtonEntity):
    """A stateless button, never a simulated open/closed sensor."""

    _attr_has_entity_name = True
    _attr_translation_key = "open"
    _attr_icon = "mdi:boom-gate-up"

    def __init__(
        self, coordinator: VezdehodCoordinator, entry: VezdehodConfigEntry, uid: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._uid = uid
        self._opening = False
        self._last_opened: float | None = None
        barrier = coordinator.data.get(uid)
        self._attr_unique_id = f"{entry.unique_id}_{uid}_open"
        self._attr_device_info = barrier_device_info(coordinator.hass, entry, uid, barrier)

    @property
    def available(self) -> bool:
        """Let the server decide whether each explicit command is permitted."""
        return True

    async def async_press(self) -> None:
        if self._opening or (
            self._last_opened is not None and time.monotonic() - self._last_opened < PRESS_COOLDOWN
        ):
            raise self._error("busy")
        self._opening = True
        self.coordinator.snapshots.async_schedule(self._uid, self.entity_id, self._context)
        try:
            await self.coordinator.api.async_open_barrier(self._uid)
        except InvalidAuth:
            self.coordinator.async_start_reauth()
            raise self._error("invalid_auth") from None
        except CannotConnect:
            raise self._error("cannot_connect") from None
        except ApiError:
            raise self._error("opening_not_confirmed") from None
        else:
            self._last_opened = time.monotonic()
        finally:
            self._opening = False

    @staticmethod
    def _error(key: str) -> HomeAssistantError:
        return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)
