"""Install a scheduler using the OS service manager (only on explicit CLI request)."""

import os
import platform
import plistlib
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .scheduler import is_running
from .storage import private


def systemd_available():
    return Path("/run/systemd/system").is_dir()


def install(store, executable=None):
    plan = store.load()
    if not plan or not plan.get("scheduled"):
        raise ValueError(
            "Enable scheduling before installing the service. Protected plans also need a saved password."
        )
    store.password()
    arguments = [sys.executable, "-m", "guestvault", "--state", str(store.root)]
    if executable:
        arguments += ["--restic", str(executable)]
    arguments.append("daemon")
    system = platform.system()
    if system == "Linux":
        if not systemd_available():
            if not is_running(store):
                log = store.root / "daemon.log"
                with log.open("ab") as stream:
                    private(log)
                    process = subprocess.Popen(
                        arguments,
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                for _ in range(50):
                    if process.poll() is not None:
                        raise ValueError(
                            "Background scheduler failed to start. Inspect daemon.log in the state directory."
                        )
                    if is_running(store) and (store.root / "scheduler.json").exists():
                        break
                    time.sleep(0.1)
                else:
                    process.terminate()
                    raise ValueError("Background scheduler did not become ready.")
            return "Background scheduler running; this environment has no systemd. It survives terminal/browser closure, but must be restarted after VM/container restart."
        root = os.geteuid() == 0
        folder = Path("/etc/systemd/system") if root else Path.home() / ".config/systemd/user"
        folder.mkdir(parents=True, exist_ok=True)

        def quote(value):
            return (
                '"'
                + value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("%", "%%")
                .replace("$", "$$")
                + '"'
            )

        unit = folder / "guestvault.service"
        unit.write_text(
            "[Unit]\nDescription=GuestVault periodic guest backups\nWants=network-online.target\n"
            "After=network-online.target\n\n[Service]\nType=simple\nExecStart="
            + " ".join(quote(v) for v in arguments)
            + "\nRestart=on-failure\nRestartSec=30\nUMask=0077\n"
            "\n[Install]\nWantedBy=" + ("multi-user.target" if root else "default.target") + "\n"
        )
        command = ["systemctl"] + ([] if root else ["--user"])
        subprocess.run([*command, "daemon-reload"], check=True)
        subprocess.run([*command, "enable", "--now", "guestvault.service"], check=True)
        return str(unit)
    if system == "Darwin":
        root = os.geteuid() == 0
        folder = Path("/Library/LaunchDaemons") if root else Path.home() / "Library/LaunchAgents"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "com.isiliconx.guestvault.plist"
        data = {
            "Label": "com.isiliconx.guestvault",
            "ProgramArguments": arguments,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(store.root / "daemon.log"),
            "StandardErrorPath": str(store.root / "daemon.log"),
        }
        target.write_bytes(plistlib.dumps(data))
        target.chmod(0o644)
        subprocess.run(
            ["launchctl", "bootstrap", "system" if root else f"gui/{os.getuid()}", str(target)],
            check=True,
        )
        return str(target)
    if system == "Windows":
        # An unlimited execution time is essential: schtasks' defaults can stop a daemon after 72h.
        account = subprocess.check_output(["whoami"], text=True).strip()
        task = ET.Element(
            "Task", version="1.2", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"
        )
        trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "LogonTrigger")
        ET.SubElement(trigger, "Enabled").text = "true"
        ET.SubElement(trigger, "UserId").text = account
        principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", id="Author")
        ET.SubElement(principal, "UserId").text = account
        ET.SubElement(principal, "LogonType").text = "InteractiveToken"
        ET.SubElement(principal, "RunLevel").text = "HighestAvailable"
        settings = ET.SubElement(task, "Settings")
        for key, value in {
            "MultipleInstancesPolicy": "IgnoreNew",
            "DisallowStartIfOnBatteries": "false",
            "StopIfGoingOnBatteries": "false",
            "StartWhenAvailable": "true",
            "ExecutionTimeLimit": "PT0S",
        }.items():
            ET.SubElement(settings, key).text = value
        restart = ET.SubElement(settings, "RestartOnFailure")
        ET.SubElement(restart, "Interval").text = "PT1M"
        ET.SubElement(restart, "Count").text = "3"
        action = ET.SubElement(ET.SubElement(task, "Actions", Context="Author"), "Exec")
        ET.SubElement(action, "Command").text = arguments[0]
        ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(arguments[1:])
        task_file = store.root / "scheduler-task.xml"
        ET.ElementTree(task).write(task_file, encoding="utf-16", xml_declaration=True)
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                "GuestVault",
                "/XML",
                str(task_file),
                "/F",
            ],
            check=True,
        )
        subprocess.run(["schtasks", "/Run", "/TN", "GuestVault"], check=True)
        return "Task Scheduler: GuestVault (at login)"
    raise ValueError("Automatic service installation supports Linux, Windows and macOS.")
