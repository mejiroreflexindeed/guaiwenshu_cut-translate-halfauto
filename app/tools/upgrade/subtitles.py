#!/usr/bin/env python3
"""Machine subtitle files (plan 1.1, sections 6.7, 7.5, 9.5; W04).

Fixed machine formats: SRT = UTF-8 BOM, CRLF, numbered from 1, milliseconds; ASS = UTF-8 BOM, CRLF, centiseconds
rounded to the nearest 10 ms (positive half up). A cue whose rounded ASS duration would not be positive is reported as
TIME_PRECISION_COLLAPSE and never lengthened; the original milliseconds of every cue are kept in a sidecar.

Styles: the machine timeline's own ASS header is reused when the run has one (the OCR output), otherwise the fixed base
style of plan 7.5 (PlayRes 1920x1080, Microsoft YaHei 80, white text with a black outline, Outline 2, Shadow 2,
Alignment 2, MarginL/R 10, MarginV 200, ScaleX/Y 100, WrapStyle 0; the remaining fields follow the audited Default
row). Human ASS files are never rewritten here; this module only produces machine output.
"""

from __future__ import annotations

from pathlib import Path
import codecs
import re

from upgrade.contracts import SCHEMA_VERSION, ContractError, require_record, sha256_bytes

MACHINE_FORMAT_VERSION = 1
ASS_RESOLUTION_MS = 10
EVENT_FIELDS = ("Layer", "Start", "End", "Style", "Name", "MarginL", "MarginR", "MarginV", "Effect", "Text")
BASE_STYLE_ID = "base_v1"
BASE_HEADER = (
    "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0", "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    "Style: Default,Microsoft YaHei,80,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,200,1",
)
DEFAULT_EVENT = {"Layer": "0", "Style": "Default", "Name": "", "MarginL": "0", "MarginR": "0", "MarginV": "0", "Effect": ""}


# --------------------------------------------------------------------------- time formatting

def srt_time(ms: int) -> str:
    if ms < 0:
        raise ContractError("SUBTITLE_INVALID", "negative time cannot be written", details={"ms": ms})
    hours, rest = divmod(int(ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, milli = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milli:03d}"


def ass_centiseconds(ms: int) -> int:
    """Nearest 10 ms, positive half rounded up (plan 6.7)."""
    return (int(ms) + ASS_RESOLUTION_MS // 2) // ASS_RESOLUTION_MS


def ass_time(centiseconds: int) -> str:
    hours, rest = divmod(int(centiseconds), 360_000)
    minutes, rest = divmod(rest, 6_000)
    seconds, centi = divmod(rest, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centi:02d}"


# --------------------------------------------------------------------------- SRT

def srt_lines(text: str) -> list[str]:
    """Cue text lines as SRT can carry them: a blank line would end the block, so empty lines are not representable."""
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def render_srt_bytes(cues: list[tuple[int, int, str]]) -> bytes:
    blocks = []
    for number, (start_ms, end_ms, text) in enumerate(cues, start=1):
        if end_ms <= start_ms:
            raise ContractError("SUBTITLE_INVALID", f"cue {number} has a non-positive duration", details={"index": number, "start_ms": start_ms, "end_ms": end_ms})
        blocks.append("\r\n".join([str(number), f"{srt_time(start_ms)} --> {srt_time(end_ms)}", *srt_lines(text)]) + "\r\n")
    return ("﻿" + "\r\n".join(blocks)).encode("utf-8")


# --------------------------------------------------------------------------- ASS

def ass_escape(text: str) -> str:
    from subtitle_axis_align import ass_escape as legacy_escape
    return legacy_escape(text.replace("\r\n", "\n").replace("\r", "\n"))


def parse_template(template_text: str) -> dict:
    """Split a machine timeline ASS into the parts the machine output reuses: header sections before [Events], the
    event field order, the Dialogue events (field dict per event, in file order) and any sections after [Events]."""
    lines = template_text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header: list[str] = []
    trailer: list[str] = []
    fields: list[str] = []
    events: list[dict[str, str]] = []
    section = None
    seen_events = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            if section == "[events]":
                seen_events = True
                continue
        if section != "[events]":
            (trailer if seen_events else header).append(line)
            continue
        if stripped.lower().startswith("format:"):
            fields = [part.strip() for part in stripped.split(":", 1)[1].split(",")]
            continue
        if stripped.lower().startswith("dialogue:") and fields:
            parts = line.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
            if len(parts) == len(fields):
                events.append(dict(zip(fields, parts)))
    while header and not header[-1].strip():
        header.pop()
    styles = [line.split(":", 1)[1].split(",", 1)[0].strip() for line in header if line.strip().lower().startswith("style:")]
    if not styles or not any(line.strip().lower() in ("[v4+ styles]", "[v4 styles]") for line in header):
        raise ContractError("SUBTITLE_INVALID", "the ASS template has no style table; the machine ASS cannot inherit its style", details={"styles": styles})
    return {"header": header, "fields": fields, "events": events, "trailer": [line for line in trailer if line.strip()], "styles": styles}


def base_template() -> dict:
    return {"header": list(BASE_HEADER), "fields": list(EVENT_FIELDS), "events": [], "trailer": [], "styles": ["Default"]}


def render_ass(cues: list[dict], template: dict) -> tuple[bytes, list[dict]]:
    """``cues``: [{index, cue_id, start_ms, end_ms, text, template_event (int|None)}] -> (bytes, timing sidecar rows).

    Event fields other than Start/End/Text come from the associated template event (the OCR event the cue was built
    from) or from the defaults with the template's first style when no event is associated.
    """
    default_style = "Default" if "Default" in template["styles"] else template["styles"][0]
    collapsed = []
    rows = []
    dialogues = []
    for cue in cues:
        start_cs, end_cs = ass_centiseconds(cue["start_ms"]), ass_centiseconds(cue["end_ms"])
        rows.append({"index": cue["index"], "cue_id": cue["cue_id"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"],
                     "ass_start_cs": start_cs, "ass_end_cs": end_cs, "template_event": cue.get("template_event")})
        if end_cs <= start_cs:
            collapsed.append({"cue_id": cue["cue_id"], "index": cue["index"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"]})
            continue
        source = template["events"][cue["template_event"]] if cue.get("template_event") is not None and cue["template_event"] < len(template["events"]) else {}
        values = {**DEFAULT_EVENT, "Style": default_style}
        for name in ("Layer", "Style", "Name", "MarginL", "MarginR", "MarginV", "Effect"):
            if str(source.get(name, "")).strip():
                values[name] = source[name].strip()
        values.update({"Start": ass_time(start_cs), "End": ass_time(end_cs), "Text": ass_escape(cue["text"])})
        dialogues.append("Dialogue: " + ",".join(values[name] for name in EVENT_FIELDS))
    if collapsed:
        raise ContractError("TIME_PRECISION_COLLAPSE", f"{len(collapsed)} cue(s) would have no duration at ASS precision; the cue is not lengthened",
                            details={"cues": collapsed, "resolution_ms": ASS_RESOLUTION_MS})
    lines = [*template["header"], "", "[Events]", "Format: " + ", ".join(EVENT_FIELDS), *dialogues]
    if template["trailer"]:
        lines += ["", *template["trailer"]]
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8"), rows


# --------------------------------------------------------------------------- read-back checks (QC01 / QC02 / QC06 on the files just written)

def parse_srt_file(path: Path) -> list:
    from subtitle_axis_align import parse_srt
    return parse_srt(Path(path))


def parse_ass_file(path: Path) -> list:
    from subtitle_axis_align import parse_ass
    return parse_ass(Path(path)).cues


def verify_written(path: Path, kind: str, expected: list[dict], *, duration_ms: int | None) -> dict:
    """Re-read a machine file with the legacy parser and compare it with what should have been written.

    ``expected``: [{index, start_ms, end_ms, text}]. ASS times are compared at 10 ms. Returns a small report; raises
    SUBTITLE_INVALID on any structural difference (QC01 parse, QC02 time, QC03 bounds, QC06 order).
    """
    from subtitle_axis_align import normalize_text
    raw = Path(path).read_bytes()
    problems: list[str] = []
    if not raw.startswith(b"\xef\xbb\xbf"):
        problems.append("missing UTF-8 BOM")
    text = raw.decode("utf-8")
    if "\n" in text.replace("\r\n", ""):
        problems.append("bare LF line ending")
    cues = parse_srt_file(path) if kind == "srt" else parse_ass_file(path)
    if len(cues) != len(expected):
        problems.append(f"parsed {len(cues)} cue(s), expected {len(expected)}")
    if kind == "srt":
        numbers = [int(block.split("\r\n", 1)[0]) for block in text.lstrip("﻿").split("\r\n\r\n") if block.strip()]
        if numbers != list(range(1, len(expected) + 1)):
            problems.append("SRT numbering is not 1..n")
    tolerance = 0 if kind == "srt" else ASS_RESOLUTION_MS
    last_start = -1
    for cue, want in zip(cues, expected):
        want_start, want_end = (want["start_ms"], want["end_ms"]) if kind == "srt" else (ass_centiseconds(want["start_ms"]) * 10, ass_centiseconds(want["end_ms"]) * 10)
        if (cue.start, cue.end) != (want_start, want_end):
            problems.append(f"cue {want['index']}: time {cue.start}-{cue.end} differs from {want_start}-{want_end}")
        if cue.start < 0 or cue.end <= cue.start:
            problems.append(f"cue {want['index']}: non-positive duration or negative start")
        if cue.start < last_start:
            problems.append(f"cue {want['index']}: order moves backwards")
        last_start = cue.start
        if normalize_text(cue.text) != normalize_text(want["text"]):
            problems.append(f"cue {want['index']}: text differs after read-back")
        if duration_ms is not None and cue.end > duration_ms + max(1, tolerance):
            problems.append(f"cue {want['index']}: ends {cue.end - duration_ms} ms after the media (QC03_BOUNDS)")
    if problems:
        raise ContractError("SUBTITLE_INVALID", f"the generated {kind.upper()} did not read back as written: {problems[0]}",
                            details={"kind": kind, "path": str(path), "problems": problems})
    return {"kind": kind, "cues": len(cues), "bytes": len(raw), "bom": True, "line_ending": "CRLF", "time_tolerance_ms": tolerance}


# --------------------------------------------------------------------------- human files: strict decoding and read-only views (plan 6.7, 9.2; W05)
#
# A human subtitle file is never rewritten. The functions below only read: the original bytes are decoded strictly and
# parsed into a SubtitleView that keeps every field as written and lists what could not be read (QC01 parse, QC02 time)
# with file line numbers. Nothing is repaired, reordered or normalized; the lenient legacy parsers above stay reserved
# for reading back machine output.

MAX_SUBTITLE_BYTES = 32 * 1024 * 1024
VIEW_VERSION = 1
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig", "utf-8"), (b"\xff\xfe", "utf-16-le", "utf-16-le"), (b"\xfe\xff", "utf-16-be", "utf-16-be"))
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_DIGITS = re.compile(r"[0-9]+")
_SRT_TIMING = re.compile(r"^\s*(-?)([0-9]+):([0-9]{2}):([0-9]{2})[,.]([0-9]{3})\s*-->\s*(-?)([0-9]+):([0-9]{2}):([0-9]{2})[,.]([0-9]{3})(?:\s+(.*))?$")
_ASS_TIME = re.compile(r"^([0-9]+):([0-9]{2}):([0-9]{2})\.([0-9]{2})$")
_ASS_SECTION = re.compile(r"^\s*\[(.+)\]\s*$")
_ASS_DRAWING = re.compile(r"p([0-9]+)\s*")
_STYLE_SECTIONS = ("v4+ styles", "v4 styles", "v4 styles+")


def subtitle_format(path) -> str:
    """The format of a human file by its extension; only .srt and .ass are subtitle files here."""
    suffix = Path(path).suffix.lower()
    if suffix not in (".srt", ".ass"):
        raise ContractError("SUBTITLE_INVALID", "only .srt and .ass subtitle files can be registered", details={"kind": "unsupported_format", "suffix": suffix})
    return suffix[1:]


def _undecodable(reason: str, **details) -> ContractError:
    return ContractError("SUBTITLE_INVALID", f"the subtitle file cannot be decoded: {reason}",
                         details={"rule_id": "QC01_PARSE", "kind": "unknown_encoding", "hint": "choose the file's encoding explicitly (--encoding CODEC)", **details})


def decode_subtitle(data: bytes, encoding: str | None = None) -> dict:
    """Strict decoding (plan 6.7): UTF-8 with or without BOM, UTF-16 LE/BE with BOM, or the one codec the user names.

    Returns {text, encoding, encoding_source}. errors='ignore'/'replace' are never used: a file that does not decode
    raises SUBTITLE_INVALID (QC01_PARSE) and the caller keeps the original bytes untouched.
    """
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ContractError("SUBTITLE_INVALID", "the file is larger than any subtitle file this program reads", details={"kind": "too_large", "bytes": len(data), "limit": MAX_SUBTITLE_BYTES})
    if encoding:
        try:
            name = codecs.lookup(encoding).name
            text = data.decode(name)
        except LookupError as exc:                      # unknown name, or a codec that is not a text encoding
            raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"unknown text encoding: {encoding}", details={"encoding": encoding}) from exc
        except (UnicodeError, ValueError) as exc:
            raise _undecodable(f"it is not valid {name}", encoding=name, error=str(exc)) from exc
        source = "user"
    else:
        for bom, name, codec in _BOMS:
            if data.startswith(bom):
                try:
                    text = data[len(bom):].decode(codec)
                except UnicodeError as exc:
                    raise _undecodable(f"it starts with a {name} byte order mark but is not valid {name}", encoding=name, error=str(exc)) from exc
                source = "bom"
                break
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeError as exc:
                raise _undecodable("it is not UTF-8 and has no UTF-16 byte order mark", error=str(exc)) from exc
            name, source = "utf-8", "utf-8"
    if text.startswith("﻿"):
        text = text[1:]
    if "\x00" in text:
        raise _undecodable("the decoded text contains NUL characters (a UTF-16/UTF-32 file without a usable byte order mark?)", encoding=name)
    return {"text": text, "encoding": name, "encoding_source": source}


def _split_lines(text: str) -> tuple[list[str], dict]:
    newlines = {"crlf": 0, "lf": 0, "cr": 0}
    lines: list[str] = []
    position = 0
    for match in _LINE_BREAK.finditer(text):
        lines.append(text[position:match.start()])
        newlines["crlf" if match.group() == "\r\n" else ("lf" if match.group() == "\n" else "cr")] += 1
        position = match.end()
    lines.append(text[position:])
    return lines, newlines


def _problem(problems: list[dict], rule_id: str, line: int, message: str, *, ordinal: int | None = None, observed=None) -> None:
    problems.append({"rule_id": rule_id, "severity": "ERROR", "line": line, "ordinal": ordinal, "message": message, "observed": observed})


def _clock(hours: str, minutes: str, seconds: str, fraction: str, unit_ms: int) -> int | None:
    if int(minutes) >= 60 or int(seconds) >= 60:
        return None
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(fraction) * unit_ms


def _srt_times(line: str) -> tuple[int | None, int | None, str | None, str | None]:
    """(start_ms, end_ms, extra, problem) of an SRT timing line; the values are None when they are not canonical."""
    match = _SRT_TIMING.match(line)
    if not match:
        return None, None, None, "时间行不是规范的 时:分:秒,毫秒 --> 时:分:秒,毫秒"
    sign_a, h_a, m_a, s_a, f_a, sign_b, h_b, m_b, s_b, f_b, extra = match.groups()
    if sign_a or sign_b:
        return None, None, extra, "时间为负数"
    start, end = _clock(h_a, m_a, s_a, f_a, 1), _clock(h_b, m_b, s_b, f_b, 1)
    if start is None or end is None:
        return None, None, extra, "时间的分或秒不在 00–59 之内"
    return start, end, extra, None


def _skip_block(lines: list[str], position: int) -> int:
    while position < len(lines) and lines[position].strip():
        position += 1
    return position


def parse_srt_view(text: str) -> dict:
    """Read-only view of SRT text: {cues, problems, newlines}. Numbering is display information only (plan 6.7)."""
    lines, newlines = _split_lines(text)
    cues: list[dict] = []
    problems: list[dict] = []
    position, total = 0, len(lines)
    while position < total:
        if not lines[position].strip():
            position += 1
            continue
        block_line = position + 1
        index_label = None
        if "-->" not in lines[position]:
            if _DIGITS.fullmatch(lines[position].strip()) and position + 1 < total and "-->" in lines[position + 1]:
                index_label = lines[position].strip()
                position += 1
            else:
                _problem(problems, "QC01_PARSE", block_line, "这一行不属于任何字幕块（缺少时间行，或字幕正文中间有空行）", observed=lines[position][:120])
                position = _skip_block(lines, position)
                continue
        timing_line = position + 1
        start_ms, end_ms, extra, time_problem = _srt_times(lines[position])
        position += 1
        text_lines: list[str] = []
        while position < total and lines[position].strip():
            if _SRT_TIMING.match(lines[position]):
                _problem(problems, "QC01_PARSE", position + 1, "字幕正文里出现了时间行：两个字幕块之间缺少空行", ordinal=len(cues), observed=lines[position][:120])
            text_lines.append(lines[position])
            position += 1
        ordinal = len(cues)
        if time_problem:
            _problem(problems, "QC02_TIME", timing_line, time_problem, ordinal=ordinal, observed=lines[timing_line - 1][:120])
        elif end_ms <= start_ms:
            _problem(problems, "QC02_TIME", timing_line, "结束时间不晚于开始时间", ordinal=ordinal, observed={"start_ms": start_ms, "end_ms": end_ms})
        cues.append({"ordinal": ordinal, "cue_id": f"cue-{ordinal + 1:06d}", "kind": "dialogue", "line": block_line, "start_ms": start_ms, "end_ms": end_ms,
                     "text": "\n".join(text_lines), "fields": {}, "layer": None, "style": None, "name": None, "drawing": False,
                     "notes": {"index_label": index_label, "timing_extra": extra}})
    return {"cues": cues, "problems": problems, "newlines": newlines, "styles": [], "script_info": {}, "sections": [], "extensions": {"lines": total}}


def ass_display_text(raw: str, wrap_style: int = 0) -> dict:
    """What an ASS Text field shows: override blocks removed, \\N as a line break, drawing commands left out.

    Returns {text, drawing, inline_fonts, reset_styles}. \\n is a line break only under WrapStyle 2 (otherwise a space),
    \\h is a hard space. An unclosed '{' is literal text, as libass shows it.
    """
    out: list[str] = []
    fonts: list[str] = []
    resets: list[str] = []
    drawing = drawing_content = False
    position, length = 0, len(raw)
    while position < length:
        char = raw[position]
        if char == "{":
            close = raw.find("}", position + 1)
            if close != -1:
                for tag in raw[position + 1:close].split("\\")[1:]:
                    mode = _ASS_DRAWING.fullmatch(tag)
                    if mode:
                        drawing = int(mode.group(1)) > 0
                    elif tag.startswith("fn"):
                        font = tag[2:].strip()
                        if font.count(")") > font.count("("):
                            font = font[:font.rfind(")")].strip()
                        font = font.lstrip("@")
                        if font and font not in fonts:
                            fonts.append(font)
                    elif tag.startswith("r") and not tag.startswith("rnd"):
                        style = tag[1:].strip()
                        if style and style not in resets:
                            resets.append(style)
                position = close + 1
                continue
        if char == "\\" and position + 1 < length and raw[position + 1] in "Nnh":
            code = raw[position + 1]
            if not drawing:
                out.append("\n" if code == "N" or (code == "n" and wrap_style == 2) else (" " if code == "n" else "\u00a0"))
            position += 2
            continue
        if drawing:
            drawing_content = drawing_content or not char.isspace()
        else:
            out.append(char)
        position += 1
    return {"text": "".join(out), "drawing": drawing_content, "inline_fonts": fonts, "reset_styles": resets}


def _ass_time_ms(value: str) -> int | None:
    match = _ASS_TIME.match(value)
    return _clock(*match.groups(), 10) if match else None


def parse_ass_view(text: str) -> dict:
    """Read-only view of ASS text (plan 6.7): the [Events] Format order is honoured, every field is kept by name, the
    Text field keeps its commas and override tags; Comment and Dialogue events both get an ordinal in file order."""
    lines, newlines = _split_lines(text)
    problems: list[dict] = []
    sections: list[dict] = []
    script_info: dict[str, str] = {}
    styles: list[dict] = []
    style_format: list[str] | None = None
    event_format: list[str] | None = None
    raw_events: list[tuple[int, str, list[str]]] = []
    current: str | None = None
    stray = 0
    for number, line in enumerate(lines, 1):
        header = _ASS_SECTION.match(line)
        if header:
            current = header.group(1).strip().lower()
            sections.append({"name": header.group(1).strip(), "line": number, "lines": 0, "other_lines": 0})
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if current is None:
            stray += 1
            continue
        sections[-1]["lines"] += 1
        if stripped.startswith(";") or stripped.startswith("!:"):
            continue
        descriptor, separator, rest = line.partition(":")
        key = descriptor.strip().lower()
        if current == "script info":
            if separator:
                script_info[descriptor.strip()] = rest.strip()
        elif current in _STYLE_SECTIONS:
            if separator and key == "format":
                style_format = [item.strip() for item in rest.split(",")]
            elif separator and key == "style":
                parts = [item.strip() for item in rest.split(",")]
                lowered = [item.lower() for item in style_format or []]
                font = parts[lowered.index("fontname")] if "fontname" in lowered and lowered.index("fontname") < len(parts) else None
                styles.append({"name": parts[0] if parts else "", "fontname": font, "raw": line, "line": number})
            else:
                sections[-1]["other_lines"] += 1
        elif current == "events":
            if separator and key == "format":
                fields = [item.strip() for item in rest.split(",")]
                lowered = [item.lower() for item in fields]
                if event_format is not None:
                    if fields != event_format:
                        _problem(problems, "QC01_PARSE", number, "[Events] 里出现了第二个不同的 Format 行", observed=line[:160])
                elif len(set(lowered)) != len(lowered) or not {"start", "end", "text"} <= set(lowered) or lowered[-1] != "text":
                    _problem(problems, "QC01_PARSE", number, "[Events] 的 Format 行必须含 Start、End，并以 Text 结尾，且字段不重复", observed=line[:160])
                else:
                    event_format = fields
            elif separator and key in ("dialogue", "comment"):
                if event_format is None:
                    _problem(problems, "QC01_PARSE", number, "事件行出现在可用的 [Events] Format 行之前，无法确定字段顺序", observed=line[:160])
                    continue
                parts = rest.split(",", len(event_format) - 1)
                if len(parts) < len(event_format):
                    _problem(problems, "QC01_PARSE", number, f"{descriptor.strip()} 字段不足：需要 {len(event_format)} 个，实际 {len(parts)} 个", observed=line[:160])
                    continue
                raw_events.append((number, key, parts))
            else:
                sections[-1]["other_lines"] += 1
        else:
            sections[-1]["other_lines"] += 1
    if not sections:
        _problem(problems, "QC01_PARSE", 1, "文件里没有任何 [段落] 标题，不是 ASS 字幕")
    elif event_format is None and not any(item["message"].startswith("[Events]") for item in problems):
        _problem(problems, "QC01_PARSE", 1, "文件里没有带 Format 行的 [Events] 段落")
    wrap_style = int(script_info["WrapStyle"]) if script_info.get("WrapStyle", "").isascii() and script_info.get("WrapStyle", "").isdigit() else 0
    cues: list[dict] = []
    for ordinal, (number, key, parts) in enumerate(raw_events):
        last = len(event_format) - 1
        fields = {name: (part if position == last else part.strip()) for position, (name, part) in enumerate(zip(event_format, parts))}
        by_lower = {name.lower(): value for name, value in fields.items()}
        start_ms, end_ms = _ass_time_ms(by_lower["start"]), _ass_time_ms(by_lower["end"])
        if key == "dialogue":                       # a Comment is never shown: its times are kept as read and not judged
            if start_ms is None or end_ms is None:
                _problem(problems, "QC02_TIME", number, "时间不是规范的 时:分:秒.厘秒（两位小数，分秒 00–59）", ordinal=ordinal, observed={"start": by_lower["start"], "end": by_lower["end"]})
            elif end_ms <= start_ms:
                _problem(problems, "QC02_TIME", number, "结束时间不晚于开始时间", ordinal=ordinal, observed={"start_ms": start_ms, "end_ms": end_ms})
        shown = ass_display_text(by_lower["text"], wrap_style)
        layer = by_lower.get("layer")
        cues.append({"ordinal": ordinal, "cue_id": f"cue-{ordinal + 1:06d}", "kind": key, "line": number, "start_ms": start_ms, "end_ms": end_ms, "text": shown["text"],
                     "fields": fields, "layer": int(layer) if layer is not None and re.fullmatch(r"-?[0-9]+", layer) else None, "style": by_lower.get("style"),
                     "name": by_lower.get("name", by_lower.get("actor")), "drawing": shown["drawing"],
                     "notes": {"inline_fonts": shown["inline_fonts"], "reset_styles": shown["reset_styles"]}})
    return {"cues": cues, "problems": problems, "newlines": newlines, "styles": styles, "script_info": script_info, "sections": sections,
            "extensions": {"lines": len(lines), "events_format": event_format or [], "styles_format": style_format or [], "wrap_style": wrap_style, "lines_before_first_section": stray}}


def build_view(data: bytes, fmt: str, decoded: dict) -> dict:
    """The SubtitleView of already decoded bytes; ``decoded`` is what decode_subtitle returned for ``data``."""
    if fmt not in ("srt", "ass"):
        raise ContractError("SUBTITLE_INVALID", "only srt and ass files have a view", details={"kind": "unsupported_format", "format": fmt})
    parsed = parse_srt_view(decoded["text"]) if fmt == "srt" else parse_ass_view(decoded["text"])
    dialogue = sum(1 for cue in parsed["cues"] if cue["kind"] == "dialogue")
    return require_record("SubtitleView", {
        "schema_version": SCHEMA_VERSION, "format": fmt, "encoding": decoded["encoding"], "encoding_source": decoded["encoding_source"], "sha256": sha256_bytes(data),
        "bytes": len(data), "newlines": parsed["newlines"], "cues": parsed["cues"], "styles": parsed["styles"], "script_info": parsed["script_info"],
        "sections": parsed["sections"], "problems": parsed["problems"],
        "extensions": {**parsed["extensions"], "view_version": VIEW_VERSION, "dialogue_count": dialogue, "comment_count": len(parsed["cues"]) - dialogue}})


def parse_view(data: bytes, fmt: str, encoding: str | None = None) -> dict:
    """Decode and parse a human subtitle file without changing it. Undecodable bytes raise SUBTITLE_INVALID (QC01)."""
    return build_view(data, fmt, decode_subtitle(data, encoding))


__all__ = ["MACHINE_FORMAT_VERSION", "ASS_RESOLUTION_MS", "EVENT_FIELDS", "BASE_STYLE_ID", "BASE_HEADER", "srt_time", "ass_centiseconds", "ass_time",
           "render_srt_bytes", "parse_template", "base_template", "render_ass", "verify_written",
           "MAX_SUBTITLE_BYTES", "VIEW_VERSION", "subtitle_format", "decode_subtitle", "parse_srt_view", "parse_ass_view", "ass_display_text", "build_view", "parse_view"]
