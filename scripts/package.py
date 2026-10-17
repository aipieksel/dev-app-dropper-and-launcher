from pathlib import Path
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP_NAME = "Dev App Dropper and Launcher"
BINARY = ROOT / "build" / "cache" / "shell-binary"
DIST.mkdir(exist_ok=True)
build = Path(tempfile.mkdtemp(prefix="bundle-", dir=DIST))
bundle = build / f"{APP_NAME}.app"
contents = bundle / "Contents"
macos = contents / "MacOS"
resources = contents / "Resources"
macos.mkdir(parents=True)
resources.mkdir()
shutil.copy2(BINARY, macos / APP_NAME)
os_chmod = (macos / APP_NAME).stat().st_mode
(macos / APP_NAME).chmod(os_chmod | 0o111)
shutil.copytree(ROOT / "app", resources / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
(resources / "runtime.json").write_text(json.dumps({"python": sys.executable}))
info = {
    "CFBundleDevelopmentRegion": "en",
    "CFBundleExecutable": APP_NAME,
    "CFBundleIdentifier": "com.aipieksel.dev-app-dropper",
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": APP_NAME,
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0.0",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "14.0",
    "NSPrincipalClass": "NSApplication",
    "NSHighResolutionCapable": True,
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    "CFBundleDocumentTypes": [
        {
            "CFBundleTypeName": "Folder",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["public.folder"],
        }
    ],
}
with (contents / "Info.plist").open("wb") as handle:
    plistlib.dump(info, handle)
for target in [macos / APP_NAME, bundle]:
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(target)], check=True)


def publish(destination: Path):
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/ditto", str(bundle), str(destination)], check=True)


for stale in (DIST / "Converto.app", ROOT / "build" / "Converto", ROOT / "build" / APP_NAME, ROOT / "build" / "FolderLauncher"):
    if stale.is_dir():
        shutil.rmtree(stale)
    elif stale.is_file():
        stale.unlink()
publish(DIST / bundle.name)
publish(ROOT / "build" / bundle.name)
shutil.rmtree(build)
print(DIST / bundle.name)
