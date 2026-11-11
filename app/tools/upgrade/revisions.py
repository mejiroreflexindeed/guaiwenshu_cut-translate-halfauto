#!/usr/bin/env python3
"""Human revision loop (plan 1.1, sections 5, 6.5-6.8 and 9): review sessions, byte snapshots, attestations.

Four public functions; their names and outward behaviour are fixed by plan section 5:

* ``create_session``    copies one committed subtitle file into a new session directory. Only that copy is handed to an
                        editor; the file inside ``revisions/`` is never the working copy.
* ``import_revision``   registers a saved subtitle file as a new immutable revision (``review`` = human Chinese
                        subtitle, ``source`` = human Japanese source subtitle). The registered bytes are the user's
                        bytes: nothing is re-encoded, normalised, re-serialised, merged or rebased.
* ``attest_revision``   stores a person's confirmation of one subtitle file of one revision (text / timing / style).
                        It is a user action; the pipeline never calls it.
* ``resolve_revision``  returns one concrete revision by id after its files were checked against the manifest hashes.

Mapping to a base is deliberately light (plan 9.2 step 4): a cue is ``exact`` only when the format is the same and the
whole cue (ASS: every event field) and its time are identical and unique on both sides. Everything else is at most a
``candidate`` and is never used to pair, retranslate or overwrite anything automatically. There is no three-way merge.
"""

from __future__ import annotations

import copy
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from upgrade import media as media_module
from upgrade import quality as quality_module
from upgrade import source as source_module
from upgrade import subtitles
from upgrade.config import DEFAULT_QUALITY
from upgrade.contracts import (
    SCHEMA_VERSION, SCOPES, SUBTITLE_FORMATS, UUID4_RE, ContractError, load_record, new_id, require_record, sha256_bytes, sha256_file, utc_now,
)
from upgrade.locks import ProjectCommitLock
from upgrade.runner import load_project, project_dir as _project_dir, update_project
from upgrade.storage import (
    MANIFEST_NAME, artifact_ref, atomic_write_bytes, atomic_write_text, begin_pending, commit_pending, read_manifest, render_json, safe_join, verify_committed,
)

REVISION_KINDS = ("machine", "source", "review")
IMPORT_KINDS = ("source", "review")
MANIFEST_KINDS = {"machine": "MachineRevision", "source": "SourceRevision", "review": "ReviewRevision"}
BASE_KINDS = {"source": ("source",), "review": ("machine", "review")}   # what an import of each kind may be based on
VIEW_NAME = "view.json"
CORRESPONDENCES_NAME = "correspondences.json"
QUALITY_NAME = "quality.json"
SCOPE_NAME, REVIEW_CSV_NAME, REVIEW_HTML_NAME = "quality_scope.json", "review.csv", "review.html"
CANDIDATE_NAME = "candidate.json"
SESSION_RECORD_NAME = "base.json"
SOURCE_BOUNDS_TOLERANCE_MS = source_module.BOUNDS_TOLERANCE_MS   # a human source becomes millisecond segments (see _check)
MAX_REPORTED_ISSUES = 20


# --------------------------------------------------------------------------- lookup

def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not UUID4_RE.match(value):
        raise ContractError("PATH_INVALID", f"the {what} id is not a valid id", details={"what": what})
    return value


def _project(data_root: Path | str, project_id: str) -> Path:
    _check_id(project_id, "project")
    load_project(data_root, project_id)
    return _project_dir(data_root, project_id)


def _locate(project: Path, revision_id: str) -> tuple[str, Path]:
    _check_id(revision_id, "revision")
    for kind in REVISION_KINDS:
        directory = project / "revisions" / kind / revision_id
        if (directory / MANIFEST_NAME).is_file():
            return kind, directory
    raise ContractError("INPUT_MISSING", "the project has no committed revision with this id (a candidate under .pending is not a revision)",
                        details={"revision_id": revision_id})


def _project_ref(directory_rel: str, ref: dict) -> dict:
    return {**ref, "path": f"{directory_rel}/{ref['path']}"}


def _managed_refs(kind: str, manifest: dict) -> list[dict]:
    if kind == "machine":
        return [manifest["subtitle_files"]["srt"], manifest["subtitle_files"]["ass"], manifest["translations_ref"], manifest["quality_ref"], *manifest["raw_evidence_refs"]]
    if kind == "source":
        return source_module.revision_refs(manifest)
    return [manifest["original_file_ref"], manifest["validated_view_ref"], manifest["correspondences_ref"], manifest["quality_ref"],
            *(manifest.get("extensions") or {}).get("report_refs", [])]


def _subtitle_files(kind: str, manifest: dict) -> dict[str, dict]:
    if kind == "machine":
        return {fmt: manifest["subtitle_files"][fmt] for fmt in SUBTITLE_FORMATS}
    if kind == "source":
        return {subtitles.subtitle_format(manifest["source_subtitle_ref"]["path"]): manifest["source_subtitle_ref"]}
    return {manifest["format"]: manifest["original_file_ref"]}


def _is_human(kind: str, manifest: dict) -> bool:
    return kind == "review" or (kind == "source" and manifest["origin"] == "human")


def _attestations(project: Path) -> tuple[list[dict], list[str]]:
    """Every readable attestation of the project plus the names of files that are not valid attestations (never counted)."""
    directory = project / "reviews" / "attestations"
    items, unreadable = [], []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                items.append(load_record("Attestation", path))
            except ContractError:
                unreadable.append(path.name)
    return items, unreadable


def _newer(project: Path, record: dict, kind: str, revision_id: str, media_id: str) -> list[str]:
    """Machine results of the same media that were committed after ``revision_id`` (project index order)."""
    ids = record[f"{kind}_revision_ids"]
    if revision_id not in ids:
        return []
    newer = []
    for candidate in ids[ids.index(revision_id) + 1:]:
        directory = project / "revisions" / kind / candidate
        try:
            manifest = read_manifest(directory, MANIFEST_KINDS[kind])
        except (ContractError, OSError):
            continue
        if manifest["media_id"] == media_id and (kind != "source" or manifest["origin"] == "machine"):
            newer.append(candidate)
    return newer


def _superseded_by(project: Path, record: dict, kind: str, manifest: dict) -> list[str]:
    """Newer machine versions than the machine version a human revision ultimately rests on (plan 9.3). Empty for machine revisions."""
    seen: set[str] = set()
    while _is_human(kind, manifest):
        parent = manifest["base_revision_id"] if kind == "review" else manifest["parent_revision_id"]
        if not parent or manifest["revision_id"] in seen:
            return []
        seen.add(manifest["revision_id"])
        try:
            kind, directory = _locate(project, parent)
            manifest = read_manifest(directory, MANIFEST_KINDS[kind])
        except (ContractError, OSError):
            return []
        if not _is_human(kind, manifest):
            return _newer(project, record, kind, manifest["revision_id"], manifest["media_id"])
    return []


def _resolve(project: Path, project_id: str, revision_id: str) -> dict:
    kind, directory = _locate(project, revision_id)
    manifest = read_manifest(directory, MANIFEST_KINDS[kind])
    if manifest["revision_id"] != revision_id or manifest["project_id"] != project_id:
        raise ContractError("ARTIFACT_CHANGED", "the manifest in this revision directory describes another revision or project",
                            details={"rule_id": "QC04_IDENTITY", "revision_id": revision_id})
    verify_committed(directory, _managed_refs(kind, manifest))
    directory_rel = directory.relative_to(project).as_posix()
    files = _subtitle_files(kind, manifest)
    human = _is_human(kind, manifest)
    attestations, unreadable = _attestations(project)
    confirmations = {fmt: quality_module.confirmation(revision_id, ref["sha256"], manifest["media_id"], fmt, attestations, default="PARTIAL" if human else "UNREVIEWED")
                     for fmt, ref in files.items()}
    notices: list[Any] = []
    if human:
        newer = _superseded_by(project, load_record("ProjectRecord", project / "project.json"), kind, manifest)
        if newer:
            notices.append({"code": "BASE_SUPERSEDED", "base_revision_id": manifest["base_revision_id"] if kind == "review" else manifest["parent_revision_id"],
                            "newer_revision_ids": newer, "message": "有新的机器版本，当前人工稿基于旧版本；人工稿保持原样，仍可导出"})
        if manifest["provenance_status"] != "exact":
            notices.append({"code": "PROVENANCE_" + manifest["provenance_status"].upper(), "message": "这份人工稿与源文的对应没有被完整审计"})
    if unreadable:
        notices.append({"code": "ATTESTATION_UNREADABLE", "files": unreadable, "message": "有确认记录文件无法读取，未计入人工确认状态"})
    return require_record("ResolvedRevision", {
        "kind": kind, "revision_id": revision_id, "project_id": project_id, "media_id": manifest["media_id"], "directory": directory_rel, "manifest": manifest,
        "subtitle_files": {fmt: _project_ref(directory_rel, ref) for fmt, ref in files.items()}, "confirmations": confirmations, "notices": notices})


def resolve_revision(data_root: Path | str, project_id: str, revision_id: str) -> dict:
    """ResolvedRevision: the concrete revision with hash-verified file refs (project-relative), human status per file and notices."""
    return _resolve(_project(data_root, project_id), project_id, revision_id)


# --------------------------------------------------------------------------- sessions (plan 9.1)

def _load_session(project: Path, session_id: str) -> dict:
    _check_id(session_id, "session")
    path = project / "reviews" / "sessions" / session_id / SESSION_RECORD_NAME
    if not path.is_file():
        raise ContractError("INPUT_MISSING", "the project has no review session with this id", details={"session_id": session_id})
    record = load_record("ReviewSession", path)
    if record["session_id"] != session_id:
        raise ContractError("ARTIFACT_CHANGED", "the session record does not describe this session", details={"rule_id": "QC04_IDENTITY", "session_id": session_id})
    return record


def create_session(data_root: Path | str, project_id: str, revision_id: str, fmt: str) -> dict:
    """New ReviewSession: ``base.json`` plus ``edit.<fmt>``, a byte copy of the chosen committed file. Never overwrites anything."""
    if fmt not in SUBTITLE_FORMATS:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "the format must be srt or ass", details={"format": fmt})
    project = _project(data_root, project_id)
    resolved = _resolve(project, project_id, revision_id)
    ref = resolved["subtitle_files"].get(fmt)
    if ref is None:
        raise ContractError("NEEDS_SELECTION", f"this revision has no {fmt} file; a session copies an existing file and never converts one",
                            details={"kind": "format_not_available", "available_formats": sorted(resolved["subtitle_files"])})
    data = safe_join(project, ref["path"]).read_bytes()
    if sha256_bytes(data) != ref["sha256"]:
        raise ContractError("ARTIFACT_CHANGED", "the committed subtitle file changed while it was being copied", details={"rule_id": "QC04_IDENTITY", "changed": [ref["path"]]})
    session_id = new_id()
    directory = project / "reviews" / "sessions" / session_id
    record = require_record("ReviewSession", {
        "schema_version": SCHEMA_VERSION, "session_id": session_id, "project_id": project_id, "base_revision_id": revision_id, "base_subtitle_sha256": ref["sha256"],
        "media_id": resolved["media_id"], "format": fmt, "edit_relative_path": f"reviews/sessions/{session_id}/edit.{fmt}", "created_at": utc_now()})
    with ProjectCommitLock(project):
        directory.mkdir(parents=True, exist_ok=False)
        atomic_write_bytes(directory / f"edit.{fmt}", data)
        atomic_write_text(directory / SESSION_RECORD_NAME, render_json(record))   # written last: a directory without it is not a session
    return record


# --------------------------------------------------------------------------- snapshot (plan 9.2 step 2)

def _stat_key(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_size, info.st_mtime_ns


def _read_source_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _busy(exc: Exception | None = None) -> ContractError:
    return ContractError("REVIEW_FILE_BUSY", "the subtitle file is still being written by another program; save it in the editor, then register it again",
                         retryable=True, details={"reason": type(exc).__name__ if exc else "changed_during_copy"})


def _snapshot(file: Path, target: Path) -> dict:
    """Copy the file's bytes once. The copy is accepted only if the source did not change around it; no retry loop."""
    try:
        if not file.is_file():
            raise ContractError("INPUT_MISSING", "the subtitle file does not exist", details={"name": file.name})
        before = _stat_key(file)
        if before[0] > subtitles.MAX_SUBTITLE_BYTES:
            raise ContractError("SUBTITLE_INVALID", "the file is too large to be a subtitle", details={"rule_id": "QC01_PARSE", "kind": "too_large", "bytes": before[0]})
        data = _read_source_bytes(file)
        atomic_write_bytes(target, data)
        after = _stat_key(file)
        copied_sha256 = sha256_file(target)
        source_sha256 = sha256_file(file)
        final = _stat_key(file)
    except PermissionError as exc:
        target.unlink(missing_ok=True)
        raise _busy(exc) from exc
    except FileNotFoundError as exc:
        target.unlink(missing_ok=True)
        raise ContractError("INPUT_MISSING", "the subtitle file disappeared while it was being read", details={"name": file.name}) from exc
    if not (before == after == final) or copied_sha256 != source_sha256 or len(data) != before[0]:
        target.unlink(missing_ok=True)
        raise _busy()
    return {"data": data, "sha256": copied_sha256, "bytes": len(data), "mtime_ns": before[1]}


# --------------------------------------------------------------------------- correspondence to the base (plan 9.2 step 4)

def _content_key(cue: dict, fmt: str) -> tuple:
    if fmt == "ass":   # every event field as written, except the two time fields (compared as parsed values)
        return cue["kind"], tuple(sorted((name, value) for name, value in cue["fields"].items() if name.strip().lower() not in ("start", "end")))
    return cue["kind"], cue["text"]


def _time_key(cue: dict) -> tuple | None:
    if isinstance(cue["start_ms"], int) and isinstance(cue["end_ms"], int):
        return cue["start_ms"], cue["end_ms"]
    return None


def _correspondences(edited: dict, base: dict | None, id_map: dict[str, str] | None) -> tuple[list[dict], dict]:
    cues = edited["cues"]
    if base is None or base["format"] != edited["format"]:
        reason = "no_base" if base is None else "format_differs"
        rows = [{"edited_cue_ordinal": cue["ordinal"], "source_cue_ids": [], "mapping_status": "unmapped", "reason": reason} for cue in cues]
        return rows, {"edited_cues": len(cues), "base_cues": len(base["cues"]) if base else 0, "exact": 0, "candidate": 0, "unmapped": len(cues), "base_cues_without_exact": len(base["cues"]) if base else 0}
    fmt = edited["format"]
    named = (lambda cue_id: id_map.get(cue_id, cue_id)) if id_map else (lambda cue_id: cue_id)
    base_full, base_time, base_content = defaultdict(list), defaultdict(list), defaultdict(list)
    for cue in base["cues"]:
        time_key, content_key = _time_key(cue), _content_key(cue, fmt)
        base_content[content_key].append(cue["cue_id"])
        if time_key is not None:
            base_full[(time_key, content_key)].append(cue["cue_id"])
            base_time[(cue["kind"], time_key)].append(cue["cue_id"])
    edited_full = Counter((_time_key(cue), _content_key(cue, fmt)) for cue in cues)
    edited_time = Counter((cue["kind"], _time_key(cue)) for cue in cues)
    edited_content = Counter(_content_key(cue, fmt) for cue in cues)
    rows, matched_base = [], set()
    for cue in cues:
        time_key, content_key = _time_key(cue), _content_key(cue, fmt)
        full = base_full.get((time_key, content_key), []) if time_key is not None else []
        if len(full) == 1 and edited_full[(time_key, content_key)] == 1:
            status, ids, reason = "exact", full, "identical_and_unique"
            matched_base.add(full[0])
        elif full:
            status, ids, reason = "candidate", full, "identical_but_not_unique"
        elif time_key is not None and len(base_time.get((cue["kind"], time_key), [])) == 1 and edited_time[(cue["kind"], time_key)] == 1:
            status, ids, reason = "candidate", base_time[(cue["kind"], time_key)], "same_time_content_changed"
        elif len(base_content.get(content_key, [])) == 1 and edited_content[content_key] == 1:
            status, ids, reason = "candidate", base_content[content_key], "same_content_time_changed"
        else:
            status, ids, reason = "unmapped", [], "invalid_time" if time_key is None else "no_identical_cue"
        rows.append({"edited_cue_ordinal": cue["ordinal"], "source_cue_ids": [named(cue_id) for cue_id in ids], "mapping_status": status, "reason": reason})
    counts = Counter(row["mapping_status"] for row in rows)
    summary = {"edited_cues": len(cues), "base_cues": len(base["cues"]), "exact": counts["exact"], "candidate": counts["candidate"], "unmapped": counts["unmapped"],
               "base_cues_without_exact": len(base["cues"]) - len(matched_base)}
    return rows, summary


def _provenance(summary: dict, has_base: bool) -> str:
    if not has_base or summary["exact"] == 0:
        return "unmapped"
    if summary["exact"] == summary["edited_cues"] and summary["base_cues_without_exact"] == 0:
        return "exact"
    return "partial"


def _style_diff(edited: dict, base: dict | None) -> dict | None:
    """What differs between the two ASS headers. Listed for a person; the program cannot tell an intended restyle from a lost style."""
    if base is None or edited["format"] != "ass" or base["format"] != "ass":
        return None
    old = {style["name"]: style["raw"] for style in base["styles"]}
    new = {style["name"]: style["raw"] for style in edited["styles"]}
    old_info, new_info = base["script_info"], edited["script_info"]
    diff = {"styles_added": sorted(set(new) - set(old)), "styles_removed": sorted(set(old) - set(new)),
            "styles_changed": sorted(name for name in set(old) & set(new) if old[name] != new[name]),
            "script_info_changed": sorted(key for key in set(old_info) | set(new_info) if old_info.get(key) != new_info.get(key))}
    return diff if any(diff.values()) else None


def _base_view(project: Path, base: dict, fmt: str) -> dict:
    if _is_human(base["kind"], base["manifest"]):
        return load_record("SubtitleView", project / base["directory"] / VIEW_NAME)
    return subtitles.parse_view(safe_join(project, base["subtitle_files"][fmt]["path"]).read_bytes(), fmt)


def _cue_sources(project: Path, base: dict | None, view: dict, rows: list[dict], id_scheme: str) -> dict[str, dict]:
    """What the review table may show next to each edited cue: only what the correspondence established, nothing guessed.
    The role is taken over only for an exact correspondence; a candidate shows the base text as a hint (provenance partial)."""
    by_ordinal = {cue["ordinal"]: cue for cue in view["cues"]}
    segments: dict[str, dict] = {}
    if base is not None and id_scheme == "revision_cue_id" and base["kind"] in ("machine", "source"):
        source_revision_id = base["manifest"]["source_revision_id"] if base["kind"] == "machine" else base["manifest"]["revision_id"]
        try:
            segments = {segment["cue_id"]: segment for segment in source_module.load_source_revision(project, source_revision_id)["segment_set"]["segments"]}
        except ContractError:
            segments = {}
    sources: dict[str, dict] = {}
    for row in rows:
        cue = by_ordinal.get(row["edited_cue_ordinal"])
        if cue is None:
            continue
        status = {"exact": "exact", "candidate": "partial"}.get(row["mapping_status"], "unmapped")
        linked = [segments[cue_id] for cue_id in row["source_cue_ids"] if cue_id in segments]
        entry: dict[str, Any] = {"provenance_status": status}
        if linked:
            entry.update({"source_text": chr(10).join(segment["source_text"] for segment in linked), "source_ref": " ".join(f"cue:{segment['cue_id']}" for segment in linked)})
            if status == "exact" and len({segment["role"] for segment in linked}) == 1:
                entry["role"] = linked[0]["role"]
        elif row["source_cue_ids"]:
            entry["source_ref"] = " ".join(f"base_cue:{cue_id}" for cue_id in row["source_cue_ids"])
        sources[cue["cue_id"]] = entry
    return sources


def _base_cue_ids(project: Path, base: dict, view: dict) -> tuple[dict[str, str] | None, str]:
    """Map the base file's view cue ids to the base revision's own cue ids where that is certain (same order, same count, same times)."""
    kind, manifest = base["kind"], base["manifest"]
    if kind == "review":
        return None, "base_view_cue_id"   # an imported subtitle's own cue ids are its view cue ids
    source_revision_id = manifest["source_revision_id"] if kind == "machine" else manifest["revision_id"]
    try:
        segment_set = source_module.load_source_revision(project, source_revision_id)["segment_set"]
    except ContractError:
        return None, "base_view_cue_id"
    if kind == "source" and manifest["origin"] == "human":
        return {row["view_cue_id"]: row["cue_id"] for row in segment_set["extensions"].get("cue_origin", [])}, "revision_cue_id"
    dialogues = [cue for cue in view["cues"] if cue["kind"] == "dialogue"]
    segments = segment_set["segments"]
    tolerance = quality_module.BOUNDS_TOLERANCE_MS[view["format"]]
    if len(dialogues) != len(segments) or any(_time_key(cue) is None or abs(cue["start_ms"] - segment["start_ms"]) > tolerance or abs(cue["end_ms"] - segment["end_ms"]) > tolerance
                                              for cue, segment in zip(dialogues, segments)):
        return None, "base_view_cue_id"
    return {cue["cue_id"]: segment["cue_id"] for cue, segment in zip(dialogues, segments)}, "revision_cue_id"


# --------------------------------------------------------------------------- human source -> segments (plan 9.4)

def _segments_from_view(view: dict) -> dict:
    """Segments of a human source subtitle: the user's text, times and splitting. No recognition coordinate is attached to them."""
    usable, omitted = [], []
    for cue in view["cues"]:
        reason = "comment" if cue["kind"] != "dialogue" else "drawing" if cue["drawing"] else "no_visible_text" if not cue["text"].strip() else None
        if reason:
            omitted.append({"view_cue_id": cue["cue_id"], "line": cue["line"], "reason": reason})
        else:
            usable.append(cue)
    ordered = sorted(usable, key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["ordinal"]))
    segments, origin = [], []
    for index, cue in enumerate(ordered, start=1):
        cue_id = new_id()
        segments.append({"cue_id": cue_id, "legacy_cue_id": None, "index": index, "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "source_text": cue["text"], "role": "unknown",
                         "source_ranges": [], "visual_ids": [], "timing_source": "human", "flags": ["human_authored"]})
        origin.append({"cue_id": cue_id, "view_cue_id": cue["cue_id"], "ordinal": cue["ordinal"], "line": cue["line"]})
    return {"segments": segments, "cue_origin": origin, "omitted": omitted, "reordered": [cue["ordinal"] for cue in ordered] != [cue["ordinal"] for cue in usable]}


# --------------------------------------------------------------------------- import (plan 9.2 / 9.4)

def _discard(pending: Path) -> None:
    shutil.rmtree(pending, ignore_errors=True)


def _keep_candidate(pending: Path, project: Path, note: dict) -> str:
    atomic_write_text(pending / CANDIDATE_NAME, render_json({"schema_version": SCHEMA_VERSION, "status": "NOT_COMMITTED", "created_at": utc_now(), **note}))
    return pending.relative_to(project).as_posix()


def import_revision(data_root: Path | str, project_id: str, media_id: str, file: Path | str, kind: str, *, base_revision_id: str | None = None,
                    session_id: str | None = None, encoding: str | None = None, rule_config: dict | None = None) -> dict:
    """Register a saved subtitle file. Returns the committed ReviewRevision (``kind='review'``) or SourceRevision (``kind='source'``).

    A file with structural errors is not committed: its bytes and the report stay under ``.pending`` and
    ``SUBTITLE_INVALID`` names that candidate. Warnings do not block. The file itself is never modified.
    Every other failure leaves nothing behind under ``.pending``.
    """
    created: list[Path] = []
    try:
        return _import(data_root, project_id, media_id, file, kind, base_revision_id, session_id, encoding, rule_config, created)
    except ContractError as exc:
        if created and "pending" not in exc.details:
            _discard(created[0])
        raise
    except BaseException:
        if created:
            _discard(created[0])
        raise


def _import(data_root: Path | str, project_id: str, media_id: str, file: Path | str, kind: str, base_revision_id: str | None, session_id: str | None,
            encoding: str | None, rule_config: dict | None, created: list[Path]) -> dict:
    if kind not in IMPORT_KINDS:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "kind must be source or review", details={"kind": kind})
    project = _project(data_root, project_id)
    _check_id(media_id, "media")
    file = Path(file)
    fmt = subtitles.subtitle_format(file)
    session = None
    if session_id is not None:
        session = _load_session(project, session_id)
        if base_revision_id is not None and base_revision_id != session["base_revision_id"]:
            raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "the session was created from another revision than the given base; give only one of them or matching ones",
                                details={"session_base_revision_id": session["base_revision_id"], "base_revision_id": base_revision_id})
        base_revision_id = session["base_revision_id"]
        if session["media_id"] != media_id:
            raise ContractError("MEDIA_CHANGED", "the session belongs to another media version; different videos are never treated as one timeline",
                                details={"rule_id": "QC21_MEDIA_BINDING", "session_media_id": session["media_id"], "media_id": media_id})
    # step 1: the media exists and its bytes are the registered ones
    if not (media_module.media_dir(project, media_id) / MANIFEST_NAME).is_file():
        raise ContractError("INPUT_MISSING", "the project has no registered media with this id", details={"rule_id": "QC21_MEDIA_BINDING", "media_id": media_id})
    media_record = media_module.load_media_record(project, media_id)
    media_module.verify_source(media_record, project)
    base = None
    if base_revision_id is not None:
        base = _resolve(project, project_id, base_revision_id)
        if base["kind"] not in BASE_KINDS[kind]:
            raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"a {kind} import cannot be based on a {base['kind']} revision",
                                details={"kind": kind, "base_kind": base["kind"], "allowed_base_kinds": list(BASE_KINDS[kind])})
        if base["media_id"] != media_id:
            raise ContractError("MEDIA_CHANGED", "the base revision belongs to another media version; register the subtitle without a base for this media, "
                                                 "or choose the media the base was made for", details={"rule_id": "QC21_MEDIA_BINDING", "base_media_id": base["media_id"], "media_id": media_id})
    # step 2: byte snapshot into .pending
    pending = begin_pending(project)
    created.append(pending)
    original_name = f"original.{fmt}"
    try:
        snapshot = _snapshot(file, pending / original_name)
    except ContractError:
        _discard(pending)
        raise
    # step 3: read-only view of the snapshot
    try:
        view = subtitles.parse_view(snapshot["data"], fmt, encoding)
    except ContractError as exc:
        if exc.code != "SUBTITLE_INVALID":
            _discard(pending)
            raise
        exc.details["pending"] = _keep_candidate(pending, project, {"kind": kind, "format": fmt, "sha256": snapshot["sha256"], "reason": exc.details.get("kind", "unreadable")})
        raise
    revision_id = new_id()
    atomic_write_text(pending / VIEW_NAME, render_json(view))
    # step 4: light correspondence to the base
    base_fmt = base_view = id_map = None
    id_scheme = "none"
    if base is not None:
        base_fmt = session["format"] if session else fmt if fmt in base["subtitle_files"] else sorted(base["subtitle_files"])[0]
        if session and base["subtitle_files"].get(base_fmt, {}).get("sha256") != session["base_subtitle_sha256"]:
            _discard(pending)
            raise ContractError("ARTIFACT_CHANGED", "the session's base file is not the committed file of that revision", details={"rule_id": "QC04_IDENTITY", "session_id": session_id})
        base_view = _base_view(project, base, base_fmt)
        id_map, id_scheme = _base_cue_ids(project, base, base_view)
    rows, summary = _correspondences(view, base_view, id_map)
    provenance = _provenance(summary, base is not None)
    style_diff = _style_diff(view, base_view)
    newer: list[str] = []
    if base is not None:
        index = load_record("ProjectRecord", project / "project.json")
        newer = (_superseded_by(project, index, base["kind"], base["manifest"]) if _is_human(base["kind"], base["manifest"])
                 else _newer(project, index, base["kind"], base["revision_id"], media_id))
    correspondence_set = require_record("CorrespondenceSet", {
        "schema_version": SCHEMA_VERSION, "revision_id": revision_id, "base_revision_id": base_revision_id, "base_format": base_fmt,
        "base_subtitle_sha256": base["subtitle_files"][base_fmt]["sha256"] if base is not None else None, "edited_format": fmt, "correspondences": rows, "summary": summary,
        "extensions": {"source_cue_id_scheme": id_scheme, "style_diff": style_diff, "candidate_note": "candidate rows are hints for a person; nothing is paired, retranslated or overwritten from them"}})
    atomic_write_text(pending / CORRESPONDENCES_NAME, render_json(correspondence_set))
    # step 5: quality
    extensions: dict[str, Any] = {"origin": "human", "review_base": {"base_revision_id": base_revision_id, "provenance_status": provenance, "superseded_by": newer, "style_diff": style_diff,
                                                                    "counts": {key: summary[key] for key in ("exact", "candidate", "unmapped", "edited_cues", "base_cues")}}}
    if kind == "source":
        extensions["bounds_tolerance_ms"] = SOURCE_BOUNDS_TOLERANCE_MS
    extensions["cue_sources"] = _cue_sources(project, base, view, rows, id_scheme)
    result = quality_module.evaluate({
        "revision_id": revision_id, "revision_kind": kind, "media_record": media_record, "subtitle_ref": artifact_ref(pending, pending / original_name), "parsed_view": view,
        "source_revision": None, "recognition_refs": [], "translation_ref": None, "rule_config": copy.deepcopy(rule_config if rule_config is not None else DEFAULT_QUALITY),
        "attestations": [], "extensions": extensions})
    report = result["report"]
    atomic_write_text(pending / QUALITY_NAME, render_json(report))
    atomic_write_text(pending / SCOPE_NAME, render_json(result["scope"]))
    links = [{"label": f"人工{'日文源' if kind == 'source' else '中文'}字幕原文件（{fmt.upper()}，按原字节保存）", "href": original_name},
             {"label": "质量记录（quality.json）", "href": QUALITY_NAME}, {"label": "复核表（review.csv）", "href": REVIEW_CSV_NAME}]
    if base is not None and base_fmt:
        links.append({"label": f"基础版本字幕（{base['kind']} {base_revision_id}，{base_fmt.upper()}）", "href": "../../../" + base["subtitle_files"][base_fmt]["path"]})
    title = f"{'人工日文源字幕' if kind == 'source' else '人工中文字幕'}复核报告"
    for name, data in quality_module.review_files(result, title=title, links=links).items():
        atomic_write_bytes(pending / name, data)
    if report["structural_status"] != "valid":
        errors = [issue for issue in report["issues"] if issue["severity"] == "ERROR"]
        candidate = _keep_candidate(pending, project, {"kind": kind, "format": fmt, "sha256": snapshot["sha256"], "reason": "structural_errors", "candidate_revision_id": revision_id})
        raise ContractError("SUBTITLE_INVALID", f"the subtitle has {len(errors)} structural error(s) and was not registered; the file was kept as it is, with the report, as a candidate",
                            details={"rule_ids": sorted({issue["rule_id"] for issue in errors}), "error_count": len(errors), "pending": candidate,
                                     "issues": [{"rule_id": issue["rule_id"], "message": issue["message"], "source_refs": issue["source_refs"]} for issue in errors[:MAX_REPORTED_ISSUES]]})
    # step 6: commit
    imported = {"name": file.name, "sha256": snapshot["sha256"], "bytes": snapshot["bytes"], "mtime_ns": snapshot["mtime_ns"]}
    common = {"encoding_source": view["encoding_source"], "imported_file": imported, "base_format": base_fmt, "superseded_by_at_import": newer, "source_cue_id_scheme": id_scheme}
    if kind == "review":
        def build_manifest(refs: list[dict]) -> dict:
            by_path = {ref["path"]: ref for ref in refs}
            return {"schema_version": SCHEMA_VERSION, "revision_id": revision_id, "project_id": project_id, "media_id": media_id, "base_revision_id": base_revision_id,
                    "session_id": session_id, "original_file_ref": by_path[original_name], "validated_view_ref": by_path[VIEW_NAME], "format": fmt, "encoding": view["encoding"],
                    "provenance_status": provenance, "correspondences_ref": by_path[CORRESPONDENCES_NAME], "quality_ref": by_path[QUALITY_NAME], "created_at": utc_now(),
                    "extensions": {**common, "report_refs": [by_path[name] for name in (SCOPE_NAME, REVIEW_CSV_NAME, REVIEW_HTML_NAME)]}}
        final_dir, index_key = project / "revisions" / "review" / revision_id, "review_revision_ids"
    else:
        built = _segments_from_view(view)
        base_set = source_module.load_source_revision(project, base_revision_id)["segment_set"] if base is not None else None
        segment_set = require_record("SegmentSet", {
            "schema_version": SCHEMA_VERSION, "source_revision_id": revision_id, "media_id": media_id, "origin": "human", "recognition_mode": "supplied_source",
            "timing_mode": base_set["timing_mode"] if base_set else "mixed", "status": "OK" if built["segments"] else "EMPTY", "segments": built["segments"], "parents": [],
            "visual_events": [], "unresolved_visual_events": [],
            "coverage": {"status": "human_authored", "audited": False, "note": "text, times and splitting are the user's; no recognition coordinate is attached"},
            "extensions": {"timing_authority": "human", "cue_origin": built["cue_origin"], "omitted_events": built["omitted"], "ordered_by_start_time": built["reordered"],
                           "source_format": fmt, "session_id": session_id}})
        atomic_write_text(pending / source_module.SEGMENTS_NAME, render_json(segment_set))

        def build_manifest(refs: list[dict]) -> dict:
            by_path = {ref["path"]: ref for ref in refs}
            named = {original_name, source_module.SEGMENTS_NAME}
            return {"schema_version": SCHEMA_VERSION, "revision_id": revision_id, "project_id": project_id, "media_id": media_id, "parent_revision_id": base_revision_id,
                    "origin": "human", "source_subtitle_ref": by_path[original_name], "segments_ref": by_path[source_module.SEGMENTS_NAME],
                    "recognition_refs": [ref for path, ref in sorted(by_path.items()) if path not in named], "provenance_status": provenance, "created_at": utc_now(),
                    "extensions": {**common, "format": fmt, "encoding": view["encoding"], "session_id": session_id}}
        final_dir, index_key = project / "revisions" / "source" / revision_id, "source_revision_ids"
    if sha256_file(pending / original_name) != snapshot["sha256"]:
        raise ContractError("ARTIFACT_CHANGED", "the snapshot under .pending changed before it was committed", details={"rule_id": "QC04_IDENTITY"})
    with ProjectCommitLock(project):
        committed = commit_pending(pending, final_dir, manifest_kind=MANIFEST_KINDS[kind], build_manifest=build_manifest)
    update_project(data_root, project_id, lambda record: record[index_key].append(revision_id) if revision_id not in record[index_key] else None)
    return committed["manifest"]


# --------------------------------------------------------------------------- attestation (plan 3.3 / 6.6)

def attest_revision(data_root: Path | str, project_id: str, revision_id: str, scopes: list[str] | tuple[str, ...], note: str = "", *, fmt: str | None = None) -> dict:
    """Attestation for exactly the current bytes of one subtitle file and its media. The revision's files are not touched."""
    wanted = list(scopes) if not isinstance(scopes, str) else [scopes]
    if not wanted or len(set(wanted)) != len(wanted) or any(scope not in SCOPES for scope in wanted):
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "scopes must be an explicit non-empty list out of text, timing, style (there is no automatic 'all')",
                            details={"scopes": wanted, "allowed": list(SCOPES)})
    if not isinstance(note, str):
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "the note must be text")
    project = _project(data_root, project_id)
    resolved = _resolve(project, project_id, revision_id)
    files = resolved["subtitle_files"]
    if fmt is None:
        if len(files) != 1:
            raise ContractError("NEEDS_SELECTION", "this revision has more than one subtitle file; say which one is being confirmed",
                                details={"kind": "format_required", "available_formats": sorted(files)})
        fmt = next(iter(files))
    elif fmt not in files:
        raise ContractError("NEEDS_SELECTION", f"this revision has no {fmt} file", details={"kind": "format_not_available", "available_formats": sorted(files)})
    if fmt == "srt" and "style" in wanted:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "an SRT file has no style to confirm (not applicable); confirm text and/or timing",
                            details={"scopes": wanted, "format": fmt})
    record = require_record("Attestation", {
        "schema_version": SCHEMA_VERSION, "attestation_id": new_id(), "revision_id": revision_id, "subtitle_sha256": files[fmt]["sha256"], "media_id": resolved["media_id"],
        "scopes": [scope for scope in SCOPES if scope in wanted], "actor": "user", "created_at": utc_now(), "note": note})
    path = project / "reviews" / "attestations" / f"{record['attestation_id']}.json"
    with ProjectCommitLock(project):
        if path.exists():
            raise ContractError("OUTPUT_EXISTS", "an attestation with this id already exists", details={"attestation_id": record["attestation_id"]})
        atomic_write_text(path, render_json(record))
    return record


__all__ = ["REVISION_KINDS", "IMPORT_KINDS", "create_session", "import_revision", "attest_revision", "resolve_revision"]
