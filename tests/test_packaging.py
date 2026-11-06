"""Ensure installed archives include local brand images, without account data."""

import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_translations_do_not_contain_lost_encoding_markers():
    component = ROOT / "custom_components/vezdehod"
    paths = [component / "strings.json", *(component / "translations").glob("*.json")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        assert "??" not in text, f"Translation contains lost Unicode characters: {path.name}"


def test_archives_include_real_brand_image_and_sensor(tmp_path):
    subprocess.run(
        [sys.executable, str(ROOT / "tools/build.py"), "--output-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    version = json.loads(
        (ROOT / "custom_components/vezdehod/manifest.json").read_text(encoding="utf-8")
    )["version"]
    expected_icon = (ROOT / "assets/vezdehod-original-180.png").read_bytes()
    with zipfile.ZipFile(tmp_path / f"vezdehod-{version}.zip") as archive:
        assert archive.read("custom_components/vezdehod/brand/icon.png") == expected_icon
        assert "custom_components/vezdehod/sensor.py" in archive.namelist()
        assert not any(".private" in name for name in archive.namelist())
    with tarfile.open(tmp_path / f"vezdehod-{version}.tar.gz") as archive:
        assert archive.extractfile("vezdehod/brand/icon.png").read() == expected_icon
        assert "vezdehod/sensor.py" in archive.getnames()
