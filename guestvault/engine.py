"""Backup plans, portable exports and verification before restoration."""

import datetime as dt
import os
import platform
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import __version__, bundle, platforms
from .restic import Restic
from .storage import Store, atomic_json


def local_repository(repository):
    repository = str(repository)
    if re.match(r"^[a-zA-Z]:[\\/]", repository):
        return Path(repository).expanduser().resolve()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", repository):
        return None
    return Path(repository).expanduser().resolve()


def configure(
    store,
    mode,
    sources,
    repository,
    interval=24,
    excludes=None,
    export_directory=None,
    image_target=None,
):
    if mode not in {"system", "folders", "windows-image"}:
        raise ValueError("Unknown backup mode.")
    if mode in {"system", "windows-image"} and not platforms.elevated():
        raise ValueError(
            "System backup requires root/Administrator. Folders mode can run unprivileged."
        )
    if not 0.01 <= float(interval) <= 8760:
        raise ValueError("Backup interval must be between 0.01 and 8760 hours.")
    if mode == "windows-image":
        if (
            platform.system() != "Windows"
            or not image_target
            or not re.fullmatch(r"[A-Za-z]:", image_target)
        ):
            raise ValueError(
                "windows-image mode requires Windows and a separate image-target drive, e.g. E:."
            )
        system_drive = os.environ.get("SystemDrive", "C:")
        if image_target.lower() == system_drive.lower():
            raise ValueError("The system image target must be a separate disk.")
        sources = [str(Path(image_target + "\\") / "WindowsImageBackup")]
    elif mode == "system":
        sources = platforms.system_sources()
    else:
        sources = [str(Path(p).expanduser().resolve()) for p in sources or []]
        if not sources or any(not Path(p).exists() for p in sources):
            raise ValueError("Choose existing source files or directories.")
    repository = str(local_repository(repository) or repository)
    local = local_repository(repository)
    if local and any(char in str(local) for char in "[]*?"):
        raise ValueError("Use a repository path without glob characters for safe backup exclusion.")
    if export_directory and any(char in str(export_directory) for char in "[]*?"):
        raise ValueError(
            "Use an export directory without glob characters for safe backup exclusion."
        )
    # Never allow a repository to be, or to contain, any selected source.
    if local and any(Path(p) == local or local in Path(p).parents for p in sources):
        raise ValueError("The repository cannot be the source or a parent of a source.")
    plan = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "mode": mode,
        "sources": sources,
        "repository": repository,
        "interval_hours": float(interval),
        "excludes": excludes or [],
        "export_directory": str(Path(export_directory).expanduser().resolve())
        if export_directory
        else None,
        "image_target": image_target,
        "scheduled": False,
        "last_success": None,
        "next_run": None,
    }
    store.save(plan)
    return plan


class Engine:
    def __init__(self, store=None, executable=None, progress=None):
        self.store = store or Store()
        self.progress = progress or (lambda message: None)
        self.restic = Restic(self.store.root, executable, self.progress)
        self.work = self.store.root / "work"
        self.work.mkdir(exist_ok=True)

    def plan(self):
        plan = self.store.load()
        if not plan:
            raise ValueError("Configure a backup plan first.")
        return plan

    def initialize(self, plan, password):
        local = local_repository(plan["repository"])
        if local and not (local / "config").exists():
            self.restic.run(plan["repository"], password, ["init"])
        else:
            # Remote repositories must be initialized explicitly; auth failures must never trigger init.
            self.restic.snapshots(plan["repository"], password)

    def backup(self, password=None, export=None):
        password = password if password is not None else self.store.password()
        with self.store.lock():
            plan = self.plan()
            if plan["mode"] != "folders" and not platforms.elevated():
                raise ValueError("System backup requires root/Administrator.")
            self.initialize(plan, password)
            if plan["mode"] == "windows-image":
                self.progress({"message": "Creating a native Windows system image using wbadmin."})
                result = subprocess.run(
                    [
                        "wbadmin",
                        "start",
                        "backup",
                        f"-backupTarget:{plan['image_target']}",
                        f"-include:{os.environ.get('SystemDrive', 'C:')}",
                        "-allCritical",
                        "-vssCopy",
                        "-quiet",
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise ValueError(
                        "Windows image creation failed: " + (result.stdout + result.stderr)[-4000:]
                    )
            run_id = uuid.uuid4().hex
            recovery = self.store.root / "recovery/current"
            recovery.mkdir(parents=True, exist_ok=True)
            excluded = self.exclusions(plan)
            metadata = platforms.recovery_metadata(plan["mode"], plan["sources"], excluded)
            metadata.update(
                {
                    "guestvault_version": __version__,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            atomic_json(recovery / "recovery.json", metadata)
            arguments = [
                "backup",
                "--tag",
                "guestvault",
                "--tag",
                f"guestvault-os={platform.system()}",
                "--tag",
                f"guestvault-plan={plan['id']}",
                "--tag",
                f"guestvault-run={run_id}",
                "--with-atime",
            ]
            if platform.system() != "Windows" and plan["mode"] == "system":
                arguments += ["--one-file-system"]
            elif plan["mode"] == "system":
                arguments += ["--use-fs-snapshot"]
            for p in excluded:
                arguments += ["--exclude", p]
            arguments += [*plan["sources"], str(recovery)]
            events = self.restic.run(plan["repository"], password, arguments)
            summary = next(
                (e for e in events if isinstance(e, dict) and e.get("message_type") == "summary"),
                None,
            )
            if not summary or not summary.get("snapshot_id"):
                raise ValueError(
                    "Backup returned no snapshot ID; it has not been marked successful."
                )
            snapshot = summary["snapshot_id"]
            # No automatic forgetting: failed/new exports must never delete the last recoverable backup.
            self.restic.run(plan["repository"], password, ["check"])
            exported = None
            target = export
            if not target and plan.get("export_directory"):
                timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                target = str(
                    Path(plan["export_directory"]) / f"backup-{timestamp}-{snapshot[:8]}.vmbackup"
                )
            if target:
                exported = self._export(plan["repository"], password, snapshot, target)
            now = dt.datetime.now(dt.timezone.utc)
            plan.update(
                {
                    "last_success": now.isoformat(),
                    "last_snapshot": snapshot,
                    "last_bundle": exported,
                    "next_run": (now + dt.timedelta(hours=plan["interval_hours"])).isoformat(),
                }
            )
            self.store.save(plan)
            return {"snapshot": snapshot, "bundle": exported, "summary": summary}

    def exclusions(self, plan):
        excluded = platforms.system_excludes() if plan["mode"] == "system" else []
        excluded += ["*.vmbackup", ".guestvault-*.partial"]
        excluded += [
            str(self.store.root / p)
            for p in [
                "secrets",
                "config.json",
                "cache",
                "work",
                "operation.lock",
                "daemon.lock",
                "activity.json",
            ]
        ]
        local = local_repository(plan["repository"])
        if local:
            excluded.append(str(local))
        if plan.get("export_directory"):
            excluded.append(plan["export_directory"])
        return excluded + plan.get("excludes", [])

    def snapshots(self, password=None):
        plan = self.plan()
        return self.restic.snapshots(plan["repository"], password or self.store.password())

    def export(self, snapshot, destination, password=None):
        plan = self.plan()
        with self.store.lock():
            return self._export(
                plan["repository"], password or self.store.password(), snapshot, destination
            )

    def _export(self, repository, password, snapshot, destination):
        self.progress({"message": "Copying a complete encrypted snapshot into a portable bundle."})
        with tempfile.TemporaryDirectory(prefix="export-", dir=self.work) as temp:
            staging = Path(temp) / "repository"
            self.restic.run(
                staging,
                password,
                ["init", "--from-repo", repository, "--copy-chunker-params"],
                from_password=password,
            )
            self.restic.run(
                staging,
                password,
                ["copy", "--from-repo", repository, snapshot],
                from_password=password,
            )
            snapshots = self.restic.snapshots(staging, password)
            if len(snapshots) != 1:
                raise ValueError("A portable export must contain exactly one snapshot.")
            self.restic.run(staging, password, ["check", "--read-data"])
            # The independent destination repository has its own encryption key and snapshot ID.
            return bundle.write(staging, snapshots[0]["id"], destination)

    def verify_bundle(self, path, password):
        with self.store.lock(), bundle.unpack(path, self.work) as (repository, descriptor):
            return self._verify(repository, descriptor, password)

    def _verify(self, repository, descriptor, password):
        snapshots = self.restic.snapshots(repository, password)
        if len(snapshots) != 1 or snapshots[0]["id"] != descriptor["snapshot"]:
            raise ValueError("Backup descriptor does not match its encrypted snapshot.")
        self.progress({"message": "Verifying every encrypted data block before import."})
        self.restic.run(repository, password, ["check", "--read-data"])
        return snapshots[0]

    def restore(self, path, target, password):
        if not target or not str(target).strip():
            raise ValueError("Choose an empty restore destination.")
        raw_target = Path(target).expanduser().absolute()
        if any(p.is_symlink() for p in [raw_target, *raw_target.parents]):
            raise ValueError("Restore target cannot contain symlink components.")
        target = raw_target.resolve()
        protected = [Path(target.anchor), self.store.root, Path.home().resolve()]
        if target in protected or target in self.store.root.parents:
            raise ValueError(
                "Choose an empty restore folder or an offline mounted root; never the running OS root."
            )
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError(
                "Restore target must be an empty directory. Existing data will not be overwritten."
            )
        with self.store.lock(), bundle.unpack(path, self.work) as (repository, descriptor):
            snapshot = self._verify(repository, descriptor, password)
            tags = snapshot.get("tags", [])
            if "guestvault-disk-image" in tags:
                raise ValueError(
                    "This is a whole-disk image. Use restore-disk from Linux rescue media."
                )
            os_tag = next(
                (t.split("=", 1)[1] for t in tags if t.startswith("guestvault-os=")), None
            )
            if os_tag and os_tag != platform.system():
                raise ValueError(
                    f"This backup was made on {os_tag}; restore it on that OS to preserve metadata."
                )
            # Integrity verification can take a long time: check the destination again before writing.
            if any(p.is_symlink() for p in [raw_target, *raw_target.parents]):
                raise ValueError("Restore target cannot contain symlink components.")
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise ValueError("Restore target must still be empty after backup verification.")
            target.mkdir(parents=True, exist_ok=True)
            self.restic.run(
                repository,
                password,
                [
                    "restore",
                    descriptor["snapshot"],
                    "--target",
                    str(target),
                    "--sparse",
                    "--verify",
                    "--overwrite",
                    "never",
                ],
            )
            return {
                "target": str(target),
                "snapshot": descriptor["snapshot"],
                "status": "Files restored and verified. OS recovery needs the steps in docs/RECOVERY.md.",
            }
