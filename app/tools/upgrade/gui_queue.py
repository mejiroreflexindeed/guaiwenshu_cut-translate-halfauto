#!/usr/bin/env python3
"""Queue list, new-task dialog and resources of the v2 panel (plan 3.1 / 3.2 / 3.4; work package W08, second part).

Like the rest of the panel this is a front end of the v2 command line: requests are created with ``submit`` /
``media register`` / ``source import``, the queue runs as ``queue run`` in a child process of this window, and
"stop current", "skip current", "remove", "resume" and "copy as new task" are the ``queue`` / ``resume`` commands.
Questions the commands raise (which stream, unsupported dictionary entries) are put to the user and the command is
run again with the user's answer; nothing is guessed. The executor started here is stopped (resumably) when the
window closes; it never keeps running invisibly.
"""

from __future__ import annotations

import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

MODE_ORDER = ("combined", "speech_only", "visual_only", "supplied_source")
MODE_TEXT = {"combined": "语音与画面联合分析", "speech_only": "仅语音", "visual_only": "纯画面文字", "supplied_source": "已有日文源字幕"}
TIMING_ORDER = ("mixed", "screen", "speech")
TIMING_TEXT = {"mixed": "混合（默认）", "screen": "画面驱动", "speech": "语音驱动"}
STAGE_TEXT = {"INPUT": "读取输入", "MEDIA_PREP": "准备媒体", "OCR": "画面文字识别", "ASR": "语音识别", "SOURCE": "整理日文原文",
              "TRANSLATION": "翻译", "SUBTITLE_BUILD": "生成字幕", "QUALITY_AND_COMMIT": "质量检查与提交"}
REQUEST_TEXT = {"QUEUED": "排队中", "RUNNING": "运行中", "COMPLETED": "已完成", "FAILED": "失败", "INTERRUPTED": "已中断（可恢复）", "CANCELLED": "已取消"}
ROLE_TEXT = {"pre": "译前词典", "gpt": "GPT 词典", "post": "译后词典", "glossary": "术语表"}
RESOURCE_STATE_TEXT = {"ok": "正常", "first_inspection": "已找到（尚未核对过内容）", "RESOURCE_MISSING": "找不到",
                       "RESOURCE_CHANGED": "内容和登记时不同（不能只改路径，需要重新登记）"}
QUEUE_ACTIONS = {"new_task": "新任务…", "start_queue": "开始队列", "stop_current": "停止当前", "skip_current": "跳过当前",
                 "remove_request": "移除未开始请求", "resume_request": "恢复中断任务", "copy_request": "复制为新任务", "resources": "资源与工具…"}
MODE_NOTES = {
    "combined": "同时用语音识别和画面文字识别。时间轴选“画面驱动”只表示字幕按画面切分时间，不保证日文原文全部来自画面识别；"
                "画面和口语有冲突时两边证据都保留，原文仍可能需要人工修订。",
    "speech_only": "只做语音识别，不运行画面文字识别。",
    "visual_only": "只识别画面上的文字，不提取音频、不运行语音识别。",
    "supplied_source": "不做识别：用你给的日文字幕（SRT/ASS）按原样分段翻译。需要对应的本地原视频。",
}
SUBTITLE_SUFFIXES = (".srt", ".ass")


# --------------------------------------------------------------------------- pure parts (tested without a window)

def format_elapsed(seconds: int | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60:02d} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60:02d} 分"


def input_text(job_input: dict) -> str:
    value = str(job_input.get("value") or "")
    if value.startswith("supplied-source:"):
        return "日文源字幕 " + value.split(":", 1)[1][:8]
    if job_input.get("kind") == "url":
        return value
    return Path(value).name


def mode_text(row: dict) -> str:
    text = MODE_TEXT.get(row["recognition_mode"], row["recognition_mode"])
    if row["recognition_mode"] == "combined" and row.get("timing_mode"):
        text += "·" + TIMING_TEXT.get(row["timing_mode"], row["timing_mode"]).replace("（默认）", "")
    return text


def request_status_text(row: dict) -> str:
    status, error = row["status"], row.get("error") or {}
    if status == "INTERRUPTED" and error.get("kind") == "queue_stop":
        return "已停止（可恢复）"
    if status == "CANCELLED":
        return "已移除（未开始）" if row.get("run_id") is None else "已跳过"
    if status == "FAILED":
        return f"失败（{error.get('code')}，可恢复）"
    if status == "COMPLETED" and not row.get("machine_revision_id"):
        return "已完成（没有生成字幕版本）"
    return REQUEST_TEXT.get(status, status)


def stage_text(row: dict) -> str:
    """Current stage and elapsed time; a percentage only when the stage reports a reliable processed/total (plan 3.2)."""
    error = row.get("error") or {}
    if row["status"] == "RUNNING":
        stage = row.get("active_stage")
        if not stage:
            return "准备中"
        text = STAGE_TEXT.get(stage, stage)
        progress = row.get("progress")
        if progress and progress.get("total"):
            text += f" {progress['processed']}/{progress['total']}"
        return text + (f"（已 {format_elapsed(row.get('elapsed_seconds'))}）" if row.get("elapsed_seconds") is not None else "")
    if row["status"] in ("FAILED", "INTERRUPTED") and error.get("stage"):
        return "停在：" + STAGE_TEXT.get(error["stage"], error["stage"])
    return ""


def quality_text(row: dict) -> str:
    quality = row.get("quality")
    if not quality:
        return "—"
    if quality["structural_status"] == "unknown":
        return "未知"
    if quality["structural_status"] != "valid":
        return "有结构错误"
    return f"提示 {quality['warnings']} 条"


def queue_values(row: dict) -> tuple:
    return (row.get("display_name") or "（未命名）", input_text(row["input"]), mode_text(row), request_status_text(row), stage_text(row),
            "已生成" if row.get("machine_revision_id") else "—", row.get("human_revisions", 0), quality_text(row))


def executor_text(overview: dict | None, *, own: bool) -> str:
    if overview is None:
        return "选择数据根后可以看到队列。"
    executor = overview["executor"]
    queued = sum(1 for row in overview["requests"] if row["status"] == "QUEUED")
    if not executor["live"] and not own:
        return "队列执行器：未运行。" + (f"有 {queued} 个任务在排队，点“开始队列”开始处理。" if queued else "")
    who = "本窗口启动" if own else f"由其他窗口或命令行启动，PID {executor.get('pid')}"
    text = f"队列执行器：正在运行（{who}）。"
    if overview.get("stop_requested"):
        text += "已请求停止：当前任务结束后队列暂停。"
    elif queued:
        text += f"之后还有 {queued} 个任务排队。"
    return text


def queue_action_states(selected: dict | None, overview: dict | None, *, own_executor: bool, data_root_ok: bool, config_ok: bool) -> dict[str, bool]:
    """Which queue buttons are usable (plan 3.2)."""
    rows = (overview or {}).get("requests") or []
    live = own_executor or bool(overview and overview["executor"]["live"])
    running = any(row["status"] == "RUNNING" for row in rows)
    queued = any(row["status"] == "QUEUED" for row in rows)
    status = selected["status"] if selected else None
    return {
        "new_task": data_root_ok and config_ok,
        "start_queue": data_root_ok and config_ok and not live and queued,
        "stop_current": live and running and not (overview or {}).get("stop_requested"),
        "skip_current": live and running,
        "remove_request": status == "QUEUED",
        "resume_request": status in ("FAILED", "INTERRUPTED") and bool(selected and selected.get("run_id")),
        "copy_request": selected is not None and config_ok,
        "resources": config_ok,
    }


def new_task_problems(spec: dict) -> list[str]:
    """What is missing before a new task can be submitted (plan 3.1: source and display name are required)."""
    problems = []
    if not str(spec.get("display_name") or "").strip():
        problems.append("请填写项目显示名（只用于显示）。")
    mode = spec.get("mode")
    if mode not in MODE_ORDER:
        problems.append("请选择识别模式。")
    if mode == "supplied_source" and spec.get("source_kind") != "file":
        problems.append("“已有日文源字幕”需要选择本地原视频，不能用 URL。")
    if spec.get("source_kind") == "file":
        if not spec.get("video") or not Path(spec["video"]).is_file():
            problems.append("请选择一个存在的本地视频文件。")
    else:
        url = str(spec.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            problems.append("URL 需要以 http:// 或 https:// 开头（只支持单个视频的地址）。")
    if mode == "combined" and spec.get("timing") not in TIMING_ORDER:
        problems.append("请选择时间轴模式。")
    if mode == "supplied_source":
        subtitle = spec.get("subtitle")
        if not subtitle or not Path(subtitle).is_file() or Path(subtitle).suffix.lower() not in SUBTITLE_SUFFIXES:
            problems.append("请选择日文源字幕文件（SRT 或 ASS）。")
    return problems


def stream_args(answers: dict) -> list[str]:
    args = []
    if answers.get("video_stream") is not None:
        args += ["--video-stream", str(answers["video_stream"])]
    if answers.get("audio_stream") is not None:
        args += ["--audio-stream", str(answers["audio_stream"])]
    return args


def submit_argv(spec: dict, config_path: Path | str, answers: dict) -> list[str]:
    """``submit`` for a new local-video or URL task. User text only ever becomes one argv element (no shell)."""
    args = ["submit", "--config", str(config_path), "--mode", spec["mode"], "--display-name", spec["display_name"].strip()]
    args += ["--video", spec["video"]] if spec["source_kind"] == "file" else ["--url", spec["url"].strip()]
    if spec["mode"] == "combined":
        args += ["--timing-mode", spec["timing"]]
    args += stream_args(answers)
    if spec.get("copy_original"):
        args.append("--copy-original")
    if answers.get("accept"):
        args.append("--accept-dictionary-warnings")
    return args


def stream_label(candidate: dict, kind: str) -> str:
    parts = [f"流 {candidate.get('index')}", str(candidate.get("codec_name") or "未知编码")]
    if kind == "video_stream" and candidate.get("width"):
        parts.append(f"{candidate['width']}×{candidate.get('height')}")
    if kind == "audio_stream":
        if candidate.get("channels"):
            parts.append(f"{candidate['channels']} 声道")
        if candidate.get("sample_rate"):
            parts.append(f"{candidate['sample_rate']} Hz")
        parts.append(f"语言 {candidate.get('language') or '未标注'}")
    return "，".join(parts)


def dictionary_lines(details: dict) -> list[str]:
    items = details.get("unsupported") or details.get("unaccepted") or []
    return [f"{ROLE_TEXT.get(item.get('role'), item.get('role'))} 第 {item.get('line')} 行：{str(item.get('text') or '').strip()}  —— {item.get('reason')}"
            for item in items]


def question_of(result: dict) -> tuple[str, dict] | None:
    """The question a refused command asks the user, if any: ("video_stream"|"audio_stream"|"no_audio_stream"|"dictionary", details)."""
    error = result.get("error") or {}
    details = error.get("details") or {}
    if error.get("code") == "NEEDS_SELECTION" and details.get("kind") in ("video_stream", "audio_stream") and details.get("candidates") is not None:
        return details["kind"], details
    if error.get("code") == "NEEDS_SELECTION" and details.get("kind") == "no_audio_stream":
        return "no_audio_stream", details
    if error.get("code") == "DICTIONARY_UNSUPPORTED":
        return "dictionary", details
    return None


def resource_state_text(row: dict) -> str:
    return RESOURCE_STATE_TEXT.get(row["state"], row["state"])


# --------------------------------------------------------------------------- the queue tab

class QueueTab(ttk.Frame):
    """The queue list and its operations. ``host`` is the V2Panel: data root, command runner, log, main-thread calls."""

    def __init__(self, master, host):
        super().__init__(master)
        self.host = host
        self.overview: dict | None = None
        self.rows: dict[str, dict] = {}
        self.executor: subprocess.Popen | None = None
        self.state_var = tk.StringVar(value=executor_text(None, own=False))
        self._poll_after: str | None = None
        self._polling = False
        self._build()
        self.update_buttons()

    def _build(self) -> None:
        columns = (("name", "名称", 150), ("input", "输入", 170), ("mode", "模式", 150), ("status", "执行状态", 140), ("stage", "当前阶段", 180),
                   ("machine", "机器版本", 64), ("human", "人工版本数", 72), ("quality", "质量状态", 96))
        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(frame, columns=[column for column, _, _ in columns], show="headings", height=9, selectmode="browse")
        for column, text, width in columns:
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, stretch=column in ("name", "input", "stage"))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_buttons())
        ttk.Label(self, textvariable=self.state_var, wraplength=900, justify=tk.LEFT).pack(fill=tk.X, pady=(4, 0))
        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, pady=(4, 0))
        self.buttons: dict[str, ttk.Button] = {}
        for position, (key, text) in enumerate([*QUEUE_ACTIONS.items(), ("refresh", "刷新列表")]):
            button = ttk.Button(buttons, text=text, command=self.refresh if key == "refresh" else (lambda key=key: self.invoke(key)))
            button.grid(row=position // 5, column=position % 5, sticky="we", padx=2, pady=2)
            if key != "refresh":
                self.buttons[key] = button

    def destroy(self) -> None:
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None
        super().destroy()

    # ---- state
    def selected_row(self) -> dict | None:
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def running_row(self) -> dict | None:
        return next((row for row in (self.overview or {}).get("requests", []) if row["status"] == "RUNNING"), None)

    def own_executor_running(self) -> bool:
        return self.executor is not None and self.executor.poll() is None

    def update_buttons(self) -> None:
        states = queue_action_states(self.selected_row(), self.overview, own_executor=self.own_executor_running(),
                                     data_root_ok=self.host.data_root() is not None, config_ok=self.host.config_path() is not None)
        for key, button in self.buttons.items():
            button.configure(state=tk.NORMAL if states[key] else tk.DISABLED)

    def refresh(self) -> None:
        root = self.host.data_root()
        if root is None:
            self.show(0, None)
            return
        self.show(*self.host.runner("queue", "list", "--data-root", str(root)))

    def show(self, code: int, result: dict | None) -> None:
        from upgrade.gui import result_text
        if result is None:
            self.overview, self.rows = None, {}
            self.tree.delete(*self.tree.get_children())
            self.state_var.set(executor_text(None, own=False))
        elif not result.get("ok"):
            self.state_var.set(result_text("读取队列", code, result))
        else:
            self.overview = result["ids"]
            keep = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            self.rows = {}
            for row in self.overview["requests"]:
                self.tree.insert("", tk.END, iid=row["request_id"], values=queue_values(row))
                self.rows[row["request_id"]] = row
            if keep and self.tree.exists(keep[0]):
                self.tree.selection_set(keep[0])
            self.state_var.set(executor_text(self.overview, own=self.own_executor_running()))
        self.update_buttons()
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        live = self.own_executor_running() or bool(self.overview and self.overview["executor"]["live"])
        if live and self._poll_after is None:
            self._poll_after = self.after(2000, self._poll)

    def _poll(self) -> None:
        self._poll_after = None
        root = self.host.data_root()
        if root is None or self._polling:
            return
        self._polling = True

        def work() -> None:
            outcome = self.host.runner("queue", "list", "--data-root", str(root))
            self.host.call_soon(lambda: self._polled(*outcome))
        threading.Thread(target=work, daemon=True).start()

    def _polled(self, code: int, result: dict) -> None:
        self._polling = False
        if self.winfo_exists():
            self.show(code, result)

    def invoke(self, key: str) -> None:
        getattr(self, f"do_{key}")()

    def where(self) -> list[str]:
        return ["--data-root", str(self.host.data_root())]

    # ---- answering the questions a command asks, then running it again
    def settle(self, title: str, build: Callable[[dict], list[str]], *, mode: str, ask_streams: bool = True) -> dict | None:
        from upgrade.gui import result_text
        answers: dict[str, Any] = {"video_stream": None, "audio_stream": None, "accept": False}
        while True:
            code, result = self.host.runner(*build(answers))
            if result.get("ok"):
                self.host.write(result_text(title, code, result))
                return result
            kind, details = question_of(result) or (None, {})
            if kind in ("video_stream", "audio_stream") and ask_streams and answers[kind] is None:
                choice = self.ask_stream(kind, details["candidates"], allow_none=kind == "audio_stream" and mode in ("visual_only", "supplied_source"))
                if choice is None:
                    self.host.write(f"✘ {title} 未完成：没有选择{'视频流' if kind == 'video_stream' else '音轨'}。")
                    return None
                answers[kind] = choice
                continue
            if kind == "no_audio_stream":
                messagebox.showwarning(title, "这个视频没有可用的音轨，而所选模式需要声音。\n可以把识别模式改成“纯画面文字”后重新提交（程序不会替你改模式）。")
            elif kind == "dictionary" and not answers["accept"]:
                if self.ask_dictionary(details):
                    answers["accept"] = True
                    continue
                self.host.write(f"✘ {title} 未完成：词典里有新版不支持的条目，没有确认接受。")
                return None
            self.host.write(result_text(title, code, result))
            return None

    # ---- plan 3.1: new task
    def do_new_task(self) -> None:
        spec = self.ask_new_task()
        if spec is not None:
            self.submit_new_task(spec)

    def submit_new_task(self, spec: dict) -> dict | None:
        from upgrade.gui import result_text
        root, config, name = str(self.host.data_root()), str(self.host.config_path()), spec["display_name"].strip()
        if spec["mode"] == "supplied_source":
            copy = ["--copy-original"] if spec.get("copy_original") else []
            self.host.write("… 登记原视频（第一次会完整计算文件哈希，大文件需要一点时间）")
            self.update_idletasks()
            registered = self.settle("登记原视频", lambda answers: ["media", "register", "--data-root", root, "--video", spec["video"], "--display-name", name,
                                                               *stream_args(answers), *copy], mode="supplied_source")
            if registered is None:
                return None
            project_id, media_id = registered["ids"]["project_id"], registered["ids"]["media_id"]
            code, imported = self.host.runner("source", "import", "--data-root", root, "--project", project_id, "--media", media_id, "--file", spec["subtitle"])
            self.host.write(result_text("登记日文源字幕", code, imported))
            if not imported.get("ok"):
                self.host.write("项目已建立并登记了原视频，但日文字幕没有登记成功，所以没有加入队列。请检查字幕文件后再建任务。")
                self.host.refresh()
                return None
            source_revision = imported["ids"]["revision_id"]
            submitted = self.settle("加入队列", lambda answers: ["submit", "--config", config, "--mode", "supplied_source", "--project", project_id,
                                                             "--source-revision", source_revision, "--display-name", name,
                                                             *(["--accept-dictionary-warnings"] if answers["accept"] else [])],
                                    mode="supplied_source", ask_streams=False)
        else:
            submitted = self.settle("加入队列", lambda answers: submit_argv(spec, config, answers), mode=spec["mode"])
        self.refresh()
        if submitted is None:
            return None
        self.host.write(f"已加入队列：{name}（{MODE_TEXT[spec['mode']]}）。" + ("" if spec.get("start") else "点“开始队列”开始处理。"))
        self.host.refresh()
        if spec.get("start"):
            self.start_queue()
        return submitted

    # ---- plan 3.2: queue operations
    def do_start_queue(self) -> None:
        self.start_queue()

    def start_queue(self) -> None:
        from upgrade.gui import finish_cli, start_cli
        root = self.host.data_root()
        if root is None or self.own_executor_running():
            return
        process = start_cli("queue", "run", "--data-root", str(root))
        self.executor = process
        self.host.write("… 开始队列：执行器在后台运行。可以随时“停止当前”（之后可恢复）或“跳过当前”。")

        def wait() -> None:
            outcome = finish_cli(process)
            self.host.call_soon(lambda: self._executor_finished(*outcome))
        threading.Thread(target=wait, daemon=True).start()
        self.update_buttons()
        self._schedule_poll()

    def _executor_finished(self, code: int, result: dict) -> None:
        from upgrade.gui import result_text
        self.executor = None
        if result.get("ok"):
            ids = result["ids"]
            self.host.write(f"✔ 队列已结束：处理 {ids['processed']} 个（完成 {ids['completed']}，失败 {ids['failed']}，跳过或取消 {ids['cancelled']}，"
                            f"中断 {ids['interrupted']}）" + ("；已按“停止当前”暂停，剩下的任务仍在排队。" if ids.get("paused") else "。"))
        else:
            self.host.write(result_text("开始队列", code, result))
        if self.winfo_exists():
            self.refresh()
            self.host.refresh()

    def do_stop_current(self) -> None:
        from upgrade.gui import result_text
        code, result = self.host.runner("queue", "stop", *self.where())
        if result.get("ok") and result["ids"]["requested"]:
            self.host.write("已请求停止：当前任务会在几秒内停下并标为“已停止（可恢复）”，队列暂停。之后可以选中它点“恢复中断任务”。")
        else:
            self.host.write(result_text("停止当前", code, result))
        self.refresh()

    def do_skip_current(self) -> None:
        from upgrade.gui import result_text
        row = self.running_row()
        if row is None:
            return
        if not messagebox.askyesno("跳过当前", f"跳过“{row.get('display_name') or ''}”？\n当前阶段的结果不会提交（已完成的阶段保留），队列接着处理下一个任务。\n"
                                             "跳过的任务不能恢复；以后需要时可以“复制为新任务”。"):
            return
        code, result = self.host.runner("queue", "cancel", *self.where(), "--request", row["request_id"])
        self.host.write("已请求跳过当前任务，队列会接着处理下一个。" if result.get("ok") else result_text("跳过当前", code, result))
        self.refresh()

    def do_remove_request(self) -> None:
        from upgrade.gui import result_text
        row = self.selected_row()
        if not messagebox.askyesno("移除未开始请求", f"把“{row.get('display_name') or ''}”移出队列？\n请求记录会保留（显示为“已移除”），不会被执行。"):
            return
        code, result = self.host.runner("queue", "cancel", *self.where(), "--request", row["request_id"])
        self.host.write(result_text("移除未开始请求", code, result))
        self.refresh()

    def do_resume_request(self) -> None:
        from upgrade.gui import result_text
        row = self.selected_row()
        code, result = self.host.runner("resume", *self.where(), "--run", row["run_id"])
        if result.get("ok"):
            live = self.own_executor_running() or bool(self.overview and self.overview["executor"]["live"])
            self.host.write("已重新排队：沿用原来的配置，新增一次尝试，已完成的阶段不重跑。" + ("队列正在运行，会接着处理它。" if live else "点“开始队列”开始处理。"))
        else:
            self.host.write(result_text("恢复中断任务", code, result))
        self.refresh()

    def do_copy_request(self) -> None:
        row = self.selected_row()
        result = self.settle("复制为新任务", lambda answers: ["queue", "copy", *self.where(), "--request", row["request_id"],
                                                         *(["--accept-dictionary-warnings"] if answers["accept"] else [])],
                             mode=row["recognition_mode"], ask_streams=False)
        if result is not None:
            self.host.write("新任务" + ("采用了改动后的当前配置。" if result["ids"]["config_changed"] else "的配置与原任务相同（之后没有改过设置）。")
                            + ("" if self.own_executor_running() else "点“开始队列”开始处理。"))
        self.refresh()

    def do_resources(self) -> None:
        self.open_resources()

    # ---- dialogs (methods so a test can answer them)
    def ask_new_task(self) -> dict | None:
        return ask_new_task_dialog(self)

    def ask_stream(self, kind: str, candidates: list[dict], *, allow_none: bool) -> str | None:
        return ask_stream_dialog(self, kind, candidates, allow_none=allow_none)

    def ask_dictionary(self, details: dict) -> bool:
        return ask_dictionary_dialog(self, details)

    def open_resources(self) -> None:
        open_resources_dialog(self, self.host)

    # ---- closing the window (plan 10.5: "取消当前并退出" or "返回"; no invisible background executor)
    EXIT_WAIT_SECONDS = 20.0      # the executor needs about 3 s to stop owned tools (plan 10.5); after this it is terminated

    def stop_for_exit(self) -> None:
        process = self.executor
        if process is None or process.poll() is not None:
            return
        root = self.host.data_root()
        if root is not None:
            self.host.runner("queue", "stop", "--data-root", str(root))
        deadline = time.monotonic() + self.EXIT_WAIT_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            try:
                self.update()
            except tk.TclError:
                pass
            time.sleep(0.1)
        if process.poll() is None:            # our own child only; its tools die with its job object
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


# --------------------------------------------------------------------------- dialogs

def _modal(dialog: tk.Toplevel, parent: tk.Misc) -> None:
    dialog.transient(parent.winfo_toplevel())
    dialog.grab_set()
    parent.wait_window(dialog)


def ask_new_task_dialog(parent: tk.Misc) -> dict | None:
    """Plan 3.1: source (local video / single-video URL) and display name are required; the mode is one of four fixed values."""
    dialog = tk.Toplevel(parent)
    dialog.title("新任务")
    source_kind, video, url, name, subtitle = tk.StringVar(value="file"), tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
    mode, timing = tk.StringVar(value=MODE_TEXT["combined"]), tk.StringVar(value=TIMING_TEXT["mixed"])
    copy_original = tk.BooleanVar(value=False)
    note, problems = tk.StringVar(value=MODE_NOTES["combined"]), tk.StringVar()
    mode_code = {text: code for code, text in MODE_TEXT.items()}
    timing_code = {text: code for code, text in TIMING_TEXT.items()}
    answer: dict = {}
    body = ttk.Frame(dialog, padding=12)
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text="输入来源").grid(row=0, column=0, sticky="w")
    kinds = ttk.Frame(body)
    kinds.grid(row=0, column=1, sticky="w")
    ttk.Radiobutton(kinds, text="本地视频", variable=source_kind, value="file").pack(side=tk.LEFT)
    ttk.Radiobutton(kinds, text="单个视频的 URL", variable=source_kind, value="url").pack(side=tk.LEFT, padx=(12, 0))

    def choose_video() -> None:
        chosen = filedialog.askopenfilename(parent=dialog, title="选择本地视频", filetypes=[("视频", "*.mp4 *.mkv *.mov *.webm *.avi *.flv *.ts"), ("全部", "*.*")])
        if chosen:
            video.set(chosen)
            if not name.get().strip():
                name.set(Path(chosen).stem)

    def choose_subtitle() -> None:
        chosen = filedialog.askopenfilename(parent=dialog, title="选择日文源字幕", filetypes=[("字幕", "*.srt *.ass"), ("全部", "*.*")])
        if chosen:
            subtitle.set(chosen)

    ttk.Label(body, text="本地视频").grid(row=1, column=0, sticky="w", pady=(6, 0))
    video_entry = ttk.Entry(body, textvariable=video, width=56)
    video_entry.grid(row=1, column=1, sticky="we", pady=(6, 0))
    video_button = ttk.Button(body, text="浏览...", command=choose_video)
    video_button.grid(row=1, column=2, padx=(4, 0), pady=(6, 0))
    ttk.Label(body, text="视频 URL").grid(row=2, column=0, sticky="w", pady=(6, 0))
    url_entry = ttk.Entry(body, textvariable=url, width=56)
    url_entry.grid(row=2, column=1, sticky="we", pady=(6, 0))
    ttk.Label(body, text="项目显示名").grid(row=3, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(body, textvariable=name, width=56).grid(row=3, column=1, sticky="we", pady=(6, 0))
    ttk.Label(body, text="（只用于显示）").grid(row=3, column=2, sticky="w", padx=(4, 0), pady=(6, 0))
    ttk.Label(body, text="识别模式").grid(row=4, column=0, sticky="w", pady=(6, 0))
    mode_box = ttk.Combobox(body, textvariable=mode, state="readonly", values=[MODE_TEXT[code] for code in MODE_ORDER], width=24)
    mode_box.grid(row=4, column=1, sticky="w", pady=(6, 0))
    ttk.Label(body, text="时间轴模式").grid(row=5, column=0, sticky="w", pady=(6, 0))
    timing_box = ttk.Combobox(body, textvariable=timing, state="readonly", values=[TIMING_TEXT[code] for code in TIMING_ORDER], width=24)
    timing_box.grid(row=5, column=1, sticky="w", pady=(6, 0))
    ttk.Label(body, text="日文源字幕").grid(row=6, column=0, sticky="w", pady=(6, 0))
    subtitle_entry = ttk.Entry(body, textvariable=subtitle, width=56)
    subtitle_entry.grid(row=6, column=1, sticky="we", pady=(6, 0))
    subtitle_button = ttk.Button(body, text="浏览...", command=choose_subtitle)
    subtitle_button.grid(row=6, column=2, padx=(4, 0), pady=(6, 0))
    copy_box = ttk.Checkbutton(body, text="把原视频复制进项目（占用空间；以后原文件移动也不影响）", variable=copy_original)
    copy_box.grid(row=7, column=1, sticky="w", pady=(6, 0))
    ttk.Label(body, textvariable=note, wraplength=520, justify=tk.LEFT).grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(body, textvariable=problems, wraplength=520, justify=tk.LEFT, foreground="#b00020").grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))
    body.columnconfigure(0, pad=10)
    body.columnconfigure(1, weight=1)

    def sync(*_args) -> None:
        code = mode_code.get(mode.get(), "combined")
        local = source_kind.get() == "file"
        for widget in (video_entry, video_button, copy_box):
            widget.configure(state=tk.NORMAL if local else tk.DISABLED)
        url_entry.configure(state=tk.DISABLED if local else tk.NORMAL)
        timing_box.configure(state="readonly" if code == "combined" else tk.DISABLED)
        for widget in (subtitle_entry, subtitle_button):
            widget.configure(state=tk.NORMAL if code == "supplied_source" else tk.DISABLED)
        note.set(MODE_NOTES[code])
    source_kind.trace_add("write", sync)
    mode_box.bind("<<ComboboxSelected>>", sync)
    sync()

    def submit(start: bool) -> None:
        spec = {"source_kind": source_kind.get(), "video": video.get().strip(), "url": url.get().strip(), "display_name": name.get(),
                "mode": mode_code.get(mode.get()), "timing": timing_code.get(timing.get()), "subtitle": subtitle.get().strip(),
                "copy_original": copy_original.get() and source_kind.get() == "file", "start": start}
        found = new_task_problems(spec)
        if found:
            problems.set("\n".join(found))
            return
        answer.update(spec)
        dialog.destroy()
    actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
    actions.pack(fill=tk.X)
    ttk.Button(actions, text="加入队列并开始", command=lambda: submit(True)).pack(side=tk.RIGHT)
    ttk.Button(actions, text="只加入队列", command=lambda: submit(False)).pack(side=tk.RIGHT, padx=6)
    ttk.Button(actions, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
    _modal(dialog, parent)
    return answer or None


def ask_stream_dialog(parent: tk.Misc, kind: str, candidates: list[dict], *, allow_none: bool) -> str | None:
    """Plan 3.1: several video streams or audio tracks are chosen by the user; the first one is never assumed to be Japanese."""
    dialog = tk.Toplevel(parent)
    what = "视频流" if kind == "video_stream" else "音轨"
    dialog.title(f"选择{what}")
    choice, answer = tk.StringVar(value=""), {}
    ttk.Label(dialog, text=f"这个文件有多个{what}，请选择要使用的一个（程序不会默认用第一个）：", wraplength=460).pack(anchor="w", padx=12, pady=(10, 4))
    for candidate in candidates:
        ttk.Radiobutton(dialog, text=stream_label(candidate, kind), variable=choice, value=str(candidate.get("index"))).pack(anchor="w", padx=20)
    if allow_none:
        ttk.Radiobutton(dialog, text="不要音轨", variable=choice, value="none").pack(anchor="w", padx=20)
    actions = ttk.Frame(dialog, padding=12)
    actions.pack(fill=tk.X)
    ttk.Button(actions, text="确定", command=lambda: (answer.update(choice=choice.get()), dialog.destroy()) if choice.get() else None).pack(side=tk.RIGHT)
    ttk.Button(actions, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=6)
    _modal(dialog, parent)
    return answer.get("choice")


def ask_dictionary_dialog(parent: tk.Misc, details: dict) -> bool:
    """Plan 8.5: unsupported dictionary entries are listed (file, line, entry, reason) and only used after the user accepts."""
    dialog = tk.Toplevel(parent)
    dialog.title("词典条目确认")
    lines = dictionary_lines(details)
    answer = {"accept": False}
    ttk.Label(dialog, text="下面这些词典条目新版不支持。继续的话，这次翻译只使用其余条目；你的确认会被记住，同一份词典内容不会再问。\n"
                           "如果想让它们生效，请先取消，到词典文件里改好再提交。", wraplength=560, justify=tk.LEFT).pack(anchor="w", padx=12, pady=(10, 4))
    text = tk.Text(dialog, height=min(12, len(lines) + 1), width=80, wrap="word")
    text.insert("1.0", "\n".join(lines) or "（没有列出具体条目）")
    text.configure(state=tk.DISABLED)
    text.pack(fill=tk.BOTH, expand=True, padx=12)
    actions = ttk.Frame(dialog, padding=12)
    actions.pack(fill=tk.X)
    ttk.Button(actions, text="接受并继续", command=lambda: (answer.update(accept=True), dialog.destroy())).pack(side=tk.RIGHT)
    ttk.Button(actions, text="取消（先去改词典）", command=dialog.destroy).pack(side=tk.RIGHT, padx=6)
    _modal(dialog, parent)
    return answer["accept"]


def open_resources_dialog(parent: tk.Misc, host) -> None:
    """Plan 3.4 / 10.3: the declared tools and models; a moved resource is pointed at its new place (same content only)."""
    from upgrade.gui import result_text
    config = host.config_path()
    dialog = tk.Toplevel(parent)
    dialog.title("资源与工具")
    info, rows = tk.StringVar(), {}
    tree = ttk.Treeview(dialog, columns=("id", "kind", "state", "path"), show="headings", height=10, selectmode="browse")
    for column, text, width in (("id", "资源", 150), ("kind", "类型", 110), ("state", "状态", 200), ("path", "本机位置", 380)):
        tree.heading(column, text=text)
        tree.column(column, width=width, stretch=column == "path")
    tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(10, 4))
    ttk.Label(dialog, textvariable=info, wraplength=820, justify=tk.LEFT).pack(fill=tk.X, padx=12)

    def load() -> None:
        code, result = host.runner("resources", "list", "--config", str(config))
        tree.delete(*tree.get_children())
        rows.clear()
        if not result.get("ok"):
            info.set(result_text("读取资源", code, result))
            return
        for row in result["ids"]["resources"]:
            tree.insert("", tk.END, iid=row["resource_id"], values=(row["resource_id"], row["kind"], resource_state_text(row), row["absolute_path"]))
            rows[row["resource_id"]] = row
        info.set("资源移动了位置时，选中它点“重新定位…”指向新位置；只有内容和登记时一致才会接受。没有登记过的资源请用命令行 resources register 登记。"
                 if rows else "还没有登记任何资源（用命令行 resources register 登记）。")

    def relocate() -> None:
        selection = tree.selection()
        if not selection:
            return
        row = rows[selection[0]]
        title = f"“{row['resource_id']}”的新位置"
        chosen = filedialog.askdirectory(parent=dialog, title=title) if row["directory"] else filedialog.askopenfilename(parent=dialog, title=title)
        if not chosen:
            return
        code, result = host.runner("resources", "rebind", "--config", str(config), "--id", row["resource_id"], "--path", chosen)
        host.write(result_text("重新定位资源", code, result))
        load()
        info.set(result_text("重新定位资源", code, result))
    actions = ttk.Frame(dialog, padding=12)
    actions.pack(fill=tk.X)
    ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)
    ttk.Button(actions, text="刷新", command=load).pack(side=tk.RIGHT, padx=6)
    ttk.Button(actions, text="重新定位…", command=relocate).pack(side=tk.RIGHT)
    load()
    _modal(dialog, parent)


def ask_close_dialog(parent: tk.Misc, what: str) -> bool:
    """Plan 10.5: with work running, closing offers only "取消当前并退出" or "返回"."""
    dialog = tk.Toplevel(parent)
    dialog.title("仍有任务在运行")
    answer = {"exit": False}
    ttk.Label(dialog, text=f"{what}。\n关闭窗口前要先停下：队列里正在运行的任务会标为“已停止（可恢复）”，下次打开后可以恢复；"
                           "后台导出或导入会被中止，不会生成不完整的成品（导出位置旁可能留下一个名字以 .pending 结尾的临时文件夹，可以删掉）。",
              wraplength=440, justify=tk.LEFT).pack(anchor="w", padx=12, pady=12)
    actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
    actions.pack(fill=tk.X)
    ttk.Button(actions, text="返回", command=dialog.destroy).pack(side=tk.RIGHT)
    ttk.Button(actions, text="取消当前并退出", command=lambda: (answer.update(exit=True), dialog.destroy())).pack(side=tk.RIGHT, padx=6)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    _modal(dialog, parent)
    return answer["exit"]


__all__ = ["MODE_TEXT", "TIMING_TEXT", "STAGE_TEXT", "QUEUE_ACTIONS", "QueueTab", "format_elapsed", "input_text", "mode_text", "request_status_text",
           "stage_text", "quality_text", "queue_values", "executor_text", "queue_action_states", "new_task_problems", "submit_argv", "stream_args",
           "stream_label", "dictionary_lines", "question_of", "resource_state_text", "ask_close_dialog"]
