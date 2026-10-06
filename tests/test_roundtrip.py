import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from guestvault import bundle
from guestvault.engine import Engine, configure
from guestvault.storage import Store

PASSWORD = "synthetic-test-password-only"


def restored_path(target, source):
    return target / Path(source).as_posix().lstrip("/")


@pytest.fixture
def completed(tmp_path, restic_binary):
    source = tmp_path / "original"
    source.mkdir()
    profile = source / ".browser-profile"
    profile.mkdir()
    secret = profile / "Cookies"
    secret.write_bytes(b"synthetic-cookie-data-never-a-real-login" * 400)
    secret.chmod(0o640)
    os.utime(secret, ns=(1700000000000000000, 1700000000123456789))
    os.link(secret, profile / "Cookies-hardlink")
    (source / "profile-link").symlink_to(".browser-profile")
    with (source / "sparse-disk").open("wb") as stream:
        stream.write(b"header")
        stream.seek(4 * 1024 * 1024)
        stream.write(b"end")
    try:
        os.setxattr(secret, "user.guestvault_test", b"preserve-this-metadata")
    except OSError:
        pass
    store = Store(source / "guestvault-state")
    configure(store, "folders", [source], tmp_path / "repo")
    store.save_password(PASSWORD)
    engine = Engine(store, restic_binary)
    export = tmp_path / "portable.vmbackup"
    result = engine.backup(PASSWORD, export)
    return source, store, engine, export, result


def test_wipe_and_restore_without_original_repository(completed, tmp_path, restic_binary):
    source, store, engine, export, result = completed
    expected = (source / ".browser-profile/Cookies").read_bytes()
    expected_sparse = hashlib.sha256((source / "sparse-disk").read_bytes()).hexdigest()
    sparse_size = (source / "sparse-disk").stat().st_size
    # Only the exported file and the externally remembered password survive this simulated wipe.
    shutil.rmtree(source)
    shutil.rmtree(tmp_path / "repo")
    replacement = Engine(Store(tmp_path / "replacement-tool"), restic_binary)
    target = tmp_path / "recovered"
    replacement.restore(export, target, PASSWORD)
    tree = restored_path(target, source)
    cookies = tree / ".browser-profile/Cookies"
    assert cookies.read_bytes() == expected
    assert stat.S_IMODE(cookies.stat().st_mode) == 0o640
    assert cookies.stat().st_mtime_ns == 1700000000123456789
    assert cookies.stat().st_ino == (tree / ".browser-profile/Cookies-hardlink").stat().st_ino
    assert (tree / "profile-link").is_symlink()
    assert os.readlink(tree / "profile-link") == ".browser-profile"
    assert hashlib.sha256((tree / "sparse-disk").read_bytes()).hexdigest() == expected_sparse
    assert (tree / "sparse-disk").stat().st_size == sparse_size
    assert not (tree / "guestvault-state/secrets").exists()
    assert not (tree / "guestvault-state/config.json").exists()
    assert (tree / "guestvault-state/recovery/current/recovery.json").exists()
    try:
        assert os.getxattr(cookies, "user.guestvault_test") == b"preserve-this-metadata"
    except OSError:
        pass
    assert b"synthetic-cookie-data" not in export.read_bytes()
    assert PASSWORD.encode() not in export.read_bytes()


def test_wrong_password_and_existing_target_are_refused(completed, tmp_path):
    source, store, engine, export, result = completed
    target = tmp_path / "new-target"
    with pytest.raises(ValueError):
        engine.restore(export, target, "wrong-password")
    assert not target.exists()
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("do not overwrite")
    with pytest.raises(ValueError, match="empty"):
        engine.restore(export, target, PASSWORD)
    assert marker.read_text() == "do not overwrite"
    with pytest.raises(ValueError, match="running OS"):
        engine.restore(export, "/", PASSWORD)
    with pytest.raises(ValueError, match="destination"):
        engine.restore(export, "", PASSWORD)


def test_symlink_target_and_duplicate_export_refused(completed, tmp_path):
    source, store, engine, export, result = completed
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual)
    with pytest.raises(ValueError, match="symlink"):
        engine.restore(export, link / "child", PASSWORD)
    before = hashlib.sha256(export.read_bytes()).digest()
    with pytest.raises(ValueError, match="already exists"):
        engine.export(result["snapshot"], export, PASSWORD)
    assert hashlib.sha256(export.read_bytes()).digest() == before


def test_ciphertext_corruption_rejected_before_restore(completed, tmp_path):
    source, store, engine, export, result = completed
    with bundle.unpack(export, engine.work) as (repository, descriptor):
        pack = next((repository / "data").rglob("*"))
        while not pack.is_file():
            pack = next(pack.iterdir())
        data = bytearray(pack.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pack.write_bytes(data)
        corrupt = tmp_path / "corrupt.vmbackup"
        bundle.write(repository, descriptor["snapshot"], corrupt)
    target = tmp_path / "untouched"
    with pytest.raises(ValueError):
        engine.restore(corrupt, target, PASSWORD)
    assert not target.exists()


def test_second_snapshot_changes_and_partial_backup_not_successful(completed, monkeypatch):
    source, store, engine, export, result = completed
    (source / "new-file").write_text("new contents")
    second = engine.backup(PASSWORD)
    assert second["snapshot"] != result["snapshot"]
    assert len(engine.snapshots(PASSWORD)) == 2
    previous = store.load()["last_success"]
    original = engine.restic.run

    def failed(repository, password, arguments, **kwargs):
        if arguments[0] == "backup":
            raise ValueError("Backup incomplete (exit 3)")
        return original(repository, password, arguments, **kwargs)

    monkeypatch.setattr(engine.restic, "run", failed)
    with pytest.raises(ValueError, match="incomplete"):
        engine.backup(PASSWORD)
    assert store.load()["last_success"] == previous
