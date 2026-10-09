"""status.json (a quick look at a running study) and the run lock; both written atomically under the study folder."""

import json
import os
import time
from pathlib import Path

from hpo.errors import Busy


def replace(tmp: Path, path: Path, tries: int = 20) -> None:
    """os.replace that survives Windows: a file a browser, an editor, an antivirus scan or OneDrive has open cannot be replaced (WinError 5 or 32), usually
    for a moment. Retry briefly; if it stays blocked, write the content straight over the target (no longer atomic, but the run and its data go on)."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1) ** 0.5)
    try:
        path.write_bytes(tmp.read_bytes())
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, doc: dict) -> None:
    """Atomic where the platform allows: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str) + "\n", encoding="utf-8")
    replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    if os.name == "nt":  # no cheap, portable probe: the stale-after rule alone decides
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """run.lock: PID and time; stale after `stale_hours` or when the PID is gone. A second holder gets Busy."""

    def __init__(self, path: Path, stale_hours: float):
        self.path, self.stale = path, stale_hours * 3600

    def __enter__(self):
        held = read_json(self.path)
        if held and held.get("pid") != os.getpid() and time.time() - held.get("time", 0) < self.stale and _alive(int(held.get("pid", 0))):
            raise Busy(f"{self.path} is held by process {held['pid']} since {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(held['time']))}")
        write_json(self.path, {"pid": os.getpid(), "time": time.time()})
        return self

    def __exit__(self, *exc):
        held = read_json(self.path)
        if held and held.get("pid") == os.getpid():
            try:
                self.path.unlink(missing_ok=True)
            except PermissionError:  # briefly held by a sync or scan on Windows: a stale lock is taken over after lockStaleHours or when its process is gone
                pass
