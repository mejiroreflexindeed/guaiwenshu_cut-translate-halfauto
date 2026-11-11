#!/usr/bin/env python3
"""Independent subtitle / video exports (plan 3.2, 7.5, 9.5; work package W07).

An export takes one explicitly chosen, committed revision (resolved by the caller: only the CLI may touch
``revisions``) and writes a new export directory once: the chosen subtitle bytes, or a video made by FFmpeg from
the registered media and the complete subtitle file. No model process is started and no legacy pipeline code runs.

Profiles (7.5): ``subtitle_files_v1`` copies the revision's own file(s); ``mp4_burn_sdr_v1`` burns the subtitle
with libass into H.264/AAC; ``mkv_soft_v1`` copies the selected streams and attaches the subtitle as a track.
Every output is first written under a ``.pending-`` sibling of the destination, validated, then renamed into place;
a failure or cancellation publishes nothing (the candidate is kept for diagnosis).
"""

from __future__ import annotations

import os
import shutil
import struct
from fractions import Fraction
from pathlib import Path
from typing import Any

from upgrade import media, quality, subtitles
from upgrade.contracts import SCHEMA_VERSION, ContractError, require_record, sha256_bytes, sha256_file, utc_now
from upgrade.storage import artifact_ref, atomic_write_bytes, atomic_write_text, ensure_ascii_root, render_json, safe_join

FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")


# --------------------------------------------------------------------------- fonts: explicit directories and the system font folders only

def system_font_dirs() -> list[Path]:
    """The Windows system and per-user font folders (plan 7.5: fonts come from explicit paths or a system query only)."""
    dirs = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return [directory for directory in dirs if directory.is_dir()]


def _read(handle, offset: int, length: int) -> bytes:
    handle.seek(offset)
    data = handle.read(length)
    if len(data) != length:
        raise ValueError("truncated font file")
    return data


def _faces(handle) -> list[int]:
    head = _read(handle, 0, 12)
    if head[:4] == b"ttcf":
        count = struct.unpack(">I", head[8:12])[0]
        return list(struct.unpack(f">{count}I", _read(handle, 12, 4 * count)))
    return [0]


def _table_directory(handle, offset: int) -> dict[bytes, tuple[int, int]]:
    count = struct.unpack(">H", _read(handle, offset + 4, 2))[0]
    raw = _read(handle, offset + 12, 16 * count)
    tables = {}
    for position in range(count):
        tag, _, table_offset, length = struct.unpack(">4sIII", raw[16 * position:16 * position + 16])
        tables[tag] = (table_offset, length)
    return tables


def _names(handle, table: tuple[int, int]) -> set[str]:
    offset, length = table
    data = _read(handle, offset, length)
    _, count, strings = struct.unpack(">HHH", data[:6])
    names = set()
    for position in range(count):
        platform, _, _, name_id, size, start = struct.unpack(">HHHHHH", data[6 + 12 * position:18 + 12 * position])
        if name_id not in (1, 4, 6, 16):
            continue
        raw = data[strings + start:strings + start + size]
        try:
            text = raw.decode("utf-16-be") if platform in (0, 3) else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        if text.strip():
            names.add(text.strip().casefold())
    return names


def font_catalog(directories: list[Path]) -> dict[str, list[dict]]:
    """name (casefolded family / full / PostScript name) -> [{path, face_index}] for every readable font file."""
    catalog: dict[str, list[dict]] = {}
    for directory in directories:
        for path in sorted(Path(directory).iterdir()) if Path(directory).is_dir() else []:
            if path.suffix.lower() not in FONT_SUFFIXES or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    for face_index, offset in enumerate(_faces(handle)):
                        tables = _table_directory(handle, offset)
                        if b"name" not in tables:
                            continue
                        for name in _names(handle, tables[b"name"]):
                            catalog.setdefault(name, []).append({"path": str(path), "face_index": face_index})
            except (OSError, ValueError, struct.error):
                continue        # an unreadable file is not a font this check can vouch for
    return catalog


def _cmap_covers(handle, table: tuple[int, int], codepoints: set[int]) -> set[int]:
    """The codepoints of ``codepoints`` the font maps to a glyph (cmap format 4 / 12, Unicode subtables)."""
    offset, length = table
    data = _read(handle, offset, length)
    _, count = struct.unpack(">HH", data[:4])
    subtables = []
    for position in range(count):
        platform, encoding, start = struct.unpack(">HHI", data[4 + 8 * position:12 + 8 * position])
        if (platform, encoding) in ((3, 10), (0, 4), (0, 6), (3, 1), (0, 3), (0, 0), (0, 1)):
            subtables.append(start)
    covered: set[int] = set()
    for start in subtables:
        fmt = struct.unpack(">H", data[start:start + 2])[0]
        if fmt == 12:
            groups = struct.unpack(">I", data[start + 12:start + 16])[0]
            for group in range(groups):
                low, high, _ = struct.unpack(">III", data[start + 16 + 12 * group:start + 28 + 12 * group])
                covered |= {point for point in codepoints if low <= point <= high}
        elif fmt == 4:
            segments = struct.unpack(">H", data[start + 6:start + 8])[0] // 2
            ends = struct.unpack(f">{segments}H", data[start + 14:start + 14 + 2 * segments])
            base = start + 16 + 2 * segments
            starts = struct.unpack(f">{segments}H", data[base:base + 2 * segments])
            deltas = struct.unpack(f">{segments}h", data[base + 2 * segments:base + 4 * segments])
            range_base = base + 4 * segments
            offsets = struct.unpack(f">{segments}H", data[range_base:range_base + 2 * segments])
            for point in codepoints - covered:
                if point > 0xFFFF:
                    continue
                for position in range(segments):
                    if starts[position] <= point <= ends[position]:
                        if offsets[position] == 0:
                            glyph = (point + deltas[position]) & 0xFFFF
                        else:
                            where = range_base + 2 * position + offsets[position] + 2 * (point - starts[position])
                            glyph = struct.unpack(">H", data[where:where + 2])[0] if where + 2 <= len(data) else 0
                            glyph = (glyph + deltas[position]) & 0xFFFF if glyph else 0
                        if glyph:
                            covered.add(point)
                        break
    return covered


def glyphs_missing(font: dict, characters: str) -> list[str]:
    """Characters of ``characters`` (whitespace and controls excluded) the font face has no glyph for."""
    wanted = {ord(char) for char in characters if not char.isspace() and ord(char) >= 0x20}
    if not wanted:
        return []
    with open(font["path"], "rb") as handle:
        offset = _faces(handle)[font["face_index"]]
        tables = _table_directory(handle, offset)
        if b"cmap" not in tables:
            return sorted(chr(point) for point in wanted)
        covered = _cmap_covers(handle, tables[b"cmap"], wanted)
    return sorted(chr(point) for point in wanted - covered)


# --------------------------------------------------------------------------- profiles (plan 7.5, fixed) and the font check

PROFILES: dict[str, dict] = {
    "subtitle_files_v1": {"kind": "subtitles", "copy": "revision subtitle bytes as committed", "include_media_default": False},
    "mp4_burn_sdr_v1": {"kind": "mp4_burn", "container": "mp4", "video": {"codec": "libx264", "crf": 18, "preset": "medium", "pix_fmt": "yuv420p"},
                        "audio": {"codec": "aac", "bitrate": "192k", "sample_rate": "kept", "channels": "kept"}, "movflags": "+faststart",
                        "subtitle_streams": 0, "odd_size": "pad right/bottom by 1 px black", "renderer": "libass (complete subtitle file, no force_style)"},
    "mkv_soft_v1": {"kind": "mkv_soft", "container": "matroska", "video": "copy", "audio": "copy", "subtitle": {"codec": "copy", "default": True, "forced": False,
                                                                                                             "language": "chi"},
                    "other_subtitles": "not copied", "font_attachments": "none (no font is confirmed as redistributable)"},
}
DEFAULT_PROFILE = {"subtitles": "subtitle_files_v1", "mp4_burn": "mp4_burn_sdr_v1", "mkv_soft": "mkv_soft_v1"}
MANIFEST_NAME = "manifest.json"
LOCATION_NAME = "location.json"


def profile_for(kind: str, profile_id: str | None) -> str:
    chosen = profile_id or DEFAULT_PROFILE[kind]
    if chosen not in PROFILES or PROFILES[chosen]["kind"] != kind:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"profile {chosen!r} is not a {kind} profile", details={"kind": kind, "profiles": sorted(PROFILES)})
    return chosen


def fonts_needed(view: dict) -> dict[str, str]:
    """font name -> the characters shown in it: style Fontname for the text of its cues, inline \\fn names listed as used."""
    styles = {style["name"]: style.get("fontname") for style in view.get("styles") or []}
    needed: dict[str, str] = {}
    for cue in view["cues"]:
        if cue["kind"] != "dialogue" or cue["drawing"] or not cue["text"].strip():
            continue
        name = styles.get(cue["style"]) or styles.get("Default")
        if name:
            needed[name] = needed.get(name, "") + cue["text"]
        for inline in (cue.get("notes") or {}).get("inline_fonts") or []:
            needed.setdefault(inline, "")
            needed[inline] += cue["text"]
    return needed


def check_fonts(view: dict, directories: list[Path], *, purpose: str) -> dict:
    """QC22 input: which needed fonts exist (explicit directories first, then the system folders) and which glyphs they lack."""
    catalog = font_catalog(directories)
    entries, missing_fonts, missing_glyphs = [], [], []
    for name, characters in sorted(fonts_needed(view).items()):
        matches = catalog.get(name.lstrip("@").casefold()) or []
        if not matches:
            missing_fonts.append(name)
            entries.append({"name": name, "found": False, "file": None, "face_index": None, "missing_characters": []})
            continue
        face = matches[0]
        lacking = glyphs_missing(face, characters)
        if lacking:
            missing_glyphs.append({"font": name, "characters": lacking})
        entries.append({"name": name, "found": True, "file": face["path"], "face_index": face["face_index"], "missing_characters": lacking})
    return {"purpose": purpose, "fonts": entries, "missing_fonts": missing_fonts, "missing_glyphs": missing_glyphs, "directories": [str(item) for item in directories]}


# --------------------------------------------------------------------------- the chosen input and format conversions (plan 9.5)

def _pending_for(destination: Path, export_id: str) -> Path:
    destination = Path(destination)
    if destination.exists():
        raise ContractError("OUTPUT_EXISTS", "the export destination already exists; exports never overwrite, choose a new directory", details={"path": str(destination)})
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.parent / f".{destination.name}.pending-{export_id[:8]}"
    pending.mkdir()
    return pending


def _revision_bytes(project_dir: Path, ref: dict) -> bytes:
    data = safe_join(project_dir, ref["path"]).read_bytes()
    if sha256_bytes(data) != ref["sha256"]:
        raise ContractError("ARTIFACT_CHANGED", "the committed subtitle file of this revision changed on disk", details={"path": ref["path"], "rule_id": "QC04_IDENTITY"})
    return data


def output_stem(revision: dict, fmt: str) -> str:
    """User-facing file names: a human file keeps the name it was imported with, a machine revision its own names."""
    if revision["kind"] == "machine":
        return "machine.zh"
    imported = ((revision["manifest"].get("extensions") or {}).get("imported_file") or {}).get("name") or f"original.{fmt}"
    stem = Path(imported).stem.strip() or "original"
    return "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in stem)


def choose_formats(revision: dict, kind: str, fmt: str | None) -> list[str]:
    available = sorted(revision["subtitle_files"])
    if fmt is not None:
        if fmt not in available:
            raise ContractError("NEEDS_SELECTION", f"this revision has no {fmt} file; it has {', '.join(available)} (use --convert-to for an explicit conversion)",
                                details={"available": available})
        return [fmt]
    if kind == "subtitles" or len(available) == 1:
        return available
    return ["ass"]          # a machine revision offers both; a video takes the ASS (its styles and layers) unless --format says otherwise


def srt_from_ass(view: dict, *, accept_omitted_drawings: bool) -> tuple[bytes, dict]:
    """Explicit lossy conversion: display text and times of each Dialogue, tags removed, line breaks kept (plan 9.5)."""
    kept, omitted = [], []
    for cue in view["cues"]:
        if cue["kind"] != "dialogue":
            omitted.append({"line": cue["line"], "reason": "comment"})
        elif cue["drawing"]:
            omitted.append({"line": cue["line"], "reason": "drawing"})
        elif not cue["text"].strip():
            omitted.append({"line": cue["line"], "reason": "no_visible_text"})
        else:
            kept.append((cue["start_ms"], cue["end_ms"], cue["text"]))
    drawings = [item for item in omitted if item["reason"] == "drawing"]
    if drawings and not accept_omitted_drawings:
        raise ContractError("NEEDS_SELECTION", f"{len(drawings)} drawing event(s) cannot become SRT text and would be left out; confirm with --accept-omitted-drawings",
                            details={"omitted": drawings[:50], "count": len(drawings)})
    return subtitles.render_srt_bytes(kept), {"lossy": True, "kept_cues": len(kept), "omitted_events": omitted, "lost": ["styles", "positions", "override tags", "layers"]}


def ass_from_srt(view: dict) -> tuple[bytes, dict]:
    """Human SRT -> ASS with the fixed base style profile of plan 7.5; marked derived, the SRT stays as it is."""
    cues = [{"index": position, "cue_id": cue["cue_id"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"], "text": cue["text"], "template_event": None}
            for position, cue in enumerate((cue for cue in view["cues"] if cue["kind"] == "dialogue"), start=1)]
    data, _ = subtitles.render_ass(cues, subtitles.base_template())
    return data, {"lossy": False, "style_profile": subtitles.BASE_STYLE_ID, "cues": len(cues)}


def _file_status(revision: dict, fmt: str) -> dict:
    confirmation = revision["confirmations"][fmt]
    return {"human_status": confirmation["human_status"], "confirmed_scopes": confirmation["confirmed_scopes"], "attestation_ids": confirmation.get("attestation_ids", [])}


def _issue_counts(report: dict) -> dict:
    return {severity: sum(1 for issue in report["issues"] if issue["severity"] == severity) for severity in ("ERROR", "WARNING", "INFO")}


# --------------------------------------------------------------------------- subtitle_files_v1

def _export_subtitles(job: dict, request: dict, pending: Path, formats: list[str]) -> dict:
    revision, project_dir = job["revision"], Path(job["project_dir"])
    files, font_checks, conversions = [], [], []
    for fmt in formats:
        data = _revision_bytes(project_dir, revision["subtitle_files"][fmt])
        name = f"{output_stem(revision, fmt)}.{fmt}"
        atomic_write_bytes(pending / name, data)
        files.append({"file": name, "format": fmt, "derived": False, "lossy": False, "sha256": sha256_bytes(data), **_file_status(revision, fmt)})
        view = subtitles.parse_view(data, fmt, encoding=revision["manifest"].get("encoding") if fmt == revision["manifest"].get("format") else None)
        if fmt == "ass":
            font_checks.append(check_fonts(view, job["font_dirs"], purpose="subtitles"))
        target = job.get("convert_to")
        if target and target != fmt:
            converted, detail = srt_from_ass(view, accept_omitted_drawings=job.get("accept_omitted_drawings", False)) if target == "srt" else ass_from_srt(view)
            derived_name = f"{output_stem(revision, fmt)}.derived.{target}"
            atomic_write_bytes(pending / derived_name, converted)
            # a derived file is a new file: no confirmation carries over, the original ones are only referenced (plan 9.5)
            files.append({"file": derived_name, "format": target, "derived": True, "lossy": detail["lossy"], "sha256": sha256_bytes(converted), "derived_from": name,
                          "derived_from_sha256": sha256_bytes(data), "human_status": "UNREVIEWED", "confirmed_scopes": [], "attestation_ids": [],
                          "referenced_confirmations": _file_status(revision, fmt)})
            conversions.append({"from": name, "to": derived_name, **detail})
    if request["include_media"]:
        record = job["media_record"]
        source = Path(media.verify_source(record, project_dir)["absolute_path"])
        name = source.name
        shutil.copyfile(source, pending / name)
        if sha256_file(pending / name) != record["source_sha256"]:
            raise ContractError("MEDIA_CHANGED", "the media copy does not match the registered media", details={"media_id": record["media_id"]})
        files.append({"file": name, "format": "media", "derived": False, "lossy": False, "sha256": record["source_sha256"]})
    return {"files": files, "font_checks": font_checks, "conversions": conversions, "tool_refs": [], "validation": {
        "copied_bytes_verified": True, "files": len(files)}, "subtitle_input": files[0]["file"]}


# --------------------------------------------------------------------------- mp4_burn_sdr_v1 / mkv_soft_v1

def canonical_offset(record: dict) -> Fraction:
    """Timestamp (seconds) of the first presented video frame, i.e. the canonical zero of every subtitle time (plan 6.3).

    Video exports run FFmpeg with ``-copyts``: filters and muxers then see the source timestamps unchanged, so the
    subtitle is placed by this one offset instead of FFmpeg's own start-time and negative-timestamp shifts."""
    clock = record["clock"]
    return Fraction(clock["origin_pts"] * clock["origin_time_base_num"], clock["origin_time_base_den"])


def _seconds(value: Fraction) -> str:
    return f"{float(value):.6f}"


def _work_dir(job: dict, export_id: str) -> Path:
    """Isolated ASCII working directory: the filter string only ever names ``input.ass`` and ``fonts`` inside it (plan 7.5)."""
    directory = ensure_ascii_root(job["ascii_temp_root"]) / "export" / export_id.replace("-", "")[:16]
    try:
        directory.mkdir(parents=True)
    except OSError as exc:
        raise ContractError("DATA_ROOT_UNWRITABLE", f"cannot create the ASCII working directory: {exc}", details={"path": str(directory)}) from exc
    return directory


def _copy_fonts(check: dict, work: Path) -> list[dict]:
    (work / "fonts").mkdir()
    copied, seen = [], {}
    for entry in check["fonts"]:
        if not entry["found"]:
            continue
        if entry["file"] not in seen:
            seen[entry["file"]] = f"font{len(seen) + 1:02d}{Path(entry['file']).suffix.lower()}"
            shutil.copyfile(entry["file"], work / "fonts" / seen[entry["file"]])
        copied.append({"name": entry["name"], "file_name": Path(entry["file"]).name, "sha256": sha256_file(work / "fonts" / seen[entry["file"]]), "face_index": entry["face_index"],
                       "attached": False})
    return copied


def _require_fonts(check: dict, revision_id: str) -> None:
    if check["missing_fonts"] or check["missing_glyphs"]:
        issues = quality.export_issues(revision_id, font_check=check)
        raise ContractError("FONT_MISSING", "fonts or glyphs needed by the subtitle are missing; no video is made with a silent fallback font",
                            details={"rule_id": "QC22_FONT", "missing_fonts": check["missing_fonts"], "missing_glyphs": check["missing_glyphs"],
                                     "issues": [{"message": issue["message"], "recommended_action": issue["recommended_action"]} for issue in issues]})


def _display_size(video: dict) -> tuple[int | None, int | None]:
    width, height = video.get("width"), video.get("height")
    if (video.get("rotation_degrees") or 0) % 180:
        return height, width
    return width, height


def _export_video(job: dict, request: dict, pending: Path, fmt: str) -> dict:
    revision, project_dir, record, runtime = job["revision"], Path(job["project_dir"]), job["media_record"], job["runtime"]
    if record is None or not record["clock"]["verified"]:
        raise ContractError("MEDIA_CLOCK_UNKNOWN", "the media clock is not verified; a video cannot be made against the subtitle times", details={"media_id": request["media_id"]})
    if request["kind"] == "mp4_burn" and record["extensions"].get("unsupported_profile"):
        raise ContractError("MEDIA_PROFILE_UNSUPPORTED", "this source (HDR / Dolby Vision / stereo / multi-program) is not burned by the SDR profile; the subtitle files can still be exported",
                            details={"reasons": record["extensions"]["unsupported_profile"]})
    ffmpeg = media.resource_executable(runtime.ffmpeg, ("ffmpeg.exe", "ffmpeg")) if runtime.ffmpeg else None
    if ffmpeg is None:
        raise ContractError("RESOURCE_MISSING", "ffmpeg is required for a video export but is not registered", details={"role": "ffmpeg"})
    source = Path(media.verify_source(record, project_dir)["absolute_path"])
    data = _revision_bytes(project_dir, revision["subtitle_files"][fmt])
    view = _view_of(revision, fmt, data)
    stem = output_stem(revision, fmt)
    atomic_write_bytes(pending / "subtitle_input" / f"{stem}.{fmt}", data)
    offset = canonical_offset(record)
    video_index, audio_index = record["selected_video_stream"], request["selected_audio_stream"]
    audio_map = ["-map", f"0:{audio_index}"] if audio_index is not None else []
    work = _work_dir(job, request["export_id"])
    try:
        details: dict[str, Any] = {"canonical_offset_seconds": _seconds(offset), "work_dir_kind": "ascii_temp_root/export"}
        if request["kind"] == "mp4_burn":
            render, render_kind = (data, "complete_ass_bytes") if fmt == "ass" else (ass_from_srt(view)[0], "derived_ass_from_srt")
            render_view = view if fmt == "ass" else subtitles.parse_view(render, "ass")
            fonts = check_fonts(render_view, job["font_dirs"], purpose="video")
            _require_fonts(fonts, request["revision_id"])
            (work / "input.ass").write_bytes(render)
            font_files = _copy_fonts(fonts, work)
            filters = [f"setpts=PTS-({offset.numerator}/{offset.denominator})/TB"] if offset else []
            filters.append("subtitles=filename=input.ass:fontsdir=fonts")
            if offset:
                filters.append(f"setpts=PTS+({offset.numerator}/{offset.denominator})/TB")
            width, height = _display_size(media.stream_by_index(record["streams"], video_index))
            padded = bool(width and height and (width % 2 or height % 2))
            if padded:
                filters.append("pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:x=0:y=0:color=black")
            output = pending / f"{stem}.burn.mp4"
            argv = ["-hide_banner", "-nostdin", "-n", "-copyts", "-i", str(source), "-map", f"0:{video_index}", *audio_map, "-vf", ",".join(filters),
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                    *(["-c:a", "aac", "-b:a", "192k"] if audio_index is not None else ["-an"]), "-sn", "-dn", "-movflags", "+faststart",
                    "-progress", "pipe:1", "-nostats", str(output)]
            details.update({"render_input": render_kind, "render_input_sha256": sha256_bytes(render), "filters": filters, "padded_to_even": padded})
            if render_kind != "complete_ass_bytes":
                atomic_write_bytes(pending / "subtitle_input" / f"{stem}.render.ass", render)
        else:
            fonts = check_fonts(view, job["font_dirs"], purpose="subtitles") if fmt == "ass" else {"purpose": "subtitles", "fonts": [], "missing_fonts": [], "missing_glyphs": [], "directories": []}
            font_files = []
            (work / f"input.{fmt}").write_bytes(data)
            output = pending / f"{stem}.soft.mkv"
            argv = ["-hide_banner", "-nostdin", "-n", "-copyts", "-i", str(source), *(["-itsoffset", _seconds(offset)] if offset else []), "-i", f"input.{fmt}",
                    "-map", f"0:{video_index}", *audio_map, "-map", "1:0", "-c", "copy", "-metadata:s:s:0", "language=chi", "-disposition:s:0", "default",
                    "-progress", "pipe:1", "-nostats", str(output)]
        media.run_tool(ffmpeg, argv, log_dir=runtime.log_dir, label=f"ffmpeg-{request['kind']}", runtime=runtime, cwd=work)
        validation = validate_video(output, request, record, view, fmt, offset, runtime, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    files = [{"file": output.name, "format": request["kind"], "derived": True, "lossy": request["kind"] == "mp4_burn", "sha256": sha256_file(output),
              "subtitle_from": f"{stem}.{fmt}", **_file_status(revision, fmt)}]
    return {"files": files, "font_checks": [fonts], "font_files": font_files, "conversions": [], "validation": validation, "details": details,
            "tool_refs": [runtime.ffmpeg["resource_ref"], runtime.ffprobe["resource_ref"]], "subtitle_input": f"subtitle_input/{stem}.{fmt}"}


# --------------------------------------------------------------------------- validation of a video output (plan 7.5) and the subtitle round trip of an MKV

def _cue_key(cue: dict, fmt: str, shift_ms: int) -> tuple:
    text = cue["fields"].get("Text", cue["text"]) if fmt == "ass" else cue["text"]
    return (cue["start_ms"] - shift_ms, cue["end_ms"] - shift_ms, text)


def validate_video(output: Path, request: dict, record: dict, view: dict, fmt: str, offset: Fraction, runtime: media.MediaRuntime, work: Path) -> dict:
    kind = request["kind"]
    _, parsed = media.probe_format_streams(output, runtime, label=f"ffprobe-{kind}-output")
    streams = [item for item in parsed.get("streams") or [] if isinstance(item, dict)]
    by_type = {name: [item for item in streams if item.get("codec_type") == name] for name in ("video", "audio", "subtitle")}
    source_video = media.stream_by_index(record["streams"], record["selected_video_stream"])
    source_audio = media.stream_by_index(record["streams"], request["selected_audio_stream"]) if request["selected_audio_stream"] is not None else None
    problems = []
    expected = {"video": 1, "audio": 1 if source_audio else 0, "subtitle": 1 if kind == "mkv_soft" else 0}
    for name, count in expected.items():
        if len(by_type[name]) != count:
            problems.append({"check": f"{name}_streams", "expected": count, "observed": len(by_type[name])})
    video = by_type["video"][0] if by_type["video"] else {}
    if kind == "mp4_burn":
        if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
            problems.append({"check": "video_codec", "expected": "h264/yuv420p", "observed": f"{video.get('codec_name')}/{video.get('pix_fmt')}"})
        if by_type["audio"] and by_type["audio"][0].get("codec_name") != "aac":
            problems.append({"check": "audio_codec", "expected": "aac", "observed": by_type["audio"][0].get("codec_name")})
    else:
        if video.get("codec_name") != source_video.get("codec_name"):
            problems.append({"check": "video_copy", "expected": source_video.get("codec_name"), "observed": video.get("codec_name")})
        if source_audio and by_type["audio"] and by_type["audio"][0].get("codec_name") != source_audio.get("codec_name"):
            problems.append({"check": "audio_copy", "expected": source_audio.get("codec_name"), "observed": by_type["audio"][0].get("codec_name")})
        subtitle = by_type["subtitle"][0] if by_type["subtitle"] else {}
        disposition, tags = subtitle.get("disposition") or {}, subtitle.get("tags") or {}
        if subtitle and (subtitle.get("codec_name") != ("ass" if fmt == "ass" else "subrip") or disposition.get("default") != 1 or disposition.get("forced") != 0
                         or tags.get("language") != "chi"):
            problems.append({"check": "subtitle_track", "observed": {"codec": subtitle.get("codec_name"), "default": disposition.get("default"),
                                                                     "forced": disposition.get("forced"), "language": tags.get("language")}})
    # duration: the end of the last presented frame against the source, within max(largest frame interval, 50 ms)
    duration_ms = tolerance_ms = video_start = None
    if video:
        tb = media.parse_rational(video.get("time_base"))
        _, packets = media.probe_packets(output, int(video["index"]), runtime, f"ffprobe-{kind}-packets")
        timeline = media.analyze_video_packets(packets)
        if timeline["status"] != "ok" or tb is None:
            problems.append({"check": "output_timeline", "observed": timeline.get("problem")})
        else:
            video_start = timeline["origin_pts"] * tb
            end, _ = media.media_end_seconds(timeline, video, parsed.get("format") or {}, tb)
            duration_ms = media.round_half_up(1000 * (end - timeline["origin_pts"] * tb)) if end is not None else None
            timing = record["extensions"].get("frame_timing") or {}
            source_tb = Fraction(source_video["time_base"]["num"], source_video["time_base"]["den"])
            largest = media.round_half_up(1000 * timing["max_interval_ticks"] * source_tb) if timing.get("max_interval_ticks") else 0
            tolerance_ms = max(largest, 50)
            if duration_ms is None or abs(duration_ms - record["duration_ms"]) > tolerance_ms:
                problems.append({"check": "duration", "expected_ms": record["duration_ms"], "observed_ms": duration_ms, "tolerance_ms": tolerance_ms})
    roundtrip = None
    if kind == "mkv_soft" and by_type["subtitle"] and video_start is not None:
        extracted = work / f"extracted.{fmt}"
        media.run_tool(media.resource_executable(runtime.ffmpeg, ("ffmpeg.exe", "ffmpeg")), ["-hide_banner", "-nostdin", "-n", "-copyts", "-i", str(output), "-map", "0:s:0", "-c", "copy", extracted.name],
                       log_dir=runtime.log_dir, label="ffmpeg-extract-subtitle", runtime=runtime, cwd=work)
        back = subtitles.parse_view(extracted.read_bytes(), fmt)
        shift = media.round_half_up(1000 * video_start)        # the track is read back against the output's own first video frame
        slack = 10 if fmt == "ass" else 1
        wanted = [_cue_key(cue, fmt, 0) for cue in view["cues"] if cue["kind"] == "dialogue"]
        got = [_cue_key(cue, fmt, shift) for cue in back["cues"] if cue["kind"] == "dialogue"]
        mismatched = [index for index, (left, right) in enumerate(zip(wanted, got))
                      if left[2] != right[2] or abs(left[0] - right[0]) > slack or abs(left[1] - right[1]) > slack]
        roundtrip = {"input_cues": len(wanted), "extracted_cues": len(got), "mismatched": len(mismatched) + abs(len(wanted) - len(got)), "time_slack_ms": slack}
        if roundtrip["mismatched"]:
            problems.append({"check": "subtitle_roundtrip", "observed": roundtrip, "first_mismatches": mismatched[:5]})
    summary = {"streams": {name: [{"index": item.get("index"), "codec": item.get("codec_name")} for item in items] for name, items in by_type.items()},
               "duration_ms": duration_ms, "source_duration_ms": record["duration_ms"], "tolerance_ms": tolerance_ms, "subtitle_roundtrip": roundtrip, "problems": problems}
    if problems:
        raise ContractError("EXPORT_VALIDATION_FAILED", "the exported video does not meet its profile; it was not published", details=summary)
    return summary


# --------------------------------------------------------------------------- one export, published once

def _view_of(revision: dict, fmt: str, data: bytes) -> dict:
    manifest = revision["manifest"]
    encoding = manifest.get("encoding") or (manifest.get("extensions") or {}).get("encoding") if revision["kind"] != "machine" else None
    return subtitles.parse_view(data, fmt, encoding=encoding if encoding not in (None, "utf-8", "utf-8-sig") else None)


def run_export(job: dict) -> dict:
    """``job``: request (ExportRequest), project_dir, revision (ResolvedRevision), quality_report, media_record, runtime (video only),
    format, convert_to, accept_omitted_drawings, font_dirs, ascii_temp_root. Returns the committed ExportManifest."""
    request = require_record("ExportRequest", job["request"])
    revision, report, kind = job["revision"], job["quality_report"], request["kind"]
    if request["revision_id"] != revision["revision_id"] or request["project_id"] != revision["project_id"]:
        raise ContractError("SCHEMA_INVALID", "the export request names another revision than the one resolved")
    if report["structural_status"] != "valid":
        raise ContractError("SUBTITLE_INVALID", "the revision has structural errors; it cannot be exported as a deliverable", details={"rule_id": "QC01_PARSE"})
    if request["media_id"] != revision["media_id"]:
        issues = quality.export_issues(request["revision_id"], media_record={"media_id": revision["media_id"]}, target_media_id=request["media_id"])
        raise ContractError("MEDIA_CHANGED", "the subtitle belongs to another media version; register it against this media first",
                            details={"rule_id": "QC21_MEDIA_BINDING", "bound_media_id": revision["media_id"], "target_media_id": request["media_id"],
                                     "message": issues[0]["message"] if issues else None})
    convert_to = job.get("convert_to")
    if convert_to and kind != "subtitles":
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "--convert-to only applies to subtitle exports")
    if convert_to and revision["kind"] == "machine":
        raise ContractError("NEEDS_SELECTION", "a machine revision already has both SRT and ASS from the same segments; choose one with --format")
    if request["include_media"] and kind != "subtitles":
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "--include-media only applies to subtitle exports")
    formats = choose_formats(revision, kind, job.get("format"))
    if convert_to and convert_to in formats and len(formats) == 1:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"the revision file already is {convert_to}; nothing to convert")
    destination = Path(request["destination_root"])
    pending = _pending_for(destination, request["export_id"])
    try:
        result = _export_subtitles(job, request, pending, formats) if kind == "subtitles" else _export_video(job, request, pending, formats[0])
        quality_bytes = safe_join(Path(job["project_dir"]), f"{revision['directory']}/quality.json").read_bytes()
        atomic_write_bytes(pending / "quality.json", quality_bytes)
        export_issues = [issue for check in result["font_checks"] for issue in quality.export_issues(request["revision_id"], font_check=check)]
        snapshot = {"revision_quality_sha256": sha256_bytes(quality_bytes), "structural_status": report["structural_status"], "issue_counts": _issue_counts(report),
                    "files": [{key: value for key, value in item.items() if key != "sha256"} for item in result["files"]], "export_issues": export_issues,
                    "note": "warnings do not block an export; only a user confirmation of these exact bytes counts as reviewed"}
        names = [item["file"] for item in result["files"]]
        manifest = require_record("ExportManifest", {
            **request, "subtitle_input_ref": artifact_ref(pending, pending / result["subtitle_input"]),
            "fonts": [entry for check in result["font_checks"] for entry in check["fonts"]] + [{"copied_for_render": item} for item in result.get("font_files", [])],
            "tool_refs": result["tool_refs"], "profile_snapshot": {"profile_id": request["profile_id"], **PROFILES[request["profile_id"]], "details": result.get("details", {}),
                                                                   "conversions": result["conversions"]},
            "output_refs": [artifact_ref(pending, pending / name) for name in names], "validation": result["validation"], "quality_snapshot": snapshot,
            "status": "COMMITTED", "completed_at": utc_now()})
        atomic_write_text(pending / MANIFEST_NAME, render_json(manifest))
    except BaseException as exc:
        # nothing is published: the candidate stays next to the destination for diagnosis, marked as not committed
        try:
            atomic_write_text(pending / "candidate.json", render_json({"status": "NOT_COMMITTED", "export_id": request["export_id"], "created_at": utc_now(),
                                                                         "error": exc.to_dict() if isinstance(exc, ContractError) else {"code": type(exc).__name__}}))
        except OSError:
            pass
        if isinstance(exc, ContractError):
            exc.details = {**(exc.details or {}), "candidate": str(pending)}
        raise
    os.replace(pending, destination)
    _record(job, request, manifest, destination)
    return manifest


def _record(job: dict, request: dict, manifest: dict, destination: Path) -> None:
    """The project keeps where each export went (plan 4.2 ``exports/<export_id>/``); the export directory itself holds the manifest."""
    from upgrade.runner import update_project
    directory = Path(job["project_dir"]) / "exports" / request["export_id"]
    directory.mkdir(parents=True)
    atomic_write_text(directory / LOCATION_NAME, render_json({"schema_version": SCHEMA_VERSION, "export_id": request["export_id"], "destination": str(destination),
                                                              "manifest_sha256": sha256_file(destination / MANIFEST_NAME), "kind": request["kind"],
                                                              "revision_id": request["revision_id"], "completed_at": manifest["completed_at"]}))
    update_project(job["data_root"], request["project_id"], lambda record: record["export_ids"].append(request["export_id"]))
