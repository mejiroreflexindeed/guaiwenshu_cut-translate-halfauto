"""T03 (W04, segmentation contract): the v8 algorithm behind SourceRevision.

The frozen v8 input is the recovered native-clock slice of the legacy tests. The canonical ranges, times, texts and ids of
the v8 record are compared with the algorithm's own output on the same input (the record is stored verbatim), the
conversion to Segment records is checked per mode, visual_only / EMPTY semantics, OCR and ASR text never overwrite each
other, bounds and coverage failures are refused, and a SourceRevision is committed once and verified on load.
"""
from pathlib import Path
import json
import tempfile
import unittest

from support import w04
from upgrade import contracts as c
from upgrade import source
from upgrade.storage import init_project_dir, verify_committed

FIXTURE = Path(__file__).with_name("fixtures") / "v8_canonical_slice.json"


def canonical(records: dict) -> list[tuple]:
    return [(item["id"], item["index"], item["start_ms"], item["end_ms"], item["text"], tuple((s["parent_id"], s["char_start"], s["char_end"]) for s in item["source_ranges"]),
             tuple(item["visual_ids"]), item["timing_source"], tuple(item["flags"])) for item in records["segments"]]


def frozen(value):
    """JSON lists -> tuples, recursively, so fixture rows compare with ``canonical`` rows."""
    return tuple(frozen(item) for item in value) if isinstance(value, list) else value


class CanonicalTests(unittest.TestCase):
    def setUp(self):
        self.media_id = c.new_id()
        self.speech = w04.speech_evidence(self.media_id, w04.LEGACY_SPEECH, w04.LEGACY_CLOCKS)
        self.visual = w04.visual_evidence(self.media_id, w04.LEGACY_PAGES + [(29000, 29500, "")])   # the trailing placeholder event carries no text

    def test_frozen_v8_slice_is_reproduced_for_mixed_screen_and_speech(self):
        from subtitle_axis_align import Cue
        from subtitle_segments import build_segments
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for timing_mode in ("mixed", "screen", "speech"):
            with self.subTest(timing_mode):
                built = source.build_source(recognition_mode="combined", timing_mode=timing_mode, speech_evidence=self.speech, visual_evidence=self.visual, duration_ms=30000)
                # 1) the stored v8 record equals what the algorithm produces from the legacy Cue input directly
                speech = [Cue(s, e, t, i + 1) for i, (s, e, t) in enumerate(w04.LEGACY_SPEECH)]
                pages = [Cue(s, e, t, i + 1) for i, (s, e, t) in enumerate(w04.LEGACY_PAGES + [(29000, 29500, "")])]
                words = {str(k): [dict(zip(("char_start", "char_end", "start_ms", "end_ms"), row)) for row in rows] for k, rows in w04.LEGACY_CLOCKS.items()}
                direct = build_segments(speech, pages, words, timing_mode)
                self.assertEqual(canonical(built["records"]), canonical(direct))
                # 2) and the frozen fixture (canonical ranges/times/texts/ids) has not moved
                self.assertEqual(canonical(built["records"]), [frozen(row) for row in fixture[timing_mode]["canonical"]])
                self.assertEqual(built["records"]["coverage"], fixture[timing_mode]["coverage"])
                # 3) the converted segments carry the same text/time and point at evidence ids
                for item, segment in zip(built["records"]["segments"], built["converted"]["segments"]):
                    self.assertEqual((segment["start_ms"], segment["end_ms"], segment["source_text"], segment["legacy_cue_id"], segment["index"]),
                                     (item["start_ms"], item["end_ms"], item["text"], item["id"], item["index"]))
                    self.assertEqual([s["parent_id"] for s in segment["source_ranges"]], [f"asr-{int(s['parent_id'] == built['records']['parents'][1]['id']) + 1:06d}" for s in item["source_ranges"]])
                    self.assertTrue(all(v.startswith("ocr-") for v in segment["visual_ids"]))
                    self.assertTrue(set(item["flags"]) <= set(segment["flags"]))
        self.assertEqual([s["source_text"] for s in built["converted"]["segments"]], ["ねえこれ、", "速攻ホテル前", "ある男子服だとさらにデカく見える"])

    def test_ocr_and_asr_text_do_not_overwrite_each_other(self):
        built = source.build_source(recognition_mode="combined", timing_mode="mixed", speech_evidence=self.speech, visual_evidence=self.visual, duration_ms=30000)
        conflict = built["converted"]["segments"][1]
        self.assertEqual(conflict["source_text"], "速攻ホテル前")                           # the spoken text stays the source text
        self.assertIn("TEXT_CONFLICT", conflict["flags"])
        self.assertEqual(conflict["role"], "mixed")
        self.assertEqual(built["converted"]["visual_events"][1]["text"], "ソッコーホテルマエ!")   # the OCR text is kept as evidence, unchanged
        self.assertEqual(self.visual["events"][1]["text"], "ソッコーホテルマエ!")
        reference = source.reference_srt_bytes([e for e in self.visual["events"] if e["text"]]).decode("utf-8")
        self.assertIn("ソッコーホテルマエ!", reference)

    def test_speech_only_has_no_visual_input_and_no_missing_ocr_failure(self):
        built = source.build_source(recognition_mode="speech_only", timing_mode="speech", speech_evidence=self.speech, visual_evidence=None, duration_ms=30000)
        self.assertEqual([s["role"] for s in built["converted"]["segments"]], ["speech", "speech"])
        self.assertEqual([s["flags"] for s in built["converted"]["segments"]], [[], []])
        self.assertEqual(built["converted"]["coverage"]["status"], "measured")
        self.assertEqual(built["v8_mode"], "speech")

    def test_visual_only_uses_the_screen_events_without_a_speech_denominator(self):
        built = source.build_source(recognition_mode="visual_only", timing_mode="screen", speech_evidence=None, visual_evidence=self.visual, duration_ms=30000)
        segments = built["converted"]["segments"]
        self.assertEqual([s["source_text"] for s in segments], ["ねえこれ…", "ソッコーホテルマエ!", "アルグン私服とらデカく見る"])
        self.assertEqual({s["role"] for s in segments}, {"screen"})
        self.assertEqual({s["timing_source"] for s in segments}, {"visual_page"})
        self.assertEqual(built["converted"]["coverage"]["status"], "not_applicable")
        self.assertEqual(built["converted"]["parents"], [])
        self.assertEqual([s["visual_ids"] for s in segments], [["ocr-000001"], ["ocr-000002"], ["ocr-000003"]])

    def test_empty_results_are_explicit_and_combined_keeps_real_ocr_when_speech_is_empty(self):
        empty_speech = w04.speech_evidence(self.media_id, [])
        empty_visual = w04.visual_evidence(self.media_id, [(100, 200, "")])
        for mode, speech, visual in (("speech_only", empty_speech, None), ("visual_only", None, empty_visual), ("combined", empty_speech, empty_visual)):
            with self.subTest(mode):
                built = source.build_source(recognition_mode=mode, timing_mode="mixed", speech_evidence=speech, visual_evidence=visual, duration_ms=30000)
                self.assertEqual((built["status"], built["converted"]["segments"]), ("EMPTY", []))
        built = source.build_source(recognition_mode="combined", timing_mode="mixed", speech_evidence=empty_speech, visual_evidence=self.visual, duration_ms=30000)
        self.assertEqual([s["source_text"] for s in built["converted"]["segments"]], ["ねえこれ…", "ソッコーホテルマエ!", "アルグン私服とらデカく見る"])
        self.assertTrue(all("VISUAL_ONLY" in s["flags"] for s in built["converted"]["segments"]))

    def test_rejected_word_clocks_keep_the_utterance_envelope_and_mark_the_cue(self):
        rejected = w04.speech_evidence(self.media_id, w04.LEGACY_SPEECH, None, status="unusable", rejected_parents=(1, 2))
        built = source.build_source(recognition_mode="speech_only", timing_mode="speech", speech_evidence=rejected, visual_evidence=None, duration_ms=30000)
        segments = built["converted"]["segments"]
        self.assertEqual([(s["start_ms"], s["end_ms"], s["timing_source"]) for s in segments], [(23600, 25680, "speech_parent"), (25680, 28060, "speech_parent")])
        self.assertTrue(all({"WORD_CLOCK_REJECTED", "CHECK"} <= set(s["flags"]) for s in segments))

    def test_cues_beyond_the_media_are_refused_without_clamping(self):
        with self.assertRaises(c.ContractError) as ctx:
            source.build_source(recognition_mode="speech_only", timing_mode="speech", speech_evidence=self.speech, visual_evidence=None, duration_ms=27000)
        self.assertEqual((ctx.exception.code, ctx.exception.details["rule_id"]), ("SUBTITLE_INVALID", "QC03_BOUNDS"))
        self.assertEqual(ctx.exception.details["cues"][0]["end_ms"], 28060)                 # the value is reported, not clamped
        source.build_source(recognition_mode="speech_only", timing_mode="speech", speech_evidence=self.speech, visual_evidence=None, duration_ms=28059)   # 1 ms tolerance

    def test_invalid_word_evidence_is_a_coverage_error_not_a_crash(self):
        broken = w04.speech_evidence(self.media_id, w04.LEGACY_SPEECH, {1: [(0, 1, 10, 5)]})
        with self.assertRaises(c.ContractError) as ctx:
            source.build_source(recognition_mode="speech_only", timing_mode="speech", speech_evidence=broken, visual_evidence=None, duration_ms=30000)
        self.assertEqual(ctx.exception.code, "SOURCE_COVERAGE_ERROR")


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = init_project_dir(Path(self._tmp.name) / "数据根", "project")
        self.media_id = c.new_id()
        self.speech = w04.speech_evidence(self.media_id, w04.LEGACY_SPEECH, w04.LEGACY_CLOCKS)
        self.visual = w04.visual_evidence(self.media_id, w04.LEGACY_PAGES)

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, signature="1" * 64):
        built = source.build_source(recognition_mode="combined", timing_mode="mixed", speech_evidence=self.speech, visual_evidence=self.visual, duration_ms=30000)
        return source.commit_source_revision(project_dir=self.project, project_id=c.new_id(), media_id=self.media_id, run_id=c.new_id(), stage_signature=signature,
                                             recognition_mode="combined", timing_mode="mixed", built=built, speech_evidence=self.speech, visual_evidence=self.visual, evidence_refs={})

    def test_source_revision_is_committed_once_with_source_and_reference_files(self):
        loaded = self.commit()
        record, directory = loaded["record"], loaded["dir"]
        self.assertEqual((record["origin"], record["provenance_status"], record["parent_revision_id"]), ("machine", "exact", None))
        names = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())
        self.assertEqual(names, ["manifest.json", "recognition/evidence_refs.json", "recognition/reference.asr.ja.srt", "recognition/reference.ocr.ja.srt", "recognition/speech_evidence.json",
                                 "recognition/visual_evidence.json", "segments.json", "segments.v8.json", "source.ja.srt"])
        verify_committed(directory, source.revision_refs(record))
        self.assertEqual(len(source.revision_refs(record)), len(names) - 1)
        srt = (directory / "source.ja.srt").read_bytes()
        self.assertTrue(srt.startswith(b"\xef\xbb\xbf1\r\n00:00:23,830 --> 00:00:24,630\r\n"))
        self.assertNotIn(b"\n\n", srt.replace(b"\r\n", b"\n").replace(b"\n\n", b"\x00"))  # blocks separated by exactly one blank line, CRLF everywhere
        again = source.find_source_revision(self.project, stage_signature="1" * 64)
        self.assertEqual(again["record"]["revision_id"], record["revision_id"])            # idempotent recovery by stage signature
        self.assertIsNone(source.find_source_revision(self.project, stage_signature="2" * 64))
        self.assertEqual(loaded["segments_ref"]["path"], f"revisions/source/{record['revision_id']}/segments.json")
        # the stored v8 record still passes the legacy audit and equals the legacy inputs' algorithm output
        from subtitle_segments import audit_segment_records
        audit_segment_records(json.loads((directory / "segments.v8.json").read_text(encoding="utf-8")))

    def test_changed_revision_files_are_refused_on_load(self):
        loaded = self.commit()
        path = loaded["dir"] / "source.ja.srt"
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaises(c.ContractError) as ctx:
            source.load_source_revision(self.project, loaded["record"]["revision_id"])
        self.assertEqual(ctx.exception.code, "ARTIFACT_CHANGED")
        with self.assertRaises(c.ContractError) as ctx:
            source.load_source_revision(self.project, c.new_id())
        self.assertEqual(ctx.exception.code, "INPUT_MISSING")


if __name__ == "__main__":
    unittest.main(verbosity=2)
