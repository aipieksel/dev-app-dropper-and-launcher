"""Build a Dock-visible macOS launcher for a discovered folder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import zlib

from business.launcher_runtime import ContractError, directory_writable, discover, resolve_destinations

RUNTIME_SOURCE = Path(__file__).resolve().with_name("launcher_runtime.py")
EXECUTABLE_NAME = "Launcher"
ICON_CANDIDATES = (
    "icon.icns",
    "icon.png",
    "icon.jpg",
    "icon.jpeg",
    "favicon.png",
    "favicon.svg",
    "favicon.ico",
    "public/favicon.png",
    "public/favicon.svg",
    "public/icon.png",
    "assets/icon.png",
)


def create_launcher(folder: Path, home: Path | None = None, applications: Path | None = None) -> dict:
    try:
        contract = discover(folder)
    except ContractError as error:
        return {"ok": False, "error": str(error)}
    destinations = resolve_destinations(home, applications)
    try:
        _require_tools()
        binary = ensure_launcher_binary()
        filename = _filename(contract, destinations)
        with tempfile.TemporaryDirectory(prefix="app-dropper-") as temporary:
            staged = _stage(Path(temporary), contract, binary, filename)
            _run(["/usr/bin/plutil", "-lint", str(staged / "Contents/Info.plist")])
            _run(["/usr/bin/codesign", "--force", "--sign", "-", str(staged)])
            _run(["/usr/bin/codesign", "--verify", "--strict", str(staged)])
            dropper_app = _publish(staged, destinations["dropper"] / filename)
            applications_app = _publish(staged, destinations["applications"] / filename)
    except (OSError, ContractError) as error:
        return {"ok": False, "error": f"Could not create the Mac app: {error}"}
    message = f"Created {contract['name']}. It is in {dropper_app.parent} and in the Apps folder at {applications_app.parent}."
    if contract.get("inferred"):
        message += f" Detected {contract['inferred']} and wrote a start file in the project."
    if destinations["applications_fallback"]:
        requested = destinations["applications_requested"]
        message += f" {requested} is not writable, so this copy is in {applications_app.parent}."
    return {
        "ok": True,
        "error": None,
        "name": contract["name"],
        "folder": contract["folder"],
        "web": contract["web"],
        "port": contract["port"],
        "command": contract["argv"],
        "bundle_id": contract["bundle_id"],
        "dropper_app": str(dropper_app),
        "applications_app": str(applications_app),
        "applications_fallback": destinations["applications_fallback"],
        "message": message,
    }


def app_root() -> Path:
    """Folder that contains business/ and entry/ in a checkout or in the Mac shell."""
    return Path(__file__).resolve().parents[1]


def swift_source() -> Path:
    return app_root() / "entry" / "macos" / "FolderLauncher.swift"


def cache_dir() -> Path:
    """Writable compile cache. A packaged app must not write inside its own bundle."""
    here = Path(__file__).resolve()
    checkout = here.parents[2]
    checkout_copy = checkout / "app" / "business" / "bundle.py"
    if (checkout / "scripts" / "run.sh").is_file() and checkout_copy.is_file() and checkout_copy.resolve() == here:
        return checkout / "build" / "cache"
    return Path.home() / "Library" / "Caches" / "App Dropper"


SWIFT_SOURCE = swift_source()


def _run(args):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ContractError(detail or "Command failed: " + " ".join(str(arg) for arg in args))
    return result


def ensure_launcher_binary() -> Path:
    source = swift_source()
    if not source.is_file():
        raise ContractError(f"Launcher source is missing: {source}")
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "FolderLauncher"
    if binary.is_file() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    module_cache = cache / "swift-cache"
    module_cache.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "xcrun", "swiftc",
            "-module-cache-path", str(module_cache),
            "-O",
            "-framework", "Cocoa",
            "-framework", "Foundation",
            str(source),
            "-o", str(binary),
        ]
    )
    binary.chmod(0o755)
    return binary


def _require_tools() -> None:
    missing = []
    for name in ("iconutil", "sips", "codesign", "plutil"):
        if shutil.which(name) is None and not Path(f"/usr/bin/{name}").exists():
            missing.append(name)
    if shutil.which("swiftc") is None:
        try:
            subprocess.run(["xcrun", "--find", "swiftc"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            missing.append("swiftc")
    if missing:
        raise ContractError("Missing Mac build tools: " + ", ".join(missing))


def _stage(temporary: Path, contract: dict, binary: Path, filename: str) -> Path:
    app = temporary / filename
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(binary, macos / EXECUTABLE_NAME)
    (macos / EXECUTABLE_NAME).chmod(0o755)
    shutil.copy2(RUNTIME_SOURCE, resources / "launcher_runtime.py")
    (resources / "launch-contract.json").write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    (resources / "repository-path").write_text(contract["folder"] + "\n", encoding="utf-8")
    _install_icon(Path(contract["folder"]), resources, temporary, contract["name"])
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleName": contract["name"],
        "CFBundleDisplayName": contract["name"],
        "CFBundleIdentifier": contract["bundle_id"],
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIconFile": "AppIcon",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "LSMinimumSystemVersion": "12.0",
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    return app


def _publish(staged: Path, destination: Path) -> Path:
    if destination.is_symlink():
        raise ContractError(f"Refusing a symlink destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not directory_writable(destination.parent):
        raise ContractError(f"Cannot write {destination.parent}")
    if destination.exists():
        shutil.rmtree(destination)
    _run(["/usr/bin/ditto", str(staged), str(destination)])
    _run(["/usr/bin/codesign", "--force", "--sign", "-", str(destination)])
    _run(["/usr/bin/codesign", "--verify", "--strict", str(destination)])
    return destination


def _filename(contract: dict, destinations: dict) -> str:
    base = _safe_name(contract["name"])
    filename = base + ".app"
    digest = hashlib.sha256(contract["folder"].encode()).hexdigest()[:8]
    for directory in (destinations["dropper"], destinations["applications"]):
        current = directory / filename
        other = _existing_folder(current)
        if other and other != contract["folder"]:
            return f"{base} {digest}.app"
    return filename


def _existing_folder(app_path: Path) -> str | None:
    contract_path = app_path / "Contents/Resources/launch-contract.json"
    if not contract_path.is_file():
        return None
    try:
        data = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    folder = data.get("folder") if isinstance(data, dict) else None
    return str(folder) if folder else None


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f/:]+", " ", name).strip(" .")
    return cleaned[:60] or "Launcher"


def _install_icon(folder: Path, resources: Path, scratch: Path, name: str) -> None:
    source = _find_icon(folder)
    if source is None:
        source = scratch / "generated-icon.png"
        _write_png(source, 256, _color(name))
    _make_icns(source, resources / "AppIcon.icns", scratch)


def _find_icon(folder: Path) -> Path | None:
    manifest = folder / "app-dropper.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("icon"), str):
            candidate = Path(data["icon"])
            if not candidate.is_absolute():
                candidate = folder / candidate
            if candidate.is_file():
                return candidate
    for relative in ICON_CANDIDATES:
        candidate = folder / relative
        if candidate.is_file():
            return candidate
    return None


def _make_icns(source: Path, target: Path, scratch: Path) -> None:
    if source.suffix.lower() == ".icns":
        shutil.copy2(source, target)
        return
    iconset = scratch / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    raster = source
    if source.suffix.lower() == ".svg":
        renderer = shutil.which("rsvg-convert")
        if not renderer:
            raster = scratch / "generated-icon.png"
            _write_png(raster, 256, (16, 163, 127))
        else:
            raster = scratch / "svg-icon.png"
            _run([renderer, "-w", "1024", "-h", "1024", str(source), "-o", str(raster)])
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            suffix = "@2x" if scale == 2 else ""
            image = iconset / f"icon_{size}x{size}{suffix}.png"
            _run(["/usr/bin/sips", "-s", "format", "png", "-z", str(pixels), str(pixels), str(raster), "--out", str(image)])
    _run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(target)])


def _color(name: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(name.encode()).digest()
    return (40 + digest[0] % 160, 70 + digest[1] % 140, 80 + digest[2] % 140)


def _write_png(path: Path, size: int, rgb: tuple[int, int, int]) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * size
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)
