#!/usr/bin/env python3
"""Dictionary revisions and line-level diagnostics (plan 1.1, section 8.5, D07; W04).

A dictionary revision is a byte-for-byte copy of the files a translation uses: the native pre / gpt / post
dictionaries, the user glossary JSON, the style prompt and, when the native prompt mode asks for them, the
VoiceTransl guideline and project configuration. Order, comments, escapes, notes and the ``^^``/``1^`` modes are
kept because the files are copied, never re-serialized. The revision is addressed by content: the same bytes give
the same revision again.

The existing parser (``voice_dictionary``) stays the only interpreter of the files. This module adds the per-line
diagnostics the plan asks for: every entry the parser would skip is listed with file, line, original text and
reason, the cross-file conflicts (same source, different target) are reported as warnings, and the effective
priority is recorded as the legacy code applies it (glossary first; gpt entries whose source the glossary already
covers are excluded from the prompt; pre replaces source text before translation; post replaces target text
after). A conditional/situational GalTransl rule is listed, not implemented half-way.

Unsupported entries need an explicit acceptance (``submit --accept-dictionary-warnings``); an acceptance is stored
per content hash so the same revision does not ask again, and a new content hash asks again.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from upgrade.contracts import ContractError, SCHEMA_VERSION, load_record, new_id, parse_json_text, sha256_bytes, sha256_text, signature, utc_now
from upgrade.storage import atomic_write_bytes, atomic_write_text, begin_pending, commit_pending, render_json, safe_join, verify_committed

DICTIONARY_ADAPTER_VERSION = 1
ROLES = ("pre", "gpt", "post", "glossary", "style_prompt", "guideline", "project_config")
NATIVE_ROLES = ("pre", "gpt", "post")
BUNDLED_NAMES = {"pre": "dict_pre.txt", "gpt": "dict_gpt.txt", "post": "dict_after.txt", "glossary": "translation_glossary.json", "style_prompt": "translation_style_prompt.txt"}
STORED_NAMES = {"pre": "pre.txt", "gpt": "gpt.txt", "post": "post.txt", "glossary": "glossary.json", "style_prompt": "style_prompt.txt",
                "guideline": "guideline.txt", "project_config": "project_config.yaml"}
LEGACY_SETTING_KEYS = {"native_pre_dictionary": "pre", "native_gpt_dictionary": "gpt", "native_post_dictionary": "post",
                       "native_guideline": "guideline", "native_project_config": "project_config"}
CONDITIONAL_KEYS = {"pre_src", "post_src", "pre_dst", "post_dst", "pre_jp", "post_jp", "pre_zh", "post_zh", "mono", "diag"}
PROMPT_MODES = {"不使用": "none", "追加": "append", "覆盖": "overwrite", "none": "none", "append": "append", "overwrite": "overwrite"}
ACCEPTANCE_FILE = "settings/dictionary-acceptances.json"
LEGACY_FALLBACK_PROMPT = "完整、忠实地把日文字幕直译为简体中文，保留未完句和真实重复。"


def bundled_defaults_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "defaults"
    return Path(__file__).resolve().parents[2] / "config"


def prompt_mode(settings: dict) -> str:
    value = str(settings.get("native_prompt_mode", "不使用")).strip()
    if value not in PROMPT_MODES:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"unknown native_prompt_mode {value!r}", details={"supported": sorted(PROMPT_MODES)})
    return PROMPT_MODES[value]


# --------------------------------------------------------------------------- inputs

def resolve_inputs(engine_settings: dict, *, defaults_dir: Path | None = None) -> dict[str, dict | None]:
    """Role -> {path, origin} from ``engine_settings.dictionary`` (explicit files), the migrated legacy keys under
    ``translation.native_files``, or the bundled defaults. Guideline/project config are optional and only used when the
    native prompt mode needs them; the native dictionaries are dropped when ``use_native_dictionaries`` is false."""
    translation = engine_settings.get("translation") if isinstance(engine_settings.get("translation"), dict) else {}
    explicit = engine_settings.get("dictionary") if isinstance(engine_settings.get("dictionary"), dict) else {}
    legacy = translation.get("native_files") if isinstance(translation.get("native_files"), dict) else {}
    defaults = Path(defaults_dir) if defaults_dir else bundled_defaults_dir()
    use_native = bool(translation.get("use_native_dictionaries", True))
    mode = prompt_mode(translation)
    resolved: dict[str, dict | None] = {}
    for role in ROLES:
        if role in NATIVE_ROLES and not use_native:
            resolved[role] = None
            continue
        if role in ("guideline", "project_config") and mode == "none":
            resolved[role] = None
            continue
        value = str(explicit.get(role) or "").strip()
        if not value:
            legacy_key = next((key for key, mapped in LEGACY_SETTING_KEYS.items() if mapped == role), None)
            value = str(legacy.get(legacy_key) or "").strip() if legacy_key else ""
        if value:
            path = Path(value)
            if not path.is_absolute():
                raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"dictionary path for {role} must be absolute", details={"role": role, "value": value})
            if not path.is_file():
                if role == "project_config":
                    resolved[role] = None    # legacy: optional even in append/overwrite mode
                    continue
                raise ContractError("INPUT_MISSING", f"dictionary file for {role} does not exist", details={"role": role, "path": value})
            resolved[role] = {"path": path, "origin": "user_file"}
        elif role in BUNDLED_NAMES and (defaults / BUNDLED_NAMES[role]).is_file():
            resolved[role] = {"path": defaults / BUNDLED_NAMES[role], "origin": "bundled_default"}
        elif role == "guideline":
            raise ContractError("INPUT_MISSING", "native_prompt_mode needs the VoiceTransl guideline file (dictionary.guideline)", details={"mode": mode})
        else:
            resolved[role] = None
    return resolved


# --------------------------------------------------------------------------- diagnostics (mirror of the legacy parser's decisions)

def _diagnose_native(role: str, text: str, *, gpt: bool) -> tuple[list[dict], list[dict], int]:
    """Replay ``voice_dictionary.load_native_dictionary`` line by line. Returns (entries, unsupported, ignored_count)."""
    from voice_dictionary import process_native_escape
    entries: list[dict] = []
    unsupported: list[dict] = []
    ignored = 0
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip("﻿\r\n")
        if not line.strip() or line.lstrip().startswith(("//", "\\\\")):
            continue
        if gpt and "->" in line and "\t" not in line:
            source, remainder = line.split("->", 1)
            target, marker, note = remainder.partition("#")
            parts = [source.strip(), target.strip(), note.strip() if marker else ""]
        else:
            parts = line.replace("    ", "\t").split("\t")
        parts = [process_native_escape(part) for part in parts]
        if len(parts) < 2 or not parts[0]:
            unsupported.append({"role": role, "line": line_no, "text": raw_line, "reason": "needs source<TAB>target (one entry per line)", "kind": "malformed"})
            continue
        if parts[0] in CONDITIONAL_KEYS:
            unsupported.append({"role": role, "line": line_no, "text": raw_line, "reason": f"conditional/situational GalTransl rule {parts[0]!r} is not supported by the plain-text mode",
                                "kind": "conditional_rule"})
            continue
        source, mode = parts[0], "all"
        if source.startswith("^^"):
            source, mode = source[2:], "prefix"
        elif source.startswith("1^"):
            source, mode = source[2:], "once"
        if not source:
            ignored += 1
            unsupported.append({"role": role, "line": line_no, "text": raw_line, "reason": "empty source after the ^^/1^ marker; the parser drops this line silently",
                                "kind": "empty_source"})
            continue
        entries.append({"line": line_no, "source": source, "target": parts[1], "note": parts[2] if len(parts) > 2 else "", "mode": mode})
    return entries, unsupported, ignored


def _diagnose_glossary(text: str) -> tuple[dict[str, dict], list[dict]]:
    try:
        raw = parse_json_text(text.lstrip("﻿"))
    except ValueError as exc:
        return {}, [{"role": "glossary", "line": None, "text": "", "reason": f"not valid JSON: {exc}; the legacy loader would silently use an empty glossary", "kind": "malformed"}]
    if not isinstance(raw, dict):
        return {}, [{"role": "glossary", "line": None, "text": "", "reason": "the glossary root must be an object of source -> target", "kind": "malformed"}]
    unsupported: list[dict] = []
    usable: dict[str, Any] = {}
    for source, target in raw.items():
        if isinstance(target, str) or (isinstance(target, dict) and all(isinstance(v, str) for v in target.values())):
            usable[str(source)] = target
        else:
            unsupported.append({"role": "glossary", "line": None, "text": f"{source}: {target!r}"[:200], "reason": "glossary values must be a string or an object of strings",
                                "kind": "malformed"})
    from voice_dictionary import normalize_glossary
    return normalize_glossary(usable), unsupported


def _conflicts(entries: dict[str, list[dict]], glossary: dict[str, dict]) -> list[dict]:
    """Same source with different targets across files or inside one file (warning; nothing is merged or re-ordered)."""
    from voice_dictionary import resolved_term_target
    targets: dict[str, list[tuple[str, str, int | None]]] = {}
    for source in glossary:
        targets.setdefault(source, []).append(("glossary", resolved_term_target(source, "", glossary), None))
    for role in ("gpt", "pre"):
        for entry in entries.get(role, []):
            targets.setdefault(entry["source"], []).append((role, entry["target"], entry["line"]))
    warnings = []
    for source, rows in sorted(targets.items()):
        if len({target for _, target, _ in rows}) > 1:
            warnings.append({"kind": "conflict", "source": source, "entries": [{"role": role, "target": target, "line": line} for role, target, line in rows],
                             "effective": "glossary wins over gpt in the prompt; pre is applied to the source text before translation; nothing is merged"})
    return warnings


def diagnose(files: dict[str, bytes | None]) -> dict:
    """Line-level diagnostics for a set of dictionary files (role -> bytes)."""
    from subtitle_axis_align import read_text
    from voice_dictionary import load_native_dictionary
    import tempfile

    def decode(data: bytes) -> str:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as handle:
            handle.write(data)
            temp = Path(handle.name)
        try:
            return read_text(temp)          # the legacy decoder (utf-8-sig, utf-8, cp932, gb18030)
        finally:
            temp.unlink(missing_ok=True)

    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "adapter_version": DICTIONARY_ADAPTER_VERSION, "files": {}, "unsupported": [], "warnings": [],
                              "entry_counts": {}, "parser_agreement": {}}
    entries: dict[str, list[dict]] = {}
    for role in NATIVE_ROLES:
        data = files.get(role)
        if data is None:
            continue
        text = decode(data)
        parsed, unsupported, ignored = _diagnose_native(role, text, gpt=(role == "gpt"))
        entries[role] = parsed
        report["unsupported"].extend(unsupported)
        report["entry_counts"][role] = len(parsed)
        # the diagnostics must agree with the parser that is actually used
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as handle:
            handle.write(data)
            temp = Path(handle.name)
        try:
            legacy_entries, legacy_unsupported = load_native_dictionary(temp, gpt=(role == "gpt"))
        finally:
            temp.unlink(missing_ok=True)
        agreement = {"parser_entries": len(legacy_entries), "parser_unsupported": legacy_unsupported, "diagnosed_entries": len(parsed),
                     "diagnosed_unsupported": len(unsupported) - ignored, "silently_dropped": ignored}
        report["parser_agreement"][role] = agreement
        if agreement["parser_entries"] != agreement["diagnosed_entries"] or agreement["parser_unsupported"] != agreement["diagnosed_unsupported"]:
            raise ContractError("DICTIONARY_UNSUPPORTED", f"dictionary diagnostics for {role} disagree with the parser; refusing to describe the file wrongly",
                                details={"role": role, **agreement})
        duplicates: dict[str, set[str]] = {}
        for entry in parsed:
            duplicates.setdefault(entry["source"], set()).add(entry["target"])
        for source, targets in sorted(duplicates.items()):
            if len(targets) > 1:
                report["warnings"].append({"kind": "duplicate_source", "role": role, "source": source, "targets": sorted(targets),
                                           "effective": "entries are applied longest-source-first in file order; the first matching entry wins in the prompt"})
    glossary: dict[str, dict] = {}
    if files.get("glossary") is not None:
        glossary, unsupported = _diagnose_glossary(decode(files["glossary"]))
        report["unsupported"].extend(unsupported)
        report["entry_counts"]["glossary"] = len(glossary)
    report["warnings"].extend(_conflicts(entries, glossary))
    for role in ROLES:
        if files.get(role) is not None:
            report["files"][role] = {"bytes": len(files[role]), "sha256": sha256_bytes(files[role])}
    for item in report["unsupported"]:
        item["id"] = unsupported_id(item)
    report["unsupported_ids"] = sorted(item["id"] for item in report["unsupported"])
    return report


def unsupported_id(item: dict) -> str:
    return f"{item['role']}:{item['line'] if item['line'] is not None else 'file'}:{sha256_text(item['text'])[:12]}"


# --------------------------------------------------------------------------- revisions

def dictionaries_dir(data_root: Path | str) -> Path:
    return Path(data_root) / "dictionaries"


def content_sha256(files: dict[str, bytes | None]) -> str:
    return signature({role: sha256_bytes(data) for role, data in files.items() if data is not None})


def find_revision(data_root: Path | str, content_hash: str) -> dict | None:
    root = dictionaries_dir(data_root)
    if not root.is_dir():
        return None
    for candidate in sorted(root.iterdir()):
        if not (candidate / "manifest.json").is_file():
            continue
        try:
            record = load_record("DictionaryRevision", candidate / "manifest.json")
        except ContractError:
            continue
        if record["content_sha256"] == content_hash:
            return record
    return None


def load_revision(data_root: Path | str, revision_id: str) -> dict:
    """Verified revision: {record, dir, diagnostics, paths (role -> Path)}."""
    directory = dictionaries_dir(data_root) / revision_id
    if not (directory / "manifest.json").is_file():
        raise ContractError("INPUT_MISSING", f"dictionary revision {revision_id} does not exist", details={"revision_id": revision_id})
    record = load_record("DictionaryRevision", directory / "manifest.json")
    verify_committed(directory, [item["artifact"] for item in record["files"]] + [record["diagnostics_ref"]])
    diagnostics = parse_json_text(safe_join(directory, record["diagnostics_ref"]["path"]).read_text(encoding="utf-8"))
    paths = {item["role"]: safe_join(directory, item["artifact"]["path"]) for item in record["files"]}
    return {"record": record, "dir": directory, "diagnostics": diagnostics, "paths": paths}


def create_revision(data_root: Path | str, inputs: dict[str, dict | None]) -> dict:
    """Copy the resolved files byte for byte into ``dictionaries/<revision_id>/`` (once per content). Returns load_revision()."""
    files = {role: (Path(item["path"]).read_bytes() if item else None) for role, item in inputs.items()}
    content_hash = content_sha256(files)
    existing = find_revision(data_root, content_hash)
    if existing is not None:
        return load_revision(data_root, existing["revision_id"])
    report = diagnose(files)
    revision_id = new_id()
    root = dictionaries_dir(data_root)
    root.mkdir(parents=True, exist_ok=True)
    pending = begin_pending(root)
    stored: dict[str, dict] = {}
    for role, item in inputs.items():
        if item is None:
            continue
        target = pending / "files" / STORED_NAMES[role]
        atomic_write_bytes(target, files[role])
        stored[role] = {"role": role, "original_name": Path(item["path"]).name, "origin": item["origin"]}
    atomic_write_text(pending / "diagnostics.json", render_json(report))

    def build_manifest(refs: list[dict]) -> dict:
        by_path = {ref["path"]: ref for ref in refs}
        return {
            "schema_version": SCHEMA_VERSION, "revision_id": revision_id, "content_sha256": content_hash,
            "files": [{**stored[role], "artifact": by_path[f"files/{STORED_NAMES[role]}"]} for role in ROLES if role in stored],
            "diagnostics_ref": by_path["diagnostics.json"], "created_at": utc_now(),
            "extensions": {"adapter_version": DICTIONARY_ADAPTER_VERSION, "unsupported_ids": report["unsupported_ids"], "unsupported_count": len(report["unsupported"]),
                           "warning_count": len(report["warnings"]), "entry_counts": report["entry_counts"]},
        }

    commit_pending(pending, root / revision_id, manifest_kind="DictionaryRevision", build_manifest=build_manifest)
    return load_revision(data_root, revision_id)


# --------------------------------------------------------------------------- acceptance of unsupported entries

def _acceptance_path(data_root: Path | str) -> Path:
    return Path(data_root) / ACCEPTANCE_FILE


def recorded_acceptance(data_root: Path | str, content_hash: str) -> list[str] | None:
    path = _acceptance_path(data_root)
    if not path.is_file():
        return None
    try:
        value = parse_json_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    entry = value.get("accepted", {}).get(content_hash) if isinstance(value, dict) else None
    return list(entry["ids"]) if isinstance(entry, dict) and isinstance(entry.get("ids"), list) else None


def record_acceptance(data_root: Path | str, content_hash: str, ids: list[str]) -> None:
    path = _acceptance_path(data_root)
    value: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "accepted": {}}
    if path.is_file():
        try:
            loaded = parse_json_text(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("accepted"), dict):
                value = loaded
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    value["accepted"][content_hash] = {"ids": sorted(ids), "accepted_at": utc_now()}
    atomic_write_text(path, render_json(value))


def prepare_for_request(data_root: Path | str, engine_settings: dict, *, accept_warnings: bool, defaults_dir: Path | None = None) -> dict:
    """Submit-time pre-check (plan 8.5): resolve files, create/reuse the revision, decide the acceptance.

    Returns {revision_id, content_sha256, accepted_ids, unsupported, warnings}. Unaccepted unsupported entries raise
    DICTIONARY_UNSUPPORTED listing file, line, original entry and reason.
    """
    inputs = resolve_inputs(engine_settings, defaults_dir=defaults_dir)
    loaded = create_revision(data_root, inputs)
    record, diagnostics = loaded["record"], loaded["diagnostics"]
    unsupported = diagnostics["unsupported"]
    ids = sorted(item["id"] for item in unsupported)
    previously = recorded_acceptance(data_root, record["content_sha256"])
    if ids and not accept_warnings and (previously is None or set(ids) - set(previously)):
        raise ContractError("DICTIONARY_UNSUPPORTED", f"{len(ids)} dictionary entr{'y is' if len(ids) == 1 else 'ies are'} not supported; review them and pass "
                            "--accept-dictionary-warnings to continue with the supported subset",
                            details={"revision_id": record["revision_id"], "unsupported": unsupported, "warnings": diagnostics["warnings"]})
    if ids and accept_warnings and (previously is None or set(ids) - set(previously)):
        record_acceptance(data_root, record["content_sha256"], ids)
    return {"revision_id": record["revision_id"], "content_sha256": record["content_sha256"], "accepted_ids": ids, "unsupported": unsupported,
            "warnings": diagnostics["warnings"], "files": {item["role"]: item["origin"] for item in record["files"]}}


# --------------------------------------------------------------------------- loading for translation

def load_for_translation(data_root: Path | str, revision_id: str, *, accepted_ids: list[str], settings: dict) -> dict:
    """Entries exactly as the legacy code would load them from the copied files, plus the effective prompt."""
    from subtitle_axis_align import read_text
    from voice_dictionary import NativeDictionaryEntry, effective_native_prompt, load_native_dictionary, normalize_glossary, resolved_term_target
    loaded = load_revision(data_root, revision_id)
    diagnostics = loaded["diagnostics"]
    missing = sorted(set(diagnostics["unsupported_ids"]) - set(accepted_ids))
    if missing:
        raise ContractError("DICTIONARY_UNSUPPORTED", "this request did not accept every unsupported dictionary entry of its dictionary revision",
                            details={"revision_id": revision_id, "unaccepted": [item for item in diagnostics["unsupported"] if item["id"] in missing]})
    paths = loaded["paths"]
    pre, _ = load_native_dictionary(paths["pre"]) if "pre" in paths else ([], 0)
    gpt, _ = load_native_dictionary(paths["gpt"], gpt=True) if "gpt" in paths else ([], 0)
    post, _ = load_native_dictionary(paths["post"]) if "post" in paths else ([], 0)
    glossary: dict[str, dict] = {}
    if "glossary" in paths:
        usable, _ = _diagnose_glossary(read_text(paths["glossary"]))
        glossary = normalize_glossary(usable)
    prompt = read_text(paths["style_prompt"]).strip() if "style_prompt" in paths else LEGACY_FALLBACK_PROMPT
    mode = prompt_mode(settings)
    if mode != "none":
        guideline = paths.get("guideline")
        if guideline is None:
            raise ContractError("INPUT_MISSING", "native_prompt_mode needs a guideline file in the dictionary revision", details={"revision_id": revision_id})
        prompt = effective_native_prompt(prompt, mode, guideline, paths.get("project_config") or Path("__not_configured__"))
    required = [NativeDictionaryEntry(source, resolved_term_target(source, "", glossary)) for source in glossary]
    return {"revision": loaded["record"], "pre": pre, "gpt": gpt, "post": post, "glossary": glossary, "required_glossary_entries": required, "prompt": prompt,
            "prompt_mode": mode, "content_sha256": loaded["record"]["content_sha256"], "diagnostics": diagnostics}


__all__ = ["DICTIONARY_ADAPTER_VERSION", "ROLES", "NATIVE_ROLES", "BUNDLED_NAMES", "STORED_NAMES", "bundled_defaults_dir", "prompt_mode", "resolve_inputs", "diagnose",
           "unsupported_id", "dictionaries_dir", "content_sha256", "find_revision", "load_revision", "create_revision", "recorded_acceptance", "record_acceptance",
           "prepare_for_request", "load_for_translation"]
