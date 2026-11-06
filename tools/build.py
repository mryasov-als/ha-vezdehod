"""Build installation archives containing only the custom component."""

import argparse
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "vezdehod"
DIST = ROOT / "dist"


def build(output_dir: Path = DIST):
    files = sorted(
        p for p in COMPONENT.rglob("*") if p.is_file() and p.suffix in (".py", ".json", ".png")
    )
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    private_file = ROOT / ".private" / "account.json"
    private = json.loads(private_file.read_text(encoding="utf-8")) if private_file.exists() else {}
    secrets = [
        str(private[key]).encode() for key in ("auth_phone", "mobile_key") if private.get(key)
    ]
    for path in files:
        raw = path.read_bytes()
        if path.suffix in (".py", ".json"):
            raw.decode("utf-8")
        if any(secret in raw for secret in secrets):
            raise RuntimeError("Private credentials found in component file: " + path.name)
        if path.suffix == ".json":
            json.loads(raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / ("vezdehod-" + version + ".zip")
    tar_path = output_dir / ("vezdehod-" + version + ".tar.gz")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    with tarfile.open(tar_path, "w:gz") as archive:
        for path in files:
            archive.add(
                path, arcname=path.relative_to(COMPONENT.parent).as_posix(), recursive=False
            )
    with zipfile.ZipFile(zip_path) as archive:
        assert set(archive.namelist()) == {path.relative_to(ROOT).as_posix() for path in files}
        assert all(name.startswith("custom_components/vezdehod/") for name in archive.namelist())
    result = {
        "version": version,
        "component_files": len(files),
        "credentials_scan": "clean",
        "archives": [
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in (zip_path, tar_path)
        ],
    }
    (output_dir / "build-info.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DIST)
    build(parser.parse_args().output_dir)
