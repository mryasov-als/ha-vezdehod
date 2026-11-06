"""Serve a standard HA camera from on-demand, regularly refreshed JPEGs."""

import asyncio
from time import monotonic

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VezdehodConfigEntry
from .api import ApiError, CannotConnect
from .const import CONF_BARRIER_IDS
from .coordinator import VezdehodCoordinator
from .entity import barrier_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VezdehodConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        VezdehodCamera(entry, uid) for uid in entry.options.get(CONF_BARRIER_IDS, [])
    )


class VezdehodCamera(CoordinatorEntity[VezdehodCoordinator], Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_frame_interval = 1.0

    def __init__(self, entry: VezdehodConfigEntry, uid: str) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, entry.runtime_data)
        self._uid = uid
        self._attr_unique_id = f"{entry.unique_id}_{uid}_camera"
        self._attr_device_info = barrier_device_info(
            self.coordinator.hass, entry, uid, self.coordinator.data.get(uid)
        )
        self._image_lock = asyncio.Lock()
        self._last_image: bytes | None = None
        self._last_request = float("-inf")
        self._last_url: str | None = None

    @property
    def available(self) -> bool:
        barrier = (self.coordinator.data or {}).get(self._uid)
        return bool(barrier and barrier.camera_url)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        barrier = (self.coordinator.data or {}).get(self._uid)
        if not barrier or not (url := barrier.camera_url):
            return None
        async with self._image_lock:
            now = monotonic()
            if url == self._last_url and now - self._last_request < self.frame_interval:
                return self._last_image
            self._last_request, self._last_url = now, url
            try:
                self._last_image = await self.coordinator.api.async_get_camera_image(url)
            except ApiError, CannotConnect:
                self._last_image = None
            return self._last_image
