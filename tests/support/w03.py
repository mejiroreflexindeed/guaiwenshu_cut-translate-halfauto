"""W03 fixtures: synthetic S05 media built with a real FFmpeg, and engine doubles that write engine-shaped output.

The engine doubles replace only the process launch (``recognition.run_engine``). They prove the adapter protocol
(arguments, staging, parsing, recovery); they say nothing about real engine accuracy.
"""
from __future__ import annotations

import json
import os
import subprocess
import wave
from pathlib import Path

from upgrade import contracts as c
from upgrade.storage import artifact_ref

FFMPEG = os.environ.get("FFMPEG_BINARY") or ""
FFPROBE = os.environ.get("FFPROBE_BINARY") or ""


def media_tools_problem() -> str | None:
    """None when a real ffmpeg (with libx264) and ffprobe are available for the S05 fixtures."""
    if not FFMPEG or not Path(FFMPEG).is_file():
        return "FFMPEG_BINARY is not set to an existing ffmpeg"
    if not FFPROBE or not Path(FFPROBE).is_file():
        return "FFPROBE_BINARY is not set to an existing ffprobe"
    encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True).stdout
    if b"libx264" not in encoders:
        return "the ffmpeg under test has no libx264 encoder"
    return None


def ffmpeg(*args: str) -> None:
    completed = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError("fixture ffmpeg failed: " + completed.stderr.decode("utf-8", errors="replace"))


VIDEO = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
# silence, then a 1 kHz tone that starts exactly 1.0 s into the audio stream
BEEP = ["-f", "lavfi", "-i", "aevalsrc='if(gte(t,1),0.5*sin(2*PI*1000*t),0)':d=3:s=48000"]


def make_cfr_mp4(path: Path, *, audio: bool = True) -> Path:
    """Common case: H.264 MP4, 30 fps constant, starts at 0, 3 s."""
    if audio:
        ffmpeg(*VIDEO, *BEEP, *X264, "-c:a", "aac", "-b:a", "96k", str(path))
    else:
        ffmpeg(*VIDEO, *X264, "-an", str(path))
    return path


def make_offset_mkv(path: Path, *, video_start: str, audio_start: str) -> Path:
    """Matroska with absolute timestamps: video and lossless audio start at different non-zero container times."""
    video, audio = path.with_name(path.stem + ".v.mkv"), path.with_name(path.stem + ".a.wav")
    ffmpeg(*VIDEO, *X264, str(video))
    ffmpeg(*BEEP, "-c:a", "pcm_s16le", str(audio))
    ffmpeg("-itsoffset", video_start, "-i", str(video), "-itsoffset", audio_start, "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c", "copy", "-copyts", str(path))
    video.unlink()
    audio.unlink()
    return path


def make_vfr(path: Path) -> Path:
    """Variable frame timing: every other frame is 10 ms late. As .mp4 the sample durations are stored (and FFmpeg marks
    the last packet as discarded); as .mkv the last frame has no duration at all, so the end of the video is unknown."""
    ffmpeg(*VIDEO, "-vf", "settb=1/1000,setpts='N*33+10*mod(N\\,2)'", "-fps_mode", "passthrough", *X264, str(path))
    return path


def make_rotated_mp4(path: Path, source: Path) -> Path:
    ffmpeg("-display_rotation", "90", "-i", str(source), "-c", "copy", str(path))
    return path


def make_two_audio_mkv(path: Path) -> Path:
    ffmpeg(*VIDEO, *BEEP, "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=3", "-map", "0:v", "-map", "1:a", "-map", "2:a",
           *X264, "-c:a", "pcm_s16le", str(path))
    return path


def make_discontinuous_ts(path: Path) -> Path:
    """Two identical transport-stream segments glued byte-wise: timestamps jump back to the start in the middle."""
    segment = path.with_name(path.stem + ".seg.ts")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=1", *X264, "-f", "mpegts", str(segment))
    path.write_bytes(segment.read_bytes() * 2)
    segment.unlink()
    return path


def make_hdr_tagged_mkv(path: Path) -> Path:
    """SDR pixels tagged as PQ/BT.2020: enough for the probe to see an HDR transfer characteristic."""
    ffmpeg(*VIDEO, "-vf", "setparams=color_trc=smpte2084:color_primaries=bt2020:colorspace=bt2020nc", *X264, str(path))
    return path


def make_odd_size_mkv(path: Path) -> Path:
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=322x242:rate=30:duration=1,format=yuv444p,crop=321:241:0:0", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv444p", str(path))
    return path


def wav_onset_ms(path: Path, threshold: int = 2000) -> int | None:
    """First sample whose magnitude exceeds the threshold, in milliseconds (16-bit PCM)."""
    with wave.open(str(path), "rb") as reader:
        rate, frames = reader.getframerate(), reader.readframes(reader.getnframes())
    for index in range(0, len(frames) - 1, 2):
        if abs(int.from_bytes(frames[index:index + 2], "little", signed=True)) > threshold:
            return (index // 2) * 1000 // rate
    return None


def write_silent_wav(path: Path, milliseconds: int = 3000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * (16 * milliseconds))
    return path


# --------------------------------------------------------------------------- engine doubles

ASS_HEADER = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Microsoft YaHei,80,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,200,1\n\n"
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)
ASS_EVENTS = (
    "Dialogue: 0,0:00:00.50,0:00:01.20,Default,,0,0,0,,こんにちは、世界\n"
    "Dialogue: 0,0:00:01.50,0:00:02.00,Default,,0,0,0,,Subtitle_Dialog_2\n"
    "Dialogue: 0,0:00:02.10,0:00:02.90,Default,,0,0,0,,二行目のテロップ\n"
)
MAGIA_TEMPLATE_CONFIG = (
    "source: []\ndestination: []\nassTemplate: template.asst\nstrategy: dtd\npreset: default\nengine: speculative\nmaxResolution: 0\n"
    "dtd:\n  default:\n    dialogRect: [0.0, 1.0, 0.0, 1.0]\n  no_region:\n    threshold: 1\nocr: {}\n"
)
UTTERANCES = [  # (start_ms, end_ms, text, tokens as (text, from, to, p))
    (500, 1500, "はい、そうです", [("はい", 520, 800, 0.97), ("、", 800, 860, 0.61), ("そうです", 860, 1480, 0.88)]),
    (1800, 2900, "ありがとう", [("ありが", 1820, 2300, 0.42), ("とう", 2300, 2880, 0.9)]),
]


def _srt_time(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def whisper_documents(*, conflict: bool = False, with_scores: bool = True) -> tuple[str, dict]:
    """A native-shaped SRT + full JSON. ``conflict`` puts token clocks in compacted-audio coordinates (before their utterance)."""
    srt, transcription = [], []
    for number, (start, end, text, tokens) in enumerate(UTTERANCES, start=1):
        srt.append(f"{number}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
        shift = -400 if conflict and number == 2 else 0
        native = [{"text": "[_BEG_]", "id": 50364, "offsets": {"from": start, "to": start}, "t_dtw": -1}]
        for position, (piece, left, right, probability) in enumerate(tokens):
            token = {"text": piece, "id": 1000 + position, "offsets": {"from": left + shift, "to": right + shift}, "t_dtw": (left + shift) // 10}
            if with_scores:
                token["p"] = probability
            native.append(token)
        transcription.append({"timestamps": {"from": _srt_time(start), "to": _srt_time(end)}, "offsets": {"from": start, "to": end}, "text": text, "tokens": native})
    return "\n".join(srt), {"systeminfo": "fixture", "model": {"type": "large"}, "params": {"language": "ja"}, "transcription": transcription}


class EngineDouble:
    """Stands in for ``recognition.run_engine``: records every launch and writes what the real engine would leave behind."""

    def __init__(self, *, whisper: str = "ok", magia: str = "ok"):
        self.whisper = whisper      # ok | conflict | conflict_always | exit7 | corrupt_json | empty | recovery_exit7 | no_scores
        self.magia = magia          # ok | empty | exit3 | no_output | acp | bad_time
        self.calls: list[dict] = []

    def launched(self, name: str) -> list[dict]:
        return [call for call in self.calls if name in call["executable"].lower()]

    def __call__(self, spec: dict, runtime, *, stdin: bytes | None = None) -> dict:
        c.require_record("ProcessSpec", spec)
        executable = Path(spec["executable"]).name
        self.calls.append({"executable": executable, "argv": list(spec["argv"]), "cwd": spec["cwd"], "stdin": stdin, "env": sorted(spec["env_overrides"])})
        log_dir = Path(spec["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "stdout.log").write_text("fixture engine log\n", encoding="utf-8")
        (log_dir / "stderr.log").write_text("", encoding="utf-8")
        (log_dir / "process.json").write_text(json.dumps({"argv": spec["argv"], "fixture": True}), encoding="utf-8")
        code = self._whisper(spec) if "whisper" in executable.lower() else self._magia(spec)
        return c.require_record("ProcessResult", {"exit_code": code, "cancelled": False, "started_at": c.utc_now(), "ended_at": c.utc_now(),
                                                  "stdout_ref": artifact_ref(log_dir, log_dir / "stdout.log"),
                                                  "stderr_ref": artifact_ref(log_dir, log_dir / "stderr.log"), "observed_engine_id": None})

    def _whisper(self, spec: dict) -> int:
        argv = spec["argv"]
        base = Path(argv[argv.index("-of") + 1])
        with_vad = "--vad" in argv
        behavior = self.whisper
        if behavior == "empty":
            Path(str(base) + ".srt").write_bytes(b"")
            Path(str(base) + ".json").write_text(json.dumps({"transcription": []}), encoding="utf-8")
            return 0
        conflict = behavior == "conflict_always" or (behavior in ("conflict", "recovery_exit7") and with_vad)
        srt, document = whisper_documents(conflict=conflict, with_scores=behavior != "no_scores")
        Path(str(base) + ".srt").write_text(srt, encoding="utf-8")
        Path(str(base) + ".json").write_text("{broken" if behavior == "corrupt_json" else json.dumps(document, ensure_ascii=False), encoding="utf-8")
        if behavior == "exit7" or (behavior == "recovery_exit7" and not with_vad):
            return 7
        return 0

    def _magia(self, spec: dict) -> int:
        import yaml
        stage = Path(spec["cwd"])
        config = yaml.safe_load((stage / "config.yml").read_text(encoding="ascii"))
        target = Path(config["destination"][0] + ".ass")
        behavior = self.magia
        if behavior == "no_output":
            return 0
        if behavior == "empty":
            target.write_bytes(ASS_HEADER.encode("utf-8-sig"))
        elif behavior == "acp":  # frozen Magia may write a UTF-8 BOM followed by Windows ACP text
            from magia_timeline_adapter import native_output_encoding
            target.write_bytes(b"\xef\xbb\xbf" + (ASS_HEADER + "Dialogue: 0,0:00:00.50,0:00:01.20,Default,,0,0,0,,ABC\n").encode(native_output_encoding()))
        elif behavior == "bad_time":
            target.write_bytes((ASS_HEADER + "Dialogue: 0,0:00:02.00,0:00:01.00,Default,,0,0,0,,逆転した時間\n").encode("utf-8-sig"))
        else:
            target.write_bytes((ASS_HEADER + ASS_EVENTS).encode("utf-8-sig"))
        return 3 if behavior == "exit3" else 0


def make_engine_resources(root: Path) -> dict[str, Path]:
    """Engine-shaped directories with placeholder binaries (never executed: launches go through EngineDouble)."""
    whisper = root / "whisper" / "bin"
    whisper.mkdir(parents=True)
    (whisper / "whisper-cli.exe").write_bytes(b"fixture whisper-cli")
    (whisper / "ggml-large-v3.bin").write_bytes(b"fixture model")
    (whisper / "ggml-silero-v5.1.2.bin").write_bytes(b"fixture vad")
    magia = root / "magia"
    (magia / "PaddleOCRModels").mkdir(parents=True)
    (magia / "MagiaTimeline.exe").write_bytes(b"fixture magia")
    (magia / "config.yml").write_text(MAGIA_TEMPLATE_CONFIG, encoding="utf-8")
    (magia / "template.asst").write_text("fixture template\n", encoding="utf-8")
    (magia / "ConfigSchema.json").write_text("{}", encoding="utf-8")
    (magia / "PaddleOCRModels" / "model.bin").write_bytes(b"fixture ocr model")
    return {"whisper-cli": whisper / "whisper-cli.exe", "whisper-model": whisper / "ggml-large-v3.bin",
            "whisper-vad-model": whisper / "ggml-silero-v5.1.2.bin", "magia-timeline": magia}


RESOURCE_KINDS = {"whisper-cli": "asr_engine", "whisper-model": "model", "whisper-vad-model": "model", "magia-timeline": "ocr_engine",
                  "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "llama-server": "translation_engine", "sakura-model": "model"}
MAGIA_FILES = ["MagiaTimeline.exe", "config.yml", "template.asst", "ConfigSchema.json", "PaddleOCRModels/model.bin"]


def register_resources(config: dict, paths: dict[str, Path]) -> None:
    from upgrade.resource_catalog import declare_resource, register_resource
    for resource_id, path in paths.items():
        files = MAGIA_FILES if resource_id == "magia-timeline" else None
        register_resource(config, resource_id, declare_resource(RESOURCE_KINDS[resource_id], path, files=files))


def passthrough_utf8(executable: Path, cache_root: Path, module) -> tuple[Path, dict]:
    """Replacement for the PE-manifest patching step, which needs the real MagiaTimeline executable."""
    return executable, {"mode": "native_utf8", "executable": str(executable), "fixture": True}
