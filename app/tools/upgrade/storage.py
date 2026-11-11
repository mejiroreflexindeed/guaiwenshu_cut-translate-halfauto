#!/usr/bin/env python3
"""DATA_ROOT layout, write checks, path safety and the immutable commit primitive (plan 1.1, sections 4.2, 4.3).

Rules implemented here:
- a data root is accepted only after a real write probe succeeds; network shares are refused;
- managed paths are relative POSIX, must resolve inside their base, never through '..' or reparse points;
- revision/export directories are created exactly once: files are staged under ``.pending/``, hashed,
  described by a manifest, then renamed into place; an existing target is OUTPUT_EXISTS;
- verifying a committed directory re-hashes every listed file; any difference is ARTIFACT_CHANGED.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from upgrade.contracts import (
    ContractError, is_safe_relative_posix, new_id, require_record, sha256_file, utc_now, windows_name_problem,
)

DATA_ROOT_DIRS = ("settings", "queue/requests", "locks", "projects")
PROJECT_DIRS = ("media", "legacy", "runs", "revisions/machine", "revisions/source", "revisions/review",
                "reviews/sessions", "reviews/attestations", "exports", "logs", ".pending")
MEDIA_TYPES = {
    ".json": "application/json", ".jsonl": "application/x-ndjson", ".srt": "application/x-subrip", ".ass": "text/x-ssa",
    ".txt": "text/plain", ".log": "text/plain", ".csv": "text/csv", ".html": "text/html", ".md": "text/markdown",
    ".wav": "audio/wav", ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".yaml": "application/yaml", ".yml": "application/yaml",
}
MANIFEST_NAME = "manifest.json"


def media_type_for(path: Path | str) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def render_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


# --------------------------------------------------------------------------- atomic small writes

def atomic_write_bytes(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


# --------------------------------------------------------------------------- path safety

def is_network_path(path: Path | str) -> bool:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True
    if sys.platform == "win32":
        drive = os.path.splitdrive(os.path.abspath(text))[0]
        if drive and len(drive) == 2:
            try:
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
            except (AttributeError, OSError):
                return False
    return False


def check_path_components(path: Path | str) -> None:
    """Reject Windows-invalid characters and reserved names in every component (plan 4.3)."""
    text = str(path)
    if "\0" in text:
        raise ContractError("PATH_INVALID", "path contains NUL", details={"path": repr(text)})
    pure = Path(text)
    for part in pure.parts:
        if part == pure.anchor or part in ("/", "\\"):
            continue
        problem = windows_name_problem(part.rstrip("\\/")) if sys.platform == "win32" else None
        if problem:
            raise ContractError("PATH_INVALID", f"path component {part!r} {problem}", details={"path": text})


def safe_join(base: Path, relative: str) -> Path:
    """Join a managed relative POSIX path under ``base``; the result must stay inside ``base`` after realpath."""
    if not is_safe_relative_posix(relative):
        raise ContractError("PATH_INVALID", "managed path must be relative POSIX without '..'", details={"path": relative})
    base_real = Path(os.path.realpath(base))
    target = base_real.joinpath(*PurePosixPath(relative).parts)
    target_real = Path(os.path.realpath(target))
    try:
        target_real.relative_to(base_real)
    except ValueError as exc:
        raise ContractError("PATH_INVALID", "managed path escapes its base directory (link or reparse point)", details={"path": relative}) from exc
    return target


def ensure_ascii_root(path: Path | str) -> Path:
    text = str(path)
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ContractError("PATH_INVALID", "ASCII temp root must contain only ASCII characters", details={"path": text}) from exc
    return Path(text)


# --------------------------------------------------------------------------- data root

def probe_writable(directory: Path) -> None:
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-probe-{uuid.uuid4().hex}"
        with probe.open("wb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError as exc:
        raise ContractError("DATA_ROOT_UNWRITABLE", f"cannot write to {directory}: {exc}", details={"path": str(directory)}) from exc


def init_data_root(path: Path | str, *, create: bool = True) -> dict[str, Any]:
    """Validate and (optionally) create the DATA_ROOT layout. Returns a small report; never touches other directories."""
    root = Path(path)
    check_path_components(root)
    if is_network_path(root):
        raise ContractError("DATA_ROOT_UNWRITABLE", "network shares are not supported as DATA_ROOT in v2", details={"path": str(root)})
    if not create and not root.is_dir():
        raise ContractError("INPUT_MISSING", "DATA_ROOT does not exist", details={"path": str(root)})
    probe_writable(root)
    for relative in DATA_ROOT_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    return {"data_root": str(root.resolve()), "created_dirs": list(DATA_ROOT_DIRS), "probed_at": utc_now()}


def init_project_dir(data_root: Path | str, project_id: str) -> Path:
    project = Path(data_root) / "projects" / project_id
    for relative in PROJECT_DIRS:
        (project / relative).mkdir(parents=True, exist_ok=True)
    return project


# --------------------------------------------------------------------------- immutable commit primitive

def fsync_file(path: Path) -> None:
    # Windows refuses fsync on a read-only handle; open read/write without truncating.
    with Path(path).open("rb+") as handle:
        os.fsync(handle.fileno())


def artifact_ref(base: Path, file_path: Path) -> dict:
    relative = PurePosixPath(*Path(file_path).relative_to(base).parts).as_posix()
    if not is_safe_relative_posix(relative):
        raise ContractError("PATH_INVALID", "artifact path is not a safe relative POSIX path", details={"path": relative})
    stat = Path(file_path).stat()
    return require_record("ArtifactRef", {"path": relative, "sha256": sha256_file(file_path), "bytes": stat.st_size,
                                          "media_type": media_type_for(file_path)})


def begin_pending(project_dir: Path | str) -> Path:
    pending = Path(project_dir) / ".pending" / uuid.uuid4().hex
    pending.mkdir(parents=True, exist_ok=False)
    return pending


def commit_pending(pending_dir: Path | str, final_dir: Path | str, *, manifest_kind: str,
                   build_manifest: Callable[[list[dict]], dict]) -> dict:
    """Hash every staged file, write ``manifest.json`` from ``build_manifest(artifact_refs)``, rename into place.

    - ``final_dir`` must not exist (OUTPUT_EXISTS). It is created exactly once by renaming the pending directory.
    - ``build_manifest`` receives the ArtifactRef list (paths relative to the directory) and returns a record of
      ``manifest_kind``; the record is validated before anything becomes visible.
    - On any failure the pending directory is left in place as a recoverable candidate; nothing is published.
    """
    pending = Path(pending_dir)
    final = Path(final_dir)
    if not pending.is_dir():
        raise ContractError("INPUT_MISSING", "pending directory does not exist", details={"path": str(pending)})
    check_path_components(final)
    if final.exists():
        raise ContractError("OUTPUT_EXISTS", "target revision/export directory already exists; refusing to overwrite",
                            details={"path": str(final)})
    refs: list[dict] = []
    for file_path in sorted(p for p in pending.rglob("*") if p.is_file()):
        if file_path.name == MANIFEST_NAME and file_path.parent == pending:
            raise ContractError("SCHEMA_INVALID", "pending directory must not contain manifest.json before commit", details={"path": str(file_path)})
        fsync_file(file_path)
        refs.append(artifact_ref(pending, file_path))
    manifest = build_manifest(refs)
    require_record(manifest_kind, manifest)
    manifest_path = pending / MANIFEST_NAME
    atomic_write_text(manifest_path, render_json(manifest))
    fsync_file(manifest_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(pending, final)
    except FileExistsError as exc:
        raise ContractError("OUTPUT_EXISTS", "target appeared during commit; pending candidate kept", details={"path": str(final)}) from exc
    return {"path": str(final), "manifest": manifest, "artifact_refs": refs}


def read_manifest(final_dir: Path | str, manifest_kind: str) -> dict:
    from upgrade.contracts import load_record
    return load_record(manifest_kind, Path(final_dir) / MANIFEST_NAME)


def verify_committed(final_dir: Path | str, artifact_refs: list[dict]) -> list[dict]:
    """Re-hash the listed artifacts; raise ARTIFACT_CHANGED on any missing/changed file. Returns the verified refs."""
    base = Path(final_dir)
    changed: list[dict] = []
    for ref in artifact_refs:
        try:
            target = safe_join(base, ref["path"])
        except ContractError as exc:
            changed.append({"path": ref.get("path"), "problem": exc.message})
            continue
        if not target.is_file():
            changed.append({"path": ref["path"], "problem": "missing"})
            continue
        stat = target.stat()
        if stat.st_size != ref["bytes"] or sha256_file(target) != ref["sha256"]:
            changed.append({"path": ref["path"], "problem": "content differs from manifest"})
    if changed:
        raise ContractError("ARTIFACT_CHANGED", "committed artifacts no longer match their manifest; the directory is treated as damaged",
                            details={"path": str(base), "changed": changed})
    return artifact_refs


def list_pending(project_dir: Path | str) -> list[str]:
    pending_root = Path(project_dir) / ".pending"
    if not pending_root.is_dir():
        return []
    return sorted(str(p) for p in pending_root.iterdir() if p.is_dir())


__all__ = [
    "DATA_ROOT_DIRS", "PROJECT_DIRS", "MANIFEST_NAME", "media_type_for", "render_json", "atomic_write_bytes", "atomic_write_text",
    "is_network_path", "check_path_components", "safe_join", "ensure_ascii_root", "probe_writable", "init_data_root",
    "init_project_dir", "artifact_ref", "begin_pending", "commit_pending", "read_manifest", "verify_committed", "list_pending", "new_id",
]
