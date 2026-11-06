"""Share a stable barrier device between controls and pictures."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.helpers.entity import DeviceInfo

from .api import Barrier
from .const import DOMAIN


def barrier_device_info(
    hass: HomeAssistant, entry: ConfigEntry, uid: str, barrier: Barrier | None
) -> DeviceInfo:
    identifier = (DOMAIN, f"{entry.unique_id}_{uid}")
    existing = device_registry.async_get(hass).async_get_device_by_identifier(
        identifier, entry.entry_id
    )
    name = barrier.name if barrier else (existing.name if existing else None)
    return DeviceInfo(
        identifiers={identifier},
        name=name or f"Шлагбаум {uid}",
        manufacturer="Комендант 24",
        model="Шлагбаум",
    )
