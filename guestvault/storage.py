"""Private local settings, credentials and cross-process operation locks."""

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


def state_path():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GuestVault"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "guestvault"


def private(path):
    path = Path(path)
    if os.name == "nt":
        account = subprocess.check_output(["whoami"], text=True).strip()
        rights = "(OI)(CI)F" if path.is_dir() else "F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:{rights}"],
            check=True,
            capture_output=True,
        )
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        private(temp)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Store:
    def __init__(self, root=None):
        self.root = Path(root or state_path()).expanduser().resolve()
        if any(char in str(self.root) for char in "[]*?"):
            raise ValueError(
                "Use a state directory without glob characters so credential exclusions remain literal."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        private(self.root)

    def load(self):
        p = self.root / "config.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save(self, config):
        atomic_json(self.root / "config.json", config)

    def save_password(self, password):
        if len(password) < 12 or "\n" in password or "\r" in password:
            raise ValueError("Use a password of at least 12 characters without line breaks.")
        folder = self.root / "secrets"
        folder.mkdir(exist_ok=True)
        private(folder)
        fd, temp = tempfile.mkstemp(dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(password)
        private(temp)
        os.replace(temp, folder / "repository-password")

    def password(self):
        if (self.load() or {}).get("password_required", True) is False:
            return ""
        p = self.root / "secrets/repository-password"
        if not p.exists():
            raise ValueError("No scheduled-backup password saved. Configure credentials first.")
        return p.read_text(encoding="utf-8").rstrip("\r\n")

    @contextmanager
    def lock(self, name="operation.lock"):
        with (self.root / name).open("a+b") as stream:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    stream.write(b"0")
                    stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("Another GuestVault operation is already running.") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
