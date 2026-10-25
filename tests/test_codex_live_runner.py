"""Live acceptance is explicitly selected and reports no captured transcripts."""

import os
import json
import copy
import io
from pathlib import Path
import subprocess
import sys
import unittest
import importlib.util
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("codex_integration", Path(__file__).with_name("codex_integration.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

RUNNER = Path(__file__).with_name("codex_integration.py")
REQUIRED_CHECKS = (
    "streaming", "command_execution", "file_edit", "tool_result_continuation",
    "high_effort_history", "saved_resume_follow_up", "local_compaction", "cancellation",
    "live_upstream_disconnect_cleanup", "independent_proxy_lifetime", "interactive_no_daemon",
)


class LiveRunnerBoundaryTests(unittest.TestCase):
    def passing_report(self):
        return {"mode": "live", "models": {
            model: {**dict.fromkeys(REQUIRED_CHECKS, "pass"),
                    "sap_context_capacity": "unverified: operator-selected catalog values",
                    "protocol_evidence": {"last_outcome": "failed"}}
            for model in ("gpt-6-astra", "gpt-6.1-sol")}}

    def test_live_result_requires_every_check_for_both_models(self):
        report = self.passing_report()
        original = copy.deepcopy(report)
        self.assertEqual(runner.live_acceptance_exit_code(report), 0)
        self.assertEqual(report, original)
        for model in ("gpt-6-astra", "gpt-6.1-sol"):
            for check in REQUIRED_CHECKS:
                for value in ("failed", "blocked: evidence missing", None, True, {}):
                    with self.subTest(model=model, check=check, value=value):
                        report = self.passing_report()
                        report["models"][model][check] = value
                        self.assertNotEqual(runner.live_acceptance_exit_code(report), 0)
                report = self.passing_report()
                del report["models"][model][check]
                self.assertNotEqual(runner.live_acceptance_exit_code(report), 0)
            report = self.passing_report()
            del report["models"][model]
            self.assertNotEqual(runner.live_acceptance_exit_code(report), 0)
        self.assertNotEqual(runner.live_acceptance_exit_code({"models": {}}), 0)
        self.assertNotEqual(runner.live_acceptance_exit_code({}), 0)

    def test_mocked_live_runner_aggregates_real_results_without_sap(self):
        source = {"models": [{"slug": model, "supported_reasoning_levels": [
            {"effort": "low"}, {"effort": "high"}]} for model in ("gpt-6-astra", "gpt-6.1-sol")]}

        def codex_run(command, **kwargs):
            workspace = Path(kwargs["cwd"])
            marker = (workspace / "input.txt").read_text().strip()
            prompt = command[-1]
            records = [{"type": "thread.started", "thread_id": "synthetic-thread"},
                       {"type": "item.completed", "item": {"type": "agent_message", "text": marker}}]
            if prompt.startswith("Read input.txt"):
                (workspace / "edited.txt").write_text(marker + "\n")
                records.append({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}})
            if "model_auto_compact_token_limit=1000" in command:
                (Path(kwargs["env"]["CODEX_HOME"]) / "rollout.jsonl").write_text('{"type":"compacted"}\n')
            return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, records)), "")

        evidence = {model: MagicMock() for model in ("gpt-6-astra", "gpt-6.1-sol")}
        for item in evidence.values():
            item.high_effort_history_verified.return_value = True
            item.active_request.return_value = "cancel-request"
            item.request_closed.return_value = True
            item.records = {"cancel-request": {"outcome": "failed"}}
            item.public_summary.return_value = {"last_outcome": "failed"}
        process = MagicMock(returncode=1)
        process.poll.return_value = None
        process.communicate.return_value = ("", "")
        for interactive, expected in ((True, 0), (False, 1)):
            with self.subTest(interactive=interactive), \
                    patch.object(runner, "installed_catalog", return_value=(source, "synthetic-codex")), \
                    patch.object(runner.subprocess, "run", side_effect=codex_run), \
                    patch.object(runner.subprocess, "Popen", return_value=process), \
                    patch.object(runner.os, "killpg"), \
                    patch.object(runner, "interactive_acceptance", return_value=interactive), \
                    patch.object(runner.proxy._native_http_session, "post", side_effect=AssertionError("Unexpected SAP call")), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                result = runner.live_acceptance("synthetic-codex", "http://127.0.0.1:9/v1", evidence)
                report = json.loads(output.getvalue())
                self.assertEqual(set(report["models"]), {"gpt-6-astra", "gpt-6.1-sol"})
                for outcomes in report["models"].values():
                    for check in REQUIRED_CHECKS:
                        if check != "interactive_no_daemon" or interactive:
                            self.assertEqual(outcomes[check], "pass", check)
                    self.assertIn("unverified", outcomes["sap_context_capacity"])
                self.assertEqual(result, expected)

    def run_cli(self, args, token=None):
        env = dict(os.environ)
        env.pop("SAP_AI_PROXY_API_KEY", None)
        if token:
            env["SAP_AI_PROXY_API_KEY"] = token
        return subprocess.run([sys.executable, str(RUNNER), *args], env=env,
                              capture_output=True, text=True, timeout=15)

    def test_missing_mode_never_defaults_to_live(self):
        result = self.run_cli([])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--offline", result.stderr)
        self.assertIn("--live", result.stderr)

    def test_live_requires_proxy_url_and_token_without_printing_credentials(self):
        result = self.run_cli(["--live", "--proxy-url", "http://127.0.0.1:9/v1"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("SAP_AI_PROXY_API_KEY", result.stderr)
        result = self.run_cli(["--live"], token="synthetic-secret-do-not-print")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--proxy-url", result.stderr)
        self.assertNotIn("synthetic-secret-do-not-print", result.stdout + result.stderr)

    def test_command_output_does_not_count_as_grounded_final_or_history_recall(self):
        records = [{"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0,
                    "aggregated_output": "MARKER"}},
                   {"type": "item.completed", "item": {"type": "agent_message", "text": "No usable result"}}]
        self.assertNotIn("MARKER", runner.final_answer(records))
        self.assertFalse(runner.history_only_answer(subprocess.CompletedProcess([], 0), records, "MARKER"))
        records[-1]["item"]["text"] = "MARKER"
        self.assertFalse(runner.history_only_answer(subprocess.CompletedProcess([], 0), records, "MARKER"))

    def test_compaction_fixture_never_manufactures_missing_tool_result(self):
        fixture = runner.OfflineFixture("unused", {}, "unused")
        fixture.marker = "MISSING_MARKER"
        event = fixture.events({"model": "gpt-6-astra", "input": [{"role": "user", "content": "OFFLINE_COMPACT"}]})[-1]
        self.assertNotIn("MISSING_MARKER", json.dumps(event))

    def test_live_reasoning_evidence_requires_high_usage_and_exact_opaque_replay(self):
        evidence = runner.LiveProtocolEvidence()
        body = {"model": "gpt-6-astra", "reasoning": {"effort": "high"}, "input": [], "prompt_cache_key": "synthetic-session"}
        evidence.request(body, "initial")
        evidence.event({"type": "response.completed", "response": {
            "output": [{"type": "reasoning", "encrypted_content": "synthetic-opaque"}],
            "usage": {"output_tokens_details": {"reasoning_tokens": 5}}}}, "initial")
        evidence.closed("completed", "initial")
        self.assertFalse(evidence.high_effort_history_verified())
        evidence.request({**body, "input": [{"type": "reasoning", "encrypted_content": "wrong"}]}, "wrong")
        evidence.closed("completed", "wrong")
        self.assertFalse(evidence.high_effort_history_verified())
        evidence.request({**body, "input": [{"type": "reasoning", "encrypted_content": "synthetic-opaque"}]}, "failed")
        evidence.closed("failed", "failed")
        self.assertFalse(evidence.high_effort_history_verified())
        evidence.request({**body, "input": [{"type": "reasoning", "encrypted_content": "synthetic-opaque"}]}, "replay")
        self.assertFalse(evidence.high_effort_history_verified())
        evidence.closed("completed", "replay")
        self.assertTrue(evidence.high_effort_history_verified())
        self.assertNotIn("synthetic-opaque", json.dumps(evidence.public_summary()))

    def test_cancellation_requires_active_upstream_then_unsuccessful_close(self):
        evidence = runner.LiveProtocolEvidence()
        evidence.request({"model": "gpt-6-astra", "input": "old"}, "old")
        evidence.opened("old")
        evidence.request({"model": "gpt-6-astra", "input": "synthetic CANCEL_ab123"}, "cancel")
        self.assertIsNone(evidence.active_request("CANCEL_ab123"))
        evidence.opened("cancel")
        self.assertEqual(evidence.active_request("CANCEL_ab123"), "cancel")
        evidence.closed("failed", "old")
        self.assertEqual(evidence.active_request("CANCEL_ab123"), "cancel")
        self.assertFalse(evidence.request_closed("cancel"))
        evidence.closed("failed", "cancel")
        self.assertTrue(evidence.request_closed("cancel"))
        self.assertEqual(evidence.public_summary()["last_outcome"], "failed")

    def test_interactive_prompt_redraws_do_not_count_as_completed_answer(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "MARKER MARKER"}}) + "\n")
            self.assertFalse(runner.interactive_completed(Path(directory), "MARKER"))
            with path.open("a") as output:
                output.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "MARKER"}}) + "\n")
            self.assertTrue(runner.interactive_completed(Path(directory), "MARKER"))

    def test_reasoning_evidence_tracks_delivered_item_not_different_terminal_copy(self):
        evidence = runner.LiveProtocolEvidence()
        body = {"model": "gpt-6-astra", "reasoning": {"effort": "high"}, "input": [], "prompt_cache_key": "session"}
        evidence.request(body, "initial")
        evidence.event({"type": "response.output_item.done", "item": {
            "type": "reasoning", "encrypted_content": "streamed-opaque"}}, "initial")
        evidence.event({"type": "response.completed", "response": {
            "output": [{"type": "reasoning", "encrypted_content": "different-terminal-opaque"}],
            "usage": {"output_tokens_details": {"reasoning_tokens": 5}}}}, "initial")
        evidence.closed("completed", "initial")
        evidence.request({**body, "input": [{"type": "reasoning", "encrypted_content": "streamed-opaque"}]}, "replay")
        evidence.closed("completed", "replay")
        self.assertTrue(evidence.high_effort_history_verified())
