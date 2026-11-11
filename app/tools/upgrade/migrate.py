#!/usr/bin/env python3
"""Read-only migration of one old ``app/work/<job>`` task into a new v2 project (plan 9.6; work package W08).

The old directory is only read: nothing is added to the old task.json, no candidate path changes, and its files are
hashed before and after (a change during the migration is ARTIFACT_CHANGED). The new project gets
``legacy/source-manifest.json`` (source directory, import time, every small file's hash, old program/stage versions,
the media reference) and byte copies of the small text files under ``legacy/files/``. Large files (videos) are only
referenced. Nothing is converted into a v2 artifact: an old DONE is not a v2 DONE, old ``final.ass`` /
``zh_machine.srt`` are listed as unreviewed legacy snapshots, and whether a person edited them is never guessed from
file times — the user registers a hand-edited file explicitly with ``review import``. Errors only remove the new
``.pending`` directory.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from upgrade.contracts import SCHEMA_VERSION, ContractError, new_id, sha256_file, utc_now
from upgrade.storage import atomic_write_text, render_json

COPY_SUFFIXES = (".json", ".jsonl", ".srt", ".ass", ".ssa", ".csv", ".txt", ".log")
COPY_LIMIT_BYTES = 20 * 1024 * 1024
SNAPSHOT_NAMES = ("deliverables/final.ass", "subs/zh_machine.srt", "subs/jp_ocr.srt", "subs/timeline.ass")


def _inventory(job_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(job_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(job_dir).as_posix()
            rows.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path), "mtime_ns": path.stat().st_mtime_ns})
    return rows


def read_legacy_task(job_dir: Path) -> dict:
    task_path = Path(job_dir) / "task.json"
    if not task_path.is_file():
        raise ContractError("INPUT_MISSING", "this directory has no task.json; it is not an old task directory", details={"path": str(job_dir)})
    try:
        task = json.loads(task_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ContractError("SCHEMA_INVALID", f"the old task.json cannot be read: {exc}", details={"path": str(task_path)}) from exc
    if not isinstance(task, dict) or not isinstance(task.get("stages"), dict) or "status" not in task:
        raise ContractError("SCHEMA_UNSUPPORTED", "task.json does not look like an old task record (no stages/status)", details={"path": str(task_path)})
    return task


def legacy_video(job_dir: Path, task: dict) -> Path | None:
    """The media the old task worked on: its own copied source first, then the recorded input path. Only a reference."""
    manifest_path = Path(job_dir) / "job_manifest.json"
    candidates = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            candidates.append(((manifest.get("source") or {}).get("video")))
        except (OSError, ValueError):
            pass
    candidates.append((task.get("source") or {}).get("video_file"))
    for value in candidates:
        if isinstance(value, str) and value and Path(value).is_file():
            return Path(value)
    return None


def build_legacy(job_dir: Path, pending: Path, task: dict, inventory: list[dict]) -> dict:
    files = []
    for row in inventory:
        copy = Path(row["path"]).suffix.lower() in COPY_SUFFIXES and row["bytes"] <= COPY_LIMIT_BYTES
        if copy:
            target = pending / "files" / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(job_dir / row["path"], target)
            if sha256_file(target) != row["sha256"]:
                raise ContractError("ARTIFACT_CHANGED", "an old file changed while it was copied", details={"path": row["path"]})
        files.append({"path": row["path"], "bytes": row["bytes"], "sha256": row["sha256"], "copied": copy})
    job_manifest = {}
    if (job_dir / "job_manifest.json").is_file():
        try:
            job_manifest = json.loads((job_dir / "job_manifest.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            job_manifest = {}
    known = {row["path"] for row in inventory}
    return {
        "schema_version": SCHEMA_VERSION, "record_kind": "legacy_source_manifest", "source_dir": str(job_dir), "imported_at": utc_now(),
        "task": {"task_id": task.get("task_id"), "display_name": task.get("display_name"), "status": task.get("status"), "task_schema_version": task.get("schema_version"),
                 "stages": {name: (stage or {}).get("status") for name, stage in task["stages"].items()}, "created_at": task.get("created_at"), "updated_at": task.get("updated_at")},
        "program": {key: job_manifest.get(key) for key in ("program_version", "pipeline_version", "segmentation_version", "segmentation", "versions") if key in job_manifest},
        "files": files,
        "machine_snapshots": [{"path": name, "status": "UNREVIEWED_LEGACY_SNAPSHOT"} for name in SNAPSHOT_NAMES if name in known],
        "legacy_status_note": "the old status is shown as history only: an old DONE is not a v2 DONE and no old file is reused as a v2 artifact",
        "human_edit_note": "whether a person edited an old subtitle is not guessed from file times; register a hand-edited file with review import (provenance unaudited)",
    }


def migrate_job(data_root: Path, job_dir: Path, *, display_name: str | None, register_media) -> dict:
    """``register_media(project_id, project_dir, video_path) -> media_id`` or None to only reference the media."""
    from upgrade.runner import create_project, project_dir, update_project
    job_dir = Path(job_dir).resolve()
    task = read_legacy_task(job_dir)
    before = _inventory(job_dir)
    record = create_project(data_root, display_name or str(task.get("display_name") or job_dir.name))
    project_id = record["project_id"]
    project = project_dir(data_root, project_id)
    pending = project / ".pending" / f"migrate-{new_id()}"
    pending.mkdir(parents=True)
    try:
        manifest = build_legacy(job_dir, pending, task, before)
        video = legacy_video(job_dir, task)
        media_id = register_media(project_id, project, video) if (video is not None and register_media is not None) else None
        manifest["media_reference"] = {"path": str(video) if video else None, "exists": video is not None,
                                       "sha256": sha256_file(video) if video else None, "media_id": media_id, "copied": False}
        after = _inventory(job_dir)
        if [(row["path"], row["sha256"], row["bytes"]) for row in after] != [(row["path"], row["sha256"], row["bytes"]) for row in before]:
            raise ContractError("ARTIFACT_CHANGED", "the old task directory changed during the migration; nothing was migrated", details={"path": str(job_dir)})
        atomic_write_text(pending / "source-manifest.json", render_json(manifest))
        target = project / "legacy"
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()                   # the empty folder every new project starts with (plan 4.2 layout)
        if target.exists():
            raise ContractError("OUTPUT_EXISTS", "this project already has a legacy import", details={"project_id": project_id})
        os.replace(pending, target)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    def index(item: dict) -> None:
        item["extensions"]["legacy_import"] = {"source_dir": str(job_dir), "imported_at": manifest["imported_at"], "old_status": task.get("status")}
        if media_id and media_id not in item["media_ids"]:
            item["media_ids"].append(media_id)
    update_project(data_root, project_id, index)
    return {"project_id": project_id, "media_id": media_id, "manifest": manifest}


def legacy_summary(manifest: dict) -> dict[str, Any]:
    return {"old_status": manifest["task"]["status"], "files": len(manifest["files"]), "copied": sum(1 for row in manifest["files"] if row["copied"]),
            "machine_snapshots": [row["path"] for row in manifest["machine_snapshots"]], "media_reference": manifest["media_reference"]}
