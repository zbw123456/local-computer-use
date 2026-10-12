"""Loopback-only UI. Authenticated JSON endpoints, same-origin checks, no CORS."""
import hmac
import json
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATIC = Path(__file__).parent / "static"


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, engine, demo=False, model="", configured=False):
        self.engine = engine
        self.token = secrets.token_urlsafe(32)
        self.demo = demo
        self.model = model
        self.configured = configured
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def launch_url(self):
        return self.origin + "/#token=" + self.token


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalComputerUse"

    def log_message(self, *_args):
        pass  # Request URLs and authentication tokens must never enter logs.

    def respond(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def guard(self, auth=True):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.respond(403, {"error": "Host rejected"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self.respond(403, {"error": "Origin rejected"})
            return False
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            self.respond(403, {"error": "Cross-origin request rejected"})
            return False
        if auth and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + self.server.token).encode()):
            self.respond(401, {"error": "连接口令无效。请使用启动时显示的完整链接。"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.guard(auth=path.startswith("/api/")):
            return
        if path == "/api/state":
            self.respond(200, {**self.server.engine.state(), "demo": self.server.demo,
                               "model": self.server.model, "configured": self.server.configured})
        elif path == "/api/windows":
            self.invoke(lambda: self.server.engine.inspect("windows"))
        elif path == "/api/doctor":
            self.invoke(lambda: self.server.engine.inspect("doctor"))
        elif path in {"/", "/index.html", "/app.js", "/style.css"}:
            name = "index.html" if path == "/" else path[1:]
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.respond(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.guard():
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.respond(415, {"error": "JSON required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 24000:
                raise ValueError("请求大小无效。")
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象。")
        except (ValueError, TimeoutError, OSError):
            self.respond(400, {"error": "请求格式无效或超时。"})
            return
        path = urlsplit(self.path).path
        engine = self.server.engine
        routes = {
            "/api/start": lambda: engine.start(data.get("task"), data.get("target")),
            "/api/stop": engine.stop,
            "/api/approve": lambda: engine.decide(data.get("id"), True),
            "/api/reject": lambda: engine.decide(data.get("id"), False),
            "/api/observe": lambda: engine.inspect("observe", **self.target(data)),
            "/api/permissions": lambda: engine.inspect("request_permissions"),
        }
        if path not in routes:
            self.respond(404, {"error": "Not found"})
            return
        self.invoke(routes[path])

    @staticmethod
    def target(data):
        value = data.get("target")
        if not isinstance(value, dict) or set(value) != {"bundle_id", "window_id"}:
            raise ValueError("请选择窗口。")
        return value

    def invoke(self, operation):
        try:
            self.respond(200, operation())
        except Exception as error:
            self.respond(400, {"error": str(error)[:1000]})
