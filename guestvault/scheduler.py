"""Persistent interval scheduling while the app or daemon is running."""

import datetime as dt
import os
import threading

from .engine import Engine
from .storage import atomic_json


def due(plan, now=None):
    if not plan or not plan.get("scheduled"):
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    return not plan.get("next_run") or dt.datetime.fromisoformat(plan["next_run"]) <= now


def tick(store, executable=None, progress=None):
    plan = store.load()
    if not due(plan):
        return None
    try:
        result = Engine(store, executable, progress).backup()
        atomic_json(
            store.root / "activity.json",
            {
                "success": True,
                "result": result,
                "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        return result
    except Exception as exc:
        # Retry after five minutes; don't mark partial snapshots as successful.
        plan = store.load()
        if plan:
            plan["next_run"] = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
            ).isoformat()
            store.save(plan)
        atomic_json(
            store.root / "activity.json",
            {
                "success": False,
                "error": str(exc),
                "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        if progress:
            progress({"error": str(exc)})
        return {"error": str(exc)}


def run(store, executable=None, progress=None, stop=None):
    stop = stop or threading.Event()
    with store.lock("daemon.lock"):
        atomic_json(store.root / "scheduler.json", {"pid": os.getpid(), "running": True})
        try:
            while not stop.is_set():
                tick(store, executable, progress)
                stop.wait(15)
        finally:
            (store.root / "scheduler.json").unlink(missing_ok=True)


def is_running(store):
    try:
        with store.lock("daemon.lock"):
            return False
    except ValueError:
        return True
