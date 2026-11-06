"""Refresh the account's facility list without opening any barrier."""

import logging
from datetime import datetime

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import ApiError, Barrier, CannotConnect, InvalidAuth, VezdehodApi
from .const import DOMAIN, UPDATE_INTERVAL
from .snapshot import VezdehodSnapshots

_LOGGER = logging.getLogger(__name__)


class VezdehodCoordinator(DataUpdateCoordinator[dict[str, Barrier]]):
    """Report checks separately from the ability to send explicit commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: VezdehodApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=True,
        )
        self.api = api
        self.key_status = 0
        self.last_check: datetime | None = None
        self.last_successful_check: datetime | None = None
        self.last_error: str | None = None
        self._reauth_requested = False
        self.snapshots = VezdehodSnapshots(hass, entry, self)

    @callback
    def async_start_reauth(self) -> None:
        """Offer a replacement key without stopping checks or button commands."""
        if self.config_entry and self.hass.config_entries.async_get_entry(
            self.config_entry.entry_id
        ):
            self._reauth_requested = True
            self.config_entry.async_start_reauth(self.hass)

    async def _async_update_data(self) -> dict[str, Barrier]:
        status, error = 1, None
        try:
            barriers = await self.api.async_get_barriers()
        except InvalidAuth:
            status, error = -1, "invalid_auth"
            barriers = self.data or {}
        except CannotConnect:
            status, error = 0, "cannot_connect"
            barriers = self.data or {}
        except ApiError:
            status, error = 0, "invalid_response"
            barriers = self.data or {}

        if error and (status, error) != (self.key_status, self.last_error):
            _LOGGER.warning("Vezdehod facility check failed: %s", error)
        self.key_status = status
        self.last_error = error
        self.last_check = dt_util.utcnow()
        if status == -1:
            self.async_start_reauth()
        elif status == 1:
            self.last_successful_check = self.last_check
            if self._reauth_requested and self.config_entry:
                # A transient rejection can recover with the same stored key.
                # Aborting the flow also removes its standard HA repair issue.
                flows = list(self.config_entry.async_get_active_flows(self.hass, {SOURCE_REAUTH}))
                for flow in flows:
                    if flow.get("step_id") in ("reauth", "reauth_confirm"):
                        self.hass.config_entries.flow.async_abort(flow["flow_id"])
                # Keep looking if HA has not finished creating the form yet.
                # Once the user chooses a login method, leave that flow alone.
                if any(flow.get("step_id") for flow in flows):
                    self._reauth_requested = False
        return barriers
