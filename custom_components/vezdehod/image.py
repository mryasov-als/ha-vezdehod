"""Expose the last archived opening photo through HA's standard image entity."""

from homeassistant.components.image import ImageEntity
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VezdehodConfigEntry
from .const import CONF_BARRIER_IDS
from .entity import barrier_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VezdehodConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        VezdehodOpeningImage(hass, entry, uid) for uid in entry.options.get(CONF_BARRIER_IDS, [])
    )


class VezdehodOpeningImage(ImageEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "opening_photo"

    def __init__(self, hass: HomeAssistant, entry: VezdehodConfigEntry, uid: str) -> None:
        super().__init__(hass)
        self._uid = uid
        self._snapshots = entry.runtime_data.snapshots
        self._attr_unique_id = f"{entry.unique_id}_{uid}_opening_photo"
        self._attr_device_info = barrier_device_info(
            hass, entry, uid, entry.runtime_data.data.get(uid)
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._snapshots.signal, self._snapshot_updated)
        )
        if snapshot := self._snapshots.latest.get(self._uid):
            self._attr_image_last_updated = snapshot.created_at

    @callback
    def _snapshot_updated(self, uid: str, context: Context | None) -> None:
        if uid != self._uid:
            return
        snapshot = self._snapshots.latest.get(uid)
        self._attr_image_last_updated = snapshot.created_at if snapshot else None
        if context:
            self.async_set_context(context)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._uid in self._snapshots.latest

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        snapshot = self._snapshots.latest.get(self._uid)
        return (
            {"barrier_id": self._uid, **snapshot.attributes()}
            if snapshot
            else {"barrier_id": self._uid}
        )

    async def async_image(self) -> bytes | None:
        return await self._snapshots.async_read_latest(self._uid)
