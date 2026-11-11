#!/usr/bin/env python3
"""OS-level exclusive locks for the data root executor and per-project commits (plan 1.1, section 10.2).

- the executor lock is a byte lock (``msvcrt.locking`` on Windows, ``fcntl`` elsewhere) on ``locks/executor.lock``;
  the lock file is never deleted while held; owner metadata lives in a separate ``executor.owner.json`` and the PID
  there is informational only, never used to decide ownership;
- a second executor gets EXECUTOR_BUSY immediately;
- project commit locks are short-lived and taken only while writing project.json / pointers;
- lock order: data-root executor lock -> project commit lock. GUIs take only the project lock.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

from upgrade.contracts import ContractError, new_id, utc_now
from upgrade.storage import atomic_write_text, render_json

if sys.platform == "win32":
    import msvcrt
else:  # pragma: no cover - Windows is the target platform
    import fcntl

EXECUTOR_LOCK = "locks/executor.lock"
EXECUTOR_OWNER = "locks/executor.owner.json"
PROJECT_LOCK = ".commit.lock"


class ByteLock:
    """Non-blocking exclusive lock on the first byte of a file. ``acquire`` returns False when another holder exists."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                handle.seek(0)
                if handle.tell() == 0 and os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def __enter__(self) -> "ByteLock":
        if not self.acquire():
            raise ContractError("EXECUTOR_BUSY", f"lock is held by another process: {self.path}", details={"path": str(self.path)})
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class ExecutorLock:
    """Exclusive executor for one DATA_ROOT. Raises EXECUTOR_BUSY when another executor holds it."""

    def __init__(self, data_root: Path | str):
        self.data_root = Path(data_root)
        self.lock = ByteLock(self.data_root / EXECUTOR_LOCK)
        self.owner_path = self.data_root / EXECUTOR_OWNER
        self.owner_token = new_id()

    def acquire(self) -> "ExecutorLock":
        if not self.lock.acquire():
            owner = read_owner(self.owner_path)
            raise ContractError("EXECUTOR_BUSY", "another executor already owns this data root",
                                details={"data_root": str(self.data_root), "owner": owner})
        atomic_write_text(self.owner_path, render_json({"schema_version": 2, "owner_token": self.owner_token, "pid": os.getpid(),
                                                        "hostname": socket.gethostname(), "started_at": utc_now(),
                                                        "note": "pid is informational; ownership is decided by the OS byte lock"}))
        return self

    def release(self) -> None:
        self.lock.release()

    @property
    def held(self) -> bool:
        return self.lock.held

    def __enter__(self) -> "ExecutorLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def read_owner(path: Path) -> dict | None:
    try:
        from upgrade.contracts import parse_json_text
        value = parse_json_text(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def executor_is_live(data_root: Path | str) -> bool:
    """True when some process currently holds the executor lock (probe by trying to take it)."""
    probe = ByteLock(Path(data_root) / EXECUTOR_LOCK)
    if probe.acquire():
        probe.release()
        return False
    return True


class ProjectCommitLock:
    """Short exclusive lock for project.json / pointer writes. Waits up to ``timeout`` seconds, then REVIEW_FILE_BUSY-like EXECUTOR_BUSY."""

    def __init__(self, project_dir: Path | str, *, timeout: float = 10.0):
        self.lock = ByteLock(Path(project_dir) / PROJECT_LOCK)
        self.timeout = timeout

    def __enter__(self) -> "ProjectCommitLock":
        deadline = time.monotonic() + self.timeout
        while not self.lock.acquire():
            if time.monotonic() >= deadline:
                raise ContractError("EXECUTOR_BUSY", "project commit lock is busy", details={"path": str(self.lock.path)})
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.lock.release()


__all__ = ["EXECUTOR_LOCK", "EXECUTOR_OWNER", "PROJECT_LOCK", "ByteLock", "ExecutorLock", "ProjectCommitLock", "executor_is_live", "read_owner"]
