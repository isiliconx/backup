"""Portable tar container holding one independently encrypted restic repository."""

import io
import json
import os
import re
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

FORMAT = "guestvault-restic-v1"
HEX = re.compile(r"^[0-9a-f]{64}$")


def valid_name(name):
    if "\\" in name or ":" in name or "\x00" in name:
        return False
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in {".", ".."} for part in name.split("/")):
        return False
    parts = p.parts
    if name == "bundle.json" or name == "repository/config":
        return True
    if not parts or parts[0] != "repository":
        return False
    if len(parts) == 1:
        return True
    if parts[1] not in {"data", "index", "keys", "snapshots"}:
        return False
    if len(parts) == 2:
        return True
    if parts[1] == "data":
        return (len(parts) == 3 and re.fullmatch(r"[0-9a-f]{2}", parts[2]) is not None) or (
            len(parts) == 4 and HEX.fullmatch(parts[3]) is not None and parts[2] == parts[3][:2]
        )
    return len(parts) == 3 and HEX.fullmatch(parts[2]) is not None


def write(repository, snapshot, destination):
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("The export file already exists. Choose a new filename.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=destination.parent, prefix=".guestvault-", suffix=".partial")
    os.close(fd)
    try:
        with tarfile.open(temp, "w", format=tarfile.PAX_FORMAT) as archive:
            blob = json.dumps({"format": FORMAT, "snapshot": snapshot}).encode()
            info = tarfile.TarInfo("bundle.json")
            info.size, info.mode = len(blob), 0o600
            archive.addfile(info, io.BytesIO(blob))
            root = Path(repository)
            for p in sorted(root.rglob("*")):
                name = "repository/" + p.relative_to(root).as_posix()
                if "locks" in p.relative_to(root).parts:
                    continue
                if not valid_name(name) or p.is_symlink():
                    raise ValueError(f"Unexpected encrypted repository entry: {name}")
                if p.is_file():
                    info = archive.gettarinfo(str(p), arcname=name)
                    # Outer container reveals only encrypted object identifiers, not local identities.
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o600
                    with p.open("rb") as stream:
                        archive.addfile(info, stream)
        with open(temp, "r+b") as stream:
            os.fsync(stream.fileno())
        # Publish only a finished file, atomically and without overwriting an existing name.
        # The temporary file is on the same filesystem, so this needs no second full-size copy.
        if os.name == "nt":
            # Windows rename fails if the destination exists and also works on FAT/exFAT.
            os.rename(temp, destination)
        else:
            os.link(temp, destination)
        return str(destination)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextmanager
def unpack(path, work_dir):
    path = Path(path).expanduser().resolve()
    limit = path.stat().st_size
    with tempfile.TemporaryDirectory(prefix="import-", dir=work_dir) as temp:
        root = Path(temp)
        manifest = None
        seen = set()
        total = 0
        with tarfile.open(path, "r|") as archive:
            for count, entry in enumerate(archive):
                name = entry.name
                if count > 1_000_000 or name in seen or not valid_name(name):
                    raise ValueError("Invalid or duplicate path in backup bundle.")
                seen.add(name)
                if not (entry.isfile() or entry.isdir()) or entry.sparse is not None:
                    raise ValueError("Backup containers may not contain links or special files.")
                total += entry.size
                if entry.size < 0 or total > limit:
                    raise ValueError("Invalid backup container size.")
                if name == "bundle.json":
                    if not entry.isfile() or entry.size > 65536:
                        raise ValueError("Invalid backup descriptor.")
                    stream = archive.extractfile(entry)
                    manifest = json.load(stream)
                    if manifest.get("format") != FORMAT or not HEX.fullmatch(
                        manifest.get("snapshot", "")
                    ):
                        raise ValueError("Unsupported backup format or snapshot identifier.")
                    continue
                target = root.joinpath(*PurePosixPath(name).parts)
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(entry) as source, target.open("xb") as destination:
                        import shutil

                        shutil.copyfileobj(source, destination, length=4 * 1024 * 1024)
                    target.chmod(0o600)
        if manifest is None or not (root / "repository/config").is_file():
            raise ValueError("Incomplete backup bundle.")
        yield root / "repository", manifest
