#!/usr/bin/env python3
"""Persistent queue, stage engine, recovery and cancellation (plan 1.1, section 10).

Interface (fixed by the plan): ``submit``, ``run_queue`` (QueueRunner), ``resume``, ``cancel``.

Guarantees implemented here:
- every request is persisted (queue/requests/<request_id>.json) before it is reported as queued; the same
  request_id is idempotent and never runs twice; cancelled requests keep their JSON;
- one executor per data root (OS byte lock); a second executor gets EXECUTOR_BUSY;
- each stage attempt writes into a pending directory, is hashed and described by a StageManifest, then renamed
  into place; only after that the task/pointer files are updated. Recovery rebuilds pointers from committed
  manifests and never re-runs a committed stage; half-written attempts stay as candidates, never DONE;
- cache reuse needs an identical input signature and a verified manifest, never just a matching file name;
- cancellation: cooperative token + owned-process termination (process_runner); other processes are untouched.

Stage implementations (INPUT, MEDIA_PREP, OCR, ...) are supplied through a registry (upgrade.stage_registry).
A build without an implementation for a required stage fails that request with STAGE_UNAVAILABLE.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from upgrade.contracts import (
    REQUEST_STATUSES, SCHEMA_VERSION, STAGES, ContractError, new_id, require_record, signature, utc_now,
)
from upgrade.config import build_config_snapshot
from upgrade.locks import EXECUTOR_OWNER, ExecutorLock, ProjectCommitLock, executor_is_live, read_owner
from upgrade.process_runner import CancelToken
from upgrade.storage import (
    MANIFEST_NAME, artifact_ref, atomic_write_text, begin_pending, commit_pending, init_project_dir, read_manifest, render_json,
    safe_join, verify_committed,
)
from task_state import append_jsonl  # legacy pure helper: redacting JSONL append with fsync

STAGE_VERSIONS: dict[str, int] = {name: 1 for name in STAGES}
STAGE_VERSIONS["QUALITY_AND_COMMIT"] = 2   # W06 (IF-28): the full rule table and the review files; old stage caches do not apply
MODE_SKIPS: dict[str, dict[str, str]] = {
    "combined": {},
    "speech_only": {"OCR": "MODE"},
    "visual_only": {"ASR": "MODE"},
    "supplied_source": {"OCR": "USER_SOURCE", "ASR": "USER_SOURCE"},
}
CANCEL_POLL_SECONDS = 0.5
EventSink = Callable[[dict], None]


# --------------------------------------------------------------------------- paths

def queue_dir(data_root: Path | str) -> Path:
    return Path(data_root) / "queue"


def envelope_path(data_root: Path | str, request_id: str) -> Path:
    return queue_dir(data_root) / "requests" / f"{request_id}.json"


def cancel_flag_path(data_root: Path | str, request_id: str) -> Path:
    return queue_dir(data_root) / "cancel" / request_id


def stop_flag_path(data_root: Path | str) -> Path:
    """"Stop current" (plan 3.2): the executor cancels the running request, keeps it resumable and pauses the queue."""
    return queue_dir(data_root) / "stop.flag"


def project_dir(data_root: Path | str, project_id: str) -> Path:
    return Path(data_root) / "projects" / project_id


def run_dir(project: Path, run_id: str) -> Path:
    return project / "runs" / run_id


def _queue_event(data_root: Path | str, event: dict) -> None:
    append_jsonl(queue_dir(data_root) / "events.jsonl", {"timestamp": utc_now(), **event})


def _run_event(run: Path, event: dict) -> None:
    append_jsonl(run / "events.jsonl", {"timestamp": utc_now(), **event})


# --------------------------------------------------------------------------- projects

def create_project(data_root: Path | str, display_name: str, *, project_id: str | None = None) -> dict:
    project_id = project_id or new_id()
    project = init_project_dir(data_root, project_id)
    record = require_record("ProjectRecord", {
        "schema_version": SCHEMA_VERSION, "project_id": project_id, "display_name": display_name, "created_at": utc_now(),
        "media_ids": [], "run_ids": [], "machine_revision_ids": [], "source_revision_ids": [], "review_revision_ids": [],
        "export_ids": [], "last_selected_revision_id": None, "extensions": {},
    })
    with ProjectCommitLock(project):
        if (project / "project.json").exists():
            raise ContractError("OUTPUT_EXISTS", "project already exists", details={"project_id": project_id})
        atomic_write_text(project / "project.json", render_json(record))
    return record


def load_project(data_root: Path | str, project_id: str) -> dict:
    from upgrade.contracts import load_record
    return load_record("ProjectRecord", project_dir(data_root, project_id) / "project.json")


def update_project(data_root: Path | str, project_id: str, mutate: Callable[[dict], None]) -> dict:
    project = project_dir(data_root, project_id)
    with ProjectCommitLock(project):
        record = load_project(data_root, project_id)
        mutate(record)
        require_record("ProjectRecord", record)
        atomic_write_text(project / "project.json", render_json(record))
    return record


# --------------------------------------------------------------------------- requests

def build_job_request(config: dict, engine_settings: dict, *, input_kind: str, input_value: str, recognition_mode: str,
                      timing_mode: str | None = None, project_id: str | None = None, video_stream_index: int | None = None,
                      audio_stream_index: int | None = None, source_revision_id: str | None = None, source_media_id: str | None = None,
                      dictionary_revision_id: str = "none", resource_refs: list[dict] | None = None,
                      accepted_dictionary_warnings: list[str] | None = None, request_id: str | None = None) -> dict:
    """Assemble a JobRequest with a frozen configuration snapshot and fingerprint. Persisting is ``submit``'s job."""
    if recognition_mode == "speech_only":
        timing_mode = "speech"
    elif recognition_mode == "visual_only":
        timing_mode = "screen"
    else:
        timing_mode = timing_mode or config["defaults"]["timing_mode"]
    snapshot = build_config_snapshot(config, engine_settings, dictionary_revision_id=dictionary_revision_id,
                                     resource_refs=resource_refs or [], accepted_dictionary_warnings=accepted_dictionary_warnings)
    body = {
        "input": {"kind": input_kind, "value": input_value, "source_media_id": source_media_id},
        "recognition_mode": recognition_mode, "timing_mode": timing_mode, "video_stream_index": video_stream_index,
        "audio_stream_index": audio_stream_index, "source_revision_id": source_revision_id,
        "config_sha256": snapshot["config_sha256"], "dictionary_revision_id": dictionary_revision_id,
        "resource_refs": resource_refs or [],
    }
    request = {
        "schema_version": SCHEMA_VERSION, "request_id": request_id or new_id(), "project_id": project_id or new_id(),
        "created_at": utc_now(), "input": body["input"], "recognition_mode": recognition_mode, "timing_mode": timing_mode,
        "video_stream_index": video_stream_index, "audio_stream_index": audio_stream_index, "source_revision_id": source_revision_id,
        "config_snapshot": snapshot, "resource_refs": resource_refs or [], "dictionary_revision_id": dictionary_revision_id,
        "request_fingerprint": signature(body), "extensions": {},
    }
    return require_record("JobRequest", request)


def load_envelope(data_root: Path | str, request_id: str) -> dict:
    from upgrade.contracts import load_record
    return load_record("QueueEnvelope", envelope_path(data_root, request_id))


def _save_envelope(data_root: Path | str, envelope: dict) -> dict:
    envelope["updated_at"] = utc_now()
    require_record("QueueEnvelope", envelope)
    atomic_write_text(envelope_path(data_root, envelope["request"]["request_id"]), render_json(envelope))
    return envelope


def list_envelopes(data_root: Path | str, *, status: str | None = None) -> list[dict]:
    from upgrade.contracts import load_record
    requests_dir = queue_dir(data_root) / "requests"
    envelopes = []
    if requests_dir.is_dir():
        for path in requests_dir.glob("*.json"):
            try:
                envelope = load_record("QueueEnvelope", path)
            except ContractError:
                continue  # unreadable envelopes are reported by diagnostics, never silently executed
            if status is None or envelope["status"] == status:
                envelopes.append(envelope)
    envelopes.sort(key=lambda item: (item["request"]["created_at"], item["request"]["request_id"]))
    return envelopes


def submit(data_root: Path | str, request: dict, *, display_name: str | None = None) -> dict:
    """Persist a request. Same request_id -> the existing envelope is returned and nothing runs again."""
    require_record("JobRequest", request)
    path = envelope_path(data_root, request["request_id"])
    if path.exists():
        existing = load_envelope(data_root, request["request_id"])
        _queue_event(data_root, {"event": "duplicate_submit", "request_id": request["request_id"], "status": existing["status"]})
        return existing
    project = project_dir(data_root, request["project_id"])
    if not (project / "project.json").exists():
        create_project(data_root, display_name or "project", project_id=request["project_id"])
    envelope = {"schema_version": SCHEMA_VERSION, "request": request, "status": "QUEUED", "run_id": None, "last_error": None,
                "created_at": utc_now(), "updated_at": utc_now()}
    _save_envelope(data_root, envelope)
    _queue_event(data_root, {"event": "request_queued", "request_id": request["request_id"], "project_id": request["project_id"],
                             "recognition_mode": request["recognition_mode"], "fingerprint": request["request_fingerprint"]})
    return envelope


def cancel(data_root: Path | str, request_id: str) -> dict:
    """Cancel a queued request immediately (JSON kept) or ask the running executor to stop this request."""
    envelope = load_envelope(data_root, request_id)
    if envelope["status"] == "QUEUED":
        envelope["status"] = "CANCELLED"
        envelope["last_error"] = ContractError("PROCESS_CANCELLED", "cancelled before start").to_dict()
        _save_envelope(data_root, envelope)
        _queue_event(data_root, {"event": "request_cancelled", "request_id": request_id, "when": "queued"})
        return envelope
    if envelope["status"] == "RUNNING":
        flag = cancel_flag_path(data_root, request_id)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(utc_now(), encoding="utf-8")
        _queue_event(data_root, {"event": "cancel_requested", "request_id": request_id, "when": "running"})
        return envelope
    _queue_event(data_root, {"event": "cancel_ignored", "request_id": request_id, "status": envelope["status"]})
    return envelope


def request_stop(data_root: Path | str) -> dict:
    """Ask the live executor to stop the current request and pause (plan 3.2). Without a live executor nothing is written."""
    if not executor_is_live(data_root):
        return {"requested": False, "executor_live": False, "running": []}
    running = [envelope["request"]["request_id"] for envelope in list_envelopes(data_root, status="RUNNING")]
    flag = stop_flag_path(data_root)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(utc_now(), encoding="utf-8")
    _queue_event(data_root, {"event": "queue_stop_requested", "running": running})
    return {"requested": True, "executor_live": True, "running": running}


def resume(data_root: Path | str, run_id: str) -> dict:
    """Re-queue the failed/interrupted request of ``run_id`` with its original configuration (new attempts, same run)."""
    for envelope in list_envelopes(data_root):
        if envelope.get("run_id") == run_id:
            if envelope["status"] not in ("FAILED", "INTERRUPTED"):
                raise ContractError("NEEDS_SELECTION", f"run {run_id} is {envelope['status']}; only FAILED/INTERRUPTED runs can be resumed",
                                    details={"run_id": run_id, "status": envelope["status"]})
            envelope["status"] = "QUEUED"
            _save_envelope(data_root, envelope)
            _queue_event(data_root, {"event": "request_resumed", "request_id": envelope["request"]["request_id"], "run_id": run_id})
            return envelope
    raise ContractError("INPUT_MISSING", f"no request belongs to run {run_id}", details={"run_id": run_id})


# --------------------------------------------------------------------------- queue overview (plan 3.2 list)

def _elapsed_seconds(started_at: str | None) -> int | None:
    if not started_at:
        return None
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _machine_quality(project: Path, revision_id: str) -> dict:
    from upgrade.contracts import load_record
    try:
        report = load_record("QualityReport", project / "revisions" / "machine" / revision_id / "quality.json")
    except ContractError:
        return {"structural_status": "unknown", "errors": None, "warnings": None}
    return {"structural_status": report["structural_status"], "errors": sum(1 for issue in report["issues"] if issue["severity"] == "ERROR"),
            "warnings": sum(1 for issue in report["issues"] if issue["severity"] == "WARNING")}


def queue_rows(data_root: Path | str) -> dict:
    """Read-only overview for the queue list: every request (history kept), its run state, versions and quality.

    No stage reports a reliable processed/total, so ``progress`` is always None and the list shows the current stage
    with its elapsed time instead of an estimated percentage (plan 3.2).
    """
    data_root = Path(data_root)
    live = executor_is_live(data_root)
    owner = read_owner(data_root / EXECUTOR_OWNER) if live else None
    projects: dict[str, dict | None] = {}
    rows = []
    for envelope in list_envelopes(data_root):
        request = envelope["request"]
        project_id = request["project_id"]
        if project_id not in projects:
            try:
                projects[project_id] = load_project(data_root, project_id)
            except ContractError:
                projects[project_id] = None
        record, project = projects[project_id], project_dir(data_root, project_id)
        task = None
        if envelope.get("run_id"):
            try:
                task = load_task(run_dir(project, envelope["run_id"]))
            except ContractError:
                task = None
        running = envelope["status"] == "RUNNING"
        active = task.get("active_stage") if (task and running) else None
        started = _stage(task, active).get("started_at") if active else None
        machine = task.get("machine_revision_id") if task else None
        error = envelope.get("last_error")
        rows.append({
            "request_id": request["request_id"], "project_id": project_id, "run_id": envelope.get("run_id"),
            "display_name": record.get("display_name") if record else None, "input": dict(request["input"]),
            "recognition_mode": request["recognition_mode"], "timing_mode": request["timing_mode"], "status": envelope["status"],
            "active_stage": active, "stage_started_at": started, "elapsed_seconds": _elapsed_seconds(started) if active else None, "progress": None,
            "machine_revision_id": machine, "human_revisions": len(record.get("review_revision_ids") or []) if record else 0,
            "quality": _machine_quality(project, machine) if machine else None,
            "error": {"code": error["code"], "stage": error.get("stage"), "message": error["message"], "kind": (error.get("details") or {}).get("kind")} if error else None,
            "created_at": request["created_at"], "updated_at": envelope["updated_at"],
        })
    return {"executor": {"live": live, "pid": (owner or {}).get("pid"), "started_at": (owner or {}).get("started_at")},
            "stop_requested": stop_flag_path(data_root).exists(), "requests": rows}


# --------------------------------------------------------------------------- task records

def _new_task(request: dict, run_id: str) -> dict:
    now = utc_now()
    return require_record("TaskRecord", {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "project_id": request["project_id"], "request_id": request["request_id"],
        "status": "QUEUED", "active_stage": None,
        "stages": [{"name": name, "status": "PENDING", "attempts": [], "input_signature": None, "output_manifest": None, "skip_reason": None,
                    "started_at": None, "ended_at": None, "error": None} for name in STAGES],
        "created_at": now, "updated_at": now, "last_error": None, "machine_revision_id": None, "extensions": {},
    })


def _stage(task: dict, name: str) -> dict:
    for stage in task["stages"]:
        if stage["name"] == name:
            return stage
    raise KeyError(name)


def _save_task(run: Path, task: dict) -> None:
    task["updated_at"] = utc_now()
    require_record("TaskRecord", task)
    atomic_write_text(run / "task.json", render_json(task))


def load_task(run: Path) -> dict:
    from upgrade.contracts import load_record
    return load_record("TaskRecord", run / "task.json")


def recover_run_pointers(project: Path, run: Path, task: dict) -> int:
    """Rebuild stage/attempt pointers from committed manifests (plan 10.2). Returns how many pointers were rebuilt."""
    rebuilt = 0
    stages_root = run / "stages"
    if not stages_root.is_dir():
        return 0
    for stage_dir in stages_root.iterdir():
        if not stage_dir.is_dir() or stage_dir.name not in STAGES:
            continue
        stage = _stage(task, stage_dir.name)
        for attempt_dir in stage_dir.iterdir():
            if not attempt_dir.is_dir() or attempt_dir.name == ".pending" or not (attempt_dir / MANIFEST_NAME).is_file():
                continue
            try:
                manifest = read_manifest(attempt_dir, "StageManifest")
                verify_committed(attempt_dir, manifest["outputs"])
            except ContractError:
                continue
            if manifest["run_id"] != task["run_id"] or manifest["request_id"] != task["request_id"] or manifest["stage"] != stage["name"]:
                continue
            pointer = artifact_ref(project, attempt_dir / MANIFEST_NAME)
            attempt = next((a for a in stage["attempts"] if a["attempt_id"] == manifest["attempt_id"]), None)
            if attempt is None:
                attempt = {"attempt_id": manifest["attempt_id"], "status": "DONE", "started_at": manifest["committed_at"], "ended_at": manifest["committed_at"],
                           "input_signature": manifest["input_signature"], "output_manifest": pointer, "error": None}
                stage["attempts"].append(attempt)
                rebuilt += 1
            elif attempt.get("output_manifest") is None or attempt["status"] != "DONE":
                attempt.update({"status": "DONE", "output_manifest": pointer, "ended_at": attempt.get("ended_at") or manifest["committed_at"], "error": None})
                rebuilt += 1
            if stage["status"] != "DONE" or stage.get("output_manifest") is None:
                stage.update({"status": "DONE", "input_signature": manifest["input_signature"], "output_manifest": pointer,
                              "ended_at": stage.get("ended_at") or manifest["committed_at"], "error": None})
                rebuilt += 1
    return rebuilt


def recover(data_root: Path | str) -> dict:
    """Startup scan: take the executor lock briefly; if nobody holds it, RUNNING work becomes INTERRUPTED and
    committed pointers are rebuilt. If an executor is live, nothing is touched (PID is never used to decide)."""
    from upgrade.locks import EXECUTOR_LOCK, ByteLock
    probe = ByteLock(Path(data_root) / EXECUTOR_LOCK)
    if not probe.acquire():
        return {"skipped": True, "reason": "an executor holds the lock", "interrupted": [], "pointers_rebuilt": 0}
    try:
        return _recover_owned(Path(data_root))
    finally:
        probe.release()


# --------------------------------------------------------------------------- stage engine

@dataclass
class StageContext:
    data_root: Path
    project_dir: Path
    run_dir: Path
    attempt_dir: Path
    request: dict
    task: dict
    config: dict
    engine_settings: dict
    cancel: CancelToken
    emit: EventSink
    owner_token: str
    outputs: dict[str, dict] = field(default_factory=dict)
    attempt_id: str = ""                                     # id of the attempt currently being produced
    scratch: dict[str, Any] = field(default_factory=dict)    # per-request memo shared by stage implementations (never persisted)

    def output_dir(self, stage: str) -> Path:
        if stage not in self.outputs:
            raise ContractError("STAGE_FAILED", f"stage {stage} has no committed output in this run", details={"stage": stage})
        return self.outputs[stage]["dir"]

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise ContractError("PROCESS_CANCELLED", self.cancel.reason or "cancelled")


class StageImpl(Protocol):
    name: str

    def signature_inputs(self, ctx: StageContext) -> dict: ...

    def run(self, ctx: StageContext) -> dict: ...


def stage_signature(name: str, request: dict, inputs: dict) -> str:
    """Cache signature of one stage (plan 10.3). It covers the stage, its algorithm version and the dependencies the
    implementation declares (content identities, parameters, tool identities, upstream signatures). The request
    fingerprint is deliberately not part of it: changing e.g. timing_mode or a translation parameter must not
    re-run OCR/ASR. The ``request`` parameter is kept so existing callers do not change."""
    return signature({"stage": name, "stage_version": STAGE_VERSIONS[name], "inputs": inputs})


class QueueRunner:
    """Serial executor for one DATA_ROOT. ``run_queue`` holds the executor lock until the queue is exhausted."""

    def __init__(self, data_root: Path | str, config: dict, engine_settings: dict, stages: dict[str, StageImpl], *,
                 event_sink: EventSink | None = None):
        self.data_root = Path(data_root)
        self.config = config
        self.engine_settings = engine_settings
        self.stages = stages
        self.event_sink = event_sink or (lambda event: None)
        self.owner_token = new_id()

    # -- queue loop
    def run_queue(self, *, cancel: CancelToken | None = None, continue_on_error: bool | None = None) -> dict:
        cancel = cancel or CancelToken()
        if continue_on_error is None:
            continue_on_error = bool(self.config["defaults"]["continue_on_error"])
        summary: dict[str, Any] = {"processed": 0, "completed": 0, "failed": 0, "cancelled": 0, "interrupted": 0, "recovered": None, "requests": [],
                                   "paused": False}
        stop_flag = stop_flag_path(self.data_root)
        with ExecutorLock(self.data_root) as lock:
            self.owner_token = lock.owner_token
            if stop_flag.exists():
                stop_flag.unlink()        # left over from a stop that arrived as the previous executor ended; starting is the user's new decision
            summary["recovered"] = _recover_owned(self.data_root)  # we hold the lock: no other executor is live
            while not cancel.is_set():
                if stop_flag.exists():
                    stop_flag.unlink()
                    summary["paused"] = True
                    _queue_event(self.data_root, {"event": "queue_paused"})
                    break
                queued = list_envelopes(self.data_root, status="QUEUED")
                if not queued:
                    break
                envelope = queued[0]
                request_id = envelope["request"]["request_id"]
                if cancel_flag_path(self.data_root, request_id).exists():
                    cancel_flag_path(self.data_root, request_id).unlink()
                    envelope["status"] = "CANCELLED"
                    envelope["last_error"] = ContractError("PROCESS_CANCELLED", "cancelled before start").to_dict()
                    _save_envelope(self.data_root, envelope)
                    summary["processed"] += 1
                    summary["cancelled"] += 1
                    continue
                status = self._run_request(envelope, cancel)
                summary["processed"] += 1
                summary[{"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "cancelled", "INTERRUPTED": "interrupted"}[status]] += 1
                finished = load_envelope(self.data_root, request_id)
                project = project_dir(self.data_root, finished["request"]["project_id"])
                task = load_task(run_dir(project, finished["run_id"])) if finished.get("run_id") else None
                revision_id = task.get("machine_revision_id") if task else None
                summary["requests"].append({"request_id": request_id, "project_id": finished["request"]["project_id"], "run_id": finished.get("run_id"), "status": status,
                                            "machine_revision_id": revision_id,
                                            "machine_revision_dir": str(project / "revisions" / "machine" / revision_id) if revision_id else None,
                                            "error": finished["last_error"]["code"] if finished.get("last_error") else None})
                if status == "FAILED" and not continue_on_error:
                    break
        return summary

    # -- one request
    def _run_request(self, envelope: dict, global_cancel: CancelToken) -> str:
        request = envelope["request"]
        request_id = request["request_id"]
        project = project_dir(self.data_root, request["project_id"])
        run_id = envelope.get("run_id") or new_id()
        run = run_dir(project, run_id)
        run.mkdir(parents=True, exist_ok=True)
        if not (run / "request.json").exists():
            atomic_write_text(run / "request.json", render_json(request))
        task = load_task(run) if (run / "task.json").is_file() else _new_task(request, run_id)
        recover_run_pointers(project, run, task)
        task["status"] = "RUNNING"
        task["last_error"] = None
        task["extensions"]["owner"] = {"owner_token": self.owner_token, "hostname": socket.gethostname()}
        _save_task(run, task)
        envelope["status"] = "RUNNING"
        envelope["run_id"] = run_id
        _save_envelope(self.data_root, envelope)
        update_project(self.data_root, request["project_id"], lambda record: record["run_ids"].append(run_id) if run_id not in record["run_ids"] else None)
        _run_event(run, {"event": "run_started", "request_id": request_id, "owner_token": self.owner_token})

        cancel = CancelToken()
        stop_watch = threading.Event()
        watcher = threading.Thread(target=self._watch_cancel, args=(request_id, cancel, global_cancel, stop_watch), daemon=True)
        watcher.start()
        emit: EventSink = lambda event: (_run_event(run, event), self.event_sink({"run_id": run_id, **event}))
        ctx = StageContext(self.data_root, project, run, run, request, task, self.config, self.engine_settings, cancel, emit, self.owner_token)
        final_status = "COMPLETED"
        try:
            for name in STAGES:
                outcome = self._run_stage(name, ctx)
                if outcome in ("CANCELLED", "FAILED"):
                    final_status = outcome
                    break
        finally:
            stop_watch.set()
            watcher.join(timeout=2)
        flag = cancel_flag_path(self.data_root, request_id)
        if flag.exists():
            flag.unlink()
        if final_status == "CANCELLED" and cancel.reason == "queue_stop":
            final_status = "INTERRUPTED"          # "stop current": the request stays resumable; "skip current" stays CANCELLED
            stopped = next((stage["name"] for stage in task["stages"] if stage["status"] == "CANCELLED"), None)
            task["last_error"] = ContractError("PROCESS_CANCELLED", "stopped by the user; resume to continue", stage=stopped, retryable=True,
                                               details={"kind": "queue_stop"}).to_dict()
        task["status"] = final_status
        task["active_stage"] = None
        _save_task(run, task)
        envelope["status"] = final_status
        envelope["last_error"] = task.get("last_error")
        _save_envelope(self.data_root, envelope)
        if task.get("machine_revision_id"):
            revision_id = task["machine_revision_id"]
            update_project(self.data_root, request["project_id"],
                           lambda record: record["machine_revision_ids"].append(revision_id) if revision_id not in record["machine_revision_ids"] else None)
        _run_event(run, {"event": "run_finished", "status": final_status})
        _queue_event(self.data_root, {"event": "request_finished", "request_id": request_id, "run_id": run_id, "status": final_status})
        return final_status

    def _watch_cancel(self, request_id: str, cancel: CancelToken, global_cancel: CancelToken, stop: threading.Event) -> None:
        flag = cancel_flag_path(self.data_root, request_id)
        stop_flag = stop_flag_path(self.data_root)
        while not stop.is_set():
            if global_cancel.is_set():
                cancel.cancel(global_cancel.reason or "executor_stop")
                return
            if stop_flag.exists():
                cancel.cancel("queue_stop")
                return
            if flag.exists():
                cancel.cancel("user_request")
                return
            stop.wait(CANCEL_POLL_SECONDS)

    # -- one stage
    def _run_stage(self, name: str, ctx: StageContext) -> str:
        task, request, run, project = ctx.task, ctx.request, ctx.run_dir, ctx.project_dir
        stage = _stage(task, name)
        skip_reason = MODE_SKIPS[request["recognition_mode"]].get(name)
        if skip_reason:
            if stage["status"] != "SKIPPED":
                stage.update({"status": "SKIPPED", "skip_reason": skip_reason, "started_at": utc_now(), "ended_at": utc_now(), "error": None})
                _save_task(run, task)
                ctx.emit({"event": "stage_skipped", "stage": name, "reason": skip_reason})
            return "SKIPPED"
        if ctx.cancel.is_set():
            return self._mark_cancelled(ctx, name, None)
        impl = self.stages.get(name)
        if impl is None:
            error = ContractError("STAGE_UNAVAILABLE", f"this build has no implementation for stage {name}", details={"stage": name})
            return self._mark_failed(ctx, name, None, error)
        try:
            sig = stage_signature(name, request, impl.signature_inputs(ctx))
        except ContractError as exc:
            return self._mark_failed(ctx, name, None, exc)
        except Exception as exc:  # noqa: BLE001 - implementation bug must surface as a failed stage, not a crash
            return self._mark_failed(ctx, name, None, ContractError("STAGE_FAILED", f"{name} signature failed: {type(exc).__name__}: {exc}"))

        # cache: identical signature + verified committed manifest (this run first, then earlier runs of the project)
        reuse = self._find_committed(project, run, name, sig)
        if reuse is not None:
            manifest, attempt_dir, source_run = reuse
            pointer = artifact_ref(project, attempt_dir / MANIFEST_NAME)
            if not any(a["attempt_id"] == manifest["attempt_id"] for a in stage["attempts"]):
                stage["attempts"].append({"attempt_id": manifest["attempt_id"], "status": "DONE", "started_at": manifest["committed_at"],
                                          "ended_at": manifest["committed_at"], "input_signature": sig, "output_manifest": pointer, "error": None})
            stage.update({"status": "DONE", "input_signature": sig, "output_manifest": pointer, "ended_at": stage.get("ended_at") or utc_now(), "error": None})
            reused_validation = _read_validation(attempt_dir)
            if isinstance(reused_validation.get("machine_revision_id"), str):
                task["machine_revision_id"] = reused_validation["machine_revision_id"]   # the same committed revision serves this run
            _save_task(run, task)
            ctx.outputs[name] = {"manifest": manifest, "dir": attempt_dir}
            ctx.emit({"event": "stage_cache_hit", "stage": name, "attempt_id": manifest["attempt_id"], "source_run": source_run, "cache_hit": True})
            return "DONE"
        if stage["status"] == "DONE":
            self._invalidate_downstream(ctx, name, "inputs or settings changed")

        attempt_id = new_id()
        pending = begin_pending(run / "stages" / name)
        attempt = {"attempt_id": attempt_id, "status": "RUNNING", "started_at": utc_now(), "ended_at": None, "input_signature": sig,
                   "output_manifest": None, "error": None}
        stage["attempts"].append(attempt)
        stage.update({"status": "RUNNING", "input_signature": sig, "started_at": utc_now(), "ended_at": None, "error": None, "output_manifest": None})
        task["active_stage"] = name
        _save_task(run, task)
        ctx.emit({"event": "stage_started", "stage": name, "attempt_id": attempt_id, "attempt": len(stage["attempts"])})
        ctx.attempt_dir = pending
        ctx.attempt_id = attempt_id
        try:
            validation = impl.run(ctx)
            ctx.check_cancelled()
            if not isinstance(validation, dict):
                raise ContractError("STAGE_FAILED", f"{name} returned no validation payload")
            atomic_write_text(pending / "validation.json", render_json(validation))
            final_dir = run / "stages" / name / attempt_id

            def build_manifest(refs: list[dict]) -> dict:
                validation_ref = next(ref for ref in refs if ref["path"] == "validation.json")
                return {"schema_version": SCHEMA_VERSION, "run_id": task["run_id"], "request_id": request["request_id"], "stage": name,
                        "attempt_id": attempt_id, "input_signature": sig, "outputs": refs, "validation_ref": validation_ref, "committed_at": utc_now()}

            committed = commit_pending(pending, final_dir, manifest_kind="StageManifest", build_manifest=build_manifest)
        except ContractError as exc:
            if exc.code == "PROCESS_CANCELLED" or ctx.cancel.is_set():
                return self._mark_cancelled(ctx, name, attempt)
            return self._mark_failed(ctx, name, attempt, exc)
        except Exception as exc:  # noqa: BLE001
            if ctx.cancel.is_set():
                return self._mark_cancelled(ctx, name, attempt)
            return self._mark_failed(ctx, name, attempt, ContractError("STAGE_FAILED", f"{name} failed: {type(exc).__name__}: {exc}"))
        # manifest is committed; now (and only now) update the pointers
        pointer = artifact_ref(project, final_dir / MANIFEST_NAME)
        attempt.update({"status": "DONE", "ended_at": utc_now(), "output_manifest": pointer})
        stage.update({"status": "DONE", "ended_at": utc_now(), "output_manifest": pointer, "error": None})
        task["active_stage"] = None
        if isinstance(validation.get("machine_revision_id"), str):
            task["machine_revision_id"] = validation["machine_revision_id"]
        _save_task(run, task)
        ctx.outputs[name] = {"manifest": committed["manifest"], "dir": final_dir}
        ctx.emit({"event": "stage_committed", "stage": name, "attempt_id": attempt_id, "outputs": len(committed["artifact_refs"])})
        return "DONE"

    def _find_committed(self, project: Path, run: Path, name: str, sig: str) -> tuple[dict, Path, str] | None:
        runs_root = project / "runs"
        candidates = [run] + sorted((p for p in runs_root.iterdir() if p.is_dir() and p != run), key=lambda p: p.name) if runs_root.is_dir() else [run]
        for candidate_run in candidates:
            stage_root = candidate_run / "stages" / name
            if not stage_root.is_dir():
                continue
            for attempt_dir in sorted(stage_root.iterdir()):
                if not attempt_dir.is_dir() or attempt_dir.name == ".pending" or not (attempt_dir / MANIFEST_NAME).is_file():
                    continue
                try:
                    manifest = read_manifest(attempt_dir, "StageManifest")
                    if manifest["stage"] != name or manifest["input_signature"] != sig:
                        continue
                    verify_committed(attempt_dir, manifest["outputs"])
                except ContractError:
                    continue
                return manifest, attempt_dir, candidate_run.name
        return None

    def _invalidate_downstream(self, ctx: StageContext, name: str, reason: str) -> None:
        passed = False
        for stage in ctx.task["stages"]:
            if stage["name"] == name:
                passed = True
            if passed and stage["status"] in ("DONE", "FAILED", "INTERRUPTED", "CANCELLED"):
                stage.update({"status": "PENDING", "input_signature": None, "output_manifest": None, "error": None, "started_at": None, "ended_at": None})
        ctx.emit({"event": "stages_invalidated", "from_stage": name, "reason": reason})

    def _mark_failed(self, ctx: StageContext, name: str, attempt: dict | None, error: ContractError) -> str:
        task, stage = ctx.task, _stage(ctx.task, name)
        payload = error.to_dict()
        if attempt is not None:
            attempt.update({"status": "FAILED", "ended_at": utc_now(), "error": payload})
        stage.update({"status": "FAILED", "ended_at": utc_now(), "error": payload})
        task.update({"status": "FAILED", "active_stage": None, "last_error": payload})
        _save_task(ctx.run_dir, task)
        ctx.emit({"event": "stage_failed", "stage": name, "code": error.code, "message": error.message, "pending_kept": True})
        return "FAILED"

    def _mark_cancelled(self, ctx: StageContext, name: str, attempt: dict | None) -> str:
        task, stage = ctx.task, _stage(ctx.task, name)
        payload = ContractError("PROCESS_CANCELLED", ctx.cancel.reason or "cancelled", retryable=True).to_dict()
        if attempt is not None:
            attempt.update({"status": "CANCELLED", "ended_at": utc_now(), "error": payload})
        stage.update({"status": "CANCELLED", "ended_at": utc_now(), "error": payload})
        task.update({"status": "CANCELLED", "active_stage": None, "last_error": payload})
        _save_task(ctx.run_dir, task)
        ctx.emit({"event": "stage_cancelled", "stage": name, "reason": ctx.cancel.reason})
        return "CANCELLED"


def _read_validation(attempt_dir: Path) -> dict:
    from upgrade.contracts import parse_json_text
    path = attempt_dir / "validation.json"
    try:
        value = parse_json_text(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _recover_owned(data_root: Path) -> dict:
    """Recovery while *we* hold the executor lock (so executor_is_live would be true for ourselves)."""
    interrupted: list[str] = []
    rebuilt = 0
    for envelope in list_envelopes(data_root):
        request = envelope["request"]
        project = project_dir(data_root, request["project_id"])
        run = run_dir(project, envelope["run_id"]) if envelope.get("run_id") else None
        task = load_task(run) if run and (run / "task.json").is_file() else None
        if task is not None:
            rebuilt += recover_run_pointers(project, run, task)
        if envelope["status"] == "RUNNING":
            error = ContractError("PROCESS_FAILED", "executor ended before the stage checkpoint completed", retryable=True,
                                  details={"kind": "unclean_shutdown"}).to_dict()
            if task is not None:
                for stage in task["stages"]:
                    for attempt in stage["attempts"]:
                        if attempt["status"] == "RUNNING":
                            attempt.update({"status": "INTERRUPTED", "ended_at": utc_now(), "error": error})
                    if stage["status"] == "RUNNING":
                        stage.update({"status": "INTERRUPTED", "ended_at": utc_now(), "error": error})
                task.update({"status": "INTERRUPTED", "active_stage": None, "last_error": error})
                _save_task(run, task)
                _run_event(run, {"event": "task_recovered", "status": "INTERRUPTED"})
            envelope["status"] = "INTERRUPTED"
            envelope["last_error"] = error
            _save_envelope(data_root, envelope)
            interrupted.append(request["request_id"])
            _queue_event(data_root, {"event": "request_interrupted", "request_id": request["request_id"], "reason": "unclean_shutdown"})
        elif task is not None and rebuilt:
            _save_task(run, task)
    return {"skipped": False, "interrupted": interrupted, "pointers_rebuilt": rebuilt}


__all__ = [
    "STAGE_VERSIONS", "MODE_SKIPS", "StageContext", "StageImpl", "QueueRunner", "build_job_request", "submit", "cancel", "resume", "recover",
    "request_stop", "stop_flag_path", "queue_rows",
    "recover_run_pointers", "list_envelopes", "load_envelope", "load_task", "create_project", "load_project", "update_project",
    "envelope_path", "project_dir", "run_dir", "stage_signature",
]
