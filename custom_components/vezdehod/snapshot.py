"""Save opening photos in HA media without delaying barrier commands."""

import asyncio
import logging
import re
from asyncio import sleep
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .api import ApiError, CannotConnect
from .const import CONF_BARRIER_IDS, CONF_SAVE_SNAPSHOTS, DOMAIN, SNAPSHOT_DELAY

_LOGGER = logging.getLogger(__name__)
_LOCAL_FILENAME = "%Y-%m-%d_%H-%M-%S_%f%z"

if TYPE_CHECKING:
    from .coordinator import VezdehodCoordinator


@dataclass(frozen=True)
class Snapshot:
    file_path: Path
    created_at: datetime
    media_content_id: str
    saved_at: datetime

    def attributes(self) -> dict[str, str]:
        return {
            "file_path": str(self.file_path),
            "captured_at": self.created_at.isoformat(),
            "saved_at": self.saved_at.isoformat(),
            "media_content_id": self.media_content_id,
        }


class VezdehodSnapshots:
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: VezdehodCoordinator
    ) -> None:
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        dirs = hass.config.media_dirs
        self._media_id = "local" if "local" in dirs else next(iter(dirs), "local")
        self._media_root = Path(dirs.get(self._media_id, hass.config.path("media")))
        account = sha256((entry.unique_id or entry.entry_id).encode()).hexdigest()[:16]
        self.root = self._media_root / DOMAIN / account
        self.signal = f"{DOMAIN}_snapshot_{entry.entry_id}"
        self.latest: dict[str, Snapshot] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sequence = 0
        self._latest_sequence: dict[str, int] = {}

    def _directory(self, uid: str) -> Path:
        if not re.fullmatch(r"[0-9]+", uid) or int(uid) <= 0:
            raise ValueError("Invalid barrier identifier")
        return self.root / uid

    def _snapshot(self, path: Path) -> Snapshot:
        if path.stem.endswith("Z"):
            timestamp = datetime.strptime(path.stem, "%Y%m%dT%H%M%S_%fZ").replace(
                tzinfo=dt_util.UTC
            )
        else:
            timestamp = datetime.strptime(path.stem, _LOCAL_FILENAME)
        timestamp = dt_util.as_utc(timestamp)
        relative = path.relative_to(self._media_root).as_posix()
        return Snapshot(
            path,
            timestamp,
            f"media-source://media_source/{self._media_id}/{relative}",
            datetime.fromtimestamp(path.stat().st_mtime, tz=dt_util.UTC),
        )

    async def async_initialize(self) -> None:
        def restore():
            result = {}
            for uid in self.entry.options.get(CONF_BARRIER_IDS, []):
                try:
                    files = list(self._directory(uid).glob("*.jpg"))
                except OSError, ValueError:
                    continue
                for path in files:
                    try:
                        if path.resolve().is_relative_to(self.root.resolve()):
                            snapshot = self._snapshot(path)
                            if uid not in result or snapshot.created_at > result[uid].created_at:
                                result[uid] = snapshot
                    except OSError, ValueError:
                        continue
            return result

        self.latest = await self.hass.async_add_executor_job(restore)

    @callback
    def async_cancel(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    @callback
    def async_schedule(self, uid: str, entity_id: str | None, context: Context | None) -> None:
        if not self.entry.options.get(CONF_SAVE_SNAPSHOTS, True):
            return
        barrier = (self.coordinator.data or {}).get(uid)
        if not barrier or not barrier.camera_url:
            return
        self._sequence += 1
        task = self.hass.async_create_background_task(
            self._async_save(
                uid,
                barrier.camera_url,
                entity_id,
                context,
                dt_util.utcnow(),
                self._sequence,
            ),
            f"Vezdehod opening photo {uid}",
            eager_start=True,
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _write(self, uid: str, content: bytes, created_at: datetime) -> Snapshot:
        directory = self._directory(uid)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(self.root.resolve()):
            raise OSError("Snapshot directory is outside its media root")
        target = directory / (dt_util.as_local(created_at).strftime(_LOCAL_FILENAME) + ".jpg")
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return self._snapshot(target)

    async def _async_save(
        self,
        uid: str,
        url: str,
        entity_id: str | None,
        context: Context | None,
        requested_at: datetime,
        sequence: int,
    ) -> None:
        try:
            await sleep(SNAPSHOT_DELAY)
            content = await self.coordinator.api.async_get_camera_image(url)
            snapshot = await self.hass.async_add_executor_job(
                self._write, uid, content, requested_at
            )
        except ApiError, CannotConnect, OSError, ValueError:
            _LOGGER.warning("Unable to save opening photo")
            return
        is_latest = sequence > self._latest_sequence.get(uid, 0)
        if is_latest:
            self._latest_sequence[uid] = sequence
            self.latest[uid] = snapshot
            async_dispatcher_send(self.hass, self.signal, uid, context)
        self.hass.bus.async_fire(
            f"{DOMAIN}_snapshot_saved",
            {
                "barrier_id": uid,
                "entity_id": entity_id,
                "is_latest": is_latest,
                **snapshot.attributes(),
            },
            context=context,
        )
