#!/usr/bin/env python3
"""AppConfig / engine settings read-write, defaults, snapshots and legacy migration (plan 1.1, sections 3.4, 4.2, 6.2, 6.8).

No inference about the user's intent: unknown legacy keys are kept under ``extensions.legacy_extra``
and never executed; shell command templates are recorded, mapped only when they equal the known
built-in defaults, otherwise reported as CONFIG_UNSUPPORTED_COMMAND.
"""

from __future__ import annotations

import copy
import ctypes
import os
import sys
from pathlib import Path
from typing import Any

from upgrade.contracts import (
    SCHEMA_VERSION, ContractError, find_sensitive_keys, load_record, require_record, sha256_canonical, utc_now,
)
from upgrade.storage import atomic_write_text, render_json

DEFAULT_QUALITY: dict[str, Any] = {
    "rule_set_version": 1, "min_duration_ms": 500, "max_duration_ms": 10000, "max_cps": 15,
    "ocr_probability_min": 0.80, "asr_probability_min": 0.50, "ocr_continuity_gap_ms": 200, "activity_gap_ms": 1000,
}
DEFAULT_DEFAULTS: dict[str, Any] = {
    "recognition_mode": "combined", "timing_mode": "mixed", "continue_on_error": True, "output_formats": ["srt", "ass"],
    "render_after_run": False, "copy_original": False, "gpu_profile": "verified_existing",
}
APP_CONFIG_NAME = "settings/app.json"
ENGINE_SETTINGS_NAME = "settings/engine.json"
RESOURCES_FILE_NAME = "settings/resources.local.json"

# Legacy example templates (app/config/roast_pipeline.example.json). A template that equals one of these maps to
# the built-in argv implementation of the same step; anything else is unsupported and only recorded.
LEGACY_BUILTIN_TEMPLATES: dict[str, str] = {
    "download": '{yt_dlp} {proxy_arg} --newline --write-info-json --merge-output-format mp4 -o "{job_dir}/source/video.%(ext)s" "{url}"',
    "extract_audio": '{ffmpeg} -y -i "{video}" -vn -ac 1 -ar 16000 "{audio}"',
    "axis": '"{python}" "{root}/tools/magia_timeline_adapter.py" --config "{config_path}" --video "{video}" --output "{timeline}"',
    "recognize_jp": '"{python}" "{root}/tools/voicetransl_adapter.py" recognize --config "{config_path}" --audio "{audio}" --output "{jp}"',
    "translate_zh": '"{python}" "{root}/tools/voicetransl_adapter.py" translate --config "{config_path}" --input "{jp}" --output "{zh}"',
}
LEGACY_TRANSLATION_KEYS = ("endpoint", "gpu_layers", "context_size", "workers", "batch_size", "temperature", "top_p",
                           "use_native_dictionaries", "native_prompt_mode", "engine", "enabled")
LEGACY_RECOGNITION_KEYS = ("engine", "enabled", "language", "vad", "word_timestamps", "dtw_model")
LEGACY_BROWSERS = {"Edge": "edge", "Chrome": "chrome", "Firefox": "firefox"}
LEGACY_YOUTUBE_CLIENTS = {"Android VR": "android_vr", "TV": "tv"}
LEGACY_AXIS_KEYS = ("enabled", "strategy", "preset", "engine", "max_resolution", "ocr")


def default_ascii_temp_root() -> str:
    drive = os.environ.get("SystemDrive", "C:") if sys.platform == "win32" else ""
    return f"{drive}/AutoRoastTemp" if drive else str(Path.home() / "AutoRoastTemp")


def documents_dir() -> Path:
    """Resolve the real Documents folder on Windows (no hard-coded user name); fall back to ~/Documents."""
    if sys.platform == "win32":
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            # CSIDL_PERSONAL = 5, SHGFP_TYPE_CURRENT = 0
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except (AttributeError, OSError):
            pass
    return Path.home() / "Documents"


def default_data_root() -> str:
    return str(documents_dir() / "AutoRoastData")


def default_app_config(data_root: str | Path, *, ascii_temp_root: str | None = None, resources_file: str | None = None) -> dict:
    config = {
        "schema_version": SCHEMA_VERSION,
        "data_root": str(data_root),
        "ascii_temp_root": ascii_temp_root or default_ascii_temp_root(),
        "resources_file": resources_file or RESOURCES_FILE_NAME,
        "editor_srt": None,
        "editor_ass": None,
        "defaults": copy.deepcopy(DEFAULT_DEFAULTS),
        "quality": copy.deepcopy(DEFAULT_QUALITY),
        "extensions": {},
    }
    return require_record("AppConfig", config)


def resolve_in_data_root(config: dict, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(config["data_root"]) / path


def load_app_config(path: str | Path) -> dict:
    return load_record("AppConfig", path)


def save_app_config(path: str | Path, config: dict) -> Path:
    require_record("AppConfig", config)
    return atomic_write_text(Path(path), render_json(config))


def load_engine_settings(path: str | Path) -> dict:
    """Engine settings are a free-form object owned by W03/W04; W01 only guarantees JSON validity and no secrets."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    from upgrade.contracts import parse_json_text
    try:
        value = parse_json_text(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractError("SCHEMA_INVALID", f"engine settings are not valid JSON: {exc}", details={"path": str(file_path)}) from exc
    if not isinstance(value, dict):
        raise ContractError("SCHEMA_INVALID", "engine settings root must be an object", details={"path": str(file_path)})
    secrets = find_sensitive_keys(value)
    if secrets:
        raise ContractError("SCHEMA_INVALID", "engine settings must not contain secrets", details={"keys": secrets})
    return value


def save_engine_settings(path: str | Path, settings: dict) -> Path:
    secrets = find_sensitive_keys(settings)
    if secrets:
        raise ContractError("SCHEMA_INVALID", "engine settings must not contain secrets", details={"keys": secrets})
    return atomic_write_text(Path(path), render_json(settings))


def build_config_snapshot(app_config: dict, engine_settings: dict, *, dictionary_revision_id: str,
                          resource_refs: list[dict], accepted_dictionary_warnings: list[str] | None = None) -> dict:
    """Freeze a complete effective configuration. Later edits to the inputs do not change the snapshot."""
    body = {
        "app_config": copy.deepcopy(require_record("AppConfig", app_config)),
        "engine_settings": copy.deepcopy(engine_settings),
        "dictionary_revision_id": dictionary_revision_id,
        "resource_refs": copy.deepcopy(resource_refs),
        "accepted_dictionary_warnings": list(accepted_dictionary_warnings or []),
    }
    snapshot = {**body, "config_sha256": sha256_canonical(body)}
    return require_record("ConfigSnapshot", snapshot)


# --------------------------------------------------------------------------- legacy migration

def _legacy_roi(dialog_rect: Any) -> dict | None:
    """Legacy dialog_rect is [left, right, top, bottom] (plan 6.4 fixes this order)."""
    if not (isinstance(dialog_rect, list) and len(dialog_rect) == 4 and all(isinstance(v, (int, float)) for v in dialog_rect)):
        return None
    left, right, top, bottom = (float(v) for v in dialog_rect)
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def migrate_legacy_config(legacy: dict, *, data_root: str | Path, ascii_temp_root: str | None = None) -> dict:
    """Map a legacy roast_pipeline.local.json to {app_config, engine_settings, suggested_resources, unsupported_commands, legacy_extra}.

    Only semantic values the plan names are mapped. Paths become *suggested* resource bindings that the user must
    confirm into resources.local.json; nothing is executed or auto-bound.
    """
    if not isinstance(legacy, dict):
        raise ContractError("SCHEMA_INVALID", "legacy config root must be an object")
    config = default_app_config(data_root, ascii_temp_root=ascii_temp_root)
    engine: dict[str, Any] = {"translation": {}, "recognition": {}, "ocr": {}, "media": {}, "download": {}, "dictionary": {}}
    consumed: set[str] = set()
    report: dict[str, Any] = {"mapped": [], "unsupported_commands": [], "suggested_resources": {}, "legacy_extra": {}, "notes": []}

    alignment = legacy.get("alignment") if isinstance(legacy.get("alignment"), dict) else {}
    timing = alignment.get("timing_mode")
    if timing in ("mixed", "screen", "speech"):
        config["defaults"]["timing_mode"] = timing
        report["mapped"].append("alignment.timing_mode -> defaults.timing_mode")
    batch = legacy.get("batch_settings") if isinstance(legacy.get("batch_settings"), dict) else {}
    if isinstance(batch.get("continue_on_error"), bool):
        config["defaults"]["continue_on_error"] = batch["continue_on_error"]
        report["mapped"].append("batch_settings.continue_on_error -> defaults.continue_on_error")
    consumed.update({"alignment", "batch_settings"})

    translation = legacy.get("translation") if isinstance(legacy.get("translation"), dict) else {}
    for key in LEGACY_TRANSLATION_KEYS:
        if key in translation:
            engine["translation"][key] = translation[key]
    for key in ("native_pre_dictionary", "native_gpt_dictionary", "native_post_dictionary", "native_guideline", "native_project_config"):
        if translation.get(key):
            engine["translation"].setdefault("native_files", {})[key] = translation[key]
    for key in ("server_executable", "model", "executable"):
        if translation.get(key):
            report["suggested_resources"][f"translation.{key}"] = translation[key]
    consumed.add("translation")
    # dictionary files (plan 8.5): explicit absolute paths are kept; relative legacy paths pointed at the bundled defaults, which v2 resolves itself
    resources = legacy.get("resources") if isinstance(legacy.get("resources"), dict) else {}
    for legacy_key, role in (("translation_prompt", "style_prompt"), ("translation_glossary", "glossary")):
        value = str(resources.get(legacy_key) or "").strip()
        if value and Path(value).is_absolute():
            engine.setdefault("dictionary", {})[role] = value
            report["mapped"].append(f"resources.{legacy_key} -> dictionary.{role}")
        elif value:
            report["notes"].append(f"resources.{legacy_key}={value!r} is a relative legacy path; v2 uses its bundled default for dictionary.{role} unless a file is chosen")

    recognition = legacy.get("recognition") if isinstance(legacy.get("recognition"), dict) else {}
    for key in LEGACY_RECOGNITION_KEYS:
        if key in recognition:
            engine["recognition"][key] = recognition[key]
    for key in ("executable", "model", "vad_model"):
        if recognition.get(key):
            report["suggested_resources"][f"recognition.{key}"] = recognition[key]
    consumed.add("recognition")

    axis = legacy.get("axis_settings") if isinstance(legacy.get("axis_settings"), dict) else {}
    for key in LEGACY_AXIS_KEYS:
        if key in axis:
            engine["ocr"][key] = axis[key]
    roi = _legacy_roi(axis.get("dialog_rect"))
    if roi:
        engine["ocr"]["roi"] = roi
        engine["ocr"]["roi_converted_from_dialog_rect"] = list(axis["dialog_rect"])
        report["mapped"].append("axis_settings.dialog_rect [left,right,top,bottom] -> ocr.roi {left,top,right,bottom}")
    if axis.get("executable"):
        report["suggested_resources"]["axis_settings.executable"] = axis["executable"]
    consumed.add("axis_settings")

    media = legacy.get("media_compatibility") if isinstance(legacy.get("media_compatibility"), dict) else {}
    if media:
        engine["media"] = {k: media[k] for k in ("auto_convert", "preset", "crf", "accepted_containers", "accepted_video_codecs") if k in media}
    consumed.add("media_compatibility")
    if legacy.get("proxy"):
        engine["download"]["proxy"] = legacy["proxy"]
    consumed.add("proxy")
    downloads = legacy.get("download_settings") if isinstance(legacy.get("download_settings"), dict) else {}
    if downloads.get("quality"):
        engine["download"]["quality"] = downloads["quality"]
    # an explicit legacy choice to log in through a browser profile is kept as a choice; no cookie content is ever stored
    browser = LEGACY_BROWSERS.get(str(downloads.get("cookies_from_browser", "")).strip())
    if browser:
        engine["download"]["browser_login_source"] = browser
    if str(downloads.get("js_runtime", "")).strip():
        engine["download"]["js_runtime"] = str(downloads["js_runtime"]).strip()
    client = LEGACY_YOUTUBE_CLIENTS.get(str(downloads.get("youtube_client", "")).strip())
    if client:
        engine["download"]["youtube_client"] = client
    if downloads:
        report["mapped"].append("download_settings -> download {quality, browser_login_source, js_runtime, youtube_client}")
    consumed.add("download_settings")

    tools = legacy.get("tools") if isinstance(legacy.get("tools"), dict) else {}
    for key in ("ffmpeg", "yt_dlp", "axis_generator", "jp_recognizer", "machine_translator"):
        if tools.get(key):
            report["suggested_resources"][f"tools.{key}"] = tools[key]
    consumed.add("tools")
    external = legacy.get("external_tools") if isinstance(legacy.get("external_tools"), dict) else {}
    for key in ("magia_root", "voice_root"):
        if external.get(key):
            report["suggested_resources"][f"external_tools.{key}"] = external[key]
    consumed.add("external_tools")

    commands = legacy.get("commands") if isinstance(legacy.get("commands"), dict) else {}
    for name, template in commands.items():
        if LEGACY_BUILTIN_TEMPLATES.get(name) == template:
            report["mapped"].append(f"commands.{name} -> builtin:{name}")
        else:
            report["unsupported_commands"].append({"code": "CONFIG_UNSUPPORTED_COMMAND", "key": f"commands.{name}", "template": template,
                                                   "message": "custom shell template is recorded only and never executed in v2"})
    consumed.add("commands")

    for key in ("expected_outputs", "jianying_ui", "forced_alignment", "resources", "work_dir"):
        consumed.add(key)
    legacy_extra = {key: copy.deepcopy(value) for key, value in legacy.items() if key not in consumed or key in ("expected_outputs", "jianying_ui", "forced_alignment", "resources", "work_dir", "commands")}
    secrets = find_sensitive_keys(legacy_extra)
    if secrets:
        report["notes"].append(f"legacy keys that look like credentials were dropped from the migrated record: {secrets}")
        legacy_extra = _drop_sensitive(legacy_extra)
    report["legacy_extra"] = legacy_extra
    config["extensions"]["legacy_extra"] = legacy_extra
    config["extensions"]["migrated_at"] = utc_now()
    engine = {key: value for key, value in engine.items() if value}
    return {"app_config": require_record("AppConfig", config), "engine_settings": engine, **report}


def _drop_sensitive(value: Any) -> Any:
    from upgrade.contracts import SENSITIVE_KEY_RE
    if isinstance(value, dict):
        return {k: _drop_sensitive(v) for k, v in value.items() if not SENSITIVE_KEY_RE.search(str(k))}
    if isinstance(value, list):
        return [_drop_sensitive(v) for v in value]
    return value


__all__ = [
    "DEFAULT_QUALITY", "DEFAULT_DEFAULTS", "APP_CONFIG_NAME", "ENGINE_SETTINGS_NAME", "RESOURCES_FILE_NAME",
    "default_ascii_temp_root", "default_data_root", "documents_dir", "default_app_config", "resolve_in_data_root",
    "load_app_config", "save_app_config", "load_engine_settings", "save_engine_settings", "build_config_snapshot",
    "migrate_legacy_config",
]
