"""Drop history for the App Dropper shell."""

import json
from pathlib import Path

from business.bundle import create_launcher
from business.launcher_runtime import resolve_destinations


class App:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = []
        saved = self.root / "jobs.json"
        if saved.is_file():
            try:
                data = json.loads(saved.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = []
            if isinstance(data, list):
                self.jobs = [item for item in data if isinstance(item, dict)][:30]

    def public_state(self):
        destinations = resolve_destinations()
        return {
            "dropper_dir": str(destinations["dropper"]),
            "applications_dir": str(destinations["applications"]),
            "applications_fallback": destinations["applications_fallback"],
            "jobs": list(self.jobs),
        }

    def create_from_folder(self, path):
        text = str(path or "").strip()
        if not text:
            raise ValueError("Choose a folder.")
        folder = Path(text).expanduser()
        if not folder.is_dir():
            raise ValueError("Drop a folder. That path is not a directory.")
        result = create_launcher(folder)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "Could not create the Mac app.")
        job = {
            "name": result["name"],
            "folder": result["folder"],
            "dropper_app": result["dropper_app"],
            "applications_app": result["applications_app"],
            "applications_fallback": result["applications_fallback"],
            "message": result["message"],
            "web": result["web"],
            "command": result["command"],
        }
        self.jobs.insert(0, job)
        self.jobs = self.jobs[:30]
        (self.root / "jobs.json").write_text(json.dumps(self.jobs), encoding="utf-8")
        return job

    def open_folder(self, which):
        destinations = resolve_destinations()
        if which == "dropper":
            target = destinations["dropper"]
        elif which == "applications":
            target = destinations["applications"]
        else:
            raise ValueError("Unknown folder.")
        target.mkdir(parents=True, exist_ok=True)
        import subprocess

        completed = subprocess.run(["/usr/bin/open", str(target)], capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "Could not open that folder.")
        return {"path": str(target)}
