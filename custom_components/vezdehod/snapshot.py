"""Save opening photos in the HA configuration directory."""

import logging
import re
from asyncio import sleep
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from shutil import copy2, copystat
from tempfile import TemporaryDirectory
from threading import Event, Lock
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .api import ApiError, CannotConnect
from .const import CONF_BARRIER_IDS, CONF_SAVE_SNAPSHOTS, DOMAIN, SNAPSHOT_DELAY

_LOGGER = logging.getLogger(__name__)
_LOCAL_FILENAME = "%Y-%m-%d_%H-%M-%S_%f%z"
_DEFAULT_MEDIA_ROOT = Path("/media")
_ARCHIVE_COMMIT_LOCK = Lock()

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
        legacy_media_roots = dict.fromkeys(
            (*media_roots, _DEFAULT_MEDIA_ROOT, Path(hass.config.path("media")))
        )
        account = sha256((entry.unique_id or entry.entry_id).encode()).hexdigest()[:16]
        self.root = Path(hass.config.path(DOMAIN, "snapshots", account))
        self._legacy_roots = tuple(root / DOMAIN / account for root in legacy_media_roots)
        self._shared_roots = (*media_roots, Path(hass.config.path("www")))
        self.signal = f"{DOMAIN}_snapshot_{entry.entry_id}"
        self.latest: dict[str, Snapshot] = {}
        self._stopping = Event()
        self._sequence = 0
        self._latest_sequence: dict[str, int] = {}

    def _directory(self, uid: str, *, for_write: bool = False) -> Path:
        if not re.fullmatch(r"[0-9]+", uid) or int(uid) <= 0:
            raise ValueError("Invalid barrier identifier")
        directory = self.root / uid
        resolved = directory.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise OSError("Snapshot directory is outside the archive")
        if for_write and any(
            resolved.is_relative_to(root.resolve()) for root in self._shared_roots
        ):
            raise OSError("Snapshot directory is shared through Media or www")
        return directory

    def _check_running(self) -> None:
        if self._stopping.is_set():
            raise InterruptedError("Snapshot archive is unloading")

    def _snapshot(self, path: Path) -> Snapshot:
        name, separator, digest = path.stem.partition(".")
        if separator and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid opening photo filename")
        if name.endswith("Z"):
            timestamp = datetime.strptime(name, "%Y%m%dT%H%M%S_%fZ").replace(tzinfo=dt_util.UTC)
        else:
            timestamp = datetime.strptime(name, _LOCAL_FILENAME)
        timestamp = dt_util.as_utc(timestamp)
        return Snapshot(
            path,
            timestamp,
            datetime.fromtimestamp(path.stat().st_mtime, tz=dt_util.UTC),
        )

    def _commit_photo(self, staged: Path, target: Path) -> Path:
        """Publish a complete JPEG without overwriting a different frame."""
        with _ARCHIVE_COMMIT_LOCK:
            self._check_running()
            if target.exists():
                content = staged.read_bytes()
                if target.is_symlink() or target.read_bytes() != content:
                    timestamp = target.stem.split(".", 1)[0]
                    target = target.with_name(f"{timestamp}.{sha256(content).hexdigest()}.jpg")
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != content:
                        raise FileExistsError("Different opening photo at the destination")
                    if target.stat().st_mtime_ns != staged.stat().st_mtime_ns:
                        self._check_running()
                        copystat(staged, target)
                    return target
            self._check_running()
            staged.replace(target)
        return target

    def _move_legacy_photo(self, source: Path) -> None:
        self._snapshot(source)  # Only move files with a snapshot filename.
        target = self._directory(source.parent.name, for_write=True) / source.name
        if target.resolve() == source.resolve():
            return
        self._check_running()
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target.parent) as staging_dir:
            staged = Path(staging_dir) / source.name
            copy2(source, staged)
            self._commit_photo(staged, target)
        self._check_running()
        source.unlink(missing_ok=True)

    def _migrate_legacy(self, legacy_root: Path) -> bool:
        try:
            resolved_root = legacy_root.resolve()
            if resolved_root == self.root.resolve():
                return False
            paths = list(legacy_root.glob("*/*.jpg"))
        except OSError as err:
            _LOGGER.warning("Unable to read old opening photo archive: %s", err)
            return False
        found_photo = False
        for path in paths:
            if self._stopping.is_set():
                return found_photo
            try:
                if path.is_file(follow_symlinks=False) and path.resolve().is_relative_to(
                    resolved_root
                ):
                    found_photo = True
                    self._move_legacy_photo(path)
            except ValueError:
                continue
            except OSError as err:
                if self._stopping.is_set():
                    return found_photo
                _LOGGER.warning("Unable to migrate opening photo %s: %s", path.name, err)
        return found_photo

    def _find_latest(self, uid: str) -> Snapshot | None:
        try:
            files = list(self._directory(uid).glob("*.jpg"))
        except OSError, ValueError:
            return None
        latest = None
        for path in files:
            try:
                if path.is_file(follow_symlinks=False):
                    snapshot = self._snapshot(path)
                    if latest is None or (snapshot.created_at, snapshot.saved_at) > (
                        latest.created_at,
                        latest.saved_at,
                    ):
                        latest = snapshot
            except OSError, ValueError:
                continue
        return latest

    def _restore(self) -> dict[str, Snapshot]:
        return {
            uid: snapshot
            for uid in self.entry.options.get(CONF_BARRIER_IDS, [])
            if (snapshot := self._find_latest(uid)) is not None
        }

    def _restore_legacy(self) -> dict[str, Snapshot]:
        found_photo = False
        for legacy_root in self._legacy_roots:
            if self._stopping.is_set():
                return {}
            if self._migrate_legacy(legacy_root):
                found_photo = True
        return self._restore() if found_photo else {}

    @callback
    def _async_apply_restored(self, restored: dict[str, Snapshot]) -> None:
        for uid, snapshot in restored.items():
            if uid not in self._latest_sequence and self.latest.get(uid) != snapshot:
                self._async_set_latest(uid, snapshot)

    async def _async_restore(self) -> None:
        self._async_apply_restored(await self.hass.async_add_executor_job(self._restore))
        self._async_apply_restored(await self.hass.async_add_executor_job(self._restore_legacy))

    @callback
    def async_start_restore(self) -> None:
        self.entry.async_create_background_task(
            self.hass, self._async_restore(), "Restore Vezdehod opening photos"
        )

    @callback
    def async_cancel(self) -> None:
        self._stopping.set()

    @callback
    def _async_set_latest(
        self, uid: str, snapshot: Snapshot | None, context: Context | None = None
    ) -> None:
        if snapshot is None:
            self.latest.pop(uid, None)
        else:
            self.latest[uid] = snapshot
        async_dispatcher_send(self.hass, self.signal, uid, context)

    async def async_read_latest(self, uid: str) -> bytes | None:
        while snapshot := self.latest.get(uid):
            try:
                return await self.hass.async_add_executor_job(snapshot.file_path.read_bytes)
            except FileNotFoundError:
                replacement = await self.hass.async_add_executor_job(self._find_latest, uid)
                if self.latest.get(uid) is snapshot:
                    self._async_set_latest(uid, replacement)
            except OSError:
                return None
        return None

    @callback
    def async_schedule(self, uid: str, entity_id: str | None, context: Context | None) -> None:
        if not self.entry.options.get(CONF_SAVE_SNAPSHOTS, False):
            return
        barrier = (self.coordinator.data or {}).get(uid)
        if not barrier or not barrier.camera_url:
            return
        self._sequence += 1
        self.entry.async_create_background_task(
            self.hass,
            self._async_save(
                uid,
                barrier.camera_url,
                entity_id,
                context,
                dt_util.utcnow(),
                self._sequence,
            ),
            f"Vezdehod opening photo {uid}",
        )

    def _write(self, uid: str, content: bytes, created_at: datetime) -> Snapshot:
        directory = self._directory(uid, for_write=True)
        self._check_running()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (dt_util.as_local(created_at).strftime(_LOCAL_FILENAME) + ".jpg")
        with TemporaryDirectory(dir=directory) as staging_dir:
            staged = Path(staging_dir) / target.name
            staged.write_bytes(content)
            return self._snapshot(self._commit_photo(staged, target))

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
            self._async_set_latest(uid, snapshot, context)
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
