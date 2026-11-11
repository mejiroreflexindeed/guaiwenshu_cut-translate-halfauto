"""T02: persistent queue, exclusive executor, stage commits, cache reuse, failure/resume, recovery, cancellation, inbox takeover."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from upgrade import contracts as c
from upgrade import inbox as inbox_mod
from upgrade import runner as r
from upgrade import storage
from upgrade.config import default_app_config
from upgrade.locks import ExecutorLock
from upgrade.process_runner import CancelToken, process_spec, run_process
from upgrade.storage import init_data_root


class FakeStage:
    """Synthetic stage: writes one file, counts calls, can fail/crash/block until cancelled, can publish a revision id."""

    def __init__(self, name: str, behavior: str = "ok", version: int = 1):
        self.name = name
        self.behavior = behavior
        self.version = version
        self.calls = 0

    def signature_inputs(self, ctx):
        return {"version": self.version, "input": ctx.request["input"]["value"]}

    def run(self, ctx):
        self.calls += 1
        (ctx.attempt_dir / f"{self.name.lower()}.json").write_text(json.dumps({"stage": self.name, "call": self.calls}), encoding="utf-8")
        if self.behavior == "fail":
            raise c.ContractError("SUBTITLE_INVALID", f"{self.name} synthetic failure")
        if self.behavior == "crash":
            raise RuntimeError("unexpected implementation bug")
        if self.behavior == "block":
            spec = process_spec(sys.executable, ["-c", "import time; time.sleep(60)"], cwd=str(ctx.run_dir), log_dir=ctx.attempt_dir / "proc",
                                owner_token=ctx.owner_token)
            run_process(spec, ctx.cancel, ctx.emit)
            ctx.check_cancelled()
        if self.behavior == "revision":
            return {"ok": True, "machine_revision_id": c.new_id()}
        return {"ok": True}


def registry(**overrides) -> dict:
    stages = {name: FakeStage(name) for name in c.STAGES}
    for name, stage in overrides.items():
        stages[name] = stage
    return stages


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "数据根 (test)"
        init_data_root(self.root)
        self.config = default_app_config(self.root)
        self.engine = {"translation": {"workers": 1}}
        self.video = Path(self._tmp.name) / "片源 (1).mp4"
        self.video.write_bytes(b"fake")

    def tearDown(self):
        self._tmp.cleanup()

    def request(self, mode="combined", **kw):
        return r.build_job_request(self.config, self.engine, input_kind="local_video", input_value=str(self.video), recognition_mode=mode, **kw)

    def runner(self, stages=None, **kw):
        return r.QueueRunner(self.root, self.config, self.engine, registry() if stages is None else stages, **kw)

    def task_for(self, envelope):
        return r.load_task(r.run_dir(r.project_dir(self.root, envelope["request"]["project_id"]), envelope["run_id"]))


class QueueBasicsTests(RunnerTestCase):
    def test_submit_persists_before_report_and_is_idempotent(self):
        request = self.request()
        envelope = r.submit(self.root, request, display_name="first")
        self.assertTrue(r.envelope_path(self.root, request["request_id"]).is_file())
        self.assertEqual(c.validate_record("QueueEnvelope", envelope), [])
        again = r.submit(self.root, request)
        self.assertEqual(again["created_at"], envelope["created_at"])
        self.assertEqual(len(list(r.queue_dir(self.root).glob("requests/*.json"))), 1)
        events = (r.queue_dir(self.root) / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("duplicate_submit", events)
        project = r.load_project(self.root, request["project_id"])
        self.assertEqual(project["display_name"], "first")

    def test_full_run_speech_only_commits_every_stage(self):
        stages = registry(QUALITY_AND_COMMIT=FakeStage("QUALITY_AND_COMMIT", "revision"))
        envelope = r.submit(self.root, self.request("speech_only"))
        summary = self.runner(stages).run_queue()
        self.assertEqual(summary["completed"], 1)
        envelope = r.load_envelope(self.root, envelope["request"]["request_id"])
        self.assertEqual(envelope["status"], "COMPLETED")
        task = self.task_for(envelope)
        statuses = {stage["name"]: stage["status"] for stage in task["stages"]}
        self.assertEqual(statuses["OCR"], "SKIPPED")
        self.assertEqual(next(s for s in task["stages"] if s["name"] == "OCR")["skip_reason"], "MODE")
        self.assertEqual(stages["OCR"].calls, 0)
        for name in c.STAGES:
            if name != "OCR":
                self.assertEqual(statuses[name], "DONE", name)
                stage = next(s for s in task["stages"] if s["name"] == name)
                manifest_path = storage.safe_join(r.project_dir(self.root, task["project_id"]), stage["output_manifest"]["path"])
                manifest = c.load_record("StageManifest", manifest_path)
                storage.verify_committed(manifest_path.parent, manifest["outputs"])
        self.assertIsNotNone(task["machine_revision_id"])
        project = r.load_project(self.root, task["project_id"])
        self.assertEqual(project["run_ids"], [task["run_id"]])
        self.assertEqual(project["machine_revision_ids"], [task["machine_revision_id"]])
        self.assertEqual(c.validate_record("TaskRecord", task), [])

    def test_second_executor_is_refused(self):
        r.submit(self.root, self.request())
        with ExecutorLock(self.root):
            with self.assertRaises(c.ContractError) as ctx:
                self.runner().run_queue()
            self.assertEqual(ctx.exception.code, "EXECUTOR_BUSY")
        self.assertEqual(self.runner().run_queue()["completed"], 1)

    def test_second_executor_process_gets_exit_code_7(self):
        from upgrade.config import APP_CONFIG_NAME, save_app_config
        save_app_config(self.root / APP_CONFIG_NAME, self.config)
        with ExecutorLock(self.root):
            completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", "queue", "run", "--data-root", str(self.root)],
                                       capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        self.assertEqual(completed.returncode, 7, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["error"]["code"], "EXECUTOR_BUSY")

    def test_cancel_queued_keeps_json_and_is_not_run(self):
        envelope = r.submit(self.root, self.request())
        cancelled = r.cancel(self.root, envelope["request"]["request_id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertTrue(r.envelope_path(self.root, envelope["request"]["request_id"]).is_file())
        stages = registry()
        summary = self.runner(stages).run_queue()
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(stages["INPUT"].calls, 0)

    def test_missing_stage_implementation_fails_honestly(self):
        envelope = r.submit(self.root, self.request())
        summary = self.runner({}).run_queue()
        self.assertEqual(summary["failed"], 1)
        envelope = r.load_envelope(self.root, envelope["request"]["request_id"])
        self.assertEqual(envelope["status"], "FAILED")
        self.assertEqual(envelope["last_error"]["code"], "STAGE_UNAVAILABLE")


class CacheFailureRecoveryTests(RunnerTestCase):
    def test_identical_request_reuses_committed_stages_without_running_them(self):
        stages = registry()
        first = r.submit(self.root, self.request())
        self.runner(stages).run_queue()
        second = r.submit(self.root, self.request(project_id=first["request"]["project_id"]))
        self.assertNotEqual(first["request"]["request_id"], second["request"]["request_id"])
        self.runner(stages).run_queue()
        for name in c.STAGES:
            self.assertEqual(stages[name].calls, 1, name)
        task = self.task_for(r.load_envelope(self.root, second["request"]["request_id"]))
        self.assertEqual(task["status"], "COMPLETED")
        events = (r.run_dir(r.project_dir(self.root, task["project_id"]), task["run_id"]) / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(events.count("stage_cache_hit"), len(c.STAGES))

    def test_changed_inputs_are_not_served_from_cache(self):
        stages = registry()
        first = r.submit(self.root, self.request())
        self.runner(stages).run_queue()
        stages["SOURCE"].version = 2  # algorithm/parameter change -> new signature for SOURCE and everything downstream
        r.submit(self.root, self.request(project_id=first["request"]["project_id"]))
        self.runner(stages).run_queue()
        self.assertEqual(stages["INPUT"].calls, 1)
        self.assertEqual(stages["SOURCE"].calls, 2)

    def test_failure_keeps_candidate_and_resume_adds_attempt(self):
        failing = FakeStage("TRANSLATION", "fail")
        stages = registry(TRANSLATION=failing)
        envelope = r.submit(self.root, self.request())
        summary = self.runner(stages).run_queue()
        self.assertEqual(summary["failed"], 1)
        envelope = r.load_envelope(self.root, envelope["request"]["request_id"])
        self.assertEqual(envelope["status"], "FAILED")
        self.assertEqual(envelope["last_error"]["code"], "SUBTITLE_INVALID")
        task = self.task_for(envelope)
        translation = next(s for s in task["stages"] if s["name"] == "TRANSLATION")
        self.assertEqual(translation["status"], "FAILED")
        self.assertEqual(len(translation["attempts"]), 1)
        self.assertIsNone(translation["output_manifest"])
        run = r.run_dir(r.project_dir(self.root, task["project_id"]), task["run_id"])
        self.assertEqual(len(storage.list_pending(run / "stages" / "TRANSLATION")), 1)
        self.assertEqual(next(s for s in task["stages"] if s["name"] == "SUBTITLE_BUILD")["status"], "PENDING")
        with self.assertRaises(c.ContractError):
            r.resume(self.root, "not-a-run")
        failing.behavior = "ok"
        resumed = r.resume(self.root, task["run_id"])
        self.assertEqual(resumed["status"], "QUEUED")
        summary = self.runner(stages).run_queue()
        self.assertEqual(summary["completed"], 1)
        task = self.task_for(r.load_envelope(self.root, envelope["request"]["request_id"]))
        translation = next(s for s in task["stages"] if s["name"] == "TRANSLATION")
        self.assertEqual(translation["status"], "DONE")
        self.assertEqual([a["status"] for a in translation["attempts"]], ["FAILED", "DONE"])
        self.assertEqual(stages["INPUT"].calls, 1, "committed upstream stages must not rerun on resume")
        self.assertEqual(stages["TRANSLATION"].calls, 2)

    def test_crash_inside_stage_is_a_failed_attempt_not_done(self):
        stages = registry(ASR=FakeStage("ASR", "crash"))
        envelope = r.submit(self.root, self.request())
        self.runner(stages).run_queue()
        envelope = r.load_envelope(self.root, envelope["request"]["request_id"])
        self.assertEqual(envelope["status"], "FAILED")
        self.assertEqual(envelope["last_error"]["code"], "STAGE_FAILED")

    def test_unclean_shutdown_recovery_rebuilds_pointer_and_does_not_rerun(self):
        stages = registry()
        envelope = r.submit(self.root, self.request())
        request = envelope["request"]
        project = r.project_dir(self.root, request["project_id"])
        run_id = c.new_id()
        run = r.run_dir(project, run_id)
        run.mkdir(parents=True)
        task = r._new_task(request, run_id)
        # simulate: INPUT manifest committed, then the executor died before task.json/envelope were updated
        sig = r.stage_signature("INPUT", request, stages["INPUT"].signature_inputs(type("Ctx", (), {"request": request})()))
        pending = storage.begin_pending(run / "stages" / "INPUT")
        (pending / "input.json").write_text("{}", encoding="utf-8")
        (pending / "validation.json").write_text('{"ok": true}', encoding="utf-8")
        attempt_id = c.new_id()
        storage.commit_pending(pending, run / "stages" / "INPUT" / attempt_id, manifest_kind="StageManifest", build_manifest=lambda refs: {
            "schema_version": 2, "run_id": run_id, "request_id": request["request_id"], "stage": "INPUT", "attempt_id": attempt_id,
            "input_signature": sig, "outputs": refs, "validation_ref": next(x for x in refs if x["path"] == "validation.json"), "committed_at": c.utc_now()})
        media = next(s for s in task["stages"] if s["name"] == "MEDIA_PREP")
        media.update({"status": "RUNNING", "started_at": c.utc_now()})
        media["attempts"].append({"attempt_id": c.new_id(), "status": "RUNNING", "started_at": c.utc_now(), "ended_at": None, "input_signature": "b" * 64,
                                  "output_manifest": None, "error": None})
        task["status"] = "RUNNING"
        r._save_task(run, task)
        envelope["status"] = "RUNNING"
        envelope["run_id"] = run_id
        r._save_envelope(self.root, envelope)
        report = r.recover(self.root)
        self.assertEqual(report["interrupted"], [request["request_id"]])
        self.assertGreaterEqual(report["pointers_rebuilt"], 1)
        task = r.load_task(run)
        self.assertEqual(task["status"], "INTERRUPTED")
        self.assertEqual(next(s for s in task["stages"] if s["name"] == "INPUT")["status"], "DONE")
        self.assertEqual(next(s for s in task["stages"] if s["name"] == "MEDIA_PREP")["status"], "INTERRUPTED")
        r.resume(self.root, run_id)
        self.runner(stages).run_queue()
        self.assertEqual(stages["INPUT"].calls, 0, "committed INPUT must not be rerun")
        self.assertEqual(stages["MEDIA_PREP"].calls, 1)
        self.assertEqual(r.load_envelope(self.root, request["request_id"])["status"], "COMPLETED")

    def test_recover_skips_when_an_executor_is_live(self):
        with ExecutorLock(self.root):
            self.assertTrue(r.recover(self.root)["skipped"])


class CancellationTests(RunnerTestCase):
    def test_cancel_running_request_kills_owned_child_and_leaves_no_done(self):
        stages = registry(ASR=FakeStage("ASR", "block"))
        envelope = r.submit(self.root, self.request())
        request_id = envelope["request"]["request_id"]
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def cancel_when_running():
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if r.load_envelope(self.root, request_id)["status"] == "RUNNING":
                            break
                    except c.ContractError:
                        pass
                    time.sleep(0.2)
                time.sleep(1.0)
                r.cancel(self.root, request_id)
            threading.Thread(target=cancel_when_running, daemon=True).start()
            summary = self.runner(stages).run_queue()
            self.assertEqual(summary["cancelled"], 1)
            envelope = r.load_envelope(self.root, request_id)
            self.assertEqual(envelope["status"], "CANCELLED")
            task = self.task_for(envelope)
            asr = next(s for s in task["stages"] if s["name"] == "ASR")
            self.assertEqual(asr["status"], "CANCELLED")
            self.assertEqual(asr["attempts"][-1]["status"], "CANCELLED")
            self.assertIsNone(asr["output_manifest"])
            self.assertEqual(next(s for s in task["stages"] if s["name"] == "SOURCE")["status"], "PENDING")
            self.assertFalse(r.cancel_flag_path(self.root, request_id).exists())
            self.assertIsNone(unrelated.poll(), "unrelated process must survive cancellation")
        finally:
            unrelated.kill()
            unrelated.wait(timeout=5)


class HumanFileProtectionTests(RunnerTestCase):
    def test_committed_review_bytes_survive_runs_and_cancel(self):
        project_id = c.new_id()
        r.create_project(self.root, "p", project_id=project_id)
        project = r.project_dir(self.root, project_id)
        pending = storage.begin_pending(project)
        (pending / "edit.ass").write_bytes("[Script Info]\r\nTitle: 手改\r\n".encode("utf-8-sig"))
        final = project / "revisions" / "review" / c.new_id()
        committed = storage.commit_pending(pending, final, manifest_kind="StageManifest", build_manifest=lambda refs: {
            "schema_version": 2, "run_id": c.new_id(), "request_id": c.new_id(), "stage": "QUALITY_AND_COMMIT", "attempt_id": c.new_id(),
            "input_signature": "c" * 64, "outputs": refs, "validation_ref": refs[0], "committed_at": c.utc_now()})
        r.submit(self.root, self.request(project_id=project_id))
        self.runner(registry(TRANSLATION=FakeStage("TRANSLATION", "fail"))).run_queue()
        storage.verify_committed(final, committed["artifact_refs"])


class InboxTests(RunnerTestCase):
    def factory(self, parsed, source):
        url = str(parsed.get("url", "")).strip()
        video = str(parsed.get("video_file", parsed.get("video", ""))).strip()
        return r.build_job_request(self.config, self.engine, input_kind="url" if url else "local_video", input_value=url or video, recognition_mode="combined")

    def test_intake_pending_working_and_duplicates(self):
        inbox = Path(self._tmp.name) / "inbox"
        (inbox / "working").mkdir(parents=True)
        (inbox / "a.txt").write_text("https://example.test/v/1\n", encoding="utf-8")
        (inbox / "b.json").write_text(json.dumps({"video_file": str(self.video), "job_name": "本地片"}), encoding="utf-8")
        (inbox / "working" / "left.url").write_text("URL=https://example.test/v/2\n", encoding="utf-8")
        rows = inbox_mod.intake(self.root, inbox, self.factory)
        self.assertEqual([row["action"] for row in rows], ["queued", "queued", "queued"])
        self.assertEqual(len(r.list_envelopes(self.root)), 3)
        self.assertTrue(any("recovered-left.url" in (row.get("recovered_copy") or "") for row in rows))
        self.assertFalse(list(inbox.glob("*.txt")) or list((inbox / "working").iterdir()))
        # crash between mapping and move: the same content shows up again -> duplicate, no second envelope
        (inbox / "a-again.txt").write_text("https://example.test/v/1\n", encoding="utf-8")
        rows = inbox_mod.intake(self.root, inbox, self.factory)
        self.assertEqual(rows[0]["action"], "duplicate")
        self.assertEqual(len(r.list_envelopes(self.root)), 3)
        self.assertEqual(len(list((inbox / inbox_mod.TAKEN_OVER_DIR).iterdir())), 5)
        (inbox / "bad.json").write_text("[1,2]", encoding="utf-8")
        rows = inbox_mod.intake(self.root, inbox, self.factory)
        self.assertEqual(rows[0]["action"], "failed")
        self.assertTrue((inbox / "bad.json").exists(), "unparseable requests stay where they are")


class CliQueueTests(RunnerTestCase):
    def _run(self, *args):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        return completed.returncode, json.loads(completed.stdout) if completed.stdout.strip() else {}, completed.stderr

    def test_submit_queue_cancel_resume_contract(self):
        from upgrade.config import APP_CONFIG_NAME, save_app_config
        config_path = self.root / APP_CONFIG_NAME
        save_app_config(config_path, self.config)
        code, result, err = self._run("submit", "--config", str(config_path), "--video", str(self.video), "--mode", "speech_only")
        self.assertEqual(code, 0, err)
        request_id = result["ids"]["request_id"]
        self.assertEqual(r.load_envelope(self.root, request_id)["status"], "QUEUED")
        code, result, _ = self._run("submit", "--config", str(config_path), "--video", str(self.video), "--mode", "speech_only", "--timing-mode", "mixed")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "NEEDS_SELECTION")
        code, result, _ = self._run("submit", "--config", str(config_path), "--video", str(self.video / "missing.mp4"), "--mode", "combined")
        self.assertEqual(code, 3)
        self.assertEqual(result["error"]["code"], "INPUT_MISSING")
        code, result, _ = self._run("queue", "run", "--data-root", str(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(result["ids"]["failed"], 1, "W02 build has no stage implementations: honest STAGE_UNAVAILABLE failure")
        run_id = r.load_envelope(self.root, request_id)["run_id"]
        code, result, _ = self._run("resume", "--data-root", str(self.root), "--run", run_id)
        self.assertEqual(code, 0)
        self.assertEqual(result["ids"]["status"], "QUEUED")
        code, result, _ = self._run("queue", "cancel", "--data-root", str(self.root), "--request", request_id)
        self.assertEqual(code, 0)
        self.assertEqual(result["ids"]["status"], "CANCELLED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
