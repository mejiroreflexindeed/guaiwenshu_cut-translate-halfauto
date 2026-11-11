"""T01: DATA_ROOT checks, path safety, immutable commit primitive, argv round-trip, legacy config migration."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from upgrade import contracts as c
from upgrade import storage
from upgrade.config import (
    default_app_config, load_app_config, load_engine_settings, migrate_legacy_config, save_app_config, save_engine_settings,
)

ROOT = Path(__file__).resolve().parents[1]
WEIRD_NAMES = ["中文 目录", "with space", "it's", "amp&ersand", "paren(1)", "日本語 (2) & 'x'"]


class DataRootTests(unittest.TestCase):
    def test_init_creates_layout_after_write_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "中文 数据根 (1)"
            report = storage.init_data_root(root)
            for relative in storage.DATA_ROOT_DIRS:
                self.assertTrue((root / relative).is_dir(), relative)
            self.assertFalse(list(root.glob("settings/.write-probe-*")))
            self.assertEqual(Path(report["data_root"]), root.resolve())

    def test_invalid_names_are_rejected_with_specific_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("CON", "nul.txt", "ends.", "bad|name"):
                with self.assertRaises(c.ContractError) as ctx:
                    storage.init_data_root(Path(tmp) / bad)
                self.assertEqual(ctx.exception.code, "PATH_INVALID", bad)
            with self.assertRaises(c.ContractError) as ctx:
                storage.check_path_components(str(Path(tmp)) + "\0x")
            self.assertEqual(ctx.exception.code, "PATH_INVALID")
        with self.assertRaises(c.ContractError) as ctx:
            storage.init_data_root(r"\\server\share\data")
        self.assertEqual(ctx.exception.code, "DATA_ROOT_UNWRITABLE")

    def test_safe_join_rejects_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(storage.safe_join(base, "a/b.json"), Path(tmp) / "a" / "b.json")
            for bad in ("../x", "/abs", "C:/x", "a\\b", ""):
                with self.assertRaises(c.ContractError) as ctx:
                    storage.safe_join(base, bad)
                self.assertEqual(ctx.exception.code, "PATH_INVALID", bad)

    def test_ascii_temp_root_check(self):
        storage.ensure_ascii_root("C:/AutoRoastTemp")
        with self.assertRaises(c.ContractError):
            storage.ensure_ascii_root("C:/临时")


class CommitPrimitiveTests(unittest.TestCase):
    def _manifest(self, refs):
        return {"schema_version": 2, "run_id": c.new_id(), "request_id": c.new_id(), "stage": "SOURCE", "attempt_id": c.new_id(),
                "input_signature": "a" * 64, "outputs": refs, "validation_ref": refs[0], "committed_at": c.utc_now()}

    def test_commit_once_verify_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = storage.init_project_dir(tmp, c.new_id())
            pending = storage.begin_pending(project)
            (pending / "validation.json").write_text('{"ok": true}', encoding="utf-8")
            (pending / "sub").mkdir()
            (pending / "sub" / "source.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nテスト\n", encoding="utf-8")
            final = project / "revisions" / "source" / "rev-1"
            result = storage.commit_pending(pending, final, manifest_kind="StageManifest", build_manifest=self._manifest)
            self.assertTrue(final.is_dir() and (final / storage.MANIFEST_NAME).is_file())
            self.assertFalse(pending.exists())
            paths = sorted(ref["path"] for ref in result["artifact_refs"])
            self.assertEqual(paths, ["sub/source.srt", "validation.json"])
            manifest = storage.read_manifest(final, "StageManifest")
            storage.verify_committed(final, manifest["outputs"])
            # second commit to the same target is refused and the new pending candidate is kept
            pending2 = storage.begin_pending(project)
            (pending2 / "validation.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(c.ContractError) as ctx:
                storage.commit_pending(pending2, final, manifest_kind="StageManifest", build_manifest=self._manifest)
            self.assertEqual(ctx.exception.code, "OUTPUT_EXISTS")
            self.assertTrue(pending2.is_dir())
            # modifying a committed artifact is damage, never silently re-accepted
            (final / "sub" / "source.srt").write_text("changed", encoding="utf-8")
            with self.assertRaises(c.ContractError) as ctx:
                storage.verify_committed(final, manifest["outputs"])
            self.assertEqual(ctx.exception.code, "ARTIFACT_CHANGED")

    def test_invalid_manifest_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = storage.init_project_dir(tmp, c.new_id())
            pending = storage.begin_pending(project)
            (pending / "a.json").write_text("{}", encoding="utf-8")
            final = project / "revisions" / "source" / "rev-x"
            with self.assertRaises(c.ContractError) as ctx:
                storage.commit_pending(pending, final, manifest_kind="StageManifest", build_manifest=lambda refs: {"schema_version": 2})
            self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
            self.assertFalse(final.exists())
            self.assertTrue(pending.is_dir())
            self.assertEqual(storage.list_pending(project), [str(pending)])


class ArgvRoundTripTests(unittest.TestCase):
    def test_special_paths_stay_single_argv_elements(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in WEIRD_NAMES:
                target = Path(tmp) / name / "file.mp4"
                target.parent.mkdir(parents=True)
                target.write_bytes(b"x")
                paths.append(str(target))
            code = "import sys, json; print(json.dumps(sys.argv[1:], ensure_ascii=False))"
            completed = subprocess.run([sys.executable, "-X", "utf8", "-c", code, *paths], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), paths)
            spec = {"executable": sys.executable, "argv": paths, "cwd": tmp, "env_overrides": {}, "stdin_mode": "none",
                    "log_dir": tmp, "owner_token": "t", "capability_id": "cpu"}
            self.assertEqual(c.validate_record("ProcessSpec", spec), [])


class ConfigTests(unittest.TestCase):
    def test_save_load_and_unknown_root_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.json"
            config = default_app_config(tmp)
            save_app_config(path, config)
            self.assertEqual(load_app_config(path), config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["mystery"] = 1
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(c.ContractError) as ctx:
                load_app_config(path)
            self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
            engine_path = Path(tmp) / "engine.json"
            with self.assertRaises(c.ContractError):
                save_engine_settings(engine_path, {"translation": {"api_token": "x"}})
            save_engine_settings(engine_path, {"translation": {"workers": 1}})
            self.assertEqual(load_engine_settings(engine_path), {"translation": {"workers": 1}})

    def test_legacy_migration_maps_known_values_and_records_the_rest(self):
        legacy = json.loads((ROOT / "app/config/roast_pipeline.example.json").read_text(encoding="utf-8"))
        legacy["alignment"]["timing_mode"] = "speech"
        legacy["axis_settings"]["dialog_rect"] = [0.1, 0.9, 0.6, 1.0]
        legacy["commands"]["axis"] = 'custom.exe "{video}" | tee log'
        legacy["weird_old_key"] = {"a": 1}
        legacy["tools"]["ffmpeg"] = "D:/tools/ffmpeg.exe"
        legacy["secret_token"] = "abc"
        legacy["proxy"] = "http://127.0.0.1:7890"
        legacy["download_settings"] = {"quality": "最高 1080p", "cookies_from_browser": "Edge", "js_runtime": "", "youtube_client": "TV"}
        migrated = migrate_legacy_config(legacy, data_root="X:/data")
        # download choices are kept as explicit settings; the engine settings never hold a cookie-named key or cookie content
        self.assertEqual(migrated["engine_settings"]["download"], {"proxy": "http://127.0.0.1:7890", "quality": "最高 1080p",
                                                                   "browser_login_source": "edge", "youtube_client": "tv"})
        self.assertEqual(c.find_sensitive_keys(migrated["engine_settings"]), [])
        self.assertNotIn("download_settings", migrated["app_config"]["extensions"]["legacy_extra"])
        config = migrated["app_config"]
        self.assertEqual(c.validate_record("AppConfig", config), [])
        self.assertEqual(config["defaults"]["timing_mode"], "speech")
        self.assertEqual(migrated["engine_settings"]["ocr"]["roi"], {"left": 0.1, "top": 0.6, "right": 0.9, "bottom": 1.0})
        unsupported = migrated["unsupported_commands"]
        self.assertEqual([item["key"] for item in unsupported], ["commands.axis"])
        self.assertEqual(unsupported[0]["code"], "CONFIG_UNSUPPORTED_COMMAND")
        self.assertTrue(any(item.startswith("commands.download -> builtin:") for item in migrated["mapped"]))
        self.assertIn("weird_old_key", config["extensions"]["legacy_extra"])
        self.assertNotIn("secret_token", config["extensions"]["legacy_extra"])
        self.assertEqual(migrated["suggested_resources"]["tools.ffmpeg"], "D:/tools/ffmpeg.exe")
        self.assertEqual(migrated["engine_settings"]["translation"]["batch_size"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
