#!/usr/bin/env python3
"""Legacy inbox takeover (plan 1.1, section 10.2 "inbox 旧 pending/working 兼容").

The old ``roast_inbox.py`` watched ``app/inbox`` for .txt/.url/.json requests and moved them through working/done/failed.
v2 ingests those files into the persistent queue:

1. hash the file; if the hash is already mapped to a request_id whose envelope exists, the file is a duplicate ->
   move it to ``taken-over/`` without creating a second run;
2. otherwise parse it with the legacy parser, build a JobRequest through the caller's factory, ``submit`` it,
   write the mapping (hash -> request_id), and only then move the source file to ``taken-over/``;
3. leftovers in ``working/`` (a crash of the old watcher) are copied to ``taken-over/recovered-*`` first and then
   ingested like pending files; a crash between mapping and move is healed by step 1 on the next intake.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from upgrade.contracts import ContractError, new_id, parse_json_text, sha256_file, utc_now
from upgrade.runner import envelope_path, submit
from upgrade.storage import atomic_write_text, render_json

MAPPING_NAME = ".v2-intake.json"
TAKEN_OVER_DIR = "taken-over"
SUPPORTED_SUFFIXES = {".txt", ".url", ".json"}
RequestFactory = Callable[[dict, Path], dict]


def _legacy_parse(path: Path) -> dict:
    try:
        import roast_inbox  # legacy module on app/tools; parse_request is a pure function
    except ImportError as exc:  # pragma: no cover
        raise ContractError("STAGE_UNAVAILABLE", "legacy roast_inbox module is not importable") from exc
    request = roast_inbox.parse_request(path)
    if not isinstance(request, dict):
        raise ContractError("SCHEMA_INVALID", "legacy request did not parse to an object", details={"path": str(path)})
    return request


def load_mapping(inbox: Path) -> dict:
    path = inbox / MAPPING_NAME
    if not path.is_file():
        return {"schema_version": 2, "entries": {}}
    try:
        value = parse_json_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {"schema_version": 2, "entries": {}}
    if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
        return {"schema_version": 2, "entries": {}}
    return value


def _save_mapping(inbox: Path, mapping: dict) -> None:
    atomic_write_text(inbox / MAPPING_NAME, render_json(mapping))


def _unique_destination(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if destination.exists():
        stem, suffix = Path(name).stem, Path(name).suffix
        destination = directory / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}_{new_id()[:8]}{suffix}"
    return destination


def _is_request_file(path: Path) -> bool:
    # dot-files (including our own mapping) and status/tmp files are never requests
    return path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES and not path.name.startswith(".") and path.name != MAPPING_NAME


def pending_files(inbox: Path) -> list[Path]:
    files = [p for p in inbox.iterdir() if _is_request_file(p)] if inbox.is_dir() else []
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def working_files(inbox: Path) -> list[Path]:
    working = inbox / "working"
    files = [p for p in working.iterdir() if _is_request_file(p)] if working.is_dir() else []
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def intake(data_root: Path | str, inbox: Path | str, request_factory: RequestFactory, *, event_sink: Callable[[dict], None] | None = None) -> list[dict]:
    """Ingest legacy inbox files into the v2 queue. Returns one summary row per file."""
    data_root = Path(data_root)
    inbox = Path(inbox)
    emit = event_sink or (lambda event: None)
    results: list[dict] = []
    mapping = load_mapping(inbox)
    taken = inbox / TAKEN_OVER_DIR
    work_items = [(path, "pending") for path in pending_files(inbox)] + [(path, "working") for path in working_files(inbox)]
    for path, origin in work_items:
        row = {"source": str(path), "origin": origin, "sha256": None, "request_id": None, "action": None, "error": None}
        try:
            digest = sha256_file(path)
            row["sha256"] = digest
            if origin == "working":
                copy = _unique_destination(taken, f"recovered-{path.name}")
                shutil.copy2(path, copy)
                row["recovered_copy"] = str(copy)
                emit({"event": "inbox_working_recovered", "source": str(path), "copy": str(copy)})
            mapped = mapping["entries"].get(digest)
            if mapped and envelope_path(data_root, mapped["request_id"]).exists():
                row["request_id"] = mapped["request_id"]
                row["action"] = "duplicate"
                emit({"event": "inbox_duplicate", "source": str(path), "request_id": mapped["request_id"]})
            else:
                parsed = _legacy_parse(path)
                request = request_factory(parsed, path)
                envelope = submit(data_root, request, display_name=str(parsed.get("job_name") or path.stem))
                mapping["entries"][digest] = {"request_id": envelope["request"]["request_id"], "source_name": path.name, "origin": origin,
                                              "taken_over_at": utc_now()}
                _save_mapping(inbox, mapping)
                row["request_id"] = envelope["request"]["request_id"]
                row["action"] = "queued"
                emit({"event": "inbox_queued", "source": str(path), "request_id": row["request_id"]})
            destination = _unique_destination(taken, path.name)
            shutil.move(str(path), str(destination))
            row["moved_to"] = str(destination)
        except ContractError as exc:
            row["action"] = "failed"
            row["error"] = exc.to_dict()
            emit({"event": "inbox_failed", "source": str(path), "code": exc.code, "message": exc.message})
        except (OSError, ValueError) as exc:
            row["action"] = "failed"
            row["error"] = ContractError("SCHEMA_INVALID", f"{type(exc).__name__}: {exc}").to_dict()
            emit({"event": "inbox_failed", "source": str(path), "code": "SCHEMA_INVALID", "message": str(exc)})
        results.append(row)
    return results


__all__ = ["MAPPING_NAME", "TAKEN_OVER_DIR", "SUPPORTED_SUFFIXES", "intake", "pending_files", "working_files", "load_mapping"]
