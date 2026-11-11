#!/usr/bin/env python3
"""Recognition: the existing Whisper (speech) and MagiaTimeline (on-screen text) engines behind one Interface
(plan 1.1, sections 6.4, 8.1, 8.2; work package W03).

Interface (fixed by the plan):
    recognize(RecognitionRequest, runtime=...) -> {speech_evidence, visual_evidence, skip_reasons, issues}

What is kept from the legacy adapters (no engine or weight is replaced):
- whisper.cpp command line: ``-m MODEL -osrt -l ja AUDIO -of BASE [-ojf -dtw large.v3] [--vad --vad-model SILERO]``,
  byte-level token reassembly (``subtitle_segments.load_whisper_word_times``) and the single same-audio, same-model
  retry without VAD when the VAD run returns token clocks in compacted-audio coordinates; both attempts are kept;
- MagiaTimeline: per-job configuration in an ASCII-only staging directory, the private UTF-8 manifest copy of the
  executable, the legacy ``dialogRect`` order ``[left, right, top, bottom]`` and lossless output re-decoding.

What changes: engines are started through the owned process runner (argv, no shell, cancellable), outputs are
collected into the stage attempt directory, and the result is expressed as SpeechEvidence / VisualEvidence.
Unknown scores stay ``null``; an engine that only reports intervals and text is ``interval_only``; empty speech is
ASR_EMPTY (a warning, never proof that the audio has no speech); a mode that does not need an engine yields a skip
reason instead of fabricated empty evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from upgrade.contracts import SCHEMA_VERSION, ContractError, new_id, require_record, utc_now
from upgrade.process_runner import CancelToken, ProcessHandle, process_spec
from upgrade.storage import artifact_ref, atomic_write_text, ensure_ascii_root, render_json, safe_join

ADAPTER_VERSION = 1
ASS_TIME_RESOLUTION_MS = 10
EventSink = Callable[[dict], None]


@dataclass
class RecognitionRuntime:
    """Runtime handles injected by the runner (plan 6.8)."""
    project_dir: Path
    output_dir: Path                    # stage attempt directory; evidence raw_refs are relative to it
    ascii_temp_root: Path
    resources: dict[str, dict]          # role -> resolved resource (asr_engine, asr_model, vad_model, ocr_engine)
    owner_token: str
    cancel: CancelToken = field(default_factory=CancelToken)
    emit: EventSink = lambda event: None
    capability_id: str = "verified_existing"


# --------------------------------------------------------------------------- shared helpers

def run_engine(spec: dict, runtime: RecognitionRuntime, *, stdin: bytes | None = None) -> dict:
    """Start one owned engine process and wait for it (the only place an engine is launched)."""
    handle = ProcessHandle(spec, cancel=runtime.cancel, event_sink=runtime.emit).start()
    if stdin is not None:
        handle.send_stdin(stdin)
    return handle.wait()


def resolve_file_ref(ref: dict, project_dir: Path) -> Path:
    """ExternalFileRef (original media) or project-relative ArtifactRef (derived media) -> existing file of the recorded size."""
    path = Path(ref["absolute_path"]) if "absolute_path" in ref else safe_join(Path(project_dir), ref["path"])
    if not path.is_file():
        raise ContractError("INPUT_MISSING", "recognition input file does not exist", details={"path": str(path)})
    if path.stat().st_size != ref["bytes"]:
        raise ContractError("MEDIA_CHANGED", "recognition input file size differs from its recorded identity", details={"path": str(path)})
    return path


def _resource_file(resource: dict, names: tuple[str, ...]) -> Path:
    root = Path(resource["absolute_path"])
    if root.is_file():
        return root
    for name in names:
        if (root / name).is_file():
            return root / name
    raise ContractError("RESOURCE_MISSING", f"none of {list(names)} exists in resource {resource['resource_ref']['resource_id']!r}",
                        details={"resource_id": resource["resource_ref"]["resource_id"], "absolute_path": str(root)})


def _role(runtime: RecognitionRuntime, role: str) -> dict:
    resource = runtime.resources.get(role)
    if resource is None:
        raise ContractError("RESOURCE_MISSING", f"no resource is bound to role {role!r}; no engine was started", details={"role": role})
    return resource


def _require_ascii(path: Path, what: str) -> None:
    if not str(path).isascii():
        raise ContractError("PATH_INVALID", f"{what} is on a non-ASCII path that the engine cannot open; rebind the resource to an ASCII-only location",
                            details={"path": str(path), "what": what})


def _staging_dir(runtime: RecognitionRuntime, kind: str) -> Path:
    root = ensure_ascii_root(runtime.ascii_temp_root)
    directory = Path(root) / kind / uuid.uuid4().hex
    try:
        directory.mkdir(parents=True)
    except OSError as exc:
        raise ContractError("DATA_ROOT_UNWRITABLE", f"cannot create the ASCII staging directory: {exc}", details={"path": str(directory)}) from exc
    return directory


def _link_or_copy(source: Path, target: Path) -> str:
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def _issue(code: str, severity: str, message: str, **details: Any) -> dict:
    return {"code": code, "severity": severity, "message": message, "details": details}


def _refs(base: Path, directory: Path) -> list[dict]:
    return [artifact_ref(base, path) for path in sorted(p for p in directory.rglob("*") if p.is_file())]


# --------------------------------------------------------------------------- speech (whisper.cpp)

def whisper_argv(*, model: Path, language: str, audio: Path, output_base: Path, word_timestamps: bool, dtw_model: str | None,
                 vad_model: Path | None) -> list[str]:
    """The legacy command line, unchanged in order and content (plan 8.1)."""
    argv = ["-m", str(model), "-osrt", "-l", language, str(audio), "-of", str(output_base)]
    if word_timestamps:
        argv += ["-ojf", "-dtw", str(dtw_model)]
    if vad_model is not None:
        argv += ["--vad", "--vad-model", str(vad_model)]
    return argv


def _whisper_attempt(label: str, *, executable: Path, argv_for: Callable[[Path], list[str]], staging: Path, attempt_dir: Path,
                     word_timestamps: bool, runtime: RecognitionRuntime) -> dict:
    from artifact_validation import validate_subtitle
    from subtitle_axis_align import parse_srt
    from subtitle_segments import load_whisper_word_times

    base = staging / label / "jp"
    base.parent.mkdir(parents=True)
    attempt_dir.mkdir(parents=True)
    cwd = executable.parent.parent if executable.parent.parent.is_dir() else executable.parent
    argv = argv_for(base)
    spec = process_spec(executable, argv, cwd=cwd, log_dir=attempt_dir / "process", owner_token=runtime.owner_token, capability_id=runtime.capability_id)
    result = run_engine(spec, runtime)
    native_srt, native_json = Path(str(base) + ".srt"), Path(str(base) + ".json")
    srt, raw_json = attempt_dir / "jp.srt", attempt_dir / "jp.whisper.json"
    for source, target in ((native_srt, srt), (native_json, raw_json)):
        if source.is_file():
            shutil.copy2(source, target)
    attempt: dict[str, Any] = {"label": label, "exit_code": result["exit_code"], "argv": argv, "srt": srt if srt.is_file() else None,
                               "json": raw_json if raw_json.is_file() else None, "cues": [], "raw": None, "word_evidence": None, "valid": True,
                               "empty": False, "problems": []}
    if result["cancelled"] or runtime.cancel.is_set():
        raise ContractError("PROCESS_CANCELLED", "speech recognition was cancelled", retryable=True)
    if result["exit_code"] != 0:
        attempt["valid"] = False
        attempt["problems"].append(f"whisper exited with code {result['exit_code']}; residual output is not accepted")
        return attempt
    if attempt["srt"] is not None and srt.stat().st_size > 0:
        try:
            attempt["cues"] = parse_srt(srt)
        except (ValueError, OSError) as exc:
            attempt["valid"] = False
            attempt["problems"].append(f"native SRT cannot be parsed: {exc}")
            return attempt
    if word_timestamps:
        if attempt["json"] is None:
            attempt["valid"] = False
            attempt["problems"].append("native JSON is missing although word timestamps were requested")
            return attempt
        try:
            attempt["raw"] = json.loads(raw_json.read_bytes().decode("utf-8", errors="surrogateescape"))
            transcription = attempt["raw"]["transcription"]
            if not isinstance(transcription, list):
                raise ValueError("transcription is not a list")
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            attempt["valid"] = False
            attempt["problems"].append(f"native JSON is incomplete: {exc}")
            return attempt
        if not attempt["cues"] and transcription:
            attempt["valid"] = False
            attempt["problems"].append("native JSON has utterances but the SRT has none")
            return attempt
    if not attempt["cues"]:
        if attempt["srt"] is None and attempt["json"] is None:
            attempt["valid"] = False
            attempt["problems"].append("whisper returned success but wrote no output")
            return attempt
        attempt["empty"] = True
        return attempt
    validation = validate_subtitle(srt, "jp_asr", require_text=True, expected_language="ja")
    attempt["validation"] = validation.to_dict()
    if not validation.valid:
        attempt["valid"] = False
        attempt["problems"].append(f"native SRT is invalid: {validation.summary()}")
        return attempt
    if word_timestamps:
        try:
            attempt["word_evidence"] = load_whisper_word_times(raw_json, attempt["cues"])
        except (OSError, ValueError, KeyError, UnicodeError, TypeError) as exc:
            attempt["valid"] = False
            attempt["problems"].append(f"native token timestamps are invalid: {exc}")
    return attempt


def _attempt_summary(attempt: dict) -> dict:
    evidence = attempt.get("word_evidence")
    return {"label": attempt["label"], "exit_code": attempt["exit_code"], "valid": attempt["valid"], "empty": attempt["empty"],
            "cues": len(attempt["cues"]), "problems": attempt["problems"],
            "word_timing_status": evidence["timing_evidence_status"] if evidence else None,
            "coordinate_issues": len(evidence["coordinate_issues"]) if evidence else None}


def _token_scores(raw: dict | None, count: int) -> list[float | None]:
    """Minimum lexical-token probability per utterance when the native JSON really carries ``p`` for every such token."""
    scores: list[float | None] = [None] * count
    if not raw:
        return scores
    for position, utterance in enumerate(raw.get("transcription", [])[:count]):
        values = []
        for token in utterance.get("tokens", []):
            text = str(token.get("text", ""))
            if text.startswith("[_") and text.endswith("]"):
                continue
            probability = token.get("p")
            if not isinstance(probability, (int, float)) or isinstance(probability, bool) or not 0 <= probability <= 1:
                values = None
                break
            values.append(float(probability))
        if values:
            scores[position] = min(values)
    return scores


def _recognize_speech(request: dict, runtime: RecognitionRuntime) -> tuple[dict, list[dict]]:
    settings = request["engine_settings"].get("recognition", {}) if isinstance(request["engine_settings"].get("recognition"), dict) else {}
    engine, model = _role(runtime, "asr_engine"), _role(runtime, "asr_model")
    executable = _resource_file(engine, ("whisper-cli.exe", "whisper-cli"))
    model_path = _resource_file(model, ("ggml-large-v3.bin",))
    language = str(settings.get("language", "ja"))
    vad_enabled = bool(settings.get("vad", True))
    word_timestamps = bool(settings.get("word_timestamps", True))
    dtw_model = str(settings.get("dtw_model", "") or "").strip() or ("large.v3" if "large-v3" in model_path.name.lower() else "")
    if word_timestamps and not dtw_model:
        raise ContractError("CAPABILITY_MISSING", "word timestamps need recognition.dtw_model for the selected Whisper model",
                            details={"model": model_path.name})
    vad_resource = _role(runtime, "vad_model") if vad_enabled else None
    vad_path = _resource_file(vad_resource, ("ggml-silero-v5.1.2.bin",)) if vad_resource else None
    for path, what in ((executable, "whisper executable"), (model_path, "whisper model"), (vad_path, "VAD model")):
        if path is not None:
            _require_ascii(path, what)
    audio = resolve_file_ref(request["audio_artifact"], runtime.project_dir)
    with wave.open(str(audio), "rb") as reader:
        audio_ms = reader.getnframes() * 1000 // reader.getframerate()

    out = runtime.output_dir / "asr"
    out.mkdir(parents=True, exist_ok=False)
    staging = _staging_dir(runtime, "asr")
    try:
        staged_audio = staging / "audio.wav"
        link_mode = _link_or_copy(audio, staged_audio)

        def argv_for(with_vad: bool) -> Callable[[Path], list[str]]:
            return lambda base: whisper_argv(model=model_path, language=language, audio=staged_audio, output_base=base,
                                             word_timestamps=word_timestamps, dtw_model=dtw_model, vad_model=vad_path if with_vad else None)

        first_label = "vad" if vad_enabled else "no_vad"
        first = _whisper_attempt(first_label, executable=executable, argv_for=argv_for(vad_enabled), staging=staging, attempt_dir=out / first_label,
                                 word_timestamps=word_timestamps, runtime=runtime)
        attempts = [first]
        selected = first
        recovery = None
        if not first["valid"]:
            atomic_write_text(out / "attempts.json", render_json({"schema_version": SCHEMA_VERSION, "attempts": [_attempt_summary(first)], "selected": None}))
            raise ContractError("PROCESS_FAILED", "speech recognition did not produce an acceptable result: " + "; ".join(first["problems"]),
                                details={"attempt": _attempt_summary(first), "candidate": str(out)})
        if vad_enabled and first["word_evidence"] is not None and first["word_evidence"]["timing_evidence_status"] == "unusable":
            # The bundled VAD can return restored utterance times with token clocks still in compacted-audio coordinates.
            # Re-run the same complete audio with the same model once; never guess, clamp or interpolate a correction.
            recovery = {"state": "RUNNING", "reason": "NATIVE_TIME_COORDINATES_CONFLICT", "selected": first_label,
                        "coordinate_issue_count": len(first["word_evidence"]["coordinate_issues"]), "same_complete_audio": True, "same_model": True}
            atomic_write_text(out / "recovery.json", render_json(recovery))
            runtime.emit({"event": "asr_clock_recovery_started", "reason": recovery["reason"]})
            try:
                second = _whisper_attempt("without_vad", executable=executable, argv_for=argv_for(False), staging=staging, attempt_dir=out / "without_vad",
                                          word_timestamps=word_timestamps, runtime=runtime)
                attempts.append(second)
                usable = second["valid"] and not second["empty"] and second["word_evidence"] is not None \
                    and second["word_evidence"]["timing_evidence_status"] == "usable"
                if usable:
                    selected = second
                    recovery["selected"] = "without_vad"
                recovery.update(state="FINISHED", recovery_exit_code=second["exit_code"], recovery_valid=second["valid"],
                                recovery_timing_status=second["word_evidence"]["timing_evidence_status"] if second["word_evidence"] else "unavailable")
            except BaseException as exc:
                interrupted = isinstance(exc, KeyboardInterrupt) or (isinstance(exc, ContractError) and exc.code == "PROCESS_CANCELLED")
                recovery.update(state="INTERRUPTED" if interrupted else "FAILED", error_type=type(exc).__name__, error=str(exc))
                raise
            finally:
                atomic_write_text(out / "recovery.json", render_json(recovery))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    atomic_write_text(out / "attempts.json", render_json({"schema_version": SCHEMA_VERSION, "attempts": [_attempt_summary(a) for a in attempts],
                                                          "selected": selected["label"]}))
    issues: list[dict] = []
    word_evidence = selected["word_evidence"]
    scores = _token_scores(selected["raw"], len(selected["cues"]))
    parents, words = [], []
    for position, cue in enumerate(selected["cues"]):
        parent_id = f"asr-{position + 1:06d}"
        score = scores[position]
        parents.append({"id": parent_id, "start_ms": cue.start, "end_ms": cue.end, "text": cue.text, "score": score,
                        "score_kind": "whisper_token_probability" if score is not None else None, "flags": []})
        if word_evidence is None:
            continue
        pointer = f"asr/{selected['label']}/jp.whisper.json#/transcription/{position}"
        for entry in word_evidence["word_times"].get(str(cue.index), []):
            words.append({"parent_id": parent_id, "char_start": entry["char_start"], "char_end": entry["char_end"], "start_ms": entry["start_ms"],
                          "end_ms": entry["end_ms"], "raw_clock_ref": pointer, "timing_valid": True})
    rejected = 0
    if word_evidence is not None:
        index_to_parent = {cue.index: f"asr-{position + 1:06d}" for position, cue in enumerate(selected["cues"])}
        for token in word_evidence["unusable_tokens"]:
            if token.get("reason") != "NATIVE_TIME_COORDINATES_CONFLICT" or "char_start" not in token:
                continue
            rejected += 1
            if token["start_ms"] >= 0 and token["end_ms"] >= token["start_ms"]:
                words.append({"parent_id": index_to_parent[token["cue_index"]], "char_start": token["char_start"], "char_end": token["char_end"],
                              "start_ms": token["start_ms"], "end_ms": token["end_ms"],
                              "raw_clock_ref": f"asr/{selected['label']}/jp.whisper.json#rejected", "timing_valid": False})
        if word_evidence["timing_evidence_status"] == "unusable":
            for parent in parents:
                parent["flags"].append("WORD_CLOCK_REJECTED")
            issues.append(_issue("ASR_WORD_CLOCK_UNUSABLE", "WARNING", "native token clocks conflict with the utterance times; utterance envelopes are kept, nothing is interpolated",
                                 coordinate_issues=len(word_evidence["coordinate_issues"]), recovery_selected=recovery["selected"] if recovery else None))
    if selected["empty"]:
        issues.append(_issue("ASR_EMPTY", "WARNING", "the engine completed but returned no speech; this does not prove that the audio has no speech",
                             attempt=selected["label"]))
    if all(parent["score"] is None for parent in parents):
        issues.append(_issue("ASR_SCORE_UNKNOWN", "INFO", "this run provides no interpretable ASR score; no score is fabricated", parents=len(parents)))
    evidence = {
        "schema_version": SCHEMA_VERSION, "evidence_id": new_id(), "media_id": request["media_id"], "audio_artifact": _as_artifact_ref(request["audio_artifact"]),
        "engine_ref": engine["resource_ref"], "model_ref": model["resource_ref"], "language": language, "parents": parents, "word_times": words,
        "processing_intervals": [{"start_ms": 0, "end_ms": audio_ms}], "activity_intervals": None, "coverage_status": "process_completed_only",
        "raw_refs": _refs(runtime.output_dir, out),
        "extensions": {
            "adapter_version": ADAPTER_VERSION, "selected_attempt": selected["label"], "attempts": [_attempt_summary(a) for a in attempts],
            "native_clock_recovery": recovery, "vad": vad_enabled, "vad_model_ref": vad_resource["resource_ref"] if vad_resource else None,
            "word_timestamps": word_timestamps, "dtw_model": dtw_model or None,
            "timestamp_kind": "whisper_cpp_token_offsets" if word_timestamps else None,
            "word_timing_status": word_evidence["timing_evidence_status"] if word_evidence else ("not_requested" if not word_timestamps else "unavailable"),
            "rejected_word_clocks": rejected, "score_aggregation": "min_lexical_token_p", "asr_empty": selected["empty"],
            "audio_duration_ms": audio_ms, "audio_staging": link_mode, "run_id": request["run_id"], "created_at": utc_now(),
            "legacy_cue_indexes": [cue.index for cue in selected["cues"]],
        },
    }
    return require_record("SpeechEvidence", evidence), issues


def _as_artifact_ref(ref: dict) -> dict:
    """SpeechEvidence.audio_artifact is an ArtifactRef; the ASR input is always a derived, project-managed WAV."""
    if "absolute_path" in ref:
        raise ContractError("SCHEMA_INVALID", "the ASR audio must be a project-managed derivative, not an external file")
    return require_record("ArtifactRef", ref)


# --------------------------------------------------------------------------- on-screen text (MagiaTimeline)

def roi_to_dialog_rect(roi: dict) -> list[float]:
    """v2 ROI {left, top, right, bottom} -> legacy dialogRect [left, right, top, bottom] (plan 6.4 fixes this order)."""
    return [float(roi["left"]), float(roi["right"]), float(roi["top"]), float(roi["bottom"])]


def _recognize_visual(request: dict, runtime: RecognitionRuntime) -> tuple[dict, list[dict]]:
    try:
        import yaml
        import magia_timeline_adapter as legacy
        import magia_utf8_runtime
        from artifact_validation import validate_subtitle
        from subtitle_axis_align import parse_ass
    except ImportError as exc:
        raise ContractError("CAPABILITY_MISSING", f"on-screen text recognition needs a missing Python package: {exc}", details={"capability": "ocr_adapter"}) from exc

    settings = request["engine_settings"].get("ocr", {}) if isinstance(request["engine_settings"].get("ocr"), dict) else {}
    engine = _role(runtime, "ocr_engine")
    executable = _resource_file(engine, ("MagiaTimeline.exe",))
    app_root = executable.parent
    template_config, ass_template = app_root / "config.yml", app_root / "template.asst"
    for required in (template_config, ass_template):
        if not required.is_file():
            raise ContractError("RESOURCE_MISSING", f"MagiaTimeline is incomplete: {required.name} is missing; no engine was started",
                                details={"resource_id": engine["resource_ref"]["resource_id"], "missing": required.name})
    strategy = str(settings.get("strategy", "dtd"))
    preset = str(settings.get("preset", "default"))
    engine_name = str(settings.get("engine", "speculative"))
    if engine_name not in ("speculative", "framewise"):
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"unknown MagiaTimeline engine {engine_name!r}", details={"supported": ["speculative", "framewise"]})
    max_resolution = int(settings.get("max_resolution", 0))
    ocr_enabled = bool(settings.get("ocr", True))
    region = roi_to_dialog_rect(request["roi"])
    video = resolve_file_ref(request["video_artifact"], runtime.project_dir)

    out = runtime.output_dir / "ocr"
    out.mkdir(parents=True, exist_ok=False)
    ascii_root = Path(ensure_ascii_root(runtime.ascii_temp_root))
    try:
        run_executable, compatibility = prepare_utf8_executable(executable, ascii_root / "magia_utf8", magia_utf8_runtime)
    except (OSError, ValueError) as exc:
        raise ContractError("PROCESS_FAILED", f"cannot prepare the UTF-8 compatibility copy of MagiaTimeline: {exc}",
                            details={"resource_id": engine["resource_ref"]["resource_id"]}) from exc
    atomic_write_text(out / "runtime_compatibility.json", render_json(compatibility))
    stage_dir = _staging_dir(runtime, "magia")
    try:
        suffix = video.suffix.lower() if video.suffix.lower() in {".mp4", ".mkv", ".webm", ".avi"} else ".mp4"
        stage_video, stage_output, task_config = stage_dir / f"input{suffix}", stage_dir / "output.ass", stage_dir / "config.yml"
        media_mode = _link_or_copy(video, stage_video)
        shutil.copy2(ass_template, stage_dir / "template.asst")
        schema = app_root / "ConfigSchema.json"
        if schema.is_file():
            shutil.copy2(schema, stage_dir / schema.name)
        paddle_models = app_root / "PaddleOCRModels"
        if paddle_models.is_dir():
            if ocr_enabled:
                shutil.copytree(paddle_models, stage_dir / "PaddleOCRModels")
                model_mode = "isolated_copy"
            else:
                model_mode = legacy.link_or_copy_directory(paddle_models, stage_dir / "PaddleOCRModels")
        else:
            model_mode = "missing"
        data = yaml.safe_load(template_config.read_text(encoding="utf-8-sig"))
        data["source"] = [str(stage_video)]
        data["destination"] = [str(stage_output.with_suffix(""))]
        data["assTemplate"] = str(stage_dir / "template.asst")
        data["strategy"] = strategy
        data["preset"] = preset
        data["engine"] = engine_name
        data["maxResolution"] = max_resolution
        data.setdefault("framewise", {})["debug"] = False
        data.setdefault("speculative", {})["debug"] = False
        data["extraJobs"] = ["ocr"] if ocr_enabled else []
        if ocr_enabled:
            data.setdefault("ocr", {}).update({
                "standaloneOutput": False, "standaloneOutputSuffix": "-ocr.txt", "separator": "", "doPaddle": True,
                "nonMajorBoxSuppressionMaxRatio": 0, "nonMajorBoxSuppressionMinRank": 1, "doTesseract": False, "tesseractLang": "jpn",
            })
        region_applied = legacy.update_strategy_region(data, strategy, preset, region)
        config_text = yaml.safe_dump(data, allow_unicode=False, sort_keys=False, width=120)
        if not config_text.isascii():
            raise ContractError("PATH_INVALID", "the generated MagiaTimeline configuration unexpectedly contains non-ASCII text", details={"stage_dir": str(stage_dir)})
        task_config.write_text(config_text, encoding="ascii", newline="\n")
        shutil.copy2(task_config, out / "config.ascii.yml")

        spec = process_spec(run_executable, [], cwd=stage_dir, log_dir=out / "process", owner_token=runtime.owner_token, capability_id=runtime.capability_id,
                            env_overrides={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, stdin_mode="pipe")
        started_at = time.time()
        result = run_engine(spec, runtime, stdin=b"\n")
        if result["cancelled"] or runtime.cancel.is_set():
            raise ContractError("PROCESS_CANCELLED", "on-screen text recognition was cancelled", retryable=True)
        generated = legacy.locate_output(stage_output, started_at)
        if generated is None:
            outcome = f"exited with code {result['exit_code']}" if result["exit_code"] != 0 else "returned success but created no ASS output"
            raise ContractError("PROCESS_FAILED", f"MagiaTimeline {outcome}", details={"exit_code": result["exit_code"], "candidate": str(out)})
        native = out / "native_output.ass"
        shutil.copy2(generated, native)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    if result["exit_code"] != 0:
        raise ContractError("PROCESS_FAILED", f"MagiaTimeline exited with code {result['exit_code']}; its output is kept as a candidate and not used",
                            details={"exit_code": result["exit_code"], "candidate": str(native)})
    normalized = out / "timeline.utf8.ass"
    shutil.copy2(native, normalized)
    try:
        encoding_details = legacy.normalize_output_encoding(normalized)
    except (UnicodeDecodeError, LookupError) as exc:
        normalized.unlink(missing_ok=True)
        raise ContractError("SUBTITLE_INVALID", f"cannot decode MagiaTimeline output without losing text: {exc}", details={"candidate": str(native)}) from exc
    try:
        cues = parse_ass(normalized).cues
    except (ValueError, OSError) as exc:
        raise ContractError("SUBTITLE_INVALID", f"MagiaTimeline output cannot be parsed: {exc}", details={"candidate": str(native)}) from exc
    if cues:
        validation = validate_subtitle(normalized, "timeline", require_text=ocr_enabled, expected_language="ja" if ocr_enabled else None)
        atomic_write_text(out / "timeline_validation.json", render_json(validation.to_dict()))
        if not validation.valid:
            raise ContractError("SUBTITLE_INVALID", f"MagiaTimeline returned success but produced an invalid ASS: {validation.summary()}",
                                details={"candidate": str(native)})
    else:
        raw_events = sum(1 for line in normalized.read_text(encoding="utf-8-sig").splitlines() if line.strip().lower().startswith("dialogue:"))
        if raw_events:
            raise ContractError("SUBTITLE_INVALID", f"none of the {raw_events} Dialogue lines in the MagiaTimeline output could be parsed",
                                details={"candidate": str(native)})

    events = []
    for ordinal, cue in enumerate(cues):
        placeholder = cue.text.startswith("Subtitle_Dialog_")
        flags = ["ENGINE_PLACEHOLDER_TEXT"] if placeholder else ([] if cue.text.strip() else ["NO_TEXT"])
        events.append({"id": f"ocr-{ordinal + 1:06d}", "ordinal": ordinal, "start_ms": cue.start, "end_ms": cue.end,
                       "text": "" if placeholder else cue.text, "score": None, "score_kind": None, "boxes": None, "frame_refs": [],
                       "boundary_evidence": "engine_interval", "flags": flags})
    text_events = sum(1 for event in events if event["text"].strip())
    issues = [_issue("OCR_SCORE_UNKNOWN", "INFO", "the engine output carries intervals and text only; no OCR score is fabricated", events=len(events))]
    if not text_events:
        issues.append(_issue("OCR_EMPTY", "WARNING", "no on-screen text was detected; the result is an explicit EMPTY, not a blank cue", events=len(events)))
    evidence = {
        "schema_version": SCHEMA_VERSION, "evidence_id": new_id(), "media_id": request["media_id"], "engine_ref": engine["resource_ref"], "model_refs": [],
        "roi": request["roi"], "events": events, "raw_refs": _refs(runtime.output_dir, out), "evidence_level": "interval_only",
        "extensions": {
            "adapter_version": ADAPTER_VERSION, "strategy": strategy, "preset": preset, "engine": engine_name, "max_resolution": max_resolution,
            "ocr_enabled": ocr_enabled, "dialog_rect_legacy_order": region, "roi_applied_to_preset": region_applied,
            "bundled_models": "PaddleOCRModels inside the engine resource" if model_mode != "missing" else None, "model_staging": model_mode,
            "media_staging": media_mode, "runtime_compatibility_mode": compatibility.get("mode"), "output_encoding": encoding_details,
            "time_resolution_ms": ASS_TIME_RESOLUTION_MS, "score_status": "unknown", "frame_pts_status": "unknown",
            "event_count": len(events), "text_event_count": text_events, "visual_empty": text_events == 0,
            "video_sha256": request["video_artifact"]["sha256"], "run_id": request["run_id"], "created_at": utc_now(),
        },
    }
    return require_record("VisualEvidence", evidence), issues


def prepare_utf8_executable(executable: Path, cache_root: Path, module: Any) -> tuple[Path, dict]:
    """The legacy private UTF-8 manifest copy, cached under the ASCII temp root instead of the program directory."""
    return module.prepare_utf8_executable(executable, cache_root)


# --------------------------------------------------------------------------- Interface

def recognize(request: dict, *, runtime: RecognitionRuntime) -> dict:
    """RecognitionRequest -> {speech_evidence, visual_evidence, skip_reasons, issues} (plan 6.8).

    The mode decides what runs: ``combined`` both (on-screen text first, then speech, serially), ``speech_only`` only
    speech, ``visual_only`` only on-screen text, ``supplied_source`` nothing. What does not run is explained in
    ``skip_reasons``; it is never replaced by fabricated empty evidence. The OCR and ASR stages call this with the
    mode narrowed to their own half.
    """
    require_record("RecognitionRequest", request)
    mode = request["recognition_mode"]
    result: dict[str, Any] = {"speech_evidence": None, "visual_evidence": None, "skip_reasons": {}, "issues": []}
    if mode == "supplied_source":
        result["skip_reasons"] = {"ocr": "USER_SOURCE", "asr": "USER_SOURCE"}
        return result
    if mode in ("combined", "visual_only"):
        result["visual_evidence"], issues = _recognize_visual(request, runtime)
        result["issues"] += issues
    else:
        result["skip_reasons"]["ocr"] = "MODE"
    if mode in ("combined", "speech_only"):
        result["speech_evidence"], issues = _recognize_speech(request, runtime)
        result["issues"] += issues
    else:
        result["skip_reasons"]["asr"] = "MODE"
    return result


__all__ = ["ADAPTER_VERSION", "RecognitionRuntime", "recognize", "run_engine", "resolve_file_ref", "whisper_argv", "roi_to_dialog_rect",
           "prepare_utf8_executable"]
