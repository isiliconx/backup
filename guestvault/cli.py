"""Command line entry point, including recovery without a browser."""

import argparse
import getpass
import json
import sys

from .engine import Engine, configure
from .platforms import doctor
from .restic import install
from .storage import Store


def progress(event):
    if isinstance(event, dict):
        if event.get("message_type") == "status":
            print(
                f"\rBackup: {100 * event.get('percent_done', 0):.1f}%",
                end="",
                file=sys.stderr,
                flush=True,
            )
        elif event.get("message") or event.get("error"):
            print(event.get("message") or event.get("error"), file=sys.stderr)


def parser():
    p = argparse.ArgumentParser(description="GuestVault: compressed backups from inside your VM")
    p.add_argument("--state", help="Private state directory (use the same one for all commands)")
    p.add_argument("--restic", help="Path to restic >=0.19.1")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Download checksum-pinned restic for this platform")
    commands.add_parser("doctor", help="Inspect privileges, mounts and recovery capabilities")
    c = commands.add_parser("configure", help="Save a backup plan")
    c.add_argument("--mode", choices=["system", "folders", "windows-image"], default="system")
    c.add_argument("--source", action="append", default=[])
    c.add_argument("--repository", required=True)
    c.add_argument("--exclude", action="append", default=[])
    c.add_argument("--every-hours", type=float, default=24)
    c.add_argument(
        "--export-directory", help="Optional folder for a standalone file after every backup"
    )
    c.add_argument("--image-target", help="Windows system image staging drive, e.g. E:")
    c.add_argument("--schedule", action="store_true", help="Enable the persisted interval schedule")
    c.add_argument(
        "--no-password",
        action="store_true",
        help="Create unprotected backups anyone with access can read",
    )
    c.add_argument(
        "--remember-password",
        action="store_true",
        help="Save password locally for unattended backups",
    )
    b = commands.add_parser("backup")
    b.add_argument("--bundle", help="Export this backup as a single .vmbackup file")
    b.add_argument("--use-saved-password", action="store_true")
    commands.add_parser("snapshots")
    for name in [
        "repository-snapshots",
        "verify-repository",
        "restore-repository",
        "init-repository",
    ]:
        remote = commands.add_parser(name, help="Use a repository directly without a portable file")
        remote.add_argument("--repository", required=True)
        remote.add_argument("--no-password", action="store_true")
        if name in {"verify-repository", "restore-repository"}:
            remote.add_argument("--snapshot", required=True)
        if name == "restore-repository":
            remote.add_argument("--target", required=True)
    e = commands.add_parser("export")
    e.add_argument("snapshot")
    e.add_argument("destination")
    e.add_argument("--use-saved-password", action="store_true")
    v = commands.add_parser("verify")
    v.add_argument("bundle")
    v.add_argument(
        "--no-password", action="store_true", help="Verify an unprotected backup without prompting"
    )
    r = commands.add_parser("restore")
    r.add_argument("bundle")
    r.add_argument("--target", required=True, help="Empty directory or empty offline root mount")
    r.add_argument(
        "--no-password", action="store_true", help="Import an unprotected backup without prompting"
    )
    image = commands.add_parser(
        "backup-disk", help="Image an unmounted whole disk from Linux rescue media"
    )
    image.add_argument("--device", required=True)
    image.add_argument("--repository", required=True)
    image.add_argument("--bundle")
    image.add_argument("--no-password", action="store_true")
    disk_restore = commands.add_parser(
        "restore-disk", help="Erase and restore an offline disk from an image bundle"
    )
    disk_restore.add_argument("bundle")
    disk_restore.add_argument("--device", required=True)
    disk_restore.add_argument("--no-password", action="store_true")
    disk_restore.add_argument(
        "--erase-device", required=True, help="Repeat the target path to authorize erasing it"
    )
    s = commands.add_parser("serve", help="Open the local backup and import interface")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--no-browser", action="store_true")
    d = commands.add_parser("daemon", help="Run the persisted interval schedule without the web UI")
    d.add_argument("--once", action="store_true", help="Run only if a scheduled backup is due")
    commands.add_parser(
        "install-service", help="Install/start the scheduler service for the current account"
    )
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = Store(args.state)
    try:
        if args.command == "setup":
            result = {"restic": install(store.root)}
        elif args.command == "doctor":
            result = doctor()
        elif args.command == "configure":
            if args.no_password and args.remember_password:
                raise ValueError("Use either --no-password or --remember-password.")
            if args.schedule and not args.remember_password and not args.no_password:
                raise ValueError("Scheduling requires --remember-password for unattended backups.")
            password = None
            if args.remember_password:
                password = getpass.getpass("Repository password (saved locally for scheduling): ")
                if password != getpass.getpass("Repeat password: "):
                    raise ValueError("Passwords do not match.")
                if len(password) < 12 or "\n" in password or "\r" in password:
                    raise ValueError("Use at least 12 characters without line breaks.")
            with store.lock():
                result = configure(
                    store,
                    args.mode,
                    args.source,
                    args.repository,
                    args.every_hours,
                    args.exclude,
                    args.export_directory,
                    args.image_target,
                    password_required=not args.no_password,
                )
                if password is not None:
                    store.save_password(password)
                result["scheduled"] = args.schedule
                store.save(result)
        elif args.command == "serve":
            from .web import serve

            serve(store, args.restic, args.port, not args.no_browser)
            return 0
        elif args.command == "daemon":
            from .scheduler import run, tick

            if args.once:
                result = tick(store, args.restic, progress)
            else:
                run(store, args.restic, progress)
                return 0
        elif args.command == "install-service":
            from .service import install as service_install

            result = {"installed": service_install(store, args.restic)}
        else:
            engine = Engine(store, args.restic, progress)

            def repository_password():
                if not engine.plan().get("password_required", True) or getattr(
                    args, "use_saved_password", False
                ):
                    return None
                return getpass.getpass("Repository password: ")

            def backup_password():
                return (
                    ""
                    if args.no_password
                    else getpass.getpass("Backup password (Enter for no password): ")
                )

            if args.command == "repository-snapshots":
                result = engine.repository_snapshots(args.repository, backup_password())
            elif args.command == "verify-repository":
                result = engine.verify_repository(args.repository, args.snapshot, backup_password())
            elif args.command == "restore-repository":
                result = engine.restore_repository(
                    args.repository, args.snapshot, args.target, backup_password()
                )
            elif args.command == "init-repository":
                if not args.repository.strip():
                    raise ValueError("Choose a backup repository.")
                result = engine.restic.run(args.repository, backup_password(), ["init"])
            elif args.command == "snapshots":
                result = engine.snapshots(repository_password())
            elif args.command == "backup":
                password = repository_password()
                result = engine.backup(password, args.bundle)
            elif args.command == "export":
                password = repository_password()
                result = engine.export(args.snapshot, args.destination, password)
            elif args.command == "verify":
                result = engine.verify_bundle(args.bundle, backup_password())
            elif args.command == "backup-disk":
                from .disks import backup as disk_backup

                result = disk_backup(
                    engine,
                    args.device,
                    args.repository,
                    "" if args.no_password else getpass.getpass("Repository password: "),
                    args.bundle,
                )
            elif args.command == "restore-disk":
                from .disks import restore as disk_restore

                result = disk_restore(
                    engine,
                    args.bundle,
                    args.device,
                    args.erase_device,
                    backup_password(),
                )
            else:
                result = engine.restore(args.bundle, args.target, backup_password())
        print(json.dumps(result, indent=2))
        return 1 if isinstance(result, dict) and result.get("error") else 0
    except Exception as exc:
        print(f"GuestVault: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nStopped. No backup has been marked successful by this interruption.", file=sys.stderr
        )
        return 130
