"""T03 (recognition fixtures, W03): the Whisper and MagiaTimeline adapters behind ``recognize``.

Engine launches are replaced by doubles that write engine-shaped output (see tests/support/w03.py). This verifies the
adapter protocol: command lines, ASCII staging, raw evidence retention, the single clock-recovery retry, failure and
empty handling, and the two evidence records. It does not verify recognition accuracy of any real engine.
"""
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

import yaml

from support import w03
from upgrade import contracts as c
from upgrade import recognition
from upgrade.storage import artifact_ref, verify_committed

ROI = {"left": 0.1, "top": 0.6, "right": 0.9, "bottom": 0.95}


def resource(resource_id: str, path: Path) -> dict:
    return {"absolute_path": str(path), "resource_ref": {"resource_id": resource_id, "content_sha256": c.sha256_text(resource_id), "manifest_sha256": "b" * 64}}


class RecognitionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "项目 (recognition)"
        self.audio = w03.write_silent_wav(self.project / "media" / "m" / "derived" / "asr-wav-x" / "audio.wav")
        self.video = self.project / "media" / "m" / "derived" / "review-video-x" / "video.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"not decoded by the adapter: the engine double never opens it" * 10)
        paths = w03.make_engine_resources(self.tmp / "engines")
        self.paths = paths
        self.resources = {"asr_engine": resource("whisper-cli", paths["whisper-cli"]), "asr_model": resource("whisper-model", paths["whisper-model"]),
                          "vad_model": resource("whisper-vad-model", paths["whisper-vad-model"]), "ocr_engine": resource("magia-timeline", paths["magia-timeline"])}
        self.ascii_root = self.tmp / "ascii"
        self.attempt = 0
        self.events = []

    def tearDown(self):
        self._tmp.cleanup()

    def runtime(self):
        self.attempt += 1
        self.output = self.project / "runs" / "r" / "stages" / "X" / f"attempt-{self.attempt}"
        self.output.mkdir(parents=True)
        return recognition.RecognitionRuntime(project_dir=self.project, output_dir=self.output, ascii_temp_root=self.ascii_root, resources=self.resources,
                                              owner_token="t03", emit=self.events.append)

    def request(self, mode, **engine_settings):
        return {"run_id": c.new_id(), "media_id": c.new_id(), "recognition_mode": mode,
                "video_artifact": artifact_ref(self.project, self.video) if mode in ("combined", "visual_only") else None,
                "audio_artifact": artifact_ref(self.project, self.audio) if mode in ("combined", "speech_only") else None,
                "roi": ROI, "engine_settings": engine_settings, "resource_refs": []}

    def recognize(self, mode, double, **engine_settings):
        with patch.object(recognition, "run_engine", double), patch.object(recognition, "prepare_utf8_executable", w03.passthrough_utf8):
            return recognition.recognize(self.request(mode, **engine_settings), runtime=self.runtime())

    def staging_is_clean(self):
        return not any(p for kind in ("asr", "magia") for p in (self.ascii_root / kind).glob("*")) if self.ascii_root.exists() else True


class SpeechTests(RecognitionTestCase):
    def test_native_output_becomes_speech_evidence_with_exact_character_ranges(self):
        double = w03.EngineDouble()
        result = self.recognize("speech_only", double)
        evidence = result["speech_evidence"]
        self.assertEqual(c.validate_record("SpeechEvidence", evidence), [])
        self.assertIsNone(result["visual_evidence"])
        self.assertEqual(result["skip_reasons"], {"ocr": "MODE"})
        self.assertEqual([(p["id"], p["start_ms"], p["end_ms"], p["text"]) for p in evidence["parents"]],
                         [("asr-000001", 500, 1500, "はい、そうです"), ("asr-000002", 1800, 2900, "ありがとう")])
        by_parent = {p["id"]: p["text"] for p in evidence["parents"]}
        pieces = [(w["parent_id"], by_parent[w["parent_id"]][w["char_start"]:w["char_end"]], w["start_ms"], w["end_ms"], w["timing_valid"]) for w in evidence["word_times"]]
        self.assertEqual(pieces, [("asr-000001", "はい", 520, 800, True), ("asr-000001", "そうです", 860, 1480, True),
                                  ("asr-000002", "ありが", 1820, 2300, True), ("asr-000002", "とう", 2300, 2880, True)])  # code points, punctuation carries no clock
        # scores come from the native token probabilities; nothing else is claimed
        self.assertEqual([(p["score"], p["score_kind"]) for p in evidence["parents"]], [(0.61, "whisper_token_probability"), (0.42, "whisper_token_probability")])
        self.assertEqual((evidence["coverage_status"], evidence["activity_intervals"], evidence["processing_intervals"], evidence["language"]),
                         ("process_completed_only", None, [{"start_ms": 0, "end_ms": 3000}], "ja"))
        self.assertEqual(evidence["extensions"]["word_timing_status"], "usable")
        # the legacy command line, as one argv list
        call = double.calls[0]
        argv = call["argv"]
        staged_audio, base = Path(argv[5]), Path(argv[7])
        self.assertEqual(call["executable"], "whisper-cli.exe")
        self.assertEqual(argv, ["-m", str(self.paths["whisper-model"]), "-osrt", "-l", "ja", str(staged_audio), "-of", str(base),
                                "-ojf", "-dtw", "large.v3", "--vad", "--vad-model", str(self.paths["whisper-vad-model"])])
        self.assertTrue(str(staged_audio).isascii() and str(base).isascii())      # whisper.cpp only ever sees ASCII paths
        self.assertFalse(str(self.audio).isascii())                               # ...although the project lives in a non-ASCII folder
        self.assertEqual(len(double.calls), 1)
        self.assertTrue(self.staging_is_clean())
        # raw engine files are kept beside the normalized evidence and are covered by hashes
        paths = {ref["path"] for ref in evidence["raw_refs"]}
        self.assertTrue({"asr/vad/jp.srt", "asr/vad/jp.whisper.json", "asr/attempts.json", "asr/vad/process/process.json"} <= paths, paths)
        verify_committed(self.output, evidence["raw_refs"])
        self.assertEqual(json.loads((self.output / "asr/vad/jp.whisper.json").read_text(encoding="utf-8"))["transcription"][0]["text"], "はい、そうです")

    def test_vad_clock_conflict_retries_the_same_audio_once_without_vad_and_keeps_both(self):
        double = w03.EngineDouble(whisper="conflict")
        result = self.recognize("speech_only", double)
        evidence = result["speech_evidence"]
        self.assertEqual(len(double.calls), 2)
        self.assertIn("--vad", double.calls[0]["argv"])
        self.assertNotIn("--vad", double.calls[1]["argv"])
        self.assertNotIn("--vad-model", double.calls[1]["argv"])
        for index in (1, 5):                                                     # same model, same complete audio
            self.assertEqual(double.calls[0]["argv"][index], double.calls[1]["argv"][index])
        recovery = json.loads((self.output / "asr/recovery.json").read_text(encoding="utf-8"))
        self.assertEqual((recovery["state"], recovery["selected"], recovery["reason"]), ("FINISHED", "without_vad", "NATIVE_TIME_COORDINATES_CONFLICT"))
        self.assertEqual((evidence["extensions"]["selected_attempt"], evidence["extensions"]["word_timing_status"]), ("without_vad", "usable"))
        self.assertTrue(all(word["timing_valid"] for word in evidence["word_times"]))
        self.assertNotEqual((self.output / "asr/vad/jp.whisper.json").read_bytes(), (self.output / "asr/without_vad/jp.whisper.json").read_bytes())
        self.assertEqual([a["word_timing_status"] for a in evidence["extensions"]["attempts"]], ["unusable", "usable"])

    def test_unrecovered_clock_keeps_utterances_marks_them_and_never_interpolates(self):
        for behavior, launches in (("conflict_always", 2), ("recovery_exit7", 2)):
            with self.subTest(behavior):
                double = w03.EngineDouble(whisper=behavior)
                result = self.recognize("speech_only", double)
                evidence = result["speech_evidence"]
                self.assertEqual(len(double.calls), launches)
                self.assertEqual((evidence["extensions"]["selected_attempt"], evidence["extensions"]["word_timing_status"]), ("vad", "unusable"))
                self.assertEqual([(p["start_ms"], p["end_ms"], p["flags"]) for p in evidence["parents"]],
                                 [(500, 1500, ["WORD_CLOCK_REJECTED"]), (1800, 2900, ["WORD_CLOCK_REJECTED"])])
                self.assertTrue(evidence["word_times"] and not any(word["timing_valid"] for word in evidence["word_times"]))  # native values kept, flagged unusable
                self.assertIn("ASR_WORD_CLOCK_UNUSABLE", [issue["code"] for issue in result["issues"]])
                self.assertEqual(c.validate_record("SpeechEvidence", evidence), [])
        recovery = json.loads((self.output / "asr/recovery.json").read_text(encoding="utf-8"))
        self.assertEqual((recovery["selected"], recovery["recovery_exit_code"], recovery["recovery_valid"]), ("vad", 7, False))

    def test_without_vad_there_is_no_retry_loop(self):
        double = w03.EngineDouble(whisper="conflict_always")
        del self.resources["vad_model"]                                           # not needed when VAD is off
        evidence = self.recognize("speech_only", double, recognition={"vad": False})["speech_evidence"]
        self.assertEqual(len(double.calls), 1)
        self.assertNotIn("--vad", double.calls[0]["argv"])
        self.assertEqual((evidence["extensions"]["selected_attempt"], evidence["extensions"]["vad"], evidence["extensions"]["vad_model_ref"]), ("no_vad", False, None))

    def test_failed_engine_run_is_not_accepted_and_its_output_stays_a_candidate(self):
        for behavior, fragment in (("exit7", "exited with code 7"), ("corrupt_json", "native JSON is incomplete")):
            with self.subTest(behavior):
                with self.assertRaises(c.ContractError) as ctx:
                    self.recognize("speech_only", w03.EngineDouble(whisper=behavior))
                self.assertEqual(ctx.exception.code, "PROCESS_FAILED")
                self.assertIn(fragment, ctx.exception.message)
                self.assertTrue((self.output / "asr/vad/jp.srt").is_file())       # residual SRT is kept for diagnosis, never promoted
                self.assertIsNone(json.loads((self.output / "asr/attempts.json").read_text(encoding="utf-8"))["selected"])
                self.assertTrue(self.staging_is_clean())

    def test_empty_speech_is_a_warning_not_an_error_and_not_proof_of_silence(self):
        result = self.recognize("speech_only", w03.EngineDouble(whisper="empty"))
        evidence = result["speech_evidence"]
        self.assertEqual(c.validate_record("SpeechEvidence", evidence), [])
        self.assertEqual((evidence["parents"], evidence["word_times"], evidence["extensions"]["asr_empty"], evidence["coverage_status"]),
                         ([], [], True, "process_completed_only"))
        issue = next(issue for issue in result["issues"] if issue["code"] == "ASR_EMPTY")
        self.assertEqual(issue["severity"], "WARNING")

    def test_missing_scores_are_null_and_reported_once(self):
        result = self.recognize("speech_only", w03.EngineDouble(whisper="no_scores"))
        self.assertEqual([(p["score"], p["score_kind"]) for p in result["speech_evidence"]["parents"]], [(None, None), (None, None)])
        self.assertEqual([issue["code"] for issue in result["issues"]].count("ASR_SCORE_UNKNOWN"), 1)

    def test_prechecks_stop_before_any_engine_starts(self):
        double = w03.EngineDouble()
        vad = self.resources.pop("vad_model")
        with self.assertRaises(c.ContractError) as ctx:
            self.recognize("speech_only", double)
        self.assertEqual((ctx.exception.code, ctx.exception.details["role"]), ("RESOURCE_MISSING", "vad_model"))
        self.resources["vad_model"] = vad
        moved = self.tmp / "模型" / "ggml-large-v3.bin"
        moved.parent.mkdir()
        moved.write_bytes(b"fixture model")
        self.resources["asr_model"] = resource("whisper-model", moved)
        with self.assertRaises(c.ContractError) as ctx:
            self.recognize("speech_only", double)
        self.assertEqual(ctx.exception.code, "PATH_INVALID")
        self.resources["asr_model"] = resource("whisper-model", self.tmp / "engines" / "whisper" / "bin" / "other-model.bin")
        (self.tmp / "engines" / "whisper" / "bin" / "other-model.bin").write_bytes(b"x")
        with self.assertRaises(c.ContractError) as ctx:
            self.recognize("speech_only", double)                                 # word timestamps need a DTW preset for an unknown model
        self.assertEqual(ctx.exception.code, "CAPABILITY_MISSING")
        self.assertEqual(double.calls, [])


class VisualTests(RecognitionTestCase):
    def test_engine_intervals_become_visual_evidence_without_invented_scores(self):
        double = w03.EngineDouble()
        video_before = self.video.read_bytes()
        result = self.recognize("visual_only", double)
        evidence = result["visual_evidence"]
        self.assertEqual(c.validate_record("VisualEvidence", evidence), [])
        self.assertIsNone(result["speech_evidence"])
        self.assertEqual(result["skip_reasons"], {"asr": "MODE"})
        self.assertEqual([(e["id"], e["ordinal"], e["start_ms"], e["end_ms"], e["text"], e["flags"]) for e in evidence["events"]],
                         [("ocr-000001", 0, 500, 1200, "こんにちは、世界", []), ("ocr-000002", 1, 1500, 2000, "", ["ENGINE_PLACEHOLDER_TEXT"]),
                          ("ocr-000003", 2, 2100, 2900, "二行目のテロップ", [])])
        self.assertTrue(all(e["score"] is None and e["score_kind"] is None and e["boxes"] is None and e["frame_refs"] == [] for e in evidence["events"]))
        self.assertEqual((evidence["evidence_level"], evidence["roi"], evidence["extensions"]["score_status"], evidence["extensions"]["text_event_count"]),
                         ("interval_only", ROI, "unknown", 2))
        self.assertEqual([issue["code"] for issue in result["issues"]], ["OCR_SCORE_UNKNOWN"])   # once per run, not once per event
        # per-job ASCII configuration with the legacy rectangle order [left, right, top, bottom]
        config = yaml.safe_load((self.output / "ocr/config.ascii.yml").read_text(encoding="ascii"))
        self.assertEqual(config["dtd"]["default"]["dialogRect"], [0.1, 0.9, 0.6, 0.95])
        self.assertEqual((evidence["extensions"]["dialog_rect_legacy_order"], evidence["extensions"]["roi_applied_to_preset"]), ([0.1, 0.9, 0.6, 0.95], True))
        self.assertEqual((config["strategy"], config["preset"], config["engine"], config["maxResolution"], config["extraJobs"]), ("dtd", "default", "speculative", 0, ["ocr"]))
        self.assertEqual((config["ocr"]["doPaddle"], config["ocr"]["doTesseract"]), (True, False))
        self.assertTrue(Path(config["source"][0]).name == "input.mp4" and config["source"][0].isascii())
        call = double.calls[0]
        self.assertEqual((call["executable"], call["argv"], call["stdin"], call["env"]), ("MagiaTimeline.exe", [], b"\n", ["PYTHONIOENCODING", "PYTHONUTF8"]))
        self.assertTrue(call["cwd"].isascii())
        self.assertFalse(Path(call["cwd"]).exists())                              # staging is removed afterwards
        self.assertEqual(self.video.read_bytes(), video_before)
        # the engine's own bytes are kept; the normalized copy is separate
        self.assertEqual((self.output / "ocr/native_output.ass").read_bytes(), (w03.ASS_HEADER + w03.ASS_EVENTS).encode("utf-8-sig"))
        self.assertTrue({"ocr/native_output.ass", "ocr/timeline.utf8.ass", "ocr/config.ascii.yml", "ocr/runtime_compatibility.json"}
                        <= {ref["path"] for ref in evidence["raw_refs"]})
        verify_committed(self.output, evidence["raw_refs"])

    def test_no_on_screen_text_is_an_explicit_empty_result(self):
        result = self.recognize("visual_only", w03.EngineDouble(magia="empty"))
        evidence = result["visual_evidence"]
        self.assertEqual(c.validate_record("VisualEvidence", evidence), [])
        self.assertEqual((evidence["events"], evidence["extensions"]["visual_empty"]), ([], True))   # no blank cue is fabricated
        self.assertEqual(sorted(issue["code"] for issue in result["issues"]), ["OCR_EMPTY", "OCR_SCORE_UNKNOWN"])

    def test_engine_failures_keep_candidates_and_produce_no_evidence(self):
        for behavior, code, kept in (("exit3", "PROCESS_FAILED", True), ("no_output", "PROCESS_FAILED", False), ("bad_time", "SUBTITLE_INVALID", True)):
            with self.subTest(behavior):
                with self.assertRaises(c.ContractError) as ctx:
                    self.recognize("visual_only", w03.EngineDouble(magia=behavior))
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual((self.output / "ocr/native_output.ass").is_file(), kept)
                self.assertTrue(self.staging_is_clean())

    def test_windows_codepage_output_is_decoded_without_loss_and_raw_bytes_are_kept(self):
        result = self.recognize("visual_only", w03.EngineDouble(magia="acp"))
        evidence = result["visual_evidence"]
        self.assertEqual([e["text"] for e in evidence["events"]], ["ABC"])
        self.assertEqual(evidence["extensions"]["output_encoding"]["output_encoding"], "utf-8-sig")
        self.assertTrue((self.output / "ocr/native_output.ass").read_bytes().startswith(b"\xef\xbb\xbf[Script Info]"))

    def test_settings_reach_the_engine_configuration(self):
        double = w03.EngineDouble()
        result = self.recognize("visual_only", double, ocr={"strategy": "dtd", "preset": "no_region", "engine": "framewise", "max_resolution": 720, "ocr": False})
        config = yaml.safe_load((self.output / "ocr/config.ascii.yml").read_text(encoding="ascii"))
        self.assertEqual((config["preset"], config["engine"], config["maxResolution"], config["extraJobs"]), ("no_region", "framewise", 720, []))
        self.assertEqual(result["visual_evidence"]["extensions"]["roi_applied_to_preset"], False)  # that preset has no dialogRect; it is not forced in
        with self.assertRaises(c.ContractError) as ctx:
            self.recognize("visual_only", double, ocr={"engine": "rapidocr"})
        self.assertEqual(ctx.exception.code, "CONFIG_UNSUPPORTED_COMMAND")

    def test_incomplete_engine_resource_is_reported_before_start(self):
        (self.paths["magia-timeline"] / "template.asst").unlink()
        double = w03.EngineDouble()
        with self.assertRaises(c.ContractError) as ctx:
            self.recognize("visual_only", double)
        self.assertEqual((ctx.exception.code, ctx.exception.details["missing"]), ("RESOURCE_MISSING", "template.asst"))
        self.assertEqual(double.calls, [])


class InterfaceTests(RecognitionTestCase):
    def test_mode_decides_what_runs_and_what_is_skipped(self):
        double = w03.EngineDouble()
        supplied = self.recognize("supplied_source", double)
        self.assertEqual((supplied["speech_evidence"], supplied["visual_evidence"], supplied["skip_reasons"], double.calls),
                         (None, None, {"ocr": "USER_SOURCE", "asr": "USER_SOURCE"}, []))
        combined = self.recognize("combined", double)
        self.assertEqual([call["executable"] for call in double.calls], ["MagiaTimeline.exe", "whisper-cli.exe"])  # serial: on-screen text first, then speech
        self.assertEqual((combined["skip_reasons"], bool(combined["speech_evidence"]), bool(combined["visual_evidence"])), ({}, True, True))
        self.assertEqual(combined["speech_evidence"]["media_id"], combined["visual_evidence"]["media_id"])
        self.assertTrue((self.output / "ocr").is_dir() and (self.output / "asr").is_dir())
        # neither source overwrites the other: both texts are kept as recognized
        self.assertEqual(combined["visual_evidence"]["events"][0]["text"], "こんにちは、世界")
        self.assertEqual(combined["speech_evidence"]["parents"][0]["text"], "はい、そうです")

    def test_request_contract_and_changed_inputs(self):
        request = self.request("speech_only")
        request["audio_artifact"] = None
        with self.assertRaises(c.ContractError) as ctx:
            recognition.recognize(request, runtime=self.runtime())
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        double = w03.EngineDouble()
        stale = self.request("speech_only")
        w03.write_silent_wav(self.audio, 1000)                                    # the file no longer matches its recorded identity
        with patch.object(recognition, "run_engine", double), self.assertRaises(c.ContractError) as ctx:
            recognition.recognize(stale, runtime=self.runtime())
        self.assertEqual(ctx.exception.code, "MEDIA_CHANGED")
        self.assertEqual(double.calls, [])
        self.assertEqual(recognition.roi_to_dialog_rect({"left": 0.0, "top": 0.25, "right": 1.0, "bottom": 0.75}), [0.0, 1.0, 0.25, 0.75])


if __name__ == "__main__":
    unittest.main(verbosity=2)
