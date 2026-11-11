"""T02: owned subprocesses — argv fidelity, env override without leaking, logs, cancellation of the owned tree only."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from upgrade import contracts as c
from upgrade.process_runner import CancelToken, run_process, process_spec

PRINT_ARGV = "import sys, os, json; print(json.dumps({'argv': sys.argv[1:], 'marker': os.environ.get('ROAST_TEST_MARKER')}, ensure_ascii=False))"


class ProcessRunnerTests(unittest.TestCase):
    def test_argv_env_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            weird = str(Path(tmp) / "中文 目录 (1) & it's" / "v.mp4")
            spec = process_spec(sys.executable, ["-X", "utf8", "-c", PRINT_ARGV, weird, "a b"], cwd=tmp, log_dir=Path(tmp) / "logs",
                                owner_token="t1", env_overrides={"ROAST_TEST_MARKER": "secret-value-not-logged"})
            events = []
            result = run_process(spec, CancelToken(), events.append)
            self.assertEqual(c.validate_record("ProcessResult", result), [])
            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["cancelled"])
            stdout = (Path(tmp) / "logs" / result["stdout_ref"]["path"]).read_text(encoding="utf-8")
            payload = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(payload["argv"], [weird, "a b"])
            self.assertEqual(payload["marker"], "secret-value-not-logged")
            sidecar = json.loads((Path(tmp) / "logs" / "process.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["env_override_names"], ["ROAST_TEST_MARKER"])
            self.assertNotIn("secret-value-not-logged", json.dumps(sidecar))
            self.assertNotIn("secret-value-not-logged", json.dumps(events))
            self.assertEqual([e["event"] for e in events], ["process_started", "process_exited"])

    def test_nonzero_exit_is_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = process_spec(sys.executable, ["-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(7)"], cwd=tmp, log_dir=Path(tmp) / "logs", owner_token="t")
            result = run_process(spec)
            self.assertEqual(result["exit_code"], 7)
            self.assertIn("boom", (Path(tmp) / "logs" / "stderr.log").read_text(encoding="utf-8"))

    def test_missing_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = process_spec(Path(tmp) / "nope.exe", [], cwd=tmp, log_dir=Path(tmp) / "logs", owner_token="t")
            with self.assertRaises(c.ContractError) as ctx:
                run_process(spec)
            self.assertEqual(ctx.exception.code, "RESOURCE_MISSING")

    def test_cancel_terminates_owned_tree_and_spares_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild_survived.txt"
            child_code = (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', \"import time, sys; time.sleep(6); open(sys.argv[1], 'w').write('x')\", sys.argv[1]])\n"
                "time.sleep(60)\n"
            )
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                spec = process_spec(sys.executable, ["-c", child_code, str(marker)], cwd=tmp, log_dir=Path(tmp) / "logs", owner_token="t")
                token = CancelToken()
                events = []
                threading.Timer(1.0, lambda: token.cancel("test_cancel")).start()
                started = time.monotonic()
                result = run_process(spec, token, events.append)
                elapsed = time.monotonic() - started
                child_stderr = (Path(tmp) / "logs" / "stderr.log").read_text(encoding="utf-8", errors="replace")
                self.assertTrue(result["cancelled"], f"child exited early with {result['exit_code']} after {elapsed:.2f}s: {child_stderr[:600]}")
                self.assertLess(elapsed, 15)
                self.assertIn("process_terminated", [e["event"] for e in events])
                time.sleep(max(0.0, 7.5 - elapsed))
                self.assertFalse(marker.exists(), "grandchild outlived the cancelled job")
                self.assertIsNone(unrelated.poll(), "an unrelated process was killed")
            finally:
                unrelated.kill()
                unrelated.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
