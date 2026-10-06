"""Sector-for-sector images from Linux rescue media; never image a mounted disk."""

import hashlib
import json
import os
import platform
import stat
import subprocess
import tempfile
from pathlib import Path

from . import bundle
from .engine import local_repository
from .platforms import elevated


def offline_device(device):
    if platform.system() != "Linux" or not elevated():
        raise ValueError("Disk imaging requires root in a Linux rescue environment.")
    device = Path(device).resolve()
    if not stat.S_ISBLK(device.stat().st_mode):
        raise ValueError("Choose a real block device, e.g. /dev/vda. Regular files are refused.")
    data = subprocess.check_output(
        ["lsblk", "--json", "--bytes", "--output", "PATH,TYPE,SIZE,MOUNTPOINTS", str(device)],
        text=True,
    )
    rows = json.loads(data)["blockdevices"]
    swap = subprocess.check_output(
        ["swapon", "--show", "--noheadings", "--raw", "--output", "NAME"], text=True
    ).splitlines()

    def check(items):
        for item in items:
            if any(item.get("mountpoints") or []) or item["path"] in swap:
                raise ValueError(
                    f"{item['path']} is mounted or used as swap. Boot rescue media and unmount it first."
                )
            check(item.get("children", []))

    check(rows)
    if len(rows) != 1 or rows[0]["type"] != "disk":
        raise ValueError("Choose a whole disk, not a partition or device-mapper volume.")
    return str(device), int(rows[0]["size"])


def backup(engine, device, repository, password, destination=None):
    device, size = offline_device(device)
    # Keep an exclusive block-device claim while dd reads it, preventing mounts during capture.
    with engine.store.lock(), os.fdopen(os.open(device, os.O_RDONLY | os.O_EXCL), "rb"):
        local = local_repository(repository)
        if local and not (local / "config").exists():
            engine.restic.run(repository, password, ["init"])
        events = engine.restic.run(
            repository,
            password,
            [
                "backup",
                "--stdin-from-command",
                "--stdin-filename",
                "disk.raw",
                "--tag",
                "guestvault-disk-image",
                "--tag",
                f"guestvault-disk-bytes={size}",
                "--",
                "dd",
                f"if={device}",
                "bs=4M",
                "status=none",
            ],
        )
        summary = next(
            (e for e in events if isinstance(e, dict) and e.get("message_type") == "summary"), None
        )
        if not summary or not summary.get("snapshot_id"):
            raise ValueError("Disk image backup did not produce a snapshot.")
        engine.restic.run(repository, password, ["check", "--read-data"])
        exported = (
            engine._export(repository, password, summary["snapshot_id"], destination)
            if destination
            else None
        )
        return {"snapshot": summary["snapshot_id"], "disk_bytes": size, "bundle": exported}


def restore(engine, path, device, erase_device, password):
    device, capacity = offline_device(device)
    if not erase_device or str(Path(erase_device).resolve()) != device:
        raise ValueError("Disk restore erases the target. Repeat its exact path in --erase-device.")
    with engine.store.lock(), bundle.unpack(path, engine.work) as (repository, descriptor):
        snapshot = engine._verify(repository, descriptor, password)
        if "guestvault-disk-image" not in snapshot.get("tags", []):
            raise ValueError("This bundle contains files, not a whole-disk image.")
        sizes = [
            int(t.split("=", 1)[1])
            for t in snapshot["tags"]
            if t.startswith("guestvault-disk-bytes=")
        ]
        if len(sizes) != 1 or capacity != sizes[0]:
            raise ValueError("Use a destination disk with exactly the saved disk's byte size.")
        # Verify the raw object's recorded size before touching any sectors.
        entries = engine.restic.run(
            repository, password, ["ls", descriptor["snapshot"], "/disk.raw"]
        )
        files = [
            e
            for e in entries
            if isinstance(e, dict) and e.get("type") == "file" and e.get("path") == "/disk.raw"
        ]
        if len(files) != 1 or files[0].get("size") != sizes[0]:
            raise ValueError("Image size does not match its encrypted metadata.")
        offline_device(device)  # Recheck after the potentially long integrity scan.
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        env["RESTIC_PASSWORD"] = password
        command = [
            engine.restic.executable,
            "--repo",
            str(repository),
            "--cache-dir",
            str(engine.store.root / "cache"),
            "dump",
            descriptor["snapshot"],
            "/disk.raw",
        ]
        # O_EXCL on a Linux block device refuses one claimed by a filesystem or another exclusive opener.
        fd = os.open(device, os.O_RDWR | os.O_EXCL)
        try:
            with os.fdopen(fd, "r+b", closefd=False) as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=errors)
                written = 0
                checksum = hashlib.sha256()
                try:
                    while chunk := process.stdout.read(4 * 1024 * 1024):
                        written += len(chunk)
                        if written > sizes[0]:
                            raise ValueError("Disk image exceeds its recorded size.")
                        output.write(chunk)
                        checksum.update(chunk)
                        engine.progress(
                            {"message": f"Writing offline disk: {written}/{sizes[0]} bytes"}
                        )
                    output.flush()
                    os.fsync(output.fileno())
                    code = process.wait()
                    errors.seek(0)
                    error = errors.read().decode(errors="replace")
                    if code or written != sizes[0]:
                        raise ValueError(
                            "Disk restore was interrupted; keep rescue media booted. "
                            + error[-2000:]
                        )
                    engine.progress(
                        {"message": "Verifying restored sectors by reading the disk back."}
                    )
                    output.seek(0)
                    readback, remaining = hashlib.sha256(), written
                    while remaining:
                        chunk = output.read(min(4 * 1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Could not read back the complete restored disk.")
                        readback.update(chunk)
                        remaining -= len(chunk)
                    if readback.digest() != checksum.digest():
                        raise ValueError(
                            "Restored disk failed checksum verification. Keep rescue media booted."
                        )
                except BaseException:
                    process.terminate()
                    process.wait()
                    raise
                finally:
                    process.stdout.close()
        finally:
            os.close(fd)
        return {
            "device": device,
            "bytes_restored": written,
            "status": "Disk sectors restored and readback verified. Shut down rescue media before booting the restored disk.",
        }
