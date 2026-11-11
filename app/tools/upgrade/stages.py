#!/usr/bin/env python3
"""Stage implementations (plan 1.1, section 10.1): INPUT, MEDIA_PREP, OCR, ASR (W03) and SOURCE, TRANSLATION,
SUBTITLE_BUILD, QUALITY_AND_COMMIT (W04).

Each stage declares the dependencies that decide its cache signature (plan 10.3) and writes its outputs into the
attempt directory handed over by the runner. Settings are read from the configuration snapshot frozen in the
request; only the local resource bindings (paths) come from the current ``resources.local.json``, and their content
identity is checked against the references frozen at submit time.

A missing or changed tool is reported before any engine starts (RESOURCE_MISSING / RESOURCE_CHANGED) and names
every affected role, the translation server included.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from upgrade import media, quality as quality_module, recognition, source as source_module, subtitles, translation
from upgrade.contracts import SCHEMA_VERSION, ContractError, load_record, new_id, parse_json_text, require_record, sha256_file, sha256_text, utc_now
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade.runner import StageContext, stage_signature, update_project
from upgrade.storage import MANIFEST_NAME, artifact_ref, atomic_write_bytes, atomic_write_text, begin_pending, commit_pending, read_manifest, render_json, safe_join, verify_committed

ROLE_DEFAULT_IDS = {"ffprobe": "ffprobe", "ffmpeg": "ffmpeg", "asr_engine": "whisper-cli", "asr_model": "whisper-model",
                    "vad_model": "whisper-vad-model", "ocr_engine": "magia-timeline", "translation_engine": "llama-server", "translation_model": "sakura-model"}
ROLE_KINDS = {"ffprobe": "ffprobe", "ffmpeg": "ffmpeg", "asr_engine": "asr_engine", "asr_model": "model", "vad_model": "model", "ocr_engine": "ocr_engine",
              "translation_engine": "translation_engine", "translation_model": "model"}
TRANSLATION_ROLES = ("translation_engine", "translation_model")
FULL_FRAME_ROI = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
SPEECH_MODES = ("combined", "speech_only")
VISUAL_MODES = ("combined", "visual_only")
MACHINE_SRT_NAME = "machine.zh.srt"
MACHINE_ASS_NAME = "machine.zh.ass"
QUALITY_GATE_VERSION = 2          # W06: the full rule table of quality.py (W04/W05 gate = 1)
GATE_RULES = quality_module.HARD_RULES


# --------------------------------------------------------------------------- settings and resources

def frozen_settings(request: dict) -> dict:
    settings = request["config_snapshot"]["engine_settings"]
    return settings if isinstance(settings, dict) else {}


def _section(settings: dict, name: str) -> dict:
    value = settings.get(name)
    return value if isinstance(value, dict) else {}


def roles_for(request: dict) -> list[str]:
    """Every tool/model role this request needs, recognition and translation included (checked once at INPUT)."""
    mode = request["recognition_mode"]
    roles = ["ffprobe", "ffmpeg"]
    if mode in VISUAL_MODES:
        roles.append("ocr_engine")
    if mode in SPEECH_MODES:
        roles += ["asr_engine", "asr_model"]
        if bool(_section(frozen_settings(request), "recognition").get("vad", True)):
            roles.append("vad_model")
    roles += list(TRANSLATION_ROLES)
    return roles


def resolve_roles(config: dict, request: dict, roles: list[str], *, report: dict | None = None) -> dict[str, dict]:
    """Bind roles to declared resources. ``engine_settings.resources`` may map a role to another resource id."""
    bindings = _section(frozen_settings(request), "resources")
    report = report or inspect_resources(config, probe=False)
    frozen = {ref["resource_id"]: ref for ref in request["resource_refs"]}
    resolved: dict[str, dict] = {}
    failures: list[dict] = []
    for role in roles:
        resource_id = str(bindings.get(role) or ROLE_DEFAULT_IDS[role])
        try:
            item = resolve_resource(resource_id, report)
        except ContractError as exc:
            failures.append({"role": role, "resource_id": resource_id, "code": exc.code, "message": exc.message})
            continue
        if item["manifest"]["kind"] != ROLE_KINDS[role]:
            failures.append({"role": role, "resource_id": resource_id, "code": "RESOURCE_MISSING",
                             "message": f"resource is declared as {item['manifest']['kind']}, the role needs {ROLE_KINDS[role]}"})
            continue
        submitted = frozen.get(resource_id)
        if submitted and submitted["content_sha256"] != item["resource_ref"]["content_sha256"]:
            failures.append({"role": role, "resource_id": resource_id, "code": "RESOURCE_CHANGED",
                             "message": "content differs from the identity frozen when the request was submitted",
                             "expected": submitted["content_sha256"], "observed": item["resource_ref"]["content_sha256"]})
            continue
        resolved[role] = item
    if failures:
        code = "RESOURCE_CHANGED" if all(item["code"] == "RESOURCE_CHANGED" for item in failures) else "RESOURCE_MISSING"
        names = ", ".join(f"{item['role']} ({item['resource_id']})" for item in failures)
        raise ContractError(code, f"required resources are missing or changed: {names}; no engine was started", details={"failures": failures})
    return resolved


def _roles(ctx: StageContext, roles: list[str]) -> dict[str, dict]:
    """Resolve once per request run; the catalog inspection is shared between stages through ``ctx.scratch``."""
    if "resource_report" not in ctx.scratch:
        ctx.scratch["resource_report"] = inspect_resources(ctx.config, probe=False)
    return resolve_roles(ctx.config, ctx.request, roles, report=ctx.scratch["resource_report"])


def _identity(resource: dict) -> str:
    return resource["resource_ref"]["content_sha256"]


def _read_json(path: Path) -> dict:
    value = parse_json_text(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError("SCHEMA_INVALID", "stage output is not a JSON object", details={"path": str(path)})
    return value


def _media_runtime(ctx: StageContext, roles: dict[str, dict]) -> media.MediaRuntime:
    settings = frozen_settings(ctx.request)
    return media.MediaRuntime(project_dir=ctx.project_dir, ffprobe=roles["ffprobe"], ffmpeg=roles.get("ffmpeg"), log_dir=ctx.attempt_dir / "logs",
                              owner_token=ctx.owner_token, cancel=ctx.cancel, emit=ctx.emit, media_settings=_section(settings, "media"),
                              download_settings=_section(settings, "download"))


def _recognition_runtime(ctx: StageContext, roles: dict[str, dict]) -> recognition.RecognitionRuntime:
    app_config = ctx.request["config_snapshot"]["app_config"]
    return recognition.RecognitionRuntime(project_dir=ctx.project_dir, output_dir=ctx.attempt_dir, ascii_temp_root=Path(app_config["ascii_temp_root"]),
                                          resources=roles, owner_token=ctx.owner_token, cancel=ctx.cancel, emit=ctx.emit,
                                          capability_id=str(app_config["defaults"]["gpu_profile"]))


def _audio_none(request: dict) -> bool:
    return request["extensions"].get("audio_stream") == "none"


# --------------------------------------------------------------------------- INPUT

class InputStage:
    """Register (download if needed) and probe the media; check stream selection and the mode's audio requirement."""
    name = "INPUT"

    def signature_inputs(self, ctx: StageContext) -> dict:
        request = ctx.request
        job_input = request["input"]
        stat = None
        if job_input["kind"] == "local_video" and not job_input["source_media_id"]:
            path = Path(job_input["value"])
            if path.is_file():
                found = path.stat()
                stat = {"bytes": found.st_size, "mtime_ns": found.st_mtime_ns}
        return {"input": job_input, "video_stream_index": request["video_stream_index"], "audio_stream_index": request["audio_stream_index"],
                "audio_none": _audio_none(request), "copy_original": bool(request["extensions"].get("copy_original")), "stat": stat,
                "needs_audio": request["recognition_mode"] in SPEECH_MODES,
                "download": _section(frozen_settings(request), "download") if job_input["kind"] == "url" else None,
                "ffprobe": _identity(_roles(ctx, ["ffprobe"])["ffprobe"]), "mapping_version": media.MAPPING_VERSION}

    def run(self, ctx: StageContext) -> dict:
        request = ctx.request
        roles = _roles(ctx, roles_for(request))  # complete pre-check: nothing long starts with a missing engine
        runtime = _media_runtime(ctx, roles)
        job_input = dict(request["input"])
        if request["recognition_mode"] == "supplied_source":
            # the source revision must exist before anything runs; without a video file the run uses the media it is bound to
            loaded = source_module.load_source_revision(ctx.project_dir, request["source_revision_id"])
            if not job_input["source_media_id"] and (job_input["kind"] != "local_video" or not Path(job_input["value"]).is_file()):
                job_input["source_media_id"] = loaded["record"]["media_id"]
        extensions: dict[str, Any] = {}
        if _audio_none(request):
            extensions["audio_stream"] = "none"
        if request["extensions"].get("copy_original"):
            extensions["copy_original"] = True
        if job_input["kind"] == "url" and not job_input["source_media_id"]:
            download = media.download_media(job_input["value"], runtime=runtime)
            extensions["local_path"] = download["local_path"]
            extensions["origin"] = download["origin"]
            atomic_write_text(ctx.attempt_dir / "download.json", render_json(download))
        media_input = require_record("MediaInput", {"input": job_input, "selected_video_stream": request["video_stream_index"],
                                                    "selected_audio_stream": request["audio_stream_index"], "project_id": request["project_id"],
                                                    "extensions": extensions})
        ctx.check_cancelled()
        record = media.probe_media(media_input, runtime=runtime)
        atomic_write_text(ctx.attempt_dir / "media_input.json", render_json(media_input))
        atomic_write_text(ctx.attempt_dir / "media_record.json", render_json(record))
        media_id = record["media_id"]
        update_project(ctx.data_root, request["project_id"], lambda project: project["media_ids"].append(media_id) if media_id not in project["media_ids"] else None)
        if request["recognition_mode"] in SPEECH_MODES and record["selected_audio_stream"] is None:
            raise ContractError("NEEDS_SELECTION", "this media has no selected audio stream; choose the visual_only mode for it (the mode is not changed silently)",
                                details={"kind": "no_audio_stream", "media_id": media_id, "recognition_mode": request["recognition_mode"]})
        return {"ok": True, "media_id": media_id, "source_sha256": record["source_sha256"], "duration_ms": record["duration_ms"],
                "clock_verified": record["clock"]["verified"], "clock_problem": record["extensions"].get("clock_problem"),
                "selected_video_stream": record["selected_video_stream"], "selected_audio_stream": record["selected_audio_stream"],
                "frame_timing": record["extensions"].get("frame_timing"), "unsupported_profile": record["extensions"].get("unsupported_profile")}


# --------------------------------------------------------------------------- MEDIA_PREP

class MediaPrepStage:
    """Time base check and the necessary derivatives: review/recognition video and the ASR WAV (only when the mode needs audio)."""
    name = "MEDIA_PREP"

    def signature_inputs(self, ctx: StageContext) -> dict:
        record = _read_json(ctx.output_dir("INPUT") / "media_record.json")
        roles = _roles(ctx, ["ffprobe", "ffmpeg"])
        mode = ctx.request["recognition_mode"]
        settings = _section(frozen_settings(ctx.request), "media")
        return {"source_sha256": record["source_sha256"], "video_stream": record["selected_video_stream"], "audio_stream": record["selected_audio_stream"],
                "needs_audio": mode in SPEECH_MODES, "needs_visual": mode in VISUAL_MODES,
                "media_settings": {key: settings.get(key) for key in ("auto_convert", "preset", "crf", "accepted_containers", "accepted_video_codecs")},
                "ffprobe": _identity(roles["ffprobe"]), "ffmpeg": _identity(roles["ffmpeg"]), "profile": media.COMPAT_PROFILE_ID,
                "mapping_version": media.MAPPING_VERSION}

    def run(self, ctx: StageContext) -> dict:
        record = _read_json(ctx.output_dir("INPUT") / "media_record.json")
        roles = _roles(ctx, ["ffprobe", "ffmpeg"])
        mode = ctx.request["recognition_mode"]
        plan = require_record("MediaPlan", {
            "media_id": record["media_id"], "needs_visual": mode in VISUAL_MODES, "needs_audio": mode in SPEECH_MODES,
            "compatibility_profile": media.COMPAT_PROFILE_ID, "run_id": ctx.task["run_id"], "attempt_id": ctx.attempt_id or new_id(),
            "extensions": {"selected_audio_stream": record["selected_audio_stream"]}})
        result = media.prepare_media(plan, runtime=_media_runtime(ctx, roles))
        atomic_write_text(ctx.attempt_dir / "media_plan.json", render_json(plan))
        atomic_write_text(ctx.attempt_dir / "media_record.json", render_json(result["media_record"]))
        atomic_write_text(ctx.attempt_dir / "prepare_result.json", render_json({key: result[key] for key in ("video_for_review", "audio_for_asr", "derivation_refs")}))
        audio = result["audio_for_asr"]
        return {"ok": True, "media_id": record["media_id"], "direct_usable": result["media_record"]["extensions"]["direct_usable"],
                "derivative_reasons": result["media_record"]["extensions"]["derivative_reasons"],
                "video_for_review": {"kind": result["video_for_review"]["kind"], "media_revision": result["video_for_review"]["media_revision"]},
                "audio_for_asr": {"media_revision": audio["media_revision"], "output_duration_ms": audio["output_duration_ms"]} if audio else None,
                "clock_mapping": result["media_record"]["clock"]["mapping_kind"]}


# --------------------------------------------------------------------------- OCR / ASR

def _recognition_request(ctx: StageContext, mode: str, prepared: dict) -> dict:
    settings = frozen_settings(ctx.request)
    record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
    roi = _section(settings, "ocr").get("roi") or FULL_FRAME_ROI
    audio = prepared["audio_for_asr"]
    return require_record("RecognitionRequest", {
        "run_id": ctx.task["run_id"], "media_id": record["media_id"], "recognition_mode": mode,
        "video_artifact": prepared["video_for_review"]["ref"] if mode == "visual_only" else None,
        "audio_artifact": audio["ref"] if (mode == "speech_only" and audio) else None,
        "roi": roi, "engine_settings": settings, "resource_refs": ctx.request["resource_refs"]})


class OcrStage:
    """On-screen text with the existing MagiaTimeline engine -> VisualEvidence."""
    name = "OCR"

    def signature_inputs(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        settings = _section(frozen_settings(ctx.request), "ocr")
        return {"video_identity": prepared["video_for_review"]["video_identity"],
                "ocr": {"strategy": settings.get("strategy", "dtd"), "preset": settings.get("preset", "default"), "engine": settings.get("engine", "speculative"),
                        "max_resolution": settings.get("max_resolution", 0), "ocr": settings.get("ocr", True), "roi": settings.get("roi") or FULL_FRAME_ROI},
                "engine": _identity(_roles(ctx, ["ocr_engine"])["ocr_engine"]), "adapter_version": recognition.ADAPTER_VERSION}

    def run(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        roles = _roles(ctx, ["ocr_engine"])
        result = recognition.recognize(_recognition_request(ctx, "visual_only", prepared), runtime=_recognition_runtime(ctx, roles))
        evidence = result["visual_evidence"]
        atomic_write_text(ctx.attempt_dir / "visual_evidence.json", render_json(evidence))
        atomic_write_text(ctx.attempt_dir / "recognition_result.json", render_json({"skip_reasons": result["skip_reasons"], "issues": result["issues"]}))
        return {"ok": True, "evidence_id": evidence["evidence_id"], "events": len(evidence["events"]),
                "text_events": evidence["extensions"]["text_event_count"], "evidence_level": evidence["evidence_level"],
                "empty": evidence["extensions"]["visual_empty"], "issue_codes": [issue["code"] for issue in result["issues"]]}


class AsrStage:
    """Speech with the existing whisper.cpp engine -> SpeechEvidence (serial, after OCR)."""
    name = "ASR"

    def signature_inputs(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        if not prepared["audio_for_asr"]:
            raise ContractError("NEEDS_SELECTION", "speech recognition needs an audio stream; none was prepared", details={"kind": "no_audio_stream"})
        settings = _section(frozen_settings(ctx.request), "recognition")
        roles = _roles(ctx, [role for role in roles_for(ctx.request) if role in ("asr_engine", "asr_model", "vad_model")])
        return {"audio_sha256": prepared["audio_for_asr"]["ref"]["sha256"],
                "recognition": {"language": settings.get("language", "ja"), "vad": settings.get("vad", True),
                                "word_timestamps": settings.get("word_timestamps", True), "dtw_model": settings.get("dtw_model")},
                "engine": _identity(roles["asr_engine"]), "model": _identity(roles["asr_model"]),
                "vad_model": _identity(roles["vad_model"]) if "vad_model" in roles else None, "adapter_version": recognition.ADAPTER_VERSION}

    def run(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        roles = _roles(ctx, [role for role in roles_for(ctx.request) if role in ("asr_engine", "asr_model", "vad_model")])
        result = recognition.recognize(_recognition_request(ctx, "speech_only", prepared), runtime=_recognition_runtime(ctx, roles))
        evidence = result["speech_evidence"]
        atomic_write_text(ctx.attempt_dir / "speech_evidence.json", render_json(evidence))
        atomic_write_text(ctx.attempt_dir / "recognition_result.json", render_json({"skip_reasons": result["skip_reasons"], "issues": result["issues"]}))
        return {"ok": True, "evidence_id": evidence["evidence_id"], "parents": len(evidence["parents"]), "words": len(evidence["word_times"]),
                "word_timing_status": evidence["extensions"]["word_timing_status"], "selected_attempt": evidence["extensions"]["selected_attempt"],
                "empty": evidence["extensions"]["asr_empty"], "issue_codes": [issue["code"] for issue in result["issues"]]}


# --------------------------------------------------------------------------- SOURCE (W04)

def _stage_output_ref(ctx: StageContext, stage: str, name: str) -> dict:
    """Project-relative ArtifactRef (with the manifest's hash) of one committed upstream output."""
    manifest = ctx.outputs[stage]["manifest"]
    ref = next((item for item in manifest["outputs"] if item["path"] == name), None)
    if ref is None:
        raise ContractError("STAGE_FAILED", f"stage {stage} has no output {name}", details={"stage": stage, "name": name})
    relative = (ctx.output_dir(stage) / name).relative_to(ctx.project_dir).as_posix()
    return require_record("ArtifactRef", {**ref, "path": relative})


def _optional_output(ctx: StageContext, stage: str, name: str) -> dict | None:
    stage_record = next(item for item in ctx.task["stages"] if item["name"] == stage)
    if stage_record["status"] != "DONE" or stage not in ctx.outputs:
        return None
    return _stage_output_ref(ctx, stage, name)


class SourceStage:
    """Source record for the run: v8 segmentation over the recognition evidence (machine origin) or the registered
    source revision named by a supplied_source request. Commits a SourceRevision once; writes a pointer into the attempt."""
    name = "SOURCE"

    def signature_inputs(self, ctx: StageContext) -> dict:
        request = ctx.request
        mode = request["recognition_mode"]
        record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
        if mode == "supplied_source":
            loaded = source_module.load_source_revision(ctx.project_dir, request["source_revision_id"])
            return {"mode": mode, "media_id": record["media_id"], "source_revision_id": request["source_revision_id"],
                    "source_manifest_sha256": sha256_file(loaded["dir"] / MANIFEST_NAME), "adapter_version": source_module.SOURCE_ADAPTER_VERSION}
        speech = _optional_output(ctx, "ASR", "speech_evidence.json")
        visual = _optional_output(ctx, "OCR", "visual_evidence.json")
        from subtitle_segments import SEGMENTATION_VERSION
        return {"mode": mode, "timing_mode": request["timing_mode"] if mode == "combined" else None, "media_id": record["media_id"], "duration_ms": record["duration_ms"],
                "speech_sha256": speech["sha256"] if speech else None, "visual_sha256": visual["sha256"] if visual else None,
                "segmentation_version": SEGMENTATION_VERSION, "adapter_version": source_module.SOURCE_ADAPTER_VERSION}

    def run(self, ctx: StageContext) -> dict:
        request = ctx.request
        mode = request["recognition_mode"]
        record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
        if mode == "supplied_source":
            loaded = source_module.load_source_revision(ctx.project_dir, request["source_revision_id"])
            if loaded["record"]["media_id"] != record["media_id"]:
                raise ContractError("MEDIA_CHANGED", "the source revision is bound to another media than this request; register the right media or revision",
                                    details={"source_media_id": loaded["record"]["media_id"], "request_media_id": record["media_id"], "rule_id": "QC21_MEDIA_BINDING"})
            beyond = source_module.check_bounds(loaded["segment_set"]["segments"], record["duration_ms"])
            if beyond:
                raise ContractError("SUBTITLE_INVALID", f"{len(beyond)} cue(s) of the supplied source end after the media (QC03_BOUNDS)",
                                    details={"kind": "bounds", "rule_id": "QC03_BOUNDS", "cues": beyond})
            reused = True
        else:
            sig = stage_signature(self.name, request, self.signature_inputs(ctx))
            loaded = source_module.find_source_revision(ctx.project_dir, stage_signature=sig)
            reused = loaded is not None
            if loaded is None:
                speech = _read_json(ctx.output_dir("ASR") / "speech_evidence.json") if _optional_output(ctx, "ASR", "speech_evidence.json") else None
                visual = _read_json(ctx.output_dir("OCR") / "visual_evidence.json") if _optional_output(ctx, "OCR", "visual_evidence.json") else None
                built = source_module.build_source(recognition_mode=mode, timing_mode=request["timing_mode"], speech_evidence=speech, visual_evidence=visual,
                                                   duration_ms=record["duration_ms"])
                timeline_ref = _optional_output(ctx, "OCR", "ocr/timeline.utf8.ass")
                evidence_refs = {"speech": _optional_output(ctx, "ASR", "speech_evidence.json"), "visual": _optional_output(ctx, "OCR", "visual_evidence.json"),
                                 "timeline": timeline_ref, "media_prep": ctx.task["stages"][1]["output_manifest"]}
                loaded = source_module.commit_source_revision(project_dir=ctx.project_dir, project_id=request["project_id"], media_id=record["media_id"], run_id=ctx.task["run_id"],
                                                              stage_signature=sig, recognition_mode=mode, timing_mode=request["timing_mode"], built=built,
                                                              speech_evidence=speech, visual_evidence=visual, evidence_refs=evidence_refs,
                                                              timeline_ass=safe_join(ctx.project_dir, timeline_ref["path"]).read_bytes() if timeline_ref else None)
                revision_id = loaded["record"]["revision_id"]
                update_project(ctx.data_root, request["project_id"],
                               lambda project: project["source_revision_ids"].append(revision_id) if revision_id not in project["source_revision_ids"] else None)
                ctx.emit({"event": "source_revision_committed", "revision_id": revision_id, "segments": len(loaded["segment_set"]["segments"]), "status": loaded["segment_set"]["status"]})
        revision = loaded["record"]
        segment_set = loaded["segment_set"]
        pointer = {"schema_version": SCHEMA_VERSION, "source_revision_id": revision["revision_id"], "origin": revision["origin"], "status": segment_set["status"],
                   "segment_count": len(segment_set["segments"]), "media_id": revision["media_id"], "reused_existing_revision": reused,
                   "manifest_ref": artifact_ref(ctx.project_dir, loaded["dir"] / MANIFEST_NAME), "segments_ref": loaded["segments_ref"],
                   "source_subtitle_ref": {**revision["source_subtitle_ref"], "path": f"revisions/source/{revision['revision_id']}/{revision['source_subtitle_ref']['path']}"}}
        atomic_write_text(ctx.attempt_dir / "source_ref.json", render_json(pointer))
        return {"ok": True, "source_revision_id": revision["revision_id"], "origin": revision["origin"], "status": segment_set["status"], "segments": len(segment_set["segments"]),
                "needs_review": segment_set["extensions"].get("needs_review"), "coverage": segment_set["coverage"]["status"], "reused_existing_revision": reused}


# --------------------------------------------------------------------------- TRANSLATION (W04)

def _source_pointer(ctx: StageContext) -> dict:
    return _read_json(ctx.output_dir("SOURCE") / "source_ref.json")


def _translation_settings(request: dict) -> dict:
    return _section(frozen_settings(request), "translation")


class TranslationStage:
    """Sakura translation of the source revision through the owned llama-server (translation.translate)."""
    name = "TRANSLATION"

    def signature_inputs(self, ctx: StageContext) -> dict:
        pointer = _source_pointer(ctx)
        roles = _roles(ctx, list(TRANSLATION_ROLES))
        params = translation.effective_parameters(_translation_settings(ctx.request))
        snapshot = ctx.request["config_snapshot"]
        return {"segments_sha256": pointer["segments_ref"]["sha256"], "source_revision_id": pointer["source_revision_id"],
                "dictionary_revision_id": snapshot["dictionary_revision_id"], "accepted_dictionary_warnings": sorted(snapshot["accepted_dictionary_warnings"]),
                "parameters": {key: params[key] for key in ("batch_size", "context_size", "gpu_layers", "temperature", "top_p", "use_native_dictionaries", "native_prompt_mode")},
                "engine": _identity(roles["translation_engine"]), "model": _identity(roles["translation_model"]),
                "adapter_version": translation.TRANSLATION_ADAPTER_VERSION, "protocol": translation.PROTOCOL_ID}

    def run(self, ctx: StageContext) -> dict:
        pointer = _source_pointer(ctx)
        roles = _roles(ctx, list(TRANSLATION_ROLES))
        request = require_record("TranslationRequest", {
            "run_id": ctx.task["run_id"], "source_revision_id": pointer["source_revision_id"], "segments_ref": pointer["segments_ref"],
            "config_snapshot": ctx.request["config_snapshot"], "checkpoint_dir": str(ctx.run_dir / "checkpoints" / "translation")})
        runtime = translation.TranslationRuntime(project_dir=ctx.project_dir, data_root=ctx.data_root, output_dir=ctx.attempt_dir, resources=roles, owner_token=ctx.owner_token,
                                                 cancel=ctx.cancel, emit=ctx.emit, capability_id=str(ctx.request["config_snapshot"]["app_config"]["defaults"]["gpu_profile"]))
        result = translation.translate(request, runtime=runtime)
        atomic_write_text(ctx.attempt_dir / "translation_request.json", render_json(request))
        atomic_write_text(ctx.attempt_dir / "translation_result.json", render_json({"issues": result["issues"], "raw_exchanges": len(result["raw_exchange_refs"])}))
        translations = _read_json(ctx.attempt_dir / result["translations_ref"]["path"])
        return {"ok": True, "source_revision_id": pointer["source_revision_id"], "status": translations["status"], "records": len(translations["records"]),
                "requests": translations["extensions"]["stats"]["requests"], "resumed_cues": translations["extensions"]["stats"]["resumed_cues"],
                "issue_codes": [issue["code"] for issue in result["issues"]], "flag_counts": translations["extensions"]["flag_counts"]}


# --------------------------------------------------------------------------- SUBTITLE_BUILD (W04)

def _ass_template_ref(ctx: StageContext) -> dict | None:
    """The machine timeline stored with the run's source revision (the OCR output it was built from); None -> base style."""
    pointer = _source_pointer(ctx)
    loaded = source_module.load_source_revision(ctx.project_dir, pointer["source_revision_id"])
    return source_module.timeline_template_ref(loaded)


class SubtitleBuildStage:
    """Machine SRT + ASS from the canonical segments and their translations (plan 6.7 / 7.5)."""
    name = "SUBTITLE_BUILD"

    def signature_inputs(self, ctx: StageContext) -> dict:
        pointer = _source_pointer(ctx)
        translations_ref = _stage_output_ref(ctx, "TRANSLATION", "translations.json")
        template = _ass_template_ref(ctx)
        return {"segments_sha256": pointer["segments_ref"]["sha256"], "translations_sha256": translations_ref["sha256"],
                "style": {"template_sha256": template["sha256"] if template else None, "base": None if template else subtitles.BASE_STYLE_ID},
                "format_version": subtitles.MACHINE_FORMAT_VERSION, "output_formats": sorted(ctx.request["config_snapshot"]["app_config"]["defaults"]["output_formats"])}

    def run(self, ctx: StageContext) -> dict:
        pointer = _source_pointer(ctx)
        segment_set = load_record("SegmentSet", safe_join(ctx.project_dir, pointer["segments_ref"]["path"]))
        translations = load_record("TranslationSet", ctx.output_dir("TRANSLATION") / "translations.json")
        record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
        if translations["source_revision_id"] != segment_set["source_revision_id"]:
            raise ContractError("TRANSLATION_IDENTITY_ERROR", "translations belong to another source revision", details={"rule_id": "QC04_IDENTITY"})
        by_cue = {item["cue_id"]: item for item in translations["records"]}
        cues = []
        for segment in segment_set["segments"]:
            item = by_cue.get(segment["cue_id"])
            if item is None or item["source_text_sha256"] != sha256_text(segment["source_text"]):
                raise ContractError("TRANSLATION_IDENTITY_ERROR", f"cue {segment['cue_id']} has no translation for its exact source text", details={"rule_id": "QC17_TRANSLATION"})
            if not item["target_text"].strip():
                raise ContractError("TRANSLATION_IDENTITY_ERROR", f"cue {segment['cue_id']} has an empty translation", details={"rule_id": "QC17_TRANSLATION"})
            cues.append({"index": segment["index"], "cue_id": segment["cue_id"], "start_ms": segment["start_ms"], "end_ms": segment["end_ms"], "text": item["target_text"],
                         "template_event": None, "visual_ids": segment["visual_ids"]})
        if len(by_cue) != len(cues):
            raise ContractError("TRANSLATION_IDENTITY_ERROR", "the translation set has records for cues that are not in the source revision", details={"rule_id": "QC04_IDENTITY"})
        template_ref = _ass_template_ref(ctx)
        if template_ref:
            template_text = safe_join(ctx.project_dir, template_ref["path"]).read_text(encoding="utf-8-sig")
            template = subtitles.parse_template(template_text)
            ordinal_of = {event["id"]: event["ordinal"] for event in segment_set["visual_events"]}
            for cue in cues:
                if cue["visual_ids"] and cue["visual_ids"][0] in ordinal_of:
                    cue["template_event"] = ordinal_of[cue["visual_ids"][0]]
            style = {"source": "machine_timeline", "template_ref": template_ref, "styles": template["styles"], "events_in_template": len(template["events"])}
        else:
            template = subtitles.base_template()
            style = {"source": subtitles.BASE_STYLE_ID, "template_ref": None, "styles": template["styles"], "fontname": "Microsoft YaHei", "fontsize": 80}
        srt_bytes = subtitles.render_srt_bytes([(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in cues])
        ass_bytes, timing_rows = subtitles.render_ass(cues, template)
        atomic_write_bytes(ctx.attempt_dir / MACHINE_SRT_NAME, srt_bytes)
        atomic_write_bytes(ctx.attempt_dir / MACHINE_ASS_NAME, ass_bytes)
        atomic_write_text(ctx.attempt_dir / "timing.ms.json", render_json({"schema_version": SCHEMA_VERSION, "resolution_note": "ASS times are the nearest 10 ms; these are the exact milliseconds",
                                                                            "cues": timing_rows}))
        atomic_write_text(ctx.attempt_dir / "style_profile.json", render_json({"schema_version": SCHEMA_VERSION, **style}))
        expected = [{"index": cue["index"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "text": cue["text"]} for cue in cues]
        checks = {"srt": subtitles.verify_written(ctx.attempt_dir / MACHINE_SRT_NAME, "srt", expected, duration_ms=record["duration_ms"]),
                  "ass": subtitles.verify_written(ctx.attempt_dir / MACHINE_ASS_NAME, "ass", expected, duration_ms=record["duration_ms"])}
        legacy = {}
        if cues:
            from artifact_validation import validate_subtitle
            for kind, name in (("srt", MACHINE_SRT_NAME), ("ass", MACHINE_ASS_NAME)):
                result = validate_subtitle(ctx.attempt_dir / name, "zh_machine", require_text=True, expected_language="zh", expected_cues=len(cues))
                if not result.valid:
                    raise ContractError("SUBTITLE_INVALID", f"the machine {kind.upper()} failed the legacy validator: {result.summary()}", details={"kind": kind, "errors": result.errors})
                legacy[kind] = {"warnings": result.warnings, "cue_count": result.cue_count, "kana_chars": result.kana_char_count, "cjk_chars": result.cjk_char_count}
        build = {"schema_version": SCHEMA_VERSION, "source_revision_id": segment_set["source_revision_id"], "translations_sha256": sha256_file(ctx.output_dir("TRANSLATION") / "translations.json"),
                 "segments_sha256": pointer["segments_ref"]["sha256"], "cues": len(cues), "status": "EMPTY" if not cues else "OK", "read_back": checks, "legacy_validation": legacy,
                 "style": style, "format_version": subtitles.MACHINE_FORMAT_VERSION}
        atomic_write_text(ctx.attempt_dir / "subtitle_build.json", render_json(build))
        return {"ok": True, "cues": len(cues), "status": build["status"], "style_source": style["source"], "legacy_warnings": {k: v["warnings"] for k, v in legacy.items()}}


# --------------------------------------------------------------------------- QUALITY_AND_COMMIT (W04 commit, W06 full rule table and review report)

QUALITY_SCOPE_NAME, REVIEW_CSV_NAME, REVIEW_HTML_NAME = "quality_scope.json", "review.csv", "review.html"


def _find_machine_revision(project_dir: Path, *, run_id: str, srt_sha256: str, ass_sha256: str, translations_sha256: str, quality: dict) -> dict | None:
    root = Path(project_dir) / "revisions" / "machine"
    if not root.is_dir():
        return None
    for candidate in sorted(root.iterdir()):
        if not (candidate / MANIFEST_NAME).is_file():
            continue
        try:
            record = load_record("MachineRevision", candidate / MANIFEST_NAME)
        except ContractError:
            continue
        if (record["run_id"], record["subtitle_files"]["srt"]["sha256"], record["subtitle_files"]["ass"]["sha256"], record["translations_ref"]["sha256"]) == (run_id, srt_sha256, ass_sha256, translations_sha256):
            try:
                verify_committed(candidate, machine_revision_refs(record))
                scope = _read_json(candidate / QUALITY_SCOPE_NAME)
            except (ContractError, OSError, ValueError):
                continue
            if scope.get("rules_version") != quality_module.QUALITY_RULES_VERSION or scope.get("thresholds") != quality:
                continue    # judged by an older rule table or other thresholds: a new revision is committed, the old one stays as it is
            return record
    return None


def machine_revision_refs(record: dict) -> list[dict]:
    return [record["subtitle_files"]["srt"], record["subtitle_files"]["ass"], record["translations_ref"], record["quality_ref"], *record["raw_evidence_refs"]]


class QualityAndCommitStage:
    """QUALITY_AND_COMMIT: the rule table of quality.py (QC01-QC23 as far as evidence exists; the scope file says which rules
    ran), the review files (quality.json, quality_scope.json, review.csv, review.html) and the immutable MachineRevision.
    A hard error commits nothing; warnings never change a subtitle byte (plan 11.1)."""
    name = "QUALITY_AND_COMMIT"

    def signature_inputs(self, ctx: StageContext) -> dict:
        pointer = _source_pointer(ctx)
        record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
        return {"srt_sha256": _stage_output_ref(ctx, "SUBTITLE_BUILD", MACHINE_SRT_NAME)["sha256"], "ass_sha256": _stage_output_ref(ctx, "SUBTITLE_BUILD", MACHINE_ASS_NAME)["sha256"],
                "translations_sha256": _stage_output_ref(ctx, "TRANSLATION", "translations.json")["sha256"], "source_revision_id": pointer["source_revision_id"],
                "media_id": record["media_id"], "duration_ms": record["duration_ms"], "quality": ctx.request["config_snapshot"]["app_config"]["quality"],
                "gate_version": QUALITY_GATE_VERSION, "gate_rules": list(GATE_RULES), "quality_rules_version": quality_module.QUALITY_RULES_VERSION}

    def run(self, ctx: StageContext) -> dict:
        request = ctx.request
        pointer = _source_pointer(ctx)
        record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
        build_dir, translation_dir = ctx.output_dir("SUBTITLE_BUILD"), ctx.output_dir("TRANSLATION")
        build = _read_json(build_dir / "subtitle_build.json")
        translations = load_record("TranslationSet", translation_dir / "translations.json")
        loaded = source_module.load_source_revision(ctx.project_dir, pointer["source_revision_id"])
        segment_set = loaded["segment_set"]
        # identity (QC04): every stage output refers to the same source revision, media and translation bytes
        translations_sha256 = sha256_file(translation_dir / "translations.json")
        if build["translations_sha256"] != translations_sha256 or build["segments_sha256"] != pointer["segments_ref"]["sha256"] \
                or translations["source_revision_id"] != segment_set["source_revision_id"] or segment_set["media_id"] != record["media_id"]:
            raise ContractError("TRANSLATION_IDENTITY_ERROR", "the subtitle build, translations and source revision do not describe the same inputs", details={"rule_id": "QC04_IDENTITY"})
        # coverage (QC05) re-audited on the stored v8 record for machine sources
        if loaded["record"]["origin"] == "machine":
            from subtitle_segments import audit_segment_records
            try:
                audit_segment_records(source_module.read_json(loaded["dir"] / source_module.V8_RECORDS_NAME))
            except ValueError as exc:
                raise ContractError("SOURCE_COVERAGE_ERROR", f"the committed source revision no longer passes the coverage audit: {exc}", details={"rule_id": "QC05_COVERAGE"}) from exc
        expected = []
        by_cue = {item["cue_id"]: item for item in translations["records"]}
        for segment in segment_set["segments"]:
            item = by_cue.get(segment["cue_id"])
            if item is None or not item["target_text"].strip():
                raise ContractError("TRANSLATION_IDENTITY_ERROR", f"cue {segment['cue_id']} has no complete translation", details={"rule_id": "QC17_TRANSLATION"})
            expected.append({"index": segment["index"], "start_ms": segment["start_ms"], "end_ms": segment["end_ms"], "text": item["target_text"]})
        # the files are read back once more against the canonical values and the media bounds (QC01/QC02/QC03/QC06)
        read_back = {"srt": subtitles.verify_written(build_dir / MACHINE_SRT_NAME, "srt", expected, duration_ms=record["duration_ms"]),
                     "ass": subtitles.verify_written(build_dir / MACHINE_ASS_NAME, "ass", expected, duration_ms=record["duration_ms"])}
        srt_sha256, ass_sha256 = sha256_file(build_dir / MACHINE_SRT_NAME), sha256_file(build_dir / MACHINE_ASS_NAME)
        existing = _find_machine_revision(ctx.project_dir, run_id=ctx.task["run_id"], srt_sha256=srt_sha256, ass_sha256=ass_sha256, translations_sha256=translations_sha256,
                                          quality=request["config_snapshot"]["app_config"]["quality"])
        if existing is not None:
            revision_id = existing["revision_id"]
            ctx.emit({"event": "machine_revision_reused", "revision_id": revision_id})
        else:
            revision_id = new_id()
            quality = self._quality(ctx, revision_id, segment_set, translations, record, loaded, build_dir, translation_dir)
            if quality["report"]["structural_status"] != "valid":
                self._refuse(ctx, quality)
            pending = begin_pending(ctx.project_dir)
            shutil.copy2(build_dir / MACHINE_SRT_NAME, pending / MACHINE_SRT_NAME)
            shutil.copy2(build_dir / MACHINE_ASS_NAME, pending / MACHINE_ASS_NAME)
            for name in ("timing.ms.json", "style_profile.json", "subtitle_build.json"):
                shutil.copy2(build_dir / name, pending / name)
            shutil.copy2(translation_dir / "translations.json", pending / "translations.json")
            shutil.copy2(translation_dir / "postprocess_trace.json", pending / "postprocess_trace.json")
            if (translation_dir / "exchanges").is_dir():
                shutil.copytree(translation_dir / "exchanges", pending / "exchanges")
            atomic_write_text(pending / "quality.json", render_json(quality["report"]))
            atomic_write_text(pending / QUALITY_SCOPE_NAME, render_json(quality["scope"]))
            for name, data in quality_module.review_files(quality, title="机器字幕复核报告（未经人工检查）", links=self._links(loaded)).items():
                atomic_write_bytes(pending / name, data)
            if sha256_file(pending / MACHINE_SRT_NAME) != srt_sha256 or sha256_file(pending / MACHINE_ASS_NAME) != ass_sha256 or sha256_file(pending / "translations.json") != translations_sha256:
                raise ContractError("ARTIFACT_CHANGED", "the copied subtitle files differ from the verified stage outputs", details={"rule_id": "QC04_IDENTITY"})
            source_revision_id, media_id, run_id, project_id = segment_set["source_revision_id"], record["media_id"], ctx.task["run_id"], request["project_id"]

            def build_manifest(refs: list[dict]) -> dict:
                by_path = {ref["path"]: ref for ref in refs}
                named = {MACHINE_SRT_NAME, MACHINE_ASS_NAME, "translations.json", "quality.json"}
                return {"schema_version": SCHEMA_VERSION, "revision_id": revision_id, "project_id": project_id, "run_id": run_id, "media_id": media_id,
                        "source_revision_id": source_revision_id, "subtitle_files": {"srt": by_path[MACHINE_SRT_NAME], "ass": by_path[MACHINE_ASS_NAME]},
                        "translations_ref": by_path["translations.json"], "quality_ref": by_path["quality.json"],
                        "raw_evidence_refs": [ref for path, ref in sorted(by_path.items()) if path not in named], "created_at": utc_now()}

            commit_pending(pending, ctx.project_dir / "revisions" / "machine" / revision_id, manifest_kind="MachineRevision", build_manifest=build_manifest)
            ctx.emit({"event": "machine_revision_committed", "revision_id": revision_id, "cues": len(expected), "empty": not expected, "issues": len(quality["report"]["issues"])})
        revision_dir = ctx.project_dir / "revisions" / "machine" / revision_id
        manifest = read_manifest(revision_dir, "MachineRevision")
        verify_committed(revision_dir, machine_revision_refs(manifest))
        quality_report = load_record("QualityReport", revision_dir / "quality.json")
        atomic_write_text(ctx.attempt_dir / "machine_revision_ref.json", render_json({"schema_version": SCHEMA_VERSION, "machine_revision_id": revision_id,
                                                                                       "manifest_ref": artifact_ref(ctx.project_dir, revision_dir / MANIFEST_NAME), "read_back": read_back}))
        return {"ok": True, "machine_revision_id": revision_id, "source_revision_id": pointer["source_revision_id"], "cues": len(expected), "empty": not expected,
                "structural_status": quality_report["structural_status"], "human_status": quality_report["human_status"],
                "issue_rule_ids": [issue["rule_id"] for issue in quality_report["issues"]], "gate_rules": list(GATE_RULES)}

    def _quality(self, ctx: StageContext, revision_id: str, segment_set: dict, translations: dict, media_record: dict, loaded: dict, build_dir: Path, translation_dir: Path) -> dict:
        """The full rule table (quality.py) on the machine SRT, with the ASS layers, the segments, translations and recognition evidence."""
        recognition_dir = loaded["dir"] / "recognition"

        def optional(path: Path) -> dict | None:
            return source_module.read_json(path) if path.is_file() else None

        view = subtitles.parse_view((build_dir / MACHINE_SRT_NAME).read_bytes(), "srt")
        ass_view = subtitles.parse_view((build_dir / MACHINE_ASS_NAME).read_bytes(), "ass")
        evidence = {"segment_set": segment_set, "translation_set": translations, "speech_evidence": optional(recognition_dir / "speech_evidence.json"),
                    "visual_evidence": optional(recognition_dir / "visual_evidence.json"), "postprocess_trace": optional(translation_dir / "postprocess_trace.json")}
        result = quality_module.evaluate({
            "revision_id": revision_id, "revision_kind": "machine", "media_record": media_record, "subtitle_ref": artifact_ref(ctx.project_dir, build_dir / MACHINE_SRT_NAME),
            "parsed_view": view, "source_revision": loaded["record"], "recognition_refs": list(loaded["record"]["recognition_refs"]),
            "translation_ref": artifact_ref(ctx.project_dir, translation_dir / "translations.json"), "rule_config": dict(ctx.request["config_snapshot"]["app_config"]["quality"]),
            "attestations": [], "extensions": {"origin": "machine", "loaded": evidence, "cue_layers": [cue["layer"] for cue in ass_view["cues"] if cue["kind"] == "dialogue"]}})
        result["scope"]["gate_version"] = QUALITY_GATE_VERSION
        result["scope"]["flag_counts"] = {"segments": _flag_counts(segment["flags"] for segment in segment_set["segments"]),
                                          "translations": _flag_counts(item["flags"] for item in translations["records"])}
        return result

    def _refuse(self, ctx: StageContext, quality: dict) -> None:
        """A hard error: no revision. The report is kept in the attempt directory so the failed input can be looked at as it is."""
        failed = ctx.attempt_dir / "quality_refused"
        failed.mkdir(parents=True, exist_ok=True)
        atomic_write_text(failed / "quality.json", render_json(quality["report"]))
        atomic_write_text(failed / QUALITY_SCOPE_NAME, render_json(quality["scope"]))
        for name, data in quality_module.review_files(quality, title="机器字幕复核报告（有结构错误，没有提交）").items():
            atomic_write_bytes(failed / name, data)
        errors = [issue for issue in quality["report"]["issues"] if issue["severity"] == "ERROR"]
        rule_ids = sorted({issue["rule_id"] for issue in errors})
        code = ("SOURCE_COVERAGE_ERROR" if "QC05_COVERAGE" in rule_ids else "TRANSLATION_IDENTITY_ERROR" if {"QC04_IDENTITY", "QC17_TRANSLATION"} & set(rule_ids)
                else "SUBTITLE_INVALID")
        raise ContractError(code, f"the machine subtitle has {len(errors)} structural error(s); no revision was committed", details={
            "rule_ids": rule_ids, "rule_id": rule_ids[0], "report_ref": artifact_ref(ctx.project_dir, failed / "quality.json"),
            "issues": [{"rule_id": issue["rule_id"], "message": issue["message"]} for issue in errors[:20]]})

    @staticmethod
    def _links(loaded: dict) -> list[dict]:
        source_rel = f"../../source/{loaded['record']['revision_id']}/"
        links = [{"label": "机器中文字幕（SRT）", "href": MACHINE_SRT_NAME}, {"label": "机器中文字幕（ASS，保留层和样式）", "href": MACHINE_ASS_NAME},
                 {"label": "质量记录（quality.json）", "href": "quality.json"}, {"label": "复核表（review.csv）", "href": REVIEW_CSV_NAME},
                 {"label": "日文源字幕（本次翻译所用的 canonical source）", "href": source_rel + loaded["record"]["source_subtitle_ref"]["path"]}]
        for name, label in ((source_module.ASR_REFERENCE_NAME, "日文语音识别参考字幕（ASR，只作对照）"), (source_module.OCR_REFERENCE_NAME, "日文画面文字参考字幕（OCR，只作对照）")):
            if (loaded["dir"] / name).is_file():
                links.append({"label": label, "href": source_rel + name})
        return links


def _flag_counts(flag_lists) -> dict[str, int]:
    counts: dict[str, int] = {}
    for flags in flag_lists:
        for flag in flags:
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- submit-time stream check

def preflight_streams(config: dict, request_like: dict, video: Path, *, mode: str, video_stream: int | None, audio_stream: int | None,
                      audio_none: bool) -> dict:
    """Resolve the stream selection of a local file before it is queued (plan 12.1: NEEDS_SELECTION instead of guessing).

    Returns ``{"checked": bool, "video_stream": int|None, "audio_stream": int|None, "warning": str|None}``. Without a
    registered ffprobe nothing is probed here; the INPUT stage reports the missing resource when the queue runs.
    """
    try:
        roles = resolve_roles(config, request_like, ["ffprobe"])
    except ContractError as exc:
        return {"checked": False, "video_stream": video_stream, "audio_stream": audio_stream,
                "warning": f"stream selection was not checked at submit time: {exc.message}"}
    with tempfile.TemporaryDirectory(prefix="roast-preflight-") as temp:
        runtime = media.MediaRuntime(project_dir=Path(temp), ffprobe=roles["ffprobe"], log_dir=Path(temp) / "logs", owner_token="preflight")
        _, raw = media.probe_format_streams(video, runtime)
    selected_video, selected_audio = media.select_streams(media.normalize_streams(raw), requested_video=video_stream, requested_audio=audio_stream,
                                                          audio_none=audio_none)
    if mode in SPEECH_MODES and selected_audio is None:
        raise ContractError("NEEDS_SELECTION", "this file has no usable audio stream for the chosen mode; use --mode visual_only",
                            details={"kind": "no_audio_stream", "recognition_mode": mode})
    return {"checked": True, "video_stream": selected_video, "audio_stream": selected_audio, "warning": None}


__all__ = ["ROLE_DEFAULT_IDS", "ROLE_KINDS", "roles_for", "resolve_roles", "frozen_settings", "InputStage", "MediaPrepStage", "OcrStage", "AsrStage",
           "preflight_streams"]
