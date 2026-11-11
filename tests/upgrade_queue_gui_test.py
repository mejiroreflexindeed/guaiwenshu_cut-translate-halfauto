#!/usr/bin/env python3
"""W08 second part: the queue list and its operations, the new-task dialog, media relocation, resources and closing
the window (plan 3.1, 3.2, 10.2, 10.5).

Queue semantics run in-process with synthetic stages; the panel runs every command through the real command line.
Run directly or through scripts/run_tests.py (PYTHONPATH = app/tools + tests).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest

from upgrade import contracts as c
from upgrade import gui, gui_queue
from upgrade import runner as r
from upgrade.config import APP_CONFIG_NAME, ENGINE_SETTINGS_NAME, save_engine_settings
from upgrade_runner_test import FakeStage, RunnerTestCase, registry
from upgrade_stages_test import StageTestCase

TOOLS = Path(__file__).resolve().parents[1] / "app" / "tools"


def cli(*args):
    completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    return completed.returncode, json.loads(completed.stdout), completed.stderr


def when_running(root: Path, request_id: str, action) -> threading.Thread:
    def worker() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if r.load_envelope(root, request_id)["status"] == "RUNNING":
                    break
            except c.ContractError:
                pass
            time.sleep(0.1)
        time.sleep(1.0)
        action()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


class BlockFor(FakeStage):
    """Blocks (in an owned child process) only for the listed requests."""

    def __init__(self, name: str, request_ids):
        super().__init__(name)
        self.blocking = set(request_ids)

    def run(self, ctx):
        self.behavior = "block" if ctx.request["request_id"] in self.blocking else "ok"
        return super().run(ctx)


def queued_row(**kw):
    base = {"request_id": c.new_id(), "run_id": None, "status": "QUEUED", "display_name": "甲", "input": {"kind": "local_video", "value": "C:/片源/甲.mp4"},
            "recognition_mode": "combined", "timing_mode": "mixed", "active_stage": None, "elapsed_seconds": None, "progress": None, "error": None,
            "machine_revision_id": None, "human_revisions": 0, "quality": None}
    return {**base, **kw}


def overview(*rows, live=False, stop=False):
    return {"executor": {"live": live, "pid": 4242 if live else None, "started_at": None}, "stop_requested": stop, "requests": list(rows)}


# --------------------------------------------------------------------------- pure parts

class QueueLabelTests(unittest.TestCase):
    def test_the_list_shows_the_stage_and_elapsed_time_never_an_invented_percentage(self):
        self.assertEqual((gui_queue.format_elapsed(5), gui_queue.format_elapsed(125), gui_queue.format_elapsed(3725)), ("5 秒", "2 分 05 秒", "1 小时 02 分"))
        running = queued_row(status="RUNNING", run_id=c.new_id(), active_stage="TRANSLATION", elapsed_seconds=75)
        self.assertEqual(gui_queue.stage_text(running), "翻译（已 1 分 15 秒）")
        self.assertNotIn("%", "".join(str(value) for value in gui_queue.queue_values(running)))
        self.assertEqual(gui_queue.stage_text({**running, "progress": {"processed": 3, "total": 10}}), "翻译 3/10（已 1 分 15 秒）")   # only if a stage reports it
        values = gui_queue.queue_values(queued_row(machine_revision_id=c.new_id(), human_revisions=2,
                                                   quality={"structural_status": "valid", "errors": 0, "warnings": 4}))
        self.assertEqual(values, ("甲", "甲.mp4", "语音与画面联合分析·混合", "排队中", "", "已生成", 2, "提示 4 条"))
        self.assertEqual(gui_queue.input_text({"kind": "local_video", "value": "supplied-source:0123456789abcdef"}), "日文源字幕 01234567")

    def test_states_say_what_happened_and_what_can_be_resumed(self):
        stopped = queued_row(status="INTERRUPTED", run_id=c.new_id(), error={"code": "PROCESS_CANCELLED", "stage": "ASR", "message": "", "kind": "queue_stop"})
        self.assertEqual((gui_queue.request_status_text(stopped), gui_queue.stage_text(stopped)), ("已停止（可恢复）", "停在：语音识别"))
        self.assertEqual(gui_queue.request_status_text(queued_row(status="CANCELLED")), "已移除（未开始）")
        self.assertEqual(gui_queue.request_status_text(queued_row(status="CANCELLED", run_id=c.new_id())), "已跳过")
        self.assertEqual(gui_queue.request_status_text(queued_row(status="FAILED", run_id=c.new_id(), error={"code": "RESOURCE_MISSING"})), "失败（RESOURCE_MISSING，可恢复）")
        self.assertEqual(gui_queue.request_status_text(queued_row(status="COMPLETED", run_id=c.new_id())), "已完成（没有生成字幕版本）")

    def test_queue_buttons_follow_plan_3_2(self):
        def usable(selected, view, own=False):
            states = gui_queue.queue_action_states(selected, view, own_executor=own, data_root_ok=True, config_ok=True)
            return {key for key, value in states.items() if value}
        waiting = queued_row()
        self.assertEqual(usable(None, overview(waiting)), {"new_task", "start_queue", "resources"})
        self.assertEqual(usable(waiting, overview(waiting)), {"new_task", "start_queue", "resources", "remove_request", "copy_request"})
        running = queued_row(status="RUNNING", run_id=c.new_id())
        self.assertEqual(usable(None, overview(running, waiting, live=True)), {"new_task", "resources", "stop_current", "skip_current"})   # one executor only
        self.assertNotIn("stop_current", usable(None, overview(running, live=True, stop=True)))          # already asked to stop
        self.assertEqual(usable(None, overview(running)), {"new_task", "resources"})                     # a stale RUNNING without executor: nothing to stop
        failed = queued_row(status="FAILED", run_id=c.new_id())
        self.assertIn("resume_request", usable(failed, overview(failed)))
        self.assertNotIn("resume_request", usable(queued_row(status="CANCELLED", run_id=c.new_id()), overview()))
        nothing = gui_queue.queue_action_states(None, None, own_executor=False, data_root_ok=False, config_ok=False)
        self.assertFalse(any(nothing.values()))

    def test_a_new_task_needs_a_source_and_a_display_name(self):
        video = Path(__file__)                       # any existing file stands in for the chosen video here
        spec = {"source_kind": "file", "video": str(video), "url": "", "display_name": "名字", "mode": "combined", "timing": "mixed", "subtitle": ""}
        self.assertEqual(gui_queue.new_task_problems(spec), [])
        self.assertEqual(len(gui_queue.new_task_problems({**spec, "display_name": "  "})), 1)
        self.assertEqual(len(gui_queue.new_task_problems({**spec, "source_kind": "url", "url": "ftp://x"})), 1)
        self.assertEqual(gui_queue.new_task_problems({**spec, "source_kind": "url", "url": " https://example.invalid/v/1 "}), [])
        self.assertEqual(len(gui_queue.new_task_problems({**spec, "mode": "supplied_source"})), 1)                     # the Japanese subtitle is missing
        self.assertEqual(len(gui_queue.new_task_problems({**spec, "mode": "supplied_source", "source_kind": "url", "url": "https://example.invalid/v"})), 2)

    def test_user_text_only_ever_becomes_one_argument(self):
        name = '甲 "乙" & del *.* | 丙'
        argv = gui_queue.submit_argv({"source_kind": "file", "video": "C:/片 源/a.mp4", "display_name": f"  {name} ", "mode": "combined", "timing": "screen"},
                                     "C:/数据根/settings/app.json", {"video_stream": None, "audio_stream": "2", "accept": True})
        self.assertEqual(argv[argv.index("--display-name") + 1], name)
        self.assertEqual(argv[argv.index("--video") + 1], "C:/片 源/a.mp4")
        self.assertEqual((argv[argv.index("--timing-mode") + 1], argv[argv.index("--audio-stream") + 1], argv[-1]), ("screen", "2", "--accept-dictionary-warnings"))
        url = gui_queue.submit_argv({"source_kind": "url", "url": " https://example.invalid/v ", "display_name": "n", "mode": "speech_only"}, "cfg", {})
        self.assertEqual(url[url.index("--url") + 1], "https://example.invalid/v")
        self.assertNotIn("--timing-mode", url)

    def test_refusals_become_questions(self):
        ask = {"ok": False, "error": {"code": "NEEDS_SELECTION", "details": {"kind": "audio_stream", "candidates": [{"index": 1}, {"index": 2}]}}}
        self.assertEqual(gui_queue.question_of(ask)[0], "audio_stream")
        self.assertEqual(gui_queue.question_of({"ok": False, "error": {"code": "NEEDS_SELECTION", "details": {"kind": "no_audio_stream"}}})[0], "no_audio_stream")
        self.assertEqual(gui_queue.question_of({"ok": False, "error": {"code": "DICTIONARY_UNSUPPORTED", "details": {}}})[0], "dictionary")
        self.assertIsNone(gui_queue.question_of({"ok": False, "error": {"code": "INPUT_MISSING", "details": {}}}))
        lines = gui_queue.dictionary_lines({"unsupported": [{"role": "pre", "line": 3, "text": "坏行\n", "reason": "needs source<TAB>target"}]})
        self.assertEqual(lines, ["译前词典 第 3 行：坏行  —— needs source<TAB>target"])


# --------------------------------------------------------------------------- queue semantics (in-process, synthetic stages)

class StopSkipTests(RunnerTestCase):
    def queued_ids(self) -> list[str]:
        """Execution order (created_at, then request_id: two requests of the same second run in id order)."""
        return [envelope["request"]["request_id"] for envelope in r.list_envelopes(self.root, status="QUEUED")]

    def test_stop_current_keeps_the_request_resumable_and_pauses_the_queue(self):
        r.submit(self.root, self.request(), display_name="第一")
        r.submit(self.root, self.request(), display_name="第二")
        first_id, second_id = self.queued_ids()
        stages = registry(ASR=BlockFor("ASR", [first_id]))
        seen: dict = {}
        when_running(self.root, first_id, lambda: (seen.update(r.queue_rows(self.root)), seen.update(stop=r.request_stop(self.root))))
        summary = self.runner(stages).run_queue()
        self.assertEqual((summary["processed"], summary["interrupted"], summary["paused"]), (1, 1, True))
        live_row = next(row for row in seen["requests"] if row["request_id"] == first_id)
        self.assertEqual((seen["executor"]["live"], live_row["status"], live_row["active_stage"], live_row["progress"]), (True, "RUNNING", "ASR", None))
        self.assertIsInstance(live_row["elapsed_seconds"], int)
        self.assertEqual((seen["stop"]["requested"], seen["stop"]["running"]), (True, [first_id]))
        stopped = r.load_envelope(self.root, first_id)
        self.assertEqual((stopped["status"], stopped["last_error"]["code"], stopped["last_error"]["stage"], stopped["last_error"]["details"]["kind"]),
                         ("INTERRUPTED", "PROCESS_CANCELLED", "ASR", "queue_stop"))
        self.assertEqual(r.load_envelope(self.root, second_id)["status"], "QUEUED")                 # paused: the next request did not start
        self.assertFalse(r.stop_flag_path(self.root).exists())
        rows = {row["request_id"]: row for row in r.queue_rows(self.root)["requests"]}
        self.assertEqual((gui_queue.request_status_text(rows[first_id]), gui_queue.stage_text(rows[first_id])), ("已停止（可恢复）", "停在：语音识别"))
        self.assertFalse(r.queue_rows(self.root)["executor"]["live"])
        # resume: same run, a new ASR attempt, the committed stages are not run again
        r.resume(self.root, stopped["run_id"])
        stages["ASR"].blocking.clear()
        summary = self.runner(stages).run_queue()
        self.assertEqual((summary["completed"], summary["paused"]), (2, False))
        task = self.task_for(r.load_envelope(self.root, first_id))
        asr = next(stage for stage in task["stages"] if stage["name"] == "ASR")
        self.assertEqual([attempt["status"] for attempt in asr["attempts"]], ["CANCELLED", "DONE"])
        self.assertEqual(stages["INPUT"].calls, 2)                                                  # first + second request, not three

    def test_stop_without_a_running_executor_writes_nothing_and_a_stale_flag_does_not_block_a_start(self):
        self.assertEqual(r.request_stop(self.root)["requested"], False)
        self.assertFalse(r.stop_flag_path(self.root).exists())
        envelope = r.submit(self.root, self.request())
        r.stop_flag_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        r.stop_flag_path(self.root).write_text("left over", encoding="utf-8")       # a stop that arrived just as an executor ended
        summary = self.runner().run_queue()
        self.assertEqual((summary["completed"], summary["paused"]), (1, False))       # starting the queue is the user's new decision
        self.assertEqual(r.load_envelope(self.root, envelope["request"]["request_id"])["status"], "COMPLETED")

    def test_skip_current_cancels_it_and_the_queue_goes_on(self):
        r.submit(self.root, self.request())
        r.submit(self.root, self.request())
        first_id, second_id = self.queued_ids()
        when_running(self.root, first_id, lambda: r.cancel(self.root, first_id))
        summary = self.runner(registry(ASR=BlockFor("ASR", [first_id]))).run_queue()
        self.assertEqual((summary["cancelled"], summary["completed"], summary["paused"]), (1, 1, False))
        rows = {row["request_id"]: row for row in r.queue_rows(self.root)["requests"]}
        self.assertEqual(gui_queue.request_status_text(rows[first_id]), "已跳过")
        self.assertEqual(rows[second_id]["status"], "COMPLETED")                                    # the skipped one went first, the next one still ran


# --------------------------------------------------------------------------- the command line (real ffprobe)

class QueueCommandTests(StageTestCase):
    def test_copy_as_new_task_takes_the_configuration_as_it_is_now(self):
        config = str(self.root / APP_CONFIG_NAME)
        code, asked, _ = cli("submit", "--config", config, "--video", str(self.two_audio), "--mode", "combined", "--timing-mode", "screen", "--display-name", "双音轨")
        self.assertEqual((code, asked["error"]["code"], asked["error"]["details"]["kind"]), (2, "NEEDS_SELECTION", "audio_stream"))
        self.assertEqual(r.list_envelopes(self.root), [])                                               # refused before anything was queued
        chosen = str(asked["error"]["details"]["candidates"][-1]["index"])
        code, first, err = cli("submit", "--config", config, "--video", str(self.two_audio), "--mode", "combined", "--timing-mode", "screen",
                               "--audio-stream", chosen, "--display-name", "双音轨")
        self.assertEqual(code, 0, err)
        old = r.load_envelope(self.root, first["ids"]["request_id"])["request"]
        save_engine_settings(self.root / ENGINE_SETTINGS_NAME, {"translation": {"endpoint": f"http://127.0.0.1:{self.port + 1}"}})   # the user changes a setting
        code, copied, err = cli("queue", "copy", "--data-root", str(self.root), "--request", old["request_id"])
        self.assertEqual((code, copied["ids"]["config_changed"], copied["ids"]["copied_from"]), (0, True, old["request_id"]), err)
        new = r.load_envelope(self.root, copied["ids"]["request_id"])["request"]
        for key in ("project_id", "input", "recognition_mode", "timing_mode", "video_stream_index", "audio_stream_index"):
            self.assertEqual(new[key], old[key], key)
        self.assertEqual(new["config_snapshot"]["engine_settings"]["translation"]["endpoint"], f"http://127.0.0.1:{self.port + 1}")
        self.assertEqual(r.load_envelope(self.root, old["request_id"])["request"], old)                   # the queued request keeps its snapshot
        code, again, _ = cli("queue", "copy", "--data-root", str(self.root), "--request", new["request_id"])
        self.assertEqual((code, again["ids"]["config_changed"]), (0, False))
        cli("queue", "cancel", "--data-root", str(self.root), "--request", again["ids"]["request_id"])
        code, listed, _ = cli("queue", "list", "--data-root", str(self.root))
        rows = {row["request_id"]: row for row in listed["ids"]["requests"]}
        self.assertEqual((code, len(rows), listed["ids"]["executor"]["live"]), (0, 3, False))
        self.assertEqual(gui_queue.queue_values(rows[old["request_id"]])[:4], ("双音轨", "双音轨.mkv", "语音与画面联合分析·画面驱动", "排队中"))
        self.assertEqual(gui_queue.request_status_text(rows[again["ids"]["request_id"]]), "已移除（未开始）")      # history is kept
        code, stopped, _ = cli("queue", "stop", "--data-root", str(self.root))
        self.assertEqual((code, stopped["ids"]["requested"], stopped["warnings"][0]["code"]), (0, False, "EXECUTOR_NOT_RUNNING"))

    def test_media_is_opened_where_it_is_and_relocated_only_to_the_same_content(self):
        original = self.tmp / "原片" / "双音轨 原片.mkv"
        original.parent.mkdir()
        shutil.copyfile(self.two_audio, original)
        code, asked, _ = cli("media", "register", "--data-root", str(self.root), "--video", str(original), "--display-name", "定位")
        self.assertEqual((code, asked["error"]["details"]["kind"]), (2, "audio_stream"))
        self.assertEqual(list((self.root / "projects").iterdir()), [])                                # no project until the question is answered
        code, registered, err = cli("media", "register", "--data-root", str(self.root), "--video", str(original), "--display-name", "定位", "--audio-stream", "none")
        self.assertEqual(code, 0, err)
        project_id, media_id = registered["ids"]["project_id"], registered["ids"]["media_id"]
        self.assertEqual((r.load_project(self.root, project_id)["media_ids"], registered["ids"]["audio_stream"]), ([media_id], None))
        where = ("--data-root", str(self.root), "--project", project_id, "--media", media_id)
        code, located, _ = cli("media", "locate", *where)
        self.assertEqual((code, located["ids"]["path"]), (0, str(original.resolve())))
        moved = self.tmp / "搬走了" / "改名.mkv"
        moved.parent.mkdir()
        original.rename(moved)
        code, lost, _ = cli("media", "locate", *where)
        self.assertEqual(lost["error"]["code"], "INPUT_MISSING")
        code, wrong, _ = cli("media", "relocate", *where, "--path", str(self.video))
        self.assertEqual(wrong["error"]["code"], "MEDIA_CHANGED")                                       # another video is new media, not a move
        code, rebound, err = cli("media", "relocate", *where, "--path", str(moved))
        self.assertEqual((code, rebound["ids"]["path"]), (0, str(moved.resolve())), err)
        code, located, _ = cli("media", "locate", *where)
        self.assertEqual(located["ids"]["path"], str(moved.resolve()))

    def test_resources_list_shows_a_moved_tool_and_rebind_accepts_the_same_content(self):
        font = self.tmp / "字体" / "测试字体.ttf"
        font.parent.mkdir()
        font.write_bytes(b"not really a font, only bytes with an identity")
        config = str(self.root / APP_CONFIG_NAME)
        code, _, err = cli("resources", "register", "--config", config, "--id", "test-font", "--kind", "font", "--path", str(font))
        self.assertEqual(code, 0, err)
        code, listed, _ = cli("resources", "list", "--config", config)
        states = {row["resource_id"]: row["state"] for row in listed["ids"]["resources"]}
        self.assertEqual((code, states["test-font"], states["ffprobe"]), (0, "ok", "ok"))
        moved = self.tmp / "字体搬家" / "测试字体.ttf"
        moved.parent.mkdir()
        font.rename(moved)
        code, listed, _ = cli("resources", "list", "--config", config)
        row = next(item for item in listed["ids"]["resources"] if item["resource_id"] == "test-font")
        self.assertEqual((row["state"], gui_queue.resource_state_text(row)), ("RESOURCE_MISSING", "找不到"))
        code, rebound, err = cli("resources", "rebind", "--config", config, "--id", "test-font", "--path", str(moved))
        self.assertEqual(code, 0, err)
        code, listed, _ = cli("resources", "list", "--config", config)
        self.assertEqual(next(item for item in listed["ids"]["resources"] if item["resource_id"] == "test-font")["state"], "ok")


# --------------------------------------------------------------------------- the panel (real command line)

class PanelQueueTests(StageTestCase):
    def setUp(self):
        super().setUp()
        try:
            import tkinter as tk
            self.window = tk.Tk()
        except Exception as exc:       # no display available
            raise unittest.SkipTest(f"tkinter cannot open a window here: {exc}")
        self.window.withdraw()
        self.extra: list[subprocess.Popen] = []
        self.panel = gui.V2Panel(self.window, settings_path=self.tmp / "面板设置" / "roast_v2_gui.local.json")
        self.panel.data_root_var.set(str(self.root))
        self.panel.refresh()
        self.queue = self.panel.queue_tab

    def tearDown(self):
        for process in [*self.extra, self.queue.executor]:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.window.update_idletasks()        # flush Tk's pending theme event before the window goes away
        self.window.destroy()
        super().tearDown()

    def spec(self, **kw):
        return {"source_kind": "file", "video": str(self.video), "url": "", "display_name": "任务", "mode": "speech_only", "timing": "mixed", "subtitle": "",
                "copy_original": False, "start": False, **kw}

    def usable(self) -> set[str]:
        return {key for key, button in self.queue.buttons.items() if str(button.cget("state")) == "normal"}

    def pump_until(self, condition, seconds=180):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.05)
        self.assertTrue(condition(), "timed out waiting for the panel")

    def test_a_new_task_asks_for_the_audio_track_and_the_dictionary_entries_before_it_is_queued(self):
        broken = self.tmp / "词典" / "译前.txt"
        broken.parent.mkdir()
        broken.write_text("先輩\t前辈\n这一行没有制表符\n", encoding="utf-8")
        save_engine_settings(self.root / ENGINE_SETTINGS_NAME, {**self.engine_settings, "dictionary": {"pre": str(broken)}})
        asked = []

        def choose(kind, candidates, *, allow_none):
            asked.append((kind, allow_none, [item["index"] for item in candidates]))
            return str(candidates[-1]["index"])
        with patch.object(self.queue, "ask_stream", side_effect=choose), patch.object(self.queue, "ask_dictionary", return_value=True) as accept:
            result = self.queue.submit_new_task(self.spec(video=str(self.two_audio), display_name="  双音轨 任务 ", mode="combined"))
        self.assertIsNotNone(result)
        self.assertEqual([(kind, allow_none) for kind, allow_none, _ in asked], [("audio_stream", False)])     # combined needs sound: no "不要音轨"
        self.assertEqual(accept.call_count, 1)
        self.assertEqual([item["role"] for item in accept.call_args.args[0]["unsupported"]], ["pre"])
        request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        self.assertEqual(request["audio_stream_index"], asked[0][2][-1])
        self.assertEqual(len(request["config_snapshot"]["accepted_dictionary_warnings"]), 1)
        self.assertEqual(r.load_project(self.root, request["project_id"])["display_name"], "双音轨 任务")
        self.assertEqual(self.queue.tree.item(request["request_id"], "values")[0], "双音轨 任务")
        self.assertIsNone(self.queue.executor)                                                         # "只加入队列" starts nothing
        with patch.object(self.queue, "ask_dictionary") as again:                                      # the acceptance is remembered for this content
            self.assertIsNotNone(self.queue.submit_new_task(self.spec(display_name="第二个")))
        self.assertEqual(again.call_count, 0)

    def test_a_file_without_sound_is_not_switched_to_another_mode(self):
        with patch.object(gui_queue.messagebox, "showwarning") as warned:
            self.assertIsNone(self.queue.submit_new_task(self.spec(video=str(self.silent))))
        self.assertEqual(warned.call_count, 1)
        self.assertIn("纯画面文字", warned.call_args.args[1])
        self.assertEqual(r.list_envelopes(self.root), [])

    def test_a_task_from_an_existing_japanese_subtitle_registers_video_and_source_first(self):
        subtitle = self.tmp / "日文.srt"
        subtitle.write_text("1\n00:00:00,500 --> 00:00:01,500\nありがとう\n\n2\n00:00:01,800 --> 00:00:02,600\nまたね\n", encoding="utf-8")
        result = self.queue.submit_new_task(self.spec(mode="supplied_source", subtitle=str(subtitle), display_name="自带原文"))
        self.assertIsNotNone(result)
        request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        project = r.load_project(self.root, request["project_id"])
        self.assertEqual((request["recognition_mode"], project["display_name"], len(project["media_ids"])), ("supplied_source", "自带原文", 1))
        self.assertEqual(request["input"]["source_media_id"], project["media_ids"][0])
        self.assertIsNotNone(request["source_revision_id"])

    def test_the_queue_runs_as_a_child_of_the_window_and_its_buttons_follow_the_request(self):
        result = self.queue.submit_new_task(self.spec(display_name="会失败的任务"))            # no ASR engine is registered here
        request_id = result["ids"]["request_id"]
        self.queue.tree.selection_set(request_id)
        self.queue.update_buttons()
        self.assertEqual(self.usable(), {"new_task", "start_queue", "resources", "remove_request", "copy_request"})
        self.queue.invoke("start_queue")
        self.assertTrue(self.queue.own_executor_running())
        self.assertNotIn("start_queue", self.usable())
        self.pump_until(lambda: self.queue.executor is None)
        self.assertIn("队列已结束", self.panel.log.get("1.0", "end"))
        row = self.queue.rows[request_id]
        self.assertEqual(row["status"], "FAILED")                                                   # an honest failure, resumable
        self.queue.tree.selection_set(request_id)
        self.queue.update_buttons()
        self.assertIn("resume_request", self.usable())
        self.queue.invoke("resume_request")
        self.assertEqual(self.queue.rows[request_id]["status"], "QUEUED")
        self.queue.tree.selection_set(request_id)
        self.queue.invoke("copy_request")
        copies = [key for key in self.queue.rows if key != request_id]
        self.assertEqual(len(copies), 1)
        self.queue.tree.selection_set(copies[0])
        self.queue.update_buttons()
        with patch.object(gui_queue.messagebox, "askyesno", return_value=True):
            self.queue.invoke("remove_request")
        self.assertEqual(self.queue.tree.item(copies[0], "values")[3], "已移除（未开始）")

    def test_open_original_video_relocates_a_moved_file_with_the_same_content(self):
        original = self.tmp / "原片.mp4"
        shutil.copyfile(self.video, original)
        code, registered, err = cli("media", "register", "--data-root", str(self.root), "--video", str(original), "--display-name", "定位")
        self.assertEqual(code, 0, err)
        self.panel.project_id, media_id = registered["ids"]["project_id"], registered["ids"]["media_id"]
        moved = self.tmp / "搬走" / "原片.mp4"
        moved.parent.mkdir()
        original.rename(moved)
        opened, reasons = [], []
        with patch.object(self.panel, "selected_row", return_value={"media_id": media_id}), patch.object(self.panel, "open_path", side_effect=opened.append), \
                patch.object(self.panel, "ask_relocate", side_effect=lambda reason: reasons.append(reason) or str(moved)):
            self.panel.do_open_media()
            self.assertEqual((len(reasons), opened), (1, [moved.resolve()]))
            self.assertIn("找不到", reasons[0])
            self.panel.do_open_media()                                                                 # found again: no question
            self.assertEqual((len(reasons), opened[-1]), (1, moved.resolve()))
            moved.write_bytes(self.silent.read_bytes())                                                # same place, different content
            with patch.object(self.panel, "ask_relocate", return_value=str(self.silent)):
                self.panel.do_open_media()
            self.assertEqual(len(opened), 2)                                                           # nothing opened for a different video
        self.assertIn("MEDIA_CHANGED", self.panel.log.get("1.0", "end"))

    def test_closing_with_running_work_offers_only_stop_or_back(self):
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        self.queue.executor = subprocess.Popen(sleeper)
        export = subprocess.Popen(sleeper)
        self.extra += [self.queue.executor, export]
        self.panel.children_running.append(export)
        self.panel.busy_title = "导出视频"
        with patch.object(gui, "ask_close_dialog", return_value=False) as asked:
            self.assertFalse(self.panel.confirm_close())
        self.assertIn("队列正在运行", asked.call_args.args[1])
        self.assertIn("导出视频正在进行", asked.call_args.args[1])
        self.assertIsNone(self.queue.executor.poll())                                                  # "返回": nothing was stopped
        with patch.object(gui, "ask_close_dialog", return_value=True), patch.object(gui_queue.QueueTab, "EXIT_WAIT_SECONDS", 0.5):
            self.assertTrue(self.panel.confirm_close())
        self.assertIsNotNone(self.queue.executor.poll())
        self.assertIsNotNone(export.poll())
        self.queue.executor = None
        self.panel.children_running.clear()
        with patch.object(gui, "ask_close_dialog") as asked:
            self.assertTrue(self.panel.confirm_close())                                                # nothing running: no question
        self.assertEqual(asked.call_count, 0)
        source = (TOOLS / "one_click_roast.py").read_text(encoding="utf-8")
        self.assertIn("panel.confirm_close()", source[source.index("def _on_close"):])


if __name__ == "__main__":
    unittest.main(verbosity=2)
