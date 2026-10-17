import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from business.bundle import SWIFT_SOURCE, create_launcher, ensure_launcher_binary
from business.launcher_runtime import (
    ContractError,
    decide_port,
    discover,
    identity_matches,
    launch,
    listening_pids,
    port_in_use,
)
from business.session import App
from entry.server import serve


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def stop_pid(pid):
    if not pid:
        return
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        if group == os.getpgrp():
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 3
    while time.time() < deadline and _alive(pid):
        time.sleep(0.05)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def field(text, name):
    prefix = name + ":"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def evidence_file(name):
    root = os.environ.get("APP_DROPPER_EVIDENCE_DIR")
    if not root:
        return None
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_evidence(name, text):
    path = evidence_file(name)
    if path is not None:
        path.write_text(text, encoding="utf-8")


class TrackedCase(unittest.TestCase):
    def setUp(self):
        self.pids = []
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc.stdout:
                proc.stdout.close()
        for pid in self.pids:
            stop_pid(pid)

    def track_pid(self, pid):
        if pid:
            self.pids.append(int(pid))

    def track_proc(self, proc):
        self.procs.append(proc)


def write_web_fixture(folder: Path, port: int, identity: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    server = folder / "server.py"
    server.write_text(
        "import json\n"
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        f"BODY = {body!r}\n"
        f"IDENTITY = {identity!r}\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        path = self.path.split('?', 1)[0]\n"
        "        if path == '/api/health':\n"
        "            payload = json.dumps({'app': IDENTITY}).encode()\n"
        "            kind = 'application/json'\n"
        "        else:\n"
        "            payload = BODY.encode()\n"
        "            kind = 'text/plain; charset=utf-8'\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', kind)\n"
        "        self.send_header('Content-Length', str(len(payload)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(payload)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "class Server(ThreadingHTTPServer):\n"
        "    allow_reuse_address = True\n"
        "    daemon_threads = True\n"
        "Server(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    script = folder / "start.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        'cd "$(dirname "$0")"\n'
        f'export PORT="${{PORT:-{port}}}"\n'
        'exec /usr/bin/python3 "$PWD/server.py"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    (folder / "app-dropper.json").write_text(
        json.dumps(
            {
                "name": "Fixture Demo",
                "command": ["./start.sh"],
                "port": port,
                "port_env": "PORT",
                "health_path": "/api/health",
                "identity": identity,
                "web": True,
                "url_path": "/",
            }
        ),
        encoding="utf-8",
    )
    return script


def wait_for_identity(port, identity, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if identity_matches(port, "/api/health", identity):
            return True
        time.sleep(0.05)
    return False


def fetch(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return response.read().decode()


class DiscoveryTests(unittest.TestCase):
    def test_package_manager_and_written_port_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"dev": "vite --port 4173"}}),
                encoding="utf-8",
            )
            (folder / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
            contract = discover(folder)
            self.assertEqual(contract["argv"][:3], ["pnpm", "run", "dev"])
            self.assertEqual(contract["port"], 4173)
            self.assertTrue(contract["web"])
            self.assertNotEqual(contract["port"], 3000)

    def test_node_app_without_a_written_port_is_not_given_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "package.json").write_text(
                json.dumps({"scripts": {"start": "node server.js"}}),
                encoding="utf-8",
            )
            contract = discover(folder)
            self.assertFalse(contract["web"])
            self.assertIsNone(contract["port"])

    def test_shell_script_port_comes_from_the_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "dev.sh"
            script.write_text("#!/bin/bash\nPORT=4321\nexec ./server\n", encoding="utf-8")
            contract = discover(folder)
            self.assertEqual(contract["port"], 4321)
            self.assertIn(str(script.resolve()), contract["argv"])

    def test_folder_without_a_start_contract_fails_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            apps = Path(tmp) / "Applications"
            home.mkdir()
            apps.mkdir()
            result = create_launcher(Path(tmp) / "empty", home=home, applications=apps)
            Path(tmp, "empty").mkdir()
            result = create_launcher(Path(tmp) / "empty", home=home, applications=apps)
            self.assertFalse(result["ok"])
            self.assertIn("could not tell", result["error"].lower())
            self.assertEqual(list(home.rglob("*.app")), [])
            self.assertEqual(list(apps.rglob("*.app")), [])

    def test_marked_web_app_without_a_port_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "start.sh"
            script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
            script.chmod(0o755)
            (folder / "app-dropper.json").write_text(
                json.dumps({"command": ["./start.sh"], "web": True}),
                encoding="utf-8",
            )
            with self.assertRaises(ContractError) as caught:
                discover(folder)
            self.assertIn("port", str(caught.exception).lower())

    def test_vite_project_without_a_start_script_gets_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "web"
            folder.mkdir()
            (folder / "package.json").write_text(
                json.dumps({"name": "web", "devDependencies": {"vite": "5.0.0"}}),
                encoding="utf-8",
            )
            (folder / "index.html").write_text("<h1>Hi</h1>", encoding="utf-8")
            contract = discover(folder)
            self.assertEqual(contract["inferred"], "Vite")
            self.assertTrue(contract["web"])
            self.assertEqual(contract["port"], 5173)
            self.assertEqual(contract["cwd"], str(folder.resolve()))
            script = (folder / "app-dropper-start.sh").read_text(encoding="utf-8")
            self.assertIn("vite", script)
            self.assertIn("npm install", script)
            saved = json.loads((folder / "app-dropper.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["port"], 5173)
            again = discover(folder)
            self.assertEqual(again["port"], 5173)
            self.assertNotIn("inferred", again)

    def test_nested_package_script_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "repo"
            client = folder / "client"
            client.mkdir(parents=True)
            (folder / "README.md").write_text("repo\n", encoding="utf-8")
            (client / "package.json").write_text(
                json.dumps({"name": "client", "scripts": {"dev": "vite --port 4173"}}),
                encoding="utf-8",
            )
            contract = discover(folder)
            self.assertEqual(Path(contract["cwd"]), client.resolve())
            self.assertEqual(contract["port"], 4173)
            self.assertFalse((folder / "app-dropper.json").exists())

    def test_static_site_without_a_start_script_gets_a_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "site"
            folder.mkdir()
            (folder / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
            home = root / "home"
            apps = root / "Applications"
            result = create_launcher(folder, home=home, applications=apps)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(result["web"])
            self.assertEqual(result["port"], 8080)
            self.assertIn("static site", result["message"])
            script = (folder / "app-dropper-start.sh").read_text(encoding="utf-8")
            self.assertIn("http.server", script)
            self.assertTrue(list((home / "App Dropper Apps").glob("*.app")))


class PortTests(TrackedCase):
    def test_taken_port_moves_to_the_next_free_port(self):
        preferred = self._open_pair()
        holder = socket.socket()
        holder.bind(("127.0.0.1", preferred))
        holder.listen(16)
        self.addCleanup(holder.close)
        self.assertFalse(port_in_use(preferred + 1))
        identity = "fixture-" + uuid.uuid4().hex[:8]
        body = "body-" + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "app"
            write_web_fixture(folder, preferred, identity, body)
            decision = decide_port(preferred, "/api/health", identity)
            self.assertEqual(decision["reason"], "next-free")
            self.assertEqual(decision["port"], preferred + 1)
            result = launch(discover(folder), state_dir=Path(tmp) / "state")
            self.track_pid(result.get("pid"))
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(result["port"], decision["port"])
            self.assertEqual(result["url"], f"http://127.0.0.1:{decision['port']}/")
            self.assertEqual(result["open_command"], ["/usr/bin/open", result["url"]])
            self.assertNotIn("-a", result["open_command"])
            self.assertIsInstance(result["open_result"]["returncode"], int)
            self.assertEqual(fetch(result["url"]), body)
            self.assertNotEqual(listening_pids(preferred), listening_pids(result["port"]))

    def test_already_running_identity_opens_the_default_browser_once(self):
        preferred = free_port()
        identity = "fixture-" + uuid.uuid4().hex[:8]
        body = "body-" + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "app"
            script = write_web_fixture(folder, preferred, identity, body)
            server = subprocess.Popen(
                [str(script)],
                cwd=folder,
                env={**os.environ, "PORT": str(preferred)},
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.track_proc(server)
            self.assertTrue(wait_for_identity(preferred, identity), "fixture did not start")
            before = set(listening_pids(preferred))
            self.assertTrue(before)
            contract = discover(folder)
            first = launch(contract, state_dir=Path(tmp) / "state")
            second = launch(contract, state_dir=Path(tmp) / "state")
            after = set(listening_pids(preferred))
            self.assertEqual(before, after)
            self.assertEqual(len(after), 1)
            for result in (first, second):
                self.assertEqual(result["reason"], "already-running")
                self.assertEqual(result["url"], f"http://127.0.0.1:{preferred}/")
                self.assertEqual(result["open_command"], ["/usr/bin/open", result["url"]])
                self.assertNotIn("-a", result["open_command"])
                self.assertIsInstance(result["open_result"]["returncode"], int)
            self.assertEqual(fetch(first["url"]), body)

    def test_non_web_script_has_no_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "tool"
            folder.mkdir()
            script = folder / "start.sh"
            script.write_text("#!/bin/bash\ntouch started.txt\n", encoding="utf-8")
            script.chmod(0o755)
            (folder / "app-dropper.json").write_text(
                json.dumps({"name": "Tool", "command": ["./start.sh"], "web": False}),
                encoding="utf-8",
            )
            result = launch(discover(folder), state_dir=Path(tmp) / "state")
            self.track_pid(result.get("pid"))
            self.assertTrue(result["ok"], result.get("error"))
            self.assertFalse(result["web"] if "web" in result else False)
            self.assertIsNone(result["url"])
            self.assertIsNone(result["port"])
            self.assertIsNone(result["open_command"])
            self.assertTrue((folder / "started.txt").is_file())

    def _open_pair(self):
        for _ in range(30):
            port = free_port()
            if port < 65535 and not port_in_use(port) and not port_in_use(port + 1):
                return port
        self.fail("Could not find a free port pair")


class ShellTests(TrackedCase):
    def test_api_reports_a_missing_start_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty"
            empty.mkdir()
            server = serve(0, root / "data", "token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            port = server.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/create",
                data=json.dumps({"path": str(empty)}).encode(),
                headers={"Content-Type": "application/json", "X-App-Dropper-Token": "token"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 400)
            payload = json.loads(caught.exception.read().decode())
            self.assertIn("could not tell", payload["error"].lower())
            self.assertFalse(App(root / "data").jobs)

    def test_run_script_serves_folder_drop_twice(self):
        bodies = []
        for index in (1, 2):
            port = free_port()
            with tempfile.TemporaryDirectory() as tmp:
                env = os.environ.copy()
                env["CONVERTO_PORT"] = str(port)
                env["APP_DROPPER_DATA_DIR"] = tmp
                proc = subprocess.Popen(
                    [str(ROOT / "scripts/run.sh")],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                self.track_proc(proc)
                output = []

                def read():
                    for line in proc.stdout:
                        output.append(line)

                threading.Thread(target=read, daemon=True).start()
                deadline = time.time() + 15
                while time.time() < deadline and proc.poll() is None:
                    if any("App Dropper ready" in line for line in output):
                        break
                    time.sleep(0.05)
                self.assertIsNone(proc.poll(), "".join(output))
                self.assertTrue(any("App Dropper ready" in line for line in output), "".join(output))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    body = response.read().decode()
                bodies.append(body)
                write_evidence(f"shell-{index}.html", body)
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                self.procs.remove(proc)
            self.assertTrue(body.strip())
            self.assertIn("App Dropper Apps", body)
            self.assertIn("Apps folder", body)
            self.assertIn("Drop a folder to create a Mac app", body)
            lowered = body.lower()
            self.assertNotIn("compress", lowered)
            self.assertNotIn("video", lowered)
            self.assertNotIn("image", lowered)
        self.assertTrue(bodies[0].strip())
        self.assertTrue(bodies[1].strip())

    def test_taken_shell_port_moves_to_the_next_one(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        preferred = holder.getsockname()[1]
        holder.listen(1)
        self.addCleanup(holder.close)
        self.assertFalse(port_in_use(preferred + 1), "the next port was already taken")
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["CONVERTO_PORT"] = str(preferred)
            env["APP_DROPPER_DATA_DIR"] = tmp
            proc = subprocess.Popen(
                [str(ROOT / "scripts/run.sh")],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            self.track_proc(proc)
            output = []

            def read():
                for line in proc.stdout:
                    output.append(line)

            threading.Thread(target=read, daemon=True).start()
            deadline = time.time() + 15
            while time.time() < deadline and proc.poll() is None:
                if any(f"http://127.0.0.1:{preferred + 1}" in line for line in output):
                    break
                time.sleep(0.05)
            text = "".join(output)
            self.assertIsNone(proc.poll(), text)
            self.assertIn(f"App Dropper ready on http://127.0.0.1:{preferred + 1}", text)
            with urllib.request.urlopen(f"http://127.0.0.1:{preferred + 1}/api/health", timeout=5) as response:
                payload = json.loads(response.read().decode())
            self.assertEqual(payload["app"], "app-dropper")
            self.assertTrue(port_in_use(preferred))
            write_evidence("shell-next-port.txt", text)


class BundleTests(TrackedCase):
    @classmethod
    def setUpClass(cls):
        ensure_launcher_binary()
        subprocess.run([str(ROOT / "scripts" / "build_and_run.sh"), "--build-only"], check=True)

    def test_packaged_shell_layout_builds_a_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources_app = root / "Converto.app" / "Contents" / "Resources" / "app"
            shutil.copytree(
                ROOT / "app",
                resources_app,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            swift = resources_app / "entry" / "macos" / "FolderLauncher.swift"
            self.assertTrue(swift.is_file())
            shipped = ROOT / "dist" / "Dev App Dropper and Launcher.app" / "Contents" / "Resources" / "app" / "entry" / "macos" / "FolderLauncher.swift"
            self.assertTrue(shipped.is_file(), "The Mac shell bundle does not include FolderLauncher.swift")
            folder = root / "tool"
            folder.mkdir()
            script = folder / "start.sh"
            script.write_text("#!/bin/bash\ntouch started.txt\n", encoding="utf-8")
            script.chmod(0o755)
            (folder / "app-dropper.json").write_text(
                json.dumps({"name": "Packaged Tool", "command": ["./start.sh"], "web": False}),
                encoding="utf-8",
            )
            home = root / "home"
            applications = root / "Applications"
            probe = (
                "import json, os\n"
                "from pathlib import Path\n"
                "from business.bundle import cache_dir, create_launcher, swift_source\n"
                "source = swift_source()\n"
                "cache = cache_dir()\n"
                "bundle = source.parents[5]\n"
                "if bundle in cache.parents or cache == bundle:\n"
                "    raise SystemExit('compile cache is inside the app bundle: ' + str(cache))\n"
                "result = create_launcher(Path(os.environ['FIXTURE']), home=Path(os.environ['HOME_DIR']), applications=Path(os.environ['APPS_DIR']))\n"
                "print(json.dumps({'ok': result.get('ok'), 'error': result.get('error'), 'swift': str(source), 'cache': str(cache), 'dropper': result.get('dropper_app')}))\n"
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(resources_app)
            env["FIXTURE"] = str(folder)
            env["HOME_DIR"] = str(home)
            env["APPS_DIR"] = str(applications)
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], payload["error"])
            self.assertEqual(Path(payload["swift"]).resolve(), swift.resolve())
            self.assertTrue((Path(payload["cache"]) / "FolderLauncher").is_file())
            self.assertNotIn("Converto.app", Path(payload["cache"]).parts)
            dropper = Path(payload["dropper"])
            self.assertTrue((dropper / "Contents" / "MacOS" / "Launcher").is_file())
            self.assertEqual(dropper.parent.name, "App Dropper Apps")

    def test_drop_installs_a_normal_app_and_reopens_in_the_default_browser(self):
        signature_log = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            applications = root / "Applications"
            home.mkdir()
            applications.mkdir()
            preferred = free_port()
            identity = "fixture-" + uuid.uuid4().hex[:8]
            body = "body-" + uuid.uuid4().hex[:8]
            folder = root / "project"
            script = write_web_fixture(folder, preferred, identity, body)
            created = create_launcher(folder, home=home, applications=applications)
            self.assertTrue(created["ok"], created.get("error"))
            dropper = Path(created["dropper_app"])
            installed = Path(created["applications_app"])
            self.assertEqual(dropper.parent.name, "App Dropper Apps")
            self.assertTrue(dropper.is_dir())
            self.assertTrue(installed.is_dir())
            self.assertFalse(created["applications_fallback"])
            for app in (dropper, installed):
                self._assert_bundle(app, script)
                self._check_signature(app, signature_log)
            write_evidence("bundle-check.log", "\n".join(signature_log))
            state = root / "state"
            self._two_runs(dropper, state, body, "launcher-run-1.log", "launcher-run-2.log")
            self._two_runs(dropper, state, body, "launcher-run-1b.log", "launcher-run-2b.log")

    def test_entrypoint_uses_the_next_port_when_the_preferred_port_is_taken(self):
        preferred = self._open_pair()
        holder = socket.socket()
        holder.bind(("127.0.0.1", preferred))
        holder.listen(16)
        self.addCleanup(holder.close)
        identity = "fixture-" + uuid.uuid4().hex[:8]
        body = "body-" + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "project"
            write_web_fixture(folder, preferred, identity, body)
            created = create_launcher(folder, home=root / "home", applications=root / "Applications")
            self.assertTrue(created["ok"], created.get("error"))
            decision = decide_port(preferred, "/api/health", identity)
            self.assertEqual(decision["port"], preferred + 1)
            text, proc = self._run_entrypoint(Path(created["dropper_app"]), root / "state")
            self.track_proc(proc)
            self.assertEqual(field(text, "ready"), "ok", text)
            self.assertEqual(field(text, "reason"), "next-free", text)
            url = field(text, "url")
            self.assertEqual(url, f"http://127.0.0.1:{decision['port']}/")
            self.assertEqual(field(text, "open"), f"/usr/bin/open {url}")
            self.assertNotIn("-a", field(text, "open"))
            self.track_pid(field(text, "server-pid"))
            self.assertEqual(fetch(url), body)
            write_evidence("launcher-next-port.log", text)

    def test_unwritable_applications_folder_is_a_visible_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            locked = root / "Applications"
            home.mkdir()
            locked.mkdir()
            locked.chmod(0o555)
            try:
                folder = root / "tool"
                folder.mkdir()
                script = folder / "start.sh"
                script.write_text("#!/bin/bash\ntouch started.txt\n", encoding="utf-8")
                script.chmod(0o755)
                (folder / "app-dropper.json").write_text(
                    json.dumps({"name": "Tool", "command": ["./start.sh"], "web": False}),
                    encoding="utf-8",
                )
                created = create_launcher(folder, home=home, applications=locked)
                self.assertTrue(created["ok"], created.get("error"))
                self.assertTrue(created["applications_fallback"])
                self.assertEqual(Path(created["dropper_app"]).parent.name, "App Dropper Apps")
                self.assertEqual(Path(created["applications_app"]).parent, home / "Applications")
                self.assertIn(str(home / "Applications"), created["message"])
                self.assertIn(str(locked), created["message"])
                self.assertIn("not writable", created["message"])
                self.assertEqual(list(locked.rglob("*.app")), [])
                contract = json.loads((Path(created["dropper_app"]) / "Contents/Resources/launch-contract.json").read_text())
                self.assertFalse(contract["web"])
                self.assertIsNone(contract["port"])
            finally:
                locked.chmod(0o755)

    def _two_runs(self, app, state, body, first_log, second_log):
        first_text, first_proc = self._run_entrypoint(app, state)
        self.track_proc(first_proc)
        self.assertEqual(field(first_text, "ready"), "ok", first_text)
        url = field(first_text, "url")
        pid = field(first_text, "server-pid")
        self.track_pid(pid)
        self.assertTrue(url.startswith("http://127.0.0.1:"), first_text)
        self.assertEqual(fetch(url), body)
        self.assertIn(pid, {str(item) for item in listening_pids(int(field(first_text, "port")))})
        listeners = set(listening_pids(int(field(first_text, "port"))))
        self._stop_launcher(first_proc)
        second_text, second_proc = self._run_entrypoint(app, state)
        self.track_proc(second_proc)
        self.assertEqual(field(second_text, "ready"), "ok", second_text)
        self.assertEqual(field(second_text, "reason"), "already-running", second_text)
        self.assertEqual(field(second_text, "url"), url)
        self.assertEqual(field(second_text, "server-pid"), pid)
        self.assertEqual(field(second_text, "open"), f"/usr/bin/open {url}")
        self.assertNotIn("-a", field(second_text, "open"))
        self.assertEqual(set(listening_pids(int(field(second_text, "port")))), listeners)
        self.assertEqual(len(listeners), 1)
        open_status = field(second_text, "open-status")
        if open_status not in ("", "0"):
            write_evidence("launcher-open-failure.log", second_text)
        write_evidence(first_log, first_text)
        write_evidence(second_log, second_text)
        self._stop_launcher(second_proc)
        stop_pid(pid)
        self.pids = [item for item in self.pids if str(item) != str(pid)]
        deadline = time.time() + 5
        while time.time() < deadline and port_in_use(int(field(first_text, "port"))):
            time.sleep(0.05)

    def _run_entrypoint(self, app, state):
        binary = app / "Contents/MacOS/Launcher"
        env = os.environ.copy()
        env["APP_DROPPER_STATE_DIR"] = str(state)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=env,
        )
        lines = []

        def read():
            for line in proc.stdout:
                lines.append(line)

        threading.Thread(target=read, daemon=True).start()
        deadline = time.time() + 40
        while time.time() < deadline:
            text = "".join(lines)
            if "ready: ok" in text or "ready: error" in text or "entrypoint: failed" in text:
                return text, proc
            if proc.poll() is not None:
                return text, proc
            time.sleep(0.05)
        return "".join(lines), proc

    def _stop_launcher(self, proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.stdout:
            proc.stdout.close()
        if proc in self.procs:
            self.procs.remove(proc)

    def _assert_bundle(self, app, script):
        with (app / "Contents/Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info.get("CFBundlePackageType"), "APPL")
        self.assertTrue(info.get("CFBundleExecutable"))
        executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
        self.assertTrue(os.access(executable, os.X_OK))
        self.assertNotIn("LSUIElement", info)
        icon = app / "Contents/Resources/AppIcon.icns"
        self.assertGreater(icon.stat().st_size, 0)
        contract = json.loads((app / "Contents/Resources/launch-contract.json").read_text(encoding="utf-8"))
        self.assertIn(str(script.resolve()), contract["argv"])
        bundled = (app / "Contents/Resources/launcher_runtime.py").read_bytes()
        self.assertEqual(bundled, (ROOT / "app/business/launcher_runtime.py").read_bytes())
        self.assertIn("setActivationPolicy(.regular)", SWIFT_SOURCE.read_text(encoding="utf-8"))

    def _check_signature(self, app, log):
        for command in (
            ["/usr/bin/plutil", "-lint", str(app / "Contents/Info.plist")],
            ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(app)],
        ):
            if not Path(command[0]).exists():
                continue
            result = subprocess.run(command, capture_output=True, text=True)
            log.append("$ " + " ".join(command))
            log.append(result.stdout)
            log.append(result.stderr)
            log.append(f"exit: {result.returncode}")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def _open_pair(self):
        for _ in range(30):
            port = free_port()
            if port < 65535 and not port_in_use(port) and not port_in_use(port + 1):
                return port
        self.fail("Could not find a free port pair")


if __name__ == "__main__":
    unittest.main()
