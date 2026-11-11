#!/usr/bin/env python3
"""The v2 panel inside the existing tkinter window (plan 3.1-3.3; work package W08).

The panel is a front end of the v2 command line: every action runs ``python -m upgrade.cli …`` and shows its one
JSON answer, so the window can never do something the commands refuse (no implicit ``latest``, no confirmation that
the user did not tick, no overwrite). It reads project indexes only to list them. It never edits a subtitle, never
waits for or watches an editor, never registers a file because an editor closed, and closing the window does not stop
an editor it opened. Its own settings (data root, editor path) live in a separate small file; the old configuration is
not rewritten. The queue list, the new-task dialog and the resources list are in ``gui_queue``.
"""

from __future__ import annotations

import datetime
import json
import os
import queue as queue_module
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from upgrade.gui_queue import QueueTab, ask_close_dialog, format_elapsed

TOOLS_DIR = Path(__file__).resolve().parents[1]
STATUS_TEXT = {"UNREVIEWED": "未经人工检查", "PARTIAL": "部分确认", "REVIEWED": "已人工确认"}
SCOPE_TEXT = {"text": "文字", "timing": "时间", "style": "样式"}
KIND_TEXT = {"machine": "机器版本", "source": "日文源版本", "review": "人工中文版本"}
ACTIONS = {
    "open_dir": "打开输出目录", "open_media": "打开原视频", "open_report": "查看复核报告", "create_session": "创建校对副本", "open_editor": "用编辑器打开",
    "register_review": "登记校对稿", "revise_source": "修订日文原文", "translate_source": "据此生成新机器译文",
    "export_subtitles": "导出字幕", "export_video": "导出视频", "attest": "标记人工确认", "migrate": "导入旧任务",
}


# --------------------------------------------------------------------------- pure parts (tested without a window)

def status_label(row: dict, fmt: str) -> str:
    """What the window says about one file. "已审" only ever comes from the user's own confirmation."""
    entry = row["formats"][fmt]
    if row["kind"] == "machine" and entry["human_status"] == "UNREVIEWED":
        return "机器字幕已生成，等待人工检查"
    scopes = "、".join(SCOPE_TEXT[scope] for scope in entry["confirmed_scopes"])
    return STATUS_TEXT[entry["human_status"]] + (f"（已确认：{scopes}）" if scopes else "（确认范围为空）")


def scope_choices(fmt: str) -> dict[str, bool]:
    """The confirmation dialog: every box starts unticked; SRT has no style to confirm ("不适用")."""
    return {"text": True, "timing": True, "style": fmt == "ass"}


def action_states(row: dict | None, fmt: str | None, *, session: dict | None, source_session: dict | None, source_revision_id: str | None,
                  data_root_ok: bool) -> dict[str, bool]:
    """Which buttons are usable (plan 3.2 conditions)."""
    selected = row is not None and "error" not in row
    valid = selected and row.get("structural_status") == "valid"
    chosen = selected and fmt in (row.get("formats") or {})
    return {
        "open_dir": selected,
        "open_media": selected and bool(row.get("media_id")),
        "open_report": selected and bool(row.get("report_html")),
        "create_session": chosen,
        "open_editor": session is not None and Path(session["edit_file"]).is_file(),
        "register_review": selected,
        "revise_source": source_session is not None or (selected and row["kind"] in ("machine", "source")),
        "translate_source": source_revision_id is not None,
        "export_subtitles": valid,
        "export_video": valid,
        "attest": chosen,
        "migrate": data_root_ok,
    }


def cli_argv(*args: str) -> list[str]:
    return [sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args]


def start_cli(*args: str) -> subprocess.Popen:
    """One command as a child process of this window (no console window, no shell)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(TOOLS_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cli_argv(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, cwd=str(TOOLS_DIR),
                            creationflags=flags)


def finish_cli(process: subprocess.Popen) -> tuple[int, dict]:
    stdout, stderr = process.communicate()        # reads both pipes to the end, so a chatty command never blocks
    try:
        return process.returncode, json.loads(stdout)
    except ValueError:
        return process.returncode, {"ok": False, "error": {"code": "CLI_OUTPUT", "message": (stderr or stdout or "没有输出（进程被中止）")[-800:]}}


def run_cli(*args: str) -> tuple[int, dict]:
    return finish_cli(start_cli(*args))


def list_projects(data_root: Path) -> list[dict]:
    rows = []
    for path in sorted((Path(data_root) / "projects").glob("*/project.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({"project_id": record.get("project_id"), "display_name": record.get("display_name"),
                     "machine": len(record.get("machine_revision_ids") or []), "human": len(record.get("review_revision_ids") or []),
                     "legacy": bool((record.get("extensions") or {}).get("legacy_import"))})
    return rows


def result_text(operation: str, code: int, result: dict) -> str:
    if result.get("ok"):
        warnings = result.get("warnings") or []
        lines = [f"✔ {operation} 完成"] + [f"  · {item.get('message') or item.get('code')}" for item in warnings[:8]]
        return "\n".join(lines)
    error = result.get("error") or {}
    return f"✘ {operation} 未完成（退出码 {code}，{error.get('code')}）：{error.get('message')}"


# --------------------------------------------------------------------------- the panel

class V2Panel(ttk.Frame):
    """Projects and revisions of one v2 data root, with the task-detail buttons of plan 3.2."""

    def __init__(self, master, *, settings_path: Path, runner: Callable[..., tuple[int, dict]] = run_cli):
        super().__init__(master)
        self.settings_path, self.runner = Path(settings_path), runner
        settings = self._load_settings()
        self.data_root_var = tk.StringVar(value=settings.get("data_root", ""))
        self.editor_var = tk.StringVar(value=settings.get("editor", ""))
        self.format_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="选择数据根后点“刷新”。")
        self.project_id: str | None = None
        self.rows: dict[str, dict] = {}
        self.session: dict | None = None
        self.source_session: dict | None = None
        self.source_revision_id: str | None = None
        self.busy = False
        self.busy_title: str | None = None
        self.busy_since = 0.0
        self.children_running: list[subprocess.Popen] = []
        self._inbox: queue_module.Queue = queue_module.Queue()
        self._build()
        self.update_buttons()
        self._timers: dict[str, str] = {"drain": self.after(150, self._drain)}

    def destroy(self) -> None:
        for timer in self._timers.values():
            if timer:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
        self._timers.clear()
        super().destroy()

    # ---- calls from worker threads run on the window's thread
    def call_soon(self, callback: Callable[[], None]) -> None:
        self._inbox.put(callback)

    def _drain(self) -> None:
        try:
            while True:
                try:
                    callback = self._inbox.get_nowait()
                except queue_module.Empty:
                    break
                callback()
        finally:
            if "drain" in self._timers:
                self._timers["drain"] = self.after(150, self._drain)

    # ---- settings (own file; the old configuration is never written)
    def _load_settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({"data_root": self.data_root_var.get(), "editor": self.editor_var.get()}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- layout
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X)
        ttk.Label(top, text="数据根").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.data_root_var, width=60).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="浏览...", command=self._choose_root).grid(row=0, column=2)
        ttk.Button(top, text="刷新", command=self.refresh).grid(row=0, column=3, padx=(4, 0))
        ttk.Label(top, text="编辑器").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(top, textvariable=self.editor_var, width=60).grid(row=1, column=1, sticky="we", padx=4, pady=(4, 0))
        ttk.Button(top, text="浏览...", command=self._choose_editor).grid(row=1, column=2, pady=(4, 0))
        top.columnconfigure(1, weight=1)
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.queue_tab = QueueTab(self.tabs, self)
        self.project_tab = ttk.Frame(self.tabs, padding=4)
        self.tabs.add(self.queue_tab, text="任务队列")
        self.tabs.add(self.project_tab, text="项目与版本")
        middle = ttk.Panedwindow(self.project_tab, orient=tk.HORIZONTAL)
        middle.pack(fill=tk.BOTH, expand=True, pady=6)
        self.projects = ttk.Treeview(middle, columns=("name", "machine", "human"), show="headings", height=8, selectmode="browse")
        for column, text, width in (("name", "项目", 180), ("machine", "机器版本", 70), ("human", "人工版本", 70)):
            self.projects.heading(column, text=text)
            self.projects.column(column, width=width, stretch=column == "name")
        self.projects.bind("<<TreeviewSelect>>", lambda _event: self._project_selected())
        middle.add(self.projects, weight=1)
        self.revisions = ttk.Treeview(middle, columns=("kind", "formats", "created", "hash", "status", "quality"), show="headings", height=8, selectmode="browse")
        for column, text, width in (("kind", "版本", 100), ("formats", "格式", 70), ("created", "创建时间(UTC)", 150), ("hash", "内容哈希", 110),
                                    ("status", "人工状态", 220), ("quality", "质量", 120)):
            self.revisions.heading(column, text=text)
            self.revisions.column(column, width=width, stretch=column == "status")
        self.revisions.bind("<<TreeviewSelect>>", lambda _event: self._revision_selected())
        middle.add(self.revisions, weight=3)
        choose = ttk.Frame(self.project_tab)
        choose.pack(fill=tk.X)
        ttk.Label(choose, text="格式").pack(side=tk.LEFT)
        self.format_box = ttk.Combobox(choose, textvariable=self.format_var, state="readonly", width=6, values=())
        self.format_box.pack(side=tk.LEFT, padx=4)
        self.format_box.bind("<<ComboboxSelected>>", lambda _event: self.update_buttons())
        buttons = ttk.Frame(self.project_tab)
        buttons.pack(fill=tk.X, pady=(6, 0))
        self.buttons: dict[str, ttk.Button] = {}
        for position, (key, text) in enumerate(ACTIONS.items()):
            button = ttk.Button(buttons, text=text, command=lambda key=key: self.invoke(key))
            button.grid(row=position // 6, column=position % 6, sticky="we", padx=2, pady=2)
            self.buttons[key] = button
        ttk.Label(self, textvariable=self.status_var, wraplength=900, justify=tk.LEFT).pack(fill=tk.X, pady=(6, 0))
        self.log = tk.Text(self, height=8, wrap="word", state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=False, pady=(4, 0))

    # ---- state
    def data_root(self) -> Path | None:
        value = self.data_root_var.get().strip()
        return Path(value) if value and (Path(value) / "projects").is_dir() else None

    def config_path(self) -> Path | None:
        root = self.data_root()
        return root / "settings" / "app.json" if root is not None and (root / "settings" / "app.json").is_file() else None

    def selected_row(self) -> dict | None:
        selection = self.revisions.selection()
        return self.rows.get(selection[0]) if selection else None

    def update_buttons(self) -> None:
        states = action_states(self.selected_row(), self.format_var.get() or None, session=self.session, source_session=self.source_session,
                               source_revision_id=self.source_revision_id, data_root_ok=self.data_root() is not None)
        for key, button in self.buttons.items():
            button.configure(state=tk.NORMAL if states[key] and not self.busy else tk.DISABLED)
        self.buttons["revise_source"].configure(text="登记原文修订" if self.source_session else ACTIONS["revise_source"])

    def write(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def refresh(self) -> None:
        self.save_settings()
        self.projects.delete(*self.projects.get_children())
        root = self.data_root()
        self.queue_tab.refresh()
        if root is None:
            self.status_var.set("这个目录不是 v2 数据根（里面没有 projects 文件夹）。")
            self.update_buttons()
            return
        projects = list_projects(root)
        for row in projects:
            self.projects.insert("", tk.END, iid=row["project_id"], values=((row["display_name"] or "") + ("（旧任务导入）" if row["legacy"] else ""), row["machine"], row["human"]))
        if self.busy_title is None:
            self.status_var.set(f"数据根已就绪：{len(projects)} 个项目。新任务和队列在“任务队列”，校对和导出在“项目与版本”。")
        if self.project_id and self.projects.exists(self.project_id):
            self.projects.selection_set(self.project_id)
            self._project_selected()
        self.update_buttons()

    def _project_selected(self) -> None:
        selection = self.projects.selection()
        if not selection or self.data_root() is None:
            return
        self.project_id = selection[0]
        code, result = self.runner("revisions", "list", "--data-root", str(self.data_root()), "--project", self.project_id)
        self.revisions.delete(*self.revisions.get_children())
        self.rows = {}
        if not result.get("ok"):
            self.write(result_text("读取版本列表", code, result))
            return
        for row in result["ids"]["revisions"]:
            if "error" in row:
                values = (KIND_TEXT.get(row["kind"], row["kind"]), "", "", "", f"无法读取：{row['error']}", "")
            else:
                formats = row["formats"]
                quality = "有结构错误，不能导出" if row["structural_status"] != "valid" else f"提示 {row['issue_counts']['WARNING']} 条"
                values = (KIND_TEXT[row["kind"]], "/".join(fmt.upper() for fmt in formats), row["created_at"].replace("T", " ").rstrip("Z"),
                          " ".join(entry["sha256_12"] for entry in formats.values()), "；".join(f"{fmt.upper()}：{status_label(row, fmt)}" for fmt in formats), quality)
            self.revisions.insert("", tk.END, iid=row["revision_id"], values=values)
            self.rows[row["revision_id"]] = row
        self.status_var.set(f"项目“{result['ids']['display_name']}”：{len(self.rows)} 个版本。导出和确认都需要先选中一个具体版本。")
        self.update_buttons()

    def _revision_selected(self) -> None:
        row = self.selected_row()
        formats = sorted(row["formats"]) if row and "formats" in row else []
        self.format_box.configure(values=formats)
        self.format_var.set(formats[0] if len(formats) == 1 else "")     # two formats: the user picks one explicitly (plan 3.3)
        if row and "formats" in row:
            self.status_var.set(f"{KIND_TEXT[row['kind']]} {row['revision_id']}；" + "；".join(f"{fmt.upper()} 哈希 {entry['sha256_12']}，{status_label(row, fmt)}"
                                                                                         for fmt, entry in row["formats"].items()))
        self.update_buttons()

    # ---- running commands
    def run(self, title: str, args: list[str], done: Callable[[dict], None] | None = None, *, background: bool = False) -> None:
        def finish(code: int, result: dict) -> None:
            self.busy, self.busy_title = False, None
            self.write(result_text(title, code, result) + (f"（用时 {format_elapsed(time.monotonic() - started)}）" if background else ""))
            if result.get("ok") and done is not None:
                done(result)
            self.refresh()
        if not background:
            finish(*self.runner(*args))
            return
        started = time.monotonic()
        self.busy, self.busy_title, self.busy_since = True, title, started
        self.update_buttons()
        self.write(f"… {title}（在后台运行，可以继续查看其他版本）")
        self._tick()
        if self.runner is run_cli:
            process = start_cli(*args)
            self.children_running.append(process)

            def work() -> None:
                outcome = finish_cli(process)
                self.call_soon(lambda: (self.children_running.remove(process) if process in self.children_running else None, finish(*outcome)))
        else:
            def work() -> None:
                outcome = self.runner(*args)
                self.call_soon(lambda: finish(*outcome))
        threading.Thread(target=work, daemon=True).start()

    def _tick(self) -> None:
        """Background work shows how long it has been running; there is no invented percentage (plan 3.2)."""
        if self.busy_title is None:
            return
        self.status_var.set(f"{self.busy_title} 在后台运行，已耗时 {format_elapsed(time.monotonic() - self.busy_since)}。")
        if "drain" in self._timers:
            self._timers["tick"] = self.after(1000, self._tick)

    def confirm_close(self) -> bool:
        """Called by the old window before it closes: running work is stopped first, or the user goes back (plan 10.5)."""
        alive = [process for process in self.children_running if process.poll() is None]
        busy = (["队列正在运行"] if self.queue_tab.own_executor_running() else []) + ([f"{self.busy_title or '后台操作'}正在进行"] if alive else [])
        if not busy:
            return True
        if not ask_close_dialog(self, "、".join(busy)):
            return False
        self.queue_tab.stop_for_exit()
        for process in alive:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        return True

    def where(self) -> list[str]:
        return ["--data-root", str(self.data_root()), "--project", str(self.project_id)]

    def project_path(self, relative: str) -> Path:
        return Path(self.data_root()) / "projects" / str(self.project_id) / relative

    @staticmethod
    def open_path(path: Path) -> None:
        if hasattr(os, "startfile"):
            os.startfile(str(path))          # noqa: S606 - the user's own file in the system default program

    def new_destination(self, label: str) -> Path | None:
        parent = filedialog.askdirectory(title="选择导出位置（会在里面新建一个文件夹）")
        if not parent:
            return None
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return Path(parent) / f"{label}_{stamp}"

    def invoke(self, key: str) -> None:
        handler = getattr(self, f"do_{key}")
        handler()

    # ---- the buttons of plan 3.2
    def do_open_dir(self) -> None:
        self.open_path(self.project_path(self.selected_row()["directory"]))

    def ask_relocate(self, reason: str) -> str | None:
        if not messagebox.askyesno("重新定位媒体", f"{reason}。\n要指向它现在的位置吗？只接受内容和登记时完全相同的文件。"):
            return None
        return filedialog.askopenfilename(title="选择原视频现在的位置", filetypes=[("视频", "*.mp4 *.mkv *.mov *.webm *.avi *.flv *.ts"), ("全部", "*.*")]) or None

    def do_open_media(self) -> None:
        """Plan 3.2: open the original when it can still be found with the same content; otherwise relocate it."""
        row = self.selected_row()
        code, result = self.runner("media", "locate", *self.where(), "--media", row["media_id"])
        if result.get("ok"):
            self.open_path(Path(result["ids"]["path"]))
            return
        error = result.get("error") or {}
        if error.get("code") not in ("INPUT_MISSING", "MEDIA_CHANGED"):
            self.write(result_text("打开原视频", code, result))
            return
        reason = "找不到原视频（可能被移动或改了名字）" if error["code"] == "INPUT_MISSING" else "原来位置上的文件和登记的视频内容不一样"
        chosen = self.ask_relocate(reason)
        if not chosen:
            self.write(f"✘ 打开原视频 未完成：{reason}。")
            return
        code, moved = self.runner("media", "relocate", *self.where(), "--media", row["media_id"], "--path", chosen)
        self.write(result_text("重新定位媒体", code, moved))
        if moved.get("ok"):
            self.open_path(Path(moved["ids"]["path"]))

    def do_open_report(self) -> None:
        self.open_path(self.project_path(self.selected_row()["report_html"]))

    def do_create_session(self) -> None:
        row, fmt = self.selected_row(), self.format_var.get()

        def created(result: dict) -> None:
            self.session = {**result["ids"], "media_id": row["media_id"]}
            self.write(f"校对副本：{result['ids']['edit_file']}\n请只编辑这个文件；如需对照画面，在编辑器里另行打开原视频。保存后点“登记校对稿”。")
        self.run("创建校对副本", ["review", "create", *self.where(), "--revision", row["revision_id"], "--format", fmt], created)

    def do_open_editor(self) -> None:
        edit_file = Path(self.session["edit_file"])
        editor = self.editor_var.get().strip()
        if not editor:
            editor = filedialog.askopenfilename(title="选择字幕编辑器（Subtitle Edit / Aegisub）", filetypes=[("程序", "*.exe"), ("全部", "*.*")]) or ""
            if not editor:
                self.open_path(edit_file.parent)         # no editor: open the folder holding the working copy
                return
            self.editor_var.set(editor)
            self.save_settings()
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([editor, str(edit_file)], close_fds=True, creationflags=flags)      # not waited for, not stopped when this window closes
        self.write(f"已用编辑器打开校对副本：{edit_file.name}（程序不会监视编辑器，保存后请回来点“登记校对稿”）")

    def do_register_review(self) -> None:
        row = self.selected_row()
        file = None
        if self.session and Path(self.session["edit_file"]).is_file() and messagebox.askyesno("登记校对稿", f"登记校对副本？\n{self.session['edit_file']}\n选“否”可改选其他文件。"):
            file, session_id = self.session["edit_file"], self.session["session_id"]
        else:
            file, session_id = filedialog.askopenfilename(title="选择已保存的中文字幕", filetypes=[("字幕", "*.srt *.ass"), ("全部", "*.*")]), None
        if not file:
            return
        media_id = self.session["media_id"] if session_id else row["media_id"]
        self.run("登记校对稿", ["review", "import", *self.where(), "--media", media_id, "--file", str(file), *(["--session", session_id] if session_id else [])])

    def do_revise_source(self) -> None:
        if self.source_session is not None:            # second click: register the corrected Japanese source
            session = self.source_session

            def registered(result: dict) -> None:
                self.source_session = None
                self.source_revision_id = result["ids"]["revision_id"]
                self.write("新的日文源版本已登记。要据此生成新的机器译文，请明确点“据此生成新机器译文”。")
            self.run("登记原文修订", ["source", "import", *self.where(), "--media", session["media_id"], "--file", session["edit_file"], "--session", session["session_id"]],
                     registered)
            return
        row = self.selected_row()
        source_id = row["source_revision_id"] if row["kind"] == "machine" else row["revision_id"]
        fmt = "srt" if row["kind"] == "machine" else sorted(row["formats"])[0]

        def created(result: dict) -> None:
            self.source_session = {**result["ids"], "media_id": row["media_id"]}
            self.write(f"日文原文校对副本：{result['ids']['edit_file']}\n改好保存后点“登记原文修订”。")
        self.run("修订日文原文", ["review", "create", *self.where(), "--revision", source_id, "--format", fmt], created)

    def do_translate_source(self) -> None:
        config = Path(self.data_root()) / "settings" / "app.json"
        source_id = self.source_revision_id

        def submitted(result: dict) -> None:
            self.source_revision_id = None
            self.tabs.select(self.queue_tab)
            self.queue_tab.start_queue()
        self.run("提交译文任务", ["submit", "--config", str(config), "--mode", "supplied_source", "--project", str(self.project_id), "--source-revision", source_id], submitted)

    def do_export_subtitles(self) -> None:
        row, fmt = self.selected_row(), self.format_var.get()
        destination = self.new_destination("字幕")
        if destination is None:
            return
        self.run("导出字幕", ["export", *self.where(), "--revision", row["revision_id"], "--kind", "subtitles", "--destination", str(destination),
                             *(["--format", fmt] if fmt else [])], lambda result: self.write(f"已导出到：{destination}"))

    def ask_video(self) -> tuple[str, str | None] | None:
        dialog = tk.Toplevel(self)
        dialog.title("导出视频")
        kind, audio, answer = tk.StringVar(value="mp4_burn"), tk.StringVar(value="default"), {}
        ttk.Radiobutton(dialog, text="MP4 硬字幕（重新编码画面）", variable=kind, value="mp4_burn").pack(anchor="w", padx=12, pady=(10, 2))
        ttk.Radiobutton(dialog, text="MKV 软字幕（画面和声音原样复制）", variable=kind, value="mkv_soft").pack(anchor="w", padx=12)
        ttk.Radiobutton(dialog, text="使用项目已选音轨", variable=audio, value="default").pack(anchor="w", padx=12, pady=(8, 0))
        ttk.Radiobutton(dialog, text="不要音轨", variable=audio, value="none").pack(anchor="w", padx=12)
        ttk.Button(dialog, text="选择导出位置并开始", command=lambda: (answer.update(kind=kind.get(), audio=audio.get()), dialog.destroy())).pack(pady=10)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        self.wait_window(dialog)
        return (answer["kind"], None if answer["audio"] == "default" else "none") if answer else None

    def do_export_video(self) -> None:
        row, fmt = self.selected_row(), self.format_var.get()
        choice = self.ask_video()
        if choice is None:
            return
        kind, audio = choice
        destination = self.new_destination("成片")
        if destination is None:
            return
        self.run("导出视频", ["export", *self.where(), "--revision", row["revision_id"], "--kind", kind, "--destination", str(destination),
                             *(["--format", fmt] if fmt else []), *(["--audio-stream", audio] if audio else [])],
                 lambda result: self.write(f"已导出到：{destination}"), background=True)

    def ask_attest(self, fmt: str) -> tuple[list[str], str] | None:
        """Every box starts unticked; SRT shows style as not applicable (plan 3.3)."""
        dialog = tk.Toplevel(self)
        dialog.title("标记人工确认")
        ttk.Label(dialog, text=f"只确认你实际检查过的范围（{fmt.upper()} 文件）：").pack(anchor="w", padx=12, pady=(10, 4))
        boxes = {}
        for scope, usable in scope_choices(fmt).items():
            boxes[scope] = tk.BooleanVar(value=False)
            ttk.Checkbutton(dialog, text=SCOPE_TEXT[scope] + ("" if usable else "（不适用）"), variable=boxes[scope], state=tk.NORMAL if usable else tk.DISABLED).pack(anchor="w", padx=20)
        note = tk.StringVar()
        ttk.Entry(dialog, textvariable=note, width=40).pack(padx=12, pady=6)
        answer = {}
        ttk.Button(dialog, text="确认", command=lambda: (answer.update(scopes=[s for s, v in boxes.items() if v.get()], note=note.get()), dialog.destroy())).pack(pady=(0, 10))
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        self.wait_window(dialog)
        return (answer["scopes"], answer["note"]) if answer and answer["scopes"] else None

    def do_attest(self) -> None:
        row, fmt = self.selected_row(), self.format_var.get()
        answer = self.ask_attest(fmt)
        if answer is None:
            return
        scopes, note = answer
        self.run("标记人工确认", ["review", "attest", *self.where(), "--revision", row["revision_id"], "--scopes", ",".join(scopes), "--format", fmt,
                               *(["--note", note] if note else [])])

    def do_migrate(self) -> None:
        job = filedialog.askdirectory(title="选择旧任务目录（含 task.json）；旧目录只读")
        if job:
            self.run("导入旧任务", ["migrate", "--data-root", str(self.data_root()), "--legacy-job", job], background=True)

    def _choose_root(self) -> None:
        chosen = filedialog.askdirectory(title="选择 v2 数据根")
        if chosen:
            self.data_root_var.set(chosen)
            self.refresh()

    def _choose_editor(self) -> None:
        chosen = filedialog.askopenfilename(title="选择字幕编辑器", filetypes=[("程序", "*.exe"), ("全部", "*.*")])
        if chosen:
            self.editor_var.set(chosen)
            self.save_settings()
