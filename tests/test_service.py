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


def test_linux_without_systemd_starts_one_real_detached_scheduler(
    tmp_path, restic_binary, monkeypatch
):
    import json
    import os
    import platform
    import signal
    import time

    import pytest

    from guestvault.engine import configure
    from guestvault.scheduler import is_running

    if platform.system() != "Linux":
        pytest.skip("Linux-only fallback")
    source = tmp_path / "source"
    source.mkdir()
    (source / "example").write_text("real background service backup")
    store = Store(tmp_path / "state")
    (store.root / "bin").mkdir()
    (store.root / "bin/restic").symlink_to(restic_binary)
    plan = configure(store, "folders", [source], tmp_path / "repo", password_required=False)
    plan["scheduled"] = True
    store.save(plan)
    monkeypatch.setattr(service, "systemd_available", lambda: False)
    try:
        assert "no systemd" in service.install(store)
        pid = json.loads((store.root / "scheduler.json").read_text())["pid"]
        assert os.getsid(pid) == pid
        assert is_running(store)
        assert "no systemd" in service.install(store)
        assert json.loads((store.root / "scheduler.json").read_text())["pid"] == pid
        deadline = time.monotonic() + 30
        while not store.load().get("last_success") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert store.load().get("last_success")
    finally:
        if (store.root / "scheduler.json").exists():
            os.kill(json.loads((store.root / "scheduler.json").read_text())["pid"], signal.SIGTERM)
