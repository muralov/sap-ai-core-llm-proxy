"""Explicit actual-Codex acceptance against a fake native backend and real proxy.

Run from the project root: python tests/codex_integration.py --offline
Only synthetic prompts and temporary workspaces are used in offline mode.
"""

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import os
import pty
import re
import select
import signal
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import time
import termios
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_server as proxy
from examples.codex.generate_catalog import MODELS, build_catalog, installed_catalog
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-offline-proxy-token"
LIVE_REQUIRED_CHECKS = frozenset({
    "streaming", "command_execution", "file_edit", "tool_result_continuation",
    "high_effort_history", "saved_resume_follow_up", "local_compaction", "cancellation",
    "live_upstream_disconnect_cleanup", "independent_proxy_lifetime", "interactive_no_daemon",
})


def response_body(model, output):
    return {"id": "resp_fixture", "object": "response", "created_at": int(time.time()),
            "model": model, "status": "completed", "output": output, "error": None,
            "incomplete_details": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}


def text_events(model, text):
    message = {"type": "message", "id": "msg_fixture", "role": "assistant", "phase": "final_answer",
               "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}
    body = response_body(model, [message])
    return [{"type": "response.created", "response": {**body, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**message, "status": "in_progress", "content": []}},
            {"type": "response.content_part.added", "item_id": message["id"], "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0,
             "content_index": 0, "delta": text},
            {"type": "response.output_text.done", "item_id": message["id"], "output_index": 0,
             "content_index": 0, "text": text},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": body}]


def command_events(model, command):
    item = {"type": "function_call", "id": "fc_command", "call_id": "call_command",
            "name": "exec_command", "arguments": json.dumps({"cmd": command, "max_output_chars": 2000}),
            "status": "completed"}
    return [{"type": "response.created", "response": {
                **response_body(model, []), "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "status": "in_progress", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": item["id"],
             "output_index": 0, "delta": item["arguments"]},
            {"type": "response.function_call_arguments.done", "item_id": item["id"],
             "output_index": 0, "arguments": item["arguments"]},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response_body(model, [item])}]


def json_records(stdout):
    records = []
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        except ValueError:
            pass
    return records


def final_answer(records):
    return "\n".join(record["item"].get("text", "") for record in records
                     if record.get("type") == "item.completed"
                     and record.get("item", {}).get("type") == "agent_message")


def successful_commands(records):
    return [record["item"] for record in records if record.get("type") == "item.completed"
            and record.get("item", {}).get("type") == "command_execution"
            and record["item"].get("exit_code") == 0]


def history_only_answer(completed, records, marker):
    commands = [record for record in records if record.get("item", {}).get("type") == "command_execution"]
    return completed.returncode == 0 and marker in final_answer(records) and not commands


def received_tool_result(body):
    return next((item["output"] for item in body.get("input", []) if isinstance(item, dict)
                 and item.get("type") == "function_call_output" and item.get("call_id") == "call_command"), "")


class OfflineFixture:
    def __init__(self, binary, source, version):
        self.binary, self.source, self.version = binary, source, version
        self.inbound = []
        self.upstream_requests = []
        self.command = None
        self.marker = "SYNTHETIC_RESULT_" + os.urandom(6).hex()
        self.compacted = False
        self.compaction_input = None
        self.force_compaction = False
        self.failure = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = []

    def events(self, body):
        if self.failure:
            partial = command_events(body["model"], "touch MUST_NOT_EXIST")[:3]
            partial[-1]["delta"] = '{"cmd":"touch '
            if self.failure in {"failed", "incomplete"}:
                terminal = response_body(body["model"], [])
                terminal["status"] = self.failure
                terminal["error"] = {"code": "server_error", "message": "synthetic failure"} if self.failure == "failed" else None
                terminal["incomplete_details"] = {"reason": "max_output_tokens"} if self.failure == "incomplete" else None
                partial.append({"type": "response." + self.failure, "response": terminal})
            return partial
        if "OFFLINE_COMPACT" in json.dumps(body.get("input", [])):
            self.compacted = True
            self.compaction_input = body
            result = received_tool_result(body)
            return text_events(body["model"], "Preserve tool result: " + str(result))
        outputs = [item for item in body.get("input", []) if isinstance(item, dict)
                   and item.get("type") == "function_call_output"]
        if self.command and not outputs and not self.compacted:
            events = command_events(body["model"], self.command)
            if self.force_compaction and not self.compacted:
                events[-1]["response"]["usage"].update(input_tokens=20000, total_tokens=20005)
            return events
        if self.command:
            result = received_tool_result(body)
            text = self.marker if self.marker in str(result) or (self.compacted and self.marker in json.dumps(body["input"])) else "TOOL_RESULT_MISSING"
            return text_events(body["model"], text)
        return text_events(body["model"], "OFFLINE_OK")

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="codex-proxy-offline-"))
        self.root = Path(directory)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        self.catalog = build_catalog(self.source, dict.fromkeys(MODELS, 272000), self.version,
                                     dict.fromkeys(MODELS, 872000))
        self.catalog_path = self.root / "models.json"
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")
        fixture = self

        class Backend(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.upstream_requests.append(body)
                raw = b"".join((f"event: {event['type']}\ndata: " +
                               json.dumps({**event, "sequence_number": index}) + "\n\n").encode()
                               for index, event in enumerate(fixture.events(body)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if fixture.failure == "stall":
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if fixture.failure == "stall":
                    self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                else:
                    self.wfile.write(raw)
                self.wfile.flush()
                fixture.started.set()
                if fixture.failure == "stall":
                    fixture.release.wait(5)
                    self.close_connection = True

        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.backend.daemon_threads = True
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.stack.callback(self.backend.server_close)
        self.stack.callback(self.backend.shutdown)
        self.stack.callback(self.release.set)
        cfg = proxy.ProxyConfig(secret_authentication_tokens=[TOKEN])
        account = proxy.SubAccountConfig("offline", "synthetic-group", "unused", {})
        account.service_key = proxy.ServiceKey("synthetic", "synthetic", "http://unused", "synthetic")
        account.normalized_models = dict.fromkeys(MODELS, [f"http://127.0.0.1:{self.backend.server_port}/deployment"])
        cfg.subaccounts["offline"] = account
        cfg.build_model_mapping()
        session = proxy.create_native_http_session()
        real_post = session.post
        def tracked_post(*args, **kwargs):
            upstream = real_post(*args, **kwargs)
            close = upstream.close
            def tracked_close():
                fixture.closed.append(upstream.url)
                close()
            upstream.close = tracked_close
            return upstream
        session.post = tracked_post
        self.stack.callback(session.close)
        self.stack.enter_context(patch.object(proxy, "proxy_config", cfg))
        self.stack.enter_context(patch.object(proxy, "fetch_token", return_value="synthetic-sap-token"))
        self.stack.enter_context(patch.object(proxy, "_native_http_session", session))
        self.stack.enter_context(patch.object(proxy, "_log_requests_enabled", False))
        self.stack.enter_context(patch.object(proxy, "_NATIVE_RESPONSES_TIMEOUT", (1, 2)))

        def capture(environ, start_response):
            if environ["PATH_INFO"] == "/v1/responses":
                data = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
                environ["wsgi.input"] = io.BytesIO(data)
                fixture.inbound.append((json.loads(data), {
                    key: value for key, value in environ.items()
                    if key.startswith("HTTP_") and key not in {"HTTP_AUTHORIZATION", "HTTP_X_API_KEY"}}))
            return proxy.app(environ, start_response)

        self.server = make_server("127.0.0.1", 0, capture, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.stack.callback(self.server.server_close)
        self.stack.callback(self.server.shutdown)
        config = (ROOT / "examples/codex/config.toml.example").read_text()
        config = config.replace("/absolute/path/to/sap-ai-core-llm-proxy/examples/codex/models.json", str(self.catalog_path))
        config = config.replace("http://127.0.0.1:3001/v1", f"http://127.0.0.1:{self.server.server_port}/v1")
        (self.home / "config.toml").write_text(config)
        self.env = dict(os.environ, CODEX_HOME=str(self.home), SAP_AI_PROXY_API_KEY=TOKEN)
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def run(self, model, prompt, resume=None, compact=False):
        command = [self.binary, "exec", "--strict-config", "--json", "--skip-git-repo-check",
                               "-s", "workspace-write", "-c", 'approval_policy="never"',
                               "-C", str(self.workspace), "-m", model]
        if compact:
            self.force_compaction = True
            command += ["-c", "model_auto_compact_token_limit=10000", "-c", 'compact_prompt="OFFLINE_COMPACT"']
        if resume:
            command += ["resume", resume]
        command += [prompt]
        return subprocess.run(command,
                              env=self.env, cwd=self.workspace, capture_output=True, text=True, timeout=30)

    def wait_closed(self, count):
        deadline = time.monotonic() + 3
        while len(self.closed) < count and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        if len(self.closed) < count:
            raise AssertionError("Native inference ownership not released within test read-timeout bound")


class ActualCodexOfflineTests(unittest.TestCase):
    binary = "codex"

    @classmethod
    def setUpClass(cls):
        cls.source, cls.version = installed_catalog(cls.binary)

    def test_both_models_emit_supported_native_requests(self):
        for model in MODELS:
            with self.subTest(model=model), OfflineFixture(self.binary, self.source, self.version) as fixture:
                result = fixture.run(model, "Reply OFFLINE_OK. Do not execute commands.")
                self.assertTrue(fixture.inbound, "Codex did not send a request to the real proxy")
                for body, headers in fixture.inbound:
                    self.assertEqual(body["model"], model)
                    self.assertIs(body.get("store"), False)
                    self.assertIs(body.get("stream"), True)
                    self.assertNotIn("additional_tools", body)
                    self.assertNotIn("responses-lite", str(headers).lower())
                    self.assertNotIn("context", body.get("reasoning", {}))
                    tools = body.get("tools", [])
                    self.assertTrue(tools, "No command tools were exposed")
                    self.assertEqual({tool["type"] for tool in tools}, {"function"},
                                     "Unsupported Codex tool types: " + str([(t["type"], t.get("name")) for t in tools]))
                    self.assertIn("exec_command", [tool["name"] for tool in tools])
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn("OFFLINE_OK", result.stdout)
                self.assertTrue(fixture.upstream_requests, "Native backend did not receive inference")

    def test_both_models_execute_command_edit_and_submit_matching_result(self):
        for model in MODELS:
            with self.subTest(model=model), OfflineFixture(self.binary, self.source, self.version) as fixture:
                (fixture.workspace / "input.txt").write_text(fixture.marker + "\n")
                fixture.command = "cp input.txt edited.txt && cat edited.txt"
                result = fixture.run(model, "Read input.txt, copy its contents to edited.txt, and report the value.")
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                edited = fixture.workspace / "edited.txt"
                self.assertTrue(edited.exists(), "Actual Codex command did not create the edited file")
                self.assertEqual(edited.read_text(), fixture.marker + "\n")
                self.assertGreaterEqual(len(fixture.upstream_requests), 2)
                continuation = fixture.upstream_requests[1]
                results = [item for item in continuation["input"] if item.get("type") == "function_call_output"]
                self.assertTrue(any(item.get("call_id") == "call_command" and fixture.marker in str(item["output"])
                                    for item in results), "Matching actual tool result not replayed")
                calls = [item for item in continuation["input"] if item.get("type") == "function_call"]
                self.assertTrue(any(item.get("call_id") == "call_command" for item in calls))
                self.assertIn(fixture.marker, final_answer(json_records(result.stdout)))
                self.assertTrue(successful_commands(json_records(result.stdout)))
                self.assertNotIn("TOOL_RESULT_MISSING", result.stdout)

    def test_same_model_saved_resume_follow_up_and_local_compaction(self):
        for model in MODELS:
            with self.subTest(model=model), OfflineFixture(self.binary, self.source, self.version) as fixture:
                (fixture.workspace / "input.txt").write_text(fixture.marker + "\n")
                fixture.command = "cat input.txt"
                initial = fixture.run(model, "Read input.txt and remember its value.", compact=True)
                self.assertEqual(initial.returncode, 0, initial.stderr[-2000:])
                self.assertTrue(fixture.compacted, "Local compaction request never reached native Responses")
                self.assertIn(fixture.marker, final_answer(json_records(initial.stdout)))
                self.assertIn(fixture.marker, str(received_tool_result(fixture.compaction_input)))
                records = [json.loads(line) for line in initial.stdout.splitlines() if line.startswith("{")]
                thread = next(record["thread_id"] for record in records if record.get("type") == "thread.started")
                before = len(fixture.upstream_requests)
                follow_up = fixture.run(model, "What was the previous tool result?", resume=thread)
                self.assertEqual(follow_up.returncode, 0, follow_up.stderr[-2000:])
                self.assertTrue(history_only_answer(follow_up, json_records(follow_up.stdout), fixture.marker))
                replay = fixture.upstream_requests[before]
                self.assertIn(fixture.marker, json.dumps(replay["input"]))
                self.assertTrue(any(item.get("type") == "function_call_output" and fixture.marker in str(item)
                                    for body in fixture.upstream_requests for item in body["input"]))
                compacted = fixture.run(model, "Continue using the remembered value.", resume=thread, compact=True)
                self.assertEqual(compacted.returncode, 0, compacted.stderr[-2000:])
                self.assertTrue(history_only_answer(compacted, json_records(compacted.stdout), fixture.marker))
                self.assertTrue(all(body.get("store") is False and not body.get("previous_response_id")
                                    for body in fixture.upstream_requests))

    def test_failed_incomplete_truncated_tool_streams_fail_without_side_effects(self):
        for model in MODELS:
            for failure in ("failed", "incomplete", "truncated"):
                with self.subTest(model=model, failure=failure), OfflineFixture(self.binary, self.source, self.version) as fixture:
                    fixture.failure = failure
                    result = fixture.run(model, "Create MUST_NOT_EXIST then report success.")
                    self.assertNotEqual(result.returncode, 0, "Partial tool call incorrectly reported a successful turn")
                    self.assertFalse((fixture.workspace / "MUST_NOT_EXIST").exists())
                    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                    self.assertFalse(any(record.get("type") == "turn.completed" for record in records))
                    self.assertEqual(len(fixture.upstream_requests), 1, "Failed inference replayed")
                    fixture.wait_closed(1)
                    fixture.failure = None
                    following = fixture.run(model, "Reply OFFLINE_OK.")
                    self.assertEqual(following.returncode, 0, following.stderr[-2000:])
                    self.assertIn("OFFLINE_OK", following.stdout)

    def test_cancelled_codex_turn_releases_upstream_and_proxy_survives(self):
        for model in MODELS:
            with self.subTest(model=model), OfflineFixture(self.binary, self.source, self.version) as fixture:
                fixture.failure = "stall"
                process = subprocess.Popen([self.binary, "exec", "--json", "--skip-git-repo-check",
                    "-C", str(fixture.workspace), "-m", model, "Wait for a tool call."],
                    env=fixture.env, cwd=fixture.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True)
                try:
                    self.assertTrue(fixture.started.wait(10), "Codex never started native inference")
                    os.killpg(process.pid, signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertNotIn('"type":"turn.completed"', stdout)
                    fixture.wait_closed(1)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                fixture.failure = None
                following = fixture.run(model, "Reply OFFLINE_OK.")
                self.assertEqual(following.returncode, 0, following.stderr[-2000:])
                self.assertIn("OFFLINE_OK", following.stdout)

    def test_command_environment_excludes_client_and_sap_secrets(self):
        with OfflineFixture(self.binary, self.source, self.version) as fixture:
            fixture.env["AICORE_SERVICE_KEY"] = "synthetic-service-key-must-not-leak"
            fixture.marker = "SECRET_ENV_ABSENT"
            fixture.command = 'if [ -z "$SAP_AI_PROXY_API_KEY" ] && [ -z "$AICORE_SERVICE_KEY" ]; then printf SECRET_ENV_ABSENT; else printf SECRET_ENV_LEAK; fi'
            result = fixture.run(MODELS[0], "Check the synthetic command environment.")
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertIn("SECRET_ENV_ABSENT", final_answer(json_records(result.stdout)))
            for body in fixture.upstream_requests:
                self.assertNotIn(TOKEN, json.dumps(body))
                self.assertNotIn("synthetic-service-key-must-not-leak", json.dumps(body))

    def test_interactive_no_daemon_loads_profile_and_answers(self):
        for model in MODELS:
            with self.subTest(model=model), OfflineFixture(self.binary, self.source, self.version) as fixture:
                passed = interactive_acceptance(self.binary, fixture.env, fixture.workspace, model,
                                                "Reply OFFLINE_OK. Do not use tools.", "OFFLINE_OK")
                self.assertTrue(passed, "Interactive --no-daemon did not produce the expected answer")
                self.assertTrue(fixture.upstream_requests)

    def test_interactive_namespace_function_executes_and_replays_result(self):
        for model in MODELS:
            with self.subTest(model=model), OfflineFixture(self.binary, self.source, self.version) as fixture:
                def events(body):
                    results = [item for item in body.get("input", []) if item.get("type") == "function_call_output"
                               and item.get("call_id") == "call_command"]
                    if results:
                        output = str(results[0]["output"])
                        return text_events(model, "NAMESPACE_RESULT_OK" if '"data":[]' in output.replace(" ", "") else "NAMESPACE_RESULT_INVALID")
                    tools = body.get("tools", [])
                    # Select the actual alias for list_worktrees by matching the known original identity.
                    alias = proxy._NativeToolAdapter({"input": [], "tools": []}).identity("mcp__codex_worktrees", "list_worktrees")
                    if not any(tool.get("name") == alias for tool in tools):
                        return text_events(model, "OFFLINE_OK")
                    result = command_events(model, "unused")
                    for event in result:
                        item = event.get("item")
                        if item:
                            item["name"] = alias
                            item["arguments"] = "{}" if event["type"] == "response.output_item.done" else ""
                        if event["type"] == "response.function_call_arguments.delta":
                            event["delta"] = "{}"
                        if event["type"] == "response.function_call_arguments.done":
                            event["arguments"] = "{}"
                        if event.get("response", {}).get("output"):
                            event["response"]["output"][0].update(name=alias, arguments="{}")
                    return result
                fixture.events = events
                passed = interactive_acceptance(self.binary, fixture.env, fixture.workspace, model,
                    "Use the worktree listing tool, then report NAMESPACE_RESULT_OK from its result.", "NAMESPACE_RESULT_OK")
                self.assertTrue(passed, "Interactive namespace result did not reach a completed answer")
                continuation = fixture.upstream_requests[-1]["input"]
                self.assertTrue(any(item.get("type") == "function_call_output" and item.get("call_id") == "call_command" for item in continuation))
                self.assertTrue(any(item.get("type") == "function_call" and item.get("call_id") == "call_command"
                                    and "namespace" not in item for item in continuation))


def interactive_completed(home, marker):
    for path in Path(home).rglob("*.jsonl"):
        for record in json_records(path.read_text()):
            payload = record.get("payload", {})
            if record.get("type") == "event_msg" and payload.get("type") == "task_complete" \
                    and marker in (payload.get("last_agent_message") or ""):
                return True
    return False


def interactive_acceptance(binary, env, workspace, model, prompt, marker):
    """Verify interactive startup under a PTY without retaining its transcript."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    process = subprocess.Popen([binary, "--no-daemon", "--no-alt-screen", "-C", str(workspace),
                                "-m", model, "-s", "workspace-write", "-a", "never", prompt],
                               env=dict(env, TERM="xterm-256color"), cwd=workspace,
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    pending = b""
    passed = False
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline and process.poll() is None:
            readable, _, _ = select.select([master], [], [], 0.2)
            if not readable:
                continue
            try:
                pending = (pending + os.read(master, 65536))[-131072:]
            except OSError:
                break
            # Terminal interrogation must be answered for the TUI to initialize.
            if b"\x1b[6n" in pending:
                os.write(master, b"\x1b[1;1R")
                pending = pending.replace(b"\x1b[6n", b"")
            for query, reply in ((b"\x1b[c", b"\x1b[?1;2c"), (b"\x1b[?u", b"\x1b[?0u")):
                if query in pending:
                    os.write(master, reply)
                    pending = pending.replace(query, b"")
            text = pending.decode("utf-8", errors="replace")
            if 'state=error:app=codex' in text or '"invalid_request_error"' in text:
                break
            if "trust" in text.lower() and ("folder" in text.lower() or "directory" in text.lower()):
                os.write(master, b"\r")
                pending = b""
            if interactive_completed(env["CODEX_HOME"], marker):
                passed = True
                break
        return passed
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)


class LiveProtocolEvidence:
    """Retain capability facts and opaque hashes, never native transcripts."""
    def __init__(self):
        self.high_requested = False
        self.reasoning_tokens = 0
        self.returned_hashes = set()
        self.replayed_hashes = set()
        self.active_upstream = threading.Event()
        self.upstream_closed = threading.Event()
        self.last_outcome = None
        self.requests = 0
        self.records = {}
        self.session_hashes = {}
        self.lock = threading.Lock()

    @staticmethod
    def reasoning_hashes(items):
        return {hashlib.sha256(item["encrypted_content"].encode()).hexdigest()
                for item in items if isinstance(item, dict) and item.get("type") == "reasoning"
                and isinstance(item.get("encrypted_content"), str) and item["encrypted_content"]}

    def request(self, body, request_id):
        with self.lock:
            self.requests += 1
            self.high_requested |= body.get("reasoning", {}).get("effort") == "high"
            items = body.get("input", [])
            incoming = self.reasoning_hashes(items if isinstance(items, list) else [])
            session = body.get("prompt_cache_key")
            self.records[request_id] = {"session": session,
                "replay": bool(session and incoming & self.session_hashes.get(session, set())),
                "high": body.get("reasoning", {}).get("effort") == "high", "reasoning_tokens": 0,
                "returned": set(), "streamed": set(), "outcome": None, "opened": False,
                "cancel_markers": set(re.findall(r"CANCEL_[a-f0-9]+", json.dumps(items)))}

    def event(self, event, request_id):
        if event.get("type") == "response.output_item.done":
            with self.lock:
                returned = self.reasoning_hashes([event.get("item", {})])
                self.returned_hashes.update(returned)
                if request_id in self.records:
                    self.records[request_id]["streamed"].update(returned)
            return
        response = event.get("response", {})
        with self.lock:
            returned = self.reasoning_hashes(response.get("output", []))
            self.returned_hashes.update(returned)
            usage = response.get("usage") or {}
            tokens = (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
            self.reasoning_tokens += tokens
            if request_id in self.records:
                self.records[request_id]["returned"] = returned
                self.records[request_id]["reasoning_tokens"] = tokens

    def opened(self, request_id):
        self.records[request_id]["opened"] = True
        self.upstream_closed.clear()
        self.active_upstream.set()

    def closed(self, outcome, request_id):
        self.last_outcome = outcome
        if request_id in self.records:
            record = self.records[request_id]
            record["outcome"] = outcome
            if outcome == "completed" and record["session"]:
                if record["high"] and record["reasoning_tokens"] > 0:
                    self.session_hashes.setdefault(record["session"], set()).update(record["streamed"] or record["returned"])
                if record["replay"]:
                    self.replayed_hashes.add(request_id)
        if not any(record["opened"] and record["outcome"] is None for record in self.records.values()):
            self.active_upstream.clear()
        self.upstream_closed.set()

    def active_request(self, marker):
        return next((key for key, record in self.records.items() if marker in record["cancel_markers"]
                     and record["opened"] and record["outcome"] is None), None)

    def request_closed(self, request_id):
        return self.records.get(request_id, {}).get("outcome") is not None

    def high_effort_history_verified(self):
        return bool(self.replayed_hashes)

    def public_summary(self):
        return {"high_effort_requested": self.high_requested, "reasoning_tokens": self.reasoning_tokens,
                "opaque_history_replayed": self.high_effort_history_verified(),
                "returned_opaque_items": len(self.returned_hashes),
                "requests": self.requests, "last_outcome": self.last_outcome}


@contextlib.contextmanager
def instrumented_live_proxy(config_path, proxy_url):
    """Opt-in test serving stack with fact-only request/ownership instrumentation."""
    parsed = urlsplit(proxy_url)
    if parsed.hostname != "127.0.0.1":
        raise ValueError("Instrumented acceptance requires a loopback proxy URL")
    cfg = proxy.load_config(config_path)
    if not isinstance(cfg, proxy.ProxyConfig):
        raise ValueError("Instrumented live acceptance requires the configured subAccounts format")
    cfg.initialize()
    evidence = {model: LiveProtocolEvidence() for model in MODELS}
    original_prepare = proxy.prepare_native_responses
    original_record = proxy._native_record_result
    original_stream = proxy.generate_native_responses_streaming
    native_session = proxy.create_native_http_session()
    original_post = native_session.post
    scope = threading.local()

    original_route = proxy._proxy_native_responses

    def route(body, model, request_id):
        scope.request_id = request_id
        return original_route(body, model, request_id)

    def prepare(body, headers, *args):
        payload = original_prepare(body, headers, *args)
        scope.model = payload["model"]
        if scope.model in evidence:
            evidence[scope.model].request(payload, scope.request_id)
        return payload

    def post(*args, **kwargs):
        response = original_post(*args, **kwargs)
        model = getattr(scope, "model", None)
        if model in evidence and response.status_code == 200:
            evidence[model].opened(scope.request_id)
        return response

    def record(request_id, model, account, agent, body, streaming):
        if model in evidence:
            evidence[model].event({"response": body}, request_id)
            evidence[model].closed(body.get("status", "failed"), request_id)
        return original_record(request_id, model, account, agent, body, streaming)

    def stream(owner, request_id=None, model=None, *args, **kwargs):
        for frame in original_stream(owner, request_id, model, *args, **kwargs):
            if model in evidence:
                lines = [line[5:].lstrip(" ") for line in re.split(r"\r\n|\r|\n", frame) if line.startswith("data:")]
                if lines:
                    event = json.loads("\n".join(lines))
                    if event.get("type") == "response.output_item.done":
                        evidence[model].event(event, request_id)
            yield frame

    with contextlib.ExitStack() as stack:
        stack.callback(native_session.close)
        stack.enter_context(patch.object(proxy, "proxy_config", cfg))
        stack.enter_context(patch.object(proxy, "prepare_native_responses", prepare))
        stack.enter_context(patch.object(proxy, "_proxy_native_responses", route))
        stack.enter_context(patch.object(proxy, "_native_record_result", record))
        stack.enter_context(patch.object(proxy, "generate_native_responses_streaming", stream))
        stack.enter_context(patch.object(proxy, "_native_http_session", native_session))
        stack.enter_context(patch.object(proxy, "_log_requests_enabled", False))
        native_session.post = post
        server = make_server(parsed.hostname, parsed.port or 3001, proxy.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        yield evidence


def live_acceptance_exit_code(report):
    """Require complete acceptance for both models; provenance is informational."""
    models = report.get("models", {})
    return 0 if all(models.get(model, {}).get(check) == "pass"
                    for model in MODELS for check in LIVE_REQUIRED_CHECKS) else 1


def live_acceptance(binary, proxy_url, protocol_evidence=None):
    """Run synthetic coding turns; emit only version/capability outcomes."""
    source, version = installed_catalog(binary)
    catalog = build_catalog(source, dict.fromkeys(MODELS, 272000), version, dict.fromkeys(MODELS, 872000))
    report = {"mode": "live", "codex_version": version, "catalog": catalog["provenance"], "models": {}}
    for model in MODELS:
        outcomes = {}
        report["models"][model] = outcomes
        with tempfile.TemporaryDirectory(prefix="codex-proxy-live-synthetic-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            marker = "SYNTHETIC_LIVE_" + os.urandom(8).hex()
            (workspace / "input.txt").write_text(marker + "\n")
            catalog_path = root / "models.json"
            catalog_path.write_text(json.dumps(catalog))
            config = (ROOT / "examples/codex/config.toml.example").read_text()
            config = config.replace("/absolute/path/to/sap-ai-core-llm-proxy/examples/codex/models.json", str(catalog_path))
            config = config.replace("http://127.0.0.1:3001/v1", proxy_url.rstrip("/"))
            (home / "config.toml").write_text(config)
            env = dict(os.environ, CODEX_HOME=str(home))

            def run(prompt, thread=None, compact=False):
                cmd = [binary, "exec", "--strict-config", "--json", "--skip-git-repo-check",
                       "-s", "workspace-write", "-C", str(workspace), "-m", model,
                       "-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="high"']
                if compact:
                    cmd += ["-c", "model_auto_compact_token_limit=1000"]
                if thread:
                    cmd += ["resume", thread]
                completed = subprocess.run(cmd + [prompt], env=env, cwd=workspace,
                                           capture_output=True, text=True, timeout=180)
                records = []
                for line in completed.stdout.splitlines():
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass
                return completed, records

            try:
                initial, records = run("Read input.txt using a local command, copy its exact contents into edited.txt, "
                                       "read edited.txt to verify, then report the value. Work only in this synthetic workspace.")
                edited = workspace / "edited.txt"
                executed = bool(successful_commands(records))
                success = initial.returncode == 0 and marker in final_answer(records)
                outcomes["streaming"] = "pass" if success else "failed"
                outcomes["command_execution"] = "pass" if success and executed else "failed"
                outcomes["file_edit"] = "pass" if edited.exists() and edited.read_text() == marker + "\n" else "failed"
                outcomes["tool_result_continuation"] = "pass" if success and executed else "failed"
                outcomes["high_effort_history"] = "blocked: high requested; reasoning usage/history replay requires protocol evidence"
                if protocol_evidence and success:
                    reasoning, _ = run("Compute 123456789123456789 multiplied by 987654321987654321 modulo 1000000007. Check modular arithmetic carefully without tools.")
                    arithmetic_thread = next((record.get("thread_id") for record in json_records(reasoning.stdout)
                                              if record.get("type") == "thread.started"), None)
                    if reasoning.returncode == 0 and arithmetic_thread:
                        replayed, _ = run("Now compute that same product modulo 1000000009; retain your earlier reasoning history. Do not use tools.", arithmetic_thread)
                        outcomes["high_effort_history"] = "pass" if replayed.returncode == 0 and protocol_evidence[model].high_effort_history_verified() else "blocked: reasoning usage or replay missing"
                thread = next((record.get("thread_id") for record in records if record.get("type") == "thread.started"), None)
                if thread and success:
                    follow, follow_records = run("Without rereading files, what was the previous tool result?", thread)
                    outcomes["saved_resume_follow_up"] = "pass" if history_only_answer(follow, follow_records, marker) else "failed"
                    if protocol_evidence and protocol_evidence[model].high_effort_history_verified():
                        outcomes["high_effort_history"] = "pass"
                    compacted, _ = run("Run a local command to read edited.txt once, then retain its value for a subsequent turn.", thread, compact=True)
                    # Inspect only synthetic rollout files locally; never print their content.
                    compaction_observed = any('"type":"compacted"' in path.read_text().replace(" ", "")
                                              or '"type":"context_compaction"' in path.read_text().replace(" ", "")
                                              for path in home.rglob("*.jsonl"))
                    next_turn, next_records = run("Without rereading files, report the retained value.", thread)
                    outcomes["local_compaction"] = "pass" if (compaction_observed and compacted.returncode == 0
                        and history_only_answer(next_turn, next_records, marker)) else "blocked: compaction or usable continuation not established"
                else:
                    outcomes["saved_resume_follow_up"] = "blocked: initial coding turn failed"
                    outcomes["local_compaction"] = "blocked: initial coding turn failed"
            except (subprocess.SubprocessError, OSError, ValueError) as error:
                outcomes["coding_workflow"] = "blocked: " + type(error).__name__

            cancel_marker = "CANCEL_" + os.urandom(8).hex()
            process = subprocess.Popen([binary, "exec", "--json", "--skip-git-repo-check", "-C", str(workspace),
                "-m", model, cancel_marker + ": Compute and explain a long synthetic arithmetic derivation. Do not use tools."],
                env=env, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                target_request = None
                deadline = time.monotonic() + 30
                while protocol_evidence and time.monotonic() < deadline and process.poll() is None:
                    target_request = protocol_evidence[model].active_request(cancel_marker)
                    if target_request:
                        break
                    threading.Event().wait(0.02)
                if target_request and process.poll() is None:
                    os.killpg(process.pid, signal.SIGINT)
                    stdout, _ = process.communicate(timeout=15)
                    records = json_records(stdout)
                    cancelled = process.returncode != 0 and not any(record.get("type") == "turn.completed" for record in records)
                    outcomes["cancellation"] = "pass" if cancelled else "failed"
                    deadline = time.monotonic() + 305
                    while not protocol_evidence[model].request_closed(target_request) and time.monotonic() < deadline:
                        threading.Event().wait(0.05)
                    closed = protocol_evidence[model].request_closed(target_request)
                    outcome = protocol_evidence[model].records[target_request]["outcome"]
                    outcomes["live_upstream_disconnect_cleanup"] = "pass" if closed and outcome != "completed" else "blocked: closure not observed or upstream completed before disconnect detection"
                else:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                    process.communicate(timeout=15)
                    outcomes["cancellation"] = "blocked: active native inference not observed"
                check, _ = run("Reply with hello. Do not use tools.")
                outcomes["independent_proxy_lifetime"] = "pass" if check.returncode == 0 else "failed"
            except (subprocess.SubprocessError, OSError, ValueError) as error:
                outcomes["cancellation"] = "blocked: " + type(error).__name__
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            outcomes.setdefault("live_upstream_disconnect_cleanup", "blocked: requires upstream ownership instrumentation")
            if protocol_evidence:
                outcomes["protocol_evidence"] = protocol_evidence[model].public_summary()
            outcomes["interactive_no_daemon"] = "pass" if interactive_acceptance(binary, env, workspace, model,
                "Reply LIVE_INTERACTIVE_OK. Do not use tools.", "LIVE_INTERACTIVE_OK") else "blocked: interactive turn not completed"
            outcomes["sap_context_capacity"] = "unverified: operator-selected catalog values"
    print(json.dumps(report, indent=2))
    return live_acceptance_exit_code(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true", help="Explicitly use the operator's running proxy and SAP deployments")
    parser.add_argument("--proxy-url", help="Operator's native proxy /v1 base URL, required in live mode")
    parser.add_argument("--instrument-proxy-config", help="Start a fact-only instrumented loopback test proxy from operator config; port must be free")
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    if args.live:
        if not args.proxy_url:
            parser.error("--live requires --proxy-url")
        parsed = urlsplit(args.proxy_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            parser.error("--proxy-url must be a plain HTTP(S) base URL without credentials/query/fragment")
        if not os.environ.get("SAP_AI_PROXY_API_KEY"):
            parser.exit(2, "BLOCKED: live mode requires operator SAP_AI_PROXY_API_KEY\n")
    if not shutil.which(args.codex):
        parser.exit(2, "BLOCKED: selected Codex binary unavailable\n")
    logging.disable(logging.CRITICAL)
    if args.live:
        try:
            if args.instrument_proxy_config:
                with instrumented_live_proxy(args.instrument_proxy_config, args.proxy_url) as evidence:
                    return live_acceptance(args.codex, args.proxy_url, evidence)
            return live_acceptance(args.codex, args.proxy_url)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(2, "BLOCKED: live setup failed (" + type(error).__name__ + ")\n")
    ActualCodexOfflineTests.binary = args.codex
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ActualCodexOfflineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"mode": "offline", "codex_version": ActualCodexOfflineTests.version,
                      "outcome": "pass" if result.wasSuccessful() else "blocked"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
