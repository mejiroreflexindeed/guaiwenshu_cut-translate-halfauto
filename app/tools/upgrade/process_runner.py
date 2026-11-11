#!/usr/bin/env python3
"""Owned subprocess execution (plan 1.1, sections 6.3, 10.5).

Interface (fixed by the plan): ``run_process(ProcessSpec, cancel, event_sink) -> ProcessResult``.

- argv lists only, ``shell=False``, explicit cwd, hidden console; the private environment is never logged
  (only the names of overridden variables are recorded);
- on Windows the child is placed in a Job Object with KILL_ON_JOB_CLOSE so an executor crash cannot leave
  engines running; cancellation terminates only that ownership scope, never processes by name;
- cancel order: set token -> ask the child to exit -> wait 3 s -> terminate the job/process tree;
- stdout/stderr go to files under ``log_dir``; a ``process.json`` sidecar records argv, pid, owner token and timing.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from upgrade.contracts import ContractError, require_record, utc_now
from upgrade.storage import artifact_ref, atomic_write_text, render_json

CANCEL_GRACE_SECONDS = 3.0
POLL_SECONDS = 0.2
EventSink = Callable[[dict], None]


class CancelToken:
    """Cooperative cancellation flag shared between the runner and running stages."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


# --------------------------------------------------------------------------- Windows job objects

class _JobObject:
    """Thin ctypes wrapper; every call degrades to "unavailable" instead of raising."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        self.handle = None
        self.available = False
        if sys.platform != "win32":
            return
        try:
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32 = kernel32
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(handle, self.JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self.available = True
        except (AttributeError, OSError, ValueError):
            self.available = False

    def assign(self, process: subprocess.Popen) -> bool:
        if not self.available:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            return False

    def terminate(self) -> bool:
        if not self.available:
            return False
        try:
            return bool(self._kernel32.TerminateJobObject(self.handle, 1))
        except (AttributeError, OSError):
            return False

    def close(self) -> None:
        if self.available and self.handle:
            try:
                self._kernel32.CloseHandle(self.handle)
            finally:
                self.handle = None
                self.available = False


def _hidden_kwargs() -> dict:
    if sys.platform != "win32":
        return {"start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP, "startupinfo": startupinfo}


def _terminate_tree_fallback(process: subprocess.Popen) -> None:
    """Used only when no Job Object is available: the legacy Toolhelp-based tree termination."""
    try:
        import one_click_roast  # legacy module; its helper is a pure Windows API wrapper
        one_click_roast.terminate_process_tree(process)
        return
    except Exception:
        pass
    try:
        process.kill()
    except OSError:
        pass


# --------------------------------------------------------------------------- handle

class ProcessHandle:
    """An owned child process: start, observe, cancel. Services (llama-server) reuse this in W04."""

    def __init__(self, spec: dict, *, cancel: CancelToken | None = None, event_sink: EventSink | None = None):
        self.spec = require_record("ProcessSpec", spec)
        self.cancel_token = cancel or CancelToken()
        self.emit = event_sink or (lambda event: None)
        self.log_dir = Path(self.spec["log_dir"])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stdout_path = self.log_dir / "stdout.log"
        self.stderr_path = self.log_dir / "stderr.log"
        self.sidecar_path = self.log_dir / "process.json"
        self.process: subprocess.Popen | None = None
        self.job = _JobObject()
        self.job_assigned = False
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.cancelled = False
        self._stdout_handle = None
        self._stderr_handle = None

    # -- lifecycle
    def start(self) -> "ProcessHandle":
        spec = self.spec
        executable = Path(spec["executable"])
        if not executable.is_file():
            raise ContractError("RESOURCE_MISSING", f"executable not found: {executable}", details={"executable": str(executable)})
        cwd = Path(spec["cwd"])
        if not cwd.is_dir():
            raise ContractError("INPUT_MISSING", f"cwd does not exist: {cwd}", details={"cwd": str(cwd)})
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in spec["env_overrides"].items()})
        env.setdefault("PYTHONUTF8", "1")
        self._stdout_handle = self.stdout_path.open("wb")
        self._stderr_handle = self.stderr_path.open("wb")
        self.started_at = utc_now()
        try:
            self.process = subprocess.Popen(
                [str(executable), *spec["argv"]], cwd=str(cwd), env=env, shell=False,
                stdin=subprocess.PIPE if spec["stdin_mode"] == "pipe" else subprocess.DEVNULL,
                stdout=self._stdout_handle, stderr=self._stderr_handle, **_hidden_kwargs(),
            )
        except OSError as exc:
            self._close_logs()
            raise ContractError("PROCESS_FAILED", f"cannot start {executable.name}: {exc}", details={"executable": str(executable)}) from exc
        self.job_assigned = self.job.assign(self.process)
        self._write_sidecar(status="RUNNING")
        self.emit({"event": "process_started", "pid": self.process.pid, "executable": str(executable), "argv": list(spec["argv"]),
                   "owner_token": spec["owner_token"], "capability_id": spec["capability_id"], "job_object": self.job_assigned,
                   "env_override_names": sorted(spec["env_overrides"])})
        return self

    def poll(self) -> int | None:
        return self.process.poll() if self.process else None

    def send_stdin(self, data: bytes, *, close: bool = True) -> None:
        """Write once to the child's stdin (needs stdin_mode=pipe). A child that already exited is not an error."""
        if not self.process or self.process.stdin is None:
            raise ContractError("PROCESS_FAILED", "stdin is not a pipe for this process", details={"stdin_mode": self.spec["stdin_mode"]})
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
            if close:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def wait(self, *, poll_seconds: float = POLL_SECONDS) -> dict:
        """Wait for exit or cancellation; returns a ProcessResult."""
        assert self.process is not None, "start() first"
        while True:
            code = self.process.poll()
            if code is not None:
                break
            if self.cancel_token.wait(poll_seconds):
                self.terminate(reason=self.cancel_token.reason or "cancelled")
                break
        return self._finish()

    def request_exit(self) -> None:
        """Best-effort graceful exit request (console break on Windows, SIGTERM elsewhere)."""
        if not self.process or self.process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()
        except (OSError, ValueError):
            pass

    def terminate(self, *, reason: str = "cancelled", grace_seconds: float = CANCEL_GRACE_SECONDS) -> None:
        """Cancel order: exit request -> grace -> terminate the owned job / tree. Never touches other processes."""
        if not self.process:
            return
        self.cancelled = True
        self.emit({"event": "process_cancel_requested", "pid": self.process.pid, "reason": reason})
        self.request_exit()
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and self.process.poll() is None:
            time.sleep(POLL_SECONDS)
        if self.process.poll() is None:
            if not (self.job_assigned and self.job.terminate()):
                _terminate_tree_fallback(self.process)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.emit({"event": "process_terminated", "pid": self.process.pid, "exit_code": self.process.poll(), "reason": reason})

    # -- internals
    def _close_logs(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle:
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()
                except (OSError, ValueError):
                    pass
        self._stdout_handle = self._stderr_handle = None

    def _write_sidecar(self, *, status: str, exit_code: int | None = None) -> None:
        sidecar = {"schema_version": 2, "status": status, "pid": self.process.pid if self.process else None, "executable": self.spec["executable"],
                   "argv": self.spec["argv"], "cwd": self.spec["cwd"], "owner_token": self.spec["owner_token"],
                   "capability_id": self.spec["capability_id"], "env_override_names": sorted(self.spec["env_overrides"]),
                   "job_object": self.job_assigned, "started_at": self.started_at, "ended_at": self.ended_at, "exit_code": exit_code,
                   "cancelled": self.cancelled}
        atomic_write_text(self.sidecar_path, render_json(sidecar))

    def _finish(self) -> dict:
        assert self.process is not None
        exit_code = self.process.poll()
        if exit_code is None:
            exit_code = self.process.wait()
        self.ended_at = utc_now()
        self._close_logs()
        self.job.close()
        self._write_sidecar(status="CANCELLED" if self.cancelled else ("DONE" if exit_code == 0 else "FAILED"), exit_code=exit_code)
        result = {
            "exit_code": int(exit_code), "cancelled": self.cancelled, "started_at": self.started_at, "ended_at": self.ended_at,
            "stdout_ref": artifact_ref(self.log_dir, self.stdout_path), "stderr_ref": artifact_ref(self.log_dir, self.stderr_path),
            "observed_engine_id": None,
        }
        self.emit({"event": "process_exited", "pid": self.process.pid, "exit_code": result["exit_code"], "cancelled": self.cancelled})
        return require_record("ProcessResult", result)


def run_process(spec: dict, cancel: CancelToken | None = None, event_sink: EventSink | None = None) -> dict:
    """Run an owned subprocess to completion or cancellation and return a ProcessResult."""
    handle = ProcessHandle(spec, cancel=cancel, event_sink=event_sink).start()
    return handle.wait()


def process_spec(executable: str | Path, argv: list[str], *, cwd: str | Path, log_dir: str | Path, owner_token: str,
                 capability_id: str = "cpu", env_overrides: dict[str, str] | None = None, stdin_mode: str = "none") -> dict:
    return require_record("ProcessSpec", {"executable": str(executable), "argv": [str(item) for item in argv], "cwd": str(cwd),
                                          "env_overrides": dict(env_overrides or {}), "stdin_mode": stdin_mode, "log_dir": str(log_dir),
                                          "owner_token": owner_token, "capability_id": capability_id})


__all__ = ["CANCEL_GRACE_SECONDS", "CancelToken", "ProcessHandle", "run_process", "process_spec"]
