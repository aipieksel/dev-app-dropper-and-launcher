"""Discover a folder start contract and launch it.

A web app opens in the default browser with /usr/bin/open and no named browser.
If the preferred port belongs to something else, startup uses the next free port.
Already running means this app's own healthy server. A folder with no start
script is scanned. A recognized project gets a start file and a launcher.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request


class ContractError(Exception):
    """The folder cannot be launched or packaged."""


SCRIPT_CANDIDATES = (
    "scripts/run.sh",
    "scripts/dev.sh",
    "scripts/start.sh",
    "run.sh",
    "dev.sh",
    "start.sh",
)
PACKAGE_SCRIPTS = ("dev", "start", "serve")
LOCK_MANAGERS = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lock", "bun"),
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)
PORT_PATTERNS = (
    re.compile(r"(?:^|[\s;\"'])PORT=(\d{1,5})\b"),
    re.compile(r"--port(?:=|\s+)(\d{1,5})\b"),
    re.compile(r"(?:^|\s)-p\s+(\d{1,5})\b"),
    re.compile(r"\blocalhost:(\d{1,5})\b"),
    re.compile(r"\b127\.0\.0\.1:(\d{1,5})\b"),
)
OPEN_BIN = "/usr/bin/open"
PORT_SPAN = 50


SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    "venv",
    ".venv",
    "__pycache__",
    "target",
    "vendor",
    "Pods",
    "coverage",
    ".turbo",
    "out",
    ".cache",
}


def discover(folder: Path) -> dict:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ContractError("Drop a folder. That path is not a directory.")
    contract = _explicit_contract(folder) or _infer_project(folder)
    if contract is None:
        raise ContractError("Could not tell what this project is, so no Mac app was created.")
    return contract


def resolve_destinations(home: Path | None = None, applications: Path | None = None) -> dict:
    home = Path(home).expanduser() if home else Path.home()
    requested = Path(applications).expanduser() if applications else Path("/Applications")
    dropper = home / "App Dropper Apps"
    if directory_writable(requested):
        return {
            "home": home,
            "dropper": dropper,
            "applications": requested,
            "applications_fallback": False,
            "applications_requested": requested,
        }
    fallback = home / "Applications"
    return {
        "home": home,
        "dropper": dropper,
        "applications": fallback,
        "applications_fallback": True,
        "applications_requested": requested,
    }


def directory_writable(path: Path) -> bool:
    path = Path(path).expanduser()
    if path.is_symlink():
        return False
    try:
        if path.exists():
            if not path.is_dir():
                return False
            probe = path / f".app-dropper-write-{os.getpid()}"
        else:
            return directory_writable(path.parent) if path.parent != path else False
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True when a process is accepting connections. TIME_WAIT does not count."""
    if listening_pids(port):
        return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return True
    finally:
        sock.close()


def listening_pids(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def identity_matches(port: int, health_path: str | None, identity: str | None, host: str = "127.0.0.1") -> bool:
    if not identity or not health_path:
        return False
    url = f"http://{host}:{int(port)}{health_path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=0.4) as response:
            body = response.read(8192)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False
    text = body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("app") == identity:
        return True
    return text.strip() == identity


def decide_port(preferred: int, health_path: str | None, identity: str | None, own_pids: set[int] | None = None) -> dict:
    preferred = int(preferred)
    own_pids = own_pids or set()
    if identity_matches(preferred, health_path, identity) or (own_pids and set(listening_pids(preferred)) & own_pids):
        return {"action": "reuse", "port": preferred, "reason": "already-running"}
    if port_in_use(preferred) or listening_pids(preferred):
        for port in range(preferred + 1, preferred + 1 + PORT_SPAN):
            if not port_in_use(port) and not listening_pids(port):
                return {"action": "bind", "port": port, "reason": "next-free"}
        raise ContractError(f"No free port after {preferred}.")
    return {"action": "bind", "port": preferred, "reason": "preferred"}


def local_url(port: int, url_path: str = "/") -> str:
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    if any(char in url_path for char in "\n\r \t"):
        raise ContractError("URL path is not usable.")
    return f"http://127.0.0.1:{int(port)}{url_path}"


def open_in_default_browser(url: str) -> dict:
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/[^\s]*", url):
        raise ContractError("Refusing to open a URL that is not this local app.")
    command = [OPEN_BIN, url]
    if "-a" in command:
        raise ContractError("Refusing a named browser.")
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        return {"command": command, "returncode": 124, "stderr": "open timed out", "stdout": ""}
    except OSError as error:
        return {"command": command, "returncode": 127, "stderr": str(error), "stdout": ""}
    return {
        "command": command,
        "returncode": completed.returncode,
        "stderr": completed.stderr,
        "stdout": completed.stdout,
    }


def apply_port(argv: list[str], preferred: int | None, chosen: int) -> list[str]:
    if preferred is None:
        return list(argv)
    preferred_text = str(preferred)
    chosen_text = str(chosen)
    updated = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in {"--port", "-p"} and index + 1 < len(argv) and argv[index + 1] == preferred_text:
            updated.extend((arg, chosen_text))
            index += 2
            continue
        prefix = "--port="
        if arg.startswith(prefix) and arg[len(prefix):] == preferred_text:
            updated.append(prefix + chosen_text)
            index += 1
            continue
        updated.append(arg)
        index += 1
    return updated


def launch(contract: dict, state_dir: Path | None = None) -> dict:
    state_root = Path(state_dir) if state_dir else default_state_dir()
    bundle_id = str(contract["bundle_id"])
    slot = state_root / bundle_id
    slot.mkdir(parents=True, exist_ok=True)
    lock_handle = (slot / "launch.lock").open("a+")
    try:
        import fcntl

        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return _launch_locked(contract, slot)
    finally:
        lock_handle.close()


def format_result(result: dict) -> str:
    open_command = result.get("open_command") or []
    port = result.get("port")
    lines = [
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"reason: {result.get('reason') or ''}",
        f"url: {result.get('url') or ''}",
        f"port: {'' if port is None else port}",
        f"server-pid: {result.get('pid') or ''}",
        f"open: {' '.join(open_command)}",
    ]
    opened = result.get("open_result") or {}
    if opened:
        lines.append(f"open-status: {opened.get('returncode')}")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    lines.append("ready: ok" if result.get("ok") else "ready: error")
    return "\n".join(lines) + "\n"


def default_state_dir() -> Path:
    override = os.environ.get("APP_DROPPER_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library/Application Support/App Dropper/launchers"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "launch":
        print("error: usage: launcher_runtime.py launch CONTRACT.json", flush=True)
        return 2
    contract: dict = {}
    try:
        contract = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        result = launch(contract)
    except (OSError, ContractError, json.JSONDecodeError) as error:
        result = {"ok": False, "error": str(error), "reason": "error", "url": None, "port": None, "pid": None}
    text = format_result(result)
    state_root = default_state_dir() / str(contract.get("bundle_id", "unknown"))
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / "last-launch.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    print(text, end="", flush=True)
    return 0 if result.get("ok") else 2


def _launch_locked(contract: dict, slot: Path) -> dict:
    state_path = slot / "state.json"
    state = _load_state(state_path)
    if state and _state_is_live(state, contract):
        return _finish_reuse(contract, state, "already-running")
    if not contract.get("web"):
        return _start_script(contract, slot, state_path, port=None, reason="script")
    decision = decide_port(
        int(contract["port"]),
        contract.get("health_path"),
        contract.get("identity"),
        _state_pids(state) if state else set(),
    )
    if decision["action"] == "reuse":
        reused = {
            "pid": _listener_pid(decision["port"]) or (state or {}).get("pid"),
            "port": decision["port"],
            "url": local_url(decision["port"], contract.get("url_path") or "/"),
        }
        _write_state(state_path, contract, reused)
        return _finish_reuse(contract, reused, "already-running")
    return _start_script(contract, slot, state_path, port=decision["port"], reason=decision["reason"])


def _finish_reuse(contract: dict, state: dict, reason: str) -> dict:
    url = state.get("url") if contract.get("web") else None
    opened = open_in_default_browser(url) if url else None
    return {
        "ok": True,
        "reason": reason,
        "url": url,
        "port": state.get("port"),
        "pid": state.get("pid"),
        "open_command": None if opened is None else opened["command"],
        "open_result": opened,
        "error": None,
    }


def _start_script(contract: dict, slot: Path, state_path: Path, port: int | None, reason: str) -> dict:
    argv = list(contract["argv"])
    env = os.environ.copy()
    path = os.environ.get("PATH", "")
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" + (":" + path if path else "")
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    url = None
    if contract.get("web"):
        argv = apply_port(argv, contract.get("port"), int(port))
        env["PORT"] = str(port)
        port_env = contract.get("port_env") or "PORT"
        env[str(port_env)] = str(port)
        if contract.get("identity"):
            env["APP_DROPPER_IDENTITY"] = str(contract["identity"])
        url = local_url(int(port), contract.get("url_path") or "/")
    log_path = slot / "server.log"
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            argv,
            cwd=contract["cwd"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        return {"ok": False, "error": f"Could not start the app: {error}", "reason": "error", "url": None, "port": port, "pid": None}
    finally:
        handle.close()
    if contract.get("web"):
        ready = _wait_ready(process, int(port), contract.get("health_path"), contract.get("identity"))
        if not ready:
            tail = _log_tail(log_path)
            if process.poll() is None:
                _stop_process(process.pid)
            return {
                "ok": False,
                "error": "The app did not become ready. " + tail,
                "reason": "error",
                "url": None,
                "port": port,
                "pid": process.pid,
            }
    else:
        for _ in range(30):
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() not in (None, 0):
            return {
                "ok": False,
                "error": "The start script failed. " + _log_tail(log_path),
                "reason": "error",
                "url": None,
                "port": None,
                "pid": process.pid,
            }
    state = {"pid": process.pid, "port": port, "url": url}
    _write_state(state_path, contract, state)
    if process.poll() is None:
        # The server is detached on purpose. Marking it waited-for keeps GC quiet.
        process.returncode = 0
    opened = open_in_default_browser(url) if url else None
    return {
        "ok": True,
        "reason": reason,
        "url": url,
        "port": port,
        "pid": process.pid,
        "open_command": None if opened is None else opened["command"],
        "open_result": opened,
        "error": None,
    }


def _wait_ready(process: subprocess.Popen, port: int, health_path: str | None, identity: str | None) -> bool:
    deadline = time.time() + 25
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if identity and health_path:
            if identity_matches(port, health_path, identity):
                return True
        elif _accepts(port):
            return True
        time.sleep(0.1)
    return False


def _accepts(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _state_is_live(state: dict, contract: dict) -> bool:
    pids = _state_pids(state)
    if not pids or not any(_pid_alive(pid) for pid in pids):
        return False
    if not contract.get("web"):
        marker = str(contract["argv"][0])
        return marker in _tree_commands(next(iter(pids)))
    port = state.get("port")
    if not isinstance(port, int):
        return False
    listeners = set(listening_pids(port))
    if listeners and listeners & _process_tree(next(iter(pids))):
        return True
    return identity_matches(port, contract.get("health_path"), contract.get("identity"))


def _state_pids(state: dict | None) -> set[int]:
    if not state:
        return set()
    pid = state.get("pid")
    if isinstance(pid, int) and pid > 0:
        return {pid}
    return set()


def _finish_listener_pid(port: int) -> int | None:
    pids = listening_pids(port)
    return pids[0] if pids else None


def _listener_pid(port: int) -> int | None:
    return _finish_listener_pid(port)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_tree(pid: int) -> set[int]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-ax", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {pid}
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen = set()
    stack = [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, []))
    return seen


def _tree_commands(pid: int) -> str:
    chunks = []
    for item in _process_tree(pid):
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(item), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        chunks.append(result.stdout)
    return "\n".join(chunks)


def _write_state(path: Path, contract: dict, state: dict) -> None:
    payload = {
        "pid": state.get("pid"),
        "port": state.get("port"),
        "url": state.get("url"),
        "folder": contract.get("folder"),
        "identity": contract.get("identity"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stop_process(pid: int) -> None:
    try:
        group = os.getpgid(pid)
        os.killpg(group, 15)
    except OSError:
        try:
            os.kill(pid, 15)
        except OSError:
            return


def _log_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    return text[-800:]


def _read_manifest(folder: Path) -> dict | None:
    for name in ("app-dropper.json", ".app-dropper.json"):
        path = folder / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ContractError(f"{name} is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise ContractError(f"{name} must be an object.")
        return data
    return None


def _from_manifest(folder: Path, data: dict) -> dict:
    command = data.get("command", data.get("start", data.get("script")))
    argv = _normalize_argv(folder, command)
    port = _coerce_port(data.get("port"))
    if port is None:
        port = _port_in_text(" ".join(argv))
    explicit_web = data.get("web") if "web" in data else None
    return _contract(
        folder,
        argv,
        name=str(data.get("name") or folder.name),
        port=port,
        port_env=str(data.get("port_env") or "PORT"),
        health_path=data.get("health_path"),
        identity=data.get("identity"),
        url_path=str(data.get("url_path") or "/"),
        explicit_web=explicit_web,
    )


def _from_package(folder: Path) -> dict | None:
    package_path = folder / "package.json"
    if not package_path.is_file():
        return None
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ContractError(f"package.json is not valid JSON: {error}") from error
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict):
        return None
    script_name = next((name for name in PACKAGE_SCRIPTS if isinstance(scripts.get(name), str) and scripts.get(name).strip()), None)
    if script_name is None:
        return None
    manager = "npm"
    for filename, candidate in LOCK_MANAGERS:
        if (folder / filename).exists():
            manager = candidate
            break
    argv = [manager, "run", script_name]
    script_text = str(scripts[script_name])
    port = _port_in_text(script_text) or _port_in_env_files(folder) or _port_in_config_files(folder)
    name = package.get("name") if isinstance(package.get("name"), str) else folder.name
    return _contract(folder, argv, name=name, port=port, port_env="PORT", health_path=None, identity=None, url_path="/", explicit_web=None)


def _from_script(folder: Path) -> dict | None:
    for relative in SCRIPT_CANDIDATES:
        path = folder / relative
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            argv = _normalize_argv(folder, [relative])
            port = _port_in_text(text) or _port_in_env_files(folder)
            return _contract(
                folder,
                argv,
                name=folder.name,
                port=port,
                port_env="PORT",
                health_path=None,
                identity=None,
                url_path="/",
                explicit_web=None,
            )
    return None


def _project_dirs(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, _filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS and not name.startswith("."))
        if depth > 3:
            dirnames[:] = []
            continue
        found.append(current)
        if depth == 3:
            dirnames[:] = []
    return found


def _explicit_contract(root: Path) -> dict | None:
    for directory in _project_dirs(root):
        manifest = _read_manifest(directory)
        if manifest is not None:
            return _from_manifest(directory, manifest)
        contract = _from_package(directory) or _from_script(directory)
        if contract is not None:
            return contract
    return None


def _infer_project(root: Path) -> dict | None:
    best = None
    for directory in _project_dirs(root):
        spec = _match_project(directory)
        if spec is None:
            continue
        depth = len(directory.relative_to(root).parts)
        rank = (spec["score"], -depth)
        if best is None or rank > best[0]:
            best = (rank, directory, spec)
    if best is None:
        return None
    directory, spec = best[1], best[2]
    try:
        contract = _write_inferred_start(directory, spec)
    except OSError as error:
        raise ContractError(f"Found a {spec['kind']} project but could not write its start file: {error}") from error
    contract["inferred"] = spec["kind"]
    return contract


def _write_inferred_start(folder: Path, spec: dict) -> dict:
    command = list(spec["command"])
    if spec.get("script"):
        script_path = folder / "app-dropper-start.sh"
        script_path.write_text(spec["script"], encoding="utf-8")
        script_path.chmod(0o755)
        command = ["./app-dropper-start.sh"]
    manifest = {"name": spec["name"], "command": command, "web": bool(spec.get("web"))}
    if spec.get("web") and spec.get("port"):
        manifest["port"] = int(spec["port"])
    path = folder / "app-dropper.json"
    if not path.exists():
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return _from_manifest(folder, manifest)


def _match_project(folder: Path) -> dict | None:
    package = _read_json(folder / "package.json") or {}
    deps = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = package.get(key)
        if isinstance(block, dict):
            deps.update(str(name) for name in block)
    name = package.get("name") if isinstance(package.get("name"), str) and package.get("name").strip() else folder.name
    found = []
    if "next" in deps or _any_file(folder, ("next.config.js", "next.config.mjs", "next.config.ts")):
        found.append(_node_spec(folder, "Next.js", name, 3000, "next dev --hostname 127.0.0.1 --port \"$PORT\"", 100))
    if "vite" in deps or _any_file(folder, ("vite.config.js", "vite.config.mjs", "vite.config.ts")):
        found.append(_node_spec(folder, "Vite", name, _port_in_config_files(folder) or 5173, "vite --host 127.0.0.1 --port \"$PORT\"", 95))
    if "astro" in deps or _any_file(folder, ("astro.config.mjs", "astro.config.js", "astro.config.ts")):
        found.append(_node_spec(folder, "Astro", name, 4321, "astro dev --host 127.0.0.1 --port \"$PORT\"", 94))
    if (folder / "angular.json").is_file() or "@angular/core" in deps:
        found.append(_node_spec(folder, "Angular", name, 4200, "ng serve --host 127.0.0.1 --port \"$PORT\"", 93))
    if "react-scripts" in deps:
        found.append(_node_spec(folder, "React", name, 3000, "react-scripts start", 92, host_env=True))
    if "nuxt" in deps or _any_file(folder, ("nuxt.config.js", "nuxt.config.ts", "nuxt.config.mjs")):
        found.append(_node_spec(folder, "Nuxt", name, 3000, "nuxt dev --host 127.0.0.1 --port \"$PORT\"", 91))
    if "@sveltejs/kit" in deps:
        found.append(_node_spec(folder, "SvelteKit", name, 5173, "vite dev --host 127.0.0.1 --port \"$PORT\"", 90))
    if (folder / "manage.py").is_file():
        found.append(_python_spec(folder, "Django", name, 8000, 'exec "$PY" manage.py runserver "127.0.0.1:${PORT}"\n', 85, ("django",)))
    entry = _python_entry(folder)
    requirements = _requirement_text(folder)
    if entry and ("FastAPI(" in entry[1] or "fastapi" in requirements):
        module = entry[0].replace("/", ".").removesuffix(".py")
        found.append(_python_spec(
            folder, "FastAPI", name, 8000,
            f'exec "$PY" -m uvicorn {module}:app --host 127.0.0.1 --port "$PORT"\n',
            84, ("uvicorn", "fastapi"),
        ))
    if entry and ("Flask(" in entry[1] or "flask" in requirements):
        module = entry[0].replace("/", ".").removesuffix(".py")
        found.append(_python_spec(
            folder, "Flask", name, 5000,
            f'exec "$PY" -m flask --app {module} run --host 127.0.0.1 --port "$PORT"\n',
            83, ("flask",),
        ))
    if "streamlit" in requirements or (entry and "import streamlit" in entry[1]):
        target = entry[0] if entry else "app.py"
        found.append(_python_spec(
            folder, "Streamlit", name, 8501,
            f'exec "$PY" -m streamlit run {target} --server.address 127.0.0.1 --server.port "$PORT"\n',
            82, ("streamlit",),
        ))
    gemfile = _text_file(folder / "Gemfile")
    if gemfile and re.search(r"\brails\b", gemfile, re.I):
        runner = "bin/rails" if (folder / "bin" / "rails").is_file() else "bundle exec rails"
        found.append(_shell_spec(folder, "Rails", name, 3000, f"exec {runner} server -b 127.0.0.1 -p \"$PORT\"\n", 80))
    if (folder / "artisan").is_file():
        found.append(_shell_spec(folder, "Laravel", name, 8000, 'exec php artisan serve --host 127.0.0.1 --port "$PORT"\n', 79))
    if (folder / "Cargo.toml").is_file():
        found.append({"kind": "Rust", "name": name, "command": ["cargo", "run"], "web": False, "score": 60})
    if (folder / "go.mod").is_file():
        found.append({"kind": "Go", "name": name, "command": ["go", "run", "."], "web": False, "score": 59})
    makefile = _text_file(folder / "Makefile")
    if makefile:
        for target in ("dev", "start", "serve", "run"):
            if re.search(rf"(?m)^{target}\s*:", makefile):
                port = _port_in_text(makefile)
                found.append({
                    "kind": "Make",
                    "name": name,
                    "command": ["make", target],
                    "web": port is not None,
                    "port": port,
                    "score": 50,
                })
                break
    if entry and not any(item["kind"] in {"Django", "FastAPI", "Flask", "Streamlit"} for item in found):
        found.append({"kind": "Python", "name": name, "command": ["python3", entry[0]], "web": False, "score": 40})
    if (folder / "index.html").is_file():
        static_dir = "."
    elif (folder / "public" / "index.html").is_file():
        static_dir = "public"
    else:
        static_dir = ""
    if static_dir:
        enter = "" if static_dir == "." else "cd public\n"
        found.append(_shell_spec(
            folder, "static site", name, 8080,
            enter + 'exec /usr/bin/python3 -m http.server "$PORT" -b 127.0.0.1\n',
            10,
        ))
    if not found:
        return None
    return max(found, key=lambda item: item["score"])


def _node_spec(folder: Path, kind: str, name: str, port: int, arguments: str, score: int, host_env: bool = False) -> dict:
    install = ""
    if (folder / "package.json").is_file() and not (folder / "node_modules").is_dir():
        install = _node_install(folder) + "\n"
    runner = _node_runner(folder, arguments)
    host = "export HOST=127.0.0.1\n" if host_env else ""
    script = _bash(f'PORT="${{PORT:-{port}}}"\n{host}{install}{runner}\n')
    return {"kind": kind, "name": name, "port": port, "web": True, "script": script, "command": [], "score": score}


def _python_spec(folder: Path, kind: str, name: str, port: int, launch: str, score: int, packages: tuple[str, ...]) -> dict:
    script = _bash(f'PORT="${{PORT:-{port}}}"\n{_python_prelude(folder, packages)}{launch}')
    return {"kind": kind, "name": name, "port": port, "web": True, "script": script, "command": [], "score": score}


def _shell_spec(folder: Path, kind: str, name: str, port: int, launch: str, score: int) -> dict:
    del folder
    script = _bash(f'PORT="${{PORT:-{port}}}"\n{launch}')
    return {"kind": kind, "name": name, "port": port, "web": True, "script": script, "command": [], "score": score}


def _bash(body: str) -> str:
    return "#!/bin/bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\n" + body


def _node_install(folder: Path) -> str:
    if (folder / "pnpm-lock.yaml").is_file():
        return "pnpm install"
    if (folder / "yarn.lock").is_file():
        return "yarn install"
    if (folder / "bun.lock").is_file() or (folder / "bun.lockb").is_file():
        return "bun install"
    return "npm install"


def _node_runner(folder: Path, arguments: str) -> str:
    if (folder / "pnpm-lock.yaml").is_file():
        return f"exec pnpm exec {arguments}"
    if (folder / "yarn.lock").is_file():
        return f"exec yarn {arguments}"
    if (folder / "bun.lock").is_file() or (folder / "bun.lockb").is_file():
        return f"exec bun x {arguments}"
    return f"exec npx --yes {arguments}"


def _python_prelude(folder: Path, packages: tuple[str, ...]) -> str:
    has_requirements = (folder / "requirements.txt").is_file()
    has_project = (folder / "pyproject.toml").is_file()
    if not has_requirements and not has_project:
        quoted = " ".join(packages)
        return (
            'if [[ ! -x .venv/bin/python ]]; then\n'
            '  python3 -m venv .venv\n'
            f'  .venv/bin/pip install {quoted}\n'
            'fi\n'
            'PY=".venv/bin/python"\n'
        )
    install = "  .venv/bin/pip install -r requirements.txt\n" if has_requirements else "  .venv/bin/pip install .\n"
    return (
        'if [[ ! -x .venv/bin/python ]]; then\n'
        '  python3 -m venv .venv\n'
        f'{install}'
        'fi\n'
        'PY=".venv/bin/python"\n'
    )


def _python_entry(folder: Path) -> tuple[str, str] | None:
    for relative in ("app.py", "main.py", "server.py", "src/app.py", "src/main.py", "asgi.py", "wsgi.py"):
        path = folder / relative
        if path.is_file():
            return relative, path.read_text(encoding="utf-8", errors="replace")[:8000]
    return None


def _requirement_text(folder: Path) -> str:
    parts = []
    for name in ("requirements.txt", "pyproject.toml", "Pipfile"):
        text = _text_file(folder / name)
        if text:
            parts.append(text.lower())
    return "\n".join(parts)


def _any_file(folder: Path, names: tuple[str, ...]) -> bool:
    return any((folder / name).is_file() for name in names)


def _text_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:8000]


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _contract(folder, argv, name, port, port_env, health_path, identity, url_path, explicit_web) -> dict:
    if explicit_web is True and port is None:
        raise ContractError("This folder is marked as a web app but does not name a port.")
    web = bool(explicit_web) if explicit_web is not None else port is not None
    if not web:
        port = None
        health_path = None
        identity = None
    if health_path is not None:
        health_path = str(health_path)
        if not health_path.startswith("/"):
            health_path = "/" + health_path
    if identity is not None:
        identity = str(identity)
        if not identity or any(char in identity for char in "\n\r"):
            raise ContractError("Identity must be a single line.")
    import hashlib

    digest = hashlib.sha256(str(folder).encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "app"
    if not slug[0].isalpha():
        slug = "app-" + slug
    return {
        "name": str(name),
        "folder": str(folder),
        "argv": argv,
        "cwd": str(folder),
        "web": web,
        "port": port,
        "port_env": port_env or "PORT",
        "health_path": health_path if web else None,
        "identity": identity if web else None,
        "url_path": url_path if url_path.startswith("/") else "/" + str(url_path),
        "bundle_id": f"local.appdropper.{slug}-{digest}",
    }


def _normalize_argv(folder: Path, command) -> list[str]:
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ContractError("The start command is empty.")
    if any(any(char in part for char in "\n\r") for part in command):
        raise ContractError("The start command must be a single line.")
    argv = list(command)
    first = argv[0]
    path = Path(first)
    local = path if path.is_absolute() else (folder / path)
    if local.is_file():
        path = local.resolve()
        if os.access(path, os.X_OK):
            argv[0] = str(path)
        else:
            argv = ["/bin/bash", str(path)]
        return argv
    if first.startswith(".") or "/" in first:
        raise ContractError(f"Start script does not exist: {local}")
    return argv


def _coerce_port(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ContractError("Port must be a number.") from error
    if not 1 <= port <= 65535:
        raise ContractError("Port is out of range.")
    return port


def _port_in_text(text: str) -> int | None:
    for pattern in PORT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        port = int(match.group(1))
        if 1 <= port <= 65535:
            return port
    return None


def _port_in_env_files(folder: Path) -> int | None:
    for name in (".env", ".env.local", ".env.development"):
        path = folder / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("PORT="):
                port = _coerce_port(stripped.split("=", 1)[1].strip().strip("\"'"))
                if port:
                    return port
    return None


def _port_in_config_files(folder: Path) -> int | None:
    for relative in ("vite.config.ts", "vite.config.js", "vite.config.mjs", "next.config.js", "next.config.mjs"):
        path = folder / relative
        if not path.is_file():
            continue
        match = re.search(r"\bport\s*:\s*(\d{1,5})\b", path.read_text(encoding="utf-8", errors="replace"))
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return None


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
