#!/usr/bin/env python3
"""v2 command-line entry (plan 1.1, section 12.1). Development form: ``python -m upgrade.cli``; frozen form: ``roast.exe``.

Every command prints one OperationResult JSON object on stdout (``worker`` is the internal exception: it runs a bundled module),
writes logs to stderr and exits with the fixed codes: 0 ok, 2 arguments/config, 3 resources/media,
4 process failure, 5 data/identity, 6 cancelled, 7 exclusive-lock conflict.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from upgrade.contracts import (
    EXIT_ARGS, EXIT_DATA, EXIT_OK, EXIT_RESOURCE, LICENSE_STATUSES, RESOURCE_KINDS, ContractError, parse_json_text, require_record,
)


def _result(operation: str, *, ok: bool, ids: dict | None = None, artifact_refs: list | None = None,
            error: ContractError | None = None, warnings: list | None = None) -> dict:
    result = {"ok": ok, "operation": operation, "ids": ids or {}, "artifact_refs": artifact_refs or [],
              "error": error.to_dict() if error else None, "warnings": warnings or []}
    return require_record("OperationResult", result)


def _emit(result: dict) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    sys.stdout.flush()


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- commands

def cmd_config_init(args: argparse.Namespace) -> int:
    from upgrade import config as cfg
    from upgrade.resource_catalog import save_declarations
    from upgrade.storage import ensure_ascii_root, init_data_root
    report = init_data_root(args.data_root)
    app_config = cfg.default_app_config(report["data_root"], ascii_temp_root=args.ascii_temp_root)
    ensure_ascii_root(app_config["ascii_temp_root"])
    config_path = Path(args.config) if args.config else Path(report["data_root"]) / cfg.APP_CONFIG_NAME
    if config_path.exists() and not args.force:
        raise ContractError("OUTPUT_EXISTS", "config file already exists; pass --force to replace it", details={"path": str(config_path)})
    cfg.save_app_config(config_path, app_config)
    resources_path = cfg.resolve_in_data_root(app_config, app_config["resources_file"])
    if not resources_path.exists():
        save_declarations(resources_path, {})
    _emit(_result("config.init", ok=True, ids={"config": str(config_path), "data_root": report["data_root"], "resources_file": str(resources_path)}))
    return EXIT_OK


def cmd_config_migrate(args: argparse.Namespace) -> int:
    from upgrade import config as cfg
    from upgrade.storage import atomic_write_text, render_json
    legacy_path = Path(args.legacy)
    try:
        legacy = parse_json_text(legacy_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractError("INPUT_MISSING", f"cannot read legacy config: {exc}", details={"path": str(legacy_path)}) from exc
    migrated = cfg.migrate_legacy_config(legacy, data_root=args.data_root, ascii_temp_root=args.ascii_temp_root)
    config_path = Path(args.config)
    if config_path.exists() and not args.force:
        raise ContractError("OUTPUT_EXISTS", "config file already exists; pass --force to replace it", details={"path": str(config_path)})
    cfg.save_app_config(config_path, migrated["app_config"])
    engine_path = Path(args.engine_settings) if args.engine_settings else config_path.parent / "engine.json"
    cfg.save_engine_settings(engine_path, migrated["engine_settings"])
    report = {key: migrated[key] for key in ("mapped", "unsupported_commands", "suggested_resources", "legacy_extra", "notes")}
    report_path = Path(args.report) if args.report else config_path.parent / "migration-report.json"
    atomic_write_text(report_path, render_json(report))
    warnings = [{"code": "CONFIG_UNSUPPORTED_COMMAND", "key": item["key"]} for item in migrated["unsupported_commands"]]
    _emit(_result("config.migrate", ok=True, ids={"config": str(config_path), "engine_settings": str(engine_path), "report": str(report_path)}, warnings=warnings))
    return EXIT_OK


def _load_config(path: str) -> dict:
    from upgrade.config import load_app_config
    return load_app_config(path)


def cmd_resources_inspect(args: argparse.Namespace) -> int:
    from upgrade.resource_catalog import inspect_resources
    from upgrade.storage import atomic_write_text, render_json
    config = _load_config(args.config)
    report = inspect_resources(config, probe=not args.no_probe)
    report_path = Path(args.report)
    atomic_write_text(report_path, render_json(report))
    _log(f"inspected {len(report['manifests'])} resource(s), {len(report['problems'])} problem(s) -> {report_path}")
    ids = {"report": str(report_path), "resources_file": report["resources_file"], "resource_ids": sorted(report["manifests"])}
    if report["problems"]:
        error = ContractError("RESOURCE_MISSING", "one or more declared resources are missing or changed; see report",
                              details={"problems": report["problems"]})
        _emit(_result("resources.inspect", ok=False, ids=ids, error=error))
        return EXIT_RESOURCE
    _emit(_result("resources.inspect", ok=True, ids=ids, warnings=[report["hash_cache_note"]]))
    return EXIT_OK


def cmd_resources_register(args: argparse.Namespace) -> int:
    from upgrade.resource_catalog import declare_resource, register_resource
    config = _load_config(args.config)
    declaration = declare_resource(args.kind, args.path, files=args.file, upstream_url=args.upstream_url,
                                   upstream_revision=args.upstream_revision, license_status=args.license_status,
                                   version_args=args.version_arg if args.version_arg is not None else None)
    saved = register_resource(config, args.id, declaration)
    _emit(_result("resources.register", ok=True, ids={"resource_id": args.id, "content_sha256": saved["expected_content_sha256"]}))
    return EXIT_OK


def cmd_resources_list(args: argparse.Namespace) -> int:
    """Declared resources with their state (no version probes): the window's "资源与工具" list."""
    from upgrade.resource_catalog import inspect_resources, load_declarations, resources_file_path
    config = _load_config(args.config)
    declarations = load_declarations(resources_file_path(config))
    report = inspect_resources(config, declarations=declarations, probe=False)
    problems = {(problem.get("details") or {}).get("resource_id"): problem for problem in report["problems"]}
    rows = []
    for resource_id, declaration in sorted(declarations.items()):
        problem = problems.get(resource_id)
        manifest = report["manifests"].get(resource_id)
        state = problem["code"] if problem else ("ok" if manifest and manifest["extensions"].get("identity") == "verified" else "first_inspection")
        rows.append({"resource_id": resource_id, "kind": declaration["kind"], "absolute_path": declaration["absolute_path"],
                     "directory": bool(declaration["files"]), "state": state, "message": problem["message"] if problem else None})
    _emit(_result("resources.list", ok=True, ids={"resources_file": report["resources_file"], "resources": rows}))
    return EXIT_OK


def cmd_resources_rebind(args: argparse.Namespace) -> int:
    from upgrade.resource_catalog import rebind_resource
    config = _load_config(args.config)
    result = rebind_resource(config, args.id, args.path)
    _emit(_result("resources.rebind", ok=True, ids=result))
    return EXIT_OK


# --------------------------------------------------------------------------- queue (W02)

def _data_root_config(data_root: str) -> dict:
    from upgrade.config import APP_CONFIG_NAME, load_app_config
    return load_app_config(Path(data_root) / APP_CONFIG_NAME)


def _engine_settings_for(config: dict) -> dict:
    from upgrade.config import ENGINE_SETTINGS_NAME, load_engine_settings, resolve_in_data_root
    return load_engine_settings(resolve_in_data_root(config, ENGINE_SETTINGS_NAME))


def _resource_refs_for(config: dict) -> list[dict]:
    from upgrade.resource_catalog import inspect_resources, resolve_resource
    report = inspect_resources(config, probe=False)
    return [resolve_resource(resource_id, report)["resource_ref"] for resource_id in sorted(report["manifests"])
            if report["manifests"][resource_id].get("extensions", {}).get("identity") != "changed"]


def _parse_stream(value: str | None) -> int | None:
    if value is None or value == "none":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ContractError("NEEDS_SELECTION", f"stream index must be an integer or 'none', got {value!r}") from exc


def _dictionary_for_request(config: dict, engine: dict, *, accept: bool) -> tuple[dict, list]:
    """Plan 8.5 pre-check: freeze the dictionary revision for this request and settle the acceptance of unsupported entries."""
    from upgrade.dictionary import prepare_for_request
    prepared = prepare_for_request(config["data_root"], engine, accept_warnings=accept)
    warnings = [{"code": "DICTIONARY_UNSUPPORTED_ACCEPTED", "count": len(prepared["unsupported"]), "entries": prepared["unsupported"]}] if prepared["unsupported"] else []
    warnings += [{"code": "DICTIONARY_CONFLICT", **item} for item in prepared["warnings"]]
    return prepared, warnings


def _submit_job(config: dict, *, mode: str, timing_mode: str | None, video_arg: str | None, url: str | None, project: str | None,
                source_revision: str | None, video_stream_arg: str | None, audio_stream_arg: str | None, copy_original: bool, accept: bool,
                display_name: str | None) -> tuple[dict, dict, dict, list]:
    """Build and persist one request with the configuration as it is now (``submit`` and ``queue copy``)."""
    from upgrade.runner import build_job_request, project_dir, submit
    engine = _engine_settings_for(config)
    source_media_id = None
    if mode == "supplied_source":
        if not (project and source_revision):
            raise ContractError("NEEDS_SELECTION", "supplied_source needs --project ID and --source-revision ID")
    elif source_revision:
        raise ContractError("NEEDS_SELECTION", "--source-revision is only valid with --mode supplied_source")
    if timing_mode and mode != "combined":
        raise ContractError("NEEDS_SELECTION", "--timing-mode only applies to --mode combined (speech_only=speech, visual_only=screen are fixed)")
    video_stream, audio_stream = _parse_stream(video_stream_arg), _parse_stream(audio_stream_arg)
    prepared, warnings = _dictionary_for_request(config, engine, accept=accept)
    if video_arg:
        video = Path(video_arg)
        if not video.is_file():
            raise ContractError("INPUT_MISSING", f"video file not found: {video}", details={"path": str(video)})
        input_kind, input_value, default_name = "local_video", str(video.resolve()), video.stem
        from upgrade.stages import preflight_streams
        checked = preflight_streams(config, {"config_snapshot": {"engine_settings": engine}, "resource_refs": []}, video.resolve(), mode=mode,
                                    video_stream=video_stream, audio_stream=audio_stream, audio_none=audio_stream_arg == "none")
        video_stream, audio_stream = checked["video_stream"], checked["audio_stream"]
        if checked["warning"]:
            warnings.append(checked["warning"])
    elif url:
        input_kind, input_value, default_name = "url", url, url
    else:
        if mode != "supplied_source":
            raise ContractError("NEEDS_SELECTION", "give --video FILE or --url URL")
        from upgrade.source import load_source_revision
        loaded = load_source_revision(project_dir(config["data_root"], project), source_revision)
        source_media_id = loaded["record"]["media_id"]             # the media the source revision is bound to (plan 10.1: SOURCE reads the revision)
        input_kind, input_value, default_name = "local_video", f"supplied-source:{source_revision}", project
    request = build_job_request(
        config, engine, input_kind=input_kind, input_value=input_value, recognition_mode=mode, timing_mode=timing_mode,
        project_id=project, video_stream_index=video_stream, audio_stream_index=audio_stream,
        source_revision_id=source_revision, source_media_id=source_media_id, dictionary_revision_id=prepared["revision_id"],
        resource_refs=_resource_refs_for(config), accepted_dictionary_warnings=prepared["accepted_ids"],
    )
    if audio_stream_arg == "none":
        request["extensions"]["audio_stream"] = "none"
    if copy_original:
        request["extensions"]["copy_original"] = True
    envelope = submit(config["data_root"], request, display_name=display_name or default_name)
    return request, envelope, prepared, warnings


def cmd_submit(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    request, envelope, prepared, warnings = _submit_job(
        config, mode=args.mode, timing_mode=args.timing_mode, video_arg=args.video, url=args.url, project=args.project, source_revision=args.source_revision,
        video_stream_arg=args.video_stream, audio_stream_arg=args.audio_stream, copy_original=args.copy_original,
        accept=args.accept_dictionary_warnings, display_name=args.display_name)
    _emit(_result("submit", ok=True, ids={"project_id": request["project_id"], "request_id": request["request_id"], "status": envelope["status"],
                                           "dictionary_revision_id": prepared["revision_id"],
                                           "duplicate": envelope["request"]["request_id"] == request["request_id"] and envelope["created_at"] != request["created_at"]},
                  warnings=warnings))
    return EXIT_OK


def cmd_queue_run(args: argparse.Namespace) -> int:
    from upgrade.runner import QueueRunner
    from upgrade.stage_registry import default_registry
    config = _data_root_config(args.data_root)
    runner = QueueRunner(config["data_root"], config, _engine_settings_for(config), default_registry(), event_sink=lambda e: _log(json.dumps(e, ensure_ascii=False)))
    summary = runner.run_queue(continue_on_error=None if args.continue_on_error is None else args.continue_on_error)
    _emit(_result("queue.run", ok=True, ids=summary))
    return EXIT_OK


def cmd_queue_cancel(args: argparse.Namespace) -> int:
    from upgrade.runner import cancel
    config = _data_root_config(args.data_root)
    envelope = cancel(config["data_root"], args.request)
    _emit(_result("queue.cancel", ok=True, ids={"request_id": args.request, "status": envelope["status"]}))
    return EXIT_OK


def cmd_queue_stop(args: argparse.Namespace) -> int:
    """"停止当前" (plan 3.2): the running request is stopped (resumable) and the queue pauses; nothing else is touched."""
    from upgrade.runner import request_stop
    config = _data_root_config(args.data_root)
    outcome = request_stop(config["data_root"])
    warnings = [] if outcome["requested"] else [{"code": "EXECUTOR_NOT_RUNNING", "message": "队列没有在运行，没有可停止的任务"}]
    _emit(_result("queue.stop", ok=True, ids=outcome, warnings=warnings))
    return EXIT_OK


def cmd_queue_list(args: argparse.Namespace) -> int:
    from upgrade.runner import queue_rows
    config = _data_root_config(args.data_root)
    _emit(_result("queue.list", ok=True, ids=queue_rows(config["data_root"])))
    return EXIT_OK


def cmd_queue_copy(args: argparse.Namespace) -> int:
    """"复制为新任务" (plan 3.1): same input, mode and streams, but the configuration, dictionary and resources as they are now."""
    from upgrade.runner import load_envelope, load_project
    config = _data_root_config(args.data_root)
    old = load_envelope(config["data_root"], args.request)["request"]
    extensions = old.get("extensions") or {}
    mode, job_input = old["recognition_mode"], old["input"]
    video = url = None
    if mode != "supplied_source":
        if job_input["kind"] == "url":
            url = job_input["value"]
        else:
            video = job_input["value"]
    audio = "none" if extensions.get("audio_stream") == "none" else (None if old["audio_stream_index"] is None else str(old["audio_stream_index"]))
    video_stream = None if old["video_stream_index"] is None else str(old["video_stream_index"])
    display = args.display_name or load_project(config["data_root"], old["project_id"]).get("display_name")
    request, envelope, prepared, warnings = _submit_job(
        config, mode=mode, timing_mode=old["timing_mode"] if mode == "combined" else None, video_arg=video, url=url, project=old["project_id"],
        source_revision=old["source_revision_id"], video_stream_arg=video_stream, audio_stream_arg=audio, copy_original=bool(extensions.get("copy_original")),
        accept=args.accept_dictionary_warnings, display_name=display)
    previous, current = old["config_snapshot"]["config_sha256"], request["config_snapshot"]["config_sha256"]
    _emit(_result("queue.copy", ok=True, ids={"copied_from": args.request, "project_id": request["project_id"], "request_id": request["request_id"],
                                               "status": envelope["status"], "config_changed": previous != current,
                                               "dictionary_revision_id": prepared["revision_id"]}, warnings=warnings))
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    from upgrade.runner import resume
    config = _data_root_config(args.data_root)
    envelope = resume(config["data_root"], args.run)
    _emit(_result("resume", ok=True, ids={"run_id": args.run, "request_id": envelope["request"]["request_id"], "status": envelope["status"]}))
    return EXIT_OK


def cmd_inbox_intake(args: argparse.Namespace) -> int:
    from upgrade.inbox import intake
    from upgrade.runner import build_job_request
    config = _load_config(args.config)
    engine = _engine_settings_for(config)
    refs = _resource_refs_for(config)
    prepared, _ = _dictionary_for_request(config, engine, accept=False)   # legacy inbox files carry no acceptance; unaccepted entries stop the intake

    def factory(parsed: dict, source: Path) -> dict:
        url = str(parsed.get("url", "")).strip()
        video = str(parsed.get("video_file", parsed.get("video", ""))).strip()
        if not url and not video:
            raise ContractError("INPUT_MISSING", "legacy request has neither url nor video_file", details={"source": str(source)})
        mode = str(parsed.get("mode", "")).strip() or config["defaults"]["recognition_mode"]
        timing = str(parsed.get("timing_mode", "")).strip() or None
        return build_job_request(config, engine, input_kind="url" if url else "local_video", input_value=url or video, recognition_mode=mode,
                                 timing_mode=timing if mode == "combined" else None, dictionary_revision_id=prepared["revision_id"], resource_refs=refs,
                                 accepted_dictionary_warnings=prepared["accepted_ids"])

    rows = intake(config["data_root"], args.inbox, factory, event_sink=lambda e: _log(json.dumps(e, ensure_ascii=False)))
    failed = [row for row in rows if row["action"] == "failed"]
    _emit(_result("inbox.intake", ok=not failed, ids={"rows": rows},
                  error=ContractError("SCHEMA_INVALID", f"{len(failed)} inbox file(s) could not be ingested", details={"failed": failed}) if failed else None))
    return EXIT_OK if not failed else EXIT_DATA


# --------------------------------------------------------------------------- media (W08: new task with a supplied source, open / relocate the original)

def cmd_media_register(args: argparse.Namespace) -> int:
    """Register a local video as media of a (new) project, for "已有日文源字幕" tasks. Streams are settled before anything is created."""
    from upgrade import media
    from upgrade.contracts import new_id
    from upgrade.runner import create_project, project_dir, update_project
    from upgrade.stages import preflight_streams, resolve_roles
    config = _data_root_config(args.data_root)
    if bool(args.project) == bool(args.display_name):
        raise ContractError("NEEDS_SELECTION", "give --project ID (existing project) or --display-name NAME (new project)")
    video = Path(args.video)
    if not video.is_file():
        raise ContractError("INPUT_MISSING", f"video file not found: {video}", details={"path": str(video)})
    request_like = {"config_snapshot": {"engine_settings": _engine_settings_for(config)}, "resource_refs": []}
    roles = resolve_roles(config, request_like, ["ffprobe"])
    video_stream, audio_stream = _parse_stream(args.video_stream), _parse_stream(args.audio_stream)
    checked = preflight_streams(config, request_like, video.resolve(), mode="supplied_source", video_stream=video_stream, audio_stream=audio_stream,
                                audio_none=args.audio_stream == "none")
    project_id = args.project or create_project(config["data_root"], args.display_name)["project_id"]
    project = project_dir(config["data_root"], project_id)
    if not (project / "project.json").is_file():
        raise ContractError("INPUT_MISSING", f"project {project_id} does not exist", details={"project_id": project_id})
    runtime = media.MediaRuntime(project_dir=project, ffprobe=roles["ffprobe"], log_dir=project / "logs" / f"media-register-{new_id()}", owner_token="media-register")
    extensions = {"audio_stream": "none"} if args.audio_stream == "none" else {}
    if args.copy_original:
        extensions["copy_original"] = True
    record = media.probe_media({"input": {"kind": "local_video", "value": str(video.resolve()), "source_media_id": None},
                                "selected_video_stream": checked["video_stream"], "selected_audio_stream": checked["audio_stream"],
                                "project_id": project_id, "extensions": extensions}, runtime=runtime)
    update_project(config["data_root"], project_id, lambda item: item["media_ids"].append(record["media_id"]) if record["media_id"] not in item["media_ids"] else None)
    _emit(_result("media.register", ok=True, ids={"project_id": project_id, "media_id": record["media_id"], "video_stream": record["selected_video_stream"],
                                                   "audio_stream": record["selected_audio_stream"], "clock_verified": record["clock"]["verified"],
                                                   "duration_ms": record["duration_ms"]}))
    return EXIT_OK


def cmd_media_locate(args: argparse.Namespace) -> int:
    """"打开原视频": the bound original if it is still there with the same content; otherwise INPUT_MISSING / MEDIA_CHANGED."""
    from upgrade import media
    from upgrade.runner import project_dir
    config = _data_root_config(args.data_root)
    project = project_dir(config["data_root"], args.project)
    ref = media.verify_source(media.load_media_record(project, args.media), project)
    _emit(_result("media.locate", ok=True, ids={"media_id": args.media, "path": ref["absolute_path"], "sha256": ref["sha256"]}))
    return EXIT_OK


def cmd_media_relocate(args: argparse.Namespace) -> int:
    from upgrade import media
    from upgrade.runner import project_dir
    config = _data_root_config(args.data_root)
    ref = media.relocate_media(project_dir(config["data_root"], args.project), args.media, Path(args.path).resolve())
    _emit(_result("media.relocate", ok=True, ids={"media_id": args.media, "path": ref["absolute_path"], "sha256": ref["sha256"], "rebound": True}))
    return EXIT_OK


# --------------------------------------------------------------------------- human revisions (W05, plan 9 / 12.1)

def _revision_summary(config: dict, project_id: str, revision_id: str) -> tuple[dict, list, list]:
    """ids / artifact_refs / warnings describing one committed revision: file refs are project-relative, nothing is re-serialized."""
    from upgrade import revisions
    from upgrade.runner import project_dir
    from upgrade.contracts import load_record
    resolved = revisions.resolve_revision(config["data_root"], project_id, revision_id)
    manifest = resolved["manifest"]
    project = project_dir(config["data_root"], project_id)
    fmt, ref = next(iter(sorted(resolved["subtitle_files"].items())))
    report = load_record("QualityReport", project / resolved["directory"] / revisions.QUALITY_NAME)
    counts = {severity: sum(1 for issue in report["issues"] if issue["severity"] == severity) for severity in ("ERROR", "WARNING", "INFO")}
    confirmation = resolved["confirmations"][fmt]
    ids = {"project_id": project_id, "revision_id": revision_id, "kind": resolved["kind"], "media_id": resolved["media_id"],
           "base_revision_id": manifest.get("base_revision_id", manifest.get("parent_revision_id")), "format": fmt,
           "directory": resolved["directory"], "subtitle_sha256": ref["sha256"], "provenance_status": manifest["provenance_status"],
           "structural_status": report["structural_status"], "human_status": confirmation["human_status"], "confirmed_scopes": confirmation["confirmed_scopes"],
           "issue_counts": counts,
           "report_files": {name: f"{resolved['directory']}/{name}" for name in ("review.html", "review.csv", "quality_scope.json")
                            if (project / resolved["directory"] / name).is_file()}}
    warnings = list(resolved["notices"])
    listed = [issue for issue in report["issues"] if issue["severity"] == "WARNING"]
    warnings += [{"code": issue["rule_id"], "severity": issue["severity"], "message": issue["message"], "cue_ids": issue["cue_ids"]} for issue in listed[:20]]
    if len(listed) > 20:
        warnings.append({"code": "MORE_WARNINGS", "count": len(listed) - 20, "message": "更多提示见该版本目录下的 quality.json"})
    return ids, [ref], warnings


def cmd_review_create(args: argparse.Namespace) -> int:
    from upgrade import revisions
    from upgrade.runner import project_dir
    from upgrade.storage import safe_join
    config = _data_root_config(args.data_root)
    session = revisions.create_session(config["data_root"], args.project, args.revision, args.format)
    edit_file = safe_join(project_dir(config["data_root"], args.project), session["edit_relative_path"])
    _emit(_result("review.create", ok=True, ids={
        "project_id": args.project, "session_id": session["session_id"], "base_revision_id": session["base_revision_id"], "media_id": session["media_id"],
        "format": session["format"], "base_subtitle_sha256": session["base_subtitle_sha256"], "edit_relative_path": session["edit_relative_path"],
        "edit_file": str(edit_file)}))
    return EXIT_OK


def _import_revision(args: argparse.Namespace, kind: str) -> tuple[dict, dict, dict, list, list]:
    from upgrade import revisions
    config = _data_root_config(args.data_root)
    manifest = revisions.import_revision(config["data_root"], args.project, args.media, Path(args.file), kind, base_revision_id=args.base,
                                         session_id=args.session, encoding=args.encoding, rule_config=config["quality"])
    ids, refs, warnings = _revision_summary(config, args.project, manifest["revision_id"])
    ids["session_id"] = args.session
    ids["encoding"] = manifest["encoding"] if kind == "review" else manifest["extensions"]["encoding"]
    return config, manifest, ids, refs, warnings


def cmd_review_import(args: argparse.Namespace) -> int:
    _, _, ids, refs, warnings = _import_revision(args, "review")
    _emit(_result("review.import", ok=True, ids=ids, artifact_refs=refs, warnings=warnings))
    return EXIT_OK


def cmd_source_import(args: argparse.Namespace) -> int:
    from upgrade.contracts import load_record
    from upgrade.runner import project_dir
    from upgrade.source import SEGMENTS_NAME
    config, manifest, ids, refs, warnings = _import_revision(args, "source")
    segment_set = load_record("SegmentSet", project_dir(config["data_root"], args.project) / ids["directory"] / SEGMENTS_NAME)
    ids["segments"] = len(segment_set["segments"])
    ids["omitted_events"] = len(segment_set["extensions"]["omitted_events"])
    # A new machine translation is never started from here: it is the user's explicit next command (plan 3.2 / 9.4).
    ids["next_command"] = ["submit", "--mode", "supplied_source", "--project", args.project, "--source-revision", manifest["revision_id"]]
    _emit(_result("source.import", ok=True, ids=ids, artifact_refs=refs, warnings=warnings))
    return EXIT_OK


def cmd_review_attest(args: argparse.Namespace) -> int:
    """User entry only (plan 12.1): nothing in the pipeline calls this to produce a human confirmation."""
    from upgrade import revisions
    config = _data_root_config(args.data_root)
    scopes = [item.strip() for item in args.scopes.split(",")]
    record = revisions.attest_revision(config["data_root"], args.project, args.revision, scopes, args.note or "", fmt=args.format)
    resolved = revisions.resolve_revision(config["data_root"], args.project, args.revision)
    fmt = next(fmt for fmt, ref in resolved["subtitle_files"].items() if ref["sha256"] == record["subtitle_sha256"] and (args.format in (None, fmt)))
    confirmation = resolved["confirmations"][fmt]
    _emit(_result("review.attest", ok=True, ids={
        "project_id": args.project, "revision_id": args.revision, "attestation_id": record["attestation_id"], "media_id": record["media_id"], "format": fmt,
        "subtitle_sha256": record["subtitle_sha256"], "scopes": record["scopes"], "human_status": confirmation["human_status"],
        "confirmed_scopes": confirmation["confirmed_scopes"]}, warnings=list(resolved["notices"])))
    return EXIT_OK


def _boundary_times(report: dict, issue_id: str) -> list[int]:
    issue = next((item for item in report["issues"] if item["issue_id"] == issue_id), None)
    if issue is None:
        raise ContractError("NEEDS_SELECTION", "this revision's quality report has no issue with that id", details={"issue_id": issue_id})
    observed = issue["observed"] if isinstance(issue["observed"], dict) else {}
    times = list(observed.get("boundary_times_ms") or [value for value in (issue["start_ms"], issue["end_ms"]) if value is not None])
    if not times:
        raise ContractError("NEEDS_SELECTION", "this issue names no time; pass --at-ms", details={"issue_id": issue_id, "rule_id": issue["rule_id"]})
    return times


def cmd_review_frames(args: argparse.Namespace) -> int:
    """Boundary frames on demand (plan 11.3): the frames around a time, labelled with the decoded frame's real PTS. Reads the media only."""
    from upgrade import media, revisions
    from upgrade.contracts import load_record, new_id
    from upgrade.runner import project_dir
    from upgrade.stages import resolve_roles
    config = _data_root_config(args.data_root)
    resolved = revisions.resolve_revision(config["data_root"], args.project, args.revision)
    project = project_dir(config["data_root"], args.project)
    times: list[int] = []
    if args.issue:
        times += _boundary_times(load_record("QualityReport", project / resolved["directory"] / revisions.QUALITY_NAME), args.issue)
    if args.at_ms:
        try:
            times += [int(value) for value in args.at_ms.split(",") if value.strip()]
        except ValueError as exc:
            raise ContractError("NEEDS_SELECTION", f"--at-ms must be integer milliseconds separated by commas, got {args.at_ms!r}") from exc
    if not times or any(value < 0 for value in times):
        raise ContractError("NEEDS_SELECTION", "pass --issue or --at-ms with non-negative milliseconds")
    record = media.load_media_record(project, resolved["media_id"])
    roles = resolve_roles(config, {"config_snapshot": {"engine_settings": _engine_settings_for(config)}, "resource_refs": []}, ["ffprobe", "ffmpeg"])
    runtime = media.MediaRuntime(project_dir=project, ffprobe=roles["ffprobe"], ffmpeg=roles["ffmpeg"], log_dir=project / "logs" / f"frames-{new_id()}", owner_token="review-frames")
    document = media.export_boundary_frames(record, times, Path(args.destination), runtime=runtime)
    _emit(_result("review.frames", ok=True, ids={
        "project_id": args.project, "revision_id": args.revision, "media_id": resolved["media_id"], "destination": str(Path(args.destination)),
        "times_ms": document["times_ms"], "frames": [{key: frame[key] for key in ("file", "pts", "time_base_num", "time_base_den", "frame_ms", "purposes")}
                                                     for frame in document["frames"]]}))
    return EXIT_OK


# --------------------------------------------------------------------------- exports (W07, plan 3.2 / 7.5 / 9.5 / 12.1)

def _audio_choice(value: str | None, record: dict | None) -> int | None:
    if record is None:
        return None
    if value is None:
        return record["selected_audio_stream"]                 # the project's selected audio stream (12.1 default)
    if value == "none":
        return None
    index = _parse_stream(value)
    streams = {item["index"]: item for item in record["streams"]}
    if index not in streams or streams[index]["codec_type"] != "audio":
        raise ContractError("NEEDS_SELECTION", f"stream {index} is not an audio stream of this media", details={"audio_streams": sorted(
            item["index"] for item in record["streams"] if item["codec_type"] == "audio")})
    return index


def cmd_export(args: argparse.Namespace) -> int:
    from upgrade import export, media, revisions
    from upgrade.contracts import SCHEMA_VERSION, load_record, new_id, utc_now
    from upgrade.runner import project_dir
    from upgrade.stages import resolve_roles
    config = _data_root_config(args.data_root)
    resolved = revisions.resolve_revision(config["data_root"], args.project, args.revision)
    project = project_dir(config["data_root"], args.project)
    report = load_record("QualityReport", project / resolved["directory"] / revisions.QUALITY_NAME)
    media_id = args.media or resolved["media_id"]
    record = media.load_media_record(project, media_id) if (args.kind != "subtitles" or args.include_media or args.media) else None
    export_id = new_id()
    runtime = None
    if args.kind != "subtitles":
        roles = resolve_roles(config, {"config_snapshot": {"engine_settings": _engine_settings_for(config)}, "resource_refs": []}, ["ffprobe", "ffmpeg"])
        runtime = media.MediaRuntime(project_dir=project, ffprobe=roles["ffprobe"], ffmpeg=roles["ffmpeg"], log_dir=project / "logs" / f"export-{export_id}", owner_token="export")
    counts = {severity: sum(1 for issue in report["issues"] if issue["severity"] == severity) for severity in ("ERROR", "WARNING", "INFO")}
    request = {"schema_version": SCHEMA_VERSION, "export_id": export_id, "project_id": args.project, "revision_id": args.revision, "media_id": media_id, "kind": args.kind,
               "selected_audio_stream": _audio_choice(args.audio_stream, record) if args.kind != "subtitles" else None,
               "profile_id": export.profile_for(args.kind, args.profile), "destination_root": str(Path(args.destination).absolute()), "include_media": bool(args.include_media),
               "quality_acknowledgement": {"structural_status": report["structural_status"], "issue_counts": counts,
                                           "human_status": {fmt: item["human_status"] for fmt, item in resolved["confirmations"].items()},
                                           "note": "soft warnings do not block an export; the manifest keeps the human status of every file"},
               "created_at": utc_now()}
    manifest = export.run_export({
        "request": request, "data_root": config["data_root"], "project_dir": project, "revision": resolved, "quality_report": report, "media_record": record,
        "runtime": runtime, "format": args.format, "convert_to": args.convert_to, "accept_omitted_drawings": bool(args.accept_omitted_drawings),
        "font_dirs": [Path(item) for item in args.font_dir or []] + export.system_font_dirs(), "ascii_temp_root": config["ascii_temp_root"]})
    files = manifest["quality_snapshot"]["files"]
    warnings = list(resolved["notices"]) + [{"code": issue["rule_id"], "severity": issue["severity"], "message": issue["message"]} for issue in manifest["quality_snapshot"]["export_issues"]]
    warnings += [{"code": "NOT_REVIEWED", "file": item["file"], "human_status": item["human_status"], "message": "这个文件还没有经过完整的人工确认"}
                 for item in files if item.get("human_status") in ("UNREVIEWED", "PARTIAL")]
    _emit(_result("export", ok=True, ids={"project_id": args.project, "export_id": export_id, "revision_id": args.revision, "media_id": media_id, "kind": args.kind,
                                          "profile_id": request["profile_id"], "destination": request["destination_root"], "files": files,
                                          "validation": manifest["validation"]}, artifact_refs=manifest["output_refs"], warnings=warnings))
    return EXIT_OK


# --------------------------------------------------------------------------- listing and migration (W08, plan 3.2 / 9.6)

def cmd_revisions_list(args: argparse.Namespace) -> int:
    """Read-only view of one project's committed revisions for the window (and for people): kind, formats, hash prefix, status."""
    from upgrade import revisions
    from upgrade.contracts import load_record
    from upgrade.runner import load_project, project_dir
    config = _data_root_config(args.data_root)
    record = load_project(config["data_root"], args.project)
    project = project_dir(config["data_root"], args.project)
    rows, warnings = [], []
    for key, kind in (("machine_revision_ids", "machine"), ("source_revision_ids", "source"), ("review_revision_ids", "review")):
        for revision_id in record[key]:
            try:
                resolved = revisions.resolve_revision(config["data_root"], args.project, revision_id)
                report = load_record("QualityReport", project / resolved["directory"] / revisions.QUALITY_NAME)
            except ContractError as exc:
                rows.append({"revision_id": revision_id, "kind": kind, "error": exc.code, "message": exc.message})
                continue
            manifest = resolved["manifest"]
            rows.append({
                "revision_id": revision_id, "kind": kind, "origin": "machine" if kind == "machine" else manifest.get("origin", "human"), "media_id": resolved["media_id"],
                "created_at": manifest["created_at"], "directory": resolved["directory"], "base_revision_id": manifest.get("base_revision_id", manifest.get("parent_revision_id")),
                "source_revision_id": manifest.get("source_revision_id"), "structural_status": report["structural_status"],
                "issue_counts": {severity: sum(1 for issue in report["issues"] if issue["severity"] == severity) for severity in ("ERROR", "WARNING", "INFO")},
                "formats": {fmt: {"path": ref["path"], "sha256_12": ref["sha256"][:12], "human_status": resolved["confirmations"][fmt]["human_status"],
                                  "confirmed_scopes": resolved["confirmations"][fmt]["confirmed_scopes"]} for fmt, ref in sorted(resolved["subtitle_files"].items())},
                "report_html": f"{resolved['directory']}/review.html" if (project / resolved["directory"] / "review.html").is_file() else None,
                "notices": resolved["notices"]})
    _emit(_result("revisions.list", ok=True, ids={"project_id": args.project, "display_name": record["display_name"], "media_ids": record["media_ids"],
                                                  "export_ids": record["export_ids"], "legacy_import": (record.get("extensions") or {}).get("legacy_import"),
                                                  "revisions": rows}, warnings=warnings))
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    from upgrade import media, migrate
    from upgrade.contracts import new_id
    from upgrade.stages import resolve_roles
    config = _data_root_config(args.data_root)
    warnings: list[dict] = []
    register = None
    if not args.no_media:
        roles = resolve_roles(config, {"config_snapshot": {"engine_settings": _engine_settings_for(config)}, "resource_refs": []}, ["ffprobe"])

        def register(project_id: str, project: Path, video: Path) -> str | None:
            runtime = media.MediaRuntime(project_dir=project, ffprobe=roles["ffprobe"], log_dir=project / "logs" / f"migrate-{new_id()}", owner_token="migrate")
            try:
                return media.probe_media({"input": {"kind": "local_video", "value": str(video), "source_media_id": None}, "selected_video_stream": None,
                                          "selected_audio_stream": None, "project_id": project_id, "extensions": {}}, runtime=runtime)["media_id"]
            except ContractError as exc:      # the media stays a reference; the migration itself does not depend on it
                warnings.append({"code": exc.code, "message": "原视频没有登记为媒体（" + exc.message + "）；需要时请在新项目里另行登记"})
                return None
    result = migrate.migrate_job(config["data_root"], Path(args.legacy_job), display_name=args.display_name, register_media=register)
    warnings.append({"code": "LEGACY_NOT_REVIEWED", "message": "旧任务的字幕只作为未审快照列出，旧的“完成”不等于新版完成；有手改的文件请用 review import 登记"})
    _emit(_result("migrate", ok=True, ids={"project_id": result["project_id"], "media_id": result["media_id"], "legacy_manifest": "legacy/source-manifest.json",
                                           **migrate.legacy_summary(result["manifest"])}, warnings=warnings))
    return EXIT_OK


# --------------------------------------------------------------------------- parser

class _Parser(argparse.ArgumentParser):
    """Argument errors are answered like every other refusal: one OperationResult on stdout and exit code 2 (plan 12.1)."""

    def error(self, message: str):
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"{self.prog}: {message}", details={"usage": " ".join(self.format_usage().split())})


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="roast", description="字幕烤肉助手 v2 命令行（计划 1.1 第 12.1 节）。每条命令在 stdout 输出一份 OperationResult JSON。")
    sub = parser.add_subparsers(dest="group", required=True)

    config_parser = sub.add_parser("config", help="配置文件与数据根")
    config_sub = config_parser.add_subparsers(dest="command", required=True)
    init = config_sub.add_parser("init", help="创建 DATA_ROOT 布局并写出默认 AppConfig（首次显式选择数据根）")
    init.add_argument("--data-root", required=True)
    init.add_argument("--config", default=None, help="AppConfig JSON 输出路径；默认 <data-root>/settings/app.json")
    init.add_argument("--ascii-temp-root", default=None)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_config_init)
    migrate = config_sub.add_parser("migrate", help="把旧 roast_pipeline.local.json 迁移为 v2 AppConfig + engine settings；旧模板只记录不执行")
    migrate.add_argument("--legacy", required=True)
    migrate.add_argument("--data-root", required=True)
    migrate.add_argument("--config", required=True)
    migrate.add_argument("--engine-settings", default=None)
    migrate.add_argument("--report", default=None)
    migrate.add_argument("--ascii-temp-root", default=None)
    migrate.add_argument("--force", action="store_true")
    migrate.set_defaults(func=cmd_config_migrate)

    resources_parser = sub.add_parser("resources", help="外部工具/模型资源登记与检查")
    resources_sub = resources_parser.add_subparsers(dest="command", required=True)
    inspect = resources_sub.add_parser("inspect", help="检查 resources.local.json 中的全部资源并写报告；只做版本/帮助探针")
    inspect.add_argument("--config", required=True)
    inspect.add_argument("--report", required=True)
    inspect.add_argument("--no-probe", action="store_true", help="不运行版本/帮助探针，只做存在性与哈希")
    inspect.set_defaults(func=cmd_resources_inspect)
    register = resources_sub.add_parser("register", help="登记一个资源的逻辑 ID 与本机路径，并冻结其内容身份")
    register.add_argument("--config", required=True)
    register.add_argument("--id", required=True)
    register.add_argument("--kind", required=True, choices=RESOURCE_KINDS)
    register.add_argument("--path", required=True)
    register.add_argument("--file", action="append", default=None, help="目录型资源的相对文件路径，可重复")
    register.add_argument("--upstream-url", default=None)
    register.add_argument("--upstream-revision", default=None)
    register.add_argument("--license-status", default="unresolved", choices=LICENSE_STATUSES)
    register.add_argument("--version-arg", action="append", default=None, help="版本/帮助探针参数，可重复；不传用按类型默认")
    register.set_defaults(func=cmd_resources_register)
    listing = resources_sub.add_parser("list", help="列出已登记资源及其状态（不运行版本探针）")
    listing.add_argument("--config", required=True)
    listing.set_defaults(func=cmd_resources_list)

    rebind = resources_sub.add_parser("rebind", help="资源移动后只更新本机路径；内容身份必须一致")
    rebind.add_argument("--config", required=True)
    rebind.add_argument("--id", required=True)
    rebind.add_argument("--path", required=True)
    rebind.set_defaults(func=cmd_resources_rebind)

    submit_parser = sub.add_parser("submit", help="持久化一个任务请求（不自动开始推理）")
    source_group = submit_parser.add_mutually_exclusive_group()
    source_group.add_argument("--video", default=None)
    source_group.add_argument("--url", default=None)
    submit_parser.add_argument("--config", required=True)
    submit_parser.add_argument("--mode", required=True, choices=("combined", "speech_only", "visual_only", "supplied_source"))
    submit_parser.add_argument("--timing-mode", default=None, choices=("mixed", "screen", "speech"))
    submit_parser.add_argument("--project", default=None, help="已有项目 ID；不给则新建项目")
    submit_parser.add_argument("--source-revision", default=None, help="supplied_source 模式的源字幕版本 ID")
    submit_parser.add_argument("--video-stream", default=None, help="视频流序号；单一合法流可省略")
    submit_parser.add_argument("--audio-stream", default=None, help="音频流序号或 none")
    submit_parser.add_argument("--copy-original", action="store_true")
    submit_parser.add_argument("--accept-dictionary-warnings", action="store_true")
    submit_parser.add_argument("--display-name", default=None)
    submit_parser.set_defaults(func=cmd_submit, command="request")

    queue_parser = sub.add_parser("queue", help="队列执行与取消")
    queue_sub = queue_parser.add_subparsers(dest="command", required=True)
    queue_run = queue_sub.add_parser("run", help="取得执行锁后按顺序处理 QUEUED 请求，队列耗尽即退出")
    queue_run.add_argument("--data-root", required=True)
    queue_run.add_argument("--continue-on-error", dest="continue_on_error", action="store_true", default=None)
    queue_run.add_argument("--stop-on-error", dest="continue_on_error", action="store_false")
    queue_run.set_defaults(func=cmd_queue_run)
    queue_stop = queue_sub.add_parser("stop", help="停止当前：终止正在运行请求的自有进程（可恢复）并暂停队列")
    queue_stop.add_argument("--data-root", required=True)
    queue_stop.set_defaults(func=cmd_queue_stop)

    queue_list = queue_sub.add_parser("list", help="列出全部请求（含历史）：名称、输入、模式、状态、当前阶段与已耗时、版本与质量（只读）")
    queue_list.add_argument("--data-root", required=True)
    queue_list.set_defaults(func=cmd_queue_list)

    queue_copy = queue_sub.add_parser("copy", help="复制为新任务：相同输入/模式/选流，采用当前配置、词典与资源")
    queue_copy.add_argument("--data-root", required=True)
    queue_copy.add_argument("--request", required=True)
    queue_copy.add_argument("--display-name", default=None)
    queue_copy.add_argument("--accept-dictionary-warnings", action="store_true")
    queue_copy.set_defaults(func=cmd_queue_copy)

    queue_cancel = queue_sub.add_parser("cancel", help="取消未开始请求，或请求执行器停止正在运行的请求")
    queue_cancel.add_argument("--data-root", required=True)
    queue_cancel.add_argument("--request", required=True)
    queue_cancel.set_defaults(func=cmd_queue_cancel)

    resume_parser = sub.add_parser("resume", help="把失败/中断的运行重新排回队列（保留原配置，新增 attempt）")
    resume_parser.add_argument("--data-root", required=True)
    resume_parser.add_argument("--run", required=True)
    resume_parser.set_defaults(func=cmd_resume, command="run")

    inbox_parser = sub.add_parser("inbox", help="旧收件箱接管")
    inbox_sub = inbox_parser.add_subparsers(dest="command", required=True)
    inbox_intake = inbox_sub.add_parser("intake", help="把旧 inbox 的 pending/working 请求入库到 v2 队列（内容哈希去重）")
    inbox_intake.add_argument("--config", required=True)
    inbox_intake.add_argument("--inbox", required=True)
    inbox_intake.set_defaults(func=cmd_inbox_intake)

    media_parser = sub.add_parser("media", help="媒体登记、定位与重新定位")
    media_sub = media_parser.add_subparsers(dest="command", required=True)
    media_register = media_sub.add_parser("register", help="把本地视频登记为项目媒体（“已有日文源字幕”任务用）；选流有歧义时先拒绝，不建项目")
    media_register.add_argument("--data-root", required=True)
    media_register.add_argument("--video", required=True)
    media_register.add_argument("--project", default=None, help="已有项目 ID")
    media_register.add_argument("--display-name", default=None, help="新项目的显示名")
    media_register.add_argument("--video-stream", default=None)
    media_register.add_argument("--audio-stream", default=None, help="音频流序号或 none")
    media_register.add_argument("--copy-original", action="store_true")
    media_register.set_defaults(func=cmd_media_register)
    media_locate = media_sub.add_parser("locate", help="原视频仍在原处且内容一致时给出路径；否则 INPUT_MISSING / MEDIA_CHANGED")
    media_locate.add_argument("--data-root", required=True)
    media_locate.add_argument("--project", required=True)
    media_locate.add_argument("--media", required=True)
    media_locate.set_defaults(func=cmd_media_locate)
    media_relocate = media_sub.add_parser("relocate", help="原视频移动后指向新位置；只接受完整哈希相同的文件")
    media_relocate.add_argument("--data-root", required=True)
    media_relocate.add_argument("--project", required=True)
    media_relocate.add_argument("--media", required=True)
    media_relocate.add_argument("--path", required=True)
    media_relocate.set_defaults(func=cmd_media_relocate)

    review_parser = sub.add_parser("review", help="人工校对：创建校对副本、登记校对稿、标记人工确认")
    review_sub = review_parser.add_subparsers(dest="command", required=True)
    review_create = review_sub.add_parser("create", help="从一个已提交版本的字幕文件创建校对副本（edit 文件是唯一交给编辑器修改的文件）")
    review_create.add_argument("--data-root", required=True)
    review_create.add_argument("--project", required=True)
    review_create.add_argument("--revision", required=True, help="具体版本 ID；没有 latest")
    review_create.add_argument("--format", required=True, choices=("srt", "ass"))
    review_create.set_defaults(func=cmd_review_create)
    review_import = review_sub.add_parser("import", help="把已保存的中文 SRT/ASS 原字节登记为不可变的人工版本（不改原文件）")
    source_parser = sub.add_parser("source", help="日文原文修订")
    source_sub = source_parser.add_subparsers(dest="command", required=True)
    source_import = source_sub.add_parser("import", help="把已保存的源字幕登记为新的源版本；据此生成新译文需另行 submit --mode supplied_source")
    for importer, func in ((review_import, cmd_review_import), (source_import, cmd_source_import)):
        importer.add_argument("--data-root", required=True)
        importer.add_argument("--project", required=True)
        importer.add_argument("--media", required=True, help="这份字幕对应的媒体版本 ID；不同视频不视为同一时间轴")
        importer.add_argument("--file", required=True)
        importer.add_argument("--base", default=None, help="可选：这份稿子所基于的版本 ID")
        importer.add_argument("--session", default=None, help="可选：创建校对副本时得到的 session ID；与 --base 同时给出时必须一致")
        importer.add_argument("--encoding", default=None, help="可选：Python 编码名；默认只认 BOM/UTF-8")
        importer.set_defaults(func=func)
    review_attest = review_sub.add_parser("attest", help="用户对一个具体版本的字幕字节标记人工确认范围（不改变任何字幕文件）")
    review_attest.add_argument("--data-root", required=True)
    review_attest.add_argument("--project", required=True)
    review_attest.add_argument("--revision", required=True)
    review_attest.add_argument("--scopes", required=True, help="明确列出的非空子集，逗号分隔：text,timing,style（SRT 没有 style）")
    review_attest.add_argument("--format", default=None, choices=("srt", "ass"), help="版本含多个字幕文件（机器版）时指明确认哪一个")
    review_attest.add_argument("--note", default=None)
    review_attest.set_defaults(func=cmd_review_attest)
    review_frames = review_sub.add_parser("frames", help="按需导出边界帧：把指定时间前后两帧另存为 PNG，文件名和 frames.json 标注实际 PTS（只读视频）")
    review_frames.add_argument("--data-root", required=True)
    review_frames.add_argument("--project", required=True)
    review_frames.add_argument("--revision", required=True)
    review_frames.add_argument("--issue", default=None, help="质量记录里的 issue_id：取它的边界时间（QC13 的边界，或问题的起止时间）")
    review_frames.add_argument("--at-ms", default=None, help="逗号分隔的毫秒时间（规范时间轴，首个显示帧 = 0）")
    review_frames.add_argument("--destination", required=True, help="新目录；已存在则拒绝（不覆盖）")
    review_frames.set_defaults(func=cmd_review_frames)
    export_parser = sub.add_parser("export", help="从一个明确的版本导出字幕文件或成片（MP4 硬字幕 / MKV 软字幕）；不启动任何模型，输出目录只创建一次")
    export_parser.add_argument("--data-root", required=True)
    export_parser.add_argument("--project", required=True)
    export_parser.add_argument("--revision", required=True, help="具体的版本 ID；没有 latest")
    export_parser.add_argument("--kind", required=True, choices=("subtitles", "mp4_burn", "mkv_soft"))
    export_parser.add_argument("--destination", required=True, help="新目录；已存在则拒绝（不覆盖）")
    export_parser.add_argument("--profile", default=None, help="缺省按 kind 取唯一的固定 profile")
    export_parser.add_argument("--format", default=None, choices=("srt", "ass"), help="版本含两种格式（机器版）时选一个；成片缺省用 ASS")
    export_parser.add_argument("--audio-stream", default=None, help="成片用的音轨序号或 none；缺省用项目已选音轨")
    export_parser.add_argument("--media", default=None, help="目标媒体 ID；缺省为字幕绑定的媒体，不同则拒绝（QC21）")
    export_parser.add_argument("--include-media", action="store_true", help="仅字幕导出：同时复制原视频")
    export_parser.add_argument("--convert-to", default=None, choices=("srt", "ass"), help="仅在明确要转换时使用：ASS→SRT 会失去排版；SRT→ASS 用固定基础样式")
    export_parser.add_argument("--accept-omitted-drawings", action="store_true", help="ASS→SRT 时确认省略绘图事件")
    export_parser.add_argument("--font-dir", action="append", default=None, help="额外的字体目录（可重复）；此外只查系统字体目录")
    export_parser.set_defaults(func=cmd_export)
    revisions_parser = sub.add_parser("revisions", help="查看一个项目里已提交的版本")
    revisions_sub = revisions_parser.add_subparsers(dest="command", required=True)
    revisions_list = revisions_sub.add_parser("list", help="列出机器/源/人工版本：格式、哈希前 12 位、人工状态、质量状态（只读）")
    revisions_list.add_argument("--data-root", required=True)
    revisions_list.add_argument("--project", required=True)
    revisions_list.set_defaults(func=cmd_revisions_list)
    migrate_parser = sub.add_parser("migrate", help="把一个旧任务目录只读导入为新项目（旧目录不改；旧字幕只作未审快照）")
    migrate_parser.add_argument("--data-root", required=True)
    migrate_parser.add_argument("--legacy-job", required=True, help="旧任务目录（含 task.json）")
    migrate_parser.add_argument("--display-name", default=None)
    migrate_parser.add_argument("--no-media", action="store_true", help="只引用原视频，不登记为媒体")
    migrate_parser.set_defaults(func=cmd_migrate)
    return parser


def run_worker(argv: list[str]) -> int:
    """Internal worker entry (plan 12.3): ``roast worker NAME ARGS...`` runs a bundled module in its own owned process."""
    if argv and argv[0] == "yt-dlp":
        try:
            import yt_dlp
        except ImportError as exc:
            _emit(_result("worker.yt-dlp", ok=False, error=ContractError("CAPABILITY_MISSING", f"yt-dlp is not installed in this runtime: {exc}")))
            return EXIT_RESOURCE
        yt_dlp.main(argv[1:])  # exits the process with yt-dlp's own exit code
        return EXIT_OK
    _emit(_result("worker", ok=False, error=ContractError("NEEDS_SELECTION", f"unknown worker {argv[:1]}", details={"supported": ["yt-dlp"]})))
    return EXIT_ARGS


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["worker"]:
        return run_worker(argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except ContractError as exc:                 # unknown command or option (for example a v1 flag such as --zh-file): refused, never reinterpreted
        _log(str(exc))
        _emit(_result("arguments", ok=False, error=exc))
        return EXIT_ARGS
    except SystemExit as exc:                    # --help
        return EXIT_ARGS if exc.code not in (0, None) else EXIT_OK
    operation = f"{args.group}.{getattr(args, 'command', 'run')}"
    try:
        return int(args.func(args))
    except ContractError as exc:
        _log(str(exc))
        _emit(_result(operation, ok=False, error=exc))
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(_result(operation, ok=False, error=ContractError("PROCESS_CANCELLED", "cancelled by user")))
        return 6
    except OSError as exc:
        error = ContractError("PROCESS_FAILED", f"{type(exc).__name__}: {exc}")
        _emit(_result(operation, ok=False, error=error))
        return EXIT_DATA


if __name__ == "__main__":
    raise SystemExit(main())
