"""Native Responses contracts; only SAP HTTP I/O is replaced."""

import copy
import json
import requests
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import proxy_server as proxy
from tests.test_routes import TOKEN, _make_config, _fake_post


def native_body(status="completed"):
    return {"id": "resp_backend", "object": "response", "status": status,
            "output": [], "usage": {"input_tokens": 2, "output_tokens": 3,
                                      "total_tokens": 5}}


def invalid_terminal_bodies(status):
    for key, wrong_values in (("object", (None, [], "other")), ("id", (None, [], 7)),
                              ("output", (None, {}, "text")), ("status", (None, [], {}, "in_progress"))):
        missing = native_body(status)
        del missing[key]
        yield key + " missing", missing
        for value in wrong_values:
            yield f"{key}={value!r}", {**native_body(status), key: value}


class NativeRouteTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _make_config()
        self.cfg.subaccounts["S"].normalized_models.update({
            "gpt-6-astra": ["https://backend/astra"],
            "gpt-6.1-sol": ["https://backend/sol"],
        })
        other = proxy.SubAccountConfig("T", "rg-other", "unused.json", {})
        other.service_key = proxy.ServiceKey("id-T", "secret-T", "https://auth-T", "zone-T")
        other.normalized_models = {"gpt-6-astra": ["https://other/astra"]}
        self.cfg.subaccounts["T"] = other
        self.cfg.build_model_mapping()
        proxy.load_balance_url.counters = {}
        self.addCleanup(setattr, proxy.load_balance_url, "counters", {})
        self.config_patch = patch.object(proxy, "proxy_config", self.cfg)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.token_patch = patch.object(proxy, "fetch_token", side_effect=lambda name: "token-" + name)
        self.token = self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.session_patch = patch.object(proxy, "_http_session")
        self.session = self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.session.post.return_value = _fake_post(native_body())
        self.native_patch = patch.object(proxy, "_native_http_session", create=True)
        self.native = self.native_patch.start()
        self.addCleanup(self.native_patch.stop)
        self.native.post.return_value = _fake_post(native_body())
        self.client = proxy.app.test_client()

    def post(self, body=None, headers=None, **kwargs):
        return self.client.post("/v1/responses", json=body or {"model": "gpt-6-astra", "input": "hi"},
                                headers=headers or {"Authorization": "Bearer " + TOKEN}, **kwargs)

    def reset_upstream_calls(self):
        self.native.reset_mock()
        self.session.reset_mock()
        self.token.reset_mock()

    def assert_no_upstream_calls(self):
        self.native.post.assert_not_called()
        self.session.assert_not_called()
        self.assertEqual(self.session.mock_calls, [])
        self.token.assert_not_called()

    def test_exact_models_use_native_url_and_selected_subaccount(self):
        for model, base, account, group, zone in [
            ("gpt-6-astra", "https://backend/astra", "S", "rg", "zone"),
            ("gpt-6-astra", "https://other/astra", "T", "rg-other", "zone-T"),
            ("gpt-6.1-sol", "https://backend/sol", "S", "rg", "zone"),
        ]:
            with self.subTest(model=model, account=account):
                result = self.post({"model": model, "input": "hi"})
                self.assertEqual(result.status_code, 200)
                sent = self.native.post.call_args
                self.assertEqual(sent.args[0], base + "/responses?api-version=2025-03-01-preview")
                self.assertEqual(sent.kwargs["headers"], {
                    "AI-Resource-Group": group, "Content-Type": "application/json",
                    "Authorization": "Bearer token-" + account, "AI-Tenant-Id": zone,
                })
                self.assertEqual(sent.kwargs["json"]["input"], "hi")
                self.assertNotIn("messages", sent.kwargs["json"])

    def test_missing_target_never_uses_available_fallback(self):
        del self.cfg.subaccounts["S"].normalized_models["gpt-6.1-sol"]
        self.cfg.build_model_mapping()
        self.reset_upstream_calls()
        result = self.post({"model": "gpt-6.1-sol", "input": "hi"})
        self.assertEqual(result.status_code, 404)
        self.assertIn("gpt-6.1-sol", result.get_json()["error"]["message"])
        self.assert_no_upstream_calls()

    def test_empty_deployment_is_configuration_error(self):
        self.cfg.subaccounts["S"].normalized_models["gpt-6.1-sol"] = []
        self.reset_upstream_calls()
        result = self.post({"model": "gpt-6.1-sol", "input": "hi"})
        self.assertEqual(result.status_code, 500)
        self.assert_no_upstream_calls()

    def test_other_responses_models_still_use_chat_conversion(self):
        self.session.post.return_value = _fake_post({"choices": [{"message": {"content": "hi"}}]})
        result = self.post({"model": "gpt-5.4", "input": "hi"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.session.post.call_args.args[0],
                         "https://backend/gpt/chat/completions?api-version=2023-05-15")
        self.assertEqual(result.get_json()["output_text"], "hi")

    def test_native_request_preserves_history_and_schemas_without_mutation(self):
        body = {"model": "gpt-6-astra", "instructions": "code", "input": [
            {"type": "message", "role": "assistant", "id": "msg_orig", "phase": "commentary",
             "content": [{"type": "output_text", "text": "checking", "annotations": []}]},
            {"type": "reasoning", "id": "rs_orig", "summary": [], "encrypted_content": "opaque"},
            {"type": "function_call", "id": "fc_orig", "call_id": "call_orig", "name": "run", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_orig", "output": "value"}],
            "tools": [{"type": "function", "name": "run", "strict": True,
                       "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}],
            "tool_choice": {"type": "function", "name": "run"}, "parallel_tool_calls": False,
            "reasoning": {"effort": "high", "summary": "auto"},
            "include": ["reasoning.encrypted_content"], "max_output_tokens": 77,
            "text": {"verbosity": "low", "format": {"type": "text"}},
            "client_metadata": {"session_id": "synthetic"}, "prompt_cache_key": "synthetic",
            "background": False, "previous_response_id": None}
        original = copy.deepcopy(body)
        result = self.post(body)
        self.assertEqual(result.status_code, 200)
        sent = self.native.post.call_args.kwargs["json"]
        expected = copy.deepcopy(original)
        del expected["background"], expected["previous_response_id"]
        expected.update(stream=False, store=False)
        self.assertEqual(sent, expected)
        self.assertEqual(body, original)

    def test_unsupported_request_semantics_rejected_before_inference(self):
        cases = [
            ({"store": True}, "store"), ({"store": 0}, "store"),
            ({"background": True}, "background"), ({"previous_response_id": "resp_previous"}, "previous_response_id"),
            ({"stream": "true"}, "stream"), ({"input": None}, "input"),
            ({"input": ["hi"]}, "input[0]"), ({"input": [{"type": "custom_tool_call"}]}, "input[0].type"),
            ({"additional_tools": []}, "additional_tools"), ({"unknown_option": True}, "unknown_option"),
            ({"tools": [{"type": "custom", "name": "patch"}]}, "tools[0].type"),
            ({"tools": [{"type": "namespace", "name": "functions"}]}, "tools[0].type"),
            ({"tools": [{"type": "web_search"}]}, "tools[0].type"),
            ({"tool_choice": {"type": "custom", "name": "patch"}}, "tool_choice"),
            ({"reasoning": {"effort": "low", "context": "override"}}, "reasoning.context"),
        ]
        for extra, param in cases:
            with self.subTest(param=param, extra=extra):
                self.reset_upstream_calls()
                result = self.post({"model": "gpt-6-astra", "input": "hi", **extra})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.get_json()["error"]["param"], param)
                self.assert_no_upstream_calls()

    def test_lite_header_is_rejected_with_catalog_diagnostic(self):
        self.reset_upstream_calls()
        result = self.post(headers={"Authorization": "Bearer " + TOKEN, "OpenAI-Beta": "responses-lite=true"})
        self.assertEqual(result.status_code, 400)
        self.assertIn("catalog", result.get_json()["error"]["message"])
        self.assert_no_upstream_calls()

    def test_rejection_guard_detects_injected_native_inference(self):
        original_prepare = proxy.prepare_native_responses

        def injected_prepare(*args, **kwargs):
            proxy._native_http_session.post("https://synthetic-inference.invalid")
            return original_prepare(*args, **kwargs)

        # Run the actual rejection test under an in-memory mutation. It must fail
        # specifically because inference occurred, even though the route rejects.
        case = NativeRouteTests("test_lite_header_is_rejected_with_catalog_diagnostic")
        result = unittest.TestResult()
        with patch.object(proxy, "prepare_native_responses", side_effect=injected_prepare):
            case.run(result)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("Expected 'post' to not have been called", result.failures[0][1])

    def test_namespace_rejection_guard_detects_credential_and_transport_calls(self):
        from tests.test_native_namespaces import NamespaceRequestTests

        original_reject = proxy._NativeToolAdapter.reject
        for target in ("native", "legacy", "credentials"):
            with self.subTest(target=target):
                def injected_reject(*args):
                    if target == "credentials":
                        proxy.fetch_token("S")
                    else:
                        session = proxy._native_http_session if target == "native" else proxy._http_session
                        session.post("https://synthetic-inference.invalid")
                    return original_reject(*args)

                case = NamespaceRequestTests("test_malformed_duplicate_and_nested_namespace_reject_before_inference")
                result = unittest.TestResult()
                with patch.object(proxy._NativeToolAdapter, "reject", side_effect=injected_reject):
                    case.run(result)
                self.assertFalse(result.errors)
                self.assertEqual(len(result.failures), 5)

    def test_non_object_json_returns_structured_error(self):
        for raw in ('[{"model":"gpt-6-astra"}]', 'null', '"text"', '7', '{broken'):
            with self.subTest(raw=raw):
                self.reset_upstream_calls()
                result = self.client.post("/v1/responses", data=raw, content_type="application/json",
                                          headers={"Authorization": "Bearer " + TOKEN})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.get_json()["error"]["type"], "invalid_request_error")
                self.assert_no_upstream_calls()

    def test_invalid_client_token_cannot_acquire_upstream_credentials(self):
        self.reset_upstream_calls()
        result = self.post(headers={"Authorization": "Bearer invalid"})
        self.assertEqual(result.status_code, 401)
        self.assert_no_upstream_calls()

    def test_operator_headers_ignore_inbound_overrides(self):
        result = self.post(headers={"x-api-key": TOKEN, "AI-Resource-Group": "attacker",
                                    "AI-Tenant-Id": "attacker", "X-Deployment-Url": "https://attacker"})
        self.assertEqual(result.status_code, 200)
        sent = self.native.post.call_args
        self.assertEqual(sent.kwargs["headers"]["AI-Resource-Group"], "rg")
        self.assertEqual(sent.kwargs["headers"]["Authorization"], "Bearer token-S")
        self.assertEqual(sent.args[0], "https://backend/astra/responses?api-version=2025-03-01-preview")
        for field in ("extra_headers", "deployment_url", "resource_group", "api_key"):
            with self.subTest(field=field):
                self.reset_upstream_calls()
                result = self.post({"model": "gpt-6-astra", "input": "hi", field: "attacker"})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.get_json()["error"]["param"], field)
                self.assert_no_upstream_calls()

    def test_real_token_cache_refresh_stays_with_selected_subaccount(self):
        self.token_patch.stop()
        self.cfg.subaccounts["S"].token_info.token = "cached-S"
        self.cfg.subaccounts["S"].token_info.expiry = float("inf")
        captured = []
        def upstream(url, **kwargs):
            if url.startswith("https://auth-T/"):
                return _fake_post({"access_token": "fresh-T", "expires_in": 3600})
            captured.append((url, kwargs["headers"]))
            return _fake_post(native_body())
        self.session.post.side_effect = upstream
        self.native.post.side_effect = upstream
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual([headers["Authorization"] for _, headers in captured],
                         ["Bearer cached-S", "Bearer fresh-T"])
        self.assertEqual(self.cfg.subaccounts["S"].token_info.token, "cached-S")
        self.assertEqual(self.cfg.subaccounts["T"].token_info.token, "fresh-T")

    def test_native_transport_has_one_attempt_finite_timeout_and_no_redirects(self):
        for error, status in [(requests.exceptions.ConnectTimeout("private-url"), 504),
                              (requests.exceptions.ReadTimeout("private-url"), 504),
                              (requests.exceptions.ConnectionError("private-url"), 502)]:
            with self.subTest(error=type(error).__name__):
                self.native.post.reset_mock()
                self.native.post.side_effect = error
                result = self.post()
                self.assertEqual(result.status_code, status)
                self.assertNotIn("private-url", result.get_data(as_text=True))
                self.assertEqual(self.native.post.call_count, 1)
                self.assertEqual(self.native.post.call_args.kwargs["timeout"], (15, 300))
                self.assertFalse(self.native.post.call_args.kwargs["allow_redirects"])
                self.session.post.assert_not_called()

    def test_native_session_disables_retries_independently_of_oauth(self):
        session = proxy.create_native_http_session()
        self.addCleanup(session.close)
        self.assertEqual(session.get_adapter("https://synthetic").max_retries.total, 0)
        control = proxy.create_http_session()
        self.addCleanup(control.close)
        self.assertEqual(control.get_adapter("https://synthetic").max_retries.total, 3)

    def test_native_json_retains_terminal_fields_and_closes(self):
        for status in ("completed", "incomplete", "failed"):
            with self.subTest(status=status):
                body = native_body(status)
                body.update(error=None if status != "failed" else {"code": "model_error", "message": "synthetic"},
                            incomplete_details={"reason": "max_output_tokens"} if status == "incomplete" else None)
                body["output"] = [
                    {"type": "reasoning", "id": "rs_native", "encrypted_content": "opaque", "summary": []},
                    {"type": "function_call", "id": "fc_native", "call_id": "call_native", "name": "run",
                     "arguments": "{}", "status": "completed"}]
                body["usage"]["output_tokens_details"] = {"reasoning_tokens": 1}
                upstream = _fake_post(body)
                self.native.post.return_value = upstream
                result = self.post()
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.get_json(), body)
                upstream.close.assert_called_once()

    def test_invalid_native_json_is_failure_and_always_closes(self):
        for value in ([], {"choices": []}, None):
            with self.subTest(value=value):
                upstream = _fake_post(value)
                self.native.post.return_value = upstream
                self.assertEqual(self.post().status_code, 502)
                upstream.close.assert_called_once()
        upstream = _fake_post(None)
        upstream.json.side_effect = ValueError("private-url")
        self.native.post.return_value = upstream
        result = self.post()
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("private-url", result.get_data(as_text=True))
        upstream.close.assert_called_once()

    def test_native_stream_failed_and_incomplete_are_terminal_without_completion(self):
        for status in ("failed", "incomplete"):
            event = {"type": "response." + status, "sequence_number": 3, "response": native_body(status)}
            frame = f"event: response.{status}\ndata: {json.dumps(event)}\n\n"
            upstream = _fake_post(None)
            def chunks():
                yield frame.encode()
                raise AssertionError("Must stop reading after terminal event")
            upstream.iter_content.side_effect = lambda **kwargs: chunks()
            self.native.post.return_value = upstream
            result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
            self.assertEqual(result.get_data(as_text=True), frame)
            self.assertNotIn("response.completed", result.get_data(as_text=True))
            result.close()
            upstream.close.assert_called_once()

    def test_native_partial_tool_eof_timeout_or_malformed_event_never_completes(self):
        partial = b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","delta":"{","sequence_number":1}\n\n'
        for ending in (None, requests.exceptions.ReadTimeout("private-url"),
                       b'event: response.completed\ndata: invalid\n\n',
                       b'event: response.completed\ndata: {}\n\n'):
            with self.subTest(ending=ending):
                self.native.post.reset_mock()
                upstream = _fake_post(None)
                def chunks():
                    yield partial
                    if isinstance(ending, Exception):
                        raise ending
                    if isinstance(ending, bytes):
                        yield ending
                upstream.iter_content.side_effect = lambda **kwargs: chunks()
                self.native.post.return_value = upstream
                result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
                delivered = result.get_data(as_text=True)
                self.assertIn("event: error\n", delivered)
                self.assertNotIn("response.completed", delivered)
                self.assertNotIn("response.output_item.done", delivered)
                self.assertNotIn("private-url", delivered)
                self.assertEqual(self.native.post.call_count, 1)
                result.close()
                upstream.close.assert_called_once()

    def test_json_rejects_all_invalid_terminal_envelope_fields(self):
        for label, body in invalid_terminal_bodies("completed"):
            with self.subTest(field=label):
                self.native.post.reset_mock()
                upstream = _fake_post(body)
                self.native.post.return_value = upstream
                with patch.object(proxy, "_native_record_result") as record:
                    result = self.post()
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.get_json()["error"]["type"], "server_error")
                record.assert_not_called()
                self.native.post.assert_called_once()
                upstream.close.assert_called_once()

    def test_malformed_terminal_envelopes_after_partial_tools_never_succeed(self):
        partial = 'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","delta":"{","sequence_number":1}\n\n'
        for status in ("completed", "incomplete", "failed"):
            cases = list(invalid_terminal_bodies(status))
            cases += [("response missing", None), ("response array", [])]
            cases += [("status mismatch", native_body(other)) for other in ("completed", "failed", "incomplete") if other != status]
            for label, body in cases:
                with self.subTest(status=status, field=label):
                    self.native.post.reset_mock()
                    event = {"type": "response." + status, "sequence_number": 2}
                    if body is not None:
                        event["response"] = body
                    upstream = _fake_post(None)
                    upstream.iter_content.return_value = iter([
                        partial.encode(), (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()])
                    self.native.post.return_value = upstream
                    with patch.object(proxy, "_native_record_result") as record:
                        result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
                        delivered = result.get_data(as_text=True)
                        result.close()
                    self.assertTrue(delivered.startswith(partial))
                    self.assertIn("event: error\n", delivered)
                    self.assertNotIn("event: response." + status + "\n", delivered)
                    self.assertNotIn("response.output_item.done", delivered)
                    record.assert_called_once()
                    self.assertEqual(record.call_args.args[4]["status"], "failed")
                    self.native.post.assert_called_once()
                    upstream.close.assert_called_once()

    def test_malformed_terminal_tool_identities_fail_before_recording_success(self):
        partial = 'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","delta":"{"}\n\n'
        for item in ({"type": []}, {"type": "function_call", "name": []}):
            for streaming in (False, True):
                with self.subTest(item=item, streaming=streaming):
                    self.native.post.reset_mock()
                    body = {**native_body(), "output": [item]}
                    upstream = _fake_post(body)
                    event = {"type": "response.completed", "response": body}
                    upstream.iter_content.return_value = iter([
                        partial.encode(), (f"event: response.completed\ndata: {json.dumps(event)}\n\n").encode()])
                    self.native.post.return_value = upstream
                    with patch.object(proxy, "_native_record_result") as record:
                        result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": streaming})
                        delivered = result.get_data(as_text=True)
                        result.close()
                    if streaming:
                        self.assertTrue(delivered.startswith(partial))
                        self.assertIn("event: error\n", delivered)
                        self.assertNotIn("event: response.completed\n", delivered)
                        record.assert_called_once()
                        self.assertEqual(record.call_args.args[4]["status"], "failed")
                    else:
                        self.assertEqual(result.status_code, 502)
                        record.assert_not_called()
                    self.native.post.assert_called_once()
                    upstream.close.assert_called_once()

    def test_http_errors_before_stream_headers_are_safe_and_not_replayed(self):
        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                proxy.load_balance_url.counters = {}
                self.native.post.reset_mock()
                upstream = _fake_post({"error": {"type": "invalid_request_error", "code": "unsupported_value",
                    "param": "reasoning.effort", "message": "token-S https://private-endpoint"}}, status)
                upstream.headers = {"Retry-After": "7"}
                self.native.post.return_value = upstream
                result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.mimetype, "application/json")
                self.assertEqual(result.get_json()["error"]["param"], "reasoning.effort")
                self.assertEqual(result.get_json()["error"]["code"], "unsupported_value")
                self.assertNotIn("token-S", result.get_data(as_text=True))
                self.assertNotIn("private-endpoint", result.get_data(as_text=True))
                if status == 429:
                    self.assertEqual(result.headers["Retry-After"], "7")
                self.assertEqual(self.native.post.call_count, 1)
                self.assertTrue(self.native.post.call_args.kwargs["stream"])
                self.session.post.assert_not_called()
                upstream.close.assert_called_once()

    def test_redirect_and_non_json_error_are_structured_failures(self):
        for status, expected in ((302, 502), (403, 403)):
            upstream = _fake_post(None, status)
            upstream.headers = {"Location": "https://private-endpoint"}
            upstream.json.side_effect = ValueError("not JSON")
            self.native.post.return_value = upstream
            result = self.post()
            self.assertEqual(result.status_code, expected)
            self.assertEqual(result.get_json()["error"]["type"], "server_error")
            self.assertNotIn("private-endpoint", result.get_data(as_text=True))
            upstream.close.assert_called_once()

    def test_broken_http_error_bodies_keep_known_status_and_retry_after(self):
        for status in (400, 429, 503):
            for failure in (requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                for streaming in (False, True):
                    with self.subTest(status=status, failure=failure.__name__, streaming=streaming):
                        self.native.post.reset_mock()
                        upstream = _fake_post(None, status)
                        upstream.headers = {"Retry-After": "7"}
                        upstream.json.side_effect = failure("token-S https://private-endpoint")
                        self.native.post.return_value = upstream
                        result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": streaming})
                        self.assertEqual(result.status_code, status)
                        self.assertEqual(result.mimetype, "application/json")
                        self.assertEqual(result.get_json()["error"], {
                            "type": "server_error", "message": f"Native upstream rejected request (HTTP {status})"})
                        self.assertEqual(result.headers.get("Retry-After"), "7" if status in (429, 503) else None)
                        self.native.post.assert_called_once()
                        self.session.post.assert_not_called()
                        upstream.close.assert_called_once()

    def test_native_sse_forwards_original_events_incrementally_across_chunk_splits(self):
        events = [
            {"type": "response.created", "sequence_number": 41, "response": {"id": "resp_original"}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_original", "delta": "é",
             "sequence_number": 42, "future_field": {"value": 7}},
            {"type": "response.output_item.done", "sequence_number": 43,
             "item": {"type": "function_call", "id": "fc_original", "call_id": "call_original",
                      "status": "completed", "arguments": "{}", "name": "run"}},
            {"type": "response.completed", "sequence_number": 44, "response": native_body()},
        ]
        events[-1]["response"]["output"] = [{"type": "reasoning", "encrypted_content": "opaque"}]
        frames = [f"id: original-{index}\r\nevent: {event['type']}\r\ndata: " +
                  json.dumps(event, ensure_ascii=False) + "\r\n\r\n" for index, event in enumerate(events)]
        upstream = _fake_post(None)
        consumed = []
        def chunks():
            for frame in frames:
                raw = frame.encode("utf-8")
                for byte in raw:
                    consumed.append(byte)
                    yield bytes([byte])
        upstream.iter_content.side_effect = lambda **kwargs: chunks()
        self.native.post.return_value = upstream
        result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True}, buffered=False)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, "text/event-stream")
        iterator = iter(result.response)
        first = next(iterator).decode("utf-8")
        self.assertEqual(first, frames[0])
        self.assertLess(len(consumed), sum(len(frame.encode("utf-8")) for frame in frames))
        delivered = first + b"".join(iterator).decode("utf-8")
        self.assertEqual(delivered, "".join(frames))
        self.assertNotIn("[DONE]", delivered)
        result.close()
        upstream.close.assert_called_once()

    def test_native_logging_records_payload_and_actual_terminal_outcomes(self):
        for status in ("completed", "incomplete", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                proxy.RequestFileLogger._dir_cache.clear()
                event = {"type": "response." + status, "response": native_body(status)}
                self.native.post.return_value = _fake_post(None)
                self.native.post.return_value.iter_content.return_value = iter([
                    (f"event: response.{status}\ndata: {json.dumps(event)}\n\n").encode()])
                with patch.object(proxy, "_log_requests_enabled", True), \
                        patch.object(proxy.RequestFileLogger, "BASE_DIR", directory), \
                        patch.object(proxy, "log_token_usage") as usage:
                    result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True},
                                       headers={"Authorization": "Bearer " + TOKEN, "User-Agent": "codex_cli/0.164.0"})
                    self.assertIn("response." + status, result.get_data(as_text=True))
                    result.close()
                records = {int(path.name.split("_")[-3]): json.loads(path.read_text())
                           for path in Path(directory).rglob("*.json")}
                self.assertEqual(set(records), {1, 2, 3, 4})
                self.assertEqual(records[2], {"model": "gpt-6-astra", "input": "hi", "stream": True, "store": False})
                self.assertEqual(records[3], native_body(status))
                self.assertEqual(records[4]["status"], status)
                self.assertEqual(usage.call_args.args[:5], ("gpt-6-astra", "S" if status != "incomplete" else "T", 2, 3, 5))

    def test_logging_failure_does_not_break_json_or_stream(self):
        with patch.object(proxy.RequestFileLogger, "log", side_effect=OSError("logger failure")), \
                patch.object(proxy, "log_token_usage", side_effect=OSError("logger failure")):
            self.assertEqual(self.post().status_code, 200)
            event = {"type": "response.completed", "response": native_body()}
            self.native.post.return_value.iter_content.return_value = iter([
                (f"event: response.completed\ndata: {json.dumps(event)}\n\n").encode()])
            response = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
            self.assertIn("response.completed", response.get_data(as_text=True))
            response.close()

    def test_native_eof_logging_is_failed_with_available_usage(self):
        captured = []
        upstream = _fake_post(None)
        upstream.iter_content.return_value = iter([b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_partial","usage":{"input_tokens":3}}}\n\n'])
        self.native.post.return_value = upstream
        with patch.object(proxy.RequestFileLogger, "log", side_effect=lambda *args, **kwargs: captured.append(args)), \
                patch.object(proxy, "log_token_usage"):
            response = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
            self.assertIn("event: error", response.get_data(as_text=True))
            response.close()
        terminal = [args[4] for args in captured if args[2] == 4][0]
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["usage"]["input_tokens"], 3)

    def test_json_and_flat_stream_error_diagnostics_redact_secrets(self):
        body = native_body("failed")
        body["error"] = {"code": "model_error", "message": "token-S https://private-endpoint"}
        self.native.post.return_value = _fake_post(body)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token-S", response.get_data(as_text=True))
        self.assertNotIn("private-endpoint", response.get_data(as_text=True))
        self.assertEqual(response.get_json()["error"]["code"], "model_error")
        proxy.load_balance_url.counters = {}
        error = {"type": "error", "code": "model_error", "message": "token-S https://private-endpoint",
                 "param": "reasoning", "sequence_number": 7}
        self.native.post.return_value = _fake_post(None)
        self.native.post.return_value.iter_content.return_value = iter([
            (f"event: error\ndata: {json.dumps(error)}\n\n").encode()])
        response = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
        delivered = response.get_data(as_text=True)
        self.assertNotIn("token-S", delivered)
        self.assertNotIn("private-endpoint", delivered)
        payload = json.loads(delivered.split("data: ", 1)[1])
        self.assertEqual(payload["code"], "model_error")
        self.assertEqual(payload["sequence_number"], 7)
        response.close()

    def test_cr_only_sse_error_redaction_replaces_original_payload(self):
        body = native_body("failed")
        body["error"] = {"code": "model_error", "message": "token-S https://private-endpoint"}
        event = {"type": "response.failed", "response": body, "sequence_number": 8}
        self.native.post.return_value = _fake_post(None)
        self.native.post.return_value.iter_content.return_value = iter([
            (f"id: backend\revent: response.failed\rdata: {json.dumps(event)}\r\r").encode()])
        response = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
        delivered = response.get_data(as_text=True)
        self.assertNotIn("token-S", delivered)
        self.assertNotIn("private-endpoint", delivered)
        self.assertEqual(delivered.count("data:"), 1)
        self.assertIn("id: backend", delivered)
        response.close()

    def test_nested_diagnostics_are_sanitized_without_changing_response_data(self):
        secret_text = "token-S https://private-endpoint"
        safe_text = "[redacted] [upstream]"
        diagnostics = [
            (secret_text, safe_text),
            ({"code": "model_error", "message": {"causes": [secret_text, {"nested": TOKEN}]},
              "details": [{"endpoint": "https://private-endpoint/path", "credentials": ["token-S", TOKEN]},
                        {"message": [None, 42, False]},
                        {"urls": ["HTTPS://private-endpoint", "hTtPs://private-endpoint/path"]}]},
             {"code": "model_error", "message": {"causes": [safe_text, {"nested": "[redacted]"}]},
              "details": [{"endpoint": "[upstream]", "credentials": ["[redacted]", "[redacted]"]},
                          {"message": [None, 42, False]},
                          {"urls": ["[upstream]", "[upstream]"]}]}),
            ({"message": 42, "details": [[{"cause": secret_text}]]},
             {"message": 42, "details": [[{"cause": safe_text}]]}),
        ]
        for transport in ("json", "terminal", "error"):
            for diagnostic, expected in diagnostics:
                with self.subTest(transport=transport, diagnostic=diagnostic):
                    proxy.load_balance_url.counters = {}
                    body = native_body("failed")
                    body["error"] = copy.deepcopy(diagnostic)
                    body["output"] = [
                        {"type": "function_call_output", "call_id": "call_original", "output": secret_text},
                        {"type": "reasoning", "id": "rs_original", "encrypted_content": secret_text}]
                    original = copy.deepcopy(body)
                    upstream = _fake_post(body if transport == "json" else None)
                    if transport != "json":
                        event = ({"type": "response.failed", "response": body, "sequence_number": 8}
                                 if transport == "terminal" else {"type": "error", **body, "sequence_number": 8})
                        upstream.iter_content.return_value = iter([
                            (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()])
                    self.native.post.return_value = upstream
                    result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": transport != "json"})
                    self.assertEqual(result.status_code, 200)
                    if transport == "json":
                        delivered = result.get_json()
                    else:
                        delivered = json.loads(result.get_data(as_text=True).split("data: ", 1)[1])
                        self.assertEqual(delivered["sequence_number"], 8)
                        if transport == "terminal":
                            delivered = delivered["response"]
                    self.assertEqual(delivered["error"], expected)
                    for key in ("id", "status", "usage", "output"):
                        self.assertEqual(delivered[key], original[key])
                    result.close()
                    upstream.close.assert_called_once()

    def test_flat_sse_error_sanitizes_diagnostic_extensions_and_nested_messages(self):
        event = {"type": "error", "sequence_number": 9, "response_id": "resp_original",
                 "code": "model_error", "message": [None, {"cause": "token-S"}],
                 "details": [{"nested": {"url": "https://private-endpoint", "auth": TOKEN}}],
                 "vendor_diagnostic": {"trace": ["token-S", "HTTPS://private-endpoint"]}}
        upstream = _fake_post(None)
        upstream.iter_content.return_value = iter([(f"event: error\ndata: {json.dumps(event)}\n\n").encode()])
        self.native.post.return_value = upstream
        result = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
        delivered = json.loads(result.get_data(as_text=True).split("data: ", 1)[1])
        self.assertEqual(delivered, {**event, "message": [None, {"cause": "[redacted]"}],
                                    "details": [{"nested": {"url": "[upstream]", "auth": "[redacted]"}}],
                                    "vendor_diagnostic": {"trace": ["[redacted]", "[upstream]"]}})
        result.close()
        upstream.close.assert_called_once()

    def test_http_error_preserves_sanitized_nested_or_string_diagnostics(self):
        for diagnostic, expected in [
            ("token-S https://private-endpoint", {"type": "server_error", "message": "[redacted] [upstream]"}),
            ({"message": {"causes": [TOKEN]}, "details": [{"url": "hTtPs://private-endpoint"}]},
             {"type": "server_error", "message": {"causes": ["[redacted]"]},
              "details": [{"url": "[upstream]"}]}),
        ]:
            with self.subTest(diagnostic=diagnostic):
                proxy.load_balance_url.counters = {}
                upstream = _fake_post({"error": diagnostic}, 400)
                self.native.post.return_value = upstream
                result = self.post()
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.get_json()["error"], expected)
                upstream.close.assert_called_once()

    def test_sparse_upstream_error_retains_status_and_retry_after(self):
        upstream = _fake_post({"error": {"message": "busy"}}, 429)
        upstream.headers = {"Retry-After": "7"}
        self.native.post.return_value = upstream
        response = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "7")
        self.assertEqual(response.get_json()["error"], {"type": "server_error", "message": "busy"})
        upstream.close.assert_called_once()

    def test_unicode_json_line_separators_are_not_sse_line_endings(self):
        events = [
            {"type": "response.output_text.delta", "delta": "a\u2028b\u0085c\u2029d", "sequence_number": 1},
            {"type": "response.completed", "response": native_body(), "sequence_number": 2}]
        frames = "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
        self.native.post.return_value = _fake_post(None)
        self.native.post.return_value.iter_content.return_value = iter([frames.encode()])
        response = self.post({"model": "gpt-6-astra", "input": "hi", "stream": True})
        self.assertEqual(response.get_data(as_text=True), frames)
        response.close()

    def test_dedicated_lite_header_rejected_before_inference(self):
        self.reset_upstream_calls()
        response = self.post(headers={"Authorization": "Bearer " + TOKEN, "X-OpenAI-Responses-Lite": "true"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["param"], "X-OpenAI-Responses-Lite")
        self.assert_no_upstream_calls()

    def test_malformed_item_and_tool_choice_types_return_field_specific_400(self):
        for value in ([], {}):
            for extra, param in (({"input": [{"type": value}]}, "input[0].type"),
                                 ({"tool_choice": {"type": value}}, "tool_choice")):
                with self.subTest(extra=extra):
                    self.reset_upstream_calls()
                    response = self.post({"model": "gpt-6-astra", "input": "hi", **extra})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json()["error"]["param"], param)
                    self.assert_no_upstream_calls()
