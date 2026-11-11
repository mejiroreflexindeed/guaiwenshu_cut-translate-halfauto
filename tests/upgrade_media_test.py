"""T03 (clock fixtures) and T06 (media part, W03): the single time axis, stream selection, registration and derived media.

The second half runs a real FFmpeg and ffprobe on S05 material built by this test (3 s synthetic pictures and tones:
constant and variable frame timing, non-zero start, rotation, no audio, two audio streams, glued transport stream,
HDR-tagged, odd size). It is skipped, not passed, when FFMPEG_BINARY / FFPROBE_BINARY are not available.
"""
from fractions import Fraction
from pathlib import Path
import json
import os
import tempfile
import unittest
import wave

from support import w03
from upgrade import contracts as c
from upgrade import media
from upgrade.config import default_app_config
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade.runner import create_project
from upgrade.storage import init_data_root, read_manifest, verify_committed


def packet(pts=None, dts=None, duration=None, flags="__"):
    item = {"flags": flags}
    for key, value in (("pts", pts), ("dts", dts), ("duration", duration)):
        if value is not None:
            item[key] = value
    return item


def cfr_packets(count=5, step=512, start=0):
    return [packet(start + i * step, start + i * step, step, "K_" if i == 0 else "__") for i in range(count)]


def raw_probe(streams, **fmt):
    return {"streams": streams, "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "start_time": "0.000000", "duration": "3.000000", **fmt}}


def video_stream(index=0, **extra):
    return {"index": index, "codec_type": "video", "codec_name": "h264", "width": 320, "height": 240, "time_base": "1/15360", "start_pts": 0,
            "start_time": "0.000000", "pix_fmt": "yuv420p", "disposition": {"attached_pic": 0}, **extra}


def audio_stream(index, **extra):
    return {"index": index, "codec_type": "audio", "codec_name": "aac", "time_base": "1/48000", "sample_rate": "48000", "channels": 2,
            "disposition": {"attached_pic": 0}, **extra}


SOURCE_REF = {"absolute_path": "C:/media/a.mp4", "sha256": "a" * 64, "bytes": 10, "mtime_ns": 1}


class ClockArithmeticTests(unittest.TestCase):
    def test_round_half_up_is_exact_and_rounds_halves_upwards(self):
        cases = [(Fraction(5, 2), 3), (Fraction(-5, 2), -2), (Fraction(1, 3), 0), (Fraction(2, 3), 1), (Fraction(-1, 3), 0), (Fraction(7), 7),
                 (Fraction(10**18 * 2 + 1, 2), 10**18 + 1)]  # far beyond float precision
        for value, expected in cases:
            self.assertEqual(media.round_half_up(value), expected, value)

    def test_evidence_time_uses_the_first_presented_video_frame_as_zero(self):
        # t_ms = round_half_up(1000 * (pts * time_base - first_video_presentation_seconds))
        origin = Fraction(1500, 1000)
        self.assertEqual(media.pts_to_ms(1500, Fraction(1, 1000), origin), 0)
        self.assertEqual(media.pts_to_ms(1533, Fraction(1, 1000), origin), 33)
        self.assertEqual(media.pts_to_ms(90000 * 2, Fraction(1, 90000), origin), 500)
        self.assertEqual(media.pts_to_ms(1001 * 3, Fraction(1, 30000), Fraction(0)), 100)          # 100.1 ms
        self.assertEqual(media.pts_to_ms(1001 * 15, Fraction(1, 30000), Fraction(0)), 501)         # 500.5 ms rounds up
        self.assertEqual(media.pts_to_ms(1000, Fraction(1, 1000), origin), -500)                    # audio that starts before the video origin
        self.assertEqual(media.ms_text(7), "0.007")
        self.assertEqual(media.ms_text(61500), "61.500")

    def test_unknown_probe_values_stay_unknown_instead_of_becoming_zero(self):
        self.assertIsNone(media.parse_rational("0/0"))
        self.assertIsNone(media.parse_rational("N/A"))
        self.assertIsNone(media.parse_rational(None))
        self.assertEqual(media.parse_rational("1001/30000"), Fraction(1001, 30000))
        self.assertIsNone(media.parse_seconds("N/A"))
        self.assertIsNone(media.parse_seconds(None))
        self.assertEqual(media.parse_seconds("1.500000"), Fraction(3, 2))
        stream = media.normalize_streams(raw_probe([{"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "N/A"}]))[0]
        self.assertIsNone(stream["time_base"])
        self.assertIsNone(stream["start_pts"])
        self.assertIsNone(stream["start_time"])
        self.assertEqual((stream["rotation_degrees"], stream["rotation_declared"]), (0, False))

    def test_packet_timeline_table(self):
        ok = media.analyze_video_packets(cfr_packets())
        self.assertEqual((ok["status"], ok["origin_pts"], ok["last_pts"], ok["last_duration"], ok["frames"], ok["frame_timing"]["kind"]),
                         ("ok", 0, 2048, 512, 5, "cfr"))
        nonzero = media.analyze_video_packets(cfr_packets(start=23040))
        self.assertEqual(nonzero["origin_pts"], 23040)
        millisecond_ticks = media.analyze_video_packets([packet(p, p, 33) for p in (0, 33, 67, 100, 133)])
        self.assertEqual(millisecond_ticks["frame_timing"]["kind"], "cfr")  # 33/34 ms is rounding of a constant rate, not variable timing
        vfr = media.analyze_video_packets([packet(p, p, 10) for p in (0, 33, 76, 99, 142)])
        self.assertEqual((vfr["frame_timing"]["kind"], vfr["frame_timing"]["min_interval_ticks"], vfr["frame_timing"]["max_interval_ticks"]), ("vfr", 23, 43))
        # B-frames: presentation order differs from file order; the first presented frame is the smallest PTS
        reordered = media.analyze_video_packets([packet(1024, 0, 512, "K_"), packet(2048, 512, 512), packet(1536, 1024, 512), packet(3072, 1536, 512), packet(2560, 2048, 512)])
        self.assertEqual((reordered["status"], reordered["origin_pts"], reordered["pts"]), ("ok", 1024, [1024, 1536, 2048, 2560, 3072]))
        # packets the container marks as discarded are not presented
        discarded = media.analyze_video_packets(cfr_packets(3) + [packet(1536, 1536, None, "_D")])
        self.assertEqual((discarded["frames"], discarded["discarded"], discarded["last_pts"]), (3, 1, 1024))
        self.assertEqual(media.analyze_video_packets([packet(0, 0, 512)])["frame_timing"]["kind"], "single_frame")

    def test_unknown_and_discontinuous_timelines_are_named_not_guessed(self):
        missing = media.analyze_video_packets([packet(0, 0, 512), packet(None, 512, 512), packet(1024, 1024, 512)])
        self.assertEqual((missing["status"], missing["problem"]["kind"]), ("unknown", "missing_pts"))
        self.assertEqual(media.analyze_video_packets([])["status"], "unknown")
        backwards = media.analyze_video_packets(cfr_packets(3) + cfr_packets(3))
        self.assertEqual((backwards["status"], backwards["problem"]["kind"], backwards["problem"]["packet"]), ("discontinuity", "dts_backward", 3))
        duplicate = media.analyze_video_packets([packet(0, None, 512), packet(512, None, 512), packet(512, None, 512)])
        self.assertEqual((duplicate["status"], duplicate["problem"]["kind"]), ("discontinuity", "duplicate_pts"))

    def test_media_end_prefers_measured_frame_time_and_never_invents_one(self):
        tb = Fraction(1, 1000)
        timeline = media.analyze_video_packets([packet(p, p, 33) for p in (1500, 1533, 1567)])
        self.assertEqual(media.media_end_seconds(timeline, {}, {}, tb), (Fraction(1600, 1000), "last_frame_plus_duration"))
        no_duration = media.analyze_video_packets([packet(p, p) for p in (0, 33, 76)])
        self.assertEqual(media.media_end_seconds(no_duration, {"start_pts": 0, "duration_ts": 110}, {}, tb), (Fraction(110, 1000), "stream_duration"))
        self.assertEqual(media.media_end_seconds(no_duration, {}, {"start_time": "0.000000", "duration": "0.120000"}, tb), (Fraction(120, 1000), "format_duration"))
        # a container duration that only repeats the last frame's start says nothing about when that frame ends
        self.assertEqual(media.media_end_seconds(no_duration, {}, {"start_time": "0.000000", "duration": "0.076000"}, tb), (None, None))

    def test_stream_selection_table(self):
        one = media.normalize_streams(raw_probe([video_stream(0), audio_stream(1)]))
        self.assertEqual(media.select_streams(one, requested_video=None, requested_audio=None, audio_none=False), (0, 1))
        self.assertEqual(media.select_streams(one, requested_video=None, requested_audio=None, audio_none=True), (0, None))
        silent = media.normalize_streams(raw_probe([video_stream(0)]))
        self.assertEqual(media.select_streams(silent, requested_video=None, requested_audio=None, audio_none=False), (0, None))
        cover = media.normalize_streams(raw_probe([video_stream(0, disposition={"attached_pic": 1}), video_stream(1), audio_stream(2)]))
        self.assertEqual(media.select_streams(cover, requested_video=None, requested_audio=None, audio_none=False), (1, 2))  # cover art is not a video stream
        two_audio = media.normalize_streams(raw_probe([video_stream(0), audio_stream(1, tags={"language": "jpn"}), audio_stream(2, tags={"language": "eng"})]))
        with self.assertRaises(c.ContractError) as ctx:
            media.select_streams(two_audio, requested_video=None, requested_audio=None, audio_none=False)
        self.assertEqual((ctx.exception.code, ctx.exception.details["kind"]), ("NEEDS_SELECTION", "audio_stream"))
        self.assertEqual([item["language"] for item in ctx.exception.details["candidates"]], ["jpn", "eng"])  # the first track is not assumed to be Japanese
        self.assertEqual(media.select_streams(two_audio, requested_video=None, requested_audio=2, audio_none=False), (0, 2))
        two_video = media.normalize_streams(raw_probe([video_stream(0), video_stream(1)]))
        for kwargs, kind in (({"requested_video": None, "requested_audio": None}, "video_stream"), ({"requested_video": 5, "requested_audio": None}, "video_stream")):
            with self.assertRaises(c.ContractError) as ctx:
                media.select_streams(two_video, audio_none=False, **kwargs)
            self.assertEqual((ctx.exception.code, ctx.exception.details["kind"]), ("NEEDS_SELECTION", kind))
        with self.assertRaises(c.ContractError) as ctx:
            media.select_streams(one, requested_video=None, requested_audio=0, audio_none=False)  # stream 0 is video
        self.assertEqual(ctx.exception.code, "NEEDS_SELECTION")
        with self.assertRaises(c.ContractError) as ctx:
            media.select_streams(media.normalize_streams(raw_probe([audio_stream(0)])), requested_video=None, requested_audio=None, audio_none=False)
        self.assertEqual(ctx.exception.code, "MEDIA_PROFILE_UNSUPPORTED")

    def test_media_record_states_what_is_and_is_not_known(self):
        good = media.build_media_record(media_id=c.new_id(), source_ref=SOURCE_REF, raw=raw_probe([video_stream(0, time_base="1/1000"), audio_stream(1)]),
                                        packets=[packet(p, p, 33) for p in (1500, 1533, 1567)], video_index=0, audio_index=1)
        self.assertEqual(c.validate_record("MediaRecord", good), [])
        self.assertEqual((good["clock"]["origin_pts"], good["clock"]["origin_time_base_den"], good["clock"]["mapping_kind"], good["clock"]["verified"]),
                         (1500, 1000, "constant_offset", True))
        self.assertEqual((good["duration_ms"], good["extensions"]["duration_source"]), (100, "last_frame_plus_duration"))
        unknown = media.build_media_record(media_id=c.new_id(), source_ref=SOURCE_REF, raw=raw_probe([video_stream(0, time_base="0/0")]), packets=cfr_packets(),
                                           video_index=0, audio_index=None)
        self.assertEqual((unknown["clock"]["verified"], unknown["extensions"]["clock_problem"]["code"], unknown["extensions"]["duration_known"]),
                         (False, "MEDIA_CLOCK_UNKNOWN", False))
        jump = media.build_media_record(media_id=c.new_id(), source_ref=SOURCE_REF, raw=raw_probe([video_stream(0)]), packets=cfr_packets(3) + cfr_packets(3),
                                        video_index=0, audio_index=None)
        self.assertEqual((jump["clock"]["verified"], jump["extensions"]["clock_problem"]["code"]), (False, "MEDIA_CLOCK_DISCONTINUITY"))

    def test_frame_map_comparison_rejects_drift_dropped_frames_and_shifted_origin(self):
        source = [1500, 1533, 1567, 1600]
        same = media.compare_frame_maps(source, Fraction(1, 1000), [0, 528, 1072, 1600], Fraction(1, 16000))
        self.assertEqual((same["verified"], same["max_abs_delta_ms"]), (True, 0))
        self.assertFalse(media.compare_frame_maps(source, Fraction(1, 1000), [0, 528, 1072], Fraction(1, 16000))["verified"])           # a frame is gone
        self.assertFalse(media.compare_frame_maps(source, Fraction(1, 1000), [16, 544, 1088, 1616], Fraction(1, 16000))["verified"])    # does not start at 0
        drift = media.compare_frame_maps(source, Fraction(1, 1000), [0, 528, 1072, 1680], Fraction(1, 16000))
        self.assertEqual((drift["verified"], drift["mismatches"][0]["frame"], drift["max_abs_delta_ms"]), (False, 3, 5))

    def test_direct_use_and_unsupported_profiles(self):
        def record(streams, packets, **fmt):
            return media.build_media_record(media_id=c.new_id(), source_ref=SOURCE_REF, raw=raw_probe(streams, **fmt), packets=packets, video_index=0, audio_index=None)

        self.assertEqual(media.is_direct_usable(record([video_stream(0)], cfr_packets()), {}), (True, []))
        self.assertEqual(media.is_direct_usable(record([video_stream(0)], cfr_packets(start=512)), {})[1], ["non_zero_start"])
        self.assertEqual(media.is_direct_usable(record([video_stream(0, codec_name="vp9")], cfr_packets(), format_name="matroska,webm"), {})[1], ["container", "video_codec"])
        rotated = record([video_stream(0, side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}])], cfr_packets())
        self.assertEqual((rotated["streams"][0]["rotation_degrees"], media.is_direct_usable(rotated, {})[1]), (270, ["rotation"]))
        vfr = record([video_stream(0)], [packet(p, p, 10) for p in (0, 33, 76, 99)])
        self.assertEqual(media.is_direct_usable(vfr, {})[1], ["variable_frame_timing"])
        self.assertEqual(media.is_direct_usable(record([video_stream(0)], cfr_packets(), format_name="matroska,webm"), {"accepted_containers": [".mkv", ".mp4"]}), (True, []))
        hdr = record([video_stream(0, color_transfer="smpte2084")], cfr_packets())
        dovi = record([video_stream(0, side_data_list=[{"side_data_type": "DOVI configuration record"}])], cfr_packets())
        stereo = record([video_stream(0, side_data_list=[{"side_data_type": "Stereo 3D", "type": "side by side"}])], cfr_packets())
        flat = record([video_stream(0, side_data_list=[{"side_data_type": "Stereo 3D", "type": "2D"}])], cfr_packets())
        self.assertEqual(hdr["extensions"]["unsupported_profile"], ["hdr_transfer:smpte2084"])
        self.assertEqual(dovi["extensions"]["unsupported_profile"], ["dolby_vision"])
        self.assertEqual(stereo["extensions"]["unsupported_profile"], ["stereo_3d:side by side"])
        self.assertEqual(flat["extensions"]["unsupported_profile"], [])

    def test_download_arguments_are_a_fixed_list_for_one_video(self):
        target = Path("C:/data/files")
        argv = media.download_argv("https://example.invalid/watch?v=1&list=PL", target, {}, None)
        self.assertEqual(argv[:5], ["--no-playlist", "--newline", "--write-info-json", "--merge-output-format", "mp4"])
        self.assertEqual(argv[-1], "https://example.invalid/watch?v=1&list=PL")  # one argv element; '&' is not shell syntax here
        self.assertNotIn("--cookies-from-browser", argv)                           # never read browser data unless explicitly configured
        configured = media.download_argv("https://example.invalid/v", target, {"proxy": "http://127.0.0.1:7890", "quality": "最高 1080p",
                                                                                "browser_login_source": "edge", "youtube_client": "tv"}, Path("C:/tools/ffmpeg.exe"))
        for expected in (["--proxy", "http://127.0.0.1:7890"], ["-f", "bv*[height<=1080]+ba/b[height<=1080]"], ["--cookies-from-browser", "edge"],
                         ["--extractor-args", "youtube:player_client=tv"], ["--ffmpeg-location", str(Path("C:/tools/ffmpeg.exe"))]):
            index = configured.index(expected[0])
            self.assertEqual(configured[index:index + 2], expected)
        for bad in ({"quality": "8K"}, {"browser_login_source": "safari"}, {"youtube_client": "web"}):
            with self.assertRaises(c.ContractError) as ctx:
                media.download_argv("https://example.invalid/v", target, bad, None)
            self.assertEqual(ctx.exception.code, "CONFIG_UNSUPPORTED_COMMAND")


@unittest.skipIf(w03.media_tools_problem(), f"real FFmpeg/ffprobe not available: {w03.media_tools_problem()}")
class RealMediaTests(unittest.TestCase):
    """S05 material, real ffprobe and FFmpeg."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.fixtures = cls.tmp / "素材 (S05)"
        cls.fixtures.mkdir()
        cls.root = cls.tmp / "数据根 (media)"
        init_data_root(cls.root)
        cls.config = default_app_config(cls.root)
        w03.register_resources(cls.config, {"ffmpeg": Path(w03.FFMPEG), "ffprobe": Path(w03.FFPROBE)})
        report = inspect_resources(cls.config, probe=False)
        cls.ffmpeg, cls.ffprobe = resolve_resource("ffmpeg", report), resolve_resource("ffprobe", report)
        cls.cfr = w03.make_cfr_mp4(cls.fixtures / "常规 片源 & (1).mp4")
        cls.serial = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def runtime(self, project_dir, **settings):
        type(self).serial += 1
        self.events = []
        return media.MediaRuntime(project_dir=project_dir, ffprobe=self.ffprobe, ffmpeg=self.ffmpeg, log_dir=self.tmp / "logs" / str(self.serial),
                                  owner_token="t06", emit=self.events.append, media_settings=settings)

    def project(self):
        record = create_project(self.root, "t06")
        return self.root / "projects" / record["project_id"], record["project_id"]

    def probe(self, project_dir, project_id, path, *, video=None, audio=None, **extensions):
        return media.probe_media({"input": {"kind": "local_video", "value": str(path), "source_media_id": None}, "selected_video_stream": video,
                                  "selected_audio_stream": audio, "project_id": project_id, "extensions": extensions}, runtime=self.runtime(project_dir))

    def prepare(self, project_dir, record, *, needs_audio, audio="record", **settings):
        selected = record["selected_audio_stream"] if audio == "record" else audio
        return media.prepare_media({"media_id": record["media_id"], "needs_visual": True, "needs_audio": needs_audio,
                                    "compatibility_profile": media.COMPAT_PROFILE_ID, "run_id": c.new_id(), "attempt_id": c.new_id(),
                                    "extensions": {"selected_audio_stream": selected}}, runtime=self.runtime(project_dir, **settings))

    def derivation(self, project_dir, record, entry):
        return c.load_record("Derivation", project_dir / "media" / record["media_id"] / entry["derivation_ref"]["path"])

    def wav_frames(self, path):
        with wave.open(str(path), "rb") as reader:
            return reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()

    def test_common_mp4_is_registered_once_used_directly_and_never_modified(self):
        project_dir, project_id = self.project()
        before = c.sha256_file(self.cfr)
        record = self.probe(project_dir, project_id, self.cfr)
        self.assertEqual(c.validate_record("MediaRecord", record), [])
        self.assertEqual((record["source_sha256"], record["source_ref"]["absolute_path"]), (before, str(self.cfr)))
        self.assertEqual((record["duration_ms"], record["clock"]["origin_pts"], record["clock"]["mapping_kind"], record["clock"]["verified"]), (3000, 0, "identity", True))
        self.assertEqual((record["selected_video_stream"], record["selected_audio_stream"]), (0, 1))
        timing = record["extensions"]["frame_timing"]
        self.assertEqual((timing["kind"], timing["frames"]), ("cfr", 90))
        video = record["streams"][0]
        self.assertEqual((video["codec_name"], video["width"], video["height"], video["time_base"], video["raw_ref"]["pointer"]),
                         ("h264", 320, 240, {"num": 1, "den": 15360}, "/streams/0"))
        # raw probe output is kept next to the normalized record and both are covered by the registration manifest
        directory = media.media_dir(project_dir, record["media_id"])
        manifest = read_manifest(directory, "MediaManifest")
        verify_committed(directory, manifest["outputs"])
        self.assertEqual({ref["path"] for ref in manifest["outputs"]}, {"media.json", "probe/format_streams.json", "probe/video_packets.json"})
        self.assertEqual(json.loads((directory / "probe/format_streams.json").read_text(encoding="utf-8"))["format"]["filename"], str(self.cfr))
        # same content again (even from another path) is the same media, not a second registration
        again = self.probe(project_dir, project_id, self.cfr)
        copy = self.fixtures / "同内容 另一路径.mp4"
        copy.write_bytes(self.cfr.read_bytes())
        moved = self.probe(project_dir, project_id, copy)
        self.assertEqual({again["media_id"], moved["media_id"]}, {record["media_id"]})
        self.assertEqual(moved["source_ref"]["absolute_path"], str(copy))
        self.assertEqual(len([p for p in (project_dir / "media").iterdir() if p.is_dir()]), 1)

        prepared = self.prepare(project_dir, record, needs_audio=True)
        self.assertEqual(prepared["video_for_review"]["kind"], "original")
        self.assertEqual(prepared["media_record"]["extensions"]["direct_usable"], True)
        self.assertEqual(c.validate_record("ExternalFileRef", prepared["video_for_review"]["ref"]), [])
        audio = prepared["audio_for_asr"]
        self.assertEqual(c.validate_record("ArtifactRef", audio["ref"]), [])
        channels, width, rate, frames = self.wav_frames(audio["absolute_path"])
        self.assertEqual((channels, width, rate), (1, 2, 16000))
        self.assertGreaterEqual(frames, 3000 * 16)                               # covers the whole video
        derivation = self.derivation(project_dir, record, prepared["media_record"]["derivatives"][0])
        self.assertEqual((derivation["origin_offset_ms"], derivation["trim_before_ms"], derivation["pad_before_ms"]), (0, 0, 0))
        self.assertEqual(derivation["output_duration_ms"], frames * 1000 // 16000)
        self.assertAlmostEqual(w03.wav_onset_ms(Path(audio["absolute_path"])), 1000, delta=30)  # AAC source: coarse check only
        argv = json.loads((project_dir / "media" / record["media_id"] / "derived" / audio["media_revision"] / derivation["argv_ref"]["path"]).read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[argv.index("-map") + 1], "0:1")                     # explicit stream, never "first audio"
        self.assertNotIn("-y", argv)
        self.assertEqual(c.sha256_file(self.cfr), before)                         # the original is untouched
        # a second request reuses the committed derivative instead of running FFmpeg again
        repeated = self.prepare(project_dir, record, needs_audio=True)
        self.assertEqual(repeated["audio_for_asr"]["ref"], audio["ref"])
        self.assertIn("derived_media_reused", [event["event"] for event in self.events])
        self.assertNotIn("process_started", [event["event"] for event in self.events])

    def test_non_zero_start_keeps_one_origin_for_video_and_audio(self):
        for name, audio_start, offset, onset, output_ms in (("audio_early", "1.0", -500, 500, 3000), ("audio_late", "2.0", 500, 1500, 3500)):
            with self.subTest(name):
                project_dir, project_id = self.project()
                source = w03.make_offset_mkv(self.fixtures / f"{name}.mkv", video_start="1.5", audio_start=audio_start)
                record = self.probe(project_dir, project_id, source)
                self.assertEqual((record["clock"]["origin_pts"], record["clock"]["origin_time_base_den"], record["clock"]["mapping_kind"], record["duration_ms"]),
                                 (1500, 1000, "constant_offset", 3000))
                prepared = self.prepare(project_dir, record, needs_audio=True)
                review = prepared["video_for_review"]
                self.assertEqual((review["kind"], sorted(prepared["media_record"]["extensions"]["derivative_reasons"])), ("derived", ["container", "non_zero_start"]))
                self.assertEqual(prepared["media_record"]["clock"]["mapping_kind"], "verified_frame_map")
                frame_map = json.loads((project_dir / "media" / record["media_id"] / prepared["media_record"]["clock"]["mapping_ref"]["path"]).read_text(encoding="utf-8"))
                self.assertEqual((frame_map["comparison"]["verified"], frame_map["comparison"]["source_frames"], frame_map["comparison"]["derived_frames"],
                                  frame_map["derived_pts"][0], frame_map["source_pts"][0]), (True, 90, 90, 0, 1500))
                wav = next(entry for entry in prepared["media_record"]["derivatives"] if entry["media_revision"].startswith("asr-wav-"))
                derivation = self.derivation(project_dir, record, wav)
                self.assertEqual((derivation["origin_offset_ms"], derivation["trim_before_ms"], derivation["pad_before_ms"], derivation["output_duration_ms"]),
                                 (offset, max(0, -offset), max(0, offset), output_ms))
                # the tone starts 1.0 s into the audio stream; on the video axis that is 1.0 s plus the measured offset (lossless source: tight check)
                self.assertAlmostEqual(w03.wav_onset_ms(Path(prepared["audio_for_asr"]["absolute_path"])), onset, delta=3)
                self.assertEqual(self.wav_frames(prepared["audio_for_asr"]["absolute_path"])[3], output_ms * 16)

    def test_variable_frame_timing_is_preserved_frame_by_frame(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_vfr(self.fixtures / "vfr.mp4"))
        timing = record["extensions"]["frame_timing"]
        self.assertEqual((timing["kind"], timing["frames"], timing["discarded_packets"]), ("vfr", 89, 1))
        self.assertGreater(timing["max_interval_ticks"], timing["min_interval_ticks"] + 1)
        prepared = self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(prepared["media_record"]["extensions"]["derivative_reasons"], ["variable_frame_timing"])
        frame_map = json.loads((project_dir / "media" / record["media_id"] / prepared["media_record"]["clock"]["mapping_ref"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual((frame_map["comparison"]["verified"], frame_map["comparison"]["derived_frames"], frame_map["comparison"]["max_abs_delta_ms"]), (True, 89, 0))
        source_tb = Fraction(frame_map["source_time_base"]["num"], frame_map["source_time_base"]["den"])
        derived_tb = Fraction(frame_map["derived_time_base"]["num"], frame_map["derived_time_base"]["den"])
        source_ms = [media.pts_to_ms(p, source_tb, Fraction(0)) for p in frame_map["source_pts"]]
        derived_ms = [media.pts_to_ms(p, derived_tb, Fraction(0)) for p in frame_map["derived_pts"]]
        self.assertEqual(source_ms, derived_ms)
        self.assertEqual(sorted(set(b - a for a, b in zip(source_ms, source_ms[1:]))), [23, 43])  # still irregular: no forced frame rate
        self.assertIsNone(prepared["audio_for_asr"])

    def test_unknown_end_of_video_is_reported_not_estimated(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_vfr(self.fixtures / "vfr_no_durations.mkv"))
        self.assertEqual((record["clock"]["verified"], record["extensions"]["clock_problem"]["code"], record["extensions"]["clock_problem"]["kind"]),
                         (False, "MEDIA_CLOCK_UNKNOWN", "end_unknown"))
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(ctx.exception.code, "MEDIA_CLOCK_UNKNOWN")
        self.assertFalse((project_dir / "media" / record["media_id"] / "derived").exists())

    def test_rotation_is_baked_in_once(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_rotated_mp4(self.fixtures / "rotated.mp4", self.cfr))
        video = media.stream_by_index(record["streams"], 0)
        self.assertEqual((video["rotation_degrees"], video["rotation_declared"], video["width"], video["height"]), (90, True, 320, 240))
        prepared = self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(prepared["media_record"]["extensions"]["derivative_reasons"], ["rotation"])
        review_dir = Path(prepared["video_for_review"]["absolute_path"]).parent
        derived = media.normalize_streams(json.loads((review_dir / "probe/format_streams.json").read_text(encoding="utf-8")))
        derived_video = next(s for s in derived if s["codec_type"] == "video")
        self.assertEqual((derived_video["width"], derived_video["height"], derived_video["rotation_degrees"], derived_video["rotation_declared"]), (240, 320, 0, False))
        self.assertEqual(c.load_record("Derivation", review_dir / "derivation.json")["extensions"]["frame_map"]["verified"], True)

    def test_media_without_audio_gets_no_fabricated_wav(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_cfr_mp4(self.fixtures / "无音轨.mp4", audio=False))
        self.assertIsNone(record["selected_audio_stream"])
        prepared = self.prepare(project_dir, record, needs_audio=False)
        self.assertIsNone(prepared["audio_for_asr"])
        self.assertEqual(prepared["media_record"]["derivatives"], [])
        self.assertEqual(list((project_dir / "media" / record["media_id"]).rglob("*.wav")), [])
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=True)
        self.assertEqual((ctx.exception.code, ctx.exception.details["kind"]), ("NEEDS_SELECTION", "no_audio_stream"))

    def test_several_audio_streams_need_an_explicit_choice(self):
        project_dir, project_id = self.project()
        source = w03.make_two_audio_mkv(self.fixtures / "双音轨.mkv")
        with self.assertRaises(c.ContractError) as ctx:
            self.probe(project_dir, project_id, source)
        self.assertEqual((ctx.exception.code, [item["index"] for item in ctx.exception.details["candidates"]]), ("NEEDS_SELECTION", [1, 2]))
        beep = self.probe(project_dir, project_id, source, audio=1)
        tone = self.probe(project_dir, project_id, source, audio=2)
        none = self.probe(project_dir, project_id, source, audio_stream="none")
        self.assertEqual({beep["media_id"], tone["media_id"], none["media_id"]}, {beep["media_id"]})  # the time axis belongs to the video stream
        self.assertEqual((beep["selected_audio_stream"], tone["selected_audio_stream"], none["selected_audio_stream"]), (1, 2, None))
        first = self.prepare(project_dir, beep, needs_audio=True)
        second = self.prepare(project_dir, tone, needs_audio=True)
        self.assertNotEqual(first["audio_for_asr"]["media_revision"], second["audio_for_asr"]["media_revision"])
        self.assertAlmostEqual(w03.wav_onset_ms(Path(first["audio_for_asr"]["absolute_path"])), 1000, delta=3)
        self.assertAlmostEqual(w03.wav_onset_ms(Path(second["audio_for_asr"]["absolute_path"])), 0, delta=3)
        # switching the audio stream does not change what on-screen recognition sees
        self.assertEqual(first["video_for_review"]["video_identity"], second["video_for_review"]["video_identity"])

    def test_timestamp_jump_blocks_everything_that_depends_on_media_time(self):
        project_dir, project_id = self.project()
        source = w03.make_discontinuous_ts(self.fixtures / "glued.ts")
        before = c.sha256_file(source)
        record = self.probe(project_dir, project_id, source)
        self.assertEqual((record["clock"]["verified"], record["extensions"]["clock_problem"]["code"], record["extensions"]["clock_problem"]["kind"]),
                         (False, "MEDIA_CLOCK_DISCONTINUITY", "dts_backward"))
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(ctx.exception.code, "MEDIA_CLOCK_DISCONTINUITY")
        self.assertEqual(c.sha256_file(source), before)                           # kept as it is; nothing is "shifted by a few seconds"
        self.assertTrue((project_dir / "media" / record["media_id"] / "media.json").is_file())  # still registered, e.g. for binding a subtitle

    def test_hdr_tagged_source_is_registered_but_not_converted(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_hdr_tagged_mkv(self.fixtures / "hdr.mkv"))
        self.assertEqual(record["extensions"]["unsupported_profile"], ["hdr_transfer:smpte2084"])
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(ctx.exception.code, "MEDIA_PROFILE_UNSUPPORTED")
        self.assertNotIn("process_started", [event["event"] for event in self.events])  # no SDR conversion was attempted

    def test_odd_picture_size_is_padded_not_scaled_or_cropped(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_odd_size_mkv(self.fixtures / "odd.mkv"))
        self.assertEqual((record["streams"][0]["width"], record["streams"][0]["height"]), (321, 241))
        prepared = self.prepare(project_dir, record, needs_audio=False)
        review_dir = Path(prepared["video_for_review"]["absolute_path"]).parent
        derivation = c.load_record("Derivation", review_dir / "derivation.json")
        self.assertEqual((derivation["extensions"]["display_size"], derivation["extensions"]["parameters"]["pad_right_px"], derivation["extensions"]["parameters"]["pad_bottom_px"]),
                         ([321, 241], 1, 1))
        derived_video = media.normalize_streams(json.loads((review_dir / "probe/format_streams.json").read_text(encoding="utf-8")))[0]
        self.assertEqual((derived_video["width"], derived_video["height"], derived_video["codec_name"], derived_video["pix_fmt"]), (322, 242, "h264", "yuv420p"))

    def test_changed_source_is_refused_and_a_touched_one_is_reverified(self):
        project_dir, project_id = self.project()
        source = self.fixtures / "会被改动.mp4"
        source.write_bytes(self.cfr.read_bytes())
        record = self.probe(project_dir, project_id, source)
        os.utime(source, ns=(record["source_ref"]["mtime_ns"] + 5_000_000_000, record["source_ref"]["mtime_ns"] + 5_000_000_000))
        prepared = self.prepare(project_dir, record, needs_audio=False)            # same bytes, new timestamp: re-hashed and accepted
        self.assertEqual(prepared["media_record"]["source_sha256"], record["source_sha256"])
        self.assertNotEqual(prepared["media_record"]["source_ref"]["mtime_ns"], record["source_ref"]["mtime_ns"])
        with source.open("ab") as handle:
            handle.write(b"appended by another program")
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(ctx.exception.code, "MEDIA_CHANGED")
        source.unlink()
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=False)
        self.assertEqual(ctx.exception.code, "INPUT_MISSING")

    def test_copy_original_points_at_the_project_copy(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, self.cfr, copy_original=True)
        copied = Path(record["source_ref"]["absolute_path"])
        self.assertEqual(copied, media.media_dir(project_dir, record["media_id"]) / "source" / "original.mp4")
        self.assertEqual((c.sha256_file(copied), record["source_sha256"]), (c.sha256_file(self.cfr),) * 2)
        self.assertEqual(self.prepare(project_dir, record, needs_audio=False)["video_for_review"]["absolute_path"], str(copied))

    def test_conversion_disabled_in_settings_is_an_explicit_stop(self):
        project_dir, project_id = self.project()
        record = self.probe(project_dir, project_id, w03.make_odd_size_mkv(self.fixtures / "odd_no_convert.mkv"))
        with self.assertRaises(c.ContractError) as ctx:
            self.prepare(project_dir, record, needs_audio=False, auto_convert=False)
        self.assertEqual((ctx.exception.code, ctx.exception.details["reasons"]), ("MEDIA_PROFILE_UNSUPPORTED", ["container"]))
        accepted = self.prepare(project_dir, record, needs_audio=False, accepted_containers=[".mkv"])
        self.assertEqual(accepted["video_for_review"]["kind"], "original")


if __name__ == "__main__":
    unittest.main(verbosity=2)
