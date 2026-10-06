"""Use the established restic format rather than inventing backup cryptography."""

import bz2
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.19.1"
CHECKSUMS = {
    "linux_amd64.bz2": "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
    "linux_arm64.bz2": "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465",
    "darwin_amd64.bz2": "c38d579622cf602f665234c5a8c315030b6cf70656028fe6dc29a786b60e5f35",
    "darwin_arm64.bz2": "7be0a144ccc377880f294204aa271d76e4b79554b42a751151d425ce6ebac143",
    "windows_amd64.zip": "da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d",
}


def install(root):
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine()
    )
    suffix = f"{system}_{arch}.{'zip' if system == 'windows' else 'bz2'}"
    if suffix not in CHECKSUMS:
        raise ValueError("Install restic >=0.19.1 yourself for this platform and put it on PATH.")
    filename = f"restic_{VERSION}_{suffix}"
    url = f"https://github.com/restic/restic/releases/download/v{VERSION}/{filename}"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(100 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != CHECKSUMS[suffix]:
        raise ValueError("Restic download checksum mismatch; refusing installation.")
    if system == "windows":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [i for i in archive.infolist() if i.filename.endswith(".exe")]
            if len(entries) != 1:
                raise ValueError("Unexpected restic download contents.")
            binary = archive.read(entries[0])
    else:
        binary = bz2.decompress(data)
    folder = Path(root) / "bin"
    folder.mkdir(exist_ok=True)
    target = folder / ("restic.exe" if system == "windows" else "restic")
    temporary = target.with_suffix(".partial")
    temporary.write_bytes(binary)
    temporary.chmod(0o700)
    os.replace(temporary, target)
    return str(target)


class Restic:
    def __init__(self, root, executable=None, progress=None):
        self.root = Path(root)
        bundled = self.root / "bin" / ("restic.exe" if os.name == "nt" else "restic")
        self.executable = executable or (
            str(bundled) if bundled.exists() else shutil.which("restic")
        )
        if not self.executable:
            raise ValueError("Restic is missing. Run guestvault setup, or install restic >=0.19.1.")
        version = subprocess.check_output([self.executable, "version"], text=True)
        match = re.search(r"restic (\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (0, 19, 1):
            raise ValueError("Restic >=0.19.1 is required for this tool's metadata support.")
        self.progress = progress or (lambda message: None)

    def run(self, repository, password, arguments, from_password=None):
        if (
            arguments
            and arguments[0] == "init"
            and password != ""
            and (len(password) < 12 or "\n" in password or "\r" in password)
        ):
            raise ValueError(
                "New encrypted repositories and exports require a password of at least 12 characters without line breaks."
            )
        env, flags = credentials(password)
        source_flags = []
        if from_password is not None:
            if from_password == "":
                source_flags.append("--from-insecure-no-password")
            else:
                env["RESTIC_FROM_PASSWORD"] = from_password
        command = [
            self.executable,
            "--repo",
            str(repository),
            "--cache-dir",
            str(self.root / "cache"),
            "--json",
            "--compression",
            "auto",
            *flags,
            *map(str, arguments),
            *source_flags,
        ]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        events, tail = [], []
        try:
            for line in process.stdout:
                line = line.strip()
                tail = (tail + [line])[-20:]
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    item = {"message": line}
                # Snapshot lists are one JSON array; backup produces a JSON event stream.
                if not isinstance(item, dict) or item.get("message_type") != "status":
                    events.append(item)
                self.progress(item)
            code = process.wait()
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            process.stdout.close()
        if code:
            label = (
                "Backup incomplete: some files could not be read" if code == 3 else "Restic failed"
            )
            raise ValueError(f"{label} (exit {code}). " + "\n".join(tail)[-4000:])
        return events

    def snapshots(self, repository, password):
        events = self.run(repository, password, ["snapshots"])
        return next((e for e in events if isinstance(e, list)), [])


def credentials(password):
    """Empty passwords require explicit restic flags, never a password environment variable."""
    if not isinstance(password, str):
        raise ValueError("A password or an explicit empty password is required.")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("RESTIC_") or k in {"RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD"}
    }
    if password == "":
        return env, ["--insecure-no-password"]
    env["RESTIC_PASSWORD"] = password
    return env, []
