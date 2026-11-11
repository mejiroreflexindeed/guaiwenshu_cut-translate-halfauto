"""T03/T04/T06 (W03 + W04, queue level): the eight stages running inside the persistent queue.

Media stages use a real FFmpeg/ffprobe on S05 material; the two recognition engines are replaced by doubles at the
process-launch seam; the model server is a scripted loopback process (real HTTP). Checked here: the per-mode skip rules,
the audio pre-check, resource pre-checks (translation server included) before any engine starts, cache reuse limited to
real dependencies, resume after a resource was fixed or a translation failed (checkpoints), the machine revision commit,
EMPTY results, supplied_source over a registered source revision, a real HTTP download through yt-dlp, and the
command line (stream pre-check, dictionary acceptance).
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import functools
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from support import w03, w04
from upgrade import contracts as c
from upgrade import dictionary, quality as quality_module, recognition, translation
from upgrade import runner as r
from upgrade.config import APP_CONFIG_NAME, ENGINE_SETTINGS_NAME, default_app_config, save_app_config, save_engine_settings
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade.stage_registry import default_registry
from upgrade.storage import init_data_root, safe_join, verify_committed


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipIf(w03.media_tools_problem(), f"real FFmpeg/ffprobe not available: {w03.media_tools_problem()}")
class StageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._fixtures = tempfile.TemporaryDirectory()
        cls.fixtures = Path(cls._fixtures.name) / "素材 (stages)"
        cls.fixtures.mkdir()
        cls.video = w03.make_cfr_mp4(cls.fixtures / "有声 片源.mp4")
        cls.silent = w03.make_cfr_mp4(cls.fixtures / "无声 片源.mp4", audio=False)
        cls.two_audio = w03.make_two_audio_mkv(cls.fixtures / "双音轨.mkv")
        cls.served = Path(cls._fixtures.name) / "served"
        cls.served.mkdir()
        (cls.served / "clip.mp4").write_bytes(cls.video.read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls._fixtures.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "数据根 (stages)"
        init_data_root(self.root)
        self.config = default_app_config(self.root, ascii_temp_root=str(self.tmp / "ascii"))
        save_app_config(self.root / APP_CONFIG_NAME, self.config)
        self.port = free_port()
        self.engine_settings: dict = {"translation": {"endpoint": f"http://127.0.0.1:{self.port}"}}
        save_engine_settings(self.root / ENGINE_SETTINGS_NAME, self.engine_settings)
        self.engine_paths = w03.make_engine_resources(self.tmp / "engines")
        self.engine_paths.update(w04.make_translation_resources(self.tmp / "engines"))
        self.model = self.engine_paths["sakura-model"]
        w03.register_resources(self.config, {"ffmpeg": Path(w03.FFMPEG), "ffprobe": Path(w03.FFPROBE)})
        self.double = w03.EngineDouble()
        self.patches = [patch.object(translation, "RETRY_PAUSE_SECONDS", 0.05), patch.object(translation, "HEALTH_POLL_SECONDS", 0.1)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self._tmp.cleanup()

    def register_engines(self, *names):
        w03.register_resources(self.config, {name: self.engine_paths[name] for name in (names or self.engine_paths)})

    def resource_refs(self):
        report = inspect_resources(self.config, probe=False)
        return [resolve_resource(resource_id, report)["resource_ref"] for resource_id in sorted(report["manifests"])]

    def submit(self, mode, video=None, *, settings=None, input_kind="local_video", **kw):
        settings = self.engine_settings if settings is None else {**self.engine_settings, **settings}
        prepared = dictionary.prepare_for_request(self.root, settings, accept_warnings=False)
        request = r.build_job_request(self.config, settings, input_kind=input_kind, input_value=str(video or self.video), recognition_mode=mode,
                                      resource_refs=self.resource_refs(), dictionary_revision_id=prepared["revision_id"],
                                      accepted_dictionary_warnings=prepared["accepted_ids"], **kw)
        return r.submit(self.root, request)

    def run_queue(self):
        with patch.object(recognition, "run_engine", self.double), patch.object(recognition, "prepare_utf8_executable", w03.passthrough_utf8), \
                patch.object(translation, "launch_spec", w04.fake_launch):
            return r.QueueRunner(self.root, self.config, self.engine_settings, default_registry()).run_queue()

    def task(self, envelope):
        envelope = r.load_envelope(self.root, envelope["request"]["request_id"])
        project = r.project_dir(self.root, envelope["request"]["project_id"])
        return envelope, project, r.load_task(r.run_dir(project, envelope["run_id"]))

    def stage(self, task, name):
        return next(stage for stage in task["stages"] if stage["name"] == name)

    def stage_dir(self, project, task, name):
        return safe_join(project, self.stage(task, name)["output_manifest"]["path"]).parent

    def statuses(self, task):
        return {stage["name"]: (stage["status"], stage["skip_reason"]) for stage in task["stages"]}

    def events(self, project, task):
        return (r.run_dir(project, task["run_id"]) / "events.jsonl").read_text(encoding="utf-8")

    def machine_revision(self, project, task):
        self.assertIsNotNone(task["machine_revision_id"], "the run has no machine revision pointer")
        directory = project / "revisions" / "machine" / task["machine_revision_id"]
        record = c.load_record("MachineRevision", directory / "manifest.json")
        from upgrade.stages import machine_revision_refs
        verify_committed(directory, machine_revision_refs(record))
        return record, directory


class ModeTests(StageTestCase):
    def test_combined_runs_every_stage_and_delivers_an_editable_machine_revision(self):
        self.register_engines()
        queued = self.submit("combined")
        summary = self.run_queue()
        envelope, project, task = self.task(queued)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        self.assertEqual(self.statuses(task), {name: ("DONE", None) for name in c.STAGES})
        for name in c.STAGES:
            directory = self.stage_dir(project, task, name)
            verify_committed(directory, c.load_record("StageManifest", directory / "manifest.json")["outputs"])
        record, revision_dir = self.machine_revision(project, task)
        self.assertEqual(summary["requests"][0]["machine_revision_dir"], str(revision_dir))
        # the deliverable: machine SRT + ASS, translations with evidence, quality report; all hashed in one immutable manifest
        srt = (revision_dir / "machine.zh.srt").read_bytes()
        self.assertTrue(srt.startswith(b"\xef\xbb\xbf1\r\n"))
        self.assertIn(b"\xe8\xaf\x91:", srt)                                                 # the scripted model echo ("译:")
        ass = (revision_dir / "machine.zh.ass").read_text(encoding="utf-8-sig")
        self.assertIn("Style: Default,Microsoft YaHei,80", ass)                            # styles inherited from the machine timeline (Magia output)
        self.assertEqual(ass.count("Dialogue: "), srt.count(b"-->"))
        segment_set = c.load_record("SegmentSet", project / "revisions" / "source" / record["source_revision_id"] / "segments.json")
        translations = c.load_record("TranslationSet", revision_dir / "translations.json")
        self.assertEqual(len(segment_set["segments"]), srt.count(b"-->"))
        self.assertEqual([t["cue_id"] for t in translations["records"]], [s["cue_id"] for s in segment_set["segments"]])
        self.assertEqual(translations["source_revision_id"], record["source_revision_id"])
        quality = c.load_record("QualityReport", revision_dir / "quality.json")
        self.assertEqual((quality["structural_status"], quality["human_status"], quality["confirmed_scopes"]), ("valid", "UNREVIEWED", []))
        scope = json.loads((revision_dir / "quality_scope.json").read_text(encoding="utf-8"))
        self.assertIn("QC05_COVERAGE", scope["evaluated_rules"])
        self.assertIn("QC08_SPEED", scope["evaluated_rules"])                              # W06: the soft rules run on the machine revision
        self.assertIn("QC22_FONT", scope["not_evaluated_rules"])                           # export-only rules are named as not evaluated, not faked
        self.assertEqual(scope["rules_version"], quality_module.QUALITY_RULES_VERSION)
        evidence_paths = {ref["path"] for ref in record["raw_evidence_refs"]}
        self.assertTrue({"quality_scope.json", "review.csv", "review.html"} <= evidence_paths)  # the review files are hashed in the manifest
        self.assertTrue((revision_dir / "review.csv").read_bytes().startswith(b'\xef\xbb\xbf"revision_id","cue_id"'))
        self.assertIn("../../source/" + record["source_revision_id"] + "/recognition/reference.asr.ja.srt", (revision_dir / "review.html").read_text(encoding="utf-8"))
        # source revision: canonical source SRT, v8 record, ASR/OCR reference subtitles
        source_dir = project / "revisions" / "source" / record["source_revision_id"]
        names = {p.relative_to(source_dir).as_posix() for p in source_dir.rglob("*") if p.is_file()}
        self.assertTrue({"source.ja.srt", "segments.json", "segments.v8.json", "recognition/reference.asr.ja.srt", "recognition/reference.ocr.ja.srt"} <= names)
        self.assertIn("ありがとう".encode("utf-8"), (source_dir / "recognition" / "reference.asr.ja.srt").read_bytes())
        self.assertIn("二行目のテロップ".encode("utf-8"), (source_dir / "recognition" / "reference.ocr.ja.srt").read_bytes())
        # project indexes and the translation server lifecycle
        project_record = r.load_project(self.root, task["project_id"])
        self.assertEqual((project_record["source_revision_ids"], project_record["machine_revision_ids"]), ([record["source_revision_id"]], [record["revision_id"]]))
        owner = json.loads((self.stage_dir(project, task, "TRANSLATION") / "server" / "server_owner.json").read_text(encoding="utf-8"))
        self.assertEqual((owner["state"], owner["stop_reason"]), ("STOPPED", "translation_stage_finished"))
        self.assertEqual(len(w04.requests_log(self.model)), 1)                              # all single-line cues in one batch request
        self.assertEqual([call["executable"] for call in self.double.calls], ["MagiaTimeline.exe", "whisper-cli.exe"])

    def test_each_mode_skips_exactly_what_it_does_not_need_and_still_delivers(self):
        self.register_engines()
        cases = [
            ("speech_only", self.video, {"OCR": ("SKIPPED", "MODE"), "ASR": ("DONE", None)}, ["whisper-cli.exe"], True, "base_v1"),
            ("visual_only", self.silent, {"OCR": ("DONE", None), "ASR": ("SKIPPED", "MODE")}, ["MagiaTimeline.exe"], False, "machine_timeline"),
        ]
        for mode, video, expected, launched, has_wav, style in cases:
            with self.subTest(mode):
                self.double.calls.clear()
                queued = self.submit(mode, video)
                self.run_queue()
                envelope, project, task = self.task(queued)
                self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
                statuses = self.statuses(task)
                for name, value in expected.items():
                    self.assertEqual(statuses[name], value, name)
                for name in ("SOURCE", "TRANSLATION", "SUBTITLE_BUILD", "QUALITY_AND_COMMIT"):
                    self.assertEqual(statuses[name], ("DONE", None), name)
                self.assertEqual([call["executable"] for call in self.double.calls], launched)
                prepared = json.loads((self.stage_dir(project, task, "MEDIA_PREP") / "prepare_result.json").read_text(encoding="utf-8"))
                self.assertEqual(prepared["audio_for_asr"] is not None, has_wav)
                self.assertEqual(bool(list((project / "media").rglob("*.wav"))), has_wav)
                record, revision_dir = self.machine_revision(project, task)
                self.assertEqual(json.loads((revision_dir / "style_profile.json").read_text(encoding="utf-8"))["source"], style)
                segment_set = c.load_record("SegmentSet", project / "revisions" / "source" / record["source_revision_id"] / "segments.json")
                self.assertEqual({s["role"] for s in segment_set["segments"]}, {"speech"} if mode == "speech_only" else {"screen"})

    def test_supplied_source_references_a_registered_revision_and_reuses_its_translation(self):
        self.register_engines()
        first = self.submit("combined")
        self.run_queue()
        _, project, first_task = self.task(first)
        first_record, _ = self.machine_revision(project, first_task)
        requests_before = len(w04.requests_log(self.model))
        self.double.calls.clear()
        # the command-line path: --project ID --source-revision ID --mode supplied_source, no video: the media comes from the revision
        supplied = self.submit("supplied_source", "supplied-source", project_id=first["request"]["project_id"], source_revision_id=first_record["source_revision_id"],
                               source_media_id=first_record["media_id"])
        self.run_queue()
        envelope, project, task = self.task(supplied)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        statuses = self.statuses(task)
        self.assertEqual((statuses["OCR"], statuses["ASR"]), (("SKIPPED", "USER_SOURCE"), ("SKIPPED", "USER_SOURCE")))
        self.assertEqual(self.double.calls, [])
        pointer = json.loads((self.stage_dir(project, task, "SOURCE") / "source_ref.json").read_text(encoding="utf-8"))
        self.assertEqual((pointer["source_revision_id"], pointer["reused_existing_revision"]), (first_record["source_revision_id"], True))
        self.assertEqual(len(w04.requests_log(self.model)), requests_before)                # the identical translation is reused: no server, no request
        self.assertIn("stage_cache_hit", self.events(project, task))
        self.assertEqual(task["machine_revision_id"], first_record["revision_id"])         # the same committed revision serves this run
        self.assertEqual(r.load_project(self.root, task["project_id"])["machine_revision_ids"], [first_record["revision_id"]])
        # a source revision bound to another media is refused before anything is translated
        other = self.submit("supplied_source", self.silent, project_id=first["request"]["project_id"], source_revision_id=first_record["source_revision_id"])
        self.run_queue()
        envelope, _, task = self.task(other)
        self.assertEqual((envelope["status"], envelope["last_error"]["code"], self.statuses(task)["SOURCE"][0]), ("FAILED", "MEDIA_CHANGED", "FAILED"))
        self.assertEqual(len(w04.requests_log(self.model)), requests_before)

    def test_empty_recognition_is_an_explicit_empty_revision_without_a_model_server(self):
        self.register_engines()
        self.double.whisper = "empty"
        queued = self.submit("speech_only")
        self.run_queue()
        envelope, project, task = self.task(queued)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        record, revision_dir = self.machine_revision(project, task)
        self.assertEqual((revision_dir / "machine.zh.srt").read_bytes(), b"\xef\xbb\xbf")       # a legal empty SRT
        ass = (revision_dir / "machine.zh.ass").read_text(encoding="utf-8-sig")
        self.assertIn("[Events]", ass)
        self.assertNotIn("Dialogue:", ass)
        quality = c.load_record("QualityReport", revision_dir / "quality.json")
        self.assertEqual([issue["rule_id"] for issue in quality["issues"] if issue["severity"] != "INFO"], ["QC23_EMPTY"])
        self.assertIn("speech_activity", quality["unknown_capabilities"])                  # QC16 says "unknown", it does not claim nothing was missed
        self.assertEqual(c.load_record("TranslationSet", revision_dir / "translations.json")["status"], "EMPTY")
        self.assertEqual(w04.requests_log(self.model), [])
        self.assertFalse((self.stage_dir(project, task, "TRANSLATION") / "server").exists())

    def test_speech_modes_on_silent_media_ask_for_visual_only_instead_of_switching(self):
        self.register_engines()
        for mode in ("combined", "speech_only"):
            with self.subTest(mode):
                queued = self.submit(mode, self.silent)
                self.run_queue()
                envelope, project, task = self.task(queued)
                self.assertEqual((envelope["status"], envelope["last_error"]["code"], envelope["last_error"]["details"]["kind"]),
                                 ("FAILED", "NEEDS_SELECTION", "no_audio_stream"))
                self.assertEqual(envelope["request"]["recognition_mode"], mode)   # the request is not rewritten
                self.assertEqual(self.statuses(task)["OCR"][0], "PENDING")
        self.assertEqual(self.double.calls, [])

    def test_unverifiable_clock_stops_at_media_prep(self):
        self.register_engines()
        glued = w03.make_discontinuous_ts(self.tmp / "glued.ts")
        queued = self.submit("visual_only", glued)
        self.run_queue()
        envelope, project, task = self.task(queued)
        statuses = self.statuses(task)
        self.assertEqual((statuses["INPUT"][0], statuses["MEDIA_PREP"][0], envelope["last_error"]["code"]), ("DONE", "FAILED", "MEDIA_CLOCK_DISCONTINUITY"))
        self.assertEqual(self.double.calls, [])


class ResourceAndCacheTests(StageTestCase):
    def test_missing_engines_are_named_before_anything_long_starts_and_resume_continues(self):
        queued = self.submit("combined")                                          # only ffmpeg/ffprobe are registered
        self.run_queue()
        envelope, project, task = self.task(queued)
        error = envelope["last_error"]
        self.assertEqual((envelope["status"], error["code"], self.statuses(task)["INPUT"][0]), ("FAILED", "RESOURCE_MISSING", "FAILED"))
        self.assertEqual(sorted(item["role"] for item in error["details"]["failures"]),
                         ["asr_engine", "asr_model", "ocr_engine", "translation_engine", "translation_model", "vad_model"])
        self.assertIn("llama-server", error["message"])
        self.assertEqual(self.double.calls, [])
        self.assertEqual(w04.requests_log(self.model), [])
        self.assertFalse((project / "media").exists() and any((project / "media").iterdir()))   # nothing was probed, copied or derived
        # fix the resources, resume the same request: a new attempt, same run, no new request
        self.register_engines()
        r.resume(self.root, envelope["run_id"])
        self.run_queue()
        resumed, _, task = self.task(queued)
        self.assertEqual((resumed["run_id"], resumed["status"]), (envelope["run_id"], "COMPLETED"))
        self.assertEqual([attempt["status"] for attempt in self.stage(task, "INPUT")["attempts"]], ["FAILED", "DONE"])
        self.assertEqual(self.statuses(task)["QUALITY_AND_COMMIT"], ("DONE", None))

    def test_changed_resource_content_is_refused(self):
        self.register_engines()
        queued = self.submit("speech_only")
        self.engine_paths["whisper-model"].write_bytes(b"a different model with the same file name")
        self.run_queue()
        envelope, _, _ = self.task(queued)
        self.assertEqual(envelope["last_error"]["code"], "RESOURCE_CHANGED")
        self.assertEqual([item["role"] for item in envelope["last_error"]["details"]["failures"]], ["asr_model"])
        self.assertEqual(self.double.calls, [])

    def test_only_real_dependencies_invalidate_recognition_and_translation(self):
        self.register_engines()
        first = self.submit("combined")
        self.run_queue()
        _, project, first_task = self.task(first)
        self.assertEqual(len(self.double.calls), 2)
        requests_after_first = len(w04.requests_log(self.model))
        # another timing mode over the same evidence: no engine runs again (plan 10.3); the source changes, so it is translated again
        second = self.submit("combined", timing_mode="speech", project_id=first["request"]["project_id"])
        self.run_queue()
        _, _, second_task = self.task(second)
        self.assertEqual(len(self.double.calls), 2)
        self.assertEqual(self.events(project, second_task).count("stage_cache_hit"), 4)
        for name in ("OCR", "ASR"):
            self.assertEqual(self.stage(second_task, name)["output_manifest"], self.stage(first_task, name)["output_manifest"])
        self.assertNotEqual(self.stage(second_task, "SOURCE")["output_manifest"], self.stage(first_task, "SOURCE")["output_manifest"])
        self.assertGreater(len(w04.requests_log(self.model)), requests_after_first)
        self.assertNotEqual(second_task["machine_revision_id"], first_task["machine_revision_id"])
        self.assertEqual(len(r.load_project(self.root, first["request"]["project_id"])["machine_revision_ids"]), 2)
        # a new OCR region: on-screen text is recognized again, the speech evidence is kept
        third = self.submit("combined", project_id=first["request"]["project_id"],
                            settings={"ocr": {"roi": {"left": 0.0, "top": 0.5, "right": 1.0, "bottom": 1.0}}})
        self.run_queue()
        _, _, third_task = self.task(third)
        self.assertEqual([call["executable"] for call in self.double.calls[2:]], ["MagiaTimeline.exe"])
        self.assertEqual(self.stage(third_task, "ASR")["output_manifest"], self.stage(first_task, "ASR")["output_manifest"])
        self.assertNotEqual(self.stage(third_task, "OCR")["output_manifest"], self.stage(first_task, "OCR")["output_manifest"])
        visual = c.load_record("VisualEvidence", self.stage_dir(project, third_task, "OCR") / "visual_evidence.json")
        self.assertEqual(visual["roi"], {"left": 0.0, "top": 0.5, "right": 1.0, "bottom": 1.0})
        # a queued request keeps the settings it was submitted with, whatever the global settings say later
        self.assertEqual(third["request"]["config_snapshot"]["engine_settings"]["ocr"]["roi"]["top"], 0.5)
        # a translation parameter change keeps every recognition and the source; only TRANSLATION and its downstream run again
        requests_before = len(w04.requests_log(self.model))
        fourth = self.submit("combined", project_id=first["request"]["project_id"], settings={"translation": {"endpoint": f"http://127.0.0.1:{self.port}", "temperature": 0.1}})
        self.run_queue()
        _, _, fourth_task = self.task(fourth)
        self.assertEqual(len(self.double.calls), 3)
        self.assertEqual(self.stage(fourth_task, "SOURCE")["output_manifest"], self.stage(first_task, "SOURCE")["output_manifest"])
        self.assertNotEqual(self.stage(fourth_task, "TRANSLATION")["output_manifest"], self.stage(first_task, "TRANSLATION")["output_manifest"])
        self.assertGreater(len(w04.requests_log(self.model)), requests_before)
        self.assertEqual(w04.requests_log(self.model)[-1]["payload"]["temperature"], 0.1)
        # speech-only over the same audio reuses the ASR evidence as well
        fifth = self.submit("speech_only", project_id=first["request"]["project_id"])
        self.run_queue()
        _, _, fifth_task = self.task(fifth)
        self.assertEqual(len(self.double.calls), 3)
        self.assertEqual(self.stage(fifth_task, "ASR")["output_manifest"], self.stage(first_task, "ASR")["output_manifest"])

    def test_failed_translation_leaves_no_revision_pointer_and_resumes_from_checkpoints(self):
        self.register_engines()
        w04.set_behaviour(self.model, responses=[{"kind": "echo"}, {"kind": "drop"}, {"kind": "drop"}, {"kind": "drop"}])
        queued = self.submit("speech_only", settings={"translation": {"endpoint": f"http://127.0.0.1:{self.port}", "batch_size": 1}})
        self.run_queue()
        envelope, project, task = self.task(queued)
        self.assertEqual((envelope["status"], envelope["last_error"]["code"], self.statuses(task)["TRANSLATION"][0]), ("FAILED", "PROCESS_FAILED", "FAILED"))
        self.assertIsNone(task["machine_revision_id"])
        self.assertEqual(r.load_project(self.root, task["project_id"])["machine_revision_ids"], [])
        self.assertFalse(list((project / "revisions" / "machine").iterdir()))                  # no revision directory, not even a candidate
        self.assertEqual(len(w04.requests_log(self.model)), 4)                                  # cue 1 once, cue 2 three times
        checkpoints = r.run_dir(project, envelope["run_id"]) / "checkpoints" / "translation" / "cues"
        self.assertEqual(sorted(p.name for p in checkpoints.iterdir()), ["cue-000001.json"])
        # the model behaves; resume translates only the pending cue and commits the revision
        w04.set_behaviour(self.model)
        r.resume(self.root, envelope["run_id"])
        self.run_queue()
        envelope, project, task = self.task(queued)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        self.assertEqual(len(w04.requests_log(self.model)), 1)
        self.assertEqual([attempt["status"] for attempt in self.stage(task, "TRANSLATION")["attempts"]], ["FAILED", "DONE"])
        translations = c.load_record("TranslationSet", self.stage_dir(project, task, "TRANSLATION") / "translations.json")
        self.assertEqual(translations["extensions"]["stats"], {"requests": 1, "ambiguous_splits": 0, "resumed_cues": 1})
        self.machine_revision(project, task)

    def test_explicit_audio_stream_selects_what_is_recognized(self):
        self.register_engines()
        unselected = self.submit("speech_only", self.two_audio)
        chosen = self.submit("speech_only", self.two_audio, audio_stream_index=2)
        self.run_queue()
        envelope, _, _ = self.task(unselected)
        self.assertEqual((envelope["last_error"]["code"], envelope["last_error"]["details"]["kind"]), ("NEEDS_SELECTION", "audio_stream"))
        _, project, task = self.task(chosen)
        record = c.load_record("MediaRecord", self.stage_dir(project, task, "MEDIA_PREP") / "media_record.json")
        self.assertEqual((record["selected_audio_stream"], self.statuses(task)["ASR"]), (2, ("DONE", None)))
        self.assertEqual(len(self.double.launched("whisper")), 1)


class UrlInputTests(StageTestCase):
    def test_single_video_url_is_downloaded_by_yt_dlp_then_handled_like_a_local_file(self):
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self.skipTest("yt-dlp is not installed in this environment")
        self.register_engines("whisper-cli", "whisper-model", "whisper-vad-model", "llama-server", "sakura-model")
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(self.served))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/clip.mp4"
            queued = self.submit("speech_only", url, input_kind="url")
            self.run_queue()
        finally:
            server.shutdown()
            server.server_close()
        envelope, project, task = self.task(queued)
        self.assertEqual(envelope["status"], "COMPLETED", envelope["last_error"])
        record = c.load_record("MediaRecord", self.stage_dir(project, task, "INPUT") / "media_record.json")
        source = Path(record["source_ref"]["absolute_path"])
        downloads = project / "media" / "downloads"
        self.assertEqual(source.parent.parent.parent, downloads)                  # the download lives in this project, committed once
        self.assertEqual((record["source_sha256"], record["extensions"]["origin"]["kind"], record["extensions"]["origin"]["url"]),
                         (c.sha256_file(self.video), "url", url))
        manifest = c.load_record("MediaManifest", source.parent.parent / "manifest.json")
        verify_committed(source.parent.parent, manifest["outputs"])
        names = {Path(ref["path"]).name for ref in manifest["outputs"]}
        self.assertIn("video.info.json", names)                                   # info JSON and the real downloaded file are both kept
        argv = json.loads((source.parent.parent / "logs" / "yt-dlp" / "process.json").read_text(encoding="utf-8"))["argv"]
        self.assertIn("--no-playlist", argv)
        self.assertEqual(argv[-1], url)


class CommandLineTests(StageTestCase):
    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        return completed.returncode, json.loads(completed.stdout) if completed.stdout.strip() else None, completed.stderr

    def test_submit_resolves_streams_or_asks_and_queue_run_uses_the_real_stages(self):
        config = str(self.root / APP_CONFIG_NAME)
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.two_audio), "--mode", "speech_only")
        self.assertEqual((code, result["ok"], result["error"]["code"], result["error"]["details"]["kind"]), (2, False, "NEEDS_SELECTION", "audio_stream"))
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.silent), "--mode", "combined")
        self.assertEqual((code, result["error"]["code"], result["error"]["details"]["kind"]), (2, "NEEDS_SELECTION", "no_audio_stream"))
        self.assertEqual(r.list_envelopes(self.root), [])                         # nothing ambiguous was queued
        code, result, stderr = self.cli("submit", "--config", config, "--video", str(self.two_audio), "--mode", "speech_only", "--audio-stream", "2")
        self.assertEqual((code, result["ok"], result["warnings"]), (0, True, []), stderr)
        request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        self.assertEqual((request["video_stream_index"], request["audio_stream_index"]), (0, 2))
        self.assertEqual(request["dictionary_revision_id"], result["ids"]["dictionary_revision_id"])
        self.assertTrue((self.root / "dictionaries" / request["dictionary_revision_id"] / "manifest.json").is_file())   # the bundled dictionaries were frozen
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.video), "--mode", "visual_only", "--audio-stream", "none")
        silent_request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        self.assertEqual((code, silent_request["audio_stream_index"], silent_request["extensions"]["audio_stream"]), (0, None, "none"))
        # the queue command runs the registered stages; without engines the first request stops with a named resource error
        code, result, _ = self.cli("queue", "run", "--data-root", str(self.root))
        self.assertEqual((code, result["ids"]["processed"], result["ids"]["failed"]), (0, 2, 2))
        self.assertEqual({row["error"] for row in result["ids"]["requests"]}, {"RESOURCE_MISSING"})
        errors = [envelope["last_error"] for envelope in r.list_envelopes(self.root)]
        self.assertEqual({error["code"] for error in errors}, {"RESOURCE_MISSING"})
        code, result, _ = self.cli("worker", "no-such-worker")
        self.assertEqual((code, result["ok"]), (2, False))

    def test_unsupported_dictionary_entries_need_the_explicit_flag_once(self):
        config = str(self.root / APP_CONFIG_NAME)
        paths = w04.write_dictionary_files(self.tmp / "用户词典")
        save_engine_settings(self.root / ENGINE_SETTINGS_NAME, w04.dictionary_settings(paths, endpoint=f"http://127.0.0.1:{self.port}"))
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.video), "--mode", "speech_only")
        self.assertEqual((code, result["error"]["code"]), (2, "DICTIONARY_UNSUPPORTED"))
        shown = result["error"]["details"]["unsupported"]
        self.assertEqual([(item["role"], item["line"]) for item in shown], [("gpt", 6), ("gpt", 7), ("gpt", 8)])
        self.assertEqual(r.list_envelopes(self.root), [])
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.video), "--mode", "speech_only", "--accept-dictionary-warnings")
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(result["warnings"][0]["code"], "DICTIONARY_UNSUPPORTED_ACCEPTED")
        request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        self.assertEqual(len(request["config_snapshot"]["accepted_dictionary_warnings"]), 3)
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.video), "--mode", "speech_only")   # same content: no second question
        self.assertEqual((code, result["ok"]), (0, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
