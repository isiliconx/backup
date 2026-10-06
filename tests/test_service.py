import sys
import xml.etree.ElementTree as ET

from guestvault import service
from guestvault.storage import Store


def test_windows_daemon_task_does_not_expire_after_three_days(tmp_path, monkeypatch):
    store = Store(tmp_path / "state & backups")
    store.save({"scheduled": True})
    store.save_password("synthetic-service-password")
    calls = []
    monkeypatch.setattr(service.platform, "system", lambda: "Windows")
    monkeypatch.setattr(service.subprocess, "check_output", lambda *a, **k: "DOMAIN\\test-user")
    monkeypatch.setattr(service.subprocess, "run", lambda command, **k: calls.append(command))
    service.install(store)
    task = ET.parse(store.root / "scheduler-task.xml")
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert task.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    assert task.find("t:Settings/t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert task.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert task.find("t:Actions/t:Exec/t:Command", ns).text == sys.executable
    assert str(store.root) in task.find("t:Actions/t:Exec/t:Arguments", ns).text
    assert calls[-1] == ["schtasks", "/Run", "/TN", "GuestVault"]
