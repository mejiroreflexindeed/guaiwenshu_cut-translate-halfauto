"""T01: resource catalog — explicit bindings, missing/changed identities, re-binding after a move, probes, CLI contract."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from upgrade import contracts as c
from upgrade import resource_catalog as rc
from upgrade.config import default_app_config, resolve_in_data_root, save_app_config
from upgrade.storage import init_data_root


def _config(tmp: str) -> dict:
    root = Path(tmp) / "data"
    init_data_root(root)
    return default_app_config(root)


def _model_dir(base: Path, name: str, content: bytes = b"weights") -> Path:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "model.bin").write_bytes(content)
    (directory / "config.json").write_text('{"k": 1}', encoding="utf-8")
    return directory


class CatalogTests(unittest.TestCase):
    def test_missing_resource_is_a_specific_problem_and_blocks_require(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            declarations = {"whisper-model": rc.declare_resource("model", Path(tmp) / "nowhere" / "ggml-large-v3.bin")}
            report = rc.inspect_resources(config, declarations=declarations)
            self.assertEqual(report["manifests"], {})
            self.assertEqual(report["problems"][0]["code"], "RESOURCE_MISSING")
            self.assertEqual(report["problems"][0]["details"]["resource_id"], "whisper-model")
            with self.assertRaises(c.ContractError) as ctx:
                rc.require_resources(report, ["whisper-model", "undeclared"])
            self.assertEqual(ctx.exception.code, "RESOURCE_MISSING")
            ids = sorted(item["resource_id"] for item in ctx.exception.details["failures"])
            self.assertEqual(ids, ["undeclared", "whisper-model"])

    def test_register_detect_change_and_rebind_after_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            model = _model_dir(Path(tmp) / "tools (old) 中文", "sakura")
            declaration = rc.declare_resource("model", model, files=["model.bin", "config.json"], license_status="recorded")
            saved = rc.register_resource(config, "sakura-model", declaration)
            self.assertRegex(saved["expected_content_sha256"], r"^[0-9a-f]{64}$")
            report = rc.inspect_resources(config)
            self.assertEqual(report["problems"], [])
            manifest = report["manifests"]["sakura-model"]
            self.assertEqual(manifest["extensions"]["identity"], "verified")
            self.assertEqual([f["path"] for f in manifest["files"]], ["config.json", "model.bin"])
            resolved = rc.resolve_resource("sakura-model", report)
            self.assertEqual(c.validate_record("ResourceRef", resolved["resource_ref"]), [])
            # move the directory: same content, new path -> rebind accepted, identity unchanged
            moved = Path(tmp) / "tools (new)" / "sakura"
            moved.parent.mkdir()
            shutil.move(str(model), str(moved))
            report_after_move = rc.inspect_resources(config)
            self.assertEqual(report_after_move["problems"][0]["code"], "RESOURCE_MISSING")
            rebound = rc.rebind_resource(config, "sakura-model", moved)
            self.assertTrue(rebound["rebound"])
            self.assertEqual(rebound["content_sha256"], saved["expected_content_sha256"])
            self.assertEqual(rc.inspect_resources(config)["problems"], [])
            # same name, different content -> RESOURCE_CHANGED, never silently accepted as the old resource
            (moved / "model.bin").write_bytes(b"other weights")
            report_changed = rc.inspect_resources(config)
            self.assertEqual(report_changed["problems"][0]["code"], "RESOURCE_CHANGED")
            with self.assertRaises(c.ContractError) as ctx:
                rc.resolve_resource("sakura-model", report_changed)
            self.assertEqual(ctx.exception.code, "RESOURCE_CHANGED")
            with self.assertRaises(c.ContractError) as ctx:
                rc.rebind_resource(config, "sakura-model", moved)
            self.assertEqual(ctx.exception.code, "RESOURCE_CHANGED")

    def test_probe_uses_only_version_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            declarations = {"py-as-engine": rc.declare_resource("translation_engine", sys.executable, version_args=["-V"])}
            report = rc.inspect_resources(config, declarations=declarations)
            self.assertEqual(report["problems"], [])
            manifest = report["manifests"]["py-as-engine"]
            self.assertTrue(manifest["version_text"].startswith("Python "), manifest["version_text"])
            self.assertIs(manifest["capabilities"]["cpu"], True)
            self.assertEqual(manifest["capabilities"]["gpu_cuda"], "unknown")
            self.assertEqual(manifest["capabilities"]["libass"], "not_applicable")
            no_probe = rc.inspect_resources(config, declarations=declarations, probe=False)
            self.assertIsNone(no_probe["manifests"]["py-as-engine"]["version_text"])
            self.assertEqual(no_probe["manifests"]["py-as-engine"]["capabilities"]["cpu"], "unknown")

    def test_large_file_hash_cache_is_metadata_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            original = rc.LARGE_FILE_BYTES
            rc.LARGE_FILE_BYTES = 8
            try:
                model = _model_dir(Path(tmp) / "m", "big", content=b"0123456789abcdef")
                declarations = {"big": rc.declare_resource("model", model, files=["model.bin"])}
                first = rc.inspect_resources(config, declarations=declarations, probe=False)
                second = rc.inspect_resources(config, declarations=declarations, probe=False)
                self.assertIn("hits=1", second["hash_cache_note"])
                self.assertEqual(first["manifests"]["big"]["content_sha256"], second["manifests"]["big"]["content_sha256"])
                cache = json.loads((Path(config["data_root"]) / rc.HASH_CACHE_NAME).read_text(encoding="utf-8"))
                self.assertEqual(len(cache["entries"]), 1)
            finally:
                rc.LARGE_FILE_BYTES = original

    def test_declarations_file_rejects_bad_kind_and_unknown_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            path = resolve_in_data_root(config, config["resources_file"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema_version": 1, "resources": {}}), encoding="utf-8")
            with self.assertRaises(c.ContractError) as ctx:
                rc.load_declarations(path)
            self.assertEqual(ctx.exception.code, "SCHEMA_UNSUPPORTED")
            path.write_text(json.dumps({"schema_version": 2, "resources": {"x": {"kind": "gpu", "absolute_path": "C:/x"}}}), encoding="utf-8")
            with self.assertRaises(c.ContractError) as ctx:
                rc.load_declarations(path)
            self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class CliTests(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, dict, str]:
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True,
                                   encoding="utf-8", env=os.environ.copy())
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        return completed.returncode, payload, completed.stderr

    def test_help_runs(self):
        completed = subprocess.run([sys.executable, "-m", "upgrade.cli", "--help"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("resources", completed.stdout)

    def test_init_register_inspect_and_missing_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "数据 (1)"
            config_path = Path(tmp) / "app.json"
            code, result, err = self._run("config", "init", "--data-root", str(data_root), "--config", str(config_path))
            self.assertEqual(code, 0, err)
            self.assertTrue(result["ok"])
            self.assertEqual(c.validate_record("OperationResult", result), [])
            code, result, _ = self._run("config", "init", "--data-root", str(data_root), "--config", str(config_path))
            self.assertEqual(code, 5)
            self.assertEqual(result["error"]["code"], "OUTPUT_EXISTS")
            model = _model_dir(Path(tmp) / "tools", "whisper")
            code, result, err = self._run("resources", "register", "--config", str(config_path), "--id", "whisper-model", "--kind", "model",
                                          "--path", str(model), "--file", "model.bin", "--license-status", "recorded")
            self.assertEqual(code, 0, err)
            report_path = Path(tmp) / "report.json"
            code, result, _ = self._run("resources", "inspect", "--config", str(config_path), "--report", str(report_path))
            self.assertEqual(code, 0)
            report = c.load_record("CatalogReport", report_path)
            self.assertIn("whisper-model", report["manifests"])
            shutil.rmtree(model)
            code, result, _ = self._run("resources", "inspect", "--config", str(config_path), "--report", str(report_path))
            self.assertEqual(code, 3)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "RESOURCE_MISSING")
            self.assertEqual(result["error"]["details"]["problems"][0]["details"]["resource_id"], "whisper-model")
            code, result, _ = self._run("resources", "inspect", "--config", str(Path(tmp) / "missing.json"), "--report", str(report_path))
            self.assertEqual(code, 3)
            self.assertEqual(result["error"]["code"], "INPUT_MISSING")

    def test_migrate_reports_unsupported_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(__file__).resolve().parents[1]
            legacy = json.loads((root / "app/config/roast_pipeline.example.json").read_text(encoding="utf-8"))
            legacy["commands"]["translate_zh"] = "echo hi"
            legacy_path = Path(tmp) / "roast_pipeline.local.json"
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            config_path = Path(tmp) / "settings" / "app.json"
            code, result, err = self._run("config", "migrate", "--legacy", str(legacy_path), "--data-root", str(Path(tmp) / "data"), "--config", str(config_path))
            self.assertEqual(code, 0, err)
            self.assertEqual(result["warnings"], [{"code": "CONFIG_UNSUPPORTED_COMMAND", "key": "commands.translate_zh"}])
            self.assertTrue(Path(result["ids"]["engine_settings"]).is_file())
            self.assertTrue(Path(result["ids"]["report"]).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
