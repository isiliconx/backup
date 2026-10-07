"""Authenticated loopback-only interface. No external services or telemetry."""

import datetime as dt
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import scheduler
from .engine import Engine, configure
from .platforms import doctor
from .restic import install


class App:
    def __init__(self, store, executable=None):
        self.store, self.executable = store, executable
        self.token = secrets.token_urlsafe(32)
        self.mutex = threading.Lock()
        self.job = {"state": "idle"}
        self.stop = threading.Event()

    def progress(self, event):
        with self.mutex:
            self.job["progress"] = event

    def launch(self, action, kind=None):
        with self.mutex:
            if self.job["state"] == "running":
                raise ValueError("Another operation is running. Wait for it to finish.")
            self.job = {
                "state": "running",
                "kind": kind,
                "started": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        def work():
            try:
                result = action()
                with self.mutex:
                    if isinstance(result, dict) and result.get("error"):
                        self.job.update({"state": "failed", "error": result["error"]})
                    else:
                        self.job.update({"state": "done", "result": result})
            except Exception as exc:
                with self.mutex:
                    self.job.update({"state": "failed", "error": str(exc)})

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def engine(self):
        return Engine(self.store, self.executable, self.progress)

    def schedule_loop(self):
        while not self.stop.wait(15):
            if not scheduler.is_running(self.store) and scheduler.due(self.store.load()):
                try:
                    self.launch(lambda: scheduler.tick(self.store, self.executable, self.progress))
                except ValueError:
                    pass

    def status(self):
        activity = self.store.root / "activity.json"
        with self.mutex:
            job = dict(self.job)
        try:
            engine = self.engine()
            restic = engine.restic.executable
        except (ValueError, OSError):
            restic = None
        return {
            "plan": self.store.load(),
            "doctor": doctor(),
            "job": job,
            "restic": restic,
            "activity": json.loads(activity.read_text()) if activity.exists() else None,
            "default_repository": str(self.store.root / "repository"),
            "home": str(Path.home()),
            "scheduler_running": scheduler.is_running(self.store),
        }

    def post(self, path, data):
        password = data.get("password")
        if path == "/api/setup":
            return self.launch(lambda: {"restic": install(self.store.root)})
        if path == "/api/configure":
            with self.mutex:
                if self.job["state"] == "running":
                    raise ValueError("Wait for the current operation before changing settings.")
            protected = data.get(
                "password_required", (self.store.load() or {}).get("password_required", True)
            )
            if not isinstance(protected, bool):
                raise ValueError("Choose whether to protect this backup with a password.")
            previous = self.store.load()
            saved = bool(
                previous
                and previous["repository"] == data["repository"]
                and (self.store.root / "secrets/repository-password").exists()
            )
            if protected and data.get("scheduled") and not data.get("remember") and not saved:
                raise ValueError("Scheduled backups require saving the password on this VM.")
            if (
                protected
                and data.get("remember")
                and not (saved and not password)
                and (
                    not isinstance(password, str)
                    or len(password) < 12
                    or "\n" in password
                    or "\r" in password
                )
            ):
                raise ValueError("Use a password of at least 12 characters without line breaks.")
            with self.store.lock():
                plan = configure(
                    self.store,
                    data.get("mode", "system"),
                    data.get("sources", []),
                    data["repository"],
                    data.get("interval", 24),
                    data.get("excludes", []),
                    data.get("export_directory"),
                    data.get("image_target"),
                    password_required=protected,
                )
                if protected and data.get("remember") and password:
                    self.store.save_password(password)
                plan["scheduled"] = bool(data.get("scheduled"))
                self.store.save(plan)
            return plan
        if path == "/api/backup":
            if (
                password is None
                and not data.get("saved")
                and (self.store.load() or {}).get("password_required", True)
            ):
                raise ValueError("Enter the repository password.")
            return self.launch(
                lambda: self.engine().backup(
                    password if not data.get("saved") else None, data.get("bundle")
                )
            )
        if path == "/api/snapshots":
            return self.launch(lambda: self.engine().snapshots(password))
        if path == "/api/export":
            return self.launch(
                lambda: self.engine().export(data["snapshot"], data["destination"], password)
            )
        if path in {"/api/restore", "/api/verify"}:
            if password is None:
                password = ""
            if not isinstance(password, str):
                raise ValueError("Enter a password or leave it empty for an unprotected backup.")
            if path == "/api/restore":
                return self.launch(
                    lambda: self.engine().restore(data["bundle"], data["target"], password)
                )
            return self.launch(lambda: self.engine().verify_bundle(data["bundle"], password))
        if path in {
            "/api/repository-snapshots",
            "/api/verify-repository",
            "/api/restore-repository",
        }:
            password = "" if password is None else password
            if not isinstance(password, str):
                raise ValueError("Enter a password or leave it empty for an unprotected backup.")
            if path == "/api/repository-snapshots":
                return self.launch(
                    lambda: self.engine().repository_snapshots(data["repository"], password),
                    "repository-snapshots",
                )
            if path == "/api/verify-repository":
                return self.launch(
                    lambda: self.engine().verify_repository(
                        data["repository"], data["snapshot"], password
                    )
                )
            return self.launch(
                lambda: self.engine().restore_repository(
                    data["repository"], data["snapshot"], data["target"], password
                )
            )
        raise ValueError("Unknown action.")


def make_server(app, port=0):
    static = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Passwords and session tokens must not appear in access logs.

        def safe_request(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                return False
            origin = self.headers.get("Origin")
            return not origin or origin == "http://" + expected

        def authenticated(self):
            header = self.headers.get("Authorization", "")
            return self.safe_request() and secrets.compare_digest(header, "Bearer " + app.token)

        def reply(self, status, body, content_type="application/json"):
            data = json.dumps(body).encode() if content_type == "application/json" else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if not self.safe_request():
                return self.reply(403, {"error": "Only same-origin loopback requests are allowed."})
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if parsed.path in assets:
                name, kind = assets[parsed.path]
                return self.reply(200, (static / name).read_bytes(), kind)
            if not self.authenticated():
                return self.reply(
                    401, {"error": "Open the session URL printed by guestvault serve."}
                )
            try:
                if parsed.path == "/api/status":
                    return self.reply(200, app.status())
                if parsed.path == "/api/browse":
                    folder = (
                        Path(parse_qs(parsed.query).get("path", [str(Path.home())])[0])
                        .expanduser()
                        .resolve()
                    )
                    entries = []
                    for p in sorted(
                        folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
                    ):
                        if p.is_dir() or p.suffix == ".vmbackup":
                            entries.append(
                                {"name": p.name, "path": str(p), "directory": p.is_dir()}
                            )
                        if len(entries) > 1000:
                            break
                    return self.reply(
                        200, {"path": str(folder), "parent": str(folder.parent), "entries": entries}
                    )
                return self.reply(404, {"error": "Unknown route."})
            except Exception as exc:
                return self.reply(400, {"error": str(exc)})

        def do_POST(self):
            if not self.authenticated():
                return self.reply(403, {"error": "Invalid session or origin."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < length <= 65536
                    or self.headers.get("Content-Type") != "application/json"
                ):
                    raise ValueError("Expected a JSON request no larger than 64 KiB.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object.")
                result = app.post(urlparse(self.path).path, data)
                return self.reply(200, result)
            except Exception as exc:
                return self.reply(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, executable=None, port=0, open_browser=True):
    app = App(store, executable)
    server = make_server(app, port)
    threading.Thread(target=app.schedule_loop, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/#{app.token}"
    print(f"GuestVault is running: {url}", flush=True)
    print(
        "Scheduling continues while this process runs. Use install-service for automatic startup.",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        app.stop.set()
        server.server_close()
