"""Expose the last facility-check result as an account-wide numeric sensor."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VezdehodConfigEntry
from .const import DOMAIN
from .coordinator import VezdehodCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VezdehodConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([VezdehodKeyStatus(entry)])


class VezdehodKeyStatus(CoordinatorEntity[VezdehodCoordinator], SensorEntity):
    """1: accepted, 0: check failed, -1: credentials explicitly rejected."""

    _attr_has_entity_name = True
    _attr_translation_key = "key_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:key-wireless"

    def __init__(self, entry: VezdehodConfigEntry) -> None:
        super().__init__(entry.runtime_data)
        self._attr_unique_id = f"{entry.unique_id}_key_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name="Вездеход",
            manufacturer="Комендант 24",
            model="Учётная запись",
        )

    @property
    def available(self) -> bool:
        """Keep error codes visible instead of replacing them with unavailable."""
        return True

    @property
    def native_value(self) -> int:
        return self.coordinator.key_status

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        coordinator = self.coordinator
        return {
            "last_check": coordinator.last_check.isoformat() if coordinator.last_check else None,
            "last_successful_check": (
                coordinator.last_successful_check.isoformat()
                if coordinator.last_successful_check
                else None
            ),
            "last_error": coordinator.last_error,
        }
