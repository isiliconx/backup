import hashlib
import os
import shutil
from pathlib import Path

import pytest

from guestvault.engine import Engine, configure
from guestvault.storage import Store, state_path


@pytest.mark.parametrize("password", ["synthetic-cross-platform-password", ""])
def test_real_export_import_on_current_os(tmp_path, password):
    suffix = "restic.exe" if os.name == "nt" else "restic"
    executable = os.environ.get("GUESTVAULT_TEST_RESTIC") or str(state_path() / "bin" / suffix)
    if not Path(executable).exists():
        pytest.skip("Run guestvault setup first.")
    tmp_path = tmp_path.resolve()
    source = tmp_path / "original"
    source.mkdir()
    payload = b"cross-platform backup verification\x00" * 5000
    (source / "example.txt").write_bytes(payload)
    store = Store(tmp_path / "state")
    repository = tmp_path / "repository"
    configure(store, "folders", [source], repository, password_required=bool(password))
    engine = Engine(store, executable)
    archive = tmp_path / "portable.vmbackup"
    result = engine.backup(password, archive)
    direct = tmp_path / "restored-direct"
    engine.restore_repository(str(repository), result["snapshot"], direct, password)
    assert next(direct.rglob("example.txt")).read_bytes() == payload
    shutil.rmtree(source)
    shutil.rmtree(repository)
    replacement = Engine(Store(tmp_path / "replacement"), executable)
    destination = tmp_path / "restored"
    replacement.restore(archive, destination, password)
    restored = list(destination.rglob("example.txt"))
    assert len(restored) == 1
    assert hashlib.sha256(restored[0].read_bytes()).digest() == hashlib.sha256(payload).digest()
