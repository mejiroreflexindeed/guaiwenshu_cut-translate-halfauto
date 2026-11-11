#!/usr/bin/env python3
"""Data contracts for the v2 workflow (plan 1.1, sections 6 and 10.4).

Interface (fixed by the plan):
    load_record(kind, path)      -> dict      read + validate a stored record
    validate_record(kind, value) -> list[str] problems, empty when valid

Everything here is pure: no inference, no repair, no file writes except
``dump_record`` which only serializes an already valid record.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

SCHEMA_VERSION = 2
RULE_SET_VERSION = 1

# --------------------------------------------------------------------------- errors

ERROR_CODES = frozenset({
    # input / resources
    "INPUT_MISSING", "RESOURCE_MISSING", "RESOURCE_CHANGED", "CAPABILITY_MISSING",
    # config / content
    "CONFIG_UNSUPPORTED_COMMAND", "DICTIONARY_UNSUPPORTED", "CONTEXT_EXCEEDED",
    # media
    "MEDIA_CLOCK_UNKNOWN", "MEDIA_CLOCK_DISCONTINUITY", "MEDIA_PROFILE_UNSUPPORTED", "MEDIA_CHANGED",
    # process
    "PROCESS_FAILED", "PROCESS_CANCELLED", "SERVER_PORT_CONFLICT", "EXECUTOR_BUSY",
    # data
    "SCHEMA_UNSUPPORTED", "SCHEMA_INVALID", "ARTIFACT_CHANGED", "SOURCE_COVERAGE_ERROR", "TRANSLATION_IDENTITY_ERROR",
    # review / export
    "REVIEW_FILE_BUSY", "SUBTITLE_INVALID", "TIME_PRECISION_COLLAPSE", "FONT_MISSING", "OUTPUT_EXISTS",
    "EXPORT_VALIDATION_FAILED",
    # cli / selection
    "NEEDS_SELECTION", "DATA_ROOT_UNWRITABLE", "PATH_INVALID",
    # stage engine (W02): a stage implementation raised, or this build has no implementation for a required stage
    "STAGE_FAILED", "STAGE_UNAVAILABLE",
})

EXIT_OK, EXIT_ARGS, EXIT_RESOURCE, EXIT_PROCESS, EXIT_DATA, EXIT_CANCELLED, EXIT_BUSY = 0, 2, 3, 4, 5, 6, 7
EXIT_CODE_BY_ERROR = {
    "INPUT_MISSING": EXIT_RESOURCE, "RESOURCE_MISSING": EXIT_RESOURCE, "RESOURCE_CHANGED": EXIT_RESOURCE,
    "CAPABILITY_MISSING": EXIT_RESOURCE, "MEDIA_CLOCK_UNKNOWN": EXIT_RESOURCE, "MEDIA_CLOCK_DISCONTINUITY": EXIT_RESOURCE,
    "MEDIA_PROFILE_UNSUPPORTED": EXIT_RESOURCE, "MEDIA_CHANGED": EXIT_RESOURCE, "FONT_MISSING": EXIT_RESOURCE,
    "CONFIG_UNSUPPORTED_COMMAND": EXIT_ARGS, "DICTIONARY_UNSUPPORTED": EXIT_ARGS, "NEEDS_SELECTION": EXIT_ARGS,
    "PATH_INVALID": EXIT_ARGS, "DATA_ROOT_UNWRITABLE": EXIT_ARGS,
    "PROCESS_FAILED": EXIT_PROCESS, "SERVER_PORT_CONFLICT": EXIT_PROCESS, "CONTEXT_EXCEEDED": EXIT_PROCESS,
    "STAGE_FAILED": EXIT_PROCESS, "STAGE_UNAVAILABLE": EXIT_RESOURCE,
    "PROCESS_CANCELLED": EXIT_CANCELLED,
    "EXECUTOR_BUSY": EXIT_BUSY, "REVIEW_FILE_BUSY": EXIT_BUSY,
}


class ContractError(ValueError):
    """Carries the unified error object: code, stage, message, retryable, artifact_refs, details."""

    def __init__(self, code: str, message: str, *, stage: str | None = None, retryable: bool = False,
                 artifact_refs: list[dict] | None = None, details: dict | None = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.retryable = retryable
        self.artifact_refs = list(artifact_refs or [])
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "stage": self.stage, "message": self.message, "retryable": self.retryable,
                "artifact_refs": self.artifact_refs, "details": self.details}

    @property
    def exit_code(self) -> int:
        return EXIT_CODE_BY_ERROR.get(self.code, EXIT_DATA)


# --------------------------------------------------------------------------- primitives

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
RULE_ID_RE = re.compile(r"^QC\d{2}_[A-Z_]+$")
WINDOWS_RESERVED_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
WINDOWS_INVALID_CHARS = set('<>:"|?*') | {chr(code) for code in range(32)}


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    """Canonical form used for signatures: sorted keys, compact, no NaN/Infinity, UTF-8 text."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_canonical(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_safe_relative_posix(value: Any) -> bool:
    """Managed artifact paths: relative, POSIX separators, no '..', no drive, no NUL, no backslash."""
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    segments = value.split("/")
    return all(segment not in ("", ".", "..") for segment in segments)


def windows_name_problem(name: str) -> str | None:
    """Return why a single path component is not a valid Windows file name, or None."""
    if not name or name in (".", ".."):
        return "empty or dot name"
    if any(ch in WINDOWS_INVALID_CHARS for ch in name):
        return "contains a character Windows does not allow (<>:\"|?* or control)"
    if name.endswith((" ", ".")):
        return "ends with a space or dot"
    if WINDOWS_RESERVED_RE.match(name):
        return "is a reserved device name"
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- field specs

@dataclass(frozen=True)
class F:
    type: str                       # str | int | number | bool | object | list | record | any | capability
    nullable: bool = False
    enum: tuple = ()
    record: str | None = None       # nested record kind (type=record, or list items when item is None)
    item: "F | None" = None         # list item spec
    check: Callable[[Any], str | None] | None = None
    min: float | None = None
    nonempty: bool = False
    doc: str = ""


def _uuid(doc: str = "", nullable: bool = False) -> F:
    return F("str", nullable=nullable, check=lambda v: None if UUID4_RE.match(v) else "must be a lowercase UUID4", doc=doc)


def _sha(doc: str = "", nullable: bool = False) -> F:
    return F("str", nullable=nullable, check=lambda v: None if SHA256_RE.match(v) else "must be 64 lowercase hex chars", doc=doc)


def _utc(doc: str = "", nullable: bool = False) -> F:
    return F("str", nullable=nullable, check=lambda v: None if UTC_Z_RE.match(v) else "must be UTC ISO8601 with Z", doc=doc)


def _ms(doc: str = "", nullable: bool = False) -> F:
    return F("int", nullable=nullable, min=0, doc=doc or "integer milliseconds")


def _rel(doc: str = "") -> F:
    return F("str", check=lambda v: None if is_safe_relative_posix(v) else "must be a safe relative POSIX path", doc=doc)


def _str(doc: str = "", nullable: bool = False, nonempty: bool = False, enum: tuple = ()) -> F:
    return F("str", nullable=nullable, nonempty=nonempty, enum=enum, doc=doc)


def _list(item: F | None = None, record: str | None = None, nonempty: bool = False, doc: str = "") -> F:
    return F("list", item=item, record=record, nonempty=nonempty, doc=doc)


def _rec(kind: str, nullable: bool = False, doc: str = "") -> F:
    return F("record", record=kind, nullable=nullable, doc=doc)


RECOGNITION_MODES = ("combined", "speech_only", "visual_only", "supplied_source")
TIMING_MODES = ("mixed", "screen", "speech")
STAGES = ("INPUT", "MEDIA_PREP", "OCR", "ASR", "SOURCE", "TRANSLATION", "SUBTITLE_BUILD", "QUALITY_AND_COMMIT")
STAGE_STATUSES = ("PENDING", "RUNNING", "DONE", "FAILED", "INTERRUPTED", "CANCELLED", "SKIPPED")
REQUEST_STATUSES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED")
RESOURCE_KINDS = ("ffmpeg", "ffprobe", "asr_engine", "ocr_engine", "translation_engine", "model", "font", "editor")
CAPABILITY_KEYS = ("cpu", "gpu_vulkan", "gpu_cuda", "word_timestamps", "vad", "ocr_scores", "frame_pts", "libass", "json_probe")
LICENSE_STATUSES = ("recorded", "unresolved", "not_for_redistribution")
SUBTITLE_FORMATS = ("srt", "ass")
SCOPES = ("text", "timing", "style")
PROVENANCE = ("exact", "partial", "unmapped")
SEVERITIES = ("ERROR", "WARNING", "INFO")
HUMAN_STATUSES = ("UNREVIEWED", "PARTIAL", "REVIEWED")
EXPORT_KINDS = ("subtitles", "mp4_burn", "mkv_soft")

ERROR_OBJECT = {
    "code": _str(nonempty=True), "stage": _str(nullable=True), "message": _str(), "retryable": F("bool"),
    "artifact_refs": _list(record="ArtifactRef"), "details": F("object"),
}

RECORDS: dict[str, dict[str, F]] = {
    # ---- 6.1 generic refs
    "ArtifactRef": {"path": _rel("relative to its manifest"), "sha256": _sha(), "bytes": F("int", min=0), "media_type": _str(nonempty=True)},
    "ExternalFileRef": {"absolute_path": _str(nonempty=True), "sha256": _sha(), "bytes": F("int", min=0), "mtime_ns": F("int", min=0)},
    "ResourceRef": {"resource_id": _str(nonempty=True), "content_sha256": _sha(), "manifest_sha256": _sha()},
    "ErrorObject": ERROR_OBJECT,
    # ---- 6.2 config and resources
    "AppDefaults": {
        "recognition_mode": _str(enum=RECOGNITION_MODES), "timing_mode": _str(enum=TIMING_MODES),
        "continue_on_error": F("bool"), "output_formats": _list(item=_str(enum=SUBTITLE_FORMATS), nonempty=True),
        "render_after_run": F("bool"), "copy_original": F("bool"), "gpu_profile": _str(nonempty=True),
    },
    "QualityConfig": {
        "rule_set_version": F("int", min=1), "min_duration_ms": F("int", nullable=True, min=0), "max_duration_ms": F("int", nullable=True, min=0),
        "max_cps": F("number", nullable=True, min=0), "ocr_probability_min": F("number", nullable=True, min=0),
        "asr_probability_min": F("number", nullable=True, min=0), "ocr_continuity_gap_ms": F("int", nullable=True, min=0),
        "activity_gap_ms": F("int", nullable=True, min=0),
    },
    "AppConfig": {
        "schema_version": F("int"), "data_root": _str(nonempty=True), "ascii_temp_root": _str(nonempty=True), "resources_file": _str(nonempty=True),
        "editor_srt": _str(nullable=True), "editor_ass": _str(nullable=True), "defaults": _rec("AppDefaults"), "quality": _rec("QualityConfig"),
        "extensions": F("object"),
    },
    "ResourceFile": {"path": _rel(), "sha256": _sha(), "bytes": F("int", min=0)},
    "Capabilities": {key: F("capability") for key in CAPABILITY_KEYS},
    "ResourceManifest": {
        "schema_version": F("int"), "resource_id": _str(nonempty=True), "kind": _str(enum=RESOURCE_KINDS), "absolute_path": _str(nonempty=True),
        "version_text": _str(nullable=True), "upstream_url": _str(nullable=True), "upstream_revision": _str(nullable=True),
        "content_sha256": _sha(), "files": _list(record="ResourceFile", nonempty=True), "capabilities": _rec("Capabilities"),
        "license_status": _str(enum=LICENSE_STATUSES), "inspected_at": _utc(), "extensions": F("object"),
    },
    # ---- 6.3 request, task, stage
    "JobInput": {"kind": _str(enum=("local_video", "url")), "value": _str(nonempty=True), "source_media_id": _uuid(nullable=True)},
    "ConfigSnapshot": {
        "app_config": _rec("AppConfig"), "engine_settings": F("object"), "dictionary_revision_id": _str(nonempty=True),
        "resource_refs": _list(record="ResourceRef"), "accepted_dictionary_warnings": _list(item=_str()), "config_sha256": _sha(),
    },
    "JobRequest": {
        "schema_version": F("int"), "request_id": _uuid(), "project_id": _uuid(), "created_at": _utc(), "input": _rec("JobInput"),
        "recognition_mode": _str(enum=RECOGNITION_MODES), "timing_mode": _str(enum=TIMING_MODES),
        "video_stream_index": F("int", nullable=True, min=0), "audio_stream_index": F("int", nullable=True, min=0),
        "source_revision_id": _uuid(nullable=True), "config_snapshot": _rec("ConfigSnapshot"), "resource_refs": _list(record="ResourceRef"),
        "dictionary_revision_id": _str(nonempty=True), "request_fingerprint": _sha(), "extensions": F("object"),
    },
    "Attempt": {
        "attempt_id": _uuid(), "status": _str(enum=STAGE_STATUSES), "started_at": _utc(), "ended_at": _utc(nullable=True),
        "input_signature": _sha(), "output_manifest": _rec("ArtifactRef", nullable=True), "error": _rec("ErrorObject", nullable=True),
    },
    "StageRecord": {
        "name": _str(enum=STAGES), "status": _str(enum=STAGE_STATUSES), "attempts": _list(record="Attempt"),
        "input_signature": _sha(nullable=True), "output_manifest": _rec("ArtifactRef", nullable=True), "skip_reason": _str(nullable=True),
        "started_at": _utc(nullable=True), "ended_at": _utc(nullable=True), "error": _rec("ErrorObject", nullable=True),
    },
    "TaskRecord": {
        "schema_version": F("int"), "run_id": _uuid(), "project_id": _uuid(), "request_id": _uuid(), "status": _str(enum=REQUEST_STATUSES),
        "active_stage": _str(nullable=True, enum=STAGES), "stages": _list(record="StageRecord", nonempty=True), "created_at": _utc(), "updated_at": _utc(),
        "last_error": _rec("ErrorObject", nullable=True), "machine_revision_id": _uuid(nullable=True), "extensions": F("object"),
    },
    "ProcessSpec": {
        "executable": _str(nonempty=True), "argv": _list(item=_str()), "cwd": _str(nonempty=True), "env_overrides": F("object"),
        "stdin_mode": _str(enum=("none", "pipe")), "log_dir": _str(nonempty=True), "owner_token": _str(nonempty=True), "capability_id": _str(nonempty=True),
    },
    "ProcessResult": {
        "exit_code": F("int"), "cancelled": F("bool"), "started_at": _utc(), "ended_at": _utc(), "stdout_ref": _rec("ArtifactRef"),
        "stderr_ref": _rec("ArtifactRef"), "observed_engine_id": _str(nullable=True),
    },
    # ---- 6.4 media and evidence
    "Clock": {
        "origin_pts": F("int"), "origin_time_base_num": F("int", min=1), "origin_time_base_den": F("int", min=1),
        "origin_kind": _str(enum=("first_presented_video_frame",)), "normalized_media_id": _uuid(nullable=True),
        "mapping_kind": _str(enum=("identity", "constant_offset", "verified_frame_map")), "mapping_ref": _rec("ArtifactRef", nullable=True), "verified": F("bool"),
    },
    "Derivative": {"media_revision": _str(nonempty=True), "artifact_ref": _rec("ArtifactRef"), "derivation_ref": _rec("ArtifactRef"), "clock_verified": F("bool")},
    "MediaRecord": {
        "schema_version": F("int"), "media_id": _uuid(), "source_ref": _rec("ExternalFileRef"), "source_sha256": _sha(), "duration_ms": _ms(),
        "selected_video_stream": F("int", min=0), "selected_audio_stream": F("int", nullable=True, min=0), "streams": _list(item=F("object")),
        "clock": _rec("Clock"), "derivatives": _list(record="Derivative"), "probe_raw_ref": _rec("ArtifactRef"), "extensions": F("object"),
    },
    "Interval": {"start_ms": _ms(), "end_ms": _ms()},
    "Parent": {"id": _str(nonempty=True), "start_ms": _ms(), "end_ms": _ms(), "text": _str(), "score": F("number", nullable=True), "score_kind": _str(nullable=True), "flags": _list(item=_str())},
    "Word": {"parent_id": _str(nonempty=True), "char_start": F("int", min=0), "char_end": F("int", min=0), "start_ms": _ms(), "end_ms": _ms(), "raw_clock_ref": _str(), "timing_valid": F("bool")},
    "SpeechEvidence": {
        "schema_version": F("int"), "evidence_id": _uuid(), "media_id": _uuid(), "audio_artifact": _rec("ArtifactRef"), "engine_ref": _rec("ResourceRef"),
        "model_ref": _rec("ResourceRef"), "language": _str(nonempty=True), "parents": _list(record="Parent"), "word_times": _list(record="Word"),
        "processing_intervals": _list(record="Interval"), "activity_intervals": F("list", nullable=True, record="Interval"),
        "coverage_status": _str(enum=("process_completed_only", "activity_compared", "unknown")), "raw_refs": _list(record="ArtifactRef"), "extensions": F("object"),
    },
    "ROI": {"left": F("number"), "top": F("number"), "right": F("number"), "bottom": F("number")},
    "FrameRef": {"pts": F("int"), "time_base_num": F("int", min=1), "time_base_den": F("int", min=1), "purpose": _str(nonempty=True), "image": _rec("ArtifactRef")},
    "VisualEvent": {
        "id": _str(nonempty=True), "ordinal": F("int", min=0), "start_ms": _ms(), "end_ms": _ms(), "text": _str(), "score": F("number", nullable=True),
        "score_kind": _str(nullable=True), "boxes": F("list", nullable=True, item=F("any")), "frame_refs": _list(record="FrameRef"),
        "boundary_evidence": _str(), "flags": _list(item=_str()),
    },
    "VisualEvidence": {
        "schema_version": F("int"), "evidence_id": _uuid(), "media_id": _uuid(), "engine_ref": _rec("ResourceRef"), "model_refs": _list(record="ResourceRef"),
        "roi": _rec("ROI"), "events": _list(record="VisualEvent"), "raw_refs": _list(record="ArtifactRef"),
        "evidence_level": _str(enum=("interval_only", "frame_supported", "scored_frames")), "extensions": F("object"),
    },
    # ---- 7.3 derived media (ASR WAV / review-compatible video) and the per-media registry manifest (W03)
    "Derivation": {
        "schema_version": F("int"), "source_media_id": _uuid(), "source_stream": F("int", min=0), "origin_offset_ms": F("int", doc="stream start minus video origin; may be negative"),
        "trim_before_ms": _ms(), "pad_before_ms": _ms(), "pad_after_ms": _ms(), "output_duration_ms": _ms(), "ffmpeg_ref": _rec("ResourceRef"),
        "argv_ref": _rec("ArtifactRef"), "extensions": F("object"),
    },
    "MediaManifest": {
        "schema_version": F("int"), "kind": _str(enum=("registration", "derived", "download")), "media_id": _uuid(nullable=True),
        "media_revision": _str(nullable=True), "signature": _sha(nullable=True), "outputs": _list(record="ArtifactRef"), "committed_at": _utc(),
        "extensions": F("object"),
    },
    # ---- 6.8 module requests (in-memory; runtime handles such as cancel tokens are injected, never serialized)
    "MediaInput": {
        "input": _rec("JobInput"), "selected_video_stream": F("int", nullable=True, min=0), "selected_audio_stream": F("int", nullable=True, min=0),
        "project_id": _uuid(), "extensions": F("object"),
    },
    "MediaPlan": {
        "media_id": _uuid(), "needs_visual": F("bool"), "needs_audio": F("bool"), "compatibility_profile": _str(nonempty=True),
        "run_id": _uuid(), "attempt_id": _uuid(), "extensions": F("object"),
    },
    "RecognitionRequest": {
        "run_id": _uuid(), "media_id": _uuid(), "recognition_mode": _str(enum=RECOGNITION_MODES), "video_artifact": F("object", nullable=True),
        "audio_artifact": F("object", nullable=True), "roi": _rec("ROI"), "engine_settings": F("object"), "resource_refs": _list(record="ResourceRef"),
    },
    # ---- 6.5 source, translation, quality
    "SourceRange": {"parent_id": _str(nonempty=True), "char_start": F("int", min=0), "char_end": F("int", min=0)},
    "Segment": {
        "cue_id": _str(nonempty=True), "legacy_cue_id": _str(nullable=True), "index": F("int", min=0), "start_ms": _ms(), "end_ms": _ms(), "source_text": _str(),
        "role": _str(enum=("speech", "screen", "mixed", "unknown")), "source_ranges": _list(record="SourceRange"), "visual_ids": _list(item=_str()),
        "timing_source": _str(nonempty=True), "flags": _list(item=_str()),
    },
    "SourceRevision": {
        "schema_version": F("int"), "revision_id": _uuid(), "project_id": _uuid(), "media_id": _uuid(), "parent_revision_id": _uuid(nullable=True),
        "origin": _str(enum=("machine", "human")), "source_subtitle_ref": _rec("ArtifactRef"), "segments_ref": _rec("ArtifactRef"),
        "recognition_refs": _list(record="ArtifactRef"), "provenance_status": _str(enum=PROVENANCE), "created_at": _utc(), "extensions": F("object"),
    },
    "TranslationRecord": {
        "cue_id": _str(nonempty=True), "source_revision_id": _uuid(), "source_text_sha256": _sha(), "target_text": _str(), "request_ref": _rec("ArtifactRef"),
        "raw_response_ref": _rec("ArtifactRef"), "engine_ref": _rec("ResourceRef"), "model_ref": _rec("ResourceRef"), "dictionary_revision_id": _str(nonempty=True),
        "prompt_sha256": _sha(), "postprocess_trace_ref": _rec("ArtifactRef"), "flags": _list(item=_str()),
    },
    # ---- W04 containers: the file a SourceRevision.segments_ref points to, the file a translations_ref points to,
    #      the immutable dictionary revision (plan 8.5) and the in-memory TranslationRequest (plan 6.8)
    "SegmentSet": {
        "schema_version": F("int"), "source_revision_id": _uuid(), "media_id": _uuid(), "origin": _str(enum=("machine", "human")),
        "recognition_mode": _str(enum=RECOGNITION_MODES), "timing_mode": _str(enum=TIMING_MODES), "status": _str(enum=("OK", "EMPTY")),
        "segments": _list(record="Segment"), "parents": _list(item=F("object")), "visual_events": _list(item=F("object")),
        "unresolved_visual_events": _list(item=F("object")), "coverage": F("object"), "extensions": F("object"),
    },
    "TranslationSet": {
        "schema_version": F("int"), "source_revision_id": _uuid(), "translation_signature": _sha(), "dictionary_revision_id": _str(nonempty=True),
        "engine_ref": _rec("ResourceRef"), "model_ref": _rec("ResourceRef"), "parameters": F("object"), "status": _str(enum=("OK", "EMPTY")),
        "records": _list(record="TranslationRecord"), "extensions": F("object"),
    },
    "DictionaryFile": {
        "role": _str(enum=("pre", "gpt", "post", "glossary", "style_prompt", "guideline", "project_config")), "original_name": _str(nonempty=True),
        "origin": _str(enum=("user_file", "bundled_default")), "artifact": _rec("ArtifactRef"),
    },
    "DictionaryRevision": {
        "schema_version": F("int"), "revision_id": _uuid(), "content_sha256": _sha(), "files": _list(record="DictionaryFile"),
        "diagnostics_ref": _rec("ArtifactRef"), "created_at": _utc(), "extensions": F("object"),
    },
    "TranslationRequest": {
        "run_id": _uuid(), "source_revision_id": _uuid(), "segments_ref": _rec("ArtifactRef", doc="relative to the project directory"),
        "config_snapshot": _rec("ConfigSnapshot"), "checkpoint_dir": _str(nonempty=True),
    },
    "Issue": {
        "issue_id": _uuid(), "rule_id": F("str", check=lambda v: None if RULE_ID_RE.match(v) else "must look like QC01_PARSE"), "severity": _str(enum=SEVERITIES),
        "revision_id": _uuid(), "cue_ids": _list(item=_str()), "start_ms": _ms(nullable=True), "end_ms": _ms(nullable=True), "source_refs": _list(item=_str()),
        "observed": F("any"), "threshold": F("any", nullable=True), "message": _str(), "recommended_action": _str(),
        "verification": _str(enum=("measured", "inferred", "unknown")),
    },
    "QualityReport": {
        "schema_version": F("int"), "revision_id": _uuid(), "rule_set_version": F("int", min=1), "issues": _list(record="Issue"), "unknown_capabilities": _list(item=_str()),
        "structural_status": _str(enum=("valid", "invalid")), "human_status": _str(enum=HUMAN_STATUSES), "confirmed_scopes": _list(item=_str(enum=SCOPES)), "generated_at": _utc(),
    },
    # ---- 6.6 review and export
    "ReviewSession": {
        "schema_version": F("int"), "session_id": _uuid(), "project_id": _uuid(), "base_revision_id": _uuid(), "base_subtitle_sha256": _sha(), "media_id": _uuid(),
        "format": _str(enum=SUBTITLE_FORMATS), "edit_relative_path": _rel(), "created_at": _utc(),
    },
    "ReviewRevision": {
        "schema_version": F("int"), "revision_id": _uuid(), "project_id": _uuid(), "media_id": _uuid(), "base_revision_id": _uuid(nullable=True), "session_id": _uuid(nullable=True),
        "original_file_ref": _rec("ArtifactRef"), "validated_view_ref": _rec("ArtifactRef"), "format": _str(enum=SUBTITLE_FORMATS), "encoding": _str(nonempty=True),
        "provenance_status": _str(enum=PROVENANCE), "correspondences_ref": _rec("ArtifactRef"), "quality_ref": _rec("ArtifactRef"), "created_at": _utc(),
        "extensions": F("object"),
    },
    "Correspondence": {"edited_cue_ordinal": F("int", min=0), "source_cue_ids": _list(item=_str()), "mapping_status": _str(enum=("exact", "candidate", "unmapped")), "reason": _str()},
    # ---- W05: the file correspondences_ref points to, the read-only parsed view of a human file (plan 6.7 / 9.2 step 3)
    #      and what resolve_revision returns (plan 6.8: "the hash-verified concrete revision and its file refs").
    "CorrespondenceSet": {
        "schema_version": F("int"), "revision_id": _uuid(), "base_revision_id": _uuid(nullable=True), "base_format": _str(nullable=True, enum=SUBTITLE_FORMATS),
        "base_subtitle_sha256": _sha(nullable=True), "edited_format": _str(enum=SUBTITLE_FORMATS), "correspondences": _list(record="Correspondence"),
        "summary": F("object"), "extensions": F("object"),
    },
    "ViewCue": {
        "ordinal": F("int", min=0), "cue_id": _str(nonempty=True), "kind": _str(enum=("dialogue", "comment")), "line": F("int", min=1),
        "start_ms": F("int", nullable=True, doc="None when the time is not a canonical value (QC02); never corrected"), "end_ms": F("int", nullable=True),
        "text": _str(doc="display text: SRT lines joined by LF; ASS with override blocks removed"), "fields": F("object", doc="ASS: every Format field by name, Text raw"),
        "layer": F("int", nullable=True), "style": _str(nullable=True), "name": _str(nullable=True), "drawing": F("bool"), "notes": F("object"),
    },
    "SubtitleView": {
        "schema_version": F("int"), "format": _str(enum=SUBTITLE_FORMATS), "encoding": _str(nonempty=True), "encoding_source": _str(enum=("bom", "utf-8", "user")),
        "sha256": _sha(doc="of the original bytes this view was read from"), "bytes": F("int", min=0), "newlines": F("object"), "cues": _list(record="ViewCue"),
        "styles": _list(item=F("object")), "script_info": F("object"), "sections": _list(item=F("object")), "problems": _list(item=F("object")), "extensions": F("object"),
    },
    # In-memory only; subtitle_files maps a format to a project-relative ArtifactRef, confirmations maps a format to
    # {subtitle_sha256, human_status, confirmed_scopes, attestation_ids} (the union of the attestations of those exact bytes).
    "ResolvedRevision": {
        "kind": _str(enum=("machine", "source", "review")), "revision_id": _uuid(), "project_id": _uuid(), "media_id": _uuid(), "directory": _rel(),
        "manifest": F("object"), "subtitle_files": F("object"), "confirmations": F("object"), "notices": _list(item=F("any")),
    },
    # In-memory input of quality.check_quality (plan 6.8). revision_kind and extensions are additions: the rules differ for
    # machine and human files, and the review-base facts (QC20) are computed by revisions before the check runs.
    "QualityInput": {
        "revision_id": _uuid(), "revision_kind": _str(enum=("machine", "source", "review")), "media_record": F("object", nullable=True),
        "subtitle_ref": _rec("ArtifactRef"), "parsed_view": _rec("SubtitleView"), "source_revision": F("object", nullable=True),
        "recognition_refs": _list(record="ArtifactRef"), "translation_ref": _rec("ArtifactRef", nullable=True), "rule_config": _rec("QualityConfig"),
        "attestations": _list(record="Attestation"), "extensions": F("object"),
    },
    "Attestation": {
        "schema_version": F("int"), "attestation_id": _uuid(), "revision_id": _uuid(), "subtitle_sha256": _sha(), "media_id": _uuid(),
        "scopes": _list(item=_str(enum=SCOPES), nonempty=True), "actor": _str(enum=("user",)), "created_at": _utc(), "note": _str(),
    },
    "ExportRequest": {
        "schema_version": F("int"), "export_id": _uuid(), "project_id": _uuid(), "revision_id": _uuid(), "media_id": _uuid(), "kind": _str(enum=EXPORT_KINDS),
        "selected_audio_stream": F("int", nullable=True, min=0), "profile_id": _str(nonempty=True), "destination_root": _str(nonempty=True), "include_media": F("bool"),
        "quality_acknowledgement": F("object"), "created_at": _utc(),
    },
    # ---- 6.8 storage roots
    "ProjectRecord": {
        "schema_version": F("int"), "project_id": _uuid(), "display_name": _str(), "created_at": _utc(), "media_ids": _list(item=_str()), "run_ids": _list(item=_str()),
        "machine_revision_ids": _list(item=_str()), "source_revision_ids": _list(item=_str()), "review_revision_ids": _list(item=_str()), "export_ids": _list(item=_str()),
        "last_selected_revision_id": _uuid(nullable=True), "extensions": F("object"),
    },
    "QueueEnvelope": {
        "schema_version": F("int"), "request": _rec("JobRequest"), "status": _str(enum=REQUEST_STATUSES), "run_id": _uuid(nullable=True),
        "last_error": _rec("ErrorObject", nullable=True), "created_at": _utc(), "updated_at": _utc(),
    },
    "SubtitleFiles": {"srt": _rec("ArtifactRef"), "ass": _rec("ArtifactRef")},
    "MachineRevision": {
        "schema_version": F("int"), "revision_id": _uuid(), "project_id": _uuid(), "run_id": _uuid(), "media_id": _uuid(), "source_revision_id": _uuid(),
        "subtitle_files": _rec("SubtitleFiles"), "translations_ref": _rec("ArtifactRef"), "quality_ref": _rec("ArtifactRef"), "raw_evidence_refs": _list(record="ArtifactRef"), "created_at": _utc(),
    },
    "StageManifest": {
        "schema_version": F("int"), "run_id": _uuid(), "request_id": _uuid(), "stage": _str(enum=STAGES), "attempt_id": _uuid(), "input_signature": _sha(),
        "outputs": _list(record="ArtifactRef"), "validation_ref": _rec("ArtifactRef"), "committed_at": _utc(),
    },
    "OperationResult": {
        "ok": F("bool"), "operation": _str(nonempty=True), "ids": F("object"), "artifact_refs": _list(record="ArtifactRef"),
        "error": _rec("ErrorObject", nullable=True), "warnings": _list(item=F("any")),
    },
    # ---- resources.local.json (user-declared bindings; plan 4.2 / 6.2). Logical id -> explicit local path.
    "ResourceDeclaration": {
        "kind": _str(enum=RESOURCE_KINDS), "absolute_path": _str(nonempty=True), "files": _list(item=_str(nonempty=True)),
        "expected_content_sha256": _sha(nullable=True), "upstream_url": _str(nullable=True), "upstream_revision": _str(nullable=True),
        "license_status": _str(enum=LICENSE_STATUSES), "version_args": _list(item=_str()), "extensions": F("object"),
    },
    "ResourcesFile": {"schema_version": F("int"), "resources": F("object")},
    "CatalogReport": {
        "schema_version": F("int"), "inspected_at": _utc(), "resources_file": _str(nonempty=True), "manifests": F("object"),
        "problems": _list(record="ErrorObject"), "hash_cache_note": _str(),
    },
}
RECORDS["ExportManifest"] = {
    **RECORDS["ExportRequest"],
    "subtitle_input_ref": _rec("ArtifactRef"), "fonts": _list(item=F("any")), "tool_refs": _list(record="ResourceRef"), "profile_snapshot": F("object"),
    "output_refs": _list(record="ArtifactRef"), "validation": F("object"), "quality_snapshot": F("object"), "status": _str(enum=("COMMITTED",)), "completed_at": _utc(),
}

# Root records carry schema_version when stored on their own; nested copies inherit it (plan 6.1).
ROOT_KINDS = frozenset(kind for kind, fields in RECORDS.items() if "schema_version" in fields)
RECORD_KINDS = tuple(RECORDS)


def _check_quality_config(value: dict) -> list[str]:
    low, high = value.get("min_duration_ms"), value.get("max_duration_ms")
    if low is not None and high is not None and high <= low:
        return ["max_duration_ms must be greater than min_duration_ms when both are set"]
    return []


def _check_job_request(value: dict) -> list[str]:
    problems = []
    mode = value.get("recognition_mode")
    source = value.get("source_revision_id")
    if mode == "supplied_source" and not source:
        problems.append("supplied_source requires source_revision_id")
    if mode != "supplied_source" and source:
        problems.append("source_revision_id is only allowed with supplied_source")
    if mode == "speech_only" and value.get("timing_mode") != "speech":
        problems.append("speech_only fixes timing_mode=speech")
    if mode == "visual_only" and value.get("timing_mode") != "screen":
        problems.append("visual_only fixes timing_mode=screen")
    job_input = value.get("input") or {}
    if job_input.get("kind") == "url" and not re.match(r"^https?://", str(job_input.get("value", ""))):
        problems.append("url input must use http or https")
    return problems


def _check_config_snapshot(value: dict) -> list[str]:
    problems = []
    body = {key: item for key, item in value.items() if key != "config_sha256"}
    try:
        expected = sha256_canonical(body)
    except (TypeError, ValueError) as exc:
        return [f"config snapshot is not canonical JSON: {exc}"]
    if value.get("config_sha256") != expected:
        problems.append("config_sha256 does not match the snapshot body")
    problems.extend(f"engine_settings must not contain secrets: {path}" for path in find_sensitive_keys(value.get("engine_settings", {})))
    return problems


def _check_media_record(value: dict) -> list[str]:
    source = value.get("source_ref") or {}
    if source.get("sha256") != value.get("source_sha256"):
        return ["source_ref.sha256 must equal source_sha256"]
    return []


def _check_roi(value: dict) -> list[str]:
    try:
        ok = 0 <= value["left"] < value["right"] <= 1 and 0 <= value["top"] < value["bottom"] <= 1
    except (KeyError, TypeError):
        return []
    return [] if ok else ["roi must satisfy 0<=left<right<=1 and 0<=top<bottom<=1"]


def _check_word(value: dict) -> list[str]:
    try:
        return [] if value["char_start"] <= value["char_end"] else ["char_start must not exceed char_end"]
    except (KeyError, TypeError):
        return []


def _check_interval_like(value: dict) -> list[str]:
    try:
        return [] if value["start_ms"] <= value["end_ms"] else ["end_ms must not be before start_ms"]
    except (KeyError, TypeError):
        return []


def _check_file_ref(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    kind = "ExternalFileRef" if isinstance(value, dict) and "absolute_path" in value else "ArtifactRef"
    problems: list[str] = []
    _validate_record(kind, value, label, problems, root=False)
    return problems


def _check_recognition_request(value: dict) -> list[str]:
    problems = _check_file_ref(value.get("video_artifact"), "video_artifact") + _check_file_ref(value.get("audio_artifact"), "audio_artifact")
    mode = value.get("recognition_mode")
    if mode in ("combined", "visual_only") and value.get("video_artifact") is None:
        problems.append(f"{mode} needs video_artifact")
    if mode in ("combined", "speech_only") and value.get("audio_artifact") is None:
        problems.append(f"{mode} needs audio_artifact")
    return problems


def _check_segment(value: dict) -> list[str]:
    problems = _check_interval_like(value)
    for source_range in value.get("source_ranges") or []:
        try:
            if source_range["char_start"] >= source_range["char_end"]:
                problems.append("source_ranges must be non-empty half-open character ranges")
        except (KeyError, TypeError):
            pass
    return problems


def _check_segment_set(value: dict) -> list[str]:
    problems = []
    segments = value.get("segments") or []
    cue_ids = [segment.get("cue_id") for segment in segments if isinstance(segment, dict)]
    if len(set(cue_ids)) != len(cue_ids):
        problems.append("cue_id must be unique inside one source revision")
    if [segment.get("index") for segment in segments if isinstance(segment, dict)] != list(range(1, len(segments) + 1)):
        problems.append("segment index must count from 1 without gaps")
    if (value.get("status") == "EMPTY") != (not segments):
        problems.append("status must be EMPTY exactly when there are no segments")
    return problems


def _check_translation_set(value: dict) -> list[str]:
    problems = []
    records = value.get("records") or []
    cue_ids = [record.get("cue_id") for record in records if isinstance(record, dict)]
    if len(set(cue_ids)) != len(cue_ids):
        problems.append("a cue must have exactly one translation record")
    if any(isinstance(record, dict) and record.get("source_revision_id") != value.get("source_revision_id") for record in records):
        problems.append("every translation record must belong to the set's source revision")
    if (value.get("status") == "EMPTY") != (not records):
        problems.append("status must be EMPTY exactly when there are no records")
    return problems


def _check_operation_result(value: dict) -> list[str]:
    if value.get("error") is not None and value.get("ok") is not False:
        return ["ok must be false when error is present"]
    return []


def _check_error_object(value: dict) -> list[str]:
    code = value.get("code")
    return [] if code in ERROR_CODES else [f"unknown error code {code!r}"]


RECORD_CHECKS: dict[str, Callable[[dict], list[str]]] = {
    "QualityConfig": _check_quality_config, "JobRequest": _check_job_request, "ConfigSnapshot": _check_config_snapshot,
    "MediaRecord": _check_media_record, "ROI": _check_roi, "Word": _check_word, "Interval": _check_interval_like,
    "Parent": _check_interval_like, "Segment": _check_segment, "VisualEvent": _check_interval_like,
    "OperationResult": _check_operation_result, "ErrorObject": _check_error_object,
    "RecognitionRequest": _check_recognition_request, "SegmentSet": _check_segment_set, "TranslationSet": _check_translation_set,
}

SENSITIVE_KEY_RE = re.compile(r"(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)", re.IGNORECASE)


def find_sensitive_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if SENSITIVE_KEY_RE.search(str(key)):
                found.append(here)
            found.extend(find_sensitive_keys(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_sensitive_keys(item, f"{path}[{index}]"))
    return found


# --------------------------------------------------------------------------- validation

def _validate_value(spec: F, value: Any, path: str, problems: list[str], *, root: bool) -> None:
    if value is None:
        if not spec.nullable:
            problems.append(f"{path}: must not be null")
        return
    kind = spec.type
    if kind == "any":
        _scan_numbers(value, path, problems)
        return
    if kind == "capability":
        if not (isinstance(value, bool) or value in ("unknown", "not_applicable")):
            problems.append(f"{path}: capability must be true, false, 'unknown' or 'not_applicable'")
        return
    if kind == "str":
        if not isinstance(value, str):
            problems.append(f"{path}: must be a string")
            return
        if spec.nonempty and not value.strip():
            problems.append(f"{path}: must not be empty")
        if spec.enum and value not in spec.enum:
            problems.append(f"{path}: must be one of {list(spec.enum)}")
        if spec.check:
            message = spec.check(value)
            if message:
                problems.append(f"{path}: {message}")
        return
    if kind == "int":
        if not _is_int(value):
            problems.append(f"{path}: must be an integer")
            return
        if spec.min is not None and value < spec.min:
            problems.append(f"{path}: must be >= {spec.min}")
        if spec.enum and value not in spec.enum:
            problems.append(f"{path}: must be one of {list(spec.enum)}")
        return
    if kind == "number":
        if not _is_number(value):
            problems.append(f"{path}: must be a finite number")
            return
        if spec.min is not None and value < spec.min:
            problems.append(f"{path}: must be >= {spec.min}")
        return
    if kind == "bool":
        if not isinstance(value, bool):
            problems.append(f"{path}: must be true or false")
        return
    if kind == "object":
        if not isinstance(value, dict):
            problems.append(f"{path}: must be an object")
            return
        _scan_numbers(value, path, problems)
        return
    if kind == "list":
        if not isinstance(value, list):
            problems.append(f"{path}: must be an array")
            return
        if spec.nonempty and not value:
            problems.append(f"{path}: must not be empty")
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if spec.record:
                _validate_record(spec.record, item, item_path, problems, root=False)
            elif spec.item:
                _validate_value(spec.item, item, item_path, problems, root=False)
        return
    if kind == "record":
        _validate_record(spec.record or "", value, path, problems, root=False)
        return
    problems.append(f"{path}: unsupported spec type {kind}")


def _scan_numbers(value: Any, path: str, problems: list[str]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        problems.append(f"{path}: NaN/Infinity are not allowed")
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_numbers(item, f"{path}.{key}", problems)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_numbers(item, f"{path}[{index}]", problems)


def _validate_record(kind: str, value: Any, path: str, problems: list[str], *, root: bool) -> None:
    fields = RECORDS.get(kind)
    if fields is None:
        problems.append(f"{path}: unknown record kind {kind!r}")
        return
    if not isinstance(value, dict):
        problems.append(f"{path}: {kind} must be an object")
        return
    if "schema_version" in fields:
        version = value.get("schema_version")
        if root or "schema_version" in value:
            if version != SCHEMA_VERSION:
                problems.append(f"{path}.schema_version: unsupported schema version {version!r} (expected {SCHEMA_VERSION})")
    for name, spec in fields.items():
        if name == "schema_version":
            continue
        if name not in value:
            problems.append(f"{path}.{name}: required field is missing")
            continue
        _validate_value(spec, value[name], f"{path}.{name}", problems, root=False)
    unknown = sorted(key for key in value if key not in fields and key != "schema_version")
    if unknown:
        hint = " (unknown fields belong in extensions)" if "extensions" in fields else ""
        problems.append(f"{path}: unknown field(s) {unknown}{hint}")
    check = RECORD_CHECKS.get(kind)
    if check and not problems:
        problems.extend(f"{path}: {message}" for message in check(value))


def validate_record(kind: str, value: Any) -> list[str]:
    """Return a list of problems; an empty list means the value satisfies the contract."""
    problems: list[str] = []
    _validate_record(kind, value, "$", problems, root=True)
    return problems


def require_record(kind: str, value: Any, *, source: str = "") -> dict:
    problems = validate_record(kind, value)
    if problems:
        version_problem = any("unsupported schema version" in item or "unknown record kind" in item for item in problems)
        code = "SCHEMA_UNSUPPORTED" if version_problem else "SCHEMA_INVALID"
        raise ContractError(code, f"{kind} record is not valid" + (f" ({source})" if source else ""), details={"problems": problems})
    return value


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in machine-readable JSON")


def parse_json_text(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def load_record(kind: str, path: Path | str) -> dict:
    """Read a UTF-8 JSON file (no BOM) and validate it as ``kind``; raise ContractError otherwise."""
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise ContractError("INPUT_MISSING", f"cannot read {kind} record: {exc}", details={"path": str(file_path)}) from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ContractError("SCHEMA_INVALID", f"{kind} record must be UTF-8 without BOM", details={"path": str(file_path)})
    try:
        value = parse_json_text(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError("SCHEMA_INVALID", f"{kind} record is not valid JSON: {exc}", details={"path": str(file_path)}) from exc
    return require_record(kind, value, source=str(file_path))


def dump_record(kind: str, value: dict, path: Path | str) -> Path:
    """Validate and write a record as pretty UTF-8 JSON (LF, no BOM). Does not use os.replace; callers decide atomicity."""
    require_record(kind, value)
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    file_path.write_text(text, encoding="utf-8", newline="\n")
    return file_path


def signature(value: Any) -> str:
    """SHA-256 over canonical JSON; the cache/identity signature used across the v2 workflow."""
    return sha256_canonical(value)


# --------------------------------------------------------------------------- JSON Schema export

def to_json_schema(kind: str) -> dict:
    """Export a record kind as a JSON Schema (draft 2020-12) document for schemas/."""
    fields = RECORDS[kind]
    properties: dict[str, Any] = {}
    for name, spec in fields.items():
        properties[name] = _spec_schema(spec) if name != "schema_version" else {"const": SCHEMA_VERSION}
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://schemas.roast.local/v2/{kind}.schema.json",
        "title": kind,
        "type": "object",
        "properties": properties,
        "required": [name for name in fields if name != "schema_version" or kind in ROOT_KINDS],
        "additionalProperties": False,
    }
    return schema


def _spec_schema(spec: F) -> dict:
    base: dict[str, Any]
    if spec.type == "str":
        base = {"type": "string"}
        if spec.enum:
            base["enum"] = list(spec.enum)
        if spec.nonempty:
            base["minLength"] = 1
    elif spec.type == "int":
        base = {"type": "integer"}
        if spec.min is not None:
            base["minimum"] = spec.min
        if spec.enum:
            base["enum"] = list(spec.enum)
    elif spec.type == "number":
        base = {"type": "number"}
        if spec.min is not None:
            base["minimum"] = spec.min
    elif spec.type == "bool":
        base = {"type": "boolean"}
    elif spec.type == "object":
        base = {"type": "object"}
    elif spec.type == "capability":
        base = {"oneOf": [{"type": "boolean"}, {"enum": ["unknown", "not_applicable"]}]}
    elif spec.type == "list":
        base = {"type": "array"}
        if spec.record:
            base["items"] = {"$ref": f"{spec.record}.schema.json"}
        elif spec.item:
            base["items"] = _spec_schema(spec.item)
        if spec.nonempty:
            base["minItems"] = 1
    elif spec.type == "record":
        base = {"$ref": f"{spec.record}.schema.json"}
    else:
        base = {}
    if spec.nullable:
        return {"oneOf": [base, {"type": "null"}]} if base else {}
    return base


__all__ = [
    "SCHEMA_VERSION", "RULE_SET_VERSION", "ERROR_CODES", "ContractError", "RECORDS", "RECORD_KINDS", "ROOT_KINDS",
    "RECOGNITION_MODES", "TIMING_MODES", "STAGES", "STAGE_STATUSES", "REQUEST_STATUSES", "RESOURCE_KINDS", "CAPABILITY_KEYS",
    "LICENSE_STATUSES", "SUBTITLE_FORMATS", "SCOPES", "EXPORT_KINDS",
    "validate_record", "require_record", "load_record", "dump_record", "parse_json_text", "to_json_schema",
    "canonical_json", "signature", "sha256_bytes", "sha256_text", "sha256_canonical", "sha256_file",
    "is_safe_relative_posix", "windows_name_problem", "find_sensitive_keys", "new_id", "utc_now",
    "EXIT_OK", "EXIT_ARGS", "EXIT_RESOURCE", "EXIT_PROCESS", "EXIT_DATA", "EXIT_CANCELLED", "EXIT_BUSY",
]
