"""Real restic traffic against a disposable loopback REST backend (no cloud account needed)."""

import hashlib
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from guestvault import bundle
from guestvault.engine import Engine, configure
from guestvault.storage import Store


@pytest.fixture
def remote_repository(tmp_path):
    storage = tmp_path / "server-storage"
    storage.mkdir()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def repository_path(self):
            path = (storage / urlparse(self.path).path.lstrip("/")).resolve()
            assert storage in path.parents
            return path

        def reply(self, code, body=b"", kind="application/octet-stream", size=None):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body) if size is None else size))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            path = self.repository_path()
            self.reply(200, size=path.stat().st_size) if path.is_file() else self.reply(404)

        def do_GET(self):
            path = self.repository_path()
            if path.is_dir():
                files = [
                    {"name": p.name, "size": p.stat().st_size}
                    for p in path.iterdir()
                    if p.is_file()
                ]
                self.reply(200, json.dumps(files).encode(), "application/vnd.x.restic.rest.v2")
            elif path.is_file():
                data = path.read_bytes()
                if self.headers.get("Range"):
                    start, end = self.headers["Range"].removeprefix("bytes=").split("-")
                    data = data[int(start) : int(end) + 1 if end else None]
                    self.reply(206, data)
                else:
                    self.reply(200, data)
            else:
                self.reply(404)

        def do_POST(self):
            path = self.repository_path()
            if urlparse(self.path).query == "create=true":
                for name in ["data", "keys", "locks", "index", "snapshots"]:
                    (path / name).mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.rfile.read(int(self.headers["Content-Length"])))
            self.reply(200)

        def do_DELETE(self):
            self.repository_path().unlink(missing_ok=True)
            self.reply(200)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"rest:http://127.0.0.1:{server.server_port}/backup", storage
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("password", ["", "synthetic-remote-password"])
def test_streaming_remote_backup_and_restore_without_local_bundle(
    tmp_path, restic_binary, remote_repository, password, monkeypatch
):
    repository, server_storage = remote_repository
    source = tmp_path / "source"
    source.mkdir()
    payload = os.urandom(16 * 1024 * 1024)
    expected = hashlib.sha256(payload).digest()
    (source / ".profile").write_bytes(payload)
    store = Store(tmp_path / "client-state")
    configure(store, "folders", [source], repository, password_required=bool(password))
    engine = Engine(store, restic_binary)
    engine.restic.run(repository, password, ["init"])
    result = engine.backup(password)
    assert result["bundle"] is None
    assert not list(engine.work.iterdir())
    assert sum(p.stat().st_size for p in store.root.rglob("*") if p.is_file()) < len(payload) // 10
    assert not (store.root / "repository").exists()
    assert any((server_storage / "backup/data").iterdir())
    shutil.rmtree(source)
    shutil.rmtree(store.root)
    replacement = Engine(Store(tmp_path / "new-vm-state"), restic_binary)
    monkeypatch.setattr(
        bundle, "unpack", lambda *a: pytest.fail("Must not unpack a complete archive")
    )
    monkeypatch.setattr(
        bundle, "write", lambda *a: pytest.fail("Must not export a complete archive")
    )
    assert replacement.repository_snapshots(repository, password)[0]["id"] == result["snapshot"]
    assert replacement.verify_repository(repository, result["snapshot"], password)["id"]
    target = tmp_path / "recovered"
    replacement.restore_repository(repository, result["snapshot"], target, password)
    restored = next(target.rglob(".profile"))
    assert hashlib.sha256(restored.read_bytes()).digest() == expected
    assert not list(replacement.work.iterdir())
    assert (
        sum(p.stat().st_size for p in replacement.store.root.rglob("*") if p.is_file())
        < len(payload) // 10
    )
    with pytest.raises(ValueError, match="empty"):
        replacement.restore_repository(repository, result["snapshot"], target, password)
    with pytest.raises(ValueError, match="running OS"):
        replacement.restore_repository(repository, result["snapshot"], "/", password)
    with pytest.raises(ValueError):
        replacement.restore_repository(repository, "0" * 64, tmp_path / "unknown", password)
    assert not (tmp_path / "unknown").exists()
