#!/usr/bin/env python3
"""Resource catalog: explicit tool/model bindings, identity hashes, capability probes (plan 1.1, sections 4.2, 6.2, 10.3, 12.2).

Interface (fixed by the plan):
    inspect_resources(config) -> CatalogReport dict
    resolve_resource(resource_id, report) -> {"resource_ref": ResourceRef, "absolute_path": str, "manifest": ResourceManifest}

Only version/help probes are executed (never a model run). A path change with identical content is a
re-binding, not a new resource; identical name with different content never hits an old cache.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from upgrade.contracts import (
    CAPABILITY_KEYS, SCHEMA_VERSION, ContractError, canonical_json, load_record, require_record, sha256_file, sha256_text, utc_now,
)
from upgrade.storage import atomic_write_text, render_json

HASH_CACHE_NAME = "settings/resource-hash-cache.json"
LARGE_FILE_BYTES = 64 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 20
PROBE_OUTPUT_LIMIT = 4000

# Capability keys that apply to each kind; everything else is not_applicable. Values start as "unknown"
# and only a probe result may turn them into true/false (plan 6.2: never 0/1 to fake a measurement).
KIND_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("cpu", "libass", "frame_pts"),
    "ffprobe": ("cpu", "json_probe", "frame_pts"),
    "asr_engine": ("cpu", "gpu_vulkan", "gpu_cuda", "word_timestamps", "vad"),
    "ocr_engine": ("cpu", "gpu_cuda", "ocr_scores", "frame_pts"),
    "translation_engine": ("cpu", "gpu_vulkan", "gpu_cuda"),
    "model": (),
    "font": (),
    "editor": (),
}
DEFAULT_VERSION_ARGS: dict[str, list[str]] = {
    "ffmpeg": ["-version"], "ffprobe": ["-version"], "asr_engine": ["--help"], "translation_engine": ["--version"],
    "ocr_engine": ["--help"], "editor": [], "model": [], "font": [],
}


def _hidden_kwargs() -> dict:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


# --------------------------------------------------------------------------- declarations

def resources_file_path(config: dict) -> Path:
    declared = Path(config["resources_file"])
    return declared if declared.is_absolute() else Path(config["data_root"]) / declared


def load_declarations(path: Path | str) -> dict[str, dict]:
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    record = load_record("ResourcesFile", file_path)
    declarations: dict[str, dict] = {}
    for resource_id, declaration in record["resources"].items():
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ContractError("SCHEMA_INVALID", "resource ids must be non-empty strings", details={"path": str(file_path)})
        declarations[resource_id] = require_record("ResourceDeclaration", _with_declaration_defaults(declaration), source=f"{file_path}#{resource_id}")
    return declarations


def _with_declaration_defaults(declaration: Any) -> dict:
    if not isinstance(declaration, dict):
        return declaration
    merged = {"files": [], "expected_content_sha256": None, "upstream_url": None, "upstream_revision": None,
              "license_status": "unresolved", "version_args": [], "extensions": {}}
    merged.update(declaration)
    if "version_args" not in declaration and merged.get("kind") in DEFAULT_VERSION_ARGS:
        merged["version_args"] = list(DEFAULT_VERSION_ARGS[merged["kind"]])
    return merged


def save_declarations(path: Path | str, declarations: dict[str, dict]) -> Path:
    for resource_id, declaration in declarations.items():
        require_record("ResourceDeclaration", declaration, source=resource_id)
    record = require_record("ResourcesFile", {"schema_version": SCHEMA_VERSION, "resources": declarations})
    return atomic_write_text(Path(path), render_json(record))


def declare_resource(kind: str, absolute_path: str | Path, *, files: list[str] | None = None, upstream_url: str | None = None,
                     upstream_revision: str | None = None, license_status: str = "unresolved", version_args: list[str] | None = None,
                     expected_content_sha256: str | None = None) -> dict:
    declaration = {
        "kind": kind, "absolute_path": str(absolute_path), "files": list(files or []), "expected_content_sha256": expected_content_sha256,
        "upstream_url": upstream_url, "upstream_revision": upstream_revision, "license_status": license_status,
        "version_args": list(version_args) if version_args is not None else list(DEFAULT_VERSION_ARGS.get(kind, [])), "extensions": {},
    }
    return require_record("ResourceDeclaration", declaration)


# --------------------------------------------------------------------------- hashing with a metadata cache

class HashCache:
    """Full SHA-256 once per large file; re-hash when size/mtime change. Metadata is an accelerator, not proof (plan 10.3)."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0
        if path.is_file():
            try:
                from upgrade.contracts import parse_json_text
                loaded = parse_json_text(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                    self.entries = loaded["entries"]
            except (OSError, ValueError, UnicodeDecodeError):
                self.entries = {}

    def sha256(self, file_path: Path) -> str:
        stat = file_path.stat()
        key = str(file_path.resolve())
        cached = self.entries.get(key)
        if stat.st_size >= LARGE_FILE_BYTES and cached and cached.get("bytes") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns:
            self.hits += 1
            return cached["sha256"]
        self.misses += 1
        digest = sha256_file(file_path)
        if stat.st_size >= LARGE_FILE_BYTES:
            self.entries[key] = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest, "hashed_at": utc_now()}
        return digest

    def save(self) -> None:
        atomic_write_text(self.path, render_json({"schema_version": SCHEMA_VERSION, "entries": self.entries}))


# --------------------------------------------------------------------------- probes

def _probe(executable: Path, args: list[str]) -> dict[str, Any]:
    """Run only a version/help probe; return exit code + first output lines. Never raises."""
    try:
        completed = subprocess.run([str(executable), *args], capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, **_hidden_kwargs())
        text = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")
        return {"ok": True, "exit_code": completed.returncode, "output": text[:PROBE_OUTPUT_LIMIT]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exit_code": None, "output": "", "error": str(exc)}


def _capabilities_for(kind: str, probe: dict | None) -> dict:
    caps: dict[str, Any] = {key: "not_applicable" for key in CAPABILITY_KEYS}
    for key in KIND_CAPABILITIES.get(kind, ()):
        caps[key] = "unknown"
    if not probe or not probe.get("ok"):
        return caps
    output = probe.get("output", "")
    lowered = output.lower()
    ran = probe.get("exit_code") == 0
    if kind in ("ffmpeg", "ffprobe"):
        caps["cpu"] = ran
        if "configuration:" in lowered:
            caps["libass"] = ("--enable-libass" in lowered) if kind == "ffmpeg" else "not_applicable"
        if kind == "ffprobe":
            caps["json_probe"] = ran
    elif kind == "asr_engine":
        caps["cpu"] = ran if ran else "unknown"
        if "--vad" in lowered or "vad" in lowered.split():
            caps["vad"] = True
        if "-ojf" in lowered or "word" in lowered and "timestamp" in lowered:
            caps["word_timestamps"] = True
    elif kind == "translation_engine":
        caps["cpu"] = ran if ran else "unknown"
    elif kind == "ocr_engine":
        caps["cpu"] = ran if ran else "unknown"
    return caps


def _version_text(probe: dict | None) -> str | None:
    if not probe or not probe.get("ok"):
        return None
    for line in probe.get("output", "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return None


# --------------------------------------------------------------------------- inspection

def _resource_files(declaration: dict, root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    if declaration["files"]:
        return [root / relative for relative in declaration["files"]]
    return sorted(p for p in root.iterdir() if p.is_file())


def inspect_resources(config: dict, *, declarations: dict[str, dict] | None = None, probe: bool = True) -> dict:
    """Inspect every declared resource. Missing/changed resources become structured problems; nothing is started.

    Returns a CatalogReport: manifests (resource_id -> ResourceManifest), problems (ErrorObject list) and a hash-cache note.
    """
    resources_path = resources_file_path(config)
    declarations = declarations if declarations is not None else load_declarations(resources_path)
    cache = HashCache(Path(config["data_root"]) / HASH_CACHE_NAME)
    manifests: dict[str, dict] = {}
    problems: list[dict] = []
    for resource_id, declaration in declarations.items():
        root = Path(declaration["absolute_path"])
        if not root.exists():
            problems.append(ContractError("RESOURCE_MISSING", f"resource {resource_id!r} is not at its declared path",
                                          details={"resource_id": resource_id, "absolute_path": str(root), "kind": declaration["kind"]}).to_dict())
            continue
        file_paths = _resource_files(declaration, root)
        missing = [str(p) for p in file_paths if not p.is_file()]
        if missing or not file_paths:
            problems.append(ContractError("RESOURCE_MISSING", f"resource {resource_id!r} is missing declared files",
                                          details={"resource_id": resource_id, "absolute_path": str(root), "missing_files": missing}).to_dict())
            continue
        files = []
        for file_path in file_paths:
            relative = file_path.name if root.is_file() else file_path.relative_to(root).as_posix()
            files.append({"path": relative, "sha256": cache.sha256(file_path), "bytes": file_path.stat().st_size})
        files.sort(key=lambda item: item["path"])
        content_sha256 = sha256_text(canonical_json([{"path": f["path"], "sha256": f["sha256"]} for f in files]))
        expected = declaration.get("expected_content_sha256")
        extensions: dict[str, Any] = dict(declaration.get("extensions") or {})
        if expected and expected != content_sha256:
            problems.append(ContractError("RESOURCE_CHANGED", f"resource {resource_id!r} content differs from its registered identity",
                                          details={"resource_id": resource_id, "expected": expected, "observed": content_sha256}).to_dict())
            extensions["identity"] = "changed"
        elif expected:
            extensions["identity"] = "verified"
        else:
            extensions["identity"] = "first_inspection"
        probe_result = None
        executable = root if root.is_file() else None
        if probe and executable and declaration["kind"] in ("ffmpeg", "ffprobe", "asr_engine", "translation_engine", "ocr_engine") and executable.suffix.lower() in (".exe", "") and declaration["version_args"]:
            probe_result = _probe(executable, declaration["version_args"])
            extensions["probe"] = {"args": declaration["version_args"], "exit_code": probe_result.get("exit_code"), "ok": probe_result.get("ok")}
        manifest = {
            "schema_version": SCHEMA_VERSION, "resource_id": resource_id, "kind": declaration["kind"], "absolute_path": str(root),
            "version_text": _version_text(probe_result), "upstream_url": declaration.get("upstream_url"),
            "upstream_revision": declaration.get("upstream_revision"), "content_sha256": content_sha256, "files": files,
            "capabilities": _capabilities_for(declaration["kind"], probe_result), "license_status": declaration["license_status"],
            "inspected_at": utc_now(), "extensions": extensions,
        }
        manifests[resource_id] = require_record("ResourceManifest", manifest, source=resource_id)
    cache.save()
    report = {
        "schema_version": SCHEMA_VERSION, "inspected_at": utc_now(), "resources_file": str(resources_path), "manifests": manifests,
        "problems": problems,
        "hash_cache_note": f"files >= {LARGE_FILE_BYTES} bytes reuse a cached SHA-256 while size and mtime are unchanged "
                           f"(hits={cache.hits}, misses={cache.misses}); metadata cannot detect edits that keep size and mtime.",
    }
    return require_record("CatalogReport", report)


def manifest_sha256(manifest: dict) -> str:
    return sha256_text(canonical_json({key: value for key, value in manifest.items() if key != "inspected_at"}))


def resolve_resource(resource_id: str, report: dict) -> dict:
    manifest = report["manifests"].get(resource_id)
    if manifest is not None and manifest.get("extensions", {}).get("identity") == "changed":
        raise ContractError("RESOURCE_CHANGED", f"resource {resource_id!r} content differs from its registered identity; re-register it explicitly",
                            details={"resource_id": resource_id, "observed": manifest["content_sha256"]})
    if manifest is None:
        matching = [p for p in report["problems"] if p.get("details", {}).get("resource_id") == resource_id]
        detail = matching[0] if matching else {"code": "RESOURCE_MISSING", "message": "resource is not declared"}
        raise ContractError(detail["code"] if detail["code"] in ("RESOURCE_MISSING", "RESOURCE_CHANGED") else "RESOURCE_MISSING",
                            f"cannot resolve resource {resource_id!r}: {detail['message']}", details={"resource_id": resource_id, **detail.get("details", {})})
    ref = require_record("ResourceRef", {"resource_id": resource_id, "content_sha256": manifest["content_sha256"], "manifest_sha256": manifest_sha256(manifest)})
    return {"resource_ref": ref, "absolute_path": manifest["absolute_path"], "manifest": manifest}


def require_resources(report: dict, resource_ids: list[str]) -> dict[str, dict]:
    """Resolve all ids or raise one RESOURCE_MISSING naming every missing/changed id (pre-check before any model start)."""
    resolved: dict[str, dict] = {}
    failures: list[dict] = []
    for resource_id in resource_ids:
        try:
            resolved[resource_id] = resolve_resource(resource_id, report)
        except ContractError as exc:
            failures.append({"resource_id": resource_id, "code": exc.code, "message": exc.message, **exc.details})
    if failures:
        raise ContractError("RESOURCE_MISSING", "required resources are missing or changed; no engine was started",
                            details={"failures": failures})
    return resolved


def register_resource(config: dict, resource_id: str, declaration: dict, *, declarations: dict[str, dict] | None = None) -> dict:
    """Add/replace a declaration, inspect it once and freeze expected_content_sha256. Saves resources.local.json."""
    path = resources_file_path(config)
    current = dict(declarations if declarations is not None else load_declarations(path))
    current[resource_id] = require_record("ResourceDeclaration", declaration)
    report = inspect_resources(config, declarations={resource_id: current[resource_id]}, probe=False)
    if resource_id not in report["manifests"]:
        raise ContractError("RESOURCE_MISSING", f"cannot register {resource_id!r}: {report['problems'][0]['message']}", details=report["problems"][0].get("details", {}))
    current[resource_id] = {**current[resource_id], "expected_content_sha256": report["manifests"][resource_id]["content_sha256"]}
    save_declarations(path, current)
    return current[resource_id]


def rebind_resource(config: dict, resource_id: str, new_path: str | Path, *, declarations: dict[str, dict] | None = None) -> dict:
    """Change only the local path of a known resource; accepted when the content identity is unchanged (plan 10.3)."""
    path = resources_file_path(config)
    current = dict(declarations if declarations is not None else load_declarations(path))
    if resource_id not in current:
        raise ContractError("RESOURCE_MISSING", f"resource {resource_id!r} is not declared", details={"resource_id": resource_id})
    candidate = {**current[resource_id], "absolute_path": str(new_path)}
    report = inspect_resources(config, declarations={resource_id: candidate}, probe=False)
    if report["problems"]:
        problem = report["problems"][0]
        raise ContractError(problem["code"] if problem["code"] in ("RESOURCE_MISSING", "RESOURCE_CHANGED") else "RESOURCE_MISSING",
                            f"cannot rebind {resource_id!r}: {problem['message']}", details=problem.get("details", {}))
    current[resource_id] = candidate
    save_declarations(path, current)
    return {"resource_id": resource_id, "absolute_path": str(new_path), "content_sha256": report["manifests"][resource_id]["content_sha256"], "rebound": True}


# --------------------------------------------------------------------------- legacy discovery as suggestions only

def suggest_declarations(*, magia_root: str | Path | None = None, voice_root: str | Path | None = None) -> dict[str, dict]:
    """Use the legacy tool_discovery locators to *suggest* declarations. Nothing is bound until the user registers it."""
    suggestions: dict[str, dict] = {}
    try:
        import tool_discovery  # legacy module on app/tools
    except ImportError:
        return suggestions
    if magia_root:
        magia = tool_discovery.discover_magia(magia_root)
        executable = magia["components"]["executable"]["path"]
        if executable:
            suggestions["magia-timeline"] = declare_resource("ocr_engine", executable, version_args=["--help"])
    if voice_root:
        voice = tool_discovery.discover_voice(voice_root)
        components = voice["components"]
        if components["whisper_executable"]["path"]:
            suggestions["whisper-cli"] = declare_resource("asr_engine", components["whisper_executable"]["path"], version_args=["--help"])
        if components["whisper_model"]["path"]:
            suggestions["whisper-model"] = declare_resource("model", components["whisper_model"]["path"])
        if components["vad_model"]["path"]:
            suggestions["whisper-vad-model"] = declare_resource("model", components["vad_model"]["path"])
        if components["llama_server"]["path"]:
            suggestions["llama-server"] = declare_resource("translation_engine", components["llama_server"]["path"], version_args=["--version"])
        if components["sakura_model"]["path"]:
            suggestions["sakura-model"] = declare_resource("model", components["sakura_model"]["path"])
    return suggestions


__all__ = [
    "HASH_CACHE_NAME", "LARGE_FILE_BYTES", "KIND_CAPABILITIES", "resources_file_path", "load_declarations", "save_declarations",
    "declare_resource", "HashCache", "inspect_resources", "manifest_sha256", "resolve_resource", "require_resources",
    "register_resource", "rebind_resource", "suggest_declarations",
]
