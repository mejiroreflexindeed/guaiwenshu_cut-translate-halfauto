#!/usr/bin/env python3
"""Media probing, the single time axis and derived media (plan 1.1, section 7; work package W03).

Interface (fixed by the plan):
    probe_media(MediaInput, runtime=...)   -> MediaRecord
    prepare_media(MediaPlan, runtime=...)  -> {media_record, video_for_review, audio_for_asr, derivation_refs}
``export_media(ExportRequest)`` is the W07 exit and is intentionally not defined here yet.

Rules implemented here (section 7):
- facts come from ``ffprobe -v error -show_format -show_streams -of json`` plus packet/frame timestamps; nothing is
  inferred from the file suffix and an unknown start time is never treated as 0;
- one time axis: t=0 is the first *presented* frame of the selected video stream,
  ``t_ms = round_half_up(1000 * (pts * time_base - origin_seconds))`` with integers/Fractions only;
- the original file is never modified; derived media are written once into
  ``media/<media_id>/derived/<media_revision>/`` with a manifest and a ``derivation.json``;
- audio and video are never zeroed independently: the audio offset against the video origin is measured and
  the ASR WAV is trimmed/padded so that its sample 0 is video time 0;
- a re-encoded review/recognition video is accepted only after every presented frame maps to the same
  millisecond as in the source (exit code 0 alone is not enough);
- unknown clocks, timestamp discontinuities and HDR/stereo/multi-programme sources stop with
  MEDIA_CLOCK_UNKNOWN / MEDIA_CLOCK_DISCONTINUITY / MEDIA_PROFILE_UNSUPPORTED instead of guessing.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import shutil
import sys
import wave
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from upgrade.contracts import (
    SCHEMA_VERSION, ContractError, load_record, new_id, parse_json_text, require_record, sha256_file, signature, utc_now,
)
from upgrade.process_runner import CancelToken, process_spec, run_process
from upgrade.storage import (
    MANIFEST_NAME, artifact_ref, atomic_write_bytes, atomic_write_text, begin_pending, check_path_components, commit_pending,
    read_manifest, render_json, verify_committed,
)

MAPPING_VERSION = 1
COMPAT_PROFILE_ID = "review_h264_v1"
ASR_SAMPLE_RATE = 16000
FRAME_MAP_TOLERANCE_MS = 1
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
CONTAINER_FORMAT_NAMES = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".webm": "webm"}
DOWNLOAD_FORMATS = {  # legacy "download_settings.quality" values, kept verbatim
    "最高画质 MP4": "bv*+ba/b",
    "最高 1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "最高 720p": "bv*[height<=720]+ba/b[height<=720]",
    "优先兼容 H.264": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]",
}
EventSink = Callable[[dict], None]


@dataclass
class MediaRuntime:
    """Runtime handles injected by the runner (plan 6.8: never part of a JSON request)."""
    project_dir: Path
    ffprobe: dict                       # resolved resource: {"absolute_path", "resource_ref", "manifest"}
    log_dir: Path                       # process logs of probes (inside the stage attempt)
    owner_token: str
    ffmpeg: dict | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    emit: EventSink = lambda event: None
    media_settings: dict = field(default_factory=dict)      # engine_settings["media"]
    download_settings: dict = field(default_factory=dict)   # engine_settings["download"]


# --------------------------------------------------------------------------- exact time arithmetic

def round_half_up(value: Fraction) -> int:
    """Round to the nearest integer, halves towards +infinity (plan 7.2), without floats."""
    return math.floor(value + Fraction(1, 2))


def parse_rational(text: Any) -> Fraction | None:
    """'1/15360' -> Fraction; '0/0', empty or malformed -> None (unknown, never 0)."""
    if not isinstance(text, str) or "/" not in text:
        return None
    left, right = text.split("/", 1)
    try:
        numerator, denominator = int(left), int(right)
    except ValueError:
        return None
    if denominator <= 0 or numerator <= 0:
        return None
    return Fraction(numerator, denominator)


def parse_seconds(text: Any) -> Fraction | None:
    """Decimal seconds text from ffprobe ('1.500000') -> exact Fraction; 'N/A' or missing -> None."""
    if not isinstance(text, str):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return Fraction(value)


def pts_to_ms(pts: int, time_base: Fraction, origin_seconds: Fraction) -> int:
    """t_ms = round_half_up(1000 * (pts * time_base - first_video_presentation_seconds))."""
    return round_half_up(1000 * (pts * time_base - origin_seconds))


def ms_text(milliseconds: int) -> str:
    """Integer milliseconds as a decimal-seconds argument for FFmpeg ('1500' -> '1.500')."""
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- owned tool runs

def resource_executable(resource: dict, names: tuple[str, ...]) -> Path:
    """A resource is bound either to the executable itself or to its directory."""
    root = Path(resource["absolute_path"])
    if root.is_file():
        return root
    for name in names:
        if (root / name).is_file():
            return root / name
    raise ContractError("RESOURCE_MISSING", f"none of {list(names)} exists in resource {resource['resource_ref']['resource_id']!r}",
                        details={"resource_id": resource["resource_ref"]["resource_id"], "absolute_path": str(root)})


def _tail(path: Path, limit: int = 1500) -> str:
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def run_tool(executable: Path, argv: list[str], *, log_dir: Path, label: str, runtime: MediaRuntime, cwd: Path | None = None,
             capability_id: str = "cpu") -> Path:
    """Run one owned tool to completion; non-zero exit is PROCESS_FAILED, cancellation is PROCESS_CANCELLED.
    Returns the directory holding stdout.log / stderr.log / process.json."""
    target = log_dir / label
    if target.exists():
        raise ContractError("OUTPUT_EXISTS", f"tool log directory already exists: {label}", details={"path": str(target)})
    target.mkdir(parents=True)
    spec = process_spec(executable, argv, cwd=cwd or target, log_dir=target, owner_token=runtime.owner_token, capability_id=capability_id)
    result = run_process(spec, runtime.cancel, runtime.emit)
    if result["cancelled"] or runtime.cancel.is_set():
        raise ContractError("PROCESS_CANCELLED", f"{label} was cancelled", retryable=True)
    if result["exit_code"] != 0:
        raise ContractError("PROCESS_FAILED", f"{label} exited with code {result['exit_code']}",
                            details={"label": label, "exit_code": result["exit_code"], "stderr_tail": _tail(target / "stderr.log"), "log_dir": str(target)})
    return target


def _ffprobe_json(runtime: MediaRuntime, argv: list[str], label: str) -> tuple[bytes, dict]:
    executable = resource_executable(runtime.ffprobe, ("ffprobe.exe", "ffprobe"))
    logs = run_tool(executable, argv, log_dir=runtime.log_dir, label=label, runtime=runtime)
    raw = (logs / "stdout.log").read_bytes()
    try:
        parsed = parse_json_text(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError("PROCESS_FAILED", f"{label} did not print valid JSON: {exc}", details={"label": label}) from exc
    if not isinstance(parsed, dict):
        raise ContractError("PROCESS_FAILED", f"{label} printed a non-object JSON document", details={"label": label})
    return raw, parsed


def probe_format_streams(path: Path, runtime: MediaRuntime, label: str = "ffprobe-streams") -> tuple[bytes, dict]:
    """The fixed probe of plan 7.1."""
    return _ffprobe_json(runtime, ["-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], label)


def probe_packets(path: Path, stream_index: int, runtime: MediaRuntime, label: str) -> tuple[bytes, list[dict]]:
    raw, parsed = _ffprobe_json(runtime, ["-v", "error", "-select_streams", str(stream_index), "-show_entries",
                                          "packet=pts,dts,duration,flags", "-of", "json", str(path)], label)
    packets = parsed.get("packets")
    return raw, packets if isinstance(packets, list) else []


def probe_first_audio_frame(path: Path, stream_index: int, runtime: MediaRuntime, label: str) -> tuple[bytes, dict | None]:
    """First *decoded* frame of an audio stream: its timestamp is where output sample 0 really sits (priming removed)."""
    raw, parsed = _ffprobe_json(runtime, ["-v", "error", "-select_streams", str(stream_index), "-read_intervals", "%+#8",
                                          "-show_frames", "-of", "json", str(path)], label)
    frames = parsed.get("frames")
    return raw, frames[0] if isinstance(frames, list) and frames and isinstance(frames[0], dict) else None


# --------------------------------------------------------------------------- stream normalization and selection

def _rotation(stream: dict) -> tuple[int, bool]:
    for item in stream.get("side_data_list") or []:
        if isinstance(item, dict) and "rotation" in item:
            value = item["rotation"]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer():
                return int(value) % 360, True
    tag = (stream.get("tags") or {}).get("rotate")
    declared = _as_int(tag)
    if declared is not None:
        return declared % 360, True
    return 0, False


def normalize_streams(raw: dict) -> list[dict]:
    """ffprobe streams -> the normalized array stored in MediaRecord.streams. Missing facts stay None (unknown)."""
    streams: list[dict] = []
    for position, stream in enumerate(raw.get("streams") or []):
        if not isinstance(stream, dict):
            continue
        time_base = parse_rational(stream.get("time_base"))
        rotation, rotation_declared = _rotation(stream)
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        side_types = [str(item.get("side_data_type")) for item in stream.get("side_data_list") or [] if isinstance(item, dict) and item.get("side_data_type")]
        streams.append({
            "index": _as_int(stream.get("index")), "codec_type": stream.get("codec_type"), "codec_name": stream.get("codec_name"),
            "profile": stream.get("profile"),
            "time_base": {"num": time_base.numerator, "den": time_base.denominator} if time_base else None,
            "start_pts": _as_int(stream.get("start_pts")), "start_time": stream.get("start_time") if parse_seconds(stream.get("start_time")) is not None else None,
            "duration_ts": _as_int(stream.get("duration_ts")), "duration": stream.get("duration") if parse_seconds(stream.get("duration")) is not None else None,
            "width": _as_int(stream.get("width")), "height": _as_int(stream.get("height")),
            "sample_aspect_ratio": stream.get("sample_aspect_ratio"), "display_aspect_ratio": stream.get("display_aspect_ratio"),
            "avg_frame_rate": stream.get("avg_frame_rate"), "r_frame_rate": stream.get("r_frame_rate"),
            "rotation_degrees": rotation, "rotation_declared": rotation_declared, "pix_fmt": stream.get("pix_fmt"),
            "color_range": stream.get("color_range"), "color_space": stream.get("color_space"),
            "color_transfer": stream.get("color_transfer"), "color_primaries": stream.get("color_primaries"),
            "sample_rate": _as_int(stream.get("sample_rate")), "channels": _as_int(stream.get("channels")),
            "channel_layout": stream.get("channel_layout"), "disposition": {key: value for key, value in disposition.items() if isinstance(value, int)},
            "attached_pic": bool(disposition.get("attached_pic")), "language": tags.get("language"), "side_data_types": side_types,
            "stereo_3d": next((item.get("type") for item in stream.get("side_data_list") or []
                               if isinstance(item, dict) and item.get("side_data_type") == "Stereo 3D"), None),
            "raw_ref": {"path": "probe/format_streams.json", "pointer": f"/streams/{position}"},
        })
    return streams


def _candidates(streams: list[dict]) -> list[dict]:
    keys = ("index", "codec_name", "width", "height", "sample_rate", "channels", "language")
    return [{key: stream.get(key) for key in keys} for stream in streams]


def select_streams(streams: list[dict], *, requested_video: int | None, requested_audio: int | None, audio_none: bool) -> tuple[int, int | None]:
    """Plan 3.1: one real video stream is selected implicitly; anything ambiguous must be chosen by the user."""
    videos = [s for s in streams if s["codec_type"] == "video" and not s["attached_pic"] and s["index"] is not None]
    audios = [s for s in streams if s["codec_type"] == "audio" and s["index"] is not None]
    if requested_video is not None:
        if requested_video not in [s["index"] for s in videos]:
            raise ContractError("NEEDS_SELECTION", f"stream {requested_video} is not a usable video stream",
                                details={"kind": "video_stream", "candidates": _candidates(videos)})
        video = requested_video
    elif len(videos) == 1:
        video = videos[0]["index"]
    elif not videos:
        raise ContractError("MEDIA_PROFILE_UNSUPPORTED", "the file has no video stream", details={"kind": "no_video_stream"})
    else:
        raise ContractError("NEEDS_SELECTION", "the file has several video streams; choose one with --video-stream",
                            details={"kind": "video_stream", "candidates": _candidates(videos)})
    if audio_none:
        if requested_audio is not None:
            raise ContractError("NEEDS_SELECTION", "audio stream is both 'none' and an index", details={"kind": "audio_stream"})
        return video, None
    if requested_audio is not None:
        if requested_audio not in [s["index"] for s in audios]:
            raise ContractError("NEEDS_SELECTION", f"stream {requested_audio} is not an audio stream",
                                details={"kind": "audio_stream", "candidates": _candidates(audios)})
        return video, requested_audio
    if not audios:
        return video, None
    if len(audios) == 1:
        return video, audios[0]["index"]
    raise ContractError("NEEDS_SELECTION", "the file has several audio streams; choose one with --audio-stream N or none",
                        details={"kind": "audio_stream", "candidates": _candidates(audios)})


def stream_by_index(streams: list[dict], index: int) -> dict:
    for stream in streams:
        if stream["index"] == index:
            return stream
    raise ContractError("SCHEMA_INVALID", f"stream {index} is not in the media record", details={"index": index})


# --------------------------------------------------------------------------- video timeline

def analyze_video_packets(packets: list[dict]) -> dict:
    """Presentation timeline of one video stream from its packets (ticks of the stream time base).

    status: ``ok`` | ``unknown`` (a presented packet has no PTS, or there is no packet) | ``discontinuity``
    (DTS goes backwards in file order, or two presented packets share a PTS). Forward gaps are legal variable
    frame timing and are kept as presentation time, not reported as an error.
    """
    presented: list[tuple[int, int | None]] = []
    discarded = 0
    missing_pts = 0
    previous_dts: int | None = None
    for position, packet in enumerate(packets):
        if not isinstance(packet, dict):
            continue
        dts = _as_int(packet.get("dts"))
        if dts is not None:
            if previous_dts is not None and dts < previous_dts:
                return {"status": "discontinuity", "problem": {"kind": "dts_backward", "packet": position, "previous_dts": previous_dts, "dts": dts}}
            previous_dts = dts
        if "D" in str(packet.get("flags", "")):
            discarded += 1
            continue
        pts = _as_int(packet.get("pts"))
        if pts is None:
            missing_pts += 1
            continue
        presented.append((pts, _as_int(packet.get("duration"))))
    if missing_pts:
        return {"status": "unknown", "problem": {"kind": "missing_pts", "packets_without_pts": missing_pts, "packets": len(packets)}}
    if not presented:
        return {"status": "unknown", "problem": {"kind": "no_presented_packets", "packets": len(packets)}}
    presented.sort(key=lambda item: item[0])
    pts_sorted = [pts for pts, _ in presented]
    for left, right in zip(pts_sorted, pts_sorted[1:]):
        if left == right:
            return {"status": "discontinuity", "problem": {"kind": "duplicate_pts", "pts": left}}
    intervals = [right - left for left, right in zip(pts_sorted, pts_sorted[1:])]
    if not intervals:
        kind = "single_frame"
    elif max(intervals) - min(intervals) <= 1:
        kind = "cfr"
    else:
        kind = "vfr"
    last_duration = presented[-1][1]
    return {"status": "ok", "problem": None, "pts": pts_sorted, "origin_pts": pts_sorted[0], "last_pts": pts_sorted[-1],
            "last_duration": last_duration if last_duration and last_duration > 0 else None, "frames": len(pts_sorted), "discarded": discarded,
            "frame_timing": {"kind": kind, "min_interval_ticks": min(intervals) if intervals else None,
                             "max_interval_ticks": max(intervals) if intervals else None}}


def media_end_seconds(timeline: dict, stream: dict, fmt: dict, time_base: Fraction) -> tuple[Fraction | None, str | None]:
    """End of the last presented frame (plan 7.2). Order: last frame + its duration, stream duration, container duration."""
    last_time = timeline["last_pts"] * time_base
    if timeline["last_duration"]:
        return (timeline["last_pts"] + timeline["last_duration"]) * time_base, "last_frame_plus_duration"
    if stream.get("start_pts") is not None and stream.get("duration_ts") is not None:
        end = (stream["start_pts"] + stream["duration_ts"]) * time_base
        if end > last_time:
            return end, "stream_duration"
    start, duration = parse_seconds(fmt.get("start_time")), parse_seconds(fmt.get("duration"))
    if start is not None and duration is not None and start + duration > last_time:
        return start + duration, "format_duration"
    return None, None


def unsupported_profile_reasons(stream: dict, fmt: dict) -> list[str]:
    reasons = []
    if stream.get("color_transfer") in HDR_TRANSFERS:
        reasons.append(f"hdr_transfer:{stream['color_transfer']}")
    for side_type in stream.get("side_data_types") or []:
        lowered = side_type.lower()
        if "dovi" in lowered or "dolby vision" in lowered:
            reasons.append("dolby_vision")
        if "mastering display" in lowered or "content light level" in lowered:
            reasons.append("hdr_static_metadata")
    if stream.get("stereo_3d") not in (None, "2D"):
        reasons.append(f"stereo_3d:{stream['stereo_3d']}")
    programs = _as_int(fmt.get("nb_programs"))
    if programs is not None and programs > 1:
        reasons.append(f"multi_program:{programs}")
    return sorted(set(reasons))


# --------------------------------------------------------------------------- project media registry

def media_dir(project_dir: Path, media_id: str) -> Path:
    return Path(project_dir) / "media" / media_id


def _binding_path(project_dir: Path, media_id: str) -> Path:
    return media_dir(project_dir, media_id) / "original.ref.json"


def load_media_record(project_dir: Path, media_id: str) -> dict:
    """Registered MediaRecord with ``source_ref`` replaced by the current local binding (original.ref.json)."""
    directory = media_dir(project_dir, media_id)
    manifest = read_manifest(directory, "MediaManifest")
    # the copied original (if any) is checked by verify_source (stat first, full hash on change), not on every load
    verify_committed(directory, [ref for ref in manifest["outputs"] if not ref["path"].startswith("source/")])
    record = load_record("MediaRecord", directory / "media.json")
    binding = _binding_path(project_dir, media_id)
    if binding.is_file():
        record["source_ref"] = load_binding(binding)
    return require_record("MediaRecord", record)


def load_binding(path: Path) -> dict:
    try:
        value = parse_json_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractError("SCHEMA_INVALID", f"media binding is not readable: {exc}", details={"path": str(path)}) from exc
    return require_record("ExternalFileRef", value.get("source_ref") if isinstance(value, dict) else None, source=str(path))


def _save_binding(project_dir: Path, media_id: str, source_ref: dict, note: str) -> None:
    require_record("ExternalFileRef", source_ref)
    atomic_write_text(_binding_path(project_dir, media_id), render_json({"schema_version": SCHEMA_VERSION, "source_ref": source_ref, "updated_at": utc_now(), "note": note}))


def _registered(project_dir: Path) -> list[dict]:
    root = Path(project_dir) / "media"
    records = []
    if root.is_dir():
        for candidate in sorted(root.iterdir()):
            if candidate.is_dir() and (candidate / MANIFEST_NAME).is_file() and (candidate / "media.json").is_file():
                try:
                    records.append(load_record("MediaRecord", candidate / "media.json"))
                except ContractError:
                    continue
    return records


def _file_identity(path: Path, project_dir: Path) -> dict:
    """ExternalFileRef for ``path``. A full SHA-256 is computed unless a registered binding already describes the same
    absolute path, size and mtime (metadata is an accelerator, not proof: plan 7.1 / 10.3)."""
    before = path.stat()
    known = None
    for record in _registered(project_dir):
        binding = _binding_path(project_dir, record["media_id"])
        ref = load_binding(binding) if binding.is_file() else record["source_ref"]
        if os.path.normcase(ref["absolute_path"]) == os.path.normcase(str(path)) and ref["bytes"] == before.st_size and ref["mtime_ns"] == before.st_mtime_ns:
            known = ref["sha256"]
            break
    digest = known or sha256_file(path)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ContractError("MEDIA_CHANGED", "the media file changed while it was being read; retry when it is stable", retryable=True,
                            details={"path": str(path)})
    return require_record("ExternalFileRef", {"absolute_path": str(path), "sha256": digest, "bytes": after.st_size, "mtime_ns": after.st_mtime_ns})


def verify_source(record: dict, project_dir: Path) -> dict:
    """Re-check the bound original before it is used (plan 7.1). A stat change forces a re-hash; different content is MEDIA_CHANGED."""
    ref = record["source_ref"]
    path = Path(ref["absolute_path"])
    if not path.is_file():
        raise ContractError("INPUT_MISSING", "the registered media file is not at its recorded location; relocate the media",
                            details={"media_id": record["media_id"], "path": str(path)})
    stat = path.stat()
    if stat.st_size == ref["bytes"] and stat.st_mtime_ns == ref["mtime_ns"]:
        return ref
    digest = sha256_file(path)
    if digest != record["source_sha256"]:
        raise ContractError("MEDIA_CHANGED", "the media file content differs from the registered media; register it as new media",
                            details={"media_id": record["media_id"], "path": str(path), "expected": record["source_sha256"], "observed": digest})
    refreshed = require_record("ExternalFileRef", {"absolute_path": str(path), "sha256": digest, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    _save_binding(project_dir, record["media_id"], refreshed, "stat changed, content verified by full hash")
    return refreshed


def relocate_media(project_dir: Path, media_id: str, new_path: Path | str) -> dict:
    """"重新定位媒体" (plan 3.2): bind a registered media to the file at its new location. Only the same content is
    accepted (full SHA-256); a different file is MEDIA_CHANGED and must be registered as new media. Nothing else changes."""
    record = load_media_record(project_dir, media_id)
    if record["extensions"].get("copied_original"):
        raise ContractError("NEEDS_SELECTION", "this media was copied into the project; it does not need relocating", details={"media_id": media_id})
    path = Path(new_path)
    check_path_components(path)
    if not path.is_absolute() or not path.is_file():
        raise ContractError("INPUT_MISSING", f"media file not found: {path}", details={"path": str(path)})
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ContractError("MEDIA_CHANGED", "the file changed while it was being read; retry when it is stable", retryable=True, details={"path": str(path)})
    if digest != record["source_sha256"]:
        raise ContractError("MEDIA_CHANGED", "this file is not the registered media (different content); register it as new media instead",
                            details={"media_id": media_id, "path": str(path), "expected": record["source_sha256"], "observed": digest})
    ref = require_record("ExternalFileRef", {"absolute_path": str(path), "sha256": digest, "bytes": after.st_size, "mtime_ns": after.st_mtime_ns})
    _save_binding(project_dir, media_id, ref, "relocated by the user; content verified by full hash")
    return ref


# --------------------------------------------------------------------------- probe_media

def _clock_problem(code: str, kind: str, details: dict) -> dict:
    return {"code": code, "kind": kind, "details": details}


def build_media_record(*, media_id: str, source_ref: dict, raw: dict, packets: list[dict], video_index: int, audio_index: int | None) -> dict:
    """Pure construction of a MediaRecord from probe results (no I/O)."""
    streams = normalize_streams(raw)
    video = stream_by_index(streams, video_index)
    fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    time_base = Fraction(video["time_base"]["num"], video["time_base"]["den"]) if video["time_base"] else None
    problem = None
    timeline: dict | None = None
    duration_ms, duration_source, origin_pts = 0, None, 0
    if time_base is None:
        problem = _clock_problem("MEDIA_CLOCK_UNKNOWN", "time_base_unknown", {"stream": video_index})
    else:
        timeline = analyze_video_packets(packets)
        if timeline["status"] == "unknown":
            problem = _clock_problem("MEDIA_CLOCK_UNKNOWN", timeline["problem"]["kind"], timeline["problem"])
        elif timeline["status"] == "discontinuity":
            problem = _clock_problem("MEDIA_CLOCK_DISCONTINUITY", timeline["problem"]["kind"], timeline["problem"])
        else:
            origin_pts = timeline["origin_pts"]
            end, duration_source = media_end_seconds(timeline, video, fmt, time_base)
            if end is None:
                problem = _clock_problem("MEDIA_CLOCK_UNKNOWN", "end_unknown", {"last_pts": timeline["last_pts"]})
            else:
                duration_ms = round_half_up(1000 * (end - origin_pts * time_base))
                if duration_ms <= 0:
                    problem = _clock_problem("MEDIA_CLOCK_UNKNOWN", "non_positive_duration", {"duration_ms": duration_ms})
                    duration_ms = 0
    verified = problem is None
    clock = {
        "origin_pts": origin_pts if verified else 0,
        "origin_time_base_num": time_base.numerator if time_base else 1, "origin_time_base_den": time_base.denominator if time_base else 1,
        "origin_kind": "first_presented_video_frame", "normalized_media_id": None,
        "mapping_kind": "identity" if (not verified or origin_pts == 0) else "constant_offset", "mapping_ref": None, "verified": verified,
    }
    extensions = {
        "ref_base": f"media/{media_id}",
        "format": {key: fmt.get(key) for key in ("format_name", "format_long_name", "start_time", "duration", "size", "bit_rate", "nb_streams", "nb_programs", "probe_score")},
        "duration_known": verified, "duration_source": duration_source,
        "frame_timing": ({**timeline["frame_timing"], "frames": timeline["frames"], "discarded_packets": timeline["discarded"],
                          "last_pts": timeline["last_pts"]} if timeline and timeline["status"] == "ok" else None),
        "clock_problem": problem,
        "unsupported_profile": unsupported_profile_reasons(video, fmt),
        "probe_packets_ref": {"path": "probe/video_packets.json"},
        "mapping_version": MAPPING_VERSION,
        "registered_at": utc_now(),
    }
    return require_record("MediaRecord", {
        "schema_version": SCHEMA_VERSION, "media_id": media_id, "source_ref": source_ref, "source_sha256": source_ref["sha256"],
        "duration_ms": duration_ms, "selected_video_stream": video_index, "selected_audio_stream": audio_index, "streams": streams,
        "clock": clock, "derivatives": [], "probe_raw_ref": {"path": "probe/format_streams.json", "sha256": "0" * 64, "bytes": 0, "media_type": "application/json"},
        "extensions": extensions,
    })


def probe_media(media_input: dict, *, runtime: MediaRuntime) -> dict:
    """MediaInput -> MediaRecord. Registers the media in the project (once per content + selected video stream).

    ``media_input["extensions"]`` may carry: ``local_path`` (the downloaded file of a url input), ``audio_stream:"none"``,
    ``copy_original: true`` and ``origin`` (download identity). A url input without ``local_path`` is INPUT_MISSING:
    downloading happens before probing (plan 6.8).
    """
    require_record("MediaInput", media_input)
    extensions = media_input["extensions"]
    job_input = media_input["input"]
    project_dir = Path(runtime.project_dir)
    if job_input["source_media_id"]:
        record = load_media_record(project_dir, job_input["source_media_id"])
        _, record["selected_audio_stream"] = select_streams(
            record["streams"], requested_video=record["selected_video_stream"], requested_audio=media_input["selected_audio_stream"],
            audio_none=extensions.get("audio_stream") == "none")
        return require_record("MediaRecord", record)
    local = extensions.get("local_path") or (job_input["value"] if job_input["kind"] == "local_video" else None)
    if not local:
        raise ContractError("INPUT_MISSING", "a url input must be downloaded before it can be probed", details={"input": job_input["kind"]})
    path = Path(local)
    check_path_components(path)
    if not path.is_absolute() or not path.is_file():
        raise ContractError("INPUT_MISSING", f"media file not found: {path}", details={"path": str(path)})

    raw_bytes, raw = probe_format_streams(path, runtime)
    streams = normalize_streams(raw)
    video_index, audio_index = select_streams(streams, requested_video=media_input["selected_video_stream"],
                                              requested_audio=media_input["selected_audio_stream"], audio_none=extensions.get("audio_stream") == "none")
    source_ref = _file_identity(path, project_dir)
    for existing in _registered(project_dir):
        if existing["source_sha256"] == source_ref["sha256"] and existing["selected_video_stream"] == video_index:
            binding = _binding_path(project_dir, existing["media_id"])
            current = load_binding(binding) if binding.is_file() else existing["source_ref"]
            copied = bool(existing["extensions"].get("copied_original"))
            if not copied and current != source_ref:
                _save_binding(project_dir, existing["media_id"], source_ref, "same content registered from another location or timestamp")
            record = load_media_record(project_dir, existing["media_id"])
            record["selected_audio_stream"] = audio_index
            runtime.emit({"event": "media_registration_reused", "media_id": existing["media_id"]})
            return require_record("MediaRecord", record)

    media_id = new_id()
    final_dir = media_dir(project_dir, media_id)
    pending = begin_pending(project_dir)
    packets_bytes, packets = probe_packets(path, video_index, runtime, "ffprobe-video-packets")
    if extensions.get("copy_original"):
        target = pending / "source" / ("original" + path.suffix.lower())
        target.parent.mkdir(parents=True)
        shutil.copy2(path, target)
        copied_stat = target.stat()
        if sha256_file(target) != source_ref["sha256"]:
            raise ContractError("MEDIA_CHANGED", "the copy of the original differs from the file that was hashed", retryable=True, details={"path": str(path)})
        source_ref = require_record("ExternalFileRef", {"absolute_path": str(final_dir / "source" / target.name), "sha256": source_ref["sha256"],
                                                        "bytes": copied_stat.st_size, "mtime_ns": copied_stat.st_mtime_ns})
    record = build_media_record(media_id=media_id, source_ref=source_ref, raw=raw, packets=packets, video_index=video_index, audio_index=audio_index)
    record["extensions"]["copied_original"] = bool(extensions.get("copy_original"))
    record["extensions"]["origin"] = extensions.get("origin") or {"kind": job_input["kind"]}
    record["extensions"]["hash_note"] = "full SHA-256 at first registration; later runs re-hash only when size or mtime changed"
    atomic_write_bytes(pending / "probe" / "format_streams.json", raw_bytes)
    atomic_write_bytes(pending / "probe" / "video_packets.json", packets_bytes)
    record["probe_raw_ref"] = artifact_ref(pending, pending / "probe" / "format_streams.json")
    record["extensions"]["probe_packets_ref"] = artifact_ref(pending, pending / "probe" / "video_packets.json")
    atomic_write_text(pending / "media.json", render_json(require_record("MediaRecord", record)))
    commit_pending(pending, final_dir, manifest_kind="MediaManifest", build_manifest=lambda refs: {
        "schema_version": SCHEMA_VERSION, "kind": "registration", "media_id": media_id, "media_revision": None, "signature": None,
        "outputs": refs, "committed_at": utc_now(), "extensions": {}})
    _save_binding(project_dir, media_id, source_ref, "initial registration")
    runtime.emit({"event": "media_registered", "media_id": media_id, "clock_verified": record["clock"]["verified"], "duration_ms": record["duration_ms"]})
    return record


# --------------------------------------------------------------------------- derived media

def is_direct_usable(record: dict, media_settings: dict) -> tuple[bool, list[str]]:
    """Plan 7.2: a common CFR, zero-start, unrotated H.264 MP4 is used as it is; everything else gets a derivative."""
    video = stream_by_index(record["streams"], record["selected_video_stream"])
    format_names = set(str(record["extensions"]["format"].get("format_name") or "").split(","))
    containers = media_settings.get("accepted_containers", [".mp4"])
    accepted_formats = {CONTAINER_FORMAT_NAMES.get(str(item).lower(), str(item).lower().lstrip(".")) for item in containers}
    accepted_codecs = {str(item).lower() for item in media_settings.get("accepted_video_codecs", ["h264"])}
    timing = (record["extensions"].get("frame_timing") or {}).get("kind")
    reasons = []
    if not format_names & accepted_formats:
        reasons.append("container")
    if str(video.get("codec_name") or "").lower() not in accepted_codecs:
        reasons.append("video_codec")
    if timing not in ("cfr", "single_frame"):
        reasons.append("variable_frame_timing")
    if record["clock"]["origin_pts"] != 0:
        reasons.append("non_zero_start")
    if video.get("rotation_degrees"):
        reasons.append("rotation")
    return not reasons, reasons


def _require_ffmpeg(runtime: MediaRuntime) -> Path:
    if runtime.ffmpeg is None:
        raise ContractError("RESOURCE_MISSING", "ffmpeg is required to derive media but is not registered", details={"role": "ffmpeg"})
    return resource_executable(runtime.ffmpeg, ("ffmpeg.exe", "ffmpeg"))


def _derived_dir(project_dir: Path, media_id: str, revision: str) -> Path:
    return media_dir(project_dir, media_id) / "derived" / revision


def _derivative_entry(project_dir: Path, media_id: str, revision: str, main_file: str) -> dict:
    """Reuse-or-report a committed derived directory as a MediaRecord.derivatives entry (paths relative to media/<id>/)."""
    directory = _derived_dir(project_dir, media_id, revision)
    manifest = read_manifest(directory, "MediaManifest")
    verify_committed(directory, manifest["outputs"])
    by_path = {ref["path"]: ref for ref in manifest["outputs"]}
    prefix = f"derived/{revision}/"
    derivation = load_record("Derivation", directory / "derivation.json")
    return {"entry": require_record("Derivative", {
                "media_revision": revision, "artifact_ref": {**by_path[main_file], "path": prefix + main_file},
                "derivation_ref": {**by_path["derivation.json"], "path": prefix + "derivation.json"},
                "clock_verified": bool(derivation["extensions"].get("clock_verified"))}),
            "directory": directory, "derivation": derivation, "manifest": manifest, "by_path": by_path}


def _audio_alignment(record: dict, audio_index: int, source: Path, runtime: MediaRuntime, log_label: str) -> dict:
    """Measure where the audio starts against the video origin (exact rationals) and express it as trim/pad milliseconds."""
    audio = stream_by_index(record["streams"], audio_index)
    if not audio["time_base"]:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the audio stream has no time base", details={"kind": "audio_time_base_unknown", "stream": audio_index})
    audio_tb = Fraction(audio["time_base"]["num"], audio["time_base"]["den"])
    _, frame = probe_first_audio_frame(source, audio_index, runtime, log_label)
    first_pts, start_source = None, None
    if frame is not None:
        for key in ("best_effort_timestamp", "pts", "pkt_pts"):
            first_pts = _as_int(frame.get(key))
            if first_pts is not None:
                start_source = f"first_decoded_frame.{key}"
                break
    if first_pts is None and audio["start_pts"] is not None:
        first_pts, start_source = audio["start_pts"], "stream.start_pts"
    if first_pts is None:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "cannot determine where the audio stream starts; it is not assumed to be 0",
                            details={"kind": "audio_start_unknown", "stream": audio_index})
    clock = record["clock"]
    origin = Fraction(clock["origin_pts"] * clock["origin_time_base_num"], clock["origin_time_base_den"])
    offset_ms = round_half_up(1000 * (first_pts * audio_tb - origin))
    filters = []
    if offset_ms < 0:
        filters = ["asetpts=N/SR/TB", f"atrim=start={ms_text(-offset_ms)}", "asetpts=N/SR/TB"]
    elif offset_ms > 0:
        filters = [f"adelay={offset_ms}:all=1"]
    return {"origin_offset_ms": offset_ms, "trim_before_ms": max(0, -offset_ms), "pad_before_ms": max(0, offset_ms), "filters": filters,
            "audio_first_pts": first_pts, "audio_time_base": {"num": audio_tb.numerator, "den": audio_tb.denominator}, "audio_start_source": start_source}


def _pad_wav_to(path: Path, target_frames: int) -> tuple[int, int]:
    """Append digital silence so the WAV covers the video duration (plan 7.3.3). Returns (frames_before, frames_after)."""
    with wave.open(str(path), "rb") as reader:
        parameters = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
        frames = reader.getnframes()
        if parameters != (1, 2, ASR_SAMPLE_RATE):
            raise ContractError("PROCESS_FAILED", "derived ASR audio is not 16 kHz mono 16-bit PCM", details={"observed": list(parameters)})
        if frames >= target_frames:
            return frames, frames
        padded = path.with_name(path.name + ".padding")
        with wave.open(str(padded), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(ASR_SAMPLE_RATE)
            while True:
                chunk = reader.readframes(ASR_SAMPLE_RATE * 30)
                if not chunk:
                    break
                writer.writeframesraw(chunk)
            remaining = target_frames - frames
            silence = b"\x00\x00" * min(remaining, ASR_SAMPLE_RATE * 30)
            while remaining > 0:
                count = min(remaining, ASR_SAMPLE_RATE * 30)
                writer.writeframesraw(silence[: count * 2])
                remaining -= count
    os.replace(padded, path)
    return frames, target_frames


def _check_source_unchanged(source: Path, ref: dict) -> None:
    stat = source.stat()
    if stat.st_size != ref["bytes"] or stat.st_mtime_ns != ref["mtime_ns"]:
        raise ContractError("MEDIA_CHANGED", "the media file changed while a derivative was being produced", retryable=True, details={"path": str(source)})


def _derive_asr_wav(record: dict, audio_index: int, runtime: MediaRuntime) -> dict:
    """16 kHz mono s16le WAV whose sample 0 is video time 0 and which covers the whole video duration."""
    project_dir = Path(runtime.project_dir)
    ffmpeg = _require_ffmpeg(runtime)
    parameters = {"kind": "asr_wav", "sample_rate": ASR_SAMPLE_RATE, "channels": 1, "codec": "pcm_s16le"}
    sig = signature({"source_sha256": record["source_sha256"], "video_stream": record["selected_video_stream"], "audio_stream": audio_index,
                     "parameters": parameters, "ffmpeg": runtime.ffmpeg["resource_ref"]["content_sha256"], "mapping_version": MAPPING_VERSION})
    revision = f"asr-wav-{sig[:16]}"
    final = _derived_dir(project_dir, record["media_id"], revision)
    if final.exists():
        runtime.emit({"event": "derived_media_reused", "media_revision": revision})
        return _derivative_entry(project_dir, record["media_id"], revision, "audio.wav")
    source = Path(record["source_ref"]["absolute_path"])
    pending = begin_pending(project_dir)
    logs = pending / "logs"
    alignment = _audio_alignment(record, audio_index, source, dataclasses.replace(runtime, log_dir=logs), "ffprobe-audio-first-frame")
    output = pending / "audio.wav"
    argv = ["-hide_banner", "-nostdin", "-n", "-i", str(source), "-map", f"0:{audio_index}", "-vn", "-sn", "-dn"]
    if alignment["filters"]:
        argv += ["-af", ",".join(alignment["filters"])]
    argv += ["-ac", "1", "-ar", str(ASR_SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", str(output)]
    run_tool(ffmpeg, argv, log_dir=logs, label="ffmpeg-asr-wav", runtime=runtime)
    _check_source_unchanged(source, record["source_ref"])
    if not output.is_file():
        raise ContractError("PROCESS_FAILED", "ffmpeg returned success but wrote no ASR audio", details={"label": "ffmpeg-asr-wav"})
    target_frames = record["duration_ms"] * ASR_SAMPLE_RATE // 1000
    natural_frames, final_frames = _pad_wav_to(output, target_frames)
    natural_ms = round_half_up(Fraction(natural_frames * 1000, ASR_SAMPLE_RATE))
    output_ms = round_half_up(Fraction(final_frames * 1000, ASR_SAMPLE_RATE))
    derivation = require_record("Derivation", {
        "schema_version": SCHEMA_VERSION, "source_media_id": record["media_id"], "source_stream": audio_index,
        "origin_offset_ms": alignment["origin_offset_ms"], "trim_before_ms": alignment["trim_before_ms"], "pad_before_ms": alignment["pad_before_ms"],
        "pad_after_ms": max(0, output_ms - natural_ms), "output_duration_ms": output_ms, "ffmpeg_ref": runtime.ffmpeg["resource_ref"],
        "argv_ref": artifact_ref(pending, logs / "ffmpeg-asr-wav" / "process.json"),
        "extensions": {"kind": "asr_wav", "parameters": parameters, "media_revision": revision, "signature": sig, "clock_verified": True,
                       "audio_first_pts": alignment["audio_first_pts"], "audio_time_base": alignment["audio_time_base"],
                       "audio_start_source": alignment["audio_start_source"], "frames_before_padding": natural_frames, "frames": final_frames,
                       "video_duration_ms": record["duration_ms"], "mapping_version": MAPPING_VERSION,
                       "note": "sample 0 is the first presented video frame; silence after the audio ends is appended as zero samples"},
    })
    atomic_write_text(pending / "derivation.json", render_json(derivation))
    commit_pending(pending, final, manifest_kind="MediaManifest", build_manifest=lambda refs: {
        "schema_version": SCHEMA_VERSION, "kind": "derived", "media_id": record["media_id"], "media_revision": revision, "signature": sig,
        "outputs": refs, "committed_at": utc_now(), "extensions": {"derivative": "asr_wav"}})
    runtime.emit({"event": "derived_media_committed", "media_revision": revision, "kind": "asr_wav", "output_duration_ms": output_ms})
    return _derivative_entry(project_dir, record["media_id"], revision, "audio.wav")


def compare_frame_maps(source_pts: list[int], source_tb: Fraction, derived_pts: list[int], derived_tb: Fraction) -> dict:
    """Every presented source frame must land on the same millisecond in the derivative (tolerance 1 ms for rounding)."""
    origin = source_pts[0] * source_tb if source_pts else Fraction(0)
    source_ms = [pts_to_ms(pts, source_tb, origin) for pts in source_pts]
    derived_ms = [round_half_up(1000 * pts * derived_tb) for pts in derived_pts]
    mismatches = []
    max_delta = 0
    if len(source_ms) == len(derived_ms):
        for position, (left, right) in enumerate(zip(source_ms, derived_ms)):
            delta = abs(left - right)
            max_delta = max(max_delta, delta)
            if delta > FRAME_MAP_TOLERANCE_MS and len(mismatches) < 20:
                mismatches.append({"frame": position, "source_ms": left, "derived_ms": right})
    verified = len(source_ms) == len(derived_ms) and bool(source_ms) and not mismatches and derived_pts[0] == 0
    return {"verified": verified, "source_frames": len(source_ms), "derived_frames": len(derived_ms), "max_abs_delta_ms": max_delta,
            "derived_first_pts": derived_pts[0] if derived_pts else None, "mismatches": mismatches, "tolerance_ms": FRAME_MAP_TOLERANCE_MS}


def _timescale(time_base: Fraction) -> int:
    if time_base.numerator == 1 and time_base.denominator <= 1_000_000:
        scale = time_base.denominator
        while scale < 1000:
            scale *= 10
        return scale
    return 90000


def _derive_review_video(record: dict, audio_index: int | None, runtime: MediaRuntime) -> dict:
    """H.264 review/recognition video on the same clock: t=0 at the first presented frame, no frame added or dropped."""
    project_dir = Path(runtime.project_dir)
    ffmpeg = _require_ffmpeg(runtime)
    settings = runtime.media_settings
    video = stream_by_index(record["streams"], record["selected_video_stream"])
    clock = record["clock"]
    source_tb = Fraction(clock["origin_time_base_num"], clock["origin_time_base_den"])
    rotated = video["rotation_degrees"] % 180 == 90
    width, height = (video["height"], video["width"]) if rotated else (video["width"], video["height"])
    if not width or not height:
        raise ContractError("MEDIA_PROFILE_UNSUPPORTED", "video dimensions are unknown", details={"kind": "dimensions_unknown"})
    pad_right, pad_bottom = width % 2, height % 2
    parameters = {"profile_id": COMPAT_PROFILE_ID, "video_codec": "libx264", "crf": int(settings.get("crf", 18)), "preset": str(settings.get("preset", "fast")),
                  "pix_fmt": "yuv420p", "fps_mode": "passthrough", "origin_shift": "bsf_setts_first_packet", "enc_time_base": "source_stream",
                  "audio_codec": "aac" if audio_index is not None else None,
                  "audio_bitrate": "192k" if audio_index is not None else None, "pad_right_px": pad_right, "pad_bottom_px": pad_bottom,
                  "timescale": _timescale(source_tb)}
    video_identity = signature({"source_sha256": record["source_sha256"], "video_stream": record["selected_video_stream"],
                                "parameters": {key: value for key, value in parameters.items() if not key.startswith("audio_")},
                                "ffmpeg": runtime.ffmpeg["resource_ref"]["content_sha256"], "mapping_version": MAPPING_VERSION})
    sig = signature({"video_identity": video_identity, "audio_stream": audio_index, "audio": [parameters["audio_codec"], parameters["audio_bitrate"]]})
    revision = f"review-video-{sig[:16]}"
    final = _derived_dir(project_dir, record["media_id"], revision)
    if final.exists():
        runtime.emit({"event": "derived_media_reused", "media_revision": revision})
        return _derivative_entry(project_dir, record["media_id"], revision, "video.mp4")
    source = Path(record["source_ref"]["absolute_path"])
    pending = begin_pending(project_dir)
    logs = pending / "logs"
    probe_runtime = dataclasses.replace(runtime, log_dir=logs)
    encoders = run_tool(ffmpeg, ["-hide_banner", "-encoders"], log_dir=logs, label="ffmpeg-encoders", runtime=runtime)
    if b"libx264" not in (encoders / "stdout.log").read_bytes():
        raise ContractError("CAPABILITY_MISSING", "the registered ffmpeg has no libx264 encoder; the review video cannot be produced",
                            details={"resource_id": runtime.ffmpeg["resource_ref"]["resource_id"], "capability": "libx264"})
    # No setpts filter: it clears frame durations, and the MP4 muxer then cuts the last frame off through the edit list.
    # The origin is moved on the encoded packets instead (setts keeps durations and works in exact integer ticks).
    filters = []
    if pad_right or pad_bottom:
        filters.append(f"pad={width + pad_right}:{height + pad_bottom}:0:0:black")
    output = pending / "video.mp4"
    argv = ["-hide_banner", "-nostdin", "-n", "-i", str(source), "-map", f"0:{record['selected_video_stream']}"]
    alignment = None
    if audio_index is not None:
        alignment = _audio_alignment(record, audio_index, source, probe_runtime, "ffprobe-audio-first-frame")
        argv += ["-map", f"0:{audio_index}", "-af", ",".join(alignment["filters"] + ["asetpts=N/SR/TB"]), "-c:a", "aac", "-b:a", "192k"]
    argv += ["-sn", "-dn"] + (["-vf", ",".join(filters)] if filters else [])
    # passthrough keeps every frame; the explicit encoder time base keeps the source ticks (the default 1/frame-rate
    # would snap variable frame timing to a coarse grid)
    argv += ["-fps_mode", "passthrough", "-enc_time_base", f"{source_tb.numerator}:{source_tb.denominator}",
             "-c:v", "libx264", "-preset", parameters["preset"], "-crf", str(parameters["crf"]), "-pix_fmt", "yuv420p",
             "-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS", "-video_track_timescale", str(parameters["timescale"]),
             "-movflags", "+faststart", str(output)]
    try:
        run_tool(ffmpeg, argv, log_dir=logs, label="ffmpeg-review-video", runtime=runtime)
    except ContractError as exc:
        if exc.code == "PROCESS_FAILED" and "fps_mode" in exc.details.get("stderr_tail", ""):
            raise ContractError("CAPABILITY_MISSING", "the registered ffmpeg does not support -fps_mode (FFmpeg 5.1 or newer is required)",
                                details={"resource_id": runtime.ffmpeg["resource_ref"]["resource_id"], "capability": "fps_mode"}) from exc
        raise
    _check_source_unchanged(source, record["source_ref"])
    if not output.is_file():
        raise ContractError("PROCESS_FAILED", "ffmpeg returned success but wrote no review video", details={"label": "ffmpeg-review-video"})

    # exit code 0 is not enough (plan 7.3.6): probe the result and compare every presented frame with the source
    derived_raw_bytes, derived_raw = probe_format_streams(output, probe_runtime, "ffprobe-derived-streams")
    derived_streams = normalize_streams(derived_raw)
    derived_video = next((s for s in derived_streams if s["codec_type"] == "video"), None)
    if derived_video is None or not derived_video["time_base"]:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the derived video has no readable video stream", details={"kind": "derivative_unreadable"})
    derived_tb = Fraction(derived_video["time_base"]["num"], derived_video["time_base"]["den"])
    derived_packets_bytes, derived_packets = probe_packets(output, derived_video["index"], probe_runtime, "ffprobe-derived-packets")
    derived_timeline = analyze_video_packets(derived_packets)
    source_packets = parse_json_text((media_dir(project_dir, record["media_id"]) / "probe" / "video_packets.json").read_text(encoding="utf-8")).get("packets") or []
    source_timeline = analyze_video_packets(source_packets)
    if derived_timeline["status"] != "ok" or source_timeline["status"] != "ok":
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the derived video has no verifiable frame timeline",
                            details={"kind": "derivative_timeline", "derived": derived_timeline.get("problem"), "source": source_timeline.get("problem")})
    comparison = compare_frame_maps(source_timeline["pts"], source_tb, derived_timeline["pts"], derived_tb)
    problems = []
    if not comparison["verified"]:
        problems.append("frame_map")
    if derived_video["codec_name"] != "h264" or derived_video["pix_fmt"] != "yuv420p":
        problems.append("codec_or_pixel_format")
    if (derived_video["width"], derived_video["height"]) != (width + pad_right, height + pad_bottom):
        problems.append("dimensions")
    if derived_video["rotation_degrees"]:
        problems.append("rotation_metadata_left")
    frame_map = {"schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "source_media_id": record["media_id"],
                 "source_time_base": {"num": source_tb.numerator, "den": source_tb.denominator}, "source_origin_pts": clock["origin_pts"],
                 "derived_time_base": {"num": derived_tb.numerator, "den": derived_tb.denominator}, "comparison": comparison, "problems": problems,
                 "source_pts": source_timeline["pts"], "derived_pts": derived_timeline["pts"]}
    atomic_write_text(pending / "frame_map.json", json.dumps(frame_map, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    atomic_write_bytes(pending / "probe" / "format_streams.json", derived_raw_bytes)
    atomic_write_bytes(pending / "probe" / "video_packets.json", derived_packets_bytes)
    if problems:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the re-encoded video does not reproduce the source presentation times; it is not used for recognition",
                            details={"kind": "derivative_drift", "problems": problems, "comparison": {k: v for k, v in comparison.items() if k != "mismatches"},
                                     "mismatches": comparison["mismatches"][:5], "candidate": str(pending)})
    end, _ = media_end_seconds(derived_timeline, derived_video, derived_raw.get("format") or {}, derived_tb)
    output_ms = round_half_up(1000 * end) if end is not None else record["duration_ms"]
    derivation = require_record("Derivation", {
        "schema_version": SCHEMA_VERSION, "source_media_id": record["media_id"], "source_stream": record["selected_video_stream"],
        "origin_offset_ms": 0, "trim_before_ms": 0, "pad_before_ms": 0, "pad_after_ms": 0, "output_duration_ms": output_ms,
        "ffmpeg_ref": runtime.ffmpeg["resource_ref"], "argv_ref": artifact_ref(pending, logs / "ffmpeg-review-video" / "process.json"),
        "extensions": {"kind": "review_video", "parameters": parameters, "media_revision": revision, "signature": sig, "video_identity": video_identity,
                       "clock_verified": True, "frame_map": {key: value for key, value in comparison.items() if key != "mismatches"},
                       "source_rotation_degrees": video["rotation_degrees"], "display_size": [width, height], "audio_stream": audio_index,
                       "audio_alignment": {key: value for key, value in (alignment or {}).items() if key != "filters"} or None,
                       "mapping_version": MAPPING_VERSION,
                       "note": "video frame 0 is the first presented source frame; origin_offset/trim/pad fields describe the video stream (none)"},
    })
    atomic_write_text(pending / "derivation.json", render_json(derivation))
    commit_pending(pending, final, manifest_kind="MediaManifest", build_manifest=lambda refs: {
        "schema_version": SCHEMA_VERSION, "kind": "derived", "media_id": record["media_id"], "media_revision": revision, "signature": sig,
        "outputs": refs, "committed_at": utc_now(), "extensions": {"derivative": "review_video", "video_identity": video_identity}})
    runtime.emit({"event": "derived_media_committed", "media_revision": revision, "kind": "review_video", "frames": comparison["derived_frames"]})
    return _derivative_entry(project_dir, record["media_id"], revision, "video.mp4")


def _project_ref(entry: dict, media_id: str) -> dict:
    """ArtifactRef of a derived file relative to the project directory."""
    ref = entry["entry"]["artifact_ref"]
    return require_record("ArtifactRef", {**ref, "path": f"media/{media_id}/{ref['path']}"})


def prepare_media(plan: dict, *, runtime: MediaRuntime) -> dict:
    """MediaPlan -> {media_record, video_for_review, audio_for_asr, derivation_refs} (plan 6.8, 7.2, 7.3).

    ``plan["extensions"]["selected_audio_stream"]`` carries this request's audio choice (int or None).
    """
    require_record("MediaPlan", plan)
    project_dir = Path(runtime.project_dir)
    record = load_media_record(project_dir, plan["media_id"])
    record["source_ref"] = verify_source(record, project_dir)
    audio_index = plan["extensions"].get("selected_audio_stream")
    record["selected_audio_stream"] = audio_index
    problem = record["extensions"].get("clock_problem")
    if not record["clock"]["verified"]:
        code = problem["code"] if problem else "MEDIA_CLOCK_UNKNOWN"
        raise ContractError(code, "the media time axis cannot be established; nothing that depends on media time is produced",
                            details={"media_id": record["media_id"], "problem": problem})
    unsupported = record["extensions"].get("unsupported_profile") or []
    if unsupported:
        raise ContractError("MEDIA_PROFILE_UNSUPPORTED", "HDR, stereo or multi-programme sources are registered but not converted in this version",
                            details={"media_id": record["media_id"], "reasons": unsupported})
    if plan["needs_audio"] and audio_index is None:
        raise ContractError("NEEDS_SELECTION", "this mode needs an audio stream; choose visual_only for media without audio",
                            details={"kind": "no_audio_stream", "media_id": record["media_id"]})
    if plan["compatibility_profile"] != COMPAT_PROFILE_ID:
        raise ContractError("MEDIA_PROFILE_UNSUPPORTED", f"unknown compatibility profile {plan['compatibility_profile']!r}",
                            details={"supported": [COMPAT_PROFILE_ID]})
    direct, reasons = is_direct_usable(record, runtime.media_settings)
    derivatives, derivation_refs = [], []
    if direct:
        video_for_review = {"kind": "original", "media_revision": "original", "absolute_path": record["source_ref"]["absolute_path"],
                            "ref": record["source_ref"], "video_identity": signature({"source_sha256": record["source_sha256"],
                                                                                    "video_stream": record["selected_video_stream"], "direct": True})}
    else:
        if runtime.media_settings.get("auto_convert", True) is False:
            raise ContractError("MEDIA_PROFILE_UNSUPPORTED", "this media needs a compatible derivative but conversion is disabled in the settings",
                                details={"media_id": record["media_id"], "reasons": reasons})
        entry = _derive_review_video(record, audio_index, runtime)
        derivatives.append(entry["entry"])
        derivation_refs.append(require_record("ArtifactRef", {**entry["entry"]["derivation_ref"], "path": f"media/{record['media_id']}/{entry['entry']['derivation_ref']['path']}"}))
        video_for_review = {"kind": "derived", "media_revision": entry["entry"]["media_revision"], "absolute_path": str(entry["directory"] / "video.mp4"),
                            "ref": _project_ref(entry, record["media_id"]), "video_identity": entry["derivation"]["extensions"]["video_identity"]}
        prefix = f"derived/{entry['entry']['media_revision']}/"
        record["clock"] = {**record["clock"], "mapping_kind": "verified_frame_map", "mapping_ref": {**entry["by_path"]["frame_map.json"], "path": prefix + "frame_map.json"}}
    audio_for_asr = None
    if plan["needs_audio"]:
        entry = _derive_asr_wav(record, audio_index, runtime)
        derivatives.append(entry["entry"])
        derivation_refs.append(require_record("ArtifactRef", {**entry["entry"]["derivation_ref"], "path": f"media/{record['media_id']}/{entry['entry']['derivation_ref']['path']}"}))
        audio_for_asr = {"kind": "derived", "media_revision": entry["entry"]["media_revision"], "absolute_path": str(entry["directory"] / "audio.wav"),
                         "ref": _project_ref(entry, record["media_id"]), "output_duration_ms": entry["derivation"]["output_duration_ms"]}
    record["derivatives"] = derivatives
    record["extensions"] = {**record["extensions"], "direct_usable": direct, "derivative_reasons": reasons}
    return {"media_record": require_record("MediaRecord", record), "video_for_review": video_for_review, "audio_for_asr": audio_for_asr,
            "derivation_refs": derivation_refs}


# --------------------------------------------------------------------------- url input (plan 7.1)

def worker_command(name: str) -> tuple[Path, list[str], dict[str, str]]:
    """How this build starts one of its own worker modules (plan 12.3): frozen ``roast.exe worker NAME`` or the dev module form."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable), ["worker", name], {}
    tools_root = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    return Path(sys.executable), ["-X", "utf8", "-m", "upgrade.cli", "worker", name], {"PYTHONPATH": tools_root + (os.pathsep + existing if existing else "")}


def download_argv(url: str, output_dir: Path, settings: dict, ffmpeg: Path | None) -> list[str]:
    """Fixed yt-dlp argument list: single video only, info JSON kept, no browser cookies unless explicitly configured."""
    argv = ["--no-playlist", "--newline", "--write-info-json", "--merge-output-format", "mp4", "-o", str(output_dir / "video.%(ext)s")]
    if settings.get("proxy"):
        argv += ["--proxy", str(settings["proxy"])]
    if ffmpeg is not None:
        argv += ["--ffmpeg-location", str(ffmpeg)]
    quality = settings.get("quality")
    if quality:
        if quality not in DOWNLOAD_FORMATS:
            raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"unknown download quality {quality!r}", details={"supported": sorted(DOWNLOAD_FORMATS)})
        argv += ["-f", DOWNLOAD_FORMATS[quality]]
    browser = settings.get("browser_login_source")
    if browser:
        if browser not in ("edge", "chrome", "firefox"):
            raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"unknown browser {browser!r}", details={"supported": ["edge", "chrome", "firefox"]})
        argv += ["--cookies-from-browser", browser]
    if settings.get("js_runtime"):
        argv += ["--js-runtimes", f"node:{settings['js_runtime']}"]
    client = settings.get("youtube_client")
    if client:
        if client not in ("android_vr", "tv"):
            raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"unknown youtube client {client!r}", details={"supported": ["android_vr", "tv"]})
        argv += ["--extractor-args", f"youtube:player_client={client}"]
    return argv + [url]


def download_media(url: str, *, runtime: MediaRuntime) -> dict:
    """Fetch one http/https video into ``media/downloads/<download_id>/`` (committed once) and return its identity."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise ContractError("INPUT_MISSING", "only http/https single-video urls are supported", details={"url": str(url)[:200]})
    project_dir = Path(runtime.project_dir)
    download_id = new_id()
    final = project_dir / "media" / "downloads" / download_id
    pending = begin_pending(project_dir)
    files = pending / "files"
    files.mkdir()
    executable, prefix, env = worker_command("yt-dlp")
    ffmpeg = resource_executable(runtime.ffmpeg, ("ffmpeg.exe", "ffmpeg")) if runtime.ffmpeg else None
    argv = prefix + download_argv(url, files, runtime.download_settings, ffmpeg)
    logs = pending / "logs" / "yt-dlp"
    logs.mkdir(parents=True)
    spec = process_spec(executable, argv, cwd=files, log_dir=logs, owner_token=runtime.owner_token, capability_id="cpu", env_overrides=env)
    result = run_process(spec, runtime.cancel, runtime.emit)
    if result["cancelled"] or runtime.cancel.is_set():
        raise ContractError("PROCESS_CANCELLED", "download was cancelled", retryable=True)
    if result["exit_code"] != 0:
        raise ContractError("PROCESS_FAILED", f"yt-dlp exited with code {result['exit_code']}; a local file can be submitted instead", retryable=True,
                            details={"exit_code": result["exit_code"], "stderr_tail": _tail(logs / "stderr.log"), "candidate": str(pending)})
    videos = sorted(p for p in files.iterdir() if p.is_file() and p.name.startswith("video.") and p.suffix.lower() not in (".json", ".part", ".ytdl", ".tmp"))
    if len(videos) != 1:
        raise ContractError("PROCESS_FAILED", "yt-dlp returned success but did not leave exactly one video file",
                            details={"files": [p.name for p in files.iterdir()], "candidate": str(pending)})
    committed = commit_pending(pending, final, manifest_kind="MediaManifest", build_manifest=lambda refs: {
        "schema_version": SCHEMA_VERSION, "kind": "download", "media_id": None, "media_revision": None, "signature": None, "outputs": refs,
        "committed_at": utc_now(), "extensions": {"download_id": download_id, "url": url}})
    local = final / "files" / videos[0].name
    info = next((ref for ref in committed["artifact_refs"] if ref["path"].endswith(".info.json")), None)
    runtime.emit({"event": "download_committed", "download_id": download_id, "file": videos[0].name})
    return {"download_id": download_id, "local_path": str(local), "info_ref": info,
            "origin": {"kind": "url", "url": url, "download_id": download_id, "manifest": f"media/downloads/{download_id}/{MANIFEST_NAME}"}}


__all__ = [
    "MAPPING_VERSION", "COMPAT_PROFILE_ID", "ASR_SAMPLE_RATE", "MediaRuntime", "round_half_up", "parse_rational", "parse_seconds", "pts_to_ms",
    "ms_text", "resource_executable", "run_tool", "probe_format_streams", "probe_packets", "probe_first_audio_frame", "normalize_streams",
    "select_streams", "stream_by_index", "analyze_video_packets", "media_end_seconds", "unsupported_profile_reasons", "media_dir",
    "load_media_record", "verify_source", "relocate_media", "build_media_record", "probe_media", "is_direct_usable", "compare_frame_maps", "prepare_media",
    "worker_command", "download_argv", "download_media",
]


# --------------------------------------------------------------------------- boundary frames on demand (plan 11.3, W06)

FRAME_PURPOSES = ("boundary_before", "boundary_at")
_SHOWINFO_RE = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*(-?[0-9.]+)")


def boundary_frame_plan(pts_sorted: list[int], time_base: Fraction, origin_seconds: Fraction, target_ms: int) -> list[dict]:
    """For one boundary time: the frame on screen at that time and the frame just before it, as presented PTS.
    A time before the first frame has only the first frame; nothing is interpolated."""
    times = [pts_to_ms(pts, time_base, origin_seconds) for pts in pts_sorted]
    at = max((position for position, ms in enumerate(times) if ms <= target_ms), default=0)
    plan = []
    if at > 0 and times[at] <= target_ms:
        plan.append({"purpose": "boundary_before", "position": at - 1})
    plan.append({"purpose": "boundary_at", "position": at})
    return [{**entry, "pts": pts_sorted[entry["position"]], "frame_ms": times[entry["position"]],
             "previous_pts": pts_sorted[entry["position"] - 1] if entry["position"] > 0 else None, "target_ms": target_ms} for entry in plan]


def _seek_seconds(entry: dict, time_base: Fraction, file_start: Fraction) -> str:
    """Seek between the previous presented frame and this one, so the first decoded frame kept is exactly this frame.
    ffmpeg's input -ss counts from the file start time (the earliest stream start), not from timestamp 0."""
    if entry["previous_pts"] is None:
        seconds = entry["pts"] * time_base - Fraction(1, 1000)
    else:
        seconds = (entry["previous_pts"] + entry["pts"]) * time_base / 2
    return f"{float(max(seconds - file_start, Fraction(0))):.6f}"


def export_boundary_frames(record: dict, times_ms: list[int], destination: Path, *, runtime: MediaRuntime) -> dict:
    """Write the frames around each boundary time as PNG into a new ``destination`` directory.

    The label of each image is the PTS ffmpeg reports for the decoded frame (showinfo), checked against the
    presented PTS from the packet probe; a mismatch is EXPORT_VALIDATION_FAILED, never a computed guess.
    The original media is only read; ``destination`` must not exist yet (OUTPUT_EXISTS).
    """
    destination = Path(destination)
    if destination.exists():
        raise ContractError("OUTPUT_EXISTS", "the frame directory already exists; choose a new one", details={"path": str(destination)})
    source = Path(verify_source(record, runtime.project_dir)["absolute_path"])
    ffmpeg = _require_ffmpeg(runtime)
    stream = stream_by_index(record["streams"], record["selected_video_stream"])
    time_base = Fraction(stream["time_base"]["num"], stream["time_base"]["den"]) if stream.get("time_base") else None
    if time_base is None:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the selected video stream has no time base", details={"media_id": record["media_id"]})
    _, packets = probe_packets(source, record["selected_video_stream"], runtime, "frames-ffprobe-packets")
    timeline = analyze_video_packets(packets)
    if timeline["status"] != "ok":
        raise ContractError("MEDIA_CLOCK_UNKNOWN" if timeline["status"] == "unknown" else "MEDIA_CLOCK_DISCONTINUITY",
                            "the video timeline cannot be read frame by frame", details={"media_id": record["media_id"], "problem": timeline["problem"]})
    clock = record["clock"]
    if not clock["verified"]:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the media clock of this video is not verified; frames cannot be placed on the subtitle time line", details={"media_id": record["media_id"]})
    origin_seconds = Fraction(clock["origin_pts"] * clock["origin_time_base_num"], clock["origin_time_base_den"])
    starts = [parse_seconds(item.get("start_time")) for item in record["streams"]]
    file_start = min((value for value in starts if value is not None), default=Fraction(0))
    wanted: dict[int, dict] = {}
    for target in sorted(set(int(value) for value in times_ms)):
        for entry in boundary_frame_plan(timeline["pts"], time_base, origin_seconds, target):
            wanted.setdefault(entry["pts"], {**entry, "purposes": []})["purposes"].append({"purpose": entry["purpose"], "target_ms": target})
    pending = destination.with_name(destination.name + ".pending-" + new_id()[:8])
    pending.mkdir(parents=True)
    frames = []
    try:
        for pts, entry in sorted(wanted.items()):
            name = f"frame-pts{pts}-tb{time_base.numerator}_{time_base.denominator}.png"
            logs = run_tool(ffmpeg, ["-hide_banner", "-nostdin", "-loglevel", "info", "-copyts", "-ss", _seek_seconds(entry, time_base, file_start),
                                     "-i", str(source), "-map", f"0:{record['selected_video_stream']}", "-vf", "showinfo", "-frames:v", "1", "-an", "-sn", "-dn",
                                     "-f", "image2", "-update", "1", str(pending / name)], log_dir=runtime.log_dir, label=f"frame-{pts}", runtime=runtime)
            found = _SHOWINFO_RE.search((logs / "stderr.log").read_text(encoding="utf-8", errors="replace"))
            reported = int(found.group(2)) if found else None
            if reported != pts or not (pending / name).is_file():
                raise ContractError("EXPORT_VALIDATION_FAILED", "ffmpeg did not decode the frame that was asked for; no frame is labelled with a guessed time",
                                    details={"expected_pts": pts, "reported_pts": reported, "media_id": record["media_id"]})
            frames.append({"file": name, "pts": pts, "time_base_num": time_base.numerator, "time_base_den": time_base.denominator, "frame_ms": entry["frame_ms"],
                           "pts_source": "ffmpeg_showinfo", "purposes": entry["purposes"], "sha256": sha256_file(pending / name)})
        document = {"schema_version": SCHEMA_VERSION, "media_id": record["media_id"], "source_sha256": record["source_sha256"], "video_stream": record["selected_video_stream"],
                    "times_ms": sorted(set(int(value) for value in times_ms)), "frames": frames, "created_at": utc_now(),
                    "note": "frame_ms is the presented time on the canonical clock of this media (first presented video frame = 0); the image is the decoded frame with that PTS"}
        atomic_write_text(pending / "frames.json", render_json(document))
        os.replace(pending, destination)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return document
