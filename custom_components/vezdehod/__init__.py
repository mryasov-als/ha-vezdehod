"""Use an existing Vezdehod phone session from Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VezdehodApi
from .const import CONF_AUTH_PHONE, CONF_BARRIER_IDS, CONF_MOBILE_KEY, DOMAIN
from .coordinator import VezdehodCoordinator

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.CAMERA, Platform.IMAGE]

type VezdehodConfigEntry = ConfigEntry[VezdehodCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VezdehodConfigEntry) -> bool:
    api = VezdehodApi(
        async_get_clientsession(hass),
        entry.data[CONF_AUTH_PHONE],
        entry.data[CONF_MOBILE_KEY],
    )
    coordinator = VezdehodCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.snapshots.async_initialize()
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.snapshots.async_cancel)
    # A changed selection must remove the old button, not leave a restored ghost.
    registry = entity_registry.async_get(hass)
    selected = {
        f"{entry.unique_id}_{uid}_{suffix}"
        for uid in entry.options.get(CONF_BARRIER_IDS, [])
        for suffix in ("open", "camera", "opening_photo")
    }
    for entity in entity_registry.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain in ("button", "camera", "image")
            and entity.platform == DOMAIN
            and entity.unique_id not in selected
        ):
            registry.async_remove(entity.entity_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VezdehodConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: VezdehodConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
