#!/usr/bin/env python3
"""W06 / T03 quality table: every hard error and soft hint class of plan 11.2 from table-driven inputs, plus the review
deliverables of plan 11.3 (CSV/HTML safety) and on-demand boundary frames labelled with the real PTS (real FFmpeg).

Run directly or through scripts/run_tests.py (PYTHONPATH = app/tools + tests).
"""

from __future__ import annotations

from pathlib import Path
import copy
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from support import w03
from upgrade import contracts as c
from upgrade import media, quality, subtitles
from upgrade.config import DEFAULT_QUALITY, default_app_config
from upgrade.contracts import ContractError, sha256_bytes, sha256_text
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade.runner import create_project
from upgrade.storage import init_data_root
from upgrade_revisions_test import SharedRunCase

REVISION, SOURCE_REVISION, MEDIA = c.new_id(), c.new_id(), c.new_id()


def stamp(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(*cues, labels=None) -> bytes:
    """cues: (start_ms, end_ms, text); labels default to 1..n."""
    labels = labels or [str(position) for position in range(1, len(cues) + 1)]
    return "\r\n".join(f"{label}\r\n{stamp(start)} --> {stamp(end)}\r\n{text}\r\n" for label, (start, end, text) in zip(labels, cues)).encode("utf-8")


def segment(position: int, start: int, end: int, text: str, *, role: str = "speech", parent: str | None = None, visual_ids=(), flags=(), timing_source: str = "speech_words") -> dict:
    parent = parent if parent is not None else f"asr-{position:06d}"
    return {"cue_id": f"cue-{position:06d}", "legacy_cue_id": None, "index": position, "start_ms": start, "end_ms": end, "source_text": text, "role": role,
            "source_ranges": [{"parent_id": parent, "char_start": 0, "char_end": len(text)}] if role in ("speech", "mixed") else [],
            "visual_ids": list(visual_ids), "timing_source": timing_source, "flags": list(flags)}


class Case:
    """One machine revision built in memory: SRT text + segments + translation records + optional recognition evidence."""

    def __init__(self, rows, *, duration_ms: int = 60000):
        # rows: (start_ms, end_ms, source_text, target_text)
        self.duration_ms = duration_ms
        self.segments = [segment(position, start, end, source) for position, (start, end, source, _) in enumerate(rows, start=1)]
        self.parents = [{"id": item["source_ranges"][0]["parent_id"], "start_ms": item["start_ms"], "end_ms": item["end_ms"], "text": item["source_text"]}
                        for item in self.segments if item["source_ranges"]]
        self.targets = [target for *_, target in rows]
        self.translation_flags = [[] for _ in rows]
        self.visual_events: list[dict] = []
        self.unresolved: list[dict] = []
        self.speech = None
        self.visual = None
        self.trace = None
        self.mode = "combined"
        self.config = dict(DEFAULT_QUALITY)
        self.extensions: dict = {}
        self.layers = None
        self.srt_override = None

    def segment_set(self) -> dict:
        return {"schema_version": 2, "source_revision_id": SOURCE_REVISION, "media_id": MEDIA, "origin": "machine", "recognition_mode": self.mode, "timing_mode": "mixed",
                "status": "OK" if self.segments else "EMPTY", "segments": self.segments, "parents": self.parents, "visual_events": self.visual_events,
                "unresolved_visual_events": self.unresolved, "coverage": {}, "extensions": {}}

    def translation_set(self) -> dict:
        return {"schema_version": 2, "source_revision_id": SOURCE_REVISION, "records": [
            {"cue_id": item["cue_id"], "source_revision_id": SOURCE_REVISION, "source_text_sha256": sha256_text(item["source_text"]), "target_text": target, "flags": flags}
            for item, target, flags in zip(self.segments, self.targets, self.translation_flags)]}

    def item(self) -> dict:
        data = self.srt_override if self.srt_override is not None else srt(*[(item["start_ms"], item["end_ms"], target) for item, target in zip(self.segments, self.targets)])
        view = subtitles.parse_view(data, "srt")
        extensions = {"origin": "machine", "loaded": {"segment_set": self.segment_set(), "translation_set": self.translation_set(), "speech_evidence": self.speech,
                                                      "visual_evidence": self.visual, "postprocess_trace": self.trace}, **self.extensions}
        if self.layers is not None:
            extensions["cue_layers"] = self.layers
        return {"revision_id": REVISION, "revision_kind": "machine", "media_record": {"media_id": MEDIA, "duration_ms": self.duration_ms},
                "subtitle_ref": {"path": "machine.zh.srt", "sha256": sha256_bytes(data), "bytes": len(data), "media_type": "application/x-subrip"},
                "parsed_view": view, "source_revision": {"revision_id": SOURCE_REVISION}, "recognition_refs": [], "translation_ref": None,
                "rule_config": self.config, "attestations": [], "extensions": extensions}

    def run(self) -> dict:
        item = self.item()
        before = copy.deepcopy(item)
        result = quality.evaluate(item)
        assert item == before, "evaluate() modified its input"
        return result


def human_item(data: bytes, *, fmt: str = "srt", kind: str = "review", duration_ms: int | None = 60000, extensions=None) -> dict:
    view = subtitles.parse_view(data, fmt)
    return {"revision_id": REVISION, "revision_kind": kind, "media_record": {"media_id": MEDIA, "duration_ms": duration_ms} if duration_ms is not None else None,
            "subtitle_ref": {"path": f"original.{fmt}", "sha256": sha256_bytes(data), "bytes": len(data), "media_type": "text/plain"}, "parsed_view": view,
            "source_revision": None, "recognition_refs": [], "translation_ref": None, "rule_config": dict(DEFAULT_QUALITY), "attestations": [],
            "extensions": {"origin": "human", **(extensions or {})}}


def found(result: dict, rule_id: str, severity: str | None = None) -> list[dict]:
    return [issue for issue in result["report"]["issues"] if issue["rule_id"] == rule_id and (severity is None or issue["severity"] == severity)]


BASIC = [(1000, 3000, "おはよう", "早上好"), (4000, 6000, "ありがとう", "谢谢")]


def mutate(rows=BASIC, **kw):
    """A Case with keyword tweaks applied by small functions (keeps the table below one line per case)."""
    case = Case(rows, duration_ms=kw.pop("duration_ms", 60000))
    for step in kw.pop("steps", ()):
        step(case)
    return case


def stale_hash(case):
    old = case.translation_set()
    case.translation_set = lambda: old                   # the translation records still carry the hash of the old text
    case.segments[1]["source_text"] = "ありがと"
    case.segments[1]["source_ranges"][0]["char_end"] = 4
    case.parents[1]["text"] = "ありがと"


def missing_chars(case):
    case.segments[0]["source_ranges"][0]["char_end"] = 2      # "おは" of "おはよう": two characters belong to no cue


def duplicate_id(case):
    case.segments[1]["cue_id"] = case.segments[0]["cue_id"]


def foreign_translation(case):
    case.translation_set = (lambda original: lambda: {**original(), "source_revision_id": c.new_id()})(case.translation_set)


def empty_target(case):
    case.targets[1] = "  "


def odd_labels(case):
    case.srt_override = srt(*[(item["start_ms"], item["end_ms"], target) for item, target in zip(case.segments, case.targets)], labels=["1", "3"])


# rule, severity, observed code (or None), a machine case; one hard error class per row (plan 14.3 T03)
HARD = [
    ("QC03_BOUNDS", "ERROR", None, lambda: mutate(duration_ms=5000)),
    ("QC03_BOUNDS", "INFO", None, lambda: mutate(duration_ms=5999)),                       # 1 ms past the end: SRT quantisation, the value is kept
    ("QC04_IDENTITY", "ERROR", "DUPLICATE_SEGMENT_ID", lambda: mutate(steps=[duplicate_id])),
    ("QC04_IDENTITY", "ERROR", "TRANSLATION_SOURCE_MISMATCH", lambda: mutate(steps=[foreign_translation])),
    ("QC04_IDENTITY", "ERROR", "SOURCE_TEXT_HASH_MISMATCH", lambda: mutate(steps=[stale_hash])),
    ("QC05_COVERAGE", "ERROR", None, lambda: mutate(steps=[missing_chars])),
    ("QC06_ORDER", "ERROR", None, lambda: mutate(steps=[odd_labels])),
    ("QC17_TRANSLATION", "ERROR", "TRANSLATION_EMPTY", lambda: mutate(steps=[empty_target])),
    ("QC21_MEDIA_BINDING", "ERROR", None, lambda: mutate(steps=[lambda case: case.extensions.update(target_media_id=c.new_id())])),
    ("QC22_FONT", "ERROR", "FONT_MISSING", lambda: mutate(steps=[lambda case: case.extensions.update(font_check={"purpose": "video", "missing_fonts": ["思源黑体"]})])),
]


class HardErrorTable(unittest.TestCase):
    def test_each_hard_error_class_is_raised_by_its_own_input_and_blocks_the_revision(self):
        clean = mutate().run()
        self.assertEqual((clean["report"]["structural_status"], [i["rule_id"] for i in clean["report"]["issues"] if i["severity"] != "INFO"]), ("valid", []))
        for rule_id, severity, code, make in HARD:
            with self.subTest(rule=rule_id, severity=severity, code=code):
                result = make().run()
                hits = found(result, rule_id, severity)
                self.assertTrue(hits, json.dumps(result["report"]["issues"], ensure_ascii=False)[:800])
                if code:
                    self.assertIn(code, [issue["observed"].get("code") for issue in hits])
                self.assertEqual(result["report"]["structural_status"], "invalid" if severity == "ERROR" else "valid")

    def test_parse_and_time_errors_of_a_human_file_keep_line_numbers(self):
        for rule_id, data in (("QC01_PARSE", "1\r\n00:00:01,000 -> 00:00:02,000\r\n文字\r\n"), ("QC02_TIME", "1\r\n00:00:03,000 --> 00:00:02,000\r\n文字\r\n")):
            with self.subTest(rule=rule_id):
                result = quality.evaluate(human_item(data.encode("utf-8")))
                hits = found(result, rule_id, "ERROR")
                self.assertTrue(hits and hits[0]["source_refs"][0].startswith("line:"))
                self.assertEqual(result["report"]["structural_status"], "invalid")

    def test_a_human_file_with_its_own_numbering_is_only_informed(self):
        result = quality.evaluate(human_item(srt((1000, 2000, "一"), (3000, 4000, "二"), labels=["7", "3"])))
        self.assertEqual([issue["severity"] for issue in found(result, "QC06_ORDER")], ["INFO"])
        self.assertEqual(result["report"]["structural_status"], "valid")

    def test_ass_quantisation_inside_ten_milliseconds_is_information_and_the_value_is_kept(self):
        data = (w03.ASS_HEADER + "Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,文字\r\n").encode("utf-8")
        result = quality.evaluate(human_item(data, fmt="ass", duration_ms=4995))
        self.assertEqual([issue["severity"] for issue in found(result, "QC03_BOUNDS")], ["INFO"])
        self.assertEqual(found(result, "QC03_BOUNDS")[0]["observed"]["end_ms"], 5000)

    def test_thresholds_set_to_null_turn_hints_off_but_the_hard_rules_still_run(self):
        case = mutate([(1000, 1100, "あ", "啊"), (2000, 2100, "い", "咦")], duration_ms=1500)
        case.config.update(min_duration_ms=None, max_duration_ms=None, max_cps=None)
        result = case.run()
        self.assertEqual((found(result, "QC07_DURATION"), found(result, "QC08_SPEED")), ([], []))
        self.assertEqual(sorted(result["scope"]["disabled_rules"]), ["QC07_DURATION", "QC08_SPEED"])
        self.assertTrue(found(result, "QC03_BOUNDS", "ERROR"))                                # a null threshold never reaches a hard rule
        self.assertTrue(set(quality.HARD_RULES) - {"QC21_MEDIA_BINDING"} <= set(result["scope"]["evaluated_rules"]) | set(result["scope"]["not_evaluated_rules"]))
        self.assertIn("QC05_COVERAGE", result["scope"]["evaluated_rules"])


def speech_evidence(case, *, scores=None, kind="whisper_token_probability", activity=None):
    case.speech = {"parents": [{**parent, "score": score, "score_kind": kind if score is not None else None, "flags": []}
                               for parent, score in zip(case.parents, scores or [None] * len(case.parents))],
                   "activity_intervals": activity, "coverage_status": "activity_compared" if activity is not None else "process_completed_only", "extensions": {}}


def visual_evidence(case, events, *, kind="ocr_confidence_0_1"):
    case.visual = {"events": [{"id": f"ocr-{n:06d}", "ordinal": n, "start_ms": start, "end_ms": end, "text": text, "score": score, "score_kind": kind if score is not None else None}
                              for n, (start, end, text, score) in enumerate(events, start=1)], "extensions": {}}


def screen_text_conflict(case):
    """ASR heard one thing, OCR read another for the same cue: both are kept as recorded, nothing is unified."""
    case.segments[0].update(role="mixed", visual_ids=["ocr-000001"], flags=["TEXT_CONFLICT", "CHECK"])
    case.visual_events = [{"id": "ocr-000001", "ordinal": 1, "start_ms": 1000, "end_ms": 3000, "text": "おはよー"}]
    case.unresolved = [{"id": "ocr-000002", "start_ms": 7000, "end_ms": 8000, "text": "次回予告", "flags": ["VISUAL_MAPPING_UNRESOLVED", "CHECK"]}]


def screen_only(case):
    for item in case.segments:
        item.update(role="screen", source_ranges=[], flags=["VISUAL_ONLY", "CHECK"])
    case.parents = []


SOFT = [
    ("QC07_DURATION", "WARNING", lambda: mutate([(1000, 1300, "あ", "啊")])),
    ("QC07_DURATION", "WARNING", lambda: mutate([(1000, 12000, "あ", "啊")])),
    ("QC08_SPEED", "WARNING", lambda: mutate([(1000, 2000, "あいうえおかきくけこさしすせそたちつてと", "一二三四五六七八九十一二三四五六七八九十")])),
    ("QC09_OVERLAP", "WARNING", lambda: mutate([(1000, 3000, "あ", "啊"), (2500, 4000, "い", "咦")])),
    ("QC10_OCR_SCORE", "WARNING", lambda: mutate(steps=[lambda case: visual_evidence(case, [(1000, 3000, "おはよう", 0.42)])])),
    ("QC11_ASR_SCORE", "WARNING", lambda: mutate(steps=[lambda case: speech_evidence(case, scores=[0.31, 0.9])])),
    ("QC12_SCORE_UNKNOWN", "INFO", lambda: mutate(steps=[lambda case: speech_evidence(case)])),
    ("QC13_OCR_CONTINUITY", "WARNING", lambda: mutate(steps=[lambda case: visual_evidence(case, [(1000, 2000, "テロップ", None), (2100, 3000, "テロップ", None)])])),
    ("QC14_SOURCE_CONFLICT", "WARNING", lambda: mutate(steps=[screen_text_conflict])),
    ("QC15_WORD_CLOCK", "WARNING", lambda: mutate(steps=[lambda case: case.segments[0]["flags"].extend(["WORD_CLOCK_REJECTED", "CHECK"])])),
    ("QC16_ACTIVITY_GAP", "WARNING", lambda: mutate(steps=[lambda case: speech_evidence(case, activity=[{"start_ms": 1000, "end_ms": 6000}])])),
    ("QC16_ACTIVITY_GAP", "INFO", lambda: mutate(steps=[lambda case: speech_evidence(case)])),
    ("QC18_TERMS", "WARNING", lambda: mutate(steps=[lambda case: case.translation_flags[0].extend(["TRANSLATION_TERM_MISSING", "CHECK"])])),
    ("QC18_TERMS", "WARNING", lambda: mutate(steps=[lambda case: case.translation_flags[1].extend(["TRANSLATION_JAPANESE_PRESENT", "CHECK"])])),
    ("QC19_UNCERTAIN", "WARNING", lambda: mutate(steps=[lambda case: case.translation_flags[0].extend(["TRANSLATION_UNCERTAIN", "CHECK"])])),
    ("QC22_FONT", "WARNING", lambda: mutate(steps=[lambda case: case.extensions.update(font_check={"purpose": "subtitles", "missing_fonts": ["思源黑体"]})])),
    ("QC23_EMPTY", "WARNING", lambda: mutate([])),
]


class SoftHintTable(unittest.TestCase):
    def test_each_soft_hint_class_warns_without_changing_a_subtitle(self):
        for rule_id, severity, make in SOFT:
            with self.subTest(rule=rule_id, severity=severity):
                case = make()
                text_before = [cue["text"] for cue in case.item()["parsed_view"]["cues"]]
                result = case.run()                                   # run() also asserts the input was not modified
                self.assertTrue(found(result, rule_id, severity), json.dumps(result["report"]["issues"], ensure_ascii=False)[:800])
                self.assertEqual(result["report"]["structural_status"], "valid")       # a hint never blocks
                self.assertEqual([row["translation_text"] for row in result["rows"]], text_before)

    def test_ocr_and_asr_texts_are_shown_side_by_side_and_never_made_the_same(self):
        result = mutate(steps=[screen_text_conflict]).run()
        conflict, unresolved = found(result, "QC14_SOURCE_CONFLICT")
        self.assertEqual(([entry["text"] for entry in conflict["observed"]["asr"]], [entry["text"] for entry in conflict["observed"]["ocr"]]), (["おはよう"], ["おはよー"]))
        self.assertEqual(result["rows"][0]["source_text"], "おはよう")                       # the source text stays what was recorded
        self.assertEqual((unresolved["observed"]["asr"], unresolved["observed"]["ocr"][0]["text"]), ([], "次回予告"))

    def test_without_a_score_the_report_says_unknown_once_per_backend_and_invents_none(self):
        case = mutate(steps=[lambda case: speech_evidence(case), lambda case: visual_evidence(case, [(1000, 3000, "おはよう", None), (4000, 6000, "ありがとう", None)])])
        result = case.run()
        self.assertEqual([issue["observed"]["backend"] for issue in found(result, "QC12_SCORE_UNKNOWN")], ["ocr", "asr"])
        self.assertEqual((found(result, "QC10_OCR_SCORE"), found(result, "QC11_ASR_SCORE")), ([], []))
        self.assertTrue({"ocr_scores", "asr_scores"} <= set(result["report"]["unknown_capabilities"]))
        self.assertNotIn("score", json.dumps(result["rows"]))

    def test_a_score_of_another_kind_is_never_held_against_a_threshold_made_for_probabilities(self):
        result = mutate(steps=[lambda case: speech_evidence(case, scores=[-0.9, -1.2], kind="avg_logprob")]).run()
        self.assertEqual(found(result, "QC11_ASR_SCORE"), [])
        self.assertEqual(found(result, "QC12_SCORE_UNKNOWN")[0]["observed"]["other_score_kinds"], {"avg_logprob": 2})

    def test_simultaneous_display_of_different_roles_or_layers_is_kept_and_only_informed(self):
        case = mutate([(1000, 4000, "おはよう", "早上好"), (2000, 3000, "看板", "招牌")])
        case.segments[1].update(role="screen", source_ranges=[], visual_ids=["ocr-000001"])
        case.parents = case.parents[:1]
        case.visual_events = [{"id": "ocr-000001", "ordinal": 1, "start_ms": 2000, "end_ms": 3000, "text": "看板"}]
        result = case.run()
        self.assertEqual([(issue["severity"], issue["observed"]["kind"]) for issue in found(result, "QC09_OVERLAP")], [("INFO", "different_role")])
        self.assertEqual(len(result["rows"]), 2)                                                  # both cues are still there
        layered = mutate([(1000, 4000, "あ", "啊"), (2000, 3000, "い", "咦")])
        layered.layers = [0, 1]
        self.assertEqual([issue["observed"]["kind"] for issue in found(layered.run(), "QC09_OVERLAP")], ["different_layer"])
        unknown = quality.evaluate(human_item(srt((1000, 4000, "甲"), (2000, 3000, "乙"))))
        self.assertEqual([(issue["severity"], issue["verification"]) for issue in found(unknown, "QC09_OVERLAP")], [("WARNING", "unknown")])

    def test_visual_only_mode_is_one_mode_fact_and_elsewhere_a_hint_per_cue(self):
        elsewhere = mutate(steps=[screen_only]).run()
        self.assertEqual([issue["severity"] for issue in found(elsewhere, "QC19_UNCERTAIN")], ["WARNING", "WARNING"])
        chosen = mutate(steps=[screen_only, lambda case: setattr(case, "mode", "visual_only")]).run()
        self.assertEqual([(issue["severity"], issue["observed"].get("code")) for issue in found(chosen, "QC19_UNCERTAIN")], [("INFO", "VISUAL_ONLY_MODE")])

    def test_a_human_file_says_what_could_not_be_checked_instead_of_passing_it(self):
        result = quality.evaluate(human_item(srt((1000, 2000, "一")), extensions={"review_base": {
            "base_revision_id": c.new_id(), "provenance_status": "partial", "superseded_by": [c.new_id()],
            "style_diff": {"styles_changed": ["Default"]}, "counts": {}}}))
        self.assertEqual(sorted(issue["severity"] for issue in found(result, "QC20_REVIEW_BASE")), ["INFO", "INFO", "WARNING"])
        for rule_id in ("QC05_COVERAGE", "QC10_OCR_SCORE", "QC17_TRANSLATION", "QC18_TERMS"):
            self.assertIn(rule_id, result["scope"]["not_evaluated_rules"])
        self.assertEqual(result["report"]["human_status"], "PARTIAL")


HOSTILE = ["=SUM(A1:A9)", "+1+1", "-2", "@cmd", "\tTAB", "\rCR", "  =padded", "<script>alert('x')</script>", '"><img src=x onerror=alert(1)>', "普通的字"]


class DeliverableSafety(unittest.TestCase):
    def result(self):
        case = mutate([(1000 + 3000 * n, 2500 + 3000 * n, f"原文{n}", text) for n, text in enumerate(HOSTILE)], duration_ms=40000)
        case.segments[7]["source_text"] = case.parents[7]["text"] = "</td><script>bad()</script>"
        case.segments[7]["source_ranges"][0]["char_end"] = len(case.parents[7]["text"])
        return case.run()

    def test_csv_has_fixed_columns_a_bom_and_formula_cells_are_neutralised_for_display_only(self):
        result = self.result()
        data = quality.render_review_csv(result["report"], result["rows"])
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        table = list(csv.reader(io.StringIO(data[3:].decode("utf-8"), newline="")))
        self.assertEqual(tuple(table[0]), quality.CSV_COLUMNS)
        shown = [row[6] for row in table[1:len(HOSTILE) + 1]]
        expected = ["'" + text if text.lstrip()[:1] in "=+-@" or text[:1] in "\t\r" else text for text in HOSTILE]
        self.assertEqual(shown, expected)
        self.assertEqual([record["target_text"] for record in mutate([(0, 1, "x", text) for text in HOSTILE]).translation_set()["records"]], HOSTILE)
        report_text = json.dumps(result["report"], ensure_ascii=False)
        self.assertNotIn("'=SUM", report_text)                                                 # the display prefix never reaches the data
        self.assertEqual([row["translation_text"] for row in result["rows"]], HOSTILE)

    def test_html_is_offline_static_and_every_subtitle_text_is_escaped(self):
        result = self.result()
        page = quality.render_review_html(result["report"], result["rows"], result["scope"], title="<b>标题</b>",
                                          links=[{"label": "<i>文件</i>", "href": "a b/字幕\"x.srt"}]).decode("utf-8")
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("<img", page.lower())
        self.assertNotIn("<i>", page)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", page)
        self.assertIn("&lt;/td&gt;&lt;script&gt;bad()&lt;/script&gt;", page)
        self.assertIn('href="a%20b/%E5%AD%97%E5%B9%95%22x.srt"', page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertIn("Content-Security-Policy", page)
        for rule_id in quality.RULE_IDS:                                                       # every rule is explained, with whether it ran
            self.assertIn(rule_id, page)


class BoundaryFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        problem = w03.media_tools_problem()
        if problem:
            raise unittest.SkipTest(problem)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.root = cls.tmp / "数据根 (frames)"
        init_data_root(cls.root)
        config = default_app_config(cls.root)
        w03.register_resources(config, {"ffmpeg": Path(w03.FFMPEG), "ffprobe": Path(w03.FFPROBE)})
        report = inspect_resources(config, probe=False)
        cls.ffmpeg, cls.ffprobe = resolve_resource("ffmpeg", report), resolve_resource("ffprobe", report)
        record = create_project(cls.root, "frames")
        cls.project = cls.root / "projects" / record["project_id"]
        cls.project_id = record["project_id"]
        cls.serial = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def runtime(self):
        type(self).serial += 1
        return media.MediaRuntime(project_dir=self.project, ffprobe=self.ffprobe, ffmpeg=self.ffmpeg, log_dir=self.tmp / "logs" / str(self.serial), owner_token="w06")

    def record(self, path: Path) -> dict:
        return media.probe_media({"input": {"kind": "local_video", "value": str(path), "source_media_id": None}, "selected_video_stream": None,
                                  "selected_audio_stream": None, "project_id": self.project_id, "extensions": {}}, runtime=self.runtime())

    def presented(self, record: dict) -> list[int]:
        _, packets = media.probe_packets(Path(record["source_ref"]["absolute_path"]), record["selected_video_stream"], self.runtime(), "check-packets")
        return media.analyze_video_packets(packets)["pts"]

    def test_frame_labels_are_the_real_pts_on_constant_variable_and_offset_clocks(self):
        sources = {"cfr": w03.make_cfr_mp4(self.tmp / "常规 片源.mp4"), "vfr": w03.make_vfr(self.tmp / "vfr.mp4"),
                   "offset": w03.make_offset_mkv(self.tmp / "offset.mkv", video_start="1.4", audio_start="1.4")}
        for name, path in sources.items():
            with self.subTest(clock=name):
                record = self.record(path)
                pts = self.presented(record)
                target = self.tmp / f"边界帧 {name}"
                document = media.export_boundary_frames(record, [1000, 0], target, runtime=self.runtime())
                self.assertEqual(json.loads((target / "frames.json").read_text(encoding="utf-8"))["frames"], document["frames"])
                for frame in document["frames"]:
                    self.assertIn(frame["pts"], pts)                                            # a presented frame, never an interpolated time
                    self.assertEqual(frame["pts_source"], "ffmpeg_showinfo")
                    self.assertIn(f"pts{frame['pts']}-", frame["file"])
                    self.assertTrue((target / frame["file"]).read_bytes().startswith(b"\x89PNG"))
                at_1000 = [frame for frame in document["frames"] if any(item == {"purpose": "boundary_at", "target_ms": 1000} for item in frame["purposes"])]
                before = [frame for frame in document["frames"] if any(item == {"purpose": "boundary_before", "target_ms": 1000} for item in frame["purposes"])]
                self.assertEqual(len(at_1000), 1)
                self.assertLessEqual(at_1000[0]["frame_ms"], 1000)
                self.assertEqual(pts.index(before[0]["pts"]) + 1, pts.index(at_1000[0]["pts"]))  # the two frames on either side of the boundary
                self.assertEqual(pts.index([frame for frame in document["frames"] if frame["frame_ms"] == 0][0]["pts"]), 0)

    def test_an_existing_directory_or_an_unverified_clock_is_refused(self):
        record = self.record(w03.make_cfr_mp4(self.tmp / "再次.mp4"))
        existing = self.tmp / "已有目录"
        existing.mkdir()
        with self.assertRaises(ContractError) as caught:
            media.export_boundary_frames(record, [500], existing, runtime=self.runtime())
        self.assertEqual((caught.exception.code, list(existing.iterdir())), ("OUTPUT_EXISTS", []))
        unknown = self.record(w03.make_vfr(self.tmp / "vfr_no_durations.mkv"))
        with self.assertRaises(ContractError) as caught:
            media.export_boundary_frames(unknown, [500], self.tmp / "不会创建", runtime=self.runtime())
        self.assertEqual(caught.exception.code, "MEDIA_CLOCK_UNKNOWN")
        self.assertFalse((self.tmp / "不会创建").exists())


class PipelineReports(SharedRunCase):
    """The rule table inside the real flows: the review files of a registered human file and boundary frames from the command line."""

    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        self.assertEqual(c.validate_record("OperationResult", json.loads(completed.stdout)), [], completed.stderr)
        return completed.returncode, json.loads(completed.stdout), completed.stderr

    def test_a_registered_human_file_gets_its_own_report_with_the_source_beside_each_mapped_cue(self):
        session, edit = self.session(self.machine_id)
        text = edit.read_bytes().decode("utf-8-sig")
        edit.write_bytes(b"\xef\xbb\xbf" + text.replace("译:", "译：", 1).encode("utf-8"))      # the first cue is edited, the others stay identical
        code, result, stderr = self.cli("review", "import", "--data-root", str(self.root), "--project", self.project_id, "--media", self.media_id,
                                        "--file", str(edit), "--session", session["session_id"])
        self.assertEqual((code, result["ok"]), (0, True), stderr)
        ids = result["ids"]
        directory = self.project / ids["directory"]
        self.assertEqual(ids["report_files"], {name: f"{ids['directory']}/{name}" for name in ("review.html", "review.csv", "quality_scope.json")})
        manifest = c.load_record("ReviewRevision", directory / "manifest.json")
        self.assertEqual([ref["path"] for ref in manifest["extensions"]["report_refs"]], ["quality_scope.json", "review.csv", "review.html"])
        segments = c.load_record("SegmentSet", self.source_dir / "segments.json")["segments"]
        table = list(csv.reader(io.StringIO((directory / "review.csv").read_bytes()[3:].decode("utf-8"), newline="")))[1:len(segments) + 1]
        self.assertEqual([row[11] for row in table][1:], ["exact"] * (len(segments) - 1))
        self.assertEqual([row[5] for row in table][1:], [item["source_text"] for item in segments][1:])   # the Japanese source beside the human Chinese line
        self.assertEqual([row[4] for row in table][1:], [item["role"] for item in segments][1:])
        self.assertNotEqual(table[0][11], "exact")
        page = (directory / "review.html").read_text(encoding="utf-8")
        self.assertIn("人工中文字幕复核报告", page)
        self.assertIn(f"../../../revisions/machine/{self.machine_id}/machine.zh.srt", page)
        self.resolve(ids["revision_id"])                                                       # the report files are part of the verified revision

    def test_boundary_frames_come_from_the_command_line_with_real_pts_and_never_overwrite(self):
        where = ("--data-root", str(self.root), "--project", self.project_id, "--revision", self.machine_id)
        target = self.tmp / "导出 边界帧"
        code, result, stderr = self.cli("review", "frames", *where, "--at-ms", "1000,1500", "--destination", str(target))
        self.assertEqual((code, result["ok"], result["operation"]), (0, True, "review.frames"), stderr)
        frames = result["ids"]["frames"]
        self.assertTrue(frames)
        record = media.load_media_record(self.project, self.media_id)
        for frame in frames:
            self.assertTrue((target / frame["file"]).is_file())
            self.assertIn(f"pts{frame['pts']}-", frame["file"])
        self.assertEqual(sorted({item["target_ms"] for frame in frames for item in frame["purposes"]}), [1000, 1500])
        self.assertEqual(record["media_id"], result["ids"]["media_id"])
        code, result, _ = self.cli("review", "frames", *where, "--at-ms", "1000", "--destination", str(target))
        self.assertEqual((code, result["error"]["code"]), (c.EXIT_DATA, "OUTPUT_EXISTS"))
        report = c.load_record("QualityReport", self.machine_dir / "quality.json")
        timeless = next((issue for issue in report["issues"] if issue["start_ms"] is None and not (issue["observed"] or {}).get("boundary_times_ms")), None)
        if timeless is not None:
            code, result, _ = self.cli("review", "frames", *where, "--issue", timeless["issue_id"], "--destination", str(self.tmp / "无时间"))
            self.assertEqual((code, result["error"]["code"]), (c.EXIT_ARGS, "NEEDS_SELECTION"))
        timed = next((issue for issue in report["issues"] if issue["start_ms"] is not None), None)
        if timed is not None:
            code, result, stderr = self.cli("review", "frames", *where, "--issue", timed["issue_id"], "--destination", str(self.tmp / "按问题"))
            self.assertEqual(code, 0, stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
