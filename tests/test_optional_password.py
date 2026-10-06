import shutil
import threading
import time

import pytest

from guestvault import cli, scheduler
from guestvault.engine import Engine, configure
from guestvault.restic import credentials
from guestvault.storage import Store
from guestvault.web import App


def test_no_password_backup_export_wipe_import(tmp_path, restic_binary, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"compressible test contents\0" * 400000
    (source / ".hidden-profile").write_bytes(payload)
    store = Store(tmp_path / "state")
    repo = tmp_path / "repository"
    configure(store, "folders", [source], repo, password_required=False)
    engine = Engine(store, restic_binary)
    archive = tmp_path / "standalone.vmbackup"
    result = engine.backup(export=archive)
    assert result["summary"]["data_added_packed"] < len(payload) // 10
    assert len(engine.snapshots()) == 1
    assert store.password() == ""
    assert not (store.root / "secrets").exists()
    second = tmp_path / "second.vmbackup"
    engine.export(result["snapshot"], second, "")
    shutil.rmtree(source)
    shutil.rmtree(repo)
    replacement = Engine(Store(tmp_path / "replacement"), restic_binary)
    assert replacement.verify_bundle(archive, "")["id"]
    target = tmp_path / "restored"
    replacement.restore(archive, target, "")
    assert next(target.rglob(".hidden-profile")).read_bytes() == payload
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a: pytest.fail("Must not prompt"))
    assert (
        cli.main(
            [
                "--state",
                str(replacement.store.root),
                "--restic",
                restic_binary,
                "verify",
                str(second),
                "--no-password",
            ]
        )
        == 0
    )


def test_credentials_do_not_mix_password_environment_with_empty_password(monkeypatch):
    monkeypatch.setenv("RESTIC_PASSWORD", "inherited-secret-must-not-leak")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/unrelated/secret")
    env, flags = credentials("")
    assert not any(k.startswith("RESTIC_") for k in env)
    assert flags == ["--insecure-no-password"]


def test_api_no_password_schedule_and_preserve_existing_protection(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    app = App(Store(tmp_path / "state"))
    data = {
        "mode": "folders",
        "sources": [str(source)],
        "repository": str(tmp_path / "repo"),
        "password_required": False,
        "scheduled": True,
    }
    plan = app.post("/api/configure", data)
    assert plan["scheduled"] and not plan["password_required"]
    assert app.store.password() == ""
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/config").write_text("an initialized repository")
    with pytest.raises(ValueError, match="new repository"):
        app.post(
            "/api/configure",
            {
                **data,
                "password_required": True,
                "remember": True,
                "password": "synthetic-password-only",
            },
        )
    assert app.store.load() == plan
    with pytest.raises(ValueError, match="repository"):
        app.post("/api/configure", {**data, "repository": ""})


def test_encrypted_plan_rejects_empty_password(tmp_path, restic_binary):
    source = tmp_path / "source"
    source.mkdir()
    store = Store(tmp_path / "state")
    configure(store, "folders", [source], tmp_path / "repo")
    with pytest.raises(ValueError, match="requires"):
        Engine(store, restic_binary).backup("")
    assert not (tmp_path / "repo").exists()


def test_real_background_interval_without_credentials(tmp_path, restic_binary):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("first generation")
    store = Store(tmp_path / "state")
    plan = configure(
        store,
        "folders",
        [source],
        tmp_path / "repo",
        interval=0.01,
        password_required=False,
        export_directory=tmp_path / "exports",
    )
    plan["scheduled"] = True
    store.save(plan)
    stop = threading.Event()
    worker = threading.Thread(target=scheduler.run, args=(store, restic_binary, None, stop))
    worker.start()
    try:
        deadline = time.monotonic() + 30
        while not store.load().get("last_success") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert store.load().get("last_success")
        assert scheduler.is_running(store)
        first = store.load()["last_snapshot"]
        (source / "payload").write_text("second generation")
        plan = store.load()
        plan["next_run"] = "2000-01-01T00:00:00+00:00"
        store.save(plan)
        deadline = time.monotonic() + 30
        while store.load().get("last_snapshot") == first and time.monotonic() < deadline:
            time.sleep(0.1)
        assert store.load()["last_snapshot"] != first
        assert len(list((tmp_path / "exports").glob("*.vmbackup"))) == 2
        assert not (store.root / "secrets").exists()
    finally:
        stop.set()
        worker.join(30)
    assert not worker.is_alive()
    assert not scheduler.is_running(store)
