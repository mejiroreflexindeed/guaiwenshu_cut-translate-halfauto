"""T05 (W05): the human revision loop on real files.

One real pipeline run (real FFmpeg/ffprobe; the recognition engines doubled at the process-launch seam; the scripted
loopback model server) delivers a machine revision. Everything after that is the code under test: review sessions as
byte copies, registration of edited SRT/ASS files with their original bytes, the light correspondence to the base,
structural errors kept as candidates, files still being written, attestations bound to one file hash, superseded
bases, human source revisions that go through supplied_source, media binding, and the command line.
"""
from pathlib import Path
from unittest.mock import patch
import ast
import json
import os
import shutil
import subprocess
import sys
import unittest

from support import w04
from upgrade import contracts as c
from upgrade import media as media_module
from upgrade import revisions
from upgrade import runner as r
from upgrade import subtitles
from upgrade.config import APP_CONFIG_NAME
from upgrade.contracts import ContractError
from upgrade_stages_test import StageTestCase


def tree_hashes(directory: Path) -> dict[str, str]:
    """Every file below a directory with its hash: what must stay as it is once a revision is committed."""
    return {path.relative_to(directory).as_posix(): c.sha256_file(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def stamp(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(*cues: tuple) -> bytes:
    """(label, start_ms, end_ms, text) rows -> SRT bytes as an editor saves them: UTF-8 without BOM, CRLF."""
    return "\r\n".join(f"{label}\r\n{stamp(start)} --> {stamp(end)}\r\n{text}\r\n" for label, start, end, text in cues).encode("utf-8")


STRUCTURE_RULES = ("QC01_PARSE", "QC02_TIME", "QC03_BOUNDS", "QC06_ORDER", "QC20_REVIEW_BASE", "QC21_MEDIA_BINDING", "QC23_EMPTY")


def found(quality: dict, rules: tuple = STRUCTURE_RULES) -> list[tuple]:
    """(rule, severity) of the structure and base rules in a report; content rules are tested with the rule set itself."""
    return sorted((issue["rule_id"], issue["severity"]) for issue in quality["issues"] if issue["rule_id"] in rules)


FANCY_TEXT = "{\\pos(960,120)\\fad(120,80)\\c&H00FFFF&}谢谢, 真的{\\i1}谢谢{\\i0}\\N第二行"
DRAWING_TEXT = "{\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}"
NOTE_STYLE = "Style: 注释,SimHei,48,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,8,10,10,30,1"
COMMENT_EVENT = "Comment: 0,0:00:00.00,0:00:00.00,Default,校对,0,0,0,,这一行是校对备注, 带逗号"


def edited_ass(machine: bytes) -> bytes:
    """The machine ASS as a person may return it from an editor: a header comment and a title, a section this program
    does not know, an added style, a Comment event, one event rewritten with layer/name/margins/effect/override tags,
    and a drawing. The first Dialogue line is left exactly as it was."""
    out, seen_dialogue = [], False
    for line in machine.decode("utf-8-sig").split("\r\n"):
        if line == "[V4+ Styles]":
            out += ["[Aegisub Project Garbage]", "Last Style Storage: Default", "Video Position: 42", ""]
        if line.startswith("Dialogue: ") and not seen_dialogue:
            out.append(COMMENT_EVENT)
            seen_dialogue = True
        if line.startswith("Dialogue: ") and line.endswith("ありがとう"):
            start, end = line.split(",")[1:3]
            out += [f"Dialogue: 3,{start},{end},注释,旁白,15,25,40,Banner;10;0;50,{FANCY_TEXT}", f"Dialogue: 1,0:00:02.00,0:00:02.50,Default,,0,0,0,,{DRAWING_TEXT}"]
            continue
        out.append(line)
        if line == "[Script Info]":
            out += ["; 校对：某某", "Title: 校对稿"]
        if line.startswith("Style: Default,"):
            out.append(NOTE_STYLE)
    return b"\xef\xbb\xbf" + "\r\n".join(out).encode("utf-8")


class RevisionHelpers:
    def fails(self, code, call, *args, **kw) -> ContractError:
        with self.assertRaises(ContractError) as caught:
            call(*args, **kw)
        self.assertEqual(caught.exception.code, code, caught.exception.to_dict())
        return caught.exception

    def pending(self) -> list[str]:
        directory = self.project / ".pending"
        return sorted(item.name for item in directory.iterdir()) if directory.is_dir() else []

    def index(self) -> dict:
        return r.load_project(self.root, self.project_id)

    def scratch(self, name: str) -> Path:
        directory = self.tmp / "外部 字幕" / self._testMethodName[5:45]
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def session(self, revision_id: str, fmt: str = "srt"):
        session = revisions.create_session(self.root, self.project_id, revision_id, fmt)
        return session, self.project / session["edit_relative_path"]

    def register(self, file, kind: str = "review", **kw) -> dict:
        return revisions.import_revision(self.root, self.project_id, self.media_id, file, kind, **kw)

    def parts(self, manifest: dict, kind: str = "review"):
        directory = self.project / "revisions" / kind / manifest["revision_id"]
        return (directory, c.load_record("SubtitleView", directory / "view.json"),
                c.load_record("CorrespondenceSet", directory / "correspondences.json"), c.load_record("QualityReport", directory / "quality.json"))

    def resolve(self, revision_id: str) -> dict:
        return revisions.resolve_revision(self.root, self.project_id, revision_id)

    def adopt(self, queued) -> None:
        """Remember the finished combined run this test (or class) works on."""
        envelope, project, task = self.task(queued)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        record, directory = self.machine_revision(project, task)
        self.project, self.project_id, self.machine, self.machine_dir = project, task["project_id"], record, directory
        self.machine_id, self.source_id, self.media_id = record["revision_id"], record["source_revision_id"], record["media_id"]
        self.source_dir = project / "revisions" / "source" / self.source_id


SHARED = ("_tmp", "tmp", "root", "config", "port", "engine_settings", "engine_paths", "model", "double", "project", "project_id", "machine", "machine_dir",
          "machine_id", "source_id", "media_id", "source_dir")


class SharedRunCase(RevisionHelpers, StageTestCase):
    """One real combined run per class. Its tests only add sessions, revisions and attestations to that project."""

    shared: dict | None = None

    def setUp(self):
        cls = type(self)
        if cls.shared is None:
            super().setUp()
            try:
                self.register_engines()
                queued = self.submit("combined")
                self.run_queue()
                self.adopt(queued)
            except BaseException:
                super().tearDown()
                raise
            for item in self.patches:
                item.stop()
            cls.shared = {name: getattr(self, name) for name in SHARED}
        for name, value in cls.shared.items():
            setattr(self, name, value)
        self.patches = []

    def tearDown(self):
        pass   # the shared data root lives until the class is done

    @classmethod
    def tearDownClass(cls):
        if cls.shared is not None:
            cls.shared["_tmp"].cleanup()
            cls.shared = None
        super().tearDownClass()


class SessionAndImportTests(SharedRunCase):
    def test_a_session_is_a_byte_copy_and_the_committed_file_is_never_the_working_file(self):
        before = tree_hashes(self.machine_dir)
        made = {}
        for fmt, name in (("srt", "machine.zh.srt"), ("ass", "machine.zh.ass")):
            session, edit = self.session(self.machine_id, fmt)
            made[fmt] = session
            self.assertEqual(edit, self.project / "reviews" / "sessions" / session["session_id"] / f"edit.{fmt}")
            self.assertEqual(edit.read_bytes(), (self.machine_dir / name).read_bytes())
            self.assertEqual(c.load_record("ReviewSession", edit.parent / "base.json"), session)
            self.assertEqual((session["project_id"], session["base_revision_id"], session["media_id"], session["format"], session["base_subtitle_sha256"]),
                             (self.project_id, self.machine_id, self.media_id, fmt, c.sha256_file(self.machine_dir / name)))
            self.assertEqual(sorted(item.name for item in edit.parent.iterdir()), ["base.json", f"edit.{fmt}"])
        # a second session of the same file is another directory; saving in one working copy changes nothing else
        again, edit = self.session(self.machine_id, "srt")
        self.assertNotEqual(again["session_id"], made["srt"]["session_id"])
        first_edit = self.project / made["srt"]["edit_relative_path"]
        first_edit.write_bytes(first_edit.read_bytes() + b"\r\n" + srt((3, 2900, 2990, "新加的一条")))
        self.assertEqual(edit.read_bytes(), (self.machine_dir / "machine.zh.srt").read_bytes())
        self.assertEqual(tree_hashes(self.machine_dir), before)
        self.assertEqual(self.resolve(self.machine_id)["subtitle_files"]["srt"]["sha256"], before["machine.zh.srt"])
        # a source revision offers its source subtitle; a session copies an existing file and never converts one
        _, source_edit = self.session(self.source_id, "srt")
        self.assertEqual(source_edit.read_bytes(), (self.source_dir / "source.ja.srt").read_bytes())
        error = self.fails("NEEDS_SELECTION", revisions.create_session, self.root, self.project_id, self.source_id, "ass")
        self.assertEqual((error.details["kind"], error.details["available_formats"]), ("format_not_available", ["srt"]))
        self.assertEqual(self.fails("CONFIG_UNSUPPORTED_COMMAND", revisions.create_session, self.root, self.project_id, self.machine_id, "vtt").exit_code, c.EXIT_ARGS)
        self.assertEqual(self.fails("INPUT_MISSING", revisions.create_session, self.root, self.project_id, c.new_id(), "srt").exit_code, c.EXIT_RESOURCE)
        self.fails("PATH_INVALID", revisions.create_session, self.root, self.project_id, "../machine", "srt")

    def test_an_edited_srt_keeps_its_bytes_and_only_identical_unique_cues_count_as_exact(self):
        machine_before = tree_hashes(self.machine_dir)
        session, edit = self.session(self.machine_id, "srt")
        # changed text, a changed time, one cue split in two, everything renumbered
        edited = srt((7, 520, 1480, "译:はい、そうです"), (3, 1700, 2300, "谢谢"), (9, 2300, 2880, "你"), (2, 2880, 2990, "译:ありがとう"))
        edit.write_bytes(edited)
        stat_before = (edit.stat().st_size, edit.stat().st_mtime_ns)
        manifest = self.register(edit, session_id=session["session_id"])
        directory, view, correspondences, quality = self.parts(manifest)
        # the user's file is untouched; the stored copy is the same bytes: not re-encoded, not renumbered
        self.assertEqual((edit.read_bytes(), (edit.stat().st_size, edit.stat().st_mtime_ns)), (edited, stat_before))
        self.assertEqual((directory / "original.srt").read_bytes(), edited)
        self.assertEqual(manifest["original_file_ref"]["sha256"], c.sha256_bytes(edited))
        self.assertEqual((manifest["format"], manifest["encoding"], manifest["extensions"]["encoding_source"]), ("srt", "utf-8", "utf-8"))
        self.assertEqual((manifest["base_revision_id"], manifest["session_id"], manifest["media_id"]), (self.machine_id, session["session_id"], self.media_id))
        self.assertEqual(manifest["extensions"]["imported_file"], {"name": "edit.srt", "sha256": c.sha256_bytes(edited), "bytes": len(edited), "mtime_ns": stat_before[1]})
        self.assertEqual([(cue["notes"]["index_label"], cue["start_ms"], cue["end_ms"], cue["text"]) for cue in view["cues"]],
                         [("7", 520, 1480, "译:はい、そうです"), ("3", 1700, 2300, "谢谢"), ("9", 2300, 2880, "你"), ("2", 2880, 2990, "译:ありがとう")])
        # the mapping is registered, not guessed: identical+unique is exact, the rest is a hint or nothing
        rows = correspondences["correspondences"]
        self.assertEqual([(row["mapping_status"], row["reason"]) for row in rows],
                         [("exact", "identical_and_unique"), ("unmapped", "no_identical_cue"), ("unmapped", "no_identical_cue"), ("candidate", "same_content_time_changed")])
        segments = c.load_record("SegmentSet", self.source_dir / "segments.json")["segments"]
        self.assertEqual([row["source_cue_ids"] for row in rows], [[segments[0]["cue_id"]], [], [], [segments[1]["cue_id"]]])
        self.assertEqual(correspondences["summary"], {"edited_cues": 4, "base_cues": 2, "exact": 1, "candidate": 1, "unmapped": 2, "base_cues_without_exact": 1})
        self.assertEqual((manifest["provenance_status"], correspondences["base_revision_id"], correspondences["base_subtitle_sha256"]),
                         ("partial", self.machine_id, session["base_subtitle_sha256"]))
        # renumbering and a partial mapping are information; the structure is valid
        self.assertEqual(quality["structural_status"], "valid")
        self.assertEqual(found(quality), [("QC06_ORDER", "INFO"), ("QC20_REVIEW_BASE", "INFO")])
        # registering is not reviewing: PARTIAL with nothing confirmed
        resolved = self.resolve(manifest["revision_id"])
        self.assertEqual((resolved["kind"], resolved["directory"]), ("review", f"revisions/review/{manifest['revision_id']}"))
        self.assertEqual(resolved["confirmations"], {"srt": {"subtitle_sha256": c.sha256_bytes(edited), "human_status": "PARTIAL", "confirmed_scopes": [], "attestation_ids": []}})
        self.assertEqual([notice["code"] for notice in resolved["notices"]], ["PROVENANCE_PARTIAL"])
        self.assertIn(manifest["revision_id"], self.index()["review_revision_ids"])
        self.assertEqual(tree_hashes(self.machine_dir), machine_before)

    def test_an_edited_ass_is_stored_byte_for_byte_with_every_field_style_and_tag(self):
        session, edit = self.session(self.machine_id, "ass")
        edited = edited_ass(edit.read_bytes())
        edit.write_bytes(edited)
        manifest = self.register(edit, session_id=session["session_id"])
        directory, view, correspondences, quality = self.parts(manifest)
        self.assertEqual((directory / "original.ass").read_bytes(), edited)
        self.assertEqual((manifest["format"], manifest["encoding"], manifest["extensions"]["encoding_source"], manifest["original_file_ref"]["sha256"]),
                         ("ass", "utf-8-sig", "bom", c.sha256_bytes(edited)))
        # the read-only view names what is in the file and drops nothing it does not understand
        self.assertEqual([cue["kind"] for cue in view["cues"]], ["comment", "dialogue", "dialogue", "dialogue"])
        fancy, drawing = view["cues"][2], view["cues"][3]
        self.assertEqual((fancy["layer"], fancy["style"], fancy["name"], fancy["text"]), (3, "注释", "旁白", "谢谢, 真的谢谢\n第二行"))
        self.assertEqual({key: fancy["fields"][key] for key in ("MarginL", "MarginR", "MarginV", "Effect", "Text")},
                         {"MarginL": "15", "MarginR": "25", "MarginV": "40", "Effect": "Banner;10;0;50", "Text": FANCY_TEXT})
        self.assertEqual((drawing["drawing"], drawing["text"], drawing["fields"]["Text"]), (True, "", DRAWING_TEXT))
        self.assertEqual(view["cues"][0]["fields"]["Text"], "这一行是校对备注, 带逗号")
        self.assertEqual([style["name"] for style in view["styles"]], ["Default", "注释"])
        self.assertEqual(view["script_info"]["Title"], "校对稿")
        self.assertEqual(next(section for section in view["sections"] if section["name"] == "Aegisub Project Garbage")["other_lines"], 2)
        self.assertEqual(view["problems"], [])
        # the untouched event is exact; the rewritten one at the same time is only a hint; added events are unmapped
        self.assertEqual([(row["mapping_status"], row["reason"]) for row in correspondences["correspondences"]],
                         [("unmapped", "no_identical_cue"), ("exact", "identical_and_unique"), ("candidate", "same_time_content_changed"), ("unmapped", "no_identical_cue")])
        self.assertEqual(correspondences["extensions"]["style_diff"], {"styles_added": ["注释"], "styles_removed": [], "styles_changed": [], "script_info_changed": ["Title"]})
        self.assertEqual(manifest["provenance_status"], "partial")
        # a changed style table is put in front of the person as a warning; it does not block registration
        self.assertEqual(quality["structural_status"], "valid")
        style_warnings = [issue for issue in quality["issues"] if issue["rule_id"] == "QC20_REVIEW_BASE" and issue["severity"] == "WARNING"]
        self.assertEqual([issue["observed"]["styles_added"] for issue in style_warnings], [["注释"]])
        self.assertEqual(self.resolve(manifest["revision_id"])["confirmations"]["ass"]["human_status"], "PARTIAL")
        # a session made from the human revision is again a byte copy; registered unchanged, every event maps exactly
        second, second_edit = self.session(manifest["revision_id"], "ass")
        self.assertEqual(second_edit.read_bytes(), edited)
        error = self.fails("NEEDS_SELECTION", revisions.create_session, self.root, self.project_id, manifest["revision_id"], "srt")
        self.assertEqual(error.details["available_formats"], ["ass"])
        unchanged = self.register(second_edit, session_id=second["session_id"])
        _, _, again, again_quality = self.parts(unchanged)
        self.assertEqual((unchanged["provenance_status"], unchanged["base_revision_id"], again["summary"]["exact"], again["extensions"]["style_diff"]),
                         ("exact", manifest["revision_id"], 4, None))
        self.assertEqual(found(again_quality), [])
        self.assertEqual(tree_hashes(directory)["original.ass"], c.sha256_bytes(edited))

    def test_a_file_from_elsewhere_is_registered_unmapped_and_its_encoding_is_never_guessed(self):
        external = self.scratch("别处来的 字幕.srt")
        data = b"\xff\xfe" + srt((1, 100, 1000, "外部字幕"), (2, 1200, 2000, "第二条")).decode("utf-8").encode("utf-16-le")
        external.write_bytes(data)
        manifest = self.register(external)
        directory, view, correspondences, quality = self.parts(manifest)
        self.assertEqual((directory / "original.srt").read_bytes(), data)                      # stored as UTF-16, not transcoded
        self.assertEqual((manifest["base_revision_id"], manifest["session_id"], manifest["provenance_status"], manifest["encoding"], manifest["extensions"]["encoding_source"]),
                         (None, None, "unmapped", "utf-16-le", "bom"))
        self.assertEqual([cue["text"] for cue in view["cues"]], ["外部字幕", "第二条"])
        self.assertEqual({(row["mapping_status"], row["reason"]) for row in correspondences["correspondences"]}, {("unmapped", "no_base")})
        self.assertEqual(found(quality), [("QC20_REVIEW_BASE", "INFO")])
        self.assertEqual([notice["code"] for notice in self.resolve(manifest["revision_id"])["notices"]], ["PROVENANCE_UNMAPPED"])
        self.assertEqual(external.read_bytes(), data)
        # a legacy code page is not guessed: the file is kept as a candidate until the user names the codec
        sjis = self.scratch("シフトJIS.srt")
        sjis_data = srt((1, 100, 1000, "こんにちは")).decode("utf-8").encode("cp932")
        sjis.write_bytes(sjis_data)
        before = self.pending()
        error = self.fails("SUBTITLE_INVALID", self.register, sjis)
        self.assertEqual((error.details["kind"], error.details["rule_id"], error.exit_code), ("unknown_encoding", "QC01_PARSE", c.EXIT_DATA))
        candidate = self.project / error.details["pending"]
        self.assertEqual([name for name in self.pending() if name not in before], [candidate.name])
        self.assertEqual((candidate / "original.srt").read_bytes(), sjis_data)
        note = json.loads((candidate / "candidate.json").read_text(encoding="utf-8"))
        self.assertEqual((note["status"], note["reason"], note["sha256"]), ("NOT_COMMITTED", "unknown_encoding", c.sha256_bytes(sjis_data)))
        chosen = self.register(sjis, encoding="cp932")
        chosen_dir, chosen_view, _, _ = self.parts(chosen)
        self.assertEqual((chosen["encoding"], chosen["extensions"]["encoding_source"], [cue["text"] for cue in chosen_view["cues"]]), ("cp932", "user", ["こんにちは"]))
        self.assertEqual((chosen_dir / "original.srt").read_bytes(), sjis_data)                # the user's bytes, not a UTF-8 rewrite
        # a wrong codec name, a wrong codec and another kind of file leave nothing behind as a revision
        before, index_before = self.pending(), self.index()
        self.assertEqual(self.fails("CONFIG_UNSUPPORTED_COMMAND", self.register, sjis, encoding="no-such-codec").exit_code, c.EXIT_ARGS)
        text_file = self.scratch("字幕.txt")
        text_file.write_bytes(sjis_data)
        self.assertEqual(self.fails("SUBTITLE_INVALID", self.register, text_file).details["kind"], "unsupported_format")
        self.assertEqual(self.fails("INPUT_MISSING", self.register, self.scratch("没有这个文件.srt")).exit_code, c.EXIT_RESOURCE)
        self.assertEqual((self.pending(), self.index()), (before, index_before))

    def test_structural_errors_keep_the_file_as_a_candidate_and_commit_nothing(self):
        duration = media_module.load_media_record(self.project, self.media_id)["duration_ms"]
        session, edit = self.session(self.machine_id, "srt")
        bad = srt((1, 2000, 1000, "倒着的时间"), (2, duration - 500, duration + 2, "超出片尾")) + "\r\n这一行不属于任何字幕\r\n".encode("utf-8")
        edit.write_bytes(bad)
        before, index_before = self.pending(), self.index()
        error = self.fails("SUBTITLE_INVALID", self.register, edit, session_id=session["session_id"])
        self.assertEqual((error.details["rule_ids"], error.details["error_count"], error.exit_code), (["QC01_PARSE", "QC02_TIME", "QC03_BOUNDS"], 3, c.EXIT_DATA))
        # each error names its place in the file: the reversed time line, the block that runs past the end, the stray line
        self.assertEqual(sorted(issue["source_refs"][0] for issue in error.details["issues"]), ["line:2", "line:5", "line:9"])
        # nothing was repaired, clamped or dropped: the bytes and the full report are kept under .pending, outside the revisions
        candidate = self.project / error.details["pending"]
        self.assertEqual([name for name in self.pending() if name not in before], [candidate.name])
        self.assertEqual((candidate / "original.srt").read_bytes(), bad)
        self.assertEqual(edit.read_bytes(), bad)
        note = json.loads((candidate / "candidate.json").read_text(encoding="utf-8"))
        self.assertEqual((note["status"], note["reason"], note["kind"]), ("NOT_COMMITTED", "structural_errors", "review"))
        report = c.load_record("QualityReport", candidate / "quality.json")
        self.assertEqual((report["structural_status"], found(report, ("QC01_PARSE", "QC02_TIME", "QC03_BOUNDS"))),
                         ("invalid", [("QC01_PARSE", "ERROR"), ("QC02_TIME", "ERROR"), ("QC03_BOUNDS", "ERROR")]))
        self.assertTrue((candidate / "view.json").is_file() and (candidate / "correspondences.json").is_file())
        # a candidate is not a revision: not indexed, not resolvable, not a base for a session
        self.assertEqual(self.index(), index_before)
        self.fails("INPUT_MISSING", revisions.resolve_revision, self.root, self.project_id, note["candidate_revision_id"])
        self.fails("INPUT_MISSING", revisions.create_session, self.root, self.project_id, note["candidate_revision_id"], "srt")
        # format quantisation is not an error: one millisecond past the end is kept as written, with a note
        edit.write_bytes(srt((1, 520, 1480, "第一条"), (2, duration - 500, duration + 1, "贴着片尾")))
        manifest = self.register(edit, session_id=session["session_id"])
        _, view, _, quality = self.parts(manifest)
        self.assertEqual((view["cues"][1]["end_ms"], found(quality, ("QC03_BOUNDS",)), quality["structural_status"]), (duration + 1, [("QC03_BOUNDS", "INFO")], "valid"))

    def test_a_file_that_is_still_being_written_is_refused_once_and_nothing_is_registered(self):
        session, edit = self.session(self.machine_id, "srt")
        edit.write_bytes(srt((1, 520, 1480, "还在保存")))
        before, index_before = self.pending(), self.index()
        real_stat = revisions._stat_key
        # the editor writes while the copy is taken: size/mtime differ between the looks around the copy
        calls = []

        def growing(path):
            calls.append(path)
            size, mtime = real_stat(path)
            return (size, mtime + (1 if len(calls) > 1 else 0))

        with patch.object(revisions, "_stat_key", growing):
            error = self.fails("REVIEW_FILE_BUSY", self.register, edit, session_id=session["session_id"])
        self.assertEqual((error.exit_code, error.retryable, error.details["reason"]), (c.EXIT_BUSY, True, "changed_during_copy"))
        self.assertEqual(len(calls), 3)                                                        # looked before, after and once more: no polling loop
        # the editor holds the file exclusively
        attempts = []

        def locked(path):
            attempts.append(path)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")

        with patch.object(revisions, "_read_source_bytes", locked):
            error = self.fails("REVIEW_FILE_BUSY", self.register, edit, session_id=session["session_id"])
        self.assertEqual((error.details["reason"], error.retryable, len(attempts)), ("PermissionError", True, 1))
        # a copy that does not match what is on disk afterwards (bytes swapped under the same size and time) is not accepted either
        with patch.object(revisions, "_read_source_bytes", lambda path: path.read_bytes()[:-2] + b"!!"):
            self.fails("REVIEW_FILE_BUSY", self.register, edit, session_id=session["session_id"])
        self.assertEqual((self.pending(), self.index()), (before, index_before))
        # once the file is at rest the same call registers it
        manifest = self.register(edit, session_id=session["session_id"])
        self.assertEqual(manifest["original_file_ref"]["sha256"], c.sha256_file(edit))

    def test_confirmation_is_explicit_per_scope_and_bound_to_one_file_hash(self):
        session, edit = self.session(self.machine_id, "srt")
        edit.write_bytes(srt((1, 520, 1480, "是的，没错"), (2, 1820, 2880, "谢谢")))
        manifest = self.register(edit, session_id=session["session_id"])
        revision_id, sha = manifest["revision_id"], manifest["original_file_ref"]["sha256"]
        files_before = tree_hashes(self.project / "revisions" / "review" / revision_id)
        status = lambda rid=revision_id, fmt="srt": (self.resolve(rid)["confirmations"][fmt]["human_status"], self.resolve(rid)["confirmations"][fmt]["confirmed_scopes"])
        self.assertEqual(status(), ("PARTIAL", []))
        # nothing is confirmed implicitly, in bulk, twice in one call, or for a scope the format does not have
        for scopes in ([], ["all"], ["all-auto"], ["text", "text"], ["text", "layout"], ""):
            self.assertEqual(self.fails("CONFIG_UNSUPPORTED_COMMAND", revisions.attest_revision, self.root, self.project_id, revision_id, scopes).exit_code, c.EXIT_ARGS)
        self.fails("CONFIG_UNSUPPORTED_COMMAND", revisions.attest_revision, self.root, self.project_id, revision_id, ["text", "style"])
        self.assertEqual(status(), ("PARTIAL", []))
        first = revisions.attest_revision(self.root, self.project_id, revision_id, ["text"], "只看了文字")
        self.assertEqual(c.load_record("Attestation", self.project / "reviews" / "attestations" / f"{first['attestation_id']}.json"), first)
        self.assertEqual((first["revision_id"], first["subtitle_sha256"], first["media_id"], first["scopes"], first["actor"], first["note"]),
                         (revision_id, sha, self.media_id, ["text"], "user", "只看了文字"))
        self.assertEqual(status(), ("PARTIAL", ["text"]))
        second = revisions.attest_revision(self.root, self.project_id, revision_id, ["timing"])
        self.assertEqual(status(), ("REVIEWED", ["text", "timing"]))                           # an SRT needs text and timing; style is not applicable
        self.assertEqual(self.resolve(revision_id)["confirmations"]["srt"]["attestation_ids"], sorted([first["attestation_id"], second["attestation_id"]]))
        self.assertEqual(tree_hashes(self.project / "revisions" / "review" / revision_id), files_before)   # confirming writes beside the revision, never into it
        # the same text saved again is another file revision: it starts unconfirmed
        edit.write_bytes(edit.read_bytes() + b"\r\n")
        resaved = self.register(edit, session_id=session["session_id"])
        self.assertNotEqual(resaved["original_file_ref"]["sha256"], sha)
        self.assertEqual(status(resaved["revision_id"]), ("PARTIAL", []))
        # an ASS is only REVIEWED with its style confirmed as well
        ass_session, ass_edit = self.session(self.machine_id, "ass")
        ass = self.register(ass_edit, session_id=ass_session["session_id"])
        revisions.attest_revision(self.root, self.project_id, ass["revision_id"], ["timing", "text"])
        self.assertEqual(status(ass["revision_id"], "ass"), ("PARTIAL", ["text", "timing"]))
        revisions.attest_revision(self.root, self.project_id, ass["revision_id"], ["style"])
        self.assertEqual(status(ass["revision_id"], "ass"), ("REVIEWED", ["text", "timing", "style"]))
        # a machine revision has two files: the confirmation names one of them and does not spread to the other
        self.assertEqual({fmt: item["human_status"] for fmt, item in self.resolve(self.machine_id)["confirmations"].items()}, {"srt": "UNREVIEWED", "ass": "UNREVIEWED"})
        error = self.fails("NEEDS_SELECTION", revisions.attest_revision, self.root, self.project_id, self.machine_id, ["text"])
        self.assertEqual((error.details["kind"], error.details["available_formats"]), ("format_required", ["ass", "srt"]))
        revisions.attest_revision(self.root, self.project_id, self.machine_id, ["text", "timing"], fmt="srt")
        self.assertEqual({fmt: item["human_status"] for fmt, item in self.resolve(self.machine_id)["confirmations"].items()}, {"srt": "REVIEWED", "ass": "UNREVIEWED"})
        self.assertEqual(self.fails("NEEDS_SELECTION", revisions.attest_revision, self.root, self.project_id, revision_id, ["text"], fmt="ass").details["kind"], "format_not_available")
        # a record that is not a valid attestation is reported and never counted
        broken = self.project / "reviews" / "attestations" / "手写的.json"
        broken.write_text(json.dumps({**first, "revision_id": resaved["revision_id"], "subtitle_sha256": resaved["original_file_ref"]["sha256"], "scopes": ["text", "timing"],
                                      "attestation_id": "not-an-id"}), encoding="utf-8")
        resolved = self.resolve(resaved["revision_id"])
        self.assertEqual((resolved["confirmations"]["srt"]["human_status"], resolved["confirmations"]["srt"]["confirmed_scopes"],
                          [notice["files"] for notice in resolved["notices"] if notice["code"] == "ATTESTATION_UNREADABLE"]), ("PARTIAL", [], [["手写的.json"]]))
        broken.unlink()

    def test_a_source_ass_feeds_only_its_displayed_text_and_times_and_keeps_its_own_bytes(self):
        lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1280", "PlayResY: 720", "", "[V4+ Styles]",
                 "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                 "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                 "Style: 日文,Yu Gothic,54,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,20,128", "", "[Events]",
                 "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                 "Comment: 0,0:00:00.00,0:00:00.50,日文,,0,0,0,,聞き取りメモ",
                 "Dialogue: 0,0:00:01.90,0:00:02.60,日文,,0,0,0,,{\\i1}ありがとう{\\i0}ございます",
                 "Dialogue: 0,0:00:00.50,0:00:01.40,日文,話者,0,0,0,,{\\an8}はい",
                 "Dialogue: 1,0:00:00.50,0:00:01.40,日文,,0,0,0,,{\\p1}m 0 0 l 10 0 10 10{\\p0}",
                 "Dialogue: 0,0:00:02.60,0:00:02.90,日文,,0,0,0,,{\\fad(100,100)}", ""]
        data = "\n".join(lines).encode("utf-8")                                              # no BOM, LF: stored exactly so
        file = self.scratch("聞き取り.ass")
        file.write_bytes(data)
        manifest = self.register(file, "source")
        directory = self.project / "revisions" / "source" / manifest["revision_id"]
        self.assertEqual((directory / "original.ass").read_bytes(), data)
        self.assertEqual((manifest["origin"], manifest["parent_revision_id"], manifest["provenance_status"], manifest["source_subtitle_ref"]["sha256"]),
                         ("human", None, "unmapped", c.sha256_bytes(data)))
        self.assertEqual((manifest["extensions"]["format"], manifest["extensions"]["encoding"], manifest["extensions"]["encoding_source"]), ("ass", "utf-8", "utf-8"))
        segment_set = c.load_record("SegmentSet", directory / "segments.json")
        # displayed text and times in time order; tags, the comment, the drawing and the event without text are not source text
        self.assertEqual([(s["index"], s["start_ms"], s["end_ms"], s["source_text"]) for s in segment_set["segments"]], [(1, 500, 1400, "はい"), (2, 1900, 2600, "ありがとうございます")])
        self.assertEqual({(s["timing_source"], tuple(s["source_ranges"]), tuple(s["visual_ids"]), tuple(s["flags"]), s["role"]) for s in segment_set["segments"]},
                         {("human", (), (), ("human_authored",), "unknown")})
        extensions = segment_set["extensions"]
        self.assertEqual(([(item["line"], item["reason"]) for item in extensions["omitted_events"]], extensions["ordered_by_start_time"], extensions["timing_authority"]),
                         ([(12, "comment"), (15, "drawing"), (16, "no_visible_text")], True, "human"))
        self.assertEqual((segment_set["origin"], segment_set["recognition_mode"], segment_set["coverage"]["status"], segment_set["coverage"]["audited"]),
                         ("human", "supplied_source", "human_authored", False))
        resolved = self.resolve(manifest["revision_id"])
        self.assertEqual((resolved["kind"], sorted(resolved["subtitle_files"]), resolved["confirmations"]["ass"]["human_status"]), ("source", ["ass"], "PARTIAL"))
        self.assertIn(manifest["revision_id"], self.index()["source_revision_ids"])
        # a Chinese review is never based on a source revision, nor a source on a machine translation
        error = self.fails("CONFIG_UNSUPPORTED_COMMAND", self.register, file, "review", base_revision_id=manifest["revision_id"])
        self.assertEqual((error.details["base_kind"], error.details["allowed_base_kinds"]), ("source", ["machine", "review"]))
        self.fails("CONFIG_UNSUPPORTED_COMMAND", self.register, file, "source", base_revision_id=self.machine_id)
        self.fails("CONFIG_UNSUPPORTED_COMMAND", self.register, file, "machine")


class FreshRunCase(RevisionHelpers, StageTestCase):
    """Tests that add machine versions or other media to the project get a data root of their own."""

    def first_run(self, video=None):
        self.register_engines()
        queued = self.submit("combined", video)
        self.run_queue()
        self.adopt(queued)

    def rerun(self, mode="combined", video=None, **kw):
        queued = self.submit(mode, video, project_id=self.project_id, **kw)
        self.run_queue()
        envelope, project, task = self.task(queued)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        return task, *self.machine_revision(project, task)


class LaterVersionTests(FreshRunCase):
    def test_newer_machine_versions_leave_a_registered_human_file_where_and_as_it_is(self):
        self.first_run()
        session, edit = self.session(self.machine_id, "srt")
        edit.write_bytes(srt((1, 520, 1480, "是的，没错"), (2, 1820, 2880, "谢谢")))
        human = self.register(edit, session_id=session["session_id"])
        human_id = human["revision_id"]
        revisions.attest_revision(self.root, self.project_id, human_id, ["text", "timing"], "两项都看过")
        human_dir = self.project / "revisions" / "review" / human_id
        late_session, late_edit = self.session(self.machine_id, "srt")                         # opened now, saved after the reruns
        kept = (tree_hashes(human_dir), tree_hashes(self.machine_dir), tree_hashes(self.source_dir), tree_hashes(self.project / "reviews" / "attestations"))
        before = self.resolve(human_id)
        self.assertEqual(([notice["code"] for notice in before["notices"]], before["confirmations"]["srt"]["human_status"]), (["PROVENANCE_UNMAPPED"], "REVIEWED"))
        # recognised again another way, translated again with another dictionary: each is a new machine revision of the same media
        _, second, _ = self.rerun(timing_mode="speech")
        paths = w04.write_dictionary_files(self.tmp / "用户词典", gpt="// GPT 词典\nアルダン\t阿尔丹\n")
        _, third, _ = self.rerun(settings=w04.dictionary_settings(paths, endpoint=f"http://127.0.0.1:{self.port}"))
        newer = [second["revision_id"], third["revision_id"]]
        self.assertEqual(self.index()["machine_revision_ids"], [self.machine_id, *newer])
        self.assertEqual((second["media_id"], third["media_id"]), (self.media_id, self.media_id))
        self.assertNotEqual(second["source_revision_id"], self.source_id)
        # the human revision: same path, same bytes, same media, same confirmation; only a notice is added
        self.assertEqual((tree_hashes(human_dir), tree_hashes(self.machine_dir), tree_hashes(self.source_dir), tree_hashes(self.project / "reviews" / "attestations")), kept)
        after = self.resolve(human_id)
        self.assertEqual({key: after[key] for key in after if key != "notices"}, {key: before[key] for key in before if key != "notices"})
        self.assertEqual(after["notices"][0], {"code": "BASE_SUPERSEDED", "base_revision_id": self.machine_id, "newer_revision_ids": newer,
                                               "message": "有新的机器版本，当前人工稿基于旧版本；人工稿保持原样，仍可导出"})
        self.assertEqual([notice["code"] for notice in after["notices"]], ["BASE_SUPERSEDED", "PROVENANCE_UNMAPPED"])
        self.assertEqual(self.index()["review_revision_ids"], [human_id])                      # nothing was re-based, merged or re-created
        # a file edited against the old base is still registered to that base, with the notice, never moved to the newest one
        late_edit.write_bytes(srt((1, 520, 1480, "译:はい、そうです"), (2, 1820, 2880, "谢谢你")))
        late = self.register(late_edit, session_id=late_session["session_id"])
        _, _, correspondences, quality = self.parts(late)
        self.assertEqual((late["base_revision_id"], late["extensions"]["superseded_by_at_import"], correspondences["base_subtitle_sha256"], late["provenance_status"]),
                         (self.machine_id, newer, kept[1]["machine.zh.srt"], "partial"))
        superseded = [issue for issue in quality["issues"] if issue["rule_id"] == "QC20_REVIEW_BASE" and issue["observed"].get("code") == "BASE_SUPERSEDED"]
        self.assertEqual([(issue["severity"], issue["observed"]["base_revision_id"], issue["observed"]["newer_revision_ids"]) for issue in superseded],
                         [("INFO", self.machine_id, newer)])
        self.assertEqual(quality["structural_status"], "valid")
        # the old human revision stays usable as a base and as a file; the new machine version offers its own session
        again, again_edit = self.session(human_id, "srt")
        self.assertEqual(again_edit.read_bytes(), (human_dir / "original.srt").read_bytes())
        on_human = self.register(again_edit, session_id=again["session_id"])
        self.assertEqual((on_human["base_revision_id"], on_human["provenance_status"], on_human["extensions"]["superseded_by_at_import"]), (human_id, "exact", newer))
        self.assertEqual([notice["code"] for notice in self.resolve(on_human["revision_id"])["notices"]], ["BASE_SUPERSEDED"])
        _, newest_edit = self.session(third["revision_id"], "srt")
        self.assertEqual(self.resolve(self.register(newest_edit, base_revision_id=third["revision_id"])["revision_id"])["notices"], [])
        self.assertEqual(self.resolve(self.machine_id)["notices"], [])                         # a machine revision is not "superseded": it is one of several versions

    def test_a_corrected_source_is_a_new_source_revision_translated_without_recognising_again(self):
        self.first_run()
        zh_session, zh_edit = self.session(self.machine_id, "srt")
        zh_edit.write_bytes(srt((1, 520, 1480, "是的，没错"), (2, 1820, 2880, "谢谢")))
        zh = self.register(zh_edit, session_id=zh_session["session_id"])
        zh_dir = self.project / "revisions" / "review" / zh["revision_id"]
        first_run_stages = r.run_dir(self.project, self.machine["run_id"]) / "stages"
        kept = (tree_hashes(zh_dir), tree_hashes(self.machine_dir), tree_hashes(self.source_dir), tree_hashes(first_run_stages))
        machine_segments = c.load_record("SegmentSet", self.source_dir / "segments.json")["segments"]
        self.assertTrue(all(segment["source_ranges"] for segment in machine_segments))      # the recognised source has word/character coordinates
        # the user corrects the Japanese: first cue untouched, second cue reworded and split at a time no recogniser produced
        session, edit = self.session(self.source_id, "srt")
        edited = srt((1, 520, 1480, "はい、そうです"), (2, 1820, 2300, "ありがとう"), (3, 2300, 2880, "ございます、\r\nアルダンさん"))
        edit.write_bytes(b"\xef\xbb\xbf" + edited)
        source = self.register(edit, "source", session_id=session["session_id"])
        source_dir = self.project / "revisions" / "source" / source["revision_id"]
        self.assertEqual((source_dir / "original.srt").read_bytes(), b"\xef\xbb\xbf" + edited)
        self.assertEqual((source["origin"], source["parent_revision_id"], source["provenance_status"], source["extensions"]["session_id"]),
                         ("human", self.source_id, "partial", session["session_id"]))
        rows = c.load_record("CorrespondenceSet", source_dir / "correspondences.json")["correspondences"]
        # the unchanged cue is exact; the same words at a changed time are only a hint for a person; the new cue is unmapped
        self.assertEqual([(row["mapping_status"], row["reason"], row["source_cue_ids"]) for row in rows],
                         [("exact", "identical_and_unique", [machine_segments[0]["cue_id"]]), ("candidate", "same_content_time_changed", [machine_segments[1]["cue_id"]]),
                          ("unmapped", "no_identical_cue", [])])
        segments = c.load_record("SegmentSet", source_dir / "segments.json")["segments"]
        self.assertEqual([(s["start_ms"], s["end_ms"], s["source_text"]) for s in segments],
                         [(520, 1480, "はい、そうです"), (1820, 2300, "ありがとう"), (2300, 2880, "ございます、\nアルダンさん")])
        # no old coordinate is attached to the user's text, not even for the cue that did not change; old ids are not reused
        self.assertEqual({(s["timing_source"], tuple(s["source_ranges"]), tuple(s["visual_ids"])) for s in segments}, {("human", (), ())})
        self.assertFalse({s["cue_id"] for s in segments} & {s["cue_id"] for s in machine_segments})
        self.assertEqual(self.index()["source_revision_ids"], [self.source_id, source["revision_id"]])
        # "new machine translation from this source": translation, publishing and quality only
        self.double.calls.clear()
        requests_before = len(w04.requests_log(self.model))
        task, machine, machine_dir = self.rerun("supplied_source", "supplied-source", source_revision_id=source["revision_id"], source_media_id=self.media_id)
        statuses = self.statuses(task)
        self.assertEqual((statuses["OCR"], statuses["ASR"], self.double.calls), (("SKIPPED", "USER_SOURCE"), ("SKIPPED", "USER_SOURCE"), []))
        pointer = json.loads((self.stage_dir(self.project, task, "SOURCE") / "source_ref.json").read_text(encoding="utf-8"))
        self.assertEqual((pointer["source_revision_id"], pointer["reused_existing_revision"]), (source["revision_id"], True))
        self.assertEqual((machine["source_revision_id"], machine["media_id"]), (source["revision_id"], self.media_id))
        self.assertNotEqual(machine["revision_id"], self.machine_id)
        self.assertGreater(len(w04.requests_log(self.model)), requests_before)                # translated in full for the new source
        view = subtitles.parse_view((machine_dir / "machine.zh.srt").read_bytes(), "srt")
        self.assertEqual([(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in view["cues"]],
                         [(520, 1480, "译:はい、そうです"), (1820, 2300, "译:ありがとう"), (2300, 2880, "译:ございます、\n译:アルダンさん")])   # the user's times and split
        timing = json.loads((machine_dir / "timing.ms.json").read_text(encoding="utf-8"))["cues"]
        self.assertEqual([(cue["cue_id"], cue["start_ms"], cue["end_ms"]) for cue in timing], [(s["cue_id"], s["start_ms"], s["end_ms"]) for s in segments])
        # the earlier Chinese human revision, the first machine result and all recognition evidence are as they were
        self.assertEqual((tree_hashes(zh_dir), tree_hashes(self.machine_dir), tree_hashes(self.source_dir), tree_hashes(first_run_stages)), kept)
        self.assertEqual(tree_hashes(source_dir)["original.srt"], c.sha256_bytes(b"\xef\xbb\xbf" + edited))
        zh_resolved = self.resolve(zh["revision_id"])
        self.assertEqual((zh_resolved["manifest"]["base_revision_id"], [n.get("newer_revision_ids") for n in zh_resolved["notices"] if n["code"] == "BASE_SUPERSEDED"]),
                         (self.machine_id, [[machine["revision_id"]]]))
        # the same source revision submitted again is the same translation: nothing is translated twice
        requests_before = len(w04.requests_log(self.model))
        again_task, again, _ = self.rerun("supplied_source", "supplied-source", source_revision_id=source["revision_id"], source_media_id=self.media_id)
        self.assertEqual((again["revision_id"], len(w04.requests_log(self.model))), (machine["revision_id"], requests_before))


class MediaBindingTests(FreshRunCase):
    def test_a_file_belongs_to_one_media_version_and_a_changed_or_missing_video_has_one_answer(self):
        video = self.tmp / "私有 片源.mp4"                                                     # this test edits and deletes the video: never the shared fixture
        shutil.copy2(self.video, video)
        self.first_run(video)
        _, other, _ = self.rerun("visual_only", self.silent)                                   # another video in the same project is another media version
        other_media = other["media_id"]
        self.assertNotEqual(other_media, self.media_id)
        self.assertEqual(self.index()["media_ids"], [self.media_id, other_media])
        session, edit = self.session(self.machine_id, "srt")
        edit.write_bytes(srt((1, 520, 1480, "是的"), (2, 1820, 2880, "谢谢")))
        human = self.register(edit, session_id=session["session_id"])
        human_dir = self.project / "revisions" / "review" / human["revision_id"]
        kept, resolved = tree_hashes(human_dir), self.resolve(human["revision_id"])
        state = (self.pending(), self.index())

        def refused(code, exit_code, *args, **kw):
            error = self.fails(code, *args, **kw)
            self.assertEqual(error.exit_code, exit_code)
            self.assertEqual(((self.pending(), self.index()), tree_hashes(human_dir), self.resolve(human["revision_id"])), (state, kept, resolved))
            return error

        # the same timeline is never assumed for two videos: not through the session, not through the base
        _, again = self.session(self.machine_id, "srt")
        error = refused("MEDIA_CHANGED", 3, revisions.import_revision, self.root, self.project_id, other_media, edit, "review", session_id=session["session_id"])
        self.assertEqual(error.details, {"rule_id": "QC21_MEDIA_BINDING", "session_media_id": self.media_id, "media_id": other_media})
        error = refused("MEDIA_CHANGED", 3, revisions.import_revision, self.root, self.project_id, other_media, again, "review", base_revision_id=self.machine_id)
        self.assertEqual(error.details, {"rule_id": "QC21_MEDIA_BINDING", "base_media_id": self.media_id, "media_id": other_media})
        error = refused("MEDIA_CHANGED", 3, revisions.import_revision, self.root, self.project_id, other_media, again, "source", base_revision_id=self.source_id)
        self.assertEqual(error.details["base_media_id"], self.media_id)
        unknown = c.new_id()
        error = refused("INPUT_MISSING", 3, revisions.import_revision, self.root, self.project_id, unknown, again, "review")
        self.assertEqual(error.details, {"rule_id": "QC21_MEDIA_BINDING", "media_id": unknown})
        refused("PATH_INVALID", 2, revisions.import_revision, self.root, self.project_id, "../" + self.media_id, again, "review")
        # the user may still register the file for the other video on purpose: bound to it, with no claimed relation to anything
        elsewhere = revisions.import_revision(self.root, self.project_id, other_media, again, "review")
        self.assertEqual((elsewhere["media_id"], elsewhere["base_revision_id"], elsewhere["provenance_status"]), (other_media, None, "unmapped"))
        self.assertEqual(self.resolve(elsewhere["revision_id"])["media_id"], other_media)
        state = (self.pending(), self.index())
        # the video was replaced by other content under the same name
        original = video.read_bytes()
        video.write_bytes(original + b"\x00" * 16)
        error = refused("MEDIA_CHANGED", 3, self.register, edit, session_id=session["session_id"])
        self.assertEqual((error.details["media_id"], error.details["expected"], error.details["observed"]),
                         (self.media_id, c.sha256_bytes(original), c.sha256_bytes(original + b"\x00" * 16)))
        refused("MEDIA_CHANGED", 3, self.register, edit, "source")
        # the video is gone: the answer is "relocate the media", and what was registered before stays as it is
        video.unlink()
        error = refused("INPUT_MISSING", 3, self.register, edit, session_id=session["session_id"])
        self.assertEqual((error.details["media_id"], error.details["path"]), (self.media_id, str(video)))
        self.assertEqual(self.session(human["revision_id"], "srt")[1].read_bytes(), (human_dir / "original.srt").read_bytes())
        # the same bytes back at the same place (touched, so the stat differs) are the same media again
        video.write_bytes(original)
        back = self.register(edit, session_id=session["session_id"])
        self.assertEqual((back["media_id"], back["base_revision_id"], tree_hashes(human_dir)), (self.media_id, self.machine_id, kept))


class CommandLineTests(FreshRunCase):
    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        self.assertEqual(c.validate_record("OperationResult", json.loads(completed.stdout)), [], completed.stderr)   # always exactly one JSON summary
        return completed.returncode, json.loads(completed.stdout), completed.stderr

    def refused(self, exit_code, code, *args):
        before = (self.pending(), self.index())
        status, result, _ = self.cli(*args)
        self.assertEqual((status, result["ok"], result["error"]["code"], result["ids"], result["artifact_refs"]), (exit_code, False, code, {}, []))
        self.assertEqual((self.pending(), self.index()), before)
        return result["error"]

    def test_the_commands_take_one_file_from_working_copy_to_confirmation(self):
        self.first_run()
        where = ("--data-root", str(self.root), "--project", self.project_id)
        # a working copy of one concrete file of one concrete revision
        code, result, stderr = self.cli("review", "create", *where, "--revision", self.machine_id, "--format", "srt")
        self.assertEqual((code, result["ok"], result["operation"], result["error"], result["warnings"]), (0, True, "review.create", None, []), stderr)
        made = result["ids"]
        edit = Path(made["edit_file"])
        self.assertEqual((made["base_revision_id"], made["media_id"], made["format"], edit, edit.read_bytes()),
                         (self.machine_id, self.media_id, "srt", self.project / made["edit_relative_path"], (self.machine_dir / "machine.zh.srt").read_bytes()))
        # the saved file becomes an immutable human revision; the summary names exactly what was registered
        edit.write_bytes(srt((1, 520, 1480, "是的，没错"), (2, 1820, 2880, "谢谢")))
        digest = c.sha256_bytes(edit.read_bytes())
        code, result, stderr = self.cli("review", "import", *where, "--media", self.media_id, "--file", str(edit), "--session", made["session_id"])
        self.assertEqual((code, result["ok"], result["operation"], result["error"]), (0, True, "review.import", None), stderr)
        ids = result["ids"]
        human_id = ids["revision_id"]
        self.assertEqual((ids["kind"], ids["media_id"], ids["base_revision_id"], ids["format"], ids["session_id"], ids["encoding"], ids["subtitle_sha256"]),
                         ("review", self.media_id, self.machine_id, "srt", made["session_id"], "utf-8", digest))
        self.assertEqual((ids["structural_status"], ids["provenance_status"], ids["human_status"], ids["confirmed_scopes"], ids["issue_counts"]["ERROR"]),
                         ("valid", "unmapped", "PARTIAL", [], 0))
        self.assertEqual([(ref["path"], ref["sha256"]) for ref in result["artifact_refs"]], [(f"revisions/review/{human_id}/original.srt", digest)])
        self.assertEqual([warning["code"] for warning in result["warnings"]], ["PROVENANCE_UNMAPPED"])
        self.assertEqual(self.index()["review_revision_ids"], [human_id])
        # confirmation names its scopes; a machine revision has two files, so the file is named too
        code, result, stderr = self.cli("review", "attest", *where, "--revision", human_id, "--scopes", "text", "--note", "只看了文字")
        self.assertEqual((code, result["operation"], result["ids"]["scopes"], result["ids"]["human_status"], result["ids"]["confirmed_scopes"], result["ids"]["subtitle_sha256"]),
                         (0, "review.attest", ["text"], "PARTIAL", ["text"], digest), stderr)
        code, result, _ = self.cli("review", "attest", *where, "--revision", human_id, "--scopes", "timing, text")
        self.assertEqual((code, result["ids"]["format"], result["ids"]["human_status"], result["ids"]["confirmed_scopes"]), (0, "srt", "REVIEWED", ["text", "timing"]))
        code, result, _ = self.cli("review", "attest", *where, "--revision", self.machine_id, "--scopes", "text,timing", "--format", "srt")
        self.assertEqual((code, result["ids"]["format"], result["ids"]["human_status"]), (0, "srt", "REVIEWED"))
        confirmations = self.resolve(self.machine_id)["confirmations"]
        self.assertEqual((confirmations["srt"]["human_status"], confirmations["ass"]["human_status"]), ("REVIEWED", "UNREVIEWED"))
        self.assertEqual(len(list((self.project / "reviews" / "attestations").glob("*.json"))), 3)
        # a corrected source: registered, and the translation is the user's next, separate command
        source_file = self.scratch("原文.srt")
        source_file.write_bytes(srt((1, 520, 1480, "はい、そうです"), (2, 1820, 2880, "ありがとうございます")))
        code, result, stderr = self.cli("source", "import", *where, "--media", self.media_id, "--file", str(source_file), "--base", self.source_id)
        self.assertEqual((code, result["ok"], result["operation"]), (0, True, "source.import"), stderr)
        ids = result["ids"]
        self.assertEqual((ids["kind"], ids["base_revision_id"], ids["format"], ids["segments"], ids["omitted_events"], ids["human_status"], ids["session_id"]),
                         ("source", self.source_id, "srt", 2, 0, "PARTIAL", None))
        self.assertEqual(ids["next_command"], ["submit", "--mode", "supplied_source", "--project", self.project_id, "--source-revision", ids["revision_id"]])
        self.assertEqual((self.index()["machine_revision_ids"], r.list_envelopes(self.root)[1:]), ([self.machine_id], []))    # nothing was queued or translated
        code, result, stderr = self.cli(ids["next_command"][0], "--config", str(self.root / APP_CONFIG_NAME), *ids["next_command"][1:])
        self.assertEqual((code, result["ok"], result["ids"]["project_id"], result["ids"]["status"]), (0, True, self.project_id, "QUEUED"), stderr)
        self.double.calls.clear()
        self.run_queue()
        envelope, project, task = self.task(r.load_envelope(self.root, result["ids"]["request_id"]))
        self.assertEqual((envelope["status"], envelope["request"]["source_revision_id"], self.double.calls), ("COMPLETED", ids["revision_id"], []), envelope["last_error"])
        record, _ = self.machine_revision(project, task)
        self.assertEqual((record["source_revision_id"], record["media_id"], self.index()["machine_revision_ids"]),
                         (ids["revision_id"], self.media_id, [self.machine_id, record["revision_id"]]))
        code, result, _ = self.cli("review", "attest", *where, "--revision", human_id, "--scopes", "text")      # the older human file still answers, with the notice
        self.assertEqual((code, result["ids"]["human_status"], [warning["code"] for warning in result["warnings"]]),
                         (0, "REVIEWED", ["BASE_SUPERSEDED", "PROVENANCE_UNMAPPED"]))

    def test_every_refusal_is_one_json_answer_with_its_exit_code_and_registers_nothing(self):
        self.first_run()
        _, other, _ = self.rerun("visual_only", self.silent)
        where = ("--data-root", str(self.root), "--project", self.project_id)
        to_media = (*where, "--media", self.media_id)
        _, edit = self.session(self.machine_id, "srt")
        human = self.register(edit, base_revision_id=self.machine_id)
        # 2: arguments and choices the user has to make; a v1 flag is refused, never given another meaning
        error = self.refused(2, "CONFIG_UNSUPPORTED_COMMAND", "review", "import", *to_media, "--file", str(edit), "--zh-file", str(edit))
        self.assertIn("--zh-file", error["message"])
        self.refused(2, "CONFIG_UNSUPPORTED_COMMAND", "review", "import", *to_media)
        self.refused(2, "CONFIG_UNSUPPORTED_COMMAND", "review", "create", *where, "--revision", self.machine_id, "--format", "vtt")
        self.refused(2, "CONFIG_UNSUPPORTED_COMMAND", "source", "import", *to_media, "--file", str(edit), "--base", self.machine_id)
        self.refused(2, "CONFIG_UNSUPPORTED_COMMAND", "review", "import", *to_media, "--file", str(edit), "--encoding", "no-such-codec")
        self.refused(2, "PATH_INVALID", "review", "create", *where, "--revision", "latest", "--format", "srt")
        error = self.refused(2, "NEEDS_SELECTION", "review", "create", *where, "--revision", human["revision_id"], "--format", "ass")
        self.assertEqual(error["details"], {"kind": "format_not_available", "available_formats": ["srt"]})
        error = self.refused(2, "NEEDS_SELECTION", "review", "attest", *where, "--revision", self.machine_id, "--scopes", "text")
        self.assertEqual(error["details"], {"kind": "format_required", "available_formats": ["ass", "srt"]})
        for scopes in ("all-auto", "all", "", "text,text", "text,style"):
            with self.subTest(scopes=scopes):
                self.refused(2, "CONFIG_UNSUPPORTED_COMMAND", "review", "attest", *where, "--revision", human["revision_id"], "--scopes", scopes)
        self.assertEqual(list((self.project / "reviews" / "attestations").glob("*.json")), [])
        # 3: the media or the file is not there, or is not the one the subtitle was made for
        error = self.refused(3, "MEDIA_CHANGED", "review", "import", *where, "--media", other["media_id"], "--file", str(edit), "--base", self.machine_id)
        self.assertEqual(error["details"], {"rule_id": "QC21_MEDIA_BINDING", "base_media_id": self.media_id, "media_id": other["media_id"]})
        self.refused(3, "INPUT_MISSING", "review", "import", *to_media, "--file", str(self.scratch("没有这个文件.srt")))
        self.refused(3, "INPUT_MISSING", "review", "import", *where, "--media", c.new_id(), "--file", str(edit))
        self.refused(3, "INPUT_MISSING", "review", "create", *where, "--revision", c.new_id(), "--format", "srt")
        self.refused(3, "INPUT_MISSING", "review", "create", "--data-root", str(self.root), "--project", c.new_id(), "--revision", self.machine_id, "--format", "srt")
        # 5: the file is kept as a candidate, named in the answer, and is not a revision
        bad = self.scratch("坏的.srt")
        bad.write_bytes(srt((1, 2000, 1000, "倒着的时间"), (2, 1500, 1800, "正常的一条")))
        before = (self.pending(), self.index())
        code, result, _ = self.cli("review", "import", *to_media, "--file", str(bad))
        details = result["error"]["details"]
        self.assertEqual((code, result["ok"], result["error"]["code"], details["rule_ids"], details["error_count"], result["ids"]), (5, False, "SUBTITLE_INVALID", ["QC02_TIME"], 1, {}))
        self.assertEqual((self.index(), [f".pending/{name}" for name in self.pending() if name not in before[0]]), (before[1], [details["pending"]]))
        self.assertEqual((self.project / details["pending"] / "original.srt").read_bytes(), bad.read_bytes())
        self.refused(5, "SUBTITLE_INVALID", "review", "import", *to_media, "--file", str(self.scratch("不是字幕.txt")))

    @unittest.skipUnless(os.name == "nt", "byte-range locks that make another program's read fail are a Windows behaviour")
    def test_a_file_locked_by_the_editor_is_answered_with_busy_and_registers_after_the_lock_is_gone(self):
        import msvcrt
        self.first_run()
        to_media = ("--data-root", str(self.root), "--project", self.project_id, "--media", self.media_id)
        _, edit = self.session(self.machine_id, "srt")
        data = edit.read_bytes()
        with open(edit, "r+b") as handle:                                                      # another program holds the file while it writes
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, len(data))
            try:
                error = self.refused(7, "REVIEW_FILE_BUSY", "review", "import", *to_media, "--file", str(edit))
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, len(data))
        self.assertEqual((error["retryable"], error["details"]), (True, {"reason": "PermissionError"}))
        self.assertEqual(edit.read_bytes(), data)
        code, result, stderr = self.cli("review", "import", *to_media, "--file", str(edit))
        self.assertEqual((code, result["ok"], result["ids"]["subtitle_sha256"]), (0, True, c.sha256_bytes(data)), stderr)


class PipelineIsolationTests(unittest.TestCase):
    """Plan 12.1: confirming is the user's action. Nothing that runs unattended may be able to reach it."""

    def test_only_the_users_attest_command_can_reach_attest_revision(self):
        package = Path(revisions.__file__).parent
        modules = {path.name: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(package.glob("*.py")) if path.name != "revisions.py"}
        self.assertLessEqual({"stages.py", "runner.py", "inbox.py", "stage_registry.py", "translation.py", "recognition.py", "source.py", "quality.py", "cli.py"}, set(modules))
        importers, callers = set(), set()
        for name, tree in modules.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module, names = node.module or "", {alias.name for alias in node.names}
                    if module.split(".")[-1] == "revisions" or (module in ("", "upgrade") and "revisions" in names):
                        importers.add(name)
                elif isinstance(node, ast.Import) and any(alias.name.split(".")[-1] == "revisions" for alias in node.names):
                    importers.add(name)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() == "upgrade.revisions":
                    importers.add(name)                                                        # importlib / __import__ by name
            for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
                if any("attest_revision" in (getattr(node, "attr", None), getattr(node, "id", None)) for node in ast.walk(function)):
                    callers.add((name, function.name))
        self.assertEqual(importers, {"cli.py"})                                               # no stage, runner, inbox or worker module sees the human loop
        self.assertEqual(callers, {("cli.py", "cmd_review_attest")})                           # and in the command line only the user's own command does


if __name__ == "__main__":
    unittest.main()
