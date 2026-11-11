"""T04 (W04, dictionary part): byte-exact dictionary revisions, line-level diagnostics that agree with the legacy parser,
pre/gpt/post/once/prefix/unsupported entries, conflicts as warnings, and the acceptance flow for unsupported entries."""
from pathlib import Path
import json
import tempfile
import unittest

from support import w04
from upgrade import contracts as c
from upgrade import dictionary
from upgrade.storage import init_data_root, verify_committed


class DictionaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "数据根 (dictionary)"
        init_data_root(self.root)
        self.paths = w04.write_dictionary_files(self.tmp / "词典 & prompts")
        self.settings = w04.dictionary_settings(self.paths)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bundled_defaults_are_used_when_nothing_is_configured(self):
        inputs = dictionary.resolve_inputs({})
        self.assertEqual({role: item["origin"] for role, item in inputs.items() if item}, {role: "bundled_default" for role in ("pre", "gpt", "post", "glossary", "style_prompt")})
        self.assertEqual(inputs["pre"]["path"], w04.BUNDLED_DEFAULTS / "dict_pre.txt")
        self.assertIsNone(inputs["guideline"])
        loaded = dictionary.create_revision(self.root, inputs)
        self.assertEqual(loaded["diagnostics"]["unsupported"], [], "the bundled dictionaries contain no unsupported entries")
        self.assertEqual(loaded["diagnostics"]["entry_counts"]["gpt"], 3)
        # the legacy migrated keys and explicit dictionary.* paths both count; use_native_dictionaries=false drops the native files
        inputs = dictionary.resolve_inputs({"translation": {"use_native_dictionaries": False, "native_files": {"native_pre_dictionary": str(self.paths["pre"])}}})
        self.assertIsNone(inputs["pre"])
        inputs = dictionary.resolve_inputs({"translation": {"native_files": {"native_pre_dictionary": str(self.paths["pre"])}}})
        self.assertEqual((inputs["pre"]["path"], inputs["pre"]["origin"]), (self.paths["pre"], "user_file"))
        with self.assertRaises(c.ContractError) as ctx:
            dictionary.resolve_inputs({"dictionary": {"gpt": "relative/dict_gpt.txt"}})
        self.assertEqual(ctx.exception.code, "CONFIG_UNSUPPORTED_COMMAND")
        with self.assertRaises(c.ContractError) as ctx:
            dictionary.resolve_inputs({"translation": {"native_prompt_mode": "追加"}})
        self.assertEqual(ctx.exception.code, "INPUT_MISSING")                                 # append mode needs the guideline file

    def test_line_diagnostics_agree_with_the_legacy_parser(self):
        from voice_dictionary import load_native_dictionary
        inputs = dictionary.resolve_inputs(self.settings)
        loaded = dictionary.create_revision(self.root, inputs)
        diagnostics = loaded["diagnostics"]
        by_kind = {item["kind"]: item for item in diagnostics["unsupported"]}
        self.assertEqual(sorted(by_kind), ["conditional_rule", "empty_source", "malformed"])
        self.assertEqual((by_kind["conditional_rule"]["role"], by_kind["conditional_rule"]["line"], by_kind["conditional_rule"]["text"]), ("gpt", 6, "pre_jp\t条件\tソース\t目标"))
        self.assertEqual((by_kind["malformed"]["line"], by_kind["malformed"]["text"]), (7, "只有一列"))
        self.assertEqual(by_kind["empty_source"]["line"], 8)
        entries, unsupported = load_native_dictionary(self.paths["gpt"], gpt=True)
        agreement = diagnostics["parser_agreement"]["gpt"]
        self.assertEqual((agreement["parser_entries"], agreement["parser_unsupported"], agreement["silently_dropped"]), (len(entries), unsupported, 1))
        self.assertEqual(len(entries), 4)
        self.assertEqual({(e.source, e.target, e.replace_mode) for e in entries}, {("ラモーヌ", "ラモーヌ [待确认]", "all"), ("アルダン", "阿尔丹", "all"), ("トレ", "训练", "prefix"), ("さん", "先生", "once")})
        self.assertEqual(diagnostics["entry_counts"], {"pre": 1, "gpt": 4, "post": 1, "glossary": 3})
        self.assertEqual(diagnostics["warnings"], [])

    def test_conflicts_between_files_are_warnings_and_nothing_is_merged(self):
        self.paths["gpt"].write_text("トレーナー\t教练\nトレーナー\t训练师\n", encoding="utf-8")
        loaded = dictionary.create_revision(self.root, dictionary.resolve_inputs(self.settings))
        kinds = sorted(item["kind"] for item in loaded["diagnostics"]["warnings"])
        self.assertEqual(kinds, ["conflict", "duplicate_source"])
        conflict = next(item for item in loaded["diagnostics"]["warnings"] if item["kind"] == "conflict")
        self.assertEqual(conflict["source"], "トレーナー")
        self.assertEqual({(e["role"], e["target"]) for e in conflict["entries"]}, {("glossary", "训练员"), ("gpt", "教练"), ("gpt", "训练师")})
        self.assertEqual(loaded["diagnostics"]["unsupported"], [])                             # warnings need no acceptance
        self.assertEqual((loaded["paths"]["gpt"]).read_bytes(), self.paths["gpt"].read_bytes())  # bytes copied, not re-serialized

    def test_revisions_are_content_addressed_and_immutable(self):
        first = dictionary.create_revision(self.root, dictionary.resolve_inputs(self.settings))
        second = dictionary.create_revision(self.root, dictionary.resolve_inputs(self.settings))
        self.assertEqual(first["record"]["revision_id"], second["record"]["revision_id"])
        for role, path in self.paths.items():
            self.assertEqual(first["paths"][role].read_bytes(), path.read_bytes())
        verify_committed(first["dir"], [item["artifact"] for item in first["record"]["files"]] + [first["record"]["diagnostics_ref"]])
        self.assertEqual([item["original_name"] for item in first["record"]["files"]], ["dict_pre.txt", "dict_gpt.txt", "dict_after.txt", "translation_glossary.json", "style.txt"])
        self.paths["post"].write_text("// 改过的后置词典\n马娘\t赛马娘\n", encoding="utf-8")
        third = dictionary.create_revision(self.root, dictionary.resolve_inputs(self.settings))
        self.assertNotEqual(third["record"]["revision_id"], first["record"]["revision_id"])
        self.assertEqual(first["paths"]["post"].read_text(encoding="utf-8"), w04.DICT_POST)   # the old revision is untouched
        (first["dir"] / "files" / "gpt.txt").write_bytes(b"tampered")
        with self.assertRaises(c.ContractError) as ctx:
            dictionary.load_revision(self.root, first["record"]["revision_id"])
        self.assertEqual(ctx.exception.code, "ARTIFACT_CHANGED")

    def test_unsupported_entries_need_one_explicit_acceptance_per_content(self):
        with self.assertRaises(c.ContractError) as ctx:
            dictionary.prepare_for_request(self.root, self.settings, accept_warnings=False)
        self.assertEqual(ctx.exception.code, "DICTIONARY_UNSUPPORTED")
        shown = ctx.exception.details["unsupported"]
        self.assertEqual([(item["role"], item["line"]) for item in shown], [("gpt", 6), ("gpt", 7), ("gpt", 8)])
        self.assertTrue(all(item["text"] and item["reason"] for item in shown))               # file, line, original entry and reason are all shown
        prepared = dictionary.prepare_for_request(self.root, self.settings, accept_warnings=True)
        self.assertEqual(len(prepared["accepted_ids"]), 3)
        again = dictionary.prepare_for_request(self.root, self.settings, accept_warnings=False)   # the same content asks no second time
        self.assertEqual(again["accepted_ids"], prepared["accepted_ids"])
        self.paths["gpt"].write_text(w04.DICT_GPT + "post_zh\t条件\t甲\t乙\n", encoding="utf-8")
        with self.assertRaises(c.ContractError):
            dictionary.prepare_for_request(self.root, self.settings, accept_warnings=False)     # new content: a new decision
        loaded = dictionary.load_for_translation(self.root, prepared["revision_id"], accepted_ids=prepared["accepted_ids"], settings=self.settings["translation"])
        self.assertEqual(loaded["prompt"], w04.STYLE_PROMPT.strip())
        self.assertEqual({e.source for e in loaded["gpt"]}, {"ラモーヌ", "アルダン", "トレ", "さん"})
        self.assertEqual(loaded["glossary"]["メジロアルダン"], {"default": "目白阿尔丹", "possessive": "true"})
        with self.assertRaises(c.ContractError) as ctx:
            dictionary.load_for_translation(self.root, prepared["revision_id"], accepted_ids=[], settings=self.settings["translation"])
        self.assertEqual(ctx.exception.code, "DICTIONARY_UNSUPPORTED")

    def test_broken_glossary_json_is_listed_instead_of_silently_becoming_empty(self):
        self.paths["glossary"].write_text("{not json", encoding="utf-8")
        loaded = dictionary.create_revision(self.root, dictionary.resolve_inputs(self.settings))
        item = next(i for i in loaded["diagnostics"]["unsupported"] if i["role"] == "glossary")
        self.assertIn("not valid JSON", item["reason"])
        self.paths["glossary"].write_text(json.dumps({"ok": "好", "bad": [1, 2]}), encoding="utf-8")
        loaded = dictionary.create_revision(self.root, dictionary.resolve_inputs(self.settings))
        self.assertEqual(loaded["diagnostics"]["entry_counts"]["glossary"], 1)
        self.assertTrue(any(i["role"] == "glossary" and "bad" in i["text"] for i in loaded["diagnostics"]["unsupported"]))

    def test_native_prompt_modes_follow_the_legacy_guideline_handling(self):
        guideline = self.tmp / "guideline.txt"
        guideline.write_text("原生指引\n", encoding="utf-8")
        settings = {"translation": {"native_prompt_mode": "追加"}, "dictionary": {**self.settings["dictionary"], "guideline": str(guideline)}}
        prepared = dictionary.prepare_for_request(self.root, settings, accept_warnings=True)
        loaded = dictionary.load_for_translation(self.root, prepared["revision_id"], accepted_ids=prepared["accepted_ids"], settings=settings["translation"])
        self.assertEqual(loaded["prompt"], w04.STYLE_PROMPT.strip() + "\n\nVoiceTransl translation guideline:\n原生指引")
        settings["translation"]["native_prompt_mode"] = "覆盖"
        loaded = dictionary.load_for_translation(self.root, prepared["revision_id"], accepted_ids=prepared["accepted_ids"], settings=settings["translation"])
        self.assertEqual(loaded["prompt"], "原生指引")
        self.assertEqual(sorted(prepared["files"]), ["glossary", "gpt", "guideline", "post", "pre", "style_prompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
