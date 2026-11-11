#!/usr/bin/env python3
"""Windows desktop front end for the subtitle roasting pipeline."""

from __future__ import annotations

import ctypes
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from diagnostics import create_diagnostic_bundle
from dictionary_editor import (
    load_glossary_dictionary,
    load_txt_dictionary,
    update_dictionary_row,
    write_glossary_dictionary,
    write_txt_dictionary,
)
from task_state import TaskState, scan_unfinished
from subtitle_axis_align import FALLBACK_CHOICES, normalize_fallback
from tool_discovery import (
    apply_discovery,
    discover_magia,
    discover_voice,
    infer_magia_root,
    infer_voice_root,
)


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = project_root()
CONFIG_PATH = ROOT / "config" / "roast_pipeline.local.json"
INBOX_RUNNER = ROOT / "tools" / "roast_inbox.py"
PIPELINE = ROOT / "tools" / "roast_pipeline.py"
AUTORUN_REQUEST = ROOT / "config" / "autorun.once.json"

WINDOW_BG = "#F0F0F0"
HEADER_BG = "#DCEBF7"
HEADER_LINE = "#9FBED3"
STATUS_BG = "#E9EEF2"
TEXT = "#1E1E1E"
MUTED = "#5E6A73"
BLUE = "#1C5D8C"
GOOD = "#147A3D"
WARN = "#9A5B00"

ALIGNMENT_FALLBACK_LABELS = {
    "不补全（推荐）": "none",
    "缺失时使用日文": "jp",
    "缺失时使用精准轴文字": "timeline",
}


def alignment_fallback_label(value: object) -> str:
    try:
        normalized = normalize_fallback(value)
    except ValueError:
        normalized = "none"
    return next(
        label for label, fallback in ALIGNMENT_FALLBACK_LABELS.items() if fallback == normalized
    )


def alignment_fallback_value(label: object) -> str:
    value = ALIGNMENT_FALLBACK_LABELS.get(str(label), "none")
    return value if value in FALLBACK_CHOICES else "none"


def load_json(path: Path, fallback: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return fallback


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def resolve_app_path(value: object, fallback: Path | None = None) -> Path:
    text = str(value or "").strip()
    if not text:
        return fallback if fallback is not None else ROOT / "__not_configured__"
    path = Path(text).expanduser()
    return path if path.is_absolute() else ROOT / path


def path_exists(value: object) -> bool:
    if not value:
        return False
    try:
        return resolve_app_path(value).exists()
    except OSError:
        return False


def hidden_process_kwargs(new_process_group: bool = False) -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    flags = subprocess.CREATE_NO_WINDOW
    if new_process_group:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return {"creationflags": flags, "startupinfo": startupinfo}


def _windows_process_tree_pids(root_pid: int) -> list[int]:
    """Return a deepest-first snapshot of a Windows process tree."""
    if os.name != "nt":
        return [root_pid]

    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        return [root_pid]

    children: dict[int, list[int]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            pid = int(entry.th32ProcessID)
            parent_pid = int(entry.th32ParentProcessID)
            children.setdefault(parent_pid, []).append(pid)
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)

    root_first: list[int] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        root_first.append(pid)
        pending.extend(children.get(pid, ()))
    return list(reversed(root_first))


def _windows_force_terminate_pids(pids: list[int]) -> None:
    """Terminate captured PIDs directly when taskkill leaves descendants alive."""
    if os.name != "nt":
        return

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_handles = []
    for pid in dict.fromkeys(pids):
        if pid <= 0 or pid == os.getpid():
            continue
        handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
        if not handle:
            continue
        kernel32.TerminateProcess(handle, 1)
        process_handles.append(handle)
    for handle in process_handles:
        kernel32.WaitForSingleObject(handle, 5000)
        kernel32.CloseHandle(handle)


def format_seconds(value: float | None) -> str:
    if value is None or value < 0:
        return "--"
    seconds = int(round(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def safe_folder_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return cleaned or "video"


def terminate_process_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        tree_pids = _windows_process_tree_pids(process.pid)
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
                **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        tree_pids.extend(_windows_process_tree_pids(process.pid))
        _windows_force_terminate_pids(tree_pids)
        if completed is None or completed.returncode != 0:
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def friendly_failure(last_line: str, code: int) -> str:
    lowered = last_line.lower()
    labels = (
        (("download",), "视频下载失败"),
        (("video conversion", "media"), "视频兼容转换失败"),
        (("extract_audio", "extract audio"), "音频提取失败"),
        (("axis", "magia"), "精准字幕轴生成失败"),
        (("recognize_jp", "whisper"), "日文识别失败"),
        (("translate_zh", "sakura"), "机器翻译失败"),
        (("align",), "字幕合轴失败"),
        (("jianying", "剪映"), "剪映自动导入失败"),
    )
    for needles, label in labels:
        if any(needle in lowered for needle in needles):
            return label
    return f"后台步骤退出（代码 {code}）"


STAGE_LABELS = {
    "INPUT": "输入与媒体",
    "TIMELINE": "精准字幕轴",
    "ASR": "日文识别",
    "SEGMENTATION": "画面与语音分段",
    "TRANSLATION": "机器翻译",
    "FINAL_OUTPUT": "字幕合轴",
    "NLE_EXPORT": "剪映导出",
}

STAGE_STATUS_LABELS = {
    "PENDING": "未开始",
    "RUNNING": "进行中",
    "DONE": "已完成",
    "SKIPPED": "已跳过",
    "FAILED": "失败",
    "INTERRUPTED": "已中断",
    "CANCELLED": "已取消",
    "UNKNOWN": "未知",
}

ERROR_KIND_LABELS = {
    "external_termination": "进程被外部终止",
    "unclean_shutdown": "上次运行未正常结束",
    "validation_failed": "产物校验失败",
    "validation_error": "产物校验异常",
    "command_error": "外部命令失败",
    "timeout": "步骤超时",
    "input_error": "输入无效",
    "interrupted": "任务被中断",
    "cancelled": "任务已取消",
    "internal_error": "内部错误",
    "download_error": "视频下载失败",
    "media_error": "媒体转换失败",
    "unknown_error": "未知错误",
}


def stage_error_text(stage: dict) -> str:
    error = stage.get("error") if isinstance(stage.get("error"), dict) else {}
    parts = []
    kind = str(error.get("kind") or "").strip()
    message = re.sub(r"\s+", " ", str(error.get("message") or "")).strip()
    component = str(error.get("component") or "").strip()
    exit_code = error.get("exit_code")
    if component:
        parts.append(component)
    if message:
        parts.append(message)
    elif kind:
        parts.append(ERROR_KIND_LABELS.get(kind, kind))
    if exit_code is not None:
        parts.append(f"退出码 {exit_code}")
    return "：".join(parts)


def build_task_detail_payload(task: dict) -> dict:
    """Extract a display-ready summary from a task.json dict."""
    last_error = task.get("last_error") if isinstance(task.get("last_error"), dict) else {}
    stage_rows = []
    for name, stage in task.get("stages", {}).items():
        if not isinstance(stage, dict):
            continue
        outputs = stage.get("outputs") if isinstance(stage.get("outputs"), dict) else {}
        existing_outputs = sorted(str(value) for value in outputs.values() if path_exists(value))
        stage_rows.append({
            "name": name,
            "label": STAGE_LABELS.get(name, name),
            "status": str(stage.get("status", "UNKNOWN")),
            "status_label": STAGE_STATUS_LABELS.get(str(stage.get("status")), str(stage.get("status"))),
            "attempt": int(stage.get("attempt", 0) or 0),
            "started_at": str(stage.get("started_at") or ""),
            "finished_at": str(stage.get("finished_at") or ""),
            "error_text": stage_error_text(stage),
            "outputs": existing_outputs,
        })
    return {
        "task_id": str(task.get("task_id") or ""),
        "display_name": str(task.get("display_name") or ""),
        "job_dir": str(task.get("job_dir") or ""),
        "status": str(task.get("status") or "UNKNOWN"),
        "status_label": STAGE_STATUS_LABELS.get(str(task.get("status")), str(task.get("status"))),
        "current_stage": str(task.get("current_stage") or ""),
        "current_stage_label": STAGE_LABELS.get(str(task.get("current_stage")), str(task.get("current_stage") or "")),
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or ""),
        "last_error_text": stage_error_text({"error": last_error}),
        "stages": stage_rows,
    }


def task_failure_detail(job_dir: Path, last_line: str, code: int) -> str:
    task = load_json(job_dir / "task.json", {})
    error = task.get("last_error") if isinstance(task.get("last_error"), dict) else {}
    failed_stage = ""
    for name, stage in task.get("stages", {}).items():
        if isinstance(stage, dict) and stage.get("status") in {"FAILED", "INTERRUPTED", "CANCELLED"}:
            failed_stage = name
    stage_label = STAGE_LABELS.get(failed_stage, failed_stage)
    component = str(error.get("component") or "").strip()
    message = re.sub(r"\s+", " ", str(error.get("message") or "")).strip()
    kind = str(error.get("kind") or "").strip()
    parts = [part for part in (stage_label, component) if part]
    location = " / ".join(parts)
    if message:
        detail = f"{location}：{message}" if location else message
        if kind == "external_termination":
            detail = f"{detail}（进程被外部终止）"
        return detail
    return friendly_failure(last_line, code)


def mark_task_interrupted(job_dir: Path, message: str, *, cancelled: bool) -> None:
    path = job_dir / "task.json"
    if not path.is_file():
        return
    try:
        data = load_json(path, {})
        state = TaskState(path, data)
        state.interrupt(message=message, cancelled=cancelled)
    except Exception:
        return


def consume_autorun_request() -> tuple[list[str], str, str]:
    if not AUTORUN_REQUEST.is_file():
        return [], "", ""
    request = load_json(AUTORUN_REQUEST, {})
    sources = [str(value).strip() for value in request.get("sources", []) if str(value).strip()]
    source = str(request.get("source", "")).strip()
    if source and not sources:
        sources = [source]
    job_name = str(request.get("job_name", "")).strip()
    output_dir = str(request.get("output_dir", "")).strip()
    claimed = AUTORUN_REQUEST.with_name(f"autorun.started.{datetime.now():%Y%m%d_%H%M%S}.json")
    try:
        AUTORUN_REQUEST.replace(claimed)
    except OSError:
        return [], "", ""
    return sources, job_name, output_dir


def nested(data: dict, *keys: str, default=None):
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


class AutoRoastApp(tk.Tk):
    def __init__(self, autorun_sources: list[str] | None = None, autorun_job_name: str = "", autorun_output_dir: str = "") -> None:
        super().__init__()
        self.title("字幕烤肉助手")
        window_width, window_height = 1160, 800
        window_x = max(0, (self.winfo_screenwidth() - window_width) // 2)
        window_y = max(0, (self.winfo_screenheight() - window_height) // 2 - 20)
        self.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
        self.minsize(1020, 720)
        self.configure(bg=WINDOW_BG)

        self.config_data = load_json(CONFIG_PATH, {})
        self.messages: queue.Queue[object] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.latest_job: Path | None = None
        self.queue_items: list[dict] = []
        self.queue_serial = 0
        self.batch_running = False
        self.stop_requested = threading.Event()
        self.skip_requested = threading.Event()
        self.current_item_id = ""
        self.current_item_progress = 0.0
        self.item_started_at = 0.0
        self.batch_started_at = 0.0
        self.completed_items = 0
        self.partial_items = 0
        self.skipped_items = 0
        self.failed_items = 0

        self.source_var = tk.StringVar()
        self.job_name_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=autorun_output_dir or str(self.config_data.get("work_dir", ROOT / "work")))
        self.state_var = tk.StringVar(value="就绪")
        self.stage_var = tk.StringVar(value="添加一个或多个视频，调整顺序后开始制作。")
        self.progress_detail_var = tk.StringVar(value="总进度 0%  |  预计剩余 --")
        self.status_detail_var = tk.StringVar(value="等待新任务")

        self._create_setting_vars()
        self._configure_style()
        self._build_menu()
        self._build_ui()
        self._detect_external_tools(notify=False)
        self._refresh_tool_status()
        self._refresh_latest_job()
        self._refresh_unfinished_status()
        self.after(120, self._drain_messages)
        self.after(1000, self._update_eta_clock)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if autorun_sources:
            for source in autorun_sources:
                self._add_source(source)
            self.job_name_var.set(autorun_job_name)
            self.after(700, self._start)

    def _create_setting_vars(self) -> None:
        cfg = self.config_data
        proxy = str(cfg.get("proxy", ""))
        download = cfg.get("download_settings", {})
        media = cfg.get("media_compatibility", {})
        batch = cfg.get("batch_settings", {})
        recognition = cfg.get("recognition", {})
        axis = cfg.get("axis_settings", {})
        translation = cfg.get("translation", {})
        alignment = cfg.get("alignment", {})
        forced = cfg.get("forced_alignment", {})
        jianying = cfg.get("jianying_ui", {})
        local = cfg.get("local", {})
        resources = cfg.get("resources", {})
        external_tools = cfg.get("external_tools", {})
        voice_native = external_tools.get("voice_native", {}) if isinstance(external_tools, dict) else {}

        self.proxy_enabled_var = tk.BooleanVar(value=bool(proxy))
        self.proxy_var = tk.StringVar(value=proxy)
        self.quality_var = tk.StringVar(value=str(download.get("quality", "最高画质 MP4")))
        self.audio_rate_var = tk.StringVar(value=str(download.get("audio_rate", 16000)))
        self.keep_source_var = tk.BooleanVar(value=bool(download.get("keep_source", True)))
        self.overwrite_var = tk.BooleanVar(value=bool(download.get("overwrite", False)))
        self.cookie_browser_var = tk.StringVar(value=str(download.get("cookies_from_browser", "不使用")))
        self.youtube_client_var = tk.StringVar(value=str(download.get("youtube_client", "默认")))
        self.js_runtime_var = tk.StringVar(value=str(download.get("js_runtime", r"C:\Program Files\Adobe\Adobe Creative Cloud Experience\libs\node.exe")))
        self.auto_convert_var = tk.BooleanVar(value=bool(media.get("auto_convert", True)))
        self.convert_preset_var = tk.StringVar(value=str(media.get("preset", "fast")))
        self.convert_crf_var = tk.IntVar(value=int(media.get("crf", 18)))
        self.continue_on_error_var = tk.BooleanVar(value=bool(batch.get("continue_on_error", True)))
        self.inactivity_timeout_var = tk.IntVar(value=int(batch.get("inactivity_timeout_minutes", 0)))

        self.magia_root_var = tk.StringVar(value=infer_magia_root(cfg))
        self.voice_root_var = tk.StringVar(value=infer_voice_root(cfg))
        self.magia_detection_var = tk.StringVar(value="尚未检测")
        self.voice_detection_var = tk.StringVar(value="尚未检测")
        self.discovery_results: dict[str, dict] = {}

        self.recognition_enabled_var = tk.BooleanVar(value=bool(recognition.get("enabled", True)))
        self.recognition_engine_var = tk.StringVar(value=str(recognition.get("engine", "VoiceTransl Whisper")))
        self.whisper_exe_var = tk.StringVar(value=str(recognition.get("executable", "")))
        self.whisper_model_var = tk.StringVar(value=str(recognition.get("model", "")))
        self.language_var = tk.StringVar(value=str(recognition.get("language", "ja")))
        self.vad_var = tk.BooleanVar(value=bool(recognition.get("vad", True)))
        self.segment_enabled_var = tk.BooleanVar(value=bool(recognition.get("segment_enabled", False)))
        self.segment_minutes_var = tk.IntVar(value=int(recognition.get("segment_minutes", 10)))

        self.forced_alignment_enabled_var = tk.BooleanVar(value=bool(forced.get("enabled", False)))
        self.forced_alignment_python_var = tk.StringVar(value=str(forced.get("python", "")))
        self.forced_alignment_model_var = tk.StringVar(value=str(forced.get("model", "")))
        self.forced_alignment_manifest_var = tk.StringVar(value=str(forced.get("model_manifest", "")))
        self.axis_enabled_var = tk.BooleanVar(value=bool(axis.get("enabled", True)))
        self.axis_ocr_var = tk.BooleanVar(value=bool(axis.get("ocr", True)))
        self.word_timestamps_var = tk.BooleanVar(value=bool(recognition.get("word_timestamps", False)))
        timing_labels = {"mixed": "画面与语音融合", "screen": "画面台词边界", "speech": "语音字词边界"}
        self.timing_mode_var = tk.StringVar(value=timing_labels.get(alignment.get("timing_mode", "mixed"), "画面与语音融合"))
        self.axis_exe_var = tk.StringVar(value=str(axis.get("executable", "")))
        self.axis_strategy_var = tk.StringVar(value=str(axis.get("strategy", "dtd")))
        self.axis_preset_var = tk.StringVar(value=str(axis.get("preset", "default")))
        self.axis_engine_var = tk.StringVar(value=str(axis.get("engine", "speculative")))
        self.axis_max_resolution_var = tk.IntVar(value=int(axis.get("max_resolution", 0)))
        region = axis.get("dialog_rect", [0.0, 1.0, 0.0, 1.0])
        if not isinstance(region, list) or len(region) != 4:
            region = [0.0, 1.0, 0.0, 1.0]
        self.region_left_var = tk.DoubleVar(value=float(region[0]))
        self.region_right_var = tk.DoubleVar(value=float(region[1]))
        self.region_top_var = tk.DoubleVar(value=float(region[2]))
        self.region_bottom_var = tk.DoubleVar(value=float(region[3]))

        self.translation_enabled_var = tk.BooleanVar(value=bool(translation.get("enabled", True)))
        self.translation_engine_var = tk.StringVar(value=str(translation.get("engine", "Sakura 本地模型")))
        self.translate_exe_var = tk.StringVar(value=str(translation.get("executable", "")))
        self.sakura_server_var = tk.StringVar(value=str(translation.get("server_executable", "")))
        self.sakura_model_var = tk.StringVar(value=str(translation.get("model", "")))
        self.sakura_endpoint_var = tk.StringVar(value=str(translation.get("endpoint", "http://127.0.0.1:8989")))
        self.gpu_layers_var = tk.IntVar(value=int(translation.get("gpu_layers", 100)))
        self.translation_context_var = tk.IntVar(value=int(translation.get("context_size", 16384)))
        self.translation_workers_var = tk.IntVar(value=int(translation.get("workers", 1)))
        self.translation_batch_size_var = tk.IntVar(value=int(translation.get("batch_size", 10)))
        self.prompt_path_var = tk.StringVar(value=str(resources.get("translation_prompt", ROOT / "config" / "translation_style_prompt.txt")))
        self.glossary_path_var = tk.StringVar(value=str(resources.get("translation_glossary", ROOT / "config" / "translation_glossary.json")))
        self.use_native_dictionaries_var = tk.BooleanVar(value=bool(translation.get("use_native_dictionaries", True)))
        self.native_pre_dictionary_var = tk.StringVar(value=str(translation.get("native_pre_dictionary") or voice_native.get("pre_dictionary", "")))
        self.native_gpt_dictionary_var = tk.StringVar(value=str(translation.get("native_gpt_dictionary") or voice_native.get("gpt_dictionary", "")))
        self.native_post_dictionary_var = tk.StringVar(value=str(translation.get("native_post_dictionary") or voice_native.get("post_dictionary", "")))
        self.native_guideline_var = tk.StringVar(value=str(translation.get("native_guideline") or voice_native.get("basic_guideline", "")))
        self.native_project_config_var = tk.StringVar(value=str(translation.get("native_project_config") or voice_native.get("project_config", "")))
        self.native_prompt_mode_var = tk.StringVar(value=str(translation.get("native_prompt_mode", "不使用")))

        self.tolerance_var = tk.IntVar(value=int(alignment.get("tolerance_ms", 1200)))
        self.fallback_var = tk.StringVar(value=alignment_fallback_label(alignment.get("fallback", "none")))
        self.mark_low_var = tk.BooleanVar(value=bool(alignment.get("mark_low", False)))
        self.keep_japanese_var = tk.BooleanVar(value=bool(alignment.get("keep_japanese", False)))

        self.jianying_enabled_var = tk.BooleanVar(value=bool(jianying.get("enabled", True)))
        self.create_draft_var = tk.BooleanVar(value=bool(jianying.get("create_new_draft", True)))
        self.open_draft_var = tk.BooleanVar(value=bool(jianying.get("open_after_complete", True)))
        self.jianying_exe_var = tk.StringVar(value=str(local.get("jianying_exe", "")))
        self.draft_root_var = tk.StringVar(value=str(local.get("draft_root", "")))

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        available = style.theme_names()
        style.theme_use("vista" if "vista" in available else "clam")
        default_font = ("Segoe UI", 9)
        style.configure(".", font=default_font)
        style.configure("TFrame", background=WINDOW_BG)
        style.configure("Header.TFrame", background=HEADER_BG)
        style.configure("HeaderTitle.TLabel", background=HEADER_BG, foreground=BLUE, font=("Segoe UI", 16, "bold"))
        style.configure("HeaderText.TLabel", background=HEADER_BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=STATUS_BG, foreground=TEXT, padding=(6, 3))
        style.configure("Section.TLabel", foreground=BLUE, font=("Segoe UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 9, "bold"), padding=(16, 7))
        style.configure("TNotebook", background=WINDOW_BG, tabmargins=(2, 4, 2, 0))
        style.configure("TNotebook.Tab", padding=(14, 6))
        style.configure("Treeview", rowheight=23)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="打开任务文件夹", command=self._open_jobs)
        file_menu.add_command(label="打开最新成品", command=self._open_latest)
        file_menu.add_command(label="载入未完成任务", command=self._load_unfinished_tasks)
        file_menu.add_command(label="查看任务详情", command=self._show_task_details)
        file_menu.add_command(label="打开任务日志目录", command=self._open_task_logs)
        file_menu.add_command(label="导出任务诊断包", command=self._export_diagnostics)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menu.add_cascade(label="文件", menu=file_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="保存全部设置", command=self._save_settings)
        tools_menu.add_command(label="重新检测工具", command=self._refresh_tool_status)
        tools_menu.add_separator()
        tools_menu.add_command(label="打开剪映", command=self._open_jianying)
        tools_menu.add_command(label="打开配置文件", command=lambda: os.startfile(CONFIG_PATH))
        menu.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="关于字幕烤肉助手", command=self._show_about)
        menu.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menu)

    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 12))
        header.pack(fill=tk.X)
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="字幕烤肉助手", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="自动下载、听写、翻译、精准合轴并生成剪映草稿", style="HeaderText.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(header, textvariable=self.state_var, style="HeaderText.TLabel", font=("Segoe UI", 10, "bold")).grid(row=0, column=1, rowspan=2, sticky="e", padx=(20, 4))
        tk.Frame(self, height=1, bg=HEADER_LINE).pack(fill=tk.X)

        body = ttk.Frame(self, padding=(10, 8, 10, 6))
        body.pack(fill=tk.BOTH, expand=True)
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.home_tab = ttk.Frame(self.notebook, padding=10)
        self.tools_tab = ttk.Frame(self.notebook, padding=10)
        self.download_tab = ttk.Frame(self.notebook, padding=10)
        self.recognition_tab = ttk.Frame(self.notebook, padding=10)
        self.axis_tab = ttk.Frame(self.notebook, padding=10)
        self.translation_tab = ttk.Frame(self.notebook, padding=10)
        self.output_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.home_tab, text="任务")
        self.notebook.add(self.tools_tab, text="工具目录")
        self.notebook.add(self.download_tab, text="下载与网络")
        self.notebook.add(self.recognition_tab, text="日文识别")
        self.notebook.add(self.axis_tab, text="精准字幕轴")
        self.notebook.add(self.translation_tab, text="机器翻译")
        self.notebook.add(self.output_tab, text="合轴与剪映")
        self.v2_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.v2_tab, text="新版项目与校对")

        self._build_home_tab()
        self._build_tools_tab()
        self._build_download_tab()
        self._build_recognition_tab()
        self._build_axis_tab()
        self._build_translation_tab()
        self._build_output_tab()
        self._build_v2_tab()

        status = ttk.Frame(self, style="Status.TLabel")
        status.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(status, textvariable=self.status_detail_var, style="Status.TLabel").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(status, text=f"工作区: {ROOT}", style="Status.TLabel").pack(side=tk.RIGHT)

    def _build_v2_tab(self) -> None:
        """v2 projects, human revisions and exports (W08). Its settings live in their own file; this window's config is not rewritten."""
        try:
            from upgrade.gui import V2Panel
        except Exception as exc:  # the old window keeps working without the v2 code
            ttk.Label(self.v2_tab, text=f"新版项目面板无法加载：{exc}").pack(anchor="w")
            return
        self.v2_panel = V2Panel(self.v2_tab, settings_path=CONFIG_PATH.parent / "roast_v2_gui.local.json")
        self.v2_panel.pack(fill=tk.BOTH, expand=True)

    def _build_tools_tab(self) -> None:
        tab = self.tools_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        magia = ttk.LabelFrame(tab, text=" MagiaTimeline 安装目录 ", padding=12)
        magia.grid(row=0, column=0, sticky="ew")
        magia.columnconfigure(1, weight=1)
        ttk.Label(magia, text="程序文件夹：").grid(row=0, column=0, sticky="w")
        ttk.Entry(magia, textvariable=self.magia_root_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(
            magia,
            text="选择文件夹...",
            command=lambda: self._browse_tool_root(self.magia_root_var),
        ).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(magia, text="检测", command=lambda: self._detect_external_tools(notify=True)).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(magia, textvariable=self.magia_detection_var, foreground=MUTED).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(7, 0)
        )

        voice = ttk.LabelFrame(tab, text=" VoiceTransl 安装目录 ", padding=12)
        voice.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        voice.columnconfigure(1, weight=1)
        ttk.Label(voice, text="程序文件夹：").grid(row=0, column=0, sticky="w")
        ttk.Entry(voice, textvariable=self.voice_root_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(
            voice,
            text="选择文件夹...",
            command=lambda: self._browse_tool_root(self.voice_root_var),
        ).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(voice, text="检测", command=lambda: self._detect_external_tools(notify=True)).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(voice, textvariable=self.voice_detection_var, foreground=MUTED).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(7, 0)
        )

        detected = ttk.LabelFrame(tab, text=" 已识别组件 ", padding=8)
        detected.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        detected.columnconfigure(0, weight=1)
        detected.rowconfigure(0, weight=1)
        self.discovery_tree = ttk.Treeview(
            detected,
            columns=("group", "component", "status", "path"),
            show="headings",
            height=12,
        )
        for key, label in (("group", "软件"), ("component", "组件"), ("status", "状态"), ("path", "识别路径")):
            self.discovery_tree.heading(key, text=label)
        self.discovery_tree.column("group", width=110, anchor="w", stretch=False)
        self.discovery_tree.column("component", width=180, anchor="w", stretch=False)
        self.discovery_tree.column("status", width=80, anchor="center", stretch=False)
        self.discovery_tree.column("path", width=650, anchor="w")
        self.discovery_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(detected, orient=tk.VERTICAL, command=self.discovery_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(detected, orient=tk.HORIZONTAL, command=self.discovery_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.discovery_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.discovery_tree.tag_configure("good", foreground=GOOD)
        self.discovery_tree.tag_configure("warn", foreground=WARN)
        self._tab_footer(tab)

    def _build_home_tab(self) -> None:
        tab = self.home_tab
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(2, weight=1)

        source = ttk.LabelFrame(tab, text=" 待处理视频 ", padding=9)
        source.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 8))
        source.columnconfigure(0, weight=1)
        source.rowconfigure(4, weight=1)
        ttk.Label(source, text="视频链接或本地文件：").grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 4))
        self.source_entry = ttk.Entry(source, textvariable=self.source_var)
        self.source_entry.grid(row=1, column=0, sticky="ew", ipady=2)
        self.source_entry.bind("<Return>", lambda _event: self._add_from_entry())
        ttk.Button(source, text="粘贴", command=self._paste).grid(row=1, column=1, padx=(6, 0))
        ttk.Button(source, text="加入队列", command=self._add_from_entry).grid(row=1, column=2, padx=(6, 0))
        ttk.Button(source, text="添加视频...", command=self._browse_source).grid(row=1, column=3, padx=(6, 0))

        ttk.Label(source, text="成品存放位置：").grid(row=2, column=0, columnspan=5, sticky="w", pady=(7, 3))
        ttk.Entry(source, textvariable=self.output_dir_var).grid(row=3, column=0, columnspan=3, sticky="ew", ipady=2)
        ttk.Button(source, text="浏览...", command=self._browse_output_dir).grid(row=3, column=3, padx=(6, 0))

        queue_frame = ttk.Frame(source)
        queue_frame.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(8, 0))
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(0, weight=1)
        self.queue_tree = ttk.Treeview(queue_frame, columns=("order", "name", "duration", "status", "progress", "eta", "detail"), show="headings", height=5, selectmode="extended")
        for key, label in (("order", "顺序"), ("name", "视频"), ("duration", "时长"), ("status", "状态"), ("progress", "进度"), ("eta", "预计剩余"), ("detail", "情况说明")):
            self.queue_tree.heading(key, text=label)
        self.queue_tree.column("order", width=48, anchor="center", stretch=False)
        self.queue_tree.column("name", width=280, anchor="w")
        self.queue_tree.column("duration", width=72, anchor="center", stretch=False)
        self.queue_tree.column("status", width=105, anchor="center", stretch=False)
        self.queue_tree.column("progress", width=66, anchor="center", stretch=False)
        self.queue_tree.column("eta", width=90, anchor="center", stretch=False)
        self.queue_tree.column("detail", width=190, anchor="w")
        self.queue_tree.grid(row=0, column=0, sticky="nsew")
        queue_scroll = ttk.Scrollbar(queue_frame, orient=tk.VERTICAL, command=self.queue_tree.yview)
        queue_scroll.grid(row=0, column=1, sticky="ns")
        self.queue_tree.configure(yscrollcommand=queue_scroll.set)
        self.queue_tree.bind("<Double-1>", lambda _event: self._show_task_details())

        queue_actions = ttk.Frame(source)
        queue_actions.grid(row=4, column=4, sticky="ns", padx=(8, 0), pady=(8, 0))
        ttk.Button(queue_actions, text="上移", width=9, command=lambda: self._move_selected(-1)).pack(fill=tk.X)
        ttk.Button(queue_actions, text="下移", width=9, command=lambda: self._move_selected(1)).pack(fill=tk.X, pady=(4, 0))
        ttk.Button(queue_actions, text="移除", width=9, command=self._remove_selected).pack(fill=tk.X, pady=(12, 0))
        ttk.Button(queue_actions, text="清空", width=9, command=self._clear_queue).pack(fill=tk.X, pady=(4, 0))

        bottom = ttk.Frame(source)
        bottom.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="单任务文件夹名（可选）：").grid(row=0, column=0, sticky="w")
        ttk.Entry(bottom, textvariable=self.job_name_var, width=30).grid(row=0, column=1, sticky="w", padx=(5, 0))
        self.start_button = ttk.Button(bottom, text="按顺序开始制作", style="Primary.TButton", command=self._start)
        self.start_button.grid(row=0, column=2, sticky="e", padx=(12, 0))

        progress_box = ttk.LabelFrame(tab, text=" 当前进度 ", padding=10)
        progress_box.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 8))
        progress_box.columnconfigure(0, weight=1)
        ttk.Label(progress_box, textvariable=self.stage_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(progress_box, mode="determinate", maximum=100, value=0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(7, 4))
        ttk.Label(progress_box, textvariable=self.progress_detail_var, foreground=MUTED).grid(row=2, column=0, sticky="w", pady=(0, 7))
        actions = ttk.Frame(progress_box)
        actions.grid(row=3, column=0, sticky="w")
        self.stop_button = ttk.Button(actions, text="停止任务", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT)
        self.skip_button = ttk.Button(actions, text="跳过当前", command=self._skip_current, state=tk.DISABLED)
        self.skip_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="打开成品", command=self._open_latest).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="打开剪映", command=self._open_jianying).pack(side=tk.LEFT, padx=(6, 0))
        self.resume_button = ttk.Button(actions, text="载入未完成", command=self._load_unfinished_tasks)
        self.resume_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="任务详情", command=self._show_task_details).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="打开日志", command=self._open_task_logs).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="导出诊断", command=self._export_diagnostics).pack(side=tk.LEFT, padx=(6, 0))

        tools = ttk.LabelFrame(tab, text=" 工具状态 ", padding=8)
        tools.grid(row=1, column=1, rowspan=2, sticky="nsew", padx=(4, 0), pady=(0, 8))
        tools.columnconfigure(0, weight=1)
        tools.rowconfigure(0, weight=1)
        self.tool_tree = ttk.Treeview(tools, columns=("tool", "status"), show="headings", height=9)
        self.tool_tree.heading("tool", text="步骤")
        self.tool_tree.heading("status", text="状态")
        self.tool_tree.column("tool", width=160, anchor="w")
        self.tool_tree.column("status", width=90, anchor="center")
        self.tool_tree.grid(row=0, column=0, sticky="nsew")
        ttk.Button(tools, text="重新检测", command=self._refresh_tool_status).grid(row=1, column=0, sticky="e", pady=(7, 0))

        log_frame = ttk.LabelFrame(tab, text=" 运行记录 ", padding=6)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 4))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", state=tk.DISABLED, relief=tk.SUNKEN, borderwidth=1, bg="white", fg=TEXT, font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

    def _build_download_tab(self) -> None:
        tab = self.download_tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        network = ttk.LabelFrame(tab, text=" 网络与代理 ", padding=12)
        network.grid(row=0, column=0, sticky="new", padx=(0, 5))
        network.columnconfigure(1, weight=1)
        ttk.Checkbutton(network, text="使用代理", variable=self.proxy_enabled_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(network, text="代理地址：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(network, textvariable=self.proxy_var).grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ttk.Label(network, text="示例：http://127.0.0.1:7890", foreground=MUTED).grid(row=2, column=1, sticky="w", pady=(3, 0))

        download = ttk.LabelFrame(tab, text=" 下载与媒体 ", padding=12)
        download.grid(row=0, column=1, sticky="new", padx=(5, 0))
        download.columnconfigure(1, weight=1)
        ttk.Label(download, text="视频画质：").grid(row=0, column=0, sticky="w")
        ttk.Combobox(download, textvariable=self.quality_var, state="readonly", values=("最高画质 MP4", "最高 1080p", "最高 720p", "优先兼容 H.264"), width=22).grid(row=0, column=1, sticky="ew")
        ttk.Label(download, text="识别采样率：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(download, textvariable=self.audio_rate_var, state="readonly", values=("16000", "24000", "48000"), width=22).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(download, text="将本地视频复制到任务目录", variable=self.keep_source_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(download, text="同名任务允许覆盖中间文件", variable=self.overwrite_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Checkbutton(download, text="不兼容时自动转为 H.264 MP4", variable=self.auto_convert_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(download, text="转码速度：").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(download, textvariable=self.convert_preset_var, state="readonly", values=("veryfast", "fast", "medium", "slow"), width=12).grid(row=5, column=1, sticky="w", pady=(8, 0))
        ttk.Label(download, text="转码质量 CRF：").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(download, textvariable=self.convert_crf_var, width=8).grid(row=6, column=1, sticky="w", pady=(8, 0))

        youtube = ttk.LabelFrame(tab, text=" YouTube 访问兼容 ", padding=12)
        youtube.grid(row=1, column=0, columnspan=2, sticky="new", pady=(10, 0))
        youtube.columnconfigure(1, weight=1)
        ttk.Label(youtube, text="浏览器登录信息：").grid(row=0, column=0, sticky="w")
        ttk.Combobox(youtube, textvariable=self.cookie_browser_var, state="readonly", values=("不使用", "Edge", "Chrome", "Firefox"), width=20).grid(row=0, column=1, sticky="w")
        ttk.Label(youtube, text="客户端模式：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(youtube, textvariable=self.youtube_client_var, state="readonly", values=("默认", "Android VR", "TV"), width=20).grid(row=1, column=1, sticky="w", pady=(8, 0))
        self._path_row(youtube, 2, "JavaScript 运行时：", self.js_runtime_var, "file")
        ttk.Label(youtube, text="遇到“确认不是机器人”时，再选择你已登录 YouTube 的浏览器。", foreground=MUTED).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._tab_footer(tab)

    def _build_recognition_tab(self) -> None:
        tab = self.recognition_tab
        tab.columnconfigure(0, weight=1)
        engine = ttk.LabelFrame(tab, text=" 日文听写 ", padding=12)
        engine.grid(row=0, column=0, sticky="new")
        engine.columnconfigure(1, weight=1)
        ttk.Checkbutton(engine, text="启用日文识别", variable=self.recognition_enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self._labeled_combo(engine, 1, "识别引擎：", self.recognition_engine_var, ("VoiceTransl Whisper", "VoiceTransl Faster Whisper"))
        self._path_row(engine, 2, "执行程序：", self.whisper_exe_var, "file")
        self._path_row(engine, 3, "模型文件：", self.whisper_model_var, "file")
        self._labeled_combo(engine, 4, "听写语言：", self.language_var, ("ja", "auto", "zh", "en"))
        options = ttk.Frame(engine)
        options.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(options, text="启用 VAD 静音检测", variable=self.vad_var).pack(side=tk.LEFT)
        ttk.Label(engine, text="大模型识别较慢但准确；赛马娘人名仍建议最后人工复核。", foreground=MUTED).grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self._tab_footer(tab)

    def _build_axis_tab(self) -> None:
        tab = self.axis_tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        basic = ttk.LabelFrame(tab, text=" MagiaTimeline ", padding=12)
        basic.grid(row=0, column=0, sticky="new", padx=(0, 5))
        basic.columnconfigure(1, weight=1)
        ttk.Checkbutton(basic, text="启用画面精准轴", variable=self.axis_enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self._path_row(basic, 1, "执行程序：", self.axis_exe_var, "file")
        self._labeled_combo(basic, 2, "检测策略：", self.axis_strategy_var, ("dtd", "bcs", "otl", "mr", "mr-s0", "md", "bdr"))
        self._labeled_combo(basic, 3, "参数预设：", self.axis_preset_var, ("default", "narrow"))
        self._labeled_combo(basic, 4, "扫描引擎：", self.axis_engine_var, ("speculative", "framewise"))
        ttk.Label(basic, text="分析分辨率上限（0=原始）：").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(basic, textvariable=self.axis_max_resolution_var, width=10).grid(row=5, column=1, sticky="w", pady=(8, 0))

        ttk.Checkbutton(basic, text="识别画面台词内容", variable=self.axis_ocr_var).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._labeled_combo(basic, 7, "时间边界：", self.timing_mode_var, ("画面与语音融合", "画面台词边界", "语音字词边界"))
        ttk.Checkbutton(basic, text="提取语音字词时间", variable=self.word_timestamps_var).grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

        region = ttk.LabelFrame(tab, text=" 字幕检测区域（0.00 - 1.00） ", padding=12)
        region.grid(row=0, column=1, sticky="new", padx=(5, 0))
        region.columnconfigure(1, weight=1)
        labels = (("左边界：", self.region_left_var), ("右边界：", self.region_right_var), ("上边界：", self.region_top_var), ("下边界：", self.region_bottom_var))
        for row, (label, variable) in enumerate(labels):
            ttk.Label(region, text=label).grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 8, 0))
            ttk.Spinbox(region, from_=0.0, to=1.0, increment=0.01, textvariable=variable, width=10).grid(row=row, column=1, sticky="w", pady=(0 if row == 0 else 8, 0))
        ttk.Label(region, text="默认检测全画面台词。\n可按素材选择字幕所在区域，避免漏掉上方或中部文字。", foreground=MUTED, justify=tk.LEFT).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Checkbutton(region, text="使用本地 Qwen 辅助字词对齐", variable=self.forced_alignment_enabled_var).grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self._path_row(region, 6, "对齐运行环境：", self.forced_alignment_python_var, "file")
        self._path_row(region, 7, "对齐模型：", self.forced_alignment_model_var, "dir")
        self._path_row(region, 8, "模型校验清单：", self.forced_alignment_manifest_var, "file")
        self._tab_footer(tab)

    def _build_translation_tab(self) -> None:
        tab = self.translation_tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        engine = ttk.LabelFrame(tab, text=" Sakura 本地翻译 ", padding=12)
        engine.grid(row=0, column=0, sticky="new", padx=(0, 5))
        engine.columnconfigure(1, weight=1)
        ttk.Checkbutton(engine, text="启用机器直译", variable=self.translation_enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        self._labeled_combo(engine, 1, "翻译引擎：", self.translation_engine_var, ("Sakura 本地模型", "不翻译，仅生成日文"))
        self._path_row(engine, 2, "翻译程序：", self.translate_exe_var, "file")
        self._path_row(engine, 3, "模型服务：", self.sakura_server_var, "file")
        self._path_row(engine, 4, "模型文件：", self.sakura_model_var, "file")
        ttk.Label(engine, text="服务地址：").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(engine, textvariable=self.sakura_endpoint_var).grid(row=5, column=1, sticky="ew", pady=(8, 0))
        controls = ttk.Frame(engine)
        controls.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(controls, text="GPU 层数：").pack(side=tk.LEFT)
        ttk.Entry(controls, textvariable=self.gpu_layers_var, width=7).pack(side=tk.LEFT, padx=(4, 18))
        ttk.Label(controls, text="每批字幕：").pack(side=tk.LEFT)
        ttk.Entry(controls, textvariable=self.translation_batch_size_var, width=5).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(controls, text="上下文：").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Entry(controls, textvariable=self.translation_context_var, width=7).pack(side=tk.LEFT, padx=(4, 0))
        self._path_row(engine, 7, "翻译规则：", self.prompt_path_var, "file")
        self._path_row(engine, 8, "术语表：", self.glossary_path_var, "file")
        ttk.Button(engine, text="编辑", command=lambda: self._edit_dictionary(
            self.glossary_path_var,
            "glossary",
            "术语表",
            "人名/专有名词的固定译法，翻译时模型必须遵守（优先级最高）。\n"
            "已内置赛马娘常用词条（アルトレ、目白アルダン 等），可在这里增删改。\n"
            "每行：原文（日文）→ 译文（中文）。",
        )).grid(row=8, column=3, padx=(4, 0), pady=(8, 0))
        ttk.Label(engine, text="翻译保留完整信息、吐槽节奏、未完句与专有名词。", foreground=MUTED).grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 0))

        native = ttk.LabelFrame(tab, text=" VoiceTransl 规则链 ", padding=12)
        native.grid(row=0, column=1, sticky="new", padx=(5, 0))
        native.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            native,
            text="启用前置、GPT 与后置词典",
            variable=self.use_native_dictionaries_var,
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._labeled_combo(
            native,
            1,
            "原生翻译规范：",
            self.native_prompt_mode_var,
            ("不使用", "追加", "覆盖"),
        )
        self._path_row(native, 2, "前置词典：", self.native_pre_dictionary_var, "file")
        ttk.Button(native, text="编辑", command=lambda: self._edit_dictionary(
            self.native_pre_dictionary_var,
            "pre",
            "前置词典",
            "翻译开始前，先把日文原文里的词替换成词典里的译文。\n"
            "适合处理机器识别常见错误（如把「です」识别成「デス」）。\n"
            "格式：原文<Tab>译文，每行一条。",
        )).grid(row=2, column=3, padx=(4, 0), pady=(8, 0))
        self._path_row(native, 3, "GPT 词典：", self.native_gpt_dictionary_var, "file")
        ttk.Button(native, text="编辑", command=lambda: self._edit_dictionary(
            self.native_gpt_dictionary_var,
            "gpt",
            "GPT 词典",
            "翻译提示词里附带的词条对照，让模型翻译时遵守。\n"
            "适合固定人名、术语的译法（如「ウマ娘」→「赛马娘」）。\n"
            "格式：原文<Tab>译文，每行一条。",
        )).grid(row=3, column=3, padx=(4, 0), pady=(8, 0))
        self._path_row(native, 4, "后置词典：", self.native_post_dictionary_var, "file")
        ttk.Button(native, text="编辑", command=lambda: self._edit_dictionary(
            self.native_post_dictionary_var,
            "post",
            "后置词典",
            "翻译完成后，把译文里的词再替换一遍。\n"
            "适合统一译法（如把所有「马娘」统一成「赛马娘」）。\n"
            "格式：原文<Tab>译文，每行一条。",
        )).grid(row=4, column=3, padx=(4, 0), pady=(8, 0))
        self._path_row(native, 5, "翻译规范：", self.native_guideline_var, "file")
        self._path_row(native, 6, "项目配置：", self.native_project_config_var, "file")
        ttk.Label(
            native,
            text="自定义术语表最后生效；条件/场景词条因缺少相邻台词上下文会跳过并记录警告。",
            foreground=MUTED,
            wraplength=470,
            justify=tk.LEFT,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self._tab_footer(tab)

    def _build_output_tab(self) -> None:
        tab = self.output_tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        align = ttk.LabelFrame(tab, text=" 字幕合轴 ", padding=12)
        align.grid(row=0, column=0, sticky="new", padx=(0, 5))
        ttk.Label(align, text="匹配容差（毫秒）：").grid(row=0, column=0, sticky="w")
        ttk.Entry(align, textvariable=self.tolerance_var, width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(align, text="无匹配时：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            align,
            textvariable=self.fallback_var,
            state="readonly",
            values=tuple(ALIGNMENT_FALLBACK_LABELS),
            width=22,
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(align, text="在最终字幕标记低置信度条目", variable=self.mark_low_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(align, text="最终 ASS 同时保留日文行", variable=self.keep_japanese_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))

        jianying = ttk.LabelFrame(tab, text=" 剪映草稿 ", padding=12)
        jianying.grid(row=0, column=1, sticky="new", padx=(5, 0))
        jianying.columnconfigure(1, weight=1)
        ttk.Checkbutton(jianying, text="自动导入剪映", variable=self.jianying_enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(jianying, text="每次创建新草稿", variable=self.create_draft_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(jianying, text="完成后打开剪映", variable=self.open_draft_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._path_row(jianying, 3, "剪映程序：", self.jianying_exe_var, "file")
        self._path_row(jianying, 4, "草稿目录：", self.draft_root_var, "dir")

        recovery = ttk.LabelFrame(tab, text=" 批量容错 ", padding=12)
        recovery.grid(row=1, column=0, columnspan=2, sticky="new", pady=(10, 0))
        ttk.Checkbutton(recovery, text="单个稿件失败后自动跳过并继续下一条", variable=self.continue_on_error_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(recovery, text="连续无新日志超时（分钟，0 为关闭）：").grid(row=1, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(recovery, textvariable=self.inactivity_timeout_var, width=8).grid(row=1, column=1, sticky="w", pady=(9, 0))
        ttk.Label(recovery, text="超时项目会终止后台进程并标记为“超时已跳过”；已生成的中间文件会保留。", foreground=MUTED).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._tab_footer(tab)

    def _tab_footer(self, tab: ttk.Frame) -> None:
        footer = ttk.Frame(tab)
        footer.grid(row=99, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Separator(footer).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 9))
        ttk.Label(footer, text="修改后点击保存；开始任务时也会自动保存。", foreground=MUTED).grid(row=1, column=0, sticky="w")
        ttk.Button(footer, text="保存设置", command=self._save_settings).grid(row=1, column=1, sticky="e")

    def _labeled_combo(self, parent, row: int, label: str, variable, values: tuple[str, ...]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(parent, textvariable=variable, values=values, state="readonly").grid(row=row, column=1, columnspan=2, sticky="ew", pady=(8, 0))

    def _path_row(self, parent, row: int, label: str, variable: tk.StringVar, kind: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(parent, text="浏览...", command=lambda: self._browse_setting_path(variable, kind)).grid(row=row, column=2, padx=(6, 0), pady=(8, 0))

    def _browse_setting_path(self, variable: tk.StringVar, kind: str) -> None:
        current = Path(variable.get()).parent if variable.get() else ROOT
        if kind == "dir":
            selected = filedialog.askdirectory(initialdir=current)
        else:
            selected = filedialog.askopenfilename(initialdir=current)
        if selected:
            variable.set(selected)

    def _browse_tool_root(self, variable: tk.StringVar) -> None:
        value = variable.get().strip()
        initial = Path(value) if value and Path(value).is_dir() else ROOT
        selected = filedialog.askdirectory(initialdir=initial)
        if selected:
            variable.set(selected)
            self._detect_external_tools(notify=False)

    def _detect_external_tools(self, notify: bool = False) -> bool:
        results: dict[str, dict] = {}
        errors: list[str] = []
        magia_value = self.magia_root_var.get().strip()
        voice_value = self.voice_root_var.get().strip()

        if magia_value:
            try:
                magia = discover_magia(magia_value)
                results["magia"] = magia
                self.magia_root_var.set(magia["root"])
                executable = magia["components"]["executable"]["path"]
                if executable:
                    self.axis_exe_var.set(executable)
                if magia["valid"]:
                    self.magia_detection_var.set("时间轴组件完整，可以使用")
                else:
                    self.magia_detection_var.set("缺少：" + "、".join(magia["missing"]))
            except Exception as exc:
                self.magia_detection_var.set(f"检测失败：{exc}")
                errors.append(f"MagiaTimeline：{exc}")
        else:
            self.magia_detection_var.set("尚未选择；可继续使用下方高级路径")

        if voice_value:
            try:
                voice = discover_voice(voice_value, project_root=ROOT)
                results["voice"] = voice
                self.voice_root_var.set(voice["root"])
                component = voice["components"]
                runtime_overrides = voice.get("runtime_overrides", {})
                whisper_overrides = runtime_overrides.get("whisper", {})
                llama_overrides = runtime_overrides.get("llama", {})
                if voice["capabilities"]["recognition"]:
                    self.whisper_exe_var.set(
                        whisper_overrides.get("whisper_executable")
                        or component["whisper_executable"]["path"]
                    )
                    self.whisper_model_var.set(
                        whisper_overrides.get("whisper_model")
                        or component["whisper_model"]["path"]
                    )
                if component["translate_executable"]["path"]:
                    self.translate_exe_var.set(component["translate_executable"]["path"])
                if voice["capabilities"]["translation"]:
                    self.sakura_server_var.set(
                        llama_overrides.get("llama_server")
                        or component["llama_server"]["path"]
                    )
                    self.sakura_model_var.set(
                        llama_overrides.get("sakura_model")
                        or component["sakura_model"]["path"]
                    )
                previous_native = nested(
                    self.config_data,
                    "external_tools",
                    "voice_native",
                    default={},
                )
                if not isinstance(previous_native, dict):
                    previous_native = {}
                native_fields = (
                    ("pre_dictionary", self.native_pre_dictionary_var),
                    ("gpt_dictionary", self.native_gpt_dictionary_var),
                    ("post_dictionary", self.native_post_dictionary_var),
                    ("basic_guideline", self.native_guideline_var),
                    ("project_config", self.native_project_config_var),
                )
                for key, variable in native_fields:
                    detected_path = component[key]["path"]
                    current_path = variable.get().strip()
                    previous_auto_path = str(previous_native.get(key, "")).strip()
                    if detected_path and (not current_path or current_path == previous_auto_path):
                        variable.set(detected_path)
                capabilities = voice["capabilities"]
                available = []
                if capabilities["recognition"]:
                    available.append("日文听写")
                if capabilities["translation"]:
                    available.append("Sakura 翻译")
                if capabilities["native_translation"]:
                    available.append("原生词典链")
                if voice["valid"]:
                    fallback = []
                    if whisper_overrides:
                        fallback.append("Whisper 已切换 ASCII 运行时")
                    if llama_overrides:
                        fallback.append("Sakura 已切换 ASCII 运行时")
                    suffix = "；" + "、".join(fallback) if fallback else ""
                    self.voice_detection_var.set("、".join(available) + " 可用" + suffix)
                else:
                    self.voice_detection_var.set("缺少：" + "、".join(voice["missing"]))
            except Exception as exc:
                self.voice_detection_var.set(f"检测失败：{exc}")
                errors.append(f"VoiceTransl：{exc}")
        else:
            self.voice_detection_var.set("尚未选择；可继续使用下方高级路径")

        self.discovery_results = results
        if hasattr(self, "discovery_tree"):
            for item in self.discovery_tree.get_children():
                self.discovery_tree.delete(item)
            for key, result in results.items():
                group = "MagiaTimeline" if key == "magia" else "VoiceTransl"
                runtime_overrides = result.get("runtime_overrides", {})
                effective_paths = {}
                if isinstance(runtime_overrides, dict):
                    for override_group in runtime_overrides.values():
                        if isinstance(override_group, dict):
                            effective_paths.update(override_group)
                for component_key, component in result["components"].items():
                    exists = bool(component["exists"])
                    status = "可用" if exists else ("缺失" if component["required"] else "未找到")
                    shown_path = effective_paths.get(component_key) or component["path"]
                    if component_key in effective_paths:
                        status = "兼容运行时"
                    self.discovery_tree.insert(
                        "",
                        tk.END,
                        values=(group, component["label"], status, shown_path),
                        tags=("good" if exists else "warn",),
                    )
        self._refresh_tool_status()
        all_valid = not errors and all(result.get("valid") for result in results.values())
        if notify:
            if errors:
                messagebox.showerror("工具检测失败", "\n".join(errors))
            elif results and all_valid:
                messagebox.showinfo("工具检测", "已识别并填入可用组件路径。")
            elif results:
                missing = []
                for result in results.values():
                    missing.extend(result.get("missing", []))
                messagebox.showwarning("工具不完整", "缺少：" + "、".join(missing))
            else:
                messagebox.showwarning("尚未选择", "请先选择 MagiaTimeline 或 VoiceTransl 的程序文件夹。")
        return all_valid

    def _browse_source(self) -> None:
        selected = filedialog.askopenfilenames(filetypes=(("视频文件", "*.mp4 *.mkv *.webm *.mov *.avi"), ("所有文件", "*.*")))
        for path in selected:
            self._add_source(path)

    def _browse_output_dir(self) -> None:
        initial = str(resolve_app_path(self.output_dir_var.get(), ROOT / "work"))
        selected = filedialog.askdirectory(initialdir=initial)
        if selected:
            self.output_dir_var.set(selected)

    def _add_from_entry(self) -> None:
        source = self.source_var.get().strip().strip('"')
        if self._add_source(source):
            self.source_var.set("")

    def _probe_duration(self, source: str) -> float | None:
        if re.match(r"^https?://", source, flags=re.IGNORECASE):
            return None
        cfg = load_json(CONFIG_PATH, self.config_data)
        ffmpeg = str(nested(cfg, "tools", "ffmpeg", default=""))
        if not ffmpeg or not Path(ffmpeg).is_file():
            return None
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", source],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_process_kwargs(),
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stdout or "")
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def _add_source(self, source: str) -> bool:
        if self.batch_running:
            return False
        source = source.strip().strip('"')
        is_url = bool(re.match(r"^https?://", source, flags=re.IGNORECASE))
        if not source or (not is_url and not Path(source).is_file()):
            if source:
                messagebox.showwarning("输入无效", f"没有找到这个视频：\n{source}")
            return False
        normalized = source.lower() if is_url else str(Path(source).resolve()).lower()
        if any(item["normalized"] == normalized for item in self.queue_items):
            return True
        duration = self._probe_duration(source)
        estimate = max(210.0, (duration or 30.0) * 5.0 + 120.0)
        self.queue_serial += 1
        item_id = f"video_{self.queue_serial}"
        name = source if is_url else Path(source).name
        self.queue_items.append({
            "id": item_id,
            "source": source,
            "normalized": normalized,
            "is_url": is_url,
            "name": name,
            "duration": duration,
            "estimate": estimate,
            "status": "等待中",
            "progress": 0.0,
            "eta": estimate,
            "detail": "等待处理",
            "job_dir": "",
            "resume_job_name": "",
            "resume_source": {},
        })
        self._refresh_queue_tree()
        self.status_detail_var.set(f"队列中有 {len(self.queue_items)} 个视频")
        return True

    def _refresh_queue_tree(self) -> None:
        if not hasattr(self, "queue_tree"):
            return
        selected = set(self.queue_tree.selection())
        for child in self.queue_tree.get_children():
            self.queue_tree.delete(child)
        for index, item in enumerate(self.queue_items, start=1):
            self.queue_tree.insert("", tk.END, iid=item["id"], values=(
                index,
                item["name"],
                format_seconds(item["duration"]),
                item["status"],
                f"{int(item['progress'])}%",
                format_seconds(item["eta"] if item["status"] in {"等待中", "处理中"} else 0),
                item.get("detail", ""),
            ))
        for item_id in selected:
            if self.queue_tree.exists(item_id):
                self.queue_tree.selection_add(item_id)

    def _move_selected(self, direction: int) -> None:
        if self.batch_running:
            return
        selected = list(self.queue_tree.selection())
        if not selected:
            return
        indices = sorted((next(i for i, item in enumerate(self.queue_items) if item["id"] == item_id) for item_id in selected), reverse=direction > 0)
        for index in indices:
            target = index + direction
            if 0 <= target < len(self.queue_items):
                self.queue_items[index], self.queue_items[target] = self.queue_items[target], self.queue_items[index]
        self._refresh_queue_tree()

    def _remove_selected(self) -> None:
        if self.batch_running:
            return
        selected = set(self.queue_tree.selection())
        self.queue_items = [item for item in self.queue_items if item["id"] not in selected]
        self._refresh_queue_tree()

    def _clear_queue(self) -> None:
        if self.batch_running:
            return
        self.queue_items.clear()
        self._refresh_queue_tree()

    def _paste(self) -> None:
        try:
            value = self.clipboard_get().strip()
        except tk.TclError:
            value = ""
        if value:
            self.source_var.set(value)
            self.source_entry.icursor(tk.END)

    def _save_settings(self, notify: bool = True) -> bool:
        try:
            self._detect_external_tools(notify=False)
            cfg = load_json(CONFIG_PATH, self.config_data.copy())
            cfg["proxy"] = self.proxy_var.get().strip() if self.proxy_enabled_var.get() else ""
            cfg["download_settings"] = {
                "quality": self.quality_var.get(),
                "audio_rate": int(self.audio_rate_var.get()),
                "keep_source": self.keep_source_var.get(),
                "overwrite": self.overwrite_var.get(),
                "cookies_from_browser": self.cookie_browser_var.get(),
                "youtube_client": self.youtube_client_var.get(),
                "js_runtime": self.js_runtime_var.get().strip(),
            }
            cfg["media_compatibility"] = {
                "auto_convert": self.auto_convert_var.get(),
                "accepted_containers": [".mp4"],
                "accepted_video_codecs": ["h264"],
                "preset": self.convert_preset_var.get(),
                "crf": self.convert_crf_var.get(),
            }
            cfg["batch_settings"] = {
                "continue_on_error": self.continue_on_error_var.get(),
                "inactivity_timeout_minutes": self.inactivity_timeout_var.get(),
            }
            cfg["work_dir"] = self.output_dir_var.get().strip() or str(ROOT / "work")
            cfg["recognition"] = {
                **cfg.get("recognition", {}),
                "word_timestamps": self.word_timestamps_var.get(),
                "enabled": self.recognition_enabled_var.get(),
                "engine": self.recognition_engine_var.get(),
                "executable": self.whisper_exe_var.get().strip(),
                "model": self.whisper_model_var.get().strip(),
                "language": self.language_var.get(),
                "vad": self.vad_var.get(),
                "segment_enabled": False,
                "segment_minutes": 10,
            }
            cfg["axis_settings"] = {
                **cfg.get("axis_settings", {}),
                "ocr": self.axis_ocr_var.get(),
                "enabled": self.axis_enabled_var.get(),
                "executable": self.axis_exe_var.get().strip(),
                "strategy": self.axis_strategy_var.get(),
                "preset": self.axis_preset_var.get(),
                "engine": self.axis_engine_var.get(),
                "max_resolution": self.axis_max_resolution_var.get(),
                "dialog_rect": [self.region_left_var.get(), self.region_right_var.get(), self.region_top_var.get(), self.region_bottom_var.get()],
            }
            cfg["translation"] = {
                **cfg.get("translation", {}),
                "enabled": self.translation_enabled_var.get()
                and self.translation_engine_var.get() != "不翻译，仅生成日文",
                "engine": self.translation_engine_var.get(),
                "executable": self.translate_exe_var.get().strip(),
                "server_executable": self.sakura_server_var.get().strip(),
                "model": self.sakura_model_var.get().strip(),
                "endpoint": self.sakura_endpoint_var.get().strip(),
                "gpu_layers": self.gpu_layers_var.get(),
                "context_size": self.translation_context_var.get(),
                "workers": 1,
                "batch_size": self.translation_batch_size_var.get(),
                "use_native_dictionaries": self.use_native_dictionaries_var.get(),
                "native_pre_dictionary": self.native_pre_dictionary_var.get().strip(),
                "native_gpt_dictionary": self.native_gpt_dictionary_var.get().strip(),
                "native_post_dictionary": self.native_post_dictionary_var.get().strip(),
                "native_guideline": self.native_guideline_var.get().strip(),
                "native_project_config": self.native_project_config_var.get().strip(),
                "native_prompt_mode": self.native_prompt_mode_var.get(),
            }
            cfg.setdefault("resources", {})["translation_prompt"] = self.prompt_path_var.get().strip()
            cfg["resources"]["translation_glossary"] = self.glossary_path_var.get().strip()
            cfg["alignment"] = {
                **cfg.get("alignment", {}),
                "timing_mode": {"画面与语音融合": "mixed", "画面台词边界": "screen", "语音字词边界": "speech"}[self.timing_mode_var.get()],
                "tolerance_ms": self.tolerance_var.get(),
                "fallback": alignment_fallback_value(self.fallback_var.get()),
                "mark_low": self.mark_low_var.get(),
                "keep_japanese": self.keep_japanese_var.get(),
            }
            cfg["forced_alignment"] = {
                **cfg.get("forced_alignment", {}),
                "enabled": self.forced_alignment_enabled_var.get(),
                "python": self.forced_alignment_python_var.get().strip(),
                "model": self.forced_alignment_model_var.get().strip(),
                "model_manifest": self.forced_alignment_manifest_var.get().strip(),
            }
            cfg["jianying_ui"] = {
                "enabled": self.jianying_enabled_var.get(),
                "create_new_draft": self.create_draft_var.get(),
                "open_after_complete": self.open_draft_var.get(),
            }
            cfg.setdefault("local", {})["jianying_exe"] = self.jianying_exe_var.get().strip()
            cfg["local"]["draft_root"] = self.draft_root_var.get().strip()
            cfg["external_tools"] = {
                **cfg.get("external_tools", {}),
                "magia_root": self.magia_root_var.get().strip(),
                "voice_root": self.voice_root_var.get().strip(),
                "auto_detect": True,
            }
            apply_discovery(
                cfg,
                self.discovery_results.get("magia"),
                self.discovery_results.get("voice"),
                project_root=ROOT,
            )
            commands = cfg.setdefault("commands", {})
            commands["axis"] = (
                '"{python}" "{root}/tools/magia_timeline_adapter.py" '
                '--config "{config_path}" --video "{video}" --output "{timeline}"'
            )
            commands["recognize_jp"] = (
                '"{python}" "{root}/tools/voicetransl_adapter.py" recognize '
                '--config "{config_path}" --audio "{audio}" --output "{jp}"'
            )
            commands["translate_zh"] = (
                '"{python}" "{root}/tools/voicetransl_adapter.py" translate '
                '--config "{config_path}" --input "{jp}" --output "{zh}"'
            )
            write_json(CONFIG_PATH, cfg)
            self.config_data = cfg
            self._refresh_tool_status()
            self.status_detail_var.set("设置已保存")
            if notify:
                messagebox.showinfo("设置", "全部设置已经保存。")
            return True
        except Exception as exc:
            messagebox.showerror("无法保存设置", str(exc))
            return False

    def _tool_items(self) -> list[tuple[str, bool]]:
        cfg = load_json(CONFIG_PATH, self.config_data)
        tools = cfg.get("tools", {})
        magia_result = self.discovery_results.get("magia", {})
        voice_result = self.discovery_results.get("voice", {})
        axis_executable = Path(self.axis_exe_var.get()) if self.axis_exe_var.get().strip() else Path("__missing__")
        magia_ready = bool(magia_result.get("valid")) or all(
            path.exists()
            for path in (
                axis_executable,
                axis_executable.parent / "config.yml",
                axis_executable.parent / "template.asst",
                axis_executable.parent / "PaddleOCRModels",
            )
        )
        recognition_ready = bool(nested(voice_result, "capabilities", "recognition", default=False)) or (
            path_exists(self.whisper_exe_var.get()) and path_exists(self.whisper_model_var.get())
        )
        translation_ready = bool(nested(voice_result, "capabilities", "translation", default=False)) or (
            path_exists(self.sakura_server_var.get()) and path_exists(self.sakura_model_var.get())
        )
        native_dictionary_ready = not self.use_native_dictionaries_var.get() or all(
            path_exists(variable.get())
            for variable in (
                self.native_pre_dictionary_var,
                self.native_gpt_dictionary_var,
                self.native_post_dictionary_var,
            )
        )
        native_guideline_ready = self.native_prompt_mode_var.get() == "不使用" or path_exists(
            self.native_guideline_var.get()
        )
        return [
            ("视频下载", path_exists(tools.get("yt_dlp"))),
            ("音频处理", path_exists(tools.get("ffmpeg"))),
            ("日文识别", not self.recognition_enabled_var.get() or recognition_ready),
            ("精准字幕轴", not self.axis_enabled_var.get() or magia_ready),
            ("Sakura 翻译", not self.translation_enabled_var.get() or translation_ready),
            ("VoiceTransl 规则链", not self.translation_enabled_var.get() or (native_dictionary_ready and native_guideline_ready)),
            ("字幕合轴", (ROOT / "tools" / "subtitle_axis_align.py").exists()),
            ("剪映草稿", not self.jianying_enabled_var.get() or path_exists(self.jianying_exe_var.get())),
        ]

    def _refresh_tool_status(self) -> None:
        if not hasattr(self, "tool_tree"):
            return
        for item in self.tool_tree.get_children():
            self.tool_tree.delete(item)
        items = self._tool_items()
        for label, ok in items:
            self.tool_tree.insert("", tk.END, values=(label, "可用" if ok else "未配置"), tags=("good" if ok else "warn",))
        self.tool_tree.tag_configure("good", foreground=GOOD)
        self.tool_tree.tag_configure("warn", foreground=WARN)
        missing = sum(1 for _, ok in items if not ok)
        self.status_detail_var.set("全部工具可用" if not missing else f"还有 {missing} 项需要配置")

    def _append(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _start(self) -> None:
        if self.source_var.get().strip():
            self._add_from_entry()
        if not self.queue_items:
            messagebox.showwarning("没有任务", "请先添加一个或多个视频。")
            return
        if self.batch_running:
            return
        output_dir = resolve_app_path(self.output_dir_var.get(), ROOT / "work")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法使用存放位置", str(exc))
            return
        if not self._save_settings(notify=False):
            return

        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)
        self.progress.configure(value=0)
        self._set_running(True)
        self.state_var.set("正在制作")
        self.stage_var.set(f"正在准备 {len(self.queue_items)} 个视频...")
        self.progress_detail_var.set("总进度 0%  |  正在估算剩余时间")
        self.notebook.select(self.home_tab)
        self.stop_requested.clear()
        self.skip_requested.clear()
        self.batch_running = True
        self.batch_started_at = time.monotonic()
        self.completed_items = 0
        self.partial_items = 0
        self.skipped_items = 0
        self.failed_items = 0
        for item in self.queue_items:
            item.update({"status": "等待中", "progress": 0.0, "eta": item["estimate"], "detail": "等待处理", "job_dir": ""})
        self._refresh_queue_tree()
        batch = [item.copy() for item in self.queue_items]
        settings = {
            "output_dir": str(output_dir),
            "single_job_name": self.job_name_var.get().strip() if len(batch) == 1 else "",
            "copy_media": self.keep_source_var.get(),
            "overwrite": self.overwrite_var.get(),
            "continue_on_error": self.continue_on_error_var.get(),
            "inactivity_timeout_seconds": self.inactivity_timeout_var.get() * 60,
        }
        threading.Thread(target=self._worker, args=(batch, settings), daemon=True).start()

    def _worker(self, batch: list[dict], settings: dict) -> None:
        cfg = load_json(CONFIG_PATH, {})
        python_exe = str(nested(cfg, "tools", "python", default="") or sys.executable)
        work_dir = Path(settings["output_dir"])
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})

        abort_batch = False
        for index, item in enumerate(batch):
            if self.stop_requested.is_set():
                break
            self.skip_requested.clear()
            source = item["source"]
            resume_job_name = str(item.get("resume_job_name", "")).strip()
            resume_source = item.get("resume_source", {}) if isinstance(item.get("resume_source"), dict) else {}
            if resume_job_name:
                job_name = safe_folder_name(resume_job_name)
            elif settings["single_job_name"]:
                job_name = safe_folder_name(settings["single_job_name"])
            elif item["is_url"]:
                job_name = f"video_{index + 1:02d}_{datetime.now():%Y%m%d_%H%M%S}"
            else:
                job_name = safe_folder_name(Path(source).stem)
            if (work_dir / job_name).exists() and not settings["overwrite"] and not resume_job_name:
                job_name = f"{job_name}_{datetime.now():%Y%m%d_%H%M%S}"

            command = [python_exe, str(PIPELINE)]
            if item["is_url"]:
                command.append(source)
            else:
                command.extend(["local-file", "--video-file", source])
            command.extend([
                "--config", str(CONFIG_PATH),
                "--job-name", job_name,
                "--work-dir", str(work_dir),
                "--wait-missing",
            ])
            for key, flag in (
                ("audio_file", "--audio-file"),
                ("timeline_file", "--timeline-file"),
                ("jp_file", "--jp-file"),
                ("zh_file", "--zh-file"),
            ):
                value = str(resume_source.get(key, "")).strip()
                if value:
                    command.extend([flag, value])
            if resume_source.get("skip_audio"):
                command.append("--skip-audio")
            if resume_source.get("skip_external"):
                command.append("--skip-external")
            if resume_source.get("skip_jianying_package"):
                command.append("--skip-jianying-package")
            if not item["is_url"] and settings["copy_media"]:
                command.append("--copy-media")

            desktop_log_path = work_dir / job_name / "logs" / "desktop.log"
            desktop_log_path.parent.mkdir(parents=True, exist_ok=True)
            job_dir = work_dir / job_name
            self.messages.put(("item_start", item["id"], index, len(batch), job_name, str(job_dir)))
            try:
                log_mode = "a" if desktop_log_path.is_file() and resume_job_name else "w"
                with desktop_log_path.open(log_mode, encoding="utf-8", errors="replace") as desktop_log:
                    if log_mode == "a":
                        desktop_log.write(f"\n===== resumed {datetime.now().isoformat(timespec='seconds')} =====\n")
                        desktop_log.flush()
                    self.process = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=env,
                        **hidden_process_kwargs(new_process_group=True),
                    )
                    assert self.process.stdout is not None
                    output_lines: queue.Queue[object] = queue.Queue()

                    def pump_output() -> None:
                        try:
                            assert self.process is not None and self.process.stdout is not None
                            for output_line in self.process.stdout:
                                output_lines.put(output_line)
                        finally:
                            output_lines.put(None)

                    reader = threading.Thread(target=pump_output, daemon=True)
                    reader.start()
                    last_activity = time.monotonic()
                    last_line = ""
                    skip_kind = ""
                    skip_detail = ""
                    while True:
                        if self.stop_requested.is_set():
                            terminate_process_tree(self.process)
                            break
                        if self.skip_requested.is_set():
                            skip_kind = "手动跳过"
                            skip_detail = "用户跳过了当前稿件"
                            terminate_process_tree(self.process)
                            break
                        try:
                            line = output_lines.get(timeout=0.5)
                        except queue.Empty:
                            if settings["inactivity_timeout_seconds"] > 0 and time.monotonic() - last_activity >= settings["inactivity_timeout_seconds"]:
                                skip_kind = "超时已跳过"
                                skip_detail = f"连续 {settings['inactivity_timeout_seconds'] // 60} 分钟没有新日志"
                                terminate_process_tree(self.process)
                                break
                            if self.process.poll() is not None and not reader.is_alive():
                                break
                            continue
                        if line is None:
                            break
                        last_activity = time.monotonic()
                        last_line = str(line).strip() or last_line
                        self.messages.put(("line", item["id"], str(line)))
                        desktop_log.write(str(line))
                        desktop_log.flush()
                    code = self.process.wait()
                if self.stop_requested.is_set():
                    mark_task_interrupted(job_dir, "User stopped the batch", cancelled=True)
                elif skip_kind:
                    mark_task_interrupted(
                        job_dir,
                        skip_detail,
                        cancelled=skip_kind == "手动跳过",
                    )
                    self.messages.put(("item_skip", item["id"], skip_kind, skip_detail, str(job_dir)))
                elif code == 0:
                    self.messages.put(("item_done", item["id"], str(job_dir)))
                else:
                    detail = task_failure_detail(job_dir, last_line, code)
                    if (job_dir / "deliverables" / "final.ass").is_file():
                        self.messages.put(("item_partial", item["id"], detail, str(job_dir)))
                    elif settings["continue_on_error"]:
                        self.messages.put(("item_skip", item["id"], "失败已跳过", detail, str(job_dir)))
                    else:
                        self.messages.put(("item_fail", item["id"], detail, str(job_dir)))
                        abort_batch = True
            except Exception as exc:
                self.messages.put(("line", item["id"], f"\n启动失败：{exc}\n"))
                if settings["continue_on_error"]:
                    self.messages.put(("item_skip", item["id"], "失败已跳过", str(exc), str(job_dir)))
                else:
                    self.messages.put(("item_fail", item["id"], str(exc), str(job_dir)))
                    abort_batch = True
            finally:
                self.process = None
            if abort_batch:
                break

        if self.stop_requested.is_set():
            self.messages.put(("batch_stopped",))
        elif abort_batch:
            self.messages.put(("batch_aborted",))
        else:
            self.messages.put(("batch_done",))

    def _infer_stage(self, line: str) -> None:
        lowered = line.lower()
        stages = [
            (("[media] converting",), "正在转换兼容视频...", 8),
            (("[run] download", "下载"), "正在下载视频...", 10),
            (("extract_audio", "extract audio"), "正在提取音频...", 16),
            (("[run] axis", "[magia]", "timeline"), "正在生成精准字幕轴...", 24),
            (("recognize_jp", "whisper", "jp_ocr"), "正在识别日文字幕...", 45),
            (("translate_zh", "sakura", "zh_machine"), "正在生成机器直译...", 74),
            (("[run] align", "final.ass"), "正在合成最终字幕...", 88),
            (("jianying", "剪映"), "正在创建剪映草稿...", 94),
        ]
        for needles, label, progress in stages:
            if any(needle in lowered for needle in needles):
                self._set_item_progress(progress, label)
                return

        timestamp = re.search(r"\[?(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)\s*(?:-->|\])", line)
        item = self._item_by_id(self.current_item_id)
        if timestamp and item and item.get("duration"):
            hours, minutes, seconds = timestamp.groups()
            position = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            ratio = min(1.0, position / item["duration"])
            if self.current_item_progress >= 40:
                self._set_item_progress(45 + ratio * 24, "正在识别日文字幕...")
            elif self.current_item_progress >= 20:
                self._set_item_progress(24 + ratio * 18, "正在生成精准字幕轴...")

    def _item_by_id(self, item_id: str) -> dict | None:
        return next((item for item in self.queue_items if item["id"] == item_id), None)

    def _set_item_progress(self, progress: float, label: str | None = None) -> None:
        item = self._item_by_id(self.current_item_id)
        if not item:
            return
        progress = max(item["progress"], min(99.0, float(progress)))
        item["progress"] = progress
        self.current_item_progress = progress
        if label:
            item["detail"] = label.rstrip(".").rstrip("…")
            position = self.queue_items.index(item) + 1
            self.stage_var.set(f"第 {position}/{len(self.queue_items)} 个：{label}  {item['name']}")
        self._refresh_queue_tree()

    def _update_eta_clock(self) -> None:
        if self.batch_running:
            current = self._item_by_id(self.current_item_id)
            current_remaining = 0.0
            if current and current["status"] == "处理中":
                elapsed = max(1.0, time.monotonic() - self.item_started_at)
                fraction = max(0.02, current["progress"] / 100.0)
                current_remaining = max(0.0, elapsed / fraction - elapsed)
                current["eta"] = current_remaining
            pending = sum(item["estimate"] for item in self.queue_items if item["status"] == "等待中")
            total_remaining = current_remaining + pending
            total = max(1, len(self.queue_items))
            finished = self.completed_items + self.partial_items + self.skipped_items + self.failed_items
            overall = (finished + (current["progress"] / 100.0 if current else 0.0)) / total * 100
            self.progress.configure(value=min(100, overall))
            finish_at = datetime.now().timestamp() + total_remaining
            finish_text = datetime.fromtimestamp(finish_at).strftime("%H:%M") if total_remaining else "--"
            self.progress_detail_var.set(f"总进度 {overall:.0f}%  |  预计剩余 {format_seconds(total_remaining)}  |  约 {finish_text} 完成")
            self._refresh_queue_tree()
        self.after(1000, self._update_eta_clock)

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.skip_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.progress.configure(mode="determinate")
        if not running:
            self.progress.stop()

    def _stop(self) -> None:
        if not self.batch_running:
            return
        self.stop_requested.set()
        terminate_process_tree(self.process)
        self.stage_var.set("正在停止任务...")

    def _skip_current(self) -> None:
        if not self.batch_running or not self.current_item_id:
            return
        self.skip_requested.set()
        self.skip_button.configure(state=tk.DISABLED)
        self.stage_var.set("正在跳过当前稿件，马上继续下一条...")

    def _refresh_latest_job(self) -> None:
        output_root = resolve_app_path(self.output_dir_var.get(), ROOT / "work")
        jobs = load_json(output_root / "jobs_index.json", {"jobs": []}).get("jobs", [])
        self.latest_job = None
        if jobs:
            candidate = Path(str(jobs[0].get("job_dir", "")))
            if candidate.exists():
                self.latest_job = candidate
                return
        manifests = sorted(
            output_root.glob("*/job_manifest.json") if output_root.is_dir() else (),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if manifests:
            self.latest_job = manifests[0].parent

    def _unfinished_tasks(self) -> list[dict]:
        root = resolve_app_path(self.output_dir_var.get(), ROOT / "work")
        return [task for task in scan_unfinished(root) if task.get("status") != "CANCELLED"]

    def _refresh_unfinished_status(self) -> None:
        tasks = self._unfinished_tasks()
        if hasattr(self, "resume_button"):
            self.resume_button.configure(
                text=f"载入未完成 ({len(tasks)})" if tasks else "载入未完成",
                state=tk.NORMAL if tasks and not self.batch_running else tk.DISABLED,
            )
        if tasks and not self.batch_running:
            self.status_detail_var.set(f"发现 {len(tasks)} 个未完成任务，可以载入后继续")

    def _load_unfinished_tasks(self) -> None:
        if self.batch_running:
            return
        tasks = self._unfinished_tasks()
        loaded = 0
        unavailable: list[str] = []
        for task_info in reversed(tasks):
            task_path = Path(task_info["task_path"])
            task = load_json(task_path, {})
            source_data = task.get("source", {}) if isinstance(task.get("source"), dict) else {}
            url = str(source_data.get("url", "")).strip()
            video_file = str(source_data.get("video_file", "")).strip()
            if url and re.match(r"^https?://", url, flags=re.IGNORECASE):
                source = url
            elif video_file and Path(video_file).is_file():
                source = video_file
            else:
                unavailable.append(str(task.get("display_name") or task_path.parent.name))
                continue
            normalized = source.lower() if re.match(r"^https?://", source, flags=re.IGNORECASE) else str(Path(source).resolve()).lower()
            item = next((entry for entry in self.queue_items if entry["normalized"] == normalized), None)
            if item is None:
                if not self._add_source(source):
                    continue
                item = self.queue_items[-1]
            item["resume_job_name"] = str(task.get("display_name") or task_path.parent.name)
            item["resume_source"] = source_data
            item["job_dir"] = str(task_path.parent)
            stage_name = str(task_info.get("current_stage") or "")
            if not stage_name:
                stage_name = next(
                    (
                        name
                        for name, stage in task.get("stages", {}).items()
                        if isinstance(stage, dict) and stage.get("status") in {"FAILED", "INTERRUPTED"}
                    ),
                    "",
                )
            item["detail"] = f"继续 {STAGE_LABELS.get(stage_name, task_info.get('status', ''))}"
            loaded += 1
        self._refresh_queue_tree()
        self._refresh_unfinished_status()
        if loaded:
            self.status_detail_var.set(f"已载入 {loaded} 个未完成任务；开始后从有效检查点继续")
            self.notebook.select(self.home_tab)
        elif unavailable:
            messagebox.showwarning(
                "无法载入未完成任务",
                "这些任务的原始视频已移动或不可访问：\n" + "\n".join(unavailable),
            )
        else:
            messagebox.showinfo("未完成任务", "当前存放位置没有可恢复的任务。")

    def _selected_job_dir(self) -> Path | None:
        for item_id in self.queue_tree.selection() if hasattr(self, "queue_tree") else ():
            item = self._item_by_id(item_id)
            if item and item.get("job_dir") and Path(item["job_dir"]).is_dir():
                return Path(item["job_dir"])
        self._refresh_latest_job()
        return self.latest_job if self.latest_job and self.latest_job.is_dir() else None

    def _show_task_details(self) -> None:
        job_dir = self._selected_job_dir()
        if job_dir is None:
            messagebox.showwarning("没有任务详情", "请先选择一个已有任务，或至少运行一次制作。")
            return
        task = load_json(job_dir / "task.json", {})
        if not task:
            messagebox.showwarning("没有任务详情", f"任务目录缺少 task.json：\n{job_dir}")
            return
        payload = build_task_detail_payload(task)

        dialog = tk.Toplevel(self)
        dialog.title("任务详情")
        dialog.transient(self)
        dialog.resizable(True, True)
        dialog.geometry("880x560")
        dialog.configure(bg=WINDOW_BG)

        head = ttk.Frame(dialog, padding=(12, 10))
        head.pack(fill=tk.X)
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=payload["display_name"] or Path(job_dir).name, style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text=f"任务状态：{payload['status_label']}", foreground=GOOD if payload["status"] == "DONE" else WARN, font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky="e")
        info_lines = []
        if payload["current_stage_label"]:
            info_lines.append(f"当前阶段：{payload['current_stage_label']}")
        if payload["last_error_text"]:
            info_lines.append(f"最近错误：{payload['last_error_text']}")
        info_lines.append(f"任务目录：{payload['job_dir']}")
        info_lines.append(f"创建：{payload['created_at']}    更新：{payload['updated_at']}")
        ttk.Label(head, text="\n".join(info_lines), foreground=MUTED, wraplength=830, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        body = ttk.Frame(dialog, padding=(12, 0, 12, 8))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        columns = ("stage", "status", "attempt", "started", "finished", "error", "outputs")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (
            ("stage", "阶段", 120),
            ("status", "状态", 80),
            ("attempt", "尝试", 50),
            ("started", "开始时间", 140),
            ("finished", "结束时间", 140),
            ("error", "错误详情", 240),
            ("outputs", "产出文件", 200),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w")
        tree.column("attempt", anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        for row in payload["stages"]:
            error_text = row["error_text"]
            outputs_text = "\n".join(Path(value).name for value in row["outputs"]) if row["outputs"] else ""
            tree.insert(
                "",
                tk.END,
                values=(row["label"], row["status_label"], row["attempt"], row["started_at"], row["finished_at"], error_text, outputs_text),
            )
        for item in tree.get_children():
            tags = tree.item(item, "tags")
            values = tree.item(item, "values")
            if values[1] == "失败" or values[1] == "已中断":
                tree.item(item, tags=("warn",))
        tree.tag_configure("warn", foreground=WARN)

        actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="打开日志目录", command=lambda: self._open_task_logs(job_dir=job_dir)).pack(side=tk.LEFT)
        ttk.Button(actions, text="打开任务文件夹", command=lambda: self._open_job_dir(job_dir)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="导出诊断包", command=lambda: self._export_diagnostics(job_dir=job_dir)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)
        dialog.update_idletasks()
        if rows:
            tree.selection_set("0")
        dialog.grab_set()
        dialog.focus_set()

    def _open_task_logs(self, job_dir: Path | None = None) -> None:
        if job_dir is None:
            job_dir = self._selected_job_dir()
            if job_dir is None:
                messagebox.showwarning("没有任务日志", "请先选择一个已有任务，或至少运行一次制作。")
                return
        logs = job_dir / "logs"
        target = logs if logs.is_dir() else job_dir
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def _open_job_dir(self, job_dir: Path) -> None:
        target = job_dir if job_dir.is_dir() else resolve_app_path(self.output_dir_var.get(), ROOT / "work")
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def _export_diagnostics(self, job_dir: Path | None = None) -> None:
        if job_dir is None:
            job_dir = self._selected_job_dir()
        if job_dir is None:
            messagebox.showwarning("没有可诊断任务", "请先选择一个已有任务，或至少运行一次制作。")
            return
        try:
            bundle = create_diagnostic_bundle(job_dir)
        except Exception as exc:
            messagebox.showerror("诊断包导出失败", str(exc))
            return
        self.status_detail_var.set(f"诊断包已导出：{bundle.name}")
        messagebox.showinfo(
            "诊断包已导出",
            f"已生成：\n{bundle}\n\n默认不包含视频、音频、字幕文件或字幕正文。",
        )

    def _open_latest(self) -> None:
        self._refresh_latest_job()
        target = (
            self.latest_job / "deliverables"
            if self.latest_job
            else resolve_app_path(self.output_dir_var.get(), ROOT / "work")
        )
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def _open_jobs(self) -> None:
        target = resolve_app_path(self.output_dir_var.get(), ROOT / "work")
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def _open_jianying(self) -> None:
        executable = Path(self.jianying_exe_var.get())
        if executable.exists():
            os.startfile(executable)
        else:
            messagebox.showwarning("未找到剪映", "请在“合轴与剪映”标签页重新选择剪映程序。")

    def _show_about(self) -> None:
        messagebox.showinfo("关于", "字幕烤肉助手\n\n从视频链接生成日文识别、机器直译、精准字幕轴和剪映草稿。\n最后的内容校对由人工完成。")

    def _edit_dictionary(self, variable: tk.StringVar, kind: str, label: str, help_text: str) -> None:
        """Open an in-place editor for a dictionary file (txt or glossary JSON)."""
        path_value = variable.get().strip()
        default_names = {
            "glossary": "translation_glossary.json",
            "pre": "dict_pre.txt",
            "gpt": "dict_gpt.txt",
            "post": "dict_after.txt",
        }
        default_path = ROOT / "config" / default_names.get(kind, "translation_glossary.json")
        path = resolve_app_path(path_value, default_path)
        is_glossary = kind == "glossary" or path.name.lower().endswith(".json")
        is_gpt = kind == "gpt"

        try:
            if is_glossary:
                rows = load_glossary_dictionary(path)
                header = ""
            else:
                rows, header = load_txt_dictionary(path, gpt=is_gpt)
        except Exception as exc:
            messagebox.showerror("无法打开词典", str(exc), parent=self)
            return

        dialog = tk.Toplevel(self)
        dialog.title(f"编辑词典：{label}")
        dialog.transient(self)
        dialog.geometry("760x520")
        dialog.configure(bg=WINDOW_BG)

        head = ttk.Frame(dialog, padding=(12, 10))
        head.pack(fill=tk.X)
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=f"词典：{label}", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            head,
            text=help_text,
            foreground=MUTED,
            wraplength=720,
            justify=tk.LEFT,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        path_label = ttk.Label(head, text=f"文件：{path}", foreground=MUTED)
        path_label.grid(row=2, column=0, sticky="w", pady=(4, 0))

        body = ttk.Frame(dialog, padding=(12, 0, 12, 8))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        columns = ("source", "target")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        tree.heading("source", text="原文（日文）")
        tree.heading("target", text="译文（中文）")
        tree.column("source", width=220, anchor="w")
        tree.column("target", width=420, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        source_var = tk.StringVar()
        target_var = tk.StringVar()
        mode_var = tk.StringVar(value="all")
        note_var = tk.StringVar()
        active_index: int | None = None
        loading_editor = False

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for index, row in enumerate(rows):
                mode = row.get("mode", "all")
                prefix = "^^" if mode == "prefix" else ("1^" if mode == "once" else "")
                source = prefix + row.get("source", "")
                tree.insert("", tk.END, iid=str(index), values=(source, row.get("target", "")))

        refresh()

        def commit_active(*_args) -> None:
            nonlocal active_index
            if loading_editor or active_index is None or active_index >= len(rows):
                return
            update_dictionary_row(
                rows,
                active_index,
                source=source_var.get(),
                target=target_var.get(),
                mode=None if is_glossary else mode_var.get(),
                note=None if is_glossary else note_var.get(),
            )
            mode = rows[active_index].get("mode", "all")
            prefix = "^^" if mode == "prefix" else ("1^" if mode == "once" else "")
            item_id = str(active_index)
            if tree.exists(item_id):
                tree.item(
                    item_id,
                    values=(prefix + rows[active_index].get("source", ""), rows[active_index].get("target", "")),
                )

        def on_select(_event=None) -> None:
            nonlocal active_index, loading_editor
            selection = tree.selection()
            if not selection:
                return
            commit_active()
            index = int(selection[0])
            row = rows[index]
            loading_editor = True
            try:
                active_index = index
                source_var.set(row.get("source", ""))
                target_var.set(row.get("target", ""))
                if not is_glossary:
                    mode_var.set(row.get("mode", "all"))
                    note_var.set(row.get("note", ""))
            finally:
                loading_editor = False

        tree.bind("<<TreeviewSelect>>", on_select)
        source_var.trace_add("write", commit_active)
        target_var.trace_add("write", commit_active)
        mode_var.trace_add("write", commit_active)
        note_var.trace_add("write", commit_active)

        editor = ttk.Frame(dialog, padding=(12, 0, 12, 8))
        editor.pack(fill=tk.X)
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(3, weight=1)
        ttk.Label(editor, text="原文：").grid(row=0, column=0, sticky="w")
        ttk.Entry(editor, textvariable=source_var).grid(row=0, column=1, sticky="ew", padx=(4, 12))
        ttk.Label(editor, text="译文：").grid(row=0, column=2, sticky="w")
        ttk.Entry(editor, textvariable=target_var).grid(row=0, column=3, sticky="ew", padx=(4, 0))
        if not is_glossary:
            ttk.Label(editor, text="模式：").grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Combobox(
                editor,
                textvariable=mode_var,
                state="readonly",
                values=("all", "prefix", "once"),
                width=10,
            ).grid(row=1, column=1, sticky="w", pady=(8, 0))
            ttk.Label(editor, text="备注：").grid(row=1, column=2, sticky="w", pady=(8, 0))
            ttk.Entry(editor, textvariable=note_var).grid(
                row=1,
                column=3,
                sticky="ew",
                padx=(4, 0),
                pady=(8, 0),
            )

        actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="添加词条", command=lambda: _add_row()).pack(side=tk.LEFT)
        ttk.Button(actions, text="删除选中", command=lambda: _delete_row()).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="保存", command=lambda: _save()).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)

        def _add_row() -> None:
            commit_active()
            if is_glossary:
                rows.append({"source": "", "target": "", "advanced": {}})
            else:
                rows.append({"source": "", "target": "", "mode": "all", "note": ""})
            refresh()
            children = tree.get_children()
            if children:
                tree.selection_set(children[-1])
                tree.see(children[-1])

        def _delete_row() -> None:
            nonlocal active_index, loading_editor
            selection = tree.selection()
            if not selection:
                messagebox.showwarning("未选择", "请先选中要删除的词条。", parent=dialog)
                return
            commit_active()
            index = int(selection[0])
            del rows[index]
            active_index = None
            loading_editor = True
            try:
                source_var.set("")
                target_var.set("")
                mode_var.set("all")
                note_var.set("")
            finally:
                loading_editor = False
            refresh()
            if rows:
                tree.selection_set(str(min(index, len(rows) - 1)))

        def _save() -> None:
            try:
                commit_active()
                if is_glossary:
                    write_glossary_dictionary(path, rows)
                else:
                    write_txt_dictionary(path, rows, header, gpt=is_gpt)
                try:
                    variable.set(str(path.relative_to(ROOT)))
                except ValueError:
                    variable.set(str(path))
                self.status_detail_var.set(f"词典已保存：{path}")
                messagebox.showinfo("词典已保存", f"已保存到：\n{path}", parent=dialog)
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)

        dialog.update_idletasks()
        dialog.grab_set()
        dialog.focus_set()

    def _drain_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if not isinstance(message, tuple):
                continue
            kind = message[0]
            if kind == "item_start":
                _, item_id, index, total, job_name, job_dir = message
                self.current_item_id = item_id
                self.current_item_progress = 1.0
                self.item_started_at = time.monotonic()
                item = self._item_by_id(item_id)
                if item:
                    item.update({"status": "处理中", "progress": 1.0, "eta": item["estimate"], "detail": "正在准备", "job_dir": job_dir})
                    self.stage_var.set(f"第 {index + 1}/{total} 个：正在准备 {item['name']}")
                self.skip_button.configure(state=tk.NORMAL)
                self._append(f"\n===== 开始任务：{job_name} =====\n")
                self._refresh_queue_tree()
            elif kind == "line":
                _, item_id, line = message
                if item_id == self.current_item_id:
                    self._append(line)
                    self._infer_stage(line)
            elif kind in {"item_done", "item_partial", "item_skip", "item_fail"}:
                item_id = message[1]
                item = self._item_by_id(item_id)
                if item:
                    if kind == "item_done":
                        job_dir = message[2]
                        quality_path = Path(job_dir) / "deliverables" / "quality_summary.json"
                        quality = {"status": "unknown"}
                        try:
                            if quality_path.is_file():
                                quality = json.loads(quality_path.read_text(encoding="utf-8-sig"))
                                if not isinstance(quality, dict):
                                    raise ValueError("Quality summary must be an object")
                                warning_groups = quality.get("validation_warnings", {})
                                if not isinstance(warning_groups, dict) or any(not isinstance(items, list) for items in warning_groups.values()):
                                    raise ValueError("Quality validation warnings must be lists grouped by artifact")
                        except (OSError, ValueError):
                            quality = {"status": "unknown"}
                        needs_review = quality.get("status") != "ready"
                        detail = "质量证据缺失或无效，需复核" if quality.get("status") not in {"ready", "needs_review"} else (f"分段待复核 {quality.get('segmentation_review', 0)}，合轴待复核 {quality.get('needs_review', 0)}，术语待复核 {quality.get('term_issue_count', 0)}，验证提示 {sum(len(items) for items in quality.get('validation_warnings', {}).values())}，未入轴 {quality.get('unmapped_source_count', 0)}" if needs_review else "全部步骤完成")
                        if quality.get("word_timing_issue_count"):
                            detail += f"，字词待复核 {quality['word_timing_issue_count']}"
                        if quality.get("word_timing_unusable"):
                            detail += "，字词时间坐标需校验"
                        item.update({"status": "完成待复核" if needs_review else "已完成", "progress": 100.0, "eta": 0.0, "detail": detail, "job_dir": job_dir})
                        self.completed_items += 1
                        self.latest_job = Path(job_dir)
                        self._append(f"===== {item['status']}：{item['name']}（{detail}）=====\n")
                    elif kind == "item_partial":
                        _, _, detail, job_dir = message
                        item.update({"status": "部分完成", "progress": 99.0, "eta": 0.0, "detail": detail, "job_dir": job_dir})
                        self.partial_items += 1
                        self.latest_job = Path(job_dir)
                        self._append(f"===== 部分完成：{item['name']}（{detail}）=====\n")
                    elif kind == "item_skip":
                        _, _, status, detail, job_dir = message
                        item.update({"status": status, "eta": 0.0, "detail": detail, "job_dir": job_dir})
                        self.skipped_items += 1
                        self._append(f"===== {status}：{item['name']}（{detail}）=====\n")
                    else:
                        _, _, detail, job_dir = message
                        item.update({"status": "失败", "eta": 0.0, "detail": detail, "job_dir": job_dir})
                        self.failed_items += 1
                        self._append(f"===== 未完成：{item['name']}（{detail}）=====\n")
                self.current_item_id = ""
                self.current_item_progress = 0.0
                self.skip_button.configure(state=tk.DISABLED)
                self._refresh_queue_tree()
            elif kind in {"batch_done", "batch_stopped", "batch_aborted"}:
                self.batch_running = False
                self._set_running(False)
                self._refresh_latest_job()
                if kind == "batch_stopped":
                    for item in self.queue_items:
                        if item["status"] in {"等待中", "处理中"}:
                            item["status"] = "已停止"
                            item["eta"] = 0.0
                    self.state_var.set("已停止")
                    self.stage_var.set("批量任务已经停止。")
                    self.status_detail_var.set("任务已停止")
                elif kind == "batch_aborted":
                    for item in self.queue_items:
                        if item["status"] == "等待中":
                            item.update({"status": "未处理", "eta": 0.0, "detail": "遇错停止已开启"})
                    self.state_var.set("已中断")
                    self.stage_var.set("当前稿件失败，队列已按设置停止。")
                    self.status_detail_var.set("队列因错误中断")
                else:
                    self.progress.configure(value=100)
                    review_count = sum(item['status'] == '完成待复核' for item in self.queue_items)
                    summary = f"完成 {self.completed_items}（待复核 {review_count}），部分完成 {self.partial_items}，跳过 {self.skipped_items}，失败 {self.failed_items}"
                    self.progress_detail_var.set(f"总进度 100%  |  {summary}")
                    if self.partial_items or self.skipped_items or self.failed_items:
                        self.state_var.set("部分完成")
                        self.stage_var.set("队列处理结束；每个稿件的烤制情况见上方列表。")
                        self.status_detail_var.set(summary)
                    else:
                        self.state_var.set("完成待复核" if review_count else "已完成")
                        self.stage_var.set("字幕和复核表已生成；请检查标记的句子。" if review_count else "全部任务处理完成。")
                        self.status_detail_var.set(summary)
                        if self.open_draft_var.get():
                            self.after(300, self._open_jianying)
                self._refresh_queue_tree()
                self._refresh_unfinished_status()
        self.after(120, self._drain_messages)

    def _on_close(self) -> None:
        panel = getattr(self, "v2_panel", None)
        if panel is not None and not panel.confirm_close():   # v2 queue/export running: stop it first or go back
            return
        if self.batch_running:
            if not messagebox.askyesno("任务仍在进行", "关闭软件会停止当前任务，确认关闭吗？"):
                return
            self._stop()
        self.destroy()


def enable_high_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Subtitle roasting desktop assistant")
    parser.add_argument("--source", default="", help="Video URL or local video path")
    parser.add_argument("--job-name", default="", help="Optional output job folder name")
    parser.add_argument("--autostart", action="store_true", help="Start processing after the window opens")
    return parser.parse_args()


if __name__ == "__main__":
    enable_high_dpi()
    args = parse_args()
    sources = [args.source] if args.autostart and args.source else []
    job_name = args.job_name
    output_dir = ""
    if not sources:
        sources, job_name, output_dir = consume_autorun_request()
    AutoRoastApp(sources, job_name, output_dir).mainloop()
