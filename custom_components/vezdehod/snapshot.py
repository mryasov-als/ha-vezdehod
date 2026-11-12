"""Save opening photos in the HA configuration directory."""

import asyncio
import logging
import re
from asyncio import sleep
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from shutil import copy2, copystat
from tempfile import TemporaryDirectory
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
    saved_at: datetime

    def attributes(self) -> dict[str, str]:
        return {
            "file_path": str(self.file_path),
            "captured_at": self.created_at.isoformat(),
            "saved_at": self.saved_at.isoformat(),
        }


class VezdehodSnapshots:
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: VezdehodCoordinator
    ) -> None:
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        media_roots = tuple(map(Path, hass.config.media_dirs.values()))
        legacy_media_roots = dict.fromkeys((*media_roots, Path(hass.config.path("media"))))
        account = sha256((entry.unique_id or entry.entry_id).encode()).hexdigest()[:16]
        self.root = Path(hass.config.path(DOMAIN, "snapshots", account))
        self._legacy_roots = tuple(root / DOMAIN / account for root in legacy_media_roots)
        self._shared_roots = (*media_roots, Path(hass.config.path("www")))
        self.signal = f"{DOMAIN}_snapshot_{entry.entry_id}"
        self.latest: dict[str, Snapshot] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sequence = 0
        self._latest_sequence: dict[str, int] = {}

    def _directory(self, uid: str) -> Path:
        if not re.fullmatch(r"[0-9]+", uid) or int(uid) <= 0:
            raise ValueError("Invalid barrier identifier")
        directory = self.root / uid
        resolved = directory.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise OSError("Snapshot directory is outside the archive")
        if any(resolved.is_relative_to(root.resolve()) for root in self._shared_roots):
            raise OSError("Snapshot directory is shared through Media or www")
        return directory

    def _snapshot(self, path: Path) -> Snapshot:
        if path.stem.endswith("Z"):
            timestamp = datetime.strptime(path.stem, "%Y%m%dT%H%M%S_%fZ").replace(
                tzinfo=dt_util.UTC
            )
        else:
            timestamp = datetime.strptime(path.stem, _LOCAL_FILENAME)
        timestamp = dt_util.as_utc(timestamp)
        return Snapshot(
            path,
            timestamp,
            datetime.fromtimestamp(path.stat().st_mtime, tz=dt_util.UTC),
        )

    def _move_legacy_photo(self, source: Path) -> None:
        self._snapshot(source)  # Only move files with a snapshot filename.
        target = self._directory(source.parent.name) / source.name
        if target.exists():
            if target.is_symlink() or target.read_bytes() != source.read_bytes():
                raise FileExistsError("Different opening photo at the destination")
            if target.resolve() == source.resolve():
                return
            if target.stat().st_mtime_ns != source.stat().st_mtime_ns:
                copystat(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Media and configuration can be separate mounts. Rename the
            # complete copy within the destination before removing the source.
            with TemporaryDirectory(dir=target.parent) as staging_dir:
                temporary = Path(staging_dir) / source.name
                copy2(source, temporary)
                temporary.replace(target)
        source.unlink()

    def _migrate_legacy(self, legacy_root: Path) -> None:
        try:
            resolved_root = legacy_root.resolve()
            if resolved_root == self.root.resolve():
                return
            paths = list(legacy_root.glob("*/*.jpg"))
        except OSError as err:
            _LOGGER.warning("Unable to read old opening photo archive: %s", err)
            return
        for path in paths:
            try:
                if path.is_file(follow_symlinks=False) and path.resolve().is_relative_to(
                    resolved_root
                ):
                    self._move_legacy_photo(path)
            except ValueError:
                continue
            except OSError as err:
                _LOGGER.warning("Unable to migrate opening photo %s: %s", path.name, err)

    async def async_initialize(self) -> None:
        def restore():
            for legacy_root in self._legacy_roots:
                self._migrate_legacy(legacy_root)
            result = {}
            for uid in self.entry.options.get(CONF_BARRIER_IDS, []):
                try:
                    files = list(self._directory(uid).glob("*.jpg"))
                except OSError, ValueError:
                    continue
                for path in files:
                    try:
                        if path.is_file(follow_symlinks=False):
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
        if not self.entry.options.get(CONF_SAVE_SNAPSHOTS, False):
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
