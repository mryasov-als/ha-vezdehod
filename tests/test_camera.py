"""Exercise camera viewing and opening snapshots without cloud operations."""

import asyncio
from hashlib import sha256
from os import utime
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.camera import get_camera_from_entity_id
from homeassistant.core import Context
from homeassistant.helpers import entity_registry
from homeassistant.util import dt as dt_util
from test_homeassistant import hass as hass
from test_homeassistant import make_entry
from test_homeassistant import mock_api as mock_api

CAMERA_URL = "https://camera.invalid/101.jpg?camera_token=test-camera-only-secret"


@pytest.fixture(autouse=True)
def snapshot_delay(monkeypatch):
    from custom_components.vezdehod import snapshot as platform

    # Timing is controlled explicitly in the delay/cancellation tests.
    monkeypatch.setattr(platform, "sleep", AsyncMock(), raising=False)
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", dt_util.DEFAULT_TIME_ZONE)


@pytest.fixture
def camera_api(mock_api):
    from custom_components.vezdehod.api import Barrier

    mock_api[0].return_value = {
        "101": Barrier("101", "Тестовый въезд", True, camera_url=CAMERA_URL),
        "102": Barrier("102", "Второй въезд", True, camera_url=CAMERA_URL + "2"),
    }
    return mock_api


async def setup_camera(hass, save_snapshots=False):
    entry = make_entry(save_snapshots=save_snapshots)
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry, hass.states.async_all("camera")[0]


async def press(hass, context=None):
    button = hass.states.async_all("button")[0]
    await hass.services.async_call(
        "button", "press", {"entity_id": button.entity_id}, blocking=True, context=context
    )


async def test_camera_shares_device_and_fetches_only_when_viewed(hass, camera_api, monkeypatch):
    from custom_components.vezdehod import camera as platform

    entry, state = await setup_camera(hass)
    assert state.state != "unavailable"
    registry = entity_registry.async_get(hass)
    button = hass.states.async_all("button")[0]
    assert (
        registry.async_get(state.entity_id).device_id
        == registry.async_get(button.entity_id).device_id
    )
    camera_api[4].assert_not_awaited()
    provider = get_camera_from_entity_id(hass, state.entity_id)
    now = [100.0]
    monkeypatch.setattr(platform, "monotonic", lambda: now[0])
    first, second = await asyncio.gather(
        provider.async_camera_image(), provider.async_camera_image()
    )
    assert first == second == camera_api[4].return_value
    camera_api[4].assert_awaited_once_with(CAMERA_URL)
    now[0] += 1.1
    await provider.async_camera_image()
    assert camera_api[4].await_count == 2
    assert provider.frame_interval == 1.0
    assert "camera_token" not in str(state.attributes)
    camera_api[1].assert_not_awaited()
    camera_api[2].assert_not_awaited()


async def test_camera_error_does_not_block_button_or_change_key_status(hass, camera_api):
    from custom_components.vezdehod.api import CannotConnect

    entry, state = await setup_camera(hass)
    camera_api[4].side_effect = CannotConnect("safe error")
    provider = get_camera_from_entity_id(hass, state.entity_id)
    assert await provider.async_camera_image() is None
    await press(hass)
    camera_api[1].assert_awaited_once_with("101")
    assert entry.runtime_data.key_status == 1


async def test_missing_camera_url_recovers_after_list_refresh(hass, mock_api):
    from custom_components.vezdehod.api import Barrier

    entry, state = await setup_camera(hass)
    assert state.state == "unavailable"
    mock_api[0].return_value = {
        "101": Barrier("101", "Тестовый въезд", True, camera_url=CAMERA_URL)
    }
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(state.entity_id).state != "unavailable"
    mock_api[4].assert_not_awaited()


async def test_selection_change_removes_old_camera_and_image(hass, camera_api):
    entry, state = await setup_camera(hass)
    old_image = hass.states.async_all("image")[0].entity_id
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"barrier_ids": ["102"], "save_snapshots": False}
    )
    await hass.async_block_till_done()
    registry = entity_registry.async_get(hass)
    assert registry.async_get(state.entity_id) is None
    assert registry.async_get(old_image) is None
    assert len(hass.states.async_all("camera")) == 1
    camera_api[1].assert_not_awaited()


async def test_opening_saves_photo_in_event_and_image_without_activity(hass, camera_api):
    from aiohttp.test_utils import TestClient, TestServer

    saved, activity = [], []
    hass.bus.async_listen("vezdehod_snapshot_saved", saved.append)
    hass.bus.async_listen("logbook_entry", activity.append)
    entry, _ = await setup_camera(hass, save_snapshots=None)
    options = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        options["flow_id"], {"barrier_ids": ["101"], "save_snapshots": True}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["save_snapshots"] is True
    user = await hass.auth.async_create_user("Snapshot test", group_ids=["system-admin"])
    context = Context(user_id=user.id)
    await press(hass, context)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(saved) == 1
    data = saved[0].data
    photo = Path(data["file_path"])
    assert photo.is_relative_to(Path(hass.config.path("vezdehod", "snapshots")))
    assert not photo.is_relative_to(Path(hass.config.media_dirs["local"]))
    assert photo.read_bytes() == camera_api[4].return_value
    assert "media_content_id" not in data
    assert data["barrier_id"] == "101"
    assert saved[0].context.id == context.id
    assert activity == []
    image = hass.states.async_all("image")[0]
    assert image.state not in ("unavailable", "unknown")
    assert image.attributes["file_path"] == str(photo)
    assert "camera_token" not in str(image.attributes)
    async with TestClient(TestServer(hass.http.app)) as client:
        image_url = image.attributes["entity_picture"]
        rejected = await client.get(image_url.split("?", 1)[0])
        assert rejected.status == 403
        response = await client.get(image_url)
        assert response.status == 200
        assert await response.read() == photo.read_bytes()
    camera_api[1].assert_awaited_once_with("101")
    before = camera_api[4].await_count
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(image.entity_id).attributes["file_path"] == str(photo)
    assert camera_api[4].await_count == before


async def test_slow_photo_does_not_delay_opening(hass, camera_api):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_image(_url):
        entered.set()
        await release.wait()
        return b"\xff\xd8\xff\xe0test-frame\xff\xd9"

    camera_api[4].side_effect = delayed_image
    await setup_camera(hass, save_snapshots=True)
    try:
        await asyncio.wait_for(press(hass), timeout=0.5)
        await entered.wait()
        camera_api[1].assert_awaited_once_with("101")
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_failed_photo_does_not_fail_successful_opening(hass, camera_api):
    from custom_components.vezdehod.api import CannotConnect

    camera_api[4].side_effect = CannotConnect("safe error")
    activity = []
    hass.bus.async_listen("logbook_entry", activity.append)
    await setup_camera(hass, save_snapshots=True)
    await press(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    camera_api[1].assert_awaited_once_with("101")
    assert not list(Path(hass.config.path("vezdehod", "snapshots")).rglob("*.jpg"))
    assert activity == []


async def test_photo_waits_three_seconds_but_opening_is_immediate(hass, camera_api, monkeypatch):
    from custom_components.vezdehod import snapshot as platform

    release = asyncio.Event()
    delays = []

    async def wait(delay):
        delays.append(delay)
        await release.wait()

    monkeypatch.setattr(platform, "sleep", wait, raising=False)
    await setup_camera(hass, save_snapshots=True)
    try:
        await asyncio.wait_for(press(hass), timeout=0.5)
        camera_api[1].assert_awaited_once_with("101")
        assert delays == [3.0]
        camera_api[4].assert_not_awaited()
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    camera_api[4].assert_awaited_once_with(CAMERA_URL)
    assert hass.states.async_all("image")[0].state not in ("unknown", "unavailable")


async def test_unload_cancels_pending_photo_before_camera_request(hass, camera_api, monkeypatch):
    from custom_components.vezdehod import snapshot as platform

    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait(delay):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(platform, "sleep", wait, raising=False)
    entry, _ = await setup_camera(hass, save_snapshots=True)
    try:
        await press(hass)
        assert entered.is_set()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cancelled.is_set()
        camera_api[4].assert_not_awaited()
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize(
    ("time_zone", "expected_name"),
    [
        ("Europe/Moscow", "2026-11-05_13-40-00_123456+0300.jpg"),
        ("America/New_York", "2026-11-05_05-40-00_123456-0500.jpg"),
        ("Asia/Kolkata", "2026-11-05_16-10-00_123456+0530.jpg"),
    ],
)
async def test_photo_filename_uses_configured_ha_timezone(
    hass, camera_api, time_zone, expected_name
):
    await hass.config.async_set_time_zone(time_zone)
    entry, _ = await setup_camera(hass, save_snapshots=True)
    pressed_at = dt_util.parse_datetime("2026-11-05T10:40:00.123456+00:00")
    snapshot = await hass.async_add_executor_job(
        entry.runtime_data.snapshots._write, "101", camera_api[4].return_value, pressed_at
    )
    assert snapshot.file_path.name == expected_name
    assert snapshot.created_at == pressed_at
    assert snapshot.file_path.read_bytes() == camera_api[4].return_value
    camera_api[1].assert_not_awaited()


@pytest.mark.parametrize(
    ("filenames", "expected_name", "expected_time"),
    [
        (
            ["20261105T104000_123456Z.jpg"],
            "20261105T104000_123456Z.jpg",
            "2026-11-05T10:40:00.123456+00:00",
        ),
        (
            [
                "20261105T130000_000000Z.jpg",
                "2026-11-05_15-00-00_000000+0300.jpg",
                "2026-11-05_09-00-00_000000-0500.jpg",
            ],
            "2026-11-05_09-00-00_000000-0500.jpg",
            "2026-11-05T14:00:00+00:00",
        ),
        (
            [
                "20261101T060000_000000Z.jpg",
                "2026-11-01_01-50-00_000000-0400.jpg",
                "2026-11-01_01-10-00_000000-0500.jpg",
            ],
            "2026-11-01_01-10-00_000000-0500.jpg",
            "2026-11-01T06:10:00+00:00",
        ),
    ],
)
async def test_migration_restores_latest_photo_from_legacy_media(
    hass, camera_api, filenames, expected_name, expected_time
):
    entry, _ = await setup_camera(hass, save_snapshots=None)
    account = entry.runtime_data.snapshots.root.name
    directory = Path(hass.config.media_dirs["local"]) / "vezdehod" / account / "101"
    directory.mkdir(parents=True)
    for name in [*filenames, "invalid-name.jpg"]:
        (directory / name).write_bytes(camera_api[4].return_value)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    snapshot = entry.runtime_data.snapshots.latest["101"]
    archive = Path(hass.config.path("vezdehod", "snapshots", account, "101"))
    assert {path.name for path in archive.glob("*.jpg")} == set(filenames)
    assert list(directory.glob("*.jpg")) == [directory / "invalid-name.jpg"]
    assert snapshot.file_path.parent == archive
    assert snapshot.file_path.name == expected_name
    assert snapshot.created_at == dt_util.parse_datetime(expected_time)
    assert hass.states.async_all("image")[0].attributes["file_path"] == str(snapshot.file_path)
    camera_api[4].assert_not_awaited()


@pytest.mark.parametrize("media_id", ["local", "previous", "linked"])
async def test_legacy_photo_is_preserved_until_migration_can_complete(
    hass, camera_api, monkeypatch, media_id
):
    hass.config.media_dirs["previous"] = hass.config.path("previous_media")
    hass.config.media_dirs["linked"] = hass.config.path("linked_media")
    entry, _ = await setup_camera(hass, save_snapshots=None)
    account = entry.runtime_data.snapshots.root.name
    filename = "20261105T104000_123456Z.jpg"
    legacy_account = Path(hass.config.media_dirs[media_id]) / "vezdehod" / account
    if media_id == "linked":
        linked_archive = Path(hass.config.path("old_photo_archive"))
        linked_archive.mkdir()
        legacy_account.parent.mkdir(parents=True)
        legacy_account.symlink_to(linked_archive, target_is_directory=True)
    source = legacy_account / "102" / filename
    target = Path(hass.config.path("vezdehod", "snapshots", account, "102", filename))
    source.parent.mkdir(parents=True)
    source.write_bytes(camera_api[4].return_value)
    original_unlink = Path.unlink

    def fail_source_unlink(path, *args, **kwargs):
        if path == source:
            raise PermissionError("Legacy archive is read-only")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_source_unlink)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert source.read_bytes() == target.read_bytes() == camera_api[4].return_value
        assert source.stat().st_mtime_ns == target.stat().st_mtime_ns

    saved_at = source.stat().st_mtime_ns
    utime(target, (0, 0))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not source.exists()
    assert list(target.parent.glob("*.jpg")) == [target]
    assert target.read_bytes() == camera_api[4].return_value
    assert target.stat().st_mtime_ns == saved_at
    camera_api[4].assert_not_awaited()


async def test_photo_checkbox_can_be_disabled_when_service_is_unreachable(hass, camera_api):
    from custom_components.vezdehod.api import CannotConnect

    entry, _ = await setup_camera(hass, save_snapshots=True)
    camera_api[0].side_effect = CannotConnect("safe error")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"barrier_ids": ["101"], "save_snapshots": False}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["save_snapshots"] is False
    await press(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    camera_api[4].assert_not_awaited()


async def test_unreadable_photo_directory_does_not_prevent_button_setup(
    hass, camera_api, monkeypatch
):
    original_glob = Path.glob
    archive = Path(hass.config.path("vezdehod", "snapshots"))

    def guarded_glob(path, *args, **kwargs):
        if path.is_relative_to(archive):
            raise PermissionError("Test storage is unavailable")
        return original_glob(path, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", guarded_glob)
    await setup_camera(hass)
    await press(hass)
    camera_api[1].assert_awaited_once_with("101")


async def test_slow_migration_leaves_button_and_existing_photo_available(
    hass, camera_api, monkeypatch
):
    from custom_components.vezdehod.snapshot import VezdehodSnapshots

    entered, release = asyncio.Event(), Event()
    entry = make_entry(save_snapshots=False)
    account = sha256(entry.unique_id.encode()).hexdigest()[:16]
    photo = Path(
        hass.config.path("vezdehod", "snapshots", account, "101", "20261105T104000_123456Z.jpg")
    )
    photo.parent.mkdir(parents=True)
    photo.write_bytes(camera_api[4].return_value)

    def slow_migration(_snapshots, _root):
        hass.loop.call_soon_threadsafe(entered.set)
        release.wait(5)

    monkeypatch.setattr(VezdehodSnapshots, "_migrate_legacy", slow_migration)
    try:
        await asyncio.wait_for(hass.config_entries.async_add(entry), timeout=2)
        await asyncio.wait_for(entered.wait(), timeout=2)
        await hass.async_block_till_done()
        await press(hass)
        camera_api[1].assert_awaited_once_with("101")
        assert hass.states.async_all("image")[0].attributes["file_path"] == str(photo)
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("former_default", [False, True])
async def test_migration_preserves_both_frames_with_the_same_filename(
    hass, camera_api, monkeypatch, former_default
):
    from custom_components.vezdehod import snapshot as platform

    entry, _ = await setup_camera(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    root = entry.runtime_data.snapshots.root
    media = Path(hass.config.media_dirs["local"])
    if former_default:
        media = Path(hass.config.path("former_default_media"))
        monkeypatch.setattr(platform, "_DEFAULT_MEDIA_ROOT", media, raising=False)
    filename = "20261105T104000_123456Z.jpg"
    source = media / "vezdehod" / root.name / "101" / filename
    target = root / "101" / filename
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"old photo")
    target.write_bytes(b"new photo")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not source.exists()
    assert {file.read_bytes() for file in target.parent.glob("*.jpg")} == {
        b"old photo",
        b"new photo",
    }
    assert entry.runtime_data.snapshots.latest["101"].created_at == dt_util.parse_datetime(
        "2026-11-05T10:40:00.123456+00:00"
    )


async def test_image_recovers_when_the_current_photo_is_removed(hass, camera_api):
    from aiohttp.test_utils import TestClient, TestServer

    entry, _ = await setup_camera(hass)
    directory = entry.runtime_data.snapshots.root / "101"
    directory.mkdir(parents=True, exist_ok=True)
    older = directory / "20261105T104000_123456Z.jpg"
    latest = directory / "20261105T104001_123456Z.jpg"
    older.write_bytes(b"old photo")
    latest.write_bytes(b"new photo")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    image = hass.states.async_all("image")[0]
    assert image.attributes["file_path"] == str(latest)
    latest.unlink()
    async with TestClient(TestServer(hass.http.app)) as client:
        response = await client.get(image.attributes["entity_picture"])
        assert response.status == 200
        assert await response.read() == older.read_bytes()
        await hass.async_block_till_done()
        state = hass.states.get(image.entity_id)
        assert state.attributes["file_path"] == str(older)
        older.unlink()
        response = await client.get(state.attributes["entity_picture"])
        assert response.status != 200
        await hass.async_block_till_done()
        assert hass.states.get(image.entity_id).state == "unavailable"


async def test_standard_ha_camera_proxy_serves_the_jpeg(hass, camera_api):
    from aiohttp.test_utils import TestClient, TestServer

    _, state = await setup_camera(hass)
    provider = get_camera_from_entity_id(hass, state.entity_id)
    async with TestClient(TestServer(hass.http.app)) as client:
        response = await client.get(
            f"/api/camera_proxy/{state.entity_id}?token={provider.access_tokens[-1]}"
        )
        assert response.status == 200
        assert response.content_type == "image/jpeg"
        assert await response.read() == camera_api[4].return_value
    camera_api[1].assert_not_awaited()


async def test_standard_ha_mjpeg_stream_refreshes_frames(hass, camera_api):
    from aiohttp.test_utils import TestClient, TestServer

    first = camera_api[4].return_value
    second = first.replace(b"test-frame", b"next-frame")
    frames = iter((first, second))

    async def next_frame(_url):
        return next(frames, second)

    camera_api[4].side_effect = next_frame
    _, state = await setup_camera(hass)
    provider = get_camera_from_entity_id(hass, state.entity_id)
    async with TestClient(TestServer(hass.http.app)) as client:
        response = await client.get(
            f"/api/camera_proxy_stream/{state.entity_id}?token={provider.access_tokens[-1]}"
        )
        assert response.status == 200
        assert response.content_type == "multipart/x-mixed-replace"
        async with asyncio.timeout(3):
            await response.content.readuntil(first)
            await response.content.readuntil(second)
        response.close()
    assert camera_api[4].await_count >= 2
    camera_api[1].assert_not_awaited()


async def test_opening_photo_is_disabled_by_default(hass, camera_api):
    await setup_camera(hass, save_snapshots=None)
    await press(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    camera_api[1].assert_awaited_once_with("101")
    camera_api[4].assert_not_awaited()
    assert hass.states.async_all("image")[0].state == "unavailable"


@pytest.mark.parametrize(("saved", "expected"), [(None, False), (True, True), (False, False)])
async def test_photo_checkbox_uses_saved_setting_or_defaults_to_off(
    hass, camera_api, saved, expected
):
    entry, _ = await setup_camera(hass, save_snapshots=saved)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({"barrier_ids": ["101"]})["save_snapshots"] is expected


async def test_photo_write_error_does_not_fail_opening(hass, camera_api, monkeypatch):
    original_write = Path.write_bytes
    archive = Path(hass.config.path("vezdehod", "snapshots"))

    def guarded_write(path, content):
        if path.is_relative_to(archive):
            raise PermissionError("Test storage is read-only")
        return original_write(path, content)

    monkeypatch.setattr(Path, "write_bytes", guarded_write)
    await setup_camera(hass, save_snapshots=True)
    await press(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    camera_api[1].assert_awaited_once_with("101")
    assert hass.states.async_all("image")[0].state == "unavailable"


async def test_photo_is_not_written_when_archive_overlaps_configured_media(hass, camera_api):
    shared = Path(hass.config.path("vezdehod", "snapshots"))
    hass.config.media_dirs["shared"] = str(shared)
    await setup_camera(hass, save_snapshots=True)
    await press(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    camera_api[1].assert_awaited_once_with("101")
    assert not list(shared.rglob("*.jpg"))
    assert hass.states.async_all("image")[0].state == "unavailable"


async def test_earlier_slow_photo_cannot_replace_later_opening(hass, camera_api):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = [0]

    async def delayed_first(_url):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            await release.wait()
            return b"old-opening"
        return b"new-opening"

    camera_api[4].side_effect = delayed_first
    entry, _ = await setup_camera(hass, save_snapshots=True)
    snapshots = entry.runtime_data.snapshots
    button = hass.states.async_all("button")[0]
    snapshots.async_schedule("101", button.entity_id, Context())
    await entered.wait()
    snapshots.async_schedule("101", button.entity_id, Context())
    try:
        # Wait for the second task only; the first is intentionally still blocked.
        async with asyncio.timeout(2):
            while "101" not in snapshots.latest:
                await asyncio.sleep(0)
        latest_path = snapshots.latest["101"].file_path
        assert latest_path.read_bytes() == b"new-opening"
    finally:
        release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert snapshots.latest["101"].file_path == latest_path
    assert len(list(snapshots.root.rglob("*.jpg"))) == 2
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.snapshots.latest["101"].file_path == latest_path
