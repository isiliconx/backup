"""OS discovery and explicit recovery capability reporting."""

import ctypes
import json
import os
import platform
import re
import subprocess
from pathlib import Path

LOCAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "zfs", "vfat", "exfat", "ntfs", "ntfs3"}
LIMITS = [
    "Running memory, open windows and active network connections are not captured.",
    "Websites can expire cookies or revoke sessions; restored login files cannot prevent that.",
    "Hardware-bound keys, virtual TPM state and hypervisor configuration are outside this tool.",
]


def capture(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def elevated():
    if os.name == "nt":
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def mounts():
    result = capture(["findmnt", "--json", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    if not result:
        return []
    rows = []

    def visit(items):
        for item in items:
            rows.append({k: v for k, v in item.items() if k != "children"})
            visit(item.get("children", []))

    visit(json.loads(result).get("filesystems", []))
    return rows


def system_sources():
    system = platform.system()
    if system == "Windows":
        return [
            f"{chr(65 + i)}:\\"
            for i in range(26)
            if ctypes.windll.kernel32.GetDriveTypeW(f"{chr(65 + i)}:\\") == 3
        ]
    if system == "Darwin":
        return [p for p in ["/Users", "/Applications", "/Library", "/private"] if Path(p).exists()]
    if system == "Linux":
        roots = ["/"]
        for row in mounts():
            target = row["target"]
            # Bind mounts and virtual/remote filesystems must not masquerade as additional disks.
            if row["fstype"] in LOCAL_FS and "[" not in row["source"] and target != "/":
                roots.append(target)
        return roots
    raise ValueError("System mode supports Linux, Windows and macOS. Use folders mode here.")


def deleted_directory_mounts(mountinfo=Path("/proc/self/mountinfo")):
    """Find empty bind mounts whose original directories were unlinked by Linux."""
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or not fields[3].endswith("//deleted"):
            continue
        target = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        try:
            if target != Path("/") and target.is_dir() and not any(target.iterdir()):
                result.append(str(target))
        except OSError:
            continue
    return result


def system_excludes():
    if platform.system() == "Linux":
        # Kernel-deleted directory bind mounts can return ENOENT from getdents even
        # though lstat succeeds. Preserve ordinary directories and mounted files.
        deleted = [re.sub(r"([\\*?\[\]])", r"\\\1", p) for p in deleted_directory_mounts()]
        return ["/dev", "/proc", "/sys", "/run", "/tmp", "/var/tmp", *deleted]
    if platform.system() == "Darwin":
        return ["/private/tmp", "/private/var/run", "/private/var/vm", "/private/var/tmp"]
    return ["pagefile.sys", "swapfile.sys", "hiberfil.sys"]


def doctor():
    system = platform.system()
    warnings = list(LIMITS)
    if system == "Linux":
        warnings += [
            "Live file backups are not an atomic disk snapshot. Close browsers and stop databases.",
            "Full OS recovery needs a rescue boot, disk preparation and bootloader repair.",
        ]
    elif system == "Windows":
        warnings += [
            "File backups use VSS but are not bootable Windows system images.",
            "Use windows-image mode and Windows Recovery for OS and installed-app recovery.",
        ]
    elif system == "Darwin":
        warnings += [
            "Grant Full Disk Access to the terminal/Python process before system backup.",
            "macOS file recovery does not recreate the sealed OS volume or perform Migration Assistant.",
        ]
    if not elevated():
        warnings.append("Run as root/Administrator to back up system files and restore ownership.")
    return {
        "os": system,
        "architecture": platform.machine(),
        "elevated": elevated(),
        "hostname": platform.node(),
        "sources": system_sources(),
        "limitations": warnings,
        "mounts": mounts() if system == "Linux" else [],
    }


def recovery_metadata(mode, sources, excludes):
    info = doctor()
    info.update(
        {
            "mode": mode,
            "sources": sources,
            "excludes": excludes,
            "os_release": capture(["cat", "/etc/os-release"])
            if platform.system() == "Linux"
            else None,
            "disk_layout": capture(
                [
                    "lsblk",
                    "--json",
                    "--bytes",
                    "--output",
                    "NAME,PATH,TYPE,SIZE,FSTYPE,UUID,PARTUUID,MOUNTPOINTS",
                ]
            )
            if platform.system() == "Linux"
            else None,
        }
    )
    return info
