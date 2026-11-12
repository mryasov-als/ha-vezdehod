"""Build a self-contained Windows GUI EXE with bundled ADB using Windows C#."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
CSC = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll", "NOTICE.txt", "source.properties")


def run(args, **kwargs):
    result = subprocess.run([str(arg) for arg in args], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    if result.stdout.strip():
        print(result.stdout.strip())
    return result


def compile_to(path, files, extra=(), gui=False):
    run([CSC, "/nologo", "/utf8output", "/optimize+", "/target:" + ("winexe" if gui else "exe"),
         "/out:" + str(path), *extra, *(ROOT / file for file in files)])


def build(sdk):
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    bundle = BUILD / "adb.zip"
    revision = re.search(r"^Pkg.Revision=([0-9.]+)$", (sdk / "source.properties").read_text(encoding="utf-8"), re.M).group(1)
    hashes = {}
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in ADB_FILES:
            content = (sdk / name).read_bytes()
            hashes[name] = hashlib.sha256(content).hexdigest()
            item = zipfile.ZipInfo(name, date_time=(2026, 11, 6, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, content)
    archive_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    entries = ",\n".join('            {"' + name + '", "' + value + '"}' for name, value in hashes.items())
    (BUILD / "BundleInfo.cs").write_text(
        'using System.Collections.Generic;\nnamespace VezdehodKeyHelper { internal static class BundleInfo {\n'
        'public const string ArchiveHash = "' + archive_hash + '";\n'
        'public const string AdbRevision = "' + revision + '";\n'
        'public static readonly Dictionary<string,string> Files = new Dictionary<string,string> {\n' +
        entries + '\n};\n}}\n', encoding="utf-8")
    refs = ["/r:System.Core.dll", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
            "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll"]
    core = ["src/Core.cs", "src/ProcessRunner.cs", "src/PrivateAdb.cs"]
    compile_to(BUILD / "CoreTests.exe", [*core, "tests/CoreTests.cs"])
    core_tests = run([BUILD / "CoreTests.exe"], timeout=10).stdout.strip()
    compile_to(BUILD / "KeyHelperTestChild.exe", ["tests/Child.cs"])
    compile_to(BUILD / "InfrastructureTests.exe", [*core, "tests/InfrastructureTests.cs"])
    infrastructure_tests = run([BUILD / "InfrastructureTests.exe", BUILD / "KeyHelperTestChild.exe"],
                               timeout=15).stdout.strip()

    logo = ROOT.parents[1] / "custom_components/vezdehod/brand/icon.png"
    png = logo.read_bytes()
    width, height = struct.unpack(">II", png[16:24])
    icon = BUILD / "app.ico"
    icon.write_bytes(struct.pack("<HHH", 0, 1, 1) +
                     struct.pack("<BBBBHHII", width if width < 256 else 0, height if height < 256 else 0,
                                 0, 0, 1, 32, len(png), 22) + png)
    exe = DIST / "vezdehod-key-helper.exe"
    compile_to(exe, [*core, "src/BundledAdb.cs", "src/Program.cs", BUILD / "BundleInfo.cs"],
               [*refs, "/resource:" + str(bundle) + ",Vezdehod.Adb.zip",
                "/resource:" + str(logo) + ",Vezdehod.Icon.png", "/win32icon:" + str(icon)], gui=True)
    probe = BUILD / "self-check.txt"
    run([exe, "--self-check", probe], timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert "ok=true" in probe.read_text(encoding="utf-8-sig")
    preview = ROOT / "preview.png"
    run([exe, "--preview", preview], timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert preview.read_bytes().startswith(b"\x89PNG")
    info = {"version": "0.1.2", "file": exe.name, "bytes": exe.stat().st_size,
            "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "adb_revision": revision,
            "adb_files": hashes, "core_tests": core_tests, "infrastructure_tests": infrastructure_tests,
            "self_check": "passed; embedded ADB version only, no devices queried",
            "ui_preview": "preview.png", "real_phone_capture_tested": False}
    (ROOT / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb-dir", type=Path,
                        default=Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk/platform-tools")
    build(parser.parse_args().adb_dir)
