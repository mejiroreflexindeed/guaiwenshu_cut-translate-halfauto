#!/usr/bin/env python3
"""W07 / T06 export part: independent subtitle and video exports from an explicitly chosen revision, on the real
eight-stage pipeline output and the real FFmpeg/ffprobe (plan 3.2, 7.5, 9.5, 12.1).

Run directly or through scripts/run_tests.py (PYTHONPATH = app/tools + tests).
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import unittest

from support import w03, w04
from upgrade import contracts as c
from upgrade import export, media, revisions, subtitles
from upgrade.process_runner import CancelToken
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade_revisions_test import SharedRunCase

# S06 (plan 14.2): two styles, two layers, inline position and font, a line break, a comma in the text, a Comment
HUMAN_ASS = (
    "[Script Info]\r\nScriptType: v4.00+\r\nPlayResX: 1920\r\nPlayResY: 1080\r\nWrapStyle: 0\r\n\r\n"
    "[V4+ Styles]\r\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Microsoft YaHei,160,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,2,10,10,60,1\r\n"
    "Style: Sign,{font},120,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,8,10,10,40,1\r\n\r\n"
    "[Events]\r\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Comment: 0,0:00:00.00,0:00:00.00,Default,,0,0,0,,校对备注\r\n"
    "Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,人工字幕一\r\n"
    "Dialogue: 1,0:00:01.00,0:00:02.00,Sign,,0,0,0,,{{\\pos(960,200)\\fnSimHei}}画面字,带逗号\\N第二行\r\n"
    "Dialogue: 0,0:00:02.20,0:00:02.80,Default,,0,0,0,,{drawing}\r\n"
)


def human_ass(*, font: str = "Microsoft YaHei", drawing: bool = False) -> bytes:
    return HUMAN_ASS.format(font=font, drawing="{\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}" if drawing else "人工字幕二").encode("utf-8-sig")


def gray_frame(path: Path, number: int, size=(320, 240)) -> bytes:
    """Decoded video frame ``number`` (0 = first presented frame) as 8-bit gray, through the real FFmpeg; frames are picked
    by their position, so two files are compared frame for frame whatever their container start times are."""
    completed = subprocess.run([w03.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", "select=eq(n\\," + str(number) + ")", "-frames:v", "1",
                                "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True)
    assert len(completed.stdout) == size[0] * size[1], len(completed.stdout)
    return completed.stdout


def mean_difference(left: bytes, right: bytes, *, bottom_rows: int = 0, width: int = 0) -> float:
    """Mean absolute gray difference; with ``bottom_rows`` only over the bottom band where a default subtitle sits."""
    if bottom_rows:
        left, right = left[-bottom_rows * width:], right[-bottom_rows * width:]
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def probe(path: Path) -> dict:
    completed = subprocess.run([w03.FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, check=True, text=True, encoding="utf-8")
    return json.loads(completed.stdout)


class ExportCase(SharedRunCase):
    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        self.assertEqual(c.validate_record("OperationResult", json.loads(completed.stdout)), [], completed.stderr)
        return completed.returncode, json.loads(completed.stdout), completed.stderr

    def export(self, revision_id, kind, destination, *extra, expect=0):
        code, result, stderr = self.cli("export", "--data-root", str(self.root), "--project", self.project_id, "--revision", revision_id, "--kind", kind,
                                        "--destination", str(destination), *extra)
        self.assertEqual(code, expect, json.dumps(result, ensure_ascii=False)[:1500] + stderr[-1500:])
        return result

    def human(self, data: bytes, name: str = "人工稿.ass") -> str:
        file = self.scratch(name)
        file.write_bytes(data)
        return self.register(file)["revision_id"]

    def destination(self, name: str) -> Path:
        return self.tmp / "导出 结果" / self._testMethodName[5:40] / name

    def video_hash(self) -> str:
        return c.sha256_file(Path(media.load_media_record(self.project, self.media_id)["source_ref"]["absolute_path"]))


class SubtitleExportTests(ExportCase):
    def test_a_subtitle_export_copies_the_chosen_bytes_once_and_keeps_their_review_status(self):
        target = self.destination("机器字幕")
        result = self.export(self.machine_id, "subtitles", target)
        files = {item["file"]: item for item in result["ids"]["files"]}
        self.assertEqual(set(files), {"machine.zh.srt", "machine.zh.ass"})
        for fmt in ("srt", "ass"):
            self.assertEqual((target / f"machine.zh.{fmt}").read_bytes(), (self.machine_dir / f"machine.zh.{fmt}").read_bytes())
            self.assertEqual(files[f"machine.zh.{fmt}"]["human_status"], "UNREVIEWED")
        manifest = c.load_record("ExportManifest", target / "manifest.json")
        self.assertEqual((manifest["status"], manifest["revision_id"], manifest["profile_id"], manifest["include_media"], manifest["tool_refs"]),
                         ("COMMITTED", self.machine_id, "subtitle_files_v1", False, []))
        self.assertEqual({ref["path"] for ref in manifest["output_refs"]}, set(files))
        self.assertIn("NOT_REVIEWED", [warning["code"] for warning in result["warnings"]])      # warnings never block, the status travels with the files
        index = self.index()
        self.assertIn(result["ids"]["export_id"], index["export_ids"])
        location = json.loads((self.project / "exports" / result["ids"]["export_id"] / "location.json").read_text(encoding="utf-8"))
        self.assertEqual((location["destination"], location["manifest_sha256"]), (str(target), c.sha256_file(target / "manifest.json")))
        # an export is created once: the same destination is refused and nothing changes
        before = sorted(path.name for path in target.iterdir())
        refused = self.export(self.machine_id, "subtitles", target, expect=c.EXIT_DATA)
        self.assertEqual(refused["error"]["code"], "OUTPUT_EXISTS")
        self.assertEqual((sorted(path.name for path in target.iterdir()), self.index()["export_ids"]), (before, index["export_ids"]))
        self.assertEqual([path.name for path in target.parent.iterdir() if ".pending-" in path.name], [])
        # the media copy is opt-in and verified against the registered hash; the machine SRT alone with --format
        with_media = self.export(self.machine_id, "subtitles", self.destination("连同视频"), "--format", "srt", "--include-media")
        record = media.load_media_record(self.project, self.media_id)
        copied = self.destination("连同视频") / Path(record["source_ref"]["absolute_path"]).name
        self.assertEqual(([item["file"] for item in with_media["ids"]["files"]], c.sha256_file(copied)), (["machine.zh.srt", copied.name], record["source_sha256"]))

    def test_a_human_ass_goes_out_alone_and_a_requested_srt_is_a_lossy_unreviewed_derivative(self):
        revision_id = self.human(human_ass(drawing=True))
        target = self.destination("人工 ASS")
        result = self.export(revision_id, "subtitles", target)
        self.assertEqual([item["file"] for item in result["ids"]["files"]], ["人工稿.ass"])         # the original format only, no implied SRT
        self.assertEqual((target / "人工稿.ass").read_bytes(), human_ass(drawing=True))
        refused = self.export(revision_id, "subtitles", self.destination("拒绝"), "--convert-to", "srt", expect=c.EXIT_ARGS)
        self.assertEqual((refused["error"]["code"], refused["error"]["details"]["count"]), ("NEEDS_SELECTION", 1))
        self.assertFalse(self.destination("拒绝").exists())
        converted = self.export(revision_id, "subtitles", self.destination("转 SRT"), "--convert-to", "srt", "--accept-omitted-drawings")
        derived = next(item for item in converted["ids"]["files"] if item["derived"])
        self.assertEqual((derived["file"], derived["lossy"], derived["human_status"], derived["confirmed_scopes"]), ("人工稿.derived.srt", True, "UNREVIEWED", []))
        view = subtitles.parse_view((self.destination("转 SRT") / "人工稿.derived.srt").read_bytes(), "srt")
        self.assertEqual([(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in view["cues"]], [(500, 1500, "人工字幕一"), (1000, 2000, "画面字,带逗号\n第二行")])
        manifest = c.load_record("ExportManifest", self.destination("转 SRT") / "manifest.json")
        conversion = manifest["profile_snapshot"]["conversions"][0]
        self.assertEqual(sorted(item["reason"] for item in conversion["omitted_events"]), ["comment", "drawing"])

    def test_missing_fonts_only_warn_for_subtitle_files(self):
        revision_id = self.human(human_ass(font="不存在的字体 W07"))
        result = self.export(revision_id, "subtitles", self.destination("缺字体"))
        self.assertIn("QC22_FONT", [warning["code"] for warning in result["warnings"]])
        manifest = c.load_record("ExportManifest", self.destination("缺字体") / "manifest.json")
        self.assertEqual([issue["severity"] for issue in manifest["quality_snapshot"]["export_issues"]], ["WARNING"])
        refused = self.export(revision_id, "mp4_burn", self.destination("缺字体成片"), expect=c.EXIT_RESOURCE)
        self.assertEqual((refused["error"]["code"], refused["error"]["details"]["missing_fonts"]), ("FONT_MISSING", ["不存在的字体 W07"]))
        self.assertFalse(self.destination("缺字体成片").exists())


class VideoExportTests(ExportCase):
    def test_a_burned_mp4_shows_the_complete_human_ass_on_the_real_frames(self):
        before_calls, before_requests, source_hash = len(self.double.calls), len(w04.requests_log(self.model)), self.video_hash()
        revision_id = self.human(human_ass())
        target = self.destination("硬字幕")
        result = self.export(revision_id, "mp4_burn", target)
        output = target / "人工稿.burn.mp4"
        streams = probe(output)["streams"]
        self.assertEqual(sorted((item["codec_type"], item["codec_name"]) for item in streams), [("audio", "aac"), ("video", "h264")])
        self.assertEqual(next(item for item in streams if item["codec_type"] == "video")["pix_fmt"], "yuv420p")
        source = Path(media.load_media_record(self.project, self.media_id)["source_ref"]["absolute_path"])
        inside = mean_difference(gray_frame(output, 36), gray_frame(source, 36))             # 1.2 s at 30 fps: both lines on screen
        outside = mean_difference(gray_frame(output, 87), gray_frame(source, 87))            # 2.9 s: after the last cue
        self.assertGreater(inside, 4 * max(outside, 0.25), (inside, outside))
        manifest = c.load_record("ExportManifest", target / "manifest.json")
        self.assertEqual(manifest["profile_snapshot"]["details"]["render_input"], "complete_ass_bytes")
        self.assertEqual((target / "subtitle_input" / "人工稿.ass").read_bytes(), human_ass())
        self.assertLessEqual(abs(manifest["validation"]["duration_ms"] - manifest["validation"]["source_duration_ms"]), manifest["validation"]["tolerance_ms"])
        self.assertEqual((len(self.double.calls), len(w04.requests_log(self.model)), self.video_hash()), (before_calls, before_requests, source_hash))  # no model, source untouched
        silent = self.export(revision_id, "mp4_burn", self.destination("无音轨"), "--audio-stream", "none")
        self.assertEqual([item["codec_type"] for item in probe(self.destination("无音轨") / "人工稿.burn.mp4")["streams"]], ["video"])
        self.assertEqual(silent["ids"]["validation"]["streams"]["audio"], [])

    def test_a_soft_mkv_carries_the_subtitle_track_that_can_be_extracted_again(self):
        revision_id = self.human(human_ass())
        target = self.destination("软字幕")
        result = self.export(revision_id, "mkv_soft", target)
        output = target / "人工稿.soft.mkv"
        streams = probe(output)["streams"]
        subtitle = next(item for item in streams if item["codec_type"] == "subtitle")
        self.assertEqual((subtitle["codec_name"], subtitle["disposition"]["default"], subtitle["disposition"]["forced"], subtitle["tags"]["language"]), ("ass", 1, 0, "chi"))
        source_streams = {item["codec_type"]: item["codec_name"] for item in media.load_media_record(self.project, self.media_id)["streams"]}
        self.assertEqual({item["codec_type"]: item["codec_name"] for item in streams if item["codec_type"] != "subtitle"}, source_streams)   # copied, not re-encoded
        roundtrip = result["ids"]["validation"]["subtitle_roundtrip"]
        self.assertEqual((roundtrip["input_cues"], roundtrip["mismatched"]), (3, 0))
        machine = self.export(self.machine_id, "mkv_soft", self.destination("机器 SRT 软字幕"), "--format", "srt")
        subtitle = next(item for item in probe(self.destination("机器 SRT 软字幕") / "machine.zh.soft.mkv")["streams"] if item["codec_type"] == "subtitle")
        self.assertEqual((subtitle["codec_name"], machine["ids"]["validation"]["subtitle_roundtrip"]["mismatched"]), ("subrip", 0))

    def test_a_cancelled_or_refused_export_publishes_nothing(self):
        revision_id = self.human(human_ass())
        resolved = self.resolve(revision_id)
        report = c.load_record("QualityReport", self.project / resolved["directory"] / "quality.json")
        record = media.load_media_record(self.project, self.media_id)
        catalog = inspect_resources(self.config, probe=False)
        roles = {role: resolve_resource(role, catalog) for role in ("ffprobe", "ffmpeg")}
        cancel = CancelToken()
        cancel.cancel()
        runtime = media.MediaRuntime(project_dir=self.project, ffprobe=roles["ffprobe"], ffmpeg=roles["ffmpeg"], log_dir=self.tmp / "取消日志", owner_token="w07", cancel=cancel)
        target = self.destination("取消")
        request = {"schema_version": 2, "export_id": c.new_id(), "project_id": self.project_id, "revision_id": revision_id, "media_id": self.media_id, "kind": "mp4_burn",
                   "selected_audio_stream": record["selected_audio_stream"], "profile_id": "mp4_burn_sdr_v1", "destination_root": str(target), "include_media": False,
                   "quality_acknowledgement": {}, "created_at": c.utc_now()}
        before = self.index()["export_ids"]
        with self.assertRaises(c.ContractError) as caught:
            export.run_export({"request": request, "data_root": self.root, "project_dir": self.project, "revision": resolved, "quality_report": report, "media_record": record,
                               "runtime": runtime, "format": None, "convert_to": None, "font_dirs": export.system_font_dirs(), "ascii_temp_root": self.config["ascii_temp_root"]})
        self.assertEqual(caught.exception.code, "PROCESS_CANCELLED")
        self.assertFalse(target.exists())
        candidate = Path(caught.exception.details["candidate"])
        self.assertEqual(json.loads((candidate / "candidate.json").read_text(encoding="utf-8"))["status"], "NOT_COMMITTED")
        self.assertEqual(self.index()["export_ids"], before)
        # QC21: the same subtitle against another registered media version is refused, whatever the file names
        other_video = w03.make_cfr_mp4(self.tmp / "剪辑后.mp4", audio=False)
        other = media.probe_media({"input": {"kind": "local_video", "value": str(other_video), "source_media_id": None}, "selected_video_stream": None,
                                   "selected_audio_stream": None, "project_id": self.project_id, "extensions": {}},
                                  runtime=media.MediaRuntime(project_dir=self.project, ffprobe=roles["ffprobe"], ffmpeg=roles["ffmpeg"], log_dir=self.tmp / "探测日志",
                                                             owner_token="w07"))
        refused = self.export(revision_id, "mp4_burn", self.destination("别的媒体"), "--media", other["media_id"], expect=c.EXIT_RESOURCE)
        self.assertEqual((refused["error"]["code"], refused["error"]["details"]["rule_id"]), ("MEDIA_CHANGED", "QC21_MEDIA_BINDING"))
        self.assertFalse(self.destination("别的媒体").exists())


class SourceClockTests(ExportCase):
    """T06 export part: VFR, a video that starts 1.4 s into its container (audio at 0.6 s), an odd frame size and a rotated source."""

    def register_media(self, path: Path) -> dict:
        catalog = inspect_resources(self.config, probe=False)
        roles = {role: resolve_resource(role, catalog) for role in ("ffprobe", "ffmpeg")}
        return media.probe_media({"input": {"kind": "local_video", "value": str(path), "source_media_id": None}, "selected_video_stream": None,
                                  "selected_audio_stream": None, "project_id": self.project_id, "extensions": {}},
                                 runtime=media.MediaRuntime(project_dir=self.project, ffprobe=roles["ffprobe"], ffmpeg=roles["ffmpeg"], log_dir=self.tmp / "探测" / path.stem,
                                                            owner_token="w07"))

    def human_srt(self, record: dict, name: str, end: str = "00:00:01,500") -> str:
        file = self.scratch(name)
        file.write_bytes(f"1\r\n00:00:00,500 --> {end}\r\n人工字幕测试\r\n".encode("utf-8"))
        return revisions.import_revision(self.root, self.project_id, record["media_id"], file, "review")["revision_id"]

    def test_the_subtitle_stays_on_the_first_video_frame_clock_whatever_the_container_does(self):
        sources = {"offset": w03.make_offset_mkv(self.tmp / "偏移 起点.mkv", video_start="1.4", audio_start="0.6"),
                   "vfr": w03.make_vfr(self.tmp / "可变帧率.mp4"),
                   "odd": w03.make_odd_size_mkv(self.tmp / "奇数尺寸.mkv"),
                   "rotated": w03.make_rotated_mp4(self.tmp / "旋转.mp4", w03.make_cfr_mp4(self.tmp / "旋转前.mp4"))}
        for name, path in sources.items():
            with self.subTest(source=name):
                record = self.register_media(path)
                self.assertTrue(record["clock"]["verified"], record["clock"])
                revision_id = self.human_srt(record, f"{name}.srt", end="00:00:00,800" if name == "odd" else "00:00:01,500")
                burn = self.export(revision_id, "mp4_burn", self.destination(f"{name} 硬字幕"))
                output = self.destination(f"{name} 硬字幕") / f"{name}.burn.mp4"
                video = next(item for item in probe(output)["streams"] if item["codec_type"] == "video")
                width, height = media.stream_by_index(record["streams"], record["selected_video_stream"])["width"], \
                    media.stream_by_index(record["streams"], record["selected_video_stream"])["height"]
                if name == "odd":
                    self.assertEqual((video["width"], video["height"], burn["ids"]["validation"]["problems"]), (width + 1, height + 1, []))
                    manifest = c.load_record("ExportManifest", self.destination(f"{name} 硬字幕") / "manifest.json")
                    self.assertTrue(manifest["profile_snapshot"]["details"]["padded_to_even"])
                    continue
                if name == "rotated":
                    self.assertEqual((video["width"], video["height"]), (240, 320))                          # turned once, into the pixels
                    self.assertFalse(any("rotation" in json.dumps(item).lower() and item.get("rotation") not in (0, None)
                                         for item in video.get("side_data_list") or []))
                    continue
                size = (video["width"], video["height"])
                band = {"bottom_rows": size[1] // 4, "width": size[0]}
                before, inside, after = (mean_difference(gray_frame(output, number, size), gray_frame(path, number, size), **band) for number in (9, 30, 60))
                # canonical 0.3 s: not yet; 1.0 s: on screen; 2.0 s: gone. A clock shifted by the container start would fail one of them.
                self.assertGreater(inside, 4 * max(before, after, 0.25), (name, before, inside, after))
                soft = self.export(revision_id, "mkv_soft", self.destination(f"{name} 软字幕"))
                self.assertEqual(soft["ids"]["validation"]["subtitle_roundtrip"]["mismatched"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
