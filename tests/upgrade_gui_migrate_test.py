#!/usr/bin/env python3
"""W08: the v2 panel inside the old tkinter window and the read-only migration of old tasks (plan 3.2, 3.3, 9.6; T05 migration part).

The panel is driven against a real pipeline run; every button goes through the real command line in a subprocess.
Run directly or through scripts/run_tests.py (PYTHONPATH = app/tools + tests).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import ast
import json
import os
import subprocess
import sys
import unittest

from support import w03
from upgrade import contracts as c
from upgrade import gui
from upgrade import runner as r
from upgrade_revisions_test import SharedRunCase
from upgrade_stages_test import StageTestCase

TOOLS = Path(__file__).resolve().parents[1] / "app" / "tools"


def row(kind="machine", formats=("srt", "ass"), status="UNREVIEWED", scopes=(), structural="valid", report=True):
    return {"revision_id": c.new_id(), "kind": kind, "media_id": c.new_id(), "structural_status": structural, "directory": "revisions/x",
            "source_revision_id": c.new_id(), "report_html": "revisions/x/review.html" if report else None, "issue_counts": {"ERROR": 0, "WARNING": 1, "INFO": 0},
            "formats": {fmt: {"path": f"x.{fmt}", "sha256_12": "0" * 12, "human_status": status, "confirmed_scopes": list(scopes)} for fmt in formats}}


class ButtonRules(unittest.TestCase):
    def states(self, selected, fmt=None, **kw):
        options = {"session": None, "source_session": None, "source_revision_id": None, "data_root_ok": True, **kw}
        return gui.action_states(selected, fmt, **options)

    def test_buttons_are_usable_only_under_the_conditions_of_plan_3_2(self):
        nothing = self.states(None)
        self.assertEqual({key for key, usable in nothing.items() if usable}, {"migrate"})
        machine = row()
        unchosen = self.states(machine)                                    # two formats and none chosen: no copy, no confirmation
        self.assertEqual((unchosen["create_session"], unchosen["attest"], unchosen["export_subtitles"], unchosen["open_report"]), (False, False, True, True))
        chosen = self.states(machine, "srt")
        self.assertTrue(chosen["create_session"] and chosen["attest"])
        self.assertFalse(chosen["open_editor"])                            # nothing to open before a working copy exists
        broken = self.states(row(structural="invalid"), "srt")
        self.assertEqual((broken["export_subtitles"], broken["export_video"]), (False, False))
        self.assertFalse(self.states(row(kind="review", formats=("srt",)), "srt")["revise_source"])
        self.assertTrue(self.states(None, source_revision_id=c.new_id())["translate_source"])

    def test_no_label_says_reviewed_unless_the_user_confirmed_the_required_scopes(self):
        self.assertEqual(gui.status_label(row(), "srt"), "机器字幕已生成，等待人工检查")
        self.assertEqual(gui.status_label(row(kind="review", formats=("ass",), status="PARTIAL"), "ass"), "部分确认（确认范围为空）")
        self.assertEqual(gui.status_label(row(kind="review", formats=("srt",), status="REVIEWED", scopes=("text", "timing")), "srt"), "已人工确认（已确认：文字、时间）")
        self.assertEqual(gui.scope_choices("srt"), {"text": True, "timing": True, "style": False})      # SRT: style shown as not applicable

    def test_the_old_window_only_gains_a_tab_and_the_panel_writes_its_own_settings_file(self):
        tree = ast.parse((TOOLS / "one_click_roast.py").read_text(encoding="utf-8"))
        methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertIn("_build_v2_tab", methods)
        source = (TOOLS / "upgrade" / "gui.py").read_text(encoding="utf-8")
        self.assertNotIn("roast_pipeline.local.json", source)                # the old configuration is never written by the panel
        self.assertNotIn("import revisions", source)
        self.assertNotIn("from upgrade import revisions", source)


def legacy_job(directory: Path, video: Path) -> Path:
    """An old app/work/<job> directory in the shape the old pipeline writes (task.json, job_manifest.json, subs/, deliverables/, source/)."""
    for name in ("subs", "deliverables", "source", "logs"):
        (directory / name).mkdir(parents=True)
    copied = directory / "source" / "input_video.mp4"
    copied.write_bytes(video.read_bytes())
    stages = {name: {"status": "DONE", "attempt": 1} for name in ("INPUT", "TIMELINE", "ASR", "SEGMENTATION", "TRANSLATION", "FINAL_OUTPUT", "NLE_EXPORT")}
    (directory / "task.json").write_text(json.dumps({"schema_version": 1, "task_id": c.new_id(), "display_name": "旧任务 A", "job_dir": str(directory),
                                                     "source": {"url": "local-file", "video_file": str(video)}, "status": "DONE", "stages": stages,
                                                     "created_at": "2026-10-01T10:00:00+11:00", "updated_at": "2026-10-01T10:30:00+11:00"}, ensure_ascii=False), encoding="utf-8")
    (directory / "job_manifest.json").write_text(json.dumps({"source": {"video": str(copied)}, "pipeline_version": "legacy-test"}), encoding="utf-8")
    (directory / "subs" / "zh_machine.srt").write_bytes("1\r\n00:00:00,500 --> 00:00:01,500\r\n旧机器译文\r\n".encode("utf-8"))
    (directory / "deliverables" / "final.ass").write_bytes(w03.ASS_HEADER.encode("utf-8") + "Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,旧成品\n".encode("utf-8"))
    (directory / "logs" / "task_events.jsonl").write_text('{"event": "done"}\n', encoding="utf-8")
    return directory


def inventory(directory: Path) -> list[tuple]:
    return [(path.relative_to(directory).as_posix(), c.sha256_file(path), path.stat().st_mtime_ns) for path in sorted(directory.rglob("*")) if path.is_file()]


def cli(*args):
    completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    return completed.returncode, json.loads(completed.stdout), completed.stderr


class MigrationTests(StageTestCase):
    def test_an_old_task_becomes_a_new_project_and_its_directory_is_not_touched(self):
        job = legacy_job(self.tmp / "旧任务 目录" / "job_a", self.video)
        before = inventory(job)
        code, result, stderr = cli("migrate", "--data-root", str(self.root), "--legacy-job", str(job))
        self.assertEqual((code, result["ok"]), (0, True), stderr + json.dumps(result, ensure_ascii=False)[:800])
        self.assertEqual(inventory(job), before)                                                     # T05: the old copy keeps every byte and time
        ids = result["ids"]
        project = r.project_dir(self.root, ids["project_id"])
        manifest = json.loads((project / "legacy" / "source-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["task"]["status"], manifest["source_dir"]), ("DONE", str(job.resolve())))
        files = {row["path"]: row for row in manifest["files"]}
        self.assertEqual({path for path, item in files.items() if not item["copied"]}, {"source/input_video.mp4"})      # the video is only referenced
        for path, item in files.items():
            self.assertEqual(item["sha256"], c.sha256_file(job / path))
            if item["copied"]:
                self.assertEqual((project / "legacy" / "files" / path).read_bytes(), (job / path).read_bytes())
        self.assertEqual([item["path"] for item in manifest["machine_snapshots"]], ["deliverables/final.ass", "subs/zh_machine.srt"])
        record = r.load_project(self.root, ids["project_id"])
        self.assertEqual((record["machine_revision_ids"], record["review_revision_ids"], record["media_ids"]), ([], [], [ids["media_id"]]))  # an old DONE is not a v2 DONE
        self.assertEqual(manifest["media_reference"]["sha256"], c.sha256_file(job / "source" / "input_video.mp4"))
        self.assertIn("LEGACY_NOT_REVIEWED", [warning["code"] for warning in result["warnings"]])
        # a file the user says was edited by hand is registered explicitly, as an external human file with unaudited provenance
        code, imported, stderr = cli("review", "import", "--data-root", str(self.root), "--project", ids["project_id"], "--media", ids["media_id"],
                                     "--file", str(project / "legacy" / "files" / "subs" / "zh_machine.srt"))
        self.assertEqual((code, imported["ids"]["provenance_status"], imported["ids"]["human_status"]), (0, "unmapped", "PARTIAL"), stderr)
        self.assertEqual(inventory(job), before)

    def test_a_directory_that_is_not_an_old_task_creates_nothing(self):
        stray = self.tmp / "不是任务"
        stray.mkdir()
        (stray / "notes.txt").write_text("x", encoding="utf-8")
        code, result, _ = cli("migrate", "--data-root", str(self.root), "--legacy-job", str(stray))
        self.assertEqual((code, result["error"]["code"]), (c.EXIT_RESOURCE, "INPUT_MISSING"))
        self.assertEqual(list((self.root / "projects").glob("*")) if (self.root / "projects").is_dir() else [], [])


class PanelTests(SharedRunCase):
    """The panel on a real data root: buttons, labels and the commands they run."""

    def setUp(self):
        super().setUp()
        try:
            import tkinter as tk
            self.window = tk.Tk()
        except Exception as exc:       # no display available
            raise unittest.SkipTest(f"tkinter cannot open a window here: {exc}")
        self.window.withdraw()
        self.settings = self.tmp / "面板设置" / self._testMethodName[:30] / "roast_v2_gui.local.json"
        self.panel = gui.V2Panel(self.window, settings_path=self.settings)
        self.panel.data_root_var.set(str(self.root))
        self.panel.refresh()
        self.panel.projects.selection_set(self.project_id)
        self.panel._project_selected()

    def tearDown(self):
        self.window.destroy()

    def select(self, revision_id: str, fmt: str | None) -> None:
        self.panel.revisions.selection_set(revision_id)
        self.panel._revision_selected()
        if fmt is not None:
            self.panel.format_var.set(fmt)
        self.panel.update_buttons()

    def usable(self) -> set[str]:
        return {key for key, button in self.panel.buttons.items() if str(button.cget("state")) == "normal"}

    def test_from_a_machine_version_to_a_confirmed_human_file_and_an_export(self):
        self.assertIn(self.machine_id, self.panel.rows)
        self.assertIn("机器字幕已生成，等待人工检查", self.panel.revisions.item(self.machine_id, "values")[4])
        self.select(self.machine_id, None)
        self.assertNotIn("create_session", self.usable())                  # a version with two formats: the format must be chosen first
        self.select(self.machine_id, "srt")
        self.assertTrue({"create_session", "attest", "export_subtitles", "export_video", "open_report", "open_dir"} <= self.usable())
        self.panel.invoke("create_session")
        edit_file = Path(self.panel.session["edit_file"])
        self.assertEqual(edit_file.read_bytes(), (self.machine_dir / "machine.zh.srt").read_bytes())
        self.assertIn("open_editor", self.usable())
        self.panel.editor_var.set(str(self.tmp / "editor.exe"))
        with patch.object(gui.subprocess, "Popen") as popen:
            self.panel.invoke("open_editor")
        self.assertEqual(popen.call_args.args[0], [str(self.tmp / "editor.exe"), str(edit_file)])      # only the working copy, never waited for
        edit_file.write_bytes(edit_file.read_bytes().replace("译:".encode("utf-8"), "译：".encode("utf-8"), 1))
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            self.panel.invoke("register_review")
        human = [revision_id for revision_id, item in self.panel.rows.items() if item["kind"] == "review"]
        self.assertEqual(len(human), 1)
        self.assertIn("部分确认（确认范围为空）", self.panel.revisions.item(human[0], "values")[4])     # registered is not reviewed
        self.select(human[0], "srt")
        with patch.object(self.panel, "ask_attest", return_value=(["text", "timing"], "看完了")):
            self.panel.invoke("attest")
        self.assertIn("已人工确认", self.panel.revisions.item(human[0], "values")[4])
        target = self.tmp / "面板导出"
        self.select(human[0], "srt")
        with patch.object(self.panel, "new_destination", return_value=target):
            self.panel.invoke("export_subtitles")
        manifest = c.load_record("ExportManifest", target / "manifest.json")
        self.assertEqual((manifest["revision_id"], manifest["quality_snapshot"]["files"][0]["human_status"]), (human[0], "REVIEWED"))
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["data_root"], str(self.root))

    def test_a_corrected_japanese_source_needs_an_explicit_second_step_to_be_translated(self):
        runs_before = self.index()["run_ids"]
        self.select(self.machine_id, "srt")
        self.panel.invoke("revise_source")
        session = self.panel.source_session
        self.assertEqual(self.panel.buttons["revise_source"].cget("text"), "登记原文修订")
        source_file = Path(session["edit_file"])
        source_file.write_bytes(source_file.read_bytes().replace("ありがとう".encode("utf-8"), "ありがとうございます".encode("utf-8"), 1))
        self.panel.invoke("revise_source")
        self.assertIsNotNone(self.panel.source_revision_id)
        self.assertIn("translate_source", self.usable())
        self.assertEqual(self.index()["run_ids"], runs_before)                                         # nothing was translated by itself


if __name__ == "__main__":
    unittest.main(verbosity=2)
