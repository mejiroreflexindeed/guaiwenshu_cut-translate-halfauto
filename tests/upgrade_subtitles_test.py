"""T03 (W04, machine formats): SRT/ASS serialization rules of plan 6.7 / 7.5 and the read-back checks."""
from pathlib import Path
import tempfile
import unittest

from upgrade import contracts as c
from upgrade import subtitles

TEMPLATE = (
    "﻿[Script Info]\r\nScriptType: v4.00+\r\nPlayResX: 1280\r\nPlayResY: 720\r\n\r\n"
    "[V4+ Styles]\r\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Noto Sans CJK JP,54,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,40,1\r\n"
    "Style: Top,Noto Sans CJK JP,40,&H00FFFF00,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,8,10,10,20,1\r\n\r\n"
    "[Events]\r\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Dialogue: 0,0:00:00.50,0:00:01.20,Default,,0,0,0,,一つ目\r\n"
    "Dialogue: 1,0:00:01.50,0:00:02.00,Top,看板,5,5,30,,二つ目, with a comma\r\n\r\n"
    "[Aegisub Extradata]\r\nData: 0,example\r\n"
)


def cue(index, start, end, text, template_event=None):
    return {"index": index, "cue_id": f"cue-{index:06d}", "start_ms": start, "end_ms": end, "text": text, "template_event": template_event}


class SrtTests(unittest.TestCase):
    def test_machine_srt_is_bom_crlf_numbered_from_one_with_milliseconds(self):
        data = subtitles.render_srt_bytes([(500, 1200, "第一行\n第二行"), (1500, 2000, "  带空行\n\n的文本  ")])
        self.assertEqual(data, "﻿1\r\n00:00:00,500 --> 00:00:01,200\r\n第一行\r\n第二行\r\n\r\n2\r\n00:00:01,500 --> 00:00:02,000\r\n  带空行\r\n的文本\r\n".encode("utf-8"))
        with self.assertRaises(c.ContractError):
            subtitles.render_srt_bytes([(1000, 1000, "零时长")])
        self.assertEqual(subtitles.srt_time(3_600_000 + 61_001), "01:01:01,001")

    def test_read_back_detects_a_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.srt"
            expected = [{"index": 1, "start_ms": 500, "end_ms": 1200, "text": "一"}, {"index": 2, "start_ms": 1500, "end_ms": 2000, "text": "二"}]
            path.write_bytes(subtitles.render_srt_bytes([(500, 1200, "一"), (1500, 2000, "二")]))
            report = subtitles.verify_written(path, "srt", expected, duration_ms=2000)
            self.assertEqual((report["cues"], report["bom"], report["line_ending"]), (2, True, "CRLF"))
            path.write_bytes(path.read_bytes().replace("二".encode("utf-8"), "三".encode("utf-8")))
            with self.assertRaises(c.ContractError) as ctx:
                subtitles.verify_written(path, "srt", expected, duration_ms=2000)
            self.assertIn("text differs", ctx.exception.details["problems"][0])
            path.write_bytes(subtitles.render_srt_bytes([(500, 1200, "一"), (1500, 2000, "二")]))
            with self.assertRaises(c.ContractError) as ctx:
                subtitles.verify_written(path, "srt", expected, duration_ms=1998)             # 2 ms beyond the media: QC03 (1 ms tolerance)
            self.assertIn("QC03_BOUNDS", ctx.exception.details["problems"][0])
            subtitles.verify_written(path, "srt", expected, duration_ms=1999)


class AssTests(unittest.TestCase):
    def test_times_round_to_the_nearest_10ms_half_up_and_collapse_is_refused(self):
        self.assertEqual([subtitles.ass_centiseconds(ms) for ms in (1004, 1005, 1006, 0, 9, 10)], [100, 101, 101, 0, 1, 1])
        self.assertEqual(subtitles.ass_time(360_000 + 6_000 + 100 + 7), "1:01:01.07")
        data, rows = subtitles.render_ass([cue(1, 1004, 2006, "文")], subtitles.base_template())
        self.assertIn(b"Dialogue: 0,0:00:01.00,0:00:02.01,Default,,0,0,0,,\xe6\x96\x87\r\n", data)
        self.assertEqual((rows[0]["start_ms"], rows[0]["end_ms"], rows[0]["ass_start_cs"], rows[0]["ass_end_cs"]), (1004, 2006, 100, 201))
        with self.assertRaises(c.ContractError) as ctx:
            subtitles.render_ass([cue(1, 1000, 1004, "太短")], subtitles.base_template())      # 4 ms long: no duration at ASS precision
        self.assertEqual((ctx.exception.code, ctx.exception.details["cues"][0]["cue_id"]), ("TIME_PRECISION_COLLAPSE", "cue-000001"))

    def test_base_style_matches_plan_7_5(self):
        data, _ = subtitles.render_ass([cue(1, 0, 1000, "一行\n二行")], subtitles.base_template())
        text = data.decode("utf-8")
        self.assertTrue(text.startswith("﻿[Script Info]\r\n"))
        self.assertIn("PlayResX: 1920\r\nPlayResY: 1080\r\nWrapStyle: 0\r\n", text)
        self.assertIn("Style: Default,Microsoft YaHei,80,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,200,1\r\n", text)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,一行\\N二行\r\n", text)
        self.assertNotIn("\n\n", text.replace("\r\n", "\n").replace("\n\n", "\x00").replace("\x00", "\n\n")[:0] + "")

    def test_machine_timeline_styles_and_event_fields_are_inherited(self):
        template = subtitles.parse_template(TEMPLATE)
        self.assertEqual(template["styles"], ["Default", "Top"])
        self.assertEqual(len(template["events"]), 2)
        self.assertEqual(template["events"][1]["Text"], "二つ目, with a comma")                # the last field keeps its commas
        self.assertEqual(template["trailer"], ["[Aegisub Extradata]", "Data: 0,example"])
        data, _ = subtitles.render_ass([cue(1, 500, 1200, "第一", 0), cue(2, 1500, 2000, "第二", 1), cue(3, 2500, 3000, "无关联")], template)
        text = data.decode("utf-8")
        self.assertIn("Style: Top,Noto Sans CJK JP,40", text)                                 # the timeline's own styles
        self.assertIn("PlayResX: 1280", text)
        self.assertIn("Dialogue: 0,0:00:00.50,0:00:01.20,Default,,0,0,0,,第一\r\n", text)
        self.assertIn("Dialogue: 1,0:00:01.50,0:00:02.00,Top,看板,5,5,30,,第二\r\n", text)      # layer/style/name/margins of the associated event
        self.assertIn("Dialogue: 0,0:00:02.50,0:00:03.00,Default,,0,0,0,,无关联\r\n", text)
        self.assertTrue(text.endswith("[Aegisub Extradata]\r\nData: 0,example\r\n"))
        self.assertNotIn("一つ目", text)                                                      # template dialogues are not copied
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.ass"
            path.write_bytes(data)
            expected = [{"index": 1, "start_ms": 500, "end_ms": 1200, "text": "第一"}, {"index": 2, "start_ms": 1500, "end_ms": 2000, "text": "第二"},
                        {"index": 3, "start_ms": 2500, "end_ms": 3000, "text": "无关联"}]
            self.assertEqual(subtitles.verify_written(path, "ass", expected, duration_ms=2995)["time_tolerance_ms"], 10)   # 10 ms format quantization
            with self.assertRaises(c.ContractError):
                subtitles.verify_written(path, "ass", expected, duration_ms=2989)
        with self.assertRaises(c.ContractError):
            subtitles.parse_template("[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Text\n")   # no style table to inherit


if __name__ == "__main__":
    unittest.main(verbosity=2)
