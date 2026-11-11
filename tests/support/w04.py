"""W04 fixtures: recognition evidence records, segment sets, dictionary files and the loopback model-server double.

The translation double replaces only the process launch (``translation.launch_spec``): a real child process serves real
HTTP on the loopback interface, so ownership, health polling, cancellation and the SSE reader run for real. It proves
the protocol, not the quality of any model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from upgrade import contracts as c
from upgrade.storage import artifact_ref, init_project_dir, render_json

FAKE_SERVER = Path(__file__).with_name("fake_llama_server.py")
REF = {"resource_id": "fixture", "content_sha256": "a" * 64, "manifest_sha256": "b" * 64}
BUNDLED_DEFAULTS = Path(__file__).resolve().parents[2] / "app" / "config"


# --------------------------------------------------------------------------- evidence

def speech_evidence(media_id: str, utterances: list[tuple[int, int, str]], words: dict[int, list[tuple[int, int, int, int]]] | None = None, *,
                    status: str = "usable", rejected_parents: tuple[int, ...] = ()) -> dict:
    parents = [{"id": f"asr-{i + 1:06d}", "start_ms": s, "end_ms": e, "text": t, "score": None, "score_kind": None,
                "flags": ["WORD_CLOCK_REJECTED"] if (i + 1) in rejected_parents else []} for i, (s, e, t) in enumerate(utterances)]
    rows = []
    for parent_index, entries in (words or {}).items():
        for cs, ce, ws, we in entries:
            rows.append({"parent_id": f"asr-{int(parent_index):06d}", "char_start": cs, "char_end": ce, "start_ms": ws, "end_ms": we, "raw_clock_ref": "fixture", "timing_valid": True})
    return c.require_record("SpeechEvidence", {
        "schema_version": 2, "evidence_id": c.new_id(), "media_id": media_id, "audio_artifact": {"path": "media/x/derived/asr/audio.wav", "sha256": "0" * 64, "bytes": 1, "media_type": "audio/wav"},
        "engine_ref": REF, "model_ref": REF, "language": "ja", "parents": parents, "word_times": rows, "processing_intervals": [{"start_ms": 0, "end_ms": 30000}],
        "activity_intervals": None, "coverage_status": "process_completed_only", "raw_refs": [], "extensions": {"word_timing_status": status, "asr_empty": not parents}})


def visual_evidence(media_id: str, events: list[tuple[int, int, str]]) -> dict:
    rows = [{"id": f"ocr-{i + 1:06d}", "ordinal": i, "start_ms": s, "end_ms": e, "text": t, "score": None, "score_kind": None, "boxes": None, "frame_refs": [],
             "boundary_evidence": "engine_interval", "flags": [] if t else ["ENGINE_PLACEHOLDER_TEXT"]} for i, (s, e, t) in enumerate(events)]
    return c.require_record("VisualEvidence", {
        "schema_version": 2, "evidence_id": c.new_id(), "media_id": media_id, "engine_ref": REF, "model_refs": [], "roi": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0},
        "events": rows, "raw_refs": [], "evidence_level": "interval_only", "extensions": {"score_status": "unknown", "frame_pts_status": "unknown", "visual_empty": not any(t for _, _, t in events)}})


# the recovered no-VAD native clocks of the legacy 8/11 slice (tests/subtitle_segments_test.py), used as the frozen v8 input
LEGACY_SPEECH = [(23600, 25680, "ねえこれ、速攻ホテル前"), (25680, 28060, "ある男子服だとさらにデカく見える")]
LEGACY_PAGES = [(23830, 24630, "ねえこれ…"), (24630, 25990, "ソッコーホテルマエ!"), (25990, 28360, "アルグン私服とらデカく見る")]
LEGACY_CLOCKS = {
    1: [(0, 1, 23660, 23780), (1, 2, 23780, 23890), (2, 4, 24010, 24330), (5, 6, 24510, 24690), (6, 7, 24690, 24870), (7, 8, 24870, 25030), (8, 9, 25090, 25170), (9, 10, 25250, 25410), (10, 11, 25410, 25610)],
    2: [(0, 2, 25740, 25970), (2, 3, 25970, 26100), (3, 4, 26130, 26250), (4, 5, 26250, 26390), (5, 6, 26390, 26530), (6, 7, 26530, 26670), (7, 8, 26670, 26810), (8, 9, 26810, 26950),
        (9, 10, 26950, 27090), (10, 11, 27090, 27230), (11, 12, 27230, 27370), (12, 13, 27370, 27510), (13, 14, 27510, 27650), (14, 16, 27650, 28060)],
}


# --------------------------------------------------------------------------- segment sets for translation tests

def segment_set(media_id: str, texts: list[str], *, revision_id: str | None = None, contexts: dict[int, str] | None = None) -> dict:
    """A machine-origin SegmentSet with one speech parent per cue (or a shared parent text given in ``contexts``)."""
    revision_id = revision_id or c.new_id()
    segments, parents = [], []
    for index, text in enumerate(texts, start=1):
        parent_text = (contexts or {}).get(index, text)
        parents.append({"id": f"asr-{index:06d}", "legacy_parent_id": f"speech_{index}", "index": index, "start_ms": index * 1000, "end_ms": index * 1000 + 800, "text": parent_text,
                        "word_count": 0, "flags": []})
        start = parent_text.find(text) if text in parent_text else 0
        segments.append({"cue_id": f"cue-{index:06d}", "legacy_cue_id": f"cue_{index}", "index": index, "start_ms": index * 1000, "end_ms": index * 1000 + 800, "source_text": text,
                         "role": "speech", "source_ranges": [{"parent_id": f"asr-{index:06d}", "char_start": start, "char_end": start + len(text)}], "visual_ids": [],
                         "timing_source": "speech_parent", "flags": []})
    return c.require_record("SegmentSet", {
        "schema_version": 2, "source_revision_id": revision_id, "media_id": media_id, "origin": "machine", "recognition_mode": "speech_only", "timing_mode": "speech",
        "status": "EMPTY" if not segments else "OK", "segments": segments, "parents": parents, "visual_events": [], "unresolved_visual_events": [],
        "coverage": {"status": "measured", "parents": []}, "extensions": {}})


def write_segment_set(project: Path, segment_set_record: dict) -> dict:
    """Store the set where a SourceRevision would (revisions/source/<id>/segments.json) and return the project-relative ref."""
    directory = project / "revisions" / "source" / segment_set_record["source_revision_id"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "segments.json"
    path.write_text(render_json(segment_set_record), encoding="utf-8", newline="\n")
    return artifact_ref(project, path)


# --------------------------------------------------------------------------- dictionaries

DICT_PRE = "// 前置词典\nデス\tです\n"
DICT_GPT = ("// GPT 词典\nラモーヌ\tラモーヌ [待确认]\nアルダン\t阿尔丹\n^^トレ\t训练\n1^さん\t先生\n"
            "pre_jp\t条件\tソース\t目标\n"          # conditional GalTransl rule: unsupported
            "只有一列\n"                            # malformed: no target
            "^^\t空源\n")                           # empty source after the mode marker: the parser drops it silently
DICT_POST = "// 后置词典\n马娘\t赛马娘\n"
GLOSSARY = {"ウマ娘": "赛马娘", "トレーナー": "训练员", "メジロアルダン": {"default": "目白阿尔丹", "possessive": "true"}}
STYLE_PROMPT = "你是日语视频字幕直译器。只输出中文译文正文。\n"


def write_dictionary_files(root: Path, *, gpt: str = DICT_GPT, glossary: dict | None = None) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = {"pre": root / "dict_pre.txt", "gpt": root / "dict_gpt.txt", "post": root / "dict_after.txt", "glossary": root / "translation_glossary.json", "style_prompt": root / "style.txt"}
    paths["pre"].write_text(DICT_PRE, encoding="utf-8", newline="\n")
    paths["gpt"].write_text(gpt, encoding="utf-8", newline="\n")
    paths["post"].write_text(DICT_POST, encoding="utf-8", newline="\n")
    paths["glossary"].write_text(json.dumps(GLOSSARY if glossary is None else glossary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    paths["style_prompt"].write_text(STYLE_PROMPT, encoding="utf-8", newline="\n")
    return paths


def dictionary_settings(paths: dict[str, Path], **translation) -> dict:
    return {"translation": {"batch_size": 10, **translation}, "dictionary": {role: str(path) for role, path in paths.items()}}


# --------------------------------------------------------------------------- model server double

def make_translation_resources(root: Path) -> dict[str, Path]:
    """Engine-shaped placeholders: the server 'executable' is never executed (launch_spec is patched), the model is a small file."""
    engine = root / "llama" / "llama-server.exe"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_bytes(b"fixture llama-server")
    model = root / "models" / "Sakura-Galtransl-7B-v3.7.gguf"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"fixture sakura model")
    set_behaviour(model)
    return {"llama-server": engine, "sakura-model": model}


def fake_launch(executable: Path, argv: list[str]) -> tuple[Path, list[str]]:
    """Replacement for ``translation.launch_spec``: run the loopback double with the same argument list."""
    return Path(sys.executable), ["-X", "utf8", str(FAKE_SERVER), *argv]


def set_behaviour(model: Path, *, responses: list[dict] | None = None, **options) -> None:
    model.with_suffix(".behaviour.json").write_text(json.dumps({"responses": responses or [], "default": "echo", "chunk": 7, **options}, ensure_ascii=False), encoding="utf-8")
    log = model.with_suffix(".requests.jsonl")
    if log.exists():
        log.unlink()


def requests_log(model: Path) -> list[dict]:
    log = model.with_suffix(".requests.jsonl")
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def resource(resource_id: str, path: Path) -> dict:
    return {"absolute_path": str(path), "resource_ref": {"resource_id": resource_id, "content_sha256": c.sha256_text(resource_id), "manifest_sha256": "b" * 64},
            "manifest": {"kind": "translation_engine" if "server" in resource_id else "model"}}


def new_project(root: Path, name: str = "p") -> Path:
    return init_project_dir(root, name)


__all__ = ["FAKE_SERVER", "REF", "BUNDLED_DEFAULTS", "speech_evidence", "visual_evidence", "LEGACY_SPEECH", "LEGACY_PAGES", "LEGACY_CLOCKS", "segment_set", "write_segment_set",
           "DICT_PRE", "DICT_GPT", "DICT_POST", "GLOSSARY", "STYLE_PROMPT", "write_dictionary_files", "dictionary_settings", "make_translation_resources", "fake_launch",
           "set_behaviour", "requests_log", "resource", "new_project"]
