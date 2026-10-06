import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from guestvault import disks
from guestvault.engine import Engine
from guestvault.storage import Store


def test_live_disk_rejected_before_read(monkeypatch):
    monkeypatch.setattr(disks, "elevated", lambda: True)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_mode=stat.S_IFBLK))
    monkeypatch.setattr(Path, "resolve", lambda self: self)
    data = {
        "blockdevices": [
            {
                "path": "/dev/fake",
                "type": "disk",
                "size": 4096,
                "mountpoints": ["/"],
                "children": [],
            }
        ]
    }
    monkeypatch.setattr(
        disks.subprocess,
        "check_output",
        lambda args, **k: json.dumps(data) if args[0] == "lsblk" else "",
    )
    with pytest.raises(ValueError, match="mounted"):
        disks.offline_device("/dev/fake")


def test_regular_files_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(disks, "elevated", lambda: True)
    path = tmp_path / "ordinary-file"
    path.write_bytes(b"safe")
    with pytest.raises(ValueError, match="Regular files"):
        disks.offline_device(path)


def test_raw_image_transport_and_readback(tmp_path, restic_binary, monkeypatch):
    # Exercise the real encryption, export, dump, write and readback pipeline using disposable files.
    # Only the device-discovery guard is substituted; no real disk is opened by this test.
    original = tmp_path / "fake-source.raw"
    payload = b"disk-header" + bytes(range(256)) * 16384 + b"disk-tail"
    original.write_bytes(payload)
    target = tmp_path / "fake-target.raw"
    target.write_bytes(b"0" * len(payload))
    monkeypatch.setattr(
        disks, "offline_device", lambda path: (str(Path(path).resolve()), Path(path).stat().st_size)
    )
    engine = Engine(Store(tmp_path / "state"), restic_binary)
    export = tmp_path / "disk.vmbackup"
    disks.backup(engine, original, str(tmp_path / "repository"), "synthetic-disk-password", export)
    original.unlink()
    with pytest.raises(ValueError, match="Repeat"):
        disks.restore(engine, export, target, None, "synthetic-disk-password")
    result = disks.restore(engine, export, target, target, "synthetic-disk-password")
    assert result["bytes_restored"] == len(payload)
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(payload).digest()
    with pytest.raises(ValueError, match="whole-disk"):
        engine.restore(export, tmp_path / "files-target", "synthetic-disk-password")
