"""T01: data contracts — schema versions, unknown fields, NaN, nullable rules, snapshot identity, import isolation."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

from upgrade import contracts as c
from upgrade.config import build_config_snapshot, default_app_config


class ContractValidationTests(unittest.TestCase):
    def test_default_app_config_is_valid_and_roundtrips(self):
        config = default_app_config("X:/data")
        self.assertEqual(c.validate_record("AppConfig", config), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.json"
            c.dump_record("AppConfig", config, path)
            self.assertEqual(c.load_record("AppConfig", path), config)

    def test_unknown_schema_version_is_refused(self):
        config = default_app_config("X:/data")
        config["schema_version"] = 3
        with self.assertRaises(c.ContractError) as ctx:
            c.require_record("AppConfig", config)
        self.assertEqual(ctx.exception.code, "SCHEMA_UNSUPPORTED")
        del config["schema_version"]
        with self.assertRaises(c.ContractError) as ctx:
            c.require_record("AppConfig", config)
        self.assertEqual(ctx.exception.code, "SCHEMA_UNSUPPORTED")

    def test_unknown_fields_only_in_extensions(self):
        config = default_app_config("X:/data")
        config["legacy_thing"] = 1
        problems = c.validate_record("AppConfig", config)
        self.assertTrue(any("unknown field" in p and "extensions" in p for p in problems), problems)
        config.pop("legacy_thing")
        config["extensions"]["legacy_thing"] = 1
        self.assertEqual(c.validate_record("AppConfig", config), [])
        segment = {"cue_id": "c1", "legacy_cue_id": None, "index": 0, "start_ms": 0, "end_ms": 10, "source_text": "a", "role": "speech",
                   "source_ranges": [], "visual_ids": [], "timing_source": "speech", "flags": [], "extra": True}
        problems = c.validate_record("Segment", segment)
        self.assertTrue(any("unknown field" in p and "extensions" not in p for p in problems), problems)

    def test_nan_infinity_and_bom_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"schema_version": 2, "x": NaN}', encoding="utf-8")
            with self.assertRaises(c.ContractError) as ctx:
                c.load_record("AppConfig", path)
            self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
            path.write_bytes(b"\xef\xbb\xbf{}")
            with self.assertRaises(c.ContractError) as ctx:
                c.load_record("AppConfig", path)
            self.assertIn("BOM", ctx.exception.message)
        issue = {"issue_id": c.new_id(), "rule_id": "QC08_SPEED", "severity": "WARNING", "revision_id": c.new_id(), "cue_ids": ["c1"],
                 "start_ms": None, "end_ms": None, "source_refs": [], "observed": float("inf"), "threshold": 15, "message": "m",
                 "recommended_action": "r", "verification": "measured"}
        self.assertTrue(any("NaN/Infinity" in p for p in c.validate_record("Issue", issue)))

    def test_nullable_and_required_rules(self):
        attempt = {"attempt_id": c.new_id(), "status": "RUNNING", "started_at": c.utc_now(), "ended_at": None,
                   "input_signature": "0" * 64, "output_manifest": None, "error": None}
        self.assertEqual(c.validate_record("Attempt", attempt), [])
        attempt["started_at"] = None
        self.assertTrue(any("must not be null" in p for p in c.validate_record("Attempt", attempt)))
        del attempt["error"]
        self.assertTrue(any("required field is missing" in p for p in c.validate_record("Attempt", attempt)))

    def test_job_request_mode_invariants(self):
        config = default_app_config("X:/data")
        snapshot = build_config_snapshot(config, {}, dictionary_revision_id="dict-1", resource_refs=[])
        request = {"schema_version": 2, "request_id": c.new_id(), "project_id": c.new_id(), "created_at": c.utc_now(),
                   "input": {"kind": "local_video", "value": "D:/v.mp4", "source_media_id": None}, "recognition_mode": "combined",
                   "timing_mode": "mixed", "video_stream_index": None, "audio_stream_index": None, "source_revision_id": None,
                   "config_snapshot": snapshot, "resource_refs": [], "dictionary_revision_id": "dict-1", "request_fingerprint": "f" * 64, "extensions": {}}
        self.assertEqual(c.validate_record("JobRequest", request), [])
        request["recognition_mode"] = "supplied_source"
        self.assertTrue(any("requires source_revision_id" in p for p in c.validate_record("JobRequest", request)))
        request["recognition_mode"] = "visual_only"
        self.assertTrue(any("timing_mode=screen" in p for p in c.validate_record("JobRequest", request)))
        request["recognition_mode"] = "combined"
        request["input"] = {"kind": "url", "value": "ftp://x/y", "source_media_id": None}
        self.assertTrue(any("http or https" in p for p in c.validate_record("JobRequest", request)))

    def test_config_snapshot_identity_and_secrets(self):
        config = default_app_config("X:/data")
        snapshot = build_config_snapshot(config, {"translation": {"workers": 1}}, dictionary_revision_id="d", resource_refs=[])
        self.assertEqual(c.validate_record("ConfigSnapshot", snapshot), [])
        tampered = dict(snapshot)
        tampered["engine_settings"] = {"translation": {"workers": 2}}
        self.assertTrue(any("config_sha256 does not match" in p for p in c.validate_record("ConfigSnapshot", tampered)))
        with self.assertRaises(c.ContractError):
            build_config_snapshot(config, {"translation": {"api_key": "x"}}, dictionary_revision_id="d", resource_refs=[])

    def test_snapshot_does_not_follow_later_setting_changes(self):
        config = default_app_config("X:/data")
        engine = {"translation": {"batch_size": 10}}
        snapshot = build_config_snapshot(config, engine, dictionary_revision_id="d", resource_refs=[])
        frozen = c.canonical_json(snapshot)
        config["defaults"]["timing_mode"] = "speech"
        engine["translation"]["batch_size"] = 1
        self.assertEqual(c.canonical_json(snapshot), frozen)
        self.assertEqual(snapshot["app_config"]["defaults"]["timing_mode"], "mixed")
        self.assertEqual(c.validate_record("ConfigSnapshot", snapshot), [])

    def test_safe_relative_paths_and_windows_names(self):
        for bad in ("", "../x", "/abs", "C:/x", "a\\b", "a/../b", "a\0b", "./a"):
            self.assertFalse(c.is_safe_relative_posix(bad), bad)
        for good in ("a", "a/b.json", "revisions/machine/x/sub.srt", "中文/路径 (1) & 'q'.ass"):
            self.assertTrue(c.is_safe_relative_posix(good), good)
        self.assertIsNotNone(c.windows_name_problem("CON"))
        self.assertIsNotNone(c.windows_name_problem("aux.txt"))
        self.assertIsNotNone(c.windows_name_problem("a:b"))
        self.assertIsNotNone(c.windows_name_problem("trailing."))
        self.assertIsNone(c.windows_name_problem("中文 (1) & 'ok'.srt"))

    def test_every_kind_exports_a_schema(self):
        for kind in c.RECORD_KINDS:
            schema = c.to_json_schema(kind)
            self.assertEqual(schema["title"], kind)
            self.assertFalse(schema["additionalProperties"])
            if kind in c.ROOT_KINDS:
                self.assertIn("schema_version", schema["required"])
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, str(root / "scripts/export_schemas.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, "schemas/ is out of date; run scripts/export_schemas.py\n" + completed.stdout)

    def test_error_object_codes_and_exit_codes(self):
        with self.assertRaises(ValueError):
            c.ContractError("NOT_A_CODE", "x")
        self.assertEqual(c.ContractError("EXECUTOR_BUSY", "x").exit_code, 7)
        self.assertEqual(c.ContractError("RESOURCE_MISSING", "x").exit_code, 3)
        self.assertEqual(c.ContractError("SCHEMA_INVALID", "x").exit_code, 5)
        result = {"ok": True, "operation": "x", "ids": {}, "artifact_refs": [], "error": c.ContractError("INPUT_MISSING", "m").to_dict(), "warnings": []}
        self.assertTrue(any("ok must be false" in p for p in c.validate_record("OperationResult", result)))


class ImportIsolationTests(unittest.TestCase):
    def test_v2_modules_do_not_import_heavy_or_gui_packages(self):
        code = ("import sys, json\n"
                "import upgrade.contracts, upgrade.config, upgrade.storage, upgrade.resource_catalog, upgrade.cli\n"
                "import upgrade.media, upgrade.recognition, upgrade.stages, upgrade.stage_registry\n"
                "import upgrade.source, upgrade.dictionary, upgrade.translation, upgrade.subtitles\n"
                "forbidden = ['torch', 'paddle', 'qwen_asr', 'pywinauto', 'tkinter', 'win32gui', 'PIL', 'yaml']\n"
                "print(json.dumps(sorted(m for m in forbidden if m in sys.modules)))\n")
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout.strip()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
