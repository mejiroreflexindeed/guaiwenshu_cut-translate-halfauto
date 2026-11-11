#!/usr/bin/env python3
"""Quality checks (plan 1.1, section 11): one read-only function, ``check_quality(QualityInput) -> QualityReport``.

Nothing here repairs, merges, deletes, shifts or clamps a subtitle (plan 11.1): a rule only reports what it measured,
with the file line or cue it is about. ERROR means the structure or identity cannot hold and the revision must not be
committed/exported as it is; WARNING is suspicious content left to a person; INFO states something unknown or
informational. The function returns the report record and writes nothing; the caller that owns the commit stores it.

The rule table is plan 11.2 (QC01-QC23) with the thresholds of ``AppConfig.quality``; a threshold set to null turns
its soft hint off, the hard checks cannot be turned off. Rules that need recognition evidence, the source segments or
the translation records read them from ``QualityInput.extensions["loaded"]`` (already loaded by the caller, this
module opens no file). What could not be judged is said so (``unknown_capabilities`` and the rule scope), never
filled in. Messages are written for the person who reads the review report, in Chinese; they never contain a user path.

The review deliverables of plan 11.3 are rendered here as bytes (``render_review_csv`` / ``render_review_html``);
the caller that owns the commit stores them. The CSV is a display file, not a way back into the program.
"""

from __future__ import annotations

import csv
import html
import io
import unicodedata
from collections import Counter
from typing import Any
from urllib.parse import quote

from upgrade.contracts import HUMAN_STATUSES, SCHEMA_VERSION, SCOPES, new_id, require_record, sha256_text, utc_now

BOUNDS_TOLERANCE_MS = {"srt": 1, "ass": 10}      # QC03: format quantisation only; the written value is kept
REQUIRED_SCOPES = {"srt": ("text", "timing"), "ass": ("text", "timing", "style")}   # plan 3.3: what REVIEWED needs
HARD_RULES = ("QC01_PARSE", "QC02_TIME", "QC03_BOUNDS", "QC04_IDENTITY", "QC05_COVERAGE", "QC06_ORDER", "QC17_TRANSLATION", "QC21_MEDIA_BINDING")
MAX_LISTED = 20                                    # ids listed inside one summary issue; the count is always exact
MAX_PAIR_ISSUES = 500                              # QC09/QC13 itemise at most this many pairs; the summary carries the exact total
QUALITY_RULES_VERSION = 2                          # implementation version of this rule table (W04/W05 gate = 1)

# Score kinds a threshold is registered for (plan 11.2 QC10/QC11). Any other kind is reported as unknown, never thresholded.
OCR_SCORE_KINDS = ("ocr_confidence_0_1",)          # a 0..1 value where higher means more reliable; raw, not calibrated
ASR_SCORE_KIND = "whisper_token_probability"
SOURCE_CONFLICT_FLAGS = ("TEXT_CONFLICT", "TEXT_ALIGNMENT_AMBIGUOUS", "VISUAL_MAPPING_UNRESOLVED")
WORD_CLOCK_FLAGS = ("WORD_CLOCK_REJECTED", "WORD_TIMING_MISSING", "WORD_TIMING_GAPS", "NATIVE_TIME_COORDINATES_CONFLICT")
WORD_CLOCK_TIMING_SOURCES = ("speech_parent_unresolved", "speech_forced_boundaries")
UNCERTAIN_SEGMENT_FLAGS = ("AMBIGUOUS_TEXT_OCCURRENCE", "UNMAPPED_SPEECH", "VISUAL_TEXT_MISSING", "VISUAL_ONLY")
TERM_FLAGS = ("TRANSLATION_TERM_MISSING", "TRANSLATION_JAPANESE_PRESENT")
ROLE_LABELS = {"speech": "语音", "screen": "画面文字", "mixed": "语音+画面", "unknown": "未知"}

# rule id -> (short title, what the rule means); shown in review.html (plan 11.3 "规则说明")
RULES = {
    "QC01_PARSE": ("无法完整解析", "字幕块或事件无法完整解析、编码未知，或 Dialogue 字段不足。原文件按原字节保留。"),
    "QC02_TIME": ("时间不成立", "开始时间小于 0、结束不晚于开始，或时间不是规范的整数毫秒。程序不补时长，也不取绝对值。"),
    "QC03_BOUNDS": ("超出视频时长", "字幕时间超出所绑定视频的范围（SRT 容差 1 毫秒；ASS 容差 10 毫秒，只用于格式量化）。不会钳到片尾，也不削去文字。"),
    "QC04_IDENTITY": ("身份不一致", "内部编号重复、译文与原文版本不匹配、原文哈希或输出清单不一致。陈旧缓存和错配会被拒绝。"),
    "QC05_COVERAGE": ("原文覆盖不完整", "机器分段后，识别原文里有非空白字符没有归属到任何字幕，或被重复归属。需要修分段，不能靠删原文通过。"),
    "QC06_ORDER": ("编号或顺序", "机器字幕的编号不连续或没有按时间排序属于错误；人工文件的编号、顺序变化只作说明，原文件保留。"),
    "QC07_DURATION": ("显示时长提示", "非空字幕的显示时间短于或长于提示值（默认 500 毫秒 / 10000 毫秒）。只是提示，不是硬限制。"),
    "QC08_SPEED": ("阅读速度提示", "可见非空白字符数 ÷ 显示秒数超过提示值（默认 15）。ASS 标签不计入字符；程序不缩句、不截字。"),
    "QC09_OVERLAP": ("同时显示", "两条字幕的时间有交集。同角色同层给出提示；不同角色或不同层的同时显示属正常情况，只作说明。不会因为重叠而删除字幕。"),
    "QC10_OCR_SCORE": ("画面文字识别分数偏低", "识别后端给出了 0—1 的可信分数且低于提示值（默认 0.80）。分数未经校准，只作原始参考。"),
    "QC11_ASR_SCORE": ("语音识别分数偏低", "Whisper 词元概率低于提示值（默认 0.50）。只在后端真实给出这个字段时才判断。"),
    "QC12_SCORE_UNKNOWN": ("没有可解释的识别分数", "这次识别没有提供可解释的分数。按后端汇总说明一次，不给每条字幕编造分数。"),
    "QC13_OCR_CONTINUITY": ("画面文字可能闪烁或逐字出现", "相邻两段画面文字相同或互为前缀，且间隔不超过提示值（默认 200 毫秒）。不自动合并，可导出边界帧查看。"),
    "QC14_SOURCE_CONFLICT": ("画面文字与语音不一致", "画面文字（OCR）和语音识别（ASR）对不上，或对应关系有歧义。两栏并列显示，程序不判断谁正确。"),
    "QC15_WORD_CLOCK": ("字词时间不可靠", "语音识别的原生字词时间被拒、缺失，或用整句时间代替了精确字词时间。显示实际采用的时间来源。"),
    "QC16_ACTIVITY_GAP": ("可能漏识别的语音段", "语音活动区间内有不短于提示值（默认 1000 毫秒）的一段没有语音字幕覆盖。没有活动证据时只说明未知，不宣称检测完备。"),
    "QC17_TRANSLATION": ("译文不完整", "译文与原文的行或编号不一致、响应未结束，或非空原文对应了空译文。不接收半成品。"),
    "QC18_TERMS": ("译名和术语提示", "词典要求的译名没有出现，或译文里残留日文假名。沿用现有词典和例外表；程序不在句尾补词。"),
    "QC19_UNCERTAIN": ("不确定内容", "带有不确定标记的字幕，或没有对应到语音的画面文字。展示源文供人工判断；程序不自动续写。"),
    "QC20_REVIEW_BASE": ("人工稿与基础版本", "人工稿的基础版本之后有更新、来源映射不完整，或编辑器改了样式表。人工稿保持可用，不自动变基或合并。"),
    "QC21_MEDIA_BINDING": ("媒体绑定", "字幕绑定的视频与要导出的视频不是同一个身份。需要登记新媒体并重新绑定，不能按同名文件认可。"),
    "QC22_FONT": ("字体", "导出视频所需的字体或字形缺失（导出视频时为错误；只导出字幕时为提示）。不会静默换字体。"),
    "QC23_EMPTY": ("没有字幕内容", "识别结果或源字幕为空。标记为空结果并保留报告，不生成占位字幕。"),
}
RULE_IDS = tuple(RULES)


# --------------------------------------------------------------------------- issues and human status

def make_issue(revision_id: str, rule_id: str, severity: str, message: str, *, recommended: str, cue_ids: list[str] | None = None, start_ms: int | None = None,
               end_ms: int | None = None, source_refs: list[str] | None = None, observed: Any = None, threshold: Any = None, verification: str = "measured") -> dict:
    return require_record("Issue", {
        "issue_id": new_id(), "rule_id": rule_id, "severity": severity, "revision_id": revision_id, "cue_ids": cue_ids or [],
        "start_ms": start_ms if isinstance(start_ms, int) and start_ms >= 0 else None, "end_ms": end_ms if isinstance(end_ms, int) and end_ms >= 0 else None,
        "source_refs": source_refs or [], "observed": observed if observed is not None else {}, "threshold": threshold, "message": message, "recommended_action": recommended,
        "verification": verification})


def confirmation(revision_id: str, subtitle_sha256: str, media_id: str | None, fmt: str, attestations: list[dict], *, default: str) -> dict:
    """Human status of one subtitle file: only attestations for exactly these bytes and this media count (plan 6.6)."""
    matching = [item for item in attestations if item["revision_id"] == revision_id and item["subtitle_sha256"] == subtitle_sha256
                and (media_id is None or item["media_id"] == media_id)]
    confirmed = [scope for scope in SCOPES if any(scope in item["scopes"] for item in matching)]
    if not matching:
        status = default
    elif all(scope in confirmed for scope in REQUIRED_SCOPES[fmt]):
        status = "REVIEWED"
    else:
        status = "PARTIAL"
    assert status in HUMAN_STATUSES
    return {"subtitle_sha256": subtitle_sha256, "human_status": status, "confirmed_scopes": confirmed,
            "attestation_ids": sorted(item["attestation_id"] for item in matching)}


# --------------------------------------------------------------------------- rules on a parsed view

def _timed(cue: dict) -> bool:
    return isinstance(cue["start_ms"], int) and isinstance(cue["end_ms"], int) and cue["end_ms"] > cue["start_ms"]


def _dialogues(view: dict) -> list[dict]:
    return [cue for cue in view["cues"] if cue["kind"] == "dialogue"]


def _listed(values: list) -> list:
    return values[:MAX_LISTED]


def _parse_and_time(revision_id: str, view: dict) -> list[dict]:
    by_ordinal = {cue["ordinal"]: cue for cue in view["cues"]}
    issues = []
    for problem in view["problems"]:
        cue = by_ordinal.get(problem.get("ordinal"))
        rule_id = problem["rule_id"]
        recommended = ("在编辑器里修正这条字幕的起止时间后重新保存并再次登记；程序不会补时长，也不会取绝对值" if rule_id == "QC02_TIME"
                       else "在编辑器里修正这一处后重新保存并再次登记；原文件已按原字节保留，没有被改写")
        issues.append(make_issue(revision_id, rule_id, "ERROR", f"第 {problem['line']} 行：{problem['message']}", recommended=recommended,
                                 cue_ids=[cue["cue_id"]] if cue else [], start_ms=cue["start_ms"] if cue else None, end_ms=cue["end_ms"] if cue else None,
                                 source_refs=[f"line:{problem['line']}"], observed=problem.get("observed")))
    return issues


def _bounds(revision_id: str, view: dict, media_record: dict, tolerance_ms: int) -> list[dict]:
    duration_ms = media_record["duration_ms"]
    issues = []
    for cue in _dialogues(view):
        if not _timed(cue) or cue["end_ms"] <= duration_ms:
            continue
        beyond = cue["end_ms"] - duration_ms
        observed = {"end_ms": cue["end_ms"], "media_duration_ms": duration_ms, "beyond_ms": beyond}
        if beyond > tolerance_ms:
            issues.append(make_issue(revision_id, "QC03_BOUNDS", "ERROR", f"第 {cue['line']} 行：字幕结束时间超出视频时长 {beyond} 毫秒",
                                     recommended="确认这份字幕对应的是这个视频；若视频剪辑过，请先把剪辑后的视频登记为新媒体。程序不会把时间钳到片尾，也不会削去文字",
                                     cue_ids=[cue["cue_id"]], start_ms=cue["start_ms"], end_ms=cue["end_ms"], source_refs=[f"line:{cue['line']}"], observed=observed,
                                     threshold={"tolerance_ms": tolerance_ms}))
        else:
            issues.append(make_issue(revision_id, "QC03_BOUNDS", "INFO", f"第 {cue['line']} 行：字幕结束时间比视频时长多 {beyond} 毫秒，在格式量化容差之内，原值保留",
                                     recommended="无需处理", cue_ids=[cue["cue_id"]], start_ms=cue["start_ms"], end_ms=cue["end_ms"], source_refs=[f"line:{cue['line']}"],
                                     observed=observed, threshold={"tolerance_ms": tolerance_ms}))
    return issues


def _order(revision_id: str, view: dict, revision_kind: str, human_file: bool) -> list[dict]:
    severity = "INFO" if human_file else "ERROR"
    issues = []
    if view["format"] == "srt":
        labels = [cue["notes"].get("index_label") for cue in view["cues"]]
        odd = [cue["cue_id"] for position, cue in enumerate(view["cues"], start=1) if labels[position - 1] != str(position)]
        if odd:
            issues.append(make_issue(revision_id, "QC06_ORDER", severity,
                                     f"有 {len(odd)} 条字幕的编号不是从 1 起连续的顺序号" + ("（人工文件的编号只作展示，不作身份；原文件已原样保留）" if human_file else ""),
                                     recommended="无需处理" if human_file else "机器字幕的编号必须连续，需要修正生成逻辑", cue_ids=_listed(odd), observed={"count": len(odd)}))
    timed = [cue for cue in _dialogues(view) if _timed(cue)]
    unsorted = [cue["cue_id"] for previous, cue in zip(timed, timed[1:]) if cue["start_ms"] < previous["start_ms"]]
    if unsorted:
        issues.append(make_issue(revision_id, "QC06_ORDER", severity,
                                 f"有 {len(unsorted)} 条字幕没有按开始时间排在前一条之后" + ("（人工文件的顺序原样保留，不重排）" if human_file else ""),
                                 recommended="无需处理；需要时可在编辑器里排序后另存再登记" if human_file else "机器字幕必须按规范顺序输出，需要修正生成逻辑",
                                 cue_ids=_listed(unsorted), observed={"count": len(unsorted)}))
    return issues


def _review_base(revision_id: str, base: dict) -> list[dict]:
    issues = []
    newer = base.get("superseded_by") or []
    if newer:
        issues.append(make_issue(revision_id, "QC20_REVIEW_BASE", "INFO", f"这份人工稿基于的版本之后又产生了 {len(newer)} 个更新的机器版本；人工稿仍绑定原来的基础版本",
                                 recommended="可以继续使用这份人工稿；想用新的机器结果时，从新版本另建校对副本。程序不会自动变基或合并",
                                 observed={"code": "BASE_SUPERSEDED", "base_revision_id": base.get("base_revision_id"), "newer_revision_ids": _listed(list(newer))}))
    provenance = base.get("provenance_status")
    if provenance in ("partial", "unmapped"):
        counts = base.get("counts") or {}
        reason = "没有可对照的基础版本" if not base.get("base_revision_id") else "有字幕经过改字、改时间、拆分或合并"
        issues.append(make_issue(revision_id, "QC20_REVIEW_BASE", "INFO", f"来源映射不完整（{provenance}）：{reason}，这些字幕与源文的对应没有被审计",
                                 recommended="这不妨碍编辑、播放和导出；需要追溯源文时请人工对照", observed={"provenance_status": provenance, **counts}, verification="measured"))
    diff = base.get("style_diff")
    if diff:
        parts = [f"{label} {len(diff[key])} 个" for key, label in (("styles_added", "新增样式"), ("styles_removed", "删除样式"), ("styles_changed", "修改样式"),
                                                                  ("script_info_changed", "脚本信息变动")) if diff.get(key)]
        issues.append(make_issue(revision_id, "QC20_REVIEW_BASE", "WARNING", "样式表与基础版本不同：" + "，".join(parts) + "。程序分不清是有意改样式还是编辑器丢了样式",
                                 recommended="请对照下面列出的样式名确认；若不是有意修改，请从基础版本重新建立校对副本", observed=diff))
    return issues


def _empty(revision_id: str, view: dict) -> list[dict]:
    if any(cue["text"].strip() for cue in _dialogues(view)):
        return []
    return [make_issue(revision_id, "QC23_EMPTY", "WARNING", "这份字幕没有任何可显示的文字", recommended="确认导入的是正确的文件；程序不会生成占位字幕",
                       observed={"dialogue_events": len(_dialogues(view)), "events": len(view["cues"])})]


# --------------------------------------------------------------------------- the cue table (what each displayed cue is, and where it came from)

def visible_characters(text: str) -> int:
    """QC08: visible non-whitespace characters of a display text (ASS tags are already gone from a view's text)."""
    return sum(1 for char in unicodedata.normalize("NFC", text) if not char.isspace() and unicodedata.category(char) not in ("Cc", "Cf", "Mn", "Me"))


def comparison_text(text: str) -> str:
    """QC13: NFKC and no whitespace, used only to compare; the stored text is never changed."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _segment_source_ref(segment: dict) -> str:
    parts = [f"parent:{span['parent_id']}[{span['char_start']}:{span['char_end']}]" for span in segment.get("source_ranges", [])]
    parts += [f"visual:{visual_id}" for visual_id in segment.get("visual_ids", [])]
    return " ".join(parts)


def cue_table(item: dict) -> list[dict]:
    """One row per dialogue cue of the view: display text and times as written, plus role/layer/source where known.

    A machine revision takes role, source text and source reference from its own segments (same order as the file).
    A human file knows only what the caller could establish from the correspondence table (``extensions.cue_sources``);
    everything else stays ``unknown`` / empty rather than guessed.
    """
    view, extensions = item["parsed_view"], item["extensions"]
    loaded = extensions.get("loaded") or {}
    dialogues = _dialogues(view)
    segments = (loaded.get("segment_set") or {}).get("segments") or []
    records = {record["cue_id"]: record for record in (loaded.get("translation_set") or {}).get("records") or []}
    aligned = item["revision_kind"] == "machine" and len(segments) == len(dialogues)
    layers = extensions.get("cue_layers") if isinstance(extensions.get("cue_layers"), list) and len(extensions["cue_layers"]) == len(dialogues) else None
    sources = extensions.get("cue_sources") if isinstance(extensions.get("cue_sources"), dict) else {}
    is_source_file = item["revision_kind"] == "source"
    rows = []
    for position, cue in enumerate(dialogues):
        row = {"cue_id": cue["cue_id"], "line": cue["line"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "text": cue["text"], "timed": _timed(cue),
               "layer": cue["layer"] if cue["layer"] is not None else (layers[position] if layers else None), "style": cue["style"],
               "role": "unknown", "source_text": "", "translation_text": "", "source_ref": f"line:{cue['line']}", "provenance_status": "unmapped", "segment": None}
        if aligned:
            segment = segments[position]
            record = records.get(segment["cue_id"])
            row.update({"cue_id": segment["cue_id"], "role": segment["role"], "source_text": segment["source_text"], "translation_text": record["target_text"] if record else cue["text"],
                        "source_ref": _segment_source_ref(segment) or f"cue:{segment['cue_id']}", "provenance_status": "exact", "segment": segment})
        else:
            known = sources.get(cue["cue_id"]) or {}
            row.update({"role": known.get("role") or "unknown", "source_ref": known.get("source_ref") or row["source_ref"],
                        "provenance_status": known.get("provenance_status") or "unmapped"})
            if is_source_file:
                row.update({"source_text": cue["text"], "translation_text": known.get("translation_text") or ""})
            else:
                row.update({"source_text": known.get("source_text") or "", "translation_text": cue["text"]})
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- QC07 / QC08 / QC09: display hints on the cues as written

def _duration_and_speed(revision_id: str, rows: list[dict], config: dict) -> list[dict]:
    low, high, max_cps = config["min_duration_ms"], config["max_duration_ms"], config["max_cps"]
    issues = []
    for row in rows:
        if not row["timed"] or not row["text"].strip():
            continue
        duration = row["end_ms"] - row["start_ms"]
        common = {"cue_ids": [row["cue_id"]], "start_ms": row["start_ms"], "end_ms": row["end_ms"], "source_refs": [f"line:{row['line']}"]}
        if low is not None and duration < low:
            issues.append(make_issue(revision_id, "QC07_DURATION", "WARNING", f"这条字幕只显示 {duration} 毫秒，短于提示值 {low} 毫秒",
                                     recommended="看一下是否来得及读；需要时在编辑器里调整时间。这只是提示，不是硬限制", observed={"duration_ms": duration}, threshold={"min_duration_ms": low}, **common))
        if high is not None and duration > high:
            issues.append(make_issue(revision_id, "QC07_DURATION", "WARNING", f"这条字幕显示 {duration} 毫秒，长于提示值 {high} 毫秒",
                                     recommended="看一下是否停留过久或漏了切分；需要时在编辑器里调整。这只是提示，不是硬限制", observed={"duration_ms": duration}, threshold={"max_duration_ms": high}, **common))
        if max_cps is not None:
            characters = visible_characters(row["text"])
            if characters * 1000 > max_cps * duration:
                speed = round(characters * 1000 / duration, 2)
                issues.append(make_issue(revision_id, "QC08_SPEED", "WARNING", f"{characters} 个可见字符在 {duration} 毫秒内显示，约每秒 {speed} 个，高于提示值 {max_cps}",
                                         recommended="看一下是否来得及读；需要时由人工改写或调整时间。程序不会缩句或截字", observed={"visible_characters": characters, "duration_ms": duration, "characters_per_second": speed},
                                         threshold={"max_cps": max_cps}, **common))
    return issues


def _overlap(revision_id: str, rows: list[dict]) -> list[dict]:
    """QC09: report simultaneous display; nothing is removed. Same known role on the same layer is the warning case."""
    shown = sorted((row for row in rows if row["timed"] and row["text"].strip()), key=lambda row: (row["start_ms"], row["end_ms"]))
    issues, totals, listed, active = [], Counter(), Counter(), []
    for row in shown:
        active = [other for other in active if other["end_ms"] > row["start_ms"]]
        for other in active:
            start, end = max(other["start_ms"], row["start_ms"]), min(other["end_ms"], row["end_ms"])
            roles, layers = [other["role"], row["role"]], [other["layer"], row["layer"]]
            if layers[0] != layers[1]:
                kind, severity, verification, why = "different_layer", "INFO", "measured", f"在不同的层（{layers[0]} / {layers[1]}），属于正常的同时显示"
            elif "unknown" in roles:
                kind, severity, verification, why = "role_unknown", "WARNING", "unknown", "这份文件没有角色信息，程序分不清是同一路字幕相互重叠还是画面字与对白同时出现"
            elif roles[0] != roles[1]:
                kind, severity, verification, why = "different_role", "INFO", "measured", f"角色不同（{ROLE_LABELS[roles[0]]} / {ROLE_LABELS[roles[1]]}），属于正常的同时显示"
            else:
                kind, severity, verification, why = "same_role_same_layer", "WARNING", "measured", f"角色相同（{ROLE_LABELS[roles[0]]}）且在同一层"
            totals[kind] += 1
            if listed[severity] >= MAX_PAIR_ISSUES:
                continue
            listed[severity] += 1
            issues.append(make_issue(
                revision_id, "QC09_OVERLAP", severity, f"第 {other['line']} 行与第 {row['line']} 行同时显示 {end - start} 毫秒：{why}",
                recommended="两条都已原样输出；需要时在编辑器里调整时间或排版。程序不会因为重叠删除或移动字幕" if severity == "WARNING" else "无需处理",
                cue_ids=[other["cue_id"], row["cue_id"]], start_ms=start, end_ms=end, source_refs=[f"line:{other['line']}", f"line:{row['line']}"], verification=verification,
                observed={"kind": kind, "intersection_ms": end - start, "roles": roles, "layers": layers,
                          "first": {"cue_id": other["cue_id"], "start_ms": other["start_ms"], "end_ms": other["end_ms"], "text": other["text"]},
                          "second": {"cue_id": row["cue_id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": row["text"]}}))
        active.append(row)
    if sum(totals.values()) > len(issues):
        issues.append(make_issue(revision_id, "QC09_OVERLAP", "INFO", f"同时显示的字幕共有 {sum(totals.values())} 对，上面只逐条列出了 {len(issues)} 对",
                                 recommended="完整情况请在编辑器的时间轴上查看", observed={"pairs": dict(totals), "listed": len(issues)}))
    return issues


# --------------------------------------------------------------------------- QC04 / QC05 / QC17: identity, coverage, translation completeness (hard)

def _identity(revision_id: str, item: dict, loaded: dict) -> list[dict]:
    segment_set, translation_set = loaded.get("segment_set"), loaded.get("translation_set")
    media_record, source_revision, view = item["media_record"], item["source_revision"], item["parsed_view"]
    issues = []

    def error(message: str, observed: Any, cue_ids: list[str] | None = None) -> None:
        issues.append(make_issue(revision_id, "QC04_IDENTITY", "ERROR", message, recommended="这是程序内部的身份校验，不是可以手工放行的问题：请重新运行对应阶段；陈旧或错配的结果不会被接收",
                                 cue_ids=_listed(cue_ids or []), observed=observed))

    segments = (segment_set or {}).get("segments") or []
    if segment_set:
        repeated = sorted(cue_id for cue_id, count in Counter(segment["cue_id"] for segment in segments).items() if count > 1)
        if repeated:
            error(f"源字幕里有 {len(repeated)} 个内部编号重复出现", {"code": "DUPLICATE_SEGMENT_ID", "count": len(repeated)}, repeated)
        if media_record is not None and segment_set["media_id"] != media_record["media_id"]:
            error("源字幕绑定的媒体与这个版本的媒体不是同一个", {"code": "SEGMENT_MEDIA_MISMATCH", "segments_media_id": segment_set["media_id"], "media_id": media_record["media_id"]})
        if source_revision is not None and source_revision.get("revision_id") != segment_set["source_revision_id"]:
            error("源字幕版本与分段记录里登记的版本号不一致", {"code": "SOURCE_REVISION_MISMATCH", "manifest": source_revision.get("revision_id"), "segments": segment_set["source_revision_id"]})
    if translation_set:
        records = translation_set["records"]
        repeated = sorted(cue_id for cue_id, count in Counter(record["cue_id"] for record in records).items() if count > 1)
        if repeated:
            error(f"译文记录里有 {len(repeated)} 个内部编号重复出现", {"code": "DUPLICATE_TRANSLATION_ID", "count": len(repeated)}, repeated)
        if segment_set and translation_set["source_revision_id"] != segment_set["source_revision_id"]:
            error("译文是为另一个源字幕版本生成的", {"code": "TRANSLATION_SOURCE_MISMATCH", "translation": translation_set["source_revision_id"], "segments": segment_set["source_revision_id"]})
        foreign = [record["cue_id"] for record in records if record["source_revision_id"] != translation_set["source_revision_id"]]
        if foreign:
            error(f"有 {len(foreign)} 条译文记录登记的源字幕版本与整份译文不一致", {"code": "RECORD_SOURCE_MISMATCH", "count": len(foreign)}, foreign)
        by_id = {segment["cue_id"]: segment for segment in segments}
        stale = [record["cue_id"] for record in records if record["cue_id"] in by_id and record["source_text_sha256"] != sha256_text(by_id[record["cue_id"]]["source_text"])]
        if stale:
            error(f"有 {len(stale)} 条译文对应的原文已经不是现在这句（原文哈希不一致），属于陈旧结果", {"code": "SOURCE_TEXT_HASH_MISMATCH", "count": len(stale)}, stale)
    if item["revision_kind"] == "machine" and segment_set:
        dialogues = _dialogues(view)
        if len(dialogues) != len(segments):
            error(f"机器字幕文件有 {len(dialogues)} 条，而源字幕有 {len(segments)} 条", {"code": "CUE_COUNT_MISMATCH", "file": len(dialogues), "segments": len(segments)})
        else:
            slack = 0 if view["format"] == "srt" else BOUNDS_TOLERANCE_MS["ass"] - 1
            moved = [segment["cue_id"] for segment, cue in zip(segments, dialogues)
                     if not _timed(cue) or abs(cue["start_ms"] - segment["start_ms"]) > slack or abs(cue["end_ms"] - segment["end_ms"]) > slack]
            if moved:
                error(f"有 {len(moved)} 条机器字幕的时间与源字幕记录不一致", {"code": "CUE_TIME_MISMATCH", "count": len(moved)}, moved)
    return issues


def _coverage(revision_id: str, segment_set: dict) -> list[dict]:
    """QC05: every non-whitespace character of every recognised utterance belongs to exactly one machine cue."""
    parents = {parent["id"]: parent for parent in segment_set["parents"]}
    counts = {parent_id: [0] * len(parent["text"]) for parent_id, parent in parents.items()}
    issues, invalid = [], []
    for segment in segment_set["segments"]:
        for span in segment["source_ranges"]:
            text = parents.get(span["parent_id"], {}).get("text")
            if text is None or not 0 <= span["char_start"] < span["char_end"] <= len(text):
                invalid.append(segment["cue_id"])
                continue
            for position in range(span["char_start"], span["char_end"]):
                counts[span["parent_id"]][position] += 1
    if invalid:
        issues.append(make_issue(revision_id, "QC05_COVERAGE", "ERROR", f"有 {len(invalid)} 条字幕引用了不存在的识别句或越界的字符范围",
                                 recommended="需要重新生成源字幕分段；不能通过删改原文来通过检查", cue_ids=_listed(invalid), observed={"code": "SOURCE_RANGE_INVALID", "count": len(invalid)}))
    broken = []
    for parent_id, parent in parents.items():
        missing = [position for position, count in enumerate(counts[parent_id]) if count == 0 and not parent["text"][position].isspace()]
        repeated = [position for position, count in enumerate(counts[parent_id]) if count > 1]
        if missing or repeated:
            broken.append((parent, missing, repeated))
    for parent, missing, repeated in broken[:MAX_LISTED]:
        parts = ([f"有 {len(missing)} 个字符没有归属到任何字幕"] if missing else []) + ([f"有 {len(repeated)} 个字符被重复归属"] if repeated else [])
        issues.append(make_issue(revision_id, "QC05_COVERAGE", "ERROR", f"识别句“{parent['text']}”" + "，".join(parts),
                                 recommended="需要重新生成源字幕分段；不能通过删改原文来通过检查", start_ms=parent["start_ms"], end_ms=parent["end_ms"], source_refs=[f"parent:{parent['id']}"],
                                 observed={"parent_id": parent["id"], "missing_positions": _listed(missing), "missing_count": len(missing),
                                           "repeated_positions": _listed(repeated), "repeated_count": len(repeated)}))
    if len(broken) > MAX_LISTED:
        issues.append(make_issue(revision_id, "QC05_COVERAGE", "ERROR", f"共有 {len(broken)} 个识别句的原文覆盖不完整，上面只列出了前 {MAX_LISTED} 个",
                                 recommended="需要重新生成源字幕分段", observed={"code": "COVERAGE_SUMMARY", "parents": len(broken)}))
    return issues


def _translation(revision_id: str, segment_set: dict, translation_set: dict) -> list[dict]:
    """QC17: one finished translation per non-empty source cue, nothing else. Unfinished responses never reach a record
    (the translation stage refuses them); what can still be wrong here is a missing, extra or empty record."""
    segments = {segment["cue_id"]: segment for segment in segment_set["segments"]}
    records = {record["cue_id"]: record for record in translation_set["records"]}
    missing = [cue_id for cue_id, segment in segments.items() if segment["source_text"].strip() and cue_id not in records]
    empty = [cue_id for cue_id, record in records.items() if cue_id in segments and segments[cue_id]["source_text"].strip() and not record["target_text"].strip()]
    extra = [cue_id for cue_id in records if cue_id not in segments]
    issues = []
    for cue_ids, code, message in ((missing, "TRANSLATION_MISSING", "条源字幕没有译文"), (empty, "TRANSLATION_EMPTY", "条非空源字幕的译文是空的"),
                                   (extra, "TRANSLATION_EXTRA", "条译文不对应任何源字幕")):
        if cue_ids:
            issues.append(make_issue(revision_id, "QC17_TRANSLATION", "ERROR", f"有 {len(cue_ids)} {message}", recommended="需要重新运行翻译阶段；半成品译文不会被接收，也不会用占位文字补齐",
                                     cue_ids=_listed(cue_ids), observed={"code": code, "count": len(cue_ids)}))
    return issues


# --------------------------------------------------------------------------- QC10 / QC11 / QC12 / QC13: recognition scores and OCR continuity

def _cues_by_parent(segments: list[dict]) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for segment in segments:
        for span in segment["source_ranges"]:
            if segment["cue_id"] not in table.setdefault(span["parent_id"], []):
                table[span["parent_id"]].append(segment["cue_id"])
    return table


def _cues_by_visual(segments: list[dict]) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for segment in segments:
        for visual_id in segment["visual_ids"]:
            table.setdefault(visual_id, []).append(segment["cue_id"])
    return table


def _scores(revision_id: str, backend: str, items: list[dict], cues_of: dict[str, list[str]], config: dict) -> tuple[list[dict], bool]:
    """QC10 (backend ``ocr``) / QC11 (backend ``asr``) / QC12. Returns the issues and whether a score stayed unknown."""
    rule_id, key, label = ("QC10_OCR_SCORE", "ocr_probability_min", "画面文字识别") if backend == "ocr" else ("QC11_ASR_SCORE", "asr_probability_min", "语音识别")
    known_kinds = OCR_SCORE_KINDS if backend == "ocr" else (ASR_SCORE_KIND,)
    limit = config[key]
    issues, unknown, other_kinds = [], 0, Counter()
    for entry in items:
        score, kind = entry.get("score"), entry.get("score_kind")
        if score is None:
            unknown += 1
        elif kind not in known_kinds:
            unknown += 1
            other_kinds[str(kind)] += 1
        elif limit is not None and score < limit:
            issues.append(make_issue(revision_id, rule_id, "WARNING", f"{label}给这一句的原始分数是 {round(score, 4)}，低于提示值 {limit}：“{entry['text']}”",
                                     recommended="请对照画面或声音确认这句原文；分数未经校准，只说明这句值得先看", cue_ids=cues_of.get(entry["id"], []), start_ms=entry["start_ms"], end_ms=entry["end_ms"],
                                     source_refs=[f"{'visual' if backend == 'ocr' else 'parent'}:{entry['id']}"], observed={"score": score, "score_kind": kind, "calibrated": False, "text": entry["text"]},
                                     threshold={key: limit}))
    if unknown:
        detail = "；".join(f"{count} 条的分数类型是 {kind}，没有为它登记的提示值" for kind, count in sorted(other_kinds.items()))
        issues.append(make_issue(revision_id, "QC12_SCORE_UNKNOWN", "INFO", f"{label}：{len(items)} 条结果里有 {unknown} 条没有可解释的分数" + (f"（{detail}）" if detail else "") + "，不能据此判断哪些更可疑",
                                 recommended="没有分数不代表识别可靠，请照常人工检查", observed={"backend": backend, "items": len(items), "without_interpretable_score": unknown, "other_score_kinds": dict(other_kinds)},
                                 verification="unknown"))
    return issues, bool(unknown)


def _continuity(revision_id: str, events: list[dict], cues_of: dict[str, list[str]], config: dict) -> list[dict]:
    """QC13: neighbouring OCR events of the same region that look like one text flickering or appearing piece by piece."""
    gap_limit = config["ocr_continuity_gap_ms"]
    if gap_limit is None:
        return []
    ordered = sorted(events, key=lambda event: (event["start_ms"], event["end_ms"], event["ordinal"]))
    issues, total = [], 0
    for previous, following in zip(ordered, ordered[1:]):
        first, second = comparison_text(previous["text"]), comparison_text(following["text"])
        gap = following["start_ms"] - previous["end_ms"]
        if not first or not second or gap > gap_limit:
            continue
        if first == second:
            relation, what = "same_text", "文字相同，可能是同一段字幕闪烁了一下"
        elif first.startswith(second) or second.startswith(first):
            relation, what = "prefix", "一段是另一段的开头，可能是文字逐字出现"
        else:
            continue
        total += 1
        if len(issues) >= MAX_PAIR_ISSUES:
            continue
        cue_ids = list(dict.fromkeys(cues_of.get(previous["id"], []) + cues_of.get(following["id"], [])))
        issues.append(make_issue(
            revision_id, "QC13_OCR_CONTINUITY", "WARNING", f"相邻两段画面文字间隔 {gap} 毫秒，{what}：“{previous['text']}” → “{following['text']}”",
            recommended="两段都已原样保留，程序不会自动合并；可导出边界帧确认文字实际出现和消失的画面", cue_ids=cue_ids, start_ms=previous["start_ms"], end_ms=following["end_ms"],
            source_refs=[f"visual:{previous['id']}", f"visual:{following['id']}"], threshold={"ocr_continuity_gap_ms": gap_limit},
            observed={"relation": relation, "gap_ms": gap, "boundary_times_ms": sorted({previous["end_ms"], following["start_ms"]}),
                      "previous": {"id": previous["id"], "start_ms": previous["start_ms"], "end_ms": previous["end_ms"], "text": previous["text"]},
                      "following": {"id": following["id"], "start_ms": following["start_ms"], "end_ms": following["end_ms"], "text": following["text"]}}))
    if total > len(issues):
        issues.append(make_issue(revision_id, "QC13_OCR_CONTINUITY", "INFO", f"这类相邻画面文字共有 {total} 处，上面只逐条列出了 {len(issues)} 处", recommended="其余请在时间轴上查看",
                                 observed={"pairs": total, "listed": len(issues)}))
    return issues


# --------------------------------------------------------------------------- QC14 / QC15 / QC16: OCR against ASR, word clocks, speech activity

_CONFLICT_TEXT = {"TEXT_CONFLICT": "画面文字与语音识别的文字不同", "TEXT_ALIGNMENT_AMBIGUOUS": "画面文字与语音的对应位置有歧义", "VISUAL_MAPPING_UNRESOLVED": "画面文字没能对应到语音"}


def _source_columns(segment: dict, parents: dict, visual: dict) -> dict:
    """The two columns of QC14: what ASR heard and what OCR read for this cue, each with its own times, as recorded."""
    asr = [{"parent_id": span["parent_id"], "start_ms": parents[span["parent_id"]]["start_ms"], "end_ms": parents[span["parent_id"]]["end_ms"],
            "text": parents[span["parent_id"]]["text"][span["char_start"]:span["char_end"]]} for span in segment.get("source_ranges", []) if span["parent_id"] in parents]
    ocr = [{"visual_id": visual_id, "start_ms": visual[visual_id]["start_ms"], "end_ms": visual[visual_id]["end_ms"], "text": visual[visual_id]["text"]}
           for visual_id in segment.get("visual_ids", []) if visual_id in visual]
    return {"asr": asr, "ocr": ocr}


def _source_conflicts(revision_id: str, segment_set: dict) -> list[dict]:
    parents = {parent["id"]: parent for parent in segment_set["parents"]}
    visual = {event["id"]: event for event in segment_set["visual_events"]}
    issues = []
    for segment in segment_set["segments"]:
        flags = [flag for flag in SOURCE_CONFLICT_FLAGS if flag in segment["flags"]]
        if not flags:
            continue
        columns = _source_columns(segment, parents, visual)
        heard, read = " / ".join(entry["text"] for entry in columns["asr"]) or "（无）", " / ".join(entry["text"] for entry in columns["ocr"]) or "（无）"
        issues.append(make_issue(revision_id, "QC14_SOURCE_CONFLICT", "WARNING", "；".join(_CONFLICT_TEXT[flag] for flag in flags) + f"。语音（ASR）：“{heard}”；画面（OCR）：“{read}”",
                                 recommended="请对照视频决定以哪一边为准并在编辑器里改定；程序不判断谁正确，也不会把一边改成另一边", cue_ids=[segment["cue_id"]], start_ms=segment["start_ms"],
                                 end_ms=segment["end_ms"], source_refs=_segment_source_ref(segment).split(), observed={"flags": flags, **columns}))
    for event in segment_set["unresolved_visual_events"]:
        issues.append(make_issue(revision_id, "QC14_SOURCE_CONFLICT", "WARNING", f"这段画面文字没能对应到任何一句语音，没有进入字幕：“{event['text']}”",
                                 recommended="请对照视频确认这段画面文字是否需要字幕；需要时在编辑器里手工加入", start_ms=event["start_ms"], end_ms=event["end_ms"], source_refs=[f"visual:{event['id']}"],
                                 observed={"flags": list(event.get("flags", [])), "asr": [],
                                           "ocr": [{"visual_id": event["id"], "start_ms": event["start_ms"], "end_ms": event["end_ms"], "text": event["text"]}]}))
    return issues


def _word_clock(revision_id: str, segment_set: dict, speech: dict | None) -> list[dict]:
    extensions = (speech or {}).get("extensions") or {}
    evidence = {key: extensions.get(key) for key in ("word_timing_status", "rejected_word_clocks", "native_clock_recovery", "selected_attempt") if key in extensions}
    issues = []
    for segment in segment_set["segments"]:
        flags = [flag for flag in WORD_CLOCK_FLAGS if flag in segment["flags"]]
        if not flags and segment["timing_source"] not in WORD_CLOCK_TIMING_SOURCES:
            continue
        reason = ("语音识别给出的字词时间与整句时间冲突，已被拒用" if "WORD_CLOCK_REJECTED" in flags or "NATIVE_TIME_COORDINATES_CONFLICT" in flags
                  else "这一句缺少可用的字词时间" if "WORD_TIMING_MISSING" in flags or segment["timing_source"] == "speech_parent_unresolved" else "字词时间有缺口，切分点是按句子边界定的")
        issues.append(make_issue(revision_id, "QC15_WORD_CLOCK", "WARNING", f"{reason}；这条字幕的时间来源是 {segment['timing_source']}，起止时间可能不贴合实际发声",
                                 recommended="请在编辑器里对照波形检查这条字幕的起止时间；程序没有插值或猜测时间", cue_ids=[segment["cue_id"]], start_ms=segment["start_ms"], end_ms=segment["end_ms"],
                                 source_refs=_segment_source_ref(segment).split(), observed={"timing_source": segment["timing_source"], "flags": flags, **evidence}))
    return issues


def _activity(revision_id: str, speech: dict, config: dict) -> tuple[list[dict], bool]:
    """QC16. Without activity evidence the answer is "unknown" (returned as True), never "nothing was missed"."""
    activity = speech["activity_intervals"]
    if activity is None:
        return [make_issue(revision_id, "QC16_ACTIVITY_GAP", "INFO", "这次识别没有提供语音活动区间，程序不能判断是否有语音没被识别出来；识别跑完整段音频不等于所有语音都被识别",
                           recommended="如果担心漏句，请对照波形或视频通听一遍", observed={"coverage_status": speech["coverage_status"]}, verification="unknown")], True
    limit = config["activity_gap_ms"]
    if limit is None:
        return [], False
    covered = sorted((parent["start_ms"], parent["end_ms"]) for parent in speech["parents"])
    issues = []
    for interval in sorted(activity, key=lambda entry: (entry["start_ms"], entry["end_ms"])):
        cursor = interval["start_ms"]
        gaps = []
        for start, end in covered:
            if end <= cursor:
                continue
            if start >= interval["end_ms"]:
                break
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < interval["end_ms"]:
            gaps.append((cursor, interval["end_ms"]))
        for start, end in gaps:
            if end - start >= limit:
                issues.append(make_issue(revision_id, "QC16_ACTIVITY_GAP", "WARNING", f"语音活动区间内有 {end - start} 毫秒没有任何语音字幕覆盖，可能有话没被识别出来",
                                         recommended="请听一下这一段；需要时在编辑器里手工补字幕。程序不会自动补句", start_ms=start, end_ms=end,
                                         observed={"uncovered_ms": end - start, "activity_interval": [interval["start_ms"], interval["end_ms"]]}, threshold={"activity_gap_ms": limit}))
    return issues, False


# --------------------------------------------------------------------------- QC18 / QC19: terms and uncertain content (existing flags only, nothing new is detected)

_UNCERTAIN_TEXT = {"AMBIGUOUS_TEXT_OCCURRENCE": "这句文字在语音里出现了不止一次，对应到哪一处有歧义", "UNMAPPED_SPEECH": "这句语音没有对应到任何画面文字",
                   "VISUAL_TEXT_MISSING": "有画面文字记录但没有可用的画面文字页，这句只来自语音", "VISUAL_ONLY": "这条字幕只来自画面文字，没有对应到任何语音",
                   "TRANSLATION_UNCERTAIN": "译文里带有“[待确认]”标记"}
_HANDLED_FLAGS = frozenset(SOURCE_CONFLICT_FLAGS + WORD_CLOCK_FLAGS + UNCERTAIN_SEGMENT_FLAGS + TERM_FLAGS + ("TRANSLATION_UNCERTAIN", "CHECK"))


def _terms(revision_id: str, segments: dict, translation_set: dict, trace: dict | None) -> list[dict]:
    traces = (trace or {}).get("cues") or {}
    issues = []
    for record in translation_set["records"]:
        flags = [flag for flag in TERM_FLAGS if flag in record["flags"]]
        if not flags:
            continue
        segment, detail = segments.get(record["cue_id"]), traces.get(record["cue_id"]) or {}
        missing = list(detail.get("missing_terms") or [])
        parts = []
        if "TRANSLATION_TERM_MISSING" in flags:
            parts.append("词典要求的译名没有出现在译文里" + (f"：{'、'.join(missing)}" if missing else ""))
        if "TRANSLATION_JAPANESE_PRESENT" in flags:
            parts.append("译文里残留日文假名")
        issues.append(make_issue(revision_id, "QC18_TERMS", "WARNING", "；".join(parts) + f"。译文：“{record['target_text']}”",
                                 recommended="请人工改定这句译文，或在词典/例外表里登记后重新翻译；程序不会在句尾补词", cue_ids=[record["cue_id"]],
                                 start_ms=segment["start_ms"] if segment else None, end_ms=segment["end_ms"] if segment else None,
                                 observed={"flags": flags, "missing_terms": missing, "source_text": segment["source_text"] if segment else None, "target_text": record["target_text"]}))
    return issues


def _uncertain(revision_id: str, segment_set: dict | None, translation_set: dict | None) -> list[dict]:
    segments = (segment_set or {}).get("segments") or []
    by_id = {segment["cue_id"]: segment for segment in segments}
    mode_fact = (segment_set or {}).get("recognition_mode") == "visual_only"     # then VISUAL_ONLY describes the chosen mode, not a doubt about one cue
    flagged: dict[str, list[str]] = {}
    for holder in list(segments) + list((translation_set or {}).get("records") or []):
        flags = [flag for flag in holder["flags"] if flag in _UNCERTAIN_TEXT and not (flag == "VISUAL_ONLY" and mode_fact)]
        other = [flag for flag in holder["flags"] if flag not in _HANDLED_FLAGS]
        if "CHECK" in holder["flags"] and other:
            flags += other
        if flags:
            flagged.setdefault(holder["cue_id"], [])
            flagged[holder["cue_id"]] += [flag for flag in flags if flag not in flagged[holder["cue_id"]]]
    issues = []
    for cue_id, flags in flagged.items():
        segment = by_id.get(cue_id)
        text = "；".join(_UNCERTAIN_TEXT.get(flag, f"带有待检查标记 {flag}") for flag in flags)
        issues.append(make_issue(revision_id, "QC19_UNCERTAIN", "WARNING", text + (f"。源文：“{segment['source_text']}”" if segment else ""),
                                 recommended="请对照视频人工确认这条字幕；程序不会自动续写或补全", cue_ids=[cue_id], start_ms=segment["start_ms"] if segment else None,
                                 end_ms=segment["end_ms"] if segment else None, source_refs=_segment_source_ref(segment).split() if segment else [],
                                 observed={"flags": flags, "source_text": segment["source_text"] if segment else None}))
    if mode_fact:
        visual_only = [segment["cue_id"] for segment in segments if "VISUAL_ONLY" in segment["flags"]]
        if visual_only:
            issues.append(make_issue(revision_id, "QC19_UNCERTAIN", "INFO", f"这次用的是“只识别画面文字”模式：{len(visual_only)} 条字幕全部只来自画面文字，没有和语音对照。这是所选模式本身的结果，不逐条提示",
                                     recommended="画面文字的识别结果请照常人工检查", cue_ids=_listed(visual_only), observed={"code": "VISUAL_ONLY_MODE", "count": len(visual_only)}))
    return issues


# --------------------------------------------------------------------------- QC21 / QC22: checks that only exist when something is exported

def _media_binding(revision_id: str, media_record: dict, target_media_id: str) -> list[dict]:
    if target_media_id == media_record["media_id"]:
        return []
    return [make_issue(revision_id, "QC21_MEDIA_BINDING", "ERROR", "这份字幕绑定的视频与这次要导出的视频不是同一个", recommended="请把要导出的视频登记为新媒体，并把字幕重新登记到它上面；不能因为文件名相同就认可",
                       observed={"bound_media_id": media_record["media_id"], "target_media_id": target_media_id})]


def _fonts(revision_id: str, check: dict) -> list[dict]:
    """QC22 on a font check made by the exporter: ``{purpose: video|subtitles, missing_fonts: [...], missing_glyphs: [{font, characters}]}``."""
    severity = "ERROR" if check.get("purpose") == "video" else "WARNING"
    tail = "不能导出视频" if severity == "ERROR" else "字幕文件照常导出，但在没有这个字体的电脑上显示会不同"
    issues = []
    for name in check.get("missing_fonts") or []:
        issues.append(make_issue(revision_id, "QC22_FONT", severity, f"找不到字体“{name}”，{tail}", recommended="请安装或登记这个字体，或在编辑器里把样式改成已有的字体；程序不会悄悄换一个字体",
                                 observed={"code": "FONT_MISSING", "font": name, "purpose": check.get("purpose")}))
    for entry in check.get("missing_glyphs") or []:
        issues.append(make_issue(revision_id, "QC22_FONT", severity, f"字体“{entry['font']}”里缺少 {len(entry['characters'])} 个字幕用到的字，{tail}",
                                 recommended="请换用包含这些字的字体；程序不会悄悄换一个字体", observed={"code": "GLYPH_MISSING", "font": entry["font"], "characters": _listed(list(entry["characters"])),
                                                                                  "count": len(entry["characters"]), "purpose": check.get("purpose")}))
    return issues


def export_issues(revision_id: str, *, media_record: dict | None = None, target_media_id: str | None = None, font_check: dict | None = None) -> list[dict]:
    """QC21 / QC22 issues of one export (plan 11.2: only exports know the target media and the fonts that must exist)."""
    issues = []
    if media_record is not None and target_media_id is not None:
        issues += _media_binding(revision_id, media_record, target_media_id)
    if font_check is not None:
        issues += _fonts(revision_id, font_check)
    return issues


# --------------------------------------------------------------------------- interface

class _Scope:
    """Which rules were judged, which were not and why. A rule that was not judged is never shown as passed."""

    def __init__(self) -> None:
        self.evaluated: list[str] = []
        self.not_evaluated: dict[str, str] = {}
        self.disabled: dict[str, str] = {}

    def ran(self, *rule_ids: str) -> None:
        self.evaluated += [rule_id for rule_id in rule_ids if rule_id not in self.evaluated]

    def skipped(self, reason: str, *rule_ids: str) -> None:
        for rule_id in rule_ids:
            self.not_evaluated[rule_id] = reason

    def off(self, rule_id: str, key: str) -> None:
        self.disabled[rule_id] = f"质量设置里的 {key} 被设为 null，这条软提示已关闭"

    def record(self, config: dict) -> dict:
        for rule_id in list(self.not_evaluated) + list(self.disabled):
            if rule_id in self.evaluated:
                self.not_evaluated.pop(rule_id, None)
        return {"schema_version": SCHEMA_VERSION, "rules_version": QUALITY_RULES_VERSION, "rule_set_version": int(config["rule_set_version"]),
                "evaluated_rules": [rule_id for rule_id in RULE_IDS if rule_id in self.evaluated],
                "not_evaluated_rules": {rule_id: self.not_evaluated[rule_id] for rule_id in RULE_IDS if rule_id in self.not_evaluated},
                "disabled_rules": {rule_id: self.disabled[rule_id] for rule_id in RULE_IDS if rule_id in self.disabled},
                "hard_rules": list(HARD_RULES), "thresholds": dict(config)}


def evaluate(quality_input: dict) -> dict:
    """QualityInput -> ``{"report": QualityReport, "scope": {...}}``. Read-only: nothing is written, the input is not modified."""
    item = require_record("QualityInput", quality_input)
    revision_id, view, extensions, config = item["revision_id"], item["parsed_view"], item["extensions"], item["rule_config"]
    media_record, kind = item["media_record"], item["revision_kind"]
    loaded = extensions.get("loaded") if isinstance(extensions.get("loaded"), dict) else {}
    segment_set, translation_set = loaded.get("segment_set"), loaded.get("translation_set")
    speech, visual = loaded.get("speech_evidence"), loaded.get("visual_evidence")
    human_file = kind == "review" or extensions.get("origin") == "human"
    machine_source = bool(segment_set) and segment_set["origin"] == "machine"
    scope, unknown = _Scope(), []

    issues = _parse_and_time(revision_id, view)
    scope.ran("QC01_PARSE", "QC02_TIME", "QC06_ORDER", "QC09_OVERLAP", "QC23_EMPTY")
    if media_record is not None:
        tolerance = extensions.get("bounds_tolerance_ms")
        issues += _bounds(revision_id, view, media_record, BOUNDS_TOLERANCE_MS[view["format"]] if tolerance is None else int(tolerance))
        scope.ran("QC03_BOUNDS")
        if extensions.get("target_media_id"):
            issues += _media_binding(revision_id, media_record, extensions["target_media_id"])
            scope.ran("QC21_MEDIA_BINDING")
        else:
            scope.skipped("字幕已绑定到一个登记过的媒体；与导出目标媒体的核对只在导出时进行", "QC21_MEDIA_BINDING")
    else:
        unknown.append("media_bounds")
        scope.skipped("没有绑定媒体，无法核对时间是否落在视频之内", "QC03_BOUNDS")
        scope.ran("QC21_MEDIA_BINDING")
        issues.append(make_issue(revision_id, "QC21_MEDIA_BINDING", "INFO", "没有绑定媒体：这次只做了字幕结构检视，没有核对时间是否落在视频之内",
                                 recommended="正式登记和导出前必须选定一个已登记的媒体版本", verification="unknown"))
    issues += _order(revision_id, view, kind, human_file)
    rows = cue_table(item)

    if segment_set or translation_set:
        issues += _identity(revision_id, item, loaded)
        scope.ran("QC04_IDENTITY")
    else:
        scope.skipped("这个版本没有程序内部的分段或译文记录可核对；人工文件的身份在登记时按文件哈希和媒体绑定核对", "QC04_IDENTITY")
    if machine_source and segment_set["parents"]:
        issues += _coverage(revision_id, segment_set)
        scope.ran("QC05_COVERAGE")
    else:
        scope.skipped("没有语音识别原文可以核对覆盖（只识别画面文字、识别为空，或源字幕来自人工文件）", "QC05_COVERAGE")
    if segment_set and translation_set:
        issues += _translation(revision_id, segment_set, translation_set)
        scope.ran("QC17_TRANSLATION")
    else:
        scope.skipped("这个版本没有机器译文记录", "QC17_TRANSLATION")

    if config["min_duration_ms"] is None and config["max_duration_ms"] is None:
        scope.off("QC07_DURATION", "min_duration_ms / max_duration_ms")
    else:
        scope.ran("QC07_DURATION")
    scope.ran("QC08_SPEED") if config["max_cps"] is not None else scope.off("QC08_SPEED", "max_cps")
    issues += _duration_and_speed(revision_id, rows, config)
    issues += _overlap(revision_id, rows)

    segments = (segment_set or {}).get("segments") or []
    no_evidence = "这个版本没有随附这一路识别证据（没有运行这一路识别，或者是人工文件）"
    if visual is not None:
        found, unknown_scores = _scores(revision_id, "ocr", visual["events"], _cues_by_visual(segments), config)
        issues += found
        if unknown_scores or (visual.get("extensions") or {}).get("score_status") == "unknown":
            unknown.append("ocr_scores")
        if (visual.get("extensions") or {}).get("frame_pts_status") == "unknown":
            unknown.append("frame_pts")
        scope.ran("QC10_OCR_SCORE") if config["ocr_probability_min"] is not None else scope.off("QC10_OCR_SCORE", "ocr_probability_min")
        scope.ran("QC12_SCORE_UNKNOWN")
        issues += _continuity(revision_id, visual["events"], _cues_by_visual(segments), config)
        scope.ran("QC13_OCR_CONTINUITY") if config["ocr_continuity_gap_ms"] is not None else scope.off("QC13_OCR_CONTINUITY", "ocr_continuity_gap_ms")
    else:
        scope.skipped(no_evidence, "QC10_OCR_SCORE", "QC13_OCR_CONTINUITY")
    if speech is not None:
        found, unknown_scores = _scores(revision_id, "asr", speech["parents"], _cues_by_parent(segments), config)
        issues += found
        if unknown_scores:
            unknown.append("asr_scores")
        scope.ran("QC11_ASR_SCORE") if config["asr_probability_min"] is not None else scope.off("QC11_ASR_SCORE", "asr_probability_min")
        scope.ran("QC12_SCORE_UNKNOWN")
        found, activity_unknown = _activity(revision_id, speech, config)
        issues += found
        if activity_unknown:
            unknown.append("speech_activity")
        scope.ran("QC16_ACTIVITY_GAP") if activity_unknown or config["activity_gap_ms"] is not None else scope.off("QC16_ACTIVITY_GAP", "activity_gap_ms")
    else:
        scope.skipped(no_evidence, "QC11_ASR_SCORE", "QC16_ACTIVITY_GAP")
    if visual is None and speech is None:
        scope.skipped(no_evidence, "QC12_SCORE_UNKNOWN")

    if machine_source:
        issues += _source_conflicts(revision_id, segment_set)
        issues += _word_clock(revision_id, segment_set, speech)
        scope.ran("QC14_SOURCE_CONFLICT", "QC15_WORD_CLOCK")
    else:
        scope.skipped("没有机器分段记录（源字幕来自人工文件，或这是一份人工稿）", "QC14_SOURCE_CONFLICT", "QC15_WORD_CLOCK")
    if translation_set:
        issues += _terms(revision_id, {segment["cue_id"]: segment for segment in segments}, translation_set, loaded.get("postprocess_trace"))
        scope.ran("QC18_TERMS")
    else:
        scope.skipped("这个版本没有机器译文记录；人工改过的译文不再套用机器的译名标记", "QC18_TERMS")
    if segment_set or translation_set:
        issues += _uncertain(revision_id, segment_set, translation_set)
        scope.ran("QC19_UNCERTAIN")
    else:
        scope.skipped("这个版本没有机器的不确定标记可读", "QC19_UNCERTAIN")
    if isinstance(extensions.get("review_base"), dict):
        issues += _review_base(revision_id, extensions["review_base"])
        scope.ran("QC20_REVIEW_BASE")
    else:
        scope.skipped("不是人工文件，没有基础版本可比", "QC20_REVIEW_BASE")
    if isinstance(extensions.get("font_check"), dict):
        issues += _fonts(revision_id, extensions["font_check"])
        scope.ran("QC22_FONT")
    else:
        scope.skipped("字体只在导出时检查", "QC22_FONT")
    issues += _empty(revision_id, view)

    status = confirmation(revision_id, item["subtitle_ref"]["sha256"], media_record["media_id"] if media_record else None, view["format"], item["attestations"],
                          default="PARTIAL" if human_file else "UNREVIEWED")
    report = require_record("QualityReport", {
        "schema_version": SCHEMA_VERSION, "revision_id": revision_id, "rule_set_version": int(config["rule_set_version"]), "issues": issues,
        "unknown_capabilities": list(dict.fromkeys(unknown)), "structural_status": "invalid" if any(issue["severity"] == "ERROR" for issue in issues) else "valid",
        "human_status": status["human_status"], "confirmed_scopes": status["confirmed_scopes"], "generated_at": utc_now()})
    return {"report": report, "scope": scope.record(config), "rows": [{key: value for key, value in row.items() if key != "segment"} for row in rows]}


def check_quality(quality_input: dict) -> dict:
    """QualityInput -> QualityReport (plan 6.8 / 11). Read-only: no file is written and the input is not modified."""
    return evaluate(quality_input)["report"]


# --------------------------------------------------------------------------- review deliverables (plan 11.3): display files, never read back

CSV_COLUMNS = ("revision_id", "cue_id", "start", "end", "role", "source_text", "translation_text", "rule_ids", "severity", "message", "source_ref", "provenance_status")
_SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}
_FORMULA_START = ("=", "+", "-", "@")


def clock_text(ms: int | None) -> str:
    if not isinstance(ms, int) or ms < 0:
        return ""
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def csv_cell(value: Any) -> str:
    """One display cell. A cell a spreadsheet could take for a formula gets a leading apostrophe (display only)."""
    text = "" if value is None else str(value)
    stripped = text.lstrip()
    if text[:1] in ("\t", "\r") or (stripped[:1] in _FORMULA_START):
        return "'" + text
    return text


def _issues_by_cue(issues: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    by_cue: dict[str, list[dict]] = {}
    for issue in issues:
        for cue_id in issue["cue_ids"]:
            by_cue.setdefault(cue_id, []).append(issue)
    return by_cue, issues


def review_rows(report: dict, rows: list[dict]) -> list[dict]:
    """Rows of the review table: every displayed cue (with its issues), then each issue that names no displayed cue."""
    known = {row["cue_id"] for row in rows}
    by_cue, _ = _issues_by_cue(report["issues"])
    table = []
    for row in rows:
        found = sorted(by_cue.get(row["cue_id"], []), key=lambda issue: (_SEVERITY_ORDER[issue["severity"]], issue["rule_id"]))
        table.append({"revision_id": report["revision_id"], "cue_id": row["cue_id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"], "role": row["role"],
                      "source_text": row["source_text"], "translation_text": row["translation_text"], "issues": found, "source_ref": row["source_ref"],
                      "provenance_status": row["provenance_status"], "line": row["line"]})
    for issue in report["issues"]:
        if any(cue_id in known for cue_id in issue["cue_ids"]):
            continue
        table.append({"revision_id": report["revision_id"], "cue_id": "", "start_ms": issue["start_ms"], "end_ms": issue["end_ms"], "role": "", "source_text": "",
                      "translation_text": "", "issues": [issue], "source_ref": " ".join(issue["source_refs"]), "provenance_status": "", "line": None})
    return table


def render_review_csv(report: dict, rows: list[dict]) -> bytes:
    """review.csv: UTF-8 with BOM, fixed columns, every cell quoted. A display file: editing it changes nothing in the program."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for entry in review_rows(report, rows):
        issues = entry["issues"]
        severity = min((issue["severity"] for issue in issues), key=_SEVERITY_ORDER.get) if issues else ""
        writer.writerow([csv_cell(value) for value in (
            entry["revision_id"], entry["cue_id"], clock_text(entry["start_ms"]), clock_text(entry["end_ms"]), entry["role"], entry["source_text"], entry["translation_text"],
            ";".join(dict.fromkeys(issue["rule_id"] for issue in issues)), severity, " | ".join(issue["message"] for issue in issues), entry["source_ref"], entry["provenance_status"])])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


_SEVERITY_LABELS = {"ERROR": "错误", "WARNING": "提示", "INFO": "说明"}
_STATUS_LABELS = {"UNREVIEWED": "未经人工检查", "PARTIAL": "部分确认", "REVIEWED": "已人工确认"}
_STYLE = """body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:16px;color:#1d1d1f;background:#fff}
h1{font-size:20px}h2{font-size:16px;margin-top:24px}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #ccc;padding:4px 6px;vertical-align:top;text-align:left;white-space:pre-wrap;word-break:break-word}
th{background:#f2f2f2}.ERROR{background:#fde2e1}.WARNING{background:#fff4d6}.INFO{background:#eef4ff}.tag{font-weight:bold}
.note{color:#555;font-size:13px}code{font-family:Consolas,monospace}"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _href(path: str) -> str:
    """A relative link to a local file next to the report; escaped for an attribute and percent-encoded."""
    return _e(quote(path.replace("\\", "/"), safe="/._-~"))


def render_review_html(report: dict, rows: list[dict], scope: dict, *, title: str, links: list[dict] | None = None, frames: list[dict] | None = None) -> bytes:
    """review.html: one offline static page; every text and attribute is escaped, there is no script and nothing online."""
    counts = Counter(issue["severity"] for issue in report["issues"])
    out = ["<!DOCTYPE html>", '<html lang="zh-CN"><head><meta charset="utf-8">',
           "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:\">",
           f"<title>{_e(title)}</title><style>{_STYLE}</style></head><body>", f"<h1>{_e(title)}</h1>",
           f"<p>版本编号 <code>{_e(report['revision_id'])}</code>；结构检查：<span class=\"tag\">{'通过' if report['structural_status'] == 'valid' else '未通过（有错误，不能正式提交或导出）'}</span>；"
           f"人工状态：<span class=\"tag\">{_e(_STATUS_LABELS[report['human_status']])}</span>"
           + (f"（已确认范围：{_e('、'.join(report['confirmed_scopes']))}）" if report["confirmed_scopes"] else "") + "</p>",
           f"<p>错误 {counts['ERROR']} 条，提示 {counts['WARNING']} 条，说明 {counts['INFO']} 条。生成时间 {_e(report['generated_at'])}（UTC）。</p>",
           "<p class=\"note\">这份报告只用来定位问题，不是编辑器：请在 Subtitle Edit（SRT）或 Aegisub（ASS）里修改字幕，再用“登记人工稿”导入。"
           "提示不会拦截导出；只有你在“标记人工确认”里确认过的范围才算人工检查过。程序没有自动改字、合并、删除或移动任何字幕。</p>"]
    if report["unknown_capabilities"]:
        out.append(f"<p class=\"note\">这次无法判断的项目：{_e('、'.join(report['unknown_capabilities']))}（没有证据就写“未知”，不代表没有问题）。</p>")
    if links:
        out.append("<h2>文件</h2><ul>" + "".join(f"<li><a href=\"{_href(link['href'])}\">{_e(link['label'])}</a></li>" for link in links) + "</ul>")
    if frames:
        out.append("<h2>边界帧</h2><table><tr><th>用途</th><th>目标时间</th><th>实际帧时间</th><th>实际 PTS / 时间基</th><th>文件</th></tr>")
        for frame in frames:
            out.append(f"<tr><td>{_e(frame.get('purpose'))}</td><td>{_e(clock_text(frame.get('target_ms')))}</td><td>{_e(clock_text(frame.get('frame_ms')))}</td>"
                       f"<td>{_e(frame.get('pts'))} × {_e(frame.get('time_base_num'))}/{_e(frame.get('time_base_den'))}</td><td><a href=\"{_href(frame['href'])}\">{_e(frame['href'])}</a></td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=\"note\">边界帧按需导出：命令行 <code>review frames</code> 会把指定时间前后两帧另存为图片，图片标注的是视频里的实际 PTS。</p>")
    out.append("<h2>逐条字幕</h2><table><tr><th>#</th><th>开始</th><th>结束</th><th>角色</th><th>源文</th><th>译文 / 显示文字</th><th>问题</th><th>来源</th></tr>")
    for position, entry in enumerate(review_rows(report, rows), start=1):
        issues = entry["issues"]
        worst = min((issue["severity"] for issue in issues), key=_SEVERITY_ORDER.get) if issues else ""
        notes = "<br>".join(f"<span class=\"tag\">[{_e(_SEVERITY_LABELS[issue['severity']])}] {_e(issue['rule_id'])}</span> {_e(issue['message'])}"
                            f"<br><span class=\"note\">建议：{_e(issue['recommended_action'])}</span>" for issue in issues)
        role = ROLE_LABELS.get(entry["role"], entry["role"])
        out.append(f"<tr class=\"{_e(worst)}\"><td>{position}</td><td>{_e(clock_text(entry['start_ms']))}</td><td>{_e(clock_text(entry['end_ms']))}</td><td>{_e(role)}</td>"
                   f"<td>{_e(entry['source_text'])}</td><td>{_e(entry['translation_text'])}</td><td>{notes}</td><td><code>{_e(entry['source_ref'])}</code><br>{_e(entry['provenance_status'])}</td></tr>")
    out.append("</table><h2>检查范围</h2><table><tr><th>规则</th><th>名称</th><th>这次</th><th>说明</th></tr>")
    for rule_id, (name, meaning) in RULES.items():
        if rule_id in scope["evaluated_rules"]:
            state = "已检查"
        elif rule_id in scope["disabled_rules"]:
            state = "已关闭：" + scope["disabled_rules"][rule_id]
        else:
            state = "未检查：" + scope["not_evaluated_rules"].get(rule_id, "")
        out.append(f"<tr><td><code>{_e(rule_id)}</code></td><td>{_e(name)}</td><td>{_e(state)}</td><td>{_e(meaning)}</td></tr>")
    out.append("</table><p class=\"note\">review.csv 是给表格软件看的展示文件：以 = + - @ 开头的单元格前加了一个英文单引号防止被当成公式，"
               "这个前缀不写回字幕或 JSON。改 CSV 不会改动程序里的任何数据，也不能当作导入格式。</p></body></html>")
    return ("\n".join(out) + "\n").encode("utf-8")


def review_files(result: dict, *, title: str, links: list[dict] | None = None) -> dict[str, bytes]:
    """review.csv and review.html of one evaluation, as bytes; the caller that owns the commit stores them."""
    return {"review.csv": render_review_csv(result["report"], result["rows"]),
            "review.html": render_review_html(result["report"], result["rows"], result["scope"], title=title, links=links)}
