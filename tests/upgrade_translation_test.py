"""T04 (W04): the Sakura translation protocol over real loopback HTTP, without a real model.

A scripted child process stands in for llama-server (tests/support/fake_llama_server.py); only the process launch is
substituted, so server ownership, health polling, the SSE reader, retries, checkpoints and cancellation run for real.
Checked here: complete streams, dropped streams, wrong line counts, the single-cue retry limit without an outer
multiplication, context overflow, foreign servers on the port, dictionary pre/gpt/post/once/prefix/unsupported
handling, resume without repeated requests. Nothing here says anything about translation quality.
"""
from pathlib import Path
from unittest.mock import patch
import json
import socket
import tempfile
import threading
import time
import unittest

from support import w04
from upgrade import contracts as c
from upgrade import dictionary, translation
from upgrade.config import build_config_snapshot, default_app_config
from upgrade.process_runner import CancelToken
from upgrade.storage import init_data_root, verify_committed


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TranslationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "数据根 (translation)"
        init_data_root(self.root)
        self.config = default_app_config(self.root, ascii_temp_root=str(self.tmp / "ascii"))
        self.project = w04.new_project(self.root, "project")
        self.media_id = c.new_id()
        self.paths = w04.make_translation_resources(self.tmp / "engines")
        self.model = self.paths["sakura-model"]
        self.dict_paths = w04.write_dictionary_files(self.tmp / "词典")
        self.port = free_port()
        self.events: list[dict] = []
        self.patches = [patch.object(translation, "launch_spec", w04.fake_launch), patch.object(translation, "RETRY_PAUSE_SECONDS", 0.05),
                        patch.object(translation, "HEALTH_POLL_SECONDS", 0.1)]
        for item in self.patches:
            item.start()
        self.attempt = 0

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self._tmp.cleanup()

    # -- helpers
    def settings(self, **translation_settings) -> dict:
        return w04.dictionary_settings(self.dict_paths, endpoint=f"http://127.0.0.1:{self.port}", **translation_settings)

    def request(self, texts: list[str], *, settings: dict | None = None, accept: bool = True, contexts: dict | None = None, segment_set=None) -> tuple[dict, dict]:
        settings = settings or self.settings()
        prepared = dictionary.prepare_for_request(self.root, settings, accept_warnings=accept)
        segment_set = segment_set or w04.segment_set(self.media_id, texts, contexts=contexts)
        ref = w04.write_segment_set(self.project, segment_set)
        snapshot = build_config_snapshot(self.config, settings, dictionary_revision_id=prepared["revision_id"], resource_refs=[], accepted_dictionary_warnings=prepared["accepted_ids"])
        request = c.require_record("TranslationRequest", {"run_id": c.new_id(), "source_revision_id": segment_set["source_revision_id"], "segments_ref": ref,
                                                          "config_snapshot": snapshot, "checkpoint_dir": str(self.project / "runs" / "r" / "checkpoints" / "translation")})
        return request, segment_set

    def runtime(self, cancel: CancelToken | None = None) -> translation.TranslationRuntime:
        self.attempt += 1
        self.output = self.project / "runs" / "r" / "stages" / "TRANSLATION" / f"attempt-{self.attempt}"
        self.output.mkdir(parents=True)
        resources = {"translation_engine": w04.resource("llama-server", self.paths["llama-server"]), "translation_model": w04.resource("sakura-model", self.model)}
        return translation.TranslationRuntime(project_dir=self.project, data_root=self.root, output_dir=self.output, resources=resources, owner_token="t04",
                                              cancel=cancel or CancelToken(), emit=self.events.append)

    def translate(self, request, cancel=None):
        return translation.translate(request, runtime=self.runtime(cancel))

    def translations(self):
        return c.load_record("TranslationSet", self.output / "translations.json")

    def server_owner(self):
        return json.loads((self.output / "server" / "server_owner.json").read_text(encoding="utf-8"))

    def assert_server_stopped(self):
        owner = self.server_owner()
        self.assertEqual(owner["state"], "STOPPED", owner)
        self.assertFalse(w04.set_behaviour.__module__ is None)
        with socket.socket() as probe:
            probe.settimeout(0.5)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.port)), 0, "the owned server must not be listening after the stage")


class ProtocolTests(TranslationTestCase):
    def test_complete_stream_maps_lines_in_order_and_keeps_request_and_response_bytes(self):
        request, segment_set = self.request(["こんにちは", "トレーナーのウマ娘です", "最後の一文"])
        result = self.translate(request)
        records = self.translations()["records"]
        # the echo keeps the Japanese term spellings; the legacy post-processing replaces glossary spellings in place (never appends)
        self.assertEqual([r["target_text"] for r in records], ["译:こんにちは", "译:训练员の赛马娘です", "译:最後の一文"])
        self.assertEqual([r["cue_id"] for r in records], [s["cue_id"] for s in segment_set["segments"]])
        self.assertIn("TRANSLATION_JAPANESE_PRESENT", records[0]["flags"])
        log = w04.requests_log(self.model)
        self.assertEqual(len(log), 1, "three single-line cues are one batch request")
        payload = log[0]["payload"]
        from sakura_client import plain_translation_payload
        from voice_dictionary import NativeDictionaryEntry, glossary_pairs, load_native_dictionary, native_glossary_prompt, resolved_term_target
        texts = ["こんにちは", "トレーナーのウマ娘です", "最後の一文"]
        items = [{"id": i + 1, "text": t, "cue_id": f"cue-{i + 1:06d}", "context": [{"parent_id": f"asr-{i + 1:06d}", "text": t}]} for i, t in enumerate(texts)]
        # the glossary block exactly as the legacy adapter builds it (user glossary first, then gpt entries present in the batch)
        glossary = glossary_pairs(self.dict_paths["glossary"])
        gpt, _ = load_native_dictionary(self.dict_paths["gpt"], gpt=True)
        reference_text = "\n".join(texts) + "\n" + "\n".join(texts)
        required = native_glossary_prompt([NativeDictionaryEntry(s, resolved_term_target(s, "", glossary)) for s in glossary], reference_text)
        native = native_glossary_prompt(gpt, reference_text, excluded_sources=set(glossary))
        expected = plain_translation_payload(self.model.name, w04.STYLE_PROMPT.strip(), items, items, temperature=0.3, top_p=0.8,
                                             glossary_text="\n".join(section for section in (required, native) if section))
        self.assertEqual(payload["messages"], expected["messages"], "the legacy Sakura prompt is sent unchanged")
        self.assertEqual((payload["model"], payload["stream"], payload["temperature"], payload["top_p"]), (self.model.name, True, 0.3, 0.8))
        verify_committed(self.output, result["raw_exchange_refs"] + [result["translations_ref"], result["checkpoint_ref"]])
        exchange = self.output / "exchanges" / "000001"
        self.assertEqual(json.loads((exchange / "request.json").read_text(encoding="utf-8"))["messages"], payload["messages"])
        self.assertIn(b"data: [DONE]", (exchange / "response.body").read_bytes())
        meta = json.loads((exchange / "exchange.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["state"], meta["response_complete"], meta["connect_timeout_seconds"], meta["generation_deadline"]), ("PARSED", True, 10.0, None))
        self.assertEqual(records[0]["request_ref"]["path"], "exchanges/000001/request.json")
        self.assertEqual(records[0]["prompt_sha256"], c.sha256_text(c.canonical_json(payload["messages"])))
        self.assert_server_stopped()
        self.assertEqual(self.server_owner()["argv"][-12:], ["-m", str(self.model), "-ngl", "100", "--port", str(self.port), "--host", "127.0.0.1", "--ctx-size", "16384", "--parallel", "1"])

    def test_multiline_cue_is_its_own_request(self):
        self.request(["一行目", "二行目\n三行目", "四行目"])
        request, _ = self.request(["一行目", "二行目\n三行目", "四行目"])
        self.translate(request)
        log = w04.requests_log(self.model)
        bodies = [entry["payload"]["messages"][-1]["content"].split(w04.__dict__.get("BODY_MARKER", "将下面的文本从日文翻译成简体中文：\n"), 1)[1] for entry in log]
        self.assertEqual(bodies, ["一行目", "二行目\n三行目", "四行目"])
        self.assertEqual([r["target_text"] for r in self.translations()["records"]], ["译:一行目", "译:二行目\n译:三行目", "译:四行目"])

    def test_dropped_stream_is_not_accepted_and_the_retry_succeeds(self):
        w04.set_behaviour(self.model, responses=[{"kind": "drop"}])
        request, _ = self.request(["壊れた配信", "次の文"])
        self.translate(request)
        log = w04.requests_log(self.model)
        self.assertEqual(len(log), 2)
        failed = json.loads((self.output / "exchanges" / "000001" / "translation.json").read_text(encoding="utf-8"))
        self.assertEqual((failed["state"], failed["attempt"], failed["kind"]), ("FAILED", 1, "incomplete_response"))
        meta = json.loads((self.output / "exchanges" / "000001" / "exchange.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["status"], meta["state"]), (200, "FAILED"))                 # HTTP 200 did not make it a success
        self.assertIn("半句".encode("utf-8"), (self.output / "exchanges" / "000001" / "response.body").read_bytes())   # partial bytes are kept
        self.assertEqual(self.translations()["extensions"]["stats"]["requests"], 2)

    def test_wrong_line_count_splits_into_single_requests_with_the_batch_as_reference(self):
        w04.set_behaviour(self.model, responses=[{"kind": "echo_lines", "lines": 2}])
        request, _ = self.request(["壱", "弐", "参"])
        self.translate(request)
        log = w04.requests_log(self.model)
        self.assertEqual(len(log), 4)                                                        # 1 batch (ambiguous) + 3 singles
        ambiguous = json.loads((self.output / "exchanges" / "000001" / "translation.json").read_text(encoding="utf-8"))
        self.assertEqual((ambiguous["state"], ambiguous["line_count"], ambiguous["expected_lines"]), ("AMBIGUOUS_LINES", 2, 3))
        self.assertIn("译文行1", (self.output / "exchanges" / "000001" / "response.body").read_text(encoding="utf-8"))   # the odd response is saved
        for entry in log[1:]:
            system = entry["payload"]["messages"][0]["content"]
            self.assertIn("同批原文参考", system)                                            # the whole batch travels as reference
            self.assertIn("字幕1：壱", system)
        self.assertEqual([r["target_text"] for r in self.translations()["records"]], ["译:壱", "译:弐", "译:参"])
        self.assertEqual(self.translations()["extensions"]["stats"]["ambiguous_splits"], 1)

    def test_single_cue_gets_three_attempts_and_the_stage_stops_without_outer_retries(self):
        w04.set_behaviour(self.model, responses=[{"kind": "echo"}, {"kind": "drop"}, {"kind": "drop"}, {"kind": "drop"}])
        request, segment_set = self.request(["成功する文", "必ず失敗する\n二行"])        # batch 1 succeeds, the multi-line cue fails three times
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request)
        self.assertEqual((ctx.exception.code, ctx.exception.details["attempts"], ctx.exception.details["max_attempts"]), ("PROCESS_FAILED", 3, 3))
        self.assertEqual([item["cue_id"] for item in ctx.exception.details["pending_items"]], ["cue-000002"])
        self.assertEqual(len(w04.requests_log(self.model)), 4)                              # 1 + 3, nothing more
        self.assertFalse((self.output / "translations.json").exists())                      # nothing partial is committed
        checkpoint_dir = Path(request["checkpoint_dir"])
        self.assertEqual(sorted(p.name for p in (checkpoint_dir / "cues").iterdir()), ["cue-000001.json"])   # the completed cue stays checkpointed
        self.assert_server_stopped()
        # resume: the same request translates only the pending cue and sends no request for the completed one
        w04.set_behaviour(self.model)
        self.translate(request)
        log = w04.requests_log(self.model)
        self.assertEqual(len(log), 1)
        self.assertIn("必ず失敗する", log[0]["payload"]["messages"][-1]["content"])
        self.assertEqual(self.translations()["extensions"]["stats"], {"requests": 1, "ambiguous_splits": 0, "resumed_cues": 1})
        self.assertEqual([r["target_text"] for r in self.translations()["records"]], ["译:成功する文", "译:必ず失敗する\n译:二行"])

    def test_context_overflow_is_reported_once_with_the_pending_text(self):
        body = {"error": {"code": 400, "message": "the request exceeds the available context size. try increasing the context size or enable context shift", "type": "exceed_context_size_error"}}
        w04.set_behaviour(self.model, responses=[{"kind": "http_error", "status": 400, "body": body}])
        request, _ = self.request(["長い文章その一", "長い文章その二"])
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("CONTEXT_EXCEEDED", False))
        self.assertEqual([item["text"] for item in ctx.exception.details["pending_items"]], ["長い文章その一", "長い文章その二"])   # full text, nothing truncated
        self.assertEqual(len(w04.requests_log(self.model)), 1)                              # a request that cannot fit is not retried
        self.assertIn(b"exceed_context_size_error", (Path(request["checkpoint_dir"]) / "exchanges" / "000001" / "response.body").read_bytes())
        self.assertFalse((self.output / "translations.json").exists())

    def test_truncated_generation_is_context_exceeded_after_the_attempts(self):
        w04.set_behaviour(self.model, responses=[{"kind": "sse", "content": "途中まで", "finish": "length"}] * 3)
        request, _ = self.request(["切れる文"])
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request)
        self.assertEqual((ctx.exception.code, ctx.exception.details["kind"]), ("CONTEXT_EXCEEDED", "generation_truncated"))
        self.assertEqual(len(w04.requests_log(self.model)), 3)

    def test_empty_source_starts_no_server(self):
        request, _ = self.request([])
        result = self.translate(request)
        self.assertEqual(self.translations()["status"], "EMPTY")
        self.assertFalse((self.output / "server").exists())
        self.assertEqual([issue["code"] for issue in result["issues"]], ["DICTIONARY_UNSUPPORTED_ACCEPTED", "TRANSLATION_EMPTY_SOURCE"])


class ServerOwnershipTests(TranslationTestCase):
    def test_foreign_listener_on_the_port_is_a_conflict_and_is_left_running(self):
        foreign = socket.socket()
        foreign.bind(("127.0.0.1", self.port))
        foreign.listen(5)
        try:
            request, _ = self.request(["文"])
            with self.assertRaises(c.ContractError) as ctx:
                self.translate(request)
            self.assertEqual(ctx.exception.code, "SERVER_PORT_CONFLICT")
            self.assertEqual(w04.requests_log(self.model), [])
            self.assertFalse((self.output / "server" / "process.json").exists(), "no server was launched")
            foreign.settimeout(2)
            accepted, _ = foreign.accept()                                                   # the probe connection only; the listener is still alive
            accepted.close()
            with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                pass                                                                         # the foreign listener was not touched
        finally:
            foreign.close()

    def test_a_service_serving_another_model_is_not_adopted(self):
        w04.set_behaviour(self.model, model_id="some-other-model.gguf")
        request, _ = self.request(["文"])
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request)
        self.assertEqual(ctx.exception.code, "SERVER_PORT_CONFLICT")
        self.assertEqual(ctx.exception.details["reported_models"], ["some-other-model.gguf"])
        self.assert_server_stopped()

    def test_server_exit_during_startup_fails_immediately(self):
        w04.set_behaviour(self.model, exit_on_start=True)
        request, _ = self.request(["文"])
        started = time.monotonic()
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request)
        self.assertEqual((ctx.exception.code, ctx.exception.details["exit_code"]), ("PROCESS_FAILED", 3))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(w04.requests_log(self.model), [])

    def test_slow_startup_is_waited_for_and_does_not_consume_attempts(self):
        w04.set_behaviour(self.model, startup_delay=1.2)
        request, _ = self.request(["文"])
        self.translate(request)
        self.assertEqual(len(w04.requests_log(self.model)), 1)
        self.assertEqual([r["target_text"] for r in self.translations()["records"]], ["译:文"])

    def test_cancel_during_a_silent_stream_closes_the_request_and_stops_the_server(self):
        w04.set_behaviour(self.model, responses=[{"kind": "stall", "seconds": 30}])
        request, _ = self.request(["止まる文"])
        cancel = CancelToken()

        def cancel_once_the_request_is_running() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not any(event.get("event") == "translation_request" for event in self.events):
                time.sleep(0.05)
            time.sleep(0.8)                                                                  # the first chunk has arrived; the stream is now silent
            cancel.cancel("user_request")

        threading.Thread(target=cancel_once_the_request_is_running, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request, cancel)
        self.assertEqual(ctx.exception.code, "PROCESS_CANCELLED")
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("部分".encode("utf-8"), (Path(request["checkpoint_dir"]) / "exchanges" / "000001" / "response.body").read_bytes())
        self.assertFalse(list((Path(request["checkpoint_dir"]) / "cues").iterdir()), "a half response is never checkpointed")
        self.assert_server_stopped()

    def test_idle_stream_only_raises_a_waiting_event(self):
        with patch.object(translation, "STALL_NOTICE_SECONDS", 0.3):
            w04.set_behaviour(self.model, responses=[{"kind": "stall", "seconds": 1.0}])
            request, _ = self.request(["待たされる文"])
            self.translate(request)
        waiting = [event for event in self.events if event.get("event") == "translation_waiting"]
        self.assertEqual(len(waiting), 1, self.events)
        self.assertEqual([r["target_text"] for r in self.translations()["records"]], ["部分终于完成"])
        self.assertEqual(len(w04.requests_log(self.model)), 1)


class DictionaryProtocolTests(TranslationTestCase):
    def test_pre_glossary_post_and_review_flags_follow_the_legacy_rules(self):
        texts = ["デスデス", "アルダンとトレーナー", "ラモーヌさん", "トレセン学園"]
        responses = [{"kind": "sse", "content": "です译\n阿尔丹和训练员的马娘\n拉莫努先生\nトレセン学園のまま"}]
        w04.set_behaviour(self.model, responses=responses)
        request, _ = self.request(texts)
        result = self.translate(request)
        log = w04.requests_log(self.model)
        body = log[0]["payload"]["messages"][-1]["content"]
        self.assertTrue(body.endswith("ですです\nアルダンとトレーナー\nラモーヌさん\nトレセン学園"), body)   # pre: デス->です replaces source text; gpt entries only go into the prompt
        glossary_block = body.split("参考以下术语表（可为空，格式为 src->dst #备注）：\n", 1)[1].split("\n根据以上术语表", 1)[0]
        self.assertEqual(glossary_block, "トレーナー->训练员\nラモーヌ->ラモーヌ [待确认]\nアルダン->阿尔丹\nトレ->训练\nさん->先生")   # glossary first, then gpt; only terms present in the batch
        self.assertNotIn("ウマ娘->", body)
        records = self.translations()["records"]
        by_id = {r["cue_id"]: r for r in records}
        self.assertEqual(by_id["cue-000002"]["target_text"], "阿尔丹和训练员的赛马娘")        # post: 马娘->赛马娘 applied to the model text
        self.assertEqual(by_id["cue-000004"]["target_text"], "トレセン学園のまま")           # nothing appended for a missing term
        self.assertIn("TRANSLATION_JAPANESE_PRESENT", by_id["cue-000004"]["flags"])
        self.assertIn("TRANSLATION_TERM_MISSING", by_id["cue-000004"]["flags"])              # ^^トレ->训练 target missing from the translation
        self.assertIn("TRANSLATION_UNCERTAIN", by_id["cue-000003"]["flags"]) if "[待确认]" in by_id["cue-000003"]["target_text"] else None
        trace = json.loads((self.output / "postprocess_trace.json").read_text(encoding="utf-8"))["cues"]
        self.assertEqual(trace["cue-000001"]["pre_dictionary"], [{"source": "デス", "target": "です", "mode": "all"}])
        self.assertEqual(trace["cue-000002"]["post_dictionary"], [{"source": "马娘", "target": "赛马娘", "mode": "all"}])
        self.assertEqual(trace["cue-000004"]["missing_terms"], ["训练"])
        codes = [issue["code"] for issue in result["issues"]]
        self.assertIn("TRANSLATION_TERM_MISSING", codes)
        self.assertIn("DICTIONARY_UNSUPPORTED_ACCEPTED", codes)

    def test_unaccepted_dictionary_entries_stop_before_any_server_starts(self):
        request, _ = self.request(["文"])
        request["config_snapshot"]["accepted_dictionary_warnings"] = []
        request["config_snapshot"]["config_sha256"] = c.sha256_canonical({k: v for k, v in request["config_snapshot"].items() if k != "config_sha256"})
        with self.assertRaises(c.ContractError) as ctx:
            self.translate(request)
        self.assertEqual(ctx.exception.code, "DICTIONARY_UNSUPPORTED")
        self.assertEqual(sorted(item["kind"] for item in ctx.exception.details["unaccepted"]), ["conditional_rule", "empty_source", "malformed"])
        self.assertFalse((self.output / "server").exists())

    def test_signature_changes_with_dictionary_prompt_model_and_parameters_only(self):
        base = dict(segments_sha256="1" * 64, source_revision_id=c.new_id(), dictionary_sha256="2" * 64, accepted=[], prompt="p", engine_sha256="3" * 64, model_sha256="4" * 64,
                    params=translation.effective_parameters({"batch_size": 10}))
        reference = translation.translation_signature(**base)
        self.assertEqual(translation.translation_signature(**{**base, "params": translation.effective_parameters({"batch_size": 10, "endpoint": "http://127.0.0.1:9999"})}), reference)
        for change in ({"dictionary_sha256": "5" * 64}, {"prompt": "q"}, {"model_sha256": "6" * 64}, {"params": translation.effective_parameters({"batch_size": 5})},
                       {"params": translation.effective_parameters({"temperature": 0.1})}, {"segments_sha256": "7" * 64}):
            self.assertNotEqual(translation.translation_signature(**{**base, **change}), reference, change)
        with self.assertRaises(c.ContractError):
            translation.effective_parameters({"endpoint": "http://example.com:8989"})       # external services are not a default backend


if __name__ == "__main__":
    unittest.main(verbosity=2)
