# GuestVault

Compressed, periodic backups with optional password protection that run **inside** a Windows, Linux or macOS VM. Export a self-contained `.vmbackup` file, keep it in cloud storage, and import it on a replacement machine using your backup password, if you chose one.

GuestVault has a local web interface, a CLI, an interval scheduler and OS service installation. It uses [restic](https://restic.net/) for compression, optional password protection, deduplication, metadata preservation and integrity verification. No accounts, telemetry or paid service are required.

## The recovery boundary

An in-guest application cannot promise “absolutely no difference.” It cannot read the hypervisor's saved memory, virtual TPM, VM configuration or external disks that the guest cannot see. Website sessions can expire independently of their saved cookies. Installing the tool in a fresh running OS and overwriting that OS is not a safe whole-machine restore.

This release provides these recovery paths:

| Mode | What is saved | How it is recovered |
| --- | --- | --- |
| Linux system files | Accessible files on mounted local filesystems, hidden profiles, installed application files, system configuration, ownership, permissions and supported extended metadata | Import onto prepared filesystems from rescue media, then repair mount configuration and bootloader as needed |
| Selected folders, all three OSes | Files, hidden files and supported metadata in the selected folders | Import into an empty folder on the same OS |
| Windows system files | Fixed-drive files through VSS; supported Windows security metadata when elevated | File recovery; **not** bare-metal Windows recovery |
| Windows system image | Native `wbadmin` image of the system drive and critical volumes, then a compressed backup of that image | Import the image and use Windows Recovery's System Image Recovery |
| macOS data files | `/Users`, `/Applications`, `/Library`, `/private`, with Full Disk Access and root permissions | File recovery; reinstall the compatible OS and recover applications separately. Sealed OS volumes and automated Migration Assistant recovery are not provided |
| Offline whole disk | Every readable sector, including partition tables, filesystems and boot files; no RAM | Run from Linux rescue media. Restore onto an unmounted disk of exactly the same size, with destructive target confirmation and readback checksum verification |

Live Linux/macOS file scans are not an atomic point-in-time image. Close browsers and stop databases/VM workloads, or back up offline. VSS handles Windows file snapshots but does not guarantee every application's transaction consistency. Unreadable files cause a failed backup status rather than a false success. OS recovery requires compatible architecture, firmware, drivers and disk layout. Test a recovery before relying on any backup.

## Install inside your VM

Requires Python 3.10 or newer. The Python package has no runtime library dependencies. `setup` downloads restic **0.19.1** from its official GitHub release and verifies a checksum embedded in this repository. An existing restic 0.19.1 or newer can also be used.

```sh
git clone https://github.com/isiliconx/backup.git
cd backup
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/guestvault setup
.venv/bin/guestvault serve
```

Windows PowerShell:

```powershell
git clone https://github.com/isiliconx/backup.git
cd backup
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\guestvault.exe setup
.\.venv\Scripts\guestvault.exe serve
```

Use **Back up** to choose your scope, a repository and an optional portable export folder. Password protection is an explicit checkbox. Save the plan, then select **Back up now**. Use **Import & restore** to browse for a downloaded `.vmbackup` file, enter its password if protected (otherwise leave it empty), verify it and restore to an empty destination.

If you enable password protection, use a long, unique password and keep it **outside** the VM. Without it, the encrypted file cannot be restored. Never put backups, passwords or cloud credentials in this GitHub repository.

### Optional password

Leave **Protect backups with a password** unchecked to create backups without a password. Anyone with a copy can read all included files and saved credentials. Integrity checks, compression and deduplication still work. Existing protected repositories keep their passwords; choose a new repository to change protection mode.

For unattended backups without saving any credential:

```sh
guestvault configure --mode folders --source /path/to/files \
  --repository /external-disk/repository --export-directory /external-disk/exports \
  --every-hours 24 --schedule --no-password
guestvault backup
guestvault install-service
guestvault verify /downloaded/backup.vmbackup --no-password
guestvault restore /downloaded/backup.vmbackup --target /empty/recovered --no-password
```

Repository commands follow the saved protection choice automatically. `verify`, `restore`, `backup-disk` and `restore-disk` also accept `--no-password`. For a password-free remote repository, initialize it with restic's `--insecure-no-password` flag. Storage-provider account credentials are separate and may still be required. See [restic's empty-password documentation](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#repositories-with-empty-password).

### Linux system mode

Run the tool with root privileges so it can read account keyrings, system files and other users' profiles. Always pass the same state directory to setup, configuration, backup and scheduling:

```sh
sudo .venv/bin/guestvault --state /var/lib/guestvault setup
sudo .venv/bin/guestvault --state /var/lib/guestvault serve --no-browser
```

Open the printed `http://127.0.0.1:…/…` session URL in the browser inside your VM. It is a local interface protected by a random session token, Host/Origin validation and a restrictive Content Security Policy. Closing the browser leaves the server running; stopping its terminal stops its scheduler.

`doctor` reports the current system, privileges, detected mounts and limitations. System mode includes `/` and detected additional local mounts. Virtual/runtime filesystems and temporary directories are excluded. Network filesystems and unmounted volumes are not automatically included. Btrfs nested subvolumes need explicitly selected folder sources; system mode uses one-filesystem boundaries. Restic's own repository, temporary export/import data, saved GuestVault credentials and previously exported `.vmbackup` files are excluded to avoid recursive backups. The encrypted snapshot includes a recovery metadata file describing the OS and disk layout.

## Periodic backups and cloud storage

A **repository** holds compressed, deduplicated snapshots. Compression is enabled automatically using restic repository format 2 and `--compression auto`. Already-compressed media may shrink very little. Incremental snapshots store changed chunks; each standalone export still includes all chunks needed to restore it. A **portable export** is a separate complete snapshot with its own key, unlockable with the same password or without one if the plan is unprotected. Each portable file can restore independently of the original repository or any earlier file.

```sh
sudo .venv/bin/guestvault --state /var/lib/guestvault configure \
  --mode system \
  --repository /external-disk/guestvault-repository \
  --export-directory /external-disk/cloud-upload \
  --every-hours 24 --schedule --remember-password

sudo .venv/bin/guestvault --state /var/lib/guestvault backup --use-saved-password
sudo .venv/bin/guestvault --state /var/lib/guestvault install-service
```

`--remember-password` explicitly stores the password in a locally protected credential file, excluded from the backup. Linux/macOS use owner-only filesystem permissions; Windows uses a restricted ACL. Anyone controlling the running VM can still access its backup credentials. Password-protected unattended backup needs access to the password; it is never stored inside an exported archive.

`install-service` installs and starts a systemd service on Linux, a launchd job on macOS or an elevated Task Scheduler job on Windows. Linux/macOS root installation starts at boot; user services depend on the user service manager. The Windows job starts at the current user's login and does not run after logout. Full Disk Access must also cover the macOS service process. `install-service` detects Linux containers without systemd and starts a detached background daemon instead. It survives terminal/browser closure but has no reboot autostart in that environment; rerun `install-service` after the container or VM restarts. Only one daemon per state directory runs at a time, and the web UI defers scheduling to it. You can also run `guestvault daemon` in the foreground. The app and daemon read the saved interval, catch up after a missed run and retry failed runs after five minutes. Keep the VM powered on for scheduled backups. Disable scheduling in the saved plan to pause backups.

Restic cloud repository addresses such as `s3:s3.amazonaws.com/bucket/guestvault` are supported using the backend's normal environment credentials. Initialize a remote repository with restic first (see the [backend documentation](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)); GuestVault initializes new local repositories itself. Backend credentials must be available to the service account as well as the interactive shell. Do not supply secrets in a repository URL because the URL is saved in local settings.

For arbitrary cloud storage, export into a locally synced folder or upload the finished `.vmbackup` file yourself. GuestVault does not configure cloud accounts, perform that upload or certify cloud sync completion. The cloud upload must finish before you wipe anything. Exports need sufficient temporary space in the state directory for a complete independent encrypted snapshot, plus space for the final file at its destination. Export publication needs hard-link support (ext4, NTFS and APFS are suitable); for a destination without it, export locally and copy the finished file. Direct cloud repositories avoid the need for a local full-size repository, but portable exports still need staging space.

### Back up and restore without a full local copy

Set the repository to a cloud address and **leave the portable export folder empty**. GuestVault reads source files and sends compressed, deduplicated chunks directly to the repository. It keeps metadata/cache files locally, whose size depends on the backup; it does not create a complete local backup or archive. The remote storage must still have enough capacity for the saved data. A mounted external disk is another option. A cloud-sync folder on the VM generally still consumes local space.

On a replacement VM, choose **Import & restore → A cloud or local repository**, enter its address and optional backup password, select **Load backups**, then choose a date and restore to an empty destination. Provider credentials must be available to the process. Recovery checks repository integrity and reads files directly into the destination, without staging a full `.vmbackup` file. It still needs enough destination space for the recovered files and retains the same OS recovery boundaries.

CLI equivalent (use your real repository address):

```sh
guestvault init-repository --repository s3:service/bucket/guestvault --no-password
guestvault configure --mode system --repository s3:service/bucket/guestvault \
  --schedule --every-hours 24 --no-password
guestvault backup
guestvault repository-snapshots --repository s3:service/bucket/guestvault --no-password
guestvault restore-repository --repository s3:service/bucket/guestvault \
  --snapshot COMPLETE_SNAPSHOT_ID --target /empty/recovered --no-password
```

`init-repository` creates a new repository only when explicitly requested. Existing remote repositories must not be reinitialized. Omit `--no-password` to use password protection. `verify-repository` accepts the same repository, snapshot and password arguments without a restore target. Repository listing and import work on a fresh GuestVault installation without configuring a new backup plan.

Linux system backups also exclude empty directory mounts whose original source directory has been deleted. These stale bind mounts can make the OS directory reader fail even though the mount point still appears present. Other mounted files, ordinary directories and directories with visible contents remain included. Automatically excluded paths are recorded in the recovery metadata.

There is no automatic retention deletion. Snapshots and portable files accumulate until you deliberately remove them. This prevents a failed backup or export from pruning your last recovery point.

## Recovery

Read [docs/RECOVERY.md](docs/RECOVERY.md) for Linux, Windows, macOS and offline disk recovery. For ordinary file recovery:

```sh
guestvault verify /downloaded/backup.vmbackup
guestvault restore /downloaded/backup.vmbackup --target /empty/recovered
```

The tool checks all encrypted data before writing recovered files, then verifies restored file contents. Ownership and metadata require the correct OS and privileges. An interrupted restore leaves a partial destination and is reported as a failure; start again with a different empty destination. It never merges restored files into an existing folder or replaces the running OS root.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest ruff build
guestvault setup
GUESTVAULT_TEST_RESTIC=/path/to/restic .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python -m build
```

The Linux integration tests use real restic encryption and recovery, delete the original files and repository, and verify recovery using only the portable file. They also run password-free backup/export/import, compression verification and actual interval scheduling without credentials. They check hidden files, hardlinks, symlinks, file modes, timestamps, supported xattrs, sparse contents, ciphertext corruption, wrong passwords, nonempty/unsafe restore targets, excluded credentials, partial-backup status and loopback API protections. Direct repository tests use real restic requests against a disposable loopback REST backend, wipe the client source/state, and recover without exporting or unpacking a bundle. They check both password choices and that local state stays much smaller than the test payload. Cross-platform smoke tests cover direct repository recovery too. Raw-image transport tests use disposable regular files with device discovery substituted; they do **not** erase a real disk or claim a booted OS recovery test. CI also runs actual folder backup/export/import smoke tests on Windows and macOS. Native `wbadmin`, native OS service startup and full OS boot recovery need validation on the intended VM.

MIT licensed. Restic remains a separately downloaded tool with its own BSD-2-Clause license.
