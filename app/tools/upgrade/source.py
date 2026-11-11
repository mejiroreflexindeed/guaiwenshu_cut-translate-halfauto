#!/usr/bin/env python3
"""Source records: the existing v8 segmentation behind the v2 SourceRevision (plan 1.1, sections 6.5, 8.3, 10.1; W04).

What is kept: ``subtitle_segments.build_segments`` / ``audit_segment_records`` run unchanged on the recognition
evidence, exactly as the legacy pipeline fed them (speech cues from the ASR SRT, visual cues from the OCR timeline,
word times from the native token clocks). The complete v8 record is stored verbatim (``segments.v8.json``) so the
canonical ranges, times and texts can always be compared with the algorithm's own output.

What this module adds is the outer conversion only: v8 items become ``Segment`` records (public key
``(source_revision_id, cue_id)``, the old id kept as ``legacy_cue_id``), parent ids point at the SpeechEvidence
parents, visual ids at the VisualEvidence events, and a machine-origin ``SourceRevision`` is committed once with the
canonical source SRT and the ASR / OCR reference subtitles. The four modes map as the plan fixes them:
combined -> v8 mixed/screen/speech, speech_only -> v8 speech without visual input, visual_only -> visual events only
(role=screen, no speech coverage denominator), supplied_source -> an already registered revision is referenced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upgrade.contracts import ContractError, SCHEMA_VERSION, load_record, new_id, parse_json_text, require_record, sha256_text, utc_now
from upgrade.storage import artifact_ref, atomic_write_bytes, atomic_write_text, begin_pending, commit_pending, render_json, safe_join, verify_committed
from upgrade.subtitles import render_srt_bytes

SOURCE_ADAPTER_VERSION = 1
BOUNDS_TOLERANCE_MS = 1        # QC03: canonical millisecond values may exceed the media end by at most 1 ms
SOURCE_SUBTITLE_NAME = "source.ja.srt"
SEGMENTS_NAME = "segments.json"
V8_RECORDS_NAME = "segments.v8.json"
ASR_REFERENCE_NAME = "recognition/reference.asr.ja.srt"
OCR_REFERENCE_NAME = "recognition/reference.ocr.ja.srt"
TIMELINE_ASS_NAME = "recognition/timeline.utf8.ass"
SOURCE_CONFLICT_FLAGS = ("TEXT_CONFLICT", "TEXT_ALIGNMENT_AMBIGUOUS", "VISUAL_MAPPING_UNRESOLVED")


# --------------------------------------------------------------------------- evidence -> legacy inputs

def speech_cues(evidence: dict) -> list:
    """SpeechEvidence parents -> the legacy ``Cue`` list (index = position + 1, as ``parse_srt`` numbered them)."""
    from subtitle_axis_align import Cue
    return [Cue(parent["start_ms"], parent["end_ms"], parent["text"], position + 1) for position, parent in enumerate(evidence["parents"])]


def speech_word_times(evidence: dict) -> dict[str, list[dict]] | None:
    """Valid native word clocks per parent, keyed like the legacy word-times file; None when none were requested."""
    if evidence["extensions"].get("word_timing_status") in (None, "not_requested"):
        return None
    by_parent: dict[str, list[dict]] = {str(position + 1): [] for position in range(len(evidence["parents"]))}
    index_of = {parent["id"]: str(position + 1) for position, parent in enumerate(evidence["parents"])}
    for word in evidence["word_times"]:
        if word["timing_valid"] and word["parent_id"] in index_of:
            by_parent[index_of[word["parent_id"]]].append({"char_start": word["char_start"], "char_end": word["char_end"],
                                                           "start_ms": word["start_ms"], "end_ms": word["end_ms"]})
    return by_parent


def visual_cues(evidence: dict) -> list:
    """VisualEvidence events -> legacy ``Cue`` list in ordinal order (placeholder events already carry an empty text)."""
    from subtitle_axis_align import Cue
    return [Cue(event["start_ms"], event["end_ms"], event["text"], event["ordinal"] + 1) for event in sorted(evidence["events"], key=lambda e: e["ordinal"])]


def legacy_arguments(recognition_mode: str, timing_mode: str, speech_evidence: dict | None, visual_evidence: dict | None) -> dict:
    """The exact ``build_segments`` call for each mode (plan 3.1 / 8.3 / 10.1 mode table)."""
    speech = speech_cues(speech_evidence) if speech_evidence else []
    words = speech_word_times(speech_evidence) if speech_evidence else None
    if recognition_mode == "combined":
        return {"speech": speech, "visual": visual_cues(visual_evidence) if visual_evidence else [], "word_times": words, "mode": timing_mode}
    if recognition_mode == "speech_only":
        return {"speech": speech, "visual": [], "word_times": words, "mode": "speech"}
    if recognition_mode == "visual_only":
        return {"speech": [], "visual": visual_cues(visual_evidence) if visual_evidence else [], "word_times": None, "mode": "screen"}
    raise ContractError("NEEDS_SELECTION", f"{recognition_mode} does not build a machine source; it references an existing source revision")


# --------------------------------------------------------------------------- v8 -> Segment

def _role(item: dict, recognition_mode: str) -> str:
    if recognition_mode == "visual_only":
        return "screen"
    has_speech, has_visual = bool(item["source_ranges"]), bool(item["visual_ids"])
    if has_speech and has_visual:
        return "mixed"
    if has_speech:
        return "speech"
    return "screen" if has_visual else "unknown"


def convert_records(records: dict, *, recognition_mode: str, speech_evidence: dict | None, visual_evidence: dict | None) -> dict:
    """v8 records -> {segments, parents, visual_events, unresolved_visual_events, coverage} with evidence ids."""
    parent_ids = {parent["id"]: speech_evidence["parents"][position]["id"] for position, parent in enumerate(records["parents"])} if speech_evidence else {}
    parent_flags = {parent["id"]: list(speech_evidence["parents"][position]["flags"]) for position, parent in enumerate(records["parents"])} if speech_evidence else {}
    events_by_ordinal = {event["ordinal"]: event for event in visual_evidence["events"]} if visual_evidence else {}
    visual_ids = {page["id"]: events_by_ordinal[page["index"] - 1]["id"] for page in records["visual_events"]} if visual_evidence else {}
    if speech_evidence and len(records["parents"]) != len(speech_evidence["parents"]):
        raise ContractError("SOURCE_COVERAGE_ERROR", "the segmentation did not keep every speech parent", details={"v8": len(records["parents"]), "evidence": len(speech_evidence["parents"])})
    segments = []
    for item in records["segments"]:
        ranges = [{"parent_id": parent_ids[span["parent_id"]], "char_start": span["char_start"], "char_end": span["char_end"]} for span in item["source_ranges"]]
        flags = list(item["flags"])
        if any("WORD_CLOCK_REJECTED" in parent_flags.get(span["parent_id"], []) for span in item["source_ranges"]):
            # plan 8.1: the utterance envelope replaces rejected word clocks; the cue is marked for review, nothing is interpolated
            flags = sorted(set(flags) | {"WORD_CLOCK_REJECTED", "CHECK"})
        segments.append(require_record("Segment", {
            "cue_id": f"cue-{item['index']:06d}", "legacy_cue_id": item["id"], "index": item["index"], "start_ms": item["start_ms"], "end_ms": item["end_ms"],
            "source_text": item["text"], "role": _role(item, recognition_mode), "source_ranges": ranges,
            "visual_ids": [visual_ids[key] for key in item["visual_ids"]], "timing_source": item["timing_source"], "flags": flags,
        }))
    parents = [{"id": parent_ids[parent["id"]], "legacy_parent_id": parent["id"], "index": parent["index"], "start_ms": parent["start_ms"],
                "end_ms": parent["end_ms"], "text": parent["text"], "word_count": len(parent.get("words", [])), "flags": parent_flags.get(parent["id"], [])}
               for parent in records["parents"]]
    visual_events = [{"id": visual_ids[page["id"]], "legacy_visual_id": page["id"], "ordinal": page["index"] - 1, "start_ms": page["start_ms"], "end_ms": page["end_ms"],
                      "text": page["text"]} for page in records["visual_events"]]
    unresolved = [{"id": visual_ids[page["id"]], "legacy_visual_id": page["id"], "start_ms": page["start_ms"], "end_ms": page["end_ms"], "text": page["text"],
                   "flags": list(page["flags"])} for page in records["unresolved_visual_events"]]
    if recognition_mode == "visual_only" or not speech_evidence:
        coverage = {"status": "not_applicable", "reason": "no speech parents in this mode; no speech coverage denominator exists", "parents": []}
    else:
        coverage = {"status": "measured", "parents": [{"parent_id": parent_ids[row["parent_id"]], "total_chars": row["total_chars"], "covered_chars": row["covered_chars"],
                                                       "missing_chars": row["missing_chars"]} for row in records["coverage"]]}
    return {"segments": segments, "parents": parents, "visual_events": visual_events, "unresolved_visual_events": unresolved, "coverage": coverage}


def check_bounds(segments: list[dict], duration_ms: int, *, tolerance_ms: int = BOUNDS_TOLERANCE_MS) -> list[dict]:
    """QC03 on canonical values: cues that end after the media (beyond the tolerance). Nothing is clamped."""
    return [{"cue_id": segment["cue_id"], "index": segment["index"], "end_ms": segment["end_ms"], "media_duration_ms": duration_ms}
            for segment in segments if segment["end_ms"] > duration_ms + tolerance_ms]


def build_source(*, recognition_mode: str, timing_mode: str, speech_evidence: dict | None, visual_evidence: dict | None, duration_ms: int) -> dict:
    """Run the v8 algorithm for the mode and convert its result. Returns {records (v8, verbatim), converted, status, needs_review}."""
    from subtitle_axis_align import normalize_text
    from subtitle_segments import SEGMENTATION_VERSION, audit_segment_records, build_segments
    if recognition_mode == "visual_only":
        speech_evidence = None          # the mode has no speech input by definition (plan 8.3)
    if recognition_mode == "speech_only":
        visual_evidence = None
    arguments = legacy_arguments(recognition_mode, timing_mode, speech_evidence, visual_evidence)
    try:
        records = build_segments(arguments["speech"], arguments["visual"], arguments["word_times"], arguments["mode"])
        audit_segment_records(records)
    except ValueError as exc:
        raise ContractError("SOURCE_COVERAGE_ERROR", f"the v8 segmentation rejected or did not cover the recognition evidence: {exc}",
                            details={"recognition_mode": recognition_mode, "timing_mode": arguments["mode"]}) from exc
    converted = convert_records(records, recognition_mode=recognition_mode, speech_evidence=speech_evidence, visual_evidence=visual_evidence)
    blank = [segment["cue_id"] for segment in converted["segments"] if not normalize_text(segment["source_text"])]
    if blank:
        raise ContractError("SUBTITLE_INVALID", "a source segment has no visible text", details={"kind": "empty_source_text", "cue_ids": blank})
    beyond = check_bounds(converted["segments"], duration_ms)
    if beyond:
        raise ContractError("SUBTITLE_INVALID", f"{len(beyond)} source cue(s) end after the media; values are kept, nothing is clamped (QC03_BOUNDS)",
                            details={"kind": "bounds", "rule_id": "QC03_BOUNDS", "cues": beyond, "tolerance_ms": BOUNDS_TOLERANCE_MS})
    return {"records": records, "converted": converted, "status": "EMPTY" if not converted["segments"] else "OK",
            "needs_review": records["needs_review"], "segmentation_version": SEGMENTATION_VERSION, "v8_mode": arguments["mode"]}


# --------------------------------------------------------------------------- files

def source_srt_bytes(segments: list[dict]) -> bytes:
    return render_srt_bytes([(segment["start_ms"], segment["end_ms"], segment["source_text"]) for segment in segments])


def reference_srt_bytes(rows: list[dict]) -> bytes:
    return render_srt_bytes([(row["start_ms"], row["end_ms"], row["text"]) for row in rows])


def revision_dir(project_dir: Path, revision_id: str) -> Path:
    return Path(project_dir) / "revisions" / "source" / revision_id


def revision_refs(record: dict) -> list[dict]:
    return [record["source_subtitle_ref"], record["segments_ref"], *record["recognition_refs"]]


def load_source_revision(project_dir: Path, revision_id: str) -> dict:
    """Committed SourceRevision + its verified SegmentSet: {record, dir, segment_set, segments_ref(project-relative)}."""
    directory = revision_dir(project_dir, revision_id)
    if not (directory / "manifest.json").is_file():
        raise ContractError("INPUT_MISSING", f"source revision {revision_id} is not registered in this project", details={"revision_id": revision_id})
    record = load_record("SourceRevision", directory / "manifest.json")
    if record["revision_id"] != revision_id:
        raise ContractError("SCHEMA_INVALID", "source revision manifest names another revision", details={"path": str(directory), "found": record["revision_id"]})
    verify_committed(directory, revision_refs(record))
    segment_set = load_record("SegmentSet", safe_join(directory, record["segments_ref"]["path"]))
    if segment_set["source_revision_id"] != revision_id or segment_set["media_id"] != record["media_id"]:
        raise ContractError("SCHEMA_INVALID", "segments do not belong to their source revision", details={"revision_id": revision_id})
    segments_ref = {**record["segments_ref"], "path": f"revisions/source/{revision_id}/{record['segments_ref']['path']}"}
    return {"record": record, "dir": directory, "segment_set": segment_set, "segments_ref": require_record("ArtifactRef", segments_ref)}


def find_source_revision(project_dir: Path, *, stage_signature: str) -> dict | None:
    """An already committed machine revision produced for this exact stage signature (idempotent recovery)."""
    root = Path(project_dir) / "revisions" / "source"
    if not root.is_dir():
        return None
    for candidate in sorted(root.iterdir()):
        if not (candidate / "manifest.json").is_file():
            continue
        try:
            record = load_record("SourceRevision", candidate / "manifest.json")
            if record["extensions"].get("stage_signature") != stage_signature:
                continue
            return load_source_revision(project_dir, record["revision_id"])
        except ContractError:
            continue
    return None


def commit_source_revision(*, project_dir: Path, project_id: str, media_id: str, run_id: str, stage_signature: str, recognition_mode: str, timing_mode: str,
                           built: dict, speech_evidence: dict | None, visual_evidence: dict | None, evidence_refs: dict, timeline_ass: bytes | None = None) -> dict:
    """Write the machine-origin SourceRevision once (plan 4.3: pending -> hash -> manifest -> rename).

    ``timeline_ass`` is the machine timeline (OCR output) of the run; it travels with the source revision so every
    machine ASS built from this source inherits the same style (plan 7.5), whichever run builds it."""
    revision_id = new_id()
    converted = built["converted"]
    segment_set = require_record("SegmentSet", {
        "schema_version": SCHEMA_VERSION, "source_revision_id": revision_id, "media_id": media_id, "origin": "machine", "recognition_mode": recognition_mode,
        "timing_mode": timing_mode, "status": built["status"], "segments": converted["segments"], "parents": converted["parents"],
        "visual_events": converted["visual_events"], "unresolved_visual_events": converted["unresolved_visual_events"], "coverage": converted["coverage"],
        "extensions": {"segmentation_version": built["segmentation_version"], "adapter_version": SOURCE_ADAPTER_VERSION, "v8_mode": built["v8_mode"],
                       "needs_review": built["needs_review"], "run_id": run_id, "text_alignment": built["records"].get("text_alignment"),
                       "source_conflict_cue_ids": [segment["cue_id"] for segment in converted["segments"] if set(segment["flags"]) & set(SOURCE_CONFLICT_FLAGS)],
                       "speech_evidence_id": speech_evidence["evidence_id"] if speech_evidence else None,
                       "visual_evidence_id": visual_evidence["evidence_id"] if visual_evidence else None},
    })
    pending = begin_pending(project_dir)
    atomic_write_bytes(pending / SOURCE_SUBTITLE_NAME, source_srt_bytes(converted["segments"]))
    atomic_write_text(pending / SEGMENTS_NAME, render_json(segment_set))
    atomic_write_text(pending / V8_RECORDS_NAME, render_json(built["records"]))
    (pending / "recognition").mkdir()
    if speech_evidence:
        atomic_write_text(pending / "recognition" / "speech_evidence.json", render_json(speech_evidence))
        atomic_write_bytes(pending / ASR_REFERENCE_NAME, reference_srt_bytes(speech_evidence["parents"]))
    if visual_evidence:
        atomic_write_text(pending / "recognition" / "visual_evidence.json", render_json(visual_evidence))
        rows = [event for event in sorted(visual_evidence["events"], key=lambda e: e["ordinal"]) if event["text"].strip()]
        atomic_write_bytes(pending / OCR_REFERENCE_NAME, reference_srt_bytes(rows))
    if timeline_ass is not None:
        atomic_write_bytes(pending / TIMELINE_ASS_NAME, timeline_ass)
    atomic_write_text(pending / "recognition" / "evidence_refs.json", render_json(evidence_refs))

    def build_manifest(refs: list[dict]) -> dict:
        by_path = {ref["path"]: ref for ref in refs}
        return {
            "schema_version": SCHEMA_VERSION, "revision_id": revision_id, "project_id": project_id, "media_id": media_id, "parent_revision_id": None,
            "origin": "machine", "source_subtitle_ref": by_path[SOURCE_SUBTITLE_NAME], "segments_ref": by_path[SEGMENTS_NAME],
            "recognition_refs": [ref for path, ref in sorted(by_path.items()) if path not in (SOURCE_SUBTITLE_NAME, SEGMENTS_NAME)],
            "provenance_status": "exact", "created_at": utc_now(),
            "extensions": {"stage_signature": stage_signature, "run_id": run_id, "recognition_mode": recognition_mode, "timing_mode": timing_mode,
                           "status": built["status"], "segment_count": len(converted["segments"]), "segmentation_version": built["segmentation_version"],
                           "adapter_version": SOURCE_ADAPTER_VERSION, "needs_review": built["needs_review"]},
        }

    commit_pending(pending, revision_dir(project_dir, revision_id), manifest_kind="SourceRevision", build_manifest=build_manifest)
    return load_source_revision(project_dir, revision_id)


def read_json(path: Path) -> Any:
    return parse_json_text(Path(path).read_text(encoding="utf-8"))


def segment_text_sha256(segment: dict) -> str:
    return sha256_text(segment["source_text"])


def project_artifact_ref(project_dir: Path, path: Path) -> dict:
    return artifact_ref(Path(project_dir), path)


def timeline_template_ref(loaded: dict) -> dict | None:
    """Project-relative ArtifactRef of the machine timeline stored with a source revision, when it has one."""
    ref = next((item for item in loaded["record"]["recognition_refs"] if item["path"] == TIMELINE_ASS_NAME), None)
    if ref is None:
        return None
    return require_record("ArtifactRef", {**ref, "path": f"revisions/source/{loaded['record']['revision_id']}/{TIMELINE_ASS_NAME}"})


__all__ = ["SOURCE_ADAPTER_VERSION", "BOUNDS_TOLERANCE_MS", "SOURCE_CONFLICT_FLAGS", "TIMELINE_ASS_NAME", "timeline_template_ref", "speech_cues", "speech_word_times", "visual_cues", "legacy_arguments",
           "convert_records", "check_bounds", "build_source", "source_srt_bytes", "revision_dir", "revision_refs", "load_source_revision",
           "find_source_revision", "commit_source_revision", "segment_text_sha256", "project_artifact_ref"]
