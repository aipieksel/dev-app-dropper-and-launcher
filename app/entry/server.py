from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
import json
import os
import secrets
import signal
import threading
import urllib.parse
from business import __version__
from business.launcher_runtime import ContractError, decide_port
from business.session import App

UI = Path(__file__).parents[1] / "view/web"


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def make_handler(app, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, status=200, content_type="application/json", extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trusted(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def authorized(self):
            return secrets.compare_digest(self.headers.get("X-App-Dropper-Token", ""), token)

        def same_origin(self):
            origin = self.headers.get("Origin")
            port = self.server.server_address[1]
            return origin in (None, f"http://127.0.0.1:{port}")

        def do_GET(self):
            if not self.trusted():
                return self.send({"error": "Untrusted host."}, 403)
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path == "/api/health":
                return self.send({"app": "app-dropper", "version": __version__, "pid": os.getpid()})
            if path.startswith("/api/"):
                if not self.authorized():
                    return self.send({"error": "Unauthorized."}, 403)
                try:
                    if path == "/api/state":
                        return self.send(app.public_state())
                except ValueError as error:
                    return self.send({"error": str(error)}, 400)
                return self.send({"error": "Not found."}, 404)
            files = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
            if path == "/bootstrap.js":
                script = "window.APP_DROPPER_TOKEN=" + json.dumps(token) + ";"
                return self.send(script.encode(), content_type="text/javascript")
            name = files.get(path)
            if not name or not (UI / name).is_file():
                return self.send({"error": "Not found."}, 404)
            if name.endswith(".css"):
                mime = "text/css"
            elif name.endswith(".js"):
                mime = "text/javascript"
            else:
                mime = "text/html; charset=utf-8"
            return self.send((UI / name).read_bytes(), content_type=mime)

        def do_POST(self):
            if not self.trusted() or not self.same_origin() or not self.authorized():
                return self.send({"error": "This action must come from App Dropper."}, 403)
            path = urllib.parse.urlsplit(self.path).path
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send({"error": "JSON required."}, 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise ValueError("Request is too large or empty.")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object.")
                if path == "/api/create":
                    return self.send(app.create_from_folder(data.get("path")))
                if path == "/api/open-folder":
                    return self.send(app.open_folder(data.get("which")))
                return self.send({"error": "Not found."}, 404)
            except ValueError as error:
                return self.send({"error": str(error)}, 400)
            except Exception:
                return self.send({"error": "The operation could not finish."}, 500)

    return Handler


def serve(port, root, token):
    app = App(root)
    server = LocalServer(("127.0.0.1", port), make_handler(app, token))
    server.daemon_threads = True
    return server


def preferred_port():
    raw = os.environ.get("CONVERTO_PORT", "8777")
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = 8777
    if 1 <= port <= 65535:
        return port
    return 8777


def run():
    root = os.environ.get("APP_DROPPER_DATA_DIR") or os.environ.get(
        "CONVERTO_DATA_DIR",
        str(Path.home() / "Library/Application Support/App Dropper"),
    )
    try:
        decision = decide_port(preferred_port(), "/api/health", "app-dropper")
    except ContractError as error:
        raise SystemExit(str(error))
    port = int(decision["port"])
    if decision["action"] == "reuse":
        print(f"App Dropper ready on http://127.0.0.1:{port}", flush=True)
        return
    token = secrets.token_urlsafe(32)
    try:
        server = serve(port, root, token)
    except OSError as error:
        raise SystemExit(f"Could not listen on {port}: {error}")

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"App Dropper ready on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
