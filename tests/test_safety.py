import datetime as dt
import io
import json
import tarfile
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from guestvault import bundle, scheduler
from guestvault.engine import configure
from guestvault.storage import Store
from guestvault.web import App, make_server


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/tmp/escape",
        "repository/../escape",
        "repository\\..\\escape",
        "repository/C:/escape",
    ],
)
def test_bundle_traversal_refused(tmp_path, name):
    path = tmp_path / "bad.vmbackup"
    with tarfile.open(path, "w") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        with bundle.unpack(path, tmp_path):
            pass


def test_container_symlinks_refused(tmp_path):
    path = tmp_path / "bad.vmbackup"
    with tarfile.open(path, "w") as archive:
        entry = tarfile.TarInfo("repository/config")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/etc/passwd"
        archive.addfile(entry)
    with pytest.raises(ValueError, match="links"):
        with bundle.unpack(path, tmp_path):
            pass


def test_repository_cannot_enclose_source_and_credentials_are_private(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    store = Store(tmp_path / "state")
    with pytest.raises(ValueError, match="parent"):
        configure(store, "folders", [source], tmp_path)
    store.save_password("synthetic-private-password")
    assert (store.root / "secrets/repository-password").stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    with store.lock(), pytest.raises(ValueError, match="already running"):
        with store.lock():
            pass


def test_scheduler_failed_run_preserves_last_success(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    plan = {"scheduled": True, "last_success": "old-success", "next_run": None}
    store.save(plan)
    assert scheduler.due(plan)
    monkeypatch.setattr(
        scheduler, "Engine", lambda *a, **k: (_ for _ in ()).throw(ValueError("synthetic failure"))
    )
    result = scheduler.tick(store)
    assert "error" in result
    assert store.load()["last_success"] == "old-success"
    assert not scheduler.due(store.load())
    assert not json.loads((store.root / "activity.json").read_text())["success"]
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)
    assert scheduler.due(store.load(), now)


def test_loopback_api_auth_host_and_origin(tmp_path, restic_binary):
    app = App(Store(tmp_path / "state"), restic_binary)
    server = make_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen(url + "/api/status")
        assert exc.value.code == 401
        auth = {"Authorization": "Bearer " + app.token}
        with urlopen(Request(url + "/api/status", headers=auth)) as response:
            assert json.load(response)["doctor"]["os"] == "Linux"
        for headers in [
            {**auth, "Host": "evil.example"},
            {**auth, "Origin": "https://evil.example"},
        ]:
            with pytest.raises(HTTPError) as exc:
                urlopen(Request(url + "/api/status", headers=headers))
            assert exc.value.code == 403
        with urlopen(url + "/") as response:
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
